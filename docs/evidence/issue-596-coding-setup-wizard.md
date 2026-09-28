# #596 ChatGPT Coding Setup wizard — verification note

Date: 2026-09-28

Branch: `feat/chatgpt-coding-wizard`

Review base: `a6d6b79b`

Implementation candidate: `68e07619`

## Implemented behavior

The production Runtime Settings page follows ADR-0019's five-step A flow. Saving full coding configuration returns immediately and asks for a manual component stop/start. The final step runs the fixed, side-effect-free `codexhub_setup_probe` through the managed runtime. Only a correlated real tool call/result for the current account, active configuration, and runtime instance marks coding setup complete. Text readiness, doctor flags, incomplete responses, and synthetic receipts cannot pass it.

Review corrections also prevent a cancelled probe's late success from overwriting cancellation, allow retry after a service restart interrupts a persisted `running` record, and release the in-process probe slot after evidence-write errors. Mobile step labels wrap within their own columns.

## Local verification on `68e07619`

- Focused HTTP/probe tests: **50 passed**.
- `scripts/verify-linux.sh`: **0 failed legs**. Python core **3621 passed, 196 skipped, 283 subtests passed**; Python partition check complete; Rust **799 passed, 1 ignored**; Clippy passed; frontend build, custom-protocol desktop build, and physical pointer-input E2E passed.
- Frontend UI contract: **34 passed**. `git diff --check` passed. Report-only quality gate was run.
- T3 collaborative browser against the isolated production settings server on this candidate: first-run screen inspected at 1280×800 and 390×844; five steps and bundled marks present, no prototype controls, no horizontal page overflow or English step-label overlap. The Chinese step-label widths were checked at 390px by replacing only the displayed labels in the isolated browser; this was a layout check, not a full Chinese-language flow.

## Outstanding acceptance

The default local runtime is installed and signed in, but its Tunnel state is `failed` and its connector is not selectable. A real account + Tunnel + connector tool roundtrip was therefore **not executed**. The isolated browser run covered first-run presentation, not the saved-awaiting-restart, expired-account, connector-restriction, or tool-failure UI flows on an actual account. Deterministic HTTP/probe tests cover their underlying status and failure contracts, but do not replace the #599 manual and live evidence. Keep PR #600 draft and #599 open until those checks are recorded. The broader #567 real-client matrix and #592 daily-browser acceptance remain open.

## Review

`$code-review` compared `a6d6b79b...HEAD` on Standards and Spec. Documented-standard violations: **0**. Hard product mismatches in the implemented path: **0**. The Spec axis retains the #599 manual/live evidence gap above. Remaining Standards notes are judgement calls around private helper use, the single-file page, and the request-thread probe wait.
