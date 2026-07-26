# CI and manual verification

GitHub Actions runs the required PR validation for branches targeting `dev` and `main`. Local candidate checks are selected by `docs/agents/verification-policy.md`; they do not duplicate every CI job by default.

## CI jobs

- **Python core**: `python -m pytest -q --ignore=tests/test_real_client_e2e.py --junitxml=.pytest-results/junit-core.xml --durations=0`. Runs on every PR.
- **Synthetic real-client contract**: appears on every PR. For unrelated paths it succeeds explicitly as `not applicable` and does not start `scripts/Run-RealClientE2E.ps1`. For relevant paths, and for all non-PR events, it runs the synthetic module through `tests/fixtures/real_client_e2e/run-with-windows-watchdog.py` with an explicit 1800-second outer bound while retaining JUnit and per-test duration output. The planner at `scripts/ci/python_test_plan.py` decides which paths are relevant using the PR merge-base; `python scripts/ci/check_python_test_partitions.py` proves the core and synthetic partitions are disjoint and complete.
- Frontend build and UI contract: `npm ci`, `npm run build`, `npm run test:ui-contract` in `frontend/`
- Rust tests (normal and debug flavors): `cargo test --locked` in `src-tauri/`, plus a release-optimized flavor build
- Rust clippy: `cargo clippy --locked --all-targets -- -D warnings` in `src-tauri/`
- Release flavor contract: portable-build dry-run parity for the normal and debug flavors
- Rust safe_file Linux compile and tests: standalone `rustc --test` compile of `src-tauri/src/safe_file.rs` on Ubuntu, a `clippy-driver -D warnings` lint of the same file, and the resulting cross-language test binary. This job exists because `safe_file.rs` contains `cfg(unix)` FFI that the Windows-only Rust jobs never compile; it must stay free of crate dependencies so the standalone compile keeps working.

### Triggers

PRs to `dev` and `main` run both Python checks, the frontend job, Rust normal/debug tests, Rust clippy, release flavor contract, and Linux `safe_file`. Pushes to `dev` and `main` preserve the same job set. `workflow_dispatch` and a weekly Sunday 03:17 UTC `schedule` run the full validation, including the synthetic suite.

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
checker in one chained command:

```powershell
python -m pytest -q --ignore=tests/test_real_client_e2e.py --junitxml=.pytest-results/junit-core.xml --durations=0 && python -m pytest -q tests/test_real_client_e2e.py --junitxml=.pytest-results/junit-synthetic.xml --durations=0 && python scripts/ci/check_python_test_partitions.py
```

This executes the core suite, the synthetic suite, and proves the two partitions
are disjoint and complete.

To verify only the planner/path logic and collection completeness without
executing `tests/test_real_client_e2e.py`, use:

```powershell
python -m pytest -q tests/test_ci_python_plan.py && python scripts/ci/check_python_test_partitions.py
```

Do not commit generated frontend output, local Tauri resource placeholders, or `dist/` artifacts.
