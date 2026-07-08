use serde::{Deserialize, Serialize};
use std::time::{SystemTime, UNIX_EPOCH};
use tauri::AppHandle;
use tauri_plugin_updater::{Error as UpdaterError, Updater, UpdaterExt};

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
    let checked_at = checked_at_now();
    let update = updater(&app, "check for updates")?
        .check()
        .await
        .map(|update| {
            update.map(|update| UpdateCandidate {
                version: update.version.clone(),
                notes: update.body.clone(),
                date: update.date.as_ref().map(ToString::to_string),
            })
        });

    if update_feed_is_missing(&update).await {
        return Ok(no_update_status(current, checked_at));
    }

    status_from_update_check(current, checked_at, update)
}

#[tauri::command]
pub async fn install_app_update(app: AppHandle) -> Result<AppUpdateInstallResult, String> {
    let current = current_version(&app);
    let Some(update) = updater(&app, "install update")?
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

    update
        .download_and_install(|_chunk_length, _content_length| {}, || {})
        .await
        .map_err(|error| operation_error("install update", error))?;
    restart_after_update(app)
}

fn current_version(app: &AppHandle) -> String {
    app.package_info().version.to_string()
}

fn version_info(current_version: impl Into<String>) -> AppVersionInfo {
    AppVersionInfo {
        current_version: current_version.into(),
    }
}

#[cfg(debug_assertions)]
fn updater(app: &AppHandle, action: &str) -> Result<Updater, String> {
    let mut builder = app.updater_builder();
    if let Ok(endpoint) = std::env::var("CODEXHUB_UPDATE_E2E_ENDPOINT") {
        let endpoint = endpoint.trim();
        if !endpoint.is_empty() {
            let endpoint = endpoint
                .parse()
                .map_err(|error| operation_error(action, error))?;
            builder = builder
                .endpoints(vec![endpoint])
                .map_err(|error| operation_error(action, error))?;
        }
    }
    builder
        .build()
        .map_err(|error| updater_setup_error(action, error))
}

#[cfg(not(debug_assertions))]
fn updater(app: &AppHandle, action: &str) -> Result<Updater, String> {
    app.updater()
        .map_err(|error| updater_setup_error(action, error))
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

async fn update_feed_is_missing(update: &Result<Option<UpdateCandidate>, UpdaterError>) -> bool {
    let Err(error) = update else {
        return false;
    };
    if matches!(error, UpdaterError::ReleaseNotFound) {
        return true;
    }
    let Some(url) = update_error_url(error) else {
        return false;
    };
    if !is_github_latest_update_manifest_url(&url) {
        return false;
    }
    update_manifest_returns_not_found(&url).await
}

fn update_error_url(error: &UpdaterError) -> Option<String> {
    match error {
        UpdaterError::Reqwest(error) => error.url().map(ToString::to_string),
        _ => None,
    }
}

fn is_github_latest_update_manifest_url(url: &str) -> bool {
    url.starts_with("https://github.com/") && url.contains("/releases/latest/download/latest.json")
}

async fn update_manifest_returns_not_found(url: &str) -> bool {
    let Ok(response) = reqwest::Client::new()
        .get(url)
        .header(reqwest::header::ACCEPT, "application/json")
        .send()
        .await
    else {
        return false;
    };
    response.status() == reqwest::StatusCode::NOT_FOUND
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
    app.restart()
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
    fn missing_feed_probe_is_limited_to_github_latest_update_manifests() {
        assert!(is_github_latest_update_manifest_url(
            "https://github.com/NOirBRight/CodexHub/releases/latest/download/latest.json",
        ));
        assert!(!is_github_latest_update_manifest_url(
            "https://github.com/NOirBRight/CodexHub/releases/download/v0.1.0/latest.json",
        ));
        assert!(!is_github_latest_update_manifest_url(
            "https://example.com/releases/latest/download/latest.json",
        ));
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
    fn checked_at_now_is_unix_timestamp_string() {
        assert!(checked_at_now().starts_with("unix:"));
    }
}
