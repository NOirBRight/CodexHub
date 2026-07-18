use crate::gateway_lifecycle::{
    coordinator as gateway_lifecycle, GatewayIdentity, GatewayLifecycleBackend,
    GatewayLifecycleSnapshot, GatewayStartFailure, GatewayStartOutcome,
};
use crate::gateway_transaction::GatewayLifecyclePhase;
use crate::AppStatus;
use crate::Settings;
use crate::{build_info, runtime_paths, safe_file};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::fs;
use std::io::{self, Read};
#[cfg(windows)]
use std::io::{Seek, SeekFrom};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};
#[cfg(windows)]
use std::sync::atomic::{AtomicBool, Ordering};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

#[cfg(windows)]
use std::os::windows::fs::OpenOptionsExt;
#[cfg(windows)]
use std::os::windows::io::AsRawHandle;
#[cfg(windows)]
use std::os::windows::process::CommandExt;
#[cfg(windows)]
use windows_sys::Win32::Foundation::{
    CloseHandle, FILETIME, HANDLE, INVALID_HANDLE_VALUE, ERROR_INSUFFICIENT_BUFFER, NO_ERROR,
};
#[cfg(windows)]
use windows_sys::Win32::NetworkManagement::IpHelper::{
    GetExtendedTcpTable, MIB_TCPROW_OWNER_PID, MIB_TCPTABLE_OWNER_PID,
    TCP_TABLE_OWNER_PID_LISTENER,
};
#[cfg(windows)]
use windows_sys::Win32::Storage::FileSystem::{
    FILE_ATTRIBUTE_TEMPORARY, FILE_FLAG_DELETE_ON_CLOSE, FILE_SHARE_DELETE,
};
#[cfg(windows)]
use windows_sys::Win32::System::Diagnostics::ToolHelp::{
    CreateToolhelp32Snapshot, Thread32First, Thread32Next, TH32CS_SNAPTHREAD, THREADENTRY32,
};
#[cfg(windows)]
use windows_sys::Win32::System::JobObjects::{
    AssignProcessToJobObject, CreateJobObjectW, SetInformationJobObject, TerminateJobObject,
    JobObjectExtendedLimitInformation, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
};
#[cfg(windows)]
use windows_sys::Win32::System::Pipes::PeekNamedPipe;
#[cfg(windows)]
use windows_sys::Win32::System::Threading::{
    GetProcessTimes, OpenProcess, OpenThread, ResumeThread, PROCESS_QUERY_LIMITED_INFORMATION,
    THREAD_SUSPEND_RESUME,
};

const HEALTH_TIMEOUT: Duration = Duration::from_millis(800);
const SHUTDOWN_TIMEOUT: Duration = Duration::from_millis(800);
const START_TIMEOUT: Duration = Duration::from_secs(20);
const GRACEFUL_STOP_TIMEOUT: Duration = Duration::from_secs(2);
const KILL_STOP_TIMEOUT: Duration = Duration::from_secs(5);
const PID_FILE_VERSION: u32 = 2;
const LEGACY_PID_FILE_VERSION: u32 = 1;
const START_OUTPUT_CAPTURE_LIMIT: usize = 8 * 1024;
#[cfg(windows)]
const WINDOWS_INSPECTION_OUTPUT_LIMIT: usize = 64 * 1024;
#[cfg(windows)]
const WINDOWS_INSPECTION_TIMEOUT: Duration = Duration::from_secs(5);
const MANAGED_OVERLAY_MARKER_BEGIN: &str = "# BEGIN CODEX PROXY SESSION CONFIG";
const MANAGED_OVERLAY_MARKER_END: &str = "# END CODEX PROXY SESSION CONFIG";
// This is intentionally reached only from the compile-selected Rust debug
// build. It initializes the recorder in the existing Gateway child, then
// invokes the unchanged proxy entry point with its original command arguments.
const DEBUG_DIAGNOSTIC_BOOTSTRAP: &str = "import atexit, os, sys; from pathlib import Path; import codex_proxy, diagnostic_recorder; from diagnostic_control import DiagnosticControlBridge; runtime_home = Path(os.environ['CODEXHUB_DIAGNOSTICS_RUNTIME_HOME']); recorder = diagnostic_recorder.for_compile_flavor(runtime_home, 'debug', build_version=os.environ['CODEXHUB_DIAGNOSTICS_BUILD_VERSION'], source_revision=os.environ['CODEXHUB_DIAGNOSTICS_SOURCE_REVISION']); codex_proxy.GATEWAY_DIAGNOSTIC_RECORDER = recorder; control = DiagnosticControlBridge(recorder, runtime_home); control.start(); atexit.register(recorder.shutdown); atexit.register(control.shutdown); raise SystemExit(codex_proxy.main(sys.argv[2:]))";

pub fn status() -> Result<AppStatus, String> {
    let backend = ProxyLifecycleBackend::runtime()?;
    gateway_lifecycle()
        .status(&backend)
        .map(|snapshot| snapshot.status)
}

pub fn start_after<Prepare>(prepare: Prepare) -> Result<AppStatus, String>
where
    Prepare: FnOnce() -> Result<(), String>,
{
    let backend = ProxyLifecycleBackend::runtime()?;
    gateway_lifecycle()
        .start(&backend, prepare)
        .map(|snapshot| snapshot.status)
}

pub fn stop() -> Result<AppStatus, String> {
    let backend = ProxyLifecycleBackend::runtime_with_session_owned_identity(
        gateway_lifecycle().session_owned_identity(),
    )?;
    gateway_lifecycle().stop(&backend)
}

pub fn restart_after<Prepare>(prepare: Prepare) -> Result<AppStatus, String>
where
    Prepare: FnOnce() -> Result<(), String>,
{
    let backend = ProxyLifecycleBackend::runtime_with_session_owned_identity(
        gateway_lifecycle().session_owned_identity(),
    )?;
    gateway_lifecycle()
        .restart(&backend, prepare)
        .map(|snapshot| snapshot.status)
}

/// Ownership-safe handoff for #112 terminal-exit cleanup. #139 intentionally
/// exposes the identity without acting on application exit or update restart.
#[allow(dead_code)]
pub(crate) fn session_owned_identity() -> Option<GatewayIdentity> {
    gateway_lifecycle().session_owned_identity()
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

    fn lifecycle_gate_path(&self) -> PathBuf {
        self.proxy_dir().join("lifecycle.lock")
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

struct ProxyLifecycleBackend {
    paths: ProxyPaths,
    lifecycle_gate_path: PathBuf,
    session_owned_identity: Option<GatewayIdentity>,
}

impl ProxyLifecycleBackend {
    fn runtime() -> Result<Self, String> {
        Self::runtime_with_session_owned_identity(None)
    }

    fn runtime_with_session_owned_identity(
        session_owned_identity: Option<GatewayIdentity>,
    ) -> Result<Self, String> {
        let paths = ProxyPaths::runtime()?;
        Ok(Self {
            lifecycle_gate_path: paths.lifecycle_gate_path(),
            paths,
            session_owned_identity,
        })
    }
}

impl GatewayLifecycleBackend for ProxyLifecycleBackend {
    fn lifecycle_gate_path(&self) -> &Path {
        &self.lifecycle_gate_path
    }

    fn snapshot(&self) -> Result<GatewayLifecycleSnapshot, String> {
        reconciled_snapshot_with_controls(
            &self.paths,
            &health,
            &SystemProcessInspector,
            &SystemListenerInspector,
        )
    }

    fn transitional_status(&self, phase: GatewayLifecyclePhase) -> Result<AppStatus, String> {
        let settings = read_settings(&self.paths)?;
        let mode = read_mode(&self.paths)?;
        let (proxy_running, message) = match phase {
            GatewayLifecyclePhase::Unavailable => (false, "Gateway lifecycle is unavailable"),
            GatewayLifecyclePhase::Starting => (false, "Gateway is starting"),
            GatewayLifecyclePhase::Stopping => (true, "Gateway is stopping"),
            GatewayLifecyclePhase::Restarting => (true, "Gateway is restarting"),
            GatewayLifecyclePhase::Running => (true, "Gateway is running"),
            GatewayLifecyclePhase::Stopped => (false, "Gateway is stopped"),
            GatewayLifecyclePhase::Failed => (false, "Gateway lifecycle needs reconciliation"),
        };
        Ok(AppStatus {
            mode,
            proxy_running,
            proxy_port: settings.proxy_port,
            proxy_build: None,
            message: message.to_string(),
            gateway_lifecycle: phase,
            history_sync_status: None,
            history_sync_message: None,
        })
    }

    fn start(&self) -> Result<GatewayStartOutcome, GatewayStartFailure> {
        start_outcome_with_paths(&self.paths)
    }

    fn stop(&self) -> Result<AppStatus, String> {
        stop_session_owned_with_paths(&self.paths, self.session_owned_identity.as_ref())
    }

    fn can_reuse(&self, snapshot: &GatewayLifecycleSnapshot) -> bool {
        let Some(identity) = snapshot.identity.as_ref() else {
            return false;
        };
        let current_script = self.paths.proxy_script_path();
        normalized_path_text(&identity.script_path)
            == normalized_path_text(&current_script.to_string_lossy())
            && identity.script_sha256.is_some()
            && identity.script_sha256 == file_sha256(&current_script)
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
    #[serde(default, skip_serializing_if = "Option::is_none")]
    process_start_id: Option<String>,
    #[serde(default, skip_serializing_if = "is_false")]
    recovery: bool,
    started_at_unix_ms: u64,
}

fn is_false(value: &bool) -> bool {
    !*value
}

impl ProxyPidMetadata {
    #[cfg(test)]
    fn new(pid: u32, port: u16, script: &Path, process_start_id: String) -> Self {
        Self::with_identity(pid, port, script, Some(process_start_id), false)
    }

    fn recovery(pid: u32, port: u16, script: &Path) -> Self {
        Self::with_identity(pid, port, script, None, true)
    }

    fn recovery_with_identity(
        pid: u32,
        port: u16,
        script: &Path,
        process_start_id: String,
    ) -> Self {
        Self::with_identity(pid, port, script, Some(process_start_id), true)
    }

    fn with_identity(
        pid: u32,
        port: u16,
        script: &Path,
        process_start_id: Option<String>,
        recovery: bool,
    ) -> Self {
        Self {
            version: PID_FILE_VERSION,
            pid,
            port,
            script_path: comparable_path(script),
            script_sha256: file_sha256(script),
            process_start_id,
            recovery,
            started_at_unix_ms: SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap_or_default()
                .as_millis()
                .try_into()
                .unwrap_or(u64::MAX),
        }
    }
}

impl From<&ProxyPidMetadata> for GatewayIdentity {
    fn from(metadata: &ProxyPidMetadata) -> Self {
        Self {
            pid: metadata.pid,
            port: metadata.port,
            script_path: metadata.script_path.clone(),
            script_sha256: metadata.script_sha256.clone(),
            process_start_id: metadata.process_start_id.clone(),
            started_at_unix_ms: metadata.started_at_unix_ms,
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

    fn process_start_id(&self) -> Option<&String> {
        match self {
            Self::Managed(metadata) => metadata.process_start_id.as_ref(),
            Self::Legacy(_) => None,
        }
    }

    fn gateway_identity(&self) -> Option<GatewayIdentity> {
        match self {
            Self::Managed(metadata) => Some(GatewayIdentity::from(metadata)),
            Self::Legacy(_) => None,
        }
    }
}

fn reconciled_snapshot_with_controls(
    paths: &ProxyPaths,
    health_probe: &dyn Fn(u16) -> Result<Option<HealthResponse>, String>,
    inspector: &dyn ProcessInspector,
    listener_inspector: &dyn ListenerInspector,
) -> Result<GatewayLifecycleSnapshot, String> {
    let settings = read_settings(paths)?;
    let mode = read_mode(paths)?;
    let pid_record = read_pid_record(paths)?;
    let reconciliation_port = pid_record
        .as_ref()
        .map(|record| record.expected_port(settings.proxy_port))
        .unwrap_or(settings.proxy_port);
    let health = health_probe(reconciliation_port)?;
    let running_health = health.as_ref().filter(|response| response.is_running());

    let Some(response) = running_health else {
        if let Some(record) = pid_record {
            match verify_proxy_process(&record, paths, settings.proxy_port, inspector)? {
                VerifiedProxyProcess::Verified { pid } => {
                    return Err(format!(
                        "managed Gateway PID {pid} exists but health is unavailable on port {}; stop it through the lifecycle coordinator before starting another process",
                        reconciliation_port
                    ));
                }
                VerifiedProxyProcess::Missing { .. } | VerifiedProxyProcess::Mismatch { .. } => {
                    remove_pid(paths)?
                }
                VerifiedProxyProcess::Unknown { pid, reason } => {
                    return Err(format!(
                        "managed Gateway PID {pid} could not be inspected ({reason}); preserved durable ownership for reconciliation"
                    ));
                }
            }
        }
        return Ok(GatewayLifecycleSnapshot {
            status: AppStatus {
                mode,
                proxy_running: false,
                proxy_port: settings.proxy_port,
                proxy_build: None,
                message: "Gateway is not running".to_string(),
                gateway_lifecycle: GatewayLifecyclePhase::Stopped,
                history_sync_status: None,
                history_sync_message: None,
            },
            identity: None,
        });
    };

    let Some(mut pid_record) = pid_record else {
        return Err(format!(
            "Gateway health responds on port {}, but no managed PID identity exists; refusing to claim or replace the external listener",
            reconciliation_port
        ));
    };
    let pid = pid_record.pid();
    let recovery_pending = matches!(
        &pid_record,
        ProxyPidRecord::Managed(metadata) if metadata.recovery
    );
    let recovery_requires_identity = matches!(
        &pid_record,
        ProxyPidRecord::Managed(metadata) if metadata.recovery && metadata.process_start_id.is_none()
    );

    if recovery_requires_identity {
        let process = inspector.inspect(pid).map_err(|error| {
            format!(
                "Gateway health responds on port {reconciliation_port}, but managed PID {pid} inspection is unavailable (process command line inspection failed: {error}); preserved durable ownership"
            )
        })?;
        let InspectedProcess::Running(info) = process else {
            remove_pid(paths)?;
            return Err(format!(
                "Gateway health responds on port {}, but managed PID {pid} no longer exists; removed stale PID identity without stopping the listener",
                reconciliation_port
            ));
        };
        if let Err(reason) = verify_proxy_command_shape(
            &pid_record,
            paths,
            reconciliation_port,
            &info,
        ) {
            remove_pid(paths)?;
            return Err(format!(
                "Gateway health responds on port {}, but managed PID {pid} ownership could not be verified ({reason}); removed stale PID identity without stopping either process",
                reconciliation_port
            ));
        }
        let listener_pid = listener_inspector.listening_pid(reconciliation_port)?;
        if listener_pid != Some(pid) {
            let detail = listener_pid
                .map(|value| format!("listener PID {value}"))
                .unwrap_or_else(|| "no TCP listener owner".to_string());
            return Err(format!(
                "Gateway health responds on port {reconciliation_port}, but {detail} differs from exact managed PID {pid}; preserved the managed PID identity without stopping either process"
            ));
        }
        let Some(process_start_id) = info.process_start_id else {
            return Err(format!(
                "Gateway health responds on port {reconciliation_port}, but managed PID {pid} inspection has no process creation identity; preserved durable ownership"
            ));
        };
        if let ProxyPidRecord::Managed(metadata) = &mut pid_record {
            metadata.process_start_id = Some(process_start_id);
            metadata.recovery = false;
        }
    } else if recovery_pending {
        if let ProxyPidRecord::Managed(metadata) = &mut pid_record {
            metadata.recovery = false;
        }
    }

    match verify_proxy_process(&pid_record, paths, reconciliation_port, inspector)? {
        VerifiedProxyProcess::Verified { .. } => {}
        VerifiedProxyProcess::Missing { .. } => {
            remove_pid(paths)?;
            return Err(format!(
                "Gateway health responds on port {}, but managed PID {pid} no longer exists; removed stale PID identity without stopping the listener",
                reconciliation_port
            ));
        }
        VerifiedProxyProcess::Mismatch { reason, .. } => {
            remove_pid(paths)?;
            return Err(format!(
                "Gateway health responds on port {}, but managed PID {pid} ownership could not be verified ({reason}); removed stale PID identity without stopping either process",
                reconciliation_port
            ));
        }
        VerifiedProxyProcess::Unknown { reason, .. } => {
            return Err(format!(
                "Gateway health responds on port {reconciliation_port}, but managed PID {pid} inspection is unavailable ({reason}); preserved durable ownership"
            ));
        }
    }

    let listener_pid = listener_inspector.listening_pid(reconciliation_port)?;
    let Some(listener_pid) = listener_pid else {
        return Err(format!(
            "Gateway health responds on port {}, but no TCP listener owner could be reconciled; preserved exact managed PID {pid} for ownership-safe recovery",
            reconciliation_port
        ));
    };
    if listener_pid != pid {
        return Err(format!(
            "Gateway health responds on port {}, but listener PID {listener_pid} differs from exact managed PID {pid}; preserved the managed PID identity without stopping either process",
            reconciliation_port
        ));
    }

    if recovery_pending {
        write_pid_record(paths, &pid_record)?;
    }

    let identity = match &pid_record {
        ProxyPidRecord::Managed(metadata) => GatewayIdentity::from(metadata),
        ProxyPidRecord::Legacy(pid) => GatewayIdentity {
            pid: *pid,
            port: settings.proxy_port,
            script_path: comparable_path(&paths.proxy_script_path()),
            script_sha256: file_sha256(&paths.proxy_script_path()),
            process_start_id: None,
            started_at_unix_ms: 0,
        },
    };

    Ok(GatewayLifecycleSnapshot {
        status: AppStatus {
            mode,
            proxy_running: true,
            proxy_port: reconciliation_port,
            proxy_build: response.build.clone(),
            message: format!("Gateway running with PID {pid}"),
            gateway_lifecycle: GatewayLifecyclePhase::Running,
            history_sync_status: None,
            history_sync_message: None,
        },
        identity: Some(identity),
    })
}

#[cfg(test)]
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
            "Gateway is healthy".to_string()
        } else {
            "Gateway is not running".to_string()
        },
        gateway_lifecycle: if proxy_running {
            GatewayLifecyclePhase::Running
        } else {
            GatewayLifecyclePhase::Stopped
        },
        history_sync_status: None,
        history_sync_message: None,
    })
}

#[cfg(test)]
fn start_with_paths(paths: &ProxyPaths) -> Result<AppStatus, String> {
    start_outcome_with_paths(paths)
        .map(|outcome| outcome.snapshot.status)
        .map_err(|failure| failure.message)
}

fn start_outcome_with_paths(paths: &ProxyPaths) -> Result<GatewayStartOutcome, GatewayStartFailure> {
    replace_managed_proxy_from_previous_bundle(paths)?;
    start_outcome_with_paths_and_timeout(paths, START_TIMEOUT)
}

fn replace_managed_proxy_from_previous_bundle(paths: &ProxyPaths) -> Result<(), String> {
    let Some(ProxyPidRecord::Managed(metadata)) = read_pid_record(paths)? else {
        return Ok(());
    };
    let current_script = paths.proxy_script_path();
    let path_matches = normalized_path_text(&metadata.script_path)
        == normalized_path_text(&current_script.to_string_lossy());
    let fingerprint_matches =
        metadata.script_sha256.is_some() && metadata.script_sha256 == file_sha256(&current_script);
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

fn start_outcome_with_paths_and_timeout(
    paths: &ProxyPaths,
    timeout: Duration,
) -> Result<GatewayStartOutcome, GatewayStartFailure> {
    start_with_paths_and_controls(
        paths,
        timeout,
        Duration::from_millis(200),
        &health,
        &SystemProcessInspector,
        &SystemListenerInspector,
        |child, port, timeout, poll_interval, health_probe, _output_capture| {
            wait_for_startup_health(child, port, timeout, poll_interval, health_probe)
        },
    )
}

#[cfg(test)]
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
    start_with_paths_and_controls(
        paths,
        timeout,
        poll_interval,
        health_probe,
        &SystemProcessInspector,
        &SystemListenerInspector,
        wait_for_startup,
    )
    .map(|outcome| outcome.snapshot.status)
    .map_err(|failure| failure.message)
}

fn start_with_paths_and_controls<F>(
    paths: &ProxyPaths,
    timeout: Duration,
    poll_interval: Duration,
    health_probe: &dyn Fn(u16) -> Result<Option<HealthResponse>, String>,
    inspector: &dyn ProcessInspector,
    listener_inspector: &dyn ListenerInspector,
    wait_for_startup: F,
) -> Result<GatewayStartOutcome, GatewayStartFailure>
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
    let existing =
        reconciled_snapshot_with_controls(paths, health_probe, inspector, listener_inspector)?;
    if existing.identity.is_some() {
        return Ok(GatewayStartOutcome {
            snapshot: existing,
            spawned: false,
        });
    }

    let settings = read_settings(paths)?;
    let script = paths.proxy_script_path();
    if !script.exists() {
        return Err(format!("Gateway script not found: {}", script.display()).into());
    }

    fs::create_dir_all(paths.proxy_dir()).map_err(|error| {
        format!(
            "failed to create Gateway runtime directory {}: {error}",
            paths.proxy_dir().display()
        )
    })?;

    remove_pid(paths)?;
    let python = find_python(paths);
    let mut command = build_start_command(&python, &script, paths, &settings);

    let mut child = command.spawn().map_err(|error| {
        format!(
            "failed to start Gateway with {} {}: {error}",
            python.display(),
            script.display()
        )
    })?;
    let output_capture = capture_child_stdio(&mut child);
    let pid = child.id();
    let process_start_id = match inspector.spawned_process_start_id(&child) {
        Ok(process_start_id) => process_start_id,
        Err(error) => {
            return Err(clean_up_failed_start(
                paths,
                &mut child,
                output_capture,
                error,
                &ProxyPidRecord::Managed(ProxyPidMetadata::recovery(
                    pid,
                    settings.proxy_port,
                    &script,
                )),
                inspector,
            ))
        }
    };
    let mut record = ProxyPidRecord::Managed(ProxyPidMetadata::recovery_with_identity(
        pid,
        settings.proxy_port,
        &script,
        process_start_id,
    ));

    let startup = match wait_for_startup(
        &mut child,
        settings.proxy_port,
        timeout,
        poll_interval,
        health_probe,
        &output_capture,
    ) {
        Ok(startup) => startup,
        Err(error) => {
            return Err(clean_up_failed_start(
                paths,
                &mut child,
                output_capture,
                error,
                &record,
                inspector,
            ))
        }
    };

    match startup {
        StartupOutcome::Healthy(response) if response.is_running() => {
            match inspector.inspect(pid) {
                Ok(InspectedProcess::Running(info)) => {
                    if info.process_start_id.as_ref() != record.process_start_id() {
                        return Err(clean_up_failed_start(
                            paths,
                            &mut child,
                            output_capture,
                            format!(
                                "spawned Gateway PID {pid} process creation identity changed during startup fencing"
                            ),
                            &record,
                            inspector,
                        ));
                    }
                }
                Ok(InspectedProcess::Missing) => {
                    return Err(clean_up_failed_start(
                        paths,
                        &mut child,
                        output_capture,
                        format!("spawned Gateway PID {pid} disappeared before ownership fencing"),
                        &record,
                        inspector,
                    ))
                }
                Err(error) => {
                    return Err(clean_up_failed_start(
                        paths,
                        &mut child,
                        output_capture,
                        format!("failed to fence spawned Gateway PID {pid}: {error}"),
                        &record,
                        inspector,
                    ))
                }
            }
            let mut verified_record = record.clone();
            if let ProxyPidRecord::Managed(metadata) = &mut verified_record {
                metadata.recovery = false;
            }
            let verification =
                verify_proxy_process(&verified_record, paths, settings.proxy_port, inspector)?;
            if verification != (VerifiedProxyProcess::Verified { pid }) {
                return Err(clean_up_failed_start(
                    paths,
                    &mut child,
                    output_capture,
                    format!(
                        "spawned Gateway PID {pid} ownership could not be reconciled: {verification:?}"
                    ),
                    &record,
                    inspector,
                ));
            }

            let listener_pid = match listener_inspector.listening_pid(settings.proxy_port) {
                Ok(listener_pid) => listener_pid,
                Err(error) => {
                    return Err(clean_up_failed_start(
                        paths,
                        &mut child,
                        output_capture,
                        error,
                        &record,
                        inspector,
                    ))
                }
            };
            if listener_pid != Some(pid) {
                let detail = listener_pid
                    .map(|listener_pid| format!("listener PID {listener_pid}"))
                    .unwrap_or_else(|| "no listener PID".to_string());
                return Err(clean_up_failed_start(
                    paths,
                    &mut child,
                    output_capture,
                    format!(
                        "spawned Gateway PID {pid} became healthy, but {detail} owns port {}",
                        settings.proxy_port
                    ),
                    &record,
                    inspector,
                ));
            }

            record = verified_record;
            if let Err(error) = write_pid_record(paths, &record) {
                return Err(clean_up_failed_start(
                    paths,
                    &mut child,
                    output_capture,
                    error,
                    &record,
                    inspector,
                ));
            }
            match reconciled_snapshot_with_controls(
                paths,
                health_probe,
                inspector,
                listener_inspector,
            ) {
                Ok(snapshot) => Ok(GatewayStartOutcome {
                    snapshot,
                    spawned: true,
                }),
                Err(error) => Err(clean_up_failed_start(
                    paths,
                    &mut child,
                    output_capture,
                    error,
                    &record,
                    inspector,
                )),
            }
        }
        StartupOutcome::Healthy(_) => Err(clean_up_failed_start(
            paths,
            &mut child,
            output_capture,
            format!(
                "Gateway returned a non-running health response at http://127.0.0.1:{}/health",
                settings.proxy_port
            ),
            &record,
            inspector,
        )),
        StartupOutcome::Exited(status) => {
            let _ = remove_pid(paths);
            let output = output_capture.finish();
            Err(format_startup_failure(
                format!("Gateway process exited before health became ready with status {status:?}"),
                &output,
            )
            .into())
        }
        StartupOutcome::TimedOut => Err(clean_up_failed_start(
            paths,
            &mut child,
            output_capture,
            format_startup_failure(
                format!(
                    "Gateway did not become healthy within {} seconds at http://127.0.0.1:{}/health",
                    timeout.as_secs(),
                    settings.proxy_port
                ),
                &StartupOutputSnapshot::default(),
            ),
            &record,
            inspector,
        )),
    }
}

fn clean_up_failed_start(
    paths: &ProxyPaths,
    child: &mut Child,
    output_capture: StartupOutputCapture,
    reason: String,
    record: &ProxyPidRecord,
    inspector: &dyn ProcessInspector,
) -> GatewayStartFailure {
    clean_up_failed_start_with_controls(
        paths,
        child,
        output_capture,
        reason,
        record,
        inspector,
        &SystemChildTerminator,
    )
}

const USER_REQUESTED_SHUTDOWN_TIMEOUT: Duration = Duration::from_secs(2);
const USER_REQUESTED_SHUTDOWN_POLL_INTERVAL: Duration = Duration::from_millis(50);

trait ShutdownClock {
    fn elapsed(&self) -> Duration;
    fn sleep(&self, duration: Duration);
}

struct SystemShutdownClock {
    started_at: Instant,
}

impl SystemShutdownClock {
    fn new() -> Self {
        Self {
            started_at: Instant::now(),
        }
    }
}

impl ShutdownClock for SystemShutdownClock {
    fn elapsed(&self) -> Duration {
        self.started_at.elapsed()
    }

    fn sleep(&self, duration: Duration) {
        if !duration.is_zero() {
            thread::sleep(duration);
        }
    }
}

struct ShutdownBudget<'a> {
    clock: &'a dyn ShutdownClock,
}

impl<'a> ShutdownBudget<'a> {
    fn new(clock: &'a dyn ShutdownClock) -> Self {
        Self { clock }
    }

    fn remaining(&self) -> Duration {
        USER_REQUESTED_SHUTDOWN_TIMEOUT.saturating_sub(self.clock.elapsed())
    }

    fn cap(&self, maximum: Duration) -> Duration {
        self.remaining().min(maximum)
    }

    fn sleep_to_next_poll(&self) -> bool {
        let wait = self.remaining().min(USER_REQUESTED_SHUTDOWN_POLL_INTERVAL);
        if wait.is_zero() {
            return false;
        }
        self.clock.sleep(wait);
        true
    }
}

struct UserRequestedShutdownControls<'a> {
    killer: &'a dyn ProcessKiller,
    inspector: &'a dyn ProcessInspector,
    listener_inspector: &'a dyn ListenerInspector,
    shutdown_request: &'a dyn Fn(u16, &str, Duration) -> Result<(), String>,
    health_probe: &'a dyn Fn(u16, Duration) -> Result<Option<HealthResponse>, String>,
    clock: &'a dyn ShutdownClock,
}

fn stop_session_owned_with_paths(
    paths: &ProxyPaths,
    session_owned_identity: Option<&GatewayIdentity>,
) -> Result<AppStatus, String> {
    let clock = SystemShutdownClock::new();
    let controls = UserRequestedShutdownControls {
        killer: &SystemProcessKiller,
        inspector: &SystemProcessInspector,
        listener_inspector: &SystemListenerInspector,
        shutdown_request: &request_shutdown_with_timeout,
        health_probe: &health_with_timeout,
        clock: &clock,
    };
    stop_session_owned_with_paths_and_controls(
        paths,
        session_owned_identity,
        &controls,
    )
}

fn stop_session_owned_with_paths_and_controls(
    paths: &ProxyPaths,
    session_owned_identity: Option<&GatewayIdentity>,
    controls: &UserRequestedShutdownControls<'_>,
) -> Result<AppStatus, String> {
    let budget = ShutdownBudget::new(controls.clock);
    let settings = read_settings(paths)?;
    let mode = read_mode(paths)?;
    let pid_record = read_pid_record(paths)?;
    let lifecycle_port = pid_record
        .as_ref()
        .map(|record| record.expected_port(settings.proxy_port))
        .unwrap_or(settings.proxy_port);
    let was_running = gateway_running_with_budget(lifecycle_port, controls.health_probe, &budget)?;

    let Some(pid_record) = pid_record else {
        if was_running {
            return Err(format!(
                "Gateway health responds on port {lifecycle_port}, but no managed PID identity exists; refusing to send shutdown to the external listener"
            ));
        }
        return stopped_gateway_status(mode, lifecycle_port, "Gateway is not running".to_string());
    };

    match verify_proxy_process(&pid_record, paths, lifecycle_port, controls.inspector)? {
        VerifiedProxyProcess::Missing { pid } => {
            remove_pid(paths)?;
            return stopped_gateway_status(
                mode,
                lifecycle_port,
                format!("Removed stale Gateway PID {pid}; user-requested shutdown found no live process"),
            );
        }
        VerifiedProxyProcess::Mismatch { pid, reason } => {
            remove_pid(paths)?;
            return Err(format!(
                "Gateway user-requested shutdown refused because managed PID {pid} ownership could not be verified ({reason}); removed stale identity"
            ));
        }
        VerifiedProxyProcess::Unknown { pid, reason } => {
            return Err(format!(
                "Gateway user-requested shutdown could not inspect managed PID {pid} ({reason}); preserved durable ownership"
            ));
        }
        VerifiedProxyProcess::Verified { pid } if was_running => {
            verify_listener_owner(
                pid,
                lifecycle_port,
                controls.listener_inspector,
                "before shutdown",
            )?;
            let timeout = budget.cap(SHUTDOWN_TIMEOUT);
            if !timeout.is_zero() {
                let _ = (controls.shutdown_request)(
                    lifecycle_port,
                    &settings.gateway_client_key,
                    timeout,
                );
            }
        }
        VerifiedProxyProcess::Verified { .. } => {}
    }

    let listener_stopped = if was_running {
        wait_for_listener_stop_with_budget(lifecycle_port, controls.health_probe, &budget)?
    } else {
        true
    };
    let process_stopped = if listener_stopped {
        wait_for_managed_process_stop_with_budget(
            &pid_record,
            paths,
            lifecycle_port,
            controls.inspector,
            &budget,
        )?
    } else {
        false
    };

    if process_stopped {
        remove_pid(paths)?;
        return stopped_gateway_status(
            mode,
            lifecycle_port,
            "Gateway stopped gracefully after user-requested shutdown".to_string(),
        );
    }

    let pid = force_kill_session_owned_gateway_at_deadline(
        paths,
        &pid_record,
        session_owned_identity,
        lifecycle_port,
        controls.killer,
        controls.inspector,
        controls.listener_inspector,
    )?;
    log::warn!(
        "Gateway user_requested_shutdown force-close issued for the current-session PID {pid}"
    );

    match verify_proxy_process(&pid_record, paths, lifecycle_port, controls.inspector)? {
        VerifiedProxyProcess::Missing { .. } => {}
        VerifiedProxyProcess::Verified { pid } => {
            return Err(format!(
                "Gateway user-requested shutdown force-close reached its two-second budget, but PID {pid} is still running"
            ));
        }
        VerifiedProxyProcess::Mismatch { pid, reason } => {
            return Err(format!(
                "Gateway PID {pid} changed identity after user-requested force-close ({reason}); preserved durable ownership"
            ));
        }
        VerifiedProxyProcess::Unknown { pid, reason } => {
            return Err(format!(
                "Gateway PID {pid} could not be inspected after user-requested force-close ({reason}); preserved durable ownership"
            ));
        }
    }
    remove_pid(paths)?;
    stopped_gateway_status(
        mode,
        lifecycle_port,
        format!("Gateway PID {pid} stopped after user-requested shutdown"),
    )
}

fn stopped_gateway_status(mode: String, port: u16, message: String) -> Result<AppStatus, String> {
    Ok(AppStatus {
        mode,
        proxy_running: false,
        proxy_port: port,
        proxy_build: None,
        message,
        gateway_lifecycle: GatewayLifecyclePhase::Stopped,
        history_sync_status: None,
        history_sync_message: None,
    })
}

fn gateway_running_with_budget(
    port: u16,
    health_probe: &dyn Fn(u16, Duration) -> Result<Option<HealthResponse>, String>,
    budget: &ShutdownBudget<'_>,
) -> Result<bool, String> {
    let timeout = budget.cap(HEALTH_TIMEOUT);
    if timeout.is_zero() {
        return Ok(false);
    }
    Ok(health_probe(port, timeout)?
        .as_ref()
        .map(HealthResponse::is_running)
        .unwrap_or(false))
}

fn wait_for_listener_stop_with_budget(
    port: u16,
    health_probe: &dyn Fn(u16, Duration) -> Result<Option<HealthResponse>, String>,
    budget: &ShutdownBudget<'_>,
) -> Result<bool, String> {
    loop {
        if !gateway_running_with_budget(port, health_probe, budget)? {
            return Ok(true);
        }
        if !budget.sleep_to_next_poll() {
            return Ok(false);
        }
    }
}

fn wait_for_managed_process_stop_with_budget(
    record: &ProxyPidRecord,
    paths: &ProxyPaths,
    port: u16,
    inspector: &dyn ProcessInspector,
    budget: &ShutdownBudget<'_>,
) -> Result<bool, String> {
    loop {
        match verify_proxy_process(record, paths, port, inspector)? {
            VerifiedProxyProcess::Missing { .. } => return Ok(true),
            VerifiedProxyProcess::Verified { .. } => {}
            VerifiedProxyProcess::Mismatch { pid, reason } => {
                return Err(format!(
                    "Gateway PID {pid} changed identity while waiting for user-requested shutdown ({reason}); preserved durable ownership"
                ));
            }
            VerifiedProxyProcess::Unknown { pid, reason } => {
                return Err(format!(
                    "Gateway PID {pid} could not be inspected while waiting for user-requested shutdown ({reason}); preserved durable ownership"
                ));
            }
        }
        if !budget.sleep_to_next_poll() {
            return Ok(false);
        }
    }
}

fn force_kill_session_owned_gateway_at_deadline(
    paths: &ProxyPaths,
    record: &ProxyPidRecord,
    session_owned_identity: Option<&GatewayIdentity>,
    port: u16,
    killer: &dyn ProcessKiller,
    inspector: &dyn ProcessInspector,
    listener_inspector: &dyn ListenerInspector,
) -> Result<u32, String> {
    let Some(session_owned_identity) = session_owned_identity else {
        return Err(
            "Gateway user-requested shutdown refused force-close because #139 has no current-session identity"
                .to_string(),
        );
    };
    let Some(record_identity) = record.gateway_identity() else {
        return Err(
            "Gateway user-requested shutdown refused force-close because the durable PID record has no verifiable current-session identity"
                .to_string(),
        );
    };
    if &record_identity != session_owned_identity {
        return Err(
            "Gateway user-requested shutdown refused force-close because the durable Gateway identity no longer matches #139's current-session identity"
                .to_string(),
        );
    }

    let pid = match verify_proxy_process(record, paths, port, inspector)? {
        VerifiedProxyProcess::Verified { pid } => pid,
        VerifiedProxyProcess::Missing { pid } => {
            remove_pid(paths)?;
            return Err(format!(
                "managed Gateway PID {pid} disappeared before user-requested force-close; preserved truthful reconciliation"
            ));
        }
        VerifiedProxyProcess::Mismatch { pid, reason } => {
            return Err(format!(
                "managed Gateway PID {pid} ownership could not be verified before user-requested force-close: {reason}"
            ));
        }
        VerifiedProxyProcess::Unknown { pid, reason } => {
            return Err(format!(
                "managed Gateway PID {pid} inspection is unavailable before user-requested force-close ({reason}); preserved durable ownership"
            ));
        }
    };

    let listener_pid = listener_inspector.listening_pid(port)?;
    if let Some(listener_pid) = listener_pid {
        if listener_pid != pid {
            return Err(format!(
                "exact managed Gateway PID {pid} does not own the current listener on port {port} immediately before user-requested force-close (PID {listener_pid}); refusing destructive action"
            ));
        }
    }
    killer.kill(pid)?;
    Ok(pid)
}

fn stop_with_paths(paths: &ProxyPaths) -> Result<AppStatus, String> {
    stop_with_paths_and_controls(
        paths,
        &SystemProcessKiller,
        &SystemProcessInspector,
        &SystemListenerInspector,
    )
}

fn stop_with_paths_and_controls(
    paths: &ProxyPaths,
    killer: &dyn ProcessKiller,
    inspector: &dyn ProcessInspector,
    listener_inspector: &dyn ListenerInspector,
) -> Result<AppStatus, String> {
    let settings = read_settings(paths)?;
    let mode = read_mode(paths)?;
    let pid_record = read_pid_record(paths)?;
    let lifecycle_port = pid_record
        .as_ref()
        .map(|record| record.expected_port(settings.proxy_port))
        .unwrap_or(settings.proxy_port);
    let was_running = health(lifecycle_port)?
        .as_ref()
        .map(HealthResponse::is_running)
        .unwrap_or(false);

    if !was_running {
        return stop_when_health_unavailable(
            paths,
            mode,
            lifecycle_port,
            pid_record,
            killer,
            inspector,
            listener_inspector,
        );
    }

    let Some(pid_record) = pid_record else {
        return Err(format!(
            "Gateway health responds on port {}, but no managed PID identity exists; refusing to send shutdown to the external listener",
            lifecycle_port
        ));
    };

    let pid = match verify_proxy_process(&pid_record, paths, lifecycle_port, inspector)? {
        VerifiedProxyProcess::Verified { pid } => pid,
        VerifiedProxyProcess::Missing { pid } => {
            remove_pid(paths)?;
            return Err(format!(
                "Gateway health responds on port {}, but managed PID {pid} no longer exists; removed stale identity and refused shutdown",
                lifecycle_port
            ));
        }
        VerifiedProxyProcess::Mismatch { pid, reason } => {
            remove_pid(paths)?;
            return Err(format!(
                "Gateway health responds on port {}, but managed PID {pid} ownership could not be verified ({reason}); removed stale identity and refused shutdown",
                lifecycle_port
            ));
        }
        VerifiedProxyProcess::Unknown { pid, reason } => {
            return Err(format!(
                "managed Gateway PID {pid} inspection is unavailable before shutdown ({reason}); preserved durable ownership"
            ));
        }
    };

    verify_listener_owner(pid, lifecycle_port, listener_inspector, "before shutdown")?;

    let _ = request_shutdown(lifecycle_port, &settings.gateway_client_key);
    if wait_for_stopped(lifecycle_port, GRACEFUL_STOP_TIMEOUT)? {
        confirm_managed_process_stopped(
            &pid_record,
            paths,
            lifecycle_port,
            inspector,
            GRACEFUL_STOP_TIMEOUT,
        )?;
        remove_pid(paths)?;
        return Ok(AppStatus {
            mode,
            proxy_running: false,
            proxy_port: lifecycle_port,
            proxy_build: None,
            message: "Gateway stopped gracefully".to_string(),
            gateway_lifecycle: GatewayLifecyclePhase::Stopped,
            history_sync_status: None,
            history_sync_message: None,
        });
    }

    let pid = force_kill_after_graceful_timeout(
        paths,
        &pid_record,
        lifecycle_port,
        killer,
        inspector,
        listener_inspector,
    )?;
    if !wait_for_stopped(lifecycle_port, KILL_STOP_TIMEOUT)? {
        return Err(format!(
            "sent kill signal to Gateway PID {pid}, but health still responds on port {}",
            lifecycle_port
        ));
    }

    confirm_managed_process_stopped(
        &pid_record,
        paths,
        lifecycle_port,
        inspector,
        KILL_STOP_TIMEOUT,
    )?;

    remove_pid(paths)?;
    Ok(AppStatus {
        mode,
        proxy_running: false,
        proxy_port: lifecycle_port,
        proxy_build: None,
        message: format!("Gateway PID {pid} stopped"),
        gateway_lifecycle: GatewayLifecyclePhase::Stopped,
        history_sync_status: None,
        history_sync_message: None,
    })
}

fn force_kill_after_graceful_timeout(
    paths: &ProxyPaths,
    record: &ProxyPidRecord,
    port: u16,
    killer: &dyn ProcessKiller,
    inspector: &dyn ProcessInspector,
    listener_inspector: &dyn ListenerInspector,
) -> Result<u32, String> {
    let pid = match verify_proxy_process(record, paths, port, inspector)? {
        VerifiedProxyProcess::Verified { pid } => pid,
        VerifiedProxyProcess::Missing { pid } => {
            remove_pid(paths)?;
            return Err(format!(
                "managed Gateway PID {pid} disappeared before force kill; preserved truthful failure for reconciliation"
            ));
        }
        VerifiedProxyProcess::Mismatch { pid, reason } => {
            remove_pid(paths)?;
            return Err(format!(
                "managed Gateway PID {pid} ownership could not be verified before force kill: {reason}"
            ));
        }
        VerifiedProxyProcess::Unknown { pid, reason } => {
            return Err(format!(
                "managed Gateway PID {pid} inspection is unavailable before force kill ({reason}); preserved durable ownership"
            ));
        }
    };

    verify_listener_owner(
        pid,
        port,
        listener_inspector,
        "immediately before force kill",
    )?;
    killer.kill(pid)?;
    Ok(pid)
}

fn stop_when_health_unavailable(
    paths: &ProxyPaths,
    mode: String,
    port: u16,
    pid_record: Option<ProxyPidRecord>,
    killer: &dyn ProcessKiller,
    inspector: &dyn ProcessInspector,
    listener_inspector: &dyn ListenerInspector,
) -> Result<AppStatus, String> {
    let message = match pid_record {
        Some(record) => match verify_proxy_process(&record, paths, port, inspector)? {
            VerifiedProxyProcess::Verified { pid } => {
                verify_listener_owner(
                    pid,
                    port,
                    listener_inspector,
                    "immediately before force kill while health is unavailable",
                )?;
                killer.kill(pid)?;
                confirm_managed_process_stopped(
                    &record,
                    paths,
                    port,
                    inspector,
                    KILL_STOP_TIMEOUT,
                )?;
                remove_pid(paths)?;
                format!("Gateway PID {pid} stopped after health endpoint was unavailable")
            }
            VerifiedProxyProcess::Missing { pid } => {
                remove_pid(paths)?;
                format!("Removed stale Gateway PID {pid}; health endpoint was unavailable")
            }
            VerifiedProxyProcess::Mismatch { pid, reason } => {
                remove_pid(paths)?;
                format!(
                    "Gateway health endpoint was unavailable, but PID {pid} was not killed because ownership could not be verified: {reason}; removed PID file"
                )
            }
            VerifiedProxyProcess::Unknown { pid, reason } => {
                return Err(format!(
                    "managed Gateway PID {pid} inspection is unavailable while health is unavailable ({reason}); preserved durable ownership"
                ));
            }
        },
        None => "Gateway is not running".to_string(),
    };

    Ok(AppStatus {
        mode,
        proxy_running: false,
        proxy_port: port,
        proxy_build: None,
        message,
        gateway_lifecycle: GatewayLifecyclePhase::Stopped,
        history_sync_status: None,
        history_sync_message: None,
    })
}

fn verify_listener_owner(
    pid: u32,
    port: u16,
    listener_inspector: &dyn ListenerInspector,
    action: &str,
) -> Result<(), String> {
    let listener_pid = listener_inspector.listening_pid(port)?;
    if listener_pid == Some(pid) {
        return Ok(());
    }
    let owner = listener_pid
        .map(|value| format!("PID {value}"))
        .unwrap_or_else(|| "no process".to_string());
    Err(format!(
        "exact managed Gateway PID {pid} does not own the current listener on port {port} {action} ({owner}); refusing destructive action and preserving durable ownership"
    ))
}

fn confirm_managed_process_stopped(
    record: &ProxyPidRecord,
    paths: &ProxyPaths,
    port: u16,
    inspector: &dyn ProcessInspector,
    timeout: Duration,
) -> Result<(), String> {
    let deadline = Instant::now() + timeout;
    loop {
        match verify_proxy_process(record, paths, port, inspector)? {
            VerifiedProxyProcess::Missing { .. } => return Ok(()),
            VerifiedProxyProcess::Verified { pid } => {
                if Instant::now() >= deadline {
                    return Err(format!(
                        "Gateway PID {pid} did not exit within {} ms; preserved durable ownership",
                        timeout.as_millis()
                    ));
                }
            }
            VerifiedProxyProcess::Mismatch { pid, reason } => {
                return Err(format!(
                    "Gateway PID {pid} changed identity while confirming termination ({reason}); preserved PID metadata for reconciliation"
                ));
            }
            VerifiedProxyProcess::Unknown { pid, reason } => {
                if Instant::now() >= deadline {
                    return Err(format!(
                        "Gateway PID {pid} could not be inspected while confirming termination ({reason}); preserved durable ownership"
                    ));
                }
            }
        }
        thread::sleep(Duration::from_millis(50));
    }
}

fn build_start_command(
    python: &Path,
    script: &Path,
    paths: &ProxyPaths,
    settings: &Settings,
) -> Command {
    let mut command = Command::new(python);
    let build = build_info::current();
    if build.diagnostics_enabled {
        command
            .arg("-c")
            .arg(DEBUG_DIAGNOSTIC_BOOTSTRAP)
            // Keep the real script path on the child command line so managed
            // PID verification continues to identify the one Gateway process.
            .arg(script)
            .env("CODEXHUB_DIAGNOSTICS_RUNTIME_HOME", paths.codex_dir.clone())
            .env("CODEXHUB_DIAGNOSTICS_BUILD_VERSION", build.semantic_version)
            .env(
                "CODEXHUB_DIAGNOSTICS_SOURCE_REVISION",
                build.source_revision,
            );
    } else {
        command.arg(script);
    }
    command
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
    #[cfg(windows)]
    stop: Arc<AtomicBool>,
}

impl StartupOutputCapture {
    #[cfg(windows)]
    fn capture_reader<R>(&self, reader: R, stream: StartupStream)
    where
        R: Read + AsRawHandle + Send + 'static,
    {
        let buffer = match stream {
            StartupStream::Stdout => Arc::downgrade(&self.stdout),
            StartupStream::Stderr => Arc::downgrade(&self.stderr),
        };
        let stop = Arc::clone(&self.stop);
        let handle = thread::spawn(move || drain_reader_to_buffer(reader, buffer, stop));
        if let Ok(mut handles) = self.handles.lock() {
            handles.push(handle);
        }
    }

    #[cfg(not(windows))]
    fn capture_reader<R>(&self, reader: R, stream: StartupStream)
    where
        R: Read + Send + 'static,
    {
        let buffer = match stream {
            StartupStream::Stdout => Arc::downgrade(&self.stdout),
            StartupStream::Stderr => Arc::downgrade(&self.stderr),
        };
        let handle = thread::spawn(move || drain_reader_to_buffer(reader, buffer));
        if let Ok(mut handles) = self.handles.lock() {
            handles.push(handle);
        }
    }

    fn finish(self) -> StartupOutputSnapshot {
        #[cfg(windows)]
        self.stop.store(true, Ordering::Release);
        self.join_readers();
        self.snapshot()
    }

    #[cfg(windows)]
    fn stop_readers(&self) {
        self.stop.store(true, Ordering::Release);
        self.join_readers();
    }

    fn join_readers(&self) {
        if let Ok(mut handles) = self.handles.lock() {
            for handle in handles.drain(..) {
                let _ = handle.join();
            }
        }
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

#[cfg(windows)]
fn drain_reader_to_buffer<R>(
    mut reader: R,
    buffer: std::sync::Weak<Mutex<BoundedOutput>>,
    stop: Arc<AtomicBool>,
)
where
    R: Read + AsRawHandle,
{
    let mut chunk = [0u8; 1024];
    while let Some(buffer) = buffer.upgrade() {
        let mut available = 0u32;
        let readable = unsafe {
            PeekNamedPipe(
                reader.as_raw_handle() as HANDLE,
                std::ptr::null_mut(),
                0,
                std::ptr::null_mut(),
                &mut available,
                std::ptr::null_mut(),
            )
        };
        if readable == 0 {
            break;
        }
        if available == 0 {
            if stop.load(Ordering::Acquire) {
                break;
            }
            drop(buffer);
            thread::sleep(Duration::from_millis(20));
            continue;
        }
        let count = available.min(chunk.len() as u32) as usize;
        match reader.read(&mut chunk[..count]) {
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

#[cfg(not(windows))]
fn drain_reader_to_buffer<R>(mut reader: R, buffer: std::sync::Weak<Mutex<BoundedOutput>>)
where
    R: Read,
{
    let mut chunk = [0u8; 1024];
    loop {
        match reader.read(&mut chunk) {
            Ok(0) => break,
            Ok(count) => {
                let Some(buffer) = buffer.upgrade() else {
                    break;
                };
                if let Ok(mut buffer) = buffer.lock() {
                    buffer.append(&chunk[..count]);
                }
            }
            Err(_) => break,
        }
    }
}

trait ChildTerminator {
    /// Returns true only when child exit was confirmed within the bounded wait.
    fn terminate(&self, child: &mut Child) -> Result<bool, String>;
}

struct SystemChildTerminator;

impl ChildTerminator for SystemChildTerminator {
    fn terminate(&self, child: &mut Child) -> Result<bool, String> {
        if child
            .try_wait()
            .map_err(|error| format!("failed to inspect Gateway child process: {error}"))?
            .is_some()
        {
            return Ok(true);
        }
        if let Err(error) = child.kill() {
            return match child.try_wait() {
                Ok(Some(_)) => Ok(true),
                Ok(None) => Err(format!("failed to kill Gateway child process: {error}")),
                Err(wait_error) => Err(format!(
                    "failed to kill Gateway child process: {error}; exit inspection also failed: {wait_error}"
                )),
            };
        }

        let deadline = Instant::now() + KILL_STOP_TIMEOUT;
        loop {
            match child.try_wait() {
                Ok(Some(_)) => return Ok(true),
                Ok(None) if Instant::now() < deadline => {
                    thread::sleep(Duration::from_millis(25));
                }
                Ok(None) => return Ok(false),
                Err(error) => {
                    return Err(format!(
                        "failed to confirm Gateway child termination: {error}"
                    ))
                }
            }
        }
    }
}

fn clean_up_failed_start_with_controls(
    paths: &ProxyPaths,
    child: &mut Child,
    output_capture: StartupOutputCapture,
    reason: String,
    record: &ProxyPidRecord,
    inspector: &dyn ProcessInspector,
    terminator: &dyn ChildTerminator,
) -> GatewayStartFailure {
    let termination = terminator.terminate(child);
    let confirmed = matches!(termination, Ok(true));
    let output = if confirmed {
        output_capture.finish()
    } else {
        #[cfg(windows)]
        output_capture.stop_readers();
        output_capture.snapshot()
    };
    let mut message = format_startup_failure(reason, &output);

    if confirmed {
        if let Err(error) = remove_pid(paths) {
            message.push_str(&format!("\nfailed to remove terminated child PID identity: {error}"));
        }
        return GatewayStartFailure {
            message,
            recovery_identity: None,
        };
    }

    match termination {
        Ok(false) => message.push_str("\nspawned child termination could not be confirmed within the bounded cleanup window"),
        Err(error) => message.push_str(&format!(
            "\nspawned child termination could not be confirmed: {error}"
        )),
        Ok(true) => unreachable!(),
    }

    let mut recovery_identity = None;
    match verify_proxy_process(record, paths, record.expected_port(0), inspector) {
        Ok(VerifiedProxyProcess::Verified { pid }) => {
            match write_pid_record(paths, record) {
                Ok(()) => {
                    message.push_str(&format!(
                        "\npreserved durable ownership for verified live Gateway PID {pid}"
                    ));
                    recovery_identity = record.gateway_identity();
                }
                Err(error) => message.push_str(&format!(
                    "\nfailed to persist verified live Gateway ownership: {error}"
                )),
            }
        }
        Ok(VerifiedProxyProcess::Missing { .. }) => {
            if let Err(error) = remove_pid(paths) {
                message.push_str(&format!("\nfailed to remove exited child PID identity: {error}"));
            }
        }
        Ok(VerifiedProxyProcess::Mismatch { pid, reason }) => message.push_str(&format!(
            "\nPID {pid} could not be durably claimed after failed cleanup because ownership changed: {reason}"
        )),
        Ok(VerifiedProxyProcess::Unknown { pid, reason }) => {
            message.push_str(&format!(
                "\nprocess inspection unavailable for spawned Gateway PID {pid}: {reason}"
            ));
            match write_pid_record(paths, record) {
                Ok(()) => recovery_identity = record.gateway_identity(),
                Err(error) => message.push_str(&format!(
                    "\nfailed to persist spawn-known Gateway recovery ownership: {error}"
                )),
            }
        }
        Err(error) => message.push_str(&format!(
            "\nfailed to reconcile child ownership after bounded cleanup: {error}"
        )),
    }
    GatewayStartFailure {
        message,
        recovery_identity,
    }
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

    fn spawned_process_start_id(&self, child: &Child) -> Result<String, String> {
        child_process_start_id(child)
    }
}

trait ListenerInspector {
    fn listening_pid(&self, port: u16) -> Result<Option<u32>, String>;
}

#[derive(Debug, Clone, PartialEq, Eq)]
enum InspectedProcess {
    Missing,
    Running(ProcessInfo),
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct ProcessInfo {
    args: Vec<String>,
    process_start_id: Option<String>,
}

impl ProcessInfo {
    #[cfg(windows)]
    fn from_command_line_with_start_id(
        command_line: String,
        process_start_id: Option<String>,
    ) -> Self {
        Self {
            args: split_command_line(&command_line),
            process_start_id,
        }
    }

    #[cfg(any(test, not(windows)))]
    fn from_args(args: Vec<String>) -> Self {
        Self {
            args,
            process_start_id: None,
        }
    }
}

#[derive(Debug, PartialEq, Eq)]
enum VerifiedProxyProcess {
    Verified { pid: u32 },
    Missing { pid: u32 },
    Mismatch { pid: u32, reason: String },
    Unknown { pid: u32, reason: String },
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

struct SystemListenerInspector;

impl ListenerInspector for SystemListenerInspector {
    fn listening_pid(&self, port: u16) -> Result<Option<u32>, String> {
        inspect_listener_pid(port)
    }
}

fn verify_proxy_process(
    record: &ProxyPidRecord,
    paths: &ProxyPaths,
    settings_port: u16,
    inspector: &dyn ProcessInspector,
) -> Result<VerifiedProxyProcess, String> {
    let pid = record.pid();
    if matches!(record, ProxyPidRecord::Managed(metadata) if metadata.recovery) {
        return Ok(VerifiedProxyProcess::Unknown {
            pid,
            reason: "spawn-known recovery ownership is pending health, process, and listener activation"
                .to_string(),
        });
    }
    let process = match inspector.inspect(pid) {
        Ok(process) => process,
        Err(error) => {
            return Ok(VerifiedProxyProcess::Unknown {
                pid,
                reason: format!("process command line inspection failed: {error}"),
            });
        }
    };

    let InspectedProcess::Running(info) = process else {
        return Ok(VerifiedProxyProcess::Missing { pid });
    };

    if let Err(reason) = verify_proxy_command_shape(record, paths, settings_port, &info) {
        return Ok(VerifiedProxyProcess::Mismatch { pid, reason });
    }

    let Some(expected_start_id) = record.process_start_id() else {
        return Ok(VerifiedProxyProcess::Unknown {
            pid,
            reason: "durable PID metadata has no process creation identity; refusing destructive ownership claims"
                .to_string(),
        });
    };
    if info.process_start_id.as_ref() != Some(expected_start_id) {
        return Ok(VerifiedProxyProcess::Mismatch {
            pid,
            reason: format!(
                "process start identity differs from managed PID metadata (expected {expected_start_id:?}, found {:?})",
                info.process_start_id
            ),
        });
    }
    Ok(VerifiedProxyProcess::Verified { pid })
}

#[cfg(test)]
fn verify_proxy_command_line(
    record: &ProxyPidRecord,
    paths: &ProxyPaths,
    settings_port: u16,
    info: &ProcessInfo,
) -> Result<(), String> {
    verify_proxy_command_shape(record, paths, settings_port, info)?;
    let Some(expected_start_id) = record.process_start_id() else {
        return Err(
            "durable PID metadata has no process creation identity; ownership is not destructively verifiable"
                .to_string(),
        );
    };
    if info.process_start_id.as_ref() != Some(expected_start_id) {
        return Err(format!(
            "process start identity differs from managed PID metadata (expected {expected_start_id:?}, found {:?})",
            info.process_start_id
        ));
    }
    Ok(())
}

fn verify_proxy_command_shape(
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
}

fn command_line_has_script_path(info: &ProcessInfo, expected_script: &str) -> bool {
    let expected = normalized_path_text(expected_script);
    info.args
        .iter()
        .any(|arg| normalized_path_text(arg) == expected)
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
    health_with_timeout(port, HEALTH_TIMEOUT)
}

fn health_with_timeout(port: u16, timeout: Duration) -> Result<Option<HealthResponse>, String> {
    let client = reqwest::blocking::Client::builder()
        .timeout(timeout)
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
    request_shutdown_with_timeout(port, gateway_client_key, SHUTDOWN_TIMEOUT)
}

fn request_shutdown_with_timeout(
    port: u16,
    gateway_client_key: &str,
    timeout: Duration,
) -> Result<(), String> {
    let client = reqwest::blocking::Client::builder()
        .timeout(timeout)
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
            .map_err(|error| format!("failed to poll Gateway child process: {error}"))?
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
                "failed to read Gateway PID file {}: {error}",
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
            format!("invalid Gateway PID metadata in {}: {error}", path.display())
        })?;
        if metadata.version != PID_FILE_VERSION && metadata.version != LEGACY_PID_FILE_VERSION {
            return Err(format!(
                "unsupported Gateway PID metadata version {} in {}",
                metadata.version,
                path.display()
            ));
        }
        if metadata.version == PID_FILE_VERSION
            && !metadata.recovery
            && metadata.process_start_id.as_deref().is_none_or(str::is_empty)
        {
            return Err(format!(
                "managed Gateway PID metadata version {PID_FILE_VERSION} in {} is missing the required process creation identity",
                path.display()
            ));
        }
        return Ok(Some(ProxyPidRecord::Managed(metadata)));
    }

    trimmed
        .parse::<u32>()
        .map(|pid| Some(ProxyPidRecord::Legacy(pid)))
        .map_err(|error| format!("invalid Gateway PID in {}: {error}", path.display()))
}

#[cfg(test)]
fn write_pid(paths: &ProxyPaths, pid: u32, port: u16, script: &Path) -> Result<(), String> {
    let metadata = ProxyPidMetadata::new(pid, port, script, test_process_start_id(pid));
    write_pid_record(paths, &ProxyPidRecord::Managed(metadata))
}

#[cfg(test)]
fn test_process_start_id(pid: u32) -> String {
    format!("test-process-start-{pid}")
}

fn write_pid_record(paths: &ProxyPaths, record: &ProxyPidRecord) -> Result<(), String> {
    fs::create_dir_all(paths.proxy_dir()).map_err(|error| {
        format!(
            "failed to create Gateway runtime directory {}: {error}",
            paths.proxy_dir().display()
        )
    })?;
    let ProxyPidRecord::Managed(metadata) = record else {
        return Err("refusing to publish legacy PID metadata for a new Gateway".to_string());
    };
    let text = serde_json::to_string_pretty(&metadata)
        .map_err(|error| format!("failed to encode Gateway PID metadata: {error}"))?;
    safe_file::write_text_atomic(&paths.pid_path(), &format!("{text}\n")).map_err(|error| {
        format!(
            "failed to write Gateway PID file {}: {error}",
            paths.pid_path().display()
        )
    })
}

fn remove_pid(paths: &ProxyPaths) -> Result<(), String> {
    match fs::remove_file(paths.pid_path()) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
        Err(error) => Err(format!(
            "failed to remove Gateway PID file {}: {error}",
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
#[derive(Clone, Copy)]
enum WindowsInspectionKind {
    Process,
    #[cfg(test)]
    Listener,
}

#[cfg(windows)]
impl WindowsInspectionKind {
    fn timeout_error(self) -> &'static str {
        match self {
            Self::Process => "Gateway process inspection timed out",
            #[cfg(test)]
            Self::Listener => "Gateway listener inspection timed out",
        }
    }

    fn start_error(self) -> &'static str {
        match self {
            Self::Process => "failed to start Gateway process inspection",
            #[cfg(test)]
            Self::Listener => "failed to start Gateway listener inspection",
        }
    }

    fn stop_error(self) -> &'static str {
        match self {
            Self::Process => "failed to stop Gateway process inspection",
            #[cfg(test)]
            Self::Listener => "failed to stop Gateway listener inspection",
        }
    }

    fn wait_error(self) -> &'static str {
        match self {
            Self::Process => "failed to wait for Gateway process inspection",
            #[cfg(test)]
            Self::Listener => "failed to wait for Gateway listener inspection",
        }
    }
}

#[cfg(windows)]
fn run_windows_inspection(
    script: &str,
    kind: WindowsInspectionKind,
    timeout: Duration,
) -> Result<std::process::Output, String> {
    let mut command = Command::new("powershell");
    command.args(["-NoProfile", "-Command", script]);
    configure_no_window(&mut command);
    run_bounded_inspection_command(command, Instant::now() + timeout, kind)
}

#[cfg(windows)]
struct WindowsInspectionJob(HANDLE);

#[cfg(windows)]
impl WindowsInspectionJob {
    fn new(kind: WindowsInspectionKind) -> Result<Self, String> {
        let handle = unsafe { CreateJobObjectW(std::ptr::null(), std::ptr::null()) };
        if handle.is_null() {
            return Err(kind.start_error().to_string());
        }

        let mut limits: JOBOBJECT_EXTENDED_LIMIT_INFORMATION = unsafe { std::mem::zeroed() };
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
        let configured = unsafe {
            SetInformationJobObject(
                handle,
                JobObjectExtendedLimitInformation,
                std::ptr::addr_of!(limits).cast(),
                std::mem::size_of_val(&limits) as u32,
            )
        };
        if configured == 0 {
            unsafe { CloseHandle(handle) };
            return Err(kind.start_error().to_string());
        }
        Ok(Self(handle))
    }

    fn assign(&self, child: &Child, kind: WindowsInspectionKind) -> Result<(), String> {
        let assigned = unsafe {
            AssignProcessToJobObject(self.0, child.as_raw_handle() as HANDLE)
        };
        if assigned == 0 {
            Err(kind.start_error().to_string())
        } else {
            Ok(())
        }
    }

    fn terminate(&self, kind: WindowsInspectionKind) -> Result<(), String> {
        if unsafe { TerminateJobObject(self.0, 1) } == 0 {
            Err(kind.stop_error().to_string())
        } else {
            Ok(())
        }
    }
}

#[cfg(windows)]
impl Drop for WindowsInspectionJob {
    fn drop(&mut self) {
        unsafe { CloseHandle(self.0) };
    }
}

#[cfg(windows)]
fn inspection_output_file(kind: WindowsInspectionKind) -> Result<fs::File, String> {
    let mut options = fs::OpenOptions::new();
    options
        .read(true)
        .write(true)
        .create_new(true)
        .share_mode(FILE_SHARE_DELETE)
        .custom_flags(FILE_ATTRIBUTE_TEMPORARY | FILE_FLAG_DELETE_ON_CLOSE);

    for attempt in 0..32u32 {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default()
            .as_nanos();
        let path = std::env::temp_dir().join(format!(
            "codexhub-inspection-{}-{nonce}-{attempt}.tmp",
            std::process::id()
        ));
        match options.open(path) {
            Ok(file) => return Ok(file),
            Err(error) if error.kind() == io::ErrorKind::AlreadyExists => continue,
            Err(_) => return Err(kind.start_error().to_string()),
        }
    }
    Err(kind.start_error().to_string())
}

#[cfg(windows)]
fn read_inspection_output(mut file: fs::File) -> Vec<u8> {
    if file.seek(SeekFrom::Start(0)).is_err() {
        return Vec::new();
    }
    let mut bytes = Vec::new();
    let _ = file
        .take((WINDOWS_INSPECTION_OUTPUT_LIMIT + 1) as u64)
        .read_to_end(&mut bytes);
    bytes.truncate(WINDOWS_INSPECTION_OUTPUT_LIMIT);
    bytes
}

#[cfg(windows)]
fn resume_suspended_inspection_child(
    child: &Child,
    kind: WindowsInspectionKind,
) -> Result<(), String> {
    let snapshot = unsafe { CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0) };
    if snapshot == INVALID_HANDLE_VALUE {
        return Err(kind.start_error().to_string());
    }

    let mut entry: THREADENTRY32 = unsafe { std::mem::zeroed() };
    entry.dwSize = std::mem::size_of::<THREADENTRY32>() as u32;
    let mut found = unsafe { Thread32First(snapshot, &mut entry) } != 0;
    let mut thread_id = None;
    while found {
        if entry.th32OwnerProcessID == child.id() {
            thread_id = Some(entry.th32ThreadID);
            break;
        }
        found = unsafe { Thread32Next(snapshot, &mut entry) } != 0;
    }
    unsafe { CloseHandle(snapshot) };

    let Some(thread_id) = thread_id else {
        return Err(kind.start_error().to_string());
    };
    let thread = unsafe { OpenThread(THREAD_SUSPEND_RESUME, 0, thread_id) };
    if thread.is_null() {
        return Err(kind.start_error().to_string());
    }
    let resumed = unsafe { ResumeThread(thread) };
    unsafe { CloseHandle(thread) };
    if resumed == u32::MAX {
        Err(kind.start_error().to_string())
    } else {
        Ok(())
    }
}

#[cfg(windows)]
fn stop_and_reap_inspection_child(
    child: &mut Child,
    job: &WindowsInspectionJob,
    deadline: Instant,
    kind: WindowsInspectionKind,
) -> Result<(), String> {
    job.terminate(kind)?;
    loop {
        match child.try_wait() {
            Ok(Some(_)) => return Ok(()),
            Ok(None) if Instant::now() < deadline => thread::sleep(
                Duration::from_millis(20)
                    .min(deadline.saturating_duration_since(Instant::now())),
            ),
            Ok(None) => return Ok(()),
            Err(_) => return Err(kind.stop_error().to_string()),
        }
    }
}

#[cfg(windows)]
fn run_bounded_inspection_command(
    mut command: Command,
    deadline: Instant,
    kind: WindowsInspectionKind,
) -> Result<std::process::Output, String> {
    let stdout = inspection_output_file(kind)?;
    let stderr = inspection_output_file(kind)?;
    command.stdout(Stdio::from(
        stdout
            .try_clone()
            .map_err(|_| kind.start_error().to_string())?,
    ));
    command.stderr(Stdio::from(
        stderr
            .try_clone()
            .map_err(|_| kind.start_error().to_string())?,
    ));

    let job = WindowsInspectionJob::new(kind)?;
    const CREATE_NO_WINDOW: u32 = 0x0800_0000;
    const CREATE_SUSPENDED: u32 = 0x0000_0004;
    command.creation_flags(CREATE_NO_WINDOW | CREATE_SUSPENDED);
    let mut child = command.spawn().map_err(|_| kind.start_error().to_string())?;
    if let Err(error) = job.assign(&child, kind) {
        let _ = child.kill();
        let _ = child.wait();
        return Err(error);
    }
    if let Err(error) = resume_suspended_inspection_child(&child, kind) {
        let _ = stop_and_reap_inspection_child(&mut child, &job, deadline, kind);
        return Err(error);
    }

    let remaining = deadline.saturating_duration_since(Instant::now());
    let cleanup_budget = (remaining / 4).min(Duration::from_millis(250));
    let inspection_deadline = deadline.checked_sub(cleanup_budget).unwrap_or(deadline);
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break status,
            Ok(None) if Instant::now() < inspection_deadline => {
                thread::sleep(Duration::from_millis(20).min(
                    inspection_deadline.saturating_duration_since(Instant::now()),
                ))
            }
            Ok(None) => {
                stop_and_reap_inspection_child(&mut child, &job, deadline, kind)?;
                return Err(kind.timeout_error().to_string());
            }
            Err(_) => {
                stop_and_reap_inspection_child(&mut child, &job, deadline, kind)?;
                return Err(kind.wait_error().to_string());
            }
        }
    };

    Ok(std::process::Output {
        status,
        stdout: read_inspection_output(stdout),
        stderr: read_inspection_output(stderr),
    })
}

#[cfg(windows)]
fn windows_filetime_start_id(filetime: FILETIME) -> String {
    let ticks = ((filetime.dwHighDateTime as u64) << 32) | filetime.dwLowDateTime as u64;
    format!("windows-filetime-ms:{}", ticks / 10_000)
}

#[cfg(windows)]
fn process_start_id_from_handle(handle: HANDLE) -> Result<String, String> {
    let mut creation: FILETIME = unsafe { std::mem::zeroed() };
    let mut exit: FILETIME = unsafe { std::mem::zeroed() };
    let mut kernel: FILETIME = unsafe { std::mem::zeroed() };
    let mut user: FILETIME = unsafe { std::mem::zeroed() };
    if unsafe { GetProcessTimes(handle, &mut creation, &mut exit, &mut kernel, &mut user) } == 0 {
        return Err("failed to read Gateway process creation identity".to_string());
    }
    Ok(windows_filetime_start_id(creation))
}

#[cfg(windows)]
fn child_process_start_id(child: &Child) -> Result<String, String> {
    process_start_id_from_handle(child.as_raw_handle() as HANDLE)
}

#[cfg(not(windows))]
fn child_process_start_id(child: &Child) -> Result<String, String> {
    match inspect_process(child.id())? {
        InspectedProcess::Running(info) => info
            .process_start_id
            .ok_or_else(|| "spawned Gateway process has no process creation identity".to_string()),
        InspectedProcess::Missing => Err("spawned Gateway process disappeared".to_string()),
    }
}

#[cfg(windows)]
fn process_start_id_for_pid(pid: u32) -> Result<String, String> {
    let handle = unsafe { OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid) };
    if handle.is_null() {
        return Err("failed to open Gateway process for creation identity".to_string());
    }
    let result = process_start_id_from_handle(handle);
    unsafe { CloseHandle(handle) };
    result
}

#[cfg(windows)]
fn inspect_listener_pid(port: u16) -> Result<Option<u32>, String> {
    const AF_INET: u32 = 2;
    const LOOPBACK_NETWORK_ORDER: u32 = 0x0100_007f;
    let mut size = 0u32;
    loop {
        let sizing = unsafe {
            GetExtendedTcpTable(
                std::ptr::null_mut(),
                &mut size,
                0,
                AF_INET,
                TCP_TABLE_OWNER_PID_LISTENER,
                0,
            )
        };
        if sizing == NO_ERROR {
            return Ok(None);
        }
        if sizing != ERROR_INSUFFICIENT_BUFFER {
            return Err(format!(
                "failed to size the Windows TCP listener table: error {sizing}"
            ));
        }
        if size < std::mem::size_of::<u32>() as u32 {
            return Ok(None);
        }

        let mut buffer = vec![0u8; size as usize];
        let status = unsafe {
            GetExtendedTcpTable(
                buffer.as_mut_ptr().cast(),
                &mut size,
                0,
                AF_INET,
                TCP_TABLE_OWNER_PID_LISTENER,
                0,
            )
        };
        if status == ERROR_INSUFFICIENT_BUFFER {
            continue;
        }
        if status != NO_ERROR {
            return Err(format!(
                "failed to inspect the Windows TCP listener table: error {status}"
            ));
        }

        let table = unsafe { &*(buffer.as_ptr().cast::<MIB_TCPTABLE_OWNER_PID>()) };
        let max_entries = (buffer.len() - std::mem::size_of::<u32>())
            / std::mem::size_of::<MIB_TCPROW_OWNER_PID>();
        let num_entries = (table.dwNumEntries as usize).min(max_entries);
        let rows = unsafe {
            std::slice::from_raw_parts(table.table.as_ptr(), num_entries)
        };
        let mut owners = rows
            .iter()
            .filter(|row: &&MIB_TCPROW_OWNER_PID| {
                row.dwLocalAddr == LOOPBACK_NETWORK_ORDER
                    && (row.dwLocalPort as u16).to_be() == port
            })
            .map(|row| row.dwOwningPid)
            .collect::<Vec<_>>();
        owners.sort_unstable();
        owners.dedup();
        return match owners.as_slice() {
            [] => Ok(None),
            [pid] => Ok(Some(*pid)),
            _ => Err(format!(
                "multiple TCP listener owners were reported for 127.0.0.1:{port}: {owners:?}"
            )),
        };
    }
}

#[cfg(not(windows))]
fn inspect_listener_pid(port: u16) -> Result<Option<u32>, String> {
    let output = match Command::new("lsof")
        .args(["-nP", &format!("-iTCP:{port}"), "-sTCP:LISTEN", "-t"])
        .output()
    {
        Ok(output) => output,
        Err(error) if error.kind() == io::ErrorKind::NotFound => {
            return Err(
                "listener ownership reconciliation requires lsof on this platform".to_string(),
            )
        }
        Err(error) => {
            return Err(format!(
                "failed to inspect listener on port {port}: {error}"
            ))
        }
    };
    if !output.status.success() && output.stdout.is_empty() {
        return Ok(None);
    }
    let mut pids = String::from_utf8_lossy(&output.stdout)
        .lines()
        .filter_map(|line| line.trim().parse::<u32>().ok())
        .collect::<Vec<_>>();
    pids.sort_unstable();
    pids.dedup();
    match pids.as_slice() {
        [] => Ok(None),
        [pid] => Ok(Some(*pid)),
        _ => Err(format!(
            "multiple TCP listener owners were reported for 127.0.0.1:{port}: {pids:?}"
        )),
    }
}

#[cfg(windows)]
fn inspect_process(pid: u32) -> Result<InspectedProcess, String> {
    let script = format!(
        "$p = Get-CimInstance -ClassName Win32_Process -Filter 'ProcessId = {pid}' -ErrorAction SilentlyContinue; \
         if ($null -eq $p) {{ exit 3 }}; \
         [Console]::Out.Write((@{{ command_line = [string]$p.CommandLine }} | ConvertTo-Json -Compress))"
    );
    let output = run_windows_inspection(
        &script,
        WindowsInspectionKind::Process,
        WINDOWS_INSPECTION_TIMEOUT,
    )?;

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

    #[derive(Deserialize)]
    struct WindowsProcessInspection {
        command_line: String,
    }
    let inspection: WindowsProcessInspection =
        serde_json::from_slice(&output.stdout).map_err(|error| {
            format!("failed to parse process identity for PID {pid}: {error}")
        })?;
    let process_start_id = process_start_id_for_pid(pid)?;
    Ok(InspectedProcess::Running(
        ProcessInfo::from_command_line_with_start_id(
            inspection.command_line,
            Some(process_start_id),
        ),
    ))
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
    let process_start_id = fs::read_to_string(format!("/proc/{pid}/stat"))
        .ok()
        .and_then(|stat| process_start_ticks(&stat))
        .map(|ticks| format!("proc-start-ticks:{ticks}"));
    let mut info = ProcessInfo::from_args(args);
    info.process_start_id = process_start_id;
    Ok(InspectedProcess::Running(info))
}

#[cfg(not(windows))]
fn process_start_ticks(stat: &str) -> Option<&str> {
    let after_command = stat.rsplit_once(')')?.1.trim();
    after_command.split_whitespace().nth(19)
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
        build_start_command, capture_child_stdio, clean_up_failed_start_with_controls,
        comparable_path, configure_start_stdio, detect_mode, find_python,
        force_kill_after_graceful_timeout, kill_process, read_pid, read_pid_record,
        reconciled_snapshot_with_controls,
        start_with_paths, start_with_paths_and_controls, start_with_paths_and_waiter,
        status_with_paths, stop_session_owned_with_paths_and_controls, stop_with_paths,
        stop_with_paths_and_controls, verify_proxy_command_line, GatewayIdentity, ShutdownClock,
        UserRequestedShutdownControls,
        write_pid, ChildTerminator, InspectedProcess, ListenerInspector, ProcessInfo,
        ProcessInspector, ProcessKiller, ProxyLifecycleBackend, ProxyPaths, ProxyPidMetadata,
        ProxyPidRecord, StartupOutcome, VerifiedProxyProcess, DEBUG_DIAGNOSTIC_BOOTSTRAP,
    };
    #[cfg(windows)]
    use super::{
        run_bounded_inspection_command, run_windows_inspection, WindowsInspectionKind,
    };
    use crate::Settings;
    use std::cell::RefCell;
    use std::fs;
    use std::io::Write;
    use std::net::TcpListener;
    use std::path::{Path, PathBuf};
    use std::process::Command;
    #[cfg(windows)]
    use std::process::Stdio;
    use std::collections::VecDeque;
    use std::sync::{
        atomic::{AtomicBool, AtomicU32, Ordering},
        Arc,
    };
    use std::thread;
    use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

    fn wait_for_missing_process(pid: u32) {
        // On Windows the inspection shells out to PowerShell/CIM, which can
        // itself stall under parallel test load; retry instead of failing on
        // one slow attempt.
        let deadline = Instant::now() + Duration::from_secs(30);
        let mut last = String::from("inspection did not run");
        while Instant::now() < deadline {
            match super::inspect_process(pid) {
                Ok(InspectedProcess::Missing) => return,
                Ok(InspectedProcess::Running(_)) => {
                    last = format!("PID {pid} is still running");
                }
                Err(error) => last = error,
            }
            thread::sleep(Duration::from_millis(200));
        }
        panic!("process {pid} did not disappear within the bounded window: {last}");
    }

    #[cfg(windows)]
    #[test]
    fn bounded_inspection_command_kills_and_reaps_a_hung_child() {
        let sentinel = "private-inspection-sentinel";
        let mut command = Command::new("powershell");
        command.args([
            "-NoProfile",
            "-Command",
            &format!(
                "$PID | Set-Content -NoNewline -Path $env:CODEXHUB_TEST_PID_PATH; Write-Error '{sentinel}'; Start-Sleep -Seconds 60"
            ),
        ]);
        let root = temp_root("bounded-inspection-command");
        let pid_path = root.join("helper.pid");
        command.env("CODEXHUB_TEST_PID_PATH", &pid_path);
        command.stdout(Stdio::piped()).stderr(Stdio::piped());
        super::configure_no_window(&mut command);
        // Generous deadline: PowerShell cold start under parallel test load can
        // take several seconds and the helper must write its PID first. The
        // timeout path still triggers because the helper sleeps far longer.
        let deadline = Instant::now() + Duration::from_secs(15);

        let error =
            run_bounded_inspection_command(command, deadline, WindowsInspectionKind::Process)
                .expect_err("hung inspection must time out");

        assert_eq!(error, "Gateway process inspection timed out");
        assert!(!error.contains(sentinel));
        let pid = fs::read_to_string(&pid_path)
            .expect("helper PID")
            .parse::<u32>()
            .expect("numeric helper PID");
        wait_for_missing_process(pid);
    }

    #[cfg(windows)]
    #[test]
    fn inspection_returns_when_a_descendant_inherits_output_handles() {
        let root = temp_root("inspection-descendant");
        let descendant_pid_path = root.join("descendant.pid");
        let escaped_path = descendant_pid_path
            .to_string_lossy()
            .replace(char::from(39), "''");
        let script = format!(
            "$child = Start-Process powershell -ArgumentList '-NoProfile','-Command','Start-Sleep -Seconds 60' -NoNewWindow -PassThru; $child.Id | Set-Content -NoNewline -Path '{escaped_path}'; [Console]::Out.Write('parent-exited')"
        );
        let (result_tx, result_rx) = std::sync::mpsc::channel();
        let worker = thread::spawn(move || {
            // Generous timeout: the parent chains two PowerShell cold starts
            // (itself plus Start-Process), which can take well over five
            // seconds under parallel test load.
            let result = run_windows_inspection(
                &script,
                WindowsInspectionKind::Process,
                Duration::from_secs(20),
            );
            let _ = result_tx.send(result);
        });

        let output = result_rx
            .recv_timeout(Duration::from_secs(25))
            .expect("inspection must not wait for a descendant-held output pipe")
            .expect("parent inspection should exit successfully");
        assert_eq!(String::from_utf8_lossy(&output.stdout), "parent-exited");
        worker.join().expect("inspection worker joins");
        let descendant_pid = fs::read_to_string(descendant_pid_path)
            .expect("descendant PID")
            .parse::<u32>()
            .expect("numeric descendant PID");
        wait_for_missing_process(descendant_pid);
    }

    #[cfg(windows)]
    #[test]
    fn process_and_listener_inspection_timeouts_are_sanitized() {
        for (kind, expected) in [
            (
                WindowsInspectionKind::Process,
                "Gateway process inspection timed out",
            ),
            (
                WindowsInspectionKind::Listener,
                "Gateway listener inspection timed out",
            ),
        ] {
            let sentinel = "private-command-line-sentinel";
            let error = run_windows_inspection(
                &format!("Write-Error '{sentinel}'; Start-Sleep -Seconds 5"),
                kind,
                Duration::from_millis(100),
            )
            .expect_err("hung PowerShell inspection must time out");

            assert_eq!(error, expected);
            assert!(!error.contains(sentinel));
        }
    }

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
    fn reconciled_snapshot_requires_matching_health_pid_process_and_listener_identity() {
        let root = temp_root("reconciled-snapshot");
        let paths = test_paths(&root);
        let port = free_port();
        let pid = 12_345;
        write_settings(&paths, port);
        write_fake_proxy_script(&paths, "print('test')");
        write_pid(&paths, pid, port, &paths.proxy_script_path()).expect("write PID metadata");
        let inspector = RecordingInspector::new(fake_proxy_process(&paths, port));
        let listener = FixedListenerInspector::new(Some(pid));

        let snapshot = reconciled_snapshot_with_controls(
            &paths,
            &|_| Ok(Some(healthy_response())),
            &inspector,
            &listener,
        )
        .expect("reconciled snapshot");

        assert!(snapshot.status.proxy_running);
        assert_eq!(
            snapshot.identity.as_ref().map(|identity| identity.pid),
            Some(pid)
        );
        assert_eq!(
            snapshot.status.message,
            format!("Gateway running with PID {pid}")
        );
    }

    #[test]
    fn reconciled_snapshot_upgrades_spawn_known_recovery_after_identity_returns() {
        let root = temp_root("reconciled-recovery-upgrade");
        let paths = test_paths(&root);
        let port = free_port();
        let pid = 12_345;
        write_settings(&paths, port);
        write_fake_proxy_script(&paths, "print('test')");
        let recovery = ProxyPidRecord::Managed(ProxyPidMetadata::recovery(
            pid,
            port,
            &paths.proxy_script_path(),
        ));
        super::write_pid_record(&paths, &recovery).expect("write recovery PID metadata");
        let inspector = RecordingInspector::new(fake_proxy_process(&paths, port));
        let listener = FixedListenerInspector::new(Some(pid));

        let snapshot = reconciled_snapshot_with_controls(
            &paths,
            &|_| Ok(Some(healthy_response())),
            &inspector,
            &listener,
        )
        .expect("recovery identity should be upgraded");

        assert!(snapshot.status.proxy_running);
        let identity = snapshot.identity.expect("upgraded identity");
        assert_eq!(identity.pid, pid);
        assert_eq!(identity.process_start_id, Some(super::test_process_start_id(pid)));
        let ProxyPidRecord::Managed(metadata) = read_pid_record(&paths)
            .expect("read upgraded PID metadata")
            .expect("upgraded PID metadata")
        else {
            panic!("expected managed PID metadata");
        };
        assert!(!metadata.recovery);
        assert_eq!(metadata.process_start_id, identity.process_start_id);
    }

    #[test]
    fn reconciled_snapshot_does_not_activate_recovery_when_listener_differs() {
        let root = temp_root("recovery-listener-mismatch");
        let paths = test_paths(&root);
        let port = free_port();
        let pid = 12_345;
        write_settings(&paths, port);
        write_fake_proxy_script(&paths, "print('test')");
        let recovery = ProxyPidRecord::Managed(ProxyPidMetadata::recovery(
            pid,
            port,
            &paths.proxy_script_path(),
        ));
        super::write_pid_record(&paths, &recovery).expect("write recovery PID metadata");

        let error = reconciled_snapshot_with_controls(
            &paths,
            &|_| Ok(Some(healthy_response())),
            &RecordingInspector::new(fake_proxy_process(&paths, port)),
            &FixedListenerInspector::new(Some(54_321)),
        )
        .expect_err("listener mismatch must block recovery activation");

        assert!(error.contains("listener PID 54321"));
        let ProxyPidRecord::Managed(metadata) = read_pid_record(&paths)
            .expect("read recovery PID metadata")
            .expect("recovery PID metadata")
        else {
            panic!("expected managed PID metadata");
        };
        assert!(metadata.recovery);
        assert_eq!(metadata.process_start_id, None);
    }

    #[test]
    fn reconciled_snapshot_preserves_exact_managed_pid_when_listener_differs() {
        let root = temp_root("reconciled-listener-mismatch");
        let paths = test_paths(&root);
        let port = free_port();
        write_settings(&paths, port);
        write_fake_proxy_script(&paths, "print('test')");
        write_pid(&paths, 12_345, port, &paths.proxy_script_path()).expect("write PID metadata");
        let inspector = RecordingInspector::new(fake_proxy_process(&paths, port));
        let listener = FixedListenerInspector::new(Some(54_321));

        let error = reconciled_snapshot_with_controls(
            &paths,
            &|_| Ok(Some(healthy_response())),
            &inspector,
            &listener,
        )
        .expect_err("mismatched listener must not publish Running");

        assert!(error.contains("listener PID 54321"));
        assert!(error.contains("managed PID 12345"));
        assert_eq!(read_pid(&paths).expect("read PID"), Some(12_345));
    }

    #[test]
    fn start_does_not_publish_pid_until_health_and_listener_reconcile() {
        let root = temp_root("late-pid-publication");
        let paths = test_paths(&root);
        let port = free_port();
        write_settings(&paths, port);
        write_fake_proxy_script(&paths, "import time\ntime.sleep(30)");
        let listener_pid = Arc::new(AtomicU32::new(0));
        let listener = AtomicListenerInspector::new(Arc::clone(&listener_pid));
        let inspector = RecordingInspector::new(fake_proxy_process(&paths, port));
        let health_calls = std::cell::Cell::new(0usize);

        let outcome = start_with_paths_and_controls(
            &paths,
            Duration::from_secs(1),
            Duration::ZERO,
            &|_| {
                let call = health_calls.get();
                health_calls.set(call + 1);
                Ok((call > 0).then(healthy_response))
            },
            &inspector,
            &listener,
            |child, _, _, _, _, _| {
                assert_eq!(read_pid(&paths).expect("read unpublished PID"), None);
                listener_pid.store(child.id(), Ordering::SeqCst);
                Ok(StartupOutcome::Healthy(healthy_response()))
            },
        )
        .expect("start after reconciliation");

        let identity = outcome.snapshot.identity.expect("published identity");
        assert!(outcome.spawned);
        assert_eq!(read_pid(&paths).expect("read PID"), Some(identity.pid));
        assert_eq!(identity.pid, listener_pid.load(Ordering::SeqCst));
        kill_process(identity.pid).expect("clean up test child");
        let _ = super::remove_pid(&paths);
    }

    #[cfg(windows)]
    #[test]
    fn post_spawn_inspection_timeout_cleans_up_gateway_without_leaking_output() {
        let root = temp_root("post-spawn-inspection-timeout");
        let paths = test_paths(&root);
        let port = free_port();
        write_settings(&paths, port);
        write_fake_proxy_script(&paths, "import time\ntime.sleep(30)");
        let spawned_pid = Arc::new(AtomicU32::new(0));
        let health_calls = std::cell::Cell::new(0usize);
        let started = Instant::now();

        let error = start_with_paths_and_controls(
            &paths,
            Duration::from_secs(1),
            Duration::ZERO,
            &|_| {
                let call = health_calls.get();
                health_calls.set(call + 1);
                Ok((call > 0).then(healthy_response))
            },
            &TimeoutAfterSpawnInspector,
            &FixedListenerInspector::new(None),
            |child, _, _, _, _, _| {
                spawned_pid.store(child.id(), Ordering::SeqCst);
                Ok(StartupOutcome::Healthy(healthy_response()))
            },
        )
        .expect_err("post-spawn process inspection must time out");

        assert!(started.elapsed() < Duration::from_secs(3));
        assert!(error.message.contains("Gateway process inspection timed out"));
        assert!(!error.message.contains("private-post-spawn-sentinel"));
        assert_eq!(read_pid(&paths).expect("PID removed"), None);
        let pid = spawned_pid.load(Ordering::SeqCst);
        let _cleanup = PidCleanup::new(pid);
        wait_for_missing_process(pid);
    }

    #[test]
    fn start_listener_mismatch_terminates_spawned_child_and_removes_pid() {
        let root = temp_root("spawned-listener-mismatch");
        let paths = test_paths(&root);
        let port = free_port();
        write_settings(&paths, port);
        write_fake_proxy_script(&paths, "import time\ntime.sleep(30)");
        let spawned_pid = Arc::new(AtomicU32::new(0));
        let inspector = RecordingInspector::new(fake_proxy_process(&paths, port));
        let listener = FixedListenerInspector::new(Some(999_999));
        let health_calls = std::cell::Cell::new(0usize);

        let error = start_with_paths_and_controls(
            &paths,
            Duration::from_secs(1),
            Duration::ZERO,
            &|_| {
                let call = health_calls.get();
                health_calls.set(call + 1);
                Ok((call > 0).then(healthy_response))
            },
            &inspector,
            &listener,
            |child, _, _, _, _, _| {
                spawned_pid.store(child.id(), Ordering::SeqCst);
                Ok(StartupOutcome::Healthy(healthy_response()))
            },
        )
        .expect_err("listener mismatch must fail startup");

        assert!(error.message.contains("listener PID 999999"));
        assert_eq!(read_pid(&paths).expect("read PID"), None);
        let pid = spawned_pid.load(Ordering::SeqCst);
        let _cleanup = PidCleanup::new(pid);
        wait_for_missing_process(pid);
    }

    #[test]
    fn start_listener_inspection_failure_terminates_spawned_child() {
        let root = temp_root("spawned-listener-inspection-error");
        let paths = test_paths(&root);
        let port = free_port();
        write_settings(&paths, port);
        write_fake_proxy_script(&paths, "import time\ntime.sleep(30)");
        let spawned_pid = Arc::new(AtomicU32::new(0));
        let inspector = RecordingInspector::new(fake_proxy_process(&paths, port));
        let health_calls = std::cell::Cell::new(0usize);

        let error = start_with_paths_and_controls(
            &paths,
            Duration::from_secs(1),
            Duration::ZERO,
            &|_| {
                let call = health_calls.get();
                health_calls.set(call + 1);
                Ok((call > 0).then(healthy_response))
            },
            &inspector,
            &FailingListenerInspector,
            |child, _, _, _, _, _| {
                spawned_pid.store(child.id(), Ordering::SeqCst);
                Ok(StartupOutcome::Healthy(healthy_response()))
            },
        )
        .expect_err("listener inspection failure must fail startup");

        assert!(error.message.contains("listener inspection unavailable"));
        assert_eq!(read_pid(&paths).expect("read PID"), None);
        let pid = spawned_pid.load(Ordering::SeqCst);
        let _cleanup = PidCleanup::new(pid);
        wait_for_missing_process(pid);
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
        assert_eq!(metadata.version, 2);
        assert_eq!(metadata.process_start_id, Some(super::test_process_start_id(42)));
        assert!(!metadata.recovery);
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
        let _dead_child = write_dead_legacy_lock(&lock);

        write_pid(&paths, 42, 4555, &paths.proxy_script_path()).expect("write pid");

        assert_eq!(fs::read_to_string(&lock).expect("lock text"), "codexhub-atomic-lock=1\n");
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
    fn stop_preserves_exact_non_listening_child_when_health_is_unavailable() {
        let root = temp_root("stale-pid");
        let paths = test_paths(&root);
        let port = free_port();
        write_settings(&paths, port);
        write_pid(&paths, 12_345u32, port, &paths.proxy_script_path()).expect("write pid");
        let killer = RecordingKiller::default();
        let inspector = RecordingInspector::new(fake_proxy_process(&paths, port));

        let error = stop_with_paths_and_controls(
            &paths,
            &killer,
            &inspector,
            &FixedListenerInspector::new(None),
        )
        .expect_err("non-listening child must remain durably tracked");

        assert!(error.contains("does not own the current listener"));
        assert_eq!(read_pid(&paths).expect("pid preserved"), Some(12_345));
        assert!(killer.killed.borrow().is_empty());
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

        let status = stop_with_paths_and_controls(
            &paths,
            &killer,
            &inspector,
            &FixedListenerInspector::new(None),
        )
        .expect("stop mismatch");

        assert!(!status.proxy_running);
        assert!(status.message.contains("was not killed"));
        assert_eq!(read_pid(&paths).expect("pid removed"), None);
        assert!(killer.killed.borrow().is_empty());
    }

    #[test]
    fn stop_refuses_shutdown_when_health_owner_is_not_exact_managed_pid() {
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

        let error = stop_with_paths_and_controls(
            &paths,
            &killer,
            &inspector,
            &FixedListenerInspector::new(Some(54_321)),
        )
        .expect_err("external listener must never receive shutdown");

        assert!(error.contains("ownership could not be verified"));
        assert_eq!(read_pid(&paths).expect("pid removed"), None);
        assert!(killer.killed.borrow().is_empty());
        health_server.join().expect("health server joins");
    }

    #[test]
    fn stop_refuses_shutdown_for_external_running_proxy_without_pid() {
        let root = temp_root("pid-missing-health-running");
        let paths = test_paths(&root);
        let port = free_port();
        write_settings(&paths, port);
        let health_server = spawn_single_health_response(port);
        let killer = RecordingKiller::default();
        let inspector = RecordingInspector::new(InspectedProcess::Missing);

        let error = stop_with_paths_and_controls(
            &paths,
            &killer,
            &inspector,
            &FixedListenerInspector::new(Some(54_321)),
        )
        .expect_err("external listener must not receive shutdown");

        assert!(error.contains("no managed PID identity"));
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

        let status = stop_with_paths_and_controls(
            &paths,
            &killer,
            &inspector,
            &FixedListenerInspector::new(None),
        )
        .expect("stop missing");

        assert!(!status.proxy_running);
        assert!(status.message.contains("Removed stale Gateway PID 12345"));
        assert_eq!(read_pid(&paths).expect("pid removed"), None);
        assert!(killer.killed.borrow().is_empty());
    }

    #[test]
    fn stop_rechecks_current_listener_immediately_before_force_kill() {
        let root = temp_root("listener-changed-before-kill");
        let paths = test_paths(&root);
        let port = free_port();
        let pid = 12_345;
        write_settings(&paths, port);
        write_pid(&paths, pid, port, &paths.proxy_script_path()).expect("write pid");
        let killer = RecordingKiller::default();
        let inspector = RecordingInspector::new(fake_proxy_process(&paths, port));
        let listener = SequenceListenerInspector::new([Some(54_321)]);
        let record = read_pid_record(&paths)
            .expect("read PID record")
            .expect("managed PID record");

        let error = force_kill_after_graceful_timeout(
            &paths,
            &record,
            port,
            &killer,
            &inspector,
            &listener,
        )
        .expect_err("changed listener must fence force kill");

        assert!(
            error.contains("immediately before force kill"),
            "{error}; listener calls: {:?}; remaining: {:?}",
            listener.calls.borrow(),
            listener.pids.borrow()
        );
        assert!(error.contains("PID 54321"));
        assert!(killer.killed.borrow().is_empty());
        assert_eq!(read_pid(&paths).expect("pid preserved"), Some(pid));
        assert_eq!(listener.calls.borrow().as_slice(), &[port]);
    }

    #[test]
    fn session_shutdown_uses_one_two_second_budget_and_refuses_replaced_identity() {
        let root = temp_root("session-shutdown-replaced-identity");
        let paths = test_paths(&root);
        let port = free_port();
        let pid = 12_345;
        write_settings(&paths, port);
        write_pid(&paths, pid, port, &paths.proxy_script_path()).expect("write pid");
        let record = read_pid_record(&paths)
            .expect("read pid record")
            .expect("managed pid record");
        let current_identity = record.gateway_identity().expect("managed Gateway identity");
        let replaced_identity = GatewayIdentity {
            pid: pid + 1,
            ..current_identity
        };
        let clock = FakeShutdownClock::default();
        let request_timeouts = RefCell::new(Vec::new());
        let killer = RecordingKiller::default();
        let inspector = RecordingInspector::new(fake_proxy_process(&paths, port));
        let listener = FixedListenerInspector::new(Some(pid));
        let shutdown_request = |_port: u16, _key: &str, timeout: Duration| -> Result<(), String> {
            request_timeouts.borrow_mut().push(timeout);
            Ok(())
        };
        let health_probe = |_port, _timeout| Ok(Some(healthy_response()));
        let controls = UserRequestedShutdownControls {
            killer: &killer,
            inspector: &inspector,
            listener_inspector: &listener,
            shutdown_request: &shutdown_request,
            health_probe: &health_probe,
            clock: &clock,
        };

        let error = stop_session_owned_with_paths_and_controls(
            &paths,
            Some(&replaced_identity),
            &controls,
        )
        .expect_err("replaced identity must never be force-killed");

        assert!(error.contains("current-session identity"), "{error}");
        assert_eq!(request_timeouts.borrow().as_slice(), &[Duration::from_millis(800)]);
        assert_eq!(clock.elapsed(), Duration::from_secs(2));
        assert!(killer.killed.borrow().is_empty());
        assert_eq!(read_pid(&paths).expect("pid preserved"), Some(pid));
    }

    #[test]
    fn session_shutdown_force_kills_only_the_exact_owned_identity_at_the_deadline() {
        let root = temp_root("session-shutdown-owned-identity");
        let paths = test_paths(&root);
        let port = free_port();
        let pid = 12_345;
        write_settings(&paths, port);
        write_pid(&paths, pid, port, &paths.proxy_script_path()).expect("write pid");
        let record = read_pid_record(&paths)
            .expect("read pid record")
            .expect("managed pid record");
        let current_identity = record.gateway_identity().expect("managed Gateway identity");
        let clock = FakeShutdownClock::default();
        let alive = Arc::new(AtomicBool::new(true));
        let listener_pid = Arc::new(AtomicU32::new(pid));
        let killer = KillAwareKiller::new(Arc::clone(&alive), Arc::clone(&listener_pid));
        let inspector = KillAwareInspector::new(Arc::clone(&alive), fake_proxy_process(&paths, port));
        let listener = AtomicListenerInspector::new(listener_pid);
        let shutdown_request = |_port: u16, _key: &str, timeout: Duration| -> Result<(), String> {
            assert_eq!(timeout, Duration::from_millis(800));
            Ok(())
        };
        let health_probe = |_port, _timeout| {
            Ok(alive
                .load(Ordering::SeqCst)
                .then(healthy_response))
        };
        let controls = UserRequestedShutdownControls {
            killer: &killer,
            inspector: &inspector,
            listener_inspector: &listener,
            shutdown_request: &shutdown_request,
            health_probe: &health_probe,
            clock: &clock,
        };

        let status = stop_session_owned_with_paths_and_controls(
            &paths,
            Some(&current_identity),
            &controls,
        )
        .expect("exact session-owned Gateway should be force-stopped at deadline");

        assert!(!status.proxy_running);
        assert!(status.message.contains("user-requested shutdown"));
        assert_eq!(killer.killed.borrow().as_slice(), &[pid]);
        assert_eq!(clock.elapsed(), Duration::from_secs(2));
        assert_eq!(read_pid(&paths).expect("pid removed"), None);
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

    #[cfg(windows)]
    #[test]
    fn startup_output_finish_returns_when_descendant_inherits_pipe() {
        let root = temp_root("startup-output-descendant");
        let descendant_pid_path = root.join("descendant.pid");
        let escaped_path = descendant_pid_path
            .to_string_lossy()
            .replace(char::from(39), "''");
        let mut command = Command::new("powershell");
        command.args([
            "-NoProfile",
            "-Command",
            &format!(
                "$child = Start-Process powershell -ArgumentList '-NoProfile','-Command','Start-Sleep -Seconds 10' -NoNewWindow -PassThru; $child.Id | Set-Content -NoNewline -Path '{escaped_path}'; [Console]::Out.Write('parent-exited')"
            ),
        ]);
        configure_start_stdio(&mut command);
        super::configure_no_window(&mut command);
        let mut child = command.spawn().expect("spawn output parent");
        let capture = capture_child_stdio(&mut child);
        child.wait().expect("output parent exits");
        let descendant_pid = fs::read_to_string(descendant_pid_path)
            .expect("descendant PID")
            .parse::<u32>()
            .expect("numeric descendant PID");
        let _cleanup = PidCleanup::new(descendant_pid);
        let (result_tx, result_rx) = std::sync::mpsc::channel();
        let worker = thread::spawn(move || {
            let _ = result_tx.send(capture.finish());
        });

        let output = result_rx
            .recv_timeout(Duration::from_secs(2))
            .expect("finish must not wait for descendant-held pipe");
        worker.join().expect("capture worker joins");
        assert_eq!(output.stdout, "parent-exited");
    }

    #[test]
    fn failed_start_cleanup_releases_output_capture_when_kill_is_unconfirmed() {
        let root = temp_root("unconfirmed-capture-release");
        let paths = test_paths(&root);
        let port = free_port();
        write_settings(&paths, port);
        write_fake_proxy_script(&paths, "import time\ntime.sleep(30)");
        let mut command = Command::new(find_python(&paths));
        command.args(["-c", "import time; time.sleep(30)"]);
        configure_start_stdio(&mut command);
        let mut child = command.spawn().expect("spawn cleanup child");
        let pid = child.id();
        let capture = capture_child_stdio(&mut child);
        let stdout = Arc::downgrade(&capture.stdout);
        let stderr = Arc::downgrade(&capture.stderr);
        let record = ProxyPidRecord::Managed(ProxyPidMetadata::recovery(
            pid,
            port,
            &paths.proxy_script_path(),
        ));

        let _ = clean_up_failed_start_with_controls(
            &paths,
            &mut child,
            capture,
            "startup reconciliation failed".to_string(),
            &record,
            &ErroringInspector,
            &UnconfirmedTerminator,
        );

        assert!(stdout.upgrade().is_none());
        assert!(stderr.upgrade().is_none());
        kill_process(pid).expect("clean up live child");
        let _ = child.wait();
        let _ = super::remove_pid(&paths);
    }

    #[test]
    fn failed_start_cleanup_is_bounded_and_persists_identity_when_kill_is_unconfirmed() {
        let root = temp_root("bounded-failed-cleanup");
        let paths = test_paths(&root);
        let port = free_port();
        write_settings(&paths, port);
        write_fake_proxy_script(&paths, "import time\ntime.sleep(30)");
        let mut command = Command::new(find_python(&paths));
        command.args(["-c", "import time; time.sleep(30)"]);
        configure_start_stdio(&mut command);
        let mut child = command.spawn().expect("spawn cleanup child");
        let pid = child.id();
        let capture = capture_child_stdio(&mut child);
        let record = ProxyPidRecord::Managed(ProxyPidMetadata::recovery(
            pid,
            port,
            &paths.proxy_script_path(),
        ));
        let inspector = RecordingInspector::new(fake_proxy_process(&paths, port));
        let started = Instant::now();

        let failure = clean_up_failed_start_with_controls(
            &paths,
            &mut child,
            capture,
            "startup reconciliation failed".to_string(),
            &record,
            &inspector,
            &UnconfirmedTerminator,
        );

        assert!(started.elapsed() < Duration::from_millis(500));
        assert!(failure.message.contains("termination could not be confirmed"));
        assert_eq!(
            failure.recovery_identity.as_ref().map(|identity| identity.pid),
            Some(pid)
        );
        assert_eq!(read_pid(&paths).expect("durable PID"), Some(pid));
        kill_process(pid).expect("clean up live child");
        let _ = child.wait();
        let _ = super::remove_pid(&paths);
    }

    #[test]
    fn failed_start_cleanup_preserves_spawn_known_recovery_when_inspection_is_unknown() {
        let root = temp_root("unknown-failed-cleanup");
        let paths = test_paths(&root);
        let port = free_port();
        write_settings(&paths, port);
        write_fake_proxy_script(&paths, "import time\ntime.sleep(30)");
        let mut command = Command::new(find_python(&paths));
        command.args(["-c", "import time; time.sleep(30)"]);
        configure_start_stdio(&mut command);
        let mut child = command.spawn().expect("spawn cleanup child");
        let pid = child.id();
        let capture = capture_child_stdio(&mut child);
        let record = ProxyPidRecord::Managed(ProxyPidMetadata::recovery(
            pid,
            port,
            &paths.proxy_script_path(),
        ));

        let failure = clean_up_failed_start_with_controls(
            &paths,
            &mut child,
            capture,
            "startup reconciliation failed".to_string(),
            &record,
            &ErroringInspector,
            &UnconfirmedTerminator,
        );

        assert!(failure.message.contains("inspection unavailable"));
        assert_eq!(
            failure.recovery_identity.as_ref().map(|identity| identity.pid),
            Some(pid)
        );
        assert_eq!(read_pid(&paths).expect("durable recovery PID"), Some(pid));
        kill_process(pid).expect("clean up live child");
        let _ = child.wait();
        let _ = super::remove_pid(&paths);
    }

    #[test]
    fn unknown_process_inspection_preserves_durable_identity() {
        let root = temp_root("unknown-inspection");
        let paths = test_paths(&root);
        let port = free_port();
        let pid = 12_345;
        write_settings(&paths, port);
        write_pid(&paths, pid, port, &paths.proxy_script_path()).expect("write PID");

        let error = reconciled_snapshot_with_controls(
            &paths,
            &|_| Ok(None),
            &ErroringInspector,
            &FixedListenerInspector::new(None),
        )
        .expect_err("unknown process inspection must not become stopped");

        assert!(error.contains("inspection unavailable"));
        assert_eq!(read_pid(&paths).expect("PID preserved"), Some(pid));
    }

    #[test]
    fn legacy_v1_without_process_start_identity_is_never_destructively_verified() {
        let root = temp_root("legacy-v1-unfenced");
        let paths = test_paths(&root);
        let port = free_port();
        let pid = 12_345;
        write_settings(&paths, port);
        fs::create_dir_all(paths.proxy_dir()).unwrap();
        fs::write(
            paths.pid_path(),
            format!(
                "{{\"version\":1,\"pid\":{pid},\"port\":{port},\"script_path\":{:?},\"started_at_unix_ms\":1}}",
                comparable_path(&paths.proxy_script_path())
            ),
        )
        .unwrap();
        let record = read_pid_record(&paths)
            .expect("read legacy v1")
            .expect("legacy record");

        let verification = super::verify_proxy_process(
            &record,
            &paths,
            port,
            &RecordingInspector::new(fake_proxy_process(&paths, port)),
        )
        .expect("verification result");

        assert!(matches!(verification, VerifiedProxyProcess::Unknown { pid: value, .. } if value == pid));
        assert_eq!(read_pid(&paths).expect("legacy PID preserved"), Some(pid));
    }

    #[test]
    fn recovery_with_identity_is_not_destructively_verified_before_activation() {
        let root = temp_root("recovery-not-destructive");
        let paths = test_paths(&root);
        let port = free_port();
        write_fake_proxy_script(&paths, "print('test')");
        let record = ProxyPidRecord::Managed(ProxyPidMetadata::recovery_with_identity(
            12_345,
            port,
            &paths.proxy_script_path(),
            super::test_process_start_id(12_345),
        ));

        let verification = super::verify_proxy_process(
            &record,
            &paths,
            port,
            &RecordingInspector::new(fake_proxy_process(&paths, port)),
        )
        .expect("verification result");

        assert!(matches!(
            verification,
            VerifiedProxyProcess::Unknown { pid: 12_345, .. }
        ));
    }

    #[test]
    fn exact_process_verification_rejects_raw_substring_only_script_matches() {
        let root = temp_root("exact-command-args");
        let paths = test_paths(&root);
        let port = free_port();
        let record = ProxyPidRecord::Managed(ProxyPidMetadata::new(
            12_345,
            port,
            &paths.proxy_script_path(),
            super::test_process_start_id(12_345),
        ));
        let misleading = ProcessInfo::from_args(vec![
            "python".to_string(),
            "wrapper.py".to_string(),
            "--note".to_string(),
            format!("{}.backup", comparable_path(&paths.proxy_script_path())),
            "--port".to_string(),
            port.to_string(),
        ]);

        let error = verify_proxy_command_line(&record, &paths, port, &misleading)
            .expect_err("raw substring must not establish exact process ownership");

        assert!(error.contains("codex_proxy.py"));
    }

    #[test]
    fn exact_process_verification_fences_pid_reuse_with_process_start_identity() {
        let root = temp_root("pid-reuse-fence");
        let paths = test_paths(&root);
        let port = free_port();
        let mut metadata = ProxyPidMetadata::new(
            12_345,
            port,
            &paths.proxy_script_path(),
            "original-start".to_string(),
        );
        metadata.process_start_id = Some("original-start".to_string());
        let record = ProxyPidRecord::Managed(metadata);
        let mut reused = match fake_proxy_process(&paths, port) {
            InspectedProcess::Running(info) => info,
            InspectedProcess::Missing => unreachable!(),
        };
        reused.process_start_id = Some("reused-start".to_string());

        let error = verify_proxy_command_line(&record, &paths, port, &reused)
            .expect_err("same PID and args with a different start identity must be rejected");

        assert!(error.contains("process start identity"));
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

    #[cfg(feature = "debug-diagnostics")]
    #[test]
    fn debug_start_command_bootstraps_diagnostics_in_the_existing_gateway_child() {
        let root = temp_root("debug-diagnostic-bootstrap");
        let paths = test_paths(&root);
        let command = build_start_command(
            Path::new("python-test"),
            &paths.proxy_script_path(),
            &paths,
            &Settings::default(),
        );
        let args = command
            .get_args()
            .map(|value| value.to_string_lossy().into_owned())
            .collect::<Vec<_>>();
        let script = paths.proxy_script_path().to_string_lossy().into_owned();
        let envs = command
            .get_envs()
            .map(|(key, value)| {
                (
                    key.to_string_lossy().into_owned(),
                    value.map(|item| item.to_os_string()),
                )
            })
            .collect::<std::collections::BTreeMap<_, _>>();

        assert_eq!(args.first().map(String::as_str), Some("-c"));
        assert!(args.iter().any(|arg| arg == DEBUG_DIAGNOSTIC_BOOTSTRAP));
        assert!(args.iter().any(|arg| arg == &script));
        assert!(args
            .windows(2)
            .any(|pair| pair[0] == "--port" && pair[1] == "9099"));
        assert_eq!(
            envs.get("CODEXHUB_DIAGNOSTICS_RUNTIME_HOME")
                .and_then(|value| value.as_ref())
                .map(PathBuf::from),
            Some(paths.codex_dir.clone())
        );
        let build = crate::build_info::current();
        assert_eq!(
            envs.get("CODEXHUB_DIAGNOSTICS_BUILD_VERSION")
                .and_then(|value| value.as_ref())
                .and_then(|value| value.to_str()),
            Some(build.semantic_version)
        );
        assert_eq!(
            envs.get("CODEXHUB_DIAGNOSTICS_SOURCE_REVISION")
                .and_then(|value| value.as_ref())
                .and_then(|value| value.to_str()),
            Some(build.source_revision)
        );
    }

    #[cfg(not(feature = "debug-diagnostics"))]
    #[test]
    fn normal_start_command_has_no_diagnostic_bootstrap_or_runtime_switch() {
        let root = temp_root("normal-diagnostic-bootstrap");
        let paths = test_paths(&root);
        let command = build_start_command(
            Path::new("python-test"),
            &paths.proxy_script_path(),
            &paths,
            &Settings::default(),
        );
        let args = command
            .get_args()
            .map(|value| value.to_string_lossy().into_owned())
            .collect::<Vec<_>>();

        assert_eq!(
            args.first().map(String::as_str),
            paths.proxy_script_path().to_str()
        );
        assert!(!args.iter().any(|arg| arg == DEBUG_DIAGNOSTIC_BOOTSTRAP));
        for key in [
            "CODEXHUB_DIAGNOSTICS_RUNTIME_HOME",
            "CODEXHUB_DIAGNOSTICS_BUILD_VERSION",
            "CODEXHUB_DIAGNOSTICS_SOURCE_REVISION",
        ] {
            assert!(!command.get_envs().any(|(candidate, _)| candidate == key));
        }
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
        assert_eq!(
            envs.get("CODEXHUB_CODEX_TARGET_HOME"),
            Some(&Some(target_home))
        );
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

            #[cfg(feature = "debug-diagnostics")]
            ensure(
                paths
                    .codex_dir
                    .join("diagnostics/control/status.json")
                    .is_file(),
                "debug Gateway should activate its recorder in the existing child",
            )?;
            #[cfg(not(feature = "debug-diagnostics"))]
            ensure(
                !paths.codex_dir.join("diagnostics").exists(),
                "normal Gateway should not create a diagnostic runtime subtree",
            )?;

            let running_status = status_with_paths(&paths)?;
            ensure(running_status.proxy_running, "status should be running")?;

            let stop_status = stop_with_paths(&paths)?;
            ensure(!stop_status.proxy_running, "stop status should be stopped")?;
            ensure(
                stop_status.message == "Gateway stopped gracefully",
                "stop should use the Gateway /shutdown endpoint",
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
    fn restart_after_settings_port_change_stops_recorded_port_before_starting_new_port() {
        let root = temp_root("python-port-change-restart");
        let repo_root = copy_python_sources_to_temp_repo(&root);
        let paths = ProxyPaths::new(root.join("codex-home"), repo_root);
        let old_port = free_port();
        let new_port = free_port();
        write_settings(&paths, old_port);

        let result = (|| {
            let old_status = start_with_paths(&paths)?;
            ensure(old_status.proxy_port == old_port, "old Gateway should use old port")?;
            let old_pid = read_pid(&paths)?.ok_or_else(|| "old PID missing".to_string())?;
            write_settings(&paths, new_port);
            let backend = ProxyLifecycleBackend {
                lifecycle_gate_path: paths.lifecycle_gate_path(),
                paths: paths.clone(),
                session_owned_identity: None,
            };
            let coordinator = crate::gateway_lifecycle::GatewayLifecycleCoordinator::new();

            let replacement = coordinator.restart(&backend, || Ok(()))?;

            ensure(
                replacement.status.proxy_running && replacement.status.proxy_port == new_port,
                "replacement Gateway should run on new settings port",
            )?;
            let new_pid = read_pid(&paths)?.ok_or_else(|| "replacement PID missing".to_string())?;
            ensure(new_pid != old_pid, "restart should replace the old process")?;
            ensure(
                super::health(old_port)?.is_none(),
                "old recorded port should be released before replacement publication",
            )?;
            Ok::<(), String>(())
        })();

        let _ = stop_with_paths(&paths);
        result.expect("port-change restart lifecycle");
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
            ensure(
                new_status.proxy_running,
                "upgraded in-place bundle should start",
            )?;
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

    use crate::lock_test_fixtures::write_dead_legacy_lock;

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

    fn healthy_response() -> super::HealthResponse {
        super::HealthResponse {
            ok: Some(true),
            build: Some("test".to_string()),
        }
    }

    struct FixedListenerInspector {
        pid: Option<u32>,
    }

    struct SequenceListenerInspector {
        pids: RefCell<VecDeque<Option<u32>>>,
        calls: RefCell<Vec<u16>>,
    }

    impl SequenceListenerInspector {
        fn new(pids: impl IntoIterator<Item = Option<u32>>) -> Self {
            Self {
                pids: RefCell::new(pids.into_iter().collect()),
                calls: RefCell::new(Vec::new()),
            }
        }
    }

    impl ListenerInspector for SequenceListenerInspector {
        fn listening_pid(&self, port: u16) -> Result<Option<u32>, String> {
            self.calls.borrow_mut().push(port);
            self.pids
                .borrow_mut()
                .pop_front()
                .ok_or_else(|| "unexpected listener inspection".to_string())
        }
    }

    impl FixedListenerInspector {
        fn new(pid: Option<u32>) -> Self {
            Self { pid }
        }
    }

    impl ListenerInspector for FixedListenerInspector {
        fn listening_pid(&self, _port: u16) -> Result<Option<u32>, String> {
            Ok(self.pid)
        }
    }

    struct AtomicListenerInspector {
        pid: Arc<AtomicU32>,
    }

    impl AtomicListenerInspector {
        fn new(pid: Arc<AtomicU32>) -> Self {
            Self { pid }
        }
    }

    impl ListenerInspector for AtomicListenerInspector {
        fn listening_pid(&self, _port: u16) -> Result<Option<u32>, String> {
            match self.pid.load(Ordering::SeqCst) {
                0 => Ok(None),
                pid => Ok(Some(pid)),
            }
        }
    }

    struct FailingListenerInspector;

    impl ListenerInspector for FailingListenerInspector {
        fn listening_pid(&self, _port: u16) -> Result<Option<u32>, String> {
            Err("listener inspection unavailable".to_string())
        }
    }

    fn fake_proxy_process(paths: &ProxyPaths, port: u16) -> InspectedProcess {
        let mut info = ProcessInfo::from_args(vec![
            "python".to_string(),
            comparable_path(&paths.proxy_script_path()),
            "--host".to_string(),
            "127.0.0.1".to_string(),
            "--port".to_string(),
            port.to_string(),
        ]);
        info.process_start_id = Some(super::test_process_start_id(12_345));
        InspectedProcess::Running(info)
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

        let config_source = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .unwrap()
            .join("config")
            .join("official_model_catalog_metadata.json");
        let config_target = repo_root.join("config");
        fs::create_dir_all(&config_target).unwrap();
        fs::copy(
            config_source,
            config_target.join("official_model_catalog_metadata.json"),
        )
        .unwrap();

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

    struct UnconfirmedTerminator;

    impl ChildTerminator for UnconfirmedTerminator {
        fn terminate(&self, _child: &mut std::process::Child) -> Result<bool, String> {
            Ok(false)
        }
    }

    impl ProcessKiller for RecordingKiller {
        fn kill(&self, pid: u32) -> Result<(), String> {
            self.killed.borrow_mut().push(pid);
            Ok(())
        }
    }

    #[derive(Default)]
    struct FakeShutdownClock {
        elapsed: RefCell<Duration>,
    }

    impl FakeShutdownClock {
        fn elapsed_value(&self) -> Duration {
            *self.elapsed.borrow()
        }
    }

    impl ShutdownClock for FakeShutdownClock {
        fn elapsed(&self) -> Duration {
            self.elapsed_value()
        }

        fn sleep(&self, duration: Duration) {
            *self.elapsed.borrow_mut() += duration;
        }
    }

    struct KillAwareKiller {
        killed: RefCell<Vec<u32>>,
        alive: Arc<AtomicBool>,
        listener_pid: Arc<AtomicU32>,
    }

    impl KillAwareKiller {
        fn new(alive: Arc<AtomicBool>, listener_pid: Arc<AtomicU32>) -> Self {
            Self {
                killed: RefCell::new(Vec::new()),
                alive,
                listener_pid,
            }
        }
    }

    impl ProcessKiller for KillAwareKiller {
        fn kill(&self, pid: u32) -> Result<(), String> {
            self.killed.borrow_mut().push(pid);
            self.alive.store(false, Ordering::SeqCst);
            self.listener_pid.store(0, Ordering::SeqCst);
            Ok(())
        }
    }

    struct KillAwareInspector {
        alive: Arc<AtomicBool>,
        running: InspectedProcess,
    }

    impl KillAwareInspector {
        fn new(alive: Arc<AtomicBool>, running: InspectedProcess) -> Self {
            Self { alive, running }
        }
    }

    impl ProcessInspector for KillAwareInspector {
        fn inspect(&self, _pid: u32) -> Result<InspectedProcess, String> {
            if self.alive.load(Ordering::SeqCst) {
                Ok(self.running.clone())
            } else {
                Ok(InspectedProcess::Missing)
            }
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

        fn spawned_process_start_id(
            &self,
            _child: &std::process::Child,
        ) -> Result<String, String> {
            match &self.process {
                InspectedProcess::Running(info) => info
                    .process_start_id
                    .clone()
                    .ok_or_else(|| "test process has no start identity".to_string()),
                InspectedProcess::Missing => Err("test process is missing".to_string()),
            }
        }
    }

    struct ErroringInspector;

    impl ProcessInspector for ErroringInspector {
        fn inspect(&self, _pid: u32) -> Result<InspectedProcess, String> {
            Err("process inspection unavailable".to_string())
        }
    }

    #[cfg(windows)]
    struct TimeoutAfterSpawnInspector;

    #[cfg(windows)]
    impl ProcessInspector for TimeoutAfterSpawnInspector {
        fn inspect(&self, _pid: u32) -> Result<InspectedProcess, String> {
            run_windows_inspection(
                "Write-Error 'private-post-spawn-sentinel'; Start-Sleep -Seconds 10",
                WindowsInspectionKind::Process,
                Duration::from_millis(200),
            )
            .map(|_| InspectedProcess::Missing)
        }
    }

    struct PidCleanup {
        pid: u32,
    }

    impl PidCleanup {
        fn new(pid: u32) -> Self {
            Self { pid }
        }
    }

    impl Drop for PidCleanup {
        fn drop(&mut self) {
            let _ = kill_process(self.pid);
        }
    }
}
