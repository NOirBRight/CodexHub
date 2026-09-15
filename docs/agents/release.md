# Release

A product release is one GitHub Release whose Linux and Windows artifacts
are built from the same SHA. Publication is that GitHub Release. A `main`
merge is a follow-up, not a gate.

## Hosts

| OS | Where | Command surface |
|---|---|---|
| Linux | This Linux workstation | `./scripts/build-linux-portable.sh`, `./scripts/build-linux-release.sh` |
| Windows | SSH host `yoga` (`~/.ssh/config`) | `scripts/build-windows-portable.ps1`, `scripts/build-windows-release.ps1` |

Windows work always goes through `ssh yoga`. Do not wait for a local Windows
GUI, a VM snapshot, or GitHub Actions.

Yoga already has an operator worktree at `D:\Workstation\CodexHub`. Leave
it untouched. Build and live Windows gates use a new isolated checkout:

`D:\Workstation\CodexHub-release-<version>`

Completion criterion: `git -C D:\Workstation\CodexHub status -sb` is unchanged
from before the release, and the isolated checkout is on the release SHA.

## Version pin

Bump every version the consistency test reads:

- `frontend/package.json`
- `frontend/package-lock.json` (`version` and `packages."".version`)
- `src-tauri/Cargo.toml`
- `src-tauri/Cargo.lock` (`name = "codexhub"` only)
- `src-tauri/tauri.conf.json`
- `src-python/route_primitives.py` (`UPSTREAM_USER_AGENT`)
- `tests/test_release_channel_scripts.py` (`expected`)
- `docs/releases/<version>.md`

Completion criterion: `./scripts/codexhub-python.sh -m pytest -q tests/test_release_channel_scripts.py::test_release_version_is_consistent_across_manifests` passes.

## Push the SHA

Push the worker branch to `origin` by name. This branch may track
`origin/main`; a bare `git push` would target `main`.

```bash
git push -u origin HEAD:fix/<branch>
```

Completion criterion: `git ls-remote origin <full-sha>` returns the commit
and `origin/main` was not updated by that push.

## Linux artifacts

On this workstation, at the tagged SHA:

```bash
./scripts/build-linux-portable.sh
./scripts/build-linux-release.sh --flavor normal --notes "CodexHub <version>"
```

The updater key is `~/.codexhub/codexhub-updater.key`. Set
`TAURI_SIGNING_PRIVATE_KEY_PASSWORD` in the process environment when the key
needs a password.

Stable releases match the 0.2.11 asset set unless the Issue adds a debug
channel: normal Linux portable tarball, AppImage, AppImage signature, and
deb. The Linux builder writes `linux-x86_64` into `latest.json`.

Completion criterion: those four files exist, the AppImage has a `.sig`,
and `latest.json` records `codexhub_source_revision` as this SHA.

## Windows artifacts

On `yoga`, clone or fetch the pushed SHA into the isolated checkout, then:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\Prepare-PythonRuntime.ps1
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build-windows-portable.ps1 -Flavor normal
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build-windows-release.ps1 -Flavor normal -Notes 'CodexHub <version>'
```

Copy artifacts back to this workstation. Required stable files: portable ZIP,
NSIS installer, installer `.sig`, and Windows `latest.json`.

Completion criterion: those files exist, `codexhub_source_revision` matches
the Linux SHA, and yoga's `D:\Workstation\CodexHub` worktree was not used.

## Merge updater manifests

Each OS builder writes only its platform into `latest.json`. Merge them so
the published manifest has both `linux-x86_64` and `windows-x86_64` for the
same version and SHA. Keep `pub_date` from the earlier file.

Completion criterion: one `latest.json` lists both platforms, same `version`,
and the same `codexhub_source_revision`.

## Publish

Write `SHA256SUMS` for every uploaded file except `SHA256SUMS` itself. Create
the GitHub Release from the pushed SHA:

```bash
gh release create "v<version>" --target <sha> --title "CodexHub <version>" --notes-file "docs/releases/<version>.md" <assets>
```

Completion criterion: `gh release view v<version>` shows that SHA, both OS
families, merged `latest.json`, and `SHA256SUMS`. GitHub Actions is not a
release gate.

## Windows live CLI

When an Issue requires the eight-case Windows CLI gate, run it on `yoga`
from the same isolated checkout. See `docs/agents/real-client-e2e.md`. A
Linux-only desktop-registration fix does not invent a new Windows CLI gate.
