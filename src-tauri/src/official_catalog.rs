//! Subscription discovery owns a total deadline and never publishes a cache.
use std::io::Read;
use std::process::{Command, Stdio};
use std::sync::{mpsc, Mutex};
use std::time::{Duration, Instant};

#[derive(Default)]
struct State {
    running: bool,
    cancelled: bool,
    request_id: Option<String>,
    early_cancellations: Vec<String>,
}
static STATE: Mutex<State> = Mutex::new(State {
    running: false,
    cancelled: false,
    request_id: None,
    early_cancellations: Vec::new(),
});
#[derive(Debug)]
pub(crate) struct Refresh {
    deadline: Instant,
}

impl Refresh {
    pub(crate) fn begin() -> Result<Self, String> {
        Self::begin_for(None)
    }
    pub(crate) fn begin_for(request_id: Option<&str>) -> Result<Self, String> {
        if let Some(id) = request_id {
            validate_request_id(id)?;
        }
        let deadline = Instant::now() + timeout()?;
        let mut state = STATE
            .lock()
            .map_err(|_| "Official refresh lock unavailable")?;
        if let Some(index) =
            request_id.and_then(|id| state.early_cancellations.iter().position(|v| v == id))
        {
            state.early_cancellations.remove(index);
            return Err("Official model refresh cancelled".into());
        }
        if state.running {
            return Err("Official refresh is already running".into());
        }
        state.running = true;
        state.cancelled = false;
        state.request_id = request_id.map(str::to_string);
        Ok(Self { deadline })
    }
    pub(crate) fn check_current(&self) -> Result<(), String> {
        self.check(self.deadline)
    }
    fn check(&self, deadline: Instant) -> Result<(), String> {
        let state = STATE
            .lock()
            .map_err(|_| "Official refresh lock unavailable")?;
        if state.cancelled {
            return Err("Official model refresh cancelled".into());
        }
        if Instant::now() >= deadline.min(self.deadline) {
            return Err("Official model refresh timed out".into());
        }
        Ok(())
    }
    pub(crate) fn publish<T, E: From<String>>(
        &self,
        write: impl FnOnce() -> Result<T, E>,
    ) -> Result<T, E> {
        let state = STATE
            .lock()
            .map_err(|_| E::from("Official refresh lock unavailable".to_string()))?;
        if state.cancelled {
            return Err(E::from("Official model refresh cancelled".to_string()));
        }
        if Instant::now() >= self.deadline {
            return Err(E::from("Official model refresh timed out".to_string()));
        }
        // Commit is the linearization point: later cancellation cannot undo a
        // completed atomic publication or cancel a different request.
        write()
    }
}
impl Drop for Refresh {
    fn drop(&mut self) {
        if let Ok(mut state) = STATE.lock() {
            state.running = false;
            state.cancelled = false;
            state.request_id = None;
        }
    }
}
fn validate_request_id(id: &str) -> Result<(), String> {
    if id.is_empty() || id.len() > 64 || !id.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-')
    {
        return Err("Invalid Official refresh request id".into());
    }
    Ok(())
}
pub(crate) fn cancel(request_id: &str) -> Result<(), String> {
    validate_request_id(request_id)?;
    let mut state = STATE
        .lock()
        .map_err(|_| "Official refresh lock unavailable")?;
    if state.running && state.request_id.as_deref() == Some(request_id) {
        state.cancelled = true;
    } else if !state.early_cancellations.iter().any(|id| id == request_id) {
        // Cancellation may reach the worker before its refresh invocation.
        // IDs are one-shot and this bounded tombstone set cannot affect others.
        if state.early_cancellations.len() >= 32 {
            state.early_cancellations.remove(0);
        }
        state.early_cancellations.push(request_id.to_string());
    }
    Ok(())
}

fn timeout() -> Result<Duration, String> {
    let seconds = match std::env::var("CODEXHUB_OFFICIAL_MODELS_TIMEOUT_SECONDS") {
        Ok(value) => value
            .parse::<u64>()
            .map_err(|_| "Official model timeout must be 1–300 seconds")?,
        Err(std::env::VarError::NotPresent) => 30,
        Err(_) => return Err("Invalid Official model timeout".into()),
    };
    if !(1..=300).contains(&seconds) {
        return Err("Official model timeout must be 1–300 seconds".into());
    }
    Ok(Duration::from_secs(seconds))
}

pub(crate) fn fetch(refresh: &Refresh) -> Result<String, String> {
    let deadline = refresh.deadline;
    let budget = deadline.saturating_duration_since(Instant::now());
    let mut version_command = crate::codex_cli::command()?;
    version_command.arg("--version");
    let output = run(version_command, refresh, deadline, 4096)?;
    let version = output
        .trim()
        .strip_prefix("codex-cli ")
        .ok_or("Cannot determine the installed Codex CLI version")?
        .trim();
    let root = crate::runtime_paths::resource_root()?;
    let python = crate::runtime_paths::find_python(Some(&root))?;
    refresh.check(deadline)?;
    let mut command = crate::runtime_paths::configured_python_command(&python);
    command
        .arg(root.join("src-python/official_catalog.py"))
        .args([
            "--client-version",
            version,
            "--timeout",
            &budget.as_secs().to_string(),
        ])
        .env(
            "CODEXHUB_CODEX_TARGET_HOME",
            crate::runtime_paths::codex_target_home_dir()?,
        );
    run(command, refresh, deadline, 33 * 1024 * 1024)
}

fn run(
    mut command: Command,
    refresh: &Refresh,
    deadline: Instant,
    limit: u64,
) -> Result<String, String> {
    refresh.check(deadline)?;
    command
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null());
    crate::runtime_paths::configure_no_window(&mut command);
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        command.process_group(0);
    }
    let mut child = command
        .spawn()
        .map_err(|_| "Unable to start Official catalog helper")?;
    #[cfg(windows)]
    let _job = match crate::app_server::AppServerJob::assign_to(&child) {
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
            .take(limit + 1)
            .read_to_end(&mut bytes)
            .map_err(|_| "Cannot read Official catalog helper output".to_string())
            .and_then(|_| {
                if bytes.len() as u64 > limit {
                    Err("Official catalog response exceeds the size limit".into())
                } else {
                    String::from_utf8(bytes)
                        .map_err(|_| "Invalid Official catalog helper output".into())
                }
            });
        let _ = sender.send(result);
    });
    let result = (|| {
        loop {
            refresh.check(deadline)?;
            match receiver.recv_timeout(Duration::from_millis(25)) {
                Ok(output) => {
                    let output = output?;
                    // stdout EOF need not mean process exit. Keep the same deadline.
                    loop {
                        refresh.check(deadline)?;
                        if let Some(status) = child
                            .try_wait()
                            .map_err(|_| "Cannot inspect Official catalog helper")?
                        {
                            if status.success() {
                                return Ok(output);
                            }
                            // Only the helper's small, sanitized error field is surfaced.
                            let error = serde_json::from_str::<serde_json::Value>(&output)
                                .ok()
                                .and_then(|v| {
                                    v.get("error").and_then(|v| v.as_str()).map(str::to_string)
                                })
                                .unwrap_or_else(|| "Official catalog helper failed".into());
                            return Err(error);
                        }
                        std::thread::sleep(Duration::from_millis(10));
                    }
                }
                Err(mpsc::RecvTimeoutError::Timeout) => {}
                Err(_) => return Err("Official catalog helper closed unexpectedly".into()),
            }
        }
    })();
    #[cfg(unix)]
    unsafe {
        libc::kill(-(child.id() as libc::pid_t), libc::SIGKILL);
    }
    #[cfg(windows)]
    drop(_job);
    let _ = child.kill();
    let _ = child.wait();
    let _ = reader.join();
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    static TEST_LOCK: Mutex<()> = Mutex::new(());
    fn python(code: &str) -> Command {
        let root = crate::runtime_paths::resource_root().unwrap();
        let python = crate::runtime_paths::find_python(Some(&root)).unwrap();
        let mut command = crate::runtime_paths::configured_python_command(&python);
        command.args(["-c", code]);
        command
    }
    #[test]
    fn total_deadline_terminates_stalled_child() {
        let _serial = TEST_LOCK.lock().unwrap();
        let refresh = Refresh::begin().unwrap();
        let start = Instant::now();
        let error = run(
            python("import time; time.sleep(20)"),
            &refresh,
            start + Duration::from_millis(250),
            4096,
        )
        .unwrap_err();
        assert!(error.contains("timed out"), "{error}");
        assert!(start.elapsed() < Duration::from_secs(3));
    }
    #[test]
    fn cancellation_stops_child_and_prevents_publication() {
        let _serial = TEST_LOCK.lock().unwrap();
        let refresh = Refresh::begin_for(Some("cancel-test")).unwrap();
        let canceller = std::thread::spawn(|| {
            std::thread::sleep(Duration::from_millis(150));
            cancel("cancel-test").unwrap();
        });
        let error = run(
            python("import time; time.sleep(20)"),
            &refresh,
            Instant::now() + Duration::from_secs(30),
            4096,
        )
        .unwrap_err();
        canceller.join().unwrap();
        assert!(error.contains("cancelled"));
        let mut published = false;
        assert!(refresh
            .publish(|| {
                published = true;
                Ok::<(), String>(())
            })
            .is_err());
        assert!(!published);
    }
    #[test]
    fn excessive_output_fails_without_waiting_for_process_exit() {
        let _serial = TEST_LOCK.lock().unwrap();
        let refresh = Refresh::begin().unwrap();
        let error = run(
            python("import sys,time; print('x'*10000); sys.stdout.flush(); time.sleep(20)"),
            &refresh,
            Instant::now() + Duration::from_secs(5),
            4096,
        )
        .unwrap_err();
        assert!(error.contains("size limit"), "{error}");
    }
    #[test]
    fn only_one_refresh_can_own_cancellation() {
        let _serial = TEST_LOCK.lock().unwrap();
        let refresh = Refresh::begin().unwrap();
        assert!(Refresh::begin().is_err());
        drop(refresh);
        assert!(Refresh::begin().is_ok());
    }
    #[test]
    fn delayed_cancel_never_cancels_a_new_request() {
        let _serial = TEST_LOCK.lock().unwrap();
        drop(Refresh::begin_for(Some("previous")).unwrap());
        let current = Refresh::begin_for(Some("current")).unwrap();
        cancel("previous").unwrap();
        current.check_current().unwrap();
        drop(current);
        cancel("not-started-yet").unwrap();
        assert!(Refresh::begin_for(Some("not-started-yet"))
            .unwrap_err()
            .contains("cancelled"));
    }
    #[test]
    fn deadline_expired_after_fetch_prevents_publication() {
        let _serial = TEST_LOCK.lock().unwrap();
        let mut refresh = Refresh::begin().unwrap();
        refresh.deadline = Instant::now() - Duration::from_millis(1);
        let mut published = false;
        let result: Result<(), String> = refresh.publish(|| {
            published = true;
            Ok(())
        });
        assert!(result.unwrap_err().contains("timed out"));
        assert!(!published);
    }
    #[test]
    fn cancellation_reaps_a_descendant_that_inherits_stdout() {
        let _serial = TEST_LOCK.lock().unwrap();
        let refresh = Refresh::begin().unwrap();
        let marker =
            std::env::temp_dir().join(format!("catalog-child-marker-{}", std::process::id()));
        let _ = std::fs::remove_file(&marker);
        let child_code = format!(
            "import time,pathlib; time.sleep(1); pathlib.Path({}).write_text('leaked')",
            serde_json::to_string(&marker.to_string_lossy()).unwrap()
        );
        let code = format!(
            "import subprocess,sys; subprocess.Popen([sys.executable,'-c',{}])",
            serde_json::to_string(&child_code).unwrap()
        );
        let start = Instant::now();
        let result = run(
            python(&code),
            &refresh,
            start + Duration::from_millis(300),
            4096,
        );
        assert!(result.unwrap_err().contains("timed out"));
        assert!(start.elapsed() < Duration::from_secs(2));
        std::thread::sleep(Duration::from_millis(1100));
        assert!(!marker.exists(), "catalog descendant survived its deadline");
    }
}
