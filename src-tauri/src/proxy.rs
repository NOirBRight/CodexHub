use crate::AppStatus;
use crate::Settings;
use serde::Deserialize;
use std::fs;
use std::io::{self, Read};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

#[cfg(windows)]
use std::os::windows::process::CommandExt;

const HEALTH_TIMEOUT: Duration = Duration::from_millis(800);
const SHUTDOWN_TIMEOUT: Duration = Duration::from_millis(800);
const START_TIMEOUT: Duration = Duration::from_secs(20);
const GRACEFUL_STOP_TIMEOUT: Duration = Duration::from_secs(2);
const KILL_STOP_TIMEOUT: Duration = Duration::from_secs(5);

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
    repo_root: PathBuf,
}

impl ProxyPaths {
    fn runtime() -> Result<Self, String> {
        let codex_dir = match std::env::var_os("CODEX_HOME").filter(|value| !value.is_empty()) {
            Some(value) => PathBuf::from(value),
            None => dirs::home_dir()
                .ok_or_else(|| "failed to resolve user home directory".to_string())?
                .join(".codex"),
        };
        let repo_root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .ok_or_else(|| "failed to resolve CodexHub repo root".to_string())?
            .to_path_buf();

        Ok(Self::new(codex_dir, repo_root))
    }

    fn new(codex_dir: impl Into<PathBuf>, repo_root: impl Into<PathBuf>) -> Self {
        Self {
            codex_dir: codex_dir.into(),
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
        self.codex_dir.join("config.toml")
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
    auto_sync_history: Option<bool>,
    auto_start_proxy: Option<bool>,
    include_official_models: Option<bool>,
    proxy_port: Option<u16>,
}

impl SettingsDocument {
    fn into_settings(self) -> Settings {
        let defaults = Settings::default();
        Settings {
            auto_sync_history: self.auto_sync_history.unwrap_or(defaults.auto_sync_history),
            auto_start_proxy: self.auto_start_proxy.unwrap_or(defaults.auto_start_proxy),
            include_official_models: self
                .include_official_models
                .unwrap_or(defaults.include_official_models),
            proxy_port: self.proxy_port.unwrap_or(defaults.proxy_port),
        }
    }
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
    })
}

fn start_with_paths(paths: &ProxyPaths) -> Result<AppStatus, String> {
    let settings = read_settings(paths)?;
    let mode = read_mode(paths)?;
    if let Some(response) = health(settings.proxy_port)? {
        if response.is_running() {
            return Ok(AppStatus {
                mode,
                proxy_running: true,
                proxy_port: settings.proxy_port,
                proxy_build: response.build,
                message: "Proxy is already running".to_string(),
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
    let mut command = build_start_command(&python, &script, paths, settings.proxy_port);

    let mut child = command.spawn().map_err(|error| {
        format!(
            "failed to start proxy with {} {}: {error}",
            python.display(),
            script.display()
        )
    })?;
    drain_child_stdio(&mut child);
    let pid = child.id();
    if let Err(error) = write_pid(paths, pid) {
        let _ = child.kill();
        return Err(error);
    }

    match wait_for_health(settings.proxy_port, START_TIMEOUT)? {
        Some(response) if response.is_running() => Ok(AppStatus {
            mode,
            proxy_running: true,
            proxy_port: settings.proxy_port,
            proxy_build: response.build,
            message: format!("Proxy started with PID {pid}"),
        }),
        _ => {
            let _ = child.kill();
            let _ = remove_pid(paths);
            Err(format!(
                "proxy did not become healthy within {} seconds at http://127.0.0.1:{}/health",
                START_TIMEOUT.as_secs(),
                settings.proxy_port
            ))
        }
    }
}

fn stop_with_paths(paths: &ProxyPaths) -> Result<AppStatus, String> {
    stop_with_paths_and_killer(paths, &SystemProcessKiller)
}

fn stop_with_paths_and_killer(
    paths: &ProxyPaths,
    killer: &dyn ProcessKiller,
) -> Result<AppStatus, String> {
    let settings = read_settings(paths)?;
    let mode = read_mode(paths)?;
    let pid = read_pid(paths)?;
    let was_running = health(settings.proxy_port)?
        .as_ref()
        .map(HealthResponse::is_running)
        .unwrap_or(false);

    if !was_running {
        if let Some(pid) = pid {
            killer.kill(pid)?;
            remove_pid(paths)?;
        }
        return Ok(AppStatus {
            mode,
            proxy_running: false,
            proxy_port: settings.proxy_port,
            proxy_build: None,
            message: if let Some(pid) = pid {
                format!("Proxy PID {pid} stopped after health endpoint was unavailable")
            } else {
                "Proxy is not running".to_string()
            },
        });
    }

    let _ = request_shutdown(settings.proxy_port);
    if wait_for_stopped(settings.proxy_port, GRACEFUL_STOP_TIMEOUT)? {
        remove_pid(paths)?;
        return Ok(AppStatus {
            mode,
            proxy_running: false,
            proxy_port: settings.proxy_port,
            proxy_build: None,
            message: "Proxy stopped gracefully".to_string(),
        });
    }

    let Some(pid) = pid else {
        let mut status = status_with_paths(paths)?;
        status.message =
            "Proxy is running, but no PID file was found; stop did not force-kill it".to_string();
        return Ok(status);
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
    })
}

fn build_start_command(python: &Path, script: &Path, paths: &ProxyPaths, port: u16) -> Command {
    let mut command = Command::new(python);
    command
        .arg(script)
        .arg("--host")
        .arg("127.0.0.1")
        .arg("--port")
        .arg(port.to_string())
        .current_dir(paths.proxy_script_dir())
        .env("PYTHONPATH", paths.proxy_script_dir());
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

fn drain_child_stdio(child: &mut Child) {
    drop(child.stdin.take());
    if let Some(stdout) = child.stdout.take() {
        drain_reader(stdout);
    }
    if let Some(stderr) = child.stderr.take() {
        drain_reader(stderr);
    }
}

fn drain_reader<R>(mut reader: R)
where
    R: Read + Send + 'static,
{
    thread::spawn(move || {
        let mut sink = io::sink();
        let _ = io::copy(&mut reader, &mut sink);
    });
}

trait ProcessKiller {
    fn kill(&self, pid: u32) -> Result<(), String>;
}

struct SystemProcessKiller;

impl ProcessKiller for SystemProcessKiller {
    fn kill(&self, pid: u32) -> Result<(), String> {
        kill_process(pid)
    }
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
    let normalized = text.to_ascii_lowercase();
    let compact = normalized
        .chars()
        .filter(|character| !character.is_whitespace())
        .collect::<String>();

    let custom_markers = [
        "model_provider=\"custom\"",
        "[model_providers.custom]",
        "[model_providers.codex_proxy]",
        "codex-proxy-official-ollama.json",
        "codex_proxy",
        "codex proxy",
        "http://127.0.0.1:",
        "http://localhost:",
    ];

    if custom_markers.iter().any(|marker| {
        if marker.contains(' ') {
            normalized.contains(marker)
        } else {
            compact.contains(marker)
        }
    }) {
        "custom"
    } else {
        "official"
    }
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

fn request_shutdown(port: u16) -> Result<(), String> {
    let client = reqwest::blocking::Client::builder()
        .timeout(SHUTDOWN_TIMEOUT)
        .build()
        .map_err(|error| format!("failed to build HTTP client: {error}"))?;
    let url = format!("http://127.0.0.1:{port}/shutdown");
    let _ = client.post(url).send();
    Ok(())
}

fn wait_for_health(port: u16, timeout: Duration) -> Result<Option<HealthResponse>, String> {
    let deadline = Instant::now() + timeout;
    loop {
        if let Some(response) = health(port)? {
            if response.is_running() {
                return Ok(Some(response));
            }
        }
        if Instant::now() >= deadline {
            return Ok(None);
        }
        thread::sleep(Duration::from_millis(200));
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

fn read_pid(paths: &ProxyPaths) -> Result<Option<u32>, String> {
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
    trimmed
        .parse::<u32>()
        .map(Some)
        .map_err(|error| format!("invalid proxy PID in {}: {error}", path.display()))
}

fn write_pid(paths: &ProxyPaths, pid: u32) -> Result<(), String> {
    fs::create_dir_all(paths.proxy_dir()).map_err(|error| {
        format!(
            "failed to create proxy runtime directory {}: {error}",
            paths.proxy_dir().display()
        )
    })?;
    fs::write(paths.pid_path(), format!("{pid}\n")).map_err(|error| {
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
    for name in ["CODEXHUB_PYTHON", "CODEXHUB_PROXY_PYTHON"] {
        if let Some(value) = std::env::var_os(name).filter(|value| !value.is_empty()) {
            candidates.push(PathBuf::from(value));
        }
    }

    #[cfg(windows)]
    {
        candidates.push(
            paths
                .proxy_script_dir()
                .join(".venv")
                .join("Scripts")
                .join("python.exe"),
        );
        candidates.push(paths.repo_root.join("python").join("python.exe"));
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
        candidates.push(paths.repo_root.join("python").join("bin").join("python"));
    }

    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            #[cfg(windows)]
            {
                candidates.push(dir.join("python.exe"));
                candidates.push(dir.join("python3.exe"));
                candidates.push(dir.join("codexhub-python.exe"));
            }
            #[cfg(not(windows))]
            {
                candidates.push(dir.join("python"));
                candidates.push(dir.join("python3"));
                candidates.push(dir.join("codexhub-python"));
            }
        }
    }

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

#[cfg(windows)]
fn kill_process(pid: u32) -> Result<(), String> {
    let pid_text = pid.to_string();
    let output = Command::new("taskkill")
        .args(["/PID", &pid_text, "/T", "/F"])
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
        configure_start_stdio, detect_mode, find_python, read_pid, start_with_paths,
        status_with_paths, stop_with_paths, stop_with_paths_and_killer, write_pid, ProcessKiller,
        ProxyPaths,
    };
    use crate::Settings;
    use std::cell::RefCell;
    use std::fs;
    use std::net::TcpListener;
    use std::path::{Path, PathBuf};
    use std::process::Command;
    use std::time::{SystemTime, UNIX_EPOCH};

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
model_catalog_json = "model-catalogs/codex-proxy-official-ollama.json"
[model_providers.codex_proxy]
"#
            ),
            "custom"
        );
    }

    #[test]
    fn pid_file_roundtrips_and_missing_pid_is_none() {
        let root = temp_root("pid-roundtrip");
        let paths = test_paths(&root);

        assert_eq!(read_pid(&paths).expect("missing pid"), None);
        write_pid(&paths, 42).expect("write pid");

        assert_eq!(read_pid(&paths).expect("read pid"), Some(42));
        let text = fs::read_to_string(paths.pid_path()).expect("pid text");
        assert_eq!(text.trim(), "42");
    }

    #[test]
    fn stop_kills_pid_when_health_is_unavailable() {
        let root = temp_root("stale-pid");
        let paths = test_paths(&root);
        write_settings(&paths, free_port());
        write_pid(&paths, 12_345u32).expect("write pid");
        let killer = RecordingKiller::default();

        let status = stop_with_paths_and_killer(&paths, &killer).expect("stop stale");

        assert!(!status.proxy_running);
        assert_eq!(read_pid(&paths).expect("pid removed"), None);
        assert_eq!(killer.killed.borrow().as_slice(), &[12_345]);
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
            ensure(read_pid(&paths)?.is_none(), "stop should remove PID")?;

            let stopped_status = status_with_paths(&paths)?;
            ensure(!stopped_status.proxy_running, "status should be stopped")?;
            Ok::<(), String>(())
        })();

        let _ = stop_with_paths(&paths);
        result.expect("python proxy lifecycle");
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

    fn free_port() -> u16 {
        let listener = TcpListener::bind(("127.0.0.1", 0)).expect("bind free port");
        listener.local_addr().unwrap().port()
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

    fn copy_python_sources_to_temp_repo(root: &Path) -> PathBuf {
        let repo_root = root.join("repo-root");
        let source = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .unwrap()
            .join("src-python");
        let target = repo_root.join("src-python");
        fs::create_dir_all(&target).unwrap();

        for entry in fs::read_dir(source).unwrap() {
            let entry = entry.unwrap();
            let path = entry.path();
            if path.extension().and_then(|value| value.to_str()) == Some("py") {
                fs::copy(&path, target.join(path.file_name().unwrap())).unwrap();
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
}
