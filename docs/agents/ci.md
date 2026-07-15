# CI and manual verification

GitHub Actions runs the required PR validation for branches targeting `dev` and `main`. Local candidate checks are selected by `docs/agents/verification-policy.md`; they do not duplicate every CI job by default.

## CI jobs

- Python tests: `python -m pytest -q`
- Frontend build and UI contract: `npm ci`, `npm run build`, `npm run test:ui-contract` in `frontend/`
- Rust tests: `cargo test --locked` in `src-tauri/`
- Rust clippy: `cargo clippy --locked --all-targets -- -D warnings` in `src-tauri/`

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
