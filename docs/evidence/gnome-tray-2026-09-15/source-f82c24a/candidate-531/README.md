# Isolation acceptance (#531)

- Branch: `fix/gnome-tray-empty-labels`
- Candidate git SHA: `488bc453796387b4e9cff76b48d4e17ac6c027c7`
- Product blob: `src-tauri/src/main.rs` Linux tray republish (same bytes as this SHA)
- Binary SHA-256: `1a5094b32b79a4f3fab96e6407abe68ebd8d5f486495ced597a6f9a572cbf461`
- Build: `npm ci --prefix frontend && npm run build --prefix frontend && (cd src-tauri && cargo build --locked --release --features custom-protocol)`
- Stock AppIndicators, no Contrast Guard, Gateway auto-start off

The binary was built from this `main.rs` immediately before `488bc45`. Parent SHA `f82c24a` is the unfixed baseline only; do not treat `source-f82c24a/environment.json` as the fixed candidate.

## Isolation

| Theme | Restarts | CodexHub pass lines (incl. theme/reopen) | Fail |
| --- | ---: | ---: | ---: |
| Light | 30 | 33 | 0 |
| Dark | 30 | 32 | 0 |

Contrast when labels are present: light 15.91:1, dark 12.03:1. Machine evidence is the jsonl actor checks, not PNGs (the PNGs under `docs/evidence/gnome-tray-2026-09-15/` are the earlier unfixed/extension-patch exploration).

Not exercised by this harness: physical menu activation, close-to-tray, or desktop-launcher re-invoke. Tray action handlers and Toast helpers were not changed.

## Rollback

Unfixed source SHA `f82c24a03082148b49b646a6e26b57c9a1450039`, binary SHA-256 `20b08e55774005a7d90dd743dbe6b65bcd600ddbb093eda7afb700c71ef15335`. Host patch restore: `~/.cache/codexhub-tray-cleanup/2026-09-15/RESTORE.md`.

## Restart

CodexHub process must be restarted to load this binary. The 0.2.11 portable launcher is a different artifact and will not pick this SHA up until it is replaced. After removing host Contrast Guard / AppIndicators patches, a new GNOME login is required before treating the live tray as a clean baseline.

## Verification

On this `main.rs` (then committed as `488bc45`): `./scripts/verify-linux.sh` (Python core, `cargo test --locked`, clippy `-D warnings`, Xvfb pointer E2E). After that commit, `cargo clippy --locked --all-targets -- -D warnings` finished clean. `./scripts/gnome-tray-lab/verify-fixtures.sh` passes. Shell-menu jsonl is not the pointer E2E.
