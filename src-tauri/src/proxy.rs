use crate::AppStatus;
use crate::Settings;
use crate::{runtime_paths, safe_file};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::fs;
use std::io::{self, Read};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

#[cfg(windows)]
use std::os::windows::process::CommandExt;

const HEALTH_TIMEOUT: Duration = Duration::from_millis(800);
const SHUTDOWN_TIMEOUT: Duration = Duration::from_millis(800);
const START_TIMEOUT: Duration = Duration::from_secs(20);
const GRACEFUL_STOP_TIMEOUT: Duration = Duration::from_secs(2);
const KILL_STOP_TIMEOUT: Duration = Duration::from_secs(5);
const PID_FILE_VERSION: u32 = 1;
const START_OUTPUT_CAPTURE_LIMIT: usize = 8 * 1024;
const MANAGED_OVERLAY_MARKER_BEGIN: &str = "# BEGIN CODEX PROXY SESSION CONFIG";
const MANAGED_OVERLAY_MARKER_END: &str = "# END CODEX PROXY SESSION CONFIG";

pub fn status() -> Result<AppStatus, String> {
    status_with_paths(&ProxyPaths::runtime()?)
}

pub fn start() -> Result<AppStatus, String> {
    start_with_paths(&ProxyPaths::runtime()?)
}

pub fn stop() -> Result<AppStatus, String> {
    stop_with_paths(&ProxyPaths::runtime()?)
}

pub fn restart() -> Result<AppStatus, String> {
    stop()?;
    start()
}

#[derive(Debug, Clone)]
struct ProxyPaths {
    codex_dir: PathBuf,
    codex_target_dir: PathBuf,
    repo_root: PathBuf,
}

impl ProxyPaths {
    fn runtime() -> Result<Self, String> {
        let codex_dir = runtime_paths::runtime_home_dir()?;
        let codex_target_dir = runtime_paths::codex_target_home_dir()?;
        let repo_root = runtime_paths::resource_root()?;

        Ok(Self::new_isolated(codex_dir, codex_target_dir, repo_root))
    }

    #[cfg(test)]
    fn new(codex_dir: impl Into<PathBuf>, repo_root: impl Into<PathBuf>) -> Self {
        let codex_dir = codex_dir.into();
        Self {
            codex_target_dir: codex_dir.clone(),
            codex_dir,
            repo_root: repo_root.into(),
        }
    }

    fn new_isolated(
        runtime_dir: impl Into<PathBuf>,
        codex_target_dir: impl Into<PathBuf>,
        repo_root: impl Into<PathBuf>,
    ) -> Self {
        Self {
            codex_dir: runtime_dir.into(),
            codex_target_dir: codex_target_dir.into(),
            repo_root: repo_root.into(),
        }
    }

    fn proxy_dir(&self) -> PathBuf {
        self.codex_dir.join("proxy")
    }

    fn settings_path(&self) -> PathBuf {
        self.proxy_dir().join("settings.json")
    }

    fn pid_path(&self) -> PathBuf {
        self.proxy_dir().join("proxy.pid")
    }

    fn codex_config_path(&self) -> PathBuf {
        self.codex_target_dir.join("config.toml")
    }

    fn proxy_script_path(&self) -> PathBuf {
        self.repo_root.join("src-python").join("codex_proxy.py")
    }

    fn proxy_script_dir(&self) -> PathBuf {
        self.repo_root.join("src-python")
    }
}

#[derive(Debug, Deserialize)]
struct SettingsDocument {
    locale: Option<String>,
    auto_sync_history: Option<bool>,
    unified_codex_history: Option<bool>,
    auto_start_software: Option<bool>,
    auto_start_gateway: Option<bool>,
    auto_start_proxy: Option<bool>,
    include_official_models: Option<bool>,
    auto_sync_catalog: Option<bool>,
    auto_sync_clients: Option<bool>,
    default_codex_route: Option<String>,
    gateway_bind_address: Option<String>,
    gateway_client_key: Option<String>,
    gateway_enable_models: Option<bool>,
    gateway_enable_responses: Option<bool>,
    gateway_enable_chat_completions: Option<bool>,
    gateway_request_timeout_seconds: Option<u32>,
    gateway_auto_retry_enabled: Option<bool>,
    gateway_auto_retry_max_attempts: Option<u32>,
    gateway_image_proxy_enabled: Option<bool>,
    gateway_image_proxy_model: Option<String>,
    openai_context_guard_enabled: Option<bool>,
    gateway_fast_model_variants: Option<Vec<String>>,
    official_disabled_models: Option<Vec<String>>,
    official_model_sort_order: Option<Vec<String>>,
    official_provider_sort_order: Option<i32>,
    proxy_port: Option<u16>,
}

impl SettingsDocument {
    fn into_settings(self) -> Settings {
        let defaults = Settings::default();
        Settings {
            locale: self.locale.unwrap_or_default(),
            auto_sync_history: self.auto_sync_history.unwrap_or(defaults.auto_sync_history),
            unified_codex_history: self
                .unified_codex_history
                .unwrap_or(defaults.unified_codex_history),
            auto_start_software: self
                .auto_start_software
                .or(self.auto_start_proxy)
                .unwrap_or(defaults.auto_start_software),
            auto_start_gateway: self
                .auto_start_gateway
                .unwrap_or(defaults.auto_start_gateway),
            include_official_models: self
                .include_official_models
                .unwrap_or(defaults.include_official_models),
            auto_sync_catalog: self.auto_sync_catalog.unwrap_or(defaults.auto_sync_catalog),
            auto_sync_clients: self
                .auto_sync_clients
                .or(self.auto_sync_catalog)
                .unwrap_or(defaults.auto_sync_clients),
            default_codex_route: self
                .default_codex_route
                .filter(|value| matches!(value.as_str(), "official" | "hub"))
                .unwrap_or(defaults.default_codex_route),
            gateway_bind_address: self
                .gateway_bind_address
                .filter(|value| value == "127.0.0.1")
                .unwrap_or(defaults.gateway_bind_address),
            gateway_client_key: self
                .gateway_client_key
                .filter(|value| !value.trim().is_empty())
                .unwrap_or(defaults.gateway_client_key),
            gateway_enable_models: self
                .gateway_enable_models
                .unwrap_or(defaults.gateway_enable_models),
            gateway_enable_responses: self
                .gateway_enable_responses
                .unwrap_or(defaults.gateway_enable_responses),
            gateway_enable_chat_completions: self
                .gateway_enable_chat_completions
                .unwrap_or(defaults.gateway_enable_chat_completions),
            gateway_request_timeout_seconds: self
                .gateway_request_timeout_seconds
                .map(|value| value.clamp(5, 600))
                .unwrap_or(defaults.gateway_request_timeout_seconds),
            gateway_auto_retry_enabled: self
                .gateway_auto_retry_enabled
                .unwrap_or(defaults.gateway_auto_retry_enabled),
            gateway_auto_retry_max_attempts: self
                .gateway_auto_retry_max_attempts
                .map(sanitize_gateway_auto_retry_max_attempts)
                .unwrap_or(defaults.gateway_auto_retry_max_attempts),
            gateway_image_proxy_enabled: self
                .gateway_image_proxy_enabled
                .unwrap_or(defaults.gateway_image_proxy_enabled),
            gateway_image_proxy_model: self
                .gateway_image_proxy_model
                .map(|value| value.trim().to_string())
                .filter(|value| !value.is_empty())
                .unwrap_or(defaults.gateway_image_proxy_model),
            openai_context_guard_enabled: self
                .openai_context_guard_enabled
                .unwrap_or(defaults.openai_context_guard_enabled),
            gateway_fast_model_variants: self
                .gateway_fast_model_variants
                .unwrap_or(defaults.gateway_fast_model_variants),
            official_disabled_models: self
                .official_disabled_models
                .unwrap_or(defaults.official_disabled_models),
            official_model_sort_order: self
                .official_model_sort_order
                .unwrap_or(defaults.official_model_sort_order),
            official_provider_sort_order: self
                .official_provider_sort_order
                .unwrap_or(defaults.official_provider_sort_order),
            proxy_port: self.proxy_port.unwrap_or(defaults.proxy_port),
        }
    }
}

fn sanitize_gateway_auto_retry_max_attempts(value: u32) -> u8 {
    value.clamp(1, 30) as u8
}

#[derive(Debug, Deserialize)]
struct HealthResponse {
    ok: Option<bool>,
    build: Option<String>,
}

impl HealthResponse {
    fn is_running(&self) -> bool {
        self.ok.unwrap_or(false)
    }
}

#[derive(Debug, Clone, Deserialize, Serialize, PartialEq, Eq)]
struct ProxyPidMetadata {
    version: u32,
    pid: u32,
    port: u16,
    script_path: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    script_sha256: Option<String>,
    started_at_unix_ms: u64,
}

impl ProxyPidMetadata {
    fn new(pid: u32, port: u16, script: &Path) -> Self {
        Self {
            version: PID_FILE_VERSION,
            pid,
            port,
            script_path: comparable_path(script),
            script_sha256: file_sha256(script),
            started_at_unix_ms: SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap_or_default()
                .as_millis()
                .try_into()
                .unwrap_or(u64::MAX),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
enum ProxyPidRecord {
    Managed(ProxyPidMetadata),
    Legacy(u32),
}

impl ProxyPidRecord {
    fn pid(&self) -> u32 {
        match self {
            Self::Managed(metadata) => metadata.pid,
            Self::Legacy(pid) => *pid,
        }
    }

    fn expected_port(&self, settings_port: u16) -> u16 {
        match self {
            Self::Managed(metadata) => metadata.port,
            Self::Legacy(_) => settings_port,
        }
    }

    fn expected_script_path(&self) -> Option<&str> {
        match self {
            Self::Managed(metadata) => Some(metadata.script_path.as_str()),
            Self::Legacy(_) => None,
        }
    }
}

fn status_with_paths(paths: &ProxyPaths) -> Result<AppStatus, String> {
    let settings = read_settings(paths)?;
    let mode = read_mode(paths)?;
    let health = health(settings.proxy_port)?;
    let proxy_running = health
        .as_ref()
        .map(HealthResponse::is_running)
        .unwrap_or(false);
    let proxy_build = health.and_then(|response| {
        if response.is_running() {
            response.build
        } else {
            None
        }
    });

    Ok(AppStatus {
        mode,
        proxy_running,
        proxy_port: settings.proxy_port,
        proxy_build,
        message: if proxy_running {
            "Proxy is healthy".to_string()
        } else {
            "Proxy is not running".to_string()
        },
        history_sync_status: None,
        history_sync_message: None,
    })
}

fn start_with_paths(paths: &ProxyPaths) -> Result<AppStatus, String> {
    replace_managed_proxy_from_previous_bundle(paths)?;
    start_with_paths_and_timeout(paths, START_TIMEOUT)
}

fn replace_managed_proxy_from_previous_bundle(paths: &ProxyPaths) -> Result<(), String> {
    let Some(ProxyPidRecord::Managed(metadata)) = read_pid_record(paths)? else {
        return Ok(());
    };
    let current_script = paths.proxy_script_path();
    let path_matches = normalized_path_text(&metadata.script_path)
        == normalized_path_text(&current_script.to_string_lossy());
    let fingerprint_matches = metadata.script_sha256.is_some()
        && metadata.script_sha256 == file_sha256(&current_script);
    if path_matches && fingerprint_matches {
        return Ok(());
    }

    let status = stop_with_paths(paths)?;
    if status.proxy_running {
        return Err(format!(
            "previous Gateway bundle is still running on port {}; stop it before starting {}",
            status.proxy_port,
            current_script.display()
        ));
    }
    Ok(())
}

fn file_sha256(path: &Path) -> Option<String> {
    let bytes = fs::read(path).ok()?;
    let mut hasher = Sha256::new();
    hasher.update(bytes);
    Some(format!("{:x}", hasher.finalize()))
}

fn start_with_paths_and_timeout(
    paths: &ProxyPaths,
    timeout: Duration,
) -> Result<AppStatus, String> {
    start_with_paths_and_timing(paths, timeout, Duration::from_millis(200), &health)
}

fn start_with_paths_and_timing(
    paths: &ProxyPaths,
    timeout: Duration,
    poll_interval: Duration,
    health_probe: &dyn Fn(u16) -> Result<Option<HealthResponse>, String>,
) -> Result<AppStatus, String> {
    start_with_paths_and_waiter(
        paths,
        timeout,
        poll_interval,
        health_probe,
        |child, port, timeout, poll_interval, health_probe, _output_capture| {
            wait_for_startup_health(child, port, timeout, poll_interval, health_probe)
        },
    )
}

fn start_with_paths_and_waiter<F>(
    paths: &ProxyPaths,
    timeout: Duration,
    poll_interval: Duration,
    health_probe: &dyn Fn(u16) -> Result<Option<HealthResponse>, String>,
    wait_for_startup: F,
) -> Result<AppStatus, String>
where
    F: FnOnce(
        &mut Child,
        u16,
        Duration,
        Duration,
        &dyn Fn(u16) -> Result<Option<HealthResponse>, String>,
        &StartupOutputCapture,
    ) -> Result<StartupOutcome, String>,
{
    let settings = read_settings(paths)?;
    let mode = read_mode(paths)?;
    if let Some(response) = health_probe(settings.proxy_port)? {
        if response.is_running() {
            return Ok(AppStatus {
                mode,
                proxy_running: true,
                proxy_port: settings.proxy_port,
                proxy_build: response.build,
                message: "Proxy is already running".to_string(),
                history_sync_status: None,
                history_sync_message: None,
            });
        }
    }

    let script = paths.proxy_script_path();
    if !script.exists() {
        return Err(format!("proxy script not found: {}", script.display()));
    }

    fs::create_dir_all(paths.proxy_dir()).map_err(|error| {
        format!(
            "failed to create proxy runtime directory {}: {error}",
            paths.proxy_dir().display()
        )
    })?;

    remove_pid(paths)?;
    let python = find_python(paths);
    let mut command = build_start_command(&python, &script, paths, &settings);

    let mut child = command.spawn().map_err(|error| {
        format!(
            "failed to start proxy with {} {}: {error}",
            python.display(),
            script.display()
        )
    })?;
    let output_capture = capture_child_stdio(&mut child);
    let pid = child.id();
    if let Err(error) = write_pid(paths, pid, settings.proxy_port, &script) {
        let _ = terminate_child(&mut child);
        return Err(error);
    }

    match wait_for_startup(
        &mut child,
        settings.proxy_port,
        timeout,
        poll_interval,
        health_probe,
        &output_capture,
    )? {
        StartupOutcome::Healthy(response) => Ok(AppStatus {
            mode,
            proxy_running: true,
            proxy_port: settings.proxy_port,
            proxy_build: response.build,
            message: format!("Proxy started with PID {pid}"),
            history_sync_status: None,
            history_sync_message: None,
        }),
        StartupOutcome::Exited(status) => {
            let _ = remove_pid(paths);
            let output = output_capture.finish();
            Err(format_startup_failure(
                format!("proxy process exited before health became ready with status {status:?}"),
                &output,
            ))
        }
        StartupOutcome::TimedOut => {
            let _ = terminate_child(&mut child);
            let _ = remove_pid(paths);
            let output = output_capture.finish();
            Err(format_startup_failure(
                format!(
                    "proxy did not become healthy within {} seconds at http://127.0.0.1:{}/health",
                    timeout.as_secs(),
                    settings.proxy_port
                ),
                &output,
            ))
        }
    }
}

fn stop_with_paths(paths: &ProxyPaths) -> Result<AppStatus, String> {
    stop_with_paths_and_controls(paths, &SystemProcessKiller, &SystemProcessInspector)
}

fn stop_with_paths_and_controls(
    paths: &ProxyPaths,
    killer: &dyn ProcessKiller,
    inspector: &dyn ProcessInspector,
) -> Result<AppStatus, String> {
    let settings = read_settings(paths)?;
    let mode = read_mode(paths)?;
    let pid_record = read_pid_record(paths)?;
    let was_running = health(settings.proxy_port)?
        .as_ref()
        .map(HealthResponse::is_running)
        .unwrap_or(false);

    if !was_running {
        return stop_when_health_unavailable(
            paths,
            mode,
            settings.proxy_port,
            pid_record,
            killer,
            inspector,
        );
    }

    let Some(pid_record) = pid_record else {
        return stop_running_proxy_without_pid(
            paths,
            mode,
            settings.proxy_port,
            &settings.gateway_client_key,
        );
    };

    match verify_proxy_process(&pid_record, paths, settings.proxy_port, inspector)? {
        VerifiedProxyProcess::Verified { pid } => pid,
        VerifiedProxyProcess::Missing { pid } => {
            remove_pid(paths)?;
            return stop_running_proxy_with_stale_pid(
                paths,
                mode,
                settings.proxy_port,
                pid,
                &settings.gateway_client_key,
                "PID no longer exists".to_string(),
            );
        }
        VerifiedProxyProcess::Mismatch { pid, reason } => {
            remove_pid(paths)?;
            return stop_running_proxy_with_stale_pid(
                paths,
                mode,
                settings.proxy_port,
                pid,
                &settings.gateway_client_key,
                format!("ownership could not be verified: {reason}"),
            );
        }
    };

    let _ = request_shutdown(settings.proxy_port, &settings.gateway_client_key);
    if wait_for_stopped(settings.proxy_port, GRACEFUL_STOP_TIMEOUT)? {
        remove_pid(paths)?;
        return Ok(AppStatus {
            mode,
            proxy_running: false,
            proxy_port: settings.proxy_port,
            proxy_build: None,
            message: "Proxy stopped gracefully".to_string(),
            history_sync_status: None,
            history_sync_message: None,
        });
    }

    let pid = match verify_proxy_process(&pid_record, paths, settings.proxy_port, inspector)? {
        VerifiedProxyProcess::Verified { pid } => pid,
        VerifiedProxyProcess::Missing { pid } => {
            remove_pid(paths)?;
            return stop_running_proxy_with_stale_pid(
                paths,
                mode,
                settings.proxy_port,
                pid,
                &settings.gateway_client_key,
                "PID disappeared before force kill".to_string(),
            );
        }
        VerifiedProxyProcess::Mismatch { pid, reason } => {
            remove_pid(paths)?;
            return stop_running_proxy_with_stale_pid(
                paths,
                mode,
                settings.proxy_port,
                pid,
                &settings.gateway_client_key,
                format!("ownership could not be verified before force kill: {reason}"),
            );
        }
    };

    killer.kill(pid)?;
    if !wait_for_stopped(settings.proxy_port, KILL_STOP_TIMEOUT)? {
        return Err(format!(
            "sent kill signal to proxy PID {pid}, but health still responds on port {}",
            settings.proxy_port
        ));
    }

    remove_pid(paths)?;
    Ok(AppStatus {
        mode,
        proxy_running: false,
        proxy_port: settings.proxy_port,
        proxy_build: None,
        message: format!("Proxy PID {pid} stopped"),
        history_sync_status: None,
        history_sync_message: None,
    })
}

fn stop_running_proxy_with_stale_pid(
    paths: &ProxyPaths,
    mode: String,
    port: u16,
    pid: u32,
    gateway_client_key: &str,
    reason: String,
) -> Result<AppStatus, String> {
    let _ = request_shutdown(port, gateway_client_key);
    if wait_for_stopped(port, GRACEFUL_STOP_TIMEOUT)? {
        return Ok(AppStatus {
            mode,
            proxy_running: false,
            proxy_port: port,
            proxy_build: None,
            message: format!(
                "Proxy stopped gracefully after removing stale proxy PID {pid}: {reason}"
            ),
            history_sync_status: None,
            history_sync_message: None,
        });
    }

    let mut status = status_with_paths(paths)?;
    status.message = format!(
        "Proxy health responds on port {port}, but stale PID {pid} was removed because {reason}; shutdown endpoint did not stop it and no force kill was attempted"
    );
    Ok(status)
}

fn stop_running_proxy_without_pid(
    paths: &ProxyPaths,
    mode: String,
    port: u16,
    gateway_client_key: &str,
) -> Result<AppStatus, String> {
    let _ = request_shutdown(port, gateway_client_key);
    if wait_for_stopped(port, GRACEFUL_STOP_TIMEOUT)? {
        return Ok(AppStatus {
            mode,
            proxy_running: false,
            proxy_port: port,
            proxy_build: None,
            message: "Proxy stopped gracefully without a proxy PID file".to_string(),
            history_sync_status: None,
            history_sync_message: None,
        });
    }

    let mut status = status_with_paths(paths)?;
    status.message = format!(
        "Proxy health responds on port {port}, but no proxy PID file was found; shutdown endpoint did not stop it and no force kill was attempted"
    );
    Ok(status)
}

fn stop_when_health_unavailable(
    paths: &ProxyPaths,
    mode: String,
    port: u16,
    pid_record: Option<ProxyPidRecord>,
    killer: &dyn ProcessKiller,
    inspector: &dyn ProcessInspector,
) -> Result<AppStatus, String> {
    let message = match pid_record {
        Some(record) => match verify_proxy_process(&record, paths, port, inspector)? {
            VerifiedProxyProcess::Verified { pid } => {
                killer.kill(pid)?;
                remove_pid(paths)?;
                format!("Proxy PID {pid} stopped after health endpoint was unavailable")
            }
            VerifiedProxyProcess::Missing { pid } => {
                remove_pid(paths)?;
                format!("Removed stale proxy PID {pid}; health endpoint was unavailable")
            }
            VerifiedProxyProcess::Mismatch { pid, reason } => {
                remove_pid(paths)?;
                format!(
                    "Proxy health endpoint was unavailable, but PID {pid} was not killed because ownership could not be verified: {reason}; removed PID file"
                )
            }
        },
        None => "Proxy is not running".to_string(),
    };

    Ok(AppStatus {
        mode,
        proxy_running: false,
        proxy_port: port,
        proxy_build: None,
        message,
        history_sync_status: None,
        history_sync_message: None,
    })
}

fn build_start_command(
    python: &Path,
    script: &Path,
    paths: &ProxyPaths,
    settings: &Settings,
) -> Command {
    let mut command = Command::new(python);
    command
        .arg(script)
        .arg("--host")
        .arg("127.0.0.1")
        .arg("--port")
        .arg(settings.proxy_port.to_string())
        .current_dir(paths.proxy_script_dir())
        .env("PYTHONPATH", paths.proxy_script_dir())
        .env("CODEX_HOME", paths.codex_dir.clone())
        .env("CODEXHUB_CODEX_TARGET_HOME", paths.codex_target_dir.clone())
        .env(
            "CODEX_PROXY_GATEWAY_CLIENT_KEY",
            settings.gateway_client_key.trim(),
        )
        .env(
            "CODEX_PROXY_UPSTREAM_TIMEOUT_SECONDS",
            settings
                .gateway_request_timeout_seconds
                .clamp(5, 600)
                .to_string(),
        )
        .env(
            "CODEX_PROXY_AUTO_RETRY_ENABLED",
            if settings.gateway_auto_retry_enabled {
                "1"
            } else {
                "0"
            },
        )
        .env(
            "CODEX_PROXY_AUTO_RETRY_MAX_ATTEMPTS",
            settings
                .gateway_auto_retry_max_attempts
                .clamp(1, 30)
                .to_string(),
        )
        .env(
            "CODEX_PROXY_IMAGE_PROXY_ENABLED",
            if settings.gateway_image_proxy_enabled {
                "1"
            } else {
                "0"
            },
        )
        .env(
            "CODEX_PROXY_IMAGE_PROXY_MODEL",
            settings.gateway_image_proxy_model.trim(),
        );
    configure_start_stdio(&mut command);
    configure_detached(&mut command);
    command
}

fn configure_start_stdio(command: &mut Command) {
    command
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
}

fn capture_child_stdio(child: &mut Child) -> StartupOutputCapture {
    drop(child.stdin.take());
    let capture = StartupOutputCapture::default();
    if let Some(stdout) = child.stdout.take() {
        capture.capture_reader(stdout, StartupStream::Stdout);
    }
    if let Some(stderr) = child.stderr.take() {
        capture.capture_reader(stderr, StartupStream::Stderr);
    }
    capture
}

#[derive(Debug)]
enum StartupOutcome {
    Healthy(HealthResponse),
    Exited(std::process::ExitStatus),
    TimedOut,
}

#[derive(Debug, Clone, Copy)]
enum StartupStream {
    Stdout,
    Stderr,
}

#[derive(Debug, Default)]
struct StartupOutputCapture {
    stdout: Arc<Mutex<BoundedOutput>>,
    stderr: Arc<Mutex<BoundedOutput>>,
    handles: Mutex<Vec<thread::JoinHandle<()>>>,
}

impl StartupOutputCapture {
    fn capture_reader<R>(&self, reader: R, stream: StartupStream)
    where
        R: Read + Send + 'static,
    {
        let buffer = match stream {
            StartupStream::Stdout => Arc::clone(&self.stdout),
            StartupStream::Stderr => Arc::clone(&self.stderr),
        };
        let handle = thread::spawn(move || drain_reader_to_buffer(reader, buffer));
        if let Ok(mut handles) = self.handles.lock() {
            handles.push(handle);
        }
    }

    fn finish(self) -> StartupOutputSnapshot {
        if let Ok(mut handles) = self.handles.lock() {
            for handle in handles.drain(..) {
                let _ = handle.join();
            }
        }
        self.snapshot()
    }

    fn snapshot(&self) -> StartupOutputSnapshot {
        StartupOutputSnapshot {
            stdout: self
                .stdout
                .lock()
                .map(|buffer| buffer.text())
                .unwrap_or_default(),
            stderr: self
                .stderr
                .lock()
                .map(|buffer| buffer.text())
                .unwrap_or_default(),
        }
    }
}

#[derive(Debug, Default)]
struct StartupOutputSnapshot {
    stdout: String,
    stderr: String,
}

#[derive(Debug)]
struct BoundedOutput {
    bytes: Vec<u8>,
    truncated: bool,
}

impl Default for BoundedOutput {
    fn default() -> Self {
        Self {
            bytes: Vec::with_capacity(START_OUTPUT_CAPTURE_LIMIT.min(1024)),
            truncated: false,
        }
    }
}

impl BoundedOutput {
    fn append(&mut self, chunk: &[u8]) {
        self.bytes.extend_from_slice(chunk);
        if self.bytes.len() > START_OUTPUT_CAPTURE_LIMIT {
            let excess = self.bytes.len() - START_OUTPUT_CAPTURE_LIMIT;
            self.bytes.drain(..excess);
            self.truncated = true;
        }
    }

    fn text(&self) -> String {
        let text = String::from_utf8_lossy(&self.bytes).trim_end().to_string();
        if self.truncated && !text.is_empty() {
            format!("[truncated]\n{text}")
        } else {
            text
        }
    }
}

fn drain_reader_to_buffer<R>(mut reader: R, buffer: Arc<Mutex<BoundedOutput>>)
where
    R: Read,
{
    let mut chunk = [0u8; 1024];
    loop {
        match reader.read(&mut chunk) {
            Ok(0) => break,
            Ok(count) => {
                if let Ok(mut buffer) = buffer.lock() {
                    buffer.append(&chunk[..count]);
                }
            }
            Err(_) => break,
        }
    }
}

fn terminate_child(child: &mut Child) -> Result<(), String> {
    child
        .kill()
        .map_err(|error| format!("failed to kill proxy child process: {error}"))?;
    let _ = child.wait();
    Ok(())
}

fn format_startup_failure(message: String, output: &StartupOutputSnapshot) -> String {
    let mut details = vec![message];
    if !output.stdout.is_empty() {
        details.push(format!("startup stdout:\n{}", output.stdout));
    }
    if !output.stderr.is_empty() {
        details.push(format!("startup stderr:\n{}", output.stderr));
    }
    details.join("\n")
}

trait ProcessKiller {
    fn kill(&self, pid: u32) -> Result<(), String>;
}

trait ProcessInspector {
    fn inspect(&self, pid: u32) -> Result<InspectedProcess, String>;
}

#[derive(Debug, Clone, PartialEq, Eq)]
enum InspectedProcess {
    Missing,
    Running(ProcessInfo),
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct ProcessInfo {
    command_line: String,
    args: Vec<String>,
}

impl ProcessInfo {
    fn from_command_line(command_line: String) -> Self {
        Self {
            args: split_command_line(&command_line),
            command_line,
        }
    }

    #[cfg(any(test, not(windows)))]
    fn from_args(args: Vec<String>) -> Self {
        Self {
            command_line: args.join(" "),
            args,
        }
    }
}

#[derive(Debug, PartialEq, Eq)]
enum VerifiedProxyProcess {
    Verified { pid: u32 },
    Missing { pid: u32 },
    Mismatch { pid: u32, reason: String },
}

struct SystemProcessKiller;

impl ProcessKiller for SystemProcessKiller {
    fn kill(&self, pid: u32) -> Result<(), String> {
        kill_process(pid)
    }
}

struct SystemProcessInspector;

impl ProcessInspector for SystemProcessInspector {
    fn inspect(&self, pid: u32) -> Result<InspectedProcess, String> {
        inspect_process(pid)
    }
}

fn verify_proxy_process(
    record: &ProxyPidRecord,
    paths: &ProxyPaths,
    settings_port: u16,
    inspector: &dyn ProcessInspector,
) -> Result<VerifiedProxyProcess, String> {
    let pid = record.pid();
    let process = match inspector.inspect(pid) {
        Ok(process) => process,
        Err(error) => {
            return Ok(VerifiedProxyProcess::Mismatch {
                pid,
                reason: format!("process command line inspection failed: {error}"),
            });
        }
    };

    let InspectedProcess::Running(info) = process else {
        return Ok(VerifiedProxyProcess::Missing { pid });
    };

    match verify_proxy_command_line(record, paths, settings_port, &info) {
        Ok(()) => Ok(VerifiedProxyProcess::Verified { pid }),
        Err(reason) => Ok(VerifiedProxyProcess::Mismatch { pid, reason }),
    }
}

fn verify_proxy_command_line(
    record: &ProxyPidRecord,
    paths: &ProxyPaths,
    settings_port: u16,
    info: &ProcessInfo,
) -> Result<(), String> {
    let expected_port = record.expected_port(settings_port);
    if !command_line_has_port(&info.args, expected_port) {
        return Err(format!(
            "command line does not include expected --port {expected_port}"
        ));
    }

    if !command_line_has_script_name(info, "codex_proxy.py") {
        return Err("command line does not include codex_proxy.py".to_string());
    }

    if let Some(expected_script) = record.expected_script_path() {
        if !command_line_has_script_path(info, expected_script) {
            return Err(format!(
                "command line does not include expected script path {}",
                expected_script
            ));
        }
    } else if !command_line_has_script_path(info, &paths.proxy_script_path().to_string_lossy()) {
        return Err(format!(
            "legacy PID command line does not include expected script path {}",
            paths.proxy_script_path().display()
        ));
    }

    Ok(())
}

fn command_line_has_port(args: &[String], expected_port: u16) -> bool {
    let expected = expected_port.to_string();
    args.windows(2)
        .any(|pair| pair[0] == "--port" && pair[1] == expected)
        || args
            .iter()
            .any(|arg| arg.strip_prefix("--port=") == Some(expected.as_str()))
}

fn command_line_has_script_name(info: &ProcessInfo, script_name: &str) -> bool {
    info.args
        .iter()
        .any(|arg| path_file_name_eq(arg, script_name))
        || normalized_command_text(&info.command_line)
            .contains(&normalized_command_text(script_name))
}

fn command_line_has_script_path(info: &ProcessInfo, expected_script: &str) -> bool {
    let expected = normalized_path_text(expected_script);
    info.args
        .iter()
        .any(|arg| normalized_path_text(arg) == expected)
        || normalized_path_text(&info.command_line).contains(&expected)
}

fn path_file_name_eq(arg: &str, expected_name: &str) -> bool {
    Path::new(trim_command_token(arg))
        .file_name()
        .and_then(|value| value.to_str())
        .map(|name| normalized_command_text(name) == normalized_command_text(expected_name))
        .unwrap_or(false)
}

fn trim_command_token(value: &str) -> &str {
    value.trim_matches(|character| character == '"' || character == '\'')
}

fn comparable_path(path: &Path) -> String {
    path.canonicalize()
        .unwrap_or_else(|_| path.to_path_buf())
        .to_string_lossy()
        .to_string()
}

fn normalized_path_text(value: &str) -> String {
    normalized_command_text(&comparable_path(Path::new(trim_command_token(value))))
}

fn normalized_command_text(value: &str) -> String {
    let normalized = value.replace('\\', "/");
    if cfg!(windows) {
        normalized.to_ascii_lowercase()
    } else {
        normalized
    }
}

fn split_command_line(command_line: &str) -> Vec<String> {
    let mut args = Vec::new();
    let mut current = String::new();
    let mut in_quotes = false;

    for character in command_line.chars() {
        match character {
            '"' => in_quotes = !in_quotes,
            character if character.is_whitespace() && !in_quotes => {
                if !current.is_empty() {
                    args.push(std::mem::take(&mut current));
                }
            }
            character => current.push(character),
        }
    }

    if !current.is_empty() {
        args.push(current);
    }

    args
}

fn read_settings(paths: &ProxyPaths) -> Result<Settings, String> {
    let path = paths.settings_path();
    if !path.exists() {
        return Ok(Settings::default());
    }

    let text = fs::read_to_string(&path)
        .map_err(|error| format!("failed to read settings JSON {}: {error}", path.display()))?;
    let document: SettingsDocument = serde_json::from_str(&text)
        .map_err(|error| format!("failed to parse settings JSON {}: {error}", path.display()))?;
    Ok(document.into_settings())
}

fn read_mode(paths: &ProxyPaths) -> Result<String, String> {
    match fs::read_to_string(paths.codex_config_path()) {
        Ok(text) => Ok(detect_mode(&text).to_string()),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok("official".to_string()),
        Err(error) => Err(format!(
            "failed to read Codex config {}: {error}",
            paths.codex_config_path().display()
        )),
    }
}

fn detect_mode(text: &str) -> &'static str {
    if text.contains(MANAGED_OVERLAY_MARKER_BEGIN) && text.contains(MANAGED_OVERLAY_MARKER_END) {
        return "custom";
    }

    if toml_config_uses_proxy(text).unwrap_or_else(|| fallback_config_uses_proxy(text)) {
        "custom"
    } else {
        "official"
    }
}

fn toml_config_uses_proxy(text: &str) -> Option<bool> {
    let document = text.parse::<toml::Value>().ok()?;
    let provider = document
        .get("model_provider")
        .and_then(toml::Value::as_str)
        .unwrap_or_default();

    if document
        .get("model_catalog_json")
        .and_then(toml::Value::as_str)
        .map(config_value_uses_managed_catalog)
        .unwrap_or(false)
    {
        return Some(true);
    }

    let providers = document
        .get("model_providers")
        .and_then(toml::Value::as_table);
    let provider_config = providers.and_then(|table| table.get(provider));
    let base_url = provider_config
        .and_then(|value| value.get("base_url"))
        .and_then(toml::Value::as_str)
        .unwrap_or_default();
    Some(config_value_uses_local_proxy_base_url(base_url))
}

fn fallback_config_uses_proxy(text: &str) -> bool {
    let config_text = text
        .lines()
        .filter(|line| !line.trim_start().starts_with('#'))
        .collect::<Vec<_>>()
        .join("\n");
    let normalized = config_text.to_ascii_lowercase();
    let compact = normalized
        .chars()
        .filter(|character| !character.is_whitespace())
        .collect::<String>();

    [
        "codexhub-model-catalog.json",
        "codex-proxy-official-ollama.json",
        "base_url=\"http://127.0.0.1:",
        "base_url='http://127.0.0.1:",
        "base_url=\"http://localhost:",
        "base_url='http://localhost:",
    ]
    .iter()
    .any(|marker| compact.contains(marker))
}

fn config_value_uses_local_proxy_base_url(value: &str) -> bool {
    value.starts_with("http://127.0.0.1:") || value.starts_with("http://localhost:")
}

fn config_value_uses_managed_catalog(value: &str) -> bool {
    value.contains("codexhub-model-catalog.json")
        || value.contains("codex-proxy-official-ollama.json")
}

fn health(port: u16) -> Result<Option<HealthResponse>, String> {
    let client = reqwest::blocking::Client::builder()
        .timeout(HEALTH_TIMEOUT)
        .build()
        .map_err(|error| format!("failed to build HTTP client: {error}"))?;
    let url = format!("http://127.0.0.1:{port}/health");
    let response = match client.get(url).send() {
        Ok(response) => response,
        Err(_) => return Ok(None),
    };

    if !response.status().is_success() {
        return Ok(None);
    }

    Ok(response.json::<HealthResponse>().ok())
}

fn request_shutdown(port: u16, gateway_client_key: &str) -> Result<(), String> {
    let client = reqwest::blocking::Client::builder()
        .timeout(SHUTDOWN_TIMEOUT)
        .build()
        .map_err(|error| format!("failed to build HTTP client: {error}"))?;
    let url = format!("http://127.0.0.1:{port}/shutdown");
    let mut request = client.post(url);
    let gateway_client_key = gateway_client_key.trim();
    if !gateway_client_key.is_empty() {
        request = request.bearer_auth(gateway_client_key);
    }
    let _ = request.send();
    Ok(())
}

fn wait_for_startup_health(
    child: &mut Child,
    port: u16,
    timeout: Duration,
    poll_interval: Duration,
    health_probe: &dyn Fn(u16) -> Result<Option<HealthResponse>, String>,
) -> Result<StartupOutcome, String> {
    let deadline = Instant::now() + timeout;
    loop {
        if let Some(response) = health_probe(port)? {
            if response.is_running() {
                return Ok(StartupOutcome::Healthy(response));
            }
        }
        if let Some(status) = child
            .try_wait()
            .map_err(|error| format!("failed to poll proxy child process: {error}"))?
        {
            return Ok(StartupOutcome::Exited(status));
        }
        if Instant::now() >= deadline {
            return Ok(StartupOutcome::TimedOut);
        }
        thread::sleep(poll_interval);
    }
}

fn wait_for_stopped(port: u16, timeout: Duration) -> Result<bool, String> {
    let deadline = Instant::now() + timeout;
    loop {
        let running = health(port)?
            .as_ref()
            .map(HealthResponse::is_running)
            .unwrap_or(false);
        if !running {
            return Ok(true);
        }
        if Instant::now() >= deadline {
            return Ok(false);
        }
        thread::sleep(Duration::from_millis(200));
    }
}

#[cfg(test)]
fn read_pid(paths: &ProxyPaths) -> Result<Option<u32>, String> {
    Ok(read_pid_record(paths)?.map(|record| record.pid()))
}

fn read_pid_record(paths: &ProxyPaths) -> Result<Option<ProxyPidRecord>, String> {
    let path = paths.pid_path();
    let text = match fs::read_to_string(&path) {
        Ok(text) => text,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(error) => {
            return Err(format!(
                "failed to read proxy PID file {}: {error}",
                path.display()
            ));
        }
    };

    let trimmed = text.trim();
    if trimmed.is_empty() {
        return Ok(None);
    }

    if trimmed.starts_with('{') {
        let metadata: ProxyPidMetadata = serde_json::from_str(trimmed).map_err(|error| {
            format!("invalid proxy PID metadata in {}: {error}", path.display())
        })?;
        if metadata.version != PID_FILE_VERSION {
            return Err(format!(
                "unsupported proxy PID metadata version {} in {}",
                metadata.version,
                path.display()
            ));
        }
        return Ok(Some(ProxyPidRecord::Managed(metadata)));
    }

    trimmed
        .parse::<u32>()
        .map(|pid| Some(ProxyPidRecord::Legacy(pid)))
        .map_err(|error| format!("invalid proxy PID in {}: {error}", path.display()))
}

fn write_pid(paths: &ProxyPaths, pid: u32, port: u16, script: &Path) -> Result<(), String> {
    fs::create_dir_all(paths.proxy_dir()).map_err(|error| {
        format!(
            "failed to create proxy runtime directory {}: {error}",
            paths.proxy_dir().display()
        )
    })?;
    let metadata = ProxyPidMetadata::new(pid, port, script);
    let text = serde_json::to_string_pretty(&metadata)
        .map_err(|error| format!("failed to encode proxy PID metadata: {error}"))?;
    safe_file::write_text_atomic(&paths.pid_path(), &format!("{text}\n")).map_err(|error| {
        format!(
            "failed to write proxy PID file {}: {error}",
            paths.pid_path().display()
        )
    })
}

fn remove_pid(paths: &ProxyPaths) -> Result<(), String> {
    match fs::remove_file(paths.pid_path()) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(format!(
            "failed to remove proxy PID file {}: {error}",
            paths.pid_path().display()
        )),
    }
}

fn find_python(paths: &ProxyPaths) -> PathBuf {
    for candidate in python_candidates(paths) {
        if candidate.exists() {
            return candidate;
        }
    }

    which::which("python")
        .or_else(|_| which::which("python3"))
        .unwrap_or_else(|_| PathBuf::from("python"))
}

fn python_candidates(paths: &ProxyPaths) -> Vec<PathBuf> {
    let mut candidates = Vec::new();
    candidates.extend(runtime_paths::python_env_candidates());

    #[cfg(windows)]
    {
        candidates.push(
            paths
                .proxy_script_dir()
                .join(".venv")
                .join("Scripts")
                .join("python.exe"),
        );
    }

    #[cfg(not(windows))]
    {
        candidates.push(
            paths
                .proxy_script_dir()
                .join(".venv")
                .join("bin")
                .join("python"),
        );
    }

    candidates.extend(runtime_paths::bundled_python_candidates(&paths.repo_root));
    candidates.extend(runtime_paths::current_exe_python_candidates());

    candidates
}

#[cfg(windows)]
fn configure_detached(command: &mut Command) {
    const DETACHED_PROCESS: u32 = 0x0000_0008;
    const CREATE_NO_WINDOW: u32 = 0x0800_0000;
    command.creation_flags(DETACHED_PROCESS | CREATE_NO_WINDOW);
}

#[cfg(not(windows))]
fn configure_detached(_command: &mut Command) {}

fn configure_no_window(command: &mut Command) {
    #[cfg(windows)]
    {
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        command.creation_flags(CREATE_NO_WINDOW);
    }
    #[cfg(not(windows))]
    {
        let _ = command;
    }
}

#[cfg(windows)]
fn inspect_process(pid: u32) -> Result<InspectedProcess, String> {
    let script = format!(
        "$p = Get-CimInstance -ClassName Win32_Process -Filter 'ProcessId = {pid}' -ErrorAction SilentlyContinue; \
         if ($null -eq $p) {{ exit 3 }}; \
         [Console]::Out.Write([string]$p.CommandLine)"
    );
    let mut command = Command::new("powershell");
    command.args(["-NoProfile", "-Command", &script]);
    configure_no_window(&mut command);
    let output = command
        .output()
        .map_err(|error| format!("failed to inspect PID {pid} with PowerShell/CIM: {error}"))?;

    if output.status.code() == Some(3) {
        return Ok(InspectedProcess::Missing);
    }
    if !output.status.success() {
        return Err(format_process_failure(
            "powershell Get-CimInstance",
            pid,
            output,
        ));
    }

    let command_line = String::from_utf8_lossy(&output.stdout).trim().to_string();
    Ok(InspectedProcess::Running(ProcessInfo::from_command_line(
        command_line,
    )))
}

#[cfg(not(windows))]
fn inspect_process(pid: u32) -> Result<InspectedProcess, String> {
    let path = PathBuf::from(format!("/proc/{pid}/cmdline"));
    let bytes = match fs::read(&path) {
        Ok(bytes) => bytes,
        Err(error) if error.kind() == io::ErrorKind::NotFound => {
            return Ok(InspectedProcess::Missing)
        }
        Err(error) => {
            return Err(format!(
                "failed to read process command line {}: {error}",
                path.display()
            ));
        }
    };

    if bytes.is_empty() {
        return Ok(InspectedProcess::Running(
            ProcessInfo::from_args(Vec::new()),
        ));
    }

    let args = bytes
        .split(|byte| *byte == 0)
        .filter(|part| !part.is_empty())
        .map(|part| String::from_utf8_lossy(part).to_string())
        .collect::<Vec<_>>();
    Ok(InspectedProcess::Running(ProcessInfo::from_args(args)))
}

#[cfg(windows)]
fn kill_process(pid: u32) -> Result<(), String> {
    let pid_text = pid.to_string();
    let mut command = Command::new("taskkill");
    command.args(["/PID", &pid_text, "/T", "/F"]);
    configure_no_window(&mut command);
    let output = command
        .output()
        .map_err(|error| format!("failed to run taskkill for PID {pid}: {error}"))?;
    if output.status.success() {
        Ok(())
    } else {
        Err(format_process_failure("taskkill", pid, output))
    }
}

#[cfg(not(windows))]
fn kill_process(pid: u32) -> Result<(), String> {
    let pid_text = pid.to_string();
    let output = Command::new("kill")
        .args(["-TERM", &pid_text])
        .output()
        .map_err(|error| format!("failed to run kill for PID {pid}: {error}"))?;
    if output.status.success() {
        return Ok(());
    }

    let output = Command::new("kill")
        .args(["-KILL", &pid_text])
        .output()
        .map_err(|error| format!("failed to run kill -KILL for PID {pid}: {error}"))?;
    if output.status.success() {
        Ok(())
    } else {
        Err(format_process_failure("kill", pid, output))
    }
}

fn format_process_failure(label: &str, pid: u32, output: std::process::Output) -> String {
    format!(
        "{label} failed for PID {pid} with status {:?}\nstdout:\n{}\nstderr:\n{}",
        output.status.code(),
        String::from_utf8_lossy(&output.stdout).trim_end(),
        String::from_utf8_lossy(&output.stderr).trim_end()
    )
}

#[cfg(test)]
mod tests {
    use super::{
        build_start_command, comparable_path, configure_start_stdio, detect_mode, find_python,
        read_pid, read_pid_record, start_with_paths, start_with_paths_and_waiter, status_with_paths,
        stop_with_paths, stop_with_paths_and_controls, write_pid, InspectedProcess, ProcessInfo,
        ProcessInspector, ProcessKiller, ProxyPaths, ProxyPidMetadata, ProxyPidRecord,
        StartupOutcome,
    };
    use crate::Settings;
    use std::cell::RefCell;
    use std::fs;
    use std::io::Write;
    use std::net::TcpListener;
    use std::path::{Path, PathBuf};
    use std::process::Command;
    use std::thread;
    use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

    #[test]
    fn status_returns_not_running_when_health_endpoint_is_unavailable() {
        let root = temp_root("status-unavailable");
        let paths = test_paths(&root);
        write_settings(&paths, free_port());

        let status = status_with_paths(&paths).expect("status");

        assert_eq!(status.mode, "official");
        assert!(!status.proxy_running);
        assert_eq!(status.proxy_port, read_settings_port(&paths));
        assert_eq!(status.proxy_build, None);
    }

    #[test]
    fn detect_mode_identifies_official_and_custom_proxy_config_text() {
        assert_eq!(detect_mode("model_provider = \"openai\"\n"), "official");
        assert_eq!(detect_mode(""), "official");
        assert_eq!(detect_mode("# model_provider = \"custom\"\n"), "official");
        assert_eq!(
            detect_mode(
                r#"
# BEGIN CODEX PROXY SESSION CONFIG
model_provider = "custom"
# END CODEX PROXY SESSION CONFIG
"#
            ),
            "custom"
        );
        assert_eq!(
            detect_mode(
                r#"
model_provider = "custom"
[model_providers.custom]
name = "OpenAI"
requires_openai_auth = true
supports_websockets = true
wire_api = "responses"
"#
            ),
            "official"
        );
        assert_eq!(
            detect_mode(
                r#"
model_provider = "codex_proxy"
[model_providers.codex_proxy]
name = "Codex Proxy"
"#
            ),
            "official"
        );
        assert_eq!(
            detect_mode(
                r#"
model_provider = "custom"
[model_providers.custom]
base_url = "http://127.0.0.1:4555/v1"
"#
            ),
            "custom"
        );
        assert_eq!(
            detect_mode(
                r#"
model_catalog_json = "model-catalogs/codexhub-model-catalog.json"
[model_providers.codex_proxy]
"#
            ),
            "custom"
        );
        assert_eq!(
            detect_mode(
                r#"
model_catalog_json = "model-catalogs/codex-proxy-official-ollama.json"
[model_providers.codex_proxy]
"#
            ),
            "custom"
        );
    }

    #[test]
    fn pid_file_json_metadata_roundtrips_and_missing_pid_is_none() {
        let root = temp_root("pid-roundtrip");
        let paths = test_paths(&root);
        let script = paths.proxy_script_path();

        assert_eq!(read_pid(&paths).expect("missing pid"), None);
        write_pid(&paths, 42, 4555, &script).expect("write pid");

        assert_eq!(read_pid(&paths).expect("read pid"), Some(42));
        let record = read_pid_record(&paths)
            .expect("read record")
            .expect("record");
        let ProxyPidRecord::Managed(metadata) = record else {
            panic!("expected managed PID metadata");
        };
        assert_eq!(metadata.pid, 42);
        assert_eq!(metadata.port, 4555);
        assert_eq!(metadata.script_path, comparable_path(&script));
        assert_eq!(metadata.version, 1);
        let text = fs::read_to_string(paths.pid_path()).expect("pid text");
        let parsed: ProxyPidMetadata = serde_json::from_str(&text).expect("metadata json");
        assert_eq!(parsed.pid, 42);
    }

    #[test]
    fn pid_file_write_recovers_stale_atomic_lock() {
        let root = temp_root("pid-stale-lock");
        let paths = test_paths(&root);
        fs::create_dir_all(paths.proxy_dir()).unwrap();
        let lock = stale_lock_path(&paths.pid_path());
        fs::write(&lock, "pid=0\nacquired_at_millis=0\n").expect("write stale lock");

        write_pid(&paths, 42, 4555, &paths.proxy_script_path()).expect("write pid");

        assert!(!lock.exists());
        assert_eq!(read_pid(&paths).expect("read pid"), Some(42));
    }

    #[test]
    fn legacy_numeric_pid_file_is_read_conservatively() {
        let root = temp_root("legacy-pid");
        let paths = test_paths(&root);
        fs::create_dir_all(paths.proxy_dir()).unwrap();
        fs::write(paths.pid_path(), "77\n").unwrap();

        assert_eq!(read_pid(&paths).expect("read pid"), Some(77));
        assert_eq!(
            read_pid_record(&paths).expect("read record"),
            Some(ProxyPidRecord::Legacy(77))
        );
    }

    #[test]
    fn stop_kills_pid_when_health_is_unavailable_and_command_line_matches() {
        let root = temp_root("stale-pid");
        let paths = test_paths(&root);
        let port = free_port();
        write_settings(&paths, port);
        write_pid(&paths, 12_345u32, port, &paths.proxy_script_path()).expect("write pid");
        let killer = RecordingKiller::default();
        let inspector = RecordingInspector::new(fake_proxy_process(&paths, port));

        let status = stop_with_paths_and_controls(&paths, &killer, &inspector).expect("stop stale");

        assert!(!status.proxy_running);
        assert_eq!(read_pid(&paths).expect("pid removed"), None);
        assert_eq!(killer.killed.borrow().as_slice(), &[12_345]);
        assert_eq!(inspector.inspected.borrow().as_slice(), &[12_345]);
    }

    #[test]
    fn stop_does_not_kill_pid_when_command_line_mismatches() {
        let root = temp_root("pid-mismatch");
        let paths = test_paths(&root);
        let port = free_port();
        write_settings(&paths, port);
        write_pid(&paths, 12_345u32, port, &paths.proxy_script_path()).expect("write pid");
        let killer = RecordingKiller::default();
        let inspector =
            RecordingInspector::new(InspectedProcess::Running(ProcessInfo::from_args(vec![
                "python".to_string(),
                "other_script.py".to_string(),
                "--port".to_string(),
                port.to_string(),
            ])));

        let status =
            stop_with_paths_and_controls(&paths, &killer, &inspector).expect("stop mismatch");

        assert!(!status.proxy_running);
        assert!(status.message.contains("was not killed"));
        assert_eq!(read_pid(&paths).expect("pid removed"), None);
        assert!(killer.killed.borrow().is_empty());
    }

    #[test]
    fn stop_treats_mismatched_pid_as_stale_when_health_responds() {
        let root = temp_root("pid-mismatch-health-running");
        let paths = test_paths(&root);
        let port = free_port();
        write_settings(&paths, port);
        write_pid(&paths, 12_345u32, port, &paths.proxy_script_path()).expect("write pid");
        let health_server = spawn_single_health_response(port);
        let killer = RecordingKiller::default();
        let inspector =
            RecordingInspector::new(InspectedProcess::Running(ProcessInfo::from_args(vec![
                "python".to_string(),
                "other_script.py".to_string(),
                "--port".to_string(),
                port.to_string(),
            ])));

        let status = stop_with_paths_and_controls(&paths, &killer, &inspector)
            .expect("stop should not error on stale mismatched pid");

        assert!(!status.proxy_running);
        assert!(status.message.contains("Proxy stopped gracefully"));
        assert!(status.message.contains("stale proxy PID 12345"));
        assert_eq!(read_pid(&paths).expect("pid removed"), None);
        assert!(killer.killed.borrow().is_empty());
        health_server.join().expect("health server joins");
    }

    #[test]
    fn stop_treats_missing_pid_file_as_external_running_proxy() {
        let root = temp_root("pid-missing-health-running");
        let paths = test_paths(&root);
        let port = free_port();
        write_settings(&paths, port);
        let health_server = spawn_health_then_shutdown_server(port);
        let killer = RecordingKiller::default();
        let inspector = RecordingInspector::new(InspectedProcess::Missing);

        let status = stop_with_paths_and_controls(&paths, &killer, &inspector)
            .expect("stop should not error when running proxy has no pid file");

        assert!(!status.proxy_running);
        assert!(status
            .message
            .contains("Proxy stopped gracefully without a proxy PID file"));
        assert_eq!(read_pid(&paths).expect("pid remains missing"), None);
        assert!(killer.killed.borrow().is_empty());
        assert!(inspector.inspected.borrow().is_empty());
        health_server.join().expect("health server joins");
    }

    #[test]
    fn stop_removes_stale_pid_when_process_is_missing() {
        let root = temp_root("pid-missing-process");
        let paths = test_paths(&root);
        let port = free_port();
        write_settings(&paths, port);
        write_pid(&paths, 12_345u32, port, &paths.proxy_script_path()).expect("write pid");
        let killer = RecordingKiller::default();
        let inspector = RecordingInspector::new(InspectedProcess::Missing);

        let status =
            stop_with_paths_and_controls(&paths, &killer, &inspector).expect("stop missing");

        assert!(!status.proxy_running);
        assert!(status.message.contains("Removed stale proxy PID 12345"));
        assert_eq!(read_pid(&paths).expect("pid removed"), None);
        assert!(killer.killed.borrow().is_empty());
    }

    #[test]
    fn start_timeout_error_includes_captured_stdout_and_stderr() {
        let root = temp_root("start-timeout-output");
        let paths = test_paths(&root);
        write_settings(&paths, free_port());
        write_fake_proxy_script(
            &paths,
            r#"
import sys
import time

print("fake proxy stdout during startup", flush=True)
print("fake proxy stderr during startup", file=sys.stderr, flush=True)
time.sleep(10)
"#,
        );

        let error = start_with_paths_and_waiter(
            &paths,
            Duration::ZERO,
            Duration::ZERO,
            &|_| Ok(None),
            |_child, _port, _timeout, _poll_interval, _health_probe, output_capture| {
                let deadline = Instant::now() + Duration::from_secs(2);
                loop {
                    let output = output_capture.snapshot();
                    if output.stdout.contains("fake proxy stdout during startup")
                        && output.stderr.contains("fake proxy stderr during startup")
                    {
                        return Ok(StartupOutcome::TimedOut);
                    }
                    if Instant::now() >= deadline {
                        return Err("fake proxy did not emit startup output in time".to_string());
                    }
                    thread::yield_now();
                }
            },
        )
        .expect_err("startup should time out");

        assert!(error.contains("did not become healthy"));
        assert!(error.contains("startup stdout"));
        assert!(error.contains("fake proxy stdout during startup"));
        assert!(error.contains("startup stderr"));
        assert!(error.contains("fake proxy stderr during startup"));
        assert_eq!(read_pid(&paths).expect("pid removed"), None);
    }

    #[test]
    fn start_stdio_configuration_exposes_piped_child_handles() {
        let root = temp_root("start-command-stdio");
        let paths = test_paths(&root);
        let mut command = Command::new(find_python(&paths));
        command.args(["-c", "import sys; sys.exit(0)"]);
        configure_start_stdio(&mut command);

        let mut child = command.spawn().expect("spawn stdio probe");
        assert!(child.stdin.take().is_some());
        assert!(child.stdout.take().is_some());
        assert!(child.stderr.take().is_some());
        let status = child.wait().expect("stdio probe exits");
        assert!(status.success());
    }

    #[test]
    fn start_command_sets_codex_home_for_runtime_logs() {
        let root = temp_root("start-command-codex-home");
        let paths = test_paths(&root);
        let settings = Settings {
            proxy_port: 4555,
            gateway_auto_retry_enabled: true,
            gateway_auto_retry_max_attempts: 23,
            gateway_image_proxy_enabled: true,
            gateway_image_proxy_model: "minimax-cn/MiniMax-M3".to_string(),
            ..Settings::default()
        };

        let command = build_start_command(
            Path::new("python-test"),
            &paths.proxy_script_path(),
            &paths,
            &settings,
        );
        let envs = command
            .get_envs()
            .map(|(key, value)| {
                (
                    key.to_string_lossy().into_owned(),
                    value.map(|item| item.to_os_string()),
                )
            })
            .collect::<std::collections::BTreeMap<_, _>>();

        let codex_home = envs
            .get("CODEX_HOME")
            .and_then(|value| value.as_ref())
            .map(PathBuf::from);
        assert_eq!(codex_home, Some(paths.codex_dir.clone()));
        assert_eq!(
            envs.get("CODEX_PROXY_AUTO_RETRY_ENABLED")
                .and_then(|value| value.as_ref())
                .and_then(|value| value.to_str()),
            Some("1")
        );
        assert_eq!(
            envs.get("CODEX_PROXY_GATEWAY_CLIENT_KEY")
                .and_then(|value| value.as_ref())
                .and_then(|value| value.to_str()),
            Some(settings.gateway_client_key.as_str())
        );
        assert_eq!(
            envs.get("CODEX_PROXY_AUTO_RETRY_MAX_ATTEMPTS")
                .and_then(|value| value.as_ref())
                .and_then(|value| value.to_str()),
            Some("23")
        );
        assert_eq!(
            envs.get("CODEX_PROXY_IMAGE_PROXY_ENABLED")
                .and_then(|value| value.as_ref())
                .and_then(|value| value.to_str()),
            Some("1")
        );
        assert_eq!(
            envs.get("CODEX_PROXY_IMAGE_PROXY_MODEL")
                .and_then(|value| value.as_ref())
                .and_then(|value| value.to_str()),
            Some("minimax-cn/MiniMax-M3")
        );
    }

    #[test]
    fn beta_style_start_command_separates_runtime_and_codex_target_homes() {
        let root = temp_root("isolated-start-homes");
        let runtime_home = root.join(".codexhub-beta");
        let target_home = root.join(".codex");
        let paths = ProxyPaths::new_isolated(&runtime_home, &target_home, root.join("repo"));
        let command = build_start_command(
            Path::new("python-test"),
            &paths.proxy_script_path(),
            &paths,
            &Settings::default(),
        );
        let envs = command
            .get_envs()
            .map(|(key, value)| (key.to_string_lossy().into_owned(), value.map(PathBuf::from)))
            .collect::<std::collections::BTreeMap<_, _>>();

        assert_eq!(envs.get("CODEX_HOME"), Some(&Some(runtime_home)));
        assert_eq!(envs.get("CODEXHUB_CODEX_TARGET_HOME"), Some(&Some(target_home)));
        assert_eq!(paths.codex_config_path(), root.join(".codex/config.toml"));
    }

    #[test]
    fn start_status_stop_real_python_proxy_on_ephemeral_port() {
        let root = temp_root("python-lifecycle");
        let repo_root = copy_python_sources_to_temp_repo(&root);
        let paths = ProxyPaths::new(root.join("codex-home"), repo_root);
        let port = free_port();
        write_settings(&paths, port);

        let result = (|| {
            let start_status = start_with_paths(&paths)?;
            ensure(start_status.proxy_running, "start status should be running")?;
            ensure(
                start_status.proxy_port == port,
                "start status should report the requested port",
            )?;
            ensure(
                start_status.proxy_build.is_some(),
                "start status should include proxy build",
            )?;
            ensure(read_pid(&paths)?.is_some(), "start should write a PID")?;

            let running_status = status_with_paths(&paths)?;
            ensure(running_status.proxy_running, "status should be running")?;

            let stop_status = stop_with_paths(&paths)?;
            ensure(!stop_status.proxy_running, "stop status should be stopped")?;
            ensure(
                stop_status.message == "Proxy stopped gracefully",
                "stop should use the proxy /shutdown endpoint",
            )?;
            ensure(read_pid(&paths)?.is_none(), "stop should remove PID")?;

            let stopped_status = status_with_paths(&paths)?;
            ensure(!stopped_status.proxy_running, "status should be stopped")?;
            Ok::<(), String>(())
        })();

        let _ = stop_with_paths(&paths);
        result.expect("python proxy lifecycle");
    }

    #[test]
    fn start_replaces_running_managed_proxy_from_previous_bundle() {
        let root = temp_root("python-bundle-upgrade");
        let old_repo_root = copy_python_sources_to_temp_repo(&root.join("old-bundle"));
        let new_repo_root = copy_python_sources_to_temp_repo(&root.join("new-bundle"));
        let runtime_home = root.join("codex-home");
        let old_paths = ProxyPaths::new(runtime_home.clone(), old_repo_root);
        let new_paths = ProxyPaths::new(runtime_home, new_repo_root);
        let port = free_port();
        write_settings(&old_paths, port);

        let result = (|| {
            let old_status = start_with_paths(&old_paths)?;
            ensure(old_status.proxy_running, "old bundle should start")?;

            let new_status = start_with_paths(&new_paths)?;
            ensure(new_status.proxy_running, "new bundle should start")?;
            let record = read_pid_record(&new_paths)?
                .ok_or_else(|| "new bundle should own the proxy PID".to_string())?;
            let ProxyPidRecord::Managed(metadata) = record else {
                return Err("new bundle should write managed PID metadata".to_string());
            };
            ensure(
                metadata.script_path == comparable_path(&new_paths.proxy_script_path()),
                "new bundle should replace the previous bundle's Gateway process",
            )?;
            Ok::<(), String>(())
        })();

        let _ = stop_with_paths(&new_paths);
        result.expect("Gateway bundle upgrade lifecycle");
    }

    #[test]
    fn start_replaces_running_managed_proxy_after_same_path_upgrade() {
        let root = temp_root("python-in-place-upgrade");
        let repo_root = copy_python_sources_to_temp_repo(&root);
        let paths = ProxyPaths::new(root.join("codex-home"), repo_root);
        let port = free_port();
        write_settings(&paths, port);

        let result = (|| {
            let old_status = start_with_paths(&paths)?;
            ensure(old_status.proxy_running, "old in-place bundle should start")?;
            let old_pid = read_pid(&paths)?
                .ok_or_else(|| "old in-place bundle should own the proxy PID".to_string())?;

            let script_path = paths.proxy_script_path();
            let script = fs::read_to_string(&script_path)
                .map_err(|error| format!("read in-place script: {error}"))?;
            fs::write(&script_path, format!("{script}\n# upgraded in place\n"))
                .map_err(|error| format!("update in-place script: {error}"))?;

            let new_status = start_with_paths(&paths)?;
            ensure(new_status.proxy_running, "upgraded in-place bundle should start")?;
            let new_pid = read_pid(&paths)?
                .ok_or_else(|| "upgraded in-place bundle should own the proxy PID".to_string())?;
            ensure(
                new_pid != old_pid,
                "same-path script changes should replace the previous Gateway process",
            )?;
            Ok::<(), String>(())
        })();

        let _ = stop_with_paths(&paths);
        result.expect("Gateway in-place upgrade lifecycle");
    }

    fn test_paths(root: &Path) -> ProxyPaths {
        ProxyPaths::new(root.join("codex-home"), root.join("repo-root"))
    }

    fn temp_root(name: &str) -> PathBuf {
        let suffix = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let path = std::env::temp_dir().join(format!(
            "codexhub-proxy-{name}-{}-{suffix}",
            std::process::id()
        ));
        let _ = fs::remove_dir_all(&path);
        fs::create_dir_all(&path).unwrap();
        path
    }

    fn stale_lock_path(path: &Path) -> PathBuf {
        path.with_file_name(format!(
            "{}.lock",
            path.file_name()
                .and_then(|name| name.to_str())
                .unwrap_or("pid")
        ))
    }

    fn free_port() -> u16 {
        let listener = TcpListener::bind(("127.0.0.1", 0)).expect("bind free port");
        listener.local_addr().unwrap().port()
    }

    fn spawn_single_health_response(port: u16) -> std::thread::JoinHandle<()> {
        std::thread::spawn(move || {
            let listener = TcpListener::bind(("127.0.0.1", port)).expect("bind health port");
            let (mut stream, _) = listener.accept().expect("accept health request");
            let mut buffer = [0u8; 1024];
            let _ = std::io::Read::read(&mut stream, &mut buffer);
            let body = r#"{"ok":true,"build":"test","features":[]}"#;
            write!(
                stream,
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                body.len(),
                body
            )
            .expect("write health response");
        })
    }

    fn spawn_health_then_shutdown_server(port: u16) -> std::thread::JoinHandle<()> {
        std::thread::spawn(move || {
            let listener = TcpListener::bind(("127.0.0.1", port)).expect("bind health port");
            for _ in 0..2 {
                let (mut stream, _) = listener.accept().expect("accept request");
                let mut buffer = [0u8; 1024];
                let count = std::io::Read::read(&mut stream, &mut buffer).unwrap_or(0);
                let request = String::from_utf8_lossy(&buffer[..count]);
                if request.starts_with("POST /shutdown ") {
                    stream
                        .write_all(
                            b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
                        )
                        .expect("write shutdown response");
                    break;
                }
                let body = r#"{"ok":true,"build":"test","features":[]}"#;
                write!(
                    stream,
                    "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
                    body.len(),
                    body
                )
                .expect("write health response");
            }
        })
    }

    fn write_settings(paths: &ProxyPaths, port: u16) {
        fs::create_dir_all(paths.proxy_dir()).unwrap();
        let settings = Settings {
            proxy_port: port,
            ..Settings::default()
        };
        let text = serde_json::to_string_pretty(&settings).unwrap();
        fs::write(paths.settings_path(), format!("{text}\n")).unwrap();
    }

    fn read_settings_port(paths: &ProxyPaths) -> u16 {
        let text = fs::read_to_string(paths.settings_path()).unwrap();
        serde_json::from_str::<Settings>(&text).unwrap().proxy_port
    }

    fn write_fake_proxy_script(paths: &ProxyPaths, body: &str) {
        fs::create_dir_all(paths.proxy_script_dir()).unwrap();
        fs::write(paths.proxy_script_path(), body.trim_start()).unwrap();
    }

    fn fake_proxy_process(paths: &ProxyPaths, port: u16) -> InspectedProcess {
        InspectedProcess::Running(ProcessInfo::from_args(vec![
            "python".to_string(),
            comparable_path(&paths.proxy_script_path()),
            "--host".to_string(),
            "127.0.0.1".to_string(),
            "--port".to_string(),
            port.to_string(),
        ]))
    }

    fn copy_python_sources_to_temp_repo(root: &Path) -> PathBuf {
        let repo_root = root.join("repo-root");
        let source = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .unwrap()
            .join("src-python");
        let target = repo_root.join("src-python");
        fs::create_dir_all(&target).unwrap();

        for entry in fs::read_dir(&source).unwrap() {
            let entry = entry.unwrap();
            let path = entry.path();
            if path.extension().and_then(|value| value.to_str()) == Some("py") {
                fs::copy(&path, target.join(path.file_name().unwrap())).unwrap();
            }
        }

        let vendor_source = source.join("vendor");
        let vendor_target = target.join("vendor");
        fs::create_dir_all(&vendor_target).unwrap();
        for entry in fs::read_dir(vendor_source).unwrap() {
            let entry = entry.unwrap();
            let path = entry.path();
            if path.extension().and_then(|value| value.to_str()) == Some("whl") {
                fs::copy(&path, vendor_target.join(path.file_name().unwrap())).unwrap();
            }
        }

        repo_root
    }

    fn ensure(condition: bool, message: &str) -> Result<(), String> {
        if condition {
            Ok(())
        } else {
            Err(message.to_string())
        }
    }

    #[derive(Default)]
    struct RecordingKiller {
        killed: RefCell<Vec<u32>>,
    }

    impl ProcessKiller for RecordingKiller {
        fn kill(&self, pid: u32) -> Result<(), String> {
            self.killed.borrow_mut().push(pid);
            Ok(())
        }
    }

    struct RecordingInspector {
        process: InspectedProcess,
        inspected: RefCell<Vec<u32>>,
    }

    impl RecordingInspector {
        fn new(process: InspectedProcess) -> Self {
            Self {
                process,
                inspected: RefCell::new(Vec::new()),
            }
        }
    }

    impl ProcessInspector for RecordingInspector {
        fn inspect(&self, pid: u32) -> Result<InspectedProcess, String> {
            self.inspected.borrow_mut().push(pid);
            Ok(self.process.clone())
        }
    }
}
