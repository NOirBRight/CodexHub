use crate::{
    config, models, safe_file, CapabilityBinding, CapabilityProfile, Model, Provider,
    QualificationState, UpstreamFormat,
};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU8, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

const RECOVERY_SCHEMA_VERSION: u32 = 3;
const MAX_RECOVERY_RECORD_BYTES: u64 = 64 * 1024;
const MAX_PROVIDER_SNAPSHOT_BYTES: u64 = 8 * 1024 * 1024;
const MAX_CATALOG_SNAPSHOT_BYTES: u64 = 64 * 1024 * 1024;
const MAX_FUTURE_CLOCK_SKEW_SECONDS: u64 = 5 * 60;
const STARTUP_UNCHECKED: u8 = 0;
const STARTUP_READY: u8 = 1;
const STARTUP_BLOCKED: u8 = 2;
#[cfg(not(test))]
static STARTUP_RECOVERY_STATE: AtomicU8 = AtomicU8::new(STARTUP_UNCHECKED);
#[cfg(test)]
static STARTUP_RECOVERY_STATE: AtomicU8 = AtomicU8::new(STARTUP_READY);

#[cfg(test)]
thread_local! {
    static TRANSACTION_FAULT_PHASE: std::cell::RefCell<Option<&'static str>> =
        const { std::cell::RefCell::new(None) };
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
    pub protocol_changed: bool,
    pub detail: Option<String>,
    pub catalog_disabled: bool,
}

trait ProviderCatalogStore {
    fn recover_pending(&mut self) -> Result<(), String>;
    fn current_providers(&self) -> Result<Vec<Provider>, String>;
    fn current_catalog(&self) -> Result<Vec<Model>, String>;
    fn generate_catalog(&mut self) -> Result<Vec<Model>, String>;
    fn prepare_recovery(&mut self) -> Result<(), String>;
    fn mark_provider_write_pending(&mut self, providers: &[Provider]) -> Result<(), String>;
    fn mark_catalog_write_pending(&mut self) -> Result<(), String>;
    fn save_providers(&mut self, providers: Vec<Provider>) -> Result<Vec<Provider>, String>;
    fn restore_pending(&mut self) -> Result<(), String>;
    fn mark_committed(&mut self) -> Result<(), String>;
    fn ensure_recovery_required(&mut self) -> Result<(), String>;
    fn clear_recovery(&mut self) -> Result<(), String>;
    fn invalidate_catalog(&mut self) -> Result<(), String>;
}

pub fn persist_provider_catalog_state(
    providers: Vec<Provider>,
    expected_providers: Vec<Provider>,
) -> Result<ProviderCatalogTransactionResult, String> {
    let paths = config::ConfigPaths::runtime()?;
    with_transaction_guard_for_paths(&paths, || {
        let mut store = RuntimeProviderCatalogStore::new(paths.clone());
        let mut result =
            persist_with_expected_store(&mut store, providers, Some(&expected_providers));
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
        Ok(result)
    })
}

pub fn initialize_startup_recovery() -> Result<(), String> {
    STARTUP_RECOVERY_STATE.store(STARTUP_BLOCKED, Ordering::Release);
    let paths = config::ConfigPaths::runtime()?;
    let result = with_transaction_guard_for_paths(&paths, || {
        let mut store = RuntimeProviderCatalogStore::new(paths.clone());
        recover_before_gateway_with_store(&mut store)
    });
    if result.is_ok() {
        STARTUP_RECOVERY_STATE.store(STARTUP_READY, Ordering::Release);
    }
    result
}

pub fn recover_before_gateway_start() -> Result<(), String> {
    initialize_startup_recovery()
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

fn with_transaction_guard_for_paths<T>(
    paths: &config::ConfigPaths,
    operation: impl FnOnce() -> Result<T, String>,
) -> Result<T, String> {
    let transaction_lock_path = paths.proxy_dir().join("provider-catalog-transaction-guard");
    let _transaction_lock = safe_file::FileLock::acquire(&transaction_lock_path)
        .map_err(|error| format!("failed to lock provider/catalog transaction: {error}"))?;
    operation()
}

pub fn save_providers(providers: Vec<Provider>) -> Result<Vec<Provider>, String> {
    with_transaction_guard(|| {
        let paths = config::ConfigPaths::runtime()?;
        config::save_providers_with_paths(providers, &paths)
    })
}

fn recover_before_gateway_with_store(store: &mut dyn ProviderCatalogStore) -> Result<(), String> {
    if let Err(recovery_error) = store.recover_pending() {
        return match store.invalidate_catalog() {
            Ok(()) => Err(format!(
                "provider/catalog recovery could not prove a consistent state ({recovery_error}); generated catalog disabled fail-closed"
            )),
            Err(invalidation_error) => Err(format!(
                "provider/catalog recovery could not prove a consistent state ({recovery_error}); fail-closed catalog invalidation also failed: {invalidation_error}"
            )),
        };
    }
    Ok(())
}

pub fn recovery_pending() -> Result<bool, String> {
    Ok(config::ConfigPaths::runtime()?
        .provider_catalog_recovery_path()
        .exists())
}

#[cfg(test)]
fn persist_with_store(
    store: &mut dyn ProviderCatalogStore,
    providers: Vec<Provider>,
) -> ProviderCatalogTransactionResult {
    persist_with_expected_store(store, providers, None)
}

fn persist_with_expected_store(
    store: &mut dyn ProviderCatalogStore,
    providers: Vec<Provider>,
    expected_providers: Option<&[Provider]>,
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
    if let Some(expected) = expected_providers {
        if hash_provider_state(expected).ok() != hash_provider_state(&previous_providers).ok() {
            return conflict_result(
                previous_providers,
                "provider configuration changed since this editor snapshot was loaded".to_string(),
            );
        }
    }
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
    if serde_json::to_value(&saved).ok() != serde_json::to_value(&requested_providers).ok() {
        return rollback_result(
            store,
            previous_providers,
            previous_models,
            "provider configuration readback did not match the requested state".to_string(),
            protocol_changed,
        );
    }
    if let Err(error) = store.mark_catalog_write_pending() {
        return rollback_result(
            store,
            previous_providers,
            previous_models,
            error,
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

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
enum RecoveryState {
    Preparing,
    Prepared,
    ProviderWritePending,
    CatalogWritePending,
    Committed,
    CatalogDisabled,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
struct FileSnapshot {
    existed: bool,
    sha256: Option<String>,
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
    committed_providers_sha256: Option<String>,
    committed_catalog_sha256: Option<String>,
}

struct RuntimeProviderCatalogStore {
    paths: config::ConfigPaths,
}

impl RuntimeProviderCatalogStore {
    fn new(paths: config::ConfigPaths) -> Self {
        Self { paths }
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
        let value: serde_json::Value = serde_json::from_str(&text).map_err(|error| {
            format!("failed to parse provider/catalog recovery record: {error}")
        })?;
        let schema_version = value
            .get("schema_version")
            .and_then(serde_json::Value::as_u64)
            .ok_or_else(|| {
                "failed to parse provider/catalog recovery record: missing schema_version"
                    .to_string()
            })?;
        if schema_version != u64::from(RECOVERY_SCHEMA_VERSION) {
            return Err(format!(
                "unsupported provider/catalog recovery schema version {schema_version}"
            ));
        }
        let record: RecoveryRecord = serde_json::from_value(value).map_err(|error| {
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
        transaction_fault(match record.state {
            RecoveryState::Preparing => "write-preparing-marker",
            RecoveryState::Prepared => "write-prepared-marker",
            RecoveryState::ProviderWritePending => "write-provider-marker",
            RecoveryState::CatalogWritePending => "write-catalog-marker",
            RecoveryState::Committed => "write-committed-marker",
            RecoveryState::CatalogDisabled => "write-disabled-marker",
        })?;
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
        verify_snapshot_target(
            &self.paths.generated_catalog_path(),
            &record.catalog,
            MAX_CATALOG_SNAPSHOT_BYTES,
            "provider-write generated catalog",
        )?;
        let actual = hash_provider_state(&self.current_providers()?)?;
        let base = record
            .base_provider_state_sha256
            .as_deref()
            .ok_or_else(|| {
                "provider-write journal is missing the base provider hash".to_string()
            })?;
        let candidate = record
            .candidate_provider_state_sha256
            .as_deref()
            .ok_or_else(|| {
                "provider-write journal is missing the candidate provider hash".to_string()
            })?;
        if actual != base && actual != candidate {
            return Err(
                "provider configuration diverged from this transaction's base and candidate states"
                    .to_string(),
            );
        }
        Ok(())
    }

    fn verify_catalog_write_prefix(&self, record: &RecoveryRecord) -> Result<(), String> {
        let providers = self.current_providers()?;
        let actual_provider_state = hash_provider_state(&providers)?;
        let base = record
            .base_provider_state_sha256
            .as_deref()
            .ok_or_else(|| "catalog-write journal is missing the base provider hash".to_string())?;
        let candidate = record
            .candidate_provider_state_sha256
            .as_deref()
            .ok_or_else(|| {
                "catalog-write journal is missing the candidate provider hash".to_string()
            })?;
        if actual_provider_state != base && actual_provider_state != candidate {
            return Err(
                "provider configuration diverged from this transaction's catalog-write prefix"
                    .to_string(),
            );
        }
        verify_file_hash(
            &self.paths.runtime_providers_path(),
            record.committed_providers_sha256.as_deref(),
            MAX_PROVIDER_SNAPSHOT_BYTES,
            "catalog-write candidate provider configuration",
        )?;
        if verify_snapshot_target(
            &self.paths.generated_catalog_path(),
            &record.catalog,
            MAX_CATALOG_SNAPSHOT_BYTES,
            "catalog-write base catalog",
        )
        .is_ok()
        {
            return Ok(());
        }
        if actual_provider_state != candidate {
            return Err(
                "generated catalog changed while providers remained at the base state".to_string(),
            );
        }
        if read_bounded(
            &self.paths.generated_catalog_path(),
            MAX_CATALOG_SNAPSHOT_BYTES,
            "catalog-write candidate catalog",
        )
            .ok()
            .and_then(|text| serde_json::from_str::<serde_json::Value>(&text).ok())
            .and_then(|value| {
                value
                    .get("models")
                    .and_then(|models| models.as_array())
                    .cloned()
            })
            .is_some_and(|models| models.is_empty())
        {
            return Ok(());
        }
        let models = self.current_catalog().map_err(|_| {
            "generated catalog is neither the transaction base nor a complete candidate".to_string()
        })?;
        verify_catalog_for_providers(&models, &providers).map_err(|_| {
            "generated catalog is neither the transaction base nor a complete candidate".to_string()
        })
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
        match record.state {
            RecoveryState::Committed => {
                verify_file_hash(
                    &self.paths.runtime_providers_path(),
                    record.committed_providers_sha256.as_deref(),
                    MAX_PROVIDER_SNAPSHOT_BYTES,
                    "committed provider configuration",
                )?;
                verify_file_hash(
                    &self.paths.generated_catalog_path(),
                    record.committed_catalog_sha256.as_deref(),
                    MAX_CATALOG_SNAPSHOT_BYTES,
                    "committed generated catalog",
                )?;
                self.clear_recovery()
            }
            RecoveryState::Preparing | RecoveryState::Prepared => {
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
            RecoveryState::ProviderWritePending => {
                self.verify_provider_write_prefix(&record)?;
                self.restore_pending()?;
                self.clear_recovery()
            }
            RecoveryState::CatalogWritePending => {
                self.verify_catalog_write_prefix(&record)?;
                self.restore_pending()?;
                self.clear_recovery()
            }
            RecoveryState::CatalogDisabled => {
                let providers = self.current_providers()?;
                let expected_hash =
                    record
                        .committed_providers_sha256
                        .as_deref()
                        .ok_or_else(|| {
                            "catalog-disabled recovery record is missing provider state hash"
                                .to_string()
                        })?;
                if hash_provider_state(&providers)? != expected_hash {
                    return Err(
                        "provider configuration changed while catalog recovery was pending"
                            .to_string(),
                    );
                }
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
        models::generate_catalog()
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
            record.state = RecoveryState::Prepared;
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
        if !matches!(record.state, RecoveryState::Prepared) {
            return Err(
                "provider/catalog transaction was not prepared before provider write".to_string(),
            );
        }
        record.state = RecoveryState::ProviderWritePending;
        record.candidate_provider_state_sha256 =
            Some(hash_provider_state(&normalized_provider_state(providers))?);
        self.write_recovery(&record)
    }

    fn mark_catalog_write_pending(&mut self) -> Result<(), String> {
        let mut record = self.read_recovery()?.ok_or_else(|| {
            "provider/catalog recovery record disappeared before catalog write".to_string()
        })?;
        if !matches!(record.state, RecoveryState::ProviderWritePending) {
            return Err("provider/catalog provider write phase was not recorded".to_string());
        }
        let expected = record
            .candidate_provider_state_sha256
            .as_deref()
            .ok_or_else(|| {
                "provider/catalog transaction is missing the candidate provider hash".to_string()
            })?;
        if hash_provider_state(&self.current_providers()?)? != expected {
            return Err("provider configuration identity changed before catalog write".to_string());
        }
        record.state = RecoveryState::CatalogWritePending;
        record.committed_providers_sha256 = Some(hash_file(
            &self.paths.runtime_providers_path(),
            MAX_PROVIDER_SNAPSHOT_BYTES,
        )?);
        self.write_recovery(&record)
    }

    fn save_providers(&mut self, providers: Vec<Provider>) -> Result<Vec<Provider>, String> {
        config::save_providers_with_paths(providers, &self.paths)?;
        config::get_providers_with_paths(&self.paths)
    }

    fn restore_pending(&mut self) -> Result<(), String> {
        let Some(record) = self.read_recovery()? else {
            return Ok(());
        };
        if matches!(record.state, RecoveryState::Committed) {
            return Ok(());
        }
        validate_snapshot_backup(
            &self.paths.provider_catalog_providers_backup_path(),
            &record.providers,
            MAX_PROVIDER_SNAPSHOT_BYTES,
            "provider configuration",
        )?;
        validate_snapshot_backup(
            &self.paths.provider_catalog_catalog_backup_path(),
            &record.catalog,
            MAX_CATALOG_SNAPSHOT_BYTES,
            "generated catalog",
        )?;
        restore_file(
            &self.paths.runtime_providers_path(),
            &self.paths.provider_catalog_providers_backup_path(),
            &record.providers,
            "provider configuration",
        )?;
        restore_file(
            &self.paths.generated_catalog_path(),
            &self.paths.provider_catalog_catalog_backup_path(),
            &record.catalog,
            "generated catalog",
        )?;
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
        if !matches!(record.state, RecoveryState::CatalogWritePending) {
            return Err(
                "provider/catalog recovery record was not prepared before commit".to_string(),
            );
        }
        record.state = RecoveryState::Committed;
        record.committed_providers_sha256 = Some(hash_file(
            &self.paths.runtime_providers_path(),
            MAX_PROVIDER_SNAPSHOT_BYTES,
        )?);
        record.committed_catalog_sha256 = Some(hash_file(
            &self.paths.generated_catalog_path(),
            MAX_CATALOG_SNAPSHOT_BYTES,
        )?);
        self.write_recovery(&record)
    }

    fn ensure_recovery_required(&mut self) -> Result<(), String> {
        if self.recovery_path().exists() {
            return Ok(());
        }
        self.clear_orphaned_backups()?;
        let providers = self.current_providers()?;
        self.write_recovery(&RecoveryRecord {
            schema_version: RECOVERY_SCHEMA_VERSION,
            transaction_id: transaction_id()?,
            state: RecoveryState::CatalogDisabled,
            created_at_unix_seconds: unix_timestamp_seconds()?,
            providers: FileSnapshot {
                existed: false,
                sha256: None,
            },
            catalog: FileSnapshot {
                existed: false,
                sha256: None,
            },
            base_provider_state_sha256: None,
            candidate_provider_state_sha256: None,
            committed_providers_sha256: Some(hash_provider_state(&providers)?),
            committed_catalog_sha256: None,
        })
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
        match safe_file::write_text_atomic(&path, "{\"models\":[]}\n") {
            Ok(()) => Ok(()),
            Err(write_error) => {
                if !path.exists() {
                    return Ok(());
                }
                fs::remove_file(&path).map_err(|remove_error| {
                    format!(
                        "failed to replace catalog with an empty fail-closed catalog ({write_error}); failed to remove it ({remove_error})"
                    )
                })
            }
        }
    }
}

impl RuntimeProviderCatalogStore {
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
        });
    }
    let contents = read_bounded(path, max_bytes, label)?;
    Ok(FileSnapshot {
        existed: true,
        sha256: Some(hash_bytes(contents.as_bytes())),
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
    if snapshot.sha256.as_deref() != Some(hash_bytes(contents.as_bytes()).as_str()) {
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

fn restore_file(
    path: &Path,
    backup_path: &Path,
    snapshot: &FileSnapshot,
    label: &str,
) -> Result<(), String> {
    if snapshot.existed {
        let max_bytes = if label.contains("provider") {
            MAX_PROVIDER_SNAPSHOT_BYTES
        } else {
            MAX_CATALOG_SNAPSHOT_BYTES
        };
        let contents = read_bounded(backup_path, max_bytes, &format!("{label} recovery backup"))?;
        let boundary = common_ancestor(path, backup_path)
            .ok_or_else(|| format!("failed to resolve trusted boundary for restored {label}"))?;
        safe_file::write_private_text_atomic(path, &contents, boundary)
            .map_err(|error| format!("failed to restore {label}: {error}"))?;
        return Ok(());
    }
    if !path.exists() {
        return Ok(());
    }
    fs::remove_file(path)
        .map_err(|error| format!("failed to remove newly created {label}: {error}"))
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
    match record.state {
        RecoveryState::Preparing | RecoveryState::Prepared => {
            validate_sha256(
                record.base_provider_state_sha256.as_deref(),
                "base provider state",
            )?;
            if record.candidate_provider_state_sha256.is_some()
                || record.committed_providers_sha256.is_some()
                || record.committed_catalog_sha256.is_some()
            {
                return Err(
                    "prepared provider/catalog recovery record contains candidate or committed hashes"
                        .to_string(),
                );
            }
        }
        RecoveryState::ProviderWritePending => {
            validate_sha256(
                record.base_provider_state_sha256.as_deref(),
                "base provider state",
            )?;
            validate_sha256(
                record.candidate_provider_state_sha256.as_deref(),
                "candidate provider state",
            )?;
            if record.committed_providers_sha256.is_some()
                || record.committed_catalog_sha256.is_some()
            {
                return Err("provider-write recovery record contains committed hashes".to_string());
            }
        }
        RecoveryState::CatalogWritePending => {
            validate_sha256(
                record.base_provider_state_sha256.as_deref(),
                "base provider state",
            )?;
            validate_sha256(
                record.candidate_provider_state_sha256.as_deref(),
                "candidate provider state",
            )?;
            validate_sha256(
                record.committed_providers_sha256.as_deref(),
                "candidate provider file",
            )?;
            if record.committed_catalog_sha256.is_some() {
                return Err(
                    "catalog-write recovery record contains a committed catalog hash".to_string(),
                );
            }
        }
        RecoveryState::Committed => {
            validate_sha256(
                record.base_provider_state_sha256.as_deref(),
                "base provider state",
            )?;
            validate_sha256(
                record.candidate_provider_state_sha256.as_deref(),
                "candidate provider state",
            )?;
            validate_sha256(
                record.committed_providers_sha256.as_deref(),
                "committed provider configuration",
            )?;
            validate_sha256(
                record.committed_catalog_sha256.as_deref(),
                "committed generated catalog",
            )?;
        }
        RecoveryState::CatalogDisabled => {
            if record.providers.existed
                || record.providers.sha256.is_some()
                || record.catalog.existed
                || record.catalog.sha256.is_some()
                || record.base_provider_state_sha256.is_some()
                || record.candidate_provider_state_sha256.is_some()
                || record.committed_catalog_sha256.is_some()
            {
                return Err(
                    "catalog-disabled recovery record contains snapshot or catalog hashes"
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
    match (snapshot.existed, snapshot.sha256.as_deref()) {
        (true, hash) => validate_sha256(hash, label),
        (false, None) => Ok(()),
        (false, Some(_)) => Err(format!(
            "provider/catalog recovery record has a hash for absent {label}"
        )),
    }
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

fn validate_snapshot_backup(
    backup_path: &Path,
    snapshot: &FileSnapshot,
    max_bytes: u64,
    label: &str,
) -> Result<(), String> {
    if !snapshot.existed {
        if backup_path.exists() {
            return Err(format!(
                "unexpected {label} recovery backup exists for an absent snapshot"
            ));
        }
        return Ok(());
    }
    let actual = hash_file(backup_path, max_bytes)?;
    if snapshot.sha256.as_deref() != Some(actual.as_str()) {
        return Err(format!(
            "{label} recovery backup hash did not match the journal"
        ));
    }
    Ok(())
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
    verify_file_hash(path, snapshot.sha256.as_deref(), max_bytes, label)
}

fn verify_file_hash(
    path: &Path,
    expected: Option<&str>,
    max_bytes: u64,
    label: &str,
) -> Result<(), String> {
    let expected = expected.ok_or_else(|| format!("missing expected hash for {label}"))?;
    let actual = hash_file(path, max_bytes)?;
    if actual != expected {
        return Err(format!("{label} hash did not match the recovery record"));
    }
    Ok(())
}

fn hash_file(path: &Path, max_bytes: u64) -> Result<String, String> {
    let label = path.display().to_string();
    let contents = read_bounded(path, max_bytes, &label)?;
    Ok(hash_bytes(contents.as_bytes()))
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
    let invalidation_error = store.invalidate_catalog().err();
    // Quarantine first: if marker publication itself crashes or is attacked,
    // no readable advanced-capability catalog remains.
    let marker_error = store.ensure_recovery_required().err();
    let providers = store.current_providers().unwrap_or(fallback_providers);
    let (models, catalog_disabled) = match invalidation_error.as_ref() {
        None => (Vec::new(), true),
        Some(_) => (store.current_catalog().unwrap_or_default(), false),
    };
    ProviderCatalogTransactionResult {
        outcome: ProviderCatalogTransactionOutcome::RecoveryRequired,
        providers,
        models,
        protocol_changed,
        detail: Some(match invalidation_error {
            Some(error) => {
                format!(
                    "{detail}{}; fail-closed catalog invalidation also failed: {error}",
                    marker_error
                        .as_ref()
                        .map(|marker| format!("; durable recovery marker also failed: {marker}"))
                        .unwrap_or_default()
                )
            }
            None => format!(
                "{detail}{}; generated catalog disabled fail-closed",
                marker_error
                    .as_ref()
                    .map(|marker| format!("; durable recovery marker failed: {marker}"))
                    .unwrap_or_default()
            ),
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
        "model_id": model_id.trim().to_ascii_lowercase(),
        "provider_id": provider_id.trim().to_ascii_lowercase(),
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
        protocol_changed,
        detail: Some(detail),
        catalog_disabled: false,
    }
}

#[cfg(test)]
mod tests {
    use super::{
        persist_with_expected_store, persist_with_store, recover_before_gateway_with_store,
        verify_catalog_for_providers, ProviderCatalogStore, ProviderCatalogTransactionOutcome,
        RuntimeProviderCatalogStore,
    };
    use crate::{
        config, CapabilityBinding, CapabilityProfile, Model, Provider, QualificationState,
        UpstreamFormat,
    };
    use std::collections::VecDeque;
    use std::fs;
    use std::path::{Path, PathBuf};
    use std::sync::mpsc;
    use std::thread;
    use std::time::Duration;
    use std::time::{SystemTime, UNIX_EPOCH};

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
        let loaded = provider(UpstreamFormat::Responses);
        let mut externally_updated = loaded.clone();
        externally_updated.name = "External update".to_string();
        let catalog = vec![catalog_model(UpstreamFormat::Responses)];
        let mut requested = loaded.clone();
        requested.name = "Stale editor update".to_string();
        let mut store = MemoryStore::new(
            vec![externally_updated.clone()],
            catalog.clone(),
            std::iter::empty(),
        );

        let result = persist_with_expected_store(
            &mut store,
            vec![requested],
            Some(std::slice::from_ref(&loaded)),
        );

        assert_eq!(result.outcome, ProviderCatalogTransactionOutcome::Conflict);
        assert_eq!(store.providers[0].name, externally_updated.name);
        assert_eq!(
            serde_json::to_value(&store.catalog).unwrap(),
            serde_json::to_value(&catalog).unwrap()
        );
        assert!(store.pending.is_none());
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
        assert!(store.mark_catalog_write_pending().is_err());
        super::clear_transaction_fault();
        assert!(paths.provider_catalog_recovery_path().exists());

        store.mark_catalog_write_pending().unwrap();
        write_fixture(&paths.generated_catalog_path(), "{\"models\":[]}\n");
        super::install_transaction_fault("write-committed-marker");
        assert!(store.mark_committed().is_err());
        super::clear_transaction_fault();
        assert!(paths.provider_catalog_recovery_path().exists());
        let _ = fs::remove_dir_all(root);
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
        store
            .mark_provider_write_pending(std::slice::from_ref(&candidate))
            .unwrap();
        store.save_providers(vec![candidate]).unwrap();
        store.mark_catalog_write_pending().unwrap();
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
        store.mark_catalog_write_pending().unwrap();
        let externally_edited = format!(
            "{}# external owner preserved this comment\n",
            fs::read_to_string(paths.runtime_providers_path()).unwrap()
        );
        write_fixture(&paths.runtime_providers_path(), &externally_edited);
        let catalog_before = fs::read_to_string(paths.generated_catalog_path()).unwrap();

        let error = store
            .recover_pending()
            .expect_err("raw provider divergence must not be overwritten");

        assert!(error.contains("hash did not match"));
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
                        "{\"schema_version\":3,\"secret\":\"must-not-appear\"}\n",
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
            let providers = config::get_providers().expect("provider read after recovery");
            config::save_providers(providers).expect("provider write after recovery");
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
            assert!(config::save_providers(Vec::new()).is_err());
            assert!(crate::catalog::sync_catalog().is_err());
            assert!(crate::official_refresh::refresh_manual().is_err());
            assert!(crate::web_bridge::dispatch_startup_recovery_probe().is_err());
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
        store.mark_catalog_write_pending().unwrap();
        let committed_providers = fs::read_to_string(&providers_path).unwrap();
        let committed_catalog = "{\"models\":[{\"id\":\"committed\"}]}\n";
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
        store.mark_catalog_write_pending().unwrap();
        write_fixture(&catalog_path, "{\"models\":[{\"id\":\"committed\"}]}\n");
        store.mark_committed().expect("mark recovery committed");
        write_fixture(&catalog_path, "{\"models\":[{\"id\":\"tampered\"}]}\n");
        drop(store);

        let mut restarted_store = RuntimeProviderCatalogStore::new(paths.clone());
        let error = restarted_store
            .recover_pending()
            .expect_err("tampered commit must fail closed");

        assert!(error.contains("committed generated catalog hash"));
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
            r#"{"schema_version":3,"state":"prepared","unexpected":"field"}"#,
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

        fn mark_catalog_write_pending(&mut self) -> Result<(), String> {
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
