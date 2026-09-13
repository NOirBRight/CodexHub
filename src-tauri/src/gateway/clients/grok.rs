use super::super::{
    adopt_legacy_baseline_locked, ensure_rollback_baseline, executable_version,
    gateway_client_provider_groups, is_codexhub_client_provider_id, is_local_gateway_url,
    read_rollback_baseline, resolve_gateway_client_model_id, route_owner_from_endpoint,
    sanitize_text, write_text_replace, BackupChannel, BaselineFile, GatewayClientApplyResult,
    GatewayClientConfigPreview, GatewayClientEndpointSelection, GatewayClientProviderGroup,
};
use crate::app_flavor::RoutingOwner;
use crate::{Provider, Settings};
use std::fs;
use std::path::{Path, PathBuf};
use toml::{Table, Value};

const CLIENT_ID: &str = "grok";
const SENTINEL_PROVIDER_ID: &str = "codexhub";

pub(in crate::gateway) fn grok_home() -> PathBuf {
    if let Some(path) = std::env::var_os("CODEXHUB_GROK_HOME")
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
    {
        return path;
    }
    if let Some(path) = std::env::var_os("GROK_HOME")
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
    {
        return path;
    }
    dirs::home_dir()
        .unwrap_or_else(|| PathBuf::from("."))
        .join(".grok")
}

pub(in crate::gateway) fn detect_grok_config_path() -> PathBuf {
    grok_home().join("config.toml")
}

pub(in crate::gateway) fn detect_grok_executable_path() -> Option<PathBuf> {
    which::which("grok")
        .ok()
        .or_else(|| detect_grok_executable_path_in_home(&grok_home()))
}

pub(in crate::gateway) fn detect_grok_executable_path_in_home(home: &Path) -> Option<PathBuf> {
    let executable = if cfg!(windows) { "grok.exe" } else { "grok" };
    let candidate = home.join("bin").join(executable);
    candidate.is_file().then_some(candidate)
}

pub(in crate::gateway) fn detect_grok_version() -> Option<String> {
    detect_grok_executable_path()
        .as_deref()
        .and_then(executable_version)
        .or_else(grok_version_from_home)
}

fn grok_version_from_home() -> Option<String> {
    let text = fs::read_to_string(grok_home().join("version.json")).ok()?;
    let value: serde_json::Value = serde_json::from_str(&text).ok()?;
    value
        .get("version")
        .and_then(serde_json::Value::as_str)
        .map(str::trim)
        .filter(|value| !value.is_empty())
        .map(ToOwned::to_owned)
}

pub(in crate::gateway) fn grok_installed() -> bool {
    detect_grok_executable_path().is_some()
        || detect_grok_config_path().exists()
        || grok_home().join("bin").exists()
}

fn grok_skips_upstream(provider: &Provider) -> bool {
    provider.id.eq_ignore_ascii_case("xai")
        || provider.auth_capabilities.as_ref().is_some_and(|caps| {
            caps.iter()
                .any(|capability| capability == "subscription:xai_oauth")
        })
}

fn grok_skips_group(group: &GatewayClientProviderGroup, providers: &[Provider]) -> bool {
    providers.iter().any(|provider| {
        super::super::codexhub_client_provider_id(&provider.id) == group.client_provider_id
            && grok_skips_upstream(provider)
    })
}

fn is_leftover_codexhub_xai_key(key: &str) -> bool {
    key == "codexhub-xai" || key.starts_with("codexhub-xai-")
}

fn grok_picker_key(client_provider_id: &str, short_id: &str) -> String {
    format!(
        "{}-{}",
        client_provider_id,
        short_id.replace(['/', '\\'], "-")
    )
}

fn grok_model_display_name(display_name: &str) -> String {
    if display_name.starts_with("CodexHub ") {
        display_name.to_string()
    } else {
        format!("CodexHub {display_name}")
    }
}

fn parse_grok_table(text: &str) -> Result<Table, String> {
    if text.trim().is_empty() {
        return Ok(Table::new());
    }
    toml::from_str(text).map_err(|error| format!("Grok config is not valid TOML: {error}"))
}

fn grok_toml_to_string(root: &Table) -> Result<String, String> {
    toml::to_string_pretty(&Value::Table(root.clone()))
        .map(|text| {
            if text.is_empty() || text.ends_with('\n') {
                text
            } else {
                format!("{text}\n")
            }
        })
        .map_err(|error| format!("failed to serialize Grok config: {error}"))
}

fn table_mut<'a>(root: &'a mut Table, key: &str) -> Result<&'a mut Table, String> {
    if !matches!(root.get(key), Some(Value::Table(_))) {
        root.insert(key.to_string(), Value::Table(Table::new()));
    }
    root.get_mut(key)
        .and_then(Value::as_table_mut)
        .ok_or_else(|| format!("Grok config {key} must be a table"))
}

fn table_base_url(table: &Table) -> Option<&str> {
    table.get("base_url").and_then(Value::as_str)
}

fn grok_owned_table_conflict(root: &Table) -> Result<(), String> {
    if let Some(providers) = root.get("model_providers").and_then(Value::as_table) {
        for (key, value) in providers {
            if !is_codexhub_client_provider_id(key) || is_leftover_codexhub_xai_key(key) {
                continue;
            }
            let Some(table) = value.as_table() else {
                continue;
            };
            if let Some(url) = table_base_url(table) {
                if !is_local_gateway_url(url) {
                    return Err(format!(
                        "Grok config already has [model_providers.{key}] pointing at a non-Gateway endpoint; refusing overwrite."
                    ));
                }
            }
        }
    }
    if let Some(models) = root.get("model").and_then(Value::as_table) {
        for (key, value) in models {
            if !is_codexhub_client_provider_id(key) || is_leftover_codexhub_xai_key(key) {
                continue;
            }
            let Some(table) = value.as_table() else {
                continue;
            };
            if let Some(url) = table_base_url(table) {
                if !is_local_gateway_url(url) {
                    return Err(format!(
                        "Grok config already has [model.{key}] pointing at a non-Gateway endpoint; refusing overwrite."
                    ));
                }
            }
        }
    }
    Ok(())
}

fn strip_owned_grok_tables(root: &mut Table) {
    if let Some(Value::Table(providers)) = root.get_mut("model_providers") {
        providers.retain(|key, _| !is_codexhub_client_provider_id(key));
        if providers.is_empty() {
            root.remove("model_providers");
        }
    }
    if let Some(Value::Table(models)) = root.get_mut("model") {
        models.retain(|key, _| !is_codexhub_client_provider_id(key));
        if models.is_empty() {
            root.remove("model");
        }
    }
}

fn repair_leftover_xai_default(root: &mut Table) {
    let Some(Value::Table(models)) = root.get_mut("models") else {
        return;
    };
    let should_clear = models
        .get("default")
        .and_then(Value::as_str)
        .is_some_and(is_leftover_codexhub_xai_key);
    if should_clear {
        models.remove("default");
    }
}

fn insert_owned_provider(
    providers: &mut Table,
    id: &str,
    base_url: String,
    api_key: &str,
    backend: &str,
) {
    let mut table = Table::new();
    table.insert("base_url".to_string(), Value::String(base_url));
    table.insert("api_key".to_string(), Value::String(api_key.to_string()));
    table.insert(
        "api_backend".to_string(),
        Value::String(backend.to_string()),
    );
    providers.insert(id.to_string(), Value::Table(table));
}

fn insert_owned_model(
    models: &mut Table,
    picker_key: &str,
    short_id: &str,
    display_name: &str,
    provider_id: &str,
    context_window: Option<u32>,
) {
    let mut table = Table::new();
    table.insert("model".to_string(), Value::String(short_id.to_string()));
    table.insert(
        "name".to_string(),
        Value::String(grok_model_display_name(display_name)),
    );
    table.insert(
        "model_provider".to_string(),
        Value::String(provider_id.to_string()),
    );
    if let Some(window) = context_window {
        table.insert(
            "context_window".to_string(),
            Value::Integer(i64::from(window)),
        );
    }
    table.insert("supports_backend_search".to_string(), Value::Boolean(false));
    models.insert(picker_key.to_string(), Value::Table(table));
}

fn sentinel_base_url(settings: &Settings) -> String {
    super::super::endpoints(settings.proxy_port)
        .base_url
        .trim_end_matches('/')
        .to_string()
}

pub(in crate::gateway) fn grok_config_text(
    current: Option<&str>,
    settings: &Settings,
    providers: &[Provider],
    model: &str,
) -> Result<String, String> {
    // Provider Injection (ADR-0004 / #523): surgical TOML merge. Preserve
    // every user-owned table. Never write [models] default (Activation),
    // [endpoints], extra_headers, or Grok auth.json. Skip CodexHub's xAI
    // subscription so native grok login stays the only Grok catalog.
    let groups = gateway_client_provider_groups(settings, providers, model)?;
    let mut root = parse_grok_table(current.unwrap_or(""))?;
    grok_owned_table_conflict(&root)?;
    strip_owned_grok_tables(&mut root);

    let kept: Vec<&GatewayClientProviderGroup> = groups
        .providers
        .iter()
        .filter(|group| !grok_skips_group(group, providers))
        .collect();

    if kept.is_empty() {
        insert_owned_provider(
            table_mut(&mut root, "model_providers")?,
            SENTINEL_PROVIDER_ID,
            sentinel_base_url(settings),
            &settings.gateway_client_key,
            GatewayClientEndpointSelection::Responses.grok_api_backend(),
        );
    } else {
        {
            let provider_tables = table_mut(&mut root, "model_providers")?;
            for group in &kept {
                insert_owned_provider(
                    provider_tables,
                    &group.client_provider_id,
                    group.base_url.clone(),
                    &settings.gateway_client_key,
                    group.endpoint_selection.grok_api_backend(),
                );
            }
        }
        let model_tables = table_mut(&mut root, "model")?;
        for group in &kept {
            for gateway_model in &group.models {
                insert_owned_model(
                    model_tables,
                    &grok_picker_key(&group.client_provider_id, &gateway_model.id),
                    &gateway_model.id,
                    &gateway_model.display_name,
                    &group.client_provider_id,
                    gateway_model.context_window,
                );
            }
        }
    }
    grok_toml_to_string(&root)
}

pub(in crate::gateway) fn is_grok_codexhub_config(text: &str) -> bool {
    let Ok(root) = parse_grok_table(text) else {
        return text.contains("codexhub");
    };
    if root
        .get("model_providers")
        .and_then(Value::as_table)
        .is_some_and(|providers| {
            providers
                .keys()
                .any(|key| is_codexhub_client_provider_id(key))
        })
    {
        return true;
    }
    root.get("model")
        .and_then(Value::as_table)
        .is_some_and(|models| models.keys().any(|key| is_codexhub_client_provider_id(key)))
}

fn grok_has_allowed_models(text: &str) -> bool {
    parse_grok_table(text)
        .ok()
        .and_then(|root| {
            root.get("models")
                .and_then(Value::as_table)
                .and_then(|models| models.get("allowed_models"))
                .cloned()
        })
        .is_some_and(|value| match value {
            Value::Array(items) => !items.is_empty(),
            Value::String(text) => !text.trim().is_empty(),
            _ => false,
        })
}

pub(in crate::gateway) fn grok_injected_keys_may_be_hidden(
    home: &Path,
    config_text: Option<&str>,
) -> bool {
    config_text.is_some_and(grok_has_allowed_models)
        || fs::read_to_string(home.join("requirements.toml"))
            .ok()
            .as_deref()
            .is_some_and(grok_has_allowed_models)
}

fn grok_owned_provider_base_url(text: &str) -> Option<String> {
    let root = parse_grok_table(text).ok()?;
    let providers = root.get("model_providers").and_then(Value::as_table)?;
    let mut fallback = None;
    for (key, value) in providers {
        if !is_codexhub_client_provider_id(key) {
            continue;
        }
        let url = value.as_table().and_then(table_base_url)?;
        if key == SENTINEL_PROVIDER_ID {
            fallback = Some(url.to_string());
            continue;
        }
        return Some(url.to_string());
    }
    fallback
}

pub(in crate::gateway) fn detect_grok_route_details(
    current_owner: RoutingOwner,
    current_port: u16,
) -> (RoutingOwner, Option<String>) {
    let path = detect_grok_config_path();
    let text = fs::read_to_string(&path).ok();
    let managed = text.as_deref().is_some_and(is_grok_codexhub_config);
    let route_endpoint = text.as_deref().and_then(grok_owned_provider_base_url);
    (
        route_owner_from_endpoint(
            route_endpoint.as_deref(),
            managed,
            text.is_some(),
            current_owner,
            current_port,
        ),
        route_endpoint,
    )
}

pub(in crate::gateway) fn preview_grok_config_with_path(
    config_path: &Path,
    settings: &Settings,
    providers: &[Provider],
    model: &str,
) -> Result<GatewayClientConfigPreview, String> {
    let current = fs::read_to_string(config_path)
        .ok()
        .map(|text| sanitize_text(&text));
    let next = grok_config_text(
        fs::read_to_string(config_path).ok().as_deref(),
        settings,
        providers,
        model,
    )?;
    Ok(GatewayClientConfigPreview {
        client_id: CLIENT_ID.to_string(),
        can_apply: true,
        strategy: "provider_injection".to_string(),
        config_path: Some(config_path.to_path_buf()),
        current_redacted: current,
        next_redacted: sanitize_text(&next),
        backup_required: config_path.exists(),
        message: "Apply will back up the current Grok CLI config, then surgically add CodexHub Gateway models while preserving your own providers and settings.".to_string(),
    })
}

pub(in crate::gateway) struct GrokApplyPlan {
    pub config_path: PathBuf,
    pub next: String,
    pub skip_snapshot: bool,
}

pub(in crate::gateway) fn plan_grok_apply(
    config_path: &Path,
    settings: &Settings,
    providers: &[Provider],
    model: &str,
) -> Result<GrokApplyPlan, String> {
    let model = resolve_gateway_client_model_id(settings, providers, model)?;
    let current = if config_path.exists() {
        Some(
            fs::read_to_string(config_path)
                .map_err(|error| format!("failed to read Grok config: {error}"))?,
        )
    } else {
        None
    };
    let skip_snapshot = current
        .as_deref()
        .is_none_or(|text| text.trim().is_empty() || is_grok_codexhub_config(text));
    let next = grok_config_text(current.as_deref(), settings, providers, &model)?;
    Ok(GrokApplyPlan {
        config_path: config_path.to_path_buf(),
        skip_snapshot,
        next,
    })
}

pub(in crate::gateway) fn publish_grok_apply(
    plan: &GrokApplyPlan,
    backup_roots: &[(PathBuf, BackupChannel)],
) -> Result<GatewayClientApplyResult, String> {
    let (backup_root, _) = backup_roots
        .first()
        .ok_or_else(|| "Grok apply requires at least one backup root".to_string())?;
    if let Some(parent) = plan.config_path.parent() {
        fs::create_dir_all(parent)
            .map_err(|error| format!("failed to create Grok config directory: {error}"))?;
    }
    fs::create_dir_all(backup_root)
        .map_err(|error| format!("failed to create Grok backup directory: {error}"))?;
    let backup_path = if plan.skip_snapshot || !plan.config_path.exists() {
        None
    } else {
        let path = backup_root.join(format!("grok-{}.toml", super::super::timestamp_millis()));
        fs::copy(&plan.config_path, &path)
            .map_err(|error| format!("failed to back up Grok config: {error}"))?;
        Some(path)
    };
    record_grok_rollback_baseline(&plan.config_path, backup_roots)?;
    write_text_replace(&plan.config_path, &plan.next)
        .map_err(|_| "failed to write managed Grok config".to_string())?;
    Ok(GatewayClientApplyResult {
        client_id: CLIENT_ID.to_string(),
        applied: true,
        config_path: Some(plan.config_path.clone()),
        backup_path,
        message: "Grok CLI now has the CodexHub provider available. Model selection is unchanged."
            .to_string(),
    })
}

pub(in crate::gateway) fn record_grok_rollback_baseline(
    config_path: &Path,
    backup_roots: &[(PathBuf, BackupChannel)],
) -> Result<(), String> {
    ensure_rollback_baseline(
        "grok",
        backup_roots,
        &[("config.toml", config_path)],
        |_, text| is_grok_codexhub_config(text),
    )
}

pub(in crate::gateway) fn restore_grok_from_baseline(
    config_path: &Path,
    file: &BaselineFile,
) -> Result<GatewayClientApplyResult, String> {
    match file {
        BaselineFile::Snapshot { content } => {
            write_text_replace(config_path, content)
                .map_err(|_| "failed to restore Grok config from baseline".to_string())?;
            Ok(GatewayClientApplyResult {
                client_id: CLIENT_ID.to_string(),
                applied: true,
                config_path: None,
                backup_path: None,
                message: "Grok CLI official config restored from canonical baseline.".to_string(),
            })
        }
        BaselineFile::Absent => {
            if config_path.exists() {
                let text = fs::read_to_string(config_path).unwrap_or_default();
                if !is_grok_codexhub_config(&text) && !text.trim().is_empty() {
                    return Err(
                        "Grok config exists but is not managed by CodexHub; refusing removal."
                            .to_string(),
                    );
                }
                grok_ownership_bounded_cleanup(config_path)?;
            }
            Ok(GatewayClientApplyResult {
                client_id: CLIENT_ID.to_string(),
                applied: true,
                config_path: None,
                backup_path: None,
                message:
                    "Grok CLI CodexHub entries removed; original baseline recorded it as absent."
                        .to_string(),
            })
        }
    }
}

pub(in crate::gateway) fn grok_ownership_bounded_cleanup(
    config_path: &Path,
) -> Result<GatewayClientApplyResult, String> {
    if !config_path.exists() {
        return Ok(GatewayClientApplyResult {
            client_id: CLIENT_ID.to_string(),
            applied: true,
            config_path: None,
            backup_path: None,
            message: "Grok config was already absent.".to_string(),
        });
    }
    let text = fs::read_to_string(config_path)
        .map_err(|_| "failed to read Grok config for cleanup.".to_string())?;
    if !is_grok_codexhub_config(&text) {
        return Ok(GatewayClientApplyResult {
            client_id: CLIENT_ID.to_string(),
            applied: true,
            config_path: Some(config_path.to_path_buf()),
            backup_path: None,
            message: "Grok config has no CodexHub block.".to_string(),
        });
    }
    let mut root = parse_grok_table(&text)?;
    strip_owned_grok_tables(&mut root);
    repair_leftover_xai_default(&mut root);
    if root.is_empty() {
        fs::remove_file(config_path)
            .map_err(|_| "failed to remove CodexHub-owned Grok config.".to_string())?;
        return Ok(GatewayClientApplyResult {
            client_id: CLIENT_ID.to_string(),
            applied: true,
            config_path: None,
            backup_path: None,
            message: "Grok CLI CodexHub config removed.".to_string(),
        });
    }
    let next = grok_toml_to_string(&root)?;
    write_text_replace(config_path, &next)
        .map_err(|_| "failed to write cleaned Grok config".to_string())?;
    Ok(GatewayClientApplyResult {
        client_id: CLIENT_ID.to_string(),
        applied: true,
        config_path: Some(config_path.to_path_buf()),
        backup_path: None,
        message: "Grok CLI CodexHub entries removed while preserving unrelated config.".to_string(),
    })
}

pub(in crate::gateway) fn restore_grok_config_with_backup_roots(
    config_path: &Path,
    backup_roots: &[(PathBuf, BackupChannel)],
) -> Result<GatewayClientApplyResult, String> {
    if let Some(baseline) = read_rollback_baseline("grok")? {
        return match baseline.files.get("config.toml") {
            Some(file) => restore_grok_from_baseline(config_path, file),
            None => Err("rollback baseline is incomplete".to_string()),
        };
    }
    let _ = adopt_legacy_baseline_locked("grok", backup_roots)?;
    if let Some(baseline) = read_rollback_baseline("grok")? {
        return match baseline.files.get("config.toml") {
            Some(file) => {
                let mut result = restore_grok_from_baseline(config_path, file)?;
                result.message =
                    "Grok CLI official config restored from legacy-adopted baseline.".to_string();
                Ok(result)
            }
            None => Err("rollback baseline is incomplete".to_string()),
        };
    }
    grok_ownership_bounded_cleanup(config_path)
}
