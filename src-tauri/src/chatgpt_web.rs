//! Desktop commands for the ChatGPT Web Runtime supervisor.
//!
//! The pinned upstream `setup` entry rewrites Codex integration. These
//! commands only talk to the CodexHub supervisor.

use crate::{config, runtime_paths};
use serde_json::Value;
use std::path::PathBuf;
use std::process::Command;

const SCRIPT_NAME: &str = "chatgpt_web_runtime.py";
const PIN_NAME: &str = "chatgpt_web_runtime_pin.json";

pub fn chatgpt_web_status_blocking() -> Result<Value, String> {
    run_cli(&["status"])
}

pub fn chatgpt_web_enable_blocking() -> Result<Value, String> {
    run_cli(&["install"])?;
    run_cli(&["start"])
}

pub fn chatgpt_web_stop_blocking() -> Result<Value, String> {
    run_cli(&["stop"])
}

pub fn chatgpt_web_disable_blocking() -> Result<Value, String> {
    run_cli(&["disable"])
}

pub fn chatgpt_web_upgrade_blocking() -> Result<Value, String> {
    run_cli(&["upgrade"])
}

pub fn chatgpt_web_delete_account_blocking() -> Result<Value, String> {
    run_cli(&["delete-account"])
}

pub fn chatgpt_web_open_login_blocking() -> Result<Value, String> {
    run_cli(&["open-login"])
}

pub fn chatgpt_web_close_login_blocking() -> Result<Value, String> {
    run_cli(&["close-login"])
}

#[tauri::command]
pub async fn chatgpt_web_status() -> Result<Value, String> {
    spawn_cli(chatgpt_web_status_blocking).await
}

#[tauri::command]
pub async fn chatgpt_web_enable() -> Result<Value, String> {
    spawn_cli(chatgpt_web_enable_blocking).await
}

#[tauri::command]
pub async fn chatgpt_web_stop() -> Result<Value, String> {
    spawn_cli(chatgpt_web_stop_blocking).await
}

#[tauri::command]
pub async fn chatgpt_web_disable() -> Result<Value, String> {
    spawn_cli(chatgpt_web_disable_blocking).await
}

#[tauri::command]
pub async fn chatgpt_web_upgrade() -> Result<Value, String> {
    spawn_cli(chatgpt_web_upgrade_blocking).await
}

#[tauri::command]
pub async fn chatgpt_web_delete_account() -> Result<Value, String> {
    spawn_cli(chatgpt_web_delete_account_blocking).await
}

#[tauri::command]
pub async fn chatgpt_web_open_login() -> Result<Value, String> {
    spawn_cli(chatgpt_web_open_login_blocking).await
}

#[tauri::command]
pub async fn chatgpt_web_close_login() -> Result<Value, String> {
    spawn_cli(chatgpt_web_close_login_blocking).await
}

async fn spawn_cli<F>(task: F) -> Result<Value, String>
where
    F: FnOnce() -> Result<Value, String> + Send + 'static,
{
    tauri::async_runtime::spawn_blocking(task)
        .await
        .map_err(|error| format!("ChatGPT Web Runtime task failed: {error}"))?
}

fn private_home() -> Result<PathBuf, String> {
    if let Some(value) = std::env::var_os("CODEXHUB_CHATGPT_WEB_HOME") {
        if !value.is_empty() {
            return Ok(PathBuf::from(value));
        }
    }
    let home = dirs::home_dir().ok_or_else(|| "failed to resolve user home directory".to_string())?;
    Ok(home.join(".codexhub").join("chatgpt-web"))
}

fn pin_path() -> Result<PathBuf, String> {
    if let Some(value) = std::env::var_os("CODEXHUB_CHATGPT_WEB_PIN") {
        if !value.is_empty() {
            return Ok(PathBuf::from(value));
        }
    }
    Ok(runtime_paths::resource_root()?.join("config").join(PIN_NAME))
}

fn script_path() -> Result<PathBuf, String> {
    let script = runtime_paths::resource_root()?
        .join("src-python")
        .join(SCRIPT_NAME);
    if !script.is_file() {
        return Err(format!(
            "ChatGPT Web Runtime supervisor not found: {}",
            script.display()
        ));
    }
    Ok(script)
}

fn run_cli(args: &[&str]) -> Result<Value, String> {
    let python = config::find_python()?;
    let script = script_path()?;
    let home = private_home()?;
    let pin = pin_path()?;
    let mut command = runtime_paths::configured_python_command(&python);
    command.arg(&script);
    command.args(args);
    command.arg("--home");
    command.arg(&home);
    command.env("CODEXHUB_CHATGPT_WEB_HOME", &home);
    command.env("CODEXHUB_CHATGPT_WEB_PIN", &pin);
    let output = command
        .output()
        .map_err(|error| format!("failed to start ChatGPT Web Runtime supervisor: {error}"))?;
    let stdout = redact_secrets(&String::from_utf8_lossy(&output.stdout));
    let stderr = redact_secrets(&String::from_utf8_lossy(&output.stderr));
    if !output.status.success() {
        if let Ok(payload) = serde_json::from_str::<Value>(stdout.trim()) {
            let message = payload
                .get("error")
                .and_then(Value::as_str)
                .unwrap_or("ChatGPT Web Runtime supervisor failed");
            return Err(redact_secrets(message));
        }
        return Err(format!(
            "ChatGPT Web Runtime supervisor failed\nstdout:\n{}\nstderr:\n{}",
            stdout.trim_end(),
            stderr.trim_end()
        ));
    }
    serde_json::from_str(stdout.trim()).map_err(|error| {
        format!(
            "ChatGPT Web Runtime supervisor returned invalid JSON: {error}\nstdout:\n{}",
            stdout.trim_end()
        )
    })
}

pub(crate) fn redact_secrets(text: &str) -> String {
    let mut output = String::with_capacity(text.len());
    let mut rest = text;
    while let Some(found) = rest.find("sk-") {
        output.push_str(&rest[..found]);
        let tail = &rest[found + 3..];
        let token_len = tail
            .chars()
            .take_while(|character| character.is_ascii_alphanumeric() || *character == '_' || *character == '-')
            .map(char::len_utf8)
            .sum::<usize>();
        if token_len > 4 {
            output.push_str("[redacted]");
            rest = &tail[token_len..];
        } else {
            output.push_str("sk-");
            rest = tail;
        }
    }
    output.push_str(rest);
    output
}

#[cfg(test)]
mod tests {
    use super::redact_secrets;
    use std::path::PathBuf;

    #[test]
    fn command_errors_redact_secret_looking_tokens() {
        let text = redact_secrets("failed sk-chatgpt-web-test-secret-DO-NOT-LOG now");
        assert!(!text.contains("sk-chatgpt-web-test-secret-DO-NOT-LOG"));
        assert!(text.contains("[redacted]"));
    }

    #[test]
    fn bundled_pin_does_not_fetch_latest() {
        let path = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .expect("repo root")
            .join("config")
            .join("chatgpt_web_runtime_pin.json");
        let text = std::fs::read_to_string(&path).expect("read pin");
        assert!(text.contains("a13cd09950969f43e3b7e25c71fa43efaf5446c5"));
        assert!(text.contains("\"version\": \"6.1.1\""));
        assert!(!text.contains("/releases/latest/"));
        assert!(text.contains("\"setup\""));
        assert!(text.contains("\"dev\""));
        assert!(text.contains("--replace-codex-route"));
    }
}
