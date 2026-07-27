# CI and manual verification

GitHub Actions runs the required PR validation for same-repository branches
targeting `dev` and `main`. Final checks use the repository-dedicated
self-hosted runners in `docs/agents/self-hosted-runner.md`; Actions still owns
the trigger, exact-SHA logs, artifacts, Check status, and readback. Local
candidate checks are selected by `docs/agents/verification-policy.md`; they do
not duplicate every CI job by default.

## CI jobs

- **Python core**: `python -m pytest -q --ignore=tests/test_real_client_e2e.py --junitxml=.pytest-results/junit-core.xml --durations=0`. Runs on every PR.
- **Synthetic real-client contract**: appears on every PR. For unrelated paths it succeeds explicitly as `not applicable` and does not start `scripts/Run-RealClientE2E.ps1`. For relevant paths, and for all non-PR events, it runs the synthetic module through `tests/fixtures/real_client_e2e/run-with-windows-watchdog.py` with an explicit 3600-second outer bound while retaining JUnit and per-test duration output. The planner at `scripts/ci/python_test_plan.py` decides which paths are relevant using the PR merge-base; `python scripts/ci/check_python_test_partitions.py` proves the core and synthetic partitions are disjoint and complete.
- Frontend build and UI contract: `npm ci`, `npm run build`, `npm run test:ui-contract` in `frontend/`
- Rust tests (normal and debug flavors): `cargo test --locked` in `src-tauri/`, plus a release-optimized flavor build
- Rust clippy: `cargo clippy --locked --all-targets -- -D warnings` in `src-tauri/`
- Release flavor contract: portable-build dry-run parity for the normal and debug flavors
- Rust safe_file Linux compile and tests: standalone `rustc --test` compile of
  `src-tauri/src/safe_file.rs` on Ubuntu WSL2, a `clippy-driver -D warnings`
  lint of the same file, and the resulting cross-language test binary. The
  self-hosted runner uses Rust's official
  `x86_64-unknown-linux-musl` target with `rust-lld`, avoiding a host-wide C
  toolchain while still compiling and executing real Linux `cfg(unix)` code.
  This job exists because the Windows-only Rust jobs never compile that FFI;
  `safe_file.rs` must stay free of crate dependencies so the standalone compile
  keeps working.

### Triggers

Same-repository PRs to `dev` and `main` run both Python checks, the frontend
job, Rust normal/debug tests, Rust clippy, release flavor contract, and Linux
`safe_file`. Fork PRs are intentionally denied access to the trusted
self-hosted runners by a host-owned pre-job hook outside the checkout; the
workflow's matching job conditions are defence-in-depth, not the trust
boundary. Pushes to `dev` and `main` preserve the same job set.
`workflow_dispatch` defaults to the full validation and supports the bounded
`runner-smoke` scope for runner provisioning. The weekly Sunday 03:17 UTC
`schedule` runs the full validation, including the synthetic suite.

The Windows jobs select
`[self-hosted, Windows, X64, codexhub-ci-windows-x64]`. The Linux-only
`safe_file` job selects
`[self-hosted, Linux, X64, codexhub-ci-linux-x64]`, so its `cfg(unix)` code is
still compiled and exercised on Linux.

Windows jobs use the runner's pre-provisioned Python 3.13, Node.js 22, and Rust
1.97.1 toolchains and fail closed on version drift. They do not call
`actions/setup-python` or `actions/setup-node`: those setup actions can require
machine-level cleanup permissions that a non-administrator runner correctly
does not have. Python jobs create a checkout-local virtual environment before
installing test dependencies.

Every final job has an explicit timeout. These are safety bounds, not Worker
budgets; record the first frozen-SHA self-hosted durations and tighten them in a
separate reviewed change if the observed distribution supports it. Never move
the complete suite into Worker or Repair rounds to consume the timeout budget
earlier.

An Actions checkout is not assumed to be a Paseo-managed Workspace. A test that
requires a live Paseo Workspace must either remain a local verification gate or
return a typed `not_applicable_unmanaged_checkout` result in CI. Missing Paseo
state in an unmanaged checkout is not a product regression and must not be
reported as one.

### Synthetic real-client contract relevant surface

A PR is considered relevant for the synthetic check when it touches any of the following:

- `scripts/Run-RealClientE2E.ps1` (the E2E runner)
- `tests/test_real_client_e2e.py` (the synthetic module)
- `tests/fixtures/real_client_e2e/**` (E2E fixtures and the Windows watchdog runner)
- `docs/agents/real-client-e2e.md` (real-client operator documentation)
- `.github/workflows/ci.yml`
- `scripts/ci/python_test_plan.py`
- `scripts/ci/check_python_test_partitions.py`
- `tests/test_ci_python_plan.py`
- `pytest.ini`

If changed-path acquisition for a PR is missing, unavailable, or fails, the check fails closed and runs the synthetic suite.

The Rust jobs create a temporary `src-tauri/resources/python/.ci-placeholder` file during CI because Tauri's resource glob requires at least one runtime Python resource file. The placeholder is not committed.

## Full manual fallback

Use all of these commands when GitHub Actions is unavailable and the change must be integrated. Before opening a normal PR, run only the local suites selected by the verification policy:

```powershell
python -m pytest -q --ignore=tests/test_real_client_e2e.py --junitxml=.pytest-results/junit-core.xml --durations=0
python tests/fixtures/real_client_e2e/run-with-windows-watchdog.py --timeout-seconds 3600 -- `
  python -m pytest -q tests/test_real_client_e2e.py --junitxml=.pytest-results/junit-synthetic.xml --durations=0
python scripts/ci/check_python_test_partitions.py

Push-Location frontend
npm ci
npm run build
npm run test:ui-contract
Pop-Location

New-Item -ItemType Directory -Force -Path src-tauri/resources/python | Out-Null
Set-Content -Path src-tauri/resources/python/.ci-placeholder -Value ''
Push-Location src-tauri
cargo test --locked
cargo clippy --locked --all-targets -- -D warnings
Pop-Location
```

### One-command Python fallback

For changes that touch only Python, run both partitions and the completeness
checker in one chained command. The synthetic partition must use the checked-in
Windows watchdog:

```powershell
python -m pytest -q --ignore=tests/test_real_client_e2e.py --junitxml=.pytest-results/junit-core.xml --durations=0 && `
python tests/fixtures/real_client_e2e/run-with-windows-watchdog.py --timeout-seconds 3600 -- `
  python -m pytest -q tests/test_real_client_e2e.py --junitxml=.pytest-results/junit-synthetic.xml --durations=0 && `
python scripts/ci/check_python_test_partitions.py
```

This executes the core suite, the watchdog-bounded synthetic suite, and proves
the two partitions are disjoint and complete.

To verify only the planner/path logic and collection completeness without
executing `tests/test_real_client_e2e.py`, use:

```powershell
python -m pytest -q tests/test_ci_python_plan.py && python scripts/ci/check_python_test_partitions.py
```

Do not commit generated frontend output, local Tauri resource placeholders, or `dist/` artifacts.
