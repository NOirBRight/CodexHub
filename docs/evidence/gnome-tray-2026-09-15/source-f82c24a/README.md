# Source-bound live reproduction

- Git SHA: `f82c24a03082148b49b646a6e26b57c9a1450039`
- Build: `npm ci --prefix frontend && npm run build --prefix frontend && (cd src-tauri && cargo build --locked --release --features custom-protocol)`
- Binary SHA-256: `20b08e55774005a7d90dd743dbe6b65bcd600ddbb093eda7afb700c71ef15335`
- Isolated lab: `/tmp/codexhub-tray-lab-529g`
- Harness: `scripts/gnome-tray-lab/run.sh --expect-failures --repeats 8 --theme light`

## Result

Stock Ubuntu AppIndicators (`gnome-shell-ubuntu-extensions=50.26.04.7ubuntu`), no Contrast Guard, Gateway auto-start off.

| Check | Result |
| --- | --- |
| CodexHub restart samples in this bound run | 8, plus theme switch and reopen |
| CodexHub empty-label failures | 1 (`empty-labels.json`, restart 6) |
| CodexHub low-contrast failures | 0 |
| Reference AppIndicator failures | 0 |
| Light contrast | 15.91:1 |
| Dark contrast | 12.03:1 (`theme-dark.json`) |

When Shell actors are empty, D-Bus `GetLayout` still contains the seven CodexHub labels. Compare [empty-labels.json](empty-labels.json) with [empty-labels-dbus.txt](empty-labels-dbus.txt). Passing menus have the same D-Bus labels, see [pass-light-dbus.txt](pass-light-dbus.txt). Actor `style` is null, so the isolated session is not carrying Contrast Guard inline colors.

A longer unfixed loop on the same binary (`summary-12.json`) saw 5 empty-label failures in 12 restarts plus theme/reopen samples. These counts are evidence that the race exists, not a user-facing failure probability.

## Files

- `environment.json` — SHA, binary hash, GNOME/theme/package versions
- `repeat-results.jsonl` — per-sample checker output
- `empty-labels.json` / `empty-labels-dbus.txt` — simultaneous Shell vs D-Bus on failure
