use serde::{Deserialize, Serialize};
use std::fs::{self, File, OpenOptions};
use std::path::{Path, PathBuf};
use std::thread;
use std::time::Duration;

const PHASE_PUBLICATION_POLL: Duration = Duration::from_millis(5);

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum GatewayLifecyclePhase {
    Unavailable,
    #[default]
    Stopped,
    Starting,
    Running,
    Stopping,
    Restarting,
    Failed,
}

impl GatewayLifecyclePhase {
    fn as_str(self) -> &'static str {
        match self {
            Self::Unavailable => "unavailable",
            Self::Stopped => "stopped",
            Self::Starting => "starting",
            Self::Running => "running",
            Self::Stopping => "stopping",
            Self::Restarting => "restarting",
            Self::Failed => "failed",
        }
    }

    fn parse(value: &str) -> Option<Self> {
        match value.trim() {
            "unavailable" => Some(Self::Unavailable),
            "stopped" => Some(Self::Stopped),
            "starting" => Some(Self::Starting),
            "running" => Some(Self::Running),
            "stopping" => Some(Self::Stopping),
            "restarting" => Some(Self::Restarting),
            "failed" => Some(Self::Failed),
            _ => None,
        }
    }
}

/// One OS-released lifecycle transaction gate shared by every CodexHub process.
///
/// This intentionally uses the platform file-lock primitive rather than the
/// repository's atomic-write lock files: there is no stale-lock deletion or
/// recovery policy, because the operating system releases ownership when the
/// process or file handle exits.
pub(crate) struct LifecycleTransactionGate {
    file: File,
    phase_path: PathBuf,
}

pub(crate) enum LifecycleGateAccess {
    Acquired(LifecycleTransactionGate),
    Held(GatewayLifecyclePhase),
}

impl LifecycleGateAccess {
    #[cfg(test)]
    pub(crate) fn held_phase(&self) -> Option<GatewayLifecyclePhase> {
        match self {
            Self::Acquired(_) => None,
            Self::Held(phase) => Some(*phase),
        }
    }
}

impl LifecycleTransactionGate {
    #[cfg(test)]
    pub(crate) fn acquire_silent(path: &Path) -> Result<Self, String> {
        let file = open_gate_file(path)?;
        lock_gate_file(&file, path)?;
        Ok(Self {
            file,
            phase_path: phase_path(path),
        })
    }

    pub(crate) fn acquire(
        path: &Path,
        phase: GatewayLifecyclePhase,
    ) -> Result<Self, String> {
        let file = open_gate_file(path)?;
        lock_gate_file(&file, path)?;
        let phase_path = phase_path(path);
        if let Err(error) = fs::write(&phase_path, phase.as_str()) {
            let _ = file.unlock();
            return Err(format!(
                "failed to publish Gateway lifecycle phase {}: {error}",
                phase_path.display()
            ));
        }
        Ok(Self { file, phase_path })
    }

    /// Atomically returns either an owned silent guard or the phase published
    /// by a lifecycle holder. Phase-less status holders are waited out rather
    /// than misclassified as corrupt after an arbitrary timeout.
    pub(crate) fn inspect_or_acquire(path: &Path) -> Result<LifecycleGateAccess, String> {
        let file = open_gate_file(path)?;
        loop {
            match file.try_lock() {
                Ok(()) => {
                    let phase_path = phase_path(path);
                    let _ = fs::remove_file(&phase_path);
                    return Ok(LifecycleGateAccess::Acquired(Self { file, phase_path }));
                }
                Err(std::fs::TryLockError::WouldBlock) => {
                    if let Ok(value) = fs::read_to_string(phase_path(path)) {
                        if let Some(phase) = GatewayLifecyclePhase::parse(&value) {
                            return Ok(LifecycleGateAccess::Held(phase));
                        }
                    }
                    notify_test_contention(path);
                    thread::sleep(PHASE_PUBLICATION_POLL);
                }
                Err(std::fs::TryLockError::Error(error)) => {
                    return Err(format!(
                        "failed to inspect Gateway lifecycle transaction gate {}: {error}",
                        path.display()
                    ))
                }
            }
        }
    }

    #[cfg(test)]
    pub(crate) fn enable_test_contention_ack(path: &Path) -> Result<PathBuf, String> {
        let watch = test_contention_watch_path(path);
        let ack = test_contention_ack_path(path);
        let _ = fs::remove_file(&ack);
        fs::write(&watch, b"watch").map_err(|error| {
            format!(
                "failed to enable lifecycle contention acknowledgement {}: {error}",
                watch.display()
            )
        })?;
        Ok(ack)
    }
}

fn lock_gate_file(file: &File, path: &Path) -> Result<(), String> {
    match file.try_lock() {
        Ok(()) => Ok(()),
        Err(std::fs::TryLockError::WouldBlock) => {
            notify_test_contention(path);
            file.lock().map_err(|error| {
                format!(
                    "failed to acquire Gateway lifecycle transaction gate {}: {error}",
                    path.display()
                )
            })
        }
        Err(std::fs::TryLockError::Error(error)) => Err(format!(
            "failed to acquire Gateway lifecycle transaction gate {}: {error}",
            path.display()
        )),
    }
}

#[cfg(test)]
fn notify_test_contention(path: &Path) {
    let watch = test_contention_watch_path(path);
    if watch.exists() {
        let _ = fs::write(test_contention_ack_path(path), b"would-block");
    }
}

#[cfg(not(test))]
fn notify_test_contention(_path: &Path) {}

#[cfg(test)]
fn test_contention_watch_path(path: &Path) -> PathBuf {
    sidecar_path(path, ".test-contention-watch")
}

#[cfg(test)]
fn test_contention_ack_path(path: &Path) -> PathBuf {
    sidecar_path(path, ".test-contention-ack")
}

impl Drop for LifecycleTransactionGate {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.phase_path);
        let _ = self.file.unlock();
    }
}

fn open_gate_file(path: &Path) -> Result<File, String> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|error| {
            format!(
                "failed to create Gateway lifecycle directory {}: {error}",
                parent.display()
            )
        })?;
    }
    OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .open(path)
        .map_err(|error| {
            format!(
                "failed to open Gateway lifecycle transaction gate {}: {error}",
                path.display()
            )
        })
}

fn phase_path(path: &Path) -> PathBuf {
    sidecar_path(path, ".phase")
}

fn sidecar_path(path: &Path, suffix: &str) -> PathBuf {
    let mut value = path.as_os_str().to_os_string();
    value.push(suffix);
    PathBuf::from(value)
}

#[cfg(test)]
mod tests {
    use super::{GatewayLifecyclePhase, LifecycleGateAccess, LifecycleTransactionGate};
    use std::fs;
    use std::path::{Path, PathBuf};
    use std::process::{Command, Stdio};
    use std::thread;
    use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

    const HELPER_ENV: &str = "CODEXHUB_LIFECYCLE_GATE_HELPER";
    const LOCK_ENV: &str = "CODEXHUB_LIFECYCLE_GATE_PATH";
    const ENTERED_ENV: &str = "CODEXHUB_LIFECYCLE_GATE_ENTERED";
    const RELEASE_ENV: &str = "CODEXHUB_LIFECYCLE_GATE_RELEASE";

    #[test]
    fn lifecycle_gate_process_helper() {
        if std::env::var_os(HELPER_ENV).is_none() {
            return;
        }
        let lock_path = PathBuf::from(std::env::var_os(LOCK_ENV).expect("helper lock path"));
        let entered = PathBuf::from(std::env::var_os(ENTERED_ENV).expect("helper entered path"));
        let release = PathBuf::from(std::env::var_os(RELEASE_ENV).expect("helper release path"));

        let _guard = LifecycleTransactionGate::acquire(
            &lock_path,
            GatewayLifecyclePhase::Starting,
        )
        .expect("helper acquire lifecycle gate");
        fs::write(&entered, b"entered").expect("publish helper entry");
        wait_until(Duration::from_secs(10), || release.exists());
    }

    #[test]
    fn lifecycle_gate_serializes_real_processes_and_reports_holder_phase() {
        let root = test_root("cross-process");
        let lock_path = root.join("lifecycle.lock");
        let first_entered = root.join("first-entered");
        let first_release = root.join("first-release");
        let second_entered = root.join("second-entered");
        let second_release = root.join("second-release");

        let mut first = spawn_helper(
            &lock_path,
            &first_entered,
            &first_release,
        );
        wait_until(Duration::from_secs(10), || first_entered.exists());
        let second_attempted = LifecycleTransactionGate::enable_test_contention_ack(&lock_path)
            .expect("enable gate-boundary contention ack");
        let mut second = spawn_helper(
            &lock_path,
            &second_entered,
            &second_release,
        );

        wait_until(Duration::from_secs(10), || second_attempted.exists());
        assert!(
            !second_entered.exists(),
            "second process must remain blocked while the first owns the production gate"
        );
        assert_eq!(
            LifecycleTransactionGate::inspect_or_acquire(&lock_path)
                .expect("inspect held gate")
                .held_phase(),
            Some(GatewayLifecyclePhase::Starting),
        );

        fs::write(&first_release, b"release").expect("release first helper");
        wait_until(Duration::from_secs(10), || second_entered.exists());
        fs::write(&second_release, b"release").expect("release second helper");
        assert!(first.wait().expect("wait first helper").success());
        assert!(second.wait().expect("wait second helper").success());
        assert_eq!(
            LifecycleTransactionGate::inspect_or_acquire(&lock_path)
                .expect("inspect released gate")
                .held_phase(),
            None,
        );
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn silent_status_holder_is_waited_out_without_phase_timeout_or_inspect_gap() {
        let root = test_root("silent-status");
        let lock_path = root.join("lifecycle.lock");
        let holder = LifecycleTransactionGate::acquire_silent(&lock_path).expect("silent holder");
        let attempted = LifecycleTransactionGate::enable_test_contention_ack(&lock_path)
            .expect("enable contention ack");
        let contender_path = lock_path.clone();
        let contender = thread::spawn(move || {
            LifecycleTransactionGate::inspect_or_acquire(&contender_path)
        });

        wait_until(Duration::from_secs(10), || attempted.exists());
        drop(holder);
        let access = contender
            .join()
            .expect("status contender thread")
            .expect("silent holder must not be misclassified");

        assert!(matches!(access, LifecycleGateAccess::Acquired(_)));
        drop(access);
        let _ = fs::remove_dir_all(root);
    }

    fn spawn_helper(
        lock_path: &Path,
        entered: &Path,
        release: &Path,
    ) -> std::process::Child {
        Command::new(std::env::current_exe().expect("current test executable"))
            .args([
                "--exact",
                "gateway_transaction::tests::lifecycle_gate_process_helper",
                "--nocapture",
            ])
            .env(HELPER_ENV, "1")
            .env(LOCK_ENV, lock_path)
            .env(ENTERED_ENV, entered)
            .env(RELEASE_ENV, release)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .expect("spawn lifecycle gate helper")
    }

    fn wait_until(timeout: Duration, condition: impl Fn() -> bool) {
        let deadline = Instant::now() + timeout;
        while Instant::now() < deadline {
            if condition() {
                return;
            }
            thread::sleep(Duration::from_millis(10));
        }
        panic!("condition was not met within {timeout:?}");
    }

    fn test_root(label: &str) -> PathBuf {
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("clock")
            .as_nanos();
        let path = std::env::temp_dir().join(format!(
            "codexhub-lifecycle-gate-{label}-{}-{nanos}",
            std::process::id()
        ));
        fs::create_dir_all(&path).expect("create test root");
        path
    }
}
