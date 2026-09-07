# Linux packaging

Linux is a first-class CodexHub surface on the same release train as Windows.
Do not fork a `linux-main`. Rebase campaign work onto `main` and ship Linux
artifacts from the same tag.

## Artifacts

Each flavor produces:

| Kind | Name |
|---|---|
| Portable | `CodexHub_<version>[_debug]_linux_portable_<sha8>.tar.gz` |
| AppImage | `CodexHub_<version>[_debug]_amd64.AppImage` |
| Debian | `CodexHub_<version>[_debug]_amd64.deb` |

The Debian package identity is always `codex-hub`, so `apt`/`dpkg` upgrades
replace the installed package instead of creating side-by-side versions. The
package owns the visible `/usr/share/applications/CodexHub.desktop` launcher
and hidden identity files `com.codexhub.app.desktop` and `codexhub.desktop`
so GNOME can map the running Wayland window without a second app-grid entry.
Its recoverable post-install migration archives exact legacy CodexHub-generated
user launchers as `*.codexhub-legacy-backup`; customized desktop entries are
left untouched. The package launcher is atomically normalized to
`Exec=/usr/bin/codexhub`, so a stale user `PATH` entry cannot redirect desktop
launches to an older AppImage. If `~/.local/bin/codexhub` is a user-owned link
that resolves exactly to `~/Applications/CodexHub.AppImage`, the hook archives
only that link with the same recoverable suffix. It retains the AppImage
payload and preserves every custom command target. The root package hook only
enumerates eligible accounts; it drops to each target UID/GID with cleared
groups and capabilities before touching that user's launcher directory.
AppImage upgrades likewise rewrite one stable managed user launcher rather
than adding per-version entries.

Portable builds register the same managed user launcher, pointing at the
running executable (including paths containing spaces). Detection requires
the archive's `CodexHub` executable name and adjacent
`src-python/codex_proxy.py` and `config/providers.toml` resources;
development and system binaries do not create portable launchers. GTK uses
the configured application identifier so GNOME can associate Wayland windows
with this launcher. Customized user launchers remain untouched.
Development binaries also leave existing managed launchers untouched; runtime
cleanup is restricted to the installed `/usr/bin/codexhub` target.

Linux runtime icons refresh all supported hicolor sizes and use a content-hashed
icon name in the managed launcher, preventing GNOME from reusing a cached
older image. Normal windows use an RGBA surface, a rounded panel and a
12-pixel transparent shadow inset; maximized/fullscreen windows omit the
inset. Debian post-install migration recognizes this managed absolute icon
format and archives the portable launcher before installing the package entry.
Verify the packaged build on Wayland as well as X11.

For the GNOME 50 menu-label initialization race and its separately applied,
reversible desktop compatibility patch, see
[GNOME support](../../support/gnome/README.md). Do not count reloading the
indicator as a restart regression test.

Updater platform key: `linux-x86_64`. Add that platform to the existing
`latest.json` / `latest-debug.json` payload; do not invent a second product
channel.

## Build

PowerShell 7 (`pwsh`) is required so Linux builds reuse `Build-TauriConfig.ps1`
and the flavor contract.

```bash
./scripts/build-linux-portable.sh --dry-run
./scripts/build-linux-portable.sh --flavor debug --dry-run
./scripts/build-linux-portable.sh
./scripts/build-linux-release.sh --flavor normal
```

Windows continues to use `build-windows-portable.ps1` / `build-windows-release.ps1`.
`tauri.conf.json` lists `nsis`, `appimage`, and `deb`; each script selects the
bundles it owns.

## Python runtime

Windows installers embed CPython. Linux packages currently use a host Python
3.13+ interpreter unless `src-tauri/resources/python/bin/python` is prepared
and copied into the artifact. Gateway discovery already looks for that path.

## Linux E2E

Every complete Linux candidate runs `./scripts/verify-linux.sh`. Its mandatory,
90-second-bounded pointer-input leg starts the real app in isolated D-Bus and
Xvfb sessions, verifies the full native input shape, sends physical XTest
clicks above a background probe, and requires the settings button to produce a
rendered DOM drawer-state change.
The host needs `xvfb`, `xauth`, and `x11-utils`.

Linux GUI preflight:

```bash
./scripts/codexhub-python.sh scripts/e2e_linux_gui_clients.py --detect-only
./scripts/codexhub-python.sh scripts/e2e_linux_gui_clients.py --skip-launch
./scripts/codexhub-python.sh scripts/e2e_linux_gui_clients.py
```

Linux real-client CLI qualification is a separate eight-case gate. Its case,
selector, route, protocol, evidence, and CLI-version contract lives at
`scripts/real_client_cli_contract.v1.json`. It builds
the current Rust candidate locally, starts its Gateway in an isolated runtime,
materializes fresh client configuration, and runs Codex CLI, OpenCode, Pi, and
OMP once against Official `gpt-5.6-luna` and once against OpenCode Go
`muse-spark-1.2-contributor`:

```bash
./scripts/codexhub-python.sh scripts/e2e_linux_cli_clients.py \
  --output test-results/linux-cli-e2e.json
```

The default credential inputs are the current operator's Codex auth,
`providers.toml`, settings, and Official catalog. They are copied only into a
temporary isolated runtime and are never written to the report. Use `--auth`,
`--providers`, `--settings`, and `--catalog` to select dedicated inputs. The
report must contain eight successful apply/readback/live sentinel cases.

This host's accepted floors are the same numeric floors as Windows: Codex
Desktop `26.715.8383.0` (Debian package `chatgpt`) and ZCode `3.3.6`.
`open_codex_app` on Linux launches `/usr/bin/chatgpt` / `codex-launcher`.

GNOME dock identity regression includes first launch followed by an upgrade
between two distinct portable directories. The `linux_window` Rust tests load the
resulting desktop entry through GIO and assert both the current executable and
fingerprinted icon file. Run `cargo test --locked --manifest-path
src-tauri/Cargo.toml linux_window`. A portable candidate must also pass
`./scripts/codexhub-python.sh scripts/e2e_linux_dock_icon.py --bin <portable>/CodexHub`.
This starts an isolated headless GNOME session with a populated stale icon cache,
resolves the running window through Shell.WindowTracker, and verifies that its
actual GIcon can load. The release builder runs this check before archiving, so
both first launch and portable-directory upgrade must pass. It requires
GNOME Shell, gdbus, gsettings, and gtk-update-icon-cache; missing dependencies
must not be reported as a pass. Desktop acceptance additionally checks that
the running window resolves to the CodexHub icon rather than the generic gear.
Use isolated test directories; do not include real user paths, configuration,
credentials, or unrelated desktop contents in shared evidence.

Windows CLI real-client E2E remains required for the Windows candidate. It is
not a substitute for the Linux CLI gate, and the Linux gate is not a substitute
for Windows.
