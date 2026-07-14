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
    restart_required: bool,
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

#[derive(Debug, Clone)]
struct RefreshOutcome {
    models: Vec<Model>,
    snapshot_available: bool,
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
    fn run(&self, work: impl FnOnce() -> Result<T, String>) -> Result<T, String> {
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
            return state.completed.clone().unwrap_or_else(|| {
                Err("Official refresh single-flight lost its result".to_string())
            });
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
        result
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
        Err("current Official context snapshot is unavailable; refuse to activate CodexHub Official without a safe budget".to_string())
    }
}

pub(crate) fn refresh_manual() -> Result<Vec<Model>, String> {
    refresh(RefreshTrigger::Manual).map(|outcome| outcome.models)
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
    refresh_flight().run(|| refresh_once(trigger))
}

fn refresh_once(trigger: RefreshTrigger) -> Result<RefreshOutcome, String> {
    let settings = config::get_settings()?;
    if !settings.include_official_models {
        return Ok(RefreshOutcome {
            models: Vec::new(),
            snapshot_available: false,
        });
    }

    let state_path = refresh_state_path()?;
    let now = unix_timestamp();
    let mut state = read_state(&state_path);
    hydrate_last_success_from_cached_snapshot(&mut state);

    if !should_attempt(trigger, &state, now) {
        catalog::sync_catalog()?;
        update_published_context_budgets(&mut state, published_context_budgets_from_catalog()?);
        write_state(&state_path, &state)?;
        let models = models::list_cached_official_models().unwrap_or_default();
        return Ok(RefreshOutcome {
            snapshot_available: state.last_success_at.is_some() || !models.is_empty(),
            models,
        });
    }

    record_attempt(&mut state, trigger, now, false);
    write_state(&state_path, &state)?;

    match models::refresh_official_models_direct() {
        Ok(models) => {
            state.last_success_at = Some(now);
            state.last_attempt_success = Some(true);
            write_state(&state_path, &state)?;
            catalog::sync_catalog()?;
            update_published_context_budgets(&mut state, published_context_budgets_from_catalog()?);
            write_state(&state_path, &state)?;
            Ok(RefreshOutcome {
                models,
                snapshot_available: true,
            })
        }
        Err(direct_error) => {
            state.last_attempt_success = Some(false);
            write_state(&state_path, &state)?;
            let models = models::list_cached_official_models().unwrap_or_default();
            catalog::sync_catalog()?;
            update_published_context_budgets(&mut state, published_context_budgets_from_catalog()?);
            write_state(&state_path, &state)?;
            if models.is_empty() && state.last_success_at.is_none() {
                return Err(format!(
                    "Direct Official refresh failed and no previous safe snapshot is available: {direct_error}"
                ));
            }
            Ok(RefreshOutcome {
                snapshot_available: state.last_success_at.is_some() || !models.is_empty(),
                models,
            })
        }
    }
}

fn should_attempt(trigger: RefreshTrigger, state: &OfficialRefreshState, now: u64) -> bool {
    match trigger {
        RefreshTrigger::Startup | RefreshTrigger::Manual => true,
        RefreshTrigger::Activation => state
            .last_success_at
            .map(|success| elapsed_at_least(now, success, OFFICIAL_REFRESH_INTERVAL_SECONDS))
            .unwrap_or(true),
        RefreshTrigger::Scheduled | RefreshTrigger::Resume => automatic_refresh_due(state, now),
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
    state.schema_version = 1;
    state.last_attempt_at = Some(now);
    state.last_attempt_trigger = Some(trigger.as_str().to_string());
    state.last_attempt_success = Some(success);
    if trigger.is_automatic() {
        state.last_automatic_attempt_at = Some(now);
    }
}

fn update_published_context_budgets(
    state: &mut OfficialRefreshState,
    next: BTreeMap<String, PublishedOfficialBudget>,
) {
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
    if changed {
        // The Gateway reads the newly written catalog immediately.  The Codex
        // App reads compaction configuration at normal restart, so any lower
        // or higher context/effective/auto-compaction transition is explicitly
        // restart-gated there.
        state.restart_required = true;
    }
    state.published_context_windows = next_windows;
    state.published_context_budgets = next;
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
        automatic_refresh_due, read_state, record_attempt, should_attempt,
        update_published_context_budgets, write_state, OfficialRefreshState,
        PublishedOfficialBudget, RefreshTrigger, SingleFlight, OFFICIAL_REFRESH_INTERVAL_SECONDS,
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
    fn lower_and_higher_direct_transitions_mark_codex_restart_required() {
        let mut state = OfficialRefreshState::default();
        update_published_context_budgets(
            &mut state,
            budget_map("gpt-5.6-terra", 300_000, 100, 300_000, 270_000),
        );
        assert!(!state.restart_required);

        update_published_context_budgets(
            &mut state,
            budget_map("gpt-5.6-terra", 272_000, 95, 258_400, 240_000),
        );
        assert!(state.restart_required);

        state.restart_required = false;
        update_published_context_budgets(
            &mut state,
            budget_map("gpt-5.6-terra", 400_000, 100, 400_000, 360_000),
        );
        assert!(state.restart_required);
    }

    #[test]
    fn a_changed_compaction_limit_marks_codex_restart_required_without_context_change() {
        let mut state = OfficialRefreshState::default();
        update_published_context_budgets(
            &mut state,
            budget_map("gpt-5.6-terra", 300_000, 100, 300_000, 270_000),
        );
        update_published_context_budgets(
            &mut state,
            budget_map("gpt-5.6-terra", 300_000, 80, 240_000, 210_000),
        );

        assert!(state.restart_required);
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

        assert_eq!(first.join().unwrap(), Ok(7));
        assert_eq!(second.join().unwrap(), Ok(7));
        assert_eq!(calls.load(Ordering::SeqCst), 1);
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

    fn temp_root(name: &str) -> PathBuf {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        std::env::temp_dir().join(format!("codexhub-official-refresh-{name}-{nonce}"))
    }
}
