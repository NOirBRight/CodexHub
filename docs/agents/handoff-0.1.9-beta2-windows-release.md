# Handoff: publish 0.1.9 Beta 2 from Windows

Use this handoff when building, signing, and publishing the Windows assets for
`v0.1.9-beta.2`. The Linux preparation host pushed the release candidate but
did not create a tag or GitHub Release.

## Fixed release identity

- Version: `0.1.9-beta.2`
- Required commit: `20db44e152c35c708c495741a0d15b5d535cb325`
- Prepared from: `main`
- Tag to create only after all gates pass: `v0.1.9-beta.2`
- GitHub CI is currently disabled manually; Windows results are the release gate.

The version is already synchronized in `frontend/package.json`,
`frontend/package-lock.json`, `src-tauri/Cargo.toml`, `src-tauri/Cargo.lock`,
and `src-tauri/tauri.conf.json`.

## 1. Pin the checkout

```powershell
git fetch origin --prune
git checkout --detach 20db44e152c35c708c495741a0d15b5d535cb325
git status --short
git rev-parse HEAD
```

Completion criterion: the worktree is empty and `HEAD` is exactly
`20db44e152c35c708c495741a0d15b5d535cb325`. Stop on any mismatch.

Confirm that the release name is unused:

```powershell
git ls-remote origin refs/tags/v0.1.9-beta.2
gh release view v0.1.9-beta.2
```

Both commands must report no existing tag/release. Do not create the tag yet.

## 2. Run release gates

Run the manifest/runtime contracts:

```powershell
.\scripts\codexhub-python.cmd -m pytest -q `
  tests/test_release_channel_scripts.py `
  tests/test_python_runtime.py
```

Run frontend build and contracts:

```powershell
Push-Location frontend
npm ci
npm run build
npm run test:ui-contract
Pop-Location
```

Run Rust tests from `src-tauri`:

```powershell
Push-Location src-tauri
cargo test --locked -- --test-threads=1
Pop-Location
```

Run the Windows real-client matrix:

```powershell
.\scripts\Run-RealClientE2E.ps1
```

Beta 2 specifically requires the DSH injection-first apply/readback/detach checks
in `docs/agents/handoff-0.1.9-windows.md`: foreign providers and
`agent-default-model` survive; only the managed block/key are detached; mode
remains owner-only; restart requirement is `none`.

Completion criterion: every command passes and the DSH/GUI evidence uses a new
Windows output root. A `-CliOnly` run does not replace the DSH or GUI gate.

## 3. Dry-run the immutable release plan

```powershell
.\scripts\New-ReleaseChannelPlan.ps1 `
  -Version 0.1.9-beta.2 `
  -Commit HEAD `
  -DryRun
```

Completion criterion: the plan reports commit `20db44e...`, tag
`v0.1.9-beta.2`, `prerelease=true`, and one immutable release containing normal
and debug Windows assets plus the normal portable ZIP.

## 4. Build and sign

The default key path is
`$env:USERPROFILE\.codexhub\codexhub-updater.key`. Set its password only in
the process environment:

```powershell
$env:TAURI_SIGNING_PRIVATE_KEY_PASSWORD = '<signing-password>'

.\scripts\build-windows-release.ps1 `
  -Flavor normal `
  -Notes 'CodexHub 0.1.9 Beta 2'

.\scripts\build-windows-portable.ps1 -Flavor normal

.\scripts\build-windows-release.ps1 `
  -Flavor debug `
  -Notes 'CodexHub 0.1.9 Beta 2 Debug'
```

The build scripts validate each signature/manifest pair. Required assets:

```text
src-tauri\target\release\bundle\nsis\
  CodexHub_0.1.9-beta.2_x64-setup.exe
  CodexHub_0.1.9-beta.2_x64-setup.exe.sig
  latest.json

src-tauri\target\build-flavors\debug\release\bundle\nsis\
  CodexHub_0.1.9-beta.2_debug_x64-setup.exe
  CodexHub_0.1.9-beta.2_debug_x64-setup.exe.sig
  latest-debug.json

output\portable\
  CodexHub_0.1.9-beta.2_portable_20db44e.zip
```

Completion criterion: all seven files exist, manifests name version
`0.1.9-beta.2` and source revision `20db44e...`, and installer hashes are saved
with the Windows evidence.

## 5. Publish once

Set paths explicitly, then create the immutable prerelease. `gh release create`
creates the tag at the verified commit:

```powershell
$normal = '.\src-tauri\target\release\bundle\nsis'
$debug = '.\src-tauri\target\build-flavors\debug\release\bundle\nsis'
$portable = '.\output\portable\CodexHub_0.1.9-beta.2_portable_20db44e.zip'

gh release create v0.1.9-beta.2 `
  --target 20db44e152c35c708c495741a0d15b5d535cb325 `
  --title 'CodexHub 0.1.9 Beta 2' `
  --prerelease `
  --generate-notes `
  "$normal\CodexHub_0.1.9-beta.2_x64-setup.exe" `
  "$normal\CodexHub_0.1.9-beta.2_x64-setup.exe.sig" `
  "$normal\latest.json" `
  $portable `
  "$debug\CodexHub_0.1.9-beta.2_debug_x64-setup.exe" `
  "$debug\CodexHub_0.1.9-beta.2_debug_x64-setup.exe.sig" `
  "$debug\latest-debug.json"
```

Do not separately create or force-move the tag. Do not overwrite an existing
release. If publication partially fails, inspect the remote release before any
retry and upload only missing assets.

## 6. Verify the remote release

```powershell
gh release view v0.1.9-beta.2 `
  --json tagName,isPrerelease,targetCommitish,assets `
  --jq '{tagName,isPrerelease,targetCommitish,assets:[.assets[].name]}'
```

Completion criterion: the release is a prerelease, targets the exact candidate,
and lists exactly the seven expected assets. Install the normal NSIS asset on a
clean Windows profile and confirm launch, version display, Gateway health, and
one updater-manifest read before declaring the release complete.

## Release highlights

- Generic Codex Responses to third-party Chat Completions bridge.
- Progressive Chat streaming with exact terminal/error behavior.
- Collaboration V2 declarations, calls, results, history, and same-Home resume.
- Authenticated GLM-5.2, Kimi K2.7 Code, and DeepSeek V4 Flash 0731 Chat paths.
- Protocol-scoped custom/apply_patch adaptation with no model-specific fallback.
