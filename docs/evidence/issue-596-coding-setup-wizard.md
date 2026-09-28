# #596 ChatGPT Coding Setup wizard — verification note

Date: 2026-09-28

Branch: `feat/chatgpt-coding-wizard`

Review base: `a6d6b79b`

Implementation candidate: `2fb55b57`

## Implemented behavior

The production Runtime Settings page follows ADR-0019's five-step A flow. Saving full coding configuration returns immediately and asks for a manual component stop/start. The final step runs the fixed, side-effect-free `codexhub_setup_probe` through the managed runtime. Only a correlated real tool call/result for the current account, active configuration, and runtime instance marks coding setup complete. Text readiness, doctor flags, incomplete responses, and synthetic receipts cannot pass it.

Review corrections also prevent a cancelled probe's late success from overwriting cancellation, allow retry after a service restart interrupts a persisted `running` record, and release the in-process probe slot after evidence-write errors. Mobile step labels wrap within their own columns.

The real Responses probe now sends the native Codex turn metadata together with a matching, read-only trusted environment context. Its second request replays the original user message and the returned function call with distinct Item and Call identities before submitting the correlated function result. `response.failed` is classified as failure rather than an absent tool call.

## Local verification

- On earlier code candidate `68e07619`, focused HTTP/probe tests: **50 passed**.
- On earlier code candidate `68e07619`, `scripts/verify-linux.sh`: **0 failed legs**. Python core **3621 passed, 196 skipped, 283 subtests passed**; Python partition check complete; Rust **799 passed, 1 ignored**; Clippy passed; frontend build, custom-protocol desktop build, and physical pointer-input E2E passed. The later delta changes only the Python probe and its tests.
- On `2fb55b57`, focused probe/check/connection tests: **42 passed, 1 skipped**. Python core: **3623 passed, 196 skipped, 283 subtests passed**. Python partition check: **3983 total = 3819 core + 164 synthetic**, disjoint and complete. `git diff --check` passed; report-only quality gate ran.
- On earlier code candidate `68e07619`, frontend UI contract: **34 passed**. T3 collaborative browser inspected the isolated production settings server's first-run screen at 1280×800 and 390×844; five steps and bundled marks were present, with no prototype controls, horizontal page overflow, or English step-label overlap. The Chinese step-label widths were checked at 390px by replacing only the displayed labels in the isolated browser; this was a layout check, not a full Chinese-language flow.

## Live coding tool roundtrip on `2fb55b57`

An existing isolated runtime instance, separate from the daily default, had saved and active `full` mode, a signed-in account, ready Tunnel, selectable connector, and no pending restart. At `2026-09-28T14:52:25Z`, `start_probe` returned `state=passed`, `reason=null`, `live_attempted=true`, and generation `584ee3ccb6744bf2`. The fixed probe observed one `codexhub_setup_probe` function call with a Call ID and Item ID, submitted a token-bearing `function_call_output` using that Call ID, then found the matching token in the completed response. No credential, request body, or raw model output is recorded here. The existing runtime was neither restarted nor reconfigured.

## Outstanding acceptance

The default daily runtime remains in `browser-only` mode; the real roundtrip above used the previously configured isolated `full` instance. The earlier isolated browser run covered first-run presentation, not the saved-awaiting-restart, expired-account, connector-restriction, or tool-failure UI flows on an actual account. A fresh collaborative-browser snapshot attempt for this candidate failed at the preview automation client even after opening a new tab, so no new UI claim is made for `2fb55b57`. Deterministic HTTP/probe tests cover the underlying status and failure contracts, but do not replace #599's remaining manual browser inspection. Keep PR #600 draft and #599 open until those checks are recorded. The broader #567 real-client matrix and #592 daily-browser acceptance remain open.

## Review

`$code-review` compared `a6d6b79b...HEAD` on Standards and Spec before the final protocol delta. Documented-standard violations: **0**. Hard product mismatches in the implemented path: **0**. The Spec axis retains the #599 manual browser evidence gap above. Remaining Standards notes are judgement calls around private helper use, the single-file page, and the request-thread probe wait. The final protocol delta is under separate review.
