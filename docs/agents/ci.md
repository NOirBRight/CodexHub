# Local verification

GitHub Actions CI is disabled for this project. It is not a merge, release, or
issue gate, and no candidate may wait for or claim a Hosted `CI / gate` result.
Historical workflow runs and the matrix below are retained only as reference.
All current candidates are verified locally on the exact candidate SHA using
`docs/agents/verification-policy.md`.

## Historical Hosted matrix (non-gating)

The former workflow created one immutable **CI classifier** and one final
**`CI / gate`** check. The classifier in
`scripts/ci/ci_change_plan.py` compares a pull request's merge-base to its
head and emits the job-selection booleans.  Formal jobs keep their existing
check names, but are skipped when their contract is unaffected.  A skipped
formal job is acceptable only when the classifier selected it as out of scope;
the historical gate failed on classifier failure, cancellation, or any
selected job that did not pass. These jobs are not run by the current release
process.

- **Python core**: `python -m pytest -q --ignore=tests/test_real_client_e2e.py --junitxml=.pytest-results/junit-core.xml --durations=0` when the classifier selects Python core.
- **Synthetic real-client contract**: when selected, it runs the synthetic module through `tests/fixtures/real_client_e2e/run-with-windows-watchdog.py` with an explicit 3600-second outer bound while retaining JUnit and per-test duration output. The unified classifier uses the existing synthetic dependency set; `python scripts/ci/check_python_test_partitions.py` still proves the core and synthetic partitions are disjoint and complete.
- Frontend build and UI contract: `npm ci`, `npm run build`, `npm run test:ui-contract` in `frontend/`
- Rust tests (normal and debug flavors): `cargo test --locked -- --test-threads=1` in `src-tauri/`, plus a release-optimized flavor build
- Rust clippy: `cargo clippy --locked --all-targets -- -D warnings` in `src-tauri/`
- Release flavor contract: portable-build dry-run parity for the normal and debug flavors
- Rust safe_file Linux compile and tests: standalone `rustc --test` compile of
  `src-tauri/src/safe_file.rs` on the Hosted `ubuntu-24.04` runner, a `clippy-driver -D warnings`
  lint of the same file, and the resulting cross-language test binary. The
  Hosted Linux uses Rust's official `x86_64-unknown-linux-musl` target with
  `rust-lld`, avoiding a host-wide C toolchain while still compiling and
  executing real Linux `cfg(unix)` code.
  This job exists because the Windows-only Rust jobs never compile that FFI;
  `safe_file.rs` must stay free of crate dependencies so the standalone compile
  keeps working.

### Former triggers and path scheduling

The former workflow ran the classifier and gate for pull requests, pushes,
schedules, and manual dispatches. Its path planner and fail-closed behavior
remain useful references for selecting the equivalent local suites, but no
GitHub check is required or authoritative.

Windows jobs pin `windows-2025`; the Linux-only `safe_file` job pins
`ubuntu-24.04`, so its `cfg(unix)` code is compiled and exercised on Linux.
Each job installs or selects the exact Python 3.13, Node.js 22, and Rust
1.97.1 toolchains it needs. Python jobs create a checkout-local virtual
environment before installing test dependencies. Rust jobs force one test
thread because the suite contains process-wide lifecycle fixtures.

Every final job has an explicit timeout. These are safety bounds, not Worker
budgets; record the first frozen-SHA Hosted durations and tighten them in a
separate reviewed change if the observed distribution supports it. Never move
the complete suite into Worker or Repair rounds to consume the timeout budget
earlier.

An Actions checkout is not assumed to be a Paseo-managed Workspace. A test that
requires a live Paseo Workspace must either remain a local verification gate or
return a typed `not_applicable_unmanaged_checkout` result in CI. Missing Paseo
state in an unmanaged checkout is not a product regression and must not be
reported as one.

### Former Hosted runner contract (historical)

The former routine CI ran on clean GitHub-hosted Windows or Linux virtual
machines. It is disabled and must not be restarted to unblock a candidate.
The old contract remains documented for provenance only. Cargo caches in any
local verification must contain only `~/.cargo/registry` and `~/.cargo/git`;
never cache `src-tauri/target`.

The former repository self-hosted runners are retired. Their historical
registration and de-registration record is kept in
`docs/agents/self-hosted-runner.md`; it is not a prerequisite for CI and must
not be restarted to unblock a PR. True real-client Desktop/CLI E2E remains a
release-operator procedure documented in `docs/agents/real-client-e2e.md`.

### Synthetic real-client contract relevant surface

A PR is considered relevant for the synthetic check when it touches any of the following:

- `scripts/Run-RealClientE2E.ps1` (the E2E runner)
- `tests/test_real_client_e2e.py` (the synthetic module)
- `tests/fixtures/real_client_e2e/**` (E2E fixtures and the Windows watchdog runner)
- `docs/agents/real-client-e2e.md` (real-client operator documentation)
- `.github/workflows/ci.yml`
- `scripts/ci/ci_change_plan.py`
- `scripts/ci/python_test_plan.py`
- `scripts/ci/check_python_test_partitions.py`
- `tests/test_ci_change_plan.py`
- `tests/test_ci_python_plan.py`
- `pytest.ini`

If changed-path acquisition for a PR is missing, unavailable, or fails, the
classifier fails closed, selects every formal job, and the gate reports the
classifier failure instead of silently accepting a partial plan.

The Rust jobs create a temporary `src-tauri/resources/python/.ci-placeholder` file during CI because Tauri's resource glob requires at least one runtime Python resource file. The placeholder is not committed.

## Full local verification matrix

For `standard` and `strict` candidates, run the affected suites selected by the verification policy. Use the following complete matrix when the changed boundary spans the whole product or when a release gate explicitly requires it:

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

To verify only the planner/path logic, unified/legacy synthetic parity, and
collection completeness without executing `tests/test_real_client_e2e.py`, use:

```powershell
python -m pytest -q tests/test_ci_python_plan.py tests/test_ci_change_plan.py && python scripts/ci/check_python_test_partitions.py
```

Do not commit generated frontend output, local Tauri resource placeholders, or `dist/` artifacts.
