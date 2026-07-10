# Task 2 report

## Result

- Third-party `ultra` requests are rejected with HTTP 400 and the existing OpenAI-compatible plus `codexhub_error` shape; official Ultra remains accepted.
- Third-party multi-agent tool translation remains independent of reasoning effort. Existing routing coverage verifies `multi_agent_v1` spawn calls and structured tool history preservation.
- Model endpoint tests and upstream-format probes now inject `gateway_client_key` only for the current HTTP loopback Gateway and configured port, and only when no explicit API key is supplied.
- The CLI smoke uses the App-managed Codex CLI when available, retains `-CodexCommand`, defaults to `--ephemeral`, parses stdout JSONL directly, and verifies ordered spawn/wait/close events, stable thread identity, completed child state, and sentinel output.

## TDD evidence

### Third-party Ultra validation

- RED/GREEN slice committed as `3551ca5f` (`fix: reject third-party ultra reasoning`).
- Regression tests cover nested Responses `reasoning.effort`, top-level Chat Completions `reasoning_effort`, OpenAI-compatible error metadata, and official Ultra passthrough.

### Local Gateway key injection

- Preserved the inherited uncommitted RED test before editing production code.
- Initial valid RED command:
  - `cargo test --manifest-path src-tauri/Cargo.toml model_endpoint_test_injects_current_loopback_gateway_key_when_key_is_blank -- --nocapture`
  - Result: 1 failed; the mock received the request but the assertion for `authorization: bearer local-test-key` failed.
- Minimal GREEN introduced a shared Gateway-aware key resolver used by both model endpoint tests and provider probes.
- GREEN command:
  - `cargo test --manifest-path src-tauri/Cargo.toml gateway_key -- --nocapture`
  - Result: 3 passed, covering local injection, explicit-key preservation, and rejection for mismatched port, remote host, HTTPS loopback, and non-Gateway path.
- Focused commit: `23a1e417` (`fix: restore local gateway auth for model probes`).

### App-managed CLI smoke

- The current branch already contained the smoke implementation and contract tests. Task 2 verified rather than duplicated it.
- `tests/test_smoke_scripts.py` checks App bundle resolution, explicit override, `--ephemeral`, direct JSONL lifecycle parsing, ordered tools, stable receiver IDs, completed state, sentinel output, and aggregate failure handling.

## Verification

- `cargo test --manifest-path src-tauri/Cargo.toml`
  - 240 passed, 0 failed.
- `pytest -q tests/test_routing.py tests/test_smoke_scripts.py`
  - 365 passed, 67 subtests passed.
- `python scripts/report_quality_gates.py`
  - Report-only exit 0; parse errors 0. Existing repository findings: 3 unused imports, 70 dead functions, 124 duplicate function names. No finding was changed as part of this task.
- `git diff --check`
  - Passed.

## Self-review

- Scope is limited to reasoning validation, local Gateway model-test/probe authentication, regression tests, and this report.
- Explicit keys take precedence before any settings lookup.
- Injection is restricted to `http` loopback hosts (`127.0.0.1`, `localhost`, `::1`), the configured Gateway port, and root/`v1` Gateway paths.
- No third-party Ultra metadata was enabled; no official Ultra metadata was changed.
- No TLS, transport, keepalive, version, or publishing changes were made.
- Accidental formatter-only changes outside `models.rs` were removed before committing.

## Concerns / deferred work

- Live Ollama Cloud / Codex App CLI E2E was not run because it depends on local App state and credentials. Per the brief, live E2E remains for Task 5; deterministic routing and script-contract tests passed here.
- The report-only quality gate findings are repository-wide pre-existing observations and remain non-blocking.

## Review fixes

The Task 2 review identified five compatibility gaps; all were handled test-first.

- RED: a third-party Responses request with string-form `reasoning: "ultra"` reached upstream with HTTP 200. GREEN: string reasoning now participates in the same third-party Ultra validation; supported string `high` and official string `ultra` remain accepted.
- RED: configured external catalog levels `ultra` and `turbo` were exported. GREEN: external `supported_reasoning_levels` is filtered to `low`, `medium`, `high`, `xhigh`, and `max` while preserving configured order.
- RED: smoke contract tests found no Windows command-shim launcher and accepted substring statuses such as `not-completed`. GREEN: `.cmd`/`.bat` launch through `cmd.exe /d /s /c` with escaped command/arguments, `.exe` remains direct, and parsed status must equal case-sensitive `completed`.
- Added focused coverage showing supported third-party `high` effort remains independent of explicit `spawn_agent -> wait_agent -> close_agent` normalization.
- Verified the shim path against the installed `codex.cmd --version`: exit 0, `codex-cli 0.142.5`.

Review verification:

- `pytest -q tests/test_routing.py tests/test_catalog_sync.py tests/test_smoke_scripts.py`
  - 408 passed, 72 subtests passed.
- PowerShell parser for `scripts/codex-tool-exposure-smoke.ps1`
  - No parse errors.
- `git diff --check`
  - Passed.
