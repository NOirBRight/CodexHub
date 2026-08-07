# Issue #370 stream-failure classification evidence

Status: deterministic Gateway evidence complete. The implementation is a
narrow classification correction; it does not change the global SSE deadlines
and it does not replay a request after downstream output or a tool side effect
has been exposed.

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
| Malformed/incomplete SSE without terminal | 502 | `quick_transient` | no | yes | yes |
| Valid `response.completed` | 200 | — | yes | yes | no |

The two delayed-success cases remain completed 200 responses. An upstream
502 is classified at the response-header phase. A downstream close is
classified at the downstream-write phase and is never replayed after visible
output. An incomplete stream emits `upstream_stream_incomplete` with a
bounded transient class and forbids replay once output or a completed tool
side effect exists. A valid `response.completed` has no failure event.

## Local verification

Commands were run from the repository root with Python 3.13:

```powershell
py -3.13 -m pytest -q tests/test_routing.py -k "timeout or stream or incomplete or cancellation or 502 or reasoning"
# 186 passed, 427 deselected, 91 subtests passed in 18.92s

py -3.13 -m pytest -q tests/test_proxy_event_logging.py
# 17 passed in 1.43s

git diff --check
# passed (no whitespace errors)
```

The focused routing test loads the six-case fixture and asserts status,
failure class, terminal state, downstream-output state, retry-forbidden state,
and retry-safety class where applicable. The event-log test confirms that
classification and bounded SSE counts survive JSONL sanitization while prompt,
reasoning, identifier, tool, and authorization fields are absent.

## CLI/Desktop availability

The exact live-provider case (DeepSeek V4 Flash 0731 with a long reasoning
phase, plus the maintained comparison route) was not run in this worker. The
sanitized model-discovery result was **DeepSeek V4 Flash 0731** and
**GLM-5.2**; no credential or raw client/provider output was retained. A
dedicated authenticated Ollama Cloud CLI run and an interactive Desktop
session are release-operator evidence gates, not available deterministic test
inputs here. Consequently there are no live first-event timestamps,
inter-event gaps, final-event timestamps, or CLI/Desktop terminal observations
to report for this candidate.

## Verdict

The deterministic evidence supports the narrow Gateway correction: valid
completed streams are delivered as completed, while transport, incomplete,
upstream-error, and downstream-cancellation paths retain explicit bounded
classes and phase context. No blanket timeout increase, retry masking, raw
reasoning capture, or post-side-effect replay is justified by this evidence.
The live CLI/Desktop/provider gate remains explicitly unrun.
