use crate::{config, models, safe_file, Model, Provider, UpstreamFormat};
use serde::{Deserialize, Serialize};
use sha2::{Digest, Sha256};
use std::fs;
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

const RECOVERY_SCHEMA_VERSION: u32 = 2;
const MAX_RECOVERY_RECORD_BYTES: u64 = 64 * 1024;
const MAX_PROVIDER_SNAPSHOT_BYTES: u64 = 8 * 1024 * 1024;
const MAX_CATALOG_SNAPSHOT_BYTES: u64 = 64 * 1024 * 1024;
const MAX_FUTURE_CLOCK_SKEW_SECONDS: u64 = 5 * 60;

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
    fn save_providers(&mut self, providers: Vec<Provider>) -> Result<Vec<Provider>, String>;
    fn restore_pending(&mut self) -> Result<(), String>;
    fn mark_committed(&mut self) -> Result<(), String>;
    fn ensure_recovery_required(&mut self) -> Result<(), String>;
    fn clear_recovery(&mut self) -> Result<(), String>;
    fn invalidate_catalog(&mut self) -> Result<(), String>;
}

pub fn persist_provider_catalog_state(
    providers: Vec<Provider>,
) -> Result<ProviderCatalogTransactionResult, String> {
    let paths = config::ConfigPaths::runtime()?;
    let transaction_lock_path = paths.proxy_dir().join("provider-catalog-transaction-guard");
    let _transaction_lock = safe_file::FileLock::acquire(&transaction_lock_path)
        .map_err(|error| format!("failed to lock provider/catalog transaction: {error}"))?;
    let mut store = RuntimeProviderCatalogStore::new(paths);
    Ok(persist_with_store(&mut store, providers))
}

pub fn recover_before_gateway_start() -> Result<(), String> {
    let paths = config::ConfigPaths::runtime()?;
    let transaction_lock_path = paths.proxy_dir().join("provider-catalog-transaction-guard");
    let _transaction_lock = safe_file::FileLock::acquire(&transaction_lock_path)
        .map_err(|error| format!("failed to lock provider/catalog recovery: {error}"))?;
    let mut store = RuntimeProviderCatalogStore::new(paths);
    recover_before_gateway_with_store(&mut store)
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
    Prepared,
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
    state: RecoveryState,
    created_at_unix_seconds: u64,
    providers: FileSnapshot,
    catalog: FileSnapshot,
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
        let path = self.recovery_path();
        if !path.exists() {
            return Ok(None);
        }
        let metadata = fs::metadata(&path).map_err(|error| {
            format!("failed to inspect provider/catalog recovery record: {error}")
        })?;
        if metadata.len() > MAX_RECOVERY_RECORD_BYTES {
            return Err(format!(
                "provider/catalog recovery record exceeds {MAX_RECOVERY_RECORD_BYTES} bytes"
            ));
        }
        let text = fs::read_to_string(&path)
            .map_err(|error| format!("failed to read provider/catalog recovery record: {error}"))?;
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
        let text = serde_json::to_string(record).map_err(|error| {
            format!("failed to serialize provider/catalog recovery record: {error}")
        })?;
        safe_file::write_text_atomic(&self.recovery_path(), &(text + "\n"))
            .map_err(|error| format!("failed to persist provider/catalog recovery record: {error}"))
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
            RecoveryState::Prepared => {
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
        models::read_catalog_models(&self.paths.generated_catalog_path())
    }

    fn generate_catalog(&mut self) -> Result<Vec<Model>, String> {
        models::generate_catalog()
    }

    fn prepare_recovery(&mut self) -> Result<(), String> {
        if self.recovery_path().exists() {
            return Err(
                "provider/catalog recovery record already exists before prepare".to_string(),
            );
        }
        self.clear_orphaned_backups()?;
        let prepared = (|| {
            let record = RecoveryRecord {
                schema_version: RECOVERY_SCHEMA_VERSION,
                state: RecoveryState::Prepared,
                created_at_unix_seconds: unix_timestamp_seconds()?,
                providers: capture_file(
                    &self.paths.runtime_providers_path(),
                    &self.paths.provider_catalog_providers_backup_path(),
                    MAX_PROVIDER_SNAPSHOT_BYTES,
                    "provider configuration",
                )?,
                catalog: capture_file(
                    &self.paths.generated_catalog_path(),
                    &self.paths.provider_catalog_catalog_backup_path(),
                    MAX_CATALOG_SNAPSHOT_BYTES,
                    "generated catalog",
                )?,
                committed_providers_sha256: None,
                committed_catalog_sha256: None,
            };
            self.write_recovery(&record)
        })();
        if prepared.is_err() {
            if let Err(error) = self.clear_orphaned_backups() {
                log::warn!("failed to clear incomplete provider/catalog recovery backups: {error}");
            }
        }
        prepared
    }

    fn save_providers(&mut self, providers: Vec<Provider>) -> Result<Vec<Provider>, String> {
        config::save_providers_with_paths(providers, &self.paths)?;
        config::read_runtime_providers_with_paths(&self.paths)
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
        if !matches!(record.state, RecoveryState::Prepared) {
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
            committed_providers_sha256: Some(hash_provider_state(&providers)?),
            committed_catalog_sha256: None,
        })
    }

    fn clear_recovery(&mut self) -> Result<(), String> {
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

fn capture_file(
    path: &Path,
    backup_path: &Path,
    max_bytes: u64,
    label: &str,
) -> Result<FileSnapshot, String> {
    if !path.exists() {
        return Ok(FileSnapshot {
            existed: false,
            sha256: None,
        });
    }
    let contents = read_bounded(path, max_bytes, label)?;
    safe_file::write_text_atomic(backup_path, &contents).map_err(|error| {
        format!(
            "failed to persist {label} recovery backup {}: {error}",
            backup_path.display()
        )
    })?;
    let permissions = fs::metadata(path)
        .map_err(|error| format!("failed to inspect {label} permissions: {error}"))?
        .permissions();
    fs::set_permissions(backup_path, permissions)
        .map_err(|error| format!("failed to preserve {label} recovery permissions: {error}"))?;
    Ok(FileSnapshot {
        existed: true,
        sha256: Some(hash_bytes(contents.as_bytes())),
    })
}

fn restore_file(
    path: &Path,
    backup_path: &Path,
    snapshot: &FileSnapshot,
    label: &str,
) -> Result<(), String> {
    if snapshot.existed {
        let contents = fs::read_to_string(backup_path)
            .map_err(|error| format!("failed to read {label} recovery backup: {error}"))?;
        safe_file::write_text_atomic(path, &contents)
            .map_err(|error| format!("failed to restore {label}: {error}"))?;
        let permissions = fs::metadata(backup_path)
            .map_err(|error| format!("failed to inspect {label} recovery permissions: {error}"))?
            .permissions();
        return fs::set_permissions(path, permissions)
            .map_err(|error| format!("failed to restore {label} permissions: {error}"));
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
    validate_snapshot_shape(&record.providers, "provider configuration")?;
    validate_snapshot_shape(&record.catalog, "generated catalog")?;
    match record.state {
        RecoveryState::Prepared => {
            if record.committed_providers_sha256.is_some()
                || record.committed_catalog_sha256.is_some()
            {
                return Err(
                    "prepared provider/catalog recovery record contains committed hashes"
                        .to_string(),
                );
            }
        }
        RecoveryState::Committed => {
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
    let metadata =
        fs::metadata(path).map_err(|error| format!("failed to inspect {label}: {error}"))?;
    if metadata.len() > max_bytes {
        return Err(format!(
            "{label} exceeds the recovery size limit of {max_bytes} bytes"
        ));
    }
    fs::read_to_string(path).map_err(|error| format!("failed to read {label}: {error}"))
}

fn hash_bytes(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

fn hash_provider_state(providers: &[Provider]) -> Result<String, String> {
    serde_json::to_vec(providers)
        .map(|bytes| hash_bytes(&bytes))
        .map_err(|error| format!("failed to hash provider state for catalog recovery: {error}"))
}

fn unix_timestamp_seconds() -> Result<u64, String> {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs())
        .map_err(|error| format!("system clock is before the Unix epoch: {error}"))
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
    let marker_error = store.ensure_recovery_required().err();
    let invalidation_error = store.invalidate_catalog().err();
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
            let matching = models
                .iter()
                .filter_map(|model| model.capability_binding.as_ref())
                .filter(|binding| {
                    binding.provider == protocol_switch.provider_id && binding.model == *model_id
                })
                .collect::<Vec<_>>();
            if matching.len() != 1 {
                return Err(format!(
                    "catalog readback expected exactly one binding for {}/{} but found {}",
                    protocol_switch.provider_id,
                    model_id,
                    matching.len()
                ));
            }
            if matching[0].upstream_protocol != protocol_switch.upstream_protocol {
                return Err(format!(
                    "catalog readback did not bind {}/{} to {:?}",
                    protocol_switch.provider_id, model_id, protocol_switch.upstream_protocol
                ));
            }
        }
    }
    Ok(())
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
        persist_with_store, recover_before_gateway_with_store, verify_catalog_for_providers,
        ProviderCatalogStore, ProviderCatalogTransactionOutcome, RuntimeProviderCatalogStore,
    };
    use crate::{config, CapabilityBinding, Model, Provider, QualificationState, UpstreamFormat};
    use std::collections::VecDeque;
    use std::fs;
    use std::path::{Path, PathBuf};
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
    fn runtime_recovery_restores_exact_provider_and_catalog_file_snapshots() {
        let root = temp_root("restore-exact");
        let paths = isolated_paths(&root);
        let providers_path = paths.runtime_providers_path();
        let catalog_path = paths.generated_catalog_path();
        let original_providers = "[[providers]]\nid = \"ollama-cloud\"\napi_key = \"secret\"\n";
        let original_catalog = "{\n  \"models\": [{\"id\":\"glm-5.2\"}]\n}\n";
        write_fixture(&providers_path, original_providers);
        write_fixture(&catalog_path, original_catalog);
        let mut store = RuntimeProviderCatalogStore::new(paths.clone());

        store.prepare_recovery().expect("prepare recovery journal");
        let journal = fs::read_to_string(paths.provider_catalog_recovery_path())
            .expect("read recovery journal");
        assert!(!journal.contains("secret"));
        assert!(!journal.contains(original_providers));
        assert_eq!(
            fs::read_to_string(paths.provider_catalog_providers_backup_path())
                .expect("read provider recovery backup"),
            original_providers
        );
        write_fixture(&providers_path, "[[providers]]\nid = \"candidate\"\n");
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
    fn runtime_recovery_preserves_files_after_commit_marker() {
        let root = temp_root("preserve-commit");
        let paths = isolated_paths(&root);
        let providers_path = paths.runtime_providers_path();
        let catalog_path = paths.generated_catalog_path();
        write_fixture(&providers_path, "[[providers]]\nid = \"previous\"\n");
        write_fixture(&catalog_path, "{\"models\":[{\"id\":\"previous\"}]}\n");
        let mut store = RuntimeProviderCatalogStore::new(paths.clone());

        store.prepare_recovery().expect("prepare recovery journal");
        let committed_providers = "[[providers]]\nid = \"committed\"\n";
        let committed_catalog = "{\"models\":[{\"id\":\"committed\"}]}\n";
        write_fixture(&providers_path, committed_providers);
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
        let providers_path = paths.runtime_providers_path();
        let catalog_path = paths.generated_catalog_path();
        write_fixture(&providers_path, "[[providers]]\nid = \"previous\"\n");
        write_fixture(&catalog_path, "{\"models\":[{\"id\":\"previous\"}]}\n");
        let mut store = RuntimeProviderCatalogStore::new(paths.clone());
        store.prepare_recovery().expect("prepare recovery journal");
        write_fixture(&providers_path, "[[providers]]\nid = \"committed\"\n");
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
            r#"{"schema_version":2,"state":"prepared","unexpected":"field"}"#,
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
            .is_some_and(|detail| detail.contains("did not bind")));
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
                enabled: true,
                gateway_exported: true,
                ..Model::default()
            }],
        }
    }

    fn catalog_model(upstream_protocol: UpstreamFormat) -> Model {
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
                capability_manifest_hash: Some(format!("sha256:{}", "a".repeat(64))),
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
