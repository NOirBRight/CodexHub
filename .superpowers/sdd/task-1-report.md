# Task 1 Report: Build Flavor Manifest And Generated Tauri Config

## Status

Completed.

## What Changed

- Added `config/build-flavors.json` with the required `stable` and `beta` entries and exact defaults from the task brief.
- Added `scripts/Build-TauriConfig.ps1` to generate a flavor-specific Tauri config under `.generated/tauri/<flavor>/` and print the generated config path.
- Updated `scripts/build-windows-release.ps1` to:
  - accept `-Flavor stable|beta`
  - load the flavor manifest
  - default the release base URL by flavor
  - generate and use the flavor-specific Tauri config
  - set and restore `CODEXHUB_FRONTEND_PORT`
  - set and restore `CODEXHUB_BUILD_FLAVOR`
  - set and restore `TAURI_CONFIG`
  - name installer assets and updater manifests from the flavor manifest
- Updated `frontend/vite.config.ts` to read `CODEXHUB_FRONTEND_PORT` with a stable fallback to `1420`.
- Updated `frontend/package.json` so `dev` and `preview` no longer hardcode port `1420`.
- Added UI contract coverage in `frontend/scripts/ui-contract.test.mjs` for the flavor manifest, build script wiring, and Vite/package port behavior.

## TDD Evidence

### RED

1. Added the new contract test:
   - `release build scripts support stable and beta flavor configuration`
2. Ran:

```powershell
cd frontend
npm run test:ui-contract -- --test-name-pattern "release build scripts support stable and beta flavor configuration"
```

Observed failure:

- `ENOENT: no such file or directory, open '...\\config\\build-flavors.json'`

This matched the expected missing-artifact failure from the brief.

### GREEN

After implementing the manifest, generated config script, and build/frontend wiring, ran the same focused contract test again:

```powershell
cd frontend
npm run test:ui-contract -- --test-name-pattern "release build scripts support stable and beta flavor configuration"
```

Observed result:

- PASS

## Tests / Results

- `npm run test:ui-contract -- --test-name-pattern "release build scripts support stable and beta flavor configuration"`  
  - First run: FAIL as expected in RED due to missing `config/build-flavors.json`
  - Second run: PASS after implementation

## Files Changed

- `config/build-flavors.json`
- `scripts/Build-TauriConfig.ps1`
- `scripts/build-windows-release.ps1`
- `frontend/package.json`
- `frontend/vite.config.ts`
- `frontend/scripts/ui-contract.test.mjs`

## Self-Review

- Stable defaults remain unchanged:
  - frontend `1420`
  - bridge `1421`
  - gateway `9099`
  - updater `latest.json`
  - product `CodexHub`
  - identifier `com.codexhub.app`
- Beta defaults match the brief exactly:
  - frontend `1430`
  - bridge `1431`
  - gateway `9109`
  - updater `latest-beta.json`
  - product `CodexHub Beta`
  - identifier `com.codexhub.beta`
- The change does not redesign gateway transport or alter protocol behavior.
- The release script now restores the new environment variables using the same pattern already used for signing keys.
- The generated config path is isolated under `.generated/tauri/<flavor>/` and the builder receives it through `TAURI_CONFIG`.

## Concerns

- Only the focused UI contract test requested by the task brief was run in this task.
- I did not run a full release build, so the new `Build-TauriConfig.ps1` path and `TAURI_CONFIG` handoff are validated by contract coverage and script inspection, not by an end-to-end packaging run in this task.

## Fix After Review

### Review Items Addressed

- Updated `scripts/build-windows-release.ps1` so the actual Tauri build command now passes the generated flavor config explicitly with `--config $generatedTauriConfigPath` while still setting and restoring `TAURI_CONFIG` for the existing Task 1 contract.
- Added post-build canonicalization logic in `scripts/build-windows-release.ps1` so if the expected canonical installer/signature pair is missing, the script finds the freshly generated version-matched NSIS installer with a matching `.sig` and moves both artifacts to the `releaseAssetPrefix`-based canonical names.
- Expanded `frontend/scripts/ui-contract.test.mjs` so the Task 1 contract now checks both guarantees: explicit `--config` usage in the Tauri build invocation and installer/signature canonicalization to `releaseAssetPrefix` naming.

### Changed Files

- `scripts/build-windows-release.ps1`
- `frontend/scripts/ui-contract.test.mjs`

### Test Command

```powershell
cd frontend
npm run test:ui-contract -- --test-name-pattern "release build scripts support stable and beta flavor configuration"
```

### Output Summary

- PASS
- `release build scripts support stable and beta flavor configuration`
- Full run result: `103` passed, `0` failed

### Self-Review

- The build script now preserves the original Task 1 environment-variable contract while using the Tauri CLI interface the reviewer called out as the actual supported config-selection mechanism.
- Canonical artifact naming is conservative: if the canonical files already exist they are left alone; fallback rename/move runs only when the expected canonical pair is missing.
- Stable naming remains intact because the canonical asset name still derives from `releaseAssetPrefix`, which stays `CodexHub` for stable and `CodexHubBeta` for beta.
- I did not run packaging; the new artifact canonicalization path is covered by script-level contract assertions and limited to the release script requested in this fix.

## Fix After Re-Review

### Changed Files

- `scripts/build-windows-release.ps1`
- `frontend/scripts/ui-contract.test.mjs`
- `.superpowers/sdd/task-1-report.md`

### Test Command

```powershell
cd frontend
npm run test:ui-contract -- --test-name-pattern "release build scripts support stable and beta flavor configuration"
```

### Output Summary

- PASS
- `release build scripts support stable and beta flavor configuration`
- Full run result: `103` passed, `0` failed

### Self-Review

- The release script now deletes only exact flavor/version NSIS installer and `.sig` candidates in `src-tauri\target\release\bundle\nsis` before the Tauri build, which prevents stale same-version canonical artifacts from surviving into the publish step.
- Cleanup stays narrow by targeting only the canonical `releaseAssetPrefix` name plus likely product-name variants for the current flavor/version; it does not remove directories or unrelated release files.
- Post-build resolution no longer depends on canonical files being absent. The script resolves the freshly generated installer/signature pair from the current flavor/version candidates, then canonicalizes to `releaseAssetPrefix` naming when needed.
- The UI contract now asserts both pieces of the regression fix: pre-build invalidation of flavor/version-specific artifacts and the removal of the old missing-canonical-only gate.
