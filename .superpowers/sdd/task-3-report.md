# Task 3 report — history preflight/process lifecycle and duplicate restart

## Status

Complete. Startup history inspection is read-only, explicit repair uses a bounded graceful release gate and transactional rollback, the UI invokes one bounded backend action and unlocks on every result, and Gateway Apply causes one restart.

## TDD evidence

### RED

- `cargo test --manifest-path src-tauri/Cargo.toml history::tests::windows_process_discovery -- --nocapture`
  - Failed because `classify_codex_processes`, `CodexProcessSnapshot`, and typed `CloseOutcome` did not exist.
- `cargo test --manifest-path src-tauri/Cargo.toml history::tests::relaunch_failure_returns_typed_error_and_rolls_repair_back -- --nocapture`
  - Failed with an untyped `Err("launch failed")`; no rollback result was returned.
- `cargo test --manifest-path src-tauri/Cargo.toml history::tests::startup_preflight_is_read_only_while_codex_is_stopped -- --nocapture`
  - Failed because startup attempted repair and returned `conflict` after exhausting the inspection-only runner.
- `cargo test --manifest-path src-tauri/Cargo.toml history::tests::requested_repair_returns_locked_files_reason_without_writing -- --nocapture`
  - Failed because `CloseOutcome::LockedFilesRemain` did not exist.
- `npm run test:ui-contract -- --test-name-pattern "history repair action|one Gateway Apply"`
  - Failed because the history action had no 30-second settlement/unlock contract and `GatewayPage` invoked a second restart.
- `npm run test:ui-contract -- --test-name-pattern "settings save restarts running gateway"`
  - Failed because App restart detection omitted Gateway port and request-timeout changes.

### GREEN

- Focused process and release tests: ChatGPT visible package UI, headless `codex.exe`, graceful-close timeout, background-only process, and locked files all pass.
- `cargo test --manifest-path src-tauri/Cargo.toml history::tests -- --nocapture` — 22 passed.
- `cargo test --manifest-path src-tauri/Cargo.toml` — 246 passed.
- `npm run test:ui-contract --prefix frontend` — 117 passed.
- `npm run build --prefix frontend` — TypeScript and Vite build passed.
- `git diff --check` — passed.

No automated test touched a real Codex home or controlled a real Codex process. Rust state-machine tests use isolated temporary homes plus injected command/process controllers. No real repair/migration probe was run.

## Commits

- `26f90d74 fix: discover packaged Codex desktop processes`
- `f584ee35 fix: make history repair lifecycle transactional`
- `9c531753 fix: settle history actions and restart gateway once`

## Self-review

- Windows discovery resolves `OpenAI.Codex`'s installed package path and classifies package-contained processes by executable path. A visible top-level window identifies the closeable UI, so `ChatGPT.exe` is accepted and headless `codex.exe` is not treated as the UI.
- Graceful close calls only `CloseMainWindow()` on visible package UI. It contains no force-kill path. The release gate is capped at 10 seconds and distinguishes UI timeout, background package processes, and locked Codex config/SQLite/JSONL files before any repair directory or file mutation.
- Startup clean, unified-history-disabled/separated-clean, drift, and unknown-provider states remain inspection-only. Only explicit action can enter repair.
- Repair retains status, counts, backup/receipt paths, and error/reason fields. Receipt failure and relaunch failure roll back config and bucket changes; relaunch failure removes the receipt and returns `conflict/relaunch_failed`.
- The frontend action makes exactly one `preflightUnifiedHistory(true)` call, preserves backend error/reason text, settles UI within 30 seconds, and clears busy state in `finally` without automatic retry.
- Gateway runtime changes, including port and request timeout, are restarted once by App; `GatewayPage` no longer performs its own restart.
- No TLS, version, or release files changed. `cargo fmt` was intentionally not run.

## Concerns

- History helper execution is now bounded in the backend; the frontend waits for the backend result and does not independently time out, cancel, retry, or loop. The shared backend operation deadline is 29 seconds, mutation helpers reserve the final 5 seconds for rollback, and the Codex graceful-close window remains capped at 10 seconds.
- Windows package/process discovery and lock probing are covered through pure snapshots and injected controller outcomes rather than mutating a live Codex installation, per the task safety constraint.

## Review follow-up

The Important review findings and receipt Minor were fixed with additional TDD cycles.

### Additional RED evidence

- `cargo test --manifest-path src-tauri/Cargo.toml history::tests::history_repair_gate_allows_only_one_mutation_at_a_time -- --nocapture`
  - Failed to compile because the backend single-flight gate did not exist.
- `cargo test --manifest-path src-tauri/Cargo.toml history::tests::inspection_helper_timeout_returns_typed_result_without_mutation -- --nocapture`
  - Failed because a timed-out inspection escaped as an untyped command error.
- Focused history tests initially failed because a successful explicit repair did not relaunch an initially stopped Codex app, timeout results used the generic repair reason, and receipt cleanup had no error-preserving finalizer.
- `npm run test:ui-contract --prefix frontend`
  - Failed because the action still used frontend `Promise.race`, Gateway restart planning still depended on `appStatus`, and `GatewayPage` independently generated restarted success copy.

### Additional GREEN evidence

- Backend mutation entry points now share an atomic single-flight guard. A concurrent request returns `conflict/repair_in_progress` before running helper commands or mutating files.
- The history-only deadline runner starts helper children without a visible Windows console, drains stdout/stderr, kills a hung helper at the shared deadline, and reserves rollback time. A real test terminates a five-second PowerShell sleep at a 100ms deadline.
- Inspection and mutation helper timeouts return typed `conflict/helper_timeout`; mutation timeouts roll back config and bucket work.
- Successful explicit repairs relaunch Codex even when it was stopped before the action. Read-only startup checks do not launch it.
- Relaunch rollback receipt deletion reports deletion errors and preserves the actual `receipt_path`; only success or `NotFound` clears it.
- Gateway restart planning accepts only the current `GatewayStatus.proxy_running` snapshot. Running produces one restart; stopped/missing status produces zero, independently of stale `appStatus`. The App returns the authoritative success message to `GatewayPage`.
- The UI waits for the bounded backend action and clears busy state in `finally`, with no automatic retry.

### Review commits and verification

- `a7e1b75b fix: bound and serialize history repairs`
- `a7b05b88 fix: use authoritative gateway restart state`
- `cargo test --manifest-path src-tauri/Cargo.toml` — 251 passed.
- `cargo test --manifest-path src-tauri/Cargo.toml history::tests -- --nocapture` — 27 passed.
- `npm run test:ui-contract --prefix frontend` — 118 passed.
- `npm run build --prefix frontend` — passed.
- `python scripts/report_quality_gates.py` — report-only run completed with `parse_errors: 0`.
- `git diff --check` — passed.
