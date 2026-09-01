use super::super::CodexAuthStatus;
use serde_json::Value;
use std::fs;
use std::path::PathBuf;

pub(in crate::gateway) fn read_codex_auth_status() -> CodexAuthStatus {
    let path = codex_home().join("auth.json");
    if !path.exists() {
        return CodexAuthStatus {
            auth_file_present: false,
            logged_in: false,
            auth_mode: None,
            account_id_present: false,
            access_token_present: false,
            refresh_token_present: false,
            token_refresh_status: "missing".to_string(),
            last_refresh: None,
            issue: Some(
                "Codex auth file is missing; log in with Codex CLI or Codex App first.".to_string(),
            ),
        };
    }

    let text = match fs::read_to_string(&path) {
        Ok(text) => text,
        Err(error) => {
            return CodexAuthStatus {
                auth_file_present: true,
                logged_in: false,
                auth_mode: None,
                account_id_present: false,
                access_token_present: false,
                refresh_token_present: false,
                token_refresh_status: "read_error".to_string(),
                last_refresh: None,
                issue: Some(format!("Codex auth file could not be read: {error}")),
            }
        }
    };
    let data: Value = match serde_json::from_str(&text) {
        Ok(data) => data,
        Err(error) => {
            return CodexAuthStatus {
                auth_file_present: true,
                logged_in: false,
                auth_mode: None,
                account_id_present: false,
                access_token_present: false,
                refresh_token_present: false,
                token_refresh_status: "invalid_json".to_string(),
                last_refresh: None,
                issue: Some(format!("Codex auth file is invalid JSON: {error}")),
            }
        }
    };

    let auth_mode = data
        .get("auth_mode")
        .and_then(Value::as_str)
        .map(ToOwned::to_owned);
    let tokens = data.get("tokens").and_then(Value::as_object);
    let access_token_present = tokens
        .and_then(|value| value.get("access_token"))
        .and_then(Value::as_str)
        .map(|value| !value.is_empty())
        .unwrap_or(false);
    let refresh_token_present = tokens
        .and_then(|value| value.get("refresh_token"))
        .and_then(Value::as_str)
        .map(|value| !value.is_empty())
        .unwrap_or(false);
    let account_id_present = tokens
        .and_then(|value| value.get("account_id"))
        .and_then(Value::as_str)
        .map(|value| !value.is_empty())
        .unwrap_or(false);
    let last_refresh = data
        .get("last_refresh")
        .and_then(Value::as_str)
        .map(ToOwned::to_owned);
    let logged_in = auth_mode.as_deref() == Some("chatgpt") && access_token_present;
    let token_refresh_status = if last_refresh.is_some() {
        "last_refresh_recorded"
    } else if refresh_token_present {
        "refresh_token_available"
    } else {
        "unknown"
    }
    .to_string();
    let issue = if auth_mode.as_deref() != Some("chatgpt") {
        Some(
            "Codex auth mode is not chatgpt; Gateway requires local Codex/ChatGPT auth."
                .to_string(),
        )
    } else if !access_token_present {
        Some("Codex auth file has no access token.".to_string())
    } else if !account_id_present {
        Some("Codex auth exists, but account id is missing.".to_string())
    } else {
        None
    };

    CodexAuthStatus {
        auth_file_present: true,
        logged_in,
        auth_mode,
        account_id_present,
        access_token_present,
        refresh_token_present,
        token_refresh_status,
        last_refresh,
        issue,
    }
}

pub(in crate::gateway) fn codex_home() -> PathBuf {
    crate::runtime_paths::codex_target_home_dir().unwrap_or_else(|_| PathBuf::from(".codex"))
}

pub(in crate::gateway) fn isolated_preview_text() -> String {
    String::new()
}

pub(in crate::gateway) fn isolated_apply_unsupported<T>() -> Result<T, String> {
    Err("codex apply must be invoked through config::apply_codex_config_isolated".to_string())
}
