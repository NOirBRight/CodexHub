//! Subscription discovery owns a total deadline and never publishes a cache.
use std::io::Read;
use std::process::{Command, Stdio};
use std::sync::{mpsc, Mutex};
use std::time::{Duration, Instant};

#[derive(Default)]
struct State {
    running: bool,
    cancelled: bool,
}
static STATE: Mutex<State> = Mutex::new(State {
    running: false,
    cancelled: false,
});
pub(crate) struct Refresh;

impl Refresh {
    pub(crate) fn begin() -> Result<Self, String> {
        let mut state = STATE
            .lock()
            .map_err(|_| "Official refresh lock unavailable")?;
        if state.running {
            return Err("Official refresh is already running".into());
        }
        *state = State {
            running: true,
            cancelled: false,
        };
        Ok(Self)
    }
    fn check(&self, deadline: Instant) -> Result<(), String> {
        let state = STATE
            .lock()
            .map_err(|_| "Official refresh lock unavailable")?;
        if state.cancelled {
            return Err("Official model refresh cancelled".into());
        }
        if Instant::now() >= deadline {
            return Err("Official model refresh timed out".into());
        }
        Ok(())
    }
    pub(crate) fn publish<T>(
        &self,
        write: impl FnOnce() -> Result<T, String>,
    ) -> Result<T, String> {
        let state = STATE
            .lock()
            .map_err(|_| "Official refresh lock unavailable")?;
        if state.cancelled {
            return Err("Official model refresh cancelled".into());
        }
        write()
    }
}
impl Drop for Refresh {
    fn drop(&mut self) {
        if let Ok(mut state) = STATE.lock() {
            *state = State::default();
        }
    }
}
pub(crate) fn cancel() -> Result<(), String> {
    let mut state = STATE
        .lock()
        .map_err(|_| "Official refresh lock unavailable")?;
    if state.running {
        state.cancelled = true;
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
    let budget = timeout()?;
    let deadline = Instant::now() + budget;
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
    std::thread::spawn(move || {
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
    let _ = child.kill();
    let _ = child.wait();
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
        let refresh = Refresh::begin().unwrap();
        let canceller = std::thread::spawn(|| {
            std::thread::sleep(Duration::from_millis(150));
            cancel().unwrap();
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
                Ok(())
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
}
