#!/usr/bin/env bash
# Build the RPM into dist/. Needs rpm-build and systemd-rpm-macros:
#   sudo dnf install rpm-build systemd-rpm-macros
set -euo pipefail
cd "$(dirname "$0")"
repo=$(cd ../.. && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT

# rpmbuild wants flat sources
cp "$repo"/midscroll.py "$repo"/midscroll-overlay.py "$repo"/midscroll.conf \
   "$repo"/midscroll-settings.py "$repo"/midscroll-apply.py \
   "$repo"/midscroll-settings.desktop \
   "$repo"/README.md "$repo"/LICENSE \
   "$repo"/systemd/midscroll.service "$repo"/systemd/midscroll-overlay.service \
   "$repo"/icons/move-vertical.svg "$work"/

mkdir -p "$repo/dist"
rpmbuild -bb \
    --define "_sourcedir $work" \
    --define "_rpmdir $work/out" \
    midscroll.spec
cp "$work"/out/noarch/*.rpm "$repo/dist/"
echo
echo "Built: $(ls "$repo"/dist/*.rpm)"
