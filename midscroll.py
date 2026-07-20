#!/usr/bin/env python3
"""midscroll - Windows-style middle-button drag autoscroll for Linux.

Hold the middle mouse button and drag: the page scrolls in that direction,
and the farther you drag from the point where you pressed, the faster it
scrolls. Release to stop. A quick middle click without dragging passes
through as a normal middle click (paste / open link in new tab).

Works on Wayland and X11 in any application, because it sits at the kernel
input layer: it grabs the real mouse and re-emits its events through a
uinput virtual mouse, injecting high-resolution wheel events while a
middle-drag is active.

Apps that use middle-drag themselves (CAD, slicers, games) can be
blacklisted by window class; while one of them is focused, the middle
button passes straight through. The focused window's class is reported by
the session helper (midscroll-overlay) over the state socket.

Tunables can be overridden in /etc/midscroll.conf (KEY = value lines) or
per run on the command line: midscroll --help.
"""

import argparse
import asyncio
import logging
import math
import os

from evdev import InputDevice, UInput, ecodes as e, list_devices

VERSION = "1.4"

log = logging.getLogger("midscroll")

# ---- Tuning (override in /etc/midscroll.conf or via CLI) ------------------
# Speed curve from Chromium/Edge's AutoscrollController:
#   velocity_px_per_sec = SPEED_MULT * |offset_px| ^ SPEED_EXP   (per axis)
# with a 15 px per-axis dead zone. Chromium uses 0.000008 * d^2.2 in px/ms;
# SPEED_MULT is that times 1000.
DEADZONE_PX = 15.0        # per-axis dead zone, as in Chromium
SPEED_MULT = 0.008        # px/sec multiplier, overall speed
SPEED_EXP = 2.2           # Chromium's exponent: slow near, fast far
MAX_PX_PER_SEC = 30000.0  # safety cap on scroll speed
PX_PER_NOTCH = 55.0       # how many px one wheel notch scrolls in your apps
MAX_DRAG_PX = 1200.0      # cap on effective drag distance (~screen height)
TICK_HZ = 90.0            # scroll event rate (higher = smoother)
NATURAL = False           # True inverts scroll direction

# Window-class substrings (case-insensitive) over which midscroll pauses
# and the middle button behaves natively.
BLACKLIST = ["freecad", "orcaslicer", "minecraft"]

HIRES_PER_LINE = 120      # kernel convention: 120 hi-res units per notch
VIRTUAL_NAME = "midscroll virtual mouse"
CONFIG_PATH = "/etc/midscroll.conf"
SOCK_DIR = "/run/midscroll"
SOCK_PATH = SOCK_DIR + "/state.sock"

FLOAT_KEYS = {"DEADZONE_PX", "TICK_HZ", "SPEED_MULT", "SPEED_EXP",
              "MAX_PX_PER_SEC", "PX_PER_NOTCH", "MAX_DRAG_PX"}
# Zero would mean a division by zero (TICK_HZ, PX_PER_NOTCH) or a daemon
# that silently never scrolls; only the dead zone may be zero.
POSITIVE_KEYS = FLOAT_KEYS - {"DEADZONE_PX"}


def validate(key, val):
    """Returns an error string if val is out of bounds for key, else None."""
    if not math.isfinite(val):
        return "must be a finite number"
    if key in POSITIVE_KEYS and val <= 0:
        return "must be strictly greater than zero"
    if val < 0:
        return "must not be negative"
    return None


def parse_blacklist(text):
    return [p.strip().lower() for p in text.split(",") if p.strip()]


def load_config(path=CONFIG_PATH):
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.split("#", 1)[0].strip()
        if "=" not in line:
            continue
        k, v = (p.strip() for p in line.split("=", 1))
        if k in FLOAT_KEYS:
            try:
                val = float(v)
            except ValueError:
                log.error("config: bad value for %s: %r (keeping %g)",
                          k, v, globals()[k])
                continue
            err = validate(k, val)
            if err:
                log.error("config: %s = %g rejected: %s (keeping %g)",
                          k, val, err, globals()[k])
                continue
            globals()[k] = val
        elif k == "NATURAL":
            globals()["NATURAL"] = v.lower() in ("1", "true", "yes", "on")
        elif k == "BLACKLIST":
            globals()["BLACKLIST"] = parse_blacklist(v)
        else:
            log.warning("config: unknown key %s", k)


class FocusFilter:
    """Latest focused-window class, as reported by the session helper.

    The root daemon cannot see session windows itself; midscroll-overlay
    polls the compositor (kdotool on KDE, xprop on X11) and pushes
    "focus <class>" lines over the state socket. With no helper connected
    the class is empty and nothing is ever blocked.
    """

    def __init__(self):
        self.wclass = ""

    def update(self, wclass):
        if wclass != self.wclass:
            self.wclass = wclass
            log.debug("focus: %r%s", wclass,
                      " (blacklisted)" if self.blocked else "")

    @property
    def blocked(self):
        c = self.wclass.lower()
        return any(b in c for b in BLACKLIST)


class Notifier:
    """State socket shared with session helpers (midscroll-overlay).

    Sends b"1\\n" when a drag-scroll starts and b"0\\n" when it stops (and
    the current state on connect) so the helper can draw the badge; reads
    "focus <window class>" lines back from the helper to drive the
    blacklist. Purely advisory: scrolling works fine with no listeners,
    and a failure to bind the socket is non-fatal.
    """

    def __init__(self, focus):
        self.focus = focus
        self.writers = set()
        self.msg = b"0\n"
        self.server = None

    async def start(self):
        try:
            os.makedirs(SOCK_DIR, exist_ok=True)
            try:
                os.unlink(SOCK_PATH)
            except FileNotFoundError:
                pass
            self.server = await asyncio.start_unix_server(
                self._client, SOCK_PATH)
            os.chmod(SOCK_PATH, 0o666)  # any session user may listen
        except OSError as err:
            log.warning("overlay socket unavailable: %s", err)

    async def _client(self, reader, writer):
        self.writers.add(writer)
        try:
            writer.write(self.msg)
            while True:
                line = await reader.readline()
                if not line:
                    break
                line = line.decode("utf-8", "replace").strip()
                if line.startswith("focus "):
                    self.focus.update(line[len("focus "):])
        except (OSError, ConnectionError, ValueError):
            pass
        finally:
            self.writers.discard(writer)
            writer.close()
            self.focus.update("")  # helper gone; its last report is stale

    def set(self, active):
        msg = b"1\n" if active else b"0\n"
        if msg == self.msg:
            return
        self.msg = msg
        for w in list(self.writers):
            try:
                w.write(msg)
            except (OSError, ConnectionError):
                self.writers.discard(w)


class State:
    """Scroll session for one grabbed mouse."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.pending = False      # middle held, deadzone not yet exceeded
        self.active = False       # scrolling
        self.passthrough = False  # middle held over a blacklisted app
        self.dx = 0.0             # cursor offset from the press point
        self.dy = 0.0
        self.acc_v = 0.0          # fractional hi-res units carried over
        self.acc_h = 0.0
        self.notch_v = 0.0        # hi-res units accumulated toward a notch
        self.notch_h = 0.0

    def press(self):
        self.reset()
        self.pending = True

    def release(self):
        """Returns True if this was a plain click (no drag)."""
        was_click = self.pending
        self.reset()
        return was_click


def _clamp(v):
    # The cursor is anchored during a drag, so nothing naturally bounds the
    # drag distance; cap it like Windows caps the cursor at the screen edge,
    # so slowing back down never requires a huge reverse drag.
    return math.copysign(min(abs(v), MAX_DRAG_PX), v)


def speed_px(offset):
    """Pixels/second for a per-axis offset from the press point.

    Components inside the dead zone are zeroed; outside it velocity grows
    as offset^2.2, so small drags crawl and full-screen drags fly.
    """
    if abs(offset) <= DEADZONE_PX:
        return 0.0
    v = SPEED_MULT * abs(offset) ** SPEED_EXP
    return math.copysign(min(v, MAX_PX_PER_SEC), offset)


async def ticker(ui, states, notifier):
    while True:
        await asyncio.sleep(1.0 / TICK_HZ)
        for st in list(states.values()):
            try:
                tick(ui, st)
            except Exception as err:  # one bad tick must not kill the loop
                log.error("tick error: %r", err)
        notifier.set(any(st.active for st in states.values()))


def tick(ui, st):
    if st.pending and (abs(st.dx) > DEADZONE_PX or abs(st.dy) > DEADZONE_PX):
        st.pending = False
        st.active = True
        log.debug("scroll started")
    if not st.active:
        return
    dt = 1.0 / TICK_HZ
    s = 1 if NATURAL else -1
    to_hires = HIRES_PER_LINE / PX_PER_NOTCH
    # Drag down => wheel-down (negative REL_WHEEL); drag right => positive
    # REL_HWHEEL. Both axes at once allows diagonal panning.
    st.acc_v += s * speed_px(st.dy) * to_hires * dt
    st.acc_h += -s * speed_px(st.dx) * to_hires * dt
    wrote = False
    iv = int(st.acc_v)
    if iv:
        st.acc_v -= iv
        st.notch_v += iv
        ui.write(e.EV_REL, e.REL_WHEEL_HI_RES, iv)
        n = int(st.notch_v / HIRES_PER_LINE)
        if n:
            st.notch_v -= n * HIRES_PER_LINE
            ui.write(e.EV_REL, e.REL_WHEEL, n)
        wrote = True
    ih = int(st.acc_h)
    if ih:
        st.acc_h -= ih
        st.notch_h += ih
        ui.write(e.EV_REL, e.REL_HWHEEL_HI_RES, ih)
        n = int(st.notch_h / HIRES_PER_LINE)
        if n:
            st.notch_h -= n * HIRES_PER_LINE
            ui.write(e.EV_REL, e.REL_HWHEEL, n)
        wrote = True
    if wrote:
        ui.syn()


async def pump(path, dev, ui, states, tasks, focus):
    """Grab one mouse and forward its events, intercepting middle-drags."""
    try:
        dev.grab()
    except OSError as err:
        log.warning("cannot grab %s: %s", path, err)
        dev.close()
        tasks.pop(path, None)
        return
    st = states[path] = State()
    log.info("grabbed %s (%s)", dev.name, path)
    try:
        async for ev in dev.async_read_loop():
            if ev.type == e.EV_KEY and ev.code == e.BTN_MIDDLE:
                if ev.value == 1:
                    if focus.blocked:
                        # A blacklisted app owns middle-drag; hand the
                        # button straight through, held state and all.
                        st.passthrough = True
                        log.debug("middle press passed through (%r focused)",
                                  focus.wclass)
                        ui.write(e.EV_KEY, e.BTN_MIDDLE, 1)
                    else:
                        st.press()
                elif ev.value == 0:
                    if st.passthrough:
                        st.passthrough = False
                        ui.write(e.EV_KEY, e.BTN_MIDDLE, 0)
                    elif st.release():
                        # No drag happened: replay as a normal middle click.
                        ui.write(e.EV_KEY, e.BTN_MIDDLE, 1)
                        ui.syn()
                        ui.write(e.EV_KEY, e.BTN_MIDDLE, 0)
                        ui.syn()
                continue
            if (ev.type == e.EV_REL and (st.pending or st.active)
                    and ev.code in (e.REL_X, e.REL_Y)):
                # Swallow cursor motion while the middle button is held:
                # the pointer stays anchored at the press point, so scroll
                # events keep hitting the original window instead of
                # whatever the cursor would have drifted over (taskbar,
                # browser tabs, ...), same lock-to-target as Windows.
                if ev.code == e.REL_X:
                    st.dx = _clamp(st.dx + ev.value)
                else:
                    st.dy = _clamp(st.dy + ev.value)
                continue
            if ev.type == e.EV_SYN:
                if ev.code == e.SYN_DROPPED:
                    # Kernel dropped events on us. If the middle-button
                    # release was among them, end the scroll (or the
                    # passthrough hold) so it can't run away.
                    if e.BTN_MIDDLE not in dev.active_keys():
                        if st.passthrough:
                            st.passthrough = False
                            ui.write(e.EV_KEY, e.BTN_MIDDLE, 0)
                            ui.syn()
                        elif st.pending or st.active:
                            st.release()
                else:
                    ui.syn()
            elif ev.type in (e.EV_KEY, e.EV_REL):
                ui.write(ev.type, ev.code, ev.value)
    except OSError:
        pass  # device unplugged
    finally:
        st.reset()
        try:
            dev.close()
        except OSError:
            pass
        states.pop(path, None)
        tasks.pop(path, None)
        log.info("released %s", path)


def is_mouse(dev):
    if "midscroll" in dev.name.lower():
        return False
    caps = dev.capabilities()
    # Plain relative mice only: middle button + relative X, and no absolute
    # axes (excludes touchpads, tablets, touchscreens).
    return (e.BTN_MIDDLE in caps.get(e.EV_KEY, ())
            and e.REL_X in caps.get(e.EV_REL, ())
            and e.EV_ABS not in caps)


async def main():
    ui = UInput(
        {
            # All possible mouse button codes (0x110-0x11f).
            e.EV_KEY: list(range(e.BTN_MOUSE, e.BTN_JOYSTICK)),
            e.EV_REL: [e.REL_X, e.REL_Y, e.REL_WHEEL, e.REL_HWHEEL,
                       e.REL_WHEEL_HI_RES, e.REL_HWHEEL_HI_RES],
        },
        name=VIRTUAL_NAME,
    )
    focus = FocusFilter()
    notifier = Notifier(focus)
    await notifier.start()
    states = {}
    tasks = {}
    seen = set()
    tick_task = asyncio.create_task(ticker(ui, states, notifier))
    log.info("running")
    try:
        while True:
            # Hotplug: probe only paths we have never examined. Non-mouse
            # devices are remembered and never reopened (repeatedly opening
            # every input device caused visible input hiccups); a path is
            # forgotten when it disappears, so replugging re-probes it.
            paths = set(list_devices())
            seen &= paths
            for path in sorted(paths - seen):
                seen.add(path)
                try:
                    dev = InputDevice(path)
                except OSError:
                    continue
                if is_mouse(dev):
                    tasks[path] = asyncio.create_task(
                        pump(path, dev, ui, states, tasks, focus))
                else:
                    log.debug("ignoring %s (%s)", dev.name, path)
                    dev.close()
            await asyncio.sleep(2)
    finally:
        tick_task.cancel()
        ui.close()


def _float_arg(key):
    def parse(text):
        try:
            val = float(text)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{text!r} is not a number")
        err = validate(key, val)
        if err:
            raise argparse.ArgumentTypeError(f"{key} {err}")
        return val
    return parse


# CLI flag -> (config key, help text); dest is the key lowercased.
CLI_FLOATS = {
    "--deadzone-px": ("DEADZONE_PX", "per-axis dead zone in pixels"),
    "--speed-mult": ("SPEED_MULT", "overall speed multiplier"),
    "--speed-exp": ("SPEED_EXP", "speed curve exponent"),
    "--max-px-per-sec": ("MAX_PX_PER_SEC", "scroll speed safety cap"),
    "--px-per-notch": ("PX_PER_NOTCH",
                       "pixels one wheel notch scrolls in your apps"),
    "--max-drag-px": ("MAX_DRAG_PX", "cap on effective drag distance"),
    "--tick-hz": ("TICK_HZ", "scroll event rate"),
}


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog="midscroll",
        description="Windows-style middle-button drag autoscroll daemon.",
        epilog=f"Defaults come from {CONFIG_PATH}; command-line options "
               "override it for this run.")
    p.add_argument("--version", action="version",
                   version=f"midscroll {VERSION}")
    p.add_argument("--config", default=CONFIG_PATH, metavar="PATH",
                   help=f"config file to read (default: {CONFIG_PATH})")
    p.add_argument("--debug", action="store_true",
                   help="log debug detail (device probing, focus changes, "
                        "scroll starts)")
    p.add_argument("--natural", action=argparse.BooleanOptionalAction,
                   default=None, help="invert the scroll direction")
    p.add_argument("--blacklist", metavar="APPS", default=None,
                   help="comma-separated window-class substrings over which "
                        "midscroll pauses (default: "
                        f"\"{', '.join(BLACKLIST)}\"; pass '' to disable)")
    for flag, (key, help_text) in CLI_FLOATS.items():
        p.add_argument(flag, dest=key.lower(), type=_float_arg(key),
                       default=None, metavar="N",
                       help=f"{help_text} (default: {globals()[key]:g})")
    return p.parse_args(argv)


def cli():
    args = parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(levelname)s: %(message)s")
    load_config(args.config)
    for key in FLOAT_KEYS:
        val = getattr(args, key.lower())
        if val is not None:
            globals()[key] = val
    if args.natural is not None:
        globals()["NATURAL"] = args.natural
    if args.blacklist is not None:
        globals()["BLACKLIST"] = parse_blacklist(args.blacklist)
    log.debug("tunables: %s NATURAL=%s BLACKLIST=%s",
              " ".join(f"{k}={globals()[k]:g}" for k in sorted(FLOAT_KEYS)),
              NATURAL, BLACKLIST)
    asyncio.run(main())


if __name__ == "__main__":
    cli()
