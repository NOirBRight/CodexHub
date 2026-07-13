# Upstream report: app-server accepts an unavailable model binding non-atomically

## Summary

The App-managed `app-server` accepts `thread/start` and `turn/start` for a
model absent from its own `model/list` result. It then persists an input-only,
no-output turn instead of rejecting the unavailable binding atomically. A
normal continuation without a model override has no usable rollout.

## Safe reproduction

1. Start the App CLI with a fresh, isolated `CODEX_HOME`; do not copy an auth
   file or change any shared configuration.
2. Configure an isolated custom-provider catalog that omits a chosen sentinel
   model identifier, then confirm the omission through `model/list`.
3. Call `thread/start` with that identifier, followed by `turn/start` with the
   same identifier.
4. After a bounded wait, call `thread/read`.
5. Resume by thread identifier and start one ordinary continuation without a
   model override, then read again. Finally call native `thread/delete`.

The repository runner provides that exact sequence without exposing identifiers
or credentials:

```powershell
python scripts/run_issue_106_task_lifecycle.py --scenario red
```

## Observed result

- Both creation and first-turn requests are accepted although the model is not
  advertised.
- The first persisted turn contains only its input item and no agent output.
- The App reports either an in-progress active thread or a completed turn with
  a system error, depending on client timing; neither state has a usable
  rollout.
- The normal continuation likewise persists no agent output.
- Native deletion succeeds, so cleanup is possible only after the invalid
  persistent state has already been created.

## Expected result

Model binding validation should happen before any persistent Task/turn state is
created. An unavailable model must produce a deterministic `thread/start` or
`turn/start` error with no rollout, input-only turn, or continuation target.

## Product boundary

CodexHub's proven responsibility is to advertise current supported official
bindings consistently in its fallback catalog. That catalog defect is fixed in
this change. CodexHub does not own App CLI request acceptance, native Task
persistence, or rollout cleanup, and it should not edit client-internal data or
invent a deletion workaround for this behavior.
