use crate::{config, runtime_paths};
use serde_json::Value;
use std::path::PathBuf;
use std::process::Command;
use std::time::Duration;

const SCRIPT_NAME: &str = "xai_device_login.py";

#[tauri::command]
pub fn xai_auth_status() -> Result<Value, String> {
    run_xai_cli(&["status"], None)
}

#[tauri::command]
pub fn xai_start_device_login() -> Result<Value, String> {
    run_xai_cli(&["start-device"], Some(Duration::from_secs(30)))
}

#[tauri::command]
pub fn xai_poll_device_login(device_json: String) -> Result<Value, String> {
    run_xai_cli(
        &["poll-device", "--device-json", &device_json],
        Some(Duration::from_secs(620)),
    )
}

#[tauri::command]
pub fn xai_logout() -> Result<Value, String> {
    run_xai_cli(&["logout"], None)
}

fn run_xai_cli(args: &[&str], timeout: Option<Duration>) -> Result<Value, String> {
    let python = config::find_python()?;
    let script = xai_script_path()?;
    let mut command = runtime_paths::configured_python_command(&python);
    command.arg(&script);
    command.args(args);
    if let Ok(home) = runtime_paths::runtime_home_dir() {
        command.env("CODEX_HOME", home);
    }
    if let Some(limit) = timeout {
        command.env("CODEXHUB_XAI_CLI_TIMEOUT_SECONDS", limit.as_secs().to_string());
    }
    let output = command
        .output()
        .map_err(|error| format!("failed to start xAI auth helper: {error}"))?;
    let stdout = String::from_utf8_lossy(&output.stdout);
    let stderr = String::from_utf8_lossy(&output.stderr);
    if !output.status.success() {
        if let Ok(payload) = serde_json::from_str::<Value>(stdout.trim()) {
            let message = payload
                .get("error")
                .and_then(Value::as_str)
                .unwrap_or("xAI auth helper failed");
            return Err(message.to_string());
        }
        return Err(format!(
            "xAI auth helper failed\nstdout:\n{}\nstderr:\n{}",
            stdout.trim_end(),
            stderr.trim_end()
        ));
    }
    serde_json::from_str(stdout.trim()).map_err(|error| {
        format!("xAI auth helper returned invalid JSON: {error}\nstdout:\n{}", stdout.trim_end())
    })
}

fn xai_script_path() -> Result<PathBuf, String> {
    let root = runtime_paths::resource_root()?;
    let script = root.join("scripts").join(SCRIPT_NAME);
    if !script.exists() {
        return Err(format!("xAI auth helper not found: {}", script.display()));
    }
    Ok(script)
}
