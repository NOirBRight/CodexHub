//! Read-only official CLI status. Settings and projection stay shared.
use crate::{runtime_paths, Model};
use serde_json::Value;
use std::io::Read;
use std::process::{Command, Stdio};
use std::sync::mpsc;
use std::time::Duration;

pub fn is_provider(id: &str) -> bool {
    matches!(id, "cursor-subscription" | "claude-subscription")
}

pub fn status(provider_id: &str) -> Result<Value, String> {
    if !is_provider(provider_id) {
        return Err("Unknown CLI subscription provider".into());
    }
    let root = runtime_paths::resource_root()?;
    let python = runtime_paths::find_python(Some(&root))?;
    let mut command = runtime_paths::configured_python_command(&python);
    command
        .arg(root.join("src-python/cli_subscription_status.py"))
        .arg(provider_id);
    run_status(command, Duration::from_secs(180))
}

fn run_status(mut command: Command, timeout: Duration) -> Result<Value, String> {
    command
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null());
    runtime_paths::configure_no_window(&mut command);
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        command.process_group(0);
    }
    let mut child = command
        .spawn()
        .map_err(|_| "Unable to start CLI account detection")?;
    #[cfg(windows)]
    let job = match crate::app_server::AppServerJob::assign_to(&child) {
        Ok(job) => job,
        Err(error) => {
            let _ = child.kill();
            let _ = child.wait();
            return Err(error);
        }
    };
    let stdout = child.stdout.take().expect("piped stdout");
    let (sender, receiver) = mpsc::channel();
    let reader = std::thread::spawn(move || {
        let mut bytes = Vec::new();
        let result = stdout
            .take(4 * 1024 * 1024 + 1)
            .read_to_end(&mut bytes)
            .map_err(|_| "Cannot read CLI account detection".to_string())
            .and_then(|_| {
                if bytes.len() > 4 * 1024 * 1024 {
                    Err("CLI account detection exceeds size limit".into())
                } else {
                    Ok(bytes)
                }
            });
        let _ = sender.send(result);
    });
    let result = receiver
        .recv_timeout(timeout)
        .map_err(|_| "CLI account detection timed out".to_string())
        .and_then(|result| result)
        .and_then(|bytes| {
            serde_json::from_slice::<Value>(&bytes)
                .map_err(|_| "Invalid CLI account detection".to_string())
        });
    #[cfg(unix)]
    unsafe {
        libc::kill(-(child.id() as libc::pid_t), libc::SIGKILL);
    }
    #[cfg(windows)]
    drop(job);
    let _ = child.kill();
    let _ = child.wait();
    let _ = reader.join();
    result
}

pub fn discover_models(provider_id: &str) -> Result<Vec<Model>, String> {
    models_from_status(&status(provider_id)?)
}

pub fn models_from_status(status: &Value) -> Result<Vec<Model>, String> {
    if status.get("state").and_then(Value::as_str) != Some("available") {
        let state = status
            .get("state")
            .and_then(Value::as_str)
            .unwrap_or("discovery-failed");
        return Err(format!(
            "Official CLI account: {state}. Model listing does not prove generation permission."
        ));
    }
    let rows = status
        .get("models")
        .and_then(Value::as_array)
        .ok_or("Invalid CLI model list")?;
    rows.iter()
        .map(|row| {
            let id = row
                .get("id")
                .and_then(Value::as_str)
                .filter(|id| !id.is_empty())
                .ok_or("Invalid CLI model identity")?;
            Ok(Model {
                id: id.into(),
                upstream_model: Some(id.into()),
                display_name: row
                    .get("display_name")
                    .and_then(Value::as_str)
                    .map(str::to_string),
                input_modalities: Some(vec!["text".into()]),
                supported_reasoning_levels: Some(Vec::new()),
                capabilities_edited: true,
                ..Model::default()
            })
        })
        .collect()
}

pub fn validate_settings(providers: &[crate::Provider]) -> Result<(), String> {
    for provider in providers.iter().filter(|p| is_provider(&p.id)) {
        if provider.enabled
            && provider.id == "claude-subscription"
            && provider.system_context_consent.as_deref() != Some("user-context-v1")
        {
            return Err("Before enabling Claude, accept that caller system instructions travel in CLI user context without native system-message priority.".into());
        }
        if provider
            .api_key
            .as_deref()
            .is_some_and(|key| !key.trim().is_empty())
        {
            return Err("CLI subscription providers use the current official CLI account; remove the API key.".into());
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn enabling_claude_requires_exact_explicit_consent() {
        let mut provider: crate::Provider = serde_json::from_value(serde_json::json!({
            "id": "claude-subscription", "name": "Claude", "base_url": "", "enabled": true
        }))
        .unwrap();
        assert!(validate_settings(&[provider.clone()]).is_err());
        provider.system_context_consent = Some("generic-enable-toggle".into());
        assert!(validate_settings(&[provider.clone()]).is_err());
        provider.system_context_consent = Some("user-context-v1".into());
        assert!(validate_settings(&[provider.clone()]).is_ok());
        provider.api_key = Some("an-api-key".into());
        assert!(validate_settings(&[provider]).is_err());
    }
    #[test]
    fn every_vendor_identity_survives_without_invented_limits() {
        let rows: Vec<_> = (0..246).map(|n| serde_json::json!({"id": format!("exact/{n}-high-fast"), "display_name": format!("Model {n}")})).collect();
        let models =
            models_from_status(&serde_json::json!({"state": "available", "models": rows})).unwrap();
        assert_eq!(models.len(), 246);
        assert_eq!(
            models[245].upstream_model.as_deref(),
            Some("exact/245-high-fast")
        );
        assert!(models
            .iter()
            .all(|m| m.context_window.is_none() && m.max_output_tokens.is_none()));
        assert!(models_from_status(
            &serde_json::json!({"state":"account-changed", "models": rows})
        )
        .is_err());
    }
}
