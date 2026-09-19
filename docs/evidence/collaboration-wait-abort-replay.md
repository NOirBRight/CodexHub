# Interrupted Collaboration V2 wait replay

## Scope and observed failure

The user reported a subagent tool failure and requested a fix, iterative
Standards/Spec review, and an isolated E2E. A local Codex CLI 0.155.1 rollout
recorded this pair (identifiers and task content omitted):

```json
{"namespace":"collaboration","name":"wait_agent","arguments":"{\"timeout_ms\":120000}"}
{"output":"aborted by user after 104.0s"}
```

On continuation, Gateway rejected history with HTTP 400 and
`malformed_collaboration_result`. The client-owned interruption is a failed
tool result, not a successful JSON result. Accept its observed framing only
for Collaboration V2 `wait_agent`, preserving the original text. This change
does not establish a V1 interruption contract or support unobserved textual
variants. Argument, identity, and successful-result validation remain intact.

## Isolated HTTP replay E2E

```bash
./scripts/codexhub-python.sh -m pytest -q tests/test_collaboration_abort_e2e.py
```

Each case starts a separate Python process with an empty temporary Codex Home,
temporary user/config/data directories, an allowlisted environment without
provider credentials, and a temporary working directory. The existing
`GatewayHarness` starts the production HTTP handler and a scripted upstream on
random loopback ports, with synthetic authentication and local routing.
It neither discovers nor connects to a running user Gateway.

The two cases cover Chat Completions and adapted Responses upstreams:

1. Replay the observed interrupted-wait history through `/v1/responses`.
2. Assert the selected upstream receives the unchanged result and Call identity.
3. Assert the client receives the expected answer and exactly one successful
   terminal SSE event.
4. Continue for another turn with the interruption still in history.
5. Append extra text to the interruption result and assert HTTP 400 before any
   further upstream request.

The test scripts the client history and upstream answer. It verifies the
Gateway HTTP/translation/streaming path, not real Codex scheduling, clicking
interrupt, live-provider behavior, or the Desktop UI. Native namespace replay
is separately covered by the existing public-plan unit tests.

## Red/green evidence

In a temporary source copy, replacing only `collaboration_runtime_contract.py`
with its version at `12215dc85d53237c8f4260698c08649f7b66521e` makes both HTTP
E2E cases fail with `malformed_collaboration_result`. The candidate passes both
cases. Production processes and original conversation history are untouched.

Candidate validation on Linux: Python core 3045 passed, 179 skipped, and
267 subtests passed; the core/synthetic partition union is complete and
disjoint. The report-only quality scan completed with zero parse errors.
Windows and real-client/live-provider E2E were not run for this change.
