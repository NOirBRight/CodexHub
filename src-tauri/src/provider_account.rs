//! Account actions and read-only quota adapters. Credentials stay in the backend.
use crate::app_server::{AppServerCall, AppServerPoll, AppServerSession};
use serde_json::{json, Value};
use std::io::Read;
use std::time::{Duration, Instant};

pub fn codex_logout_blocking() -> Result<(), String> {
    let mut command = crate::codex_cli::command()?;
    command.args(["app-server", "--stdio"]);
    logout_with_command(command)
}

fn logout_with_command(command: std::process::Command) -> Result<(), String> {
    let mut session = AppServerSession::start(command, "Codex sign out")?;
    session.initialize()?;
    session.send_calls(
        &[AppServerCall {
            id: json!(2),
            method: "account/logout",
            params: None,
        }],
        "Codex sign out",
    )?;
    let deadline = Instant::now() + Duration::from_secs(15);
    loop {
        let remaining = deadline.saturating_duration_since(Instant::now());
        if remaining.is_zero() {
            return Err("Codex sign out timed out; refresh account status before retrying".into());
        }
        match session.poll_message(remaining)? {
            AppServerPoll::Message(value) if value.get("id") == Some(&json!(2)) => {
                if value.get("error").is_some() {
                    return Err("Codex rejected the sign-out request".into());
                }
                if value.get("result").is_some() {
                    return Ok(());
                }
                return Err("Codex returned an invalid sign-out response".into());
            }
            AppServerPoll::Closed => return Err("Codex closed before confirming sign out".into()),
            _ => {}
        }
    }
}

#[tauri::command]
pub async fn codex_logout() -> Result<(), String> {
    crate::desktop_commands::run_blocking("Codex sign out", codex_logout_blocking).await
}

fn get_json(client: &reqwest::blocking::Client, url: &str, key: &str) -> Result<Value, String> {
    let mut response = client
        .get(url)
        .bearer_auth(key)
        .send()
        .map_err(|_| "Quota service could not be reached".to_string())?;
    if !response.status().is_success() {
        return Err(format!(
            "Quota service returned HTTP {}",
            response.status().as_u16()
        ));
    }
    // Do not expose remote response bodies or secrets through IPC errors.
    let mut bytes = Vec::new();
    std::io::Read::take(&mut response, 1_048_577)
        .read_to_end(&mut bytes)
        .map_err(|_| "Could not read quota response".to_string())?;
    if bytes.len() > 1_048_576 {
        return Err("Quota response was too large".into());
    }
    serde_json::from_slice(&bytes).map_err(|_| "Quota service returned invalid JSON".into())
}

pub fn provider_usage_blocking(provider_id: String) -> Result<Value, String> {
    let base = match provider_id.as_str() {
        "opencode-go" => "https://opencode.ai/zen/go/v1",
        "commandcode" => "https://api.commandcode.ai/provider/v1",
        _ => return Err("Provider has no quota adapter".into()),
    };
    let provider = crate::config::get_providers()?
        .into_iter()
        .find(|p| p.id == provider_id)
        .ok_or("Provider was not found")?;
    // A catalog ID with a custom endpoint must never forward that endpoint's key.
    if provider.base_url.trim_end_matches('/') != base {
        return Err("Quota requires the official provider endpoint".into());
    }
    let key = crate::models::resolve_provider_discovery_api_key(
        provider.api_key.as_deref().unwrap_or(""),
        None,
    )?;
    if key.is_empty() {
        return Err("Configure an API key to query quota".into());
    }
    let client = reqwest::blocking::Client::builder()
        .timeout(Duration::from_secs(15))
        .redirect(reqwest::redirect::Policy::none())
        .build()
        .map_err(|_| "Could not create quota client")?;
    if provider_id == "opencode-go" {
        return normalize_opencode(&get_json(
            &client,
            "https://opencode.ai/zen/go/v1/usage",
            &key,
        )?);
    }
    let identity = get_json(
        &client,
        "https://api.commandcode.ai/alpha/whoami?limits=1",
        &key,
    )?;
    let mut url = reqwest::Url::parse("https://api.commandcode.ai/alpha/billing/credits")
        .expect("constant URL");
    if let Some(org) = identity.pointer("/org/id").and_then(Value::as_str) {
        url.query_pairs_mut().append_pair("orgId", org);
    }
    normalize_commandcode(&get_json(&client, url.as_str(), &key)?)
}

#[tauri::command]
pub async fn provider_usage(provider_id: String) -> Result<Value, String> {
    crate::desktop_commands::run_blocking("Provider quota", move || {
        provider_usage_blocking(provider_id)
    })
    .await
}

pub fn normalize_opencode(value: &Value) -> Result<Value, String> {
    let mut limits = Vec::new();
    for (key, period, name) in [
        ("rolling", "5h", "5 hours"),
        ("weekly", "week", "Weekly"),
        ("monthly", "month", "Monthly"),
    ] {
        if let Some(window) = value.get("usage").and_then(|v| v.get(key)) {
            if let Some(used) = window
                .get("percent")
                .and_then(Value::as_f64)
                .filter(|v| v.is_finite() && *v >= 0.0)
            {
                limits.push(json!({"key": key, "period": period, "name": name, "limit":100.0,"used":used,"resets_at":window.get("resetsAt")}));
            }
        }
    }
    if limits.is_empty() {
        return Err("Quota response has no recognized usage windows".into());
    }
    Ok(json!({"limits":limits}))
}

pub fn normalize_commandcode(value: &Value) -> Result<Value, String> {
    let mut limits = Vec::new();
    for (key, period, name) in [("fiveHour", "5h", "5 hours"), ("weekly", "week", "Weekly")] {
        if let Some(window) = value.get("windowLimits").and_then(|v| v.get(key)) {
            if let (Some(used), Some(cap)) = (
                window.get("used").and_then(Value::as_f64),
                window.get("cap").and_then(Value::as_f64),
            ) {
                if used >= 0.0 && cap > 0.0 {
                    let reset = window
                        .get("resetAt")
                        .filter(|v| {
                            !v.is_null() && v.as_f64() != Some(0.0) && v.as_str() != Some("0")
                        })
                        .map(|v| {
                            v.as_str()
                                .map(str::to_owned)
                                .unwrap_or_else(|| v.to_string())
                        });
                    limits.push(json!({"key":key,"period":period,"name":name,"used":used,"limit":cap,"resets_at":reset}));
                }
            }
        }
    }
    let credits = value.get("credits");
    let amounts: Option<Vec<f64>> = ["monthlyCredits", "purchasedCredits", "freeCredits"]
        .iter()
        .map(|key| {
            credits
                .and_then(|v| v.get(*key))
                .and_then(Value::as_f64)
                .filter(|v| *v >= 0.0)
        })
        .collect();
    let balance = amounts.map(|v| v.iter().sum::<f64>());
    if limits.is_empty() && balance.is_none() {
        return Err("Quota response has no recognized credits or windows".into());
    }
    Ok(json!({"limits":limits,"balance":balance,"currency":"USD"}))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[cfg(unix)]
    #[test]
    fn logout_requires_acknowledgement_and_redacts_remote_errors() {
        for (response, ok) in [
            (r#"{"id":2,"result":{}}"#, true),
            (r#"{"id":2,"error":{"message":"private-detail"}}"#, false),
        ] {
            let mut command = std::process::Command::new("sh");
            command.args(["-c", "read -r init; read -r notification; read -r request; case \"$request\" in *account/logout*) printf '%s\\n' \"$1\";; *) exit 1;; esac", "fixture", response]);
            let result = logout_with_command(command);
            assert_eq!(result.is_ok(), ok);
            if let Err(error) = result {
                assert!(!error.contains("private-detail"));
            }
        }
    }
    #[test]
    fn quota_windows_keep_all_periods_and_reset_dates() {
        let value = normalize_opencode(&json!({"usage":{"rolling":{"percent":25,"resetsAt":"2026-09-08T00:00:00Z"},"weekly":{"percent":120},"monthly":{"percent":0}}})).unwrap();
        assert_eq!(value["limits"].as_array().unwrap().len(), 3);
        assert_eq!(value["limits"][1]["used"], 120.0);
        assert_eq!(value["limits"][0]["resets_at"], "2026-09-08T00:00:00Z");
        assert!(normalize_opencode(&json!({"usage":{}})).is_err());
    }
    #[test]
    fn credits_and_week_only_are_independent() {
        let value=normalize_commandcode(&json!({"credits":{"monthlyCredits":2,"purchasedCredits":3,"freeCredits":0},"windowLimits":{"weekly":{"used":8,"cap":10,"resetAt":1789000000}}})).unwrap();
        assert_eq!(value["balance"], 5.0);
        assert_eq!(value["limits"][0]["period"], "week");
        assert_eq!(value["limits"][0]["resets_at"], "1789000000");
        assert!(normalize_commandcode(&json!({"credits":{}})).is_err());
    }
}
