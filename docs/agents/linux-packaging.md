# Linux packaging

Linux is a first-class CodexHub surface on the same 0.1.9 train as Windows.
Do not fork a `linux-main`. Rebase campaign work onto `main` and ship Linux
artifacts from the same tag.

## Artifacts

Each flavor produces:

| Kind | Name |
|---|---|
| Portable | `CodexHub_<version>[_debug]_linux_portable_<sha8>.tar.gz` |
| AppImage | `CodexHub_<version>[_debug]_amd64.AppImage` |
| Debian | `CodexHub_<version>[_debug]_amd64.deb` |

Updater platform key: `linux-x86_64`. `scripts/build-linux-release.sh` writes
`latest.json` / `latest-debug.json` with that key and the signed AppImage.
The Windows release script still writes `windows-x86_64`. A combined ship
keeps both keys in one payload; do not invent a second product channel.
Linux auto-update is AppImage-only. The live endpoint remains
`releases/latest/download/latest.json`; prereleases are not that Latest
release, so a Beta tester must install this build first and then point a
newer signed AppImage at a reachable `latest.json`.

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

Linux GUI preflight:

```bash
python3 scripts/e2e_linux_gui_clients.py --detect-only
python3 scripts/e2e_linux_gui_clients.py --skip-launch
python3 scripts/e2e_linux_gui_clients.py
```

This host's accepted floors are the same numeric floors as Windows: Codex
Desktop `26.715.8383.0` (Debian package `chatgpt`) and ZCode `3.3.6`.
`open_codex_app` on Linux launches `/usr/bin/chatgpt` / `codex-launcher`.

Windows AppX real-client E2E remains required for the Windows installer. It is
not a substitute for this Linux preflight, and this preflight is not a
substitute for Windows.
