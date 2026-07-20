#!/usr/bin/env python3
"""midscroll-overlay - session helper for midscroll's middle-drag autoscroll.

Two jobs, both over the daemon's socket at /run/midscroll/state.sock:

1. While a drag-scroll is active, shows a small badge with a
   vertical-arrows icon at the point where midscroll has anchored the
   cursor, like the autoscroll icon on Windows (Wayland only; the badge
   uses wlr-layer-shell).
2. Reports the focused window's class back to the daemon so it can pause
   itself over blacklisted apps (CAD, slicers, games that use middle-drag
   natively). Polled once a second via kdotool on Wayland KDE, or xprop
   on X11 - both tiny, ubiquitous tools, so no new library dependencies.

The badge sits on the compositor's overlay layer with an empty input
region and no keyboard interactivity, so clicks, scrolling and focus pass
straight through it. The cursor position is read once per drag via kdotool
(KWin scripting); midscroll pins the cursor for the whole drag, so one
query is enough. Without kdotool the badge is skipped and scrolling is
unaffected. On X11 sessions there is no badge, but focus reporting still
runs.
"""

import ctypes.util
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time

log = logging.getLogger("midscroll-overlay")

WAYLAND = bool(os.environ.get("WAYLAND_DISPLAY"))

if WAYLAND:
    # gtk4-layer-shell must be loaded before libwayland-client, which a
    # Python process can only guarantee via LD_PRELOAD; re-exec once with
    # it set.
    _LS = ctypes.util.find_library("gtk4-layer-shell")
    if _LS and _LS not in os.environ.get("LD_PRELOAD", ""):
        os.environ["LD_PRELOAD"] = (_LS + " "
                                    + os.environ.get("LD_PRELOAD", "")).strip()
        os.execv(sys.executable, [sys.executable] + sys.argv)

    import cairo
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    gi.require_version("Gtk4LayerShell", "1.0")
    from gi.repository import Gdk, Gio, GLib, Gtk
    from gi.repository import Gtk4LayerShell as LayerShell

SOCK_PATH = "/run/midscroll/state.sock"
ICON_PATH = "/usr/share/midscroll/move-vertical.svg"
BADGE_PX = 42   # badge diameter
ICON_PX = 24    # icon size inside the badge
FOCUS_POLL_SEC = 1.0

FOCUS_TOOL = shutil.which("kdotool") if WAYLAND else shutil.which("xprop")

CSS = """
window { background: transparent; }
.badge {
    background-color: rgba(30, 30, 32, 0.82);
    border: 1px solid rgba(255, 255, 255, 0.28);
    border-radius: 9999px;
}
"""


def active_window_class():
    """Class of the focused window, or "" if it can't be determined."""
    try:
        if WAYLAND:
            # KDE only: kdotool drives KWin's scripting API.
            out = subprocess.run(
                ["kdotool", "getactivewindow", "getwindowclassname"],
                capture_output=True, text=True, timeout=5)
            return out.stdout.strip() if out.returncode == 0 else ""
        out = subprocess.run(["xprop", "-root", "_NET_ACTIVE_WINDOW"],
                             capture_output=True, text=True, timeout=5)
        m = re.search(r"window id # (0x[0-9a-fA-F]+)", out.stdout)
        if not m:
            return ""
        out = subprocess.run(["xprop", "-id", m.group(1), "WM_CLASS"],
                             capture_output=True, text=True, timeout=5)
        # WM_CLASS(STRING) = "instance", "Class"; report both so the
        # daemon's substring match sees whichever casing the app uses.
        m = re.search(r'=\s*"([^"]*)",\s*"([^"]*)"', out.stdout)
        return f"{m.group(1)} {m.group(2)}" if m else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def report_focus(sock, stop):
    """Push focus changes to the daemon until the connection dies."""
    if not FOCUS_TOOL:
        log.warning("%s not found; app blacklist will be inactive",
                    "kdotool" if WAYLAND else "xprop")
        return
    last = None
    while not stop.is_set():
        cls = active_window_class()
        if cls != last:
            last = cls
            try:
                sock.sendall(b"focus " + cls.encode("utf-8", "replace")
                             + b"\n")
            except OSError:
                return
        stop.wait(FOCUS_POLL_SEC)


if WAYLAND:
    class Overlay:
        def __init__(self, app):
            self.win = Gtk.Window(application=app)
            LayerShell.init_for_window(self.win)
            LayerShell.set_layer(self.win, LayerShell.Layer.OVERLAY)
            LayerShell.set_namespace(self.win, "midscroll")
            LayerShell.set_keyboard_mode(self.win,
                                         LayerShell.KeyboardMode.NONE)
            LayerShell.set_exclusive_zone(self.win, -1)
            LayerShell.set_anchor(self.win, LayerShell.Edge.TOP, True)
            LayerShell.set_anchor(self.win, LayerShell.Edge.LEFT, True)

            icon = Gtk.Image.new_from_file(ICON_PATH)
            icon.set_pixel_size(ICON_PX)
            icon.set_halign(Gtk.Align.CENTER)
            icon.set_valign(Gtk.Align.CENTER)
            icon.set_hexpand(True)
            icon.set_vexpand(True)
            badge = Gtk.Box()
            badge.add_css_class("badge")
            badge.set_size_request(BADGE_PX, BADGE_PX)
            badge.append(icon)
            self.win.set_child(badge)
            self.win.set_default_size(BADGE_PX, BADGE_PX)

            css = Gtk.CssProvider()
            css.load_from_string(CSS)
            Gtk.StyleContext.add_provider_for_display(
                Gdk.Display.get_default(), css,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

            self.win.connect("realize", self._make_click_through)
            self.active = False
            self.seq = 0  # discards stale position queries

        def _make_click_through(self, *_):
            self.win.get_surface().set_input_region(cairo.Region())

        def set_active(self, active):
            if active == self.active:
                return
            self.active = active
            self.seq += 1
            if not active:
                self.win.set_visible(False)
                return
            # Cursor is anchored for the whole drag, so one query suffices.
            try:
                proc = Gio.Subprocess.new(
                    ["kdotool", "getmouselocation", "--shell"],
                    Gio.SubprocessFlags.STDOUT_PIPE
                    | Gio.SubprocessFlags.STDERR_SILENCE)
            except GLib.Error:
                log.warning("kdotool not available; no badge will be shown")
                return
            proc.communicate_utf8_async(None, None, self._got_pos, self.seq)

        def _got_pos(self, proc, res, seq):
            try:
                _ok, out, _err = proc.communicate_utf8_finish(res)
            except GLib.Error:
                return
            if seq != self.seq or not self.active:
                return  # the drag already ended
            mx = re.search(r"x[=:](-?\d+)", out, re.I)
            my = re.search(r"y[=:](-?\d+)", out, re.I)
            if not (mx and my):
                log.warning("bad kdotool output: %r", out)
                return
            self._place(int(mx.group(1)), int(my.group(1)))
            self.win.set_visible(True)

        def _place(self, x, y):
            # Layer-shell margins are relative to one output; find the
            # monitor containing the (global) cursor position and convert.
            monitors = Gdk.Display.get_default().get_monitors()
            for i in range(monitors.get_n_items()):
                mon = monitors.get_item(i)
                geo = mon.get_geometry()
                if (geo.x <= x < geo.x + geo.width
                        and geo.y <= y < geo.y + geo.height):
                    LayerShell.set_monitor(self.win, mon)
                    x -= geo.x
                    y -= geo.y
                    break
            LayerShell.set_margin(self.win, LayerShell.Edge.LEFT,
                                  max(0, x - BADGE_PX // 2))
            LayerShell.set_margin(self.win, LayerShell.Edge.TOP,
                                  max(0, y - BADGE_PX // 2))


def watch_socket(overlay):
    """Follow the daemon's state socket, reconnecting if it goes away.

    Each connection also gets a thread pushing focus reports back up it.
    """
    while True:
        stop = threading.Event()
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.connect(SOCK_PATH)
                threading.Thread(target=report_focus, args=(s, stop),
                                 daemon=True).start()
                for line in s.makefile("r"):
                    if overlay:
                        GLib.idle_add(overlay.set_active,
                                      line.strip() == "1")
        except OSError:
            pass
        finally:
            stop.set()
        if overlay:
            GLib.idle_add(overlay.set_active, False)
        time.sleep(2)


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(levelname)s: %(message)s")
    if not WAYLAND:
        # X11: the layer-shell badge can't work; still report focus so the
        # daemon's app blacklist functions.
        log.info("X11 session: focus reporting only, no badge")
        watch_socket(None)
        return

    app = Gtk.Application(application_id="org.midscroll.overlay",
                          flags=Gio.ApplicationFlags.NON_UNIQUE)

    def activate(app):
        overlay = Overlay(app)
        threading.Thread(target=watch_socket, args=(overlay,),
                         daemon=True).start()

    app.connect("activate", activate)
    app.hold()  # stay alive while the badge is hidden
    app.run()


if __name__ == "__main__":
    main()
