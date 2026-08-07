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

## Verification

- `py -3.13 -m pytest -q tests/test_routing.py -k "timeout or stream or incomplete or cancellation or 502 or reasoning"` — **186 passed, 427 deselected, 91 subtests passed** (18.92s).
- `py -3.13 -m pytest -q tests/test_proxy_event_logging.py` — **17 passed** (1.43s).
- `git diff --check` — **passed**.

The authenticated DeepSeek V4 Flash 0731 CLI case and interactive Desktop
comparison were not available in this worker. Model discovery was recorded
only as the sanitized names DeepSeek V4 Flash 0731 and GLM-5.2; no credentials,
raw reasoning, prompts, identifiers, tool payloads, or raw provider output was
retained. See `docs/evidence/issue-370/README.md` for the exact limitation and
verdict.

## Concerns

The live provider/CLI/Desktop gate remains unrun and must be performed by a
release operator with the configured authenticated route. Deterministic
classification evidence does not substitute for that external observation.
