//! Declarative per-client injection descriptor — the managed-client adapter
//! seam (#432, ADR-0004, campaign #436).
//!
//! Under Provider Injection semantics CodexHub adds exactly one provider entry
//! (the Injected Block, fixed route key `codexhub`) into a client's existing
//! configuration, preserving every user-owned provider and setting. CodexHub
//! never rewrites foreign content and never flips the client's global
//! default-model selection (activation is always the user's own action).
//!
//! A descriptor is *data, not code branches*: per client it declares the
//! config file(s) and format, path resolution, the injection point, the
//! injected-entry template, the credential file + key, the read-only
//! activation key, the adoption rule, the removal key set, and the
//! block-fingerprint computation. Adding a new client means adding a new
//! descriptor value, never editing another client's predicates. The engine
//! below is generic over the descriptor; the only per-shape branch is the
//! file-format strategy (YAML today; JSON/TOML land with their first
//! consumer).
//!
//! First consumer: DSH (#430). Existing takeover-era clients keep their
//! legacy predicates in `gateway.rs` untouched until their migration phase.
//!
//! Consumed by #430 wiring; the framework intentionally lands ahead of its
//! first caller so nothing existing changes behavior.
#![allow(dead_code)]

use serde::Serialize;
use serde_yaml::{Mapping, Value};
use sha2::{Digest, Sha256};
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

/// Fixed route key of the Injected Block in every client configuration.
pub(crate) const ROUTE_KEY: &str = "codexhub";

/// Serde format of a client configuration file.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum ConfigFormat {
    Yaml,
    Json,
    Toml,
}

/// One declaratively described client configuration file.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct ConfigFileSpec {
    /// Path relative to the client home root (e.g. `settings.yaml` under
    /// `~/.dsh`). Forward slashes on every platform; resolution joins onto
    /// the platform-correct client root.
    pub relative_path: &'static str,
    pub format: ConfigFormat,
}

impl ConfigFileSpec {
    pub(crate) fn resolve(&self, client_root: &Path) -> PathBuf {
        self.relative_path
            .split('/')
            .fold(client_root.to_path_buf(), |path, segment| path.join(segment))
    }
}

/// How a pre-existing same-named provider entry is treated on inject.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum AdoptionRule {
    /// Adopt the entry when it already points at the local Gateway (Q4);
    /// a same-named entry pointing anywhere else is a truthful conflict,
    /// never a silent overwrite.
    AdoptLocalGatewayElseConflict,
}

/// Credential store declaration: a single surgical key in a separate file,
/// referenced from the provider entry by bare env-var name.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct CredentialSpec {
    pub file: ConfigFileSpec,
    /// Key inside the credential file owning the value (e.g.
    /// `CODEXHUB_API_KEY` in `~/.dsh/.credentials.yaml`).
    pub key: &'static str,
    /// Bare env-var name the injected provider entry references.
    pub env_var: &'static str,
}

/// Injected-entry template: which keys CodexHub owns inside the one provider
/// entry it writes.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct EntryTemplate {
    /// Provider API/wire-protocol identifier (e.g. `openai-responses`).
    pub api: &'static str,
    /// Key carrying the local Gateway base URL (e.g. `baseURL`).
    pub base_url_key: &'static str,
    /// Key carrying the credential env-var reference (e.g. `apiKeyEnv`).
    pub credential_ref_key: &'static str,
    /// Key carrying the enabled-model projection (e.g. `models`).
    pub models_key: &'static str,
    /// Key carrying the model id inside each model entry (e.g. `id`).
    pub model_id_key: &'static str,
}

/// Declarative per-client injection descriptor. Pure data plus path
/// resolution; all mutation logic lives in the generic engine below.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct InjectionDescriptor {
    pub client_id: &'static str,
    /// Directory holding the client's configuration, relative to the user
    /// home directory (e.g. `.dsh`).
    pub client_home_relative: &'static str,
    /// Primary config file receiving the provider entry.
    pub config_file: ConfigFileSpec,
    /// Key path to the providers map inside the config file (e.g.
    /// `["llm-pi-ai", "providers"]`); the injected entry lives at
    /// `providers_path + [route_key]`.
    pub providers_path: &'static [&'static str],
    /// Fixed route key of the injected entry; always `codexhub`.
    pub route_key: &'static str,
    pub entry_template: EntryTemplate,
    pub credential: CredentialSpec,
    /// Key path of the client's global default-model selection (e.g.
    /// `["agent-default-model"]`). READ-ONLY: injection never writes it
    /// (Q1); it is surfaced as informational state only.
    pub activation_path: &'static [&'static str],
    pub adoption: AdoptionRule,
}

impl InjectionDescriptor {
    /// Injection point: providers path plus the fixed route key.
    pub(crate) fn injection_point(&self) -> Vec<&'static str> {
        let mut path = self.providers_path.to_vec();
        path.push(self.route_key);
        path
    }

    /// Removal key set for surgical detach (Q3): exactly the injected
    /// provider entry plus the credential key. Detach removes precisely this.
    pub(crate) fn removal_key_set(&self) -> Vec<String> {
        vec![
            self.injection_point().join("."),
            format!("{}:{}", self.credential.file.relative_path, self.credential.key),
        ]
    }

    /// Platform-correct client home root (e.g. `~/.dsh`).
    pub(crate) fn client_home(&self) -> Option<PathBuf> {
        dirs::home_dir().map(|home| {
            self.client_home_relative
                .split('/')
                .fold(home, |path, segment| path.join(segment))
        })
    }
}

/// The DSH descriptor: `~/.dsh/settings.yaml`, provider map
/// `llm-pi-ai.providers`, api `openai-responses`, credential reference
/// `apiKeyEnv` with the value held as a single key in
/// `~/.dsh/.credentials.yaml`, activation key `agent-default-model`
/// (read-only).
pub(crate) fn dsh_descriptor() -> InjectionDescriptor {
    InjectionDescriptor {
        client_id: "dsh",
        client_home_relative: ".dsh",
        config_file: ConfigFileSpec {
            relative_path: "settings.yaml",
            format: ConfigFormat::Yaml,
        },
        providers_path: &["llm-pi-ai", "providers"],
        route_key: ROUTE_KEY,
        entry_template: EntryTemplate {
            api: "openai-responses",
            base_url_key: "baseURL",
            credential_ref_key: "apiKeyEnv",
            models_key: "models",
            model_id_key: "id",
        },
        credential: CredentialSpec {
            file: ConfigFileSpec {
                relative_path: ".credentials.yaml",
                format: ConfigFormat::Yaml,
            },
            key: "CODEXHUB_API_KEY",
            env_var: "CODEXHUB_API_KEY",
        },
        activation_path: &["agent-default-model"],
        adoption: AdoptionRule::AdoptLocalGatewayElseConflict,
    }
}

/// Descriptor registry. Only clients migrated to Provider Injection appear
/// here; takeover-era clients (codex, opencode, pi, omp, zcode) keep their
/// legacy predicates in `gateway.rs` until their campaign phase.
pub(crate) fn descriptor_for(client_id: &str) -> Option<InjectionDescriptor> {
    match client_id {
        "dsh" => Some(dsh_descriptor()),
        _ => None,
    }
}

/// A credential value that can never leak through formatting: `Debug` and
/// `Display` both render `***`. Logs, previews, errors, and test evidence
/// only ever see the masked form.
#[derive(Clone, PartialEq, Eq)]
pub(crate) struct MaskedSecret(String);

impl MaskedSecret {
    pub(crate) fn new(value: impl Into<String>) -> Self {
        Self(value.into())
    }

    /// The only way to reach the plaintext; call sites are writes into the
    /// credential file and nothing else.
    fn reveal(&self) -> &str {
        &self.0
    }
}

impl std::fmt::Debug for MaskedSecret {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("***")
    }
}

impl std::fmt::Display for MaskedSecret {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("***")
    }
}

/// Scrub every occurrence of `secret` from free-form `text` (logs,
/// evidence) before it leaves the process.
pub(crate) fn mask_secret_in_text(text: &str, secret: &MaskedSecret) -> String {
    if secret.reveal().is_empty() {
        return text.to_owned();
    }
    text.replace(secret.reveal(), "***")
}

/// Inputs for one inject operation. The caller (#430 wiring) supplies the
/// local Gateway base URL, the current local Gateway client key, and the
/// full enabled-model projection (Q5).
#[derive(Debug, Clone)]
pub(crate) struct InjectionRequest {
    pub base_url: String,
    pub api_key: MaskedSecret,
    pub models: Vec<String>,
}

/// Truthful record of one inject operation. Backup paths are disclosed;
/// credential values never appear here.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct InjectionOutcome {
    pub config_path: PathBuf,
    pub credential_path: PathBuf,
    pub config_backup: Option<PathBuf>,
    pub credential_backup: Option<PathBuf>,
    /// True when a pre-existing local-Gateway entry was adopted (Q4).
    pub adopted: bool,
    pub fingerprint: String,
}

/// Truthful record of one detach operation.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct DetachOutcome {
    pub config_changed: bool,
    pub credential_changed: bool,
    pub config_backup: Option<PathBuf>,
    pub credential_backup: Option<PathBuf>,
    /// True when the credential file held only the injected key and was
    /// removed rather than left as an empty mapping.
    pub credential_file_removed: bool,
}

/// Inject the CodexHub provider entry plus credential reference into the
/// client configuration under `client_root`. Backup + atomic write for both
/// files; foreign content is preserved; the activation key is never touched.
pub(crate) fn inject(
    client_root: &Path,
    descriptor: &InjectionDescriptor,
    request: &InjectionRequest,
) -> Result<InjectionOutcome, String> {
    require_yaml(descriptor)?;

    let config_path = descriptor.config_file.resolve(client_root);
    let credential_path = descriptor.credential.file.resolve(client_root);

    let mut config = read_yaml_mapping(&config_path)?;
    let providers = navigate_mapping_mut(&mut config, descriptor.providers_path)?;

    let route_key = Value::String(descriptor.route_key.to_owned());
    let adopted = match providers.get(&route_key) {
        Some(existing) => {
            match descriptor.adoption {
                AdoptionRule::AdoptLocalGatewayElseConflict => {
                    let base_url = existing
                        .get(descriptor.entry_template.base_url_key)
                        .and_then(Value::as_str);
                    match base_url {
                        Some(url) if is_local_gateway_url(url) => true,
                        _ => {
                            return Err(format!(
                                "{} config already has a '{}' provider entry not pointing at the local Gateway; refusing to overwrite user-owned configuration",
                                descriptor.client_id, descriptor.route_key
                            ));
                        }
                    }
                }
            }
        }
        None => false,
    };

    providers.insert(
        route_key,
        injected_entry(descriptor, &request.base_url, &request.models),
    );

    // Never touch the activation key: read-back only, asserted here so a
    // future edit to this engine fails loudly instead of silently flipping
    // the client's default-model selection.
    let activation_before = read_path(&config, descriptor.activation_path).cloned();

    let config_backup = backup_file(&config_path)?;
    crate::safe_file::write_text_atomic(&config_path, &serialize_yaml(&config)?)?;

    let mut credentials = read_yaml_mapping(&credential_path)?;
    credentials.insert(
        Value::String(descriptor.credential.key.to_owned()),
        Value::String(request.api_key.reveal().to_owned()),
    );
    let credential_backup = backup_file(&credential_path)?;
    crate::safe_file::write_text_atomic(&credential_path, &serialize_yaml(&credentials)?)?;

    debug_assert_eq!(
        read_yaml_mapping(&config_path)
            .ok()
            .and_then(|written| read_path(&written, descriptor.activation_path).cloned()),
        activation_before,
        "injection must never modify the activation key"
    );

    let fingerprint = block_fingerprint_value(
        descriptor,
        read_path(&config, &descriptor.injection_point()),
        true,
    );
    Ok(InjectionOutcome {
        config_path,
        credential_path,
        config_backup,
        credential_backup,
        adopted,
        fingerprint,
    })
}

/// Surgically detach: remove exactly the removal key set (the injected
/// provider entry plus the credential key). Foreign content and the
/// activation key are preserved; missing pieces are a no-op, not an error.
pub(crate) fn detach(
    client_root: &Path,
    descriptor: &InjectionDescriptor,
) -> Result<DetachOutcome, String> {
    require_yaml(descriptor)?;

    let config_path = descriptor.config_file.resolve(client_root);
    let credential_path = descriptor.credential.file.resolve(client_root);

    let mut config_backup = None;
    let mut config_changed = false;
    if config_path.exists() {
        let mut config = read_yaml_mapping(&config_path)?;
        if let Some(providers) = navigate_mapping_opt_mut(&mut config, descriptor.providers_path) {
            if providers
                .remove(Value::String(descriptor.route_key.to_owned()))
                .is_some()
            {
                config_changed = true;
                config_backup = backup_file(&config_path)?;
                crate::safe_file::write_text_atomic(&config_path, &serialize_yaml(&config)?)?;
            }
        }
    }

    let mut credential_backup = None;
    let mut credential_changed = false;
    let mut credential_file_removed = false;
    if credential_path.exists() {
        let mut credentials = read_yaml_mapping(&credential_path)?;
        if credentials
            .remove(Value::String(descriptor.credential.key.to_owned()))
            .is_some()
        {
            credential_changed = true;
            credential_backup = backup_file(&credential_path)?;
            if credentials.is_empty() {
                // The file held only the injected credential; remove it
                // rather than leaving an empty mapping behind.
                fs::remove_file(&credential_path).map_err(|error| {
                    format!(
                        "failed to remove now-empty credential file {}: {error}",
                        credential_path.display()
                    )
                })?;
                credential_file_removed = true;
            } else {
                crate::safe_file::write_text_atomic(
                    &credential_path,
                    &serialize_yaml(&credentials)?,
                )?;
            }
        }
    }

    Ok(DetachOutcome {
        config_changed,
        credential_changed,
        config_backup,
        credential_backup,
        credential_file_removed,
    })
}

/// Block fingerprint for readback (#433): presence and content hash of the
/// Injected Block only — foreign content is never validated. The credential
/// contributes presence, not value, so key rotation (#428) does not drift
/// the fingerprint. `None` when the block is absent.
pub(crate) fn block_fingerprint(
    client_root: &Path,
    descriptor: &InjectionDescriptor,
) -> Result<Option<String>, String> {
    require_yaml(descriptor)?;
    let config_path = descriptor.config_file.resolve(client_root);
    if !config_path.exists() {
        return Ok(None);
    }
    let config = read_yaml_mapping(&config_path)?;
    let entry = read_path(&config, &descriptor.injection_point());
    if entry.is_none() {
        return Ok(None);
    }
    let credential_path = descriptor.credential.file.resolve(client_root);
    let credential_present = if credential_path.exists() {
        read_yaml_mapping(&credential_path)?
            .get(Value::String(descriptor.credential.key.to_owned()))
            .is_some()
    } else {
        false
    };
    Ok(Some(block_fingerprint_value(
        descriptor,
        entry,
        credential_present,
    )))
}

/// Read-only activation state: the client's current global default-model
/// selection, surfaced as informational state only. Injection and detach
/// never write this key (Q1).
pub(crate) fn activation_state(
    client_root: &Path,
    descriptor: &InjectionDescriptor,
) -> Result<Option<String>, String> {
    require_yaml(descriptor)?;
    let config_path = descriptor.config_file.resolve(client_root);
    if !config_path.exists() {
        return Ok(None);
    }
    let config = read_yaml_mapping(&config_path)?;
    Ok(read_path(&config, descriptor.activation_path)
        .and_then(Value::as_str)
        .map(str::to_owned))
}

/// What readback expects the Injected Block to contain: the local Gateway
/// base URL and the enabled-model projection from the last inject/republish.
/// Deliberately carries NO credential value — readback validates the
/// credential reference and key presence only, so no code path here can ever
/// observe, compare, or report a secret (MaskedSecret discipline extended to
/// readback evidence).
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct ReadbackExpectation {
    pub base_url: String,
    pub models: Vec<String>,
}

/// Readback verdict for one injected client.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum ReadbackStatus {
    /// Injected Block present and fingerprint-identical to the expectation.
    Clean,
    /// Block absent or fingerprint-mismatched; `drift_details` explains
    /// exactly which owned piece diverged and how to repair it.
    Drift,
}

/// Truthful readback report (#433). Contains no credential values; safe to
/// log, surface in the UI, and attach to evidence.
#[derive(Debug, Clone, PartialEq, Eq)]
pub(crate) struct ReadbackReport {
    pub status: ReadbackStatus,
    pub block_present: bool,
    pub credential_key_present: bool,
    pub expected_fingerprint: String,
    pub actual_fingerprint: Option<String>,
    /// The client's global default-model selection. INFORMATIONAL ONLY
    /// (Q1): never an error condition, never part of the fingerprint.
    pub activation: Option<String>,
    /// Actionable, field-level drift explanations; empty when Clean.
    pub drift_details: Vec<String>,
}

/// Fingerprint the Injected Block must have for `base_url` + `models`.
/// Computed through the same canonical entry builder and hasher as inject,
/// so expectation and actual can never disagree about canonicalization.
pub(crate) fn expected_block_fingerprint(
    descriptor: &InjectionDescriptor,
    base_url: &str,
    models: &[String],
) -> String {
    let entry = injected_entry(descriptor, base_url, models);
    block_fingerprint_value(descriptor, Some(&entry), true)
}

/// Block-fingerprint readback for injected clients (#433): validates ONLY
/// the Injected Block (presence + fingerprint) plus the single credential
/// key's presence. Foreign providers, settings, and credential keys are
/// never validated and never reported as drift; the activation key is read
/// back as informational state, never as an error. Legacy takeover clients
/// keep byte-compare readback in `gateway.rs` until their migration phase.
pub(crate) fn verify_readback(
    client_root: &Path,
    descriptor: &InjectionDescriptor,
    expectation: &ReadbackExpectation,
) -> Result<ReadbackReport, String> {
    require_yaml(descriptor)?;

    let expected = expected_block_fingerprint(
        descriptor,
        &expectation.base_url,
        &expectation.models,
    );
    let actual = block_fingerprint(client_root, descriptor)?;
    let activation = activation_state(client_root, descriptor)?;

    let config_path = descriptor.config_file.resolve(client_root);
    let entry = if config_path.exists() {
        let config = read_yaml_mapping(&config_path)?;
        read_path(&config, &descriptor.injection_point()).cloned()
    } else {
        None
    };
    let credential_path = descriptor.credential.file.resolve(client_root);
    let credential_key_present = if credential_path.exists() {
        read_yaml_mapping(&credential_path)?
            .get(Value::String(descriptor.credential.key.to_owned()))
            .is_some()
    } else {
        false
    };

    let block_present = entry.is_some();
    let clean = actual.as_deref() == Some(expected.as_str());
    let drift_details = if clean {
        Vec::new()
    } else {
        field_drift_details(
            descriptor,
            entry.as_ref(),
            expectation,
            credential_key_present,
        )
    };

    Ok(ReadbackReport {
        status: if clean {
            ReadbackStatus::Clean
        } else {
            ReadbackStatus::Drift
        },
        block_present,
        credential_key_present,
        expected_fingerprint: expected,
        actual_fingerprint: actual,
        activation,
        drift_details,
    })
}

/// Field-level drift explanation. Only CodexHub-owned fields are compared,
/// and the credential contributes its env-var NAME and key PRESENCE — never
/// a value — so no detail string can contain a secret.
fn field_drift_details(
    descriptor: &InjectionDescriptor,
    entry: Option<&Value>,
    expectation: &ReadbackExpectation,
    credential_key_present: bool,
) -> Vec<String> {
    let template = descriptor.entry_template;
    let injection_point = descriptor.injection_point().join(".");
    let mut details = Vec::new();

    let Some(entry) = entry else {
        details.push(format!(
            "injected block absent: no '{}' provider entry at {injection_point}; reconnect the client to re-inject",
            descriptor.route_key
        ));
        if !credential_key_present {
            details.push(format!(
                "credential key '{}' missing from {}; reconnect the client to rewrite it",
                descriptor.credential.key, descriptor.credential.file.relative_path
            ));
        }
        return details;
    };

    let compare_str = |label: &str, found: Option<&str>, expected: &str, details: &mut Vec<String>| {
        let found = found.unwrap_or("");
        if found != expected {
            details.push(format!(
                "provider entry '{}' {label}: found '{found}', expected '{expected}'; re-apply to restore",
                descriptor.route_key
            ));
        }
    };
    compare_str(
        "api",
        entry.get("api").and_then(Value::as_str),
        template.api,
        &mut details,
    );
    compare_str(
        template.base_url_key,
        entry.get(template.base_url_key).and_then(Value::as_str),
        &expectation.base_url,
        &mut details,
    );
    compare_str(
        template.credential_ref_key,
        entry.get(template.credential_ref_key).and_then(Value::as_str),
        descriptor.credential.env_var,
        &mut details,
    );

    let mut found_models: Vec<&str> = entry
        .get(template.models_key)
        .and_then(Value::as_sequence)
        .map(|sequence| {
            sequence
                .iter()
                .filter_map(|model| model.get(template.model_id_key).and_then(Value::as_str))
                .collect()
        })
        .unwrap_or_default();
    found_models.sort_unstable();
    let mut expected_models: Vec<&str> = expectation.models.iter().map(String::as_str).collect();
    expected_models.sort_unstable();
    if found_models != expected_models {
        details.push(format!(
            "provider entry '{}' {}: found [{}], expected [{}]; re-apply to re-project the enabled model set",
            descriptor.route_key,
            template.models_key,
            found_models.join(", "),
            expected_models.join(", ")
        ));
    }

    if !credential_key_present {
        details.push(format!(
            "credential key '{}' missing from {}; re-apply to rewrite it",
            descriptor.credential.key, descriptor.credential.file.relative_path
        ));
    }
    details
}

fn require_yaml(descriptor: &InjectionDescriptor) -> Result<(), String> {
    for file in [descriptor.config_file, descriptor.credential.file] {
        if file.format != ConfigFormat::Yaml {
            return Err(format!(
                "injection engine has no {:?} strategy yet (client {})",
                file.format, descriptor.client_id
            ));
        }
    }
    Ok(())
}

/// Canonical injected entry: api, base URL, credential env-var reference,
/// and the full enabled-model projection. Deliberately takes no credential
/// value: the entry only ever references the bare env-var name.
fn injected_entry(descriptor: &InjectionDescriptor, base_url: &str, models: &[String]) -> Value {
    let template = descriptor.entry_template;
    let mut entry = Mapping::new();
    entry.insert(
        Value::String("api".to_owned()),
        Value::String(template.api.to_owned()),
    );
    entry.insert(
        Value::String(template.base_url_key.to_owned()),
        Value::String(base_url.to_owned()),
    );
    entry.insert(
        Value::String(template.credential_ref_key.to_owned()),
        Value::String(descriptor.credential.env_var.to_owned()),
    );
    let models = models
        .iter()
        .map(|id| {
            let mut model = Mapping::new();
            model.insert(
                Value::String(template.model_id_key.to_owned()),
                Value::String(id.clone()),
            );
            Value::Mapping(model)
        })
        .collect();
    entry.insert(
        Value::String(template.models_key.to_owned()),
        Value::Sequence(models),
    );
    Value::Mapping(entry)
}

/// Stable fingerprint over the Injected Block: api, base URL, credential
/// env-var reference, sorted model ids, and credential-key presence. Model
/// order and formatting differences do not drift the fingerprint; catalog
/// re-projection (Q5) does, deliberately.
fn block_fingerprint_value(
    descriptor: &InjectionDescriptor,
    entry: Option<&Value>,
    credential_present: bool,
) -> String {
    let template = descriptor.entry_template;
    let api = entry
        .and_then(|value| value.get("api"))
        .and_then(Value::as_str)
        .unwrap_or("");
    let base_url = entry
        .and_then(|value| value.get(template.base_url_key))
        .and_then(Value::as_str)
        .unwrap_or("");
    let credential_ref = entry
        .and_then(|value| value.get(template.credential_ref_key))
        .and_then(Value::as_str)
        .unwrap_or("");
    let mut models: Vec<&str> = entry
        .and_then(|value| value.get(template.models_key))
        .and_then(Value::as_sequence)
        .map(|sequence| {
            sequence
                .iter()
                .filter_map(|model| model.get(template.model_id_key).and_then(Value::as_str))
                .collect()
        })
        .unwrap_or_default();
    models.sort_unstable();

    let canonical = format!(
        "codexhub-injected-block/v1\nclient={}\napi={}\nbase_url={}\ncredential_ref={}\nmodels={}\ncredential_present={}",
        descriptor.client_id,
        api,
        base_url,
        credential_ref,
        models.join(","),
        credential_present,
    );
    let digest = Sha256::digest(canonical.as_bytes());
    let mut hex = String::with_capacity(digest.len() * 2);
    for byte in digest {
        hex.push_str(&format!("{byte:02x}"));
    }
    hex
}

fn is_local_gateway_url(url: &str) -> bool {
    let value = url.trim().trim_matches('"').trim_matches('\'');
    value.starts_with("http://127.0.0.1:")
        || value.starts_with("http://localhost:")
        || value.starts_with("http://[::1]:")
}

fn read_yaml_mapping(path: &Path) -> Result<Mapping, String> {
    if !path.exists() {
        return Ok(Mapping::new());
    }
    let text = fs::read_to_string(path)
        .map_err(|error| format!("failed to read {}: {error}", path.display()))?;
    let value: Value = serde_yaml::from_str(&text)
        .map_err(|error| format!("failed to parse {}: {error}", path.display()))?;
    match value {
        Value::Mapping(mapping) => Ok(mapping),
        Value::Null => Ok(Mapping::new()),
        _ => Err(format!(
            "{}: expected a YAML mapping at the top level",
            path.display()
        )),
    }
}

fn serialize_yaml(mapping: &Mapping) -> Result<String, String> {
    serde_yaml::to_string(mapping).map_err(|error| format!("failed to serialize YAML: {error}"))
}

fn read_path<'a>(root: &'a Mapping, path: &[&str]) -> Option<&'a Value> {
    let (first, rest) = path.split_first()?;
    let mut node: &Value = root.get(Value::String((*first).to_owned()))?;
    for segment in rest {
        node = node.get(Value::String((*segment).to_owned()))?;
    }
    Some(node)
}

fn navigate_mapping_mut<'a>(root: &'a mut Mapping, path: &[&str]) -> Result<&'a mut Mapping, String> {
    let mut current = root;
    for segment in path {
        let key = Value::String((*segment).to_owned());
        let entry = current.entry(key).or_insert_with(|| Value::Mapping(Mapping::new()));
        match entry {
            Value::Mapping(mapping) => current = mapping,
            _ => {
                return Err(format!(
                    "config key '{segment}' exists but is not a mapping; refusing to overwrite user-owned configuration"
                ));
            }
        }
    }
    Ok(current)
}

fn navigate_mapping_opt_mut<'a>(
    root: &'a mut Mapping,
    path: &[&str],
) -> Option<&'a mut Mapping> {
    let mut current = root;
    for segment in path {
        current = current
            .get_mut(Value::String((*segment).to_owned()))?
            .as_mapping_mut()?;
    }
    Some(current)
}

static BACKUP_COUNTER: AtomicU64 = AtomicU64::new(0);

/// Disaster-recovery backup (Q3): a sibling copy taken before any write.
/// Never contains anything the source file did not; call sites must still
/// keep credential values out of logs and evidence.
fn backup_file(path: &Path) -> Result<Option<PathBuf>, String> {
    if !path.exists() {
        return Ok(None);
    }
    let millis = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_err(|error| format!("system clock before epoch: {error}"))?
        .as_millis();
    let counter = BACKUP_COUNTER.fetch_add(1, Ordering::Relaxed);
    let file_name = path
        .file_name()
        .ok_or_else(|| format!("{} has no file name", path.display()))?
        .to_string_lossy();
    let backup_path = path.with_file_name(format!(
        "{file_name}.codexhub-backup-{millis}-{}-{counter}",
        std::process::id()
    ));
    fs::copy(path, &backup_path).map_err(|error| {
        format!(
            "failed to back up {} to {}: {error}",
            path.display(),
            backup_path.display()
        )
    })?;
    Ok(Some(backup_path))
}

/// Headless DSH lifecycle result for the backend command contract.
#[derive(Debug, Clone, Serialize)]
pub(crate) struct DshLifecycleReport {
    pub client_id: String,
    pub connected: bool,
    pub config_path: PathBuf,
    pub credential_path: PathBuf,
    pub activation: Option<String>,
    pub fingerprint: Option<String>,
    pub drift_details: Vec<String>,
    pub restart_required: String,
}

fn dsh_report(root: &Path, expectation: &ReadbackExpectation) -> Result<DshLifecycleReport, String> {
    let descriptor = dsh_descriptor();
    let readback = verify_readback(root, &descriptor, expectation)?;
    Ok(DshLifecycleReport {
        client_id: descriptor.client_id.to_owned(),
        connected: matches!(readback.status, ReadbackStatus::Clean),
        config_path: descriptor.config_file.resolve(root),
        credential_path: descriptor.credential.file.resolve(root),
        activation: readback.activation,
        fingerprint: readback.actual_fingerprint,
        drift_details: readback.drift_details,
        restart_required: "none".to_owned(),
    })
}

pub(crate) fn dsh_connect(root: &Path, base_url: String, api_key: MaskedSecret, models: Vec<String>) -> Result<DshLifecycleReport, String> {
    let descriptor = dsh_descriptor();
    let expectation = ReadbackExpectation { base_url: base_url.clone(), models: models.clone() };
    inject(root, &descriptor, &InjectionRequest { base_url, api_key, models })?;
    dsh_report(root, &expectation)
}

pub(crate) fn dsh_disconnect(root: &Path, expectation: &ReadbackExpectation) -> Result<DshLifecycleReport, String> {
    let descriptor = dsh_descriptor();
    detach(root, &descriptor)?;
    let mut report = dsh_report(root, expectation)?;
    report.connected = false;
    Ok(report)
}

pub(crate) fn dsh_readback(root: &Path, expectation: &ReadbackExpectation) -> Result<DshLifecycleReport, String> {
    dsh_report(root, expectation)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Read;

    static TEMP_DIR_COUNTER: AtomicU64 = AtomicU64::new(0);

    fn unique_temp_dir(prefix: &str) -> PathBuf {
        let millis = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_millis();
        let counter = TEMP_DIR_COUNTER.fetch_add(1, Ordering::Relaxed);
        std::env::temp_dir().join(format!(
            "{prefix}-{millis}-{}-{counter}",
            std::process::id()
        ))
    }

    fn dsh_root(prefix: &str) -> PathBuf {
        let root = unique_temp_dir(prefix);
        fs::create_dir_all(&root).unwrap();
        root
    }

    fn request() -> InjectionRequest {
        InjectionRequest {
            base_url: "http://127.0.0.1:9109/v1".to_owned(),
            api_key: MaskedSecret::new("cx-test-key-0000-SECRET"),
            models: vec!["gpt-5.5".to_owned(), "gpt-5.5-codex".to_owned()],
        }
    }

    fn read_file(path: &Path) -> String {
        let mut text = String::new();
        fs::File::open(path)
            .unwrap()
            .read_to_string(&mut text)
            .unwrap();
        text
    }

    /// serde_yaml has no JSON-pointer; walk "/a/b/c" path segments instead.
    fn at<'a>(value: &'a Value, path: &str) -> Option<&'a Value> {
        let mut node = value;
        for segment in path.split('/').filter(|segment| !segment.is_empty()) {
            node = node.get(segment)?;
        }
        Some(node)
    }

    // --- descriptor field tests (data, not code branches) ---

    #[test]
    fn dsh_descriptor_declares_config_file_and_format() {
        let descriptor = dsh_descriptor();
        assert_eq!(descriptor.client_id, "dsh");
        assert_eq!(descriptor.client_home_relative, ".dsh");
        assert_eq!(descriptor.config_file.relative_path, "settings.yaml");
        assert_eq!(descriptor.config_file.format, ConfigFormat::Yaml);
    }

    #[test]
    fn dsh_descriptor_declares_injection_point_and_route_key() {
        let descriptor = dsh_descriptor();
        assert_eq!(descriptor.providers_path, ["llm-pi-ai", "providers"]);
        assert_eq!(descriptor.route_key, "codexhub");
        assert_eq!(
            descriptor.injection_point(),
            ["llm-pi-ai", "providers", "codexhub"]
        );
    }

    #[test]
    fn dsh_descriptor_declares_entry_template() {
        let template = dsh_descriptor().entry_template;
        assert_eq!(template.api, "openai-responses");
        assert_eq!(template.base_url_key, "baseURL");
        assert_eq!(template.credential_ref_key, "apiKeyEnv");
        assert_eq!(template.models_key, "models");
        assert_eq!(template.model_id_key, "id");
    }

    #[test]
    fn dsh_descriptor_declares_credential_file_and_key() {
        let credential = dsh_descriptor().credential;
        assert_eq!(credential.file.relative_path, ".credentials.yaml");
        assert_eq!(credential.file.format, ConfigFormat::Yaml);
        assert_eq!(credential.key, "CODEXHUB_API_KEY");
        assert_eq!(credential.env_var, "CODEXHUB_API_KEY");
    }

    #[test]
    fn dsh_descriptor_declares_read_only_activation_key() {
        assert_eq!(dsh_descriptor().activation_path, ["agent-default-model"]);
    }

    #[test]
    fn dsh_descriptor_declares_adoption_rule() {
        assert_eq!(
            dsh_descriptor().adoption,
            AdoptionRule::AdoptLocalGatewayElseConflict
        );
    }

    #[test]
    fn dsh_descriptor_declares_removal_key_set() {
        let removal = dsh_descriptor().removal_key_set();
        assert_eq!(
            removal,
            [
                "llm-pi-ai.providers.codexhub".to_owned(),
                ".credentials.yaml:CODEXHUB_API_KEY".to_owned()
            ]
        );
    }

    #[test]
    fn registry_returns_only_injection_migrated_clients() {
        assert!(descriptor_for("dsh").is_some());
        // Takeover-era clients stay on legacy predicates until migration.
        for legacy in ["codex", "opencode", "pi", "omp", "zcode"] {
            assert!(descriptor_for(legacy).is_none(), "{legacy} must not migrate implicitly");
        }
        assert!(descriptor_for("unknown").is_none());
    }

    #[test]
    fn config_file_resolution_is_relative_to_client_root() {
        let descriptor = dsh_descriptor();
        let root = Path::new("/tmp/example-home/.dsh");
        assert_eq!(
            descriptor.config_file.resolve(root),
            PathBuf::from("/tmp/example-home/.dsh/settings.yaml")
        );
        assert_eq!(
            descriptor.credential.file.resolve(root),
            PathBuf::from("/tmp/example-home/.dsh/.credentials.yaml")
        );
    }

    #[test]
    fn engine_rejects_formats_without_a_strategy() {
        let mut descriptor = dsh_descriptor();
        descriptor.config_file.format = ConfigFormat::Json;
        let root = dsh_root("codexhub-injection-json");
        let error = inject(&root, &descriptor, &request()).unwrap_err();
        assert!(error.contains("Json"), "unexpected error: {error}");
        let error = block_fingerprint(&root, &descriptor).unwrap_err();
        assert!(error.contains("Json"), "unexpected error: {error}");
    }

    // --- credential masking ---

    #[test]
    fn masked_secret_never_renders_plaintext() {
        let secret = MaskedSecret::new("cx-test-key-0000-SECRET");
        assert_eq!(format!("{secret}"), "***");
        assert_eq!(format!("{secret:?}"), "***");
        let request = request();
        let debug = format!("{request:?}");
        assert!(!debug.contains("cx-test-key-0000-SECRET"), "debug leaked: {debug}");
    }

    #[test]
    fn mask_secret_in_text_scrubs_every_occurrence() {
        let secret = MaskedSecret::new("cx-test-key-0000-SECRET");
        let text = "key=cx-test-key-0000-SECRET again cx-test-key-0000-SECRET end";
        let masked = mask_secret_in_text(text, &secret);
        assert_eq!(masked, "key=*** again *** end");
    }

    // --- DSH inject ---

    #[test]
    fn inject_creates_entry_and_credential_on_fresh_install() {
        let root = dsh_root("codexhub-injection-fresh");
        let descriptor = dsh_descriptor();
        let outcome = inject(&root, &descriptor, &request()).unwrap();

        assert!(!outcome.adopted);
        assert_eq!(outcome.config_backup, None);
        assert_eq!(outcome.credential_backup, None);

        let config = read_file(&outcome.config_path);
        let parsed: Value = serde_yaml::from_str(&config).unwrap();
        let entry = at(&parsed, "/llm-pi-ai/providers/codexhub").expect("injected entry missing");
        assert_eq!(entry.get("api").unwrap(), "openai-responses");
        assert_eq!(entry.get("baseURL").unwrap(), "http://127.0.0.1:9109/v1");
        assert_eq!(entry.get("apiKeyEnv").unwrap(), "CODEXHUB_API_KEY");
        let models = entry.get("models").unwrap().as_sequence().unwrap();
        let ids: Vec<&str> = models
            .iter()
            .map(|model| model.get("id").unwrap().as_str().unwrap())
            .collect();
        assert_eq!(ids, ["gpt-5.5", "gpt-5.5-codex"]);

        // Credential: bare env-var name in the entry, value only in the
        // credential file.
        assert!(!config.contains("cx-test-key-0000-SECRET"));
        let credentials = read_file(&outcome.credential_path);
        assert!(credentials.contains("CODEXHUB_API_KEY: cx-test-key-0000-SECRET"));

        assert_eq!(
            block_fingerprint(&root, &descriptor).unwrap(),
            Some(outcome.fingerprint)
        );
    }

    #[test]
    fn inject_preserves_foreign_content_and_never_flips_activation() {
        let root = dsh_root("codexhub-injection-foreign");
        let descriptor = dsh_descriptor();
        let original = concat!(
            "agent-default-model: anthropic/claude-opus
",
            "llm-pi-ai:\n",
            "  providers:\n",
            "    anthropic:\n",
            "      api: anthropic-messages\n",
            "      baseURL: https://api.anthropic.example\n",
            "  stream: true\n",
            "presets:\n",
            "  standard: {}\n"
        );
        fs::write(root.join("settings.yaml"), original).unwrap();

        let outcome = inject(&root, &descriptor, &request()).unwrap();
        let backup = outcome.config_backup.expect("pre-existing file must be backed up");
        assert_eq!(read_file(&backup), original);

        let parsed: Value =
            serde_yaml::from_str(&read_file(&outcome.config_path)).unwrap();
        assert_eq!(
            at(&parsed, "/agent-default-model").unwrap(),
            "anthropic/claude-opus",
            "injection must never touch the activation key"
        );
        assert_eq!(
            at(&parsed, "/llm-pi-ai/providers/anthropic/baseURL").unwrap(),
            "https://api.anthropic.example"
        );
        assert_eq!(at(&parsed, "/llm-pi-ai/stream").and_then(Value::as_bool), Some(true));
        assert!(at(&parsed, "/presets/standard").is_some());
        assert!(at(&parsed, "/llm-pi-ai/providers/codexhub").is_some());
        assert_eq!(
            activation_state(&root, &descriptor).unwrap(),
            Some("anthropic/claude-opus".to_owned())
        );
    }

    #[test]
    fn inject_adopts_preexisting_local_gateway_entry() {
        let root = dsh_root("codexhub-injection-adopt");
        let descriptor = dsh_descriptor();
        fs::write(
            root.join("settings.yaml"),
            concat!(
                "llm-pi-ai:\n",
                "  providers:\n",
                "    codexhub:\n",
                "      api: openai-responses\n",
                "      baseURL: http://localhost:9109/v1\n"
            ),
        )
        .unwrap();

        let outcome = inject(&root, &descriptor, &request()).unwrap();
        assert!(outcome.adopted, "local-Gateway entry must be adopted, not a conflict");
        let parsed: Value =
            serde_yaml::from_str(&read_file(&outcome.config_path)).unwrap();
        assert_eq!(
            at(&parsed, "/llm-pi-ai/providers/codexhub/baseURL").unwrap(),
            "http://127.0.0.1:9109/v1",
            "adopted entry is canonicalized to the current request"
        );
    }

    #[test]
    fn inject_refuses_foreign_entry_named_codexhub() {
        let root = dsh_root("codexhub-injection-conflict");
        let descriptor = dsh_descriptor();
        let original = concat!(
            "llm-pi-ai:\n",
            "  providers:\n",
            "    codexhub:\n",
            "      api: openai-responses\n",
            "      baseURL: https://remote.example.invalid/v1\n"
        );
        fs::write(root.join("settings.yaml"), original).unwrap();

        let error = inject(&root, &descriptor, &request()).unwrap_err();
        assert!(error.contains("codexhub"), "unexpected error: {error}");
        assert_eq!(
            read_file(&root.join("settings.yaml")),
            original,
            "conflict must leave the user file untouched"
        );
        assert!(!root.join(".credentials.yaml").exists());
    }

    #[test]
    fn inject_is_idempotent_with_stable_fingerprint() {
        let root = dsh_root("codexhub-injection-idem");
        let descriptor = dsh_descriptor();
        let first = inject(&root, &descriptor, &request()).unwrap();
        let second = inject(&root, &descriptor, &request()).unwrap();
        assert!(second.adopted, "re-inject adopts its own block");
        assert_eq!(first.fingerprint, second.fingerprint);
        assert!(second.config_backup.is_some());
        assert!(second.credential_backup.is_some());
    }

    #[test]
    fn inject_preserves_foreign_credential_keys() {
        let root = dsh_root("codexhub-injection-creds");
        let descriptor = dsh_descriptor();
        fs::write(
            root.join(".credentials.yaml"),
            "OPENAI_API_KEY: sk-user-owned-123\n",
        )
        .unwrap();

        let outcome = inject(&root, &descriptor, &request()).unwrap();
        let backup = outcome.credential_backup.expect("credential backup missing");
        assert_eq!(read_file(&backup), "OPENAI_API_KEY: sk-user-owned-123\n");

        let parsed: Value =
            serde_yaml::from_str(&read_file(&outcome.credential_path)).unwrap();
        assert_eq!(
            parsed.get("OPENAI_API_KEY").unwrap(),
            "sk-user-owned-123",
            "foreign credential keys must survive"
        );
        assert_eq!(
            parsed.get("CODEXHUB_API_KEY").unwrap(),
            "cx-test-key-0000-SECRET"
        );
    }

    // --- DSH detach ---

    #[test]
    fn detach_removes_exactly_the_injected_block() {
        let root = dsh_root("codexhub-injection-detach");
        let descriptor = dsh_descriptor();
        fs::write(
            root.join("settings.yaml"),
            concat!(
                "agent-default-model: codexhub/gpt-5.5\n",
                "llm-pi-ai:\n",
                "  providers:\n",
                "    anthropic:\n",
                "      api: anthropic-messages\n"
            ),
        )
        .unwrap();
        fs::write(
            root.join(".credentials.yaml"),
            "OPENAI_API_KEY: sk-user-owned-123\n",
        )
        .unwrap();
        inject(&root, &descriptor, &request()).unwrap();

        let outcome = detach(&root, &descriptor).unwrap();
        assert!(outcome.config_changed);
        assert!(outcome.credential_changed);
        assert!(!outcome.credential_file_removed);
        assert!(outcome.config_backup.is_some());
        assert!(outcome.credential_backup.is_some());

        let parsed: Value =
            serde_yaml::from_str(&read_file(&root.join("settings.yaml"))).unwrap();
        assert!(at(&parsed, "/llm-pi-ai/providers/codexhub").is_none());
        assert!(at(&parsed, "/llm-pi-ai/providers/anthropic").is_some());
        assert_eq!(
            at(&parsed, "/agent-default-model").unwrap(),
            "codexhub/gpt-5.5",
            "detach must never touch the activation key"
        );

        let credentials: Value =
            serde_yaml::from_str(&read_file(&root.join(".credentials.yaml"))).unwrap();
        assert!(credentials.get("CODEXHUB_API_KEY").is_none());
        assert_eq!(credentials.get("OPENAI_API_KEY").unwrap(), "sk-user-owned-123");

        assert_eq!(block_fingerprint(&root, &descriptor).unwrap(), None);
    }

    #[test]
    fn detach_removes_credential_file_when_it_held_only_our_key() {
        let root = dsh_root("codexhub-injection-detach-empty");
        let descriptor = dsh_descriptor();
        inject(&root, &descriptor, &request()).unwrap();
        assert!(root.join(".credentials.yaml").exists());

        let outcome = detach(&root, &descriptor).unwrap();
        assert!(outcome.credential_changed);
        assert!(outcome.credential_file_removed);
        assert!(!root.join(".credentials.yaml").exists());
    }

    #[test]
    fn detach_on_absent_block_is_a_truthful_noop() {
        let root = dsh_root("codexhub-injection-detach-noop");
        let descriptor = dsh_descriptor();
        let outcome = detach(&root, &descriptor).unwrap();
        assert!(!outcome.config_changed);
        assert!(!outcome.credential_changed);
        assert_eq!(outcome.config_backup, None);
        assert_eq!(outcome.credential_backup, None);
    }

    // --- fingerprint ---

    #[test]
    fn fingerprint_is_absent_without_block_and_sensitive_to_projection() {
        let root = dsh_root("codexhub-injection-fp");
        let descriptor = dsh_descriptor();
        assert_eq!(block_fingerprint(&root, &descriptor).unwrap(), None);

        inject(&root, &descriptor, &request()).unwrap();
        let original = block_fingerprint(&root, &descriptor).unwrap().unwrap();

        // Model order in the request does not drift the fingerprint.
        let mut reordered = request();
        reordered.models.reverse();
        inject(&root, &descriptor, &reordered).unwrap();
        assert_eq!(
            block_fingerprint(&root, &descriptor).unwrap().as_deref(),
            Some(original.as_str())
        );

        // A changed enabled-model projection re-fingerprints (Q5).
        let mut extended = request();
        extended.models.push("gpt-5.6".to_owned());
        inject(&root, &descriptor, &extended).unwrap();
        let changed = block_fingerprint(&root, &descriptor).unwrap().unwrap();
        assert_ne!(changed, original);
    }

    #[test]
    fn fingerprint_survives_credential_rotation() {
        let root = dsh_root("codexhub-injection-rotate");
        let descriptor = dsh_descriptor();
        inject(&root, &descriptor, &request()).unwrap();
        let before = block_fingerprint(&root, &descriptor).unwrap();

        let mut rotated = request();
        rotated.api_key = MaskedSecret::new("cx-test-key-9999-ROTATED");
        inject(&root, &descriptor, &rotated).unwrap();

        assert_eq!(
            block_fingerprint(&root, &descriptor).unwrap(),
            before,
            "key rotation rewrites one credential key only; the block fingerprint must not drift"
        );
        let credentials = read_file(&root.join(".credentials.yaml"));
        assert!(credentials.contains("cx-test-key-9999-ROTATED"));
        assert!(!credentials.contains("cx-test-key-0000-SECRET"));
    }

    #[test]
    fn activation_state_is_none_when_unset() {
        let root = dsh_root("codexhub-injection-activation");
        let descriptor = dsh_descriptor();
        assert_eq!(activation_state(&root, &descriptor).unwrap(), None);
        inject(&root, &descriptor, &request()).unwrap();
        assert_eq!(
            activation_state(&root, &descriptor).unwrap(),
            None,
            "injection must not activate"
        );
    }

    // --- block-fingerprint readback (#433) ---

    fn expectation() -> ReadbackExpectation {
        ReadbackExpectation {
            base_url: "http://127.0.0.1:9109/v1".to_owned(),
            models: vec!["gpt-5.5".to_owned(), "gpt-5.5-codex".to_owned()],
        }
    }

    #[test]
    fn readback_is_clean_right_after_inject() {
        let root = dsh_root("codexhub-readback-clean");
        let descriptor = dsh_descriptor();
        let outcome = inject(&root, &descriptor, &request()).unwrap();

        let report = verify_readback(&root, &descriptor, &expectation()).unwrap();
        assert_eq!(report.status, ReadbackStatus::Clean);
        assert!(report.block_present);
        assert!(report.credential_key_present);
        assert_eq!(report.actual_fingerprint.as_deref(), Some(outcome.fingerprint.as_str()));
        assert_eq!(
            report.expected_fingerprint,
            expected_block_fingerprint(&descriptor, "http://127.0.0.1:9109/v1", &expectation().models)
        );
        assert_eq!(report.actual_fingerprint, Some(report.expected_fingerprint.clone()));
        assert!(report.drift_details.is_empty(), "clean report must have no drift details");
        assert_eq!(report.activation, None);
    }

    #[test]
    fn readback_ignores_foreign_provider_and_credential_churn() {
        let root = dsh_root("codexhub-readback-foreign");
        let descriptor = dsh_descriptor();
        fs::write(
            root.join("settings.yaml"),
            concat!(
                "llm-pi-ai:\n",
                "  providers:\n",
                "    anthropic:\n",
                "      api: anthropic-messages\n",
                "      baseURL: https://api.anthropic.example\n"
            ),
        )
        .unwrap();
        fs::write(
            root.join(".credentials.yaml"),
            "OPENAI_API_KEY: sk-user-owned-123\n",
        )
        .unwrap();
        inject(&root, &descriptor, &request()).unwrap();

        // User rewrites their own providers and rotates their own keys.
        fs::write(
            root.join("settings.yaml"),
            concat!(
                "agent-default-model: openai/gpt-5\n",
                "llm-pi-ai:\n",
                "  providers:\n",
                "    openai:\n",
                "      api: openai-responses\n",
                "      baseURL: https://api.openai.example/v1\n",
                "    codexhub:\n",
                "      api: openai-responses\n",
                "      baseURL: http://127.0.0.1:9109/v1\n",
                "      apiKeyEnv: CODEXHUB_API_KEY\n",
                "      models:\n",
                "        - id: gpt-5.5-codex\n",
                "        - id: gpt-5.5\n"
            ),
        )
        .unwrap();
        fs::write(
            root.join(".credentials.yaml"),
            concat!(
                "ANTHROPIC_API_KEY: sk-ant-user-owned\n",
                "CODEXHUB_API_KEY: cx-test-key-0000-SECRET\n"
            ),
        )
        .unwrap();

        let report = verify_readback(&root, &descriptor, &expectation()).unwrap();
        assert_eq!(
            report.status,
            ReadbackStatus::Clean,
            "foreign churn must never read as drift: {:?}",
            report.drift_details
        );
        // The user's own activation choice is surfaced informationally.
        assert_eq!(report.activation, Some("openai/gpt-5".to_owned()));
    }

    #[test]
    fn readback_reports_deleted_block_with_actionable_detail() {
        let root = dsh_root("codexhub-readback-deleted");
        let descriptor = dsh_descriptor();
        inject(&root, &descriptor, &request()).unwrap();
        fs::write(root.join("settings.yaml"), "llm-pi-ai:\n  providers: {}\n").unwrap();

        let report = verify_readback(&root, &descriptor, &expectation()).unwrap();
        assert_eq!(report.status, ReadbackStatus::Drift);
        assert!(!report.block_present);
        assert!(report.credential_key_present, "credential survives; only the entry was deleted");
        assert_eq!(report.actual_fingerprint, None);
        let detail = report.drift_details.join("\n");
        assert!(detail.contains("llm-pi-ai.providers.codexhub"), "detail must name the injection point: {detail}");
        assert!(detail.contains("re-inject"), "detail must be actionable: {detail}");
    }

    #[test]
    fn readback_reports_missing_config_file_as_absent_block() {
        let root = dsh_root("codexhub-readback-nofile");
        let descriptor = dsh_descriptor();
        let report = verify_readback(&root, &descriptor, &expectation()).unwrap();
        assert_eq!(report.status, ReadbackStatus::Drift);
        assert!(!report.block_present);
        assert!(!report.credential_key_present);
        assert!(report.drift_details.iter().any(|detail| detail.contains("credential key 'CODEXHUB_API_KEY' missing")));
    }

    #[test]
    fn readback_reports_tampered_fields_individually() {
        let root = dsh_root("codexhub-readback-tampered");
        let descriptor = dsh_descriptor();
        inject(&root, &descriptor, &request()).unwrap();
        fs::write(
            root.join("settings.yaml"),
            concat!(
                "llm-pi-ai:\n",
                "  providers:\n",
                "    codexhub:\n",
                "      api: openai-completions\n",
                "      baseURL: http://127.0.0.1:9999/v1\n",
                "      apiKeyEnv: TAMPERED_VAR\n",
                "      models:\n",
                "        - id: gpt-5.5\n"
            ),
        )
        .unwrap();

        let report = verify_readback(&root, &descriptor, &expectation()).unwrap();
        assert_eq!(report.status, ReadbackStatus::Drift);
        assert!(report.block_present);
        assert_ne!(report.actual_fingerprint, Some(report.expected_fingerprint.clone()));
        let detail = report.drift_details.join("\n");
        assert!(detail.contains("api: found 'openai-completions', expected 'openai-responses'"), "{detail}");
        assert!(detail.contains("baseURL: found 'http://127.0.0.1:9999/v1', expected 'http://127.0.0.1:9109/v1'"), "{detail}");
        assert!(detail.contains("apiKeyEnv: found 'TAMPERED_VAR', expected 'CODEXHUB_API_KEY'"), "{detail}");
        assert!(detail.contains("found [gpt-5.5], expected [gpt-5.5, gpt-5.5-codex]"), "{detail}");
    }

    #[test]
    fn readback_reports_missing_credential_key_without_exposing_values() {
        let root = dsh_root("codexhub-readback-credkey");
        let descriptor = dsh_descriptor();
        inject(&root, &descriptor, &request()).unwrap();
        // User deletes only our credential key, keeps their own.
        fs::write(root.join(".credentials.yaml"), "OPENAI_API_KEY: sk-user-owned-123\n").unwrap();

        let report = verify_readback(&root, &descriptor, &expectation()).unwrap();
        assert_eq!(report.status, ReadbackStatus::Drift);
        assert!(report.block_present);
        assert!(!report.credential_key_present);
        let detail = report.drift_details.join("\n");
        assert!(detail.contains("credential key 'CODEXHUB_API_KEY' missing"), "{detail}");
        assert!(!detail.contains("sk-user-owned-123"), "foreign credential value leaked: {detail}");
    }

    #[test]
    fn readback_stays_clean_across_credential_rotation() {
        let root = dsh_root("codexhub-readback-rotation");
        let descriptor = dsh_descriptor();
        inject(&root, &descriptor, &request()).unwrap();
        // Key rotation (#428) rewrites the value only; readback must not drift.
        fs::write(
            root.join(".credentials.yaml"),
            "CODEXHUB_API_KEY: cx-test-key-9999-ROTATED\n",
        )
        .unwrap();

        let report = verify_readback(&root, &descriptor, &expectation()).unwrap();
        assert_eq!(report.status, ReadbackStatus::Clean);
        assert!(report.credential_key_present);
    }

    #[test]
    fn readback_never_treats_activation_as_error() {
        let root = dsh_root("codexhub-readback-activation");
        let descriptor = dsh_descriptor();
        fs::write(
            root.join("settings.yaml"),
            "agent-default-model: anthropic/claude-opus\n",
        )
        .unwrap();
        inject(&root, &descriptor, &request()).unwrap();

        let report = verify_readback(&root, &descriptor, &expectation()).unwrap();
        assert_eq!(report.status, ReadbackStatus::Clean);
        assert_eq!(
            report.activation,
            Some("anthropic/claude-opus".to_owned()),
            "activation is informational state, never an error condition"
        );
    }

    #[test]
    fn readback_report_never_contains_credential_values() {
        let root = dsh_root("codexhub-readback-masking");
        let descriptor = dsh_descriptor();
        inject(&root, &descriptor, &request()).unwrap();
        // Tamper everything owned so every drift detail fires.
        fs::write(root.join("settings.yaml"), "llm-pi-ai:\n  providers: {}\n").unwrap();
        fs::remove_file(root.join(".credentials.yaml")).unwrap();

        let report = verify_readback(&root, &descriptor, &expectation()).unwrap();
        assert_eq!(report.status, ReadbackStatus::Drift);
        let evidence = format!("{report:?}") + &report.drift_details.join("\n");
        assert!(
            !evidence.contains("cx-test-key-0000-SECRET"),
            "credential value must never appear in readback evidence: {evidence}"
        );
    }

    #[test]
    fn readback_fails_truthfully_on_unparseable_config() {
        let root = dsh_root("codexhub-readback-broken");
        let descriptor = dsh_descriptor();
        fs::write(root.join("settings.yaml"), "llm-pi-ai: [unclosed\n").unwrap();

        let error = verify_readback(&root, &descriptor, &expectation()).unwrap_err();
        assert!(error.contains("failed to parse"), "unexpected error: {error}");
    }
}
