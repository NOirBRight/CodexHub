use crate::AppStatus;
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
    fn snapshot(&self) -> Result<GatewayLifecycleSnapshot, String>;
    fn start(&self) -> Result<GatewayStartOutcome, String>;
    fn stop(&self) -> Result<AppStatus, String>;

    fn can_reuse(&self, _snapshot: &GatewayLifecycleSnapshot) -> bool {
        true
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum LifecyclePhase {
    Stopped,
    Starting,
    Running,
    Stopping,
    Restarting,
    Failed,
}

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
        let mut state = self.lock_state()?;
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
        let mut state = self.lock_state()?;
        state.phase = LifecyclePhase::Starting;
        state.last_error = None;

        match backend.snapshot() {
            Ok(snapshot) if snapshot.identity.is_some() && backend.can_reuse(&snapshot) => {
                Self::publish_snapshot(&mut state, &snapshot, false)?;
                return Ok(snapshot);
            }
            Ok(_) => {}
            Err(error) => return Self::fail_reconciliation(&mut state, error),
        }

        if let Err(error) = prepare() {
            return Self::fail_preserving_identity(&mut state, error);
        }

        match backend.start() {
            Ok(outcome) => {
                Self::publish_snapshot(&mut state, &outcome.snapshot, outcome.spawned)?;
                Ok(outcome.snapshot)
            }
            Err(error) => Self::fail(&mut state, error),
        }
    }

    pub(crate) fn stop<B>(&self, backend: &B) -> Result<AppStatus, String>
    where
        B: GatewayLifecycleBackend,
    {
        let mut state = self.lock_state()?;
        state.phase = LifecyclePhase::Stopping;
        state.last_error = None;

        match backend.stop() {
            Ok(status) if !status.proxy_running => {
                state.phase = LifecyclePhase::Stopped;
                state.published = None;
                state.session_owned = None;
                Ok(status)
            }
            Ok(status) => {
                state.phase = LifecyclePhase::Failed;
                state.published = None;
                state.session_owned = None;
                state.last_error = Some(status.message.clone());
                Ok(status)
            }
            Err(error) => Self::fail_reconciliation(&mut state, error),
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
        let mut state = self.lock_state()?;
        state.phase = LifecyclePhase::Restarting;
        state.last_error = None;

        if let Err(error) = prepare() {
            return Self::fail_preserving_identity(&mut state, error);
        }

        let stopped = match backend.stop() {
            Ok(status) => status,
            Err(error) => return Self::fail_reconciliation(&mut state, error),
        };
        if stopped.proxy_running {
            return Self::fail(
                &mut state,
                format!(
                    "Gateway restart refused because stop did not release port {}: {}",
                    stopped.proxy_port, stopped.message
                ),
            );
        }
        state.published = None;
        state.session_owned = None;

        match backend.start() {
            Ok(outcome) => {
                Self::publish_snapshot(&mut state, &outcome.snapshot, outcome.spawned)?;
                Ok(outcome.snapshot)
            }
            Err(error) => Self::fail(&mut state, error),
        }
    }

    #[cfg(test)]
    fn published_identity(&self) -> Option<GatewayIdentity> {
        self.state
            .lock()
            .ok()
            .and_then(|state| state.published.clone())
    }

    pub(crate) fn session_owned_identity(&self) -> Option<GatewayIdentity> {
        self.state
            .lock()
            .ok()
            .and_then(|state| state.session_owned.clone())
    }

    #[cfg(test)]
    fn phase(&self) -> LifecyclePhase {
        self.state
            .lock()
            .map(|state| state.phase)
            .unwrap_or(LifecyclePhase::Failed)
    }

    fn lock_state(&self) -> Result<MutexGuard<'_, GatewayLifecycleState>, String> {
        self.state
            .lock()
            .map_err(|_| "Gateway lifecycle coordinator lock is poisoned".to_string())
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
    use std::sync::{Arc, Barrier, Mutex};
    use std::thread;

    #[test]
    fn coordinator_coalesces_concurrent_starts_to_one_spawn_and_identity() {
        let coordinator = Arc::new(GatewayLifecycleCoordinator::new());
        let entered_start = Arc::new(Barrier::new(2));
        let backend =
            Arc::new(FakeLifecycleBackend::stopped().pause_next_start(entered_start.clone()));

        let first_coordinator = Arc::clone(&coordinator);
        let first_backend = Arc::clone(&backend);
        let first =
            thread::spawn(move || first_coordinator.start(first_backend.as_ref(), || Ok(())));
        entered_start.wait();

        let second_coordinator = Arc::clone(&coordinator);
        let second_backend = Arc::clone(&backend);
        let second =
            thread::spawn(move || second_coordinator.start(second_backend.as_ref(), || Ok(())));

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
        let entered_stop = Arc::new(Barrier::new(2));
        let backend = Arc::new(
            FakeLifecycleBackend::running(old_identity)
                .with_next_pid(52)
                .pause_next_stop(entered_stop.clone()),
        );

        let restart_coordinator = Arc::clone(&coordinator);
        let restart_backend = Arc::clone(&backend);
        let restart =
            thread::spawn(move || restart_coordinator.restart(restart_backend.as_ref(), || Ok(())));
        entered_stop.wait();

        let start_coordinator = Arc::clone(&coordinator);
        let start_backend = Arc::clone(&backend);
        let concurrent_start =
            thread::spawn(move || start_coordinator.start(start_backend.as_ref(), || Ok(())));

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
        assert_eq!(backend.events(), vec!["stop:51", "spawn:52", "snapshot:52"]);
        assert_eq!(coordinator.session_owned_identity(), replacement.identity);
    }

    #[test]
    fn coordinator_serializes_start_then_stop_without_stale_publication() {
        let coordinator = Arc::new(GatewayLifecycleCoordinator::new());
        let entered_start = Arc::new(Barrier::new(2));
        let backend =
            Arc::new(FakeLifecycleBackend::stopped().pause_next_start(entered_start.clone()));

        let start_coordinator = Arc::clone(&coordinator);
        let start_backend = Arc::clone(&backend);
        let start =
            thread::spawn(move || start_coordinator.start(start_backend.as_ref(), || Ok(())));
        entered_start.wait();

        let stop_coordinator = Arc::clone(&coordinator);
        let stop_backend = Arc::clone(&backend);
        let stop = thread::spawn(move || stop_coordinator.stop(stop_backend.as_ref()));

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
    }

    struct FakeLifecycleState {
        identity: Option<GatewayIdentity>,
        next_pid: u32,
        spawn_count: usize,
        stop_count: usize,
        events: Vec<String>,
        pause_next_start: Option<Arc<Barrier>>,
        pause_next_stop: Option<Arc<Barrier>>,
        fail_next_start: Option<String>,
        fail_next_stop: Option<String>,
        replace_on_start: bool,
    }

    impl FakeLifecycleBackend {
        fn stopped() -> Self {
            Self {
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

        fn pause_next_start(self, barrier: Arc<Barrier>) -> Self {
            self.state.lock().unwrap().pause_next_start = Some(barrier);
            self
        }

        fn pause_next_stop(self, barrier: Arc<Barrier>) -> Self {
            self.state.lock().unwrap().pause_next_stop = Some(barrier);
            self
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

        fn replace_identity(&self, identity: GatewayIdentity) {
            self.state.lock().unwrap().identity = Some(identity);
        }

        fn snapshot_from_state(state: &FakeLifecycleState) -> GatewayLifecycleSnapshot {
            match &state.identity {
                Some(identity) => GatewayLifecycleSnapshot {
                    status: fake_status(true, format!("Gateway running with PID {}", identity.pid)),
                    identity: Some(identity.clone()),
                },
                None => GatewayLifecycleSnapshot {
                    status: fake_status(false, "Gateway is not running"),
                    identity: None,
                },
            }
        }
    }

    impl GatewayLifecycleBackend for FakeLifecycleBackend {
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
                pause.wait();
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
            let pause = {
                let mut state = self.state.lock().unwrap();
                if let Some(failure) = state.fail_next_stop.take() {
                    return Err(failure);
                }
                state.stop_count += 1;
                let pid = state.identity.take().map(|identity| identity.pid);
                state.events.push(
                    pid.map(|pid| format!("stop:{pid}"))
                        .unwrap_or_else(|| "stop:none".to_string()),
                );
                state.pause_next_stop.take()
            };
            if let Some(pause) = pause {
                pause.wait();
            }
            Ok(fake_status(false, "Gateway stopped"))
        }
    }

    fn fake_status(running: bool, message: impl Into<String>) -> AppStatus {
        AppStatus {
            mode: "custom".to_string(),
            proxy_running: running,
            proxy_port: 9099,
            proxy_build: running.then(|| "test".to_string()),
            message: message.into(),
            history_sync_status: None,
            history_sync_message: None,
        }
    }
}
