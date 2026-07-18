# CI and manual verification

GitHub Actions runs the required PR validation for branches targeting `dev` and `main`. Local candidate checks are selected by `docs/agents/verification-policy.md`; they do not duplicate every CI job by default.

## CI jobs

- Python tests: `python -m pytest -q`
- Frontend build and UI contract: `npm ci`, `npm run build`, `npm run test:ui-contract` in `frontend/`
- Rust tests (normal and debug flavors): `cargo test --locked` in `src-tauri/`, plus a release-optimized flavor build
- Rust clippy: `cargo clippy --locked --all-targets -- -D warnings` in `src-tauri/`
- Release flavor contract: portable-build dry-run parity for the normal and debug flavors
- Rust safe_file Linux compile and tests: standalone `rustc --test` compile of `src-tauri/src/safe_file.rs` on Ubuntu, a `clippy-driver -D warnings` lint of the same file, and the resulting cross-language test binary. This job exists because `safe_file.rs` contains `cfg(unix)` FFI that the Windows-only Rust jobs never compile; it must stay free of crate dependencies so the standalone compile keeps working.

The Rust jobs create a temporary `src-tauri/resources/python/.ci-placeholder` file during CI because Tauri's resource glob requires at least one runtime Python resource file. The placeholder is not committed.

## Full manual fallback

Use all of these commands when GitHub Actions is unavailable and the change must be integrated. Before opening a normal PR, run only the local suites selected by the verification policy:

```powershell
python -m pytest -q

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

Do not commit generated frontend output, local Tauri resource placeholders, or `dist/` artifacts.
