# Windows Auto Update Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Windows-only CodexHub version management and Tauri-backed automatic updates for the v0.1.0 Free Beta.

**Architecture:** Rust owns the updater plugin and exposes three app-update commands. The React frontend calls those commands through the existing `frontend/src/lib/tauri.ts` API layer, renders update controls inside the mounted `SettingsDrawer`, and runs one quiet startup check. Tauri packaging is enabled for Windows NSIS with updater artifacts and GitHub Releases metadata.

**Tech Stack:** Tauri 2, `tauri-plugin-updater`, Rust async Tauri commands, React 18, TypeScript, existing `node --test` UI contract tests.

## Global Constraints

- Windows x64 is the only v0.1.0 installer target.
- GitHub Releases is the update metadata and installer distribution channel.
- Endpoint: `https://github.com/NOirBRight/CodexHub/releases/latest/download/latest.json`.
- Windows Authenticode signing is not part of v0.1.0.
- Tauri updater signing is required for every updater package.
- The updater private key must not be committed.
- The update UI must be placed in `frontend/src/components/SettingsDrawer.tsx`, not `SettingsPage` and not Gateway.
- Exact UI placement: first `CodexHub` section, inside the rounded settings panel, immediately below the language selector and above the CodexHub behavior toggles.
- Startup update check failures must stay silent.
- Manual update check and install failures must be shown to the user.
- No forced updates, silent updates, rollback, downgrade, custom update server, macOS installer, or Linux installer.

---

## File Structure

- Create `src-tauri/src/app_updates.rs`: Owns update DTOs, pure mapping helpers, Tauri commands, and Rust unit tests.
- Modify `src-tauri/src/main.rs`: Registers the updater plugin and update commands.
- Modify `src-tauri/Cargo.toml`: Adds `tauri-plugin-updater`.
- Modify `src-tauri/tauri.conf.json`: Enables bundling, NSIS target, updater artifacts, public updater key, GitHub Releases endpoint, and passive Windows install mode.
- Modify `frontend/src/lib/types.ts`: Adds app version/update DTO types.
- Modify `frontend/src/lib/tauri.ts`: Adds desktop-only update API wrappers.
- Modify `frontend/src/components/SettingsDrawer.tsx`: Adds the `Version & Updates` block in the exact approved location.
- Modify `frontend/src/App.tsx`: Adds one delayed startup update check and install action.
- Modify `frontend/src/i18n/locales/en-US.ts`: Adds English update strings.
- Modify `frontend/src/i18n/locales/zh-CN.ts`: Adds Chinese update strings.
- Modify `frontend/scripts/ui-contract.test.mjs`: Adds source-level contract coverage for update APIs, UI placement, config, and startup behavior.

---

### Task 1: Backend Update Commands

**Files:**
- Create: `src-tauri/src/app_updates.rs`
- Modify: `src-tauri/src/main.rs`
- Modify: `src-tauri/Cargo.toml`
- Test: `src-tauri/src/app_updates.rs`

**Interfaces:**
- Produces: `app_updates::get_app_version(app: AppHandle) -> AppVersionInfo`
- Produces: `app_updates::check_app_update(app: AppHandle) -> Result<AppUpdateStatus, String>`
- Produces: `app_updates::install_app_update(app: AppHandle) -> Result<AppUpdateInstallResult, String>`
- Produces serialized DTOs:
  - `AppVersionInfo { current_version: String }`
  - `AppUpdateStatus { available: bool, current_version: String, latest_version: Option<String>, checked_at: String, notes: Option<String>, date: Option<String> }`
  - `AppUpdateInstallResult { installed: bool, version: String, message: String }`
- Consumes: `tauri-plugin-updater` Rust API via `tauri_plugin_updater::UpdaterExt`

- [ ] **Step 1: Add the updater dependency**

Edit `src-tauri/Cargo.toml` and add the updater plugin next to the shell plugin:

```toml
tauri-plugin-shell = "2"
tauri-plugin-updater = "2"
```

- [ ] **Step 2: Create failing backend unit tests**

Create `src-tauri/src/app_updates.rs` with DTOs, pure helper signatures, and tests. The command implementations can return obvious temporary values at this step so the helper tests fail first.

```rust
use serde::{Deserialize, Serialize};
use std::time::{SystemTime, UNIX_EPOCH};
use tauri::{AppHandle, Manager};
use tauri_plugin_updater::UpdaterExt;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AppVersionInfo {
    pub current_version: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AppUpdateStatus {
    pub available: bool,
    pub current_version: String,
    pub latest_version: Option<String>,
    pub checked_at: String,
    pub notes: Option<String>,
    pub date: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AppUpdateInstallResult {
    pub installed: bool,
    pub version: String,
    pub message: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct UpdateCandidate {
    version: String,
    notes: Option<String>,
    date: Option<String>,
}

#[tauri::command]
pub fn get_app_version(app: AppHandle) -> AppVersionInfo {
    version_info(current_version(&app))
}

#[tauri::command]
pub async fn check_app_update(app: AppHandle) -> Result<AppUpdateStatus, String> {
    let current = current_version(&app);
    Ok(no_update_status(current, checked_at_now()))
}

#[tauri::command]
pub async fn install_app_update(app: AppHandle) -> Result<AppUpdateInstallResult, String> {
    Ok(AppUpdateInstallResult {
        installed: false,
        version: current_version(&app),
        message: "CodexHub is already up to date.".to_string(),
    })
}

fn current_version(app: &AppHandle) -> String {
    app.package_info().version.to_string()
}

fn version_info(current_version: impl Into<String>) -> AppVersionInfo {
    AppVersionInfo {
        current_version: current_version.into(),
    }
}

fn no_update_status(current_version: impl Into<String>, checked_at: impl Into<String>) -> AppUpdateStatus {
    AppUpdateStatus {
        available: false,
        current_version: current_version.into(),
        latest_version: None,
        checked_at: checked_at.into(),
        notes: None,
        date: None,
    }
}

fn update_status(
    current_version: impl Into<String>,
    candidate: UpdateCandidate,
    checked_at: impl Into<String>,
) -> AppUpdateStatus {
    AppUpdateStatus {
        available: false,
        current_version: current_version.into(),
        latest_version: None,
        checked_at: checked_at.into(),
        notes: None,
        date: None,
    }
}

fn operation_error(action: &str, error: impl std::fmt::Display) -> String {
    format!("Failed to {action}: {error}")
}

fn checked_at_now() -> String {
    let seconds = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs())
        .unwrap_or_default();
    format!("unix:{seconds}")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn version_info_returns_current_version() {
        assert_eq!(
            version_info("0.1.0"),
            AppVersionInfo {
                current_version: "0.1.0".to_string(),
            },
        );
    }

    #[test]
    fn no_update_status_keeps_current_version_and_checked_at() {
        assert_eq!(
            no_update_status("0.1.0", "unix:123"),
            AppUpdateStatus {
                available: false,
                current_version: "0.1.0".to_string(),
                latest_version: None,
                checked_at: "unix:123".to_string(),
                notes: None,
                date: None,
            },
        );
    }

    #[test]
    fn update_status_maps_candidate_metadata() {
        assert_eq!(
            update_status(
                "0.1.0",
                UpdateCandidate {
                    version: "0.1.1".to_string(),
                    notes: Some("Bug fixes".to_string()),
                    date: Some("2026-07-08T12:00:00Z".to_string()),
                },
                "unix:456",
            ),
            AppUpdateStatus {
                available: true,
                current_version: "0.1.0".to_string(),
                latest_version: Some("0.1.1".to_string()),
                checked_at: "unix:456".to_string(),
                notes: Some("Bug fixes".to_string()),
                date: Some("2026-07-08T12:00:00Z".to_string()),
            },
        );
    }

    #[test]
    fn operation_error_includes_action_and_source_error() {
        assert_eq!(
            operation_error("check for updates", "network down"),
            "Failed to check for updates: network down",
        );
        assert_eq!(
            operation_error("install update", "signature rejected"),
            "Failed to install update: signature rejected",
        );
    }

    #[test]
    fn checked_at_now_is_unix_timestamp_string() {
        assert!(checked_at_now().starts_with("unix:"));
    }
}
```

- [ ] **Step 3: Run the failing backend test**

Run:

```powershell
cd src-tauri
cargo test app_updates --lib
```

Expected: `update_status_maps_candidate_metadata` fails because `update_status` returns `available: false` and does not copy candidate metadata.

- [ ] **Step 4: Implement backend updater behavior**

Replace the temporary command and helper bodies in `src-tauri/src/app_updates.rs` with:

```rust
#[tauri::command]
pub async fn check_app_update(app: AppHandle) -> Result<AppUpdateStatus, String> {
    let current = current_version(&app);
    let checked_at = checked_at_now();
    let update = app
        .updater()
        .map_err(|error| operation_error("check for updates", error))?
        .check()
        .await
        .map_err(|error| operation_error("check for updates", error))?;

    Ok(match update {
        Some(update) => update_status(
            current,
            UpdateCandidate {
                version: update.version.clone(),
                notes: update.body.clone(),
                date: update.date.map(|date| date.to_string()),
            },
            checked_at,
        ),
        None => no_update_status(current, checked_at),
    })
}

#[tauri::command]
pub async fn install_app_update(app: AppHandle) -> Result<AppUpdateInstallResult, String> {
    let current = current_version(&app);
    let Some(update) = app
        .updater()
        .map_err(|error| operation_error("install update", error))?
        .check()
        .await
        .map_err(|error| operation_error("install update", error))?
    else {
        return Ok(AppUpdateInstallResult {
            installed: false,
            version: current,
            message: "CodexHub is already up to date.".to_string(),
        });
    };

    let version = update.version.clone();
    update
        .download_and_install(|_chunk_length, _content_length| {}, || {})
        .await
        .map_err(|error| operation_error("install update", error))?;
    app.restart();

    Ok(AppUpdateInstallResult {
        installed: true,
        version: version.clone(),
        message: format!("CodexHub {version} installed. Restarting..."),
    })
}

fn update_status(
    current_version: impl Into<String>,
    candidate: UpdateCandidate,
    checked_at: impl Into<String>,
) -> AppUpdateStatus {
    AppUpdateStatus {
        available: true,
        current_version: current_version.into(),
        latest_version: Some(candidate.version),
        checked_at: checked_at.into(),
        notes: candidate.notes,
        date: candidate.date,
    }
}
```

Keep the tests from Step 2. If the updater crate exposes `date` as a reference type in the installed version, use `date: update.date.as_ref().map(ToString::to_string)` while keeping the DTO unchanged.

- [ ] **Step 5: Register module and commands**

Edit `src-tauri/src/main.rs`.

Add the module near the other module declarations:

```rust
mod app_updates;
mod autostart;
mod catalog;
```

Add the updater plugin before the shell plugin in `run_gui()`:

```rust
fn run_gui() {
    tauri::Builder::default()
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_shell::init())
```

Add update commands to the invoke handler before `get_status`:

```rust
.invoke_handler(tauri::generate_handler![
    app_updates::get_app_version,
    app_updates::check_app_update,
    app_updates::install_app_update,
    get_status,
```

- [ ] **Step 6: Run backend tests**

Run:

```powershell
cd src-tauri
cargo test app_updates --lib
cargo test
```

Expected: both commands pass. The full suite may compile the new updater dependency and update `src-tauri/Cargo.lock`.

- [ ] **Step 7: Commit backend update commands**

Run:

```powershell
git add src-tauri/Cargo.toml src-tauri/Cargo.lock src-tauri/src/app_updates.rs src-tauri/src/main.rs
git commit -m "feat: add app update backend commands"
```

---

### Task 2: Tauri Packaging and Updater Configuration

**Files:**
- Modify: `src-tauri/tauri.conf.json`
- Test: `frontend/scripts/ui-contract.test.mjs`

**Interfaces:**
- Consumes: `tauri_plugin_updater::Builder::new().build()` registered by Task 1
- Produces: valid Tauri config for NSIS bundling and updater metadata
- Produces: committed updater public key only

- [ ] **Step 1: Generate the updater signing key outside the repo**

Run these commands from the repo root. This stores the private key under the Windows user profile, not in Git.

```powershell
$keyDir = Join-Path $env:USERPROFILE ".codexhub"
New-Item -ItemType Directory -Force -Path $keyDir | Out-Null
$keyPath = Join-Path $keyDir "codexhub-updater.key"
cd src-tauri
cargo tauri signer generate -w $keyPath
```

If `cargo tauri` is not installed, run:

```powershell
cargo install tauri-cli --version "^2"
```

Then run the `cargo tauri signer generate` command again. Copy the generated public key line printed by the command. Do not copy or commit the private key file.

- [ ] **Step 2: Add failing config contract test**

Edit `frontend/scripts/ui-contract.test.mjs` and add this test near the existing Tauri config tests:

```js
test("tauri config enables Windows updater packaging", async () => {
  const tauriConfig = JSON.parse(await readFile(tauriConfigPath, "utf8"));

  assert.equal(tauriConfig.bundle.active, true);
  assert.deepEqual(tauriConfig.bundle.targets, ["nsis"]);
  assert.equal(tauriConfig.bundle.createUpdaterArtifacts, true);
  assert.deepEqual(tauriConfig.plugins.updater.endpoints, [
    "https://github.com/NOirBRight/CodexHub/releases/latest/download/latest.json",
  ]);
  assert.equal(tauriConfig.plugins.updater.windows.installMode, "passive");
  assert.equal(typeof tauriConfig.plugins.updater.pubkey, "string");
  assert.ok(tauriConfig.plugins.updater.pubkey.length > 80);
});
```

- [ ] **Step 3: Run the failing config test**

Run:

```powershell
cd frontend
npm run test:ui-contract
```

Expected: the new config test fails because `bundle.active` is still `false` and `plugins.updater` is missing.

- [ ] **Step 4: Enable Windows packaging and updater config**

Edit `src-tauri/tauri.conf.json`. Keep the existing app, CSP, product name, version, identifier, and icons. Change only the `bundle` block and add `plugins` at the top level:

```json
  "bundle": {
    "active": true,
    "targets": ["nsis"],
    "createUpdaterArtifacts": true,
    "icon": [
      "icons/32x32.png",
      "icons/128x128.png",
      "icons/128x128@2x.png",
      "icons/icon.png",
      "icons/icon.ico"
    ]
  },
  "plugins": {
    "updater": {
      "endpoints": [
        "https://github.com/NOirBRight/CodexHub/releases/latest/download/latest.json"
      ],
      "windows": {
        "installMode": "passive"
      }
    }
  }
```

Add `pubkey` inside `plugins.updater` as a sibling of `endpoints`. Its value must be the exact public key printed by the Step 1 signer command. After saving, `src-tauri/tauri.conf.json` must contain:

- `bundle.active` set to `true`
- `bundle.targets` set to `["nsis"]`
- `bundle.createUpdaterArtifacts` set to `true`
- `plugins.updater.pubkey` set to the generated public key string
- `plugins.updater.endpoints[0]` set to the GitHub Releases `latest.json` URL
- `plugins.updater.windows.installMode` set to `"passive"`

Do not commit the file if `plugins.updater.pubkey` is empty, contains angle brackets, contains the word `paste`, or is shorter than 80 characters.

- [ ] **Step 5: Verify no private key is tracked**

Run:

```powershell
git status --short
git ls-files | Select-String -Pattern "codexhub-updater.key|tauri.key|private"
```

Expected: `git status --short` shows only repo files, and `git ls-files` prints no private key path.

- [ ] **Step 6: Run config and backend tests**

Run:

```powershell
cd frontend
npm run test:ui-contract
cd ..\src-tauri
cargo test app_updates --lib
```

Expected: tests pass.

- [ ] **Step 7: Commit packaging config**

Run:

```powershell
git add src-tauri/tauri.conf.json frontend/scripts/ui-contract.test.mjs
git commit -m "build: enable Windows updater packaging"
```

---

### Task 3: Frontend Update API and Settings Drawer UI

**Files:**
- Modify: `frontend/src/lib/types.ts`
- Modify: `frontend/src/lib/tauri.ts`
- Modify: `frontend/src/components/SettingsDrawer.tsx`
- Modify: `frontend/src/i18n/locales/en-US.ts`
- Modify: `frontend/src/i18n/locales/zh-CN.ts`
- Modify: `frontend/scripts/ui-contract.test.mjs`

**Interfaces:**
- Consumes: `get_app_version`, `check_app_update`, `install_app_update` commands from Task 1
- Produces: `api.getAppVersion()`, `api.checkAppUpdate()`, `api.installAppUpdate()`
- Produces: `Version & Updates` block in the approved `SettingsDrawer` location

- [ ] **Step 1: Add failing frontend contract tests**

Edit `frontend/scripts/ui-contract.test.mjs`.

Add `settingsPagePath` near the other file constants:

```js
const settingsPagePath = new URL("../src/pages/SettingsPage.tsx", import.meta.url);
```

Add these tests near the existing settings and Tauri API contract tests:

```js
test("app update APIs are desktop-only wrappers", async () => {
  const [tauriSource, typesSource] = await Promise.all([
    readFile(tauriSourcePath, "utf8"),
    readFile(typesPath, "utf8"),
  ]);

  assert.match(typesSource, /export interface AppVersionInfo/);
  assert.match(typesSource, /current_version: string/);
  assert.match(typesSource, /export interface AppUpdateStatus/);
  assert.match(typesSource, /available: boolean/);
  assert.match(typesSource, /latest_version\?: string \| null/);
  assert.match(typesSource, /export interface AppUpdateInstallResult/);
  assert.match(typesSource, /installed: boolean/);
  assert.match(tauriSource, /getAppVersion: \(\) => desktopCall<AppVersionInfo>\("get_app_version"\)/);
  assert.match(tauriSource, /checkAppUpdate: \(\) => desktopCall<AppUpdateStatus>\("check_app_update"\)/);
  assert.match(tauriSource, /installAppUpdate: \(\) => desktopCall<AppUpdateInstallResult>\("install_app_update"\)/);
  assert.doesNotMatch(tauriSource, /checkAppUpdate: \(\) => call/);
  assert.doesNotMatch(tauriSource, /installAppUpdate: \(\) => call/);
});

test("settings drawer places version updates below language and above behavior toggles", async () => {
  const [settingsDrawerSource, settingsPageSource, enSource, zhSource] = await Promise.all([
    readFile(settingsDrawerPath, "utf8"),
    readFile(settingsPagePath, "utf8"),
    readFile(enLocalePath, "utf8"),
    readFile(zhLocalePath, "utf8"),
  ]);

  const codexSection =
    settingsDrawerSource.match(/<section className="grid gap-3">[\s\S]*?<h3 className="text-sm font-semibold text-ink">CodexHub<\/h3>[\s\S]*?<section className="grid gap-3">/)?.[0] ?? "";

  assert.match(codexSection, /t\("settings\.language"\)[\s\S]*t\("settings\.updates"\)[\s\S]*draft\.auto_start_proxy/);
  assert.match(settingsDrawerSource, /function VersionUpdateBlock/);
  assert.match(settingsDrawerSource, /api\.getAppVersion\(\)/);
  assert.match(settingsDrawerSource, /api\.checkAppUpdate\(\)/);
  assert.match(settingsDrawerSource, /api\.installAppUpdate\(\)/);
  assert.doesNotMatch(settingsPageSource, /settings\.updates/);
  assert.match(enSource, /updates: "Version & Updates"/);
  assert.match(zhSource, /updates: "版本与更新"/);
  assert.match(enSource, /installUpdate: "Install update"/);
  assert.match(zhSource, /installUpdate: "安装更新"/);
});
```

- [ ] **Step 2: Run failing frontend contract tests**

Run:

```powershell
cd frontend
npm run test:ui-contract
```

Expected: the new tests fail because types, APIs, translations, and `VersionUpdateBlock` do not exist.

- [ ] **Step 3: Add TypeScript update DTOs**

Edit `frontend/src/lib/types.ts` near `AppStatus` and add:

```ts
export interface AppVersionInfo {
  current_version: string;
}

export interface AppUpdateStatus {
  available: boolean;
  current_version: string;
  latest_version?: string | null;
  checked_at: string;
  notes?: string | null;
  date?: string | null;
}

export interface AppUpdateInstallResult {
  installed: boolean;
  version: string;
  message: string;
}
```

- [ ] **Step 4: Add desktop-only update API wrappers**

Edit `frontend/src/lib/tauri.ts`.

Add the new types to the import list:

```ts
  AppUpdateInstallResult,
  AppUpdateStatus,
  AppVersionInfo,
```

Add the update API wrappers at the top of `export const api = {` before `getStatus`:

```ts
  getAppVersion: () => desktopCall<AppVersionInfo>("get_app_version"),
  checkAppUpdate: () => desktopCall<AppUpdateStatus>("check_app_update"),
  installAppUpdate: () => desktopCall<AppUpdateInstallResult>("install_app_update"),
```

- [ ] **Step 5: Add update translation strings**

Edit the `settings` object in `frontend/src/i18n/locales/en-US.ts` and add:

```ts
    checkForUpdates: "Check for updates",
    checkingUpdates: "Checking for updates...",
    currentVersion: "Current version",
    desktopUpdatesUnavailable: "Updates are available in the desktop app.",
    installUpdate: "Install update",
    installingUpdate: "Installing update...",
    noUpdatesAvailable: "CodexHub is up to date.",
    updateAvailable: "CodexHub {{version}} is available.",
    updateCheckFailed: "Update check failed: {{message}}",
    updateInstallConfirm: "CodexHub will close while the update installer runs. Continue?",
    updateInstallUnavailable: "No update is available to install.",
    updates: "Version & Updates",
```

Edit the `settings` object in `frontend/src/i18n/locales/zh-CN.ts` and add:

```ts
    checkForUpdates: "检查更新",
    checkingUpdates: "正在检查更新...",
    currentVersion: "当前版本",
    desktopUpdatesUnavailable: "更新功能仅在桌面应用中可用。",
    installUpdate: "安装更新",
    installingUpdate: "正在安装更新...",
    noUpdatesAvailable: "CodexHub 已是最新版本。",
    updateAvailable: "CodexHub {{version}} 可用。",
    updateCheckFailed: "检查更新失败：{{message}}",
    updateInstallConfirm: "CodexHub 会在运行更新安装器时关闭。继续？",
    updateInstallUnavailable: "当前没有可安装的更新。",
    updates: "版本与更新",
```

- [ ] **Step 6: Add the SettingsDrawer update block**

Edit `frontend/src/components/SettingsDrawer.tsx`.

Change imports:

```ts
import { Check, ChevronDown, Download, RefreshCcw, Save, X } from "lucide-react";
import { api, messageFromError } from "../lib/tauri";
import type { AppUpdateStatus, AppVersionInfo, Model, Provider, Settings } from "../lib/types";
```

Add state inside `SettingsDrawer`:

```ts
  const [versionInfo, setVersionInfo] = useState<AppVersionInfo | null>(null);
  const [updateStatus, setUpdateStatus] = useState<AppUpdateStatus | null>(null);
  const [updateBusy, setUpdateBusy] = useState<"check" | "install" | null>(null);
```

Add this effect after the existing `open` effects:

```ts
  useEffect(() => {
    if (!open) {
      return;
    }
    void loadAppVersion();
  }, [open]);
```

Add these functions inside `SettingsDrawer` before `requestClose()`:

```ts
  async function loadAppVersion() {
    const info = await api.getAppVersion();
    setVersionInfo(info);
  }

  async function checkForUpdates() {
    const toastId = showToast(t("settings.checkingUpdates"), "loading");
    setUpdateBusy("check");
    try {
      const status = await api.checkAppUpdate();
      if (!status) {
        updateToast(toastId, {
          action: null,
          text: t("settings.desktopUpdatesUnavailable"),
          tone: "info",
        });
        return;
      }
      setVersionInfo({ current_version: status.current_version });
      setUpdateStatus(status);
      updateToast(toastId, {
        action: null,
        text: status.available && status.latest_version
          ? t("settings.updateAvailable", { version: status.latest_version })
          : t("settings.noUpdatesAvailable"),
        tone: status.available ? "info" : "success",
      });
    } catch (err) {
      updateToast(toastId, {
        action: null,
        text: t("settings.updateCheckFailed", { message: messageFromError(err) }),
        tone: "error",
      });
    } finally {
      setUpdateBusy(null);
    }
  }

  async function installUpdate() {
    if (!updateStatus?.available) {
      showToast(t("settings.updateInstallUnavailable"), "info");
      return;
    }
    if (!window.confirm(t("settings.updateInstallConfirm"))) {
      return;
    }
    const toastId = showToast({
      text: t("settings.installingUpdate"),
      tone: "loading",
      timeoutMs: null,
    });
    setUpdateBusy("install");
    try {
      const result = await api.installAppUpdate();
      if (!result) {
        updateToast(toastId, {
          action: null,
          text: t("settings.desktopUpdatesUnavailable"),
          tone: "info",
        });
        return;
      }
      if (!result.installed) {
        updateToast(toastId, {
          action: null,
          text: result.message,
          tone: "info",
        });
      }
    } catch (err) {
      updateToast(toastId, {
        action: null,
        text: messageFromError(err),
        tone: "error",
      });
    } finally {
      setUpdateBusy(null);
    }
  }
```

Place this JSX immediately after the language selector `<div>` and before the `Toggle` for `draft.auto_start_proxy`:

```tsx
                <VersionUpdateBlock
                  busy={updateBusy}
                  status={updateStatus}
                  versionInfo={versionInfo}
                  onCheck={() => void checkForUpdates()}
                  onInstall={() => void installUpdate()}
                />
```

Add this component below `settingsSaveComparable`:

```tsx
function VersionUpdateBlock({
  busy,
  onCheck,
  onInstall,
  status,
  versionInfo,
}: {
  busy: "check" | "install" | null;
  onCheck: () => void;
  onInstall: () => void;
  status: AppUpdateStatus | null;
  versionInfo: AppVersionInfo | null;
}) {
  const { t } = useTranslation();
  const currentVersion = status?.current_version ?? versionInfo?.current_version ?? t("common.unknown");
  const latestVersion = status?.latest_version ?? null;

  return (
    <div className="grid gap-2 rounded-inner bg-surface px-3 py-2 text-sm font-medium text-slate-700 shadow-control">
      <div className="flex min-w-0 items-center justify-between gap-3">
        <span className="min-w-0 truncate text-xs font-semibold text-slate-500">
          {t("settings.updates")}
        </span>
        <span className="shrink-0 rounded-full bg-panel px-2 py-0.5 font-mono text-[11px] font-semibold text-slate-600">
          v{currentVersion}
        </span>
      </div>
      <div className="grid min-w-0 gap-1">
        <div className="flex min-w-0 items-center justify-between gap-3">
          <span className="min-w-0 truncate text-sm text-slate-700">{t("settings.currentVersion")}</span>
          <span className="shrink-0 font-mono text-xs font-semibold text-ink">{currentVersion}</span>
        </div>
        {status?.available && latestVersion && (
          <p className="min-w-0 text-xs leading-5 text-action">
            {t("settings.updateAvailable", { version: latestVersion })}
          </p>
        )}
        {status && !status.available && (
          <p className="min-w-0 text-xs leading-5 text-emerald-700">{t("settings.noUpdatesAvailable")}</p>
        )}
        {status?.notes && (
          <p className="max-h-24 min-w-0 overflow-auto whitespace-pre-wrap break-words text-xs leading-5 text-slate-500">
            {status.notes}
          </p>
        )}
      </div>
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          className="mini-button"
          disabled={Boolean(busy)}
          onClick={onCheck}
        >
          <RefreshCcw size={14} className={busy === "check" ? "animate-spin" : ""} />
          {t("settings.checkForUpdates")}
        </button>
        {status?.available && (
          <button
            type="button"
            className="focus-ring inline-flex h-8 items-center justify-center gap-2 rounded-control bg-ink px-3 text-xs font-semibold text-white shadow-control transition-[box-shadow,background-color,transform] duration-150 ease-out hover:bg-slate-800 hover:shadow-raised active:scale-[0.96] disabled:bg-slate-300"
            disabled={Boolean(busy)}
            onClick={onInstall}
          >
            <Download size={14} />
            {t("settings.installUpdate")}
          </button>
        )}
      </div>
    </div>
  );
}
```

- [ ] **Step 7: Run frontend tests and type check**

Run:

```powershell
cd frontend
npm run test:ui-contract
npm run build
```

Expected: both commands pass.

- [ ] **Step 8: Commit frontend settings update UI**

Run:

```powershell
git add frontend/src/lib/types.ts frontend/src/lib/tauri.ts frontend/src/components/SettingsDrawer.tsx frontend/src/i18n/locales/en-US.ts frontend/src/i18n/locales/zh-CN.ts frontend/scripts/ui-contract.test.mjs
git commit -m "feat: add update controls to settings drawer"
```

---

### Task 4: Startup Update Check and End-to-End Verification

**Files:**
- Modify: `frontend/src/App.tsx`
- Modify: `frontend/scripts/ui-contract.test.mjs`
- Verify: `docs/superpowers/specs/2026-07-08-windows-auto-update-design.md`

**Interfaces:**
- Consumes: `api.checkAppUpdate()` and `api.installAppUpdate()` from Task 3
- Produces: one delayed, silent-on-error startup update check
- Produces: toast action that installs the update after user confirmation through existing install command

- [ ] **Step 1: Add failing startup behavior contract test**

Edit `frontend/scripts/ui-contract.test.mjs` and add this test near other `App.tsx` behavior tests:

```js
test("startup update check is delayed and silent on failure", async () => {
  const appSource = await readFile(appPath, "utf8");

  assert.match(appSource, /STARTUP_UPDATE_CHECK_DELAY_MS\s*=\s*2500/);
  assert.match(appSource, /startupUpdateCheckStarted/);
  assert.match(appSource, /api\.checkAppUpdate\(\)/);
  assert.match(appSource, /settings\.updateAvailable/);
  assert.match(appSource, /settings\.installUpdate/);
  assert.match(appSource, /api\.installAppUpdate\(\)/);
  assert.match(appSource, /Startup update checks are best-effort/);
  assert.doesNotMatch(appSource, /setBanner\(messageFromError\(err\)\)[\s\S]*Startup update/);
});
```

- [ ] **Step 2: Run the failing startup contract test**

Run:

```powershell
cd frontend
npm run test:ui-contract
```

Expected: the new test fails because `App.tsx` has no startup update check yet.

- [ ] **Step 3: Add startup update check to App.tsx**

Edit `frontend/src/App.tsx`.

Add this constant near `BACKGROUND_VERSION_PROBE_DELAY_MS`:

```ts
const STARTUP_UPDATE_CHECK_DELAY_MS = 2500;
```

Add this ref inside `App()` near `gatewayClientLoadSeq`:

```ts
  const startupUpdateCheckStarted = useRef(false);
```

Add this callback after `loadRuntime`:

```ts
  const installAppUpdate = useCallback(async () => {
    if (!window.confirm(t("settings.updateInstallConfirm"))) {
      return;
    }
    const toastId = showToast({
      text: t("settings.installingUpdate"),
      tone: "loading",
      timeoutMs: null,
    });
    try {
      const result = await api.installAppUpdate();
      if (!result) {
        updateToast(toastId, {
          action: null,
          text: t("settings.desktopUpdatesUnavailable"),
          tone: "info",
        });
        return;
      }
      if (!result.installed) {
        updateToast(toastId, {
          action: null,
          text: result.message,
          tone: "info",
        });
      }
    } catch (err) {
      updateToast(toastId, {
        action: null,
        text: messageFromError(err),
        tone: "error",
      });
    }
  }, [showToast, t, updateToast]);

  const runStartupUpdateCheck = useCallback(async () => {
    try {
      const status = await api.checkAppUpdate();
      if (!status?.available || !status.latest_version) {
        return;
      }
      showToast({
        action: {
          label: t("settings.installUpdate"),
          onClick: () => void installAppUpdate(),
        },
        text: t("settings.updateAvailable", { version: status.latest_version }),
        timeoutMs: null,
        tone: "info",
      });
    } catch {
      // Startup update checks are best-effort and should not create noisy banners.
    }
  }, [installAppUpdate, showToast, t]);
```

Add this effect after the existing first-load effect:

```ts
  useEffect(() => {
    if (startupUpdateCheckStarted.current || !runtime.settings) {
      return;
    }
    startupUpdateCheckStarted.current = true;
    const timer = window.setTimeout(
      () => void runStartupUpdateCheck(),
      STARTUP_UPDATE_CHECK_DELAY_MS,
    );
    return () => window.clearTimeout(timer);
  }, [runStartupUpdateCheck, runtime.settings]);
```

- [ ] **Step 4: Run frontend tests**

Run:

```powershell
cd frontend
npm run test:ui-contract
npm run build
```

Expected: both commands pass.

- [ ] **Step 5: Run backend tests**

Run:

```powershell
cd src-tauri
cargo test
```

Expected: all Rust tests pass.

- [ ] **Step 6: Verify release build configuration without publishing**

Run:

```powershell
cd src-tauri
cargo tauri build --bundles nsis
```

Expected:

- A Windows NSIS setup executable is produced under `src-tauri/target/release/bundle/nsis/`.
- Updater artifacts are produced with Tauri signatures.
- No private updater key appears in `git status --short`.

- [ ] **Step 7: Verify spec coverage**

Open `docs/superpowers/specs/2026-07-08-windows-auto-update-design.md` and confirm each of these requirements has implementation:

```text
Current version visible in settings.
Manual check in SettingsDrawer.
Install button appears only when update is available.
Startup check is delayed.
Startup check errors are silent.
Manual check errors are shown.
Tauri updater plugin is registered.
NSIS bundling is active.
Updater artifacts are enabled.
GitHub Releases latest.json endpoint is configured.
Windows install mode is passive.
Updater private key is not committed.
```

- [ ] **Step 8: Commit startup check and verification**

Run:

```powershell
git add frontend/src/App.tsx frontend/scripts/ui-contract.test.mjs
git commit -m "feat: check for app updates on startup"
```

---

## Final Verification

Run the full verification set from the repository root:

```powershell
cd frontend
npm run test:ui-contract
npm run build
cd ..\src-tauri
cargo test
cargo tauri build --bundles nsis
git status --short
```

Expected:

- Frontend contract tests pass.
- Frontend production build passes.
- Rust tests pass.
- Tauri produces a Windows NSIS installer and updater artifacts.
- `git status --short` is clean after the final commit.
- No updater private key path is tracked.

## Release Notes for This Work

Use this summary when preparing the PR or release note:

```text
Added Windows app version management and Tauri updater support. CodexHub now shows its current version in Settings, can manually check for updates, prompts before installing an available update, and performs a quiet startup update check. Windows packaging is configured for NSIS and GitHub Releases updater metadata with Tauri updater signature verification.
```
