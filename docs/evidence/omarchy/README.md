# Omarchy installation and E2E evidence

**Follow-up 0.2.24:** The AppImage construction and startup blockers below were
resolved using the checked-in Ubuntu build environment, Wayland-library
finalization, and Python child library isolation. Native AppImage lifecycle
E2E now passes all 10 checks. The native portable also passes at 125% scale;
GNOME first-launch/portable-upgrade tests pass. Muse was waived by the
operator. The original results below remain historical evidence for 0.2.21.
Final release hashes and source revision belong to the release manifests.

Measured on 2026-09-22, Omarchy 4.0.4 / Hyprland 0.56.2, GTK 3.24.52,
WebKitGTK 2.52.6, GdkPixbuf 2.44.7. Source baseline `7185c1a1`, branch
`compat/omarchy`; working-tree changes are not a published release.

## Installed candidate

The optimized portable application and adjacent resources are installed at:

```text
~/.local/share/codexhub/omarchy-candidate-0.2.21/CodexHub
SHA256 0b9d49332013cb66fbcb09ba15edec074063e0ff92a30d1cf9c8505c524b427c
```

It was built locally with Tauri `--no-bundle --ci` and run from that installed
path. Tests isolate HOME, XDG data, client configuration, and Gateway state;
the final test exits the application and verifies that its Gateway stops.
No Omarchy configuration overrides were installed. No updater/release was
published. The existing GNOME qualification gate was not bypassed or removed.

## Findings fixed

- Linux Gateway shutdown left an unreaped zombie with an empty command line.
  Identity checks treated it as an unrelated live process, rejecting restart
  and bundle replacement. Process inspection now recognizes `/proc` states
  Z/X as exited while retaining fail-closed behavior for live/unknown identity.
  The previously ignored real Python start/stop and two upgrade lifecycle
  tests are enabled again. The original lifecycle test failed before the fix;
  all 50 proxy tests passed afterward.
- `lsof` was missing on the host: Gateway could listen but could not reconcile
  listener ownership. Installed it and added the Debian runtime dependency;
  Arch prerequisites are documented.
- GUI-client detection now resolves the selected executable's installed owner
  using pacman or dpkg instead of assuming Debian package names. Unknown
  versions fail closed. The launcher now observes the full startup interval,
  catching a process that exits after the first poll.
- CLI sentinel verification requires actual assistant output, rather than a
  sentinel echoed in the user prompt (observed in failed Pi/OMP transcripts).
- Isolated Rust fixtures from the host Official model cache, replaced a retired
  model in a catalog fixture, fixed a prematurely restored rollback path, and
  updated stale frontend source-contract expectations.
- Disabled linuxdeploy's obsolete strip pass, which rejected Arch SHT_RELR.

## Verified

- [Native Wayland report](native-wayland.json): **10/10**. Native window/input,
  Settings and Workspace pointer transitions, tray labels, Gateway start,
  close-to-tray with Gateway retained, window restore, restart with a new
  listener PID, stop, and application exit with Gateway cleanup.
  The tray bus is checked against the test window's PID before actions.
- [Rendered Settings screenshot](native-settings.png), captured only from the
  isolated headless compositor. This does not prove IME or fractional scaling.
- Xvfb physical pointer E2E: **passed**, 13 clicks, full input region,
  no background click-through, rendered drawer transitions 0.572–0.583.
- Rust: **759 passed, 1 ignored**; clippy with warnings denied passed.
- Frontend build and **148 UI-contract tests passed**.
- Python: **3163 passed, 184 skipped, 267 subtests passed**. The first combined
  verification found the new E2E missing from the explicit entrypoint inventory;
  that inventory was corrected and the Python leg rerun. Other legs passed.
- Report-only quality scan: zero parse errors; existing findings remain
  non-blocking. `git diff --check` passed.
- [Real CLI summary](cli-summary.json): all four Official combinations passed
  again against the fixed installed binary and stricter assistant-output check.

Commands:

```bash
mise exec rust powershell -- ./scripts/verify-linux.sh
./scripts/codexhub-python.sh -m pytest -q --ignore=tests/test_real_client_e2e.py
./scripts/codexhub-python.sh scripts/e2e_linux_hyprland.py \
  --bin "$HOME/.local/share/codexhub/omarchy-candidate-0.2.21/CodexHub" \
  --output test-results/omarchy-native.json
```

## Remaining limits

The full eight-case live CLI gate is **not passed**. OpenCode's existing
`opencode-go` API credential was reused via a child-process environment without
printing or committing it. Three Muse clients returned HTTP 429 /
`GoUsageLimitError: Go usage limit exceeded`; OpenCode timed out after 90s.
All eight configuration apply/readback steps succeeded. The final repeat ran
only the four Official cases, explicitly not a substitute for the eight-case
gate. On 2026-09-22 the operator explicitly waived further Muse testing for this release. This is a waiver, not a passing result.

AppImage construction on this Arch host remains blocked by linuxdeploy's GTK
plugin, which unconditionally copies the absent
`/usr/lib/gdk-pixbuf-2.0/2.10.0` tree. The strip fix revealed this second
failure. No system GTK directories were fabricated and no incomplete AppDir
was accepted as an artifact. The upstream
[GTK plugin](https://github.com/linuxdeploy/linuxdeploy-plugin-gtk) still
assumes loader directories; GdkPixbuf's
[build configuration](https://github.com/GNOME/gdk-pixbuf/blob/master/meson_options.txt)
supports built-in/Glycin loaders. A supported AppImage build host/toolchain is
still needed before AppImage distribution can be qualified.

Real-login autostart, IME, fractional scaling, installed AppImage upgrade,
Windows checks, and release qualification were not performed. The checked-in
Linux policy reserves synthetic real-client tests for the Windows watchdog.
Desktop/ZCode live GUI cases were not run, as required by the current
`docs/agents/real-client-e2e.md` CLI-only gate.
