# Beta model-switch hotfix verification

Date: 2026-08-06

## Candidate and review

- Production hotfix SHA: `ddb927b` (routing code; exact reviewed change).
- Issue108 gate-fix SHA: `d9c25b4` (exact scoped review; no release tag).
- Active-call harness SHA: `959b40d` (test-only synthetic evidence; no release tag).
- Active-call isolation hardening SHA: `8602858` (test-only; no release tag).
- Active-call cleanup sanitization SHA: `2639a0c` (test-only; no release tag).
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
  **37 passed**.
- Full Python core gate (`--ignore=tests/test_real_client_e2e.py`): **2137
  passed, 1 skipped, 467 subtests passed**.
- The catalog fixture fixes only isolate the owner-key/HMAC setup and do not
  weaken production signature validation.

## CLI-only build and compatibility evidence

- The exact-source debug portable build **succeeded** for source SHA
  `a7168e98581e1706f2782d5663b208e74751d642`.
- The same-identifier candidate runner failed because the current
  `D:\CodexHub-Dev\codexhub.exe` instance held the singleton. No sensitive
  process or session details are recorded here.
- A test-only variant that changed only the Tauri identifier, from the same
  source SHA (explicitly **not a production candidate**), ran
  `Run-RealClientE2E.ps1 -CliOnly`: **8/8 passed**.
- Tool versions for that run: Codex CLI **0.146.0**, OpenCode **1.18.6**, Pi
  **0.80.6**, and OMP **17.2.2**.

## Bounded CLI acceptance

- Real CLI: `codex-cli 0.146.1`.
- Candidate proxy endpoint: `http://127.0.0.1:9399/v1/responses`.

| Bounded sequence | Completed turns | HTTP responses | Fallback count | Collaboration boundary/protocol errors | Reconnect storms |
| --- | ---: | --- | ---: | ---: | ---: |
| Luna → DeepSeek → Luna | 3 | all 200 | 0 | 0 | 0 |
| DeepSeek → Luna | 2 | all 200 | 0 | 0 | 0 |

The record contains only bounded labels and counts; it does not include session/thread IDs or response text.

## Active-call boundary

Synthetic app-server probe: **passed** with `codex-cli 0.146.0`. The isolated
loopback fixture observed one active `shell_command`, accepted
`thread/settings/update` while that item was in flight, kept the original
request/model bound as `A → A`, and used the new model on the next turn (`B`).
Counts were one tool-call response, three upstream requests, zero fallback,
zero reconnect, and zero collaboration-boundary errors. The probe is recorded
by `scripts/e2e_codex_active_call_regression.py` and deliberately does not
claim live provider, CodexHub Gateway, or Desktop coverage.

The CLI-only active-call harness and its cleanup checks both **passed** in this
round.

Desktop/GUL validation is intentionally out of scope for this CLI-only
authorization and is deferred to later manual validation; it is not a current
Beta2 release gate. The CLI-only acceptance is the gate for this verification.

## Release decision

An internal candidate was built, and the CLI-only test variant passed. The
formal exact-candidate CLI gate still must be rerun after closing the existing
instance; no formal release tag was created.
