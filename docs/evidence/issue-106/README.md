# Issue #106 task-creation evidence

This directory records a sanitized, deterministic replay of the observed
Task-creation A/B boundary. It is not a live Task creator and it does not
modify Codex configuration, native Task state, worktrees, or any internal
Codex database.

Run the replay from the repository root:

```powershell
python scripts/check_codex_task_creation_lifecycle.py
```

The verifier also includes negative controls. Each must fail visibly:

```powershell
python scripts/check_codex_task_creation_lifecycle.py --replay-case materialize-red
python scripts/check_codex_task_creation_lifecycle.py --replay-case unmaterialize-green
python scripts/check_codex_task_creation_lifecycle.py --replay-case fail-git-preflight
python scripts/check_codex_task_creation_lifecycle.py --replay-case skip-cleanup
python scripts/check_codex_task_creation_lifecycle.py --replay-case identifier-leak
```

## Retained boundary

The red case reached client-placeholder and worktree provisioning, but no
rollout/session materialized. Native Task listing therefore had no task to
read, message, rename, archive, or delete. The green case materialized after
the optional global `openaiDeveloperDocs` MCP was disabled, completed in
10.146 seconds, and passed create/read/message plus the requested full-access
preflight.

Read-only inspection establishes that CodexHub's configuration overlay manages
only the model-provider/catalog surface; it does not configure global MCP
servers or expose the native Task lifecycle. CodexHub does start bounded
external app-server probes for model and usage reads, but no committed path
links those probes to post-worktree Task materialization. Accordingly, this
change adds evidence coverage only and deliberately leaves CodexHub product
behavior unchanged.

The verifier reads the known CodexHub configuration, catalog, Gateway, and
app-server-probe boundary source modules. It fails if they gain either the
named global MCP or a generic `mcp_servers` configuration surface, and it
requires the bounded model and usage probe launch sites to remain present.
That source check is deliberately narrow: it supports the ownership boundary,
not a claim about the official client's internal implementation or a proven
causal link to Task materialization.

The retained JSON has a closed schema: it requires source provenance, rejects
unknown fields, and checks strings for local paths, Task/session identifiers,
and credential-shaped material without echoing their values in mismatches.

The official Task read surface was available for an existing materialized Task.
A new create replay and repeated process-leak run were intentionally not run:
they would create replacement Tasks and require Orchestrator approval.
The retained successful GitHub preflight is structurally checked by the replay;
it does not create or inspect another worktree.

## Cleanup boundary

Two clean orphan worktrees were removable. One failed placeholder left an empty
directory until the official client releases its handle. Supported archive and
delete operations rejected the placeholder because no real session existed.
No internal Codex database was edited. The precise upstream report is in
[official-client-half-created-task-defect.md](official-client-half-created-task-defect.md).
