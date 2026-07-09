# Beta Release Channel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a full beta release channel that can run next to the release app, use separate ports and data paths, and manage Codex/third-party routing with `Official` / `Release` / `Beta` owner states.

**Architecture:** Add a shared build/runtime flavor layer first, then route all port/path/app-identity decisions through it. Add backend routing-owner detection as the source of truth, and let the frontend render owner state rather than deriving connection state from binary `official` / `hub` route strings. Keep existing release behavior as the stable default.

**Tech Stack:** Tauri v2, Rust, Python config overlay helper, React, TypeScript, Vite, PowerShell release scripts, Node contract tests, Rust unit tests, Python pytest where Python helpers change.

## Global Constraints

- Build stable and beta Windows installers from the same source tree.
- Allow stable and beta apps to run at the same time on one Windows machine.
- Stable defaults stay unchanged: frontend `1420`, bridge `1421`, gateway `9099`, `CODEX_HOME=%USERPROFILE%\.codex`, updater `latest.json`, product `CodexHub`, identifier `com.codexhub.app`.
- Beta defaults are: frontend `1430`, bridge `1431`, gateway `9109`, `CODEX_HOME=%USERPROFILE%\.codexhub-beta\codex-home`, updater `latest-beta.json`, product `CodexHub Beta`, identifier `com.codexhub.beta`.
- Do not redesign the gateway protocol.
- Do not add HTTP/WebSocket transport changes as part of release-channel work.
- Do not make beta automatically take over the user's real Codex config on first launch.
- Routing UI states are `Official`, `Release`, and `Beta`; the release app must not call the user-facing owner `Stable`.
- Each managed target has one resolved owner state. The app must not maintain independent binary connected flags.
- A normal disconnect can restore only targets owned by the current app owner.
- Switching `Release -> Beta` or `Beta -> Release` requires explicit takeover confirmation that shows target path, current owner, new owner, old gateway URL, and new gateway URL.
- When working locally, do not kill broad Codex or Gateway processes. Match exact executable path, PID file, or known flavor-owned process metadata.

---

## File Structure

- Create `config/build-flavors.json`: single source of truth for stable/beta product name, identifier, ports, updater endpoint, asset prefix, autostart names, and beta `CODEX_HOME` suffix.
- Create `scripts/Build-TauriConfig.ps1`: reads `config/build-flavors.json`, generates a temporary flavor-specific `tauri.conf.json`, and returns the generated path.
- Modify `scripts/build-windows-release.ps1`: add `-Flavor stable|beta`, generated Tauri config, flavor env vars, flavor asset naming, and `latest-beta.json` output.
- Modify `scripts/e2e-app-update.ps1`: add `-Flavor stable|beta`, default bridge URL per flavor, and beta manifest naming.
- Modify `frontend/vite.config.ts`: read frontend port from `CODEXHUB_FRONTEND_PORT`, defaulting to `1420`.
- Create `src-tauri/src/app_flavor.rs`: runtime flavor resolution and flavor defaults.
- Modify `src-tauri/build.rs`: emit compile-time `CODEXHUB_BUILD_FLAVOR`.
- Modify `src-tauri/src/main.rs`: register `app_flavor`, expose `get_app_flavor`, set tray labels, single-instance identity behavior through generated Tauri config, and use flavor-aware startup.
- Modify `src-tauri/src/runtime_paths.rs`: resolve beta default `CODEX_HOME` when flavor is beta and env var is absent.
- Modify `src-tauri/src/config.rs`: use flavor default gateway port for first-run settings, use channel-specific Codex backup names, and call `config_overlay.py` with owner metadata.
- Modify `src-tauri/src/web_bridge.rs`: use flavor bridge port instead of `DEFAULT_ADDR`.
- Modify `src-tauri/src/autostart.rs`: use flavor-specific Windows task, macOS label/plist, and Linux service names.
- Modify `src-python/config_overlay.py`: write/read owner metadata in Codex config overlay markers.
- Modify `src-tauri/src/gateway.rs`: add `RoutingOwner`, owner detection, owner-safe disconnect/takeover, and route-owner fields on `GatewayClientInfo`.
- Modify `frontend/src/lib/types.ts`: add `AppFlavorInfo`, `RoutingOwner`, and owner-aware gateway client fields/results.
- Modify `frontend/src/lib/tauri.ts`: add `getAppFlavor`, owner-aware route switch args, and bridge default awareness.
- Modify `frontend/src/lib/settings.ts`: flavor defaults stay backend-owned; frontend continues to normalize received settings.
- Modify `frontend/src/components/RuntimeBar.tsx`: show `CodexHub Beta`/badge and ports.
- Modify `frontend/src/components/GatewayClientCard.tsx`: render owner chips/buttons with distinct `Official`, `Release`, and `Beta` colors.
- Modify `frontend/src/pages/GatewayPage.tsx`: pass current flavor owner, show takeover confirmation, and call owner-aware route switch.
- Modify `frontend/src/i18n/locales/en-US.ts` and `frontend/src/i18n/locales/zh-CN.ts`: add owner/takeover copy.
- Modify `frontend/scripts/ui-contract.test.mjs`: add contract checks for flavor ports, owner colors, labels, and release script flavor flags.
- Modify `src-tauri/tauri.conf.json`: keep stable checked-in defaults; generated beta config is not committed.

---

### Task 1: Build Flavor Manifest And Generated Tauri Config

**Files:**
- Create: `config/build-flavors.json`
- Create: `scripts/Build-TauriConfig.ps1`
- Modify: `scripts/build-windows-release.ps1`
- Modify: `frontend/package.json`
- Modify: `frontend/vite.config.ts`
- Test: `frontend/scripts/ui-contract.test.mjs`

**Interfaces:**
- Produces: `config/build-flavors.json` with top-level keys `stable` and `beta`.
- Produces: PowerShell script `scripts/Build-TauriConfig.ps1 -Flavor stable|beta -RepoRoot <path>` that prints the generated config path.
- Produces: environment variables `CODEXHUB_BUILD_FLAVOR`, `CODEXHUB_FRONTEND_PORT`, and `TAURI_CONFIG` for build scripts.

- [ ] **Step 1: Write failing UI contract coverage for flavor build inputs**

Add this test to `frontend/scripts/ui-contract.test.mjs` near the existing release-build tests:

```js
test("release build scripts support stable and beta flavor configuration", async () => {
  const [buildScript, packageSource, viteSource] = await Promise.all([
    readFile(buildWindowsReleasePath, "utf8"),
    readFile(new URL("../package.json", import.meta.url), "utf8"),
    readFile(viteConfigPath, "utf8"),
  ]);
  const packageJson = JSON.parse(packageSource);
  const flavorManifest = JSON.parse(await readFile(new URL("../../config/build-flavors.json", import.meta.url), "utf8"));

  assert.deepEqual(Object.keys(flavorManifest).sort(), ["beta", "stable"]);
  assert.equal(flavorManifest.stable.productName, "CodexHub");
  assert.equal(flavorManifest.stable.identifier, "com.codexhub.app");
  assert.equal(flavorManifest.stable.frontendPort, 1420);
  assert.equal(flavorManifest.stable.bridgePort, 1421);
  assert.equal(flavorManifest.stable.gatewayPort, 9099);
  assert.equal(flavorManifest.stable.routingOwner, "release");
  assert.equal(flavorManifest.beta.productName, "CodexHub Beta");
  assert.equal(flavorManifest.beta.identifier, "com.codexhub.beta");
  assert.equal(flavorManifest.beta.frontendPort, 1430);
  assert.equal(flavorManifest.beta.bridgePort, 1431);
  assert.equal(flavorManifest.beta.gatewayPort, 9109);
  assert.equal(flavorManifest.beta.routingOwner, "beta");
  assert.equal(flavorManifest.beta.updaterManifestName, "latest-beta.json");
  assert.match(buildScript, /ValidateSet\("stable",\s*"beta"\)/);
  assert.match(buildScript, /Build-TauriConfig\.ps1/);
  assert.match(buildScript, /CODEXHUB_BUILD_FLAVOR/);
  assert.match(buildScript, /CODEXHUB_FRONTEND_PORT/);
  assert.match(viteSource, /CODEXHUB_FRONTEND_PORT/);
  assert.doesNotMatch(packageJson.scripts.dev, /--port\s+1420/);
  assert.doesNotMatch(packageJson.scripts.preview, /--port\s+1420/);
});
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
cd frontend
npm run test:ui-contract -- --test-name-pattern "release build scripts support stable and beta flavor configuration"
```

Expected: FAIL because `config/build-flavors.json`, `Build-TauriConfig.ps1`, and Vite flavor port support do not exist.

- [ ] **Step 3: Add the flavor manifest**

Create `config/build-flavors.json`:

```json
{
  "stable": {
    "productName": "CodexHub",
    "executableBaseName": "codexhub",
    "identifier": "com.codexhub.app",
    "windowTitle": "CodexHub",
    "frontendPort": 1420,
    "bridgePort": 1421,
    "gatewayPort": 9099,
    "routingOwner": "release",
    "defaultCodexHome": ".codex",
    "updaterEndpoint": "https://github.com/NOirBRight/CodexHub/releases/latest/download/latest.json",
    "updaterManifestName": "latest.json",
    "releaseAssetPrefix": "CodexHub",
    "autostartTaskName": "CodexHubProxy",
    "macosLabel": "com.codexhub.proxy",
    "macosPlistFile": "com.codexhub.proxy.plist",
    "linuxServiceFile": "codexhub-proxy.service"
  },
  "beta": {
    "productName": "CodexHub Beta",
    "executableBaseName": "codexhub-beta",
    "identifier": "com.codexhub.beta",
    "windowTitle": "CodexHub Beta",
    "frontendPort": 1430,
    "bridgePort": 1431,
    "gatewayPort": 9109,
    "routingOwner": "beta",
    "defaultCodexHome": ".codexhub-beta/codex-home",
    "updaterEndpoint": "https://github.com/NOirBRight/CodexHub/releases/download/beta/latest-beta.json",
    "updaterManifestName": "latest-beta.json",
    "releaseAssetPrefix": "CodexHubBeta",
    "autostartTaskName": "CodexHubBetaProxy",
    "macosLabel": "com.codexhub.beta.proxy",
    "macosPlistFile": "com.codexhub.beta.proxy.plist",
    "linuxServiceFile": "codexhub-beta-proxy.service"
  }
}
```

- [ ] **Step 4: Add the generated Tauri config script**

Create `scripts/Build-TauriConfig.ps1`:

```powershell
[CmdletBinding()]
param(
    [ValidateSet("stable", "beta")]
    [string]$Flavor = "stable",
    [string]$RepoRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$OutputRoot = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if ([string]::IsNullOrWhiteSpace($OutputRoot)) {
    $OutputRoot = Join-Path $RepoRoot ".generated\tauri\$Flavor"
}

$manifestPath = Join-Path $RepoRoot "config\build-flavors.json"
$baseConfigPath = Join-Path $RepoRoot "src-tauri\tauri.conf.json"
$manifest = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json
$flavorConfig = $manifest.$Flavor
if ($null -eq $flavorConfig) {
    throw "Unknown CodexHub build flavor: $Flavor"
}

$config = Get-Content -Raw -LiteralPath $baseConfigPath | ConvertFrom-Json
$config.productName = [string]$flavorConfig.productName
$config.identifier = [string]$flavorConfig.identifier
$config.build.devUrl = "http://localhost:$($flavorConfig.frontendPort)"
$config.app.windows[0].title = [string]$flavorConfig.windowTitle
$config.plugins.updater.endpoints = @([string]$flavorConfig.updaterEndpoint)

New-Item -ItemType Directory -Force -Path $OutputRoot | Out-Null
$outputPath = Join-Path $OutputRoot "tauri.$Flavor.conf.json"
$json = $config | ConvertTo-Json -Depth 32
$utf8NoBom = [System.Text.UTF8Encoding]::new($false)
[System.IO.File]::WriteAllText($outputPath, $json + [Environment]::NewLine, $utf8NoBom)
Write-Output $outputPath
```

- [ ] **Step 5: Make Vite use the flavor frontend port**

Modify `frontend/vite.config.ts`:

```ts
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

const frontendPort = Number(process.env.CODEXHUB_FRONTEND_PORT ?? 1420);

export default defineConfig({
  plugins: [react()],
  clearScreen: false,
  build: {
    assetsInlineLimit: 0,
  },
  server: {
    host: "127.0.0.1",
    port: Number.isInteger(frontendPort) ? frontendPort : 1420,
    strictPort: true,
  },
});
```

- [ ] **Step 6: Stop package scripts from overriding the flavor port**

Modify `frontend/package.json`:

```json
{
  "scripts": {
    "dev": "vite --host 127.0.0.1",
    "build": "tsc && vite build",
    "test:ui-contract": "node --test scripts/ui-contract.test.mjs",
    "preview": "vite preview --host 127.0.0.1"
  }
}
```

- [ ] **Step 7: Wire flavor args into `build-windows-release.ps1`**

Modify the param block:

```powershell
param(
    [ValidateSet("stable", "beta")]
    [string]$Flavor = "stable",
    [string]$PrivateKeyPath = (Join-Path $env:USERPROFILE ".codexhub\codexhub-updater.key"),
    [string]$PrivateKeyPassword = $env:TAURI_SIGNING_PRIVATE_KEY_PASSWORD,
    [string]$ReleaseBaseUrl = "",
    [string]$Notes = "",
    [switch]$SkipFrontendBuild
)
```

After existing path variables, add:

```powershell
$flavorManifestPath = Join-Path $repoRoot "config\build-flavors.json"
$flavorManifest = Get-Content -Raw -LiteralPath $flavorManifestPath | ConvertFrom-Json
$flavorConfig = $flavorManifest.$Flavor
if ($null -eq $flavorConfig) {
    throw "Unknown build flavor: $Flavor"
}
if ([string]::IsNullOrWhiteSpace($ReleaseBaseUrl)) {
    if ($Flavor -eq "beta") {
        $ReleaseBaseUrl = "https://github.com/NOirBRight/CodexHub/releases/download/beta"
    }
    else {
        $ReleaseBaseUrl = "https://github.com/NOirBRight/CodexHub/releases/latest/download"
    }
}
```

Before reading Tauri config, generate it:

```powershell
$generatedTauriConfigPath = (& (Join-Path $PSScriptRoot "Build-TauriConfig.ps1") -Flavor $Flavor -RepoRoot $repoRoot).Trim()
$tauriConfigPath = $generatedTauriConfigPath
```

Before `npm run build`, set frontend port:

```powershell
$previousFrontendPort = $env:CODEXHUB_FRONTEND_PORT
$env:CODEXHUB_FRONTEND_PORT = [string]$flavorConfig.frontendPort
```

Before `cargo tauri build`, set:

```powershell
$previousBuildFlavor = $env:CODEXHUB_BUILD_FLAVOR
$previousTauriConfig = $env:TAURI_CONFIG
$env:CODEXHUB_BUILD_FLAVOR = $Flavor
$env:TAURI_CONFIG = $generatedTauriConfigPath
```

Restore those environment variables in existing `finally` blocks using the same pattern already used for signing keys.

Replace installer and manifest naming:

```powershell
$assetPrefix = [string]$flavorConfig.releaseAssetPrefix
$installerName = "{0}_{1}_x64-setup.exe" -f $assetPrefix, $version
$manifestPath = Join-Path $bundleDir ([string]$flavorConfig.updaterManifestName)
```

- [ ] **Step 8: Run contract test**

Run:

```powershell
cd frontend
npm run test:ui-contract -- --test-name-pattern "release build scripts support stable and beta flavor configuration"
```

Expected: PASS.

- [ ] **Step 9: Commit**

```powershell
git add config/build-flavors.json scripts/Build-TauriConfig.ps1 scripts/build-windows-release.ps1 frontend/package.json frontend/vite.config.ts frontend/scripts/ui-contract.test.mjs
git commit -m "build: add stable and beta flavor config"
```

---

### Task 2: Runtime Flavor Defaults

**Files:**
- Create: `src-tauri/src/app_flavor.rs`
- Modify: `src-tauri/build.rs`
- Modify: `src-tauri/src/main.rs`
- Modify: `src-tauri/src/runtime_paths.rs`
- Modify: `src-tauri/src/config.rs`
- Modify: `src-tauri/src/web_bridge.rs`
- Modify: `src-tauri/src/autostart.rs`
- Test: Rust unit tests in changed modules

**Interfaces:**
- Produces: `app_flavor::RuntimeFlavor`.
- Produces: `app_flavor::current() -> RuntimeFlavor`.
- Produces: `app_flavor::current_info() -> AppFlavorInfo`.
- Produces: `app_flavor::default_gateway_port() -> u16`.
- Produces: `app_flavor::bridge_addr() -> String`.
- Produces: `app_flavor::default_codex_home_dir() -> Result<PathBuf, String>`.
- Produces: `get_app_flavor` Tauri/web-bridge command returning flavor metadata.

- [ ] **Step 1: Write failing Rust flavor tests**

Create `src-tauri/src/app_flavor.rs` with tests first:

```rust
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn stable_defaults_match_existing_ports_and_identity() {
        let flavor = RuntimeFlavor::Stable;
        assert_eq!(flavor.routing_owner(), RoutingOwner::Release);
        assert_eq!(flavor.product_name(), "CodexHub");
        assert_eq!(flavor.bridge_port(), 1421);
        assert_eq!(flavor.gateway_port(), 9099);
        assert_eq!(flavor.autostart_task_name(), "CodexHubProxy");
    }

    #[test]
    fn beta_defaults_are_isolated_from_stable() {
        let flavor = RuntimeFlavor::Beta;
        assert_eq!(flavor.routing_owner(), RoutingOwner::Beta);
        assert_eq!(flavor.product_name(), "CodexHub Beta");
        assert_eq!(flavor.bridge_port(), 1431);
        assert_eq!(flavor.gateway_port(), 9109);
        assert_eq!(flavor.autostart_task_name(), "CodexHubBetaProxy");
        assert_ne!(flavor.default_codex_home_suffix(), RuntimeFlavor::Stable.default_codex_home_suffix());
    }
}
```

Add `mod app_flavor;` to `src-tauri/src/main.rs` and run:

```powershell
cd src-tauri
cargo test app_flavor
```

Expected: FAIL until the module is implemented.

- [ ] **Step 2: Implement `app_flavor.rs`**

Use this complete module as the initial implementation:

```rust
use serde::Serialize;
use std::path::PathBuf;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum RoutingOwner {
    Official,
    Release,
    Beta,
    UnknownExternal,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum RuntimeFlavor {
    Stable,
    Beta,
}

#[derive(Debug, Clone, Serialize)]
pub struct AppFlavorInfo {
    pub flavor: RuntimeFlavor,
    pub routing_owner: RoutingOwner,
    pub product_name: &'static str,
    pub bridge_port: u16,
    pub gateway_port: u16,
    pub default_codex_home_suffix: &'static str,
}

pub fn current() -> RuntimeFlavor {
    RuntimeFlavor::from_name(option_env!("CODEXHUB_BUILD_FLAVOR").unwrap_or("stable"))
}

pub fn current_info() -> AppFlavorInfo {
    current().info()
}

pub fn default_gateway_port() -> u16 {
    current().gateway_port()
}

pub fn bridge_addr() -> String {
    format!("127.0.0.1:{}", current().bridge_port())
}

impl RuntimeFlavor {
    pub fn from_name(value: &str) -> Self {
        match value.trim().to_ascii_lowercase().as_str() {
            "beta" => Self::Beta,
            _ => Self::Stable,
        }
    }

    pub fn info(self) -> AppFlavorInfo {
        AppFlavorInfo {
            flavor: self,
            routing_owner: self.routing_owner(),
            product_name: self.product_name(),
            bridge_port: self.bridge_port(),
            gateway_port: self.gateway_port(),
            default_codex_home_suffix: self.default_codex_home_suffix(),
        }
    }

    pub fn routing_owner(self) -> RoutingOwner {
        match self {
            Self::Stable => RoutingOwner::Release,
            Self::Beta => RoutingOwner::Beta,
        }
    }

    pub fn product_name(self) -> &'static str {
        match self {
            Self::Stable => "CodexHub",
            Self::Beta => "CodexHub Beta",
        }
    }

    pub fn bridge_port(self) -> u16 {
        match self {
            Self::Stable => 1421,
            Self::Beta => 1431,
        }
    }

    pub fn gateway_port(self) -> u16 {
        match self {
            Self::Stable => 9099,
            Self::Beta => 9109,
        }
    }

    pub fn default_codex_home_suffix(self) -> &'static str {
        match self {
            Self::Stable => ".codex",
            Self::Beta => ".codexhub-beta/codex-home",
        }
    }

    pub fn autostart_task_name(self) -> &'static str {
        match self {
            Self::Stable => "CodexHubProxy",
            Self::Beta => "CodexHubBetaProxy",
        }
    }
}

pub fn default_codex_home_dir() -> Result<PathBuf, String> {
    dirs::home_dir()
        .ok_or_else(|| "failed to resolve user home directory".to_string())
        .map(|home| {
            current()
                .default_codex_home_suffix()
                .split('/')
                .fold(home, |path, segment| path.join(segment))
        })
}
```

- [ ] **Step 3: Make build.rs emit compile-time flavor**

Modify `src-tauri/build.rs`:

```rust
fn main() {
    let flavor = std::env::var("CODEXHUB_BUILD_FLAVOR").unwrap_or_else(|_| "stable".to_string());
    println!("cargo:rustc-env=CODEXHUB_BUILD_FLAVOR={flavor}");
    println!("cargo:rerun-if-env-changed=CODEXHUB_BUILD_FLAVOR");
    tauri_build::build()
}
```

- [ ] **Step 4: Use flavor defaults in runtime paths and settings**

Modify `runtime_paths::codex_home_dir()`:

```rust
pub(crate) fn codex_home_dir() -> Result<PathBuf, String> {
    match std::env::var_os("CODEX_HOME").filter(|value| !value.is_empty()) {
        Some(value) => Ok(PathBuf::from(value)),
        None => crate::app_flavor::default_codex_home_dir(),
    }
}
```

Modify `Settings::default()` in `src-tauri/src/main.rs`:

```rust
proxy_port: app_flavor::default_gateway_port(),
```

- [ ] **Step 5: Use flavor bridge address**

Modify `src-tauri/src/web_bridge.rs`:

```rust
fn default_addr() -> String {
    crate::app_flavor::bridge_addr()
}
```

Replace `DEFAULT_ADDR` binding uses in `run()` and `start_background()` with `default_addr()`. Keep `INVOKE_PATH` and request handling unchanged.

- [ ] **Step 6: Use flavor autostart names**

Replace fixed autostart constants in `src-tauri/src/autostart.rs` with helper functions:

```rust
fn windows_task_name() -> &'static str {
    crate::app_flavor::current().autostart_task_name()
}
```

In Windows register/remove args, replace `WINDOWS_TASK_NAME.to_string()` with `windows_task_name().to_string()`. Add equivalent helpers for macOS label/plist and Linux service in this task only if those constants are used in generated paths or test assertions.

- [ ] **Step 7: Expose flavor metadata to frontend and bridge**

In `src-tauri/src/main.rs`, add:

```rust
#[tauri::command]
fn get_app_flavor() -> app_flavor::AppFlavorInfo {
    app_flavor::current_info()
}
```

Add `get_app_flavor` to `invoke_handler`. In `src-tauri/src/web_bridge.rs`, add a dispatch arm:

```rust
"get_app_flavor" => to_value(Ok(crate::app_flavor::current_info())),
```

- [ ] **Step 8: Run Rust tests**

Run:

```powershell
cd src-tauri
cargo test app_flavor runtime_paths::tests autostart::tests web_bridge::tests config::tests
```

Expected: PASS.

- [ ] **Step 9: Commit**

```powershell
git add src-tauri/build.rs src-tauri/src/app_flavor.rs src-tauri/src/main.rs src-tauri/src/runtime_paths.rs src-tauri/src/config.rs src-tauri/src/web_bridge.rs src-tauri/src/autostart.rs
git commit -m "feat: add runtime flavor defaults"
```

---

### Task 3: Codex Config Owner Detection

**Files:**
- Modify: `src-python/config_overlay.py`
- Modify: `tests/test_config_overlay.py`
- Modify: `src-tauri/src/config.rs`
- Test: `pytest tests/test_config_overlay.py`
- Test: `cargo test config::tests`

**Interfaces:**
- Produces: Codex overlay marker lines containing `# owner = release|beta`.
- Produces: Python CLI args `--owner release|beta` and `--base-url`.
- Produces: Rust call path that passes current flavor owner into the overlay helper.

- [ ] **Step 1: Add failing Python tests for owner markers**

Append to `tests/test_config_overlay.py`:

```python
def test_apply_overlay_writes_owner_marker(tmp_path):
    config = tmp_path / "config.toml"
    backup = tmp_path / "backup.toml"
    catalog = tmp_path / "catalog.json"

    config_overlay.apply_overlay(
        config,
        backup,
        catalog,
        "http://127.0.0.1:9109",
        owner="beta",
    )

    text = config.read_text(encoding="utf-8")
    assert "# owner = beta" in text
    assert "http://127.0.0.1:9109/v1" in text


def test_restore_overlay_removes_owner_marker(tmp_path):
    config = tmp_path / "config.toml"
    backup = tmp_path / "backup.toml"
    catalog = tmp_path / "catalog.json"
    config_overlay.apply_overlay(
        config,
        backup,
        catalog,
        "http://127.0.0.1:9099",
        owner="release",
    )

    config_overlay.restore_overlay(config, backup, unified_history=False)

    text = config.read_text(encoding="utf-8")
    assert "# owner = release" not in text
    assert "# BEGIN CODEX PROXY SESSION CONFIG" not in text
```

- [ ] **Step 2: Run Python test to verify failure**

Run:

```powershell
pytest tests/test_config_overlay.py -q
```

Expected: FAIL because `apply_overlay` does not accept `owner`.

- [ ] **Step 3: Implement Python owner marker support**

Modify signatures in `src-python/config_overlay.py`:

```python
def build_overlay(catalog_value: str, owner: str) -> str:
    return "\n".join(
        [
            MARKER_BEGIN,
            f"# owner = {owner}",
            'model = "openai/gpt-5.5"',
            f'model_provider = "{PROXY_PROVIDER_ID}"',
            f"model_catalog_json = {toml_literal(catalog_value)}",
            MARKER_END,
            "",
        ]
    )


def apply_overlay(config_path: Path, backup_path: Path, catalog_path: Path, base_url: str, owner: str = "release") -> None:
    if owner not in {"release", "beta"}:
        raise ValueError(f"unsupported CodexHub owner: {owner}")
    original = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    cleaned = strip_marked_overlay(original)
    atomic_write_text(backup_path, cleaned if cleaned != original else original, encoding="utf-8")

    for section in STALE_PROXY_PROVIDER_SECTIONS:
        cleaned = strip_section(cleaned, section)
    cleaned = strip_top_level_keys(cleaned)
    cleaned = set_feature_flags(cleaned, PROXY_FEATURE_FLAGS)
    updated = build_overlay(catalog_config_value(config_path, catalog_path), owner) + cleaned.lstrip()
    updated = insert_provider_section(updated, build_provider_section(base_url))
    atomic_write_text(config_path, updated, encoding="utf-8")
```

Add CLI arg:

```python
apply_parser.add_argument("--owner", choices=["release", "beta"], default="release")
```

Call:

```python
apply_overlay(args.config, args.backup, args.catalog, args.base_url, args.owner)
```

- [ ] **Step 4: Pass owner from Rust config switch**

In `src-tauri/src/config.rs`, in custom mode args, add after `--base-url` value:

```rust
"--owner".to_string(),
match crate::app_flavor::current().routing_owner() {
    crate::app_flavor::RoutingOwner::Beta => "beta".to_string(),
    _ => "release".to_string(),
},
```

Also make `config_backup_path()` channel-specific:

```rust
fn config_backup_path(&self) -> PathBuf {
    let name = match crate::app_flavor::current().routing_owner() {
        crate::app_flavor::RoutingOwner::Beta => "config.toml.beta.backup",
        _ => "config.toml.release.backup",
    };
    self.proxy_dir().join(name)
}
```

- [ ] **Step 5: Run tests**

Run:

```powershell
pytest tests/test_config_overlay.py -q
cd src-tauri
cargo test config::tests
```

Expected: PASS.

- [ ] **Step 6: Commit**

```powershell
git add src-python/config_overlay.py tests/test_config_overlay.py src-tauri/src/config.rs
git commit -m "feat: mark Codex routing owner in managed config"
```

---

### Task 4: Gateway Client Routing Owner Contract

**Files:**
- Modify: `src-tauri/src/gateway.rs`
- Modify: `src-tauri/src/web_bridge.rs`
- Modify: `src-tauri/src/main.rs`
- Test: Rust unit tests in `src-tauri/src/gateway.rs`

**Interfaces:**
- Produces: `GatewayClientInfo.route_owner: RoutingOwner`.
- Produces: `GatewayClientInfo.route_endpoint: Option<String>`.
- Produces: `GatewayClientInfo.managed_by_current_app: bool`.
- Produces: `switch_gateway_client_route(client_id, mode, model, force_takeover)` where `mode` accepts `official`, `release`, `beta`, and legacy `hub`.

- [ ] **Step 1: Add failing owner detection tests**

Add tests in `src-tauri/src/gateway.rs` near existing client config tests:

```rust
#[test]
fn local_gateway_owner_detects_release_and_beta_ports() {
    assert_eq!(
        routing_owner_from_gateway_url("http://127.0.0.1:9099/v1"),
        crate::app_flavor::RoutingOwner::Release
    );
    assert_eq!(
        routing_owner_from_gateway_url("http://127.0.0.1:9109/v1"),
        crate::app_flavor::RoutingOwner::Beta
    );
    assert_eq!(
        routing_owner_from_gateway_url("https://api.openai.com/v1"),
        crate::app_flavor::RoutingOwner::UnknownExternal
    );
}

#[test]
fn owner_safe_disconnect_rejects_other_channel_without_takeover() {
    let current = crate::app_flavor::RoutingOwner::Release;
    let target = crate::app_flavor::RoutingOwner::Beta;
    let error = ensure_route_owner_mutation_allowed(current, target, crate::app_flavor::RoutingOwner::Official, false)
        .expect_err("release must not disconnect beta-owned config");
    assert!(error.contains("Managed by Beta"));
}

#[test]
fn takeover_allows_cross_channel_owner_change_when_explicit() {
    ensure_route_owner_mutation_allowed(
        crate::app_flavor::RoutingOwner::Release,
        crate::app_flavor::RoutingOwner::Beta,
        crate::app_flavor::RoutingOwner::Release,
        true,
    )
    .expect("explicit takeover should be allowed");
}
```

Run:

```powershell
cd src-tauri
cargo test "routing_owner"
```

Expected: FAIL because helpers do not exist.

- [ ] **Step 2: Add owner helpers**

In `src-tauri/src/gateway.rs`, import the owner enum and add:

```rust
use crate::app_flavor::RoutingOwner;

fn routing_owner_from_gateway_url(url: &str) -> RoutingOwner {
    let trimmed = url.trim().trim_end_matches('/');
    if trimmed.starts_with("http://127.0.0.1:9099") || trimmed.starts_with("http://localhost:9099") {
        return RoutingOwner::Release;
    }
    if trimmed.starts_with("http://127.0.0.1:9109") || trimmed.starts_with("http://localhost:9109") {
        return RoutingOwner::Beta;
    }
    if trimmed.contains("127.0.0.1") || trimmed.contains("localhost") {
        return RoutingOwner::UnknownExternal;
    }
    RoutingOwner::UnknownExternal
}

fn owner_label(owner: RoutingOwner) -> &'static str {
    match owner {
        RoutingOwner::Official => "Official",
        RoutingOwner::Release => "Release",
        RoutingOwner::Beta => "Beta",
        RoutingOwner::UnknownExternal => "Unknown external",
    }
}

fn ensure_route_owner_mutation_allowed(
    current_app_owner: RoutingOwner,
    current_target_owner: RoutingOwner,
    next_owner: RoutingOwner,
    force_takeover: bool,
) -> Result<(), String> {
    if current_target_owner == RoutingOwner::Official || current_target_owner == current_app_owner {
        return Ok(());
    }
    if force_takeover && next_owner != RoutingOwner::Official {
        return Ok(());
    }
    Err(format!(
        "Managed by {}; explicit takeover is required before changing this target.",
        owner_label(current_target_owner)
    ))
}
```

- [ ] **Step 3: Extend backend structs**

Modify `GatewayClientInfo`:

```rust
pub route_owner: RoutingOwner,
pub route_endpoint: Option<String>,
pub managed_by_current_app: bool,
```

Keep `route_mode` for compatibility during this release:

```rust
pub route_mode: String,
```

Use `route_mode = "hub"` for current-app owner, `route_mode = "official"` for `Official`, `route_mode = "other_channel"` for the other recognized owner, and `route_mode = "unknown"` for `UnknownExternal`.

- [ ] **Step 4: Resolve owner in `list_gateway_clients`**

Add a helper:

```rust
fn route_mode_for_owner(owner: RoutingOwner, current: RoutingOwner, stale: bool) -> &'static str {
    if stale {
        return "stale";
    }
    match owner {
        RoutingOwner::Official => "official",
        RoutingOwner::Release | RoutingOwner::Beta if owner == current => "hub",
        RoutingOwner::Release | RoutingOwner::Beta => "other_channel",
        RoutingOwner::UnknownExternal => "unknown",
    }
}
```

For each client, set:

```rust
let current_owner = crate::app_flavor::current().routing_owner();
let route_owner = detect_client_owner(...);
let route_mode = route_mode_for_owner(route_owner, current_owner, stale);
```

Use existing `is_*_codexhub_config` checks as compatibility fallbacks. If an existing managed config has no beta marker but points at the current `settings.proxy_port`, classify as `current_owner`.

- [ ] **Step 5: Make switch route owner-safe**

Change function signature:

```rust
pub fn switch_gateway_client_route(
    client_id: String,
    mode: String,
    model: Option<String>,
    force_takeover: Option<bool>,
) -> Result<GatewayClientApplyResult, String>
```

Map mode:

```rust
let next_owner = match mode.as_str() {
    "official" => RoutingOwner::Official,
    "release" => RoutingOwner::Release,
    "beta" => RoutingOwner::Beta,
    "hub" => crate::app_flavor::current().routing_owner(),
    other => return Err(format!("unsupported routing owner: {other}")),
};
```

Before restore/apply, read the current target owner and call:

```rust
ensure_route_owner_mutation_allowed(
    crate::app_flavor::current().routing_owner(),
    current_target_owner,
    next_owner,
    force_takeover.unwrap_or(false),
)?;
```

If `next_owner == Official`, call restore. If `next_owner == current_app_owner`, call apply. If `next_owner` is the other app owner, return an error unless this binary is being used only for takeover into its own owner; do not make release write beta-owned config endpoints.

- [ ] **Step 6: Wire Tauri and bridge optional takeover arg**

In `main.rs` command:

```rust
fn switch_gateway_client_route(
    client_id: String,
    mode: String,
    model: Option<String>,
    force_takeover: Option<bool>,
) -> Result<gateway::GatewayClientApplyResult, String> {
    gateway::switch_gateway_client_route(client_id, mode, model, force_takeover)
}
```

In `web_bridge.rs`:

```rust
let force_takeover = optional_bool_arg(&request.args, &["forceTakeover", "force_takeover"]);
to_value(gateway::switch_gateway_client_route(client_id, mode, model, force_takeover))
```

- [ ] **Step 7: Run Rust tests**

Run:

```powershell
cd src-tauri
cargo test gateway::tests
```

Expected: PASS.

- [ ] **Step 8: Commit**

```powershell
git add src-tauri/src/gateway.rs src-tauri/src/web_bridge.rs src-tauri/src/main.rs
git commit -m "feat: add routing owner contract for gateway clients"
```

---

### Task 5: Owner-Aware Frontend Routing UI

**Files:**
- Modify: `frontend/src/lib/types.ts`
- Modify: `frontend/src/lib/tauri.ts`
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/src/components/RuntimeBar.tsx`
- Modify: `frontend/src/components/GatewayClientCard.tsx`
- Modify: `frontend/src/pages/GatewayPage.tsx`
- Modify: `frontend/src/i18n/locales/en-US.ts`
- Modify: `frontend/src/i18n/locales/zh-CN.ts`
- Modify: `frontend/scripts/ui-contract.test.mjs`

**Interfaces:**
- Consumes: `get_app_flavor`.
- Consumes: `GatewayClientInfo.route_owner`, `route_endpoint`, and `managed_by_current_app`.
- Produces: UI chips/buttons using distinct colors for `Official`, `Release`, and `Beta`.
- Produces: takeover confirmation before cross-channel overwrite.

- [ ] **Step 1: Write failing UI contract for owner colors and labels**

Add to `frontend/scripts/ui-contract.test.mjs`:

```js
test("gateway client cards render tri-state routing owner colors", async () => {
  const [cardSource, typesSource, tauriSource, gatewaySource, enSource, zhSource] = await Promise.all([
    readFile(gatewayClientCardPath, "utf8"),
    readFile(typesPath, "utf8"),
    readFile(tauriSourcePath, "utf8"),
    readFile(gatewayPagePath, "utf8"),
    readFile(enLocalePath, "utf8"),
    readFile(zhLocalePath, "utf8"),
  ]);

  assert.match(typesSource, /export type RoutingOwner = "official" \| "release" \| "beta" \| "unknown_external"/);
  assert.match(typesSource, /route_owner: RoutingOwner/);
  assert.match(tauriSource, /getAppFlavor/);
  assert.match(tauriSource, /forceTakeover/);
  assert.match(cardSource, /ROUTING_OWNER_STYLES/);
  assert.match(cardSource, /release:[\s\S]*border-sky-300[\s\S]*bg-sky-50[\s\S]*text-sky-800/);
  assert.match(cardSource, /beta:[\s\S]*border-amber-300[\s\S]*bg-amber-50[\s\S]*text-amber-800/);
  assert.match(cardSource, /Managed by/);
  assert.match(gatewaySource, /takeover/i);
  assert.match(enSource, /managedByRelease/);
  assert.match(enSource, /managedByBeta/);
  assert.match(zhSource, /managedByRelease/);
  assert.match(zhSource, /managedByBeta/);
});
```

- [ ] **Step 2: Run UI contract test to verify failure**

Run:

```powershell
cd frontend
npm run test:ui-contract -- --test-name-pattern "gateway client cards render tri-state routing owner colors"
```

Expected: FAIL.

- [ ] **Step 3: Extend frontend types and API**

In `frontend/src/lib/types.ts`:

```ts
export type RuntimeFlavor = "stable" | "beta";
export type RoutingOwner = "official" | "release" | "beta" | "unknown_external";

export interface AppFlavorInfo {
  flavor: RuntimeFlavor;
  routing_owner: RoutingOwner;
  product_name: string;
  bridge_port: number;
  gateway_port: number;
  default_codex_home_suffix: string;
}
```

Extend `GatewayClientInfo`:

```ts
route_owner: RoutingOwner;
route_endpoint?: string | null;
managed_by_current_app: boolean;
```

In `frontend/src/lib/tauri.ts`:

```ts
getAppFlavor: () => call<AppFlavorInfo>("get_app_flavor"),
switchGatewayClientRoute: (
  clientId: string,
  mode: RoutingOwner | "hub",
  model?: string | null,
  forceTakeover = false,
) =>
  call<GatewayClientApplyResult>("switch_gateway_client_route", {
    clientId,
    mode,
    model: model ?? null,
    forceTakeover,
    force_takeover: forceTakeover,
  }),
```

- [ ] **Step 4: Load app flavor in App**

In `frontend/src/App.tsx`, add flavor to runtime cache:

```ts
appFlavor: RuntimeCache<AppFlavorInfo>;
```

Initialize:

```ts
appFlavor: runtimeCache<AppFlavorInfo | null>(null),
```

Add loader:

```ts
const loadAppFlavor = useCallback(async (options?: LoadRuntimeOptions) => {
  await runCachedRequest<AppFlavorInfo>(
    "appFlavor",
    () => api.getAppFlavor(),
    options,
  );
}, [runCachedRequest]);
```

Call it in the initial runtime load path next to `loadSettings` and `loadAppVersion`. Pass `runtime.appFlavor.data` to `RuntimeBar` and `GatewayPage`.

- [ ] **Step 5: Render flavor in RuntimeBar**

In `frontend/src/components/RuntimeBar.tsx`, accept:

```ts
appFlavor?: AppFlavorInfo | null;
```

Use label:

```tsx
<span className="truncate text-base font-semibold text-ink">
  {appFlavor?.product_name ?? "CodexHub"}
</span>
{appFlavor?.flavor === "beta" ? (
  <span className="rounded-control border border-amber-300 bg-amber-50 px-1.5 py-0.5 text-[10px] font-semibold text-amber-800">
    Beta
  </span>
) : null}
```

- [ ] **Step 6: Replace binary client card route display**

In `frontend/src/components/GatewayClientCard.tsx`, replace route types:

```ts
import type { GatewayClientContract, GatewayClientInfo, RoutingOwner } from "../lib/types";

type RouteAction = "official" | "current_owner" | "takeover";

const ROUTING_OWNER_STYLES: Record<RoutingOwner, string> = {
  official: "border-slate-300 bg-slate-100 text-slate-700",
  release: "border-sky-300 bg-sky-50 text-sky-800",
  beta: "border-amber-300 bg-amber-50 text-amber-800",
  unknown_external: "border-rose-300 bg-rose-50 text-rose-800",
};
```

Compute label:

```ts
function ownerLabel(owner: RoutingOwner, endpoint?: string | null) {
  if (owner === "release") return endpoint ? `Release ${hostPort(endpoint)}` : "Release";
  if (owner === "beta") return endpoint ? `Beta ${hostPort(endpoint)}` : "Beta";
  if (owner === "unknown_external") return "External";
  return "Official";
}

function hostPort(endpoint?: string | null) {
  try {
    const url = new URL(endpoint ?? "");
    return url.host;
  } catch {
    return endpoint ?? "";
  }
}
```

Render a chip:

```tsx
<span
  className={cx(
    "rounded-full border px-2 py-0.5 text-[11px] font-semibold shadow-control",
    ROUTING_OWNER_STYLES[info?.route_owner ?? "unknown_external"],
  )}
>
  {info?.managed_by_current_app === false && (info.route_owner === "release" || info.route_owner === "beta")
    ? t(info.route_owner === "release" ? "gateway.managedByRelease" : "gateway.managedByBeta")
    : ownerLabel(info?.route_owner ?? "unknown_external", info?.route_endpoint)}
</span>
```

Keep `SegmentedSwitch` only for actionable `Official` and current owner if no other owner is present. If `managed_by_current_app === false`, show a disabled current-owner button and a separate takeover button.

- [ ] **Step 7: Add takeover confirmation in GatewayPage**

Change handler:

```ts
async function switchClientMode(clientId: string, owner: RoutingOwner, forceTakeover = false) {
  setClientBusy(`${clientId}:switch:${owner}`);
  const client = clientInfoById.get(clientId);
  if (!forceTakeover && client && client.managed_by_current_app === false) {
    const confirmText = t("gateway.takeoverConfirm", {
      name: client.name,
      path: client.config_path ?? t("common.unknown"),
      current: ownerDisplayName(client.route_owner, t),
      next: ownerDisplayName(runtimeOwner, t),
      endpoint: client.route_endpoint ?? t("common.unknown"),
    });
    if (!window.confirm(confirmText)) {
      setClientBusy(null);
      return;
    }
    forceTakeover = true;
    owner = runtimeOwner;
  }
  await api.switchGatewayClientRoute(clientId, owner, defaultModel, forceTakeover);
  await onRefreshClients();
  setClientBusy(null);
}
```

Pass `runtimeOwner={appFlavor?.routing_owner ?? "release"}` into cards.

- [ ] **Step 8: Add translations**

In English:

```ts
managedByRelease: "Managed by Release",
managedByBeta: "Managed by Beta",
takeover: "Take over",
takeoverConfirm: "Switch {{name}} from {{current}} to {{next}}?\n\nTarget: {{path}}\nCurrent endpoint: {{endpoint}}",
```

In Chinese:

```ts
managedByRelease: "由正式版管理",
managedByBeta: "由 Beta 管理",
takeover: "接管",
takeoverConfirm: "要把 {{name}} 从 {{current}} 切换到 {{next}} 吗？\n\n目标配置：{{path}}\n当前端点：{{endpoint}}",
```

- [ ] **Step 9: Run frontend checks**

Run:

```powershell
cd frontend
npm run test:ui-contract -- --test-name-pattern "gateway client cards render tri-state routing owner colors"
npm run build
```

Expected: PASS.

- [ ] **Step 10: Commit**

```powershell
git add frontend/src/lib/types.ts frontend/src/lib/tauri.ts frontend/src/App.tsx frontend/src/components/RuntimeBar.tsx frontend/src/components/GatewayClientCard.tsx frontend/src/pages/GatewayPage.tsx frontend/src/i18n/locales/en-US.ts frontend/src/i18n/locales/zh-CN.ts frontend/scripts/ui-contract.test.mjs
git commit -m "feat: render routing owner states in gateway UI"
```

---

### Task 6: Beta Update E2E And Release Artifacts

**Files:**
- Modify: `scripts/e2e-app-update.ps1`
- Modify: `scripts/build-windows-release.ps1`
- Modify: `frontend/scripts/ui-contract.test.mjs`
- Test: `scripts/e2e-app-update.ps1 -Flavor stable -ValidateOnly`
- Test: `scripts/e2e-app-update.ps1 -Flavor beta -ValidateOnly`

**Interfaces:**
- Consumes: `config/build-flavors.json`.
- Produces: beta update manifest named `latest-beta.json`.
- Produces: beta asset names `CodexHubBeta_<version>_x64-setup.exe`.

- [ ] **Step 1: Add contract test for beta update script**

Add to `frontend/scripts/ui-contract.test.mjs`:

```js
test("app update e2e script supports beta manifest and bridge ports", async () => {
  const script = await readFile(appUpdateE2ePath, "utf8");

  assert.match(script, /ValidateSet\("stable",\s*"beta"\)/);
  assert.match(script, /latest-beta\.json/);
  assert.match(script, /CodexHubBeta_/);
  assert.match(script, /1421/);
  assert.match(script, /1431/);
});
```

- [ ] **Step 2: Update `e2e-app-update.ps1` params and defaults**

Add:

```powershell
[ValidateSet("stable", "beta")]
[string]$Flavor = "stable",
```

After repo root setup:

```powershell
$flavorManifest = Get-Content -Raw -LiteralPath (Join-Path $repoRoot "config\build-flavors.json") | ConvertFrom-Json
$flavorConfig = $flavorManifest.$Flavor
if ($null -eq $flavorConfig) {
    throw "Unknown update E2E flavor: $Flavor"
}
if ([string]::IsNullOrWhiteSpace($BridgeUrl)) {
    $BridgeUrl = "http://127.0.0.1:$($flavorConfig.bridgePort)/api/invoke"
}
```

Change the param default for `$BridgeUrl` to empty string:

```powershell
[string]$BridgeUrl = "",
```

When finding assets, choose filter:

```powershell
$assetPrefix = [string]$flavorConfig.releaseAssetPrefix
$candidate = Get-ChildItem -LiteralPath $bundleDir -Filter "${assetPrefix}_*_x64-setup.exe" -ErrorAction SilentlyContinue |
```

When writing manifest:

```powershell
$manifestName = [string]$flavorConfig.updaterManifestName
$manifestPath = Join-Path $ReleaseRoot $manifestName
$manifestUrl = "http://127.0.0.1:$Port/$manifestName"
```

- [ ] **Step 3: Run validation scripts**

Run:

```powershell
scripts\e2e-app-update.ps1 -Flavor stable -ValidateOnly
scripts\e2e-app-update.ps1 -Flavor beta -ValidateOnly
```

Expected: both generate and validate manifests. Stable writes `latest.json`; beta writes `latest-beta.json`.

- [ ] **Step 4: Run contract test**

Run:

```powershell
cd frontend
npm run test:ui-contract -- --test-name-pattern "app update e2e script supports beta manifest and bridge ports"
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add scripts/e2e-app-update.ps1 scripts/build-windows-release.ps1 frontend/scripts/ui-contract.test.mjs
git commit -m "test: support beta update validation"
```

---

### Task 7: End-To-End Verification And Packaging

**Files:**
- Modify: `README.md`
- Modify: `README.zh-CN.md`
- Modify: `docs/superpowers/specs/2026-07-09-beta-release-channel-design.md`
- Test: full local quality commands

**Interfaces:**
- Consumes all prior tasks.
- Produces local stable/beta build artifacts suitable for manual side-by-side testing.

- [ ] **Step 1: Add release-channel docs**

Add a short section to `README.zh-CN.md`:

```md
## Release 与 Beta 通道

CodexHub 正式版默认使用前端端口 `1420`、桥接端口 `1421`、Gateway 端口 `9099`，并使用 `%USERPROFILE%\.codex`。

CodexHub Beta 默认使用前端端口 `1430`、桥接端口 `1431`、Gateway 端口 `9109`，并使用 `%USERPROFILE%\.codexhub-beta\codex-home`。Beta 不会在首次启动时自动接管正式版 Codex 配置。

客户端路由状态以目标配置为准：`Official`、`Release` 或 `Beta`。当某个目标由另一个通道管理时，当前 App 会显示 `Managed by Release` 或 `Managed by Beta`，需要显式确认后才会接管。
```

Add the English equivalent to `README.md`:

```md
## Release And Beta Channels

CodexHub release uses frontend port `1420`, bridge port `1421`, Gateway port `9099`, and `%USERPROFILE%\.codex` by default.

CodexHub Beta uses frontend port `1430`, bridge port `1431`, Gateway port `9109`, and `%USERPROFILE%\.codexhub-beta\codex-home` by default. Beta does not take over the release Codex config on first launch.

Client routing state is target-based: `Official`, `Release`, or `Beta`. If a target is managed by the other channel, the current app displays `Managed by Release` or `Managed by Beta` and requires explicit takeover confirmation before rewriting it.
```

- [ ] **Step 2: Run full automated checks**

Run:

```powershell
pytest -q
cd src-tauri
cargo test
cd ..\frontend
npm run test:ui-contract
npm run build
```

Expected: all pass.

- [ ] **Step 3: Build release artifacts**

Run:

```powershell
scripts\build-windows-release.ps1 -Flavor stable -SkipFrontendBuild
scripts\build-windows-release.ps1 -Flavor beta -SkipFrontendBuild
```

Expected:

- Stable installer: `src-tauri\target\release\bundle\nsis\CodexHub_0.1.1_x64-setup.exe`
- Stable manifest: `src-tauri\target\release\bundle\nsis\latest.json`
- Beta installer: `src-tauri\target\release\bundle\nsis\CodexHubBeta_0.1.1_x64-setup.exe`
- Beta manifest: `src-tauri\target\release\bundle\nsis\latest-beta.json`

- [ ] **Step 4: Run manual side-by-side smoke test**

Install or launch both builds. Verify:

```text
Release window title: CodexHub
Beta window title: CodexHub Beta
Release bridge: http://127.0.0.1:1421/api/invoke
Beta bridge: http://127.0.0.1:1431/api/invoke
Release gateway: http://127.0.0.1:9099/v1/models
Beta gateway: http://127.0.0.1:9109/v1/models
Release CODEX_HOME: %USERPROFILE%\.codex
Beta CODEX_HOME: %USERPROFILE%\.codexhub-beta\codex-home
```

Open the Gateway page in both apps. Verify one third-party target can show `Release`, then the beta app shows `Managed by Release`, and beta takeover requires confirmation before write.

- [ ] **Step 5: Commit docs and verification updates**

```powershell
git add README.md README.zh-CN.md docs/superpowers/specs/2026-07-09-beta-release-channel-design.md
git commit -m "docs: document release and beta channel behavior"
```

---

## Self-Review

Spec coverage:

- Stable/beta installer identity: Tasks 1, 2, and 6.
- Simultaneous app ports: Tasks 1 and 2.
- Separate beta `CODEX_HOME`: Task 2.
- Routing safety and tri-state owner model: Tasks 3, 4, and 5.
- UI color distinction for Release and Beta: Task 5.
- Updater channel split: Tasks 1 and 6.
- E2E and manual validation: Tasks 6 and 7.

Placeholder scan:

- The plan avoids unfinished-work placeholder markers.
- TypeScript nullish-coalescing operators in code blocks are expected syntax, not placeholder markers.
- Each task has concrete files, interfaces, commands, and expected outcomes.

Type consistency:

- Backend owner enum serializes as `official`, `release`, `beta`, `unknown_external`.
- Frontend `RoutingOwner` uses the same string values.
- `GatewayClientInfo.route_owner`, `route_endpoint`, and `managed_by_current_app` are produced in Task 4 and consumed in Task 5.
- `switch_gateway_client_route` keeps legacy `hub` compatibility while adding owner-aware modes and `force_takeover`.
