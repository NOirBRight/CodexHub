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

- The 30-second frontend timeout bounds UI settlement but cannot cancel an already-dispatched Tauri command. The backend's process/lock wait is independently bounded to 10 seconds; repair script duration remains governed by the existing command runner. The UI does not automatically retry or loop after timeout.
- Windows package/process discovery and lock probing are covered through pure snapshots and injected controller outcomes rather than mutating a live Codex installation, per the task safety constraint.
