# Issue #106 task-creation evidence

This directory contains both the retained historical fixture and the authorized
isolated App CLI replay used to separate CodexHub catalog ownership from native
Task lifecycle ownership.

## Deterministic isolated replay

Run these commands from the repository root while the local CodexHub Gateway is
active:

```powershell
python scripts/run_issue_106_task_lifecycle.py --scenario compare
python scripts/run_issue_106_task_lifecycle.py --scenario green --repeat 2
python scripts/run_issue_106_task_lifecycle.py --scenario red
```

The runner creates a fresh task-owned `CODEX_HOME` for each run, writes its
catalog and overlay only there, disables nonessential plugin services, removes
provider-key environment variables from its child process, and uses native
`thread/delete`, client-pipe close, and app-server shutdown before removing
that temporary home. It emits only sanitized
counts, states, and binding fields: no local paths, prompts, task identifiers,
session identifiers, credentials, or gateway token values.

`compare` is the official/disconnected versus CodexHub/connected catalog
control. The fresh official control has no copied authentication, so it is a
catalog control rather than a valid official-rollout control. It confirms that
the App CLI itself knows the requested current official model and `max` effort.
The connected control then verifies that CodexHub preserves that binding in its
isolated catalog.

A separate authenticated native App Task control validated the requested
official full binding, read/rename sequence, normal continuation, and supported
archive cleanup without retaining any Task identifier. The native Task API does
not expose the Desktop's presentation label, so this evidence makes no claim
about a visual `Custom` or `Light` label.

`green` exercises create, list, bootstrap, read, rename, explicit full binding
and permission preflight, resume, normal continuation without a binding
override, read replay, native delete, client-pipe close, task-owned app-server
shutdown, and temporary-home cleanup against a catalog-listed external model.
It defaults to,
and requires, at least two runs for the repeated-run leak check.

`red` uses a deliberately unlisted model identifier. The current App CLI
accepts both create and message requests, persists input-only no-output turns,
and then leaves a normal continuation without a usable rollout. Native cleanup
still succeeds. That is an upstream atomicity defect, documented in
[official-app-server-non-atomic-model-binding.md](official-app-server-non-atomic-model-binding.md).
Before the catalog fix, the requested Luna binding was absent from the connected
fallback catalog and exercised this same invalid-binding boundary. The sentinel
keeps that red control deterministic after Luna becomes correctly listed.

## CodexHub-owned fix

The fallback catalog policy had stopped listing the current official Sol, Terra,
and Luna bindings when no runtime subscription seed was available. The App
could therefore accept a full binding that CodexHub had not advertised. The
policy now includes those bindings, and the catalog regression test verifies
the Terra `max` binding plus resolved context limits in a fresh-home fallback.

CodexHub does not create native Tasks, own the App CLI's rollout persistence,
or configure global MCP servers. The isolated runner deliberately does not
attempt a client-side workaround for the upstream non-atomic behavior.

## Retained historical fixture

The original sanitized fixture remains a structural record of the earlier
sidebar placeholder observation. It does not create a live Task:

```powershell
python scripts/check_codex_task_creation_lifecycle.py
```

Its negative controls remain deterministic and fail visibly. The fixture has a
closed schema and rejects local paths, Task/session identifiers, and
credential-shaped strings without echoing them in mismatches. The earlier
placeholder report is retained in
[official-client-half-created-task-defect.md](official-client-half-created-task-defect.md).
