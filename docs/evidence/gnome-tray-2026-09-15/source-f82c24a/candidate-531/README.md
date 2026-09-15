# Isolation acceptance (#531)

Git worktree on `fix/gnome-tray-empty-labels` implementing Linux tray label republish.
Binary SHA-256 `1a5094b32b79a4f3fab96e6407abe68ebd8d5f486495ced597a6f9a572cbf461`.
Stock AppIndicators, no Contrast Guard.

| Theme | Restarts | CodexHub pass lines (incl. theme/reopen) | Fail |
| --- | ---: | ---: | ---: |
| Light | 30 | 33 | 0 |
| Dark | 30 | 32 | 0 |

Contrast when labels are present remains 15.91:1 light / 12.03:1 dark.
Tray action handlers and Toast helpers were not changed.
