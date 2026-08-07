# Task 3 report: #370 stream-failure classification

## Outcome

Completed the deterministic #370 stream-classification fixture and bounded
proxy-event coverage. The six required classes now assert status,
`failure_class`, terminal state, downstream-output state, and retry safety:

- delayed first event followed by valid completion;
- long inter-event gap followed by valid completion;
- explicit upstream 502;
- downstream 499 after visible output;
- malformed/incomplete SSE without a terminal event; and
- valid `response.completed`.

The correction is narrow: downstream write failures record
`downstream_client_closed` with downstream-write phase and retry suppression,
and incomplete Responses streams record `quick_transient`, terminal false,
and retry suppression after output or completed tool side effects. No global
timeout was changed and no replay is permitted after exposure or side effects.
The production 502 boundary and emitted JSONL are now exercised directly, and
the failure-event context is allowlisted so injected prompt, reasoning,
response/tool payload, credential, and provider-local identifier fields do not
reach telemetry.

## Files

- `src-python/codex_proxy.py` — bounded classification fields at downstream
  write and incomplete-stream boundaries.
- `tests/test_routing.py` — deterministic six-case fixture assertions.
- `tests/test_proxy_event_logging.py` — bounded JSONL classification/count
  sanitization assertions.
- `tests/fixtures/issue_370_stream_classification.json` — synthetic redacted
  fixture.
- `docs/evidence/issue-370/README.md` — evidence, commands, limitation, and
  verdict.
- `docs/evidence/issue-370/stream-classification-summary.json` — sanitized
  case summary.
- `docs/evidence/issue-370/live-cli-comparison.json` — sanitized authenticated
  CLI timestamps, event gaps, terminal states, and Gateway classifications.

## Verification

- `py -3.13 -m pytest -q tests/test_routing.py -k "timeout or stream or incomplete or cancellation or 502 or reasoning"` — **187 passed, 427 deselected, 91 subtests passed** (18.38s).
- `py -3.13 -m pytest -q tests/test_proxy_event_logging.py` — **17 passed** (1.35s).
- `py -3.13 -m pytest -q --ignore=tests/test_real_client_e2e.py` — **2149 passed, 1 skipped, 473 subtests passed** (153.73s).
- `py -3.13 scripts/report_quality_gates.py` — report-only findings, **0 parse errors**.
- `git diff --check` — **passed**.

Authenticated Codex CLI 0.146.1 comparisons completed for DeepSeek V4 Flash
0731 and GLM-5.2. Both exited 0 with `turn.completed`; the Gateway recorded
`request_complete` status 200 and no failure class. GLM-5.2 completed after a
131,965 ms exposed CLI event gap. Only sanitized timestamps, event names/gaps,
terminal status, and bounded classifications were retained.

## Concerns

Codex `exec --json` does not expose individual raw SSE frame timestamps, and
the privacy-preserving collector intentionally discarded raw provider/client
content. The interactive Desktop observation remains unrun; the authenticated
CLI and Gateway terminal evidence is complete.
