# Beta model-switch hotfix verification

Date: 2026-08-06

## Candidate and review

- Candidate SHA: `ddb927b` (exact reviewed HEAD).
- Base: `48efb0a` (`v0.1.8-beta.2`).
- Whole-branch review: clean. The final-fix scoped re-review addressed the request-history validation finding. No Critical or Important findings remain.

## Local verification (Python 3.13)

- Semantic, boundary, and integration checks: **278 passed**.
- Routing model-switch/collaboration regression targets: **35 passed** (five
  existing structured-routing cases plus the 30-case V1/V2 isolation module).
- Catalog synchronization suite: **87 passed, 51 subtests passed**.
- Real-client fixture contract: **1 passed, 145 deselected**.
- `git diff --check`: **passed**.
- `scripts/report_quality_gates.py`: exited **0** in report-only mode; its findings remain non-blocking report-only findings.
- Full Python core gate (`--ignore=tests/test_real_client_e2e.py`): **did not
  pass** because exactly two untouched Issue108 smoke tests fail. The run
  otherwise completed with **2130 passed, 1 skipped, 467 subtests passed**.
  Excluding only those two pre-existing tests, the same gate passed with
  **2130 passed, 1 skipped, 2 deselected, 467 subtests passed**.
- The catalog failures that blocked the prior run are gone; the two fixture
  fixes only isolate the owner-key/HMAC setup and do not weaken production
  signature validation.

## Bounded CLI acceptance

- Real CLI: `codex-cli 0.146.1`.
- Candidate proxy endpoint: `http://127.0.0.1:9399/v1/responses`.

| Bounded sequence | Completed turns | HTTP responses | Fallback count | Collaboration boundary/protocol errors | Reconnect storms |
| --- | ---: | --- | ---: | ---: | ---: |
| Luna → DeepSeek → Luna | 3 | all 200 | 0 | 0 | 0 |
| DeepSeek → Luna | 2 | all 200 | 0 | 0 | 0 |

The record contains only bounded labels and counts; it does not include session/thread IDs or response text.

## Active-call boundary

Status: `bounded_not_run`. A noninteractive CLI cannot safely inject a
concurrent model switch while a turn is in flight, so no pass is claimed.
The app-server protocol exposes `thread/settings/update` for subsequent
turns, but no candidate-bound interactive Desktop session was available for a
safe in-flight tool-call switch. This remains a release gate.

## Release decision

No internal candidate was built and no release tag was created because the
full Python gate still has two pre-existing Issue108 failures and the
active-call gate is incomplete. The next actions are to decide whether those
Issue108 failures are a separate release blocker and to perform the isolated
interactive active-call validation before candidate promotion.
