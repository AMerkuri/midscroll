# midscroll

Windows-style middle-button drag autoscroll for Linux.

Hold the middle mouse button and drag. The page scrolls in that direction,
faster the farther you drag from where you pressed, like Windows 10/11.
Release to stop. A quick middle click without dragging still works as a
normal middle click (paste, open link in new tab). Diagonal drags scroll
both axes, so it pans wide pages too.

It works in every app, on Wayland and X11, because it operates at the
kernel input layer (evdev in, uinput out) instead of hooking any
particular desktop or toolkit.

Details:

- The speed curve is Chromium/Edge's actual Windows autoscroll formula
  (`0.000008 * distance^2.2` px/ms, 15 px per-axis dead zone): tiny drags
  crawl, a full-screen drag flies.
- While scrolling, the cursor stays anchored at the press point, so the
  scroll stays locked to the window you started in. Dragging "over" the
  taskbar or another window can't steal it.
- While a drag-scroll is active, a small badge with a vertical-arrows icon
  appears at the anchored cursor (KDE Plasma Wayland; see notes below).

## Install

### Fedora / RPM distros

```
./packaging/fedora/build-rpm.sh
sudo dnf install ./dist/midscroll-*.noarch.rpm
```

### Debian / Ubuntu

```
./packaging/debian/build-deb.sh
sudo apt install ./dist/midscroll_*_all.deb
```

Needs a release with gtk4-layer-shell packaged (Debian 13 "trixie",
Ubuntu 24.04+). kdotool isn't in the Debian/Ubuntu repos, so the scroll
badge needs it [built from source](https://github.com/jinliu/kdotool);
everything else works without it.

### Arch

```
cd packaging/arch
makepkg -si
```

kdotool (for the scroll badge) is in the AUR.

### Manual (any systemd distro)

`sudo ./install.sh` copies the files and enables the services directly;
`sudo ./uninstall.sh` reverses it. You'll need the dependencies installed:
python3-evdev, and for the badge overlay PyGObject, GTK 4,
gtk4-layer-shell, librsvg and kdotool.

All methods install two services: `midscroll` (system, the scroll daemon)
and `midscroll-overlay` (per-user, the badge). Package installs enable
both; the overlay starts at your next login, or immediately with
`systemctl --user start midscroll-overlay`.

## Tuning

Edit `/etc/midscroll.conf`, then `sudo systemctl restart midscroll`:

```
DEADZONE_PX = 15          # per-axis dead zone in pixels
SPEED_MULT = 0.008        # overall speed (bigger = faster everywhere)
SPEED_EXP = 2.2           # curve shape (bigger = more extreme at long drags)
PX_PER_NOTCH = 55         # px one wheel notch scrolls in your apps
TICK_HZ = 90              # scroll event rate (higher = smoother)
NATURAL = false           # true = inverted / touchscreen-style direction
```

## Pause / uninstall

```
sudo systemctl stop midscroll     # pause (e.g. for Blender/CAD, which use
sudo systemctl start midscroll    #   middle-drag themselves)
sudo dnf remove midscroll         # or apt remove / pacman -R
```

## Notes

- The scroll badge is KDE-specific: it reads the cursor position through
  kdotool, which uses KWin's scripting API, and draws via wlr-layer-shell.
  On other desktops the daemon still scrolls fine; there's just no badge.
  (Wayland doesn't let a background process change the real pointer image,
  so the badge is drawn at the anchored cursor instead, which looks the
  same since the cursor doesn't move during a drag.)
- If Firefox's built-in autoscroll is enabled (`general.autoScroll` in
  about:config), turn it off so the two don't fight. It's off by default
  on Linux.
- Why not a flatpak: the sandbox forbids the raw input-device and uinput
  access this needs, and flatpaks can't run boot-time services.
- Logs: `journalctl -u midscroll -f` and
  `journalctl --user -u midscroll-overlay -f`.

## License

Public domain ([the Unlicense](https://unlicense.org)): use, copy, modify,
sell or distribute freely. The badge icon is
[move-vertical](https://lucide.dev/icons/move-vertical) from Lucide
(ISC license).
