use crate::AppStatus;
use crate::gateway_transaction::{
    GatewayLifecyclePhase, LifecycleTransactionGate,
};
use std::path::Path;
use std::sync::{Mutex, MutexGuard, OnceLock};

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct GatewayIdentity {
    pub pid: u32,
    pub port: u16,
    pub script_path: String,
    pub script_sha256: Option<String>,
    pub started_at_unix_ms: u64,
}

#[derive(Debug)]
pub(crate) struct GatewayLifecycleSnapshot {
    pub(crate) status: AppStatus,
    pub(crate) identity: Option<GatewayIdentity>,
}

#[derive(Debug)]
pub(crate) struct GatewayStartOutcome {
    pub(crate) snapshot: GatewayLifecycleSnapshot,
    pub(crate) spawned: bool,
}

pub(crate) trait GatewayLifecycleBackend {
    fn lifecycle_gate_path(&self) -> &Path;
    fn snapshot(&self) -> Result<GatewayLifecycleSnapshot, String>;
    fn transitional_status(&self, phase: GatewayLifecyclePhase) -> Result<AppStatus, String>;
    fn start(&self) -> Result<GatewayStartOutcome, String>;
    fn stop(&self) -> Result<AppStatus, String>;

    fn can_reuse(&self, _snapshot: &GatewayLifecycleSnapshot) -> bool {
        true
    }
}

type LifecyclePhase = GatewayLifecyclePhase;

#[derive(Debug)]
struct GatewayLifecycleState {
    phase: LifecyclePhase,
    published: Option<GatewayIdentity>,
    session_owned: Option<GatewayIdentity>,
    last_error: Option<String>,
}

impl Default for GatewayLifecycleState {
    fn default() -> Self {
        Self {
            phase: LifecyclePhase::Stopped,
            published: None,
            session_owned: None,
            last_error: None,
        }
    }
}

#[derive(Debug, Default)]
pub(crate) struct GatewayLifecycleCoordinator {
    state: Mutex<GatewayLifecycleState>,
}

impl GatewayLifecycleCoordinator {
    fn new() -> Self {
        Self::default()
    }

    pub(crate) fn status<B>(&self, backend: &B) -> Result<GatewayLifecycleSnapshot, String>
    where
        B: GatewayLifecycleBackend,
    {
        if let Some(phase) = LifecycleTransactionGate::inspect(backend.lifecycle_gate_path())? {
            let status = backend.transitional_status(phase)?;
            let mut state = self.lock_state();
            state.phase = phase;
            state.published = None;
            state.last_error = None;
            return Ok(GatewayLifecycleSnapshot {
                status,
                identity: None,
            });
        }
        let _transaction =
            LifecycleTransactionGate::acquire_silent(backend.lifecycle_gate_path())?;
        let mut state = self.lock_state();
        match backend.snapshot() {
            Ok(snapshot) if snapshot.identity.is_some() => {
                Self::publish_snapshot(&mut state, &snapshot, false)?;
                Ok(snapshot)
            }
            Ok(snapshot) => {
                state.phase = LifecyclePhase::Stopped;
                state.published = None;
                state.session_owned = None;
                state.last_error = None;
                Ok(snapshot)
            }
            Err(error) => Self::fail_reconciliation(&mut state, error),
        }
    }

    pub(crate) fn start<B, Prepare>(
        &self,
        backend: &B,
        prepare: Prepare,
    ) -> Result<GatewayLifecycleSnapshot, String>
    where
        B: GatewayLifecycleBackend,
        Prepare: FnOnce() -> Result<(), String>,
    {
        let _transaction = LifecycleTransactionGate::acquire(
            backend.lifecycle_gate_path(),
            LifecyclePhase::Starting,
        )?;
        {
            let mut state = self.lock_state();
            state.phase = LifecyclePhase::Starting;
            state.last_error = None;
        }

        match backend.snapshot() {
            Ok(snapshot) if snapshot.identity.is_some() && backend.can_reuse(&snapshot) => {
                let mut state = self.lock_state();
                Self::publish_snapshot(&mut state, &snapshot, false)?;
                return Ok(snapshot);
            }
            Ok(snapshot) if snapshot.identity.is_some() => {
                let mut state = self.lock_state();
                Self::publish_snapshot(&mut state, &snapshot, false)?;
                state.phase = LifecyclePhase::Starting;
            }
            Ok(_) => {
                let mut state = self.lock_state();
                Self::clear_stopped(&mut state);
                state.phase = LifecyclePhase::Starting;
            }
            Err(error) => {
                let mut state = self.lock_state();
                return Self::fail_reconciliation(&mut state, error);
            }
        }

        if let Err(error) = prepare() {
            let mut state = self.lock_state();
            return Self::fail_preserving_identity(&mut state, error);
        }

        match backend.start() {
            Ok(outcome) => {
                let mut state = self.lock_state();
                Self::publish_snapshot(&mut state, &outcome.snapshot, outcome.spawned)?;
                Ok(outcome.snapshot)
            }
            Err(error) => {
                let mut state = self.lock_state();
                Self::fail(&mut state, error)
            }
        }
    }

    pub(crate) fn stop<B>(&self, backend: &B) -> Result<AppStatus, String>
    where
        B: GatewayLifecycleBackend,
    {
        let _transaction = LifecycleTransactionGate::acquire(
            backend.lifecycle_gate_path(),
            LifecyclePhase::Stopping,
        )?;
        {
            let mut state = self.lock_state();
            state.phase = LifecyclePhase::Stopping;
            state.last_error = None;
        }

        match backend.stop() {
            Ok(status) if !status.proxy_running => {
                let mut state = self.lock_state();
                Self::clear_stopped(&mut state);
                Ok(status)
            }
            Ok(status) => {
                let mut state = self.lock_state();
                Self::fail_preserving_identity(&mut state, status.message)
            }
            Err(error) => {
                let mut state = self.lock_state();
                Self::fail_reconciliation(&mut state, error)
            }
        }
    }

    pub(crate) fn restart<B, Prepare>(
        &self,
        backend: &B,
        prepare: Prepare,
    ) -> Result<GatewayLifecycleSnapshot, String>
    where
        B: GatewayLifecycleBackend,
        Prepare: FnOnce() -> Result<(), String>,
    {
        let _transaction = LifecycleTransactionGate::acquire(
            backend.lifecycle_gate_path(),
            LifecyclePhase::Restarting,
        )?;
        {
            let mut state = self.lock_state();
            state.phase = LifecyclePhase::Restarting;
            state.last_error = None;
        }

        match backend.snapshot() {
            Ok(snapshot) if snapshot.identity.is_some() => {
                let mut state = self.lock_state();
                Self::publish_snapshot(&mut state, &snapshot, false)?;
                state.phase = LifecyclePhase::Restarting;
            }
            Ok(_) => {
                let mut state = self.lock_state();
                Self::clear_stopped(&mut state);
                state.phase = LifecyclePhase::Restarting;
            }
            Err(error) => {
                let mut state = self.lock_state();
                return Self::fail_reconciliation(&mut state, error);
            }
        }

        if let Err(error) = prepare() {
            let mut state = self.lock_state();
            return Self::fail_preserving_identity(&mut state, error);
        }

        let stopped = match backend.stop() {
            Ok(status) => status,
            Err(error) => {
                let mut state = self.lock_state();
                return Self::fail_reconciliation(&mut state, error);
            }
        };
        if stopped.proxy_running {
            let mut state = self.lock_state();
            return Self::fail(
                &mut state,
                format!(
                    "Gateway restart refused because stop did not release port {}: {}",
                    stopped.proxy_port, stopped.message
                ),
            );
        }
        {
            let mut state = self.lock_state();
            Self::clear_stopped(&mut state);
            state.phase = LifecyclePhase::Restarting;
        }

        match backend.start() {
            Ok(outcome) => {
                let mut state = self.lock_state();
                Self::publish_snapshot(&mut state, &outcome.snapshot, outcome.spawned)?;
                Ok(outcome.snapshot)
            }
            Err(error) => {
                let mut state = self.lock_state();
                Self::fail(&mut state, error)
            }
        }
    }

    #[cfg(test)]
    fn published_identity(&self) -> Option<GatewayIdentity> {
        self.lock_state().published.clone()
    }

    pub(crate) fn session_owned_identity(&self) -> Option<GatewayIdentity> {
        self.lock_state().session_owned.clone()
    }

    #[cfg(test)]
    fn phase(&self) -> LifecyclePhase {
        self.lock_state().phase
    }

    fn lock_state(&self) -> MutexGuard<'_, GatewayLifecycleState> {
        match self.state.lock() {
            Ok(state) => state,
            Err(poisoned) => {
                let mut state = poisoned.into_inner();
                state.phase = LifecyclePhase::Failed;
                state.published = None;
                state.last_error = Some(
                    "Gateway lifecycle state recovered after an internal panic; reconciliation is required"
                        .to_string(),
                );
                self.state.clear_poison();
                state
            }
        }
    }

    fn clear_stopped(state: &mut GatewayLifecycleState) {
        state.phase = LifecyclePhase::Stopped;
        state.published = None;
        state.session_owned = None;
        state.last_error = None;
    }

    fn publish_snapshot(
        state: &mut GatewayLifecycleState,
        snapshot: &GatewayLifecycleSnapshot,
        spawned: bool,
    ) -> Result<(), String> {
        let Some(identity) = snapshot.identity.clone() else {
            return Self::fail(
                state,
                "Gateway lifecycle refused to publish Running without a reconciled identity"
                    .to_string(),
            );
        };
        if !snapshot.status.proxy_running {
            return Self::fail(
                state,
                "Gateway lifecycle refused to publish an identity while health is not running"
                    .to_string(),
            );
        }

        if state.session_owned.as_ref() != Some(&identity) {
            state.session_owned = None;
        }
        if spawned {
            state.session_owned = Some(identity.clone());
        }
        state.published = Some(identity);
        state.phase = LifecyclePhase::Running;
        state.last_error = None;
        Ok(())
    }

    fn fail<T>(state: &mut GatewayLifecycleState, error: String) -> Result<T, String> {
        state.phase = LifecyclePhase::Failed;
        state.published = None;
        state.session_owned = None;
        state.last_error = Some(error.clone());
        Err(error)
    }

    fn fail_preserving_identity<T>(
        state: &mut GatewayLifecycleState,
        error: String,
    ) -> Result<T, String> {
        state.phase = LifecyclePhase::Failed;
        state.last_error = Some(error.clone());
        Err(error)
    }

    fn fail_reconciliation<T>(
        state: &mut GatewayLifecycleState,
        error: String,
    ) -> Result<T, String> {
        state.phase = LifecyclePhase::Failed;
        state.published = None;
        state.last_error = Some(error.clone());
        Err(error)
    }
}

static GATEWAY_LIFECYCLE: OnceLock<GatewayLifecycleCoordinator> = OnceLock::new();

pub(crate) fn coordinator() -> &'static GatewayLifecycleCoordinator {
    GATEWAY_LIFECYCLE.get_or_init(GatewayLifecycleCoordinator::new)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::gateway_transaction::{GatewayLifecyclePhase, LifecycleTransactionGate};
    use std::path::{Path, PathBuf};
    use std::sync::atomic::{AtomicU64, Ordering};
    use std::sync::{mpsc, Arc, Barrier, Mutex};
    use std::thread;
    use std::time::Duration;

    #[derive(Clone)]
    struct PauseControl {
        entered: Arc<Barrier>,
        release: Arc<Barrier>,
    }

    impl PauseControl {
        fn new() -> Self {
            Self {
                entered: Arc::new(Barrier::new(2)),
                release: Arc::new(Barrier::new(2)),
            }
        }

        fn hold(&self) {
            self.entered.wait();
            self.release.wait();
        }
    }

    #[test]
    fn coordinator_coalesces_concurrent_starts_to_one_spawn_and_identity() {
        let coordinator = Arc::new(GatewayLifecycleCoordinator::new());
        let pause = PauseControl::new();
        let backend = Arc::new(FakeLifecycleBackend::stopped().pause_next_start(pause.clone()));

        let first_coordinator = Arc::clone(&coordinator);
        let first_backend = Arc::clone(&backend);
        let first =
            thread::spawn(move || first_coordinator.start(first_backend.as_ref(), || Ok(())));
        pause.entered.wait();

        let second_coordinator = Arc::clone(&coordinator);
        let second_backend = Arc::clone(&backend);
        let (second_attempt_tx, second_attempt_rx) = mpsc::channel();
        let (second_done_tx, second_done_rx) = mpsc::channel();
        let second = thread::spawn(move || {
            second_attempt_tx.send(()).expect("publish second attempt");
            let result = second_coordinator.start(second_backend.as_ref(), || Ok(()));
            second_done_tx.send(()).expect("publish second completion");
            result
        });
        second_attempt_rx
            .recv_timeout(Duration::from_secs(1))
            .expect("second contender attempted start");
        assert!(matches!(
            second_done_rx.recv_timeout(Duration::from_millis(150)),
            Err(mpsc::RecvTimeoutError::Timeout)
        ));
        pause.release.wait();

        let first = first
            .join()
            .expect("first start thread")
            .expect("first start");
        let second = second
            .join()
            .expect("second start thread")
            .expect("second start");

        assert_eq!(backend.spawn_count(), 1);
        assert_eq!(first.identity, second.identity);
        assert_eq!(first.status.message, second.status.message);
        assert_eq!(coordinator.session_owned_identity(), first.identity);
    }

    #[test]
    fn coordinator_reuses_healthy_managed_identity_without_preparing_or_spawning() {
        let identity = fake_identity(41);
        let coordinator = GatewayLifecycleCoordinator::new();
        let backend = FakeLifecycleBackend::running(identity.clone());
        let prepare_calls = std::cell::Cell::new(0);

        let snapshot = coordinator
            .start(&backend, || {
                prepare_calls.set(prepare_calls.get() + 1);
                Ok(())
            })
            .expect("reuse running Gateway");

        assert_eq!(snapshot.identity, Some(identity));
        assert_eq!(backend.spawn_count(), 0);
        assert_eq!(prepare_calls.get(), 0);
        assert_eq!(coordinator.session_owned_identity(), None);
    }

    #[test]
    fn coordinator_replaces_healthy_identity_that_backend_marks_non_reusable() {
        let coordinator = GatewayLifecycleCoordinator::new();
        let backend = FakeLifecycleBackend::running(fake_identity(41))
            .with_next_pid(42)
            .replace_on_start();
        let prepare_calls = std::cell::Cell::new(0);

        let snapshot = coordinator
            .start(&backend, || {
                prepare_calls.set(prepare_calls.get() + 1);
                Ok(())
            })
            .expect("replace incompatible managed Gateway");

        assert_eq!(snapshot.identity, Some(fake_identity(42)));
        assert_eq!(backend.spawn_count(), 1);
        assert_eq!(prepare_calls.get(), 1);
        assert_eq!(coordinator.session_owned_identity(), snapshot.identity);
    }

    #[test]
    fn coordinator_restart_owns_stop_to_replacement_boundary_against_start() {
        let old_identity = fake_identity(51);
        let coordinator = Arc::new(GatewayLifecycleCoordinator::new());
        let pause = PauseControl::new();
        let backend = Arc::new(
            FakeLifecycleBackend::running(old_identity)
                .with_next_pid(52)
                .pause_next_stop(pause.clone()),
        );

        let restart_coordinator = Arc::clone(&coordinator);
        let restart_backend = Arc::clone(&backend);
        let restart =
            thread::spawn(move || restart_coordinator.restart(restart_backend.as_ref(), || Ok(())));
        pause.entered.wait();

        let start_coordinator = Arc::clone(&coordinator);
        let start_backend = Arc::clone(&backend);
        let (start_attempt_tx, start_attempt_rx) = mpsc::channel();
        let (start_done_tx, start_done_rx) = mpsc::channel();
        let concurrent_start = thread::spawn(move || {
            start_attempt_tx.send(()).expect("publish start attempt");
            let result = start_coordinator.start(start_backend.as_ref(), || Ok(()));
            start_done_tx.send(()).expect("publish start completion");
            result
        });
        start_attempt_rx
            .recv_timeout(Duration::from_secs(1))
            .expect("concurrent start attempted");
        assert!(matches!(
            start_done_rx.recv_timeout(Duration::from_millis(150)),
            Err(mpsc::RecvTimeoutError::Timeout)
        ));
        pause.release.wait();

        let replacement = restart
            .join()
            .expect("restart thread")
            .expect("restart replacement");
        let reused = concurrent_start
            .join()
            .expect("concurrent start thread")
            .expect("concurrent start");

        assert_eq!(replacement.identity, Some(fake_identity(52)));
        assert_eq!(reused.identity, replacement.identity);
        assert_eq!(backend.spawn_count(), 1);
        assert_eq!(backend.stop_count(), 1);
        assert_eq!(
            backend.events(),
            vec!["snapshot:51", "stop:51", "spawn:52", "snapshot:52"]
        );
        assert_eq!(coordinator.session_owned_identity(), replacement.identity);
    }

    #[test]
    fn coordinator_serializes_start_then_stop_without_stale_publication() {
        let coordinator = Arc::new(GatewayLifecycleCoordinator::new());
        let pause = PauseControl::new();
        let backend = Arc::new(FakeLifecycleBackend::stopped().pause_next_start(pause.clone()));

        let start_coordinator = Arc::clone(&coordinator);
        let start_backend = Arc::clone(&backend);
        let start =
            thread::spawn(move || start_coordinator.start(start_backend.as_ref(), || Ok(())));
        pause.entered.wait();

        let stop_coordinator = Arc::clone(&coordinator);
        let stop_backend = Arc::clone(&backend);
        let (stop_attempt_tx, stop_attempt_rx) = mpsc::channel();
        let (stop_done_tx, stop_done_rx) = mpsc::channel();
        let stop = thread::spawn(move || {
            stop_attempt_tx.send(()).expect("publish stop attempt");
            let result = stop_coordinator.stop(stop_backend.as_ref());
            stop_done_tx.send(()).expect("publish stop completion");
            result
        });
        stop_attempt_rx
            .recv_timeout(Duration::from_secs(1))
            .expect("concurrent stop attempted");
        assert!(matches!(
            stop_done_rx.recv_timeout(Duration::from_millis(150)),
            Err(mpsc::RecvTimeoutError::Timeout)
        ));
        pause.release.wait();

        assert!(start.join().expect("start thread").is_ok());
        let stopped = stop.join().expect("stop thread").expect("stop");

        assert!(!stopped.proxy_running);
        assert_eq!(
            backend.events(),
            vec!["snapshot:none", "spawn:42", "stop:42"]
        );
        assert_eq!(coordinator.session_owned_identity(), None);
        assert_eq!(coordinator.phase(), LifecyclePhase::Stopped);
    }

    #[test]
    fn coordinator_failed_start_publishes_no_identity_or_session_handoff() {
        let coordinator = GatewayLifecycleCoordinator::new();
        let backend = FakeLifecycleBackend::stopped().fail_next_start("spawn failed");

        let error = coordinator
            .start(&backend, || Ok(()))
            .expect_err("start should fail");

        assert_eq!(error, "spawn failed");
        assert_eq!(coordinator.published_identity(), None);
        assert_eq!(coordinator.session_owned_identity(), None);
        assert_eq!(coordinator.phase(), LifecyclePhase::Failed);
    }

    #[test]
    fn start_from_definitively_stopped_clears_stale_session_before_preparation_failure() {
        let coordinator = GatewayLifecycleCoordinator::new();
        let backend = FakeLifecycleBackend::stopped();
        coordinator.start(&backend, || Ok(())).expect("session start");
        backend.replace_identity(None);

        let error = coordinator
            .start(&backend, || Err("preparation failed".to_string()))
            .expect_err("preparation should fail");

        assert_eq!(error, "preparation failed");
        assert_eq!(coordinator.published_identity(), None);
        assert_eq!(coordinator.session_owned_identity(), None);
    }

    #[test]
    fn status_reports_cross_process_transition_without_entering_backend_snapshot() {
        let coordinator = GatewayLifecycleCoordinator::new();
        let backend = FakeLifecycleBackend::stopped();
        let _guard = LifecycleTransactionGate::acquire(
            backend.lifecycle_gate_path(),
            GatewayLifecyclePhase::Restarting,
        )
        .expect("hold external gate");

        let snapshot = coordinator.status(&backend).expect("transition status");

        assert_eq!(
            snapshot.status.gateway_lifecycle,
            GatewayLifecyclePhase::Restarting
        );
        assert!(backend.events().is_empty());
    }

    #[test]
    fn poisoned_state_recovers_failed_without_dropping_session_handoff() {
        let coordinator = Arc::new(GatewayLifecycleCoordinator::new());
        let backend = FakeLifecycleBackend::stopped();
        let started = coordinator.start(&backend, || Ok(())).expect("session start");
        let poison_target = Arc::clone(&coordinator);
        let _ = thread::spawn(move || {
            let _guard = poison_target.state.lock().expect("state lock");
            panic!("poison lifecycle state");
        })
        .join();

        assert_eq!(coordinator.session_owned_identity(), started.identity);
        assert_eq!(coordinator.phase(), LifecyclePhase::Failed);
    }

    #[test]
    fn unsuccessful_stop_is_an_error_not_a_successful_running_status() {
        let coordinator = GatewayLifecycleCoordinator::new();
        let backend = FakeLifecycleBackend::running(fake_identity(91)).leave_running_on_stop();

        let error = coordinator
            .stop(&backend)
            .expect_err("a stop that leaves the Gateway running must fail");

        assert!(error.contains("did not stop"));
        assert_eq!(coordinator.phase(), LifecyclePhase::Failed);
    }

    #[test]
    fn coordinator_stop_error_unpublishes_running_but_preserves_safe_session_handoff() {
        let coordinator = GatewayLifecycleCoordinator::new();
        let backend = FakeLifecycleBackend::stopped();
        let started = coordinator
            .start(&backend, || Ok(()))
            .expect("session start");
        backend.fail_next_stop("stop inspection failed");

        let error = coordinator.stop(&backend).expect_err("stop should fail");

        assert_eq!(error, "stop inspection failed");
        assert_eq!(coordinator.published_identity(), None);
        assert_eq!(coordinator.session_owned_identity(), started.identity);
        assert_eq!(coordinator.phase(), LifecyclePhase::Failed);
    }

    #[test]
    fn coordinator_status_preserves_session_handoff_only_for_same_identity() {
        let coordinator = GatewayLifecycleCoordinator::new();
        let backend = FakeLifecycleBackend::stopped();
        let started = coordinator
            .start(&backend, || Ok(()))
            .expect("session start");

        let refreshed = coordinator.status(&backend).expect("status refresh");

        assert_eq!(refreshed.identity, started.identity);
        assert_eq!(coordinator.session_owned_identity(), started.identity);
    }

    #[test]
    fn coordinator_status_clears_handoff_after_external_identity_replacement() {
        let coordinator = GatewayLifecycleCoordinator::new();
        let backend = FakeLifecycleBackend::stopped();
        coordinator
            .start(&backend, || Ok(()))
            .expect("session start");
        backend.replace_identity(fake_identity(77));

        let refreshed = coordinator.status(&backend).expect("replacement status");

        assert_eq!(refreshed.identity, Some(fake_identity(77)));
        assert_eq!(coordinator.session_owned_identity(), None);
    }

    fn fake_identity(pid: u32) -> GatewayIdentity {
        GatewayIdentity {
            pid,
            port: 9099,
            script_path: "C:/CodexHub/codex_proxy.py".to_string(),
            script_sha256: Some("fake-sha256".to_string()),
            started_at_unix_ms: u64::from(pid),
        }
    }

    struct FakeLifecycleBackend {
        state: Mutex<FakeLifecycleState>,
        gate_path: PathBuf,
    }

    struct FakeLifecycleState {
        identity: Option<GatewayIdentity>,
        next_pid: u32,
        spawn_count: usize,
        stop_count: usize,
        events: Vec<String>,
        pause_next_start: Option<PauseControl>,
        pause_next_stop: Option<PauseControl>,
        fail_next_start: Option<String>,
        fail_next_stop: Option<String>,
        replace_on_start: bool,
        leave_running_on_stop: bool,
    }

    impl FakeLifecycleBackend {
        fn stopped() -> Self {
            static NEXT_GATE: AtomicU64 = AtomicU64::new(1);
            Self {
                gate_path: std::env::temp_dir().join(format!(
                    "codexhub-fake-lifecycle-{}-{}.lock",
                    std::process::id(),
                    NEXT_GATE.fetch_add(1, Ordering::Relaxed)
                )),
                state: Mutex::new(FakeLifecycleState {
                    identity: None,
                    next_pid: 42,
                    spawn_count: 0,
                    stop_count: 0,
                    events: Vec::new(),
                    pause_next_start: None,
                    pause_next_stop: None,
                    fail_next_start: None,
                    fail_next_stop: None,
                    replace_on_start: false,
                    leave_running_on_stop: false,
                }),
            }
        }

        fn running(identity: GatewayIdentity) -> Self {
            let backend = Self::stopped();
            backend.state.lock().unwrap().identity = Some(identity);
            backend
        }

        fn with_next_pid(self, pid: u32) -> Self {
            self.state.lock().unwrap().next_pid = pid;
            self
        }

        fn pause_next_start(self, pause: PauseControl) -> Self {
            self.state.lock().unwrap().pause_next_start = Some(pause);
            self
        }

        fn pause_next_stop(self, pause: PauseControl) -> Self {
            self.state.lock().unwrap().pause_next_stop = Some(pause);
            self
        }

        fn leave_running_on_stop(self) -> Self {
            self.state.lock().unwrap().leave_running_on_stop = true;
            self
        }

        fn lifecycle_gate_path(&self) -> &Path {
            &self.gate_path
        }

        fn fail_next_start(self, message: &str) -> Self {
            self.state.lock().unwrap().fail_next_start = Some(message.to_string());
            self
        }

        fn replace_on_start(self) -> Self {
            self.state.lock().unwrap().replace_on_start = true;
            self
        }

        fn fail_next_stop(&self, message: &str) {
            self.state.lock().unwrap().fail_next_stop = Some(message.to_string());
        }

        fn spawn_count(&self) -> usize {
            self.state.lock().unwrap().spawn_count
        }

        fn stop_count(&self) -> usize {
            self.state.lock().unwrap().stop_count
        }

        fn events(&self) -> Vec<String> {
            self.state.lock().unwrap().events.clone()
        }

        fn replace_identity(&self, identity: impl Into<Option<GatewayIdentity>>) {
            self.state.lock().unwrap().identity = identity.into();
        }

        fn snapshot_from_state(state: &FakeLifecycleState) -> GatewayLifecycleSnapshot {
            match &state.identity {
                Some(identity) => GatewayLifecycleSnapshot {
                    status: fake_status(
                        true,
                        format!("Gateway running with PID {}", identity.pid),
                        GatewayLifecyclePhase::Running,
                    ),
                    identity: Some(identity.clone()),
                },
                None => GatewayLifecycleSnapshot {
                    status: fake_status(
                        false,
                        "Gateway is not running",
                        GatewayLifecyclePhase::Stopped,
                    ),
                    identity: None,
                },
            }
        }
    }

    impl GatewayLifecycleBackend for FakeLifecycleBackend {
        fn lifecycle_gate_path(&self) -> &Path {
            self.lifecycle_gate_path()
        }

        fn transitional_status(
            &self,
            phase: GatewayLifecyclePhase,
        ) -> Result<AppStatus, String> {
            Ok(fake_status(false, format!("Gateway is {phase:?}"), phase))
        }

        fn snapshot(&self) -> Result<GatewayLifecycleSnapshot, String> {
            let mut state = self.state.lock().unwrap();
            let event = state
                .identity
                .as_ref()
                .map(|identity| format!("snapshot:{}", identity.pid))
                .unwrap_or_else(|| "snapshot:none".to_string());
            state.events.push(event);
            Ok(Self::snapshot_from_state(&state))
        }

        fn start(&self) -> Result<GatewayStartOutcome, String> {
            let (pause, snapshot) = {
                let mut state = self.state.lock().unwrap();
                if state.identity.is_some() && !state.replace_on_start {
                    let snapshot = Self::snapshot_from_state(&state);
                    return Ok(GatewayStartOutcome {
                        snapshot,
                        spawned: false,
                    });
                }
                state.identity = None;
                if let Some(failure) = state.fail_next_start.take() {
                    return Err(failure);
                }
                let identity = fake_identity(state.next_pid);
                state.spawn_count += 1;
                state.events.push(format!("spawn:{}", identity.pid));
                state.identity = Some(identity);
                let pause = state.pause_next_start.take();
                let snapshot = Self::snapshot_from_state(&state);
                (pause, snapshot)
            };
            if let Some(pause) = pause {
                pause.hold();
            }
            Ok(GatewayStartOutcome {
                snapshot,
                spawned: true,
            })
        }

        fn can_reuse(&self, _snapshot: &GatewayLifecycleSnapshot) -> bool {
            !self.state.lock().unwrap().replace_on_start
        }

        fn stop(&self) -> Result<AppStatus, String> {
            let (pause, leave_running) = {
                let mut state = self.state.lock().unwrap();
                if let Some(failure) = state.fail_next_stop.take() {
                    return Err(failure);
                }
                state.stop_count += 1;
                let pid = if state.leave_running_on_stop {
                    state.identity.as_ref().map(|identity| identity.pid)
                } else {
                    state.identity.take().map(|identity| identity.pid)
                };
                state.events.push(
                    pid.map(|pid| format!("stop:{pid}"))
                        .unwrap_or_else(|| "stop:none".to_string()),
                );
                (state.pause_next_stop.take(), state.leave_running_on_stop)
            };
            if let Some(pause) = pause {
                pause.hold();
            }
            Ok(fake_status(
                leave_running,
                if leave_running {
                    "Gateway did not stop"
                } else {
                    "Gateway stopped"
                },
                if leave_running {
                    GatewayLifecyclePhase::Running
                } else {
                    GatewayLifecyclePhase::Stopped
                },
            ))
        }
    }

    fn fake_status(
        running: bool,
        message: impl Into<String>,
        gateway_lifecycle: GatewayLifecyclePhase,
    ) -> AppStatus {
        AppStatus {
            mode: "custom".to_string(),
            proxy_running: running,
            proxy_port: 9099,
            proxy_build: running.then(|| "test".to_string()),
            message: message.into(),
            gateway_lifecycle,
            history_sync_status: None,
            history_sync_message: None,
        }
    }
}
