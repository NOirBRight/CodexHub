# Isolated GNOME tray harness

Live CodexHub menu readability checks against stock Ubuntu AppIndicators.
The harness uses bubblewrap, a private D-Bus, a temporary home, and a
headless GNOME Shell. It does not load `codexhub-tray-contrast@codexhub.app`,
does not eval the host Shell, and does not start Gateway.

## Build the binary under test

From the worktree whose SHA you want to bind:

```sh
npm ci --prefix frontend
npm run build --prefix frontend
(cd src-tauri && cargo build --locked --release --features custom-protocol)
```

Record `git rev-parse HEAD` and `sha256sum src-tauri/target/release/codexhub`.

## Live run

```sh
./scripts/gnome-tray-lab/verify-fixtures.sh
./scripts/gnome-tray-lab/run.sh --expect-failures --repeats 10 --theme light
```

`--expect-failures` is for an unfixed binary: the run passes only if CodexHub
empty or unreadable labels are actually captured. After a fix, use
`--expect-pass`.

Artifacts land in `$CODEXHUB_TRAY_LAB` (default `/tmp/codexhub-tray-lab`):

- `artifacts/environment.json` — SHA, binary hash, GNOME/theme/package versions
- `artifacts/repeat-results.jsonl` — per-sample Shell label/contrast checks
- `artifacts/repeat-N.json` and `repeat-N-dbus.json` — actor dump and D-Bus layout
- `artifacts/summary.json`

The test observer only enables `unsafe_mode` inside the disposable Shell so
Eval can read actors and open menus. It does not set colors.

## Static fixtures

Saved snapshots from 2026-09-15 are in
`docs/evidence/gnome-tray-2026-09-15/`. `verify-fixtures.sh` checks those
files only; it is not a substitute for a live restart loop.
