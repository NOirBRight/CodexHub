# CI and manual verification

GitHub Actions runs the required PR validation for branches targeting `dev`
and `main` on fresh GitHub-hosted runners. Actions owns the trigger, exact-SHA
logs, artifacts, Check status, and readback. Local candidate checks are
selected by `docs/agents/verification-policy.md`; they do not duplicate every
CI job by default.

## CI jobs

Every workflow run creates one immutable **CI classifier** and one final
**`CI / gate`** check.  The classifier in
`scripts/ci/ci_change_plan.py` compares a pull request's merge-base to its
head and emits the job-selection booleans.  Formal jobs keep their existing
check names, but are skipped when their contract is unaffected.  A skipped
formal job is acceptable only when the classifier selected it as out of scope;
the gate fails on classifier failure, cancellation, or any selected job that
does not pass.

- **Python core**: `python -m pytest -q --ignore=tests/test_real_client_e2e.py --junitxml=.pytest-results/junit-core.xml --durations=0` when the classifier selects Python core.
- **Synthetic real-client contract**: when selected, it runs the synthetic module through `tests/fixtures/real_client_e2e/run-with-windows-watchdog.py` with an explicit 3600-second outer bound while retaining JUnit and per-test duration output. The unified classifier uses the existing synthetic dependency set; `python scripts/ci/check_python_test_partitions.py` still proves the core and synthetic partitions are disjoint and complete.
- Frontend build and UI contract: `npm ci`, `npm run build`, `npm run test:ui-contract` in `frontend/`
- Rust tests (normal and debug flavors): `cargo test --locked -- --test-threads=1` in `src-tauri/`, plus a release-optimized flavor build
- Rust clippy: `cargo clippy --locked --all-targets -- -D warnings` in `src-tauri/`
- Release flavor contract: portable-build dry-run parity for the normal and debug flavors
- Rust safe_file Linux compile and tests: standalone `rustc --test` compile of
  `src-tauri/src/safe_file.rs` on Ubuntu WSL2, a `clippy-driver -D warnings`
  lint of the same file, and the resulting cross-language test binary. The
  Hosted Linux uses Rust's official `x86_64-unknown-linux-musl` target with
  `rust-lld`, avoiding a host-wide C toolchain while still compiling and
  executing real Linux `cfg(unix)` code.
  This job exists because the Windows-only Rust jobs never compile that FFI;
  `safe_file.rs` must stay free of crate dependencies so the standalone compile
  keeps working.

### Triggers and path scheduling

Same-repository and fork PRs to `dev` and `main` always run the classifier and
gate.  The classifier selects Python, frontend, Rust, Linux `safe_file`, and
release checks from changed paths; documentation-only changes run only the
classifier and gate.  Unknown paths, planner/workflow changes, path-read
failures, pushes, schedules, and manual dispatches fail closed to the full
matrix.  Workflow triggers intentionally do not use `paths` filters, so the
required `CI / gate` check is never left permanently pending by GitHub.
Hosted runners are disposable and do not depend on the developer machine or a
repository self-hosted registration.

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

### Hosted runner contract

Routine CI must remain runnable on a clean GitHub-hosted Windows or Linux
virtual machine. Do not add requirements for a persistent service, desktop
session, local credentials, or a developer/Paseo workspace. Cargo caches may
contain only `~/.cargo/registry` and `~/.cargo/git`; never cache
`src-tauri/target`.

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
- `scripts/ci/python_test_plan.py`
- `scripts/ci/check_python_test_partitions.py`
- `tests/test_ci_python_plan.py`
- `pytest.ini`

If changed-path acquisition for a PR is missing, unavailable, or fails, the
classifier fails closed, selects every formal job, and the gate reports the
classifier failure instead of silently accepting a partial plan.

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
