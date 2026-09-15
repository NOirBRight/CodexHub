# App-side candidate (#530)

Candidate git SHA `488bc453796387b4e9cff76b48d4e17ac6c027c7` (same `main.rs` blob as the 30/30 run).
Binary SHA-256 `1a5094b32b79a4f3fab96e6407abe68ebd8d5f486495ced597a6f9a572cbf461`.
Stock AppIndicators, no Contrast Guard.

## Decision

Keep native tray-icon/muda. After the Linux tray menu is published, rewrite
each GTK label (word-joiner then original text) so dbusmenu emits
`ItemsPropertiesUpdated`.

- Trigger: `setup_tray` has called `TrayIconBuilder::build`
- Bound: immediate, next GTK idle, one 100ms timeout; then stop
- Why a bump: same-string `set_text` does not notify GTK, so GNOME never
  receives a replacement for the cancelled label fetch

Tried and rejected on this harness:

1. tray-icon `set_status(Active)` after `set_menu` — empty labels still
   appeared on restart
2. `set_text` with the unchanged string — failed on restart 9 of 15

## Isolated results

| Run | CodexHub pass | CodexHub fail | Reference fail |
| --- | ---: | ---: | ---: |
| Light, 15 restarts + theme + reopen | 18 | 0 | 0 |
| Dark, 10 restarts + theme + reopen | 12 | 0 | 0 |

Unfixed baseline SHA `f82c24a03082148b49b646a6e26b57c9a1450039` (binary `20b08e55…`): 1/8 and 5/12 empty-label failures.
