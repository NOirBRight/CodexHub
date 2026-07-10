# Task 5 report — v0.1.4 Beta candidate integration

Status: implementation and branch verification complete; human maintainer approval,
live isolated E2E, and portable construction remain outside this report.

## Version changes

The development candidate is consistently `0.1.4-beta.1` in:

- `src-tauri/tauri.conf.json`
- `src-tauri/Cargo.toml`
- the `codexhub` package in `src-tauri/Cargo.lock`
- `frontend/package.json`
- both root package version fields in `frontend/package-lock.json`

`tests/test_release_channel_scripts.py` pins this six-field contract. The test was
observed RED against `0.1.4`, then GREEN after the manifest changes.

## Audit evidence and examples

`docs/reviews/v0.1.3-human-audit.md` now records:

- restoration of intended commits `38e99408` and `08d507af` as reviewed,
  patch-equivalent branch-native changes;
- `git rev-list --reverse --oneline 2c284bd0..08d507af` enumerating exactly those
  two commits, so no third intended v0.1.4 commit was missing;
- retained patch-equivalent fixes, the model/history/Beta/review series, and the
  exclusion of v0.2/TLS/FlClash/keepalive/proxy-node/transport work;
- the human-maintainer gate and the fact that AI review cannot replace it.

Active README, product, configuration, script, and training-document examples
were searched. No conflicting active Sol example was found. The audit now pins
first mention `OpenAI 5.6 Sol`, later mention `5.6 Sol`, API ID `gpt-5.6-sol`, and
external selector `codexhub-openai/gpt-5.6-sol`. Historical telemetry, fixtures,
and snapshots were not rewritten.

## Regression verification

Focused results before the full gates:

- frontend UI contracts: 119 passed, including desktop-started Web Bridge,
  web fallback, Windows portable custom-protocol pipeline, official account usage,
  saved official draft/order preservation, sortable official models, and connection
  plus background-history behavior;
- Python model/config/history/release matrix: 104 passed plus 5 subtests;
- Rust Web Bridge: 7 passed;
- Rust simple-slider metadata, owner-safe disconnect, official alias dedupe, and
  official discovery/dedupe/sort: 1 passed each.

No tracked Chrome password/cookie import implementation, test, or E2E entrypoint
exists in this repository (`rg` and history searches returned no such surface).
That reported regression therefore has no branch-local verification target and is
not claimed as passing.

## Complete quality gates

Final candidate results after the integration fix:

- `python -m pytest`: **763 passed, 1 skipped**.
- `cargo test --locked`: **266 passed**.
- `cargo clippy --locked --all-targets -- -D warnings`: **passed**.
- `npm run test:ui-contract`: **119 passed**.
- `npm run build`: **passed** (`tsc` and Vite production build, 1638 modules).
- `python scripts/report_quality_gates.py`: **exit 0, report-only**; 3 unused
  imports, 70 dead-function candidates, 127 duplicate names, and 0 parse errors.
- `git diff --check`: **passed**.

The first clippy run was RED on six test-only helpers exposed to non-test targets
and one `is_some` followed by `unwrap`. The minimal fix gates those helpers with
`#[cfg(test)]` and safely matches the optional default reasoning level. Clippy and
all 266 Rust tests then returned GREEN. No `cargo fmt` was run.

## E2E outcomes and blockers

Safe static/isolated gates passed through Rust and Python tests:

- Stable/Beta runtime homes, ports, owners, update endpoints, executable names,
  and takeover behavior are distinct;
- release-channel dry-runs accept a prerelease Beta from `dev`, require exact
  `main` for Stable, use immutable version URLs, and never publish Stable or Beta
  pointer manifests during tests;
- official Sol/Terra/Luna admission, metadata preservation, bare-ID dedupe, and
  rejection of unknown/disabled/denylisted/forged entries are covered;
- gateway routing tests accept discovered Sol/Terra/Luna and reject denylisted
  models rather than producing an unconditional `model is not allowed` result.

Live gates were not executed. Environment inspection found Codex CLI `0.142.5`,
Ollama `0.31.2`, an existing real `$HOME/.codex/auth.json`, and an Ollama API key,
but no OpenAI API key environment variable. More importantly, the available
`codex-tool-exposure-smoke.ps1` resolves the real App/CLI and reads/writes real
`$HOME/.codex` sessions. There is no checked-in harness that proves the App-managed
Sol/Terra/Luna discovery, live Gateway requests, or the GLM 5.2
`spawn -> wait -> close` lifecycle while keeping all session/config state isolated.
Running it would violate this task's prohibition on real Codex configuration and
session mutation. These three live gates are explicitly **blocked pending an
isolated disposable Codex home/runtime and approved live credentials/upstream**.

No real Codex config was switched or rewritten. No release, tag, merge, signing,
GitHub mutation, updater pointer publication, or issue closure was performed.

## Commits

- `307b5035` — `chore: set 0.1.4 beta candidate version`
- `9de80ce8` — `docs: record v0.1.4 candidate audit`
- `eb0418bc` — `fix: satisfy release candidate clippy gate`

## Self-review and candidate artifact

The diff is limited to version manifests and lockfiles, one version/audit contract
test, the tracked audit, and the minimal clippy fix. Excluded v0.2/TLS/FlClash/
keepalive/proxy-node work is absent. Historical evidence is unchanged.

No portable was built, per the integration assignment: the main agent will build
it only after final branch-wide review. Consequently there is no candidate archive,
native `CodexHubBeta.exe` metadata inspection, or SHA256 to report yet. The expected
future prefix remains `CodexHubBeta_0.1.4-beta.1_portable_...`.
