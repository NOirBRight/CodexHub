use crate::{config, runtime_paths};
use serde_json::Value;
use std::path::PathBuf;
use std::process::Command;
use std::time::Duration;

const SCRIPT_NAME: &str = "xai_device_login.py";
const XAI_ROOT_HOST: &str = "x.ai";
const XAI_HOST_SUFFIX: &str = ".x.ai";

pub fn xai_auth_status_blocking() -> Result<Value, String> {
    run_xai_cli(&["status"], None)
}

pub fn xai_start_device_login_blocking() -> Result<Value, String> {
    run_xai_cli(&["start-device"], Some(Duration::from_secs(30)))
}

pub fn xai_poll_device_login_blocking(device_json: String) -> Result<Value, String> {
    run_xai_cli(
        &["poll-device", "--device-json", &device_json],
        Some(Duration::from_secs(620)),
    )
}

pub fn xai_logout_blocking() -> Result<Value, String> {
    run_xai_cli(&["logout"], None)
}

pub fn xai_usage_snapshot_blocking() -> Result<Value, String> {
    run_xai_cli(&["usage"], Some(Duration::from_secs(20)))
}

pub fn xai_access_token_blocking() -> Result<String, String> {
    access_token_from_cli_payload(&run_xai_cli(
        &["access-token"],
        Some(Duration::from_secs(30)),
    )?)
}

pub(crate) fn access_token_from_cli_payload(value: &Value) -> Result<String, String> {
    value
        .get("access_token")
        .and_then(Value::as_str)
        .map(str::trim)
        .filter(|token| !token.is_empty())
        .map(ToOwned::to_owned)
        .ok_or_else(|| "xAI session has no access_token".to_string())
}

#[tauri::command]
pub async fn xai_auth_status() -> Result<Value, String> {
    spawn_xai_cli(xai_auth_status_blocking).await
}

#[tauri::command]
pub async fn xai_start_device_login() -> Result<Value, String> {
    spawn_xai_cli(xai_start_device_login_blocking).await
}

#[tauri::command]
pub async fn xai_poll_device_login(device_json: String) -> Result<Value, String> {
    spawn_xai_cli(move || xai_poll_device_login_blocking(device_json)).await
}

#[tauri::command]
pub async fn xai_logout() -> Result<Value, String> {
    spawn_xai_cli(xai_logout_blocking).await
}

#[tauri::command]
pub async fn xai_usage_snapshot() -> Result<Value, String> {
    spawn_xai_cli(xai_usage_snapshot_blocking).await
}

pub fn xai_open_verification_url_blocking(url: String) -> Result<String, String> {
    let pinned = pin_https_xai_url(&url)?;
    spawn_system_browser(&pinned)?;
    Ok(format!("Opened {pinned}"))
}

#[tauri::command]
pub async fn xai_open_verification_url(url: String) -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(move || xai_open_verification_url_blocking(url))
        .await
        .map_err(|error| format!("xAI verification open task failed: {error}"))?
}

pub(crate) fn pin_https_xai_url(url: &str) -> Result<String, String> {
    let parsed =
        reqwest::Url::parse(url.trim()).map_err(|error| format!("invalid URL: {error}"))?;
    if parsed.scheme() != "https" {
        return Err("xAI verification URL must be HTTPS".to_string());
    }
    if !parsed.username().is_empty() || parsed.password().is_some() {
        return Err("xAI verification URL must not contain credentials".to_string());
    }
    let host = parsed
        .host_str()
        .ok_or_else(|| "xAI verification URL host is missing".to_string())?
        .to_ascii_lowercase();
    if host != XAI_ROOT_HOST && !host.ends_with(XAI_HOST_SUFFIX) {
        return Err(format!(
            "xAI verification URL host is not pinned to x.ai: {host}"
        ));
    }
    Ok(parsed.as_str().to_string())
}

fn spawn_system_browser(url: &str) -> Result<(), String> {
    wait_for_browser_launcher(system_browser_command(url), Duration::from_secs(10))
}

fn wait_for_browser_launcher(mut command: Command, timeout: Duration) -> Result<(), String> {
    use std::process::Stdio;
    command
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null());
    let mut child = command
        .spawn()
        .map_err(|error| format!("failed to open xAI verification page: {error}"))?;
    let deadline = std::time::Instant::now() + timeout;
    loop {
        match child.try_wait() {
            Ok(Some(status)) if status.success() => return Ok(()),
            Ok(Some(status)) => {
                return Err(format!(
                    "browser launcher failed ({status}); check your default browser"
                ))
            }
            Ok(None) if std::time::Instant::now() < deadline => {
                std::thread::sleep(Duration::from_millis(25));
            }
            result => {
                let _ = child.kill();
                let _ = child.wait();
                return Err(match result {
                    Err(error) => format!("failed to check browser launcher: {error}"),
                    _ => "browser launcher did not finish within the time limit; check your browser window".to_string(),
                });
            }
        }
    }
}

#[cfg(all(not(target_os = "windows"), not(target_os = "macos")))]
fn linux_browser_launcher(omarchy_root: Option<&std::ffi::OsStr>) -> std::ffi::OsString {
    omarchy_root
        .map(|root| PathBuf::from(root).join("bin/omarchy-launch-browser"))
        .filter(|launcher| launcher.is_file())
        .map(|launcher| launcher.into_os_string())
        .unwrap_or_else(|| "xdg-open".into())
}

fn system_browser_command(url: &str) -> Command {
    #[cfg(target_os = "windows")]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x08000000;
        let escaped = url.replace('\'', "''");
        let mut command = Command::new("powershell");
        command.args([
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            &format!("Start-Process '{escaped}'"),
        ]);
        command.creation_flags(CREATE_NO_WINDOW);
        command
    }
    #[cfg(target_os = "macos")]
    {
        let mut command = Command::new("open");
        command.arg(url);
        command
    }
    #[cfg(all(not(target_os = "windows"), not(target_os = "macos")))]
    {
        // Omarchy opens the default browser and focuses its existing workspace.
        let mut command = Command::new(linux_browser_launcher(
            std::env::var_os("OMARCHY_PATH").as_deref(),
        ));
        command.arg(url);
        command
    }
}

async fn spawn_xai_cli<F>(task: F) -> Result<Value, String>
where
    F: FnOnce() -> Result<Value, String> + Send + 'static,
{
    tauri::async_runtime::spawn_blocking(task)
        .await
        .map_err(|error| format!("xAI auth helper task failed: {error}"))?
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
        command.env(
            "CODEXHUB_XAI_CLI_TIMEOUT_SECONDS",
            limit.as_secs().to_string(),
        );
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
            redact_xai_cli_output(stdout.trim_end()),
            stderr.trim_end()
        ));
    }
    serde_json::from_str(stdout.trim()).map_err(|error| {
        format!(
            "xAI auth helper returned invalid JSON: {error}\nstdout:\n{}",
            redact_xai_cli_output(stdout.trim_end())
        )
    })
}

fn redact_xai_cli_output(text: &str) -> String {
    if let Ok(mut payload) = serde_json::from_str::<Value>(text) {
        if let Some(object) = payload.as_object_mut() {
            if object.contains_key("access_token") {
                object.insert(
                    "access_token".to_string(),
                    Value::String("<redacted>".to_string()),
                );
                return payload.to_string();
            }
        }
    }
    if text.contains("access_token") {
        return "<redacted>".to_string();
    }
    text.to_string()
}

fn xai_script_path() -> Result<PathBuf, String> {
    let root = runtime_paths::resource_root()?;
    let script = root.join("scripts").join(SCRIPT_NAME);
    if !script.exists() {
        return Err(format!("xAI auth helper not found: {}", script.display()));
    }
    Ok(script)
}

#[cfg(test)]
mod tests {
    use super::{access_token_from_cli_payload, pin_https_xai_url, system_browser_command};
    use serde_json::json;

    #[test]
    fn access_token_from_cli_payload_reads_non_empty_token() {
        assert_eq!(
            access_token_from_cli_payload(&json!({"access_token": " xai-live "})).expect("token"),
            "xai-live"
        );
        access_token_from_cli_payload(&json!({"ok": true})).expect_err("missing token");
    }

    #[test]
    fn pin_https_xai_url_accepts_auth_host_and_query() {
        let pinned =
            pin_https_xai_url("https://auth.x.ai/device?user_code=ABCD-EFGH").expect("pinned");
        assert!(pinned.starts_with("https://auth.x.ai/device"));
        assert!(pinned.contains("user_code=ABCD-EFGH"));
    }

    #[test]
    fn pin_https_xai_url_rejects_non_https_and_foreign_hosts() {
        for url in [
            "http://auth.x.ai/device",
            "javascript:alert(1)",
            "file:///etc/passwd",
            "https://evil.example/device",
            "https://auth.x.ai.evil.com/device",
            "https://user:pass@auth.x.ai/device",
        ] {
            pin_https_xai_url(url).expect_err(url);
        }
    }

    #[test]
    #[cfg(target_os = "linux")]
    fn omarchy_browser_launch_uses_the_desktop_focus_adapter() {
        let root = std::env::temp_dir().join(format!("codexhub-browser-{}", std::process::id()));
        std::fs::create_dir_all(root.join("bin")).unwrap();
        let launcher = root.join("bin/omarchy-launch-browser");
        std::fs::write(&launcher, "fixture").unwrap();
        assert_eq!(
            super::linux_browser_launcher(Some(root.as_os_str())),
            launcher.as_os_str()
        );
        std::fs::remove_dir_all(&root).unwrap();
        assert_eq!(
            super::linux_browser_launcher(Some(root.as_os_str())),
            "xdg-open"
        );
        assert_eq!(super::linux_browser_launcher(None), "xdg-open");
    }

    #[test]
    fn browser_launch_reports_nonzero_exit_instead_of_success() {
        #[cfg(target_os = "windows")]
        let mut command = std::process::Command::new("cmd");
        #[cfg(target_os = "windows")]
        command.args(["/C", "exit 7"]);
        #[cfg(not(target_os = "windows"))]
        let mut command = std::process::Command::new("sh");
        #[cfg(not(target_os = "windows"))]
        command.args(["-c", "exit 7"]);
        let error = super::wait_for_browser_launcher(command, std::time::Duration::from_secs(2))
            .unwrap_err();
        assert!(error.contains("browser launcher failed"), "{error}");
    }

    #[test]
    #[cfg(target_os = "linux")]
    fn hung_browser_launcher_is_bounded_and_reaped() {
        let mut command = std::process::Command::new("sh");
        command.args(["-c", "exec sleep 5"]);
        let started = std::time::Instant::now();
        let error = super::wait_for_browser_launcher(command, std::time::Duration::from_millis(50))
            .unwrap_err();
        assert!(error.contains("time limit"), "{error}");
        assert!(started.elapsed() < std::time::Duration::from_secs(2));
    }

    #[test]
    fn system_browser_command_targets_the_os_opener() {
        let url = "https://auth.x.ai/device?user_code=ABCD-EFGH";
        let command = system_browser_command(url);
        let program = command.get_program().to_string_lossy().into_owned();
        let args: Vec<String> = command
            .get_args()
            .map(|arg| arg.to_string_lossy().into_owned())
            .collect();
        #[cfg(target_os = "windows")]
        {
            assert_eq!(program, "powershell");
            assert_eq!(
                args,
                [
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    "Start-Process 'https://auth.x.ai/device?user_code=ABCD-EFGH'",
                ]
            );
        }
        #[cfg(target_os = "macos")]
        {
            assert_eq!(program, "open");
            assert_eq!(args, [url]);
        }
        #[cfg(all(not(target_os = "windows"), not(target_os = "macos")))]
        {
            assert_eq!(
                program,
                super::linux_browser_launcher(std::env::var_os("OMARCHY_PATH").as_deref())
                    .to_string_lossy()
            );
            assert_eq!(args, [url]);
        }
    }
}
