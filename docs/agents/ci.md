# CI and manual verification

GitHub Actions runs the required PR validation for branches targeting `dev` and `main`.

## CI jobs

- Python tests: `python -m pytest -q`
- Frontend build and UI contract: `npm ci`, `npm run build`, `npm run test:ui-contract` in `frontend/`
- Rust tests: `cargo test --locked` in `src-tauri/`
- Rust clippy: `cargo clippy --locked --all-targets -- -D warnings` in `src-tauri/`

The Rust jobs create a temporary `src-tauri/resources/python/.ci-placeholder` file during CI because Tauri's resource glob requires at least one runtime Python resource file. The placeholder is not committed.

## Manual fallback

Use these commands when GitHub Actions is unavailable or before opening a PR:

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
