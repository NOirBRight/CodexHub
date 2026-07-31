use crate::{
    config, models, safe_file, CapabilityBinding, CapabilityProfile, Model, Provider,
    QualificationState, UpstreamFormat,
};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::collections::{HashMap, VecDeque};
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU8, Ordering};
use std::sync::{Mutex, OnceLock};
use std::time::{SystemTime, UNIX_EPOCH};

const RECOVERY_SCHEMA_VERSION: u32 = 4;
const MAX_RECOVERY_RECORD_BYTES: u64 = 64 * 1024;
const MAX_PROVIDER_SNAPSHOT_BYTES: u64 = 8 * 1024 * 1024;
const MAX_CATALOG_SNAPSHOT_BYTES: u64 = 64 * 1024 * 1024;
const DISABLED_CATALOG: &str = "{\"models\":[]}\n";
const MAX_FUTURE_CLOCK_SKEW_SECONDS: u64 = 5 * 60;
const STARTUP_UNCHECKED: u8 = 0;
const STARTUP_READY: u8 = 1;
const STARTUP_BLOCKED: u8 = 2;
const MAX_ISSUED_REVISIONS: usize = 1024;
#[cfg(not(test))]
static STARTUP_RECOVERY_STATE: AtomicU8 = AtomicU8::new(STARTUP_UNCHECKED);
#[cfg(test)]
static STARTUP_RECOVERY_STATE: AtomicU8 = AtomicU8::new(STARTUP_READY);

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(transparent)]
pub struct ProviderCatalogRevision(String);

impl ProviderCatalogRevision {
    fn unavailable() -> Self {
        Self(String::new())
    }

    pub fn as_str(&self) -> &str {
        &self.0
    }
}

#[derive(Default)]
struct ProviderCatalogRevisionRegistry {
    fingerprints: HashMap<String, String>,
    issued: VecDeque<String>,
}

static PROVIDER_CATALOG_REVISIONS: OnceLock<Mutex<ProviderCatalogRevisionRegistry>> =
    OnceLock::new();

fn provider_catalog_revision_registry() -> &'static Mutex<ProviderCatalogRevisionRegistry> {
    PROVIDER_CATALOG_REVISIONS
        .get_or_init(|| Mutex::new(ProviderCatalogRevisionRegistry::default()))
}

fn provider_state_fingerprint(paths: &config::ConfigPaths) -> Result<String, String> {
    let (surface, path) = if paths.runtime_providers_path().exists() {
        ("runtime", paths.runtime_providers_path())
    } else {
        ("bundled", paths.bundled_providers_path())
    };
    let bytes = fs::read(&path)
        .map_err(|error| format!("failed to read provider configuration revision: {error}"))?;
    let mut digest = Sha256::new();
    digest.update(surface.as_bytes());
    digest.update([0]);
    digest.update(path.as_os_str().to_string_lossy().as_bytes());
    digest.update([0]);
    digest.update(bytes);
    Ok(format!("{:x}", digest.finalize()))
}

fn issue_provider_catalog_revision(
    paths: &config::ConfigPaths,
) -> Result<ProviderCatalogRevision, String> {
    let fingerprint = provider_state_fingerprint(paths)?;
    let mut nonce = [0_u8; 32];
    getrandom::getrandom(&mut nonce)
        .map_err(|error| format!("failed to create opaque provider revision: {error}"))?;
    let token = format!("pcr1_{}", hash_bytes(&nonce));
    let mut registry = provider_catalog_revision_registry()
        .lock()
        .map_err(|_| "provider revision registry lock was poisoned".to_string())?;
    registry.fingerprints.insert(token.clone(), fingerprint);
    registry.issued.push_back(token.clone());
    while registry.issued.len() > MAX_ISSUED_REVISIONS {
        if let Some(expired) = registry.issued.pop_front() {
            registry.fingerprints.remove(&expired);
        }
    }
    Ok(ProviderCatalogRevision(token))
}

fn validate_provider_catalog_revision(
    paths: &config::ConfigPaths,
    revision: &ProviderCatalogRevision,
) -> Result<(), String> {
    let current = provider_state_fingerprint(paths)?;
    let mut registry = provider_catalog_revision_registry()
        .lock()
        .map_err(|_| "provider revision registry lock was poisoned".to_string())?;
    if registry.fingerprints.get(revision.as_str()) == Some(&current) {
        registry.fingerprints.remove(revision.as_str());
        return Ok(());
    }
    Err("provider configuration changed since this editor snapshot was loaded".to_string())
}

#[cfg(test)]
type TestPreRestorePublishHook = Box<dyn Fn()>;

#[cfg(test)]
type TestPreRestoreTargetCommitHook = Box<dyn Fn(&Path)>;

#[cfg(test)]
thread_local! {
    static TRANSACTION_FAULT_PHASE: std::cell::RefCell<Option<&'static str>> =
        const { std::cell::RefCell::new(None) };
    static TEST_PRE_RESTORE_PUBLISH_HOOK: std::cell::RefCell<Option<TestPreRestorePublishHook>> =
        std::cell::RefCell::new(None);
    static TEST_PRE_RESTORE_TARGET_COMMIT_HOOK:
        std::cell::RefCell<Option<TestPreRestoreTargetCommitHook>> =
        std::cell::RefCell::new(None);
}

thread_local! {
    static HELD_TRANSACTION_GUARDS: std::cell::RefCell<Vec<PathBuf>> =
        const { std::cell::RefCell::new(Vec::new()) };
}

struct HeldTransactionGuard(PathBuf);

impl Drop for HeldTransactionGuard {
    fn drop(&mut self) {
        HELD_TRANSACTION_GUARDS.with(|held| {
            let mut held = held.borrow_mut();
            let removed = held.pop();
            debug_assert_eq!(removed.as_ref(), Some(&self.0));
        });
    }
}

#[cfg(test)]
fn install_transaction_fault(phase: &'static str) {
    TRANSACTION_FAULT_PHASE.with(|slot| *slot.borrow_mut() = Some(phase));
}

#[cfg(test)]
fn clear_transaction_fault() {
    TRANSACTION_FAULT_PHASE.with(|slot| *slot.borrow_mut() = None);
}

fn transaction_fault(phase: &'static str) -> Result<(), String> {
    #[cfg(test)]
    if TRANSACTION_FAULT_PHASE.with(|slot| slot.borrow().as_ref() == Some(&phase)) {
        return Err(format!("injected provider/catalog fault at {phase}"));
    }
    let _ = phase;
    Ok(())
}

#[cfg(test)]
fn install_test_pre_restore_publish_hook(hook: impl Fn() + 'static) {
    TEST_PRE_RESTORE_PUBLISH_HOOK.with(|slot| *slot.borrow_mut() = Some(Box::new(hook)));
}

#[cfg(test)]
fn clear_test_pre_restore_publish_hook() {
    TEST_PRE_RESTORE_PUBLISH_HOOK.with(|slot| *slot.borrow_mut() = None);
}

#[cfg(test)]
fn invoke_test_pre_restore_publish_hook() {
    TEST_PRE_RESTORE_PUBLISH_HOOK.with(|slot| {
        if let Some(hook) = slot.borrow().as_ref() {
            hook();
        }
    });
}

#[cfg(test)]
fn install_test_pre_restore_target_commit_hook(hook: impl Fn(&Path) + 'static) {
    TEST_PRE_RESTORE_TARGET_COMMIT_HOOK.with(|slot| *slot.borrow_mut() = Some(Box::new(hook)));
}

#[cfg(test)]
fn clear_test_pre_restore_target_commit_hook() {
    TEST_PRE_RESTORE_TARGET_COMMIT_HOOK.with(|slot| *slot.borrow_mut() = None);
}

#[cfg(test)]
fn invoke_test_pre_restore_target_commit_hook(path: &Path) {
    TEST_PRE_RESTORE_TARGET_COMMIT_HOOK.with(|slot| {
        if let Some(hook) = slot.borrow().as_ref() {
            hook(path);
        }
    });
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct ProviderProtocolSwitch {
    provider_id: String,
    upstream_protocol: UpstreamFormat,
    model_ids: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ProviderCatalogTransactionOutcome {
    Committed,
    Unchanged,
    Conflict,
    RolledBack,
    RecoveryRequired,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ProviderCatalogTransactionResult {
    pub outcome: ProviderCatalogTransactionOutcome,
    pub providers: Vec<Provider>,
    pub models: Vec<Model>,
    pub revision: ProviderCatalogRevision,
    pub protocol_changed: bool,
    pub detail: Option<String>,
    pub catalog_disabled: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct ProviderCatalogSnapshot {
    pub providers: Vec<Provider>,
    pub revision: ProviderCatalogRevision,
}

trait ProviderCatalogStore {
    fn recover_pending(&mut self) -> Result<(), String>;
    fn current_providers(&self) -> Result<Vec<Provider>, String>;
    fn current_catalog(&self) -> Result<Vec<Model>, String>;
    fn generate_catalog(&mut self) -> Result<Vec<Model>, String>;
    fn prepare_recovery(&mut self) -> Result<(), String>;
    fn mark_provider_write_pending(&mut self, providers: &[Provider]) -> Result<(), String>;
    fn save_providers(&mut self, providers: Vec<Provider>) -> Result<Vec<Provider>, String>;
    fn restore_pending(&mut self) -> Result<(), String>;
    fn mark_committed(&mut self) -> Result<(), String>;
    fn ensure_recovery_required(&mut self) -> Result<(), String>;
    fn clear_recovery(&mut self) -> Result<(), String>;
    fn invalidate_catalog(&mut self) -> Result<(), String>;
}

pub fn persist_provider_catalog_state(
    providers: Vec<Provider>,
    expected_revision: ProviderCatalogRevision,
) -> Result<ProviderCatalogTransactionResult, String> {
    let paths = config::ConfigPaths::runtime()?;
    persist_provider_catalog_state_with_paths(&paths, providers, expected_revision)
}

fn persist_provider_catalog_state_with_paths(
    paths: &config::ConfigPaths,
    providers: Vec<Provider>,
    expected_revision: ProviderCatalogRevision,
) -> Result<ProviderCatalogTransactionResult, String> {
    with_transaction_guard_for_paths(paths, || {
        let mut store = RuntimeProviderCatalogStore::new_guarded(paths.clone())?;
        if let Err(detail) = validate_provider_catalog_revision(paths, &expected_revision) {
            let current = store.current_providers().unwrap_or_default();
            let mut result = conflict_result(current, detail);
            result.revision = issue_provider_catalog_revision(paths)?;
            return Ok(result);
        }
        let mut result = persist_with_store(&mut store, providers);
        if paths.provider_catalog_recovery_path().exists()
            && result.outcome != ProviderCatalogTransactionOutcome::RecoveryRequired
        {
            result = recovery_required_result(
                &mut store,
                result.providers,
                result
                    .detail
                    .unwrap_or_else(|| "provider/catalog recovery remains pending".to_string()),
                result.protocol_changed,
            );
        }
        STARTUP_RECOVERY_STATE.store(
            if result.outcome == ProviderCatalogTransactionOutcome::RecoveryRequired
                || paths.provider_catalog_recovery_path().exists()
            {
                STARTUP_BLOCKED
            } else {
                STARTUP_READY
            },
            Ordering::Release,
        );
        result.revision = issue_provider_catalog_revision(paths)?;
        Ok(result)
    })
}

pub fn get_provider_catalog_snapshot() -> Result<ProviderCatalogSnapshot, String> {
    with_transaction_guard(|| {
        let paths = config::ConfigPaths::runtime()?;
        Ok(ProviderCatalogSnapshot {
            providers: config::get_providers_with_paths(&paths)?,
            revision: issue_provider_catalog_revision(&paths)?,
        })
    })
}

pub fn initialize_startup_recovery() -> Result<(), String> {
    let paths = config::ConfigPaths::runtime()?;
    recover_before_gateway_start_with_paths(&paths, || Ok(()))
}

pub fn recover_before_gateway_start_with<T>(
    operation: impl FnOnce() -> Result<T, String>,
) -> Result<T, String> {
    let paths = config::ConfigPaths::runtime()?;
    recover_before_gateway_start_with_paths(&paths, operation)
}

fn recover_before_gateway_start_with_paths<T>(
    paths: &config::ConfigPaths,
    operation: impl FnOnce() -> Result<T, String>,
) -> Result<T, String> {
    STARTUP_RECOVERY_STATE.store(STARTUP_BLOCKED, Ordering::Release);
    let mut recovery_completed = false;
    let result = with_transaction_guard_for_paths(paths, || {
        let mut store = RuntimeProviderCatalogStore::new_guarded(paths.clone())?;
        recover_before_gateway_with_store(&mut store)?;
        recovery_completed = true;
        STARTUP_RECOVERY_STATE.store(STARTUP_READY, Ordering::Release);
        operation()
    });
    if recovery_completed && !paths.provider_catalog_recovery_path().exists() {
        STARTUP_RECOVERY_STATE.store(STARTUP_READY, Ordering::Release);
    } else {
        STARTUP_RECOVERY_STATE.store(STARTUP_BLOCKED, Ordering::Release);
    }
    result
}

pub fn require_startup_recovery() -> Result<(), String> {
    match STARTUP_RECOVERY_STATE.load(Ordering::Acquire) {
        STARTUP_READY => {
            if config::ConfigPaths::runtime()?
                .provider_catalog_recovery_path()
                .exists()
            {
                STARTUP_RECOVERY_STATE.store(STARTUP_BLOCKED, Ordering::Release);
                return Err(
                    "provider/catalog recovery is pending; operation refused fail-closed"
                        .to_string(),
                );
            }
            Ok(())
        }
        STARTUP_UNCHECKED => Err(
            "provider/catalog startup recovery has not completed; operation refused fail-closed"
                .to_string(),
        ),
        _ => Err(
            "provider/catalog startup recovery is required; operation refused fail-closed"
                .to_string(),
        ),
    }
}

pub(crate) fn with_transaction_guard<T>(
    operation: impl FnOnce() -> Result<T, String>,
) -> Result<T, String> {
    require_startup_recovery()?;
    let paths = config::ConfigPaths::runtime()?;
    with_transaction_guard_for_paths(&paths, || {
        require_startup_recovery()?;
        operation()
    })
}

pub(crate) fn with_transaction_guard_for_paths<T>(
    paths: &config::ConfigPaths,
    operation: impl FnOnce() -> Result<T, String>,
) -> Result<T, String> {
    let proxy_dir = paths.proxy_dir();
    fs::create_dir_all(&proxy_dir).map_err(|error| {
        format!("failed to create provider/catalog transaction directory: {error}")
    })?;
    let transaction_lock_path = proxy_dir.join("provider-catalog-transaction-guard");
    if HELD_TRANSACTION_GUARDS
        .with(|held| held.borrow().iter().any(|path| path == &transaction_lock_path))
    {
        return operation();
    }
    let _transaction_lock = safe_file::FileLock::acquire(&transaction_lock_path)
        .map_err(|error| format!("failed to lock provider/catalog transaction: {error}"))?;
    HELD_TRANSACTION_GUARDS.with(|held| held.borrow_mut().push(transaction_lock_path.clone()));
    let _held_transaction_guard = HeldTransactionGuard(transaction_lock_path);
    operation()
}

fn recover_before_gateway_with_store(store: &mut dyn ProviderCatalogStore) -> Result<(), String> {
    if let Err(recovery_error) = store.recover_pending() {
        return match store.ensure_recovery_required() {
            Err(marker_error) => Err(format!(
                "provider/catalog recovery could not prove a consistent state ({recovery_error}); durable catalog-disabled marker failed before invalidation: {marker_error}"
            )),
            Ok(()) => match store.invalidate_catalog() {
                Ok(()) => Err(format!(
                    "provider/catalog recovery could not prove a consistent state ({recovery_error}); generated catalog disabled fail-closed"
                )),
                Err(invalidation_error) => Err(format!(
                    "provider/catalog recovery could not prove a consistent state ({recovery_error}); fail-closed catalog invalidation also failed: {invalidation_error}"
                )),
            },
        };
    }
    Ok(())
}

pub fn recovery_pending() -> Result<bool, String> {
    Ok(config::ConfigPaths::runtime()?
        .provider_catalog_recovery_path()
        .exists())
}

fn persist_with_store(
    store: &mut dyn ProviderCatalogStore,
    providers: Vec<Provider>,
) -> ProviderCatalogTransactionResult {
    if let Err(error) = store.recover_pending() {
        let providers = store.current_providers().unwrap_or_default();
        return recovery_required_result(
            store,
            providers,
            format!("pending provider/catalog recovery failed: {error}"),
            false,
        );
    }
    let previous_providers = match store.current_providers() {
        Ok(providers) => providers,
        Err(error) => return unchanged_result(Vec::new(), Vec::new(), error, false),
    };
    let protocol_switches = detected_protocol_switches(&previous_providers, &providers);
    let protocol_changed = !protocol_switches.is_empty();
    let previous_models = match store.current_catalog().and_then(|models| {
        verify_catalog_for_providers(&models, &previous_providers)?;
        Ok(models)
    }) {
        Ok(models) => models,
        Err(readback_error) => match store.generate_catalog().and_then(|models| {
            verify_catalog_for_providers(&models, &previous_providers)?;
            Ok(models)
        }) {
            Ok(models) => models,
            Err(regeneration_error) => {
                return recovery_required_result(
                    store,
                    previous_providers,
                    format!(
                        "provider/catalog update was not started because the current catalog could not be verified ({readback_error}) or regenerated ({regeneration_error})"
                    ),
                    protocol_changed,
                )
            }
        },
    };
    if let Err(error) = store.prepare_recovery() {
        return unchanged_result(previous_providers, previous_models, error, protocol_changed);
    }
    let requested_providers = providers;
    if let Err(error) = store.mark_provider_write_pending(&requested_providers) {
        return rollback_result(
            store,
            previous_providers,
            previous_models,
            error,
            protocol_changed,
        );
    }
    let saved = match store.save_providers(requested_providers.clone()) {
        Ok(saved) => saved,
        Err(error) => {
            return rollback_result(
                store,
                previous_providers,
                previous_models,
                error,
                protocol_changed,
            )
        }
    };
    let saved_state = serde_json::to_value(&saved).ok();
    let requested_state = serde_json::to_value(&requested_providers).ok();
    let normalized_requested_state =
        serde_json::to_value(normalized_provider_state(&requested_providers)).ok();
    if saved_state != requested_state && saved_state != normalized_requested_state {
        return rollback_result(
            store,
            previous_providers,
            previous_models,
            "provider configuration readback did not match the requested state".to_string(),
            protocol_changed,
        );
    }
    let models = match store.generate_catalog() {
        Ok(models) => models,
        Err(error) => {
            return rollback_result(
                store,
                previous_providers,
                previous_models,
                error,
                protocol_changed,
            )
        }
    };
    if let Err(error) = verify_catalog_for_providers(&models, &saved) {
        return rollback_result(
            store,
            previous_providers,
            previous_models,
            error,
            protocol_changed,
        );
    }
    if let Err(error) = store.mark_committed() {
        return rollback_result(
            store,
            previous_providers,
            previous_models,
            error,
            protocol_changed,
        );
    }
    let detail = store.clear_recovery().err();
    ProviderCatalogTransactionResult {
        outcome: ProviderCatalogTransactionOutcome::Committed,
        providers: saved,
        models,
        revision: ProviderCatalogRevision::unavailable(),
        protocol_changed,
        detail,
        catalog_disabled: false,
    }
}

fn conflict_result(providers: Vec<Provider>, detail: String) -> ProviderCatalogTransactionResult {
    ProviderCatalogTransactionResult {
        outcome: ProviderCatalogTransactionOutcome::Conflict,
        providers,
        models: Vec::new(),
        revision: ProviderCatalogRevision::unavailable(),
        protocol_changed: false,
        detail: Some(detail),
        catalog_disabled: false,
    }
}

fn detected_protocol_switches(
    current_providers: &[Provider],
    next_providers: &[Provider],
) -> Vec<ProviderProtocolSwitch> {
    next_providers
        .iter()
        .filter_map(|provider| {
            let current = current_providers
                .iter()
                .find(|current| current.id == provider.id)?;
            let upstream_protocol = provider.upstream_format.clone()?;
            if current.upstream_format.as_ref() == Some(&upstream_protocol) {
                return None;
            }
            Some(ProviderProtocolSwitch {
                provider_id: provider.id.clone(),
                upstream_protocol,
                model_ids: provider
                    .models
                    .iter()
                    .filter(|model| model.enabled && model.gateway_exported)
                    .map(|model| model.id.clone())
                    .collect(),
            })
        })
        .collect()
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
enum RecoveryState {
    Preparing,
    Prepared,
    ProviderWritePending,
    CatalogWritePending,
    Committed,
    CatalogDisabled,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum RecoveryAction {
    ClearPrepared,
    RestoreProviderPrefix,
    RestoreCatalogPrefix,
    VerifyCommitted,
    RegenerateDisabled,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum RecoveryRecordShape {
    Prepared,
    ProviderCandidate,
    CatalogCandidate,
    Committed,
    Disabled,
}

#[derive(Debug, Clone, Copy)]
struct RecoveryPhase {
    marker_fault: &'static str,
    action: RecoveryAction,
    shape: RecoveryRecordShape,
}

impl RecoveryState {
    fn classify(self) -> RecoveryPhase {
        match self {
            Self::Preparing => RecoveryPhase {
                marker_fault: "write-preparing-marker",
                action: RecoveryAction::ClearPrepared,
                shape: RecoveryRecordShape::Prepared,
            },
            Self::Prepared => RecoveryPhase {
                marker_fault: "write-prepared-marker",
                action: RecoveryAction::ClearPrepared,
                shape: RecoveryRecordShape::Prepared,
            },
            Self::ProviderWritePending => RecoveryPhase {
                marker_fault: "write-provider-marker",
                action: RecoveryAction::RestoreProviderPrefix,
                shape: RecoveryRecordShape::ProviderCandidate,
            },
            Self::CatalogWritePending => RecoveryPhase {
                marker_fault: "write-catalog-marker",
                action: RecoveryAction::RestoreCatalogPrefix,
                shape: RecoveryRecordShape::CatalogCandidate,
            },
            Self::Committed => RecoveryPhase {
                marker_fault: "write-committed-marker",
                action: RecoveryAction::VerifyCommitted,
                shape: RecoveryRecordShape::Committed,
            },
            Self::CatalogDisabled => RecoveryPhase {
                marker_fault: "write-disabled-marker",
                action: RecoveryAction::RegenerateDisabled,
                shape: RecoveryRecordShape::Disabled,
            },
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum RecoveryTransition {
    FinishPreparation,
    BeginProviderPublish,
    BeginCatalogPublish,
    Commit,
}

impl RecoveryTransition {
    fn states(self) -> (RecoveryState, RecoveryState) {
        match self {
            Self::FinishPreparation => (RecoveryState::Preparing, RecoveryState::Prepared),
            Self::BeginProviderPublish => (
                RecoveryState::Prepared,
                RecoveryState::ProviderWritePending,
            ),
            Self::BeginCatalogPublish => (
                RecoveryState::ProviderWritePending,
                RecoveryState::CatalogWritePending,
            ),
            Self::Commit => (RecoveryState::CatalogWritePending, RecoveryState::Committed),
        }
    }
}

fn apply_recovery_transition(
    record: &mut RecoveryRecord,
    transition: RecoveryTransition,
) -> Result<(), String> {
    let (expected, next) = transition.states();
    if record.state != expected {
        return Err(format!(
            "provider/catalog recovery phase mismatch for {transition:?}: expected {expected:?}, found {:?}",
            record.state
        ));
    }
    record.state = next;
    Ok(())
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct FileSnapshot {
    existed: bool,
    sha256: Option<String>,
    bytes: Option<u64>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct RecoveryRecord {
    schema_version: u32,
    transaction_id: String,
    state: RecoveryState,
    created_at_unix_seconds: u64,
    providers: FileSnapshot,
    catalog: FileSnapshot,
    base_provider_state_sha256: Option<String>,
    candidate_provider_state_sha256: Option<String>,
    candidate_providers_sha256: Option<String>,
    candidate_providers_bytes: Option<u64>,
    candidate_catalog_sha256: Option<String>,
    candidate_catalog_bytes: Option<u64>,
    committed_providers_sha256: Option<String>,
    committed_catalog_sha256: Option<String>,
}

struct RecoveryRecordEnvelope {
    schema_version: u64,
}

impl<'de> Deserialize<'de> for RecoveryRecordEnvelope {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        struct EnvelopeVisitor;

        impl<'de> serde::de::Visitor<'de> for EnvelopeVisitor {
            type Value = RecoveryRecordEnvelope;

            fn expecting(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                formatter.write_str("a provider/catalog recovery object")
            }

            fn visit_map<A>(self, mut map: A) -> Result<Self::Value, A::Error>
            where
                A: serde::de::MapAccess<'de>,
            {
                let mut schema_version = None;
                let mut fields = Vec::<String>::new();
                while let Some(field) = map.next_key::<String>()? {
                    if fields.iter().any(|seen| seen == &field) {
                        return Err(<A::Error as serde::de::Error>::custom(format!(
                            "duplicate provider/catalog recovery field {field}"
                        )));
                    }
                    fields.push(field.clone());
                    if field == "schema_version" {
                        schema_version = Some(map.next_value::<u64>()?);
                    } else {
                        map.next_value::<serde::de::IgnoredAny>()?;
                    }
                }
                let schema_version = schema_version.ok_or_else(|| {
                    <A::Error as serde::de::Error>::missing_field("schema_version")
                })?;
                Ok(RecoveryRecordEnvelope { schema_version })
            }
        }

        deserializer.deserialize_map(EnvelopeVisitor)
    }
}

struct RuntimeProviderCatalogStore {
    paths: config::ConfigPaths,
}

impl RuntimeProviderCatalogStore {
    #[cfg(test)]
    fn new(paths: config::ConfigPaths) -> Self {
        Self { paths }
    }

    /// Production construction is deliberately coupled to the shared
    /// provider/catalog transaction guard. The store and its mutation trait
    /// are private to this module; direct construction remains available only
    /// to the crash-prefix tests below.
    fn new_guarded(paths: config::ConfigPaths) -> Result<Self, String> {
        let transaction_lock_path = paths.proxy_dir().join("provider-catalog-transaction-guard");
        let held = HELD_TRANSACTION_GUARDS.with(|guards| {
            guards
                .borrow()
                .iter()
                .any(|path| path == &transaction_lock_path)
        });
        if !held {
            return Err(
                "provider/catalog runtime writer requires the cross-process transaction guard"
                    .to_string(),
            );
        }
        Ok(Self { paths })
    }

    fn recovery_path(&self) -> PathBuf {
        self.paths.provider_catalog_recovery_path()
    }

    fn read_recovery(&self) -> Result<Option<RecoveryRecord>, String> {
        self.validate_transaction_paths()?;
        let path = self.recovery_path();
        if !path.exists() {
            return Ok(None);
        }
        let text = safe_file::read_single_link_text(
            &path,
            MAX_RECOVERY_RECORD_BYTES,
            "provider/catalog recovery record",
        )?;
        let schema_version = serde_json::from_str::<RecoveryRecordEnvelope>(&text)
            .map_err(|error| {
                format!("failed to parse provider/catalog recovery record: {error}")
            })?
            .schema_version;
        if schema_version != u64::from(RECOVERY_SCHEMA_VERSION) {
            return Err(format!(
                "unsupported provider/catalog recovery schema version {schema_version}"
            ));
        }
        let record: RecoveryRecord = serde_json::from_str(&text).map_err(|error| {
            format!("failed to parse provider/catalog recovery record: {error}")
        })?;
        if record.schema_version != RECOVERY_SCHEMA_VERSION {
            return Err(format!(
                "unsupported provider/catalog recovery schema version {}",
                record.schema_version
            ));
        }
        validate_recovery_record(&record)?;
        Ok(Some(record))
    }

    fn write_recovery(&self, record: &RecoveryRecord) -> Result<(), String> {
        transaction_fault(record.state.classify().marker_fault)?;
        let text = serde_json::to_string(record).map_err(|error| {
            format!("failed to serialize provider/catalog recovery record: {error}")
        })?;
        safe_file::write_private_text_atomic(
            &self.recovery_path(),
            &(text + "\n"),
            self.paths.runtime_root(),
        )
        .map_err(|error| format!("failed to persist provider/catalog recovery record: {error}"))
    }

    fn validate_transaction_paths(&self) -> Result<(), String> {
        fs::create_dir_all(self.paths.runtime_root()).map_err(|error| {
            format!(
                "failed to create provider/catalog runtime root {}: {error}",
                self.paths.runtime_root().display()
            )
        })?;
        for parent in [
            self.paths
                .runtime_providers_path()
                .parent()
                .map(Path::to_path_buf),
            self.paths
                .generated_catalog_path()
                .parent()
                .map(Path::to_path_buf),
            self.recovery_path().parent().map(Path::to_path_buf),
        ]
        .into_iter()
        .flatten()
        {
            fs::create_dir_all(&parent).map_err(|error| {
                format!(
                    "failed to create provider/catalog transaction directory {}: {error}",
                    parent.display()
                )
            })?;
        }
        for path in [
            self.paths.runtime_providers_path(),
            self.paths.generated_catalog_path(),
            self.recovery_path(),
            self.paths.provider_catalog_providers_backup_path(),
            self.paths.provider_catalog_catalog_backup_path(),
        ] {
            safe_file::validate_confined_path(&path, self.paths.runtime_root(), true)?;
        }
        Ok(())
    }

    fn verify_provider_write_prefix(&self, record: &RecoveryRecord) -> Result<(), String> {
        if !file_matches_snapshot(
            &self.paths.generated_catalog_path(),
            &record.catalog,
            MAX_CATALOG_SNAPSHOT_BYTES,
            "provider-write generated catalog",
        )? {
            return Err(
                "generated catalog diverged from this transaction's provider-write base"
                    .to_string(),
            );
        }
        let provider_is_base = file_matches_snapshot(
            &self.paths.runtime_providers_path(),
            &record.providers,
            MAX_PROVIDER_SNAPSHOT_BYTES,
            "provider-write base provider configuration",
        )?;
        let provider_is_candidate = file_matches_identity(
            &self.paths.runtime_providers_path(),
            record.candidate_providers_sha256.as_deref(),
            record.candidate_providers_bytes,
            MAX_PROVIDER_SNAPSHOT_BYTES,
            "provider-write candidate provider configuration",
        )?;
        if !provider_is_base && !provider_is_candidate {
            return Err(
                "provider configuration diverged from this transaction's exact base and candidate bytes"
                    .to_string(),
            );
        }
        Ok(())
    }

    fn verify_catalog_write_prefix(&self, record: &RecoveryRecord) -> Result<(), String> {
        let provider_is_base = file_matches_snapshot(
            &self.paths.runtime_providers_path(),
            &record.providers,
            MAX_PROVIDER_SNAPSHOT_BYTES,
            "catalog-write base provider configuration",
        )?;
        let provider_is_candidate = file_matches_identity(
            &self.paths.runtime_providers_path(),
            record.candidate_providers_sha256.as_deref(),
            record.candidate_providers_bytes,
            MAX_PROVIDER_SNAPSHOT_BYTES,
            "catalog-write candidate provider configuration",
        )?;
        if !provider_is_base && !provider_is_candidate {
            return Err(
                "provider configuration diverged from this transaction's exact catalog-write base or candidate bytes"
                    .to_string(),
            );
        }
        let catalog_is_base = file_matches_snapshot(
            &self.paths.generated_catalog_path(),
            &record.catalog,
            MAX_CATALOG_SNAPSHOT_BYTES,
            "catalog-write base catalog",
        )?;
        let catalog_is_candidate = file_matches_identity(
            &self.paths.generated_catalog_path(),
            record.candidate_catalog_sha256.as_deref(),
            record.candidate_catalog_bytes,
            MAX_CATALOG_SNAPSHOT_BYTES,
            "catalog-write candidate catalog",
        )?;
        if !catalog_is_base && !catalog_is_candidate {
            return Err(
                "generated catalog diverged from this transaction's exact base and candidate bytes"
                    .to_string(),
            );
        }
        Ok(())
    }

    fn catalog_quarantine_path(&self, transaction_id: &str) -> Result<PathBuf, String> {
        let path = self.paths.generated_catalog_path();
        let file_name = path
            .file_name()
            .and_then(|name| name.to_str())
            .ok_or_else(|| "generated catalog has no valid file name".to_string())?;
        Ok(path.with_file_name(format!(
            "{file_name}.recovery-{transaction_id}.quarantine"
        )))
    }

    fn rollback_evidence_path(
        &self,
        path: &Path,
        transaction_id: &str,
    ) -> Result<PathBuf, String> {
        let file_name = path
            .file_name()
            .and_then(|name| name.to_str())
            .ok_or_else(|| "rollback target has no valid file name".to_string())?;
        Ok(path.with_file_name(format!(
            "{file_name}.rollback-{transaction_id}.quarantine"
        )))
    }

    fn confined_file_matches_snapshot(
        &self,
        path: &Path,
        snapshot: &FileSnapshot,
        max_bytes: u64,
        label: &str,
    ) -> Result<bool, String> {
        if !snapshot.existed {
            return Ok(!path.exists());
        }
        let contents =
            safe_file::read_private_text(path, self.paths.runtime_root(), max_bytes, label)?;
        Ok(snapshot.bytes == Some(contents.len() as u64)
            && snapshot.sha256.as_deref() == Some(hash_bytes(contents.as_bytes()).as_str()))
    }

    fn confined_file_matches_identity(
        &self,
        path: &Path,
        expected_hash: Option<&str>,
        expected_bytes: Option<u64>,
        max_bytes: u64,
        label: &str,
    ) -> Result<bool, String> {
        let expected_hash =
            expected_hash.ok_or_else(|| format!("missing expected hash for {label}"))?;
        let expected_bytes =
            expected_bytes.ok_or_else(|| format!("missing expected byte length for {label}"))?;
        if !path.exists() {
            return Ok(false);
        }
        let contents =
            safe_file::read_private_text(path, self.paths.runtime_root(), max_bytes, label)?;
        Ok(contents.len() as u64 == expected_bytes
            && hash_bytes(contents.as_bytes()) == expected_hash)
    }

    fn snapshot_confined_file(
        &self,
        path: &Path,
        max_bytes: u64,
        label: &str,
    ) -> Result<FileSnapshot, String> {
        if !path.exists() {
            return Ok(FileSnapshot {
                existed: false,
                sha256: None,
                bytes: None,
            });
        }
        let contents =
            safe_file::read_private_text(path, self.paths.runtime_root(), max_bytes, label)?;
        Ok(FileSnapshot {
            existed: true,
            sha256: Some(hash_bytes(contents.as_bytes())),
            bytes: Some(contents.len() as u64),
        })
    }

    fn verify_provider_owner_before_disable(&self, record: &RecoveryRecord) -> Result<(), String> {
        let path = self.paths.runtime_providers_path();
        let is_base = self.confined_file_matches_snapshot(
            &path,
            &record.providers,
            MAX_PROVIDER_SNAPSHOT_BYTES,
            "provider configuration before catalog disable",
        )?;
        let is_candidate = match record.state {
            RecoveryState::ProviderWritePending
            | RecoveryState::CatalogWritePending
            | RecoveryState::Committed => self.confined_file_matches_identity(
                &path,
                record.candidate_providers_sha256.as_deref(),
                record.candidate_providers_bytes,
                MAX_PROVIDER_SNAPSHOT_BYTES,
                "candidate provider configuration before catalog disable",
            )?,
            _ => false,
        };
        if !is_base && !is_candidate {
            return Err(
                "provider configuration is not an exact transaction-owned base or candidate before catalog disable"
                    .to_string(),
            );
        }
        Ok(())
    }

    fn verify_disabled_prefix(&self, record: &RecoveryRecord) -> Result<(), String> {
        let providers = self.current_providers()?;
        let expected_hash = record
            .committed_providers_sha256
            .as_deref()
            .ok_or_else(|| {
                "catalog-disabled recovery record is missing provider state hash".to_string()
            })?;
        if hash_provider_state(&providers)? != expected_hash {
            return Err(
                "provider configuration changed while catalog recovery was pending".to_string(),
            );
        }
        if record.providers.existed
            && !self.confined_file_matches_snapshot(
                &self.paths.runtime_providers_path(),
                &record.providers,
                MAX_PROVIDER_SNAPSHOT_BYTES,
                "catalog-disabled provider owner",
            )?
        {
            return Err(
                "provider configuration owner bytes changed while catalog recovery was pending"
                    .to_string(),
            );
        }

        let catalog_path = self.paths.generated_catalog_path();
        let quarantine = self.catalog_quarantine_path(&record.transaction_id)?;
        let read_optional = |path: &Path, label: &str| -> Result<Option<String>, String> {
            let exists = path.try_exists().map_err(|error| {
                format!(
                    "failed to inspect catalog-disabled {label} {}: {error}",
                    path.display()
                )
            })?;
            if !exists {
                return Ok(None);
            }
            safe_file::read_private_text(
                path,
                self.paths.runtime_root(),
                MAX_CATALOG_SNAPSHOT_BYTES,
                label,
            )
            .map(Some)
        };
        let live = read_optional(&catalog_path, "generated catalog")?;
        let quarantined = read_optional(&quarantine, "quarantine evidence")?;
        let matches_snapshot = |contents: &str| {
            record.catalog.existed
                && record.catalog.bytes == Some(contents.len() as u64)
                && record.catalog.sha256.as_deref()
                    == Some(hash_bytes(contents.as_bytes()).as_str())
        };

        let legal_prefix = if !record.catalog.existed {
            matches!(
                (live.as_deref(), quarantined.as_deref()),
                (None, None) | (Some(DISABLED_CATALOG), None)
            )
        } else {
            match (live.as_deref(), quarantined.as_deref()) {
                // Initial state.
                (Some(current), None) => matches_snapshot(current),
                // Linux pre-exchange crash: exact source plus the exact sentinel placeholder.
                (Some(current), Some(placeholder)) => {
                    (matches_snapshot(current) && placeholder == DISABLED_CATALOG)
                        // Linux post-exchange crash: exact sentinel plus exact source evidence.
                        || (current == DISABLED_CATALOG && matches_snapshot(placeholder))
                }
                // Windows handle-bound rename completed before sentinel publication.
                (None, Some(evidence)) => matches_snapshot(evidence),
                _ => false,
            }
        };
        if !legal_prefix {
            return Err(
                "catalog-disabled live/quarantine bytes do not match any authorized crash prefix; recovery remains fail-closed"
                    .to_string(),
            );
        }
        Ok(())
    }
}

impl ProviderCatalogStore for RuntimeProviderCatalogStore {
    fn recover_pending(&mut self) -> Result<(), String> {
        let Some(record) = self.read_recovery()? else {
            if let Err(error) = self.clear_orphaned_backups() {
                log::warn!(
                    "provider/catalog recovery found only orphaned backups and could not remove them: {error}"
                );
            }
            return Ok(());
        };
        let recovery_action = record.state.classify().action;
        match recovery_action {
            RecoveryAction::VerifyCommitted => {
                verify_file_identity(
                    &self.paths.runtime_providers_path(),
                    record.committed_providers_sha256.as_deref(),
                    record.candidate_providers_bytes,
                    MAX_PROVIDER_SNAPSHOT_BYTES,
                    "committed provider configuration",
                )?;
                verify_file_identity(
                    &self.paths.generated_catalog_path(),
                    record.committed_catalog_sha256.as_deref(),
                    record.candidate_catalog_bytes,
                    MAX_CATALOG_SNAPSHOT_BYTES,
                    "committed generated catalog",
                )?;
                self.clear_recovery()
            }
            RecoveryAction::ClearPrepared => {
                verify_snapshot_target(
                    &self.paths.runtime_providers_path(),
                    &record.providers,
                    MAX_PROVIDER_SNAPSHOT_BYTES,
                    "prepared provider configuration",
                )?;
                verify_snapshot_target(
                    &self.paths.generated_catalog_path(),
                    &record.catalog,
                    MAX_CATALOG_SNAPSHOT_BYTES,
                    "prepared generated catalog",
                )?;
                self.clear_recovery()
            }
            RecoveryAction::RestoreProviderPrefix => {
                self.verify_provider_write_prefix(&record)?;
                self.restore_pending()?;
                self.clear_recovery()
            }
            RecoveryAction::RestoreCatalogPrefix => {
                self.verify_catalog_write_prefix(&record)?;
                self.restore_pending()?;
                self.clear_recovery()
            }
            RecoveryAction::RegenerateDisabled => {
                self.verify_disabled_prefix(&record)?;
                self.invalidate_catalog()?;
                let providers = self.current_providers()?;
                let models = self.generate_catalog()?;
                verify_catalog_for_providers(&models, &providers)?;
                self.clear_recovery()
            }
        }
    }

    fn current_providers(&self) -> Result<Vec<Provider>, String> {
        config::get_providers_with_paths(&self.paths)
    }

    fn current_catalog(&self) -> Result<Vec<Model>, String> {
        let path = self.paths.generated_catalog_path();
        let text = read_bounded(&path, MAX_CATALOG_SNAPSHOT_BYTES, "generated catalog")?;
        models::read_catalog_models_text(&text, &path)
    }

    fn generate_catalog(&mut self) -> Result<Vec<Model>, String> {
        let staged = models::stage_catalog_for_config(&self.paths)?;
        let providers = self.current_providers()?;
        verify_catalog_for_providers(staged.models(), &providers)?;
        if self
            .read_recovery()?
            .is_some_and(|record| record.state == RecoveryState::ProviderWritePending)
        {
            self.mark_catalog_write_pending(staged.catalog_text())?;
            transaction_fault("after-catalog-marker")?;
        }
        let published = models::publish_staged_catalog_for_config(&self.paths, &staged)?;
        transaction_fault("after-catalog-publish")?;
        verify_catalog_for_providers(&published, &providers)?;
        Ok(published)
    }

    fn prepare_recovery(&mut self) -> Result<(), String> {
        self.validate_transaction_paths()?;
        if self.recovery_path().exists() {
            return Err(
                "provider/catalog recovery record already exists before prepare".to_string(),
            );
        }
        self.clear_orphaned_backups()?;
        let prepared = (|| {
            let mut record = RecoveryRecord {
                schema_version: RECOVERY_SCHEMA_VERSION,
                transaction_id: transaction_id()?,
                state: RecoveryState::Preparing,
                created_at_unix_seconds: unix_timestamp_seconds()?,
                providers: snapshot_file(
                    &self.paths.runtime_providers_path(),
                    MAX_PROVIDER_SNAPSHOT_BYTES,
                    "provider configuration",
                )?,
                catalog: snapshot_file(
                    &self.paths.generated_catalog_path(),
                    MAX_CATALOG_SNAPSHOT_BYTES,
                    "generated catalog",
                )?,
                base_provider_state_sha256: Some(hash_provider_state(&self.current_providers()?)?),
                candidate_provider_state_sha256: None,
                candidate_providers_sha256: None,
                candidate_providers_bytes: None,
                candidate_catalog_sha256: None,
                candidate_catalog_bytes: None,
                committed_providers_sha256: None,
                committed_catalog_sha256: None,
            };
            // The durable marker is published before any secret-bearing backup
            // becomes visible.
            self.write_recovery(&record)?;
            transaction_fault("after-preparing-marker")?;
            capture_snapshot(
                &self.paths.runtime_providers_path(),
                &self.paths.provider_catalog_providers_backup_path(),
                &record.providers,
                MAX_PROVIDER_SNAPSHOT_BYTES,
                "provider configuration",
            )?;
            transaction_fault("after-provider-backup")?;
            capture_snapshot(
                &self.paths.generated_catalog_path(),
                &self.paths.provider_catalog_catalog_backup_path(),
                &record.catalog,
                MAX_CATALOG_SNAPSHOT_BYTES,
                "generated catalog",
            )?;
            transaction_fault("after-catalog-backup")?;
            apply_recovery_transition(&mut record, RecoveryTransition::FinishPreparation)?;
            self.write_recovery(&record)
        })();
        if prepared.is_err() {
            if let Err(error) = self.clear_orphaned_backups() {
                log::warn!("failed to clear incomplete provider/catalog recovery backups: {error}");
            }
        }
        prepared
    }

    fn mark_provider_write_pending(&mut self, providers: &[Provider]) -> Result<(), String> {
        let mut record = self.read_recovery()?.ok_or_else(|| {
            "provider/catalog recovery record disappeared before provider write".to_string()
        })?;
        apply_recovery_transition(&mut record, RecoveryTransition::BeginProviderPublish)?;
        let candidate_text = config::serialize_providers_document(providers)?;
        record.candidate_provider_state_sha256 =
            Some(hash_provider_state(&normalized_provider_state(providers))?);
        record.candidate_providers_sha256 = Some(hash_bytes(candidate_text.as_bytes()));
        record.candidate_providers_bytes = Some(candidate_text.len() as u64);
        self.write_recovery(&record)
    }

    fn save_providers(&mut self, providers: Vec<Provider>) -> Result<Vec<Provider>, String> {
        config::save_providers_with_paths(providers, &self.paths)?;
        transaction_fault("after-provider-publish")?;
        let saved = config::get_providers_with_paths(&self.paths)?;
        let record = self.read_recovery()?.ok_or_else(|| {
            "provider/catalog recovery record disappeared after provider write".to_string()
        })?;
        if !file_matches_identity(
            &self.paths.runtime_providers_path(),
            record.candidate_providers_sha256.as_deref(),
            record.candidate_providers_bytes,
            MAX_PROVIDER_SNAPSHOT_BYTES,
            "published candidate provider configuration",
        )? {
            return Err(
                "published provider configuration did not match the journaled candidate bytes"
                    .to_string(),
            );
        }
        Ok(saved)
    }

    fn restore_pending(&mut self) -> Result<(), String> {
        let Some(record) = self.read_recovery()? else {
            return Ok(());
        };
        if record.state == RecoveryState::Committed {
            return Ok(());
        }
        let recovery_action = record.state.classify().action;
        match recovery_action {
            RecoveryAction::ClearPrepared => {
                verify_snapshot_target(
                    &self.paths.runtime_providers_path(),
                    &record.providers,
                    MAX_PROVIDER_SNAPSHOT_BYTES,
                    "prepared provider configuration",
                )?;
                verify_snapshot_target(
                    &self.paths.generated_catalog_path(),
                    &record.catalog,
                    MAX_CATALOG_SNAPSHOT_BYTES,
                    "prepared generated catalog",
                )?;
            }
            RecoveryAction::RestoreProviderPrefix => {
                self.verify_provider_write_prefix(&record)?;
            }
            RecoveryAction::RestoreCatalogPrefix => {
                self.verify_catalog_write_prefix(&record)?;
            }
            RecoveryAction::VerifyCommitted | RecoveryAction::RegenerateDisabled => {
                return Err(
                    "provider/catalog recovery phase cannot restore a transaction snapshot"
                        .to_string(),
                );
            }
        }
        let provider_backup = load_snapshot_backup(
            &self.paths.provider_catalog_providers_backup_path(),
            &record.providers,
            MAX_PROVIDER_SNAPSHOT_BYTES,
            "provider configuration",
        )?;
        let catalog_backup = load_snapshot_backup(
            &self.paths.provider_catalog_catalog_backup_path(),
            &record.catalog,
            MAX_CATALOG_SNAPSHOT_BYTES,
            "generated catalog",
        )?;
        #[cfg(test)]
        invoke_test_pre_restore_publish_hook();
        let provider_rollback_owner = match recovery_action {
            RecoveryAction::RestoreProviderPrefix | RecoveryAction::RestoreCatalogPrefix => record
                .candidate_providers_sha256
                .as_deref()
                .zip(record.candidate_providers_bytes)
                .map(|(hash, bytes)| (hash, bytes, MAX_PROVIDER_SNAPSHOT_BYTES)),
            _ => None,
        };
        let catalog_rollback_owner = match recovery_action {
            RecoveryAction::RestoreCatalogPrefix => record
                .candidate_catalog_sha256
                .as_deref()
                .zip(record.candidate_catalog_bytes)
                .map(|(hash, bytes)| (hash, bytes, MAX_CATALOG_SNAPSHOT_BYTES)),
            _ => None,
        };
        let provider_evidence = self.rollback_evidence_path(
            &self.paths.runtime_providers_path(),
            &record.transaction_id,
        )?;
        let catalog_evidence = self.rollback_evidence_path(
            &self.paths.generated_catalog_path(),
            &record.transaction_id,
        )?;
        #[cfg(all(
            target_os = "linux",
            any(target_arch = "x86_64", target_arch = "aarch64")
        ))]
        {
            #[cfg(test)]
            {
                if provider_rollback_owner.is_some() {
                    invoke_test_pre_restore_target_commit_hook(
                        &self.paths.runtime_providers_path(),
                    );
                }
                if catalog_rollback_owner.is_some() {
                    invoke_test_pre_restore_target_commit_hook(
                        &self.paths.generated_catalog_path(),
                    );
                }
            }
            let provider_plan = plan_restore_file_linux(
                &self.paths.runtime_providers_path(),
                &self.paths.provider_catalog_providers_backup_path(),
                &provider_evidence,
                &record.providers,
                provider_backup.as_deref(),
                provider_rollback_owner,
                "provider configuration",
            )?;
            let catalog_plan = plan_restore_file_linux(
                &self.paths.generated_catalog_path(),
                &self.paths.provider_catalog_catalog_backup_path(),
                &catalog_evidence,
                &record.catalog,
                catalog_backup.as_deref(),
                catalog_rollback_owner,
                "generated catalog",
            )?;
            safe_file::commit_private_text_rollback(provider_plan)?;
            transaction_fault("after-provider-restore")?;
            safe_file::commit_private_text_rollback(catalog_plan)?;
        }
        #[cfg(not(all(
            target_os = "linux",
            any(target_arch = "x86_64", target_arch = "aarch64")
        )))]
        {
        restore_file(
            &self.paths.runtime_providers_path(),
            &self.paths.provider_catalog_providers_backup_path(),
            &provider_evidence,
            &record.providers,
            provider_backup.as_deref(),
            provider_rollback_owner,
            "provider configuration",
        )?;
        transaction_fault("after-provider-restore")?;
        restore_file(
            &self.paths.generated_catalog_path(),
            &self.paths.provider_catalog_catalog_backup_path(),
            &catalog_evidence,
            &record.catalog,
            catalog_backup.as_deref(),
            catalog_rollback_owner,
            "generated catalog",
        )?;
        }
        verify_snapshot_target(
            &self.paths.runtime_providers_path(),
            &record.providers,
            MAX_PROVIDER_SNAPSHOT_BYTES,
            "restored provider configuration",
        )?;
        verify_snapshot_target(
            &self.paths.generated_catalog_path(),
            &record.catalog,
            MAX_CATALOG_SNAPSHOT_BYTES,
            "restored generated catalog",
        )
    }

    fn mark_committed(&mut self) -> Result<(), String> {
        let Some(mut record) = self.read_recovery()? else {
            return Err("provider/catalog recovery record disappeared before commit".to_string());
        };
        apply_recovery_transition(&mut record, RecoveryTransition::Commit)?;
        verify_file_identity(
            &self.paths.runtime_providers_path(),
            record.candidate_providers_sha256.as_deref(),
            record.candidate_providers_bytes,
            MAX_PROVIDER_SNAPSHOT_BYTES,
            "committed provider configuration",
        )?;
        verify_file_identity(
            &self.paths.generated_catalog_path(),
            record.candidate_catalog_sha256.as_deref(),
            record.candidate_catalog_bytes,
            MAX_CATALOG_SNAPSHOT_BYTES,
            "committed generated catalog",
        )?;
        record.committed_providers_sha256 = record.candidate_providers_sha256.clone();
        record.committed_catalog_sha256 = record.candidate_catalog_sha256.clone();
        self.write_recovery(&record)
    }

    fn ensure_recovery_required(&mut self) -> Result<(), String> {
        let existing = self.read_recovery()?;
        if let Some(record) = existing.as_ref() {
            if record.state == RecoveryState::CatalogDisabled {
                return self.verify_disabled_prefix(record);
            }
            self.verify_provider_owner_before_disable(record)?;
        } else {
            self.clear_orphaned_backups()?;
        }
        let providers = self.current_providers()?;
        let record = RecoveryRecord {
            schema_version: RECOVERY_SCHEMA_VERSION,
            transaction_id: existing
                .as_ref()
                .map(|record| record.transaction_id.clone())
                .map_or_else(transaction_id, Ok)?,
            state: RecoveryState::CatalogDisabled,
            created_at_unix_seconds: existing
                .as_ref()
                .map(|record| record.created_at_unix_seconds)
                .map_or_else(unix_timestamp_seconds, Ok)?,
            providers: self.snapshot_confined_file(
                &self.paths.runtime_providers_path(),
                MAX_PROVIDER_SNAPSHOT_BYTES,
                "provider owner before catalog disable",
            )?,
            catalog: self.snapshot_confined_file(
                &self.paths.generated_catalog_path(),
                MAX_CATALOG_SNAPSHOT_BYTES,
                "generated catalog before disable",
            )?,
            base_provider_state_sha256: None,
            candidate_provider_state_sha256: None,
            candidate_providers_sha256: None,
            candidate_providers_bytes: None,
            candidate_catalog_sha256: None,
            candidate_catalog_bytes: None,
            committed_providers_sha256: Some(hash_provider_state(&providers)?),
            committed_catalog_sha256: None,
        };
        self.write_recovery(&record)?;
        let readback = self
            .read_recovery()?
            .ok_or_else(|| "catalog-disabled recovery marker disappeared after write".to_string())?;
        if readback.state != RecoveryState::CatalogDisabled
            || readback.transaction_id != record.transaction_id
            || readback.committed_providers_sha256 != record.committed_providers_sha256
            || readback.providers.sha256 != record.providers.sha256
            || readback.providers.bytes != record.providers.bytes
            || readback.catalog.sha256 != record.catalog.sha256
            || readback.catalog.bytes != record.catalog.bytes
        {
            return Err(
                "catalog-disabled recovery marker readback did not match the authorized state"
                    .to_string(),
            );
        }
        Ok(())
    }

    fn clear_recovery(&mut self) -> Result<(), String> {
        self.validate_transaction_paths()?;
        let path = self.recovery_path();
        if !path.exists() {
            if let Err(error) = self.clear_orphaned_backups() {
                log::warn!(
                    "provider/catalog recovery record is absent, but orphaned backup cleanup failed: {error}"
                );
            }
            return Ok(());
        }
        fs::remove_file(&path).map_err(|error| {
            format!("failed to clear provider/catalog recovery record: {error}")
        })?;
        if let Err(error) = self.clear_orphaned_backups() {
            log::warn!(
                "provider/catalog state is consistent, but orphaned recovery backup cleanup failed: {error}"
            );
        }
        Ok(())
    }

    fn invalidate_catalog(&mut self) -> Result<(), String> {
        self.validate_transaction_paths()?;
        let path = self.paths.generated_catalog_path();
        let record = self.read_recovery()?.ok_or_else(|| {
            "catalog invalidation requires a durable recovery marker".to_string()
        })?;
        if record.state != RecoveryState::CatalogDisabled {
            return Err(
                "catalog invalidation requires a durable catalog-disabled marker".to_string(),
            );
        }
        self.verify_disabled_prefix(&record)?;
        let quarantine = self.catalog_quarantine_path(&record.transaction_id)?;
        safe_file::validate_confined_path(&quarantine, self.paths.runtime_root(), true)?;
        if path.exists() {
            let current = safe_file::read_private_text(
                &path,
                self.paths.runtime_root(),
                MAX_CATALOG_SNAPSHOT_BYTES,
                "catalog requiring fail-closed quarantine",
            )?;
            let quarantine_is_snapshot = quarantine.exists()
                && self.confined_file_matches_snapshot(
                    &quarantine,
                    &record.catalog,
                    MAX_CATALOG_SNAPSHOT_BYTES,
                    "completed catalog quarantine evidence",
                )?;
            if current == DISABLED_CATALOG
                && ((!record.catalog.existed && !quarantine.exists())
                    || (record.catalog.existed && quarantine_is_snapshot))
            {
                return Ok(());
            }
            let quarantined = safe_file::quarantine_private_text(
                &path,
                &quarantine,
                self.paths.runtime_root(),
                DISABLED_CATALOG,
                MAX_CATALOG_SNAPSHOT_BYTES,
                "catalog requiring fail-closed quarantine",
            )?;
            if quarantined != current {
                return Err(
                    "quarantined generated catalog readback changed during confinement"
                        .to_string(),
                );
            }
            transaction_fault("after-catalog-quarantine")?;
        }
        safe_file::write_private_text_atomic(&path, DISABLED_CATALOG, self.paths.runtime_root())
            .map_err(|error| {
            format!("failed to publish the empty fail-closed generated catalog: {error}")
        })?;
        let readback = safe_file::read_private_text(
            &path,
            self.paths.runtime_root(),
            MAX_CATALOG_SNAPSHOT_BYTES,
            "disabled generated catalog readback",
        )?;
        if readback != DISABLED_CATALOG {
            return Err(
                "empty fail-closed generated catalog readback did not match the sentinel"
                    .to_string(),
            );
        }
        Ok(())
    }

}

impl RuntimeProviderCatalogStore {
    fn mark_catalog_write_pending(&mut self, candidate_catalog: &str) -> Result<(), String> {
        let mut record = self.read_recovery()?.ok_or_else(|| {
            "provider/catalog recovery record disappeared before catalog write".to_string()
        })?;
        apply_recovery_transition(&mut record, RecoveryTransition::BeginCatalogPublish)?;
        let expected = record
            .candidate_provider_state_sha256
            .as_deref()
            .ok_or_else(|| {
                "provider/catalog transaction is missing the candidate provider hash".to_string()
            })?;
        if hash_provider_state(&self.current_providers()?)? != expected {
            return Err("provider configuration identity changed before catalog write".to_string());
        }
        verify_file_identity(
            &self.paths.runtime_providers_path(),
            record.candidate_providers_sha256.as_deref(),
            record.candidate_providers_bytes,
            MAX_PROVIDER_SNAPSHOT_BYTES,
            "candidate provider configuration before catalog write",
        )?;
        record.candidate_catalog_sha256 = Some(hash_bytes(candidate_catalog.as_bytes()));
        record.candidate_catalog_bytes = Some(candidate_catalog.len() as u64);
        self.write_recovery(&record)
    }
    fn clear_orphaned_backups(&self) -> Result<(), String> {
        self.validate_transaction_paths()?;
        for path in [
            self.paths.provider_catalog_providers_backup_path(),
            self.paths.provider_catalog_catalog_backup_path(),
        ] {
            if path.exists() {
                fs::remove_file(&path).map_err(|error| {
                    format!(
                        "failed to clear provider/catalog recovery backup {}: {error}",
                        path.display()
                    )
                })?;
            }
        }
        Ok(())
    }
}

fn snapshot_file(path: &Path, max_bytes: u64, label: &str) -> Result<FileSnapshot, String> {
    if !path.exists() {
        return Ok(FileSnapshot {
            existed: false,
            sha256: None,
            bytes: None,
        });
    }
    let contents = read_bounded(path, max_bytes, label)?;
    Ok(FileSnapshot {
        existed: true,
        sha256: Some(hash_bytes(contents.as_bytes())),
        bytes: Some(contents.len() as u64),
    })
}

fn capture_snapshot(
    path: &Path,
    backup_path: &Path,
    snapshot: &FileSnapshot,
    max_bytes: u64,
    label: &str,
) -> Result<(), String> {
    if !snapshot.existed {
        return Ok(());
    }
    let contents = read_bounded(path, max_bytes, label)?;
    if snapshot.sha256.as_deref() != Some(hash_bytes(contents.as_bytes()).as_str())
        || snapshot.bytes != Some(contents.len() as u64)
    {
        return Err(format!(
            "{label} changed while its recovery backup was captured"
        ));
    }
    let boundary = common_ancestor(path, backup_path)
        .ok_or_else(|| format!("failed to resolve trusted boundary for {label} backup"))?;
    safe_file::write_private_text_atomic(backup_path, &contents, boundary).map_err(|error| {
        format!(
            "failed to persist {label} recovery backup {}: {error}",
            backup_path.display()
        )
    })?;
    Ok(())
}

fn common_ancestor<'a>(left: &'a Path, right: &'a Path) -> Option<&'a Path> {
    let left_parent = left.parent()?;
    let right_parent = right.parent()?;
    left_parent
        .ancestors()
        .find(|candidate| right_parent.starts_with(candidate))
}

#[cfg(all(
    target_os = "linux",
    any(target_arch = "x86_64", target_arch = "aarch64")
))]
fn plan_restore_file_linux(
    path: &Path,
    backup_path: &Path,
    evidence_path: &Path,
    snapshot: &FileSnapshot,
    validated_backup: Option<&str>,
    rollback_owner: Option<(&str, u64, u64)>,
    label: &str,
) -> Result<safe_file::LinuxPrivateRollbackPlan, String> {
    let max_bytes = if label.contains("provider") {
        MAX_PROVIDER_SNAPSHOT_BYTES
    } else {
        MAX_CATALOG_SNAPSHOT_BYTES
    };
    let replacement = if snapshot.existed {
        Some(validated_backup.ok_or_else(|| {
            format!("validated {label} recovery backup was not loaded before restore")
        })?)
    } else {
        if validated_backup.is_some() {
            return Err(format!(
                "validated {label} recovery backup exists for an absent snapshot"
            ));
        }
        None
    };
    let boundary = common_ancestor(path, backup_path)
        .ok_or_else(|| format!("failed to resolve trusted boundary for restored {label}"))?;
    safe_file::plan_private_text_rollback(
        safe_file::LinuxPrivateRollbackRequest {
            path,
            evidence: evidence_path,
            boundary,
            replacement,
            max_bytes,
            label,
        },
        |current| {
            rollback_owner.is_some_and(|(expected_hash, expected_bytes, _)| {
                current.len() as u64 == expected_bytes
                    && hash_bytes(current.as_bytes()) == expected_hash
            })
        },
    )
}

fn restore_file(
    path: &Path,
    backup_path: &Path,
    evidence_path: &Path,
    snapshot: &FileSnapshot,
    validated_backup: Option<&str>,
    rollback_owner: Option<(&str, u64, u64)>,
    label: &str,
) -> Result<(), String> {
    let max_bytes = if label.contains("provider") {
        MAX_PROVIDER_SNAPSHOT_BYTES
    } else {
        MAX_CATALOG_SNAPSHOT_BYTES
    };
    if snapshot.existed {
        let contents = validated_backup.ok_or_else(|| {
            format!("validated {label} recovery backup was not loaded before restore")
        })?;
        let boundary = common_ancestor(path, backup_path)
            .ok_or_else(|| format!("failed to resolve trusted boundary for restored {label}"))?;
        let expected_owner = rollback_owner;
        safe_file::replace_private_text_if_unchanged(
            safe_file::PrivateTextReplacement {
                path,
                evidence: evidence_path,
                boundary,
                contents,
                max_bytes,
                label,
            },
            |current| {
                expected_owner.is_some_and(|(expected_hash, expected_bytes, _)| {
                    current.len() as u64 == expected_bytes
                        && hash_bytes(current.as_bytes()) == expected_hash
                })
            },
            || {
                #[cfg(test)]
                invoke_test_pre_restore_target_commit_hook(path);
            },
        )
        .map_err(|error| format!("failed to conditionally restore {label}: {error}"))?;
        return Ok(());
    }
    if validated_backup.is_some() {
        return Err(format!(
            "validated {label} recovery backup exists for an absent snapshot"
        ));
    }
    let boundary = common_ancestor(path, backup_path)
        .ok_or_else(|| format!("failed to resolve trusted boundary for removed {label}"))?;
    let expected_owner = rollback_owner;
    safe_file::remove_private_text_if_unchanged(
        path,
        evidence_path,
        boundary,
        max_bytes,
        label,
        |current| {
            expected_owner.is_some_and(|(expected_hash, expected_bytes, _)| {
                current.len() as u64 == expected_bytes
                    && hash_bytes(current.as_bytes()) == expected_hash
            })
        },
        || {
            #[cfg(test)]
            invoke_test_pre_restore_target_commit_hook(path);
        },
    )
    .map_err(|error| format!("failed to conditionally remove {label}: {error}"))
}

fn validate_recovery_record(record: &RecoveryRecord) -> Result<(), String> {
    let now = unix_timestamp_seconds()?;
    if record.created_at_unix_seconds == 0
        || record.created_at_unix_seconds > now.saturating_add(MAX_FUTURE_CLOCK_SKEW_SECONDS)
    {
        return Err("provider/catalog recovery record has an invalid creation time".to_string());
    }
    if record.transaction_id.len() < 16
        || record.transaction_id.len() > 128
        || !record
            .transaction_id
            .bytes()
            .all(|byte| byte.is_ascii_alphanumeric() || byte == b'-')
    {
        return Err("provider/catalog recovery record has an invalid transaction id".to_string());
    }
    validate_snapshot_shape(&record.providers, "provider configuration")?;
    validate_snapshot_shape(&record.catalog, "generated catalog")?;
    match record.state.classify().shape {
        RecoveryRecordShape::Prepared => {
            validate_sha256(
                record.base_provider_state_sha256.as_deref(),
                "base provider state",
            )?;
            if record.candidate_provider_state_sha256.is_some()
                || record.candidate_providers_sha256.is_some()
                || record.candidate_providers_bytes.is_some()
                || record.candidate_catalog_sha256.is_some()
                || record.candidate_catalog_bytes.is_some()
                || record.committed_providers_sha256.is_some()
                || record.committed_catalog_sha256.is_some()
            {
                return Err(
                    "prepared provider/catalog recovery record contains candidate or committed hashes"
                        .to_string(),
                );
            }
        }
        RecoveryRecordShape::ProviderCandidate => {
            validate_sha256(
                record.base_provider_state_sha256.as_deref(),
                "base provider state",
            )?;
            validate_sha256(
                record.candidate_provider_state_sha256.as_deref(),
                "candidate provider state",
            )?;
            validate_hash_and_length(
                record.candidate_providers_sha256.as_deref(),
                record.candidate_providers_bytes,
                MAX_PROVIDER_SNAPSHOT_BYTES,
                "candidate provider configuration",
            )?;
            if record.candidate_catalog_sha256.is_some()
                || record.candidate_catalog_bytes.is_some()
                || record.committed_providers_sha256.is_some()
                || record.committed_catalog_sha256.is_some()
            {
                return Err("provider-write recovery record contains committed hashes".to_string());
            }
        }
        RecoveryRecordShape::CatalogCandidate => {
            validate_sha256(
                record.base_provider_state_sha256.as_deref(),
                "base provider state",
            )?;
            validate_sha256(
                record.candidate_provider_state_sha256.as_deref(),
                "candidate provider state",
            )?;
            validate_hash_and_length(
                record.candidate_providers_sha256.as_deref(),
                record.candidate_providers_bytes,
                MAX_PROVIDER_SNAPSHOT_BYTES,
                "candidate provider configuration",
            )?;
            validate_hash_and_length(
                record.candidate_catalog_sha256.as_deref(),
                record.candidate_catalog_bytes,
                MAX_CATALOG_SNAPSHOT_BYTES,
                "candidate generated catalog",
            )?;
            if record.committed_providers_sha256.is_some()
                || record.committed_catalog_sha256.is_some()
            {
                return Err(
                    "catalog-write recovery record contains committed hashes".to_string(),
                );
            }
        }
        RecoveryRecordShape::Committed => {
            validate_sha256(
                record.base_provider_state_sha256.as_deref(),
                "base provider state",
            )?;
            validate_sha256(
                record.candidate_provider_state_sha256.as_deref(),
                "candidate provider state",
            )?;
            validate_hash_and_length(
                record.candidate_providers_sha256.as_deref(),
                record.candidate_providers_bytes,
                MAX_PROVIDER_SNAPSHOT_BYTES,
                "candidate provider configuration",
            )?;
            validate_hash_and_length(
                record.candidate_catalog_sha256.as_deref(),
                record.candidate_catalog_bytes,
                MAX_CATALOG_SNAPSHOT_BYTES,
                "candidate generated catalog",
            )?;
            validate_sha256(
                record.committed_providers_sha256.as_deref(),
                "committed provider configuration",
            )?;
            validate_sha256(
                record.committed_catalog_sha256.as_deref(),
                "committed generated catalog",
            )?;
            if record.committed_providers_sha256 != record.candidate_providers_sha256
                || record.committed_catalog_sha256 != record.candidate_catalog_sha256
            {
                return Err(
                    "committed provider/catalog hashes do not match the journaled candidates"
                        .to_string(),
                );
            }
        }
        RecoveryRecordShape::Disabled => {
            if record.base_provider_state_sha256.is_some()
                || record.candidate_provider_state_sha256.is_some()
                || record.candidate_providers_sha256.is_some()
                || record.candidate_providers_bytes.is_some()
                || record.candidate_catalog_sha256.is_some()
                || record.candidate_catalog_bytes.is_some()
                || record.committed_catalog_sha256.is_some()
            {
                return Err(
                    "catalog-disabled recovery record contains candidate or committed catalog hashes"
                        .to_string(),
                );
            }
            validate_sha256(
                record.committed_providers_sha256.as_deref(),
                "catalog-disabled provider state",
            )?;
        }
    }
    Ok(())
}

fn validate_snapshot_shape(snapshot: &FileSnapshot, label: &str) -> Result<(), String> {
    match (snapshot.existed, snapshot.sha256.as_deref(), snapshot.bytes) {
        (true, Some(hash), Some(bytes)) => {
            let max_bytes = if label.contains("provider") {
                MAX_PROVIDER_SNAPSHOT_BYTES
            } else {
                MAX_CATALOG_SNAPSHOT_BYTES
            };
            validate_hash_and_length(Some(hash), Some(bytes), max_bytes, label)
        }
        (false, None, None) => Ok(()),
        (false, _, _) => Err(format!(
            "provider/catalog recovery record has identity metadata for absent {label}"
        )),
        (true, _, _) => Err(format!(
            "provider/catalog recovery record is missing identity metadata for {label}"
        )),
    }
}

fn validate_hash_and_length(
    hash: Option<&str>,
    bytes: Option<u64>,
    max_bytes: u64,
    label: &str,
) -> Result<(), String> {
    validate_sha256(hash, label)?;
    let bytes = bytes.ok_or_else(|| {
        format!("provider/catalog recovery record is missing the {label} byte length")
    })?;
    if bytes > max_bytes {
        return Err(format!(
            "provider/catalog recovery record has an oversized {label} byte length"
        ));
    }
    Ok(())
}

fn validate_sha256(value: Option<&str>, label: &str) -> Result<(), String> {
    let Some(value) = value else {
        return Err(format!(
            "provider/catalog recovery record is missing the {label} hash"
        ));
    };
    if value.len() != 64 || !value.bytes().all(|byte| byte.is_ascii_hexdigit()) {
        return Err(format!(
            "provider/catalog recovery record has an invalid {label} hash"
        ));
    }
    Ok(())
}

fn load_snapshot_backup(
    backup_path: &Path,
    snapshot: &FileSnapshot,
    max_bytes: u64,
    label: &str,
) -> Result<Option<String>, String> {
    if !snapshot.existed {
        if backup_path.try_exists().map_err(|error| {
            format!(
                "failed to inspect {label} recovery backup {}: {error}",
                backup_path.display()
            )
        })? {
            return Err(format!(
                "unexpected {label} recovery backup exists for an absent snapshot"
            ));
        }
        return Ok(None);
    }
    let contents = read_bounded(
        backup_path,
        max_bytes,
        &format!("{label} recovery backup"),
    )?;
    if snapshot.bytes != Some(contents.len() as u64)
        || snapshot.sha256.as_deref() != Some(hash_bytes(contents.as_bytes()).as_str())
    {
        return Err(format!(
            "{label} recovery backup identity did not match the journal"
        ));
    }
    Ok(Some(contents))
}

fn verify_snapshot_target(
    path: &Path,
    snapshot: &FileSnapshot,
    max_bytes: u64,
    label: &str,
) -> Result<(), String> {
    if !snapshot.existed {
        return if path.exists() {
            Err(format!("{label} should be absent"))
        } else {
            Ok(())
        };
    }
    verify_file_identity(path, snapshot.sha256.as_deref(), snapshot.bytes, max_bytes, label)
}

fn file_matches_snapshot(
    path: &Path,
    snapshot: &FileSnapshot,
    max_bytes: u64,
    label: &str,
) -> Result<bool, String> {
    if !snapshot.existed {
        return Ok(!path.exists());
    }
    file_matches_identity(path, snapshot.sha256.as_deref(), snapshot.bytes, max_bytes, label)
}

fn file_matches_identity(
    path: &Path,
    expected_hash: Option<&str>,
    expected_bytes: Option<u64>,
    max_bytes: u64,
    label: &str,
) -> Result<bool, String> {
    let expected_hash =
        expected_hash.ok_or_else(|| format!("missing expected hash for {label}"))?;
    let expected_bytes =
        expected_bytes.ok_or_else(|| format!("missing expected byte length for {label}"))?;
    if !path.exists() {
        return Ok(false);
    }
    let contents = read_bounded(path, max_bytes, label)?;
    Ok(contents.len() as u64 == expected_bytes
        && hash_bytes(contents.as_bytes()) == expected_hash)
}

fn verify_file_identity(
    path: &Path,
    expected_hash: Option<&str>,
    expected_bytes: Option<u64>,
    max_bytes: u64,
    label: &str,
) -> Result<(), String> {
    if file_matches_identity(
        path,
        expected_hash,
        expected_bytes,
        max_bytes,
        label,
    )? {
        return Ok(());
    }
    Err(format!("{label} identity did not match the recovery record"))
}

fn read_bounded(path: &Path, max_bytes: u64, label: &str) -> Result<String, String> {
    safe_file::read_single_link_text(path, max_bytes, label)
}

fn hash_bytes(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

fn hash_provider_state(providers: &[Provider]) -> Result<String, String> {
    serde_json::to_vec(providers)
        .map(|bytes| hash_bytes(&bytes))
        .map_err(|error| format!("failed to hash provider state for catalog recovery: {error}"))
}

fn normalized_provider_state(providers: &[Provider]) -> Vec<Provider> {
    let mut providers = providers.to_vec();
    for provider in &mut providers {
        for model in &mut provider.models {
            models::apply_resolved_model_limits(&provider.id, model);
        }
    }
    providers
}

fn unix_timestamp_seconds() -> Result<u64, String> {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs())
        .map_err(|error| format!("system clock is before the Unix epoch: {error}"))
}

fn transaction_id() -> Result<String, String> {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|error| format!("system clock is before Unix epoch: {error}"))?;
    Ok(format!(
        "{:08x}-{:032x}",
        std::process::id(),
        now.as_nanos()
    ))
}

fn rollback_result(
    store: &mut dyn ProviderCatalogStore,
    previous_providers: Vec<Provider>,
    previous_models: Vec<Model>,
    cause: String,
    protocol_changed: bool,
) -> ProviderCatalogTransactionResult {
    if let Err(rollback_error) = store.restore_pending() {
        return recovery_required_result(
            store,
            previous_providers,
            format!(
                "{cause}; automatic rollback failed: {rollback_error}; recovery snapshot retained"
            ),
            protocol_changed,
        );
    }
    let restored_providers = match store.current_providers() {
        Ok(providers) => providers,
        Err(error) => {
            return recovery_required_result(
                store,
                previous_providers,
                format!("{cause}; restored provider readback failed: {error}"),
                protocol_changed,
            )
        }
    };
    let restored_models = match store.current_catalog() {
        Ok(models) => models,
        Err(error) => {
            return recovery_required_result(
                store,
                previous_providers,
                format!("{cause}; restored catalog readback failed: {error}"),
                protocol_changed,
            )
        }
    };
    if serde_json::to_value(&restored_providers).ok()
        != serde_json::to_value(&previous_providers).ok()
    {
        return recovery_required_result(
            store,
            previous_providers,
            format!("{cause}; restored provider configuration did not match its snapshot"),
            protocol_changed,
        );
    }
    if let Err(error) = verify_catalog_for_providers(&restored_models, &previous_providers) {
        return recovery_required_result(
            store,
            previous_providers,
            format!("{cause}; restored catalog verification failed: {error}"),
            protocol_changed,
        );
    }
    if serde_json::to_value(&restored_models).ok() != serde_json::to_value(&previous_models).ok() {
        return recovery_required_result(
            store,
            previous_providers,
            format!("{cause}; restored catalog did not match its snapshot"),
            protocol_changed,
        );
    }
    let cleanup_error = store.clear_recovery().err();
    ProviderCatalogTransactionResult {
        outcome: ProviderCatalogTransactionOutcome::RolledBack,
        providers: restored_providers,
        models: restored_models,
        revision: ProviderCatalogRevision::unavailable(),
        protocol_changed,
        detail: Some(match cleanup_error {
            Some(error) => format!(
                "{cause}; previous provider configuration and catalog were restored; recovery cleanup remains pending: {error}"
            ),
            None => format!(
                "{cause}; previous provider configuration and catalog were restored"
            ),
        }),
        catalog_disabled: false,
    }
}

fn recovery_required_result(
    store: &mut dyn ProviderCatalogStore,
    fallback_providers: Vec<Provider>,
    detail: String,
    protocol_changed: bool,
) -> ProviderCatalogTransactionResult {
    let marker_error = store.ensure_recovery_required().err();
    // The marker's readback authorizes the exact current catalog plus the
    // empty sentinel before quarantine changes the live catalog namespace.
    let invalidation_error = if marker_error.is_none() {
        store.invalidate_catalog().err()
    } else {
        None
    };
    let providers = store.current_providers().unwrap_or(fallback_providers);
    let (models, catalog_disabled) = match (marker_error.as_ref(), invalidation_error.as_ref()) {
        (None, None) => (Vec::new(), true),
        _ => (store.current_catalog().unwrap_or_default(), false),
    };
    ProviderCatalogTransactionResult {
        outcome: ProviderCatalogTransactionOutcome::RecoveryRequired,
        providers,
        models,
        revision: ProviderCatalogRevision::unavailable(),
        protocol_changed,
        detail: Some(match (marker_error, invalidation_error) {
            (Some(marker), _) => format!(
                "{detail}; durable catalog-disabled marker failed before invalidation: {marker}"
            ),
            (None, Some(error)) => {
                format!(
                    "{detail}; fail-closed catalog invalidation also failed: {error}"
                )
            }
            (None, None) => format!("{detail}; generated catalog disabled fail-closed"),
        }),
        catalog_disabled,
    }
}

fn verify_catalog_for_providers(models: &[Model], providers: &[Provider]) -> Result<(), String> {
    for protocol_switch in expected_catalog_bindings(providers) {
        for model_id in &protocol_switch.model_ids {
            let matching_models = models
                .iter()
                .filter(|model| {
                    model.capability_binding.as_ref().is_some_and(|binding| {
                        binding.provider == protocol_switch.provider_id
                            && binding.model == *model_id
                    })
                })
                .collect::<Vec<_>>();
            if matching_models.len() != 1 {
                return Err(format!(
                    "catalog readback expected exactly one binding for {}/{} but found {}",
                    protocol_switch.provider_id,
                    model_id,
                    matching_models.len()
                ));
            }
            let binding = matching_models[0]
                .capability_binding
                .as_ref()
                .expect("matching catalog model has a binding");
            let provider_model = providers
                .iter()
                .find(|provider| provider.id == protocol_switch.provider_id)
                .and_then(|provider| provider.models.iter().find(|model| model.id == *model_id))
                .ok_or_else(|| {
                    format!(
                        "catalog readback binding {}/{} has no provider model",
                        protocol_switch.provider_id, model_id
                    )
                })?;
            let expected = expected_capability_binding(
                &protocol_switch.provider_id,
                model_id,
                &protocol_switch.upstream_protocol,
                &provider_model.capability_profiles,
            )?;
            if binding != &expected {
                return Err(format!(
                    "catalog readback capability contract did not match the route-qualified provider profile for {}/{}",
                    protocol_switch.provider_id, model_id
                ));
            }
        }
    }
    Ok(())
}

fn expected_capability_binding(
    provider_id: &str,
    model_id: &str,
    upstream_protocol: &UpstreamFormat,
    profiles: &[CapabilityProfile],
) -> Result<CapabilityBinding, String> {
    let mut binding = CapabilityBinding {
        schema_version: 1,
        provider: provider_id.to_string(),
        model: model_id.to_string(),
        upstream_protocol: upstream_protocol.clone(),
        tool_profile: None,
        collaboration_backend: None,
        collaboration_version: None,
        capability_manifest_version: None,
        capability_manifest_hash: None,
        qualification_state: QualificationState::Unqualified,
        advanced_capabilities_enabled: false,
        rejection_reason: None,
    };
    if !matches!(
        upstream_protocol,
        UpstreamFormat::Responses | UpstreamFormat::ChatCompletions
    ) {
        binding.rejection_reason = Some("unsupported_upstream_protocol_scope".to_string());
        return Ok(binding);
    }
    let matching = profiles
        .iter()
        .filter(|profile| &profile.upstream_protocol == upstream_protocol)
        .collect::<Vec<_>>();
    let profile = match matching.as_slice() {
        [] => {
            binding.rejection_reason = Some("missing_route_profile".to_string());
            return Ok(binding);
        }
        [profile] => *profile,
        _ => {
            binding.rejection_reason = Some("conflicting_route_profiles".to_string());
            return Ok(binding);
        }
    };
    let route_fields_valid = profile.schema_version == 1
        && !profile.tool_profile.trim().is_empty()
        && !profile.collaboration_backend.trim().is_empty()
        && !profile.collaboration_version.trim().is_empty()
        && !profile.capability_manifest_version.trim().is_empty()
        && !profile.capability_manifest_hash.trim().is_empty()
        && (profile.collaboration_backend == "none") == (profile.collaboration_version == "none");
    if !route_fields_valid {
        return Err(format!(
            "catalog readback provider profile for {provider_id}/{model_id} has a future or malformed schema or is missing route fields"
        ));
    }
    if capability_profile_manifest_hash(provider_id, model_id, profile)?
        != profile.capability_manifest_hash
    {
        return Err(format!(
            "catalog readback provider profile for {provider_id}/{model_id} has a stale capability manifest hash"
        ));
    }
    binding.tool_profile = Some(profile.tool_profile.clone());
    binding.collaboration_backend = Some(profile.collaboration_backend.clone());
    binding.collaboration_version = Some(profile.collaboration_version.clone());
    binding.capability_manifest_version = Some(profile.capability_manifest_version.clone());
    binding.capability_manifest_hash = Some(profile.capability_manifest_hash.clone());
    binding.qualification_state = profile.qualification_state.clone();
    binding.advanced_capabilities_enabled =
        profile.qualification_state == QualificationState::Supported;
    if !binding.advanced_capabilities_enabled {
        binding.rejection_reason = Some(format!(
            "qualification_state_{}",
            qualification_state_name(&profile.qualification_state)
        ));
    }
    Ok(binding)
}

fn capability_profile_manifest_hash(
    provider_id: &str,
    model_id: &str,
    profile: &CapabilityProfile,
) -> Result<String, String> {
    let value = serde_json::json!({
        "capability_manifest_version": profile.capability_manifest_version,
        "collaboration_backend": profile.collaboration_backend,
        "collaboration_version": profile.collaboration_version,
        "model_id": model_id.trim(),
        "provider_id": provider_id.trim(),
        "qualification_state": qualification_state_name(&profile.qualification_state),
        "schema_version": profile.schema_version,
        "tool_profile": profile.tool_profile,
        "upstream_protocol": upstream_protocol_name(&profile.upstream_protocol),
    });
    serde_json::to_vec(&value)
        .map(|bytes| format!("sha256:{}", hash_bytes(&bytes)))
        .map_err(|error| format!("failed to hash capability profile manifest: {error}"))
}

fn qualification_state_name(value: &QualificationState) -> &'static str {
    match value {
        QualificationState::Supported => "supported",
        QualificationState::Unsupported => "unsupported",
        QualificationState::Unqualified => "unqualified",
        QualificationState::TemporarilyUnavailable => "temporarily_unavailable",
        QualificationState::Degraded => "degraded",
    }
}

fn upstream_protocol_name(value: &UpstreamFormat) -> &'static str {
    match value {
        UpstreamFormat::Auto => "auto",
        UpstreamFormat::Responses => "responses",
        UpstreamFormat::ChatCompletions => "chat_completions",
        UpstreamFormat::AnthropicMessages => "anthropic_messages",
    }
}

fn expected_catalog_bindings(providers: &[Provider]) -> Vec<ProviderProtocolSwitch> {
    providers
        .iter()
        .filter(|provider| provider.enabled)
        .filter_map(|provider| {
            let upstream_protocol = provider.upstream_format.clone()?;
            if upstream_protocol == UpstreamFormat::Auto {
                return None;
            }
            Some(ProviderProtocolSwitch {
                provider_id: provider.id.clone(),
                upstream_protocol,
                model_ids: provider
                    .models
                    .iter()
                    .filter(|model| model.enabled && model.gateway_exported)
                    .map(|model| model.id.clone())
                    .collect(),
            })
        })
        .collect()
}

fn unchanged_result(
    providers: Vec<Provider>,
    models: Vec<Model>,
    detail: String,
    protocol_changed: bool,
) -> ProviderCatalogTransactionResult {
    ProviderCatalogTransactionResult {
        outcome: ProviderCatalogTransactionOutcome::Unchanged,
        providers,
        models,
        revision: ProviderCatalogRevision::unavailable(),
        protocol_changed,
        detail: Some(detail),
        catalog_disabled: false,
    }
}

#[cfg(test)]
mod tests {
    use super::{
        expected_capability_binding, issue_provider_catalog_revision,
        persist_provider_catalog_state_with_paths,
        persist_with_store, recover_before_gateway_with_store,
        validate_provider_catalog_revision, verify_catalog_for_providers, ProviderCatalogStore,
        ProviderCatalogRevision, ProviderCatalogTransactionOutcome, RecoveryAction,
        RecoveryRecordShape, RecoveryState, RecoveryTransition, RuntimeProviderCatalogStore,
        DISABLED_CATALOG, RECOVERY_SCHEMA_VERSION,
    };
    use crate::{
        config, CapabilityBinding, CapabilityProfile, Model, Provider, QualificationState,
        UpstreamFormat,
    };
    use std::collections::{HashSet, VecDeque};
    use std::fs;
    use std::io::{Read, Write};
    use std::net::TcpListener;
    use std::path::{Path, PathBuf};
    use std::sync::mpsc;
    use std::thread;
    use std::time::Duration;
    use std::time::{SystemTime, UNIX_EPOCH};

    #[test]
    fn opaque_revision_detects_secret_only_changes_without_disclosing_provider_state() {
        let root = temp_root("opaque-revision-secret-change");
        let paths = isolated_paths(&root);
        let mut original = provider(UpstreamFormat::Responses);
        original.api_key = Some("secret-alpha".to_string());
        config::save_providers_with_paths(vec![original.clone()], &paths).unwrap();

        let revision = issue_provider_catalog_revision(&paths).expect("issue opaque revision");
        assert!(!revision.as_str().contains("secret-alpha"));
        assert!(!revision.as_str().contains(&original.id));
        validate_provider_catalog_revision(&paths, &revision)
            .expect("unchanged raw provider state must validate");

        original.api_key = Some("secret-bravo".to_string());
        config::save_providers_with_paths(vec![original], &paths).unwrap();
        let error = validate_provider_catalog_revision(&paths, &revision)
            .expect_err("secret-only changes must stale the opaque revision");

        assert!(error.contains("changed since this editor snapshot"));
        assert!(!error.contains("secret-alpha"));
        assert!(!error.contains("secret-bravo"));
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn opaque_revisions_are_multiwindow_safe_process_local_and_one_shot() {
        let root = temp_root("opaque-revision-lifecycle");
        let paths = isolated_paths(&root);
        let loaded = provider(UpstreamFormat::Responses);
        config::save_providers_with_paths(vec![loaded.clone()], &paths).unwrap();
        let first_window = issue_provider_catalog_revision(&paths).unwrap();
        let second_window = issue_provider_catalog_revision(&paths).unwrap();
        assert_ne!(first_window, second_window);

        validate_provider_catalog_revision(&paths, &first_window)
            .expect("first window may claim its unchanged snapshot");
        validate_provider_catalog_revision(&paths, &second_window)
            .expect("second window has an independent token for the same unchanged snapshot");
        let replay = validate_provider_catalog_revision(&paths, &first_window)
            .expect_err("opaque revisions are one-shot capabilities");
        assert!(replay.contains("changed since this editor snapshot"));

        let mutation_owner = issue_provider_catalog_revision(&paths).unwrap();
        let stale_peer = issue_provider_catalog_revision(&paths).unwrap();
        validate_provider_catalog_revision(&paths, &mutation_owner)
            .expect("mutation owner claims the current snapshot");
        let mut changed = loaded.clone();
        changed.name = "Committed by another window".to_string();
        config::save_providers_with_paths(vec![changed], &paths).unwrap();
        let stale = validate_provider_catalog_revision(&paths, &stale_peer)
            .expect_err("a successful raw mutation must stale peer window tokens");
        assert!(stale.contains("changed since this editor snapshot"));

        let provider_before = fs::read(paths.runtime_providers_path()).unwrap();
        let catalog_path = paths.generated_catalog_path();
        write_fixture(&catalog_path, "{\"models\":[{\"slug\":\"restart-evidence\"}]}\n");
        let catalog_before = fs::read(&catalog_path).unwrap();
        let restarted_process_token =
            ProviderCatalogRevision(format!("pcr1_{}", "f".repeat(64)));
        let result = persist_provider_catalog_state_with_paths(
            &paths,
            vec![loaded],
            restarted_process_token,
        )
        .unwrap();

        assert_eq!(result.outcome, ProviderCatalogTransactionOutcome::Conflict);
        assert_eq!(fs::read(paths.runtime_providers_path()).unwrap(), provider_before);
        assert_eq!(fs::read(catalog_path).unwrap(), catalog_before);
        assert!(!paths.provider_catalog_recovery_path().exists());
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn protocol_switch_commits_only_after_matching_catalog_readback() {
        let previous_provider = provider(UpstreamFormat::Responses);
        let candidate_provider = provider(UpstreamFormat::ChatCompletions);
        let previous_catalog = vec![catalog_model(UpstreamFormat::Responses)];
        let candidate_catalog = vec![catalog_model(UpstreamFormat::ChatCompletions)];
        let mut store = MemoryStore::new(
            vec![previous_provider],
            previous_catalog,
            [Ok(candidate_catalog.clone())],
        );

        let result = persist_with_store(&mut store, vec![candidate_provider.clone()]);

        assert_eq!(result.outcome, ProviderCatalogTransactionOutcome::Committed);
        assert_eq!(
            store.providers[0].upstream_format,
            candidate_provider.upstream_format
        );
        assert_eq!(
            store.catalog[0]
                .capability_binding
                .as_ref()
                .map(|binding| &binding.upstream_protocol),
            Some(&UpstreamFormat::ChatCompletions)
        );
        assert_eq!(result.models.len(), 1);
        assert!(result.protocol_changed);
        assert!(!result.catalog_disabled);
    }

    #[test]
    fn same_protocol_catalog_update_commits_without_protocol_restart_flag() {
        let previous_provider = provider(UpstreamFormat::Responses);
        let mut candidate_provider = previous_provider.clone();
        candidate_provider.name = "Renamed Ollama Cloud".to_string();
        let catalog = vec![catalog_model(UpstreamFormat::Responses)];
        let mut store = MemoryStore::new(vec![previous_provider], catalog.clone(), [Ok(catalog)]);

        let result = persist_with_store(&mut store, vec![candidate_provider]);

        assert_eq!(result.outcome, ProviderCatalogTransactionOutcome::Committed);
        assert!(!result.protocol_changed);
        assert!(!result.catalog_disabled);
    }

    #[test]
    fn stale_editor_snapshot_returns_typed_conflict_without_modifying_either_file() {
        let root = temp_root("stale-revision-zero-mutation");
        let paths = isolated_paths(&root);
        let loaded = provider(UpstreamFormat::Responses);
        config::save_providers_with_paths(vec![loaded.clone()], &paths).unwrap();
        let catalog = "{\"models\":[{\"slug\":\"external-catalog-evidence\"}]}\n";
        write_fixture(&paths.generated_catalog_path(), catalog);
        let revision = issue_provider_catalog_revision(&paths).unwrap();

        let mut externally_updated = loaded.clone();
        externally_updated.name = "External update".to_string();
        config::save_providers_with_paths(vec![externally_updated.clone()], &paths).unwrap();
        let provider_bytes = fs::read(paths.runtime_providers_path()).unwrap();
        let catalog_bytes = fs::read(paths.generated_catalog_path()).unwrap();
        let mut requested = loaded.clone();
        requested.name = "Stale editor update".to_string();

        let result =
            persist_provider_catalog_state_with_paths(&paths, vec![requested], revision).unwrap();

        assert_eq!(result.outcome, ProviderCatalogTransactionOutcome::Conflict);
        assert_eq!(result.providers[0].name, externally_updated.name);
        assert_eq!(fs::read(paths.runtime_providers_path()).unwrap(), provider_bytes);
        assert_eq!(fs::read(paths.generated_catalog_path()).unwrap(), catalog_bytes);
        assert!(!paths.provider_catalog_recovery_path().exists());
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn production_runtime_store_constructor_requires_the_shared_transaction_guard() {
        let root = temp_root("runtime-store-construction-guard");
        let paths = isolated_paths(&root);
        let error = RuntimeProviderCatalogStore::new_guarded(paths.clone())
            .err()
            .expect("unguarded production construction must be rejected");
        assert!(error.contains("cross-process transaction guard"));

        super::with_transaction_guard_for_paths(&paths, || {
            RuntimeProviderCatalogStore::new_guarded(paths.clone())
                .map(|_| ())
                .map_err(|error| format!("guarded construction failed: {error}"))
        })
        .unwrap();
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn direct_save_sync_and_official_publication_contend_on_one_real_filesystem_guard() {
        let root = temp_root("shared-writer-guard");
        let paths = isolated_paths(&root);
        fs::create_dir_all(paths.proxy_dir()).unwrap();
        let guard_path = paths.proxy_dir().join("provider-catalog-transaction-guard");
        let holder = crate::safe_file::FileLock::acquire(&guard_path).unwrap();
        let (entered_tx, entered_rx) = mpsc::channel();
        let mut workers = Vec::new();
        for writer in ["direct-save", "direct-sync", "official-refresh"] {
            let paths = paths.clone();
            let entered_tx = entered_tx.clone();
            workers.push(thread::spawn(move || {
                super::with_transaction_guard_for_paths(&paths, || {
                    entered_tx.send(writer).unwrap();
                    Ok(())
                })
                .unwrap();
            }));
        }
        assert!(
            entered_rx.recv_timeout(Duration::from_millis(250)).is_err(),
            "a provider/catalog writer entered while the shared guard was held"
        );
        drop(holder);
        let mut entered = (0..3)
            .map(|_| entered_rx.recv_timeout(Duration::from_secs(10)).unwrap())
            .collect::<Vec<_>>();
        entered.sort_unstable();
        assert_eq!(
            entered,
            vec!["direct-save", "direct-sync", "official-refresh"]
        );
        for worker in workers {
            worker.join().unwrap();
        }
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn provider_reader_waits_for_the_real_transaction_guard_before_opening_config() {
        let root = temp_root("shared-reader-guard");
        let paths = isolated_paths(&root);
        config::save_providers_with_paths(vec![provider(UpstreamFormat::Responses)], &paths)
            .unwrap();
        fs::create_dir_all(paths.proxy_dir()).unwrap();
        let guard_path = paths.proxy_dir().join("provider-catalog-transaction-guard");
        let holder = crate::safe_file::FileLock::acquire(&guard_path).unwrap();
        let (completed_tx, completed_rx) = mpsc::channel();
        let reader_paths = paths.clone();
        let reader = thread::spawn(move || {
            let result = config::get_providers_with_paths(&reader_paths);
            completed_tx.send(result).unwrap();
        });

        assert!(
            completed_rx
                .recv_timeout(Duration::from_millis(250))
                .is_err(),
            "provider reader opened configuration while the transaction guard was held"
        );
        drop(holder);
        let providers = completed_rx
            .recv_timeout(Duration::from_secs(10))
            .expect("reader should resume after guard release")
            .expect("provider read");
        assert_eq!(providers.len(), 1);
        reader.join().unwrap();
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn startup_recovery_keeps_the_guard_through_gateway_snapshot_and_start_handshake() {
        let root = temp_root("startup-wide-guard");
        let paths = isolated_paths(&root);
        config::save_providers_with_paths(vec![provider(UpstreamFormat::Responses)], &paths)
            .unwrap();
        write_fixture(
            &paths.generated_catalog_path(),
            r#"{"models":[{"slug":"startup-safe"}]}"#,
        );
        let guard_path = paths.proxy_dir().join("provider-catalog-transaction-guard");
        let contender_path = guard_path.clone();

        let (contender, entered_rx) =
            super::recover_before_gateway_start_with_paths(&paths, || {
            let (entered_tx, entered_rx) = mpsc::channel();
            let contender = thread::spawn(move || {
                let _guard = crate::safe_file::FileLock::acquire(&contender_path).unwrap();
                entered_tx.send(()).unwrap();
            });
            assert!(
                entered_rx
                    .recv_timeout(Duration::from_millis(250))
                    .is_err(),
                "startup guard was released before the Gateway handshake finished"
            );
            Ok((contender, entered_rx))
        })
        .unwrap();
        entered_rx
            .recv_timeout(Duration::from_secs(10))
            .expect("contender should enter after startup guard release");
        contender.join().unwrap();

        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn rust_transaction_guard_blocks_the_public_python_provider_writer() {
        use std::io::BufRead;
        use std::process::Stdio;

        let root = temp_root("rust-guard-python-provider-writer");
        let paths = isolated_paths(&root);
        fs::create_dir_all(paths.runtime_providers_path().parent().unwrap()).unwrap();
        let source = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../src-python");
        let python = std::env::var_os("CODEXHUB_PYTHON")
            .map(PathBuf::from)
            .or_else(|| {
                std::env::var_os("USERPROFILE")
                    .map(PathBuf::from)
                    .map(|home| home.join(".local").join("bin").join("python3.13.exe"))
                    .filter(|candidate| candidate.exists())
            })
            .unwrap_or_else(config::find_python);
        let script = "import pathlib, sys; from providers_config import save_providers; print('ready', flush=True);\ntry: save_providers([], pathlib.Path(sys.argv[1]))\nexcept PermissionError: print('refused', flush=True)\nelse: raise SystemExit('runtime provider write unexpectedly bypassed the Rust transaction')";
        let mut child = None;
        let mut events = None;

        super::with_transaction_guard_for_paths(&paths, || {
            let mut writer = std::process::Command::new(&python)
                .env("PYTHONPATH", &source)
                .env("CODEX_HOME", paths.runtime_root())
                .arg("-c")
                .arg(script)
                .arg(paths.runtime_providers_path())
                .stdout(Stdio::piped())
                .stderr(Stdio::inherit())
                .spawn()
                .expect("python is required for cross-process writer tests");
            let stdout = writer.stdout.take().unwrap();
            let (events_tx, events_rx) = mpsc::channel();
            thread::spawn(move || {
                for line in std::io::BufReader::new(stdout).lines() {
                    let Ok(line) = line else { break };
                    if events_tx.send(line).is_err() {
                        break;
                    }
                }
            });
            assert_eq!(
                events_rx
                    .recv_timeout(Duration::from_secs(10))
                    .expect("Python writer should reach its public save seam"),
                "ready"
            );
            assert!(
                events_rx
                    .recv_timeout(Duration::from_millis(250))
                    .is_err(),
                "Python provider writer bypassed the provider/catalog transaction guard"
            );
            child = Some(writer);
            events = Some(events_rx);
            Ok(())
        })
        .unwrap();

        assert_eq!(
            events
                .unwrap()
                .recv_timeout(Duration::from_secs(10))
                .unwrap(),
            "refused"
        );
        assert!(child.unwrap().wait().unwrap().success());
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn rust_transaction_guard_blocks_the_public_python_catalog_writer() {
        use std::io::BufRead;
        use std::process::Stdio;

        let root = temp_root("rust-guard-python-catalog-writer");
        let paths = isolated_paths(&root);
        fs::create_dir_all(paths.proxy_dir()).unwrap();
        let source = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../src-python");
        let python = std::env::var_os("CODEXHUB_PYTHON")
            .map(PathBuf::from)
            .or_else(|| {
                std::env::var_os("USERPROFILE")
                    .map(PathBuf::from)
                    .map(|home| home.join(".local").join("bin").join("python3.13.exe"))
                    .filter(|candidate| candidate.exists())
            })
            .unwrap_or_else(config::find_python);
        let script = "import catalog_sync; catalog_sync._sync_catalog_unlocked = lambda **kwargs: print('entered', flush=True) or {}; print('ready', flush=True); catalog_sync.sync_catalog()";
        let mut child = None;
        let mut events = None;

        super::with_transaction_guard_for_paths(&paths, || {
            let mut writer = std::process::Command::new(&python)
                .env("PYTHONPATH", &source)
                .env("CODEX_HOME", paths.runtime_root())
                .arg("-c")
                .arg(script)
                .stdout(Stdio::piped())
                .stderr(Stdio::inherit())
                .spawn()
                .expect("python is required for cross-process writer tests");
            let stdout = writer.stdout.take().unwrap();
            let (events_tx, events_rx) = mpsc::channel();
            thread::spawn(move || {
                for line in std::io::BufReader::new(stdout).lines() {
                    let Ok(line) = line else { break };
                    if events_tx.send(line).is_err() {
                        break;
                    }
                }
            });
            assert_eq!(
                events_rx
                    .recv_timeout(Duration::from_secs(10))
                    .expect("Python catalog writer should reach its public sync seam"),
                "ready"
            );
            assert!(
                events_rx
                    .recv_timeout(Duration::from_millis(250))
                    .is_err(),
                "Python catalog writer bypassed the provider/catalog transaction guard"
            );
            child = Some(writer);
            events = Some(events_rx);
            Ok(())
        })
        .unwrap();

        assert_eq!(
            events
                .unwrap()
                .recv_timeout(Duration::from_secs(10))
                .unwrap(),
            "entered"
        );
        assert!(child.unwrap().wait().unwrap().success());
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn python_transaction_guard_blocks_the_rust_writer_until_release() {
        use std::io::{BufRead, Write};
        use std::process::Stdio;

        let root = temp_root("python-guard-rust-writer");
        let paths = isolated_paths(&root);
        fs::create_dir_all(paths.proxy_dir()).unwrap();
        let source = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../src-python");
        let python = std::env::var_os("CODEXHUB_PYTHON")
            .map(PathBuf::from)
            .or_else(|| {
                std::env::var_os("USERPROFILE")
                    .map(PathBuf::from)
                    .map(|home| home.join(".local").join("bin").join("python3.13.exe"))
                    .filter(|candidate| candidate.exists())
            })
            .unwrap_or_else(config::find_python);
        let script = "import pathlib, sys; from atomic_io import provider_catalog_transaction_guard; root = pathlib.Path(sys.argv[1]);\nwith provider_catalog_transaction_guard(root):\n print('ready', flush=True)\n if sys.stdin.readline().strip() != 'release': raise SystemExit(2)\nprint('released', flush=True)";
        let mut holder = std::process::Command::new(python)
            .env("PYTHONPATH", source)
            .arg("-c")
            .arg(script)
            .arg(paths.runtime_root())
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .spawn()
            .expect("python is required for cross-process writer tests");
        let mut stdin = holder.stdin.take().unwrap();
        let stdout = holder.stdout.take().unwrap();
        let (python_tx, python_rx) = mpsc::channel();
        thread::spawn(move || {
            for line in std::io::BufReader::new(stdout).lines() {
                let Ok(line) = line else { break };
                if python_tx.send(line).is_err() {
                    break;
                }
            }
        });
        assert_eq!(
            python_rx.recv_timeout(Duration::from_secs(10)).unwrap(),
            "ready"
        );

        let (entered_tx, entered_rx) = mpsc::channel();
        let rust_paths = paths.clone();
        let rust_writer = thread::spawn(move || {
            super::with_transaction_guard_for_paths(&rust_paths, || {
                entered_tx.send(()).unwrap();
                Ok(())
            })
            .unwrap();
        });
        assert!(
            entered_rx
                .recv_timeout(Duration::from_millis(250))
                .is_err(),
            "Rust writer entered while Python held the shared transaction guard"
        );
        stdin.write_all(b"release\n").unwrap();
        stdin.flush().unwrap();
        assert_eq!(
            python_rx.recv_timeout(Duration::from_secs(10)).unwrap(),
            "released"
        );
        assert!(holder.wait().unwrap().success());
        entered_rx
            .recv_timeout(Duration::from_secs(10))
            .expect("Rust writer should enter after Python releases the guard");
        rust_writer.join().unwrap();
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn every_prepare_crash_prefix_has_a_durable_marker_or_untouched_catalog() {
        for phase in [
            "write-preparing-marker",
            "after-preparing-marker",
            "after-provider-backup",
            "after-catalog-backup",
            "write-prepared-marker",
        ] {
            let root = temp_root(phase);
            let paths = isolated_paths(&root);
            config::save_providers_with_paths(vec![provider(UpstreamFormat::Responses)], &paths)
                .unwrap();
            let catalog = "{\"models\":[{\"id\":\"safe-base\"}]}\n";
            write_fixture(&paths.generated_catalog_path(), catalog);
            let mut store = RuntimeProviderCatalogStore::new(paths.clone());
            super::install_transaction_fault(phase);

            let error = store
                .prepare_recovery()
                .expect_err("fault prefix must stop the transaction");
            super::clear_transaction_fault();

            assert!(error.contains(phase));
            if phase == "write-preparing-marker" {
                assert!(!paths.provider_catalog_recovery_path().exists());
                assert_eq!(
                    fs::read_to_string(paths.generated_catalog_path()).unwrap(),
                    catalog
                );
            } else {
                assert!(
                    paths.provider_catalog_recovery_path().exists(),
                    "phase {phase} lost the durable recovery marker"
                );
            }
            let _ = fs::remove_dir_all(root);
        }
    }

    #[test]
    fn every_commit_marker_fault_retains_the_previous_durable_phase() {
        let root = temp_root("commit-marker-prefixes");
        let paths = isolated_paths(&root);
        config::save_providers_with_paths(vec![provider(UpstreamFormat::Responses)], &paths)
            .unwrap();
        write_fixture(&paths.generated_catalog_path(), "{\"models\":[]}\n");
        let mut store = RuntimeProviderCatalogStore::new(paths.clone());
        store.prepare_recovery().unwrap();
        let candidate = provider(UpstreamFormat::ChatCompletions);

        super::install_transaction_fault("write-provider-marker");
        assert!(store
            .mark_provider_write_pending(std::slice::from_ref(&candidate))
            .is_err());
        super::clear_transaction_fault();
        assert!(paths.provider_catalog_recovery_path().exists());

        store
            .mark_provider_write_pending(std::slice::from_ref(&candidate))
            .unwrap();
        store.save_providers(vec![candidate]).unwrap();
        super::install_transaction_fault("write-catalog-marker");
        assert!(store
            .mark_catalog_write_pending("{\"models\":[]}\n")
            .is_err());
        super::clear_transaction_fault();
        assert!(paths.provider_catalog_recovery_path().exists());

        store
            .mark_catalog_write_pending("{\"models\":[]}\n")
            .unwrap();
        write_fixture(&paths.generated_catalog_path(), "{\"models\":[]}\n");
        super::install_transaction_fault("write-committed-marker");
        assert!(store.mark_committed().is_err());
        super::clear_transaction_fault();
        assert!(paths.provider_catalog_recovery_path().exists());
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn every_publication_crash_prefix_recovers_only_exact_transaction_owned_bytes() {
        for phase in [
            "after-provider-marker",
            "after-provider-publish",
            "after-catalog-marker",
            "after-catalog-publish",
        ] {
            let root = temp_root(phase);
            let paths = isolated_paths(&root);
            config::save_providers_with_paths(
                vec![provider(UpstreamFormat::Responses)],
                &paths,
            )
            .unwrap();
            let base_providers = fs::read(paths.runtime_providers_path()).unwrap();
            let base_catalog = capability_catalog_text(UpstreamFormat::Responses, false);
            write_fixture(&paths.generated_catalog_path(), &base_catalog);
            let mut store = RuntimeProviderCatalogStore::new(paths.clone());
            store.prepare_recovery().unwrap();
            let candidate = provider(UpstreamFormat::ChatCompletions);
            store
                .mark_provider_write_pending(std::slice::from_ref(&candidate))
                .unwrap();

            if phase != "after-provider-marker" {
                if phase == "after-provider-publish" {
                    super::install_transaction_fault("after-provider-publish");
                    let error = store
                        .save_providers(vec![candidate])
                        .expect_err("fault must interrupt provider readback");
                    super::clear_transaction_fault();
                    assert!(error.contains("after-provider-publish"));
                } else {
                    store.save_providers(vec![candidate]).unwrap();
                    let candidate_catalog =
                        capability_catalog_text(UpstreamFormat::ChatCompletions, false);
                    store
                        .mark_catalog_write_pending(&candidate_catalog)
                        .unwrap();
                    if phase == "after-catalog-publish" {
                        write_fixture(&paths.generated_catalog_path(), &candidate_catalog);
                    }
                }
            }
            drop(store);

            let mut restarted = RuntimeProviderCatalogStore::new(paths.clone());
            restarted
                .recover_pending()
                .unwrap_or_else(|error| panic!("phase {phase} failed exact recovery: {error}"));

            assert_eq!(
                fs::read(paths.runtime_providers_path()).unwrap(),
                base_providers,
                "phase {phase} did not restore exact provider bytes"
            );
            assert_eq!(
                fs::read_to_string(paths.generated_catalog_path()).unwrap(),
                base_catalog,
                "phase {phase} did not restore exact catalog bytes"
            );
            assert!(!paths.provider_catalog_recovery_path().exists());
            let _ = fs::remove_dir_all(root);
        }
    }

    #[test]
    fn rollback_publishes_only_backup_bytes_loaded_before_the_restore_boundary() {
        let root = temp_root("rollback-immutable-backups");
        let paths = isolated_paths(&root);
        config::save_providers_with_paths(
            vec![provider(UpstreamFormat::Responses)],
            &paths,
        )
        .unwrap();
        let base_provider_bytes = fs::read(paths.runtime_providers_path()).unwrap();
        let base_catalog = capability_catalog_text(UpstreamFormat::Responses, false);
        write_fixture(&paths.generated_catalog_path(), &base_catalog);

        let mut store = RuntimeProviderCatalogStore::new(paths.clone());
        store.prepare_recovery().expect("capture both base snapshots");
        let candidate = provider(UpstreamFormat::ChatCompletions);
        store
            .mark_provider_write_pending(std::slice::from_ref(&candidate))
            .unwrap();
        store.save_providers(vec![candidate]).unwrap();
        let provider_backup = paths.provider_catalog_providers_backup_path();
        let catalog_backup = paths.provider_catalog_catalog_backup_path();
        super::install_test_pre_restore_publish_hook(move || {
            fs::write(&provider_backup, "attacker-provider-backup").unwrap();
            fs::write(&catalog_backup, "attacker-catalog-backup").unwrap();
        });

        let result = store.restore_pending();
        super::clear_test_pre_restore_publish_hook();
        result.expect("validated immutable backups must survive pathname replacement");

        assert_eq!(
            fs::read(paths.runtime_providers_path()).unwrap(),
            base_provider_bytes,
            "rollback must publish only the provider bytes validated before the hook"
        );
        assert_eq!(
            fs::read_to_string(paths.generated_catalog_path()).unwrap(),
            base_catalog,
            "rollback must publish only the catalog bytes validated before the hook"
        );
        let _ = fs::remove_dir_all(root);
    }

    #[cfg(all(
        target_os = "linux",
        any(target_arch = "x86_64", target_arch = "aarch64")
    ))]
    #[test]
    fn aggregate_restore_plans_both_targets_before_provider_mutation() {
        use std::os::unix::fs::PermissionsExt;

        let root = temp_root("aggregate-restore-capacity-preflight");
        let paths = isolated_paths(&root);
        config::save_providers_with_paths(
            vec![provider(UpstreamFormat::Responses)],
            &paths,
        )
        .unwrap();
        let base_catalog = capability_catalog_text(UpstreamFormat::Responses, false);
        write_fixture(&paths.generated_catalog_path(), &base_catalog);

        let mut store = RuntimeProviderCatalogStore::new(paths.clone());
        store.prepare_recovery().unwrap();
        let mut candidate_provider = provider(UpstreamFormat::ChatCompletions);
        candidate_provider.api_key = Some("aggregate-provider-candidate-secret".to_string());
        store
            .mark_provider_write_pending(std::slice::from_ref(&candidate_provider))
            .unwrap();
        store.save_providers(vec![candidate_provider]).unwrap();
        let candidate_provider_bytes = fs::read(paths.runtime_providers_path()).unwrap();
        let candidate_catalog =
            capability_catalog_text(UpstreamFormat::ChatCompletions, false);
        store
            .mark_catalog_write_pending(&candidate_catalog)
            .unwrap();
        write_fixture(&paths.generated_catalog_path(), &candidate_catalog);

        let marker_path = paths.provider_catalog_recovery_path();
        let marker_before = fs::read(&marker_path).unwrap();
        let provider_path = paths.runtime_providers_path();
        let catalog_path = paths.generated_catalog_path();
        let record = store
            .read_recovery()
            .unwrap()
            .expect("restore marker must remain durable");
        let provider_evidence = store
            .rollback_evidence_path(&provider_path, &record.transaction_id)
            .unwrap();
        let catalog_evidence = store
            .rollback_evidence_path(&catalog_path, &record.transaction_id)
            .unwrap();

        for slot in 0..3 {
            let tombstone = crate::safe_file::rollback_tombstone_path(&catalog_path, slot);
            fs::write(&tombstone, b"").unwrap();
            fs::set_permissions(&tombstone, fs::Permissions::from_mode(0o600)).unwrap();
        }
        let final_catalog_slot =
            crate::safe_file::rollback_tombstone_path(&catalog_path, 3);
        let catalog_path_for_hook = catalog_path.clone();
        super::install_test_pre_restore_target_commit_hook(move |target| {
            if target == catalog_path_for_hook {
                fs::write(&final_catalog_slot, b"").unwrap();
                fs::set_permissions(
                    &final_catalog_slot,
                    fs::Permissions::from_mode(0o600),
                )
                .unwrap();
            }
        });

        let error = store
            .restore_pending()
            .expect_err("catalog capacity must fail before the provider plan commits");
        super::clear_test_pre_restore_target_commit_hook();

        assert!(
            error.contains("rollback tombstone capacity exhausted"),
            "unexpected aggregate preflight error: {error}"
        );
        assert_eq!(
            fs::read(&provider_path).unwrap(),
            candidate_provider_bytes,
            "provider bytes must remain at the candidate until every plan succeeds"
        );
        assert_eq!(
            fs::read_to_string(&catalog_path).unwrap(),
            candidate_catalog
        );
        assert!(!provider_evidence.exists());
        assert!(!catalog_evidence.exists());
        assert_eq!(
            fs::read(&marker_path).unwrap(),
            marker_before,
            "failed aggregate planning must preserve the durable marker byte-for-byte"
        );
        let _ = fs::remove_dir_all(root);
    }

    #[cfg(all(
        target_os = "linux",
        any(target_arch = "x86_64", target_arch = "aarch64")
    ))]
    #[test]
    fn aggregate_restore_allows_a_full_completed_target_while_restoring_the_other() {
        use std::os::unix::fs::PermissionsExt;

        let root = temp_root("aggregate-restore-full-noop-target");
        let paths = isolated_paths(&root);
        config::save_providers_with_paths(
            vec![provider(UpstreamFormat::Responses)],
            &paths,
        )
        .unwrap();
        let base_provider_bytes = fs::read(paths.runtime_providers_path()).unwrap();
        let base_catalog = capability_catalog_text(UpstreamFormat::Responses, false);
        write_fixture(&paths.generated_catalog_path(), &base_catalog);

        let mut store = RuntimeProviderCatalogStore::new(paths.clone());
        store.prepare_recovery().unwrap();
        let candidate_provider = provider(UpstreamFormat::ChatCompletions);
        store
            .mark_provider_write_pending(std::slice::from_ref(&candidate_provider))
            .unwrap();
        store.save_providers(vec![candidate_provider]).unwrap();
        let candidate_catalog =
            capability_catalog_text(UpstreamFormat::ChatCompletions, false);
        store
            .mark_catalog_write_pending(&candidate_catalog)
            .unwrap();
        write_fixture(&paths.generated_catalog_path(), &candidate_catalog);

        let provider_path = paths.runtime_providers_path();
        let catalog_path = paths.generated_catalog_path();
        fs::write(&provider_path, &base_provider_bytes).unwrap();
        for slot in 0..4 {
            let tombstone = crate::safe_file::rollback_tombstone_path(&provider_path, slot);
            fs::write(&tombstone, b"").unwrap();
            fs::set_permissions(&tombstone, fs::Permissions::from_mode(0o600)).unwrap();
        }

        store
            .restore_pending()
            .expect("full tombstone capacity on a completed target must not block catalog restore");

        assert_eq!(fs::read(&provider_path).unwrap(), base_provider_bytes);
        assert_eq!(fs::read_to_string(&catalog_path).unwrap(), base_catalog);
        assert_eq!(
            fs::metadata(crate::safe_file::rollback_tombstone_path(
                &catalog_path,
                0,
            ))
            .unwrap()
            .len(),
            0
        );
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn rollback_never_clobbers_unjournaled_live_bytes_after_prefix_validation() {
        let root = temp_root("rollback-existing-target-cas");
        let paths = isolated_paths(&root);
        config::save_providers_with_paths(
            vec![provider(UpstreamFormat::Responses)],
            &paths,
        )
        .unwrap();
        let base_catalog = capability_catalog_text(UpstreamFormat::Responses, false);
        write_fixture(&paths.generated_catalog_path(), &base_catalog);
        let mut store = RuntimeProviderCatalogStore::new(paths.clone());
        store.prepare_recovery().unwrap();
        let candidate = provider(UpstreamFormat::ChatCompletions);
        store
            .mark_provider_write_pending(std::slice::from_ref(&candidate))
            .unwrap();
        store.save_providers(vec![candidate]).unwrap();
        let live_provider = paths.runtime_providers_path();
        let external = b"unjournaled-provider-bytes-after-prefix-validation".to_vec();
        super::install_test_pre_restore_target_commit_hook({
            let live_provider = live_provider.clone();
            let external = external.clone();
            move |path| {
                if path == live_provider {
                    fs::write(path, &external).unwrap();
                }
            }
        });

        let error = store
            .restore_pending()
            .expect_err("rollback must not clobber bytes written after prefix validation");
        super::clear_test_pre_restore_target_commit_hook();

        assert!(
            error.contains("changed") || error.contains("mismatch"),
            "unexpected rollback error: {error}"
        );
        assert!(paths.provider_catalog_recovery_path().exists());
        let evidence_preserved = fs::read(&live_provider)
            .is_ok_and(|contents| contents == external)
            || fs::read_dir(live_provider.parent().unwrap())
                .unwrap()
                .filter_map(Result::ok)
                .filter(|entry| entry.path().is_file())
                .any(|entry| fs::read(entry.path()).is_ok_and(|contents| contents == external));
        assert!(
            evidence_preserved,
            "the unjournaled live bytes must remain exact at the live path or in transaction evidence"
        );
        let _ = fs::remove_dir_all(root);
    }

    #[cfg(windows)]
    #[test]
    fn rollback_keeps_the_durable_marker_when_evidence_path_is_repopulated_before_handle_delete() {
        let root = temp_root("rollback-evidence-handle-delete-race");
        let paths = isolated_paths(&root);
        config::save_providers_with_paths(
            vec![provider(UpstreamFormat::Responses)],
            &paths,
        )
        .unwrap();
        let base_provider_bytes = fs::read(paths.runtime_providers_path()).unwrap();
        let base_catalog = capability_catalog_text(UpstreamFormat::Responses, false);
        write_fixture(&paths.generated_catalog_path(), &base_catalog);
        let mut store = RuntimeProviderCatalogStore::new(paths.clone());
        store.prepare_recovery().unwrap();
        let mut candidate = provider(UpstreamFormat::ChatCompletions);
        candidate.api_key = Some("candidate-secret-before-handle-delete".to_string());
        store
            .mark_provider_write_pending(std::slice::from_ref(&candidate))
            .unwrap();
        store.save_providers(vec![candidate]).unwrap();
        let observed_evidence =
            std::rc::Rc::new(std::cell::RefCell::new(None::<PathBuf>));
        let observed_evidence_for_hook = observed_evidence.clone();
        crate::safe_file::install_test_pre_private_evidence_handle_delete_hook(move |path| {
            let moved_owned = path.with_file_name("owned-provider-evidence-before-delete");
            fs::rename(path, moved_owned).unwrap();
            fs::write(path, "unowned-evidence-after-verification").unwrap();
            *observed_evidence_for_hook.borrow_mut() = Some(path.to_path_buf());
        });

        let error = store
            .restore_pending()
            .expect_err("evidence pathname replacement must retain the recovery marker");
        crate::safe_file::clear_test_pre_private_evidence_handle_delete_hook();
        let evidence = observed_evidence
            .borrow()
            .clone()
            .expect("provider rollback must reach evidence cleanup");

        assert!(error.contains("evidence pathname mismatch"));
        assert_eq!(
            fs::read(paths.runtime_providers_path()).unwrap(),
            base_provider_bytes
        );
        assert_eq!(
            fs::read_to_string(paths.generated_catalog_path()).unwrap(),
            base_catalog
        );
        assert_eq!(
            fs::read_to_string(evidence).unwrap(),
            "unowned-evidence-after-verification"
        );
        assert!(
            paths.provider_catalog_recovery_path().exists(),
            "cleanup mismatch must keep the durable recovery marker"
        );
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn rollback_never_deletes_unjournaled_bytes_for_an_absent_base_snapshot() {
        let root = temp_root("rollback-absent-snapshot-owner-cas");
        let repo_root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .expect("src-tauri has a repository parent")
            .to_path_buf();
        let paths = config::ConfigPaths::new_isolated(
            root.join("runtime"),
            root.join("codex-target"),
            repo_root,
        );
        assert!(!paths.runtime_providers_path().exists());
        assert!(!paths.generated_catalog_path().exists());

        let mut store = RuntimeProviderCatalogStore::new(paths.clone());
        store
            .prepare_recovery()
            .expect("capture absent runtime provider and catalog snapshots");
        let candidate = provider(UpstreamFormat::Responses);
        store
            .mark_provider_write_pending(std::slice::from_ref(&candidate))
            .unwrap();
        store.save_providers(vec![candidate]).unwrap();
        let live_provider = paths.runtime_providers_path();
        super::install_test_pre_restore_publish_hook({
            let live_provider = live_provider.clone();
            move || {
                fs::write(&live_provider, "unjournaled-external-provider-bytes").unwrap();
            }
        });

        let error = store
            .restore_pending()
            .expect_err("an absent snapshot may delete only its exact journaled candidate");
        super::clear_test_pre_restore_publish_hook();

        assert!(
            error.contains("candidate")
                || error.contains("journal")
                || error.contains("changed"),
            "unexpected rollback error: {error}"
        );
        assert_eq!(
            fs::read_to_string(&live_provider).unwrap(),
            "unjournaled-external-provider-bytes",
            "rollback must not delete a replacement it does not own"
        );
        assert!(paths.provider_catalog_recovery_path().exists());
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn rollback_never_deletes_replacement_bytes_after_absent_candidate_validation() {
        let root = temp_root("rollback-absent-target-commit-race");
        let repo_root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .expect("src-tauri has a repository parent")
            .to_path_buf();
        let paths = config::ConfigPaths::new_isolated(
            root.join("runtime"),
            root.join("codex-target"),
            repo_root,
        );
        let mut store = RuntimeProviderCatalogStore::new(paths.clone());
        store.prepare_recovery().unwrap();
        let candidate = provider(UpstreamFormat::Responses);
        store
            .mark_provider_write_pending(std::slice::from_ref(&candidate))
            .unwrap();
        store.save_providers(vec![candidate]).unwrap();
        let live_provider = paths.runtime_providers_path();
        let external = b"unjournaled-bytes-after-absent-candidate-validation".to_vec();
        super::install_test_pre_restore_target_commit_hook({
            let live_provider = live_provider.clone();
            let external = external.clone();
            move |path| {
                if path == live_provider {
                    fs::write(path, &external).unwrap();
                }
            }
        });

        let error = store
            .restore_pending()
            .expect_err("rollback must not delete bytes replacing a validated absent candidate");
        super::clear_test_pre_restore_target_commit_hook();

        assert!(
            error.contains("changed") || error.contains("mismatch"),
            "unexpected rollback error: {error}"
        );
        assert!(paths.provider_catalog_recovery_path().exists());
        let evidence_preserved = fs::read(&live_provider)
            .is_ok_and(|contents| contents == external)
            || fs::read_dir(live_provider.parent().unwrap())
                .unwrap()
                .filter_map(Result::ok)
                .filter(|entry| entry.path().is_file())
                .any(|entry| fs::read(entry.path()).is_ok_and(|contents| contents == external));
        assert!(
            evidence_preserved,
            "the replacement bytes must remain exact at the live path or in transaction evidence"
        );
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn runtime_transient_recovery_failure_durably_authorizes_the_disabled_catalog_before_retry() {
        let root = temp_root("runtime-disabled-retry");
        let repo_root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .expect("src-tauri has a repository parent")
            .to_path_buf();
        let paths = config::ConfigPaths::new_isolated(
            root.join("runtime"),
            root.join("codex-target"),
            repo_root,
        );
        let base = provider(UpstreamFormat::Responses);
        config::save_providers_with_paths(vec![base], &paths).unwrap();
        write_fixture(
            &paths.generated_catalog_path(),
            &capability_catalog_text(UpstreamFormat::Responses, false),
        );
        let mut store = RuntimeProviderCatalogStore::new(paths.clone());
        store.prepare_recovery().expect("prepare real recovery");
        let mut candidate = provider(UpstreamFormat::ChatCompletions);
        candidate.api_key = Some("candidate-api-secret-must-not-enter-marker".to_string());
        store
            .mark_provider_write_pending(std::slice::from_ref(&candidate))
            .unwrap();
        store.save_providers(vec![candidate.clone()]).unwrap();
        let candidate_catalog =
            capability_catalog_text(UpstreamFormat::ChatCompletions, false);
        store
            .mark_catalog_write_pending(&candidate_catalog)
            .unwrap();
        write_fixture(&paths.generated_catalog_path(), &candidate_catalog);
        let candidate_provider_bytes = fs::read(paths.runtime_providers_path()).unwrap();
        drop(store);

        // Make recovery fail once without changing either transaction-owned
        // live prefix. Restoring this file models a transient filesystem
        // failure across a process restart.
        let catalog_backup = paths.provider_catalog_catalog_backup_path();
        let parked_backup = catalog_backup.with_extension("transiently-unavailable");
        fs::rename(&catalog_backup, &parked_backup).unwrap();
        let revision = issue_provider_catalog_revision(&paths).unwrap();

        let first = persist_provider_catalog_state_with_paths(
            &paths,
            vec![candidate.clone()],
            revision,
        )
        .expect("typed transaction returns a recovery-required result");

        assert_eq!(
            first.outcome,
            ProviderCatalogTransactionOutcome::RecoveryRequired
        );
        assert!(first.catalog_disabled);
        assert_eq!(
            fs::read(paths.runtime_providers_path()).unwrap(),
            candidate_provider_bytes,
            "fail-closed handling must preserve an exact transaction-owned provider prefix"
        );
        assert_eq!(
            fs::read_to_string(paths.generated_catalog_path()).unwrap(),
            "{\"models\":[]}\n"
        );
        let marker_text = fs::read_to_string(paths.provider_catalog_recovery_path()).unwrap();
        assert!(!marker_text.contains("candidate-api-secret-must-not-enter-marker"));
        let marker = RuntimeProviderCatalogStore::new(paths.clone())
            .read_recovery()
            .unwrap()
            .expect("disabled marker remains durable");
        assert_eq!(
            marker.state,
            RecoveryState::CatalogDisabled,
            "the durable marker must authorize the sentinel before retry validates it"
        );

        fs::rename(&parked_backup, &catalog_backup).unwrap();
        let mut restarted = RuntimeProviderCatalogStore::new(paths.clone());
        restarted
            .recover_pending()
            .expect("a later real RuntimeStore must regenerate and clear disabled recovery");
        let providers = restarted.current_providers().unwrap();
        let models = restarted.current_catalog().unwrap();
        verify_catalog_for_providers(&models, &providers).unwrap();
        assert_eq!(
            fs::read(paths.runtime_providers_path()).unwrap(),
            candidate_provider_bytes
        );
        assert!(!paths.provider_catalog_recovery_path().exists());
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn catalog_disabled_recovery_completes_after_crash_between_quarantine_and_sentinel() {
        let root = temp_root("disabled-after-quarantine-crash");
        let repo_root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .expect("src-tauri has a repository parent")
            .to_path_buf();
        let paths = config::ConfigPaths::new_isolated(
            root.join("runtime"),
            root.join("codex-target"),
            repo_root,
        );
        let configured = provider(UpstreamFormat::Responses);
        config::save_providers_with_paths(vec![configured], &paths).unwrap();
        let original_catalog = capability_catalog_text(UpstreamFormat::Responses, false);
        write_fixture(&paths.generated_catalog_path(), &original_catalog);

        let mut store = RuntimeProviderCatalogStore::new(paths.clone());
        store
            .ensure_recovery_required()
            .expect("publish durable catalog-disabled marker");
        let record = store
            .read_recovery()
            .unwrap()
            .expect("catalog-disabled marker");
        let quarantine = store
            .catalog_quarantine_path(&record.transaction_id)
            .expect("transaction-owned quarantine path");

        super::install_transaction_fault("after-catalog-quarantine");
        let error = store
            .invalidate_catalog()
            .expect_err("fault must interrupt sentinel publication");
        super::clear_transaction_fault();
        assert!(error.contains("after-catalog-quarantine"));
        #[cfg(not(all(
            target_os = "linux",
            any(target_arch = "x86_64", target_arch = "aarch64")
        )))]
        assert!(
            !paths.generated_catalog_path().exists(),
            "the handle-bound rename crash prefix must leave the live catalog name absent"
        );
        #[cfg(all(
            target_os = "linux",
            any(target_arch = "x86_64", target_arch = "aarch64")
        ))]
        assert_eq!(
            fs::read_to_string(paths.generated_catalog_path()).unwrap(),
            DISABLED_CATALOG,
            "the Linux exchange crash prefix must already expose the exact sentinel"
        );
        assert_eq!(
            fs::read_to_string(&quarantine).unwrap(),
            original_catalog,
            "the exact authorized catalog must already be durable in quarantine"
        );
        drop(store);

        let mut restarted = RuntimeProviderCatalogStore::new(paths.clone());
        restarted
            .recover_pending()
            .expect("restart must complete sentinel publication and regeneration");
        let providers = restarted.current_providers().unwrap();
        let models = restarted.current_catalog().unwrap();
        verify_catalog_for_providers(&models, &providers).unwrap();
        assert!(!paths.provider_catalog_recovery_path().exists());
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn catalog_disabled_prefix_accepts_an_originally_absent_catalog_without_quarantine() {
        let root = temp_root("disabled-originally-absent");
        let paths = isolated_paths(&root);
        config::save_providers_with_paths(
            vec![provider(UpstreamFormat::Responses)],
            &paths,
        )
        .unwrap();
        fs::create_dir_all(
            paths
                .generated_catalog_path()
                .parent()
                .expect("catalog parent"),
        )
        .unwrap();
        assert!(!paths.generated_catalog_path().exists());

        let mut store = RuntimeProviderCatalogStore::new(paths.clone());
        store
            .ensure_recovery_required()
            .expect("authorize an absent catalog");
        let record = store
            .read_recovery()
            .unwrap()
            .expect("catalog-disabled marker");
        assert!(!record.catalog.existed);
        store
            .verify_disabled_prefix(&record)
            .expect("the initial absent prefix is legal");
        store
            .invalidate_catalog()
            .expect("an absent catalog publishes only the sentinel");
        let record = store
            .read_recovery()
            .unwrap()
            .expect("marker remains until regeneration");
        store
            .verify_disabled_prefix(&record)
            .expect("the completed absent prefix is legal");
        assert_eq!(
            fs::read_to_string(paths.generated_catalog_path()).unwrap(),
            DISABLED_CATALOG
        );
        let quarantine = store
            .catalog_quarantine_path(&record.transaction_id)
            .unwrap();
        assert!(!quarantine.exists());
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn catalog_disabled_prefix_rejects_mismatched_quarantine_without_clearing_the_marker() {
        let root = temp_root("disabled-mismatched-quarantine");
        let paths = isolated_paths(&root);
        config::save_providers_with_paths(
            vec![provider(UpstreamFormat::Responses)],
            &paths,
        )
        .unwrap();
        let original_catalog = capability_catalog_text(UpstreamFormat::Responses, false);
        write_fixture(&paths.generated_catalog_path(), &original_catalog);
        let mut store = RuntimeProviderCatalogStore::new(paths.clone());
        store
            .ensure_recovery_required()
            .expect("publish catalog-disabled marker");
        let record = store.read_recovery().unwrap().expect("marker");
        let quarantine = store
            .catalog_quarantine_path(&record.transaction_id)
            .unwrap();
        write_fixture(&paths.generated_catalog_path(), DISABLED_CATALOG);
        write_fixture(&quarantine, "unjournaled-replacement-evidence");

        let error = store
            .recover_pending()
            .expect_err("mismatched quarantine evidence must fail closed");

        assert!(error.contains("authorized crash prefix"));
        assert!(paths.provider_catalog_recovery_path().exists());
        assert_eq!(
            fs::read_to_string(paths.generated_catalog_path()).unwrap(),
            DISABLED_CATALOG
        );
        assert_eq!(
            fs::read_to_string(&quarantine).unwrap(),
            "unjournaled-replacement-evidence"
        );
        let _ = fs::remove_dir_all(root);
    }

    #[cfg(all(
        target_os = "linux",
        any(target_arch = "x86_64", target_arch = "aarch64")
    ))]
    #[test]
    fn catalog_disabled_exchange_crash_prefixes_recover_to_a_generated_catalog() {
        for phase in [
            "placeholder-publish",
            "after-placeholder",
            "before-exchange",
            "exchange",
            "after-exchange",
        ] {
            let root = temp_root(&format!("disabled-linux-{phase}"));
            let repo_root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                .parent()
                .expect("src-tauri has a repository parent")
                .to_path_buf();
            let paths = config::ConfigPaths::new_isolated(
                root.join("runtime"),
                root.join("codex-target"),
                repo_root,
            );
            config::save_providers_with_paths(
                vec![provider(UpstreamFormat::Responses)],
                &paths,
            )
            .unwrap();
            let original_catalog = capability_catalog_text(UpstreamFormat::Responses, false);
            write_fixture(&paths.generated_catalog_path(), &original_catalog);
            let mut store = RuntimeProviderCatalogStore::new(paths.clone());
            store.ensure_recovery_required().unwrap();

            crate::safe_file::install_test_private_quarantine_fault(phase);
            let error = store
                .invalidate_catalog()
                .expect_err("the injected Linux exchange fault must interrupt invalidation");
            crate::safe_file::clear_test_private_quarantine_fault();
            assert!(error.contains(phase));
            drop(store);

            let mut restarted = RuntimeProviderCatalogStore::new(paths.clone());
            restarted
                .recover_pending()
                .unwrap_or_else(|error| panic!("phase {phase} failed recovery: {error}"));
            let providers = restarted.current_providers().unwrap();
            let models = restarted.current_catalog().unwrap();
            verify_catalog_for_providers(&models, &providers).unwrap();
            assert!(!paths.provider_catalog_recovery_path().exists());
            let _ = fs::remove_dir_all(root);
        }
    }

    #[cfg(all(
        target_os = "linux",
        any(target_arch = "x86_64", target_arch = "aarch64")
    ))]
    #[test]
    fn catalog_disabled_inode_mismatch_preserves_evidence_and_blocks_recovery() {
        let root = temp_root("disabled-linux-inode-mismatch");
        let paths = isolated_paths(&root);
        config::save_providers_with_paths(
            vec![provider(UpstreamFormat::Responses)],
            &paths,
        )
        .unwrap();
        let original_catalog = capability_catalog_text(UpstreamFormat::Responses, false);
        let catalog = paths.generated_catalog_path();
        write_fixture(&catalog, &original_catalog);
        let displaced = catalog.with_file_name("catalog.original");
        let replacement = catalog.with_file_name("catalog.replacement");
        fs::write(&replacement, "one-shot-replacement").unwrap();
        let mut store = RuntimeProviderCatalogStore::new(paths.clone());
        store.ensure_recovery_required().unwrap();
        let record = store.read_recovery().unwrap().expect("marker");
        let quarantine = store
            .catalog_quarantine_path(&record.transaction_id)
            .unwrap();
        crate::safe_file::install_test_pre_private_quarantine_rename_hook({
            let catalog = catalog.clone();
            let displaced = displaced.clone();
            let replacement = replacement.clone();
            move |_| {
                fs::rename(&catalog, &displaced).unwrap();
                fs::rename(&replacement, &catalog).unwrap();
            }
        });

        let error = store
            .invalidate_catalog()
            .expect_err("a losing source inode must never report success");
        crate::safe_file::clear_test_pre_private_quarantine_rename_hook();
        assert!(error.contains("identity"));
        assert_eq!(fs::read_to_string(&catalog).unwrap(), DISABLED_CATALOG);
        assert_eq!(
            fs::read_to_string(&quarantine).unwrap(),
            "one-shot-replacement"
        );
        assert_eq!(fs::read_to_string(&displaced).unwrap(), original_catalog);
        assert!(paths.provider_catalog_recovery_path().exists());

        let recovery_error = store
            .recover_pending()
            .expect_err("mismatched evidence must block regeneration");
        assert!(recovery_error.contains("authorized crash prefix"));
        assert!(paths.provider_catalog_recovery_path().exists());
        assert_eq!(fs::read_to_string(&catalog).unwrap(), DISABLED_CATALOG);
        assert_eq!(
            fs::read_to_string(&quarantine).unwrap(),
            "one-shot-replacement"
        );
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn catalog_disabled_marker_rejects_duplicate_future_unknown_and_provider_owner_mismatch() {
        for attack in ["duplicate", "future", "unknown", "owner-mismatch"] {
            let root = temp_root(&format!("disabled-marker-{attack}"));
            let paths = isolated_paths(&root);
            let mut configured = provider(UpstreamFormat::Responses);
            configured.api_key = Some("disabled-marker-secret".to_string());
            config::save_providers_with_paths(vec![configured], &paths).unwrap();
            let mut store = RuntimeProviderCatalogStore::new(paths.clone());
            store
                .ensure_recovery_required()
                .expect("create a valid disabled marker");
            let marker_path = paths.provider_catalog_recovery_path();
            let marker = fs::read_to_string(&marker_path).unwrap();
            assert!(!marker.contains("disabled-marker-secret"));
            let attacked = match attack {
                "duplicate" => marker.replacen(
                    "\"state\":\"catalog_disabled\"",
                    "\"state\":\"catalog_disabled\",\"state\":\"catalog_disabled\"",
                    1,
                ),
                "future" => marker.replacen(
                    &format!("\"schema_version\":{RECOVERY_SCHEMA_VERSION}"),
                    "\"schema_version\":999",
                    1,
                ),
                "unknown" => marker.replacen(
                    "\"state\":\"catalog_disabled\"",
                    "\"state\":\"catalog_disabled\",\"unknown_owner\":\"attacker\"",
                    1,
                ),
                "owner-mismatch" => {
                    let expected = store
                        .read_recovery()
                        .unwrap()
                        .unwrap()
                        .committed_providers_sha256
                        .unwrap();
                    marker.replacen(&expected, &"f".repeat(64), 1)
                }
                _ => unreachable!(),
            };
            write_fixture(&marker_path, &attacked);

            let error = if attack == "owner-mismatch" {
                store
                    .recover_pending()
                    .expect_err("provider owner mismatch must remain blocked")
            } else {
                store
                    .read_recovery()
                    .expect_err("invalid disabled marker must be rejected")
            };

            assert!(
                error.contains("duplicate")
                    || error.contains("schema version")
                    || error.contains("unknown field")
                    || error.contains("provider configuration changed"),
                "{attack} returned an unexpected parser error: {error}"
            );
            assert!(marker_path.exists());
            let _ = fs::remove_dir_all(root);
        }
    }

    #[test]
    fn runtime_recovery_restores_exact_provider_and_catalog_file_snapshots() {
        let root = temp_root("restore-exact");
        let paths = isolated_paths(&root);
        let providers_path = paths.runtime_providers_path();
        let catalog_path = paths.generated_catalog_path();
        let mut original_provider = provider(UpstreamFormat::Responses);
        original_provider.api_key = Some("secret".to_string());
        config::save_providers_with_paths(vec![original_provider], &paths).unwrap();
        let original_providers = fs::read_to_string(&providers_path).unwrap();
        let original_catalog = "{\n  \"models\": [{\"id\":\"glm-5.2\"}]\n}\n";
        write_fixture(&catalog_path, original_catalog);
        let mut store = RuntimeProviderCatalogStore::new(paths.clone());

        store.prepare_recovery().expect("prepare recovery journal");
        let journal = fs::read_to_string(paths.provider_catalog_recovery_path())
            .expect("read recovery journal");
        assert!(!journal.contains("secret"));
        assert!(!journal.contains(&original_providers));
        assert_eq!(
            fs::read_to_string(paths.provider_catalog_providers_backup_path())
                .expect("read provider recovery backup"),
            original_providers
        );
        #[cfg(windows)]
        {
            let backup = paths.provider_catalog_providers_backup_path();
            let sddl = crate::safe_file::security_descriptor_sddl(&backup)
                .expect("inspect recovery backup DACL");
            assert!(sddl.contains("D:P"), "DACL must be protected: {sddl}");
            for broad_sid in [";;;WD)", ";;;BU)", ";;;AU)"] {
                assert!(
                    !sddl.contains(broad_sid),
                    "secret backup grants a broad principal: {sddl}"
                );
            }
        }
        let mut candidate = provider(UpstreamFormat::ChatCompletions);
        candidate.name = "Candidate".to_string();
        candidate.api_key = Some("candidate-secret".to_string());
        store
            .mark_provider_write_pending(std::slice::from_ref(&candidate))
            .unwrap();
        let candidate_journal =
            fs::read_to_string(paths.provider_catalog_recovery_path()).unwrap();
        assert!(!candidate_journal.contains("candidate-secret"));
        store.save_providers(vec![candidate]).unwrap();
        store
            .mark_catalog_write_pending("{\"models\":[]}\n")
            .unwrap();
        write_fixture(&catalog_path, "{\"models\":[]}\n");
        drop(store);
        let mut restarted_store = RuntimeProviderCatalogStore::new(paths.clone());
        restarted_store
            .recover_pending()
            .expect("recover journal after simulated restart");

        assert_eq!(
            fs::read_to_string(&providers_path).expect("read restored providers"),
            original_providers
        );
        assert_eq!(
            fs::read_to_string(&catalog_path).expect("read restored catalog"),
            original_catalog
        );
        assert!(!paths.provider_catalog_recovery_path().exists());
        assert!(!paths.provider_catalog_providers_backup_path().exists());
        assert!(!paths.provider_catalog_catalog_backup_path().exists());
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn catalog_write_restart_finishes_existing_provider_rollback_after_first_restore_crash() {
        let root = temp_root("catalog-write-existing-first-restore-crash");
        let paths = isolated_paths(&root);
        let providers_path = paths.runtime_providers_path();
        let catalog_path = paths.generated_catalog_path();
        config::save_providers_with_paths(
            vec![provider(UpstreamFormat::Responses)],
            &paths,
        )
        .unwrap();
        let base_provider = fs::read(&providers_path).unwrap();
        let base_catalog = capability_catalog_text(UpstreamFormat::Responses, false);
        write_fixture(&catalog_path, &base_catalog);
        let mut store = RuntimeProviderCatalogStore::new(paths.clone());
        store.prepare_recovery().unwrap();
        let candidate = provider(UpstreamFormat::ChatCompletions);
        store
            .mark_provider_write_pending(std::slice::from_ref(&candidate))
            .unwrap();
        store.save_providers(vec![candidate]).unwrap();
        #[cfg(windows)]
        let candidate_provider = fs::read(&providers_path).unwrap();
        let candidate_catalog =
            capability_catalog_text(UpstreamFormat::ChatCompletions, false);
        store
            .mark_catalog_write_pending(&candidate_catalog)
            .unwrap();
        write_fixture(&catalog_path, &candidate_catalog);
        let marker = store.read_recovery().unwrap().unwrap();
        let provider_evidence = store
            .rollback_evidence_path(&providers_path, &marker.transaction_id)
            .unwrap();

        super::install_transaction_fault("after-provider-restore");
        let error = store
            .restore_pending()
            .expect_err("the injected crash must stop before catalog rollback");
        super::clear_transaction_fault();

        assert!(error.contains("after-provider-restore"));
        assert_eq!(fs::read(&providers_path).unwrap(), base_provider);
        assert_eq!(fs::read_to_string(&catalog_path).unwrap(), candidate_catalog);
        assert!(paths.provider_catalog_recovery_path().exists());
        #[cfg(windows)]
        write_fixture(
            &provider_evidence,
            std::str::from_utf8(&candidate_provider).unwrap(),
        );
        drop(store);

        let mut restarted = RuntimeProviderCatalogStore::new(paths.clone());
        restarted
            .recover_pending()
            .expect("restart must accept the exact restored provider base prefix");

        assert_eq!(fs::read(&providers_path).unwrap(), base_provider);
        assert_eq!(fs::read_to_string(&catalog_path).unwrap(), base_catalog);
        assert!(!provider_evidence.exists());
        assert!(!paths.provider_catalog_recovery_path().exists());
        assert!(!paths.provider_catalog_providers_backup_path().exists());
        assert!(!paths.provider_catalog_catalog_backup_path().exists());
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn catalog_write_restart_finishes_absent_provider_rollback_after_first_restore_crash() {
        let root = temp_root("catalog-write-absent-first-restore-crash");
        let repo_root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .expect("src-tauri has a repository parent")
            .to_path_buf();
        let paths = config::ConfigPaths::new_isolated(
            root.join("runtime"),
            root.join("codex"),
            repo_root,
        );
        let providers_path = paths.runtime_providers_path();
        let catalog_path = paths.generated_catalog_path();
        assert!(!providers_path.exists());
        let base_catalog = capability_catalog_text(UpstreamFormat::Responses, false);
        write_fixture(&catalog_path, &base_catalog);
        let mut store = RuntimeProviderCatalogStore::new(paths.clone());
        store.prepare_recovery().unwrap();
        let candidate = provider(UpstreamFormat::ChatCompletions);
        store
            .mark_provider_write_pending(std::slice::from_ref(&candidate))
            .unwrap();
        store.save_providers(vec![candidate]).unwrap();
        #[cfg(windows)]
        let candidate_provider = fs::read(&providers_path).unwrap();
        let candidate_catalog =
            capability_catalog_text(UpstreamFormat::ChatCompletions, false);
        store
            .mark_catalog_write_pending(&candidate_catalog)
            .unwrap();
        write_fixture(&catalog_path, &candidate_catalog);
        let marker = store.read_recovery().unwrap().unwrap();
        let provider_evidence = store
            .rollback_evidence_path(&providers_path, &marker.transaction_id)
            .unwrap();

        super::install_transaction_fault("after-provider-restore");
        let error = store
            .restore_pending()
            .expect_err("the injected crash must stop before catalog rollback");
        super::clear_transaction_fault();

        assert!(error.contains("after-provider-restore"));
        assert!(!providers_path.exists());
        assert_eq!(fs::read_to_string(&catalog_path).unwrap(), candidate_catalog);
        assert!(paths.provider_catalog_recovery_path().exists());
        #[cfg(windows)]
        write_fixture(
            &provider_evidence,
            std::str::from_utf8(&candidate_provider).unwrap(),
        );
        drop(store);

        let mut restarted = RuntimeProviderCatalogStore::new(paths.clone());
        restarted
            .recover_pending()
            .expect("restart must accept the exact absent provider base prefix");

        assert!(!providers_path.exists());
        assert_eq!(fs::read_to_string(&catalog_path).unwrap(), base_catalog);
        assert!(!provider_evidence.exists());
        assert!(!paths.provider_catalog_recovery_path().exists());
        assert!(!paths.provider_catalog_providers_backup_path().exists());
        assert!(!paths.provider_catalog_catalog_backup_path().exists());
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn catalog_write_recovery_preserves_semantically_equal_external_provider_edit() {
        let root = temp_root("catalog-write-external-formatting");
        let paths = isolated_paths(&root);
        config::save_providers_with_paths(vec![provider(UpstreamFormat::Responses)], &paths)
            .unwrap();
        write_fixture(&paths.generated_catalog_path(), "{\"models\":[]}\n");
        let mut store = RuntimeProviderCatalogStore::new(paths.clone());
        store.prepare_recovery().unwrap();
        let candidate = provider(UpstreamFormat::ChatCompletions);
        store
            .mark_provider_write_pending(std::slice::from_ref(&candidate))
            .unwrap();
        store.save_providers(vec![candidate]).unwrap();
        store
            .mark_catalog_write_pending("{\"models\":[]}\n")
            .unwrap();
        let externally_edited = format!(
            "{}# external owner preserved this comment\n",
            fs::read_to_string(paths.runtime_providers_path()).unwrap()
        );
        write_fixture(&paths.runtime_providers_path(), &externally_edited);
        let catalog_before = fs::read_to_string(paths.generated_catalog_path()).unwrap();

        let error = store
            .recover_pending()
            .expect_err("raw provider divergence must not be overwritten");

        assert!(error.contains("exact catalog-write base or candidate bytes"));
        assert_eq!(
            fs::read_to_string(paths.runtime_providers_path()).unwrap(),
            externally_edited
        );
        assert_eq!(
            fs::read_to_string(paths.generated_catalog_path()).unwrap(),
            catalog_before
        );
        assert!(paths.provider_catalog_recovery_path().exists());
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn catalog_write_recovery_accepts_only_the_exact_base_or_journaled_candidate_bytes() {
        for (label, external_catalog) in [
            ("external-empty", "{\"models\":[]}\n".to_string()),
            (
                "external-semantic",
                capability_catalog_text(UpstreamFormat::ChatCompletions, true),
            ),
        ] {
            let root = temp_root(label);
            let paths = isolated_paths(&root);
            config::save_providers_with_paths(
                vec![provider(UpstreamFormat::Responses)],
                &paths,
            )
            .unwrap();
            let base_catalog = capability_catalog_text(UpstreamFormat::Responses, false);
            write_fixture(&paths.generated_catalog_path(), &base_catalog);
            let mut store = RuntimeProviderCatalogStore::new(paths.clone());
            store.prepare_recovery().unwrap();
            let candidate = provider(UpstreamFormat::ChatCompletions);
            store
                .mark_provider_write_pending(std::slice::from_ref(&candidate))
                .unwrap();
            store.save_providers(vec![candidate]).unwrap();
            let candidate_catalog =
                capability_catalog_text(UpstreamFormat::ChatCompletions, false);
            store
                .mark_catalog_write_pending(&candidate_catalog)
                .unwrap();
            assert_ne!(
                external_catalog.as_bytes(),
                candidate_catalog.as_bytes(),
                "fixture must be semantically or structurally different at the raw boundary"
            );
            write_fixture(&paths.generated_catalog_path(), &external_catalog);
            let provider_before = fs::read(paths.runtime_providers_path()).unwrap();

            let error = store
                .recover_pending()
                .expect_err("unknown catalog bytes must not be rolled back over");

            assert!(error.contains("exact base and candidate bytes"));
            assert_eq!(
                fs::read(paths.runtime_providers_path()).unwrap(),
                provider_before
            );
            assert_eq!(
                fs::read_to_string(paths.generated_catalog_path()).unwrap(),
                external_catalog
            );
            assert!(paths.provider_catalog_recovery_path().exists());

            let startup_error = recover_before_gateway_with_store(&mut store)
                .expect_err("Gateway must remain blocked after catalog divergence");
            assert!(startup_error.contains("generated catalog disabled fail-closed"));
            assert_eq!(
                fs::read(paths.runtime_providers_path()).unwrap(),
                provider_before
            );
            assert_eq!(
                fs::read_to_string(paths.generated_catalog_path()).unwrap(),
                "{\"models\":[]}\n"
            );
            let quarantined = fs::read_dir(paths.generated_catalog_path().parent().unwrap())
                .unwrap()
                .filter_map(Result::ok)
                .map(|entry| entry.path())
                .find(|path| {
                    path.file_name()
                        .and_then(|name| name.to_str())
                        .is_some_and(|name| name.ends_with(".quarantine"))
                })
                .expect("divergent catalog evidence must be quarantined");
            assert_eq!(fs::read_to_string(quarantined).unwrap(), external_catalog);
            assert!(paths.provider_catalog_recovery_path().exists());
            let _ = fs::remove_dir_all(root);
        }
    }

    #[test]
    fn stale_prepared_journal_never_overwrites_external_provider_catalog_or_both_changes() {
        for changed in ["provider", "catalog", "both"] {
            let root = temp_root(changed);
            let paths = isolated_paths(&root);
            config::save_providers_with_paths(vec![provider(UpstreamFormat::Responses)], &paths)
                .unwrap();
            write_fixture(
                &paths.generated_catalog_path(),
                "{\"models\":[{\"id\":\"base\"}]}\n",
            );
            let mut store = RuntimeProviderCatalogStore::new(paths.clone());
            store.prepare_recovery().unwrap();
            if changed == "provider" || changed == "both" {
                let mut external = provider(UpstreamFormat::ChatCompletions);
                external.name = "External owner".to_string();
                config::save_providers_with_paths(vec![external], &paths).unwrap();
            }
            if changed == "catalog" || changed == "both" {
                write_fixture(
                    &paths.generated_catalog_path(),
                    "{\"models\":[{\"id\":\"external\"}]}\n",
                );
            }
            let provider_before = fs::read_to_string(paths.runtime_providers_path()).unwrap();
            let catalog_before = fs::read_to_string(paths.generated_catalog_path()).unwrap();

            let error = store
                .recover_pending()
                .expect_err("unknown prepared-journal divergence must fail closed");

            assert!(error.contains("prepared"));
            assert_eq!(
                fs::read_to_string(paths.runtime_providers_path()).unwrap(),
                provider_before
            );
            assert_eq!(
                fs::read_to_string(paths.generated_catalog_path()).unwrap(),
                catalog_before
            );
            assert!(paths.provider_catalog_recovery_path().exists());
            let _ = fs::remove_dir_all(root);
        }
    }

    #[test]
    fn transaction_rejects_hardlinked_source_backup_journal_and_destination_paths() {
        for attacked in ["providers", "catalog", "provider-backup", "journal"] {
            let root = temp_root(&format!("hardlink-{attacked}"));
            let paths = isolated_paths(&root);
            fs::create_dir_all(paths.runtime_providers_path().parent().unwrap()).unwrap();
            fs::create_dir_all(paths.generated_catalog_path().parent().unwrap()).unwrap();
            let peer = root.join("outside-peer");
            write_fixture(&peer, "outside-content");
            let attacked_path = match attacked {
                "providers" => paths.runtime_providers_path(),
                "catalog" => paths.generated_catalog_path(),
                "provider-backup" => paths.provider_catalog_providers_backup_path(),
                _ => paths.provider_catalog_recovery_path(),
            };
            fs::hard_link(&peer, &attacked_path).unwrap();
            let before = fs::read_to_string(&peer).unwrap();
            let mut store = RuntimeProviderCatalogStore::new(paths.clone());

            let error = store
                .prepare_recovery()
                .expect_err("hardlinked transaction path must fail closed");

            assert!(error.contains("single-link"));
            assert_eq!(fs::read_to_string(peer).unwrap(), before);
            let _ = fs::remove_dir_all(root);
        }
    }

    #[cfg(windows)]
    #[test]
    fn transaction_rejects_junction_parent_without_touching_its_target() {
        let root = temp_root("junction-parent");
        let paths = isolated_paths(&root);
        fs::create_dir_all(paths.runtime_root()).unwrap();
        let victim = root.join("victim");
        fs::create_dir_all(&victim).unwrap();
        let sentinel = victim.join("sentinel");
        fs::write(&sentinel, "untouched").unwrap();
        let proxy = paths.proxy_dir();
        let status = std::process::Command::new("cmd")
            .args([
                "/C",
                "mklink",
                "/J",
                &proxy.to_string_lossy(),
                &victim.to_string_lossy(),
            ])
            .status()
            .unwrap();
        assert!(status.success(), "test requires a real directory junction");
        let mut store = RuntimeProviderCatalogStore::new(paths);

        let error = store
            .prepare_recovery()
            .expect_err("junction parent must fail closed");

        assert!(error.contains("escapes trusted boundary") || error.contains("reparse"));
        assert_eq!(fs::read_to_string(sentinel).unwrap(), "untouched");
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn real_startup_matrix_blocks_reads_writes_refresh_and_bridge_until_recovery_succeeds() {
        for auto_start in [false, true] {
            for journal in ["valid", "malformed", "divergent"] {
                let root = temp_root(&format!("startup-{auto_start}-{journal}"));
                let paths = isolated_paths(&root);
                config::save_providers_with_paths(
                    vec![provider(UpstreamFormat::Responses)],
                    &paths,
                )
                .unwrap();
                write_fixture(&paths.generated_catalog_path(), "{\"models\":[]}\n");
                let mut store = RuntimeProviderCatalogStore::new(paths.clone());
                match journal {
                    "valid" => {
                        store.prepare_recovery().unwrap();
                    }
                    "malformed" => write_fixture(
                        &paths.provider_catalog_recovery_path(),
                        "{\"schema_version\":4,\"secret\":\"must-not-appear\"}\n",
                    ),
                    "divergent" => {
                        store.prepare_recovery().unwrap();
                        write_fixture(
                            &paths.generated_catalog_path(),
                            "{\"models\":[{\"id\":\"external\"}]}\n",
                        );
                    }
                    _ => {}
                }
                drop(store);
                let repo_root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                    .parent()
                    .unwrap()
                    .to_path_buf();
                let output = std::process::Command::new(std::env::current_exe().unwrap())
                    .args([
                        "--exact",
                        "provider_catalog_transaction::tests::startup_process_fixture",
                        "--ignored",
                        "--nocapture",
                    ])
                    .env("CODEXHUB_STARTUP_FIXTURE", "1")
                    .env(
                        "CODEXHUB_STARTUP_EXPECT",
                        if journal == "valid" {
                            "success"
                        } else {
                            "failure"
                        },
                    )
                    .env(
                        "CODEXHUB_STARTUP_AUTO_GATEWAY",
                        if auto_start { "1" } else { "0" },
                    )
                    .env("CODEXHUB_RUNTIME_HOME", paths.runtime_root())
                    .env("CODEX_HOME", paths.codex_dir())
                    .env("CODEXHUB_RESOURCE_ROOT", repo_root)
                    .output()
                    .expect("startup fixture process should launch");
                assert!(
                    output.status.success(),
                    "startup fixture failed for auto_start={auto_start}, journal={journal}: {}",
                    String::from_utf8_lossy(&output.stderr)
                );
                if journal == "valid" {
                    assert!(!paths.provider_catalog_recovery_path().exists());
                } else {
                    assert!(paths.provider_catalog_recovery_path().exists());
                    assert_eq!(
                        fs::read_to_string(paths.generated_catalog_path()).unwrap(),
                        "{\"models\":[]}\n"
                    );
                }
                let _ = fs::remove_dir_all(root);
            }
        }
    }

    #[test]
    #[ignore = "launched as a subprocess by the startup matrix"]
    fn startup_process_fixture() {
        if std::env::var("CODEXHUB_STARTUP_FIXTURE").as_deref() != Ok("1") {
            return;
        }
        let expected_success =
            std::env::var("CODEXHUB_STARTUP_EXPECT").as_deref() == Ok("success");
        let auto_start =
            std::env::var("CODEXHUB_STARTUP_AUTO_GATEWAY").as_deref() == Ok("1");
        let recovered = super::initialize_startup_recovery();
        assert_eq!(
            recovered.is_ok(),
            expected_success,
            "startup recovery result contradicted the fixture expectation"
        );

        if expected_success {
            super::get_provider_catalog_snapshot().expect("provider snapshot after recovery");
            crate::web_bridge::dispatch_startup_recovery_probe()
                .expect("bridge dispatch after recovery");
            let started = std::cell::Cell::new(false);
            let planned = crate::start_gateway_after_startup(auto_start, || {
                super::require_startup_recovery()?;
                started.set(true);
                Ok(crate::AppStatus::scaffold("startup fixture"))
            })
            .expect("gateway plan after recovery");
            assert_eq!(planned, auto_start);
            assert_eq!(started.get(), auto_start);
        } else {
            assert!(config::get_providers().is_err());
            assert!(super::get_provider_catalog_snapshot().is_err());
            assert!(crate::catalog::sync_catalog().is_err());
            assert!(crate::official_refresh::refresh_manual().is_err());
            assert!(crate::web_bridge::dispatch_startup_recovery_probe().is_err());
            assert!(crate::web_bridge::dispatch_startup_recovery_route_probe().is_err());
        }
    }

    #[test]
    fn runtime_recovery_preserves_files_after_commit_marker() {
        let root = temp_root("preserve-commit");
        let paths = isolated_paths(&root);
        let providers_path = paths.runtime_providers_path();
        let catalog_path = paths.generated_catalog_path();
        config::save_providers_with_paths(vec![provider(UpstreamFormat::Responses)], &paths)
            .unwrap();
        write_fixture(&catalog_path, "{\"models\":[{\"id\":\"previous\"}]}\n");
        let mut store = RuntimeProviderCatalogStore::new(paths.clone());

        store.prepare_recovery().expect("prepare recovery journal");
        let candidate = provider(UpstreamFormat::ChatCompletions);
        store
            .mark_provider_write_pending(std::slice::from_ref(&candidate))
            .unwrap();
        store.save_providers(vec![candidate]).unwrap();
        let committed_catalog = "{\"models\":[{\"id\":\"committed\"}]}\n";
        store
            .mark_catalog_write_pending(committed_catalog)
            .unwrap();
        let committed_providers = fs::read_to_string(&providers_path).unwrap();
        write_fixture(&catalog_path, committed_catalog);
        store.mark_committed().expect("mark recovery committed");
        store.recover_pending().expect("clean committed journal");

        assert_eq!(
            fs::read_to_string(&providers_path).expect("read committed providers"),
            committed_providers
        );
        assert_eq!(
            fs::read_to_string(&catalog_path).expect("read committed catalog"),
            committed_catalog
        );
        assert!(!paths.provider_catalog_recovery_path().exists());
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn committed_recovery_record_rejects_tampered_catalog_and_stays_pending() {
        let root = temp_root("tampered-commit");
        let paths = isolated_paths(&root);
        let catalog_path = paths.generated_catalog_path();
        config::save_providers_with_paths(vec![provider(UpstreamFormat::Responses)], &paths)
            .unwrap();
        write_fixture(&catalog_path, "{\"models\":[{\"id\":\"previous\"}]}\n");
        let mut store = RuntimeProviderCatalogStore::new(paths.clone());
        store.prepare_recovery().expect("prepare recovery journal");
        let candidate = provider(UpstreamFormat::ChatCompletions);
        store
            .mark_provider_write_pending(std::slice::from_ref(&candidate))
            .unwrap();
        store.save_providers(vec![candidate]).unwrap();
        let committed_catalog = "{\"models\":[{\"id\":\"committed\"}]}\n";
        store
            .mark_catalog_write_pending(committed_catalog)
            .unwrap();
        write_fixture(&catalog_path, committed_catalog);
        store.mark_committed().expect("mark recovery committed");
        write_fixture(&catalog_path, "{\"models\":[{\"id\":\"tampered\"}]}\n");
        drop(store);

        let mut restarted_store = RuntimeProviderCatalogStore::new(paths.clone());
        let error = restarted_store
            .recover_pending()
            .expect_err("tampered commit must fail closed");

        assert!(error.contains("committed generated catalog identity"));
        assert!(paths.provider_catalog_recovery_path().exists());
        assert!(paths.provider_catalog_catalog_backup_path().exists());
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn future_recovery_schema_is_rejected_without_exposing_snapshot_contents() {
        let root = temp_root("future-schema");
        let paths = isolated_paths(&root);
        write_fixture(
            &paths.provider_catalog_recovery_path(),
            r#"{"schema_version":999,"state":"prepared","created_at_unix_seconds":1,"providers":{"existed":false,"sha256":null},"catalog":{"existed":false,"sha256":null},"committed_providers_sha256":null,"committed_catalog_sha256":null}"#,
        );
        let store = RuntimeProviderCatalogStore::new(paths.clone());

        let error = store
            .read_recovery()
            .expect_err("future recovery schema must fail closed");

        assert!(error.contains("unsupported provider/catalog recovery schema version 999"));
        assert!(paths.provider_catalog_recovery_path().exists());
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn malformed_recovery_record_is_rejected_and_retained_for_fail_closed_handling() {
        let root = temp_root("malformed-record");
        let paths = isolated_paths(&root);
        write_fixture(
            &paths.provider_catalog_recovery_path(),
            r#"{"schema_version":4,"state":"prepared","unexpected":"field"}"#,
        );
        let store = RuntimeProviderCatalogStore::new(paths.clone());

        let error = store
            .read_recovery()
            .expect_err("malformed recovery record must fail closed");

        assert!(error.contains("failed to parse provider/catalog recovery record"));
        assert!(paths.provider_catalog_recovery_path().exists());
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn catalog_generation_failure_restores_provider_and_catalog_snapshots() {
        let previous_provider = provider(UpstreamFormat::Responses);
        let previous_catalog = vec![catalog_model(UpstreamFormat::Responses)];
        let mut store = MemoryStore::new(
            vec![previous_provider.clone()],
            previous_catalog.clone(),
            [Err("injected catalog generation failure".to_string())],
        );

        let result =
            persist_with_store(&mut store, vec![provider(UpstreamFormat::ChatCompletions)]);

        assert_eq!(
            result.outcome,
            ProviderCatalogTransactionOutcome::RolledBack
        );
        assert_eq!(
            store.providers[0].upstream_format,
            previous_provider.upstream_format
        );
        assert_eq!(
            store.catalog[0]
                .capability_binding
                .as_ref()
                .map(|binding| &binding.upstream_protocol),
            previous_catalog[0]
                .capability_binding
                .as_ref()
                .map(|binding| &binding.upstream_protocol)
        );
        assert!(result
            .detail
            .as_deref()
            .is_some_and(|detail| detail.contains("generation failure")));
        assert!(store.pending.is_none());
    }

    #[test]
    fn mismatched_catalog_binding_rolls_back_both_persistent_surfaces() {
        let previous_provider = provider(UpstreamFormat::Responses);
        let previous_catalog = vec![catalog_model(UpstreamFormat::Responses)];
        let mut store = MemoryStore::new(
            vec![previous_provider.clone()],
            previous_catalog,
            [Ok(vec![catalog_model(UpstreamFormat::Responses)])],
        );

        let result =
            persist_with_store(&mut store, vec![provider(UpstreamFormat::ChatCompletions)]);

        assert_eq!(
            result.outcome,
            ProviderCatalogTransactionOutcome::RolledBack
        );
        assert_eq!(
            store.providers[0].upstream_format,
            previous_provider.upstream_format
        );
        assert_eq!(
            store.catalog[0]
                .capability_binding
                .as_ref()
                .map(|binding| &binding.upstream_protocol),
            Some(&UpstreamFormat::Responses)
        );
        assert!(result
            .detail
            .as_deref()
            .is_some_and(|detail| detail.contains("capability contract")));
    }

    #[test]
    fn catalog_readback_rejects_every_incomplete_or_conflicting_capability_contract() {
        let providers = vec![provider(UpstreamFormat::Responses)];
        let valid = catalog_model(UpstreamFormat::Responses);
        verify_catalog_for_providers(std::slice::from_ref(&valid), &providers).unwrap();

        let mut cases = Vec::new();
        let mut future_schema = valid.clone();
        future_schema
            .capability_binding
            .as_mut()
            .unwrap()
            .schema_version = 2;
        cases.push(vec![future_schema]);

        let mut stale_manifest = valid.clone();
        stale_manifest
            .capability_binding
            .as_mut()
            .unwrap()
            .capability_manifest_hash = Some(format!("sha256:{}", "0".repeat(64)));
        cases.push(vec![stale_manifest]);

        let mut qualification_mismatch = valid.clone();
        let binding = qualification_mismatch.capability_binding.as_mut().unwrap();
        binding.qualification_state = QualificationState::Supported;
        binding.advanced_capabilities_enabled = false;
        binding.rejection_reason = None;
        cases.push(vec![qualification_mismatch]);

        let mut missing_route_field = valid.clone();
        missing_route_field
            .capability_binding
            .as_mut()
            .unwrap()
            .tool_profile = None;
        cases.push(vec![missing_route_field]);

        cases.push(vec![valid.clone(), valid]);

        for models in cases {
            let error = verify_catalog_for_providers(&models, &providers)
                .expect_err("incomplete capability contract must fail closed");
            assert!(error.contains("capability contract") || error.contains("exactly one binding"));
        }

        let mut stale_profile = providers.clone();
        stale_profile[0].models[0].capability_profiles[0].capability_manifest_hash =
            format!("sha256:{}", "0".repeat(64));
        assert!(verify_catalog_for_providers(
            std::slice::from_ref(&catalog_model(UpstreamFormat::Responses)),
            &stale_profile,
        )
        .unwrap_err()
        .contains("stale capability manifest hash"));

        let mut missing_profile_route = providers;
        missing_profile_route[0].models[0].capability_profiles[0]
            .tool_profile
            .clear();
        assert!(verify_catalog_for_providers(
            std::slice::from_ref(&catalog_model(UpstreamFormat::Responses)),
            &missing_profile_route,
        )
        .unwrap_err()
        .contains("missing route fields"));
    }

    #[test]
    fn rust_capability_binding_matches_the_shared_python_schema_vectors() {
        let fixture: serde_json::Value = serde_json::from_str(include_str!(
            "../../tests/fixtures/capability_binding_parity.json"
        ))
        .unwrap();
        let schema = fixture["schema"].as_object().unwrap();
        assert_eq!(schema["version"], 1);
        let required = schema["required_fields"]
            .as_array()
            .unwrap()
            .iter()
            .filter_map(serde_json::Value::as_str)
            .collect::<HashSet<_>>();
        let optional = schema["optional_fields"]
            .as_array()
            .unwrap()
            .iter()
            .filter_map(serde_json::Value::as_str)
            .collect::<HashSet<_>>();
        let allowed = required
            .union(&optional)
            .copied()
            .collect::<HashSet<_>>();

        for vector in fixture["vectors"].as_array().unwrap() {
            let name = vector["name"].as_str().unwrap();
            let profiles = serde_json::from_value::<Vec<CapabilityProfile>>(
                vector["profiles"].clone(),
            )
            .unwrap();
            let protocol = serde_json::from_value::<UpstreamFormat>(
                vector["upstream_protocol"].clone(),
            )
            .unwrap();
            let binding = expected_capability_binding(
                vector["provider"].as_str().unwrap(),
                vector["model"].as_str().unwrap(),
                &protocol,
                &profiles,
            )
            .unwrap_or_else(|error| panic!("{name}: {error}"));
            let actual = serde_json::to_value(binding).unwrap();

            assert_eq!(actual, vector["expected"], "parity vector {name}");
            let fields = actual.as_object().unwrap().keys().map(String::as_str);
            let fields = fields.collect::<HashSet<_>>();
            assert!(
                required.is_subset(&fields),
                "{name} omitted a required binding field"
            );
            assert!(
                fields.is_subset(&allowed),
                "{name} emitted a field outside the shared schema"
            );
        }
    }

    #[test]
    fn real_discovery_persists_reviewed_profiles_through_python_stage_and_rust_readback() {
        let root = temp_root("real-discovery-profile-round-trip");
        let repo_root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .expect("src-tauri has a repository parent")
            .to_path_buf();
        let paths = config::ConfigPaths::new_isolated(
            root.join("runtime"),
            root.join("codex"),
            repo_root,
        );

        let listener = TcpListener::bind("127.0.0.1:0").expect("bind discovery server");
        let address = listener.local_addr().expect("read discovery address");
        let server = thread::spawn(move || {
            let (mut stream, _) = listener.accept().expect("accept discovery request");
            let mut request = [0_u8; 4096];
            let bytes = stream.read(&mut request).expect("read discovery request");
            assert!(
                String::from_utf8_lossy(&request[..bytes]).starts_with("GET /v1/models "),
                "discovery must call the provider models endpoint"
            );
            let body = r#"{"data":[{"id":"glm-5.2","owned_by":"discovery"}]}"#;
            let response = format!(
                "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                body.len()
            );
            stream
                .write_all(response.as_bytes())
                .expect("write discovery response");
        });
        let base_url = format!("http://{address}");

        let seeded = provider(UpstreamFormat::Responses);
        config::save_providers_with_paths(vec![seeded], &paths)
            .expect("seed reviewed provider");
        let previous =
            config::get_providers_with_paths(&paths).expect("read resolved reviewed provider");
        let previous = previous
            .into_iter()
            .next()
            .expect("seeded provider is present");
        let reviewed_profiles = previous.models[0].capability_profiles.clone();
        let revision = issue_provider_catalog_revision(&paths).expect("issue catalog revision");

        let mut discovered =
            crate::models::discover_provider_models(&base_url, "").expect("discover models");
        server.join().expect("join discovery server");
        assert_eq!(discovered.len(), 1);
        assert!(discovered[0].capability_profiles.is_empty());
        assert!(discovered[0].capability_binding.is_none());

        let mut merged = discovered.remove(0);
        merged.upstream_model = previous.models[0].upstream_model.clone();
        merged.capability_profiles = reviewed_profiles.clone();
        merged.capability_binding = previous.models[0].capability_binding.clone();
        merged.enabled = previous.models[0].enabled;
        merged.gateway_exported = previous.models[0].gateway_exported;
        let mut candidate = previous;
        candidate.base_url = base_url;
        candidate.models = vec![merged];

        let result =
            persist_provider_catalog_state_with_paths(&paths, vec![candidate], revision)
                .expect("persist discovered provider through the real catalog transaction");
        assert_eq!(
            result.outcome,
            ProviderCatalogTransactionOutcome::Committed,
            "{:?}",
            result.detail
        );

        let persisted =
            config::get_providers_with_paths(&paths).expect("read persisted provider config");
        assert_eq!(persisted[0].models[0].capability_profiles, reviewed_profiles);
        let catalog =
            crate::models::read_catalog_models(&paths.generated_catalog_path())
                .expect("Rust reads the Python-staged catalog");
        let readback = catalog
            .iter()
            .find(|model| {
                model.capability_binding.as_ref().is_some_and(|binding| {
                    binding.provider == "ollama-cloud" && binding.model == "glm-5.2"
                })
            })
            .expect("persisted discovered model is exported");
        let binding = readback
            .capability_binding
            .as_ref()
            .expect("reviewed profile produces a capability binding");
        assert_eq!(
            binding.capability_manifest_hash.as_deref(),
            Some(reviewed_profiles[0].capability_manifest_hash.as_str())
        );
        assert_eq!(
            binding.rejection_reason.as_deref(),
            Some("qualification_state_unqualified")
        );
        assert!(!paths.provider_catalog_recovery_path().exists());

        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn recovery_phase_classifier_has_one_typed_forward_transition_graph() {
        let transitions = [
            (
                RecoveryTransition::FinishPreparation,
                RecoveryState::Preparing,
                RecoveryState::Prepared,
            ),
            (
                RecoveryTransition::BeginProviderPublish,
                RecoveryState::Prepared,
                RecoveryState::ProviderWritePending,
            ),
            (
                RecoveryTransition::BeginCatalogPublish,
                RecoveryState::ProviderWritePending,
                RecoveryState::CatalogWritePending,
            ),
            (
                RecoveryTransition::Commit,
                RecoveryState::CatalogWritePending,
                RecoveryState::Committed,
            ),
        ];
        for (transition, expected, next) in transitions {
            assert_eq!(transition.states(), (expected, next));
        }

        let phases = [
            (
                RecoveryState::Preparing,
                RecoveryAction::ClearPrepared,
                RecoveryRecordShape::Prepared,
            ),
            (
                RecoveryState::Prepared,
                RecoveryAction::ClearPrepared,
                RecoveryRecordShape::Prepared,
            ),
            (
                RecoveryState::ProviderWritePending,
                RecoveryAction::RestoreProviderPrefix,
                RecoveryRecordShape::ProviderCandidate,
            ),
            (
                RecoveryState::CatalogWritePending,
                RecoveryAction::RestoreCatalogPrefix,
                RecoveryRecordShape::CatalogCandidate,
            ),
            (
                RecoveryState::Committed,
                RecoveryAction::VerifyCommitted,
                RecoveryRecordShape::Committed,
            ),
            (
                RecoveryState::CatalogDisabled,
                RecoveryAction::RegenerateDisabled,
                RecoveryRecordShape::Disabled,
            ),
        ];
        let mut marker_faults = HashSet::new();
        for (state, action, shape) in phases {
            let classified = state.classify();
            assert_eq!(classified.action, action);
            assert_eq!(classified.shape, shape);
            assert!(
                marker_faults.insert(classified.marker_fault),
                "every durable state must own a unique marker fault seam"
            );
        }
    }

    #[test]
    fn provider_publication_failure_restores_both_persistent_surfaces() {
        let previous_provider = provider(UpstreamFormat::Responses);
        let previous_catalog = vec![catalog_model(UpstreamFormat::Responses)];
        let mut store = MemoryStore::new(
            vec![previous_provider.clone()],
            previous_catalog.clone(),
            [],
        );
        store.fail_save = true;

        let result =
            persist_with_store(&mut store, vec![provider(UpstreamFormat::ChatCompletions)]);

        assert_eq!(
            result.outcome,
            ProviderCatalogTransactionOutcome::RolledBack
        );
        assert_eq!(
            serde_json::to_value(&store.providers).expect("serialize restored providers"),
            serde_json::to_value([previous_provider]).expect("serialize previous providers")
        );
        assert_eq!(
            serde_json::to_value(&store.catalog).expect("serialize restored catalog"),
            serde_json::to_value(&previous_catalog).expect("serialize previous catalog")
        );
        assert!(store.pending.is_none());
    }

    #[test]
    fn provider_publication_readback_mismatch_restores_both_persistent_surfaces() {
        let previous_provider = provider(UpstreamFormat::Responses);
        let previous_catalog = vec![catalog_model(UpstreamFormat::Responses)];
        let mut store = MemoryStore::new(
            vec![previous_provider.clone()],
            previous_catalog.clone(),
            [],
        );
        store.corrupt_save_readback = true;

        let result =
            persist_with_store(&mut store, vec![provider(UpstreamFormat::ChatCompletions)]);

        assert_eq!(
            result.outcome,
            ProviderCatalogTransactionOutcome::RolledBack
        );
        assert!(result
            .detail
            .as_deref()
            .is_some_and(|detail| detail.contains("readback did not match")));
        assert_eq!(
            serde_json::to_value(&store.providers).expect("serialize restored providers"),
            serde_json::to_value([previous_provider]).expect("serialize previous providers")
        );
        assert_eq!(
            serde_json::to_value(&store.catalog).expect("serialize restored catalog"),
            serde_json::to_value(&previous_catalog).expect("serialize previous catalog")
        );
    }

    #[test]
    fn catalog_rollback_failure_disables_catalog_until_idempotent_retry_recovers() {
        let mut store = MemoryStore::new(
            vec![provider(UpstreamFormat::Responses)],
            vec![catalog_model(UpstreamFormat::Responses)],
            [
                Err("injected generation failure".to_string()),
                Ok(vec![catalog_model(UpstreamFormat::Responses)]),
            ],
        );
        store.fail_catalog_restore = true;
        let candidate = vec![provider(UpstreamFormat::ChatCompletions)];

        let first = persist_with_store(&mut store, candidate);

        assert_eq!(
            first.outcome,
            ProviderCatalogTransactionOutcome::RecoveryRequired
        );
        assert!(first.catalog_disabled);
        assert!(store.pending.is_some());
        store.fail_catalog_restore = false;
        let ui_state_loaded_before_recovery = store.providers.clone();

        let retry = persist_with_store(&mut store, ui_state_loaded_before_recovery);

        assert_eq!(retry.outcome, ProviderCatalogTransactionOutcome::Committed);
        assert!(!retry.protocol_changed);
        assert!(store.pending.is_none());
        assert_eq!(
            store.catalog[0]
                .capability_binding
                .as_ref()
                .map(|binding| &binding.upstream_protocol),
            Some(&UpstreamFormat::Responses)
        );
    }

    #[test]
    fn rollback_failure_disables_catalog_and_retains_recovery_snapshot() {
        let mut store = MemoryStore::new(
            vec![provider(UpstreamFormat::Responses)],
            vec![catalog_model(UpstreamFormat::Responses)],
            [Err("injected generation failure".to_string())],
        );
        store.fail_restore = true;

        let result =
            persist_with_store(&mut store, vec![provider(UpstreamFormat::ChatCompletions)]);

        assert_eq!(
            result.outcome,
            ProviderCatalogTransactionOutcome::RecoveryRequired
        );
        assert!(result.catalog_disabled);
        assert!(store.catalog.is_empty());
        assert!(store.catalog_disabled);
        assert!(store.pending.is_some());
        assert!(result
            .detail
            .as_deref()
            .is_some_and(|detail| detail.contains("automatic rollback failed")));
    }

    #[test]
    fn gateway_start_recovery_failure_disables_catalog_and_refuses_start() {
        let mut store = MemoryStore::new(
            vec![provider(UpstreamFormat::Responses)],
            vec![catalog_model(UpstreamFormat::Responses)],
            [],
        );
        store.prepare_recovery().expect("prepare restart recovery");
        store.providers = vec![provider(UpstreamFormat::ChatCompletions)];
        store.catalog = vec![catalog_model(UpstreamFormat::ChatCompletions)];
        store.fail_restore = true;

        let error = recover_before_gateway_with_store(&mut store)
            .expect_err("Gateway start must fail when recovery cannot be proved");

        assert!(error.contains("generated catalog disabled fail-closed"));
        assert!(store.catalog_disabled);
        assert!(store.catalog.is_empty());
        assert!(store.pending.is_some());
    }

    #[test]
    fn rollback_and_catalog_invalidation_failure_are_reported_explicitly() {
        let mut store = MemoryStore::new(
            vec![provider(UpstreamFormat::Responses)],
            vec![catalog_model(UpstreamFormat::Responses)],
            [Err("injected generation failure".to_string())],
        );
        store.fail_restore = true;
        store.fail_invalidate = true;

        let result =
            persist_with_store(&mut store, vec![provider(UpstreamFormat::ChatCompletions)]);

        assert_eq!(
            result.outcome,
            ProviderCatalogTransactionOutcome::RecoveryRequired
        );
        assert!(!result.catalog_disabled);
        assert!(store.pending.is_some());
        assert!(result.detail.as_deref().is_some_and(|detail| {
            detail.contains("automatic rollback failed")
                && detail.contains("catalog invalidation also failed")
        }));
    }

    #[test]
    fn retry_recovers_pending_snapshot_before_attempting_switch_again() {
        let mut store = MemoryStore::new(
            vec![provider(UpstreamFormat::Responses)],
            vec![catalog_model(UpstreamFormat::Responses)],
            [
                Err("injected first generation failure".to_string()),
                Ok(vec![catalog_model(UpstreamFormat::ChatCompletions)]),
            ],
        );
        store.fail_restore = true;
        let candidate = vec![provider(UpstreamFormat::ChatCompletions)];
        let first = persist_with_store(&mut store, candidate);
        assert_eq!(
            first.outcome,
            ProviderCatalogTransactionOutcome::RecoveryRequired
        );

        store.fail_restore = false;
        let ui_state_loaded_before_recovery = store.providers.clone();
        assert_eq!(
            ui_state_loaded_before_recovery[0].upstream_format,
            Some(UpstreamFormat::ChatCompletions)
        );
        assert_eq!(
            store
                .pending
                .as_ref()
                .and_then(|(providers, _)| providers[0].upstream_format.as_ref()),
            Some(&UpstreamFormat::Responses)
        );
        let retry = persist_with_store(&mut store, ui_state_loaded_before_recovery);

        assert_eq!(retry.outcome, ProviderCatalogTransactionOutcome::Committed);
        assert!(retry.protocol_changed);
        assert!(store.pending.is_none());
        assert_eq!(
            store.providers[0].upstream_format,
            Some(UpstreamFormat::ChatCompletions)
        );
        assert_eq!(
            store.catalog[0]
                .capability_binding
                .as_ref()
                .map(|binding| &binding.upstream_protocol),
            Some(&UpstreamFormat::ChatCompletions)
        );
    }

    #[test]
    fn invalid_existing_catalog_leaves_durable_recovery_and_retry_regenerates_before_commit() {
        let previous_provider = provider(UpstreamFormat::Responses);
        let previous_catalog = vec![catalog_model(UpstreamFormat::ChatCompletions)];
        let mut store = MemoryStore::new(
            vec![previous_provider],
            previous_catalog,
            [
                Err("injected recovery generation failure".to_string()),
                Ok(vec![catalog_model(UpstreamFormat::Responses)]),
                Ok(vec![catalog_model(UpstreamFormat::ChatCompletions)]),
            ],
        );
        let candidate = vec![provider(UpstreamFormat::ChatCompletions)];

        let first = persist_with_store(&mut store, candidate.clone());

        assert_eq!(
            first.outcome,
            ProviderCatalogTransactionOutcome::RecoveryRequired
        );
        assert!(first.catalog_disabled);
        assert!(store.recovery_required);

        let retry = persist_with_store(&mut store, candidate);

        assert_eq!(retry.outcome, ProviderCatalogTransactionOutcome::Committed);
        assert!(!store.recovery_required);
        assert_eq!(
            store.catalog[0]
                .capability_binding
                .as_ref()
                .map(|binding| &binding.upstream_protocol),
            Some(&UpstreamFormat::ChatCompletions)
        );
    }

    struct MemoryStore {
        providers: Vec<Provider>,
        catalog: Vec<Model>,
        pending: Option<(Vec<Provider>, Vec<Model>)>,
        generated: VecDeque<Result<Vec<Model>, String>>,
        fail_restore: bool,
        fail_catalog_restore: bool,
        fail_save: bool,
        corrupt_save_readback: bool,
        fail_invalidate: bool,
        catalog_disabled: bool,
        recovery_required: bool,
    }

    impl MemoryStore {
        fn new(
            providers: Vec<Provider>,
            catalog: Vec<Model>,
            generated: impl IntoIterator<Item = Result<Vec<Model>, String>>,
        ) -> Self {
            Self {
                providers,
                catalog,
                pending: None,
                generated: generated.into_iter().collect(),
                fail_restore: false,
                fail_catalog_restore: false,
                fail_save: false,
                corrupt_save_readback: false,
                fail_invalidate: false,
                catalog_disabled: false,
                recovery_required: false,
            }
        }
    }

    impl ProviderCatalogStore for MemoryStore {
        fn recover_pending(&mut self) -> Result<(), String> {
            if self.pending.is_some() {
                self.restore_pending()?;
            }
            if self.recovery_required {
                let providers = self.providers.clone();
                let models = self.generate_catalog()?;
                verify_catalog_for_providers(&models, &providers)?;
                self.recovery_required = false;
            }
            Ok(())
        }

        fn current_providers(&self) -> Result<Vec<Provider>, String> {
            Ok(self.providers.clone())
        }

        fn current_catalog(&self) -> Result<Vec<Model>, String> {
            Ok(self.catalog.clone())
        }

        fn generate_catalog(&mut self) -> Result<Vec<Model>, String> {
            let generated = self
                .generated
                .pop_front()
                .unwrap_or_else(|| Err("no generated catalog result".to_string()))?;
            self.catalog = generated.clone();
            Ok(generated)
        }

        fn prepare_recovery(&mut self) -> Result<(), String> {
            self.pending = Some((self.providers.clone(), self.catalog.clone()));
            Ok(())
        }

        fn mark_provider_write_pending(&mut self, _providers: &[Provider]) -> Result<(), String> {
            Ok(())
        }

        fn save_providers(&mut self, providers: Vec<Provider>) -> Result<Vec<Provider>, String> {
            if self.fail_save {
                return Err("injected provider publication failure".to_string());
            }
            self.providers = providers.clone();
            if self.corrupt_save_readback {
                self.providers[0].name = "corrupt persisted provider".to_string();
            }
            Ok(self.providers.clone())
        }

        fn restore_pending(&mut self) -> Result<(), String> {
            if self.fail_restore {
                return Err("injected rollback failure".to_string());
            }
            let Some((providers, catalog)) = self.pending.clone() else {
                return Ok(());
            };
            self.providers = providers;
            if self.fail_catalog_restore {
                return Err("injected catalog rollback failure".to_string());
            }
            self.catalog = catalog;
            self.pending = None;
            Ok(())
        }

        fn mark_committed(&mut self) -> Result<(), String> {
            Ok(())
        }

        fn ensure_recovery_required(&mut self) -> Result<(), String> {
            if self.pending.is_none() {
                self.recovery_required = true;
            }
            Ok(())
        }

        fn clear_recovery(&mut self) -> Result<(), String> {
            self.pending = None;
            Ok(())
        }

        fn invalidate_catalog(&mut self) -> Result<(), String> {
            if self.fail_invalidate {
                return Err("injected catalog invalidation failure".to_string());
            }
            self.catalog.clear();
            self.catalog_disabled = true;
            Ok(())
        }
    }

    fn provider(upstream_format: UpstreamFormat) -> Provider {
        let mut profile = CapabilityProfile {
            schema_version: 1,
            upstream_protocol: upstream_format.clone(),
            tool_profile: "test-tools".to_string(),
            collaboration_backend: "none".to_string(),
            collaboration_version: "none".to_string(),
            capability_manifest_version: "test-manifest".to_string(),
            capability_manifest_hash: String::new(),
            qualification_state: QualificationState::Unqualified,
        };
        profile.capability_manifest_hash =
            super::capability_profile_manifest_hash("ollama-cloud", "glm-5.2", &profile).unwrap();
        Provider {
            id: "ollama-cloud".to_string(),
            name: "Ollama Cloud".to_string(),
            base_url: "https://ollama.example.test/v1".to_string(),
            api_key: None,
            upstream_format: Some(upstream_format),
            available_upstream_formats: None,
            tool_protocol: None,
            tool_surface_strategy: None,
            reports_cached_input_tokens: None,
            supports_developer_role: None,
            display_prefix: Some("Ollama".to_string()),
            sort_order: Some(1),
            enabled: true,
            locked: false,
            models: vec![Model {
                id: "glm-5.2".to_string(),
                upstream_model: Some("upstream-alias".to_string()),
                capability_profiles: vec![profile],
                enabled: true,
                gateway_exported: true,
                ..Model::default()
            }],
        }
    }

    fn catalog_model(upstream_protocol: UpstreamFormat) -> Model {
        let provider = provider(upstream_protocol.clone());
        let profile = &provider.models[0].capability_profiles[0];
        Model {
            id: "glm-5.2".to_string(),
            capability_binding: Some(CapabilityBinding {
                schema_version: 1,
                provider: "ollama-cloud".to_string(),
                model: "glm-5.2".to_string(),
                upstream_protocol,
                tool_profile: Some("test-tools".to_string()),
                collaboration_backend: Some("none".to_string()),
                collaboration_version: Some("none".to_string()),
                capability_manifest_version: Some("test-manifest".to_string()),
                capability_manifest_hash: Some(profile.capability_manifest_hash.clone()),
                qualification_state: QualificationState::Unqualified,
                advanced_capabilities_enabled: false,
                rejection_reason: Some("qualification_state_unqualified".to_string()),
            }),
            ..Model::default()
        }
    }

    fn capability_catalog_text(
        upstream_protocol: UpstreamFormat,
        alternate_formatting: bool,
    ) -> String {
        let provider = provider(upstream_protocol.clone());
        let profile = &provider.models[0].capability_profiles[0];
        let value = serde_json::json!({
            "models": [{
                "slug": "glm-5.2",
                "codex_proxy_metadata": {
                    "capability_binding": {
                        "schema_version": 1,
                        "provider": "ollama-cloud",
                        "model": "glm-5.2",
                        "upstream_protocol": upstream_protocol,
                        "tool_profile": "test-tools",
                        "collaboration_backend": "none",
                        "collaboration_version": "none",
                        "capability_manifest_version": "test-manifest",
                        "capability_manifest_hash": profile.capability_manifest_hash,
                        "qualification_state": "unqualified",
                        "advanced_capabilities_enabled": false,
                        "rejection_reason": "qualification_state_unqualified"
                    }
                }
            }]
        });
        if alternate_formatting {
            format!("{}\n", serde_json::to_string_pretty(&value).unwrap())
        } else {
            format!("{}\n", serde_json::to_string(&value).unwrap())
        }
    }

    fn isolated_paths(root: &Path) -> config::ConfigPaths {
        config::ConfigPaths::new_isolated(
            root.join("runtime"),
            root.join("codex"),
            root.join("repo"),
        )
    }

    fn temp_root(label: &str) -> PathBuf {
        let unique = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("clock after epoch")
            .as_nanos();
        std::env::temp_dir().join(format!(
            "codexhub-provider-catalog-{label}-{}-{unique}",
            std::process::id()
        ))
    }

    fn write_fixture(path: &Path, contents: &str) {
        fs::create_dir_all(path.parent().expect("fixture parent")).expect("create fixture parent");
        fs::write(path, contents).expect("write fixture");
    }
}
