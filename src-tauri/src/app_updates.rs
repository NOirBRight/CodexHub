use crate::{
    build_info::{self, BuildFlavor},
    runtime_paths, safe_file,
};
use serde::{Deserialize, Serialize};
use std::{
    fs,
    path::{Path, PathBuf},
    sync::{Mutex, OnceLock},
    time::{SystemTime, UNIX_EPOCH},
};
use tauri::AppHandle;
use tauri_plugin_updater::{Error as UpdaterError, Update, Updater, UpdaterExt};

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

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum AppUpdateInstallPhase {
    Idle,
    Checking,
    Downloading,
    Installing,
    Restarting,
    Failed,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AppUpdateInstallStatus {
    pub phase: AppUpdateInstallPhase,
    pub current_version: String,
    pub target_version: Option<String>,
    pub downloaded_bytes: u64,
    pub total_bytes: Option<u64>,
    pub message: String,
    pub updated_at: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct AppUpdateCompletionStatus {
    pub completed: bool,
    pub current_version: String,
    pub target_version: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct UpdateCandidate {
    version: String,
    notes: Option<String>,
    date: Option<String>,
}

#[derive(Debug, Deserialize)]
struct FlavorUpdateManifest {
    version: String,
    #[serde(default)]
    codexhub_flavor: Option<BuildFlavor>,
    platforms: std::collections::BTreeMap<String, FlavorUpdatePlatform>,
}

#[derive(Debug, Deserialize)]
struct FlavorUpdatePlatform {
    signature: String,
    url: String,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
struct PendingUpdate {
    target_version: String,
    written_at: String,
}

static INSTALL_STATUS: OnceLock<Mutex<AppUpdateInstallStatus>> = OnceLock::new();

#[tauri::command]
pub fn get_app_version(app: AppHandle) -> AppVersionInfo {
    version_info(current_version(&app))
}

#[tauri::command]
pub async fn check_app_update(app: AppHandle) -> Result<AppUpdateStatus, String> {
    let current = current_version(&app);
    let checked_at = checked_at_now();
    let flavor = build_info::current().flavor;
    let update = match updater(&app, "check for updates")?.check().await {
        Ok(Some(update)) => {
            validate_checked_update(&update, flavor)?;
            Ok(Some(UpdateCandidate {
                version: update.version.clone(),
                notes: update.body.clone(),
                date: update.date.as_ref().map(ToString::to_string),
            }))
        }
        Ok(None) => Ok(None),
        Err(error) => Err(error),
    };

    status_from_update_check(current, checked_at, update)
}

#[tauri::command]
pub async fn install_app_update(app: AppHandle) -> Result<AppUpdateInstallResult, String> {
    let status = start_app_update_install(app)?;
    Ok(AppUpdateInstallResult {
        installed: is_active_install_phase(&status.phase),
        version: status
            .target_version
            .clone()
            .unwrap_or_else(|| status.current_version.clone()),
        message: status.message,
    })
}

#[tauri::command]
pub fn start_app_update_install(app: AppHandle) -> Result<AppUpdateInstallStatus, String> {
    let current = current_version(&app);
    let status = get_install_status_with_current(&current);
    if is_active_install_phase(&status.phase) {
        return Ok(status);
    }

    let checking = set_install_status(AppUpdateInstallStatus {
        phase: AppUpdateInstallPhase::Checking,
        current_version: current,
        target_version: None,
        downloaded_bytes: 0,
        total_bytes: None,
        message: "Checking for updates...".to_string(),
        updated_at: checked_at_now(),
    });
    tauri::async_runtime::spawn(async move {
        if let Err(error) = run_app_update_install(app).await {
            mark_failed(error);
        }
    });

    Ok(checking)
}

#[tauri::command]
pub fn get_app_update_install_status(app: AppHandle) -> AppUpdateInstallStatus {
    get_install_status_with_current(&current_version(&app))
}

#[tauri::command]
pub fn consume_app_update_completion(
    app: AppHandle,
) -> Result<Option<AppUpdateCompletionStatus>, String> {
    consume_pending_update_completion(&pending_update_path()?, &current_version(&app))
}

fn current_version(app: &AppHandle) -> String {
    app.package_info().version.to_string()
}

fn version_info(current_version: impl Into<String>) -> AppVersionInfo {
    AppVersionInfo {
        current_version: current_version.into(),
    }
}

fn updater(app: &AppHandle, action: &str) -> Result<Updater, String> {
    let endpoints = configured_updater_endpoints()?;
    app.updater_builder()
        .endpoints(endpoints)
        .map_err(|error| operation_error(action, error))?
        .build()
        .map_err(|error| updater_setup_error(action, error))
}

fn configured_updater_endpoints() -> Result<Vec<reqwest::Url>, String> {
    #[cfg(debug_assertions)]
    if let Some(endpoint) =
        std::env::var_os("CODEXHUB_UPDATE_E2E_ENDPOINT").filter(|value| !value.is_empty())
    {
        let endpoint = endpoint.to_string_lossy().trim().to_string();
        return endpoint
            .parse()
            .map(|endpoint| vec![endpoint])
            .map_err(|error| operation_error("configure update endpoint", error));
    }

    build_info::current()
        .flavor
        .updater_endpoint()
        .parse()
        .map(|endpoint| vec![endpoint])
        .map_err(|error| operation_error("configure update endpoint", error))
}

async fn checked_update(app: &AppHandle, action: &str) -> Result<Option<Update>, String> {
    match updater(app, action)?.check().await {
        Ok(update) => Ok(update),
        Err(UpdaterError::ReleaseNotFound) => Ok(None),
        Err(error) => Err(operation_error(action, error)),
    }
}

fn parse_flavor_manifest(
    manifest: &str,
    expected_flavor: BuildFlavor,
) -> Result<FlavorUpdateManifest, String> {
    serde_json::from_str(manifest).map_err(|error| {
        format!(
            "Rejected {} update manifest: invalid JSON or flavor metadata: {error}",
            expected_flavor.as_str()
        )
    })
}

#[cfg(test)]
fn validate_flavor_manifest(manifest: &str, expected_flavor: BuildFlavor) -> Result<(), String> {
    let manifest = parse_flavor_manifest(manifest, expected_flavor)?;
    validate_flavor_manifest_data(&manifest, expected_flavor)
}

fn validate_flavor_manifest_data(
    manifest: &FlavorUpdateManifest,
    expected_flavor: BuildFlavor,
) -> Result<(), String> {
    let version = manifest.version.trim();
    if version.is_empty() {
        return Err(format!(
            "Rejected {} update manifest: version is empty.",
            expected_flavor.as_str()
        ));
    }

    match manifest.codexhub_flavor {
        Some(actual_flavor) if actual_flavor == expected_flavor => {}
        Some(actual_flavor) => {
            return Err(format!(
                "Rejected {} update manifest: manifest declares {}.",
                expected_flavor.as_str(),
                actual_flavor.as_str()
            ));
        }
        None if expected_flavor.accepts_legacy_flavorless_manifest() => {}
        None => {
            return Err(format!(
                "Rejected {} update manifest: codexhub_flavor is required.",
                expected_flavor.as_str()
            ));
        }
    }

    let platform = manifest.platforms.get("windows-x86_64").ok_or_else(|| {
        format!(
            "Rejected {} update manifest: windows-x86_64 artifact is missing.",
            expected_flavor.as_str()
        )
    })?;
    if platform.signature.trim().is_empty() {
        return Err(format!(
            "Rejected {} update manifest: artifact signature is empty.",
            expected_flavor.as_str()
        ));
    }

    let url: reqwest::Url = platform.url.parse().map_err(|error| {
        format!(
            "Rejected {} update manifest: artifact URL is invalid: {error}",
            expected_flavor.as_str()
        )
    })?;
    let expected_name = expected_flavor.installer_name(version);
    if !url.path().ends_with(&format!("/{expected_name}")) {
        return Err(format!(
            "Rejected {} update manifest: expected artifact {expected_name}, got {}.",
            expected_flavor.as_str(),
            url.path()
        ));
    }

    Ok(())
}

fn validate_checked_update(update: &Update, expected_flavor: BuildFlavor) -> Result<(), String> {
    let raw_manifest = serde_json::to_string(&update.raw_json).map_err(|error| {
        format!(
            "Rejected {} update manifest: unable to encode updater response: {error}",
            expected_flavor.as_str()
        )
    })?;
    validate_checked_update_payload(
        &raw_manifest,
        &update.version,
        update.download_url.as_str(),
        expected_flavor,
    )
}

fn validate_checked_update_payload(
    raw_manifest: &str,
    selected_version: &str,
    selected_download_url: &str,
    expected_flavor: BuildFlavor,
) -> Result<(), String> {
    let manifest = parse_flavor_manifest(raw_manifest, expected_flavor)?;
    validate_flavor_manifest_data(&manifest, expected_flavor)?;

    let manifest_version = manifest.version.trim();
    if manifest_version != selected_version.trim() {
        return Err(format!(
            "Rejected {} update: updater selected version {}, but its checked manifest declares {}.",
            expected_flavor.as_str(),
            selected_version,
            manifest.version
        ));
    }

    let selected_url: reqwest::Url = selected_download_url.parse().map_err(|error| {
        format!(
            "Rejected {} update: updater selected an invalid artifact URL: {error}",
            expected_flavor.as_str()
        )
    })?;
    let expected_name = expected_flavor.installer_name(manifest_version);
    if !selected_url.path().ends_with(&format!("/{expected_name}")) {
        return Err(format!(
            "Rejected {} update: updater selected {}, expected {expected_name}.",
            expected_flavor.as_str(),
            selected_url.path()
        ));
    }

    Ok(())
}

fn no_update_status(
    current_version: impl Into<String>,
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

fn status_from_update_check(
    current_version: impl Into<String>,
    checked_at: impl Into<String>,
    update: Result<Option<UpdateCandidate>, UpdaterError>,
) -> Result<AppUpdateStatus, String> {
    let current_version = current_version.into();
    let checked_at = checked_at.into();
    match update {
        Ok(Some(candidate)) => Ok(update_status(current_version, candidate, checked_at)),
        Ok(None) | Err(UpdaterError::ReleaseNotFound) => {
            Ok(no_update_status(current_version, checked_at))
        }
        Err(error) => Err(operation_error("check for updates", error)),
    }
}

async fn run_app_update_install(app: AppHandle) -> Result<(), String> {
    let current = current_version(&app);
    let Some(update) = checked_update(&app, "install update").await? else {
        set_install_status(install_status_idle_with_message(
            current,
            "CodexHub is already up to date.",
        ));
        return Ok(());
    };
    validate_checked_update(&update, build_info::current().flavor)?;

    let target = update.version.clone();
    set_install_status(AppUpdateInstallStatus {
        phase: AppUpdateInstallPhase::Downloading,
        current_version: current.clone(),
        target_version: Some(target.clone()),
        downloaded_bytes: 0,
        total_bytes: None,
        message: "Downloading update...".to_string(),
        updated_at: checked_at_now(),
    });

    let chunk_current = current.clone();
    let chunk_target = target.clone();
    let finish_target = target.clone();
    let bytes = update
        .download(
            move |chunk_length, content_length| {
                mutate_install_status(|status| {
                    if status.current_version.is_empty() {
                        status.current_version = chunk_current.clone();
                    }
                    record_download_chunk(status, &chunk_target, chunk_length, content_length);
                });
            },
            move || {
                mutate_install_status(|status| mark_installing(status, &finish_target));
            },
        )
        .await
        .map_err(|error| operation_error("download update", error))?;

    write_pending_update(&pending_update_path()?, &target)?;
    mark_restarting_global(&target);

    if update_e2e_download_only() {
        return Ok(());
    }

    update
        .install(bytes)
        .map_err(|error| operation_error("install update", error))?;
    restart_after_update(app)
}

fn install_status_store() -> &'static Mutex<AppUpdateInstallStatus> {
    INSTALL_STATUS.get_or_init(|| Mutex::new(install_status_idle("")))
}

fn get_install_status_with_current(current_version: &str) -> AppUpdateInstallStatus {
    mutate_install_status(|status| {
        if status.current_version.is_empty()
            || matches!(status.phase, AppUpdateInstallPhase::Idle)
                && status.target_version.is_none()
        {
            status.current_version = current_version.to_string();
        }
    })
}

fn set_install_status(status: AppUpdateInstallStatus) -> AppUpdateInstallStatus {
    let mut guard = install_status_store()
        .lock()
        .expect("app update install status lock poisoned");
    *guard = status;
    guard.clone()
}

fn mutate_install_status(
    mutate: impl FnOnce(&mut AppUpdateInstallStatus),
) -> AppUpdateInstallStatus {
    let mut guard = install_status_store()
        .lock()
        .expect("app update install status lock poisoned");
    mutate(&mut guard);
    guard.updated_at = checked_at_now();
    guard.clone()
}

fn install_status_idle(current_version: impl Into<String>) -> AppUpdateInstallStatus {
    install_status_idle_with_message(current_version, "Idle")
}

fn install_status_idle_with_message(
    current_version: impl Into<String>,
    message: impl Into<String>,
) -> AppUpdateInstallStatus {
    AppUpdateInstallStatus {
        phase: AppUpdateInstallPhase::Idle,
        current_version: current_version.into(),
        target_version: None,
        downloaded_bytes: 0,
        total_bytes: None,
        message: message.into(),
        updated_at: checked_at_now(),
    }
}

fn record_download_chunk(
    status: &mut AppUpdateInstallStatus,
    target_version: &str,
    chunk_length: usize,
    content_length: Option<u64>,
) {
    status.phase = AppUpdateInstallPhase::Downloading;
    status.target_version = Some(target_version.to_string());
    status.downloaded_bytes = status
        .downloaded_bytes
        .saturating_add(u64::try_from(chunk_length).unwrap_or(u64::MAX));
    status.total_bytes = content_length;
    status.message = "Downloading update...".to_string();
    status.updated_at = checked_at_now();
}

fn mark_installing(status: &mut AppUpdateInstallStatus, target_version: &str) {
    status.phase = AppUpdateInstallPhase::Installing;
    status.target_version = Some(target_version.to_string());
    status.message = "Installing update...".to_string();
    status.updated_at = checked_at_now();
}

fn mark_restarting(status: &mut AppUpdateInstallStatus, target_version: &str) {
    status.phase = AppUpdateInstallPhase::Restarting;
    status.target_version = Some(target_version.to_string());
    status.message = "Installing update, the app will restart automatically...".to_string();
    status.updated_at = checked_at_now();
}

fn mark_restarting_global(target_version: &str) {
    mutate_install_status(|status| mark_restarting(status, target_version));
}

fn mark_failed(message: String) {
    mutate_install_status(|status| {
        status.phase = AppUpdateInstallPhase::Failed;
        status.message = message;
        status.updated_at = checked_at_now();
    });
}

fn is_active_install_phase(phase: &AppUpdateInstallPhase) -> bool {
    matches!(
        phase,
        AppUpdateInstallPhase::Checking
            | AppUpdateInstallPhase::Downloading
            | AppUpdateInstallPhase::Installing
            | AppUpdateInstallPhase::Restarting
    )
}

#[cfg(debug_assertions)]
fn update_e2e_download_only() -> bool {
    std::env::var_os("CODEXHUB_UPDATE_E2E_SKIP_INSTALL")
        .filter(|value| !value.is_empty())
        .is_some()
}

#[cfg(not(debug_assertions))]
fn update_e2e_download_only() -> bool {
    false
}

fn pending_update_path() -> Result<PathBuf, String> {
    Ok(runtime_paths::codex_home_dir()?
        .join("proxy")
        .join("app-update-pending.json"))
}

fn write_pending_update(path: &Path, target_version: &str) -> Result<(), String> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)
            .map_err(|error| operation_error("write pending update", error))?;
    }
    let pending = PendingUpdate {
        target_version: target_version.to_string(),
        written_at: checked_at_now(),
    };
    let body = serde_json::to_string_pretty(&pending)
        .map_err(|error| operation_error("write pending update", error))?;
    safe_file::write_text_atomic(path, &body)
        .map_err(|error| operation_error("write pending update", error))
}

fn read_pending_update(path: &Path) -> Result<Option<PendingUpdate>, String> {
    if !path.exists() {
        return Ok(None);
    }
    let body = fs::read(path).map_err(|error| operation_error("read pending update", error))?;
    serde_json::from_slice(&body)
        .map(Some)
        .map_err(|error| operation_error("read pending update", error))
}

fn consume_pending_update_completion(
    path: &Path,
    current_version: &str,
) -> Result<Option<AppUpdateCompletionStatus>, String> {
    let Some(pending) = read_pending_update(path)? else {
        return Ok(None);
    };
    clear_pending_update(path)?;
    Ok(Some(AppUpdateCompletionStatus {
        completed: version_reaches_target(current_version, &pending.target_version),
        current_version: current_version.to_string(),
        target_version: pending.target_version,
    }))
}

fn clear_pending_update(path: &Path) -> Result<(), String> {
    match fs::remove_file(path) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(operation_error("clear pending update", error)),
    }
}

fn version_reaches_target(current_version: &str, target_version: &str) -> bool {
    if current_version == target_version {
        return true;
    }
    match (
        parse_semver_triplet(current_version),
        parse_semver_triplet(target_version),
    ) {
        (Some(current), Some(target)) => current >= target,
        _ => false,
    }
}

fn parse_semver_triplet(version: &str) -> Option<(u64, u64, u64)> {
    let normalized = version
        .trim()
        .trim_start_matches('v')
        .split(['-', '+'])
        .next()?;
    let mut parts = normalized.split('.');
    let major = parts.next()?.parse().ok()?;
    let minor = parts.next()?.parse().ok()?;
    let patch = parts.next()?.parse().ok()?;
    if parts.next().is_some() {
        return None;
    }
    Some((major, minor, patch))
}

fn operation_error(action: &str, error: impl std::fmt::Display) -> String {
    format!("Failed to {action}: {error}")
}

fn updater_setup_error(action: &str, error: UpdaterError) -> String {
    updater_not_configured_message(&error)
        .map(str::to_string)
        .unwrap_or_else(|| operation_error(action, error))
}

fn updater_not_configured_message(error: &UpdaterError) -> Option<&'static str> {
    matches!(error, UpdaterError::EmptyEndpoints)
        .then_some("App updates are not configured in this build.")
}

fn checked_at_now() -> String {
    let seconds = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs())
        .unwrap_or_default();
    format!("unix:{seconds}")
}

fn restart_after_update(app: AppHandle) -> ! {
    crate::run_app_lifecycle_action(
        crate::AppLifecycleAction::UpdateRestart,
        crate::proxy::stop_session_owned_for_terminal_exit,
        || app.restart(),
    )
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
    fn release_not_found_from_update_feed_counts_as_no_update() {
        assert_eq!(
            status_from_update_check("0.1.0", "unix:789", Err(UpdaterError::ReleaseNotFound)),
            Ok(no_update_status("0.1.0", "unix:789")),
        );
    }

    #[test]
    fn other_update_check_errors_still_surface() {
        let error =
            status_from_update_check("0.1.0", "unix:789", Err(UpdaterError::UnsupportedArch))
                .expect_err("unsupported arch should remain an updater error");

        assert!(
            error.starts_with("Failed to check for updates: Unsupported application architecture")
        );
    }

    #[test]
    fn normal_manifest_keeps_flavorless_latest_json_backward_compatibility() {
        let manifest = flavor_manifest(
            None,
            "0.1.5",
            "https://github.com/NOirBRight/CodexHub/releases/download/v0.1.5/CodexHub_0.1.5_x64-setup.exe",
        );

        assert_eq!(
            validate_flavor_manifest(&manifest, BuildFlavor::Normal),
            Ok(())
        );
    }

    #[test]
    fn debug_manifest_requires_matching_flavor_metadata_and_artifact_name() {
        let manifest = flavor_manifest(
            Some("debug"),
            "0.1.5",
            "https://github.com/NOirBRight/CodexHub/releases/download/v0.1.5/CodexHub_0.1.5_debug_x64-setup.exe",
        );

        assert_eq!(
            validate_flavor_manifest(&manifest, BuildFlavor::Debug),
            Ok(())
        );
    }

    #[test]
    fn updater_rejects_cross_flavor_manifest_before_download() {
        let manifest = flavor_manifest(
            Some("normal"),
            "0.1.5",
            "https://github.com/NOirBRight/CodexHub/releases/download/v0.1.5/CodexHub_0.1.5_x64-setup.exe",
        );

        let error = validate_flavor_manifest(&manifest, BuildFlavor::Debug)
            .expect_err("debug must reject a normal manifest");

        assert!(error.contains("manifest declares normal"));
    }

    #[test]
    fn updater_rejects_mismatched_artifact_even_when_flavor_metadata_matches() {
        let manifest = flavor_manifest(
            Some("debug"),
            "0.1.5",
            "https://github.com/NOirBRight/CodexHub/releases/download/v0.1.5/CodexHub_0.1.5_x64-setup.exe",
        );

        let error = validate_flavor_manifest(&manifest, BuildFlavor::Debug)
            .expect_err("debug must reject a normal installer artifact");

        assert!(error.contains("expected artifact CodexHub_0.1.5_debug_x64-setup.exe"));
    }

    #[test]
    fn updater_binds_the_checked_manifest_to_the_selected_download_artifact() {
        let manifest = flavor_manifest(
            Some("debug"),
            "0.1.5",
            "https://github.com/NOirBRight/CodexHub/releases/download/v0.1.5/CodexHub_0.1.5_debug_x64-setup.exe",
        );

        let error = validate_checked_update_payload(
            &manifest,
            "0.1.5",
            "https://github.com/NOirBRight/CodexHub/releases/download/v0.1.5/CodexHub_0.1.5_x64-setup.exe",
            BuildFlavor::Debug,
        )
        .expect_err("debug must reject a checked update that selects the normal artifact");

        assert!(error.contains("updater selected"));
    }

    #[test]
    fn normal_checked_update_keeps_flavorless_latest_json_compatibility() {
        let manifest = flavor_manifest(
            None,
            "0.1.5",
            "https://github.com/NOirBRight/CodexHub/releases/download/v0.1.5/CodexHub_0.1.5_x64-setup.exe",
        );

        assert_eq!(
            validate_checked_update_payload(
                &manifest,
                "0.1.5",
                "https://github.com/NOirBRight/CodexHub/releases/download/v0.1.5/CodexHub_0.1.5_x64-setup.exe",
                BuildFlavor::Normal,
            ),
            Ok(())
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
    fn updater_not_configured_message_maps_empty_endpoints() {
        assert_eq!(
            updater_not_configured_message(&UpdaterError::EmptyEndpoints),
            Some("App updates are not configured in this build."),
        );
    }

    #[test]
    fn updater_not_configured_message_ignores_other_errors() {
        assert_eq!(
            updater_not_configured_message(&UpdaterError::UnsupportedArch),
            None,
        );
    }

    #[test]
    fn download_chunks_accumulate_into_install_status() {
        let mut status = install_status_idle("0.1.0");

        record_download_chunk(&mut status, "0.1.1", 512, Some(2048));
        record_download_chunk(&mut status, "0.1.1", 256, Some(2048));

        assert_eq!(status.phase, AppUpdateInstallPhase::Downloading);
        assert_eq!(status.current_version, "0.1.0");
        assert_eq!(status.target_version.as_deref(), Some("0.1.1"));
        assert_eq!(status.downloaded_bytes, 768);
        assert_eq!(status.total_bytes, Some(2048));
    }

    #[test]
    fn download_finish_moves_through_installing_and_restarting() {
        let mut status = install_status_idle("0.1.0");

        mark_installing(&mut status, "0.1.1");
        assert_eq!(status.phase, AppUpdateInstallPhase::Installing);
        assert_eq!(status.target_version.as_deref(), Some("0.1.1"));

        mark_restarting(&mut status, "0.1.1");
        assert_eq!(status.phase, AppUpdateInstallPhase::Restarting);
        assert_eq!(status.target_version.as_deref(), Some("0.1.1"));
    }

    #[test]
    fn pending_update_version_is_consumed_after_successful_restart() {
        let path = unique_pending_update_path("success");

        write_pending_update(&path, "0.1.1").expect("write pending update");
        assert_eq!(
            read_pending_update(&path)
                .expect("read pending update")
                .as_ref()
                .map(|pending| pending.target_version.as_str()),
            Some("0.1.1"),
        );

        let completion =
            consume_pending_update_completion(&path, "0.1.1").expect("consume pending update");

        assert_eq!(
            completion,
            Some(AppUpdateCompletionStatus {
                completed: true,
                current_version: "0.1.1".to_string(),
                target_version: "0.1.1".to_string(),
            }),
        );
        assert!(!path.exists());
    }

    #[test]
    fn pending_update_mismatch_is_consumed_without_success() {
        let path = unique_pending_update_path("mismatch");

        write_pending_update(&path, "0.1.1").expect("write pending update");
        let completion =
            consume_pending_update_completion(&path, "0.1.0").expect("consume pending update");

        assert_eq!(
            completion,
            Some(AppUpdateCompletionStatus {
                completed: false,
                current_version: "0.1.0".to_string(),
                target_version: "0.1.1".to_string(),
            }),
        );
        assert!(!path.exists());
    }

    #[test]
    fn pending_update_write_recovers_stale_atomic_lock() {
        let path = unique_pending_update_path("stale-lock");
        let lock = stale_lock_path(&path);
        let _dead_child = write_dead_legacy_lock(&lock);

        write_pending_update(&path, "0.1.1").expect("write pending update");

        assert_eq!(fs::read_to_string(&lock).expect("lock text"), "codexhub-atomic-lock=1\n");
        assert_eq!(
            read_pending_update(&path)
                .expect("read pending update")
                .as_ref()
                .map(|pending| pending.target_version.as_str()),
            Some("0.1.1"),
        );
    }

    #[test]
    fn checked_at_now_is_unix_timestamp_string() {
        assert!(checked_at_now().starts_with("unix:"));
    }

    fn flavor_manifest(flavor: Option<&str>, version: &str, url: &str) -> String {
        let mut manifest = serde_json::json!({
            "version": version,
            "platforms": {
                "windows-x86_64": {
                    "signature": "signed-value",
                    "url": url,
                }
            }
        });
        if let Some(flavor) = flavor {
            manifest["codexhub_flavor"] = serde_json::Value::String(flavor.to_string());
        }
        serde_json::to_string(&manifest).expect("serialize flavor manifest")
    }

    use crate::lock_test_fixtures::write_dead_legacy_lock;

    fn stale_lock_path(path: &std::path::Path) -> std::path::PathBuf {
        path.with_file_name(format!(
            "{}.lock",
            path.file_name()
                .and_then(|name| name.to_str())
                .unwrap_or("pending-update")
        ))
    }

    fn unique_pending_update_path(name: &str) -> std::path::PathBuf {
        let unique = format!(
            "codexhub-pending-update-{name}-{}-{}.json",
            std::process::id(),
            checked_at_now().replace(':', "_"),
        );
        std::env::temp_dir().join(unique)
    }
}
