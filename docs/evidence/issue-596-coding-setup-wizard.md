# #596 ChatGPT Coding Setup wizard — verification note

Date: 2026-09-28

Branch: `feat/chatgpt-coding-wizard`

Review base: `a6d6b79b`

Implementation candidate: `bc6d48db` (protocol fix `2fb55b57`)

## Implemented behavior

The production Runtime Settings page follows ADR-0019's five-step A flow. Saving full coding configuration returns immediately and asks for a manual component stop/start. The final step runs the fixed, side-effect-free `codexhub_setup_probe` through the managed runtime. Only a correlated real tool call/result for the current account, active configuration, and runtime instance marks coding setup complete. Text readiness, doctor flags, incomplete responses, and synthetic receipts cannot pass it.

Review corrections also prevent a cancelled probe's late success from overwriting cancellation, allow retry after a service restart interrupts a persisted `running` record, and release the in-process probe slot after evidence-write errors. Mobile step labels wrap within their own columns.

The real Responses probe now sends the native Codex turn metadata together with a matching, read-only trusted environment context. Its second request replays the original user message and the returned function call with distinct Item and Call identities before submitting the correlated function result. `response.failed` is classified as failure rather than an absent tool call.

The activation step now shows the manual restart instruction only while the saved configuration is inactive. When the full runtime is active, it shows the ready state and one primary Continue action.

The account and connector steps make the external action primary before offering a recheck or extension download. The connector target is a single focusable link with button styling, without an interactive element nested inside it.

## Local verification

- On earlier code candidate `68e07619`, focused HTTP/probe tests: **50 passed**.
- On earlier code candidate `68e07619`, `scripts/verify-linux.sh`: **0 failed legs**. Python core **3621 passed, 196 skipped, 283 subtests passed**; Python partition check complete; Rust **799 passed, 1 ignored**; Clippy passed; frontend build, custom-protocol desktop build, and physical pointer-input E2E passed.
- On `2fb55b57`, focused probe/check/connection tests: **42 passed, 1 skipped**. Python core: **3623 passed, 196 skipped, 283 subtests passed**. Python partition check: **3983 total = 3819 core + 164 synthetic**, disjoint and complete. `git diff --check` passed; report-only quality gate ran.
- On earlier code candidate `68e07619`, frontend UI contract: **34 passed**. T3 collaborative browser inspected the isolated production settings server's first-run screen at 1280×800 and 390×844; five steps and bundled marks were present, with no prototype controls, horizontal page overflow, or English step-label overlap. The Chinese step-label widths were checked at 390px by replacing only the displayed labels in the isolated browser; this was a layout check, not a full Chinese-language flow.
- On `20d58030`, focused settings/probe tests: **52 passed**; frontend UI contract: **34 passed**. The first UI-contract invocation failed because the host's `cargo` mise shim had no selected toolchain; rerunning with the installed rustup `cargo` first on `PATH` passed.
- On `20d58030`, `scripts/verify-linux.sh` passed Python core (**3623 passed, 196 skipped, 283 subtests passed**), partition completeness, Clippy, frontend/custom-protocol build, and physical pointer-input E2E. Its first serial Rust leg had one PID-identity lifecycle failure in `start_replaces_running_managed_proxy_from_previous_bundle` (**798 passed, 1 failed, 1 ignored**). The exact test passed alone, then the complete serial Rust suite passed (**799 passed, 1 ignored**) on the same SHA. No production code was changed for this test.
- On final code candidate `bc6d48db`, focused settings/probe tests: **52 passed**; frontend UI contract: **34 passed**; `scripts/verify-linux.sh`: **0 failed legs**. Python core **3623 passed, 196 skipped, 283 subtests passed**; partitions complete; Rust **799 passed, 1 ignored**; Clippy, frontend/custom-protocol build, and physical pointer-input E2E passed. A prior attempt on this SHA again had one failing leg; the captured final complete run passed without changing code.

## Live coding tool roundtrip and page check

An existing isolated runtime instance, separate from the daily default, had saved and active `full` mode, a signed-in account, ready Tunnel, selectable connector, and no pending restart. At `2026-09-28T14:52:25Z`, `start_probe` returned `state=passed`, `reason=null`, `live_attempted=true`, and generation `584ee3ccb6744bf2`. The fixed probe observed one `codexhub_setup_probe` function call with a Call ID and Item ID, submitted a token-bearing `function_call_output` using that Call ID, then found the matching token in the completed response. No credential, request body, or raw model output is recorded here. The existing runtime was neither restarted nor reconfigured.

On `20d58030`, the production settings page was opened against that same isolated runtime through a Yoga loopback-only SSH forward. The browser's **Verify coding capability** action showed a running state, then **Tool roundtrip succeeded** and the **Coding tools ready** completion view. The persisted probe state was `passed`, `live_attempted=true`, checked at `2026-09-28T15:02:37Z`, with the same generation. Reviewing the active activation step showed only the ready notice and one **Continue** action; no restart demand or horizontal overflow at 1280px. Account, tunnel, connector, and verify step headings were inspected without changing credentials or runtime settings. The preview snapshot operation failed even on a public test page, so inspection used T3's browser DOM evaluation and click tools; there is no screenshot claim. The settings server and SSH forwards were stopped after inspection.

On `bc6d48db`, the same production page and HTTP server were driven in a temporary, credential-free scenario harness. Only the status response was controlled; the harness was not committed or bundled. T3 browser inspection at 390px found no horizontal overflow, no nested interactive elements, and one primary action in each state:

| State | Primary visible action |
| --- | --- |
| First run | Open ChatGPT |
| Saved, awaiting manual restart | Recheck component status (after the stop/start instruction) |
| Account expired | Open ChatGPT |
| Connector restricted | Open ChatGPT connector settings (one focusable link) |
| Tool result rejected | Verify coding capability (with failure reason and authorization fallback) |

The already-active connector view also showed **Continue** as its single primary action. The isolated live runtime was not altered for these failure scenarios. The temporary server, Yoga SSH forward, and scenario files were removed afterward.

## Outstanding acceptance

The default daily runtime remains in `browser-only` mode; the real roundtrips above used the previously configured isolated `full` instance. The abnormal UI states were inspected with controlled status responses, without expiring the real account or revoking its connector. #599's browser matrix and live tool check are covered for PR review; keep the issue open until merge. The broader #567 real-client matrix and #592 daily-browser acceptance remain open.

## Review

`$code-review` compared `a6d6b79b...20d58030` on Standards and Spec, including focused delta review after the original full review. Documented-standard violations: **0**. Hard product mismatches in the implemented path: **0**. The final `bc6d48db` HTML delta was checked directly against the same sources and browser states with **0 hard findings**. A final two-agent retry could not start because its Gateway model route returned `unsupported_model`; this did not affect runtime or local verification. Remaining Standards notes are judgement calls around private helper use, the single-file page, and the request-thread probe wait.
