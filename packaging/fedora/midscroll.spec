Name:           midscroll
Version:        1.6
Release:        1%{?dist}
Summary:        Windows-style middle-button drag autoscroll
License:        Unlicense
BuildArch:      noarch

Source0:        midscroll.py
Source1:        midscroll.service
Source2:        midscroll.conf
Source3:        README.md
Source4:        midscroll-overlay.py
Source5:        midscroll-overlay.service
Source6:        move-vertical.svg
Source7:        LICENSE

Requires:       python3
Requires:       python3-evdev
# Overlay (session helper showing the scroll badge during a drag)
Requires:       python3-gobject
Requires:       python3-cairo
Requires:       gtk4
Requires:       gtk4-layer-shell
Requires:       librsvg2
Requires:       kdotool
# Focus detection on X11 sessions (app blacklist)
Recommends:     xprop
%{?systemd_requires}
BuildRequires:  systemd-rpm-macros

%description
Hold the middle mouse button and drag to scroll, with speed proportional
to drag distance, like Windows 10/11. Works on Wayland and X11 in every
application by operating at the kernel input layer (evdev/uinput).
Includes a session overlay that shows a scroll badge at the anchored
cursor while a drag-scroll is active.

%install
install -Dm755 %{SOURCE0} %{buildroot}%{_bindir}/midscroll
install -Dm644 %{SOURCE1} %{buildroot}%{_unitdir}/midscroll.service
install -Dm644 %{SOURCE2} %{buildroot}%{_sysconfdir}/midscroll.conf
install -Dm644 %{SOURCE3} %{buildroot}%{_docdir}/midscroll/README.md
install -Dm755 %{SOURCE4} %{buildroot}%{_bindir}/midscroll-overlay
install -Dm644 %{SOURCE5} %{buildroot}%{_userunitdir}/midscroll-overlay.service
install -Dm644 %{SOURCE6} %{buildroot}%{_datadir}/midscroll/move-vertical.svg
install -Dm644 %{SOURCE7} %{buildroot}%{_licensedir}/midscroll/LICENSE
install -d %{buildroot}%{_userpresetdir}
echo "enable midscroll-overlay.service" \
    > %{buildroot}%{_userpresetdir}/90-midscroll.preset

%post
%systemd_post midscroll.service
%systemd_user_post midscroll-overlay.service
# Enable and start immediately on first install
if [ $1 -eq 1 ]; then
    systemctl enable --now midscroll.service >/dev/null 2>&1 || :
fi

%preun
%systemd_preun midscroll.service
%systemd_user_preun midscroll-overlay.service

%postun
%systemd_postun_with_restart midscroll.service
%systemd_user_postun_with_restart midscroll-overlay.service

%files
%license %{_licensedir}/midscroll/LICENSE
%doc %{_docdir}/midscroll/README.md
%{_bindir}/midscroll
%{_bindir}/midscroll-overlay
%{_unitdir}/midscroll.service
%{_userunitdir}/midscroll-overlay.service
%{_userpresetdir}/90-midscroll.preset
%{_datadir}/midscroll/move-vertical.svg
%config(noreplace) %{_sysconfdir}/midscroll.conf

%changelog
* Mon Jul 20 2026 midscroll - 1.6-1
- Preserve per-mouse pointer settings: mirror each mouse through its own
  uinput device that copies the source name/vendor/product, so libinput and
  KDE no longer reset pointer speed and acceleration to defaults
- Stop grabbing keyboards: require both REL_X and REL_Y so a device exposing
  a stray pointer capability via media keys is no longer captured

* Mon Jul 20 2026 midscroll - 1.5-1
- Only trust focus reports from a logged-in user's helper (peer-credential
  check on the state socket), so a stray local process can't pause the
  daemon
- Track focus per helper: a second session or a stale helper exiting no
  longer wipes the live one's focus
- Keep enforcing the app blacklist during a drag, not just at press time,
  so switching into a blacklisted app stops an in-progress scroll
- Resync held buttons after dropped kernel events so a button can't stick
- Time the scroll ticks by the monotonic clock, so speed holds under load
- is_mouse only excludes real pointing-absolute devices, not any device
  with a stray absolute axis

* Mon Jul 20 2026 midscroll - 1.4-1
- Per-device scroll state (multiple mice can no longer corrupt each other)
- App blacklist: pause automatically over apps that use middle-drag
  themselves (default: FreeCAD, OrcaSlicer, Minecraft)
- Command-line interface for overriding any tunable per run
- Strict config validation, logging with --debug, systemd sandboxing

* Mon Jul 20 2026 midscroll - 1.3-1
- Initial public release
