use crate::{
    config, runtime_paths, safe_file, CatalogVisibility, MetadataProvenance, Model, ModelPricing,
    Settings, UpstreamFormat,
};
use reqwest::blocking::Client;
use reqwest::header::{ACCEPT, AUTHORIZATION, CONTENT_TYPE};
use serde::Deserialize;
use serde_json::{json, Map, Value};
use std::collections::{HashMap, HashSet};
use std::fs;
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdout, Command, Stdio};
use std::sync::{mpsc, OnceLock};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

const DISCOVERY_TIMEOUT: Duration = Duration::from_secs(20);
const MODEL_TEST_TIMEOUT: Duration = Duration::from_secs(8);
const CODEX_APP_SERVER_MODEL_LIST_TIMEOUT: Duration = Duration::from_secs(8);
// Codex CLI 0.146 may answer model/list before it atomically publishes the
// native context cache.  A response that already carries context is a safe
// direct snapshot and returns immediately; a context-less response needs this
// bounded grace before the catalog can resolve a safe Official budget.
const CODEX_APP_SERVER_NATIVE_CACHE_GRACE: Duration = Duration::from_secs(20);
const MAX_VISIBILITY_DIAGNOSTIC_COUNT: u64 = 100;
const GENERATED_CATALOG_FILE: &str = "codexhub-model-catalog.json";
const LEGACY_GENERATED_CATALOG_FILE: &str = "codex-proxy-official-ollama.json";
const RESOLVED_MODEL_LIMITS_JSON: &str = include_str!("../../config/resolved_model_limits.json");
const OFFICIAL_CATALOG_METADATA_JSON: &str =
    include_str!("../../config/official_model_catalog_metadata.json");
const PINNED_OFFICIAL_MODEL_IDS: &[&str] = &[
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "gpt-5.5",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gpt-5.3-codex-spark",
];
const PINNED_OFFICIAL_PLANNER_FIELDS: &[&str] = &[
    "prefer_websockets",
    "tool_mode",
    "multi_agent_version",
    "use_responses_lite",
];
const PINNED_OFFICIAL_LITE_ONLY_FIELDS: &[&str] = &["use_responses_lite"];
type PinnedOfficialCatalogMetadata = HashMap<String, Map<String, Value>>;
type PinnedOfficialCatalogMetadataResult = Result<PinnedOfficialCatalogMetadata, String>;
static PINNED_OFFICIAL_CATALOG_METADATA: OnceLock<PinnedOfficialCatalogMetadataResult> =
    OnceLock::new();
const RESPONSE_ENDPOINT_SUFFIXES: &[&str] = &["/responses", "/response"];
const KNOWN_PROVIDER_ENDPOINT_SUFFIXES: &[&str] = &[
    "/chat/completions",
    "/responses",
    "/response",
    "/messages",
    "/models",
];

/// Acquire a new Direct Official snapshot.  Callers that need to make refresh
/// scheduling decisions must use this path so a cache fallback is never
/// mistaken for a successful Direct acquisition.
pub(crate) fn refresh_official_models_direct() -> Result<Vec<Model>, String> {
    let paths = ModelPaths::runtime()?;
    let runner = ProcessAppServerModelListRunner;
    refresh_official_models_direct_with_runner(&paths, &runner)
}

pub(crate) fn list_cached_official_models() -> Result<Vec<Model>, String> {
    let paths = ModelPaths::runtime()?;
    read_official_subscription_models_from_cache(&paths)
}

pub(crate) fn cached_official_snapshot_timestamp() -> Option<u64> {
    let paths = ModelPaths::runtime().ok()?;
    let payload = load_json_file(&paths.official_subscription_cache_path()).ok()?;
    payload
        .get("fetched_at")
        .and_then(Value::as_u64)
        .or_else(|| {
            payload
                .get("fetched_at")
                .and_then(Value::as_str)
                .and_then(|value| value.parse::<u64>().ok())
        })
}

pub fn discover_provider_models(base_url: &str, api_key: &str) -> Result<Vec<Model>, String> {
    discover_provider_models_with_timeout(base_url, api_key, DISCOVERY_TIMEOUT)
}

pub fn probe_upstream_format(
    base_url: &str,
    api_key: &str,
    model: Option<&str>,
) -> Result<Value, String> {
    let api_key = resolve_gateway_api_key(base_url, api_key)?.unwrap_or_default();
    let paths = ModelPaths::runtime()?;
    let python = find_python();
    let script = paths.upstream_format_probe_script();
    if !script.exists() {
        return Err(format!(
            "upstream format probe script not found: {}",
            script.display()
        ));
    }

    let mut command = Command::new(&python);
    command
        .arg(&script)
        .arg("--base-url")
        .arg(base_url)
        .env("CODEXHUB_PROBE_API_KEY", api_key);
    if let Some(model) = model.map(str::trim).filter(|value| !value.is_empty()) {
        command.arg("--model").arg(model);
    }
    configure_no_window(&mut command);

    let output = command
        .output()
        .map_err(|error| format!("failed to start upstream format probe: {error}"))?;
    let stdout = String::from_utf8_lossy(&output.stdout);
    if !output.status.success() {
        return Err(format!(
            "upstream format probe failed with {}\nstdout:\n{}\nstderr:\n{}",
            format_exit_code(output.status.code()),
            stdout.trim_end(),
            String::from_utf8_lossy(&output.stderr).trim_end()
        ));
    }

    serde_json::from_str(stdout.trim())
        .map_err(|error| format!("upstream format probe returned invalid JSON: {error}"))
}

pub fn test_model_endpoint(
    base_url: &str,
    api_key: &str,
    model: &str,
    upstream_format: &UpstreamFormat,
) -> Result<Value, String> {
    test_model_endpoint_with_timeout(
        base_url,
        api_key,
        model,
        upstream_format,
        MODEL_TEST_TIMEOUT,
    )
}

fn test_model_endpoint_with_timeout(
    base_url: &str,
    api_key: &str,
    model: &str,
    upstream_format: &UpstreamFormat,
    timeout: Duration,
) -> Result<Value, String> {
    let model = model.trim();
    if model.is_empty() {
        return Err("model is required for endpoint connectivity test".to_string());
    }

    let api_key = resolve_gateway_api_key(base_url, api_key)?;
    let (format_id, label, path, payload) = model_test_payload(model, upstream_format);
    let endpoint = provider_api_endpoint(base_url, path)?;
    let client = Client::builder()
        .timeout(timeout)
        .build()
        .map_err(|error| format!("failed to build HTTP client for {label} model test: {error}"))?;
    let mut request = client
        .post(&endpoint)
        .header(ACCEPT, "application/json")
        .header(CONTENT_TYPE, "application/json")
        .json(&payload);

    if let Some(api_key) = api_key.as_deref() {
        if matches!(upstream_format, UpstreamFormat::AnthropicMessages)
            && is_direct_anthropic_endpoint(&endpoint)
        {
            request = request
                .header("x-api-key", api_key)
                .header("anthropic-version", "2023-06-01");
        } else {
            request = request.header(AUTHORIZATION, format!("Bearer {api_key}"));
        }
    }

    let response = request.send().map_err(|error| {
        format!(
            "{label} model test request failed: {}",
            safe_http_error(error)
        )
    })?;
    let status = response.status();
    let body = response.text().unwrap_or_default();
    if !status.is_success() {
        return Err(format!(
            "{label} model test failed with HTTP status {status}: {}",
            compact_response_body(&body)
        ));
    }

    Ok(json!({
        "ok": true,
        "upstream_format": format_id,
        "endpoint": endpoint,
        "status": status.as_u16(),
    }))
}

fn model_test_payload(
    model: &str,
    upstream_format: &UpstreamFormat,
) -> (&'static str, &'static str, &'static str, Value) {
    match upstream_format {
        UpstreamFormat::ChatCompletions => (
            "chat_completions",
            "Chat Completions",
            "/chat/completions",
            json!({
                "model": model,
                "messages": [{"role": "user", "content": "Endpoint connectivity probe. Reply exactly: OK"}],
                "max_tokens": 16,
                "stream": false,
            }),
        ),
        UpstreamFormat::AnthropicMessages => (
            "anthropic_messages",
            "Anthropic Messages",
            "/messages",
            json!({
                "model": model,
                "messages": [{"role": "user", "content": "Endpoint connectivity probe. Reply exactly: OK"}],
                "max_tokens": 16,
            }),
        ),
        UpstreamFormat::Auto | UpstreamFormat::Responses => (
            "responses",
            "Responses",
            "/responses",
            json!({
                "model": model,
                "input": "Endpoint connectivity probe. Reply exactly: OK",
                "max_output_tokens": 16,
                "stream": false,
            }),
        ),
    }
}

pub fn generate_catalog() -> Result<Vec<Model>, String> {
    let paths = ModelPaths::runtime()?;
    let python = find_python();
    let runner = ProcessCatalogSyncRunner;

    generate_catalog_with_runner(&paths, &python, &runner)
}

pub fn list_models() -> Result<Vec<Model>, String> {
    Ok(list_models_with_presence()?.unwrap_or_default())
}

pub(crate) fn list_models_with_presence() -> Result<Option<Vec<Model>>, String> {
    let paths = ModelPaths::runtime()?;
    let catalog_path = paths.existing_generated_catalog_path();
    if !catalog_path.exists() {
        return Ok(None);
    }

    let mut models = read_catalog_models(&catalog_path)?;
    apply_catalog_multi_agent_overrides(&paths, &mut models);
    Ok(Some(models))
}

pub fn list_model_metadata() -> Result<Vec<Model>, String> {
    let paths = ModelPaths::runtime()?;
    let config_paths = config::ConfigPaths::runtime()?;
    let known_official_models = config::known_official_model_ids(&config_paths);
    let cached = read_metadata_cache(&paths).unwrap_or_default();
    let cached = merge_metadata_with_overrides(
        builtin_model_metadata(),
        cached,
        &known_official_models,
    );
    let overrides = read_metadata_overrides(&paths).unwrap_or_default();
    let merged = merge_metadata_with_overrides(
        cached,
        overrides,
        &known_official_models,
    );
    let mut merged = merged;
    apply_catalog_multi_agent_overrides(&paths, &mut merged);
    Ok(merged.into_iter().filter(model_is_catalog_visible).collect())
}

pub(crate) fn list_cached_official_subscription_models_with_presence(
) -> Result<Option<Vec<Model>>, String> {
    let paths = ModelPaths::runtime()?;
    read_official_subscription_models_from_cache_with_presence(&paths)
}

pub fn refresh_model_metadata() -> Result<Vec<Model>, String> {
    let paths = ModelPaths::runtime()?;
    let metadata = builtin_model_metadata();
    write_models_json(&paths.metadata_cache_path(), &metadata)?;
    list_model_metadata()
}

pub fn save_model_metadata_override(model: Model) -> Result<Model, String> {
    let paths = ModelPaths::runtime()?;
    let mut overrides = read_metadata_overrides(&paths).unwrap_or_default();
    if let Some(existing) = overrides.iter_mut().find(|item| item.id == model.id) {
        *existing = model.clone();
    } else {
        overrides.push(model.clone());
    }
    write_models_json(&paths.metadata_overrides_path(), &overrides)?;
    Ok(model)
}

/// Persist one explicit Official Collaboration V1/V2 choice in the existing
/// catalog ownership sidecar and immediately rematerialize the effective
/// catalog.  The identity is exact (`openai`/`official`/canonical model slug)
/// and only the pinned Code Mode models are eligible.
pub fn save_official_multi_agent_version(
    model_id: String,
    version: Option<String>,
) -> Result<Model, String> {
    let paths = ModelPaths::runtime()?;
    let known_official_models = config::known_official_model_ids(&config::ConfigPaths::runtime()?);
    let canonical = config::normalize_official_model_id(&model_id, &known_official_models)
        .ok_or_else(|| "official model identity is invalid".to_string())?;
    if qualified_official_code_mode_multi_agent_version(&canonical).is_none() {
        return Err("this Official model has no qualified Collaboration V1/V2 selector".to_string());
    }
    let version = version.map(|value| value.trim().to_ascii_lowercase());
    if let Some(value) = version.as_deref() {
        if value != "v1" && value != "v2" {
            return Err("Collaboration version must be v1 or v2".to_string());
        }
    }

    // Reconcile the current generated catalog before reading the sidecar.  A
    // stale effective catalog can otherwise cause the sync process to discard
    // an older sidecar entry as if the user had manually cleared it.
    generate_catalog()?;
    let current_models = list_models_with_presence()?.unwrap_or_default();
    let visible_official_row = current_models.iter().any(|model| {
        let model_id = model
            .id
            .strip_prefix("openai/")
            .unwrap_or(model.id.as_str());
        model.source_kind.as_deref() == Some("official")
            && model_id == canonical
            && model_has_exact_official_upstream(model, &canonical)
            && model_is_catalog_visible(model)
    });
    if !visible_official_row {
        return Err("selected Official model is not a visible qualified catalog row".to_string());
    }
    let baseline_version = read_managed_catalog_multi_agent_version(&paths, &canonical)
        .or_else(|| {
            builtin_model_metadata()
                .into_iter()
                .find(|model| model.id == canonical)
                .and_then(|model| model.multi_agent_version)
        })
        .ok_or_else(|| "Official catalog baseline has no Collaboration version".to_string())?;

    let path = paths.catalog_overrides_path();
    let mut payload = if path.exists() {
        let text = fs::read_to_string(&path)
            .map_err(|error| format!("failed to read catalog overrides: {error}"))?;
        serde_json::from_str::<Value>(&text)
            .map_err(|error| format!("catalog overrides are invalid: {error}"))?
    } else {
        json!({"schema_version": 1, "overrides": []})
    };
    let object = payload
        .as_object_mut()
        .ok_or_else(|| "catalog overrides are invalid".to_string())?;
    if object.get("schema_version").and_then(Value::as_u64) != Some(1) {
        return Err("catalog overrides have an unsupported schema".to_string());
    }
    update_catalog_override_payload(&mut payload, &canonical, version.as_deref())?;

    // The catalog sync process intentionally removes a sidecar entry when the
    // current effective row equals the managed baseline.  Publish the desired
    // effective value first, then publish the sidecar, so a newly selected V2
    // value cannot be lost during the same save operation.  Clearing uses the
    // pinned managed baseline and therefore leaves no stale user value in the
    // generated catalog.
    set_catalog_multi_agent_version(
        &paths,
        &canonical,
        version.as_deref().unwrap_or(baseline_version.as_str()),
    )?;
    let text = serde_json::to_string_pretty(&payload)
        .map_err(|error| format!("failed to serialize catalog overrides: {error}"))?;
    safe_file::write_text_atomic(&path, &format!("{text}\n"))
        .map_err(|error| format!("failed to write catalog overrides: {error}"))?;

    let models = list_models_with_presence()?.unwrap_or_default();
    models
        .into_iter()
        .find(|model| model.id == canonical)
        .ok_or_else(|| "selected Official model is not present in the generated catalog".to_string())
}

fn update_catalog_override_payload(
    payload: &mut Value,
    canonical_model_id: &str,
    version: Option<&str>,
) -> Result<(), String> {
    let object = payload
        .as_object_mut()
        .ok_or_else(|| "catalog overrides are invalid".to_string())?;
    if object.get("schema_version").and_then(Value::as_u64) != Some(1) {
        return Err("catalog overrides have an unsupported schema".to_string());
    }
    let entries = object
        .get_mut("overrides")
        .and_then(Value::as_array_mut)
        .ok_or_else(|| "catalog overrides are invalid".to_string())?;
    let mut matched = false;
    for entry in entries.iter_mut() {
        let is_exact_identity = entry.get("provider").and_then(Value::as_str) == Some("openai")
            && entry.get("upstream_name").and_then(Value::as_str) == Some("official")
            && entry.get("upstream_model").and_then(Value::as_str)
                == Some(canonical_model_id);
        if !is_exact_identity {
            continue;
        }
        matched = true;
        let fields = entry
            .as_object_mut()
            .and_then(|object| object.get_mut("fields"))
            .and_then(Value::as_object_mut)
            .ok_or_else(|| "catalog override fields are invalid".to_string())?;
        match version {
            Some(value) => {
                fields.insert(
                    "multi_agent_version".to_string(),
                    Value::String(value.to_string()),
                );
            }
            None => {
                fields.remove("multi_agent_version");
            }
        }
    }
    if version.is_some() && !matched {
        entries.push(json!({
            "provider": "openai",
            "upstream_name": "official",
            "upstream_model": canonical_model_id,
            "fields": {"multi_agent_version": version.unwrap()},
        }));
    }
    entries.retain(|entry| {
        let is_exact_identity = entry.get("provider").and_then(Value::as_str) == Some("openai")
            && entry.get("upstream_name").and_then(Value::as_str) == Some("official")
            && entry.get("upstream_model").and_then(Value::as_str)
                == Some(canonical_model_id);
        !(is_exact_identity
            && entry
                .get("fields")
                .and_then(Value::as_object)
                .is_some_and(Map::is_empty))
    });
    entries.sort_by(|left, right| {
        let left_key = left
            .get("upstream_model")
            .and_then(Value::as_str)
            .unwrap_or_default();
        let right_key = right
            .get("upstream_model")
            .and_then(Value::as_str)
            .unwrap_or_default();
        left_key.cmp(right_key)
    });
    Ok(())
}

/// Return only valid, exact Official model-level Collaboration overrides.
/// The sidecar is user-owned input, so malformed or third-party entries are
/// ignored rather than being allowed to influence the Official catalog.
pub fn list_official_multi_agent_overrides() -> Result<HashMap<String, String>, String> {
    let paths = ModelPaths::runtime()?;
    let visible_models = list_models_with_presence()?.unwrap_or_default();
    let visible_qualified_ids: HashSet<String> = visible_models
        .iter()
        .filter_map(|model| {
            if model.source_kind.as_deref() != Some("official")
                || !model_is_catalog_visible(model)
            {
                return None;
            }
            let canonical = model
                .id
                .strip_prefix("openai/")
                .unwrap_or(model.id.as_str())
                .to_string();
            (qualified_official_code_mode_multi_agent_version(&canonical)
                .is_some()
                && model_has_exact_official_upstream(model, &canonical))
            .then_some(canonical)
        })
        .collect();
    let path = paths.catalog_overrides_path();
    if !path.exists() {
        return Ok(HashMap::new());
    }
    let text = fs::read_to_string(&path)
        .map_err(|error| format!("failed to read catalog overrides: {error}"))?;
    let payload = serde_json::from_str::<Value>(&text)
        .map_err(|error| format!("catalog overrides are invalid: {error}"))?;
    if payload.get("schema_version").and_then(Value::as_u64) != Some(1) {
        return Err("catalog overrides have an unsupported schema".to_string());
    }
    let Some(entries) = payload.get("overrides").and_then(Value::as_array) else {
        return Err("catalog overrides are invalid".to_string());
    };
    let mut result = HashMap::new();
    for entry in entries {
        if entry.get("provider").and_then(Value::as_str) != Some("openai")
            || entry.get("upstream_name").and_then(Value::as_str) != Some("official")
        {
            continue;
        }
        let Some(model_id) = entry.get("upstream_model").and_then(Value::as_str) else {
            continue;
        };
        let canonical = model_id.strip_prefix("openai/").unwrap_or(model_id);
        if qualified_official_code_mode_multi_agent_version(canonical).is_none()
            || !visible_qualified_ids.contains(canonical)
        {
            continue;
        }
        let Some(version) = entry
            .get("fields")
            .and_then(Value::as_object)
            .and_then(|fields| fields.get("multi_agent_version"))
            .and_then(Value::as_str)
            .filter(|value| *value == "v1" || *value == "v2")
        else {
            continue;
        };
        result.insert(canonical.to_string(), version.to_string());
    }
    Ok(result)
}

#[cfg(test)]
fn refresh_official_models_from_endpoint(
    endpoint: &str,
    api_key: &str,
    timeout: Duration,
) -> Result<Vec<Model>, String> {
    discover_models_http(
        endpoint,
        api_key,
        timeout,
        DiscoveryKind::Official,
        "official OpenAI models",
    )
}

trait AppServerModelListRunner {
    fn read_model_list(&self) -> Result<Value, String>;
}

struct ProcessAppServerModelListRunner;

impl AppServerModelListRunner for ProcessAppServerModelListRunner {
    fn read_model_list(&self) -> Result<Value, String> {
        read_codex_app_server_model_list()
    }
}

#[cfg(test)]
fn refresh_official_models_with_runner(
    paths: &ModelPaths,
    runner: &dyn AppServerModelListRunner,
) -> Result<Vec<Model>, String> {
    refresh_official_models_direct_with_runner(paths, runner).or_else(|error| {
        read_official_subscription_models_from_cache(paths).map_err(|cache_error| {
            format!(
                "Codex subscription model list unavailable: {error}; cached official models unavailable: {cache_error}"
            )
        })
    })
}

fn refresh_official_models_direct_with_runner(
    paths: &ModelPaths,
    runner: &dyn AppServerModelListRunner,
) -> Result<Vec<Model>, String> {
    let subscription_models = runner
        .read_model_list()
        .and_then(|payload| {
            let visibility_diagnostics = visibility_diagnostics_from_payload(&payload);
            let subscription_models = subscription_models_from_app_server_payload(&payload)?;
            Ok((subscription_models, visibility_diagnostics))
        })?;
    let (subscription_models, visibility_diagnostics) = subscription_models;
    let models = subscription_models_to_metadata_models(&subscription_models);
    write_official_subscription_caches(
        paths,
        &subscription_models,
        &models,
        &visibility_diagnostics,
    )?;
    Ok(models)
}

fn visibility_diagnostics_from_payload(payload: &Value) -> Value {
    let items = payload
        .get("data")
        .or_else(|| payload.get("models"))
        .and_then(Value::as_array)
        .or_else(|| payload.as_array());
    let mut counts = Map::from_iter([
        ("hidden".to_string(), json!(0u64)),
        ("unknown".to_string(), json!(0u64)),
        ("internal".to_string(), json!(0u64)),
    ]);
    for item in items.into_iter().flatten() {
        let Some(object) = item.as_object() else {
            continue;
        };
        let key = if item_has_internal_identity(object) {
            "internal"
        } else {
            match catalog_visibility_from_item(object) {
                CatalogVisibility::Hide => "hidden",
                CatalogVisibility::Unknown => "unknown",
                CatalogVisibility::List => continue,
            }
        };
        if let Some(count) = counts.get(key).and_then(Value::as_u64) {
            counts.insert(
                key.to_string(),
                json!(count.saturating_add(1).min(MAX_VISIBILITY_DIAGNOSTIC_COUNT)),
            );
        }
    }
    Value::Object(counts)
}

fn read_codex_app_server_model_list() -> Result<Value, String> {
    let codex = find_codex_executable()?;
    let mut command = Command::new(&codex);
    command.args(["app-server", "--stdio"]);
    let cache_path = runtime_paths::codex_target_home_dir()?.join("models_cache.json");
    read_codex_app_server_model_list_with_cache_path(
        command,
        CODEX_APP_SERVER_MODEL_LIST_TIMEOUT,
        &cache_path,
        CODEX_APP_SERVER_NATIVE_CACHE_GRACE,
    )
}

#[cfg(test)]
fn read_codex_app_server_model_list_with_command(
    command: Command,
    timeout: Duration,
) -> Result<Value, String> {
    read_codex_app_server_model_list_with_cache_grace(command, timeout, None, Duration::ZERO)
}

fn read_codex_app_server_model_list_with_cache_path(
    command: Command,
    timeout: Duration,
    cache_path: &Path,
    cache_grace: Duration,
) -> Result<Value, String> {
    read_codex_app_server_model_list_with_cache_grace(
        command,
        timeout,
        Some(cache_path),
        cache_grace,
    )
}

fn read_codex_app_server_model_list_with_cache_grace(
    mut command: Command,
    timeout: Duration,
    cache_path: Option<&Path>,
    cache_grace: Duration,
) -> Result<Value, String> {
    // Capture the cache before starting app-server.  A context-less
    // model/list response may only use a cache atomically published by this
    // request; accepting an unchanged file could mistake an older snapshot
    // for the current Official context evidence.
    let initial_cache = cache_path.and_then(readable_native_model_cache);
    let deadline = Instant::now() + timeout;
    command
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null());
    configure_no_window(&mut command);
    let mut child = command
        .spawn()
        .map_err(|error| format!("failed to start codex app-server for model list: {error}"))?;

    let mut stdin = match child.stdin.take() {
        Some(stdin) => stdin,
        None => {
            kill_child(&mut child);
            return Err("failed to open codex app-server stdin".to_string());
        }
    };
    if let Err(error) = write_app_server_json_line(
        &mut stdin,
        &json!({
            "id": 1,
            "method": "initialize",
            "params": {
                "clientInfo": {
                    "name": "codexhub",
                    "title": "CodexHub",
                    "version": env!("CARGO_PKG_VERSION")
                },
                "capabilities": {
                    "experimentalApi": true,
                    "requestAttestation": false,
                    "optOutNotificationMethods": []
                }
            }
        }),
    ) {
        kill_child(&mut child);
        return Err(error);
    }
    if let Err(error) =
        write_app_server_json_line(&mut stdin, &json!({ "method": "initialized" }))
    {
        kill_child(&mut child);
        return Err(error);
    }
    if let Err(error) = write_app_server_json_line(
        &mut stdin,
        &json!({
            "id": 2,
            "method": "model/list",
            "params": {}
        }),
    ) {
        kill_child(&mut child);
        return Err(error);
    }
    if let Err(error) = stdin.flush() {
        kill_child(&mut child);
        return Err(format!(
            "failed to flush codex app-server model list request: {error}"
        ));
    }

    let stdout = match child.stdout.take() {
        Some(stdout) => stdout,
        None => {
            kill_child(&mut child);
            return Err("failed to open codex app-server stdout".to_string());
        }
    };
    let message = read_codex_app_server_value_response(
        &mut child,
        stdout,
        json!(2),
        deadline.saturating_duration_since(Instant::now()),
    )?;
    if let Some(error) = message.get("error") {
        let message = error
            .get("message")
            .and_then(Value::as_str)
            .unwrap_or("request failed");
        kill_child(&mut child);
        return Err(format!("codex app-server model list failed: {message}"));
    }
    let result = message
        .get("result")
        .cloned()
        .ok_or_else(|| "codex app-server model list response did not include a result".to_string());
    let result = match result {
        Ok(result) => result,
        Err(error) => {
            kill_child(&mut child);
            return Err(error);
        }
    };
    if let Some(cache_path) = cache_path {
        if !model_list_contains_context_metadata(&result)
            && !wait_for_native_model_cache_publication(
                cache_path,
                cache_grace,
                initial_cache.as_deref(),
            )
        {
            kill_child(&mut child);
            return Err(
                "codex app-server model list did not publish a readable native models cache before the refresh deadline (context metadata absent)"
                    .to_string(),
            );
        }
    }
    kill_child(&mut child);
    Ok(result)
}

fn model_list_contains_context_metadata(result: &Value) -> bool {
    let Some(items) = result
        .get("data")
        .or_else(|| result.get("models"))
        .and_then(Value::as_array)
        .or_else(|| result.as_array())
    else {
        return false;
    };
    let visible_models: Vec<&Value> = items
        .iter()
        .filter(|item| {
            item.as_object().is_some_and(|object| {
                !item_has_internal_identity(object)
                    && catalog_visibility_from_item(object) == CatalogVisibility::List
            })
        })
        .collect();
    !visible_models.is_empty()
        && visible_models.iter().all(|item| {
            numeric_limit(item, &["context_window", "max_context_window"], "context").is_some()
                && numeric_limit(
                    item,
                    &[
                        "effective_context_window_percent",
                        "effectiveContextWindowPercent",
                    ],
                    "context",
                )
                .is_some()
        })
}

fn wait_for_native_model_cache_publication(
    cache_path: &Path,
    grace: Duration,
    initial_cache: Option<&[u8]>,
) -> bool {
    let deadline = Instant::now() + grace;
    let mut previous = None;
    loop {
        let current = readable_native_model_cache(cache_path);
        let changed = match (current.as_deref(), initial_cache) {
            (Some(current), Some(initial)) => current != initial,
            (Some(_), None) => true,
            _ => false,
        };
        if changed && current == previous {
            return true;
        }
        previous = current;
        let now = Instant::now();
        if now >= deadline {
            return false;
        }
        thread::sleep(Duration::from_millis(10).min(deadline.saturating_duration_since(now)));
    }
}

fn readable_native_model_cache(cache_path: &Path) -> Option<Vec<u8>> {
    let bytes = fs::read(cache_path).ok()?;
    let payload: Value = serde_json::from_slice(&bytes).ok()?;
    let models = payload.get("models")?.as_array()?;
    if models.is_empty() || models.iter().any(|item| !item.is_object()) {
        return None;
    }
    // Detailed freshness, identity, and comp_hash authority remains in
    // catalog_sync.py.  This seam only proves that a stable JSON cache was
    // atomically published; the caller separately requires it to differ from
    // the pre-request snapshot when context metadata was absent.
    Some(bytes)
}

fn read_codex_app_server_value_response(
    child: &mut Child,
    stdout: ChildStdout,
    expected_id: Value,
    timeout: Duration,
) -> Result<Value, String> {
    let receiver = spawn_app_server_line_reader(stdout);
    let deadline = Instant::now() + timeout;
    loop {
        let now = Instant::now();
        if now >= deadline {
            kill_child(child);
            return Err(format!(
                "codex app-server model list timed out after {} seconds",
                timeout.as_secs()
            ));
        }
        let remaining = deadline.saturating_duration_since(now);
        match receiver.recv_timeout(remaining) {
            Ok(Ok(Some(line))) => {
                let trimmed = line.trim();
                if trimmed.is_empty() {
                    continue;
                }
                let message: Value = match serde_json::from_str(trimmed) {
                    Ok(message) => message,
                    Err(_) => continue,
                };
                if message.get("id") == Some(&expected_id) {
                    return Ok(message);
                }
            }
            Ok(Ok(None)) => {
                let _ = child.wait();
                return Err("codex app-server model list did not return a response".to_string());
            }
            Ok(Err(error)) => {
                kill_child(child);
                return Err(format!(
                    "failed to read codex app-server model list response: {error}"
                ));
            }
            Err(mpsc::RecvTimeoutError::Timeout) => {
                kill_child(child);
                return Err(format!(
                    "codex app-server model list timed out after {} seconds",
                    timeout.as_secs()
                ));
            }
            Err(mpsc::RecvTimeoutError::Disconnected) => {
                let _ = child.wait();
                return Err(
                    "codex app-server model list reader stopped before a response".to_string(),
                );
            }
        }
    }
}

fn spawn_app_server_line_reader(
    stdout: ChildStdout,
) -> mpsc::Receiver<Result<Option<String>, String>> {
    let (sender, receiver) = mpsc::channel();
    thread::spawn(move || {
        let mut reader = BufReader::new(stdout);
        let mut line = String::new();
        loop {
            line.clear();
            match reader.read_line(&mut line) {
                Ok(0) => {
                    let _ = sender.send(Ok(None));
                    break;
                }
                Ok(_) => {
                    if sender.send(Ok(Some(line.clone()))).is_err() {
                        break;
                    }
                }
                Err(error) => {
                    let _ = sender.send(Err(error.to_string()));
                    break;
                }
            }
        }
    });
    receiver
}

fn kill_child(child: &mut Child) {
    let _ = child.kill();
    let _ = child.wait();
}

fn configure_no_window(command: &mut Command) {
    #[cfg(target_os = "windows")]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        command.creation_flags(CREATE_NO_WINDOW);
    }
    #[cfg(not(target_os = "windows"))]
    {
        let _ = command;
    }
}

fn write_app_server_json_line(stdin: &mut impl Write, value: &Value) -> Result<(), String> {
    serde_json::to_writer(&mut *stdin, value)
        .map_err(|error| format!("failed to encode codex app-server request: {error}"))?;
    stdin
        .write_all(b"\n")
        .map_err(|error| format!("failed to write codex app-server request: {error}"))
}

#[derive(Debug, Clone)]
struct OfficialSubscriptionModel {
    raw: Value,
    slug: String,
    legacy_prefixed: bool,
    display_name: String,
    description: Option<String>,
    context_window: Option<u32>,
    max_output_tokens: Option<u32>,
    input_modalities: Vec<String>,
    reasoning_levels: Vec<ReasoningLevelEntry>,
    default_reasoning_level: Option<String>,
    additional_speed_tiers: Vec<String>,
    service_tiers: Vec<Value>,
    is_default: bool,
    enabled: bool,
    enabled_present: bool,
}

#[derive(Debug, Clone)]
struct ReasoningLevelEntry {
    effort: String,
    description: Option<String>,
}

fn subscription_models_from_app_server_payload(
    payload: &Value,
) -> Result<Vec<OfficialSubscriptionModel>, String> {
    subscription_models_from_payload(payload)
}

fn subscription_models_from_payload(
    payload: &Value,
) -> Result<Vec<OfficialSubscriptionModel>, String> {
    let items = payload
        .get("data")
        .or_else(|| payload.get("models"))
        .and_then(Value::as_array)
        .or_else(|| payload.as_array())
        .ok_or_else(|| {
            "Codex subscription model list response did not contain a model array".to_string()
        })?;
    let mut positions = HashMap::new();
    let mut output = Vec::new();
    for item in items {
        let Some(mut model) = subscription_model_from_item(item) else {
            continue;
        };
        apply_pinned_official_catalog_metadata_to_seed(&mut model)?;
        if let Some(&position) = positions.get(&model.slug) {
            let existing: &OfficialSubscriptionModel = &output[position];
            let enabled = existing.enabled || model.enabled;
            let enabled_present = existing.enabled_present || model.enabled_present;
            if existing.legacy_prefixed && !model.legacy_prefixed {
                // A bare App CLI record is the authoritative metadata shape.
            } else if !existing.legacy_prefixed && model.legacy_prefixed {
                model = existing.clone();
            }
            model.enabled = enabled;
            model.enabled_present = enabled_present;
            if enabled_present {
                if let Some(raw) = model.raw.as_object_mut() {
                    raw.insert("enabled".to_string(), json!(enabled));
                }
            }
            output[position] = model;
        } else {
            positions.insert(model.slug.clone(), output.len());
            output.push(model);
        }
    }
    Ok(output)
}

fn apply_pinned_official_catalog_metadata_to_seed(
    model: &mut OfficialSubscriptionModel,
) -> Result<(), String> {
    let metadata = pinned_official_catalog_metadata()?.get(&model.slug);
    let raw = model
        .raw
        .as_object_mut()
        .ok_or_else(|| format!("official model {} has invalid raw metadata", model.slug))?;
    ensure_responses_lite_opt_in(raw);
    if let Some(metadata) = metadata {
        for (key, value) in metadata {
            raw.insert(key.clone(), value.clone());
        }
    }
    Ok(())
}

fn ensure_responses_lite_opt_in(metadata: &mut Map<String, Value>) {
    if metadata
        .get("use_responses_lite")
        .and_then(Value::as_bool)
        .is_none()
    {
        metadata.insert("use_responses_lite".to_string(), json!(false));
    }
}

fn pinned_official_catalog_metadata() -> Result<&'static PinnedOfficialCatalogMetadata, String> {
    PINNED_OFFICIAL_CATALOG_METADATA
        .get_or_init(parse_pinned_official_catalog_metadata)
        .as_ref()
        .map_err(|error| error.clone())
}

fn parse_pinned_official_catalog_metadata() -> PinnedOfficialCatalogMetadataResult {
    let document: Value = serde_json::from_str(OFFICIAL_CATALOG_METADATA_JSON)
        .map_err(|error| format!("official catalog metadata is unreadable: {error}"))?;
    if document.get("schema_version") != Some(&json!(1)) {
        return Err("official catalog metadata has an unsupported schema".to_string());
    }
    let models = document
        .get("models")
        .and_then(Value::as_object)
        .ok_or_else(|| "official catalog metadata has no model map".to_string())?;
    if models.len() != PINNED_OFFICIAL_MODEL_IDS.len()
        || PINNED_OFFICIAL_MODEL_IDS
            .iter()
            .any(|slug| !models.contains_key(*slug))
    {
        return Err("official catalog metadata has an incomplete model set".to_string());
    }

    let mut output = HashMap::new();
    for slug in PINNED_OFFICIAL_MODEL_IDS {
        let metadata = models
            .get(*slug)
            .and_then(Value::as_object)
            .ok_or_else(|| format!("official catalog metadata for {slug} is invalid"))?;
        validate_pinned_official_catalog_metadata(slug, metadata)?;
        output.insert((*slug).to_string(), metadata.clone());
    }
    Ok(output)
}

fn validate_pinned_official_catalog_metadata(
    slug: &str,
    metadata: &Map<String, Value>,
) -> Result<(), String> {
    let required_fields = if pinned_official_code_mode_multi_agent_version(slug).is_some()
        || is_pinned_official_legacy_model(slug)
    {
        PINNED_OFFICIAL_PLANNER_FIELDS
    } else {
        PINNED_OFFICIAL_LITE_ONLY_FIELDS
    };
    if metadata.len() != required_fields.len()
        || required_fields.iter().any(|field| !metadata.contains_key(*field))
    {
        return Err(format!(
            "official catalog metadata for {slug} has an invalid field set"
        ));
    }
    if let Some(expected_multi_agent_version) = pinned_official_code_mode_multi_agent_version(slug)
    {
        if metadata
            .get("prefer_websockets")
            .and_then(Value::as_bool)
            != Some(true)
        {
            return Err(format!(
                "official catalog metadata for {slug} has an invalid websocket flag"
            ));
        }
        if metadata.get("tool_mode").and_then(Value::as_str) != Some("code_mode_only") {
            return Err(format!(
                "official catalog metadata for {slug} has an invalid tool mode"
            ));
        }
        if metadata
            .get("multi_agent_version")
            .and_then(Value::as_str)
            != Some(expected_multi_agent_version)
        {
            return Err(format!(
                "official catalog metadata for {slug} has an invalid multi-agent version"
            ));
        }
        if metadata
            .get("use_responses_lite")
            .and_then(Value::as_bool)
            != Some(true)
        {
            return Err(format!(
                "official catalog metadata for {slug} has an invalid Responses Lite flag"
            ));
        }
    } else if is_pinned_official_legacy_model(slug) {
        if metadata
            .get("prefer_websockets")
            .and_then(Value::as_bool)
            != Some(true)
        {
            return Err(format!(
                "official catalog metadata for {slug} has an invalid websocket flag"
            ));
        }
        if !metadata.get("tool_mode").is_some_and(Value::is_null) {
            return Err(format!(
                "official catalog metadata for {slug} has an invalid tool mode"
            ));
        }
        if !metadata
            .get("multi_agent_version")
            .is_some_and(Value::is_null)
        {
            return Err(format!(
                "official catalog metadata for {slug} has an invalid multi-agent version"
            ));
        }
        if metadata
            .get("use_responses_lite")
            .and_then(Value::as_bool)
            != Some(false)
        {
            return Err(format!(
                "official catalog metadata for {slug} has an invalid Responses Lite flag"
            ));
        }
    } else if metadata
        .get("use_responses_lite")
        .and_then(Value::as_bool)
        != Some(false)
    {
        return Err(format!(
            "official catalog metadata for {slug} has an invalid Responses Lite flag"
        ));
    }
    Ok(())
}

fn pinned_official_code_mode_multi_agent_version(slug: &str) -> Option<&'static str> {
    match slug {
        "gpt-5.6-sol" | "gpt-5.6-terra" => Some("v2"),
        "gpt-5.6-luna" => Some("v1"),
        _ => None,
    }
}

fn is_pinned_official_legacy_model(slug: &str) -> bool {
    matches!(slug, "gpt-5.5" | "gpt-5.4" | "gpt-5.4-mini")
}

const INTERNAL_MODEL_IDENTIFIERS: &[&str] = &["codex-auto-review"];

pub(crate) fn model_is_catalog_visible(model: &Model) -> bool {
    if model.visibility != CatalogVisibility::List {
        return false;
    }
    let mut identities = vec![model.id.as_str()];
    if let Some(upstream_model) = model.upstream_model.as_deref() {
        identities.push(upstream_model);
    }
    identities.extend(model.aliases.iter().map(String::as_str));
    !identities.iter().any(|value| {
        let normalized = value
            .trim()
            .strip_prefix("openai/")
            .unwrap_or(value.trim())
            .to_ascii_lowercase();
        let normalized = normalized.strip_suffix(":cloud").unwrap_or(&normalized);
        INTERNAL_MODEL_IDENTIFIERS.iter().any(|identifier| {
            normalized == *identifier || normalized.starts_with(&format!("{identifier}/"))
        })
    })
}

fn catalog_visibility_from_item(object: &Map<String, Value>) -> CatalogVisibility {
    if object.get("hidden").and_then(Value::as_bool) == Some(true) {
        return CatalogVisibility::Hide;
    }
    match object
        .get("visibility")
        .and_then(Value::as_str)
        .map(str::trim)
        .map(|value| value.to_ascii_lowercase())
        .as_deref()
    {
        Some("list") => CatalogVisibility::List,
        Some("hide") => CatalogVisibility::Hide,
        Some(_) => CatalogVisibility::Unknown,
        None if object.get("hidden").and_then(Value::as_bool) == Some(false) => {
            CatalogVisibility::List
        }
        None => CatalogVisibility::Unknown,
    }
}

fn item_has_internal_identity(object: &Map<String, Value>) -> bool {
    object
        .iter()
        .filter(|(key, _)| {
            matches!(
                key.as_str(),
                "id" | "slug" | "model" | "name" | "upstream_model"
            )
        })
        .filter_map(|(_, value)| value.as_str())
        .map(str::trim)
        .map(|value| value.strip_prefix("openai/").unwrap_or(value))
        .map(str::to_ascii_lowercase)
        .map(|value| value.strip_suffix(":cloud").unwrap_or(&value).to_string())
        .any(|value| {
            INTERNAL_MODEL_IDENTIFIERS.iter().any(|identifier| {
                value == *identifier || value.starts_with(&format!("{identifier}/"))
            })
        })
}

fn subscription_model_from_item(item: &Value) -> Option<OfficialSubscriptionModel> {
    let object = item.as_object()?;
    if item_has_internal_identity(object)
        || catalog_visibility_from_item(object) != CatalogVisibility::List
    {
        return None;
    }
    let raw_slug = first_string(object, &["model", "slug", "id"])?;
    let slug = raw_slug
        .strip_prefix("openai/")
        .unwrap_or(&raw_slug)
        .to_string();
    if !slug.starts_with("gpt-") {
        return None;
    }
    let display_name = first_string(
        object,
        &["displayName", "display_name", "name", "model", "slug", "id"],
    )
    .unwrap_or_else(|| slug.clone());

    Some(OfficialSubscriptionModel {
        raw: item.clone(),
        slug,
        legacy_prefixed: raw_slug.starts_with("openai/"),
        display_name,
        description: first_string(object, &["description"]),
        context_window: numeric_limit(item, &["context_window", "max_context_window"], "context"),
        max_output_tokens: numeric_limit(item, &["max_output_tokens", "output_tokens"], "output"),
        input_modalities: first_string_array(object, &["inputModalities", "input_modalities"])
            .unwrap_or_else(|| vec!["text".to_string()]),
        reasoning_levels: reasoning_level_entries(
            object
                .get("supportedReasoningEfforts")
                .or_else(|| object.get("supported_reasoning_levels")),
        ),
        default_reasoning_level: first_string(
            object,
            &["defaultReasoningEffort", "default_reasoning_level"],
        ),
        additional_speed_tiers: first_string_array(
            object,
            &["additionalSpeedTiers", "additional_speed_tiers"],
        )
        .unwrap_or_default(),
        service_tiers: object
            .get("serviceTiers")
            .or_else(|| object.get("service_tiers"))
            .and_then(Value::as_array)
            .map(|items| {
                items
                    .iter()
                    .filter(|item| item.is_object())
                    .cloned()
                    .collect()
            })
            .unwrap_or_default(),
        is_default: object
            .get("isDefault")
            .or_else(|| object.get("is_default"))
            .and_then(Value::as_bool)
            .unwrap_or(false),
        enabled: object
            .get("enabled")
            .and_then(Value::as_bool)
            .unwrap_or(true),
        enabled_present: object.contains_key("enabled"),
    })
}

fn subscription_models_to_metadata_models(
    subscription_models: &[OfficialSubscriptionModel],
) -> Vec<Model> {
    let builtin = builtin_model_metadata();
    let mut output = Vec::new();
    for subscription_model in subscription_models {
        let id = subscription_model.slug.clone();
        let legacy_prefixed_id = format!("openai/{}", subscription_model.slug);
        let defaults = builtin
            .iter()
            .find(|model| model.id == id || model.id == legacy_prefixed_id);
        output.push(Model {
            id,
            display_name: Some(subscription_model_display_name(
                subscription_model,
                defaults,
            )),
            upstream_model: Some(subscription_model.slug.clone()),
            tool_surface_strategy: None,
            multi_agent_version: subscription_model
                .raw
                .get("multi_agent_version")
                .and_then(Value::as_str)
                .map(str::to_string)
                .or_else(|| defaults.and_then(|model| model.multi_agent_version.clone())),
            aliases: Vec::new(),
            source_kind: Some("official".to_string()),
            locked: true,
            codex_enabled: true,
            gateway_exported: true,
            visibility: CatalogVisibility::List,
            context_window: subscription_model
                .context_window
                .or_else(|| defaults.and_then(|model| model.context_window)),
            max_context_window: defaults.and_then(|model| model.max_context_window),
            effective_source: subscription_model
                .context_window
                .map(|_| "codex_app_model_list".to_string())
                .or_else(|| defaults.and_then(|model| model.effective_source.clone())),
            max_source: defaults.and_then(|model| model.max_source.clone()),
            confidence: defaults.and_then(|model| model.confidence.clone()),
            verified_at: defaults.and_then(|model| model.verified_at.clone()),
            max_output_tokens: subscription_model
                .max_output_tokens
                .or_else(|| defaults.and_then(|model| model.max_output_tokens)),
            input_modalities: Some(subscription_model.input_modalities.clone()),
            supported_reasoning_levels: Some(
                subscription_model
                    .reasoning_levels
                    .iter()
                    .map(|level| level.effort.clone())
                    .collect(),
            ),
            default_reasoning_level: subscription_model
                .default_reasoning_level
                .clone()
                .or_else(|| defaults.and_then(|model| model.default_reasoning_level.clone())),
            pricing: defaults.and_then(|model| model.pricing.clone()),
            metadata_provenance: Some(MetadataProvenance {
                source: "codex_subscription".to_string(),
                source_url: None,
                fetched_at: Some(current_unix_timestamp().to_string()),
                confidence: "high".to_string(),
            }),
            sort_order: defaults.and_then(|model| model.sort_order),
            enabled: subscription_model.enabled,
        });
    }
    output
}

fn subscription_model_display_name(
    subscription_model: &OfficialSubscriptionModel,
    defaults: Option<&Model>,
) -> String {
    let raw = subscription_model.display_name.trim();
    if let Some(default_name) = defaults.and_then(|model| model.display_name.as_deref()) {
        if raw.eq_ignore_ascii_case(&subscription_model.slug)
            || raw.eq_ignore_ascii_case(&format!("openai/{}", subscription_model.slug))
        {
            return official_short_display_name(default_name);
        }
    }
    official_short_display_name(raw)
}

fn write_official_subscription_caches(
    paths: &ModelPaths,
    subscription_models: &[OfficialSubscriptionModel],
    metadata_models: &[Model],
    visibility_diagnostics: &Value,
) -> Result<(), String> {
    write_models_json(&paths.metadata_cache_path(), metadata_models)?;
    write_official_subscription_seed(
        &paths.official_subscription_cache_path(),
        subscription_models,
        visibility_diagnostics,
    )
}

fn read_official_subscription_models_from_cache_with_presence(
    paths: &ModelPaths,
) -> Result<Option<Vec<Model>>, String> {
    if !paths.official_subscription_cache_path().exists() {
        return Ok(None);
    }
    let payload = load_json_file(&paths.official_subscription_cache_path())?;
    let subscription_models = subscription_models_from_payload(&payload)?;
    Ok(Some(subscription_models_to_metadata_models(&subscription_models)))
}

fn read_official_subscription_models_from_cache(paths: &ModelPaths) -> Result<Vec<Model>, String> {
    match read_official_subscription_models_from_cache_with_presence(paths)? {
        Some(models) if !models.is_empty() => Ok(models),
        Some(_) => Err(
            "cached Codex subscription model list did not include visible GPT models".to_string(),
        ),
        None => Err("cached Codex subscription model list is missing".to_string()),
    }
}

fn write_official_subscription_seed(
    path: &Path,
    subscription_models: &[OfficialSubscriptionModel],
    visibility_diagnostics: &Value,
) -> Result<(), String> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)
            .map_err(|error| format!("failed to create official model cache directory: {error}"))?;
    }
    let models: Vec<Value> = subscription_models
        .iter()
        .map(official_subscription_seed_model)
        .collect();
    let payload = json!({
        "client_version": env!("CARGO_PKG_VERSION"),
        "fetched_at": current_unix_timestamp(),
        "visibility_diagnostics": visibility_diagnostics,
        "models": models,
    });
    let text = serde_json::to_string_pretty(&payload)
        .map_err(|error| format!("failed to serialize official model cache: {error}"))?;
    safe_file::write_text_atomic(path, &format!("{text}\n")).map_err(|error| {
        format!(
            "failed to write official model cache {}: {error}",
            path.display()
        )
    })
}

fn official_subscription_seed_model(model: &OfficialSubscriptionModel) -> Value {
    let mut payload = model.raw.as_object().cloned().unwrap_or_default();
    ensure_responses_lite_opt_in(&mut payload);
    payload.insert("visibility".to_string(), json!(CatalogVisibility::List));
    payload.insert("slug".to_string(), json!(model.slug));
    payload
        .entry("display_name".to_string())
        .or_insert_with(|| json!(official_short_display_name(&model.display_name)));
    if let Some(description) = model.description.as_ref() {
        payload
            .entry("description".to_string())
            .or_insert_with(|| json!(description));
    }
    if let Some(context_window) = model.context_window {
        payload
            .entry("context_window".to_string())
            .or_insert_with(|| json!(context_window));
        payload
            .entry("max_context_window".to_string())
            .or_insert_with(|| json!(context_window));
    }
    if let Some(max_output_tokens) = model.max_output_tokens {
        payload
            .entry("max_output_tokens".to_string())
            .or_insert_with(|| json!(max_output_tokens));
    }
    if payload.contains_key("inputModalities") && !payload.contains_key("input_modalities") {
        payload.insert(
            "input_modalities".to_string(),
            json!(model.input_modalities),
        );
    }
    if payload.contains_key("supportedReasoningEfforts")
        && !payload.contains_key("supported_reasoning_levels")
        && !model.reasoning_levels.is_empty()
    {
        payload.insert(
            "supported_reasoning_levels".to_string(),
            json!(model
                .reasoning_levels
                .iter()
                .map(|level| {
                    let mut entry = serde_json::Map::new();
                    entry.insert("effort".to_string(), json!(level.effort));
                    if let Some(description) = level.description.as_ref() {
                        entry.insert("description".to_string(), json!(description));
                    }
                    Value::Object(entry)
                })
                .collect::<Vec<_>>()),
        );
    }
    if let Some(default_reasoning_level) = model.default_reasoning_level.as_ref().filter(|_| {
        payload.contains_key("defaultReasoningEffort")
            && !payload.contains_key("default_reasoning_level")
    }) {
        payload.insert(
            "default_reasoning_level".to_string(),
            json!(default_reasoning_level),
        );
    }
    if payload.contains_key("additionalSpeedTiers")
        && !payload.contains_key("additional_speed_tiers")
    {
        payload.insert(
            "additional_speed_tiers".to_string(),
            json!(model.additional_speed_tiers),
        );
    }
    if payload.contains_key("serviceTiers") && !payload.contains_key("service_tiers") {
        payload.insert("service_tiers".to_string(), json!(model.service_tiers));
    }
    if payload.contains_key("isDefault") && !payload.contains_key("is_default") {
        payload.insert("is_default".to_string(), json!(model.is_default));
    }
    if model.enabled_present {
        payload.insert("enabled".to_string(), json!(model.enabled));
    }
    Value::Object(payload)
}

fn load_json_file(path: &Path) -> Result<Value, String> {
    let text = fs::read_to_string(path)
        .map_err(|error| format!("failed to read JSON file {}: {error}", path.display()))?;
    serde_json::from_str(&text)
        .map_err(|error| format!("failed to parse JSON file {}: {error}", path.display()))
}

fn first_string(object: &serde_json::Map<String, Value>, keys: &[&str]) -> Option<String> {
    keys.iter()
        .find_map(|key| object.get(*key).and_then(Value::as_str).and_then(nonblank))
}

fn first_string_array(
    object: &serde_json::Map<String, Value>,
    keys: &[&str],
) -> Option<Vec<String>> {
    keys.iter()
        .find_map(|key| object.get(*key).and_then(string_array))
        .filter(|values| !values.is_empty())
}

fn reasoning_level_entries(value: Option<&Value>) -> Vec<ReasoningLevelEntry> {
    let Some(values) = value.and_then(Value::as_array) else {
        return Vec::new();
    };
    values
        .iter()
        .filter_map(|item| {
            if let Some(text) = item.as_str().and_then(nonblank) {
                return Some(ReasoningLevelEntry {
                    effort: text,
                    description: None,
                });
            }
            let object = item.as_object()?;
            let effort = first_string(object, &["reasoningEffort", "effort"])?;
            Some(ReasoningLevelEntry {
                effort,
                description: first_string(object, &["description"]),
            })
        })
        .collect()
}

pub(crate) fn official_short_display_name(display_name: &str) -> String {
    let mut display_name = display_name.trim();
    if display_name
        .get(..7)
        .is_some_and(|prefix| prefix.eq_ignore_ascii_case("OpenAI "))
    {
        display_name = display_name.get(7..).unwrap_or_default().trim();
    }
    if display_name
        .get(..4)
        .is_some_and(|prefix| prefix.eq_ignore_ascii_case("GPT-"))
    {
        display_name = display_name.get(4..).unwrap_or_default();
    }
    display_name
        .replace(['-', '_'], " ")
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
}

fn current_unix_timestamp() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs())
        .unwrap_or(0)
}

fn discover_provider_models_with_timeout(
    base_url: &str,
    api_key: &str,
    timeout: Duration,
) -> Result<Vec<Model>, String> {
    let endpoint = provider_models_endpoint(base_url)?;
    let mut models = discover_models_http(
        &endpoint,
        api_key,
        timeout,
        DiscoveryKind::Provider,
        "provider models",
    )?;
    if let Some(show_endpoint) = ollama_show_endpoint(base_url) {
        let client = Client::builder()
            .timeout(timeout)
            .build()
            .map_err(|error| {
                format!("failed to build HTTP client for Ollama model metadata: {error}")
            })?;
        enrich_models_with_ollama_show(&client, &show_endpoint, api_key, &mut models);
    }
    Ok(models)
}

fn discover_models_http(
    endpoint: &str,
    api_key: &str,
    timeout: Duration,
    kind: DiscoveryKind,
    label: &str,
) -> Result<Vec<Model>, String> {
    let client = Client::builder()
        .timeout(timeout)
        .build()
        .map_err(|error| format!("failed to build HTTP client for {label}: {error}"))?;
    let mut request = client.get(endpoint).header(ACCEPT, "application/json");
    if let Some(api_key) = resolve_api_key(api_key)? {
        request = request.header(AUTHORIZATION, format!("Bearer {api_key}"));
    }

    let response = request.send().map_err(|error| {
        format!(
            "{label} discovery request failed: {}",
            safe_http_error(error)
        )
    })?;
    let status = response.status();
    if !status.is_success() {
        return Err(format!(
            "{label} discovery request failed with HTTP status {status}"
        ));
    }

    let payload = response.json::<Value>().map_err(|error| {
        format!(
            "{label} discovery response was not valid JSON: {}",
            safe_http_error(error)
        )
    })?;

    Ok(parse_discovered_models(&payload, kind))
}

fn provider_models_endpoint(base_url: &str) -> Result<String, String> {
    let base_url = base_url.trim();
    if base_url.is_empty() {
        return Err("provider base_url is required for model discovery".to_string());
    }

    let root = provider_endpoint_root(base_url);
    if base_url_path_matches(base_url, "/models") {
        Ok(base_url.trim_end_matches('/').to_string())
    } else if base_url_has_version_suffix(&root) {
        Ok(format!("{root}/models"))
    } else {
        Ok(format!("{root}/v1/models"))
    }
}

fn provider_api_endpoint(base_url: &str, path: &str) -> Result<String, String> {
    let base_url = base_url.trim();
    if base_url.is_empty() {
        return Err("provider base_url is required for endpoint connectivity test".to_string());
    }
    let path = if path.starts_with('/') {
        path.to_string()
    } else {
        format!("/{path}")
    };
    if base_url_path_matches(base_url, &path) {
        Ok(base_url.trim_end_matches('/').to_string())
    } else {
        let root = provider_endpoint_root(base_url);
        if base_url_has_version_suffix(&root) {
            Ok(format!("{root}{path}"))
        } else {
            Ok(format!("{root}/v1{path}"))
        }
    }
}

fn provider_endpoint_root(base_url: &str) -> String {
    let base_url = base_url.trim().trim_end_matches('/');
    let path = base_url_path(base_url);
    for suffix in KNOWN_PROVIDER_ENDPOINT_SUFFIXES {
        if path.ends_with(suffix) {
            return base_url[..base_url.len() - suffix.len()]
                .trim_end_matches('/')
                .to_string();
        }
    }
    base_url.to_string()
}

fn base_url_path_matches(base_url: &str, path: &str) -> bool {
    let base_path = base_url_path(base_url);
    let requested_path = path.to_ascii_lowercase();
    if requested_path == "/responses" {
        return RESPONSE_ENDPOINT_SUFFIXES
            .iter()
            .any(|suffix| base_path.ends_with(suffix));
    }
    base_path.ends_with(&requested_path)
}

fn base_url_path(base_url: &str) -> String {
    reqwest::Url::parse(base_url)
        .map(|url| url.path().trim_end_matches('/').to_ascii_lowercase())
        .unwrap_or_else(|_| base_url.trim_end_matches('/').to_ascii_lowercase())
}

fn base_url_has_version_suffix(base_url: &str) -> bool {
    let path = reqwest::Url::parse(base_url)
        .map(|url| url.path().trim_end_matches('/').to_string())
        .unwrap_or_else(|_| base_url.trim_end_matches('/').to_string());
    let Some(last_segment) = path.rsplit('/').next().filter(|value| !value.is_empty()) else {
        return false;
    };
    let Some(version) = last_segment
        .to_ascii_lowercase()
        .strip_prefix('v')
        .map(str::to_string)
    else {
        return false;
    };
    !version.is_empty()
        && version.chars().any(|value| value.is_ascii_digit())
        && version
            .chars()
            .all(|value| value.is_ascii_digit() || value == '.')
}

fn resolve_api_key(api_key: &str) -> Result<Option<String>, String> {
    let api_key = api_key.trim();
    if api_key.is_empty() {
        return Ok(None);
    }
    let Some(env_name) = env_placeholder_name(api_key)? else {
        return Ok(Some(api_key.to_string()));
    };
    let value = std::env::var(&env_name)
        .map_err(|_| format!("{env_name} is not set"))?
        .trim()
        .to_string();
    if value.is_empty() {
        return Err(format!("{env_name} is empty"));
    }
    Ok(Some(value))
}

fn resolve_gateway_api_key(base_url: &str, api_key: &str) -> Result<Option<String>, String> {
    resolve_gateway_api_key_for_settings(base_url, api_key, None)
}

fn resolve_gateway_api_key_for_settings(
    base_url: &str,
    api_key: &str,
    gateway_settings: Option<&Settings>,
) -> Result<Option<String>, String> {
    if let Some(api_key) = resolve_api_key(api_key)? {
        return Ok(Some(api_key));
    }
    if let Some(settings) = gateway_settings {
        return Ok(local_gateway_api_key_for_settings(base_url, settings));
    }
    Ok(config::get_settings()
        .ok()
        .and_then(|settings| local_gateway_api_key_for_settings(base_url, &settings)))
}

fn local_gateway_api_key_for_settings(base_url: &str, settings: &Settings) -> Option<String> {
    if !is_current_local_gateway_base_url(base_url, settings.proxy_port) {
        return None;
    }
    let key = settings.gateway_client_key.trim();
    (!key.is_empty()).then(|| key.to_string())
}

fn is_current_local_gateway_base_url(base_url: &str, proxy_port: u16) -> bool {
    let Ok(url) = reqwest::Url::parse(base_url.trim()) else {
        return false;
    };
    if url.scheme() != "http" {
        return false;
    }
    let Some(host) = url.host_str().map(str::to_ascii_lowercase) else {
        return false;
    };
    if !matches!(host.as_str(), "127.0.0.1" | "localhost" | "::1") {
        return false;
    }
    if url.port_or_known_default() != Some(proxy_port) {
        return false;
    }
    let path = url.path().trim_end_matches('/').to_ascii_lowercase();
    path.is_empty() || path == "/v1" || path.starts_with("/v1/")
}

fn env_placeholder_name(value: &str) -> Result<Option<String>, String> {
    if !value.starts_with("{env:") && !value.ends_with('}') {
        return Ok(None);
    }
    if !value.starts_with("{env:") || !value.ends_with('}') {
        return Err("invalid API key environment placeholder".to_string());
    }
    let name = &value[5..value.len() - 1];
    let mut chars = name.chars();
    let Some(first) = chars.next() else {
        return Err("invalid API key environment placeholder".to_string());
    };
    if !(first == '_' || first.is_ascii_alphabetic()) {
        return Err(format!("invalid API key environment placeholder: {name}"));
    }
    if chars.any(|ch| !(ch == '_' || ch.is_ascii_alphanumeric())) {
        return Err(format!("invalid API key environment placeholder: {name}"));
    }
    Ok(Some(name.to_string()))
}

fn is_direct_anthropic_endpoint(endpoint: &str) -> bool {
    reqwest::Url::parse(endpoint)
        .ok()
        .and_then(|url| url.host_str().map(|host| host.to_ascii_lowercase()))
        .is_some_and(|host| host == "api.anthropic.com" || host.ends_with(".anthropic.com"))
}

fn ollama_show_endpoint(base_url: &str) -> Option<String> {
    let mut url = reqwest::Url::parse(base_url.trim()).ok()?;
    let host = url.host_str()?.to_ascii_lowercase();
    let is_ollama_cloud = host == "ollama.com";
    let is_local_ollama =
        matches!(host.as_str(), "localhost" | "127.0.0.1" | "::1") && url.port() == Some(11434);
    if !is_ollama_cloud && !is_local_ollama {
        return None;
    }

    let path = url.path().trim_end_matches('/');
    let api_path = if let Some(prefix) = path.strip_suffix("/v1") {
        format!("{prefix}/api/show")
    } else if let Some(prefix) = path.strip_suffix("/api") {
        format!("{prefix}/api/show")
    } else {
        "/api/show".to_string()
    };
    url.set_path(&api_path);
    url.set_query(None);
    url.set_fragment(None);
    Some(url.to_string())
}

fn enrich_models_with_ollama_show(
    client: &Client,
    show_endpoint: &str,
    api_key: &str,
    models: &mut [Model],
) {
    let api_key = resolve_api_key(api_key).ok().flatten();
    for model in models {
        let mut request = client
            .post(show_endpoint)
            .header(CONTENT_TYPE, "application/json")
            .json(&serde_json::json!({ "model": model.id }));
        if let Some(api_key) = api_key.as_deref() {
            request = request.header(AUTHORIZATION, format!("Bearer {api_key}"));
        }
        let Ok(response) = request.send() else {
            continue;
        };
        if !response.status().is_success() {
            continue;
        }
        let Ok(payload) = response.json::<Value>() else {
            continue;
        };
        apply_ollama_show_metadata(model, &payload);
    }
}

fn apply_ollama_show_metadata(model: &mut Model, payload: &Value) {
    if let Some(context_window) = ollama_show_context_window(payload) {
        model.context_window = Some(context_window);
    }

    let capabilities = string_array(payload.get("capabilities").unwrap_or(&Value::Null))
        .unwrap_or_default()
        .into_iter()
        .map(|value| value.to_ascii_lowercase())
        .collect::<HashSet<_>>();

    if capabilities.contains("vision") {
        model.input_modalities = Some(vec!["text".to_string(), "image".to_string()]);
    }

    if capabilities.contains("thinking") || known_ollama_thinking_model(&model.id) {
        let levels = ollama_reasoning_levels(&model.id);
        model.default_reasoning_level = levels
            .iter()
            .find(|level| level.as_str() == "medium")
            .cloned()
            .or_else(|| levels.first().cloned());
        model.supported_reasoning_levels = Some(levels);
    }
}

fn ollama_show_context_window(payload: &Value) -> Option<u32> {
    let model_info = payload.get("model_info")?.as_object()?;
    model_info
        .iter()
        .filter_map(|(key, value)| {
            if key.ends_with(".context_length") || key == "context_length" {
                optional_u32(value)
            } else {
                None
            }
        })
        .max()
}

fn known_ollama_thinking_model(model_id: &str) -> bool {
    let model_id = model_id.to_ascii_lowercase();
    model_id.starts_with("qwen3")
        || model_id.contains("/qwen3")
        || model_id.starts_with("gpt-oss")
        || model_id.contains("/gpt-oss")
        || model_id.starts_with("deepseek-r1")
        || model_id.contains("/deepseek-r1")
        || model_id.starts_with("deepseek-v3.1")
        || model_id.contains("/deepseek-v3.1")
}

fn ollama_reasoning_levels(model_id: &str) -> Vec<String> {
    let model_id = model_id.to_ascii_lowercase();
    let levels = if model_id.starts_with("gpt-oss") || model_id.contains("/gpt-oss") {
        ["low", "medium", "high"].as_slice()
    } else {
        ["low", "medium", "high", "max"].as_slice()
    };
    levels.iter().map(|level| (*level).to_string()).collect()
}

fn safe_http_error(error: reqwest::Error) -> String {
    error.without_url().to_string()
}

fn compact_response_body(body: &str) -> String {
    let trimmed = body.trim();
    if trimmed.is_empty() {
        return "empty response".to_string();
    }
    let message = serde_json::from_str::<Value>(trimmed)
        .ok()
        .and_then(|value| {
            value
                .pointer("/error/message")
                .or_else(|| value.get("error"))
                .and_then(|item| {
                    if let Some(message) = item.as_str() {
                        Some(message.to_string())
                    } else if let Some(message) = item.get("message").and_then(Value::as_str) {
                        Some(message.to_string())
                    } else {
                        serde_json::to_string(item).ok()
                    }
                })
                .or_else(|| serde_json::to_string(&value).ok())
        })
        .unwrap_or_else(|| trimmed.replace(['\r', '\n'], " "));
    truncate_for_status(&message, 320)
}

fn truncate_for_status(value: &str, max_chars: usize) -> String {
    if value.chars().count() <= max_chars {
        return value.to_string();
    }
    let mut output = value.chars().take(max_chars).collect::<String>();
    output.push_str("...");
    output
}

#[derive(Debug, Clone, Copy)]
enum DiscoveryKind {
    #[cfg(test)]
    Official,
    Provider,
}

fn parse_discovered_models(payload: &Value, kind: DiscoveryKind) -> Vec<Model> {
    let mut models = Vec::new();
    let mut seen = HashSet::new();

    for item in payload_model_items(payload) {
        let Some(id) = discovered_model_id(item) else {
            continue;
        };
        if !discovery_allows_model(&id, kind) {
            continue;
        }
        if !seen.insert(id.clone()) {
            continue;
        }

        models.push(model_from_discovered_item(id, item));
    }

    sort_discovered_models(&mut models, kind);

    models
}

fn discovery_allows_model(_id: &str, kind: DiscoveryKind) -> bool {
    match kind {
        #[cfg(test)]
        DiscoveryKind::Official => _id.starts_with("gpt-"),
        DiscoveryKind::Provider => true,
    }
}

fn sort_discovered_models(_models: &mut [Model], kind: DiscoveryKind) {
    match kind {
        #[cfg(test)]
        DiscoveryKind::Official => _models.sort_by(|left, right| left.id.cmp(&right.id)),
        DiscoveryKind::Provider => {}
    }
}

fn payload_model_items(payload: &Value) -> Vec<&Value> {
    if let Some(items) = payload.as_array() {
        return items.iter().collect();
    }

    if let Some(document) = payload.as_object() {
        if let Some(items) = document.get("data").and_then(Value::as_array) {
            return items.iter().collect();
        }
        if let Some(items) = document.get("models").and_then(Value::as_array) {
            return items.iter().collect();
        }
    }

    Vec::new()
}

fn discovered_model_id(value: &Value) -> Option<String> {
    if let Some(text) = value.as_str() {
        return nonblank(text);
    }

    let object = value.as_object()?;
    for key in ["id", "model", "name", "slug"] {
        if let Some(text) = object.get(key).and_then(Value::as_str) {
            if let Some(model_id) = nonblank(text) {
                return Some(model_id);
            }
        }
    }

    None
}

fn model_from_discovered_item(id: String, item: &Value) -> Model {
    Model {
        id,
        display_name: None,
        upstream_model: None,
        context_window: numeric_limit(
            item,
            &["context_window", "max_context_window", "context_length"],
            "context",
        ),
        max_output_tokens: numeric_limit(item, &["max_output_tokens", "output_tokens"], "output"),
        ..Model::default()
    }
}

fn numeric_limit(item: &Value, keys: &[&str], nested_limit_key: &str) -> Option<u32> {
    let object = item.as_object()?;
    for key in keys {
        if let Some(value) = object.get(*key).and_then(optional_u32) {
            return Some(value);
        }
    }

    object
        .get("limit")
        .and_then(Value::as_object)
        .and_then(|limit| limit.get(nested_limit_key))
        .and_then(optional_u32)
}

fn optional_u32(value: &Value) -> Option<u32> {
    if let Some(value) = value.as_u64() {
        return u32::try_from(value).ok();
    }
    value.as_str().and_then(|text| text.trim().parse().ok())
}

fn optional_i32(value: &Value) -> Option<i32> {
    if let Some(value) = value.as_i64() {
        return i32::try_from(value).ok();
    }
    value.as_str().and_then(|text| text.trim().parse().ok())
}

fn nonblank(value: &str) -> Option<String> {
    let value = value.trim();
    if value.is_empty() {
        None
    } else {
        Some(value.to_string())
    }
}

#[derive(Debug, Clone)]
struct ModelPaths {
    codex_dir: PathBuf,
    repo_root: PathBuf,
}

impl ModelPaths {
    fn runtime() -> Result<Self, String> {
        let codex_dir = runtime_paths::codex_home_dir()?;
        let repo_root = runtime_paths::resource_root()?;

        Ok(Self::new(codex_dir, repo_root))
    }

    fn new(codex_dir: impl Into<PathBuf>, repo_root: impl Into<PathBuf>) -> Self {
        Self {
            codex_dir: codex_dir.into(),
            repo_root: repo_root.into(),
        }
    }

    fn catalog_sync_script(&self) -> PathBuf {
        self.repo_root.join("src-python").join("catalog_sync.py")
    }

    fn upstream_format_probe_script(&self) -> PathBuf {
        self.repo_root
            .join("src-python")
            .join("probe_upstream_format.py")
    }

    fn generated_catalog_path(&self) -> PathBuf {
        self.codex_dir
            .join("model-catalogs")
            .join(GENERATED_CATALOG_FILE)
    }

    fn legacy_generated_catalog_path(&self) -> PathBuf {
        self.codex_dir
            .join("model-catalogs")
            .join(LEGACY_GENERATED_CATALOG_FILE)
    }

    fn official_subscription_cache_path(&self) -> PathBuf {
        self.codex_dir
            .join("model-catalogs")
            .join("openai-plus-ollama-cloud.json")
    }

    fn existing_generated_catalog_path(&self) -> PathBuf {
        let catalog_path = self.generated_catalog_path();
        if catalog_path.exists() {
            return catalog_path;
        }
        let legacy_path = self.legacy_generated_catalog_path();
        if legacy_path.exists() {
            return legacy_path;
        }
        catalog_path
    }

    fn metadata_cache_path(&self) -> PathBuf {
        self.codex_dir
            .join("proxy")
            .join("model-metadata-cache.json")
    }

    fn metadata_overrides_path(&self) -> PathBuf {
        self.codex_dir
            .join("proxy")
            .join("model-metadata-overrides.json")
    }

    fn catalog_overrides_path(&self) -> PathBuf {
        self.codex_dir
            .join("model-catalogs")
            .join("codexhub-model-catalog-overrides.json")
    }

    fn managed_catalog_baseline_path(&self) -> PathBuf {
        self.codex_dir
            .join("model-catalogs")
            .join("codexhub-model-catalog-baseline.json")
    }
}

fn qualified_official_code_mode_multi_agent_version(slug: &str) -> Option<&'static str> {
    // Sol/Terra retain catalog baselines, but only an accepted GO matrix row
    // may be exposed as a user-selectable model override.
    match slug {
        "gpt-5.6-luna" => Some("v1"),
        _ => None,
    }
}

fn model_has_exact_official_upstream(model: &Model, canonical_model_id: &str) -> bool {
    model
        .upstream_model
        .as_deref()
        .map(|value| value.strip_prefix("openai/").unwrap_or(value) == canonical_model_id)
        .unwrap_or(false)
}

fn set_catalog_multi_agent_version(
    paths: &ModelPaths,
    canonical_model_id: &str,
    version: &str,
) -> Result<(), String> {
    let path = paths.generated_catalog_path();
    let text = fs::read_to_string(&path)
        .map_err(|error| format!("failed to read generated catalog: {error}"))?;
    let mut payload: Value = serde_json::from_str(&text)
        .map_err(|error| format!("generated catalog is invalid: {error}"))?;
    let models = payload
        .get_mut("models")
        .and_then(Value::as_array_mut)
        .ok_or_else(|| "generated catalog does not contain a models array".to_string())?;

    let mut found = false;
    for item in models {
        let Some(object) = item.as_object_mut() else {
            continue;
        };
        let Some(metadata) = object
            .get("codex_proxy_metadata")
            .and_then(Value::as_object)
        else {
            continue;
        };
        let is_exact_official_identity = metadata.get("provider").and_then(Value::as_str)
            == Some("openai")
            && metadata.get("upstream_name").and_then(Value::as_str) == Some("official")
            && metadata.get("upstream_model").and_then(Value::as_str)
                == Some(canonical_model_id);
        if !is_exact_official_identity {
            continue;
        }
        object.insert(
            "multi_agent_version".to_string(),
            Value::String(version.to_string()),
        );
        found = true;
    }

    if !found {
        return Err(format!(
            "selected Official model is not present in generated catalog: {canonical_model_id}"
        ));
    }

    let serialized = serde_json::to_string_pretty(&payload)
        .map_err(|error| format!("failed to serialize generated catalog: {error}"))?;
    safe_file::write_text_atomic(&path, &format!("{serialized}\n"))
        .map_err(|error| format!("failed to publish generated catalog: {error}"))
}

fn read_managed_catalog_multi_agent_version(
    paths: &ModelPaths,
    canonical_model_id: &str,
) -> Option<String> {
    let path = paths.managed_catalog_baseline_path();
    let text = fs::read_to_string(path).ok()?;
    let payload = serde_json::from_str::<Value>(&text).ok()?;
    let models = payload.get("models").and_then(Value::as_array)?;
    models.iter().find_map(|item| {
        let object = item.as_object()?;
        let metadata = object.get("codex_proxy_metadata").and_then(Value::as_object)?;
        let exact_identity = metadata.get("provider").and_then(Value::as_str) == Some("openai")
            && metadata.get("upstream_name").and_then(Value::as_str) == Some("official")
            && metadata.get("upstream_model").and_then(Value::as_str)
                == Some(canonical_model_id);
        if !exact_identity {
            return None;
        }
        object
            .get("multi_agent_version")
            .and_then(Value::as_str)
            .filter(|value| *value == "v1" || *value == "v2")
            .map(str::to_string)
    })
}

fn apply_catalog_multi_agent_overrides(paths: &ModelPaths, models: &mut [Model]) {
    let Ok(text) = fs::read_to_string(paths.catalog_overrides_path()) else {
        return;
    };
    let Ok(payload) = serde_json::from_str::<Value>(&text) else {
        return;
    };
    let Some(entries) = payload.get("overrides").and_then(Value::as_array) else {
        return;
    };
    let mut values = HashMap::<String, String>::new();
    for entry in entries {
        if entry.get("provider").and_then(Value::as_str) != Some("openai")
            || entry.get("upstream_name").and_then(Value::as_str) != Some("official")
        {
            continue;
        }
        let Some(model_id) = entry.get("upstream_model").and_then(Value::as_str) else {
            continue;
        };
        let canonical_model_id = model_id.strip_prefix("openai/").unwrap_or(model_id);
        if qualified_official_code_mode_multi_agent_version(canonical_model_id).is_none() {
            continue;
        }
        let Some(version) = entry
            .get("fields")
            .and_then(Value::as_object)
            .and_then(|fields| fields.get("multi_agent_version"))
            .and_then(Value::as_str)
            .filter(|value| *value == "v1" || *value == "v2")
        else {
            continue;
        };
        values.insert(canonical_model_id.to_string(), version.to_string());
    }
    for model in models.iter_mut() {
        if model.source_kind.as_deref() != Some("official") {
            continue;
        }
        let canonical = model
            .id
            .strip_prefix("openai/")
            .unwrap_or(model.id.as_str());
        if model_is_catalog_visible(model)
            && model_has_exact_official_upstream(model, canonical)
            && qualified_official_code_mode_multi_agent_version(canonical).is_some()
        {
            if let Some(version) = values.get(canonical) {
                model.multi_agent_version = Some(version.clone());
            }
        }
    }
}

#[derive(Debug, Clone)]
struct CatalogCommandOutcome {
    code: Option<i32>,
    stdout: String,
    stderr: String,
}

trait CatalogSyncRunner {
    fn run_sync(
        &self,
        python: &Path,
        script: &Path,
        codex_dir: &Path,
    ) -> Result<CatalogCommandOutcome, String>;
}

struct ProcessCatalogSyncRunner;

impl CatalogSyncRunner for ProcessCatalogSyncRunner {
    fn run_sync(
        &self,
        python: &Path,
        script: &Path,
        codex_dir: &Path,
    ) -> Result<CatalogCommandOutcome, String> {
        let mut command = Command::new(python);
        command
            .arg(script)
            .arg("--sync")
            .env("CODEX_HOME", codex_dir);
        configure_no_window(&mut command);
        let output = command
            .output()
            .map_err(|error| format!("failed to start catalog sync: {error}"))?;

        Ok(CatalogCommandOutcome {
            code: output.status.code(),
            stdout: String::from_utf8_lossy(&output.stdout).to_string(),
            stderr: String::from_utf8_lossy(&output.stderr).to_string(),
        })
    }
}

fn generate_catalog_with_runner(
    paths: &ModelPaths,
    python: &Path,
    runner: &dyn CatalogSyncRunner,
) -> Result<Vec<Model>, String> {
    let script = paths.catalog_sync_script();
    if !script.exists() {
        return Err(format!(
            "catalog sync script not found: {}",
            script.display()
        ));
    }

    let catalog_path = paths.generated_catalog_path();
    if let Some(parent) = catalog_path.parent() {
        fs::create_dir_all(parent).map_err(|error| {
            format!(
                "failed to create catalog output directory {}: {error}",
                parent.display()
            )
        })?;
    }

    let outcome = runner.run_sync(python, &script, &paths.codex_dir)?;
    if outcome.code != Some(0) {
        return Err(format!(
            "catalog sync failed with {}\nstdout:\n{}\nstderr:\n{}",
            format_exit_code(outcome.code),
            outcome.stdout.trim_end(),
            outcome.stderr.trim_end()
        ));
    }

    let mut models = read_catalog_models(&catalog_path)?;
    apply_catalog_multi_agent_overrides(paths, &mut models);
    Ok(models)
}

fn read_catalog_models(path: &Path) -> Result<Vec<Model>, String> {
    read_catalog_models_matching(path, |_| true)
}

pub(crate) fn read_official_catalog_models(path: &Path) -> Result<Vec<Model>, String> {
    read_catalog_models_matching(path, |item| {
        item.get("codex_proxy_metadata")
            .and_then(Value::as_object)
            .is_some_and(|metadata| {
                metadata.get("provider").and_then(Value::as_str) == Some("openai")
                    && metadata.get("upstream_name").and_then(Value::as_str) == Some("official")
            })
    })
}

fn read_catalog_models_matching(
    path: &Path,
    include: impl Fn(&Value) -> bool,
) -> Result<Vec<Model>, String> {
    let text = fs::read_to_string(path)
        .map_err(|error| format!("failed to read catalog JSON {}: {error}", path.display()))?;
    let payload: Value = serde_json::from_str(&text)
        .map_err(|error| format!("failed to parse catalog JSON {}: {error}", path.display()))?;
    let items = payload
        .get("models")
        .and_then(Value::as_array)
        .ok_or_else(|| {
            format!(
                "catalog JSON {} does not contain a models array",
                path.display()
            )
        })?;

    let mut seen = HashSet::new();
    let mut models = Vec::new();
    for item in items {
        if !include(item) {
            continue;
        }
        let Some(model) = catalog_model_from_item(item) else {
            continue;
        };
        if seen.insert(model.id.clone()) {
            models.push(model);
        }
    }

    Ok(models)
}

fn read_metadata_cache(paths: &ModelPaths) -> Result<Vec<Model>, String> {
    read_models_json(&paths.metadata_cache_path())
}

fn read_metadata_overrides(paths: &ModelPaths) -> Result<Vec<Model>, String> {
    read_models_json(&paths.metadata_overrides_path())
}

fn read_models_json(path: &Path) -> Result<Vec<Model>, String> {
    if !path.exists() {
        return Ok(Vec::new());
    }
    let text = fs::read_to_string(path)
        .map_err(|error| format!("failed to read model metadata {}: {error}", path.display()))?;
    serde_json::from_str(&text)
        .map_err(|error| format!("failed to parse model metadata {}: {error}", path.display()))
}

fn write_models_json(path: &Path, models: &[Model]) -> Result<(), String> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|error| {
            format!(
                "failed to create model metadata directory {}: {error}",
                parent.display()
            )
        })?;
    }
    let text = serde_json::to_string_pretty(models)
        .map_err(|error| format!("failed to serialize model metadata: {error}"))?;
    safe_file::write_text_atomic(path, &format!("{text}\n"))
        .map_err(|error| format!("failed to write model metadata {}: {error}", path.display()))
}

fn merge_metadata_with_overrides(
    base: Vec<Model>,
    overrides: Vec<Model>,
    known_official_models: &HashSet<String>,
) -> Vec<Model> {
    let mut base = base
        .into_iter()
        .filter_map(|mut model| {
            model.id = config::normalize_official_model_id(&model.id, known_official_models)?;
            Some(model)
        })
        .collect::<Vec<_>>();
    for mut override_model in overrides {
        let Some(model_id) =
            config::normalize_official_model_id(&override_model.id, known_official_models)
        else {
            continue;
        };
        override_model.id = model_id;
        override_model.metadata_provenance = Some(MetadataProvenance {
            source: "user_override".to_string(),
            source_url: None,
            fetched_at: None,
            confidence: "user".to_string(),
        });
        if let Some(existing) = base.iter_mut().find(|model| model.id == override_model.id) {
            merge_model_override(existing, override_model);
        } else {
            base.push(override_model);
        }
    }
    base.sort_by(|left, right| left.id.cmp(&right.id));
    base
}

fn merge_model_override(base: &mut Model, override_model: Model) {
    let original_enabled = base.enabled;
    let original_codex_enabled = base.codex_enabled;
    let original_gateway_exported = base.gateway_exported;
    let original_visibility = base.visibility;
    let visibility = match (original_visibility, override_model.visibility) {
        (CatalogVisibility::Hide, _) | (_, CatalogVisibility::Hide) => CatalogVisibility::Hide,
        (CatalogVisibility::Unknown, _) | (_, CatalogVisibility::Unknown) => {
            CatalogVisibility::Unknown
        }
        _ => CatalogVisibility::List,
    };
    let aliases = merge_model_aliases(base.aliases.clone(), override_model.aliases);
    *base = Model {
        id: override_model.id,
        display_name: override_model.display_name.or(base.display_name.take()),
        upstream_model: override_model.upstream_model.or(base.upstream_model.take()),
        tool_surface_strategy: override_model
            .tool_surface_strategy
            .or(base.tool_surface_strategy.take()),
        multi_agent_version: override_model
            .multi_agent_version
            .or(base.multi_agent_version.take()),
        aliases,
        source_kind: override_model.source_kind.or(base.source_kind.take()),
        locked: base.locked || override_model.locked,
        codex_enabled: override_model.codex_enabled && original_codex_enabled,
        gateway_exported: override_model.gateway_exported && original_gateway_exported,
        visibility,
        context_window: override_model.context_window.or(base.context_window),
        max_context_window: override_model.max_context_window.or(base.max_context_window),
        effective_source: override_model.effective_source.or(base.effective_source.take()),
        max_source: override_model.max_source.or(base.max_source.take()),
        confidence: override_model.confidence.or(base.confidence.take()),
        verified_at: override_model.verified_at.or(base.verified_at.take()),
        max_output_tokens: override_model.max_output_tokens.or(base.max_output_tokens),
        input_modalities: override_model
            .input_modalities
            .or(base.input_modalities.take()),
        supported_reasoning_levels: override_model
            .supported_reasoning_levels
            .or(base.supported_reasoning_levels.take()),
        default_reasoning_level: override_model
            .default_reasoning_level
            .or(base.default_reasoning_level.take()),
        pricing: override_model.pricing.or(base.pricing.take()),
        metadata_provenance: override_model.metadata_provenance,
        sort_order: override_model.sort_order.or(base.sort_order),
        enabled: override_model.enabled && original_enabled,
    };
}

fn merge_model_aliases(mut base: Vec<String>, overrides: Vec<String>) -> Vec<String> {
    for alias in overrides {
        let alias = alias.trim().to_string();
        if !alias.is_empty() && !base.iter().any(|existing| existing == &alias) {
            base.push(alias);
        }
    }
    base
}

fn builtin_model_metadata() -> Vec<Model> {
    vec![
        official_resolved_metadata("gpt-5.6-sol", "GPT-5.6 Sol"),
        official_resolved_metadata("gpt-5.6-terra", "GPT-5.6 Terra"),
        official_resolved_metadata("gpt-5.6-luna", "GPT-5.6 Luna"),
        official_priced_metadata(
            "gpt-5.5",
            "GPT-5.5",
            272_000,
            "https://developers.openai.com/api/docs/pricing",
            5.00,
            Some(0.50),
            22.50,
        ),
        official_priced_metadata(
            "gpt-5.4",
            "GPT-5.4",
            272_000,
            "https://developers.openai.com/api/docs/pricing",
            2.50,
            Some(0.25),
            11.25,
        ),
        official_priced_metadata(
            "gpt-5.4-mini",
            "GPT-5.4 mini",
            272_000,
            "https://developers.openai.com/api/docs/pricing",
            0.375,
            Some(0.0375),
            2.25,
        ),
        official_metadata("gpt-5.3-codex-spark", "GPT-5.3 Codex Spark", 128_000),
        priced_metadata(
            "zai/glm-5.2",
            "GLM 5.2",
            "external",
            1_000_000,
            "https://docs.z.ai/guides/llm/glm-5.2",
            Some((0.30, 0.03, 1.20)),
        ),
        priced_metadata(
            "moonshot/kimi-k2.7-code",
            "Kimi K2.7 Code",
            "external",
            256_000,
            "https://platform.kimi.ai/docs/guide/kimi-k2-7-code-quickstart",
            None,
        ),
        priced_metadata(
            "minimax/minimax-m3",
            "MiniMax M3",
            "external",
            1_000_000,
            "https://platform.minimax.io/docs/guides/text-generation",
            None,
        ),
        priced_metadata(
            "deepseek/deepseek-chat",
            "DeepSeek Chat",
            "external",
            128_000,
            "https://api-docs.deepseek.com/quick_start/pricing",
            Some((0.27, 0.07, 1.10)),
        ),
        Model {
            id: "ollama/glm-5.2".to_string(),
            display_name: Some("GLM 5.2 via Ollama".to_string()),
            source_kind: Some("external".to_string()),
            context_window: Some(128_000),
            pricing: None,
            metadata_provenance: Some(MetadataProvenance {
                source: "official".to_string(),
                source_url: Some("https://docs.ollama.com/api/openai-compatibility".to_string()),
                fetched_at: None,
                confidence: "medium".to_string(),
            }),
            ..Model::default()
        },
    ]
}

#[derive(Debug, Deserialize)]
struct ResolvedLimitsDocument {
    entries: Vec<ResolvedLimitsEntry>,
}

#[derive(Debug, Deserialize)]
struct ResolvedLimitsEntry {
    provider_id: String,
    model_id: String,
    effective_context_window: Option<u32>,
    max_context_window: Option<u32>,
    max_output_tokens: Option<u32>,
    effective_source: Option<String>,
    max_source: Option<String>,
    confidence: String,
    verified_at: Option<String>,
}

pub(crate) fn apply_resolved_model_limits(provider_id: &str, model: &mut Model) -> bool {
    let document: ResolvedLimitsDocument = serde_json::from_str(RESOLVED_MODEL_LIMITS_JSON)
        .expect("bundled resolved model limits must be valid JSON");
    let Some(limits) = document
        .entries
        .into_iter()
        .find(|entry| entry.provider_id == provider_id && entry.model_id == model.id)
    else {
        return false;
    };

    model.context_window = limits.effective_context_window;
    model.max_context_window = limits.max_context_window;
    if model.max_output_tokens.is_none() {
        model.max_output_tokens = limits.max_output_tokens;
    }
    model.effective_source = limits.effective_source;
    model.max_source = limits.max_source;
    model.confidence = Some(limits.confidence);
    model.verified_at = limits.verified_at;
    true
}

fn official_resolved_metadata(id: &str, display_name: &str) -> Model {
    let mut model = Model {
        id: id.to_string(),
        display_name: Some(display_name.to_string()),
        source_kind: Some("official".to_string()),
        locked: true,
        multi_agent_version: pinned_official_code_mode_multi_agent_version(id).map(str::to_string),
        input_modalities: Some(vec!["text".to_string(), "image".to_string()]),
        ..Model::default()
    };
    assert!(
        apply_resolved_model_limits("openai", &mut model),
        "official resolved model limit must exist"
    );
    model
}

fn official_priced_metadata(
    id: &str,
    display_name: &str,
    context_window: u32,
    source_url: &str,
    input_per_million: f64,
    cached_input_per_million: Option<f64>,
    output_per_million: f64,
) -> Model {
    let mut model = official_metadata(id, display_name, context_window);
    model.pricing = Some(ModelPricing {
        input_per_million: Some(input_per_million),
        cached_input_per_million,
        output_per_million: Some(output_per_million),
        currency: "USD".to_string(),
        source: "official".to_string(),
        estimate: true,
    });
    model.metadata_provenance = Some(MetadataProvenance {
        source: "official".to_string(),
        source_url: Some(source_url.to_string()),
        fetched_at: None,
        confidence: "medium".to_string(),
    });
    model
}

fn official_metadata(id: &str, display_name: &str, context_window: u32) -> Model {
    Model {
        id: id.to_string(),
        display_name: Some(display_name.to_string()),
        source_kind: Some("official".to_string()),
        locked: true,
        multi_agent_version: pinned_official_code_mode_multi_agent_version(id).map(str::to_string),
        context_window: Some(context_window),
        input_modalities: Some(vec!["text".to_string(), "image".to_string()]),
        supported_reasoning_levels: Some(vec![
            "low".to_string(),
            "medium".to_string(),
            "high".to_string(),
            "xhigh".to_string(),
            "max".to_string(),
        ]),
        default_reasoning_level: Some("medium".to_string()),
        metadata_provenance: Some(MetadataProvenance {
            source: "official".to_string(),
            source_url: Some("https://developers.openai.com/api/docs/models".to_string()),
            fetched_at: None,
            confidence: "high".to_string(),
        }),
        ..Model::default()
    }
}

fn priced_metadata(
    id: &str,
    display_name: &str,
    source_kind: &str,
    context_window: u32,
    source_url: &str,
    pricing: Option<(f64, f64, f64)>,
) -> Model {
    Model {
        id: id.to_string(),
        display_name: Some(display_name.to_string()),
        source_kind: Some(source_kind.to_string()),
        context_window: Some(context_window),
        pricing: pricing.map(|(input, cached, output)| ModelPricing {
            input_per_million: Some(input),
            cached_input_per_million: Some(cached),
            output_per_million: Some(output),
            currency: "USD".to_string(),
            source: "official".to_string(),
            estimate: true,
        }),
        metadata_provenance: Some(MetadataProvenance {
            source: "official".to_string(),
            source_url: Some(source_url.to_string()),
            fetched_at: None,
            confidence: "medium".to_string(),
        }),
        ..Model::default()
    }
}

fn catalog_model_from_item(item: &Value) -> Option<Model> {
    let object = item.as_object()?;
    // Generated catalogs predate the typed visibility field and also contain
    // trusted third-party provider entries that never carried an upstream
    // visibility flag.  Preserve that legacy list default at this boundary;
    // raw app-server/upstream ingestion remains fail-closed through
    // `subscription_model_from_item` and `catalog_visibility_from_item`.
    let listable = if object.contains_key("visibility") || object.contains_key("hidden") {
        catalog_visibility_from_item(object) == CatalogVisibility::List
    } else {
        true
    };
    if item_has_internal_identity(object) || !listable {
        return None;
    }
    let id = object
        .get("slug")
        .or_else(|| object.get("id"))
        .or_else(|| object.get("model"))
        .or_else(|| object.get("name"))
        .and_then(Value::as_str)
        .and_then(nonblank)?;

    Some(Model {
        id,
        display_name: object
            .get("display_name")
            .and_then(Value::as_str)
            .and_then(nonblank),
        upstream_model: object
            .get("upstream_model")
            .and_then(Value::as_str)
            .and_then(nonblank)
            .or_else(|| {
                object
                    .get("codex_proxy_metadata")
                    .and_then(Value::as_object)
                    .and_then(|metadata| metadata.get("upstream_model"))
                    .and_then(Value::as_str)
                    .and_then(nonblank)
            }),
        tool_surface_strategy: None,
        multi_agent_version: object
            .get("multi_agent_version")
            .and_then(Value::as_str)
            .map(str::to_string),
        aliases: object
            .get("aliases")
            .and_then(string_array)
            .unwrap_or_default(),
        context_window: numeric_limit(
            item,
            &["context_window", "max_context_window", "context_length"],
            "context",
        ),
        max_context_window: object.get("max_context_window").and_then(optional_u32),
        effective_source: object.get("effective_source").and_then(Value::as_str).and_then(nonblank),
        max_source: object.get("max_source").and_then(Value::as_str).and_then(nonblank),
        confidence: object.get("confidence").and_then(Value::as_str).and_then(nonblank),
        verified_at: object.get("verified_at").and_then(Value::as_str).and_then(nonblank),
        max_output_tokens: numeric_limit(item, &["max_output_tokens", "output_tokens"], "output"),
        input_modalities: object.get("input_modalities").and_then(string_array),
        supported_reasoning_levels: object
            .get("supported_reasoning_levels")
            .and_then(reasoning_efforts),
        default_reasoning_level: object
            .get("default_reasoning_level")
            .and_then(Value::as_str)
            .and_then(nonblank),
        sort_order: object.get("priority").and_then(optional_i32),
        enabled: object
            .get("enabled")
            .and_then(Value::as_bool)
            .unwrap_or(true),
        source_kind: catalog_source_kind(object),
        locked: object
            .get("locked")
            .and_then(Value::as_bool)
            .unwrap_or(false),
        codex_enabled: object
            .get("codex_enabled")
            .and_then(Value::as_bool)
            .unwrap_or(true),
        gateway_exported: object
            .get("gateway_exported")
            .and_then(Value::as_bool)
            .unwrap_or(true),
        visibility: CatalogVisibility::List,
        pricing: None,
        metadata_provenance: None,
    })
}

fn catalog_source_kind(object: &Map<String, Value>) -> Option<String> {
    // The provider/upstream tuple is the authoritative catalog identity.  A
    // top-level `source_kind` without that tuple is only a presentation hint
    // and must not promote a same-slug row into the Official surface.
    if let Some(metadata) = object
        .get("codex_proxy_metadata")
        .and_then(Value::as_object)
    {
        let provider = metadata.get("provider").and_then(Value::as_str);
        let upstream_name = metadata.get("upstream_name").and_then(Value::as_str);
        return Some(if provider == Some("openai") && upstream_name == Some("official") {
            "official".to_string()
        } else {
            "external".to_string()
        });
    }
    None
}

fn string_array(value: &Value) -> Option<Vec<String>> {
    let values = value.as_array()?;
    Some(
        values
            .iter()
            .filter_map(Value::as_str)
            .filter_map(nonblank)
            .collect(),
    )
}

fn reasoning_efforts(value: &Value) -> Option<Vec<String>> {
    let values = value.as_array()?;
    Some(
        values
            .iter()
            .filter_map(|item| {
                if let Some(text) = item.as_str() {
                    return nonblank(text);
                }
                item.as_object()
                    .and_then(|object| object.get("effort"))
                    .and_then(Value::as_str)
                    .and_then(nonblank)
            })
            .collect(),
    )
}

fn format_exit_code(code: Option<i32>) -> String {
    code.map_or_else(
        || "no exit code".to_string(),
        |code| format!("exit code {code}"),
    )
}

fn find_python() -> PathBuf {
    let resource_root = runtime_paths::resource_root().ok();
    runtime_paths::find_python(resource_root.as_deref())
}

fn find_codex_executable() -> Result<PathBuf, String> {
    if let Some(path) = std::env::var_os("CODEXHUB_CODEX_PATH")
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
    {
        return Ok(path);
    }
    if let Some(path) = desktop_codex_exe() {
        return Ok(path);
    }
    if let Some(path) = npm_codex_vendor_exe() {
        return Ok(path);
    }
    for candidate in codex_executable_candidates() {
        if let Ok(path) = which::which(candidate) {
            return Ok(path);
        }
    }
    Err(
        "Codex subscription model refresh requires the Codex CLI to be installed and on PATH."
            .to_string(),
    )
}

fn desktop_codex_exe() -> Option<PathBuf> {
    let local_appdata = std::env::var_os("LOCALAPPDATA")?;
    desktop_codex_exe_from_local_appdata(Path::new(&local_appdata))
}

fn desktop_codex_exe_from_local_appdata(local_appdata: &Path) -> Option<PathBuf> {
    let bin_dir = local_appdata.join("OpenAI").join("Codex").join("bin");
    let mut candidates = fs::read_dir(bin_dir)
        .ok()?
        .filter_map(Result::ok)
        .map(|entry| entry.path().join("codex.exe"))
        .filter(|path| path.is_file())
        .collect::<Vec<_>>();
    candidates.sort_by_key(|path| {
        fs::metadata(path)
            .and_then(|metadata| metadata.modified())
            .unwrap_or(UNIX_EPOCH)
    });
    candidates.pop()
}

fn npm_codex_vendor_exe() -> Option<PathBuf> {
    let appdata = std::env::var_os("APPDATA")?;
    let path = PathBuf::from(appdata)
        .join("npm")
        .join("node_modules")
        .join("@openai")
        .join("codex")
        .join("node_modules")
        .join("@openai")
        .join("codex-win32-x64")
        .join("vendor")
        .join("x86_64-pc-windows-msvc")
        .join("codex")
        .join("codex.exe");
    path.exists().then_some(path)
}

fn codex_executable_candidates() -> Vec<&'static str> {
    vec!["codex.cmd", "codex", "codex.exe"]
}

#[cfg(test)]
mod tests {
    use super::{
        desktop_codex_exe_from_local_appdata, discover_provider_models_with_timeout,
        enrich_models_with_ollama_show, generate_catalog_with_runner, list_model_metadata,
        list_models, list_official_multi_agent_overrides, load_json_file,
        merge_metadata_with_overrides, ollama_show_endpoint,
        provider_api_endpoint,
        provider_models_endpoint, read_models_json, refresh_official_models_from_endpoint,
        refresh_official_models_with_runner, resolve_gateway_api_key_for_settings,
        subscription_models_from_payload, subscription_models_to_metadata_models,
        test_model_endpoint_with_timeout, visibility_diagnostics_from_payload,
        AppServerModelListRunner, CatalogCommandOutcome, CatalogSyncRunner, ModelPaths,
    };
    use crate::{MetadataProvenance, Model, Settings, ToolSurfaceStrategy, UpstreamFormat};
    use reqwest::blocking::Client;
    use serde_json::{json, Value};
    use std::cell::RefCell;
    use std::collections::HashSet;
    use std::fs;
    use std::io::{Read, Write};
    use std::net::TcpListener;
    use std::path::{Path, PathBuf};
    use std::sync::mpsc::{self, Receiver};
    use std::sync::Mutex;
    use std::thread::{self, JoinHandle};
    use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

    static ENV_LOCK: Mutex<()> = Mutex::new(());
    const APP_SERVER_TEST_PROCESS_ENV: &str = "CODEXHUB_APP_SERVER_TEST_PROCESS";
    const APP_SERVER_TEST_PROCESS_LIVENESS_ENV: &str =
        "CODEXHUB_APP_SERVER_TEST_PROCESS_LIVENESS";
    const APP_SERVER_TEST_PROCESS_HELPER: &str =
        "models::tests::app_server_model_list_test_process_helper";

    #[test]
    #[ignore = "launched as a subprocess by app-server model-list tests"]
    fn app_server_model_list_test_process_helper() {
        let mode = std::env::var(APP_SERVER_TEST_PROCESS_ENV);
        if !matches!(
            mode.as_deref(),
            Ok("respond" | "respond-without-context" | "silent")
        ) {
            return;
        }
        let liveness_path = PathBuf::from(
            std::env::var_os(APP_SERVER_TEST_PROCESS_LIVENESS_ENV)
                .expect("app-server test-process liveness path"),
        );
        let liveness_listener = std::net::TcpListener::bind(("127.0.0.1", 0))
            .expect("bind app-server test-process liveness listener");
        let liveness_address = liveness_listener
            .local_addr()
            .expect("read app-server test-process liveness address");
        let mut liveness_options = fs::OpenOptions::new();
        liveness_options.create_new(true).read(true).write(true);
        #[cfg(windows)]
        {
            use std::os::windows::fs::OpenOptionsExt;
            liveness_options.share_mode(0);
        }
        let mut liveness = liveness_options
            .open(&liveness_path)
            .expect("hold app-server test-process liveness file");
        writeln!(
            liveness,
            "pid={}\naddress={liveness_address}",
            std::process::id()
        )
        .expect("write app-server test-process liveness record");
        liveness
            .flush()
            .expect("flush app-server test-process liveness record");

        if matches!(mode.as_deref(), Ok("respond" | "respond-without-context")) {
            let context = if mode.as_deref() == Ok("respond") {
                Some(272000)
            } else {
                None
            };
            let mut model = serde_json::Map::from_iter([
                ("id".to_string(), json!("gpt-5.6-terra")),
                ("model".to_string(), json!("gpt-5.6-terra")),
                ("visibility".to_string(), json!("list")),
                ("hidden".to_string(), json!(false)),
                (
                    "effective_context_window_percent".to_string(),
                    json!(95),
                ),
            ]);
            if let Some(context) = context {
                model.insert("context_window".to_string(), json!(context));
            }
            println!(
                "\n{}",
                json!({
                    "id": 2,
                    "result": {
                        "data": [Value::Object(model)]
                    }
                })
            );
            std::io::stdout()
                .flush()
                .expect("flush app-server model-list test response");
        }
        loop {
            thread::park();
        }
    }

    fn responding_app_server_test_process_command(liveness_path: &Path) -> std::process::Command {
        let mut command =
            std::process::Command::new(std::env::current_exe().expect("current test executable"));
        command
            .args([
                "--ignored",
                "--exact",
                APP_SERVER_TEST_PROCESS_HELPER,
                "--nocapture",
            ])
            .env(APP_SERVER_TEST_PROCESS_ENV, "respond")
            .env(APP_SERVER_TEST_PROCESS_LIVENESS_ENV, liveness_path);
        command
    }

    fn silent_app_server_test_process_command(liveness_path: &Path) -> std::process::Command {
        let mut command =
            std::process::Command::new(std::env::current_exe().expect("current test executable"));
        command
            .args([
                "--ignored",
                "--exact",
                APP_SERVER_TEST_PROCESS_HELPER,
                "--nocapture",
            ])
            .env(APP_SERVER_TEST_PROCESS_ENV, "silent")
            .env(APP_SERVER_TEST_PROCESS_LIVENESS_ENV, liveness_path);
        command
    }

    fn assert_app_server_test_process_stopped(liveness_path: &Path) {
        assert!(
            liveness_path.is_file(),
            "the deterministic app-server helper must reach response readiness"
        );
        let liveness_record =
            fs::read_to_string(liveness_path).expect("read app-server helper liveness record");
        let liveness_address = liveness_record
            .lines()
            .find_map(|line| line.strip_prefix("address="))
            .expect("app-server helper liveness address")
            .parse::<std::net::SocketAddr>()
            .expect("parse app-server helper liveness address");
        let released_listener = std::net::TcpListener::bind(liveness_address)
            .expect("app-server helper must release its liveness listener");
        drop(released_listener);
        #[cfg(windows)]
        {
            use std::os::windows::fs::OpenOptionsExt;
            fs::OpenOptions::new()
                .read(true)
                .write(true)
                .share_mode(0)
                .open(liveness_path)
                .expect("app-server helper must be terminated and release its liveness file");
        }
    }

    #[test]
    fn desktop_codex_exe_finds_the_app_managed_runtime() {
        let root = temp_root("desktop-codex-runtime");
        let executable = root
            .join("OpenAI")
            .join("Codex")
            .join("bin")
            .join("runtime-hash")
            .join("codex.exe");
        fs::create_dir_all(executable.parent().unwrap()).unwrap();
        fs::write(&executable, b"desktop codex").unwrap();

        assert_eq!(
            desktop_codex_exe_from_local_appdata(&root),
            Some(executable)
        );
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn app_server_model_list_does_not_wait_for_delayed_atomic_native_cache_publication() {
        let root = temp_root("app-server-delayed-cache");
        let script = root.join("fake-codex-app-server.py");
        let cache_path = root.join("codex-home").join("models_cache.json");
        fs::create_dir_all(cache_path.parent().unwrap()).unwrap();
        fs::write(
            &script,
            r#"import json
import os
from pathlib import Path
import sys
import time

for line in sys.stdin:
    message = json.loads(line)
    if message.get("id") != 2:
        continue
    print(json.dumps({"id": 2, "result": {"data": [{"id": "gpt-5.6-terra", "model": "gpt-5.6-terra", "visibility": "list", "hidden": False, "context_window": 272000, "effective_context_window_percent": 95}]}}), flush=True)
    time.sleep(2)
    cache_path = Path(os.environ["FAKE_MODELS_CACHE"])
    temporary_path = cache_path.with_suffix(".tmp")
    temporary_path.write_text(
        json.dumps({"fetched_at": "2026-07-23T00:00:00Z", "models": []}),
        encoding="utf-8",
    )
    os.replace(temporary_path, cache_path)
    time.sleep(10)
"#,
        )
        .unwrap();
        let mut command = std::process::Command::new(super::find_python());
        command
            .arg(&script)
            .env("FAKE_MODELS_CACHE", &cache_path);

        let result = super::read_codex_app_server_model_list_with_command(
            command,
            Duration::from_secs(5),
        )
        .expect("model list response");

        assert_eq!(
            result,
            json!({
                "data": [{
                    "id": "gpt-5.6-terra",
                    "model": "gpt-5.6-terra",
                    "visibility": "list",
                    "hidden": false,
                    "context_window": 272000,
                    "effective_context_window_percent": 95
                }]
            })
        );
        assert!(
            !cache_path.is_file(),
            "the app-server response must not wait for optional native cache publication"
        );
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn app_server_model_list_accepts_models_and_bare_array_response_shapes() {
        let root = temp_root("app-server-response-shapes");
        let script = root.join("fake-codex-app-server.py");
        fs::write(
            &script,
            r#"import json
import os
import sys
import time

model = {
    "id": "gpt-5.6-terra",
    "model": "gpt-5.6-terra",
    "visibility": "list",
    "hidden": False,
    "context_window": 272000,
    "effective_context_window_percent": 95,
}
if os.environ["MODEL_LIST_SHAPE"] == "models":
    result = {"models": [model]}
else:
    result = [model]

for line in sys.stdin:
    message = json.loads(line)
    if message.get("id") != 2:
        continue
    print(json.dumps({"id": 2, "result": result}), flush=True)
    time.sleep(2)
"#,
        )
        .unwrap();

        for shape in ["models", "array"] {
            let cache_path = root
                .join(format!("{shape}-codex-home"))
                .join("models_cache.json");
            fs::create_dir_all(cache_path.parent().unwrap()).unwrap();
            let mut command = std::process::Command::new(super::find_python());
            command
                .arg(&script)
                .env("MODEL_LIST_SHAPE", shape);

            let result = super::read_codex_app_server_model_list_with_cache_path(
                command,
                Duration::from_secs(2),
                &cache_path,
                Duration::from_millis(100),
            )
            .expect("model/list response shape should be accepted");
            let model = json!({
                "id": "gpt-5.6-terra",
                "model": "gpt-5.6-terra",
                "visibility": "list",
                "hidden": false,
                "context_window": 272000,
                "effective_context_window_percent": 95
            });
            let expected = if shape == "models" {
                json!({"models": [model]})
            } else {
                json!([model])
            };
            assert_eq!(result, expected);
            assert!(!cache_path.is_file());
        }

        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn readable_native_model_cache_rejects_malformed_model_entries() {
        let root = temp_root("malformed-native-model-cache");
        let cache_path = root.join("models_cache.json");
        let cases = [
            ("invalid-json", "{not-json", false),
            ("missing-models", "{}", false),
            ("empty-models", r#"{"models":[]}"#, false),
            ("null-model", r#"{"models":[null]}"#, false),
            ("string-model", r#"{"models":["gpt-5.6-terra"]}"#, false),
            ("object-model", r#"{"models":[{"slug":"gpt-5.6-terra"}]}"#, true),
        ];

        for (name, payload, expected_readable) in cases {
            fs::write(&cache_path, payload).unwrap();
            assert_eq!(
                super::readable_native_model_cache(&cache_path).is_some(),
                expected_readable,
                "unexpected native cache readability for {name}"
            );
        }

        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn app_server_model_list_accepts_valid_response_without_native_cache_publication() {
        let root = temp_root("app-server-missing-cache");
        let cache_path = root.join("codex-home").join("models_cache.json");
        let helper_liveness_path = root.join("helper-liveness.lock");
        fs::create_dir_all(cache_path.parent().unwrap()).unwrap();
        let command = responding_app_server_test_process_command(&helper_liveness_path);
        let started = Instant::now();

        // A Windows test binary can take more than 500 ms to start under a
        // loaded workspace. Keep this helper-only response budget bounded
        // well below the production 20 s native-cache grace: if the code
        // regresses into waiting for cache publication despite complete
        // context metadata, the 5 s cache grace below outlasts the 4 s
        // elapsed bound and keeps that wait observable.
        let result = super::read_codex_app_server_model_list_with_cache_path(
            command,
            Duration::from_secs(2),
            &cache_path,
            Duration::from_secs(5),
        )
        .expect("valid model/list response must not require native cache publication");

        assert_eq!(
            result,
            json!({
                "data": [{
                    "id": "gpt-5.6-terra",
                    "model": "gpt-5.6-terra",
                    "visibility": "list",
                    "hidden": false,
                    "context_window": 272000,
                    "effective_context_window_percent": 95
                }]
            })
        );
        assert!(
            started.elapsed() < Duration::from_secs(4),
            "model/list handling must remain bounded without native cache publication"
        );
        assert!(!cache_path.is_file());
        assert_app_server_test_process_stopped(&helper_liveness_path);
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn app_server_model_list_fails_closed_when_context_and_native_cache_are_missing() {
        let root = temp_root("app-server-missing-context-and-cache");
        let cache_path = root.join("codex-home").join("models_cache.json");
        let helper_liveness_path = root.join("helper-liveness.lock");
        fs::create_dir_all(cache_path.parent().unwrap()).unwrap();
        let mut command = responding_app_server_test_process_command(&helper_liveness_path);
        command.env(APP_SERVER_TEST_PROCESS_ENV, "respond-without-context");
        let started = Instant::now();

        let error = super::read_codex_app_server_model_list_with_cache_path(
            command,
            Duration::from_millis(500),
            &cache_path,
            Duration::from_millis(100),
        )
        .expect_err("context-less model/list must fail closed without native cache evidence");

        assert_eq!(
            error,
            "codex app-server model list did not publish a readable native models cache before the refresh deadline (context metadata absent)"
        );
        assert!(
            started.elapsed() < Duration::from_secs(2),
            "missing context handling must remain bounded"
        );
        assert!(!cache_path.is_file());
        assert_app_server_test_process_stopped(&helper_liveness_path);
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn app_server_model_list_waits_for_cache_when_context_metadata_is_partial() {
        let root = temp_root("app-server-context-cache-grace");
        let script = root.join("fake-codex-app-server.py");
        let cache_path = root.join("codex-home").join("models_cache.json");
        fs::create_dir_all(cache_path.parent().unwrap()).unwrap();
        fs::write(
            &cache_path,
            json!({
                "fetched_at": "2026-08-03T13:00:00Z",
                "etag": "stale",
                "client_version": "0.146.0",
                "models": [{
                    "slug": "gpt-5.6-terra",
                    "context_window": 128000,
                    "max_context_window": 128000,
                    "effective_context_window_percent": 95,
                    "comp_hash": "stale"
                }]
            })
            .to_string(),
        )
        .unwrap();
        fs::write(
            &script,
            r#"import json
import os
from pathlib import Path
import sys
import time

for line in sys.stdin:
    message = json.loads(line)
    if message.get("id") != 2:
        continue
    print(json.dumps({"id": 2, "result": {"data": [{"id": "gpt-5.6-terra", "model": "gpt-5.6-terra", "visibility": "list", "hidden": False, "context_window": 272000}]}}), flush=True)
    time.sleep(0.15)
    cache_path = Path(os.environ["FAKE_MODELS_CACHE"])
    temporary_path = cache_path.with_suffix(".tmp")
    temporary_path.write_text(
        json.dumps({
            "fetched_at": "2026-08-03T13:01:00Z",
            "etag": "fresh",
            "client_version": "0.146.0",
            "models": [{
                "slug": "gpt-5.6-terra",
                "context_window": 272000,
                "max_context_window": 272000,
                "effective_context_window_percent": 95,
                "comp_hash": "fresh"
            }]
        }),
        encoding="utf-8",
    )
    os.replace(temporary_path, cache_path)
    time.sleep(2)
"#,
        )
        .unwrap();
        let mut command = std::process::Command::new(super::find_python());
        command
            .arg(&script)
            .env("FAKE_MODELS_CACHE", &cache_path);
        let started = Instant::now();

        let result = super::read_codex_app_server_model_list_with_cache_path(
            command,
            Duration::from_secs(2),
            &cache_path,
            Duration::from_secs(1),
        )
        .expect("partial response context should use the bounded native-cache grace");

        assert_eq!(result["data"][0]["model"], "gpt-5.6-terra");
        let cache: Value =
            serde_json::from_str(&fs::read_to_string(&cache_path).unwrap()).unwrap();
        assert_eq!(cache["models"][0]["context_window"], 272000);
        assert!(started.elapsed() >= Duration::from_millis(100));
        assert!(started.elapsed() < Duration::from_secs(2));
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn app_server_model_list_rejects_unchanged_native_cache_without_context() {
        let root = temp_root("app-server-unchanged-cache");
        let cache_path = root.join("codex-home").join("models_cache.json");
        let helper_liveness_path = root.join("helper-liveness.lock");
        fs::create_dir_all(cache_path.parent().unwrap()).unwrap();
        fs::write(
            &cache_path,
            json!({
                "fetched_at": "2026-08-03T13:00:00Z",
                "etag": "unchanged",
                "client_version": "0.146.0",
                "models": [{
                    "slug": "gpt-5.6-terra",
                    "context_window": 272000,
                    "max_context_window": 272000,
                    "effective_context_window_percent": 95,
                    "comp_hash": "unchanged"
                }]
            })
            .to_string(),
        )
        .unwrap();
        let mut command = responding_app_server_test_process_command(&helper_liveness_path);
        command.env(APP_SERVER_TEST_PROCESS_ENV, "respond-without-context");

        let error = super::read_codex_app_server_model_list_with_cache_path(
            command,
            Duration::from_millis(500),
            &cache_path,
            Duration::from_millis(100),
        )
        .expect_err("unchanged native cache must not satisfy a context-less refresh");

        assert_eq!(
            error,
            "codex app-server model list did not publish a readable native models cache before the refresh deadline (context metadata absent)"
        );
        assert_app_server_test_process_stopped(&helper_liveness_path);
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn app_server_model_list_response_timeout_remains_independently_bounded() {
        let root = temp_root("app-server-response-timeout");
        let cache_path = root.join("codex-home").join("models_cache.json");
        let helper_liveness_path = root.join("helper-liveness.lock");
        fs::create_dir_all(cache_path.parent().unwrap()).unwrap();
        let command = silent_app_server_test_process_command(&helper_liveness_path);
        let started = Instant::now();

        let error = super::read_codex_app_server_model_list_with_command(
            command,
            Duration::from_secs(1),
        )
        .expect_err("silent app-server must hit the response timeout");

        assert_eq!(
            error,
            "codex app-server model list timed out after 0 seconds"
        );
        assert!(
            started.elapsed() < Duration::from_secs(2),
            "response timeout handling must remain bounded"
        );
        assert_app_server_test_process_stopped(&helper_liveness_path);
        let _ = fs::remove_dir_all(root);
    }

    struct StaticAppServerModelListRunner {
        result: Result<Value, String>,
    }

    impl StaticAppServerModelListRunner {
        fn ok(value: Value) -> Self {
            Self { result: Ok(value) }
        }

        fn err(message: &str) -> Self {
            Self {
                result: Err(message.to_string()),
            }
        }
    }

    impl AppServerModelListRunner for StaticAppServerModelListRunner {
        fn read_model_list(&self) -> Result<Value, String> {
            self.result.clone()
        }
    }

    #[test]
    fn subscription_models_use_bare_official_ids_and_keep_builtin_defaults() {
        let subscription_models = subscription_models_from_payload(&json!({
            "data": [
                {
                    "id": "openai/gpt-5.4",
                    "displayName": "gpt-5.4",
                    "hidden": false
                }
            ]
        }))
        .expect("subscription models");

        let models = subscription_models_to_metadata_models(&subscription_models);

        assert_eq!(model_ids(&models), ["gpt-5.4"]);
        assert_eq!(models[0].display_name.as_deref(), Some("5.4"));
        assert_eq!(models[0].upstream_model.as_deref(), Some("gpt-5.4"));
        assert!(models[0].pricing.is_some());
    }

    #[test]
    fn subscription_models_apply_typed_visibility_and_internal_identity_policy() {
        let subscription_models = subscription_models_from_payload(&json!({
            "data": [
                {
                    "id": "gpt-5.6-terra",
                    "model": "gpt-5.6-terra",
                    "visibility": "list",
                    "hidden": false
                },
                {
                    "id": "gpt-5.6-sol",
                    "model": "gpt-5.6-sol",
                    "visibility": "hide",
                    "hidden": false
                },
                {
                    "id": "codex-auto-review",
                    "model": "gpt-5.6-terra",
                    "slug": "gpt-5.6-terra",
                    "visibility": "list",
                    "hidden": false
                },
                {
                    "id": "gpt-5.6-luna",
                    "model": "gpt-5.6-luna",
                    "visibility": "future",
                    "hidden": false
                }
            ]
        }))
        .expect("subscription models");

        let models = subscription_models_to_metadata_models(&subscription_models);

        assert_eq!(model_ids(&models), ["gpt-5.6-terra"]);
        assert_eq!(models[0].visibility, crate::CatalogVisibility::List);
    }

    #[test]
    fn visibility_diagnostics_are_bounded_and_secret_free() {
        let diagnostics = visibility_diagnostics_from_payload(&json!({
            "data": [
                {"id": "gpt-5.6-sol", "visibility": "hide"},
                {"id": "gpt-5.6-luna", "visibility": "future"},
                {"id": "codex-auto-review", "model": "gpt-5.6-terra", "visibility": "list"},
                {"id": "gpt-5.6-terra", "visibility": "list"}
            ]
        }));

        assert_eq!(diagnostics["hidden"], 1);
        assert_eq!(diagnostics["unknown"], 1);
        assert_eq!(diagnostics["internal"], 1);
        assert!(!diagnostics.to_string().contains("gpt-5.6"));
    }

    #[test]
    fn subscription_refresh_converts_visible_codex_models_and_writes_caches() {
        let root = temp_root("subscription-refresh");
        let paths = test_paths(&root);
        let runner = StaticAppServerModelListRunner::ok(json!({
            "data": [
                {
                    "id": "gpt-subscription-live",
                    "model": "gpt-subscription-live",
                    "displayName": "GPT Subscription Live",
                    "description": "Subscription model from Codex.",
                    "hidden": false,
                    "supportedReasoningEfforts": [
                        {"reasoningEffort": "low", "description": "Fast"},
                        {"reasoningEffort": "xhigh", "description": "Deep"}
                    ],
                    "defaultReasoningEffort": "xhigh",
                    "inputModalities": ["text", "image"],
                    "additionalSpeedTiers": ["fast"],
                    "serviceTiers": [{"id": "priority", "name": "Fast"}],
                    "isDefault": true
                },
                {
                    "id": "gpt-hidden",
                    "model": "gpt-hidden",
                    "displayName": "Hidden",
                    "hidden": true
                },
                {
                    "id": "gpt-5.4",
                    "model": "gpt-5.4",
                    "displayName": "gpt-5.4",
                    "hidden": false
                },
                {
                    "id": "not-gpt",
                    "model": "not-gpt",
                    "displayName": "Not GPT",
                    "hidden": false
                }
            ]
        }));

        let models =
            refresh_official_models_with_runner(&paths, &runner).expect("subscription refresh");

        assert_eq!(model_ids(&models), ["gpt-subscription-live", "gpt-5.4"]);
        assert_eq!(
            models[0].display_name.as_deref(),
            Some("GPT Subscription Live")
        );
        assert_eq!(
            models[0].upstream_model.as_deref(),
            Some("gpt-subscription-live")
        );
        assert_eq!(
            models[0].supported_reasoning_levels.as_deref(),
            Some(&["low".to_string(), "xhigh".to_string()][..])
        );
        assert_eq!(models[0].default_reasoning_level.as_deref(), Some("xhigh"));
        assert_eq!(
            models[0].input_modalities.as_deref(),
            Some(&["text".to_string(), "image".to_string()][..])
        );
        let known_model = models
            .iter()
            .find(|model| model.id == "gpt-5.4")
            .expect("known subscription model");
        assert_eq!(known_model.display_name.as_deref(), Some("5.4"));

        let cached_metadata =
            read_models_json(&paths.metadata_cache_path()).expect("metadata cache");
        assert_eq!(
            model_ids(&cached_metadata),
            ["gpt-subscription-live", "gpt-5.4"]
        );

        let seed: Value = serde_json::from_str(
            &fs::read_to_string(paths.official_subscription_cache_path()).expect("runtime seed"),
        )
        .expect("runtime seed json");
        let cached_model = &seed["models"][0];
        assert_eq!(cached_model["slug"], "gpt-subscription-live");
        assert_eq!(cached_model["display_name"], "GPT Subscription Live");
        assert_eq!(cached_model["additional_speed_tiers"], json!(["fast"]));
        assert_eq!(cached_model["service_tiers"][0]["id"], "priority");
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn subscription_refresh_preserves_empty_visibility_snapshot_and_diagnostics() {
        let root = temp_root("subscription-empty-visibility-refresh");
        let paths = test_paths(&root);
        let runner = StaticAppServerModelListRunner::ok(json!({
            "data": [
                {"id": "gpt-hidden", "visibility": "hide"},
                {"id": "gpt-unknown", "visibility": "future"},
                {
                    "id": "codex-auto-review",
                    "model": "gpt-5.6-terra",
                    "visibility": "list"
                },
                {"id": 42, "visibility": "list"}
            ]
        }));

        let models = refresh_official_models_with_runner(&paths, &runner)
            .expect("empty visibility snapshot should publish fail-closed");

        assert!(models.is_empty());
        let payload = load_json_file(&paths.official_subscription_cache_path())
            .expect("empty official subscription cache");
        assert_eq!(payload["models"].as_array().map(Vec::len), Some(0));
        assert_eq!(payload["visibility_diagnostics"]["hidden"], 1);
        assert_eq!(payload["visibility_diagnostics"]["unknown"], 1);
        assert_eq!(payload["visibility_diagnostics"]["internal"], 1);
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn subscription_refresh_rejects_malformed_model_list_without_publishing() {
        let root = temp_root("subscription-malformed-refresh");
        let paths = test_paths(&root);
        let runner = StaticAppServerModelListRunner::ok(json!({
            "data": {"not": "an array"}
        }));

        let error = refresh_official_models_with_runner(&paths, &runner)
            .expect_err("malformed model/list response must fail closed");

        assert!(error.contains("Codex subscription model list unavailable"));
        assert!(!paths.official_subscription_cache_path().exists());
        assert!(!paths.metadata_cache_path().exists());
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn subscription_seed_preserves_app_cli_metadata_and_simple_slider_presets() {
        let subscription_models = subscription_models_from_payload(&json!({
            "data": [
                {
                    "id": "gpt-5.6-terra",
                    "model": "gpt-5.6-terra",
                    "displayName": "GPT-5.6-Terra",
                    "hidden": false,
                    "supportedReasoningEfforts": [
                        {"reasoningEffort": "low", "description": "Light"},
                        {"reasoningEffort": "medium", "description": "Medium"},
                        {"reasoningEffort": "high", "description": "High"},
                        {"reasoningEffort": "xhigh", "description": "Extra High"},
                        {"reasoningEffort": "max", "description": "Max"},
                        {"reasoningEffort": "ultra", "description": "Ultra"}
                    ],
                    "defaultReasoningEffort": "medium"
                },
                {
                    "id": "gpt-5.6-sol",
                    "model": "gpt-5.6-sol",
                    "displayName": "GPT-5.6-Sol",
                    "hidden": false,
                    "context_window": 400000,
                    "supportedReasoningEfforts": [
                        {"reasoningEffort": "low", "description": "Light"},
                        {"reasoningEffort": "medium", "description": "Medium"},
                        {"reasoningEffort": "high", "description": "High"},
                        {"reasoningEffort": "xhigh", "description": "Extra High"},
                        {"reasoningEffort": "max", "description": "Max"},
                        {"reasoningEffort": "ultra", "description": "Ultra"}
                    ],
                    "defaultReasoningEffort": "low",
                    "multi_agent_version": "v2",
                    "tool_mode": "code_mode_only",
                    "model_messages": {"upgrade": "Use Sol"},
                    "skills_instructions": "Official skills contract",
                    "web_search_tool_type": "text",
                    "use_responses_lite": true,
                    "availability": {"plan": "plus"},
                    "upgrade": "gpt-5.7-sol",
                    "upgradeInfo": {"message": "Upgrade available"},
                    "comp_hash": "3000"
                }
            ]
        }))
        .expect("subscription models");
        let seeds = subscription_models
            .iter()
            .map(super::official_subscription_seed_model)
            .collect::<Vec<_>>();
        let terra = &seeds[0];
        let sol = &seeds[1];

        assert_eq!(terra["display_name"], "5.6 Terra");
        assert_eq!(sol["display_name"], "5.6 Sol");
        assert_eq!(sol["context_window"], 400000);
        assert_eq!(sol["multi_agent_version"], "v2");
        assert_eq!(sol["tool_mode"], "code_mode_only");
        assert_eq!(sol["model_messages"]["upgrade"], "Use Sol");
        assert_eq!(sol["skills_instructions"], "Official skills contract");
        assert_eq!(sol["web_search_tool_type"], "text");
        assert_eq!(sol["use_responses_lite"], true);
        assert_eq!(sol["availability"]["plan"], "plus");
        assert_eq!(sol["upgrade"], "gpt-5.7-sol");
        assert_eq!(sol["upgradeInfo"]["message"], "Upgrade available");
        assert_eq!(sol["comp_hash"], "3000");
        let terra_efforts = terra["supported_reasoning_levels"]
            .as_array()
            .unwrap()
            .iter()
            .filter_map(|entry| entry["effort"].as_str())
            .collect::<Vec<_>>();
        let sol_efforts = sol["supported_reasoning_levels"]
            .as_array()
            .unwrap()
            .iter()
            .filter_map(|entry| entry["effort"].as_str())
            .collect::<Vec<_>>();
        assert_eq!(
            terra_efforts,
            ["low", "medium", "high", "xhigh", "max", "ultra"]
        );
        assert_eq!(
            sol_efforts,
            ["low", "medium", "high", "xhigh", "max", "ultra"]
        );
    }

    #[test]
    fn subscription_seed_backfills_pinned_official_planner_metadata_per_model() {
        let subscription_models = subscription_models_from_payload(&json!({
            "data": [
                {"id": "gpt-5.6-sol", "model": "gpt-5.6-sol", "hidden": false},
                {"id": "gpt-5.6-terra", "model": "gpt-5.6-terra", "hidden": false},
                {"id": "gpt-5.6-luna", "model": "gpt-5.6-luna", "hidden": false},
                {
                    "id": "gpt-5.5",
                    "model": "gpt-5.5",
                    "hidden": false,
                    "use_responses_lite": true,
                    "tool_mode": "code_mode_only",
                    "multi_agent_version": "v2"
                },
                {
                    "id": "gpt-5.4",
                    "model": "gpt-5.4",
                    "hidden": false,
                    "use_responses_lite": true,
                    "tool_mode": "code_mode_only",
                    "multi_agent_version": "v2"
                },
                {
                    "id": "gpt-5.4-mini",
                    "model": "gpt-5.4-mini",
                    "hidden": false,
                    "use_responses_lite": true,
                    "tool_mode": "code_mode_only",
                    "multi_agent_version": "v2"
                },
                {
                    "id": "gpt-5.3-codex-spark",
                    "model": "gpt-5.3-codex-spark",
                    "hidden": false,
                    "use_responses_lite": true
                },
                {"id": "gpt-sparse", "model": "gpt-sparse", "hidden": false},
                {
                    "id": "gpt-explicit-lite",
                    "model": "gpt-explicit-lite",
                    "hidden": false,
                    "use_responses_lite": true
                }
            ]
        }))
        .expect("subscription models");
        let seeds = subscription_models
            .iter()
            .map(super::official_subscription_seed_model)
            .collect::<Vec<_>>();

        for (index, version) in [(0, "v2"), (1, "v2"), (2, "v1")] {
            assert_eq!(seeds[index]["tool_mode"], "code_mode_only");
            assert_eq!(seeds[index]["multi_agent_version"], version);
            assert_eq!(seeds[index]["prefer_websockets"], true);
            assert_eq!(seeds[index]["use_responses_lite"], true);
        }

        for legacy_model in &seeds[3..6] {
            assert!(legacy_model.get("tool_mode").is_some());
            assert!(legacy_model["tool_mode"].is_null());
            assert!(legacy_model.get("multi_agent_version").is_some());
            assert!(legacy_model["multi_agent_version"].is_null());
            assert_eq!(legacy_model["prefer_websockets"], true);
            assert_eq!(legacy_model["use_responses_lite"], false);
        }

        for sparse_model in [&seeds[6], &seeds[7]] {
            assert_eq!(sparse_model["use_responses_lite"], false);
            assert!(sparse_model.get("prefer_websockets").is_none());
            assert!(sparse_model.get("tool_mode").is_none());
            assert!(sparse_model.get("multi_agent_version").is_none());
        }
        assert_eq!(seeds[8]["use_responses_lite"], true);
    }

    #[test]
    fn catalog_multi_agent_overrides_require_official_pinned_identity() {
        let root = temp_root("catalog-multi-agent-override-identity");
        let paths = test_paths(&root);
        let override_path = paths.catalog_overrides_path();
        fs::create_dir_all(override_path.parent().unwrap()).unwrap();
        fs::write(
            &override_path,
            serde_json::to_vec(&json!({
                "schema_version": 1,
                "overrides": [
                    {
                        "provider": "openai",
                        "upstream_name": "official",
                        "upstream_model": "gpt-5.6-luna",
                        "fields": {"multi_agent_version": "v2"}
                    },
                    {
                        "provider": "openai",
                        "upstream_name": "official",
                        "upstream_model": "gpt-5.5",
                        "fields": {"multi_agent_version": "v2"}
                    },
                    {
                        "provider": "volc",
                        "upstream_name": "official",
                        "upstream_model": "gpt-5.6-luna",
                        "fields": {"multi_agent_version": "v2"}
                    }
                ]
            }))
            .unwrap(),
        )
        .unwrap();

        let mut models = vec![
            Model {
                id: "gpt-5.6-luna".to_string(),
                source_kind: Some("official".to_string()),
                upstream_model: Some("gpt-5.6-luna".to_string()),
                multi_agent_version: Some("v1".to_string()),
                ..Model::default()
            },
            Model {
                id: "gpt-5.6-luna".to_string(),
                source_kind: Some("external".to_string()),
                ..Model::default()
            },
            Model {
                id: "gpt-5.5".to_string(),
                source_kind: Some("official".to_string()),
                ..Model::default()
            },
            Model {
                id: "gpt-5.6-luna".to_string(),
                source_kind: Some("official".to_string()),
                upstream_model: Some("gpt-5.6-terra".to_string()),
                multi_agent_version: Some("v1".to_string()),
                ..Model::default()
            },
            Model {
                id: "gpt-5.6-luna".to_string(),
                source_kind: Some("official".to_string()),
                multi_agent_version: Some("v1".to_string()),
                ..Model::default()
            },
        ];

        super::apply_catalog_multi_agent_overrides(&paths, &mut models);

        assert_eq!(models[0].multi_agent_version.as_deref(), Some("v2"));
        assert_eq!(models[1].multi_agent_version, None);
        assert_eq!(models[2].multi_agent_version, None);
        assert_eq!(models[3].multi_agent_version.as_deref(), Some("v1"));
        assert_eq!(models[4].multi_agent_version.as_deref(), Some("v1"));
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn list_official_multi_agent_overrides_requires_exact_upstream_identity() {
        let _guard = ENV_LOCK.lock().unwrap();
        let previous = std::env::var_os("CODEX_HOME");
        let root = temp_root("list-catalog-multi-agent-upstream-identity");
        let codex_home = root.join("codex-home");
        let paths = test_paths(&root);
        let catalog_path = paths.generated_catalog_path();
        let override_path = paths.catalog_overrides_path();
        fs::create_dir_all(catalog_path.parent().unwrap()).unwrap();
        fs::write(
            &override_path,
            serde_json::to_vec(&json!({
                "schema_version": 1,
                "overrides": [{
                    "provider": "openai",
                    "upstream_name": "official",
                    "upstream_model": "gpt-5.6-luna",
                    "fields": {"multi_agent_version": "v2"}
                }]
            }))
            .unwrap(),
        )
        .unwrap();
        std::env::set_var("CODEX_HOME", &codex_home);

        fs::write(
            &catalog_path,
            serde_json::to_vec(&json!({
                "models": [{
                    "slug": "gpt-5.6-luna",
                    "codex_proxy_metadata": {
                        "provider": "openai",
                        "upstream_name": "official"
                    }
                }]
            }))
            .unwrap(),
        )
        .unwrap();
        let missing = list_official_multi_agent_overrides();

        fs::write(
            &catalog_path,
            serde_json::to_vec(&json!({
                "models": [{
                    "slug": "gpt-5.6-luna",
                    "codex_proxy_metadata": {
                        "provider": "openai",
                        "upstream_name": "official",
                        "upstream_model": "gpt-5.6-terra"
                    }
                }]
            }))
            .unwrap(),
        )
        .unwrap();
        let wrong = list_official_multi_agent_overrides();

        fs::write(
            &catalog_path,
            serde_json::to_vec(&json!({
                "models": [{
                    "slug": "gpt-5.6-luna",
                    "codex_proxy_metadata": {
                        "provider": "openai",
                        "upstream_name": "official",
                        "upstream_model": "gpt-5.6-luna"
                    }
                }]
            }))
            .unwrap(),
        )
        .unwrap();
        let valid = list_official_multi_agent_overrides();

        restore_env("CODEX_HOME", previous);
        assert!(missing.expect("missing upstream model should be ignored").is_empty());
        assert!(wrong.expect("wrong upstream model should be ignored").is_empty());
        assert_eq!(
            valid
                .expect("exact upstream model should be listed")
                .get("gpt-5.6-luna")
                .map(String::as_str),
            Some("v2")
        );
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn set_catalog_multi_agent_version_changes_only_exact_official_identity() {
        let root = temp_root("catalog-multi-agent-effective-write");
        let paths = test_paths(&root);
        let catalog_path = paths.generated_catalog_path();
        fs::create_dir_all(catalog_path.parent().unwrap()).unwrap();
        fs::write(
            &catalog_path,
            serde_json::to_vec(&json!({
                "schema_version": 1,
                "models": [
                    {
                        "slug": "gpt-5.6-luna",
                        "multi_agent_version": "v1",
                        "codex_proxy_metadata": {
                            "provider": "openai",
                            "upstream_name": "official",
                            "upstream_model": "gpt-5.6-luna"
                        }
                    },
                    {
                        "slug": "volc/gpt-5.6-luna",
                        "multi_agent_version": "v1",
                        "codex_proxy_metadata": {
                            "provider": "volc",
                            "upstream_name": "volcengine",
                            "upstream_model": "gpt-5.6-luna"
                        }
                    }
                ]
            }))
            .unwrap(),
        )
        .unwrap();

        super::set_catalog_multi_agent_version(&paths, "gpt-5.6-luna", "v2")
            .expect("exact Official row should be writable");

        let payload: Value = serde_json::from_str(
            &fs::read_to_string(&catalog_path).expect("published catalog"),
        )
        .unwrap();
        assert_eq!(payload["models"][0]["multi_agent_version"], "v2");
        assert_eq!(payload["models"][1]["multi_agent_version"], "v1");
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn catalog_override_update_preserves_other_fields_and_exact_identity() {
        let mut payload = json!({
            "schema_version": 1,
            "overrides": [
                {
                    "provider": "openai",
                    "upstream_name": "official",
                    "upstream_model": "gpt-5.6-luna",
                    "fields": {
                        "multi_agent_version": "v1",
                        "tool_mode": "code_mode_only"
                    }
                },
                {
                    "provider": "ollama",
                    "upstream_name": "third-party",
                    "upstream_model": "gpt-5.6-luna",
                    "fields": {"multi_agent_version": "v1"}
                }
            ]
        });

        super::update_catalog_override_payload(&mut payload, "gpt-5.6-luna", Some("v2"))
            .expect("exact Official override should update");
        assert_eq!(payload["overrides"][0]["fields"]["multi_agent_version"], "v2");
        assert_eq!(payload["overrides"][0]["fields"]["tool_mode"], "code_mode_only");
        assert_eq!(payload["overrides"][1]["fields"]["multi_agent_version"], "v1");

        super::update_catalog_override_payload(&mut payload, "gpt-5.6-luna", None)
            .expect("clearing should remove only the model-level field");
        assert!(payload["overrides"][0]["fields"]
            .get("multi_agent_version")
            .is_none());
        assert_eq!(payload["overrides"][0]["fields"]["tool_mode"], "code_mode_only");
        assert_eq!(payload["overrides"][1]["fields"]["multi_agent_version"], "v1");
    }

    #[test]
    fn catalog_override_update_rejects_invalid_payload_before_mutation() {
        let mut payload = json!({"schema_version": 1, "overrides": {}});
        let before = payload.clone();
        let error = super::update_catalog_override_payload(&mut payload, "gpt-5.6-luna", Some("v2"))
            .expect_err("non-array overrides must fail closed");
        assert_eq!(error, "catalog overrides are invalid");
        assert_eq!(payload, before);
    }

    #[test]
    fn codex_0_144_2_model_list_without_numeric_context_fields_preserves_that_absence() {
        let fixture: Value = serde_json::from_str(include_str!(
            "../../tests/fixtures/codex_0_144_2_model_list_without_context_fields.json"
        ))
        .expect("current Codex model/list fixture");
        let expected_slugs = [
            "gpt-5.6-sol",
            "gpt-5.6-terra",
            "gpt-5.6-luna",
            "gpt-5.5",
            "gpt-5.4",
            "gpt-5.4-mini",
            "gpt-5.3-codex-spark",
        ];
        let numeric_context_fields = [
            "context_window",
            "max_context_window",
            "contextWindow",
            "maxContextWindow",
            "effective_context_window_percent",
            "effectiveContextWindowPercent",
            "auto_compact_token_limit",
            "autoCompactTokenLimit",
            "model_auto_compact_token_limit",
            "modelAutoCompactTokenLimit",
        ];

        let raw_models = fixture["data"].as_array().expect("fixture model list");
        assert_eq!(raw_models.len(), expected_slugs.len());
        for raw_model in raw_models {
            for &field in &numeric_context_fields {
                assert!(
                    raw_model.get(field).is_none(),
                    "fixture must omit {field} from the Direct Official response"
                );
            }
        }

        let subscription_models =
            subscription_models_from_payload(&fixture).expect("subscription models");
        assert_eq!(
            subscription_models
                .iter()
                .map(|model| model.slug.as_str())
                .collect::<Vec<_>>(),
            expected_slugs
        );

        for seed in subscription_models
            .iter()
            .map(super::official_subscription_seed_model)
        {
            for &field in &numeric_context_fields {
                assert!(
                    seed.get(field).is_none(),
                    "seed must not invent a numeric {field}"
                );
            }
        }
    }

    #[test]
    fn subscription_seed_does_not_overwrite_or_default_raw_app_metadata() {
        let subscription_models = subscription_models_from_payload(&json!({
            "data": [
                {
                    "id": "gpt-5.6-sol",
                    "model": "gpt-5.6-sol",
                    "displayName": "GPT-5.6-Sol",
                    "hidden": false,
                    "context_window": 400000,
                    "max_context_window": 999999
                }
            ]
        }))
        .expect("subscription models");

        let seed = super::official_subscription_seed_model(&subscription_models[0]);
        let seed = seed.as_object().expect("seed object");

        assert_eq!(seed.get("context_window"), Some(&json!(400000)));
        assert_eq!(seed.get("max_context_window"), Some(&json!(999999)));
        assert!(!seed.contains_key("input_modalities"));
        assert!(!seed.contains_key("additional_speed_tiers"));
        assert!(!seed.contains_key("service_tiers"));
        assert!(!seed.contains_key("is_default"));
    }

    #[test]
    fn subscription_seed_always_canonicalizes_legacy_only_slug() {
        let subscription_models = subscription_models_from_payload(&json!({
            "data": [
                {
                    "slug": "openai/gpt-5.6-sol",
                    "displayName": "GPT-5.6-Sol",
                    "hidden": false
                }
            ]
        }))
        .expect("legacy-only subscription model");

        let seed = super::official_subscription_seed_model(&subscription_models[0]);

        assert_eq!(seed["slug"], "gpt-5.6-sol");
    }

    #[test]
    fn subscription_duplicate_app_records_prefer_bare_metadata_and_or_enabled() {
        let legacy = json!({
            "id": "openai/gpt-5.6-sol",
            "model": "openai/gpt-5.6-sol",
            "displayName": "Legacy Sol",
            "hidden": false,
            "context_window": 1,
            "enabled": true
        });
        let bare = json!({
            "id": "gpt-5.6-sol",
            "model": "gpt-5.6-sol",
            "displayName": "GPT-5.6-Sol",
            "hidden": false,
            "context_window": 400000,
            "enabled": false
        });
        let other = json!({
            "id": "gpt-5.5",
            "model": "gpt-5.5",
            "displayName": "GPT-5.5",
            "hidden": false
        });

        for items in [
            vec![legacy.clone(), other.clone(), bare.clone()],
            vec![bare.clone(), other.clone(), legacy.clone()],
        ] {
            let subscription_models = subscription_models_from_payload(&json!({ "data": items }))
                .expect("subscription models");
            assert_eq!(
                subscription_models
                    .iter()
                    .map(|model| model.slug.as_str())
                    .collect::<Vec<_>>(),
                ["gpt-5.6-sol", "gpt-5.5"]
            );
            let seed = super::official_subscription_seed_model(&subscription_models[0]);
            assert_eq!(seed["display_name"], "5.6 Sol");
            assert_eq!(seed["context_window"], 400000);
            assert_eq!(seed["enabled"], true);
            let metadata = subscription_models_to_metadata_models(&subscription_models);
            assert!(metadata[0].enabled);
        }

        let all_disabled = subscription_models_from_payload(&json!({
            "data": [
                {
                    "id": "openai/gpt-5.6-sol",
                    "model": "openai/gpt-5.6-sol",
                    "hidden": false,
                    "enabled": false
                },
                {
                    "id": "gpt-5.6-sol",
                    "model": "gpt-5.6-sol",
                    "hidden": false,
                    "enabled": false
                }
            ]
        }))
        .expect("all-disabled subscription models");
        let seed = super::official_subscription_seed_model(&all_disabled[0]);
        assert_eq!(seed["enabled"], false);
        let metadata = subscription_models_to_metadata_models(&all_disabled);
        assert!(!metadata[0].enabled);
    }

    #[test]
    fn subscription_refresh_uses_cached_subscription_models_when_app_server_fails() {
        let root = temp_root("subscription-cache-fallback");
        let paths = test_paths(&root);
        fs::create_dir_all(paths.official_subscription_cache_path().parent().unwrap()).unwrap();
        fs::write(
            paths.official_subscription_cache_path(),
            json!({
                "models": [
                    {
                        "slug": "gpt-cached-subscription",
                        "display_name": "GPT Cached Subscription",
                        "hidden": false,
                        "input_modalities": ["text"],
                        "supported_reasoning_levels": [
                            {"effort": "medium", "description": "Balanced"}
                        ],
                        "default_reasoning_level": "medium"
                    }
                ]
            })
            .to_string(),
        )
        .unwrap();
        let runner = StaticAppServerModelListRunner::err("codex app-server unavailable");

        let models = refresh_official_models_with_runner(&paths, &runner)
            .expect("cached subscription refresh");

        assert_eq!(model_ids(&models), ["gpt-cached-subscription"]);
        assert_eq!(
            models[0].display_name.as_deref(),
            Some("GPT Cached Subscription")
        );
        assert_eq!(
            models[0].upstream_model.as_deref(),
            Some("gpt-cached-subscription")
        );
        assert_eq!(models[0].default_reasoning_level.as_deref(), Some("medium"));
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn official_discovery_uses_expected_url_headers_and_timeout() {
        let body = r#"{"data":[{"id":"gpt-live","context_window":128000}]}"#;
        let server = MockServer::json(body, Duration::ZERO);
        let endpoint = format!("{}/v1/models", server.base_url());

        let models = refresh_official_models_from_endpoint(
            &endpoint,
            " test-secret ",
            Duration::from_secs(2),
        )
        .expect("official discovery");

        assert_eq!(model_ids(&models), ["gpt-live"]);
        let request = server.request();
        assert!(request.starts_with("GET /v1/models "));
        let lowered = request.to_ascii_lowercase();
        assert!(lowered.contains("authorization: bearer test-secret"));
        assert!(lowered.contains("accept: application/json"));
        server.join();

        let slow_server = MockServer::json(body, Duration::from_millis(250));
        let slow_endpoint = format!("{}/v1/models", slow_server.base_url());
        let error = refresh_official_models_from_endpoint(
            &slow_endpoint,
            "test-secret",
            Duration::from_millis(30),
        )
        .expect_err("slow response should time out");

        assert!(error.contains("official OpenAI models"));
        assert!(!error.contains("test-secret"));
        slow_server.join();
    }

    #[test]
    fn official_discovery_filters_dedupes_sorts_and_parses_limits() {
        let body = r#"
        {
          "data": [
            {"id":" gpt-4.1-mini ","context_window":128000,"max_output_tokens":32768},
            {"id":"gpt-4.1","context_length":"1047576","output_tokens":"32768"},
            {"model":"gpt-4o","limit":{"context":128000,"output":16384}},
            {"id":"gpt-4.1","context_window":1,"max_output_tokens":1},
            {"id":"chatgpt-4o-latest"},
            {"id":"o3"},
            {"id":"  "},
            {"id":123}
          ]
        }
        "#;
        let server = MockServer::json(body, Duration::ZERO);
        let endpoint = format!("{}/v1/models", server.base_url());

        let models =
            refresh_official_models_from_endpoint(&endpoint, "test-secret", Duration::from_secs(2))
                .expect("official discovery");

        assert_eq!(
            compact_models(&models),
            vec![
                ("gpt-4.1", Some(1_047_576), Some(32_768)),
                ("gpt-4.1-mini", Some(128_000), Some(32_768)),
                ("gpt-4o", Some(128_000), Some(16_384)),
            ]
        );
        server.join();
    }

    #[test]
    fn provider_discovery_handles_payload_shapes_dedupes_and_preserves_order() {
        let cases = [
            (
                r#"{"data":[{"id":"alpha","context_window":128000,"max_output_tokens":8192},{"model":"beta","max_context_window":64000,"output_tokens":4096},{"name":"nested","limit":{"context":32000,"output":2048}},"string-model",{"slug":"alpha","context_length":1},{"id":"  "}]}"#,
                vec![
                    ("alpha", Some(128_000), Some(8_192)),
                    ("beta", Some(64_000), Some(4_096)),
                    ("nested", Some(32_000), Some(2_048)),
                    ("string-model", None, None),
                ],
            ),
            (
                r#"{"models":[{"slug":"from-models","context_length":1024}]}"#,
                vec![("from-models", Some(1_024), None)],
            ),
            (
                r#"[{"id":"from-list","max_output_tokens":"256"}]"#,
                vec![("from-list", None, Some(256))],
            ),
        ];

        for (body, expected) in cases {
            let server = MockServer::json(body, Duration::ZERO);

            let models = discover_provider_models_with_timeout(
                &server.base_url(),
                " provider-secret ",
                Duration::from_secs(2),
            )
            .expect("provider discovery");

            assert_eq!(compact_models(&models), expected);
            let request = server.request();
            assert!(request.starts_with("GET /v1/models "));
            assert!(request
                .to_ascii_lowercase()
                .contains("authorization: bearer provider-secret"));
            server.join();
        }
    }

    #[test]
    fn model_endpoint_test_posts_only_the_selected_endpoint() {
        let server = MockServer::json(r#"{"id":"chatcmpl-test"}"#, Duration::ZERO);

        let result = test_model_endpoint_with_timeout(
            &format!("{}/v1", server.base_url()),
            " provider-secret ",
            "model-a",
            &UpstreamFormat::ChatCompletions,
            Duration::from_secs(2),
        )
        .expect("model endpoint test");

        assert_eq!(result["ok"], true);
        assert_eq!(result["upstream_format"], "chat_completions");
        assert_eq!(result["status"], 200);
        let request = server.request();
        assert!(request.starts_with("POST /v1/chat/completions "));
        assert!(request
            .to_ascii_lowercase()
            .contains("authorization: bearer provider-secret"));
        assert!(request.contains(r#""model":"model-a""#));
        server.join();
    }

    #[test]
    fn model_endpoint_test_injects_current_loopback_gateway_key_when_key_is_blank() {
        let _guard = ENV_LOCK.lock().unwrap();
        let original_codex_home = std::env::var_os("CODEX_HOME");
        let root = temp_root("local-gateway-model-test-key");
        let codex_home = root.join("codex-home");
        let settings_path = codex_home.join("proxy").join("settings.json");
        let server = MockServer::json(r#"{"id":"resp-test"}"#, Duration::ZERO);
        let base_url = format!("{}/v1", server.base_url());
        let port = reqwest::Url::parse(&base_url).unwrap().port().unwrap();
        fs::create_dir_all(settings_path.parent().unwrap()).unwrap();
        fs::write(
            settings_path,
            serde_json::to_vec(&json!({
                "proxy_port": port,
                "gateway_client_key": "local-test-key"
            }))
            .unwrap(),
        )
        .unwrap();
        std::env::set_var("CODEX_HOME", &codex_home);

        let result = test_model_endpoint_with_timeout(
            &base_url,
            "  ",
            "openai/gpt-5.5",
            &UpstreamFormat::Responses,
            Duration::from_secs(2),
        );
        restore_env("CODEX_HOME", original_codex_home);

        assert_eq!(result.expect("model endpoint test")["ok"], true);
        let request = server.request();
        assert!(request.starts_with("POST /v1/responses "));
        assert!(request
            .to_ascii_lowercase()
            .contains("authorization: bearer local-test-key"));
        server.join();
    }

    #[test]
    fn local_gateway_key_resolution_preserves_explicit_key() {
        let settings = Settings {
            proxy_port: 4555,
            gateway_client_key: "local-test-key".to_string(),
            ..Settings::default()
        };

        let resolved = resolve_gateway_api_key_for_settings(
            "http://127.0.0.1:4555/v1",
            " explicit-key ",
            Some(&settings),
        )
        .unwrap();

        assert_eq!(resolved.as_deref(), Some("explicit-key"));
    }

    #[test]
    fn local_gateway_key_resolution_rejects_nonmatching_or_remote_endpoints() {
        let settings = Settings {
            proxy_port: 4555,
            gateway_client_key: "local-test-key".to_string(),
            ..Settings::default()
        };

        for base_url in [
            "http://127.0.0.1:4556/v1",
            "http://example.com:4555/v1",
            "https://127.0.0.1:4555/v1",
            "http://127.0.0.1:4555/custom",
        ] {
            assert_eq!(
                resolve_gateway_api_key_for_settings(base_url, "", Some(&settings)).unwrap(),
                None,
                "unexpected local Gateway key for {base_url}"
            );
        }
    }

    #[test]
    fn metadata_overrides_win_over_registry_values() {
        let base = vec![Model {
            id: "minimax/minimax-m3".to_string(),
            context_window: Some(1_000_000),
            tool_surface_strategy: Some(ToolSurfaceStrategy::DeferredCore),
            metadata_provenance: Some(MetadataProvenance {
                source: "official".to_string(),
                source_url: Some("https://platform.minimax.io/docs".to_string()),
                fetched_at: None,
                confidence: "high".to_string(),
            }),
            ..Model::default()
        }];
        let overrides = vec![Model {
            id: "minimax/minimax-m3".to_string(),
            context_window: Some(245_000),
            display_name: Some("MiniMax M3 Custom".to_string()),
            ..Model::default()
        }];

        let merged = merge_metadata_with_overrides(base, overrides, &HashSet::new());

        assert_eq!(merged[0].context_window, Some(245_000));
        assert_eq!(merged[0].display_name.as_deref(), Some("MiniMax M3 Custom"));
        assert_eq!(
            merged[0].tool_surface_strategy,
            Some(ToolSurfaceStrategy::DeferredCore)
        );
        assert_eq!(
            merged[0].metadata_provenance.as_ref().unwrap().source,
            "user_override"
        );
    }

    #[test]
    fn metadata_merge_normalizes_only_known_official_aliases() {
        let known_official_models = HashSet::from(["gpt-5.5".to_string()]);
        let base = vec![Model {
            id: "gpt-5.5".to_string(),
            display_name: Some("5.5".to_string()),
            ..Model::default()
        }];
        let overrides = vec![
            Model {
                id: "openai/gpt-5.5".to_string(),
                display_name: Some("Known override".to_string()),
                ..Model::default()
            },
            Model {
                id: "openai/gpt-9.9-unknown".to_string(),
                display_name: Some("Unknown alias".to_string()),
                ..Model::default()
            },
            Model {
                id: "acme/gpt-5.6-sol".to_string(),
                display_name: Some("Third-party GPT".to_string()),
                ..Model::default()
            },
        ];

        let merged =
            merge_metadata_with_overrides(base, overrides, &known_official_models);
        let ids = merged
            .iter()
            .map(|model| model.id.as_str())
            .collect::<Vec<_>>();

        assert_eq!(ids, ["acme/gpt-5.6-sol", "gpt-5.5"]);
        assert_eq!(
            merged
                .iter()
                .find(|model| model.id == "gpt-5.5")
                .and_then(|model| model.display_name.as_deref()),
            Some("Known override")
        );
    }

    #[test]
    fn provider_discovery_accepts_blank_api_key_without_authorization_header() {
        let server = MockServer::json(r#"{"models":["public-model"]}"#, Duration::ZERO);

        let models =
            discover_provider_models_with_timeout(&server.base_url(), "  ", Duration::from_secs(2))
                .expect("provider discovery");

        assert_eq!(model_ids(&models), ["public-model"]);
        let request = server.request();
        assert!(request.starts_with("GET /v1/models "));
        assert!(!request.to_ascii_lowercase().contains("authorization:"));
        server.join();
    }

    #[test]
    fn provider_discovery_does_not_duplicate_v1_suffix() {
        let server = MockServer::json(r#"{"models":["v1-model"]}"#, Duration::ZERO);
        let base_url = format!("{}/v1/", server.base_url());

        let models =
            discover_provider_models_with_timeout(&base_url, "test-secret", Duration::from_secs(2))
                .expect("provider discovery");

        assert_eq!(model_ids(&models), ["v1-model"]);
        let request = server.request();
        assert!(request.starts_with("GET /v1/models "));
        server.join();
    }

    #[test]
    fn provider_endpoints_do_not_duplicate_version_suffixes() {
        assert_eq!(
            provider_models_endpoint("https://example.test/v2/").unwrap(),
            "https://example.test/v2/models"
        );
        assert_eq!(
            provider_api_endpoint("https://example.test/v2", "/responses").unwrap(),
            "https://example.test/v2/responses"
        );
        assert_eq!(
            provider_models_endpoint("https://example.test").unwrap(),
            "https://example.test/v1/models"
        );
        assert_eq!(
            provider_api_endpoint("https://example.test/v1", "/responses").unwrap(),
            "https://example.test/v1/responses"
        );
        assert_eq!(
            provider_api_endpoint("https://example.test/v2", "/chat/completions").unwrap(),
            "https://example.test/v2/chat/completions"
        );
    }

    #[test]
    fn provider_endpoints_accept_complete_endpoint_urls() {
        assert_eq!(
            provider_api_endpoint("https://example.test/v1/responses", "/responses").unwrap(),
            "https://example.test/v1/responses"
        );
        assert_eq!(
            provider_api_endpoint("https://example.test/v1/response", "/responses").unwrap(),
            "https://example.test/v1/response"
        );
        assert_eq!(
            provider_models_endpoint("https://example.test/v1/response").unwrap(),
            "https://example.test/v1/models"
        );
        assert_eq!(
            provider_models_endpoint("https://example.test/v1/responses").unwrap(),
            "https://example.test/v1/models"
        );
        assert_eq!(
            provider_api_endpoint(
                "https://example.test/v2/chat/completions",
                "/chat/completions",
            )
            .unwrap(),
            "https://example.test/v2/chat/completions"
        );
        assert_eq!(
            provider_api_endpoint("https://example.test/v2/chat/completions", "/responses")
                .unwrap(),
            "https://example.test/v2/responses"
        );
    }

    #[test]
    fn provider_endpoints_append_standard_suffixes_to_bare_bases() {
        assert_eq!(
            provider_api_endpoint("https://example.test", "/responses").unwrap(),
            "https://example.test/v1/responses"
        );
        assert_eq!(
            provider_api_endpoint("https://example.test/api/coding/v3", "/chat/completions",)
                .unwrap(),
            "https://example.test/api/coding/v3/chat/completions"
        );
    }

    #[test]
    fn provider_discovery_resolves_env_api_key_placeholders() {
        let _guard = ENV_LOCK.lock().unwrap();
        let previous = std::env::var_os("MINIMAX_API_KEY");
        std::env::set_var("MINIMAX_API_KEY", "resolved-minimax-secret");
        let server = MockServer::json(r#"{"models":["minimax-m3"]}"#, Duration::ZERO);

        let models = discover_provider_models_with_timeout(
            &server.base_url(),
            "{env:MINIMAX_API_KEY}",
            Duration::from_secs(2),
        )
        .expect("provider discovery");

        assert_eq!(model_ids(&models), ["minimax-m3"]);
        let request = server.request();
        assert!(request
            .to_ascii_lowercase()
            .contains("authorization: bearer resolved-minimax-secret"));
        assert!(!request.contains("{env:MINIMAX_API_KEY}"));
        restore_env("MINIMAX_API_KEY", previous);
        server.join();
    }

    #[test]
    fn provider_discovery_reports_missing_env_api_key_placeholders() {
        let _guard = ENV_LOCK.lock().unwrap();
        let previous = std::env::var_os("MINIMAX_API_KEY");
        std::env::remove_var("MINIMAX_API_KEY");

        let error = discover_provider_models_with_timeout(
            "http://127.0.0.1:9/v1",
            "{env:MINIMAX_API_KEY}",
            Duration::from_millis(50),
        )
        .expect_err("missing env placeholder should fail before HTTP");

        restore_env("MINIMAX_API_KEY", previous);
        assert!(error.contains("MINIMAX_API_KEY"));
        assert!(error.contains("not set"));
    }

    #[test]
    fn ollama_show_endpoint_is_derived_only_for_ollama_hosts() {
        assert_eq!(
            ollama_show_endpoint("https://ollama.com/v1").as_deref(),
            Some("https://ollama.com/api/show")
        );
        assert_eq!(
            ollama_show_endpoint("http://127.0.0.1:11434/v1/").as_deref(),
            Some("http://127.0.0.1:11434/api/show")
        );
        assert_eq!(ollama_show_endpoint("https://example.test/v1"), None);
    }

    #[test]
    fn ollama_show_metadata_enriches_context_vision_and_thinking() {
        let server = MockServer::json(
            r#"{"capabilities":["completion","vision","thinking"],"model_info":{"gptoss.context_length":131072}}"#,
            Duration::ZERO,
        );
        let client = Client::builder()
            .timeout(Duration::from_secs(2))
            .build()
            .unwrap();
        let mut models = vec![Model {
            id: "gpt-oss:120b".to_string(),
            ..Model::default()
        }];

        enrich_models_with_ollama_show(
            &client,
            &format!("{}/api/show", server.base_url()),
            "ollama-secret",
            &mut models,
        );

        assert_eq!(models[0].context_window, Some(131_072));
        assert_eq!(
            models[0].input_modalities.as_deref(),
            Some(["text".to_string(), "image".to_string()].as_slice())
        );
        assert_eq!(
            models[0].supported_reasoning_levels.as_deref(),
            Some(["low".to_string(), "medium".to_string(), "high".to_string()].as_slice())
        );
        assert_eq!(models[0].default_reasoning_level.as_deref(), Some("medium"));
        let request = server.request();
        assert!(request.starts_with("POST /api/show "));
        assert!(request
            .to_ascii_lowercase()
            .contains("authorization: bearer ollama-secret"));
        assert!(request.contains(r#""model":"gpt-oss:120b""#));
        server.join();
    }

    #[test]
    fn subscription_refresh_failure_does_not_ask_for_openai_api_key() {
        let root = temp_root("subscription-no-cache");
        let paths = test_paths(&root);
        let runner = StaticAppServerModelListRunner::err("codex auth unavailable");

        let error = refresh_official_models_with_runner(&paths, &runner)
            .expect_err("missing subscription and cache should fail");

        assert!(error.contains("Codex subscription model list unavailable"));
        assert!(!error.contains("OPENAI_API_KEY"));
        assert!(!error.contains("sk-"));
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn catalog_model_identity_ignores_spoofed_source_kind() {
        let spoofed = json!({
            "slug": "gpt-5.6-luna",
            "source_kind": "official",
            "upstream_model": "gpt-5.6-luna",
            "codex_proxy_metadata": {
                "provider": "ollama_cloud",
                "upstream_name": "custom",
                "upstream_model": "gpt-5.6-luna"
            }
        });
        let model = super::catalog_model_from_item(&spoofed).expect("catalog row");
        assert_eq!(model.source_kind.as_deref(), Some("external"));

        let official = json!({
            "slug": "gpt-5.6-luna",
            "source_kind": "external",
            "upstream_model": "gpt-5.6-luna",
            "codex_proxy_metadata": {
                "provider": "openai",
                "upstream_name": "official",
                "upstream_model": "gpt-5.6-luna"
            }
        });
        let model = super::catalog_model_from_item(&official).expect("catalog row");
        assert_eq!(model.source_kind.as_deref(), Some("official"));
    }

    #[test]
    fn catalog_model_identity_requires_provider_metadata() {
        let same_slug_without_identity = json!({
            "slug": "gpt-5.6-luna",
            "source_kind": "official",
            "upstream_model": "gpt-5.6-luna"
        });
        let model = super::catalog_model_from_item(&same_slug_without_identity)
            .expect("catalog row");
        assert_eq!(model.source_kind, None);
    }

    #[test]
    fn generate_catalog_runs_sync_and_reads_generated_catalog_from_codex_home() {
        let root = temp_root("generate-catalog");
        let paths = test_paths(&root);
        fs::create_dir_all(paths.catalog_sync_script().parent().unwrap()).unwrap();
        fs::write(paths.catalog_sync_script(), "# fake catalog sync").unwrap();
        let catalog_path = paths.generated_catalog_path();
        let runner = WritingCatalogRunner::new(
            catalog_path.clone(),
            r#"
            {
              "models": [
                {
                  "slug": "openai/gpt-5.5",
                  "display_name": "OpenAI GPT-5.5",
                  "context_window": 128000,
                  "max_output_tokens": "32768",
                  "priority": 3,
                  "codex_proxy_metadata": {"upstream_model": "gpt-5.5"}
                },
                {
                  "slug": "glm-5.2",
                  "display_name": "GLM-5.2",
                  "max_context_window": 1000000,
                  "limit": {"output": 131072}
                },
                {"slug": "  "}
              ]
            }
            "#,
            CatalogCommandOutcome {
                code: Some(0),
                stdout: "visible_models=2\n".to_string(),
                stderr: String::new(),
            },
        );

        let models = generate_catalog_with_runner(&paths, Path::new("python-test"), &runner)
            .expect("catalog");

        assert!(catalog_path.exists());
        assert_eq!(models.len(), 2);
        assert_model(
            &models[0],
            "openai/gpt-5.5",
            Some("OpenAI GPT-5.5"),
            Some("gpt-5.5"),
            Some(128_000),
            Some(32_768),
            Some(3),
        );
        assert_model(
            &models[1],
            "glm-5.2",
            Some("GLM-5.2"),
            None,
            Some(1_000_000),
            Some(131_072),
            None,
        );

        let commands = runner.commands.borrow();
        assert_eq!(commands.len(), 1);
        assert_eq!(commands[0].python, PathBuf::from("python-test"));
        assert_eq!(commands[0].script, paths.catalog_sync_script());
        assert_eq!(commands[0].codex_dir, root.join("codex-home"));
    }

    #[test]
    fn generate_catalog_reads_codex_home_even_if_sync_prints_another_catalog_path() {
        let root = temp_root("generate-catalog-stdout-path");
        let paths = test_paths(&root);
        fs::create_dir_all(paths.catalog_sync_script().parent().unwrap()).unwrap();
        fs::write(paths.catalog_sync_script(), "# fake catalog sync").unwrap();
        let printed_catalog = root.join("printed").join("catalog.json");
        fs::create_dir_all(printed_catalog.parent().unwrap()).unwrap();
        fs::write(
            &printed_catalog,
            r#"{"models":[{"slug":"printed-model","display_name":"Printed Model"}]}"#,
        )
        .unwrap();
        let runner = WritingCatalogRunner::new(
            paths.generated_catalog_path(),
            r#"{"models":[{"slug":"codex-home-model","display_name":"Codex Home Model"}]}"#,
            CatalogCommandOutcome {
                code: Some(0),
                stdout: format!("catalog={}\n", printed_catalog.display()),
                stderr: String::new(),
            },
        );

        let models = generate_catalog_with_runner(&paths, Path::new("python-test"), &runner)
            .expect("catalog");

        assert_eq!(model_ids(&models), ["codex-home-model"]);
    }

    #[test]
    fn list_models_reads_generated_catalog_from_codex_home() {
        let _guard = ENV_LOCK.lock().unwrap();
        let previous = std::env::var_os("CODEX_HOME");
        let root = temp_root("list-models-codex-home");
        let codex_home = root.join("codex-home");
        let catalog_path = codex_home
            .join("model-catalogs")
            .join(super::GENERATED_CATALOG_FILE);
        fs::create_dir_all(catalog_path.parent().unwrap()).unwrap();
        fs::write(
            &catalog_path,
            r#"{"models":[{"slug":"codex-home-list-model","display_name":"Codex Home List Model"}]}"#,
        )
        .unwrap();
        std::env::set_var("CODEX_HOME", &codex_home);

        let models = list_models();

        restore_env("CODEX_HOME", previous);
        assert_eq!(
            model_ids(&models.expect("list models")),
            ["codex-home-list-model"]
        );
    }

    #[test]
    fn list_models_falls_back_to_legacy_generated_catalog_name() {
        let _guard = ENV_LOCK.lock().unwrap();
        let previous = std::env::var_os("CODEX_HOME");
        let root = temp_root("list-models-legacy-catalog");
        let codex_home = root.join("codex-home");
        let catalog_path = codex_home
            .join("model-catalogs")
            .join(super::LEGACY_GENERATED_CATALOG_FILE);
        fs::create_dir_all(catalog_path.parent().unwrap()).unwrap();
        fs::write(
            &catalog_path,
            r#"{"models":[{"slug":"legacy-list-model","display_name":"Legacy List Model"}]}"#,
        )
        .unwrap();
        std::env::set_var("CODEX_HOME", &codex_home);

        let models = list_models();

        restore_env("CODEX_HOME", previous);
        assert_eq!(
            model_ids(&models.expect("list models")),
            ["legacy-list-model"]
        );
    }

    #[test]
    fn list_model_metadata_merges_builtin_pricing_into_stale_cache() {
        let _guard = ENV_LOCK.lock().unwrap();
        let previous = std::env::var_os("CODEX_HOME");
        let root = temp_root("metadata-stale-cache");
        let codex_home = root.join("codex-home");
        let paths = ModelPaths::new(&codex_home, root.join("repo-root"));
        fs::create_dir_all(paths.metadata_cache_path().parent().unwrap()).unwrap();
        fs::write(
            paths.metadata_cache_path(),
            r#"[{"id":"openai/gpt-5.5","display_name":"Cached GPT-5.5","context_window":120000}]"#,
        )
        .unwrap();
        std::env::set_var("CODEX_HOME", &codex_home);

        let models = list_model_metadata().expect("metadata");

        restore_env("CODEX_HOME", previous);
        let gpt55 = models
            .iter()
            .find(|model| model.id == "gpt-5.5")
            .expect("gpt-5.5 metadata");
        assert_eq!(
            models
                .iter()
                .filter(|model| { model.id == "gpt-5.5" || model.id == "openai/gpt-5.5" })
                .count(),
            1
        );
        assert_eq!(gpt55.display_name.as_deref(), Some("Cached GPT-5.5"));
        assert_eq!(gpt55.context_window, Some(120_000));
        assert_eq!(
            gpt55
                .pricing
                .as_ref()
                .and_then(|pricing| pricing.input_per_million),
            Some(5.0)
        );
        assert_eq!(
            gpt55
                .pricing
                .as_ref()
                .and_then(|pricing| pricing.cached_input_per_million),
            Some(0.5)
        );
        assert_eq!(
            gpt55
                .pricing
                .as_ref()
                .and_then(|pricing| pricing.output_per_million),
            Some(22.5)
        );
    }

    fn compact_models(models: &[Model]) -> Vec<(&str, Option<u32>, Option<u32>)> {
        models
            .iter()
            .map(|model| {
                (
                    model.id.as_str(),
                    model.context_window,
                    model.max_output_tokens,
                )
            })
            .collect()
    }

    fn model_ids(models: &[Model]) -> Vec<&str> {
        models.iter().map(|model| model.id.as_str()).collect()
    }

    #[allow(clippy::too_many_arguments)]
    fn assert_model(
        model: &Model,
        id: &str,
        display_name: Option<&str>,
        upstream_model: Option<&str>,
        context_window: Option<u32>,
        max_output_tokens: Option<u32>,
        sort_order: Option<i32>,
    ) {
        assert_eq!(model.id, id);
        assert_eq!(model.display_name.as_deref(), display_name);
        assert_eq!(model.upstream_model.as_deref(), upstream_model);
        assert_eq!(model.context_window, context_window);
        assert_eq!(model.max_output_tokens, max_output_tokens);
        assert_eq!(model.sort_order, sort_order);
        assert!(model.enabled);
    }

    fn restore_env(name: &str, value: Option<std::ffi::OsString>) {
        match value {
            Some(value) => std::env::set_var(name, value),
            None => std::env::remove_var(name),
        }
    }

    #[derive(Debug)]
    struct MockServer {
        base_url: String,
        request: Receiver<String>,
        handle: JoinHandle<()>,
    }

    impl MockServer {
        fn json(body: &str, delay: Duration) -> Self {
            let listener = TcpListener::bind(("127.0.0.1", 0)).expect("bind mock server");
            let base_url = format!("http://{}", listener.local_addr().unwrap());
            let body = body.to_string();
            let (request_tx, request_rx) = mpsc::channel();
            let handle = thread::spawn(move || {
                let (mut stream, _) = listener.accept().expect("accept request");
                let mut buffer = [0; 8192];
                let count = stream.read(&mut buffer).expect("read request");
                let request = String::from_utf8_lossy(&buffer[..count]).to_string();
                request_tx.send(request).expect("send request");
                if !delay.is_zero() {
                    thread::sleep(delay);
                }
                let response = format!(
                    "HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-length: {}\r\nconnection: close\r\n\r\n{}",
                    body.len(),
                    body
                );
                let _ = stream.write_all(response.as_bytes());
            });

            Self {
                base_url,
                request: request_rx,
                handle,
            }
        }

        fn base_url(&self) -> String {
            self.base_url.clone()
        }

        fn request(&self) -> String {
            self.request
                .recv_timeout(Duration::from_secs(2))
                .expect("mock server received request")
        }

        fn join(self) {
            self.handle.join().expect("mock server thread");
        }
    }

    #[derive(Debug, Clone)]
    struct RecordedCatalogCommand {
        python: PathBuf,
        script: PathBuf,
        codex_dir: PathBuf,
    }

    struct WritingCatalogRunner {
        commands: RefCell<Vec<RecordedCatalogCommand>>,
        catalog_path: PathBuf,
        catalog_body: String,
        outcome: CatalogCommandOutcome,
    }

    impl WritingCatalogRunner {
        fn new(catalog_path: PathBuf, catalog_body: &str, outcome: CatalogCommandOutcome) -> Self {
            Self {
                commands: RefCell::new(Vec::new()),
                catalog_path,
                catalog_body: catalog_body.to_string(),
                outcome,
            }
        }
    }

    impl CatalogSyncRunner for WritingCatalogRunner {
        fn run_sync(
            &self,
            python: &Path,
            script: &Path,
            codex_dir: &Path,
        ) -> Result<CatalogCommandOutcome, String> {
            let catalog_parent = self
                .catalog_path
                .parent()
                .ok_or_else(|| "catalog path must have a parent".to_string())?;
            assert!(
                catalog_parent.is_dir(),
                "catalog output directory should exist before sync runs"
            );
            fs::write(&self.catalog_path, &self.catalog_body)
                .map_err(|error| format!("failed to write test catalog: {error}"))?;
            self.commands.borrow_mut().push(RecordedCatalogCommand {
                python: python.to_path_buf(),
                script: script.to_path_buf(),
                codex_dir: codex_dir.to_path_buf(),
            });
            Ok(self.outcome.clone())
        }
    }

    fn test_paths(root: &Path) -> ModelPaths {
        ModelPaths::new(root.join("codex-home"), root.join("repo-root"))
    }

    fn temp_root(name: &str) -> PathBuf {
        let suffix = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let path = std::env::temp_dir().join(format!(
            "codexhub-models-{name}-{}-{suffix}",
            std::process::id()
        ));
        let _ = fs::remove_dir_all(&path);
        fs::create_dir_all(&path).unwrap();
        path
    }
}
