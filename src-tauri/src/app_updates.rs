use serde::{Deserialize, Serialize};
use std::time::{SystemTime, UNIX_EPOCH};
use tauri::AppHandle;
use tauri_plugin_updater::{Error as UpdaterError, UpdaterExt};

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
    let update = app
        .updater()
        .map_err(|error| updater_setup_error("check for updates", error))?
        .check()
        .await
        .map_err(|error| operation_error("check for updates", error))?;

    Ok(match update {
        Some(update) => update_status(
            current,
            UpdateCandidate {
                version: update.version.clone(),
                notes: update.body.clone(),
                date: update.date.as_ref().map(ToString::to_string),
            },
            checked_at,
        ),
        None => no_update_status(current, checked_at),
    })
}

#[tauri::command]
#[allow(unreachable_code, unused_variables)]
pub async fn install_app_update(app: AppHandle) -> Result<AppUpdateInstallResult, String> {
    let current = current_version(&app);
    let Some(update) = app
        .updater()
        .map_err(|error| updater_setup_error("install update", error))?
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
    let message = format!("CodexHub {version} installed. Restarting...");
    update
        .download_and_install(|_chunk_length, _content_length| {}, || {})
        .await
        .map_err(|error| operation_error("install update", error))?;
    app.restart();
    Ok(AppUpdateInstallResult {
        installed: true,
        version,
        message,
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
