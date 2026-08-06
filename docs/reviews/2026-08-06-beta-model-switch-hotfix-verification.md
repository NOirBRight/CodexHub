# Beta model-switch hotfix verification

Date: 2026-08-06

## Candidate and review

- Candidate SHA: `eb84fa1` (exact reviewed HEAD).
- Base: `48efb0a` (`v0.1.8-beta.2`).
- Whole-branch review: clean. The final-fix scoped re-review addressed the request-history validation finding. No Critical or Important findings remain.

## Local verification (Python 3.13)

- Semantic, boundary, and integration checks: **278 passed**.
- Routing `model_switch`/`collaboration`: **2 passed, 602 deselected**.
- Real-client fixture contract: **1 passed, 145 deselected**.
- `git diff --check`: **passed**.
- `scripts/report_quality_gates.py`: exited **0** in report-only mode; its findings remain non-blocking report-only findings.
- Full `pytest -q`: **did not pass**. The first failure was `CatalogSyncTests.test_catalog_override_survives_managed_baseline_refresh_publication`, after **135 passed / 1 skipped**. The same test fails on clean base `48efb0a`; it is outside this hotfix diff and remains a pre-existing release blocker, not fixed here.

## Bounded CLI acceptance

- Real CLI: `codex-cli 0.146.1`.
- Candidate proxy endpoint: `http://127.0.0.1:9399/v1/responses`.

| Bounded sequence | Completed turns | HTTP responses | Fallback count | Collaboration boundary/protocol errors | Reconnect storms |
| --- | ---: | --- | ---: | ---: | ---: |
| Luna → DeepSeek → Luna | 3 | all 200 | 0 | 0 | 0 |
| DeepSeek → Luna | 2 | all 200 | 0 | 0 | 0 |

The record contains only bounded labels and counts; it does not include session/thread IDs or response text.

## Active-call boundary

Status: `bounded_not_run`. A noninteractive CLI cannot safely inject a concurrent model switch while a turn is in flight, so no pass is claimed.

## Release decision

No internal candidate was built and no release tag was created because the full Python gate and active-call gate are incomplete. Next action: fix or triage the pre-existing catalog test, rerun the full suite, and perform interactive active-call validation before candidate promotion.
