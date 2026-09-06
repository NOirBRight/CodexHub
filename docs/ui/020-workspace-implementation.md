# 0.2.0 workspace implementation

The production entry (`/`) implements the approved prototype at commit `2de4613`.
The development-only `?prototype=overview` route remains the reference. No demo
providers, balances, request events or scenario controls are imported by production.

## Design and functional mapping

| Surface | Production implementation | Preserved behavior |
| --- | --- | --- |
| Desktop frame | WorkspaceShell, RuntimeBar, FitStage | Five top tabs; compact 1024 × 768 initial size; real window drag/minimize/maximize/close-to-tray; persisted light/dark appearance |
| Gateway strip | WorkspaceFrame → App runtime actions | Real start/stop/restart, lifecycle busy guards, existing retirement confirmation and Toast feedback |
| Codex bridge | ProviderWorkspaceView → ProvidersPage controller | Official/custom switch, existing authorization, ownership/takeover, restart and history handling |
| Overview | ProviderWorkspaceView, ResourceLimits | Daily real Gateway requests/tokens/success/duration; real OpenAI/xAI quota; fixed header and independently scrolling resources; single weekly quota occupies the right slot |
| Provider management | ProviderWorkspaceView → useProviderWorkspace | Catalog/custom add, enable, reorder, model count, endpoint/credential entrypoints |
| Provider details | ProviderEditor, OfficialDetail | Models/config/account tabs; original draft/save, discovery, probe, delete, model editor, collaboration versions, official catalog refresh/cancellation, xAI device authorization, quota and sign-out |
| Usage | GatewayPage → unchanged StackedUsageChartShell | Metrics, provider/model/client breakdown, daily/weekly grouping, custom dates, legend filters, hover details, cost/cache summaries |
| Clients | GatewayPage, GatewayClientCard, WorkspaceDialog | Existing adapters and routing lifecycle; independent cards scroll; connection filters; real icons, paths, versions, ownership and redacted config preview; saved manual connection parameters |
| Settings / General | SettingsDrawer | Language and autostart operations; appearance; Gateway-on-open |
| Settings / Codex & clients | SettingsDrawer, OfficialDetail | Include official models, client auto-sync, history sync/repair; context-guard management opens its existing model controls and conflict feedback |
| Settings / Gateway | SettingsDrawer → App.saveSettings | Bind address readback, port, timeout, key visibility/copy/regeneration, saved endpoint URLs; one settings draft and fixed save/discard footer |
| Settings / Request policy | SettingsDrawer | Retry enable/count and image proxy/model selection |
| Settings / Diagnostics | RecoveryActivityPanel, DebugDiagnosticsOverlay | Recovery activity, retry shortcut; debug-build diagnostic actions and production-build restrictions remain intact |
| Settings / About | VersionUpdateBlock, useAppUpdateLifecycle | Version check, existing install/restart confirmation, progress and completion lifecycle |

Settings drafts rebase clean fields on backend refresh while retaining edits.
Provider editor contents stay mounted when closed so drafts survive; switching
providers still uses the original unsaved-changes controller. Dialog bodies and
list regions scroll independently of their actions.

## Data and scope boundaries

- Generic API providers have no common balance-query backend. Their overview rows
  explicitly say balance querying is unavailable; no example dollar amounts are shown.
- Gateway events provide response duration, not time-to-first-token. The overview
  uses the accurate label “Mean response time”. Missing token usage stays unknown.
- Unknown clients receive connection parameters only; their configuration files
  are not guessed or rewritten. Supported clients retain their actual adapters.
- Existing Settings fields, IPC command names, auth, routing and persistence formats
  remain unchanged. The frontend uses the existing context-guard workflow instead
  of duplicating its sensitive restart/sync behavior in a new settings toggle.
- Fixed native Linux navigation targets were updated in the physical pointer E2E
  because Settings is now a page. Its event-capture checks and transition thresholds
  are unchanged. Native Linux still uses webview zoom instead of CSS transform scaling.
- A development StrictMode mount failure in the update hook was fixed by creating
  each disposable lifecycle during effect setup; a replay regression test covers it.

## Verification

Risk: strict for the adjacent update-hook lifecycle fix; the UI/controller split
itself introduces no new IPC or persistence contract. Direct Standards/Spec review
checks the mapping above, production-only imports, draft retention, real-data labels,
window/input behavior and existing confirmation/Toast routes. No remote issue or PR
was posted as part of this implementation.

- `npm run build` and `npm run test:ui-contract`: pass, including new quota-layout,
  timestamp, unknown-data, draft-rebase and effect-replay tests.
- `./scripts/verify-linux.sh`: pass (2091 Python tests, 165 skips, 263 subtests;
  694 Rust tests, 4 ignored; partition completeness; clippy; native physical pointer E2E).
- Report-only quality gates: completed; parse errors 0. Existing report findings
  remain nonblocking under project policy.
- Browser inspection: real readback on overview, Provider and account surfaces,
  client details, settings categories; light/dark and 840 × 600 layout; full custom
  calendar; catalog/custom-add form; port draft retained across navigation and restored with Discard.
- Final visual corrections passed frontend rebuild and targeted checks plus another
  native pointer run (820 × 620; all four page transitions 0.449). Actual provider login/logout, user client reconnection,
  Gateway interruption and update installation were not triggered for browser QA.

This is source integration, not a signed 0.2.0 release or installation.
