use crate::{catalog, config, models, runtime_paths, safe_file, Model};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::{Condvar, Mutex, Once, OnceLock};
use std::thread;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

pub(crate) const OFFICIAL_REFRESH_INTERVAL_SECONDS: u64 = 12 * 60 * 60;
const REFRESH_STATE_FILE: &str = "official-refresh-state.json";
const GENERATED_CATALOG_FILE: &str = "codexhub-model-catalog.json";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum RefreshTrigger {
    Startup,
    Manual,
    Scheduled,
    Resume,
    Activation,
}

impl RefreshTrigger {
    fn as_str(self) -> &'static str {
        match self {
            Self::Startup => "startup",
            Self::Manual => "manual",
            Self::Scheduled => "scheduled",
            Self::Resume => "resume",
            Self::Activation => "activation",
        }
    }

    fn is_automatic(self) -> bool {
        !matches!(self, Self::Manual)
    }
}

#[derive(Debug, Clone, Default, Serialize, Deserialize)]
#[serde(default)]
struct OfficialRefreshState {
    schema_version: u32,
    last_success_at: Option<u64>,
    last_attempt_at: Option<u64>,
    last_automatic_attempt_at: Option<u64>,
    last_attempt_trigger: Option<String>,
    last_attempt_success: Option<bool>,
    // This is a publication fence, not a cache hint.  It becomes false before
    // acquisition and true only after the catalog and managed Codex runtime
    // overlay have both published successfully.
    publication_ready: bool,
    // Codex App has no supported restart-observation seam.  Once a managed
    // runtime projection changes, retain this durable disclosure requirement
    // so a later same-budget manual refresh cannot hide it.
    outstanding_restart_required: bool,
    // Retained for a conservative one-time migration from state written by
    // earlier builds that only tracked the raw context window.
    published_context_windows: BTreeMap<String, u32>,
    published_context_budgets: BTreeMap<String, PublishedOfficialBudget>,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
struct PublishedOfficialBudget {
    model_context_window: u32,
    effective_context_window_percent: u32,
    effective_context_window: u32,
    model_auto_compact_token_limit: u32,
}

#[derive(Debug, Clone, Serialize)]
pub(crate) struct OfficialRefreshResult {
    pub models: Vec<Model>,
    pub restart_required: bool,
}

#[derive(Debug, Clone)]
struct RefreshOutcome {
    trigger: RefreshTrigger,
    models: Vec<Model>,
    snapshot_available: bool,
    restart_required: bool,
}

#[derive(Debug, Clone)]
struct FlightRun<T> {
    result: Result<T, String>,
    joined: bool,
}

struct FlightState<T> {
    active: bool,
    completed: Option<Result<T, String>>,
}

impl<T> Default for FlightState<T> {
    fn default() -> Self {
        Self {
            active: false,
            completed: None,
        }
    }
}

struct SingleFlight<T> {
    state: Mutex<FlightState<T>>,
    completed: Condvar,
}

impl<T> Default for SingleFlight<T> {
    fn default() -> Self {
        Self {
            state: Mutex::new(FlightState::default()),
            completed: Condvar::new(),
        }
    }
}

impl<T: Clone> SingleFlight<T> {
    fn run(&self, work: impl FnOnce() -> Result<T, String>) -> FlightRun<T> {
        let mut state = self
            .state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        if state.active {
            while state.active {
                state = self
                    .completed
                    .wait(state)
                    .unwrap_or_else(|poisoned| poisoned.into_inner());
            }
            return FlightRun {
                result: state.completed.clone().unwrap_or_else(|| {
                    Err("Official refresh single-flight lost its result".to_string())
                }),
                joined: true,
            };
        }

        state.active = true;
        state.completed = None;
        drop(state);

        let result = work();
        let mut state = self
            .state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        state.active = false;
        state.completed = Some(result.clone());
        self.completed.notify_all();
        FlightRun {
            result,
            joined: false,
        }
    }
}

fn refresh_flight() -> &'static SingleFlight<RefreshOutcome> {
    static FLIGHT: OnceLock<SingleFlight<RefreshOutcome>> = OnceLock::new();
    FLIGHT.get_or_init(SingleFlight::default)
}

pub(crate) fn refresh_at_startup() -> Result<(), String> {
    refresh(RefreshTrigger::Startup).map(|_| ())
}

pub(crate) fn refresh_before_official_activation() -> Result<(), String> {
    if !config::get_settings()?.include_official_models {
        return Ok(());
    }
    let outcome = refresh(RefreshTrigger::Activation)?;
    if outcome.snapshot_available {
        Ok(())
    } else {
        Err(
            "current Official context snapshot is unavailable; refuse to activate CodexHub Official without a safe budget"
                .to_string(),
        )
    }
}

pub(crate) fn refresh_manual() -> Result<OfficialRefreshResult, String> {
    refresh(RefreshTrigger::Manual).map(|outcome| OfficialRefreshResult {
        models: outcome.models,
        restart_required: outcome.restart_required,
    })
}

pub(crate) fn refresh_after_resume() -> Result<(), String> {
    refresh(RefreshTrigger::Resume).map(|_| ())
}

pub(crate) fn start_scheduled_refresh_loop() {
    static STARTED: Once = Once::new();
    STARTED.call_once(|| {
        let _ = thread::Builder::new()
            .name("codexhub-official-refresh".to_string())
            .spawn(|| loop {
                thread::sleep(next_automatic_wait());
                if let Err(error) = refresh(RefreshTrigger::Scheduled) {
                    log::warn!("scheduled Official model refresh failed: {error}");
                }
            });
    });
}

fn refresh(trigger: RefreshTrigger) -> Result<RefreshOutcome, String> {
    refresh_with_flight(refresh_flight(), trigger, refresh_once)
}

fn refresh_with_flight<F>(
    flight: &SingleFlight<RefreshOutcome>,
    trigger: RefreshTrigger,
    mut work: F,
) -> Result<RefreshOutcome, String>
where
    F: FnMut(RefreshTrigger) -> Result<RefreshOutcome, String>,
{
    loop {
        let flight_run = flight.run(|| work(trigger));
        let outcome = flight_run.result?;
        // A manual click which joined a cache-only automatic refresh still
        // needs its own Direct acquisition.  Re-enter the same single-flight
        // after the automatic work completes rather than returning its weaker
        // no-op result to the user.
        if trigger != RefreshTrigger::Manual || !flight_run.joined || outcome.trigger == trigger {
            return Ok(outcome);
        }
    }
}

fn refresh_once(trigger: RefreshTrigger) -> Result<RefreshOutcome, String> {
    let settings = config::get_settings()?;
    if !settings.include_official_models {
        return Ok(RefreshOutcome {
            trigger,
            models: Vec::new(),
            snapshot_available: false,
            restart_required: false,
        });
    }

    let state_path = refresh_state_path()?;
    let now = unix_timestamp();
    let mut state = read_state(&state_path);
    hydrate_last_success_from_cached_snapshot(&mut state);

    if !should_attempt(trigger, &state, now) {
        let models = models::list_cached_official_models().unwrap_or_default();
        return Ok(RefreshOutcome {
            trigger,
            snapshot_available: current_published_snapshot_available(&state),
            models,
            restart_required: state.outstanding_restart_required,
        });
    }

    record_attempt(&mut state, trigger, now, false);
    write_state(&state_path, &state)?;

    match models::refresh_official_models_direct() {
        Ok(models) => {
            let publication = publish_resolved_snapshot(&state_path, &mut state, now, true)
                .map_err(|error| persist_failed_publication(&state_path, &mut state, error))?;
            Ok(RefreshOutcome {
                trigger,
                models,
                snapshot_available: true,
                restart_required: publication.restart_required,
            })
        }
        Err(direct_error) => {
            let models = models::list_cached_official_models().unwrap_or_default();
            let publication = publish_resolved_snapshot(&state_path, &mut state, now, false)
                .map_err(|publication_error| {
                    persist_failed_publication(
                        &state_path,
                        &mut state,
                        format!(
                            "Direct Official refresh failed: {direct_error}; failed to publish a degraded safe snapshot: \
                             {publication_error}"
                        ),
                    )
                })?;
            if !current_published_snapshot_available(&state) {
                return Err(format!(
                    "Direct Official refresh failed and no previous safe snapshot is available: \
                     {direct_error}"
                ));
            }
            Ok(RefreshOutcome {
                trigger,
                snapshot_available: true,
                models,
                restart_required: publication.restart_required,
            })
        }
    }
}

#[derive(Debug, Clone, Copy)]
struct PublicationOutcome {
    restart_required: bool,
}

fn publish_resolved_snapshot(
    state_path: &Path,
    state: &mut OfficialRefreshState,
    now: u64,
    direct_success: bool,
) -> Result<PublicationOutcome, String> {
    let outcome = finalize_published_snapshot(
        state,
        now,
        direct_success,
        || catalog::sync_catalog().map(|_| ()),
        published_context_budgets_from_catalog,
        config::republish_managed_codex_context_budget,
    )?;
    write_state(state_path, state)?;
    Ok(outcome)
}

fn finalize_published_snapshot<SyncCatalog, ReadBudgets, ProjectRuntime>(
    state: &mut OfficialRefreshState,
    now: u64,
    direct_success: bool,
    sync_catalog: SyncCatalog,
    read_budgets: ReadBudgets,
    project_runtime: ProjectRuntime,
) -> Result<PublicationOutcome, String>
where
    SyncCatalog: FnOnce() -> Result<(), String>,
    ReadBudgets: FnOnce() -> Result<BTreeMap<String, PublishedOfficialBudget>, String>,
    ProjectRuntime: FnOnce() -> Result<bool, String>,
{
    // The state fence remains false until every consumer has the same safe
    // snapshot.  A failed catalog or runtime publication therefore cannot
    // leave an older larger catalog accepted as current.
    sync_catalog()?;
    let next_budgets = read_budgets()?;
    if next_budgets.is_empty() {
        return Err(
            "published Official catalog contains no safe resolved context budget".to_string(),
        );
    }
    let runtime_changed = project_runtime()?;
    update_published_context_budgets(state, next_budgets);
    state.publication_ready = true;
    state.outstanding_restart_required |= runtime_changed;
    state.last_attempt_success = Some(direct_success);
    if direct_success {
        // Direct success is durable only after both catalog and runtime
        // publication succeeded.  This ordering keeps a publication failure
        // recoverable instead of treating it as a fresh snapshot for 12h.
        state.last_success_at = Some(now);
    }
    Ok(PublicationOutcome {
        restart_required: state.outstanding_restart_required,
    })
}

fn persist_failed_publication(
    state_path: &Path,
    state: &mut OfficialRefreshState,
    error: String,
) -> String {
    state.last_attempt_success = Some(false);
    state.publication_ready = false;
    match write_state(state_path, state) {
        Ok(()) => error,
        Err(persist_error) => format!(
            "{error}; additionally failed to persist the unsafe publication fence: {persist_error}"
        ),
    }
}

fn should_attempt(trigger: RefreshTrigger, state: &OfficialRefreshState, now: u64) -> bool {
    match trigger {
        RefreshTrigger::Startup | RefreshTrigger::Manual => true,
        RefreshTrigger::Activation | RefreshTrigger::Scheduled | RefreshTrigger::Resume => {
            automatic_refresh_due(state, now)
        }
    }
}

fn automatic_refresh_due(state: &OfficialRefreshState, now: u64) -> bool {
    let reference = match (state.last_success_at, state.last_automatic_attempt_at) {
        (Some(success), Some(attempt)) => Some(success.max(attempt)),
        (Some(success), None) => Some(success),
        (None, Some(attempt)) => Some(attempt),
        (None, None) => None,
    };
    reference
        .map(|timestamp| elapsed_at_least(now, timestamp, OFFICIAL_REFRESH_INTERVAL_SECONDS))
        .unwrap_or(true)
}

fn next_automatic_wait() -> Duration {
    if !config::get_settings()
        .map(|settings| settings.include_official_models)
        .unwrap_or(false)
    {
        return Duration::from_secs(OFFICIAL_REFRESH_INTERVAL_SECONDS);
    }
    let now = unix_timestamp();
    let state_path = match refresh_state_path() {
        Ok(path) => path,
        Err(_) => return Duration::from_secs(OFFICIAL_REFRESH_INTERVAL_SECONDS),
    };
    let mut state = read_state(&state_path);
    hydrate_last_success_from_cached_snapshot(&mut state);
    let reference = match (state.last_success_at, state.last_automatic_attempt_at) {
        (Some(success), Some(attempt)) => Some(success.max(attempt)),
        (Some(success), None) => Some(success),
        (None, Some(attempt)) => Some(attempt),
        (None, None) => None,
    };
    let Some(reference) = reference else {
        return Duration::ZERO;
    };
    let elapsed = now.saturating_sub(reference);
    Duration::from_secs(OFFICIAL_REFRESH_INTERVAL_SECONDS.saturating_sub(elapsed))
}

fn elapsed_at_least(now: u64, then: u64, interval_seconds: u64) -> bool {
    now.saturating_sub(then) >= interval_seconds
}

fn record_attempt(
    state: &mut OfficialRefreshState,
    trigger: RefreshTrigger,
    now: u64,
    success: bool,
) {
    state.schema_version = 3;
    state.last_attempt_at = Some(now);
    state.last_attempt_trigger = Some(trigger.as_str().to_string());
    state.last_attempt_success = Some(success);
    state.publication_ready = false;
    if trigger.is_automatic() {
        state.last_automatic_attempt_at = Some(now);
    }
}

fn update_published_context_budgets(
    state: &mut OfficialRefreshState,
    next: BTreeMap<String, PublishedOfficialBudget>,
) -> bool {
    let next_windows = next
        .iter()
        .map(|(id, budget)| (id.clone(), budget.model_context_window))
        .collect::<BTreeMap<_, _>>();
    let changed = if state.published_context_budgets.is_empty() {
        !state.published_context_windows.is_empty()
            && state.published_context_windows != next_windows
    } else {
        state.published_context_budgets != next
    };
    state.published_context_windows = next_windows;
    state.published_context_budgets = next;
    changed
}

fn published_context_budgets_from_catalog(
) -> Result<BTreeMap<String, PublishedOfficialBudget>, String> {
    let catalog_path = runtime_paths::codex_home_dir()?
        .join("model-catalogs")
        .join(GENERATED_CATALOG_FILE);
    let text = fs::read_to_string(&catalog_path).map_err(|error| {
        format!(
            "failed to read published Official context catalog {}: {error}",
            catalog_path.display()
        )
    })?;
    let payload: Value = serde_json::from_str(&text).map_err(|error| {
        format!(
            "failed to parse published Official context catalog {}: {error}",
            catalog_path.display()
        )
    })?;
    let models = payload
        .get("models")
        .and_then(Value::as_array)
        .ok_or_else(|| "published Official context catalog has no model list".to_string())?;
    let mut budgets = BTreeMap::new();
    for model in models {
        let Some(model) = model.as_object() else {
            continue;
        };
        let Some(raw_slug) = model.get("slug").and_then(Value::as_str) else {
            continue;
        };
        let slug = raw_slug.strip_prefix("openai/").unwrap_or(raw_slug);
        if !slug.starts_with("gpt-") {
            continue;
        }
        let Some(metadata) = model.get("codex_proxy_metadata").and_then(Value::as_object) else {
            continue;
        };
        if metadata.get("provider").and_then(Value::as_str) != Some("openai")
            || metadata.get("upstream_name").and_then(Value::as_str) != Some("official")
        {
            continue;
        }
        let Some(budget) = metadata
            .get("official_context_budget")
            .and_then(Value::as_object)
        else {
            continue;
        };
        let source = budget.get("source").and_then(Value::as_str);
        let freshness = budget.get("freshness").and_then(Value::as_str);
        let trusted = matches!(
            (source, freshness),
            (Some("current_direct_official"), Some("fresh"))
                | (Some("degraded_last_known_official"), _)
        );
        if !trusted {
            continue;
        }
        let Some(model_context_window) = positive_budget_value(budget.get("model_context_window"))
        else {
            continue;
        };
        let Some(effective_context_window_percent) =
            positive_budget_value(budget.get("effective_context_window_percent"))
                .filter(|value| *value <= 100)
        else {
            continue;
        };
        let Some(effective_context_window) =
            positive_budget_value(budget.get("effective_context_window"))
        else {
            continue;
        };
        let Some(model_auto_compact_token_limit) =
            positive_budget_value(budget.get("model_auto_compact_token_limit"))
        else {
            continue;
        };
        budgets.insert(
            slug.to_string(),
            PublishedOfficialBudget {
                model_context_window,
                effective_context_window_percent,
                effective_context_window,
                model_auto_compact_token_limit,
            },
        );
    }
    Ok(budgets)
}

fn current_published_snapshot_available(state: &OfficialRefreshState) -> bool {
    state.publication_ready
        && !state.published_context_budgets.is_empty()
        && published_context_budgets_from_catalog()
            .map(|budgets| budgets == state.published_context_budgets)
            .unwrap_or(false)
}

fn positive_budget_value(value: Option<&Value>) -> Option<u32> {
    value
        .and_then(Value::as_u64)
        .and_then(|value| u32::try_from(value).ok())
        .filter(|value| *value > 0)
}

fn refresh_state_path() -> Result<PathBuf, String> {
    Ok(runtime_paths::codex_home_dir()?
        .join("model-catalogs")
        .join(REFRESH_STATE_FILE))
}

fn hydrate_last_success_from_cached_snapshot(state: &mut OfficialRefreshState) {
    if state.last_success_at.is_none() {
        state.last_success_at = models::cached_official_snapshot_timestamp();
    }
}

fn read_state(path: &Path) -> OfficialRefreshState {
    fs::read_to_string(path)
        .ok()
        .and_then(|text| serde_json::from_str(&text).ok())
        .unwrap_or_default()
}

fn write_state(path: &Path, state: &OfficialRefreshState) -> Result<(), String> {
    let text = serde_json::to_string_pretty(state)
        .map_err(|error| format!("failed to serialize Official refresh state: {error}"))?;
    safe_file::write_text_atomic(path, &format!("{text}\n")).map_err(|error| {
        format!(
            "failed to persist Official refresh state {}: {error}",
            path.display()
        )
    })
}

fn unix_timestamp() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs())
        .unwrap_or_default()
}

#[cfg(test)]
mod tests {
    use super::{
        automatic_refresh_due, finalize_published_snapshot, read_state, record_attempt,
        refresh_with_flight, should_attempt, update_published_context_budgets, write_state,
        OfficialRefreshState, PublishedOfficialBudget, RefreshOutcome, RefreshTrigger,
        SingleFlight, OFFICIAL_REFRESH_INTERVAL_SECONDS,
    };
    use std::collections::BTreeMap;
    use std::fs;
    use std::path::PathBuf;
    use std::sync::{
        atomic::{AtomicUsize, Ordering},
        mpsc, Arc,
    };
    use std::thread;
    use std::time::{Duration, SystemTime, UNIX_EPOCH};

    #[test]
    fn startup_and_manual_are_bounded_triggers_but_activation_waits_twelve_hours() {
        let state = OfficialRefreshState {
            last_success_at: Some(1_000),
            ..OfficialRefreshState::default()
        };
        let before_due = 1_000 + OFFICIAL_REFRESH_INTERVAL_SECONDS - 1;

        assert!(should_attempt(RefreshTrigger::Startup, &state, before_due));
        assert!(should_attempt(RefreshTrigger::Manual, &state, before_due));
        assert!(!should_attempt(
            RefreshTrigger::Activation,
            &state,
            before_due
        ));
        assert!(should_attempt(
            RefreshTrigger::Activation,
            &state,
            1_000 + OFFICIAL_REFRESH_INTERVAL_SECONDS,
        ));
    }

    #[test]
    fn failed_automatic_attempt_cannot_enter_a_tight_loop() {
        let mut state = OfficialRefreshState::default();
        record_attempt(&mut state, RefreshTrigger::Scheduled, 1_000, false);

        assert_eq!(state.last_attempt_at, Some(1_000));
        assert_eq!(state.last_automatic_attempt_at, Some(1_000));
        assert_eq!(state.last_attempt_trigger.as_deref(), Some("scheduled"));
        assert_eq!(state.last_attempt_success, Some(false));
        assert!(!automatic_refresh_due(&state, 1_001));
        assert!(automatic_refresh_due(
            &state,
            1_000 + OFFICIAL_REFRESH_INTERVAL_SECONDS,
        ));
        assert!(!should_attempt(RefreshTrigger::Resume, &state, 1_001));
        assert!(should_attempt(
            RefreshTrigger::Resume,
            &state,
            1_000 + OFFICIAL_REFRESH_INTERVAL_SECONDS,
        ));
        assert!(should_attempt(RefreshTrigger::Manual, &state, 1_001));
        assert!(should_attempt(RefreshTrigger::Startup, &state, 1_001));
    }

    #[test]
    fn activation_honors_the_failed_automatic_attempt_bound() {
        let mut state = OfficialRefreshState::default();
        record_attempt(&mut state, RefreshTrigger::Scheduled, 1_000, false);

        assert!(!should_attempt(RefreshTrigger::Activation, &state, 1_001));
        assert!(should_attempt(
            RefreshTrigger::Activation,
            &state,
            1_000 + OFFICIAL_REFRESH_INTERVAL_SECONDS,
        ));
    }

    #[test]
    fn lower_and_higher_direct_transitions_are_detected_for_runtime_projection() {
        let mut state = OfficialRefreshState::default();
        assert!(!update_published_context_budgets(
            &mut state,
            budget_map("gpt-5.6-terra", 300_000, 100, 300_000, 270_000),
        ));

        assert!(update_published_context_budgets(
            &mut state,
            budget_map("gpt-5.6-terra", 272_000, 95, 258_400, 240_000),
        ));

        assert!(update_published_context_budgets(
            &mut state,
            budget_map("gpt-5.6-terra", 400_000, 100, 400_000, 360_000),
        ));
    }

    #[test]
    fn a_changed_compaction_limit_marks_codex_restart_required_without_context_change() {
        let mut state = OfficialRefreshState::default();
        assert!(!update_published_context_budgets(
            &mut state,
            budget_map("gpt-5.6-terra", 300_000, 100, 300_000, 270_000),
        ));
        assert!(update_published_context_budgets(
            &mut state,
            budget_map("gpt-5.6-terra", 300_000, 80, 240_000, 210_000),
        ));

        assert_eq!(
            state
                .published_context_budgets
                .get("gpt-5.6-terra")
                .map(|budget| budget.model_auto_compact_token_limit),
            Some(210_000)
        );
    }

    #[test]
    fn concurrent_triggers_share_one_refresh_operation() {
        let flight = Arc::new(SingleFlight::<u8>::default());
        let calls = Arc::new(AtomicUsize::new(0));
        let (started_tx, started_rx) = mpsc::channel();
        let first_flight = Arc::clone(&flight);
        let first_calls = Arc::clone(&calls);
        let first = thread::spawn(move || {
            first_flight.run(|| {
                first_calls.fetch_add(1, Ordering::SeqCst);
                started_tx.send(()).unwrap();
                thread::sleep(Duration::from_millis(40));
                Ok(7)
            })
        });

        started_rx.recv().unwrap();
        let second_flight = Arc::clone(&flight);
        let second_calls = Arc::clone(&calls);
        let second = thread::spawn(move || {
            second_flight.run(|| {
                second_calls.fetch_add(1, Ordering::SeqCst);
                Ok(8)
            })
        });

        let first = first.join().unwrap();
        let second = second.join().unwrap();
        assert_eq!(first.result, Ok(7));
        assert!(!first.joined);
        assert_eq!(second.result, Ok(7));
        assert!(second.joined);
        assert_eq!(calls.load(Ordering::SeqCst), 1);
    }

    #[test]
    fn manual_refresh_joining_a_cache_only_automatic_refresh_runs_directly_afterward() {
        let flight = Arc::new(SingleFlight::<RefreshOutcome>::default());
        let calls = Arc::new(AtomicUsize::new(0));
        let (automatic_started_tx, automatic_started_rx) = mpsc::channel();

        let automatic_flight = Arc::clone(&flight);
        let automatic_calls = Arc::clone(&calls);
        let automatic = thread::spawn(move || {
            refresh_with_flight(
                &automatic_flight,
                RefreshTrigger::Scheduled,
                move |trigger| {
                    automatic_calls.fetch_add(1, Ordering::SeqCst);
                    automatic_started_tx.send(()).unwrap();
                    thread::sleep(Duration::from_millis(40));
                    Ok(outcome(trigger, false))
                },
            )
        });

        automatic_started_rx.recv().unwrap();
        let manual_flight = Arc::clone(&flight);
        let manual_calls = Arc::clone(&calls);
        let manual = thread::spawn(move || {
            refresh_with_flight(&manual_flight, RefreshTrigger::Manual, move |trigger| {
                manual_calls.fetch_add(1, Ordering::SeqCst);
                Ok(outcome(trigger, true))
            })
        });

        let automatic = automatic.join().unwrap().unwrap();
        let manual = manual.join().unwrap().unwrap();
        assert_eq!(automatic.trigger, RefreshTrigger::Scheduled);
        assert!(!automatic.snapshot_available);
        assert_eq!(manual.trigger, RefreshTrigger::Manual);
        assert!(manual.snapshot_available);
        assert_eq!(calls.load(Ordering::SeqCst), 2);
    }

    #[test]
    fn publication_failure_does_not_mark_direct_success_or_leave_the_snapshot_ready() {
        let mut state = OfficialRefreshState {
            last_success_at: Some(1_000),
            publication_ready: false,
            ..OfficialRefreshState::default()
        };
        record_attempt(&mut state, RefreshTrigger::Activation, 2_000, false);

        let error = finalize_published_snapshot(
            &mut state,
            2_000,
            true,
            || Err("catalog write failed".to_string()),
            || Ok(budget_map("gpt-5.6-terra", 272_000, 95, 258_400, 240_000)),
            || Ok(true),
        )
        .expect_err("failed catalog publication must fail closed");

        assert!(error.contains("catalog write failed"));
        assert_eq!(state.last_success_at, Some(1_000));
        assert_eq!(state.last_attempt_success, Some(false));
        assert!(!state.publication_ready);
        assert!(should_attempt(RefreshTrigger::Startup, &state, 2_001));
        assert!(should_attempt(RefreshTrigger::Manual, &state, 2_001));
    }

    #[test]
    fn codex_0_144_2_model_list_without_numeric_context_fields_fails_publication_closed() {
        let fixture: serde_json::Value = serde_json::from_str(include_str!(
            "../../tests/fixtures/codex_0_144_2_model_list_without_context_fields.json"
        ))
        .expect("current Codex model/list fixture");
        let models = fixture["data"].as_array().expect("fixture model list");
        assert_eq!(models.len(), 7);
        assert!(models.iter().all(|model| {
            [
                "context_window",
                "max_context_window",
                "contextWindow",
                "maxContextWindow",
                "effective_context_window_percent",
                "effectiveContextWindowPercent",
                "auto_compact_token_limit",
                "autoCompactTokenLimit",
            ]
            .iter()
            .all(|field| model.get(*field).is_none())
        }));

        let mut state = OfficialRefreshState::default();
        record_attempt(&mut state, RefreshTrigger::Manual, 2_000, false);
        let error = finalize_published_snapshot(
            &mut state,
            2_000,
            true,
            || Ok(()),
            BTreeMap::new,
            || panic!("runtime projection must not run without a safe budget"),
        )
        .expect_err("missing numeric context authority must fail publication closed");

        assert_eq!(
            error,
            "published Official catalog contains no safe resolved context budget"
        );
        assert!(!state.publication_ready);
        assert_eq!(state.last_success_at, None);
    }

    #[test]
    fn publication_finalization_updates_runtime_before_marking_the_snapshot_current() {
        let mut state = OfficialRefreshState::default();
        record_attempt(&mut state, RefreshTrigger::Manual, 2_000, false);

        let publication = finalize_published_snapshot(
            &mut state,
            2_000,
            true,
            || Ok(()),
            || Ok(budget_map("gpt-5.6-terra", 272_000, 95, 258_400, 240_000)),
            || Ok(true),
        )
        .expect("catalog and managed runtime projection publish");

        assert!(publication.restart_required);
        assert_eq!(state.last_success_at, Some(2_000));
        assert_eq!(state.last_attempt_success, Some(true));
        assert!(state.publication_ready);
        assert_eq!(state.schema_version, 3);
    }

    #[test]
    fn scheduled_runtime_change_persists_restart_requirement_for_later_manual_same_budget_refresh() {
        let root = temp_root("scheduled-restart-requirement");
        let path = root.join("official-refresh-state.json");
        fs::create_dir_all(&root).unwrap();
        let mut state = OfficialRefreshState::default();
        update_published_context_budgets(
            &mut state,
            budget_map("gpt-5.6-terra", 353_000, 100, 353_000, 300_000),
        );

        record_attempt(&mut state, RefreshTrigger::Scheduled, 2_000, false);
        let scheduled = finalize_published_snapshot(
            &mut state,
            2_000,
            true,
            || Ok(()),
            || Ok(budget_map("gpt-5.6-terra", 272_000, 95, 258_400, 240_000)),
            || Ok(true),
        )
        .expect("scheduled refresh publishes the lower managed budget");

        assert!(scheduled.restart_required);
        assert!(state.outstanding_restart_required);
        write_state(&path, &state).expect("persist scheduled restart requirement");

        let mut resumed_state = read_state(&path);
        assert!(resumed_state.outstanding_restart_required);
        record_attempt(&mut resumed_state, RefreshTrigger::Manual, 2_001, false);
        let manual = finalize_published_snapshot(
            &mut resumed_state,
            2_001,
            true,
            || Ok(()),
            || Ok(budget_map("gpt-5.6-terra", 272_000, 95, 258_400, 240_000)),
            || Ok(false),
        )
        .expect("manual same-budget refresh publishes without another file delta");

        assert!(manual.restart_required);
        assert!(resumed_state.outstanding_restart_required);
    }

    #[test]
    fn interrupted_state_file_fails_closed_and_atomic_write_round_trips() {
        let root = temp_root("interrupted-state");
        let path = root.join("official-refresh-state.json");
        fs::create_dir_all(&root).unwrap();
        fs::write(&path, "{not-json").unwrap();
        assert!(read_state(&path).last_success_at.is_none());

        let state = OfficialRefreshState {
            schema_version: 1,
            last_success_at: Some(2_000),
            ..OfficialRefreshState::default()
        };
        write_state(&path, &state).unwrap();
        assert_eq!(read_state(&path).last_success_at, Some(2_000));
    }

    fn budget_map(
        id: &str,
        context_window: u32,
        effective_percent: u32,
        effective_context_window: u32,
        auto_compact_token_limit: u32,
    ) -> BTreeMap<String, PublishedOfficialBudget> {
        BTreeMap::from([(
            id.to_string(),
            PublishedOfficialBudget {
                model_context_window: context_window,
                effective_context_window_percent: effective_percent,
                effective_context_window,
                model_auto_compact_token_limit: auto_compact_token_limit,
            },
        )])
    }

    fn outcome(trigger: RefreshTrigger, snapshot_available: bool) -> RefreshOutcome {
        RefreshOutcome {
            trigger,
            models: Vec::new(),
            snapshot_available,
            restart_required: false,
        }
    }

    fn temp_root(name: &str) -> PathBuf {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        std::env::temp_dir().join(format!("codexhub-official-refresh-{name}-{nonce}"))
    }
}
