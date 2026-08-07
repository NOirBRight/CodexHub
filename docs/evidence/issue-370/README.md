# Issue #370 stream-failure classification evidence

Status: deterministic Gateway evidence and authenticated Codex CLI comparison
complete. The implementation is a narrow classification correction; it does
not change the global SSE deadlines and it does not replay a request after
downstream output or a completed tool call has been exposed.

## Source of truth and fixture

The Gateway's existing phase fields remain authoritative: connect/TLS,
time-to-first-byte/event, inter-event gap, terminal event, client
cancellation, and Gateway deadline phase. The redacted fixture
[`stream-classification-summary.json`](stream-classification-summary.json)
records only bounded status, class, terminal, output, retry-safety, and event
counts. It contains no prompt, reasoning text, response or tool identifiers,
tool arguments/results, credentials, or raw provider output.

The six required classes are covered:

| Class | Status | `failure_class` | Terminal | Output started | Retry forbidden |
| --- | ---: | --- | :---: | :---: | :---: |
| Slow first event, then valid completion | 200 | — | yes | yes | no |
| Long inter-event gap, then valid completion | 200 | — | yes | yes | no |
| Explicit upstream 502 | 502 | `quick_transient` | no | no | yes (`suppressed_post_write`) |
| Downstream 499 after visible output | 499 | `downstream_client_closed` | no | yes | yes (`suppressed_post_exposure`) |
| Malformed/incomplete SSE without terminal | 502 | `quick_transient` | no | yes | yes (`suppressed_post_exposure`) |
| Valid `response.completed` | 200 | — | yes | yes | no |

The two delayed-success cases remain completed 200 responses. An upstream
502 is classified at the response-header phase. A downstream close is
classified at the downstream-write phase and is never replayed after visible
output. An incomplete stream emits `upstream_stream_incomplete` with a
bounded transient class and forbids replay once output or a completed tool
side effect exists. A valid `response.completed` has no failure event.

The upstream-502 fixture now drives the production pre-response open boundary
and then relays the resulting HTTP error. The incomplete-stream fixture records
`failure_phase=stream_body` and its retry-safety class. A separate completed
tool-call case proves that replay remains suppressed even when the incomplete
stream has no ordinary assistant text to replay safely.

## Local verification

Commands were run from the repository root with Python 3.13:

```powershell
py -3.13 -m pytest -q tests/test_routing.py -k "timeout or stream or incomplete or cancellation or 502 or reasoning"
# 187 passed, 427 deselected, 91 subtests passed in 18.38s

py -3.13 -m pytest -q tests/test_proxy_event_logging.py
# 17 passed in 1.35s

py -3.13 -m pytest -q --ignore=tests/test_real_client_e2e.py
# 2149 passed, 1 skipped, 473 subtests passed in 153.73s

git diff --check
# passed (no whitespace errors)
```

`py -3.13 scripts/report_quality_gates.py` completed in report-only mode with
zero parse errors; its dead-code and duplicate-name findings remain
non-blocking by repository policy.

The focused routing test loads the six-case fixture and asserts status,
failure class, failure phase, terminal state, downstream-output state,
retry-forbidden state, retry-safety class, terminal `response.completed`
delivery, and completed-tool replay suppression. The event-log test injects
prompt, reasoning, response/tool identifiers, tool arguments/results,
credentials, and a spoofed provider ID through the real pre-response failure
boundary. The emitted JSONL keeps the bounded configured provider name and
classification fields while retaining none of the injected private values.

## Authenticated Codex CLI comparison

Codex CLI 0.146.1 ran both configured routes ephemerally with a read-only
sandbox. [`live-cli-comparison.json`](live-cli-comparison.json) retains only
timestamps, exposed CLI event names/gaps, exit status, and bounded Gateway
classification fields.

| Route | Longest exposed CLI gap | Gateway terminal | CLI terminal |
| --- | ---: | --- | --- |
| DeepSeek V4 Flash 0731 | 14,175 ms | `request_complete` 200 | `turn.completed`, exit 0 |
| GLM-5.2 | 131,965 ms | `request_complete` 200 | `turn.completed`, exit 0 |

Both streams completed despite the reasoning interval; neither emitted a
Gateway transport-failure class. Codex `exec --json` does not expose individual
raw SSE frame timestamps, so the evidence records every exposed CLI JSON event
gap and the Gateway request/reasoning-summary/completion timestamps instead of
retaining provider frames. The interactive Desktop observation remains unrun.
No prompt, reasoning text, response/tool identifier, tool payload, credential,
or raw model output was retained.

## Verdict

The deterministic evidence supports the narrow Gateway correction: valid
completed streams are delivered as completed, while transport, incomplete,
upstream-error, and downstream-cancellation paths retain explicit bounded
classes and phase context. No blanket timeout increase, retry masking, raw
reasoning capture, or post-side-effect replay is justified by this evidence.
The authenticated CLI comparison supports that verdict; only the interactive
Desktop observation remains unrun.
