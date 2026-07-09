# Beta Release Channel Design

## Context

CodexHub 0.1.1 now has a working stable updater path. The next release-management gap is that test builds and stable builds cannot safely run side by side. They currently share app identity, process single-instance identity, bridge/gateway ports, `CODEX_HOME`, runtime settings, telemetry, Codex overlays, third-party client config targets, autostart names, and updater endpoint.

This design defines a full beta channel, not only alternate ports. The goal is to support an installed beta app that can run next to the stable app and update through its own release channel.

## Goals

- Build stable and beta Windows installers from the same source tree.
- Allow stable and beta apps to run at the same time on one Windows machine.
- Give beta its own app identity, install directory, app data directory, updater endpoint, bridge port, gateway port, `CODEX_HOME`, telemetry store, autostart registration, and UI labeling.
- Keep stable defaults unchanged for existing users.
- Make beta routing explicit enough that test runs do not accidentally overwrite the user's stable Codex or third-party client routing.
- Replace binary "connected/disconnected" routing controls with an owner-state model that distinguishes `Official`, `Release`, and `Beta`.
- Keep release scripts deterministic and auditable.

## Non-Goals

- Do not redesign the gateway protocol.
- Do not add HTTP/WebSocket transport changes as part of release-channel work.
- Do not solve multi-profile support inside one app instance.
- Do not make beta automatically take over the user's real Codex config on first launch.

## Channel Matrix

| Field | Stable | Beta |
| --- | --- | --- |
| Product name | `CodexHub` | `CodexHub Beta` |
| Executable base name | `codexhub.exe` | `codexhub-beta.exe` |
| Tauri identifier | `com.codexhub.app` | `com.codexhub.beta` |
| Default install dir | `D:\CodexHub` or installer default | `D:\CodexHub-Beta` or installer default |
| App data namespace | `com.codexhub.app` | `com.codexhub.beta` |
| Frontend dev port | `1420` | `1430` |
| Web bridge port | `1421` | `1431` |
| Gateway port | `9099` | `9109` |
| Default `CODEX_HOME` | `%USERPROFILE%\.codex` | `%USERPROFILE%\.codexhub-beta\codex-home` |
| Updater manifest | `latest.json` | `latest-beta.json` |
| GitHub release asset | `CodexHub_..._setup.exe` | `CodexHubBeta_..._setup.exe` |
| Autostart task | `CodexHubProxy` | `CodexHubBetaProxy` |

## Build Flavor Model

Introduce a build flavor abstraction with two supported values: `stable` and `beta`.

Flavor data should be defined once in a small machine-readable manifest, for example `config/build-flavors.toml`, then consumed by scripts. The manifest should contain product name, identifier, executable name, dev URL port, bridge port, gateway default port, updater endpoint, release asset naming prefix, default `CODEX_HOME`, and autostart suffix.

Release scripts should accept `-Flavor stable|beta`. Stable remains the default. Beta scripts should generate a temporary Tauri config from the checked-in base config and flavor values, rather than permanently editing `src-tauri/tauri.conf.json`.

The temporary config should be written under a generated build directory and passed to Tauri through `TAURI_CONFIG`. The generated file must not be committed.

## Runtime Configuration

Runtime code should expose a single `AppFlavor` or `RuntimeFlavor` value resolved from build metadata. Release builds should embed the flavor at compile time. Development builds may accept an environment override for local testing.

The runtime flavor controls:

- Web bridge bind address.
- Default gateway port for first-run settings.
- Default `CODEX_HOME` when no explicit environment variable is provided.
- Autostart task/service/label names.
- UI display label.
- Telemetry and runtime config locations.

Stable must keep today's behavior unless the user has explicitly configured another value.

Beta must not read or write stable runtime settings by default. Beta should initialize its own settings and provider config from bundled defaults. Importing stable settings may be added later, but should be an explicit user action.

## Port Handling

The app should stop hard-coding the in-app bridge at `127.0.0.1:1421`. `web_bridge::start_in_app()` should accept or resolve the flavor bridge address.

Gateway defaults should come from flavor defaults only on first-run settings creation. If the user edits beta gateway port from `9109` to another value, the app should preserve that setting.

Port collision behavior should be explicit:

- If the beta bridge port is occupied, beta should show a beta-specific bridge error and not assume stable bridge ownership.
- If the beta gateway port is occupied, beta should not stop a process unless it can prove that process belongs to the beta install or beta `CODEX_HOME`.
- Stable should keep existing ports and behavior.

## Data And Config Isolation

Beta must use an isolated `CODEX_HOME` by default:

`%USERPROFILE%\.codexhub-beta\codex-home`

That isolates:

- `proxy/settings.json`
- `proxy/config/providers.toml`
- `proxy/config.toml.backup`
- `model-catalogs/codexhub-model-catalog.json`
- proxy event JSONL
- proxy telemetry SQLite
- generated runtime config state

The stable app continues to use `%USERPROFILE%\.codex` unless the user explicitly overrides `CODEX_HOME`.

## Codex And Client Routing Safety

The highest-risk behavior is not port conflict; it is one app overwriting the other app's client routing.

For beta:

- Do not automatically switch the user's real `%USERPROFILE%\.codex\config.toml` on startup.
- Show clearly whether beta is using beta-isolated `CODEX_HOME` or the real user Codex config.
- Any action that writes to real Codex/OpenCode/Pi/OMP/ZCode config must be explicit and visibly show the target path and gateway URL.
- Backup names for beta-managed config rewrites should be distinct from stable where practical.

Stable defaults can remain unchanged, but stable and beta must share the same routing owner rules before both channels can be installed side by side.

## Routing State Model

Routing state is per managed target, not per app instance. A target is a concrete config destination such as:

- Codex App `%USERPROFILE%\.codex\config.toml`
- Beta-isolated Codex `%USERPROFILE%\.codexhub-beta\codex-home\config.toml`
- OpenCode config
- Pi config
- OMP config
- ZCode config

Each target should resolve to one visible owner state:

| Owner state | Meaning | Expected endpoint |
| --- | --- | --- |
| `Official` | CodexHub is not managing this target; the client should use its official/default upstream configuration. | No CodexHub gateway URL |
| `Release` | Stable CodexHub is managing this target. | Release gateway, default `http://127.0.0.1:9099` |
| `Beta` | CodexHub Beta is managing this target. | Beta gateway, default `http://127.0.0.1:9109` |

Implementation should also support an internal `UnknownExternal` classification for configs that are neither official nor recognized CodexHub-managed configs. The UI should present this as an unsafe external state and require explicit confirmation before overwrite. `UnknownExternal` is not one of the main user-facing connection choices.

The stable build should use the user-facing routing label `Release`. The beta build should use `Beta`. "Stable" can remain an internal build flavor name, but routing UI should not call the stable owner `Stable` because the user-facing state set is `Official`, `Release`, and `Beta`.

Owner detection should prefer explicit markers written by CodexHub-managed config blocks. When markers are unavailable, detection may fall back to gateway URL matching:

- Release if the target points at the configured release gateway URL.
- Beta if the target points at the configured beta gateway URL.
- Official if the target has no CodexHub gateway URL and matches the known official/default shape.
- Unknown external if the target has a custom endpoint or an unrecognized mutation.

Disconnect behavior must be owner-safe:

- In the release app, "Disconnect Release" may restore `Official` only when the current target owner is `Release`.
- In the release app, a target owned by `Beta` must display `Managed by Beta` and must not be overwritten by a normal disconnect action.
- In the beta app, "Disconnect Beta" may restore `Official` only when the current target owner is `Beta`.
- In the beta app, a target owned by `Release` must display `Managed by Release` and must not be overwritten by a normal disconnect action.

Takeover behavior must be explicit:

- Switching `Release -> Beta` or `Beta -> Release` should be a separate "Take over" action.
- The confirmation must show the target config path, current owner, new owner, old gateway URL, and new gateway URL.
- The action should create a channel-specific backup before writing.
- The result should update both the owner marker and gateway URL atomically where the file format allows it.

This model also applies to third-party client settings. The release app and beta app must not each maintain independent binary flags such as "OpenCode connected". They must read the target config and render the resolved owner state.

## Updater Channels

Stable updater endpoint remains:

`https://github.com/NOirBRight/CodexHub/releases/latest/download/latest.json`

Beta should use a separate endpoint, for example:

`https://github.com/NOirBRight/CodexHub/releases/download/beta/latest-beta.json`

The exact GitHub release shape can be one of:

- A moving `beta` release containing `latest-beta.json` and the latest beta installer.
- Versioned prereleases such as `v0.1.2-beta.1`, with a separately maintained `latest-beta.json`.

Recommendation: start with a moving `beta` prerelease because it keeps the beta updater URL stable and avoids GitHub `latest` semantics.

The beta release manifest must point at beta-named installer assets and use the same updater signing key unless we intentionally introduce a separate beta key. Using the same key is simpler and acceptable for early beta as long as beta endpoint and app identity are separate.

## UI Labeling

The beta app should be visibly labeled:

- Window title: `CodexHub Beta`
- Header/status area: beta badge
- Settings/about: flavor, version, bridge port, gateway port, `CODEX_HOME`, updater endpoint

This reduces the chance that testing actions are performed in stable by mistake.

## Routing UI

Connection controls should use the same three owner states everywhere the app can rewrite a client target:

- `Official`
- `Release`
- `Beta`

Recommended color tokens:

| State | Foreground | Background | Border | Use |
| --- | --- | --- | --- | --- |
| `Official` | `#4b5563` | `#f3f4f6` | `#d1d5db` | Neutral/default upstream |
| `Release` | `#075985` | `#e0f2fe` | `#38bdf8` | Stable release gateway |
| `Beta` | `#92400e` | `#fef3c7` | `#f59e0b` | Beta gateway |

Routing chips and primary routing buttons should include both label and endpoint when connected:

- `Official`
- `Release 127.0.0.1:9099`
- `Beta 127.0.0.1:9109`
- `Managed by Release`
- `Managed by Beta`

Release and Beta connected states must use visibly different button and chip colors. Official should use the neutral style. A target managed by the other channel should use that channel's color and disable normal disconnect/connect actions until the user chooses explicit takeover.

The routing UI should show target-level state, not only app-level state. For example, Codex may be `Release` while OpenCode remains `Official`, and the UI should make that mixed state scannable.

## Scripts

Update scripts should support:

- `scripts/build-windows-release.ps1 -Flavor stable`
- `scripts/build-windows-release.ps1 -Flavor beta`
- `scripts/build-windows-portable.ps1 -Flavor stable`
- `scripts/build-windows-portable.ps1 -Flavor beta`
- `scripts/e2e-app-update.ps1 -Flavor stable|beta`

Stable default command behavior must remain backward-compatible.

Beta release output should include:

- `CodexHubBeta_<version>_x64-setup.exe`
- `CodexHubBeta_<version>_x64-setup.exe.sig`
- `latest-beta.json`

## Testing

Minimum automated coverage:

- Unit test flavor default resolution.
- Unit test stable defaults remain `1421` and `9099`.
- Unit test beta defaults are `1431` and `9109`.
- Unit test beta `CODEX_HOME` default does not equal stable `CODEX_HOME`.
- Unit test autostart names differ by flavor.
- Unit test generated Tauri config contains beta identifier/product/updater endpoint.
- Unit test routing owner detection for `Official`, `Release`, `Beta`, and `UnknownExternal`.
- Unit test release disconnect restores only release-owned targets.
- Unit test beta disconnect restores only beta-owned targets.
- Unit test takeover requires explicit owner change data and writes a channel-specific backup.
- UI test or component test that Release and Beta chips/buttons use different color tokens and labels.
- E2E updater test for beta manifest detection and quiet install.

Manual release validation:

- Install stable and beta at the same time.
- Launch both apps.
- Confirm both bridge ports respond independently.
- Start both gateways and confirm they bind to different ports.
- Confirm stable `CODEX_HOME` and beta `CODEX_HOME` are distinct.
- Confirm beta update check reads `latest-beta.json`, not stable `latest.json`.
- Confirm stable update check still reads stable latest.

### Task 7 Verification Commands

Run:

```powershell
pytest -q
cd src-tauri
cargo test
cd ..\frontend
npm run test:ui-contract
npm run build
```

Build release artifacts with:

```powershell
scripts\build-windows-release.ps1 -Flavor stable -SkipFrontendBuild
scripts\build-windows-release.ps1 -Flavor beta -SkipFrontendBuild
```

Expected stable/beta artifacts:

- Stable installer: `src-tauri\target\release\bundle\nsis\CodexHub_0.1.1_x64-setup.exe`
- Stable manifest: `src-tauri\target\release\bundle\nsis\latest.json`
- Beta installer: `src-tauri\target\release\bundle\nsis\CodexHubBeta_0.1.1_x64-setup.exe`
- Beta manifest: `src-tauri\target\release\bundle\nsis\latest-beta.json`

Manual side-by-side smoke checklist:

- Launch or install the release build and beta build at the same time.
- Verify release window title is `CodexHub`.
- Verify beta window title is `CodexHub Beta`.
- Verify release bridge responds at `http://127.0.0.1:1421/api/invoke`.
- Verify beta bridge responds at `http://127.0.0.1:1431/api/invoke`.
- Verify release gateway responds at `http://127.0.0.1:9099/v1/models`.
- Verify beta gateway responds at `http://127.0.0.1:9109/v1/models`.
- Verify release uses `%USERPROFILE%\.codex` when `CODEX_HOME` is unset.
- Verify beta uses `%USERPROFILE%\.codexhub-beta\codex-home` when `CODEX_HOME` is unset.
- Open the Gateway page in both apps. Verify a third-party target can show `Release`, the beta app shows `Managed by Release`, and beta takeover requires explicit confirmation before writing.

## Rollout Plan

1. Add flavor manifest and script support for generated Tauri config.
2. Add runtime flavor resolution and use it for bridge/gateway/autostart defaults.
3. Isolate beta `CODEX_HOME` and runtime paths.
4. Add routing owner detection and owner-safe disconnect/takeover operations for Codex and third-party client targets.
5. Replace binary routing controls with `Official` / `Release` / `Beta` UI states and color tokens.
6. Add beta UI labeling.
7. Add beta release and updater manifest generation.
8. Extend E2E update tests to cover beta.
9. Produce one beta installer and manually verify stable/beta side-by-side operation.

## Open Decisions

- Whether beta should be distributed through a moving `beta` prerelease or versioned `vX.Y.Z-beta.N` prereleases plus a maintained manifest.
- Whether beta uses the stable updater signing key or a separate beta key.
- Whether beta should ever offer one-click takeover of the real user Codex config, or only support isolated beta `CODEX_HOME`.
