#!/usr/bin/env bash
set -euo pipefail

if [ "$EUID" -ne 0 ]; then
    exec sudo "$0" "$@"
fi

if [ -n "${SUDO_USER:-}" ]; then
    uid=$(id -u "$SUDO_USER")
    sudo -u "$SUDO_USER" XDG_RUNTIME_DIR="/run/user/$uid" \
        systemctl --user stop midscroll-overlay.service || true
fi
systemctl --global disable midscroll-overlay.service || true
systemctl disable --now midscroll.service || true
rm -f /etc/systemd/system/midscroll.service /usr/bin/midscroll /etc/midscroll.conf
rm -f /usr/lib/systemd/user/midscroll-overlay.service /usr/bin/midscroll-overlay
rm -rf /usr/share/midscroll
systemctl daemon-reload

echo "midscroll removed."
