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

Tunables can be overridden in /etc/midscroll.conf (KEY = value lines).
"""

import asyncio
import math
import os

from evdev import InputDevice, UInput, ecodes as e, list_devices

# ---- Tuning (override in /etc/midscroll.conf) -----------------------------
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

HIRES_PER_LINE = 120      # kernel convention: 120 hi-res units per notch
VIRTUAL_NAME = "midscroll virtual mouse"
CONFIG_PATH = "/etc/midscroll.conf"
SOCK_DIR = "/run/midscroll"
SOCK_PATH = SOCK_DIR + "/state.sock"

FLOAT_KEYS = {"DEADZONE_PX", "TICK_HZ", "SPEED_MULT", "SPEED_EXP",
              "MAX_PX_PER_SEC", "PX_PER_NOTCH", "MAX_DRAG_PX"}


def load_config(path=CONFIG_PATH):
    global NATURAL
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
                globals()[k] = float(v)
            except ValueError:
                print(f"midscroll: bad value for {k}: {v!r}", flush=True)
        elif k == "NATURAL":
            NATURAL = v.lower() in ("1", "true", "yes", "on")


class Notifier:
    """Tells overlay helpers when a drag-scroll is active.

    Serves a unix socket that session helpers (midscroll-overlay) connect
    to; sends b"1\\n" when scrolling starts and b"0\\n" when it stops (and
    the current state on connect). Purely advisory: scrolling works fine
    with no listeners, and a failure to bind the socket is non-fatal.
    """

    def __init__(self):
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
            print(f"midscroll: overlay socket unavailable: {err}", flush=True)

    async def _client(self, reader, writer):
        self.writers.add(writer)
        try:
            writer.write(self.msg)
            await reader.read()  # block until the client disconnects
        except (OSError, ConnectionError):
            pass
        finally:
            self.writers.discard(writer)
            writer.close()

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
    """One shared scroll session across all grabbed mice."""

    def __init__(self, notifier):
        self.notifier = notifier
        self.reset()

    def reset(self):
        self.pending = False   # middle held, deadzone not yet exceeded
        self.active = False    # scrolling
        self.dx = 0.0          # cursor offset from the press point
        self.dy = 0.0
        self.acc_v = 0.0       # fractional hi-res units carried between ticks
        self.acc_h = 0.0
        self.notch_v = 0.0     # hi-res units accumulated toward a full notch
        self.notch_h = 0.0
        self.notifier.set(False)

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


async def ticker(ui, st):
    while True:
        await asyncio.sleep(1.0 / TICK_HZ)
        try:
            tick(ui, st)
        except Exception as err:  # one bad tick must not kill the loop
            print(f"midscroll: tick error: {err!r}", flush=True)


def tick(ui, st):
    if st.pending and (abs(st.dx) > DEADZONE_PX or abs(st.dy) > DEADZONE_PX):
        st.pending = False
        st.active = True
        st.notifier.set(True)
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


async def pump(path, dev, ui, st, tasks):
    """Grab one mouse and forward its events, intercepting middle-drags."""
    try:
        dev.grab()
    except OSError as err:
        print(f"midscroll: cannot grab {path}: {err}", flush=True)
        dev.close()
        tasks.pop(path, None)
        return
    print(f"midscroll: grabbed {dev.name} ({path})", flush=True)
    try:
        async for ev in dev.async_read_loop():
            if ev.type == e.EV_KEY and ev.code == e.BTN_MIDDLE:
                if ev.value == 1:
                    st.press()
                elif ev.value == 0 and st.release():
                    # No drag happened: replay it as a normal middle click.
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
                    # release was among them, end the scroll so it can't
                    # run away.
                    if ((st.pending or st.active)
                            and e.BTN_MIDDLE not in dev.active_keys()):
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
        tasks.pop(path, None)
        print(f"midscroll: released {path}", flush=True)


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
    load_config()
    ui = UInput(
        {
            # All possible mouse button codes (0x110-0x11f).
            e.EV_KEY: list(range(e.BTN_MOUSE, e.BTN_JOYSTICK)),
            e.EV_REL: [e.REL_X, e.REL_Y, e.REL_WHEEL, e.REL_HWHEEL,
                       e.REL_WHEEL_HI_RES, e.REL_HWHEEL_HI_RES],
        },
        name=VIRTUAL_NAME,
    )
    notifier = Notifier()
    await notifier.start()
    st = State(notifier)
    tasks = {}
    seen = set()
    tick_task = asyncio.ensure_future(ticker(ui, st))
    print("midscroll: running", flush=True)
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
                    tasks[path] = asyncio.ensure_future(
                        pump(path, dev, ui, st, tasks))
                else:
                    dev.close()
            await asyncio.sleep(2)
    finally:
        tick_task.cancel()
        ui.close()


if __name__ == "__main__":
    asyncio.run(main())
