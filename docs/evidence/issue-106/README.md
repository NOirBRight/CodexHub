# Issue #106 task-creation evidence

This directory separates the current isolated App CLI controls from a retained
historical fixture. Neither substitutes for the unresolved end-to-end Issue
#106 acceptance contract.

## Current isolated controls

Run these commands from the repository root while the local CodexHub Gateway is
active:

```powershell
python scripts/run_issue_106_task_lifecycle.py --scenario compare
python scripts/run_issue_106_task_lifecycle.py --scenario green --repeat 2
python scripts/run_issue_106_task_lifecycle.py --scenario red
```

The runner creates a fresh task-owned `CODEX_HOME` for every run, writes its
catalog and overlay only there, removes provider-key environment variables from
its child process, and uses native `thread/delete`, client-pipe close, and
app-server shutdown before removing that temporary home. Its result contains
only sanitized counts, states, and binding fields; it does not emit local paths,
Task or session identifiers, credentials, gateway token values, or raw rollout
contents.

`compare` is an account and catalog control only. It makes fresh disconnected
and CodexHub-connected `model/list` calls and confirms the requested official
model and `max` effort are listed in both isolated catalogs, while the external
model is listed only when connected. It does not call `thread/start`,
`turn/start`, `thread/read`, or an official remote lifecycle. The official
remote full-lifecycle A/B remains an unrun external gate.

`green` creates one connected native Task with a catalog-listed external model,
completes its bootstrap turn, reads back the retained non-archived Task, and
then proves that it is present in `thread/list` before rename or deletion. The
App server does not list this Task while its first turn is in flight, so the
runner makes no in-flight-list claim. It then full-binds, resumes, performs an
ordinary continuation, reads replay, and deletes natively. The only binding
transition it verifies is the same external custom-provider model from `low`
bootstrap effort to `max` effort. Its resume assertion requires the persisted
model, `modelProvider: custom`, `max` effort, `never` approval, and full-access
sandbox values. It does not verify a different explicit Worker model/reasoning
transition, an official remote lifecycle, or a Desktop `Custom`/`Light`
presentation label.

The full-bind preflight is deliberately metadata-only: it sends the requested
approval and sandbox policy values, but does not exercise filesystem or network
access. `green` requires two isolated runs. Its repeated cleanup result covers
only native Task deletion, the task-owned client pipe, task-owned app-server,
and task-owned temporary home. It does not cover remote-control enrollment,
shared configuration, global client worktrees, or client-placeholder cleanup.

`red` uses a deliberately unlisted sentinel model. The current App CLI accepts
both create and first-turn requests, persists an input-only no-output turn, and
then leaves an ordinary continuation without a usable rollout. That upstream
residual is documented in
[official-app-server-non-atomic-model-binding.md](official-app-server-non-atomic-model-binding.md).
If a create or turn request instead rejects, the runner calls it
`atomic_rejection` only when it retains a numeric JSON-RPC error code and a
subsequent `thread/read` proves zero persisted turns. A rejected create without
a readable Task, or any nonempty/error-imprecise readback, is
`unverified_rejection`.

| Contract area | Status | Exact evidence boundary |
| --- | --- | --- |
| CodexHub fallback catalog lists current requested official bindings and effort | Covered | Fresh isolated catalog policy and catalog regression test |
| Connected external-model create/read/rename/same-model full-bind/continue/delete | Covered, partial | Active `thread/list` assertion and replay require the persisted `custom` provider |
| Low-cost bootstrap to a different explicit Worker model/reasoning binding | Unrun external gate | The current live runner deliberately uses the same external model |
| Official remote full-lifecycle A/B | Unrun external gate | `compare` is account/catalog/model-list only |
| Permission policy acceptance | Partial | Metadata-only policy fields; no filesystem or network access exercise |
| Unlisted-model request atomicity | Reproduced upstream residual | Accepted input-only turn and unusable continuation; rejected paths are classified conservatively |
| Repeated cleanup | Covered, task-owned only | Native Task/client-pipe/app-server/temporary-home cleanup; not remote-control, worktree, or placeholder cleanup |

## CodexHub-owned correction

The fallback catalog policy had stopped listing the current official Sol, Terra,
and Luna bindings when no runtime subscription seed was available. The App
could therefore accept a full binding that CodexHub had not advertised. The
policy now includes those bindings, and the catalog regression test verifies the
Terra `max` binding plus resolved context limits in a fresh-home fallback.

CodexHub does not create native Tasks, own App CLI rollout persistence, or
configure global MCP servers. The isolated runner deliberately does not attempt
a client-side workaround for the upstream non-atomic behavior.

## Retained historical fixture

The original sanitized fixture remains a structural record of an earlier
sidebar-placeholder observation. It does not create a live Task:

```powershell
python scripts/check_codex_task_creation_lifecycle.py
```

The fixture does not validate active Task listing, an official remote lifecycle,
access exercise, remote-control enrollment, repeated-run cleanup, client
placeholder cleanup, or worktree cleanup. Its negative controls remain
deterministic and fail visibly. The fixture has a closed schema and rejects
local paths, Task/session identifiers, and credential-shaped strings without
echoing them in mismatches. The earlier placeholder report is retained in
[official-client-half-created-task-defect.md](official-client-half-created-task-defect.md).
