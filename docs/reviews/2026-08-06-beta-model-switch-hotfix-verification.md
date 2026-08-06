# Beta model-switch hotfix verification

Date: 2026-08-06

## Candidate and review

- Production hotfix SHA: `ddb927b` (routing code; exact reviewed change).
- Issue108 gate-fix SHA: `d9c25b4` (exact scoped review; no release tag).
- Active-call harness SHA: `959b40d` (test-only synthetic evidence; no release tag).
- Active-call isolation hardening SHA: `8602858` (test-only; no release tag).
- Base: `48efb0a` (`v0.1.8-beta.2`).
- Whole-branch review: clean. The final-fix scoped re-review addressed the request-history validation finding. No Critical or Important findings remain.
- The Issue108 replay fix received an exact-SHA scoped review with no Critical or Important findings.

## Local verification (Python 3.13)

- Semantic, boundary, and integration checks: **278 passed**.
- Routing model-switch/collaboration regression targets: **35 passed** (five
  existing structured-routing cases plus the 30-case V1/V2 isolation module).
- Catalog synchronization suite: **87 passed, 51 subtests passed**.
- Real-client fixture contract: **1 passed, 145 deselected**.
- `git diff --check`: **passed**.
- `scripts/report_quality_gates.py`: exited **0** in report-only mode; its findings remain non-blocking report-only findings.
- Issue108 smoke suite: **33 passed**. The replay child processes now resolve
  the repository's Python 3.13 interpreter instead of an ambient Python 3.11
  that cannot parse the repository's type-parameter syntax.
- Active-call harness unit tests: **4 passed**; the combined focused run was
  **36 passed**.
- Full Python core gate (`--ignore=tests/test_real_client_e2e.py`): **2133
  passed, 1 skipped, 467 subtests passed**.
- The catalog fixture fixes only isolate the owner-key/HMAC setup and do not
  weaken production signature validation.

## Bounded CLI acceptance

- Real CLI: `codex-cli 0.146.1`.
- Candidate proxy endpoint: `http://127.0.0.1:9399/v1/responses`.

| Bounded sequence | Completed turns | HTTP responses | Fallback count | Collaboration boundary/protocol errors | Reconnect storms |
| --- | ---: | --- | ---: | ---: | ---: |
| Luna → DeepSeek → Luna | 3 | all 200 | 0 | 0 | 0 |
| DeepSeek → Luna | 2 | all 200 | 0 | 0 | 0 |

The record contains only bounded labels and counts; it does not include session/thread IDs or response text.

## Active-call boundary

Synthetic app-server probe: **passed** with `codex-cli 0.146.1`. The isolated
loopback fixture observed one active `shell_command`, accepted
`thread/settings/update` while that item was in flight, kept the original
request/model bound as `A → A`, and used the new model on the next turn (`B`).
Counts were one tool-call response, three upstream requests, zero fallback,
zero reconnect, and zero collaboration-boundary errors. The probe is recorded
by `scripts/e2e_codex_active_call_regression.py` and deliberately does not
claim live provider, CodexHub Gateway, or Desktop coverage.

Real provider/Desktop active-call evidence remains `bounded_not_run`; no
candidate-bound interactive Desktop session was available. This remains a
release gate.

## Release decision

No internal candidate was built and no release tag was created because the
real provider/Desktop active-call gate is incomplete. The next action is to
run that bounded manual validation before candidate promotion.
