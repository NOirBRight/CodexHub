# Beta4.1 CLI E2E plan

This is the release qualification plan for the Beta4.1 Collaboration and
session-compatibility fixes. It complements the general Windows gate in
[`real-client-e2e.md`](real-client-e2e.md); it is intentionally CLI-only.

## Scope

The run must use the exact candidate Gateway build and the real Codex CLI
`0.146.1`, with a fresh isolated `CODEX_HOME` for every run. The harness may
record sanitized request/response metadata, but it must not rewrite the CLI's
request schema, inject `agent_type`, add binding sidecars, or synthesize tool
outputs. The existing `scripts/run_issue_283_cli_v2_lifecycle.py` remains
synthetic-fixture evidence because its `_inject_collaboration_agent_type()`
helper changes the request shape; it is not direct-schema evidence for this
plan.

The run requires a dedicated authenticated account and a disposable copy of
the historical Session `019fe193-d396-7293-86ea-4bc2c204ca9f`. Never modify
the user's original Session or shared Codex home.

## Matrix

| Case | Native path | Required coverage | Pass evidence |
| --- | --- | --- | --- |
| `v1-lifecycle` | `multi_agent_v1` | spawn worker, send input, wait, close; verify terminal result | Native V1 namespace and argument shape remain unchanged; one complete lifecycle |
| `v2-lifecycle` | `collaboration` | spawn/list, send message, follow-up, wait, interrupt, and final readback | Native V2 lifecycle completes without a V1 rewrite or duplicate terminal event |
| `model-switch-replay` | V1 then V2, and V2 then V1 | switch the selected Luna Collaboration version between turns; replay prior history | Each request uses the selected model/version and prior messages/call IDs remain ordered and readable |
| `legacy-session-resume` | historical V1 Session | restore the supplied pre-Beta4 Session, continue it, wait, and close | Old worker calls with the exact native shape are accepted; continuation and history are preserved |
| `new-binding-integrity` | Beta4.1 worker binding | create a new worker with model/reasoning binding, then replay unchanged and tampered histories | Signed binding/readback matches; model, reasoning, signature, missing, duplicate, and malformed cases fail closed |
| `cli-restart-continuity` | real CLI process | stop and restart the CLI/Gateway while retaining the isolated home, then resume | Same Session identity, prior history, worker state, and selected version are read back after restart |

The `legacy-session-resume` case is the regression gate for #418. The
`new-binding-integrity` case must prove that the compatibility exception is
limited to the exact old V1 shape; an extended or partially edited old call
must still be rejected.

## Execution phases

1. **Preflight**
   - Verify the candidate SHA, CLI version, Gateway health, loopback route,
     catalog model, and dedicated auth.
   - Create a new non-reparse isolated root and record only safe paths,
     versions, protocol names, and hashes.
   - Copy the historical Session into the isolated root with its identity
     intact. Redact prompts, tokens, account data, and local paths before any
     evidence leaves the machine.

2. **Direct-schema capture**
   - Run one request through the real CLI for each native path.
   - Capture the sanitized top-level request/response shape, tool namespace,
     call/output IDs, event order, model, reasoning, HTTP status, and Gateway
     correlation IDs.
   - Assert that the bytes/JSON emitted by the CLI reached the Gateway before
     any CodexHub compatibility translation. The harness may hash the input;
     it must not patch it.

3. **Lifecycle and history**
   - Execute the V1 and V2 cases in fresh Session roots.
   - Assert exactly one successful terminal outcome per turn, paired call and
     output IDs, preserved call order, and readable assistant/worker history.
   - Run the model-switch case with at least one follow-up turn after each
     switch; verify that old turns are replayed rather than dropped or
     reclassified.

4. **Historical recovery**
   - Start from the copied Session `019fe193-d396-7293-86ea-4bc2c204ca9f`.
   - Resume without rewriting its old `multi_agent_v1.spawn_agent` calls.
   - Complete a new turn and verify that the old worker readbacks plus the new
     turn are accepted by the same Gateway process.

5. **Binding and tamper checks**
   - Record a new worker call with its model/reasoning binding and effective
     readback.
   - Replay the unchanged history, then independently alter the model,
     reasoning, signature, call identity, output, and call/output cardinality.
   - Require a bounded, sanitized rejection classification for every altered
     case. Do not log the prompt, model secrets, signatures, or raw payload.

6. **Restart and readback**
   - Stop the CLI after a completed worker turn and restart it against the same
     isolated home; repeat once with a Gateway restart if the candidate runner
     supports it.
   - Query/read the existing Session before creating a new one. Verify the
     Session ID, selected version, history, worker IDs, and next legal action.
   - Finish with a new turn and confirm no duplicate spawn or terminal event.

## Evidence contract

The planned runner should emit one sanitized summary with schema
`codexhub.beta41.cli-e2e.v1` and one record per matrix case. Each record may
contain:

- candidate SHA, CLI version, protocol (`v1`/`v2`), selected model, and
  reasoning level;
- lifecycle phase names and bounded counts for calls, outputs, stream events,
  restarts, and Gateway correlations;
- stable hashes for request/response bodies and the preserved Session history;
- safe result codes such as `accepted`, `legacy_native_spawn`,
  `binding_rejected`, `history_replayed`, or `restart_readback`.

It must not contain prompts, assistant text, authorization headers, tokens,
signatures, account identifiers, PIDs, absolute paths, or raw upstream
payloads. A missing, contradictory, duplicated, or unparseable event is a
failure, not an omitted field.

## Local and release checks

The implementation phase should add deterministic tests for the runner's
schema parser and sanitized evidence validator, then execute the real matrix
only on the dedicated Windows host. The release checklist is:

- all six cases pass with the exact CLI version and candidate SHA;
- the legacy Session continuation passes without changing its old call shape;
- direct-schema hashes show no harness mutation;
- restart readback passes for both selected versions;
- no secret-bearing or raw Session artifact is written to the repository.

## Manual confirmation still required

- Real Codex CLI lifecycle, model switching, historical Session resume, and
  process restart require the dedicated authenticated Windows environment and
  cannot be proven by the local unit/UI checks alone.
- Desktop #419 still needs a manual UI check: select Luna V1 and V2, save each,
  restart CodexHub, and confirm the selected value and `(Default)` marker are
  retained. The CLI plan does not replace this Desktop verification.
- If the historical Session is unavailable in the isolated test fixture, stop
  with `legacy_session_fixture_unavailable`; do not fall back to a newly
  created Session and call #418 verified.
