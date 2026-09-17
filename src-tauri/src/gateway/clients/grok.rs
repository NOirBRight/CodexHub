use super::super::{
    adopt_legacy_baseline_locked, ensure_rollback_baseline, executable_version,
    gateway_client_provider_groups, is_codexhub_client_provider_id, is_this_app_gateway_url,
    read_rollback_baseline, resolve_gateway_client_model_id, rollback_file_text,
    route_owner_from_endpoint, sanitize_text, write_text_replace, BackupChannel, BaselineFile,
    GatewayClientApplyResult, GatewayClientConfigPreview, GatewayClientEndpointSelection,
    GatewayClientProviderGroup, GatewayClientProviderModel, RollbackBaseline,
};
use crate::app_flavor::RoutingOwner;
use crate::{Provider, Settings};
use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};
use toml::{Table, Value};

const GROK_FLEET_REQUIREMENTS: &str = "/etc/grok/requirements.toml";

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

fn owning_kept_provider_id<'a>(key: &str, kept_ids: &'a [String]) -> Option<&'a str> {
    kept_ids
        .iter()
        .filter(|id| key == id.as_str() || key.starts_with(&format!("{id}-")))
        .max_by_key(|id| id.len())
        .map(String::as_str)
}

fn is_leftover_codexhub_xai_key(key: &str, kept_ids: &[String]) -> bool {
    // Longest kept id wins so a live `codexhub-xai-proxy` group is not treated
    // as leftover `codexhub-xai*` from an older adapter.
    if owning_kept_provider_id(key, kept_ids).is_some() {
        return false;
    }
    key == "codexhub-xai" || key.starts_with("codexhub-xai-")
}

fn is_legacy_skipped_xai_selector(value: &str) -> bool {
    // Story 12: only leftover selectors from the skipped Maintained xAI
    // adapter. Live custom `codexhub-xai-proxy*` stays as Activation.
    value == "codexhub-xai" || value.starts_with("codexhub-xai-grok")
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

fn grok_owned_table_conflict(
    root: &Table,
    kept_ids: &[String],
    settings: &Settings,
) -> Result<(), String> {
    if let Some(providers) = root.get("model_providers").and_then(Value::as_table) {
        for (key, value) in providers {
            if !is_codexhub_client_provider_id(key) || is_leftover_codexhub_xai_key(key, kept_ids) {
                continue;
            }
            let Some(table) = value.as_table() else {
                continue;
            };
            grok_refuse_foreign_owned_url(
                table_base_url(table),
                settings.proxy_port,
                &format!("[model_providers.{key}]"),
                true,
            )?;
        }
    }
    if let Some(models) = root.get("model").and_then(Value::as_table) {
        for (key, value) in models {
            if !is_codexhub_client_provider_id(key) || is_leftover_codexhub_xai_key(key, kept_ids) {
                continue;
            }
            let Some(table) = value.as_table() else {
                continue;
            };
            grok_refuse_foreign_owned_url(
                table_base_url(table),
                settings.proxy_port,
                &format!("[model.{key}]"),
                false,
            )?;
        }
    }
    Ok(())
}

fn grok_refuse_foreign_owned_url(
    url: Option<&str>,
    port: u16,
    table_name: &str,
    missing_is_conflict: bool,
) -> Result<(), String> {
    match url {
        Some(url) if is_this_app_gateway_url(url, port) => Ok(()),
        Some(_) => Err(format!(
            "Grok config already has {table_name} pointing at a non-Gateway endpoint; refusing overwrite."
        )),
        None if missing_is_conflict => Err(format!(
            "Grok config already has {table_name} without a Gateway base_url; refusing overwrite."
        )),
        None => Ok(()),
    }
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
        .is_some_and(is_legacy_skipped_xai_selector);
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

const GROK_REASONING_EFFORTS: [&str; 7] =
    ["none", "minimal", "low", "medium", "high", "xhigh", "max"];

fn grok_canonical_reasoning_effort(level: &str) -> Option<String> {
    let effort = level.trim().to_ascii_lowercase();
    grok_effort_rank(&effort).map(|_| effort)
}

fn grok_effort_rank(effort: &str) -> Option<usize> {
    GROK_REASONING_EFFORTS
        .iter()
        .position(|name| *name == effort)
}

fn grok_snap_default(efforts: &[String], default: Option<&str>) -> String {
    let Some(requested) = default.filter(|value| !value.trim().is_empty()) else {
        return efforts[0].clone();
    };
    if let Some(canonical) = grok_canonical_reasoning_effort(requested) {
        if efforts.iter().any(|effort| effort == &canonical) {
            return canonical;
        }
        if let Some(target) = grok_effort_rank(&canonical) {
            return grok_nearest_effort(efforts, target);
        }
    }
    grok_nearest_effort(efforts, GROK_REASONING_EFFORTS.len())
}

fn grok_nearest_effort(efforts: &[String], target: usize) -> String {
    efforts
        .iter()
        .min_by_key(|effort| {
            let rank = grok_effort_rank(effort).unwrap_or(0);
            (rank.abs_diff(target), usize::MAX - rank)
        })
        .cloned()
        .unwrap_or_else(|| efforts[0].clone())
}

fn grok_reasoning_effort_label(effort: &str) -> &'static str {
    match effort {
        "none" => "None",
        "minimal" => "Minimal Effort",
        "low" => "Low Effort",
        "medium" => "Medium Effort",
        "high" => "High Effort",
        "xhigh" => "Extra High Effort",
        "max" => "Max Effort",
        _ => "Effort",
    }
}

fn grok_reasoning_effort_menu(
    gateway_model: &GatewayClientProviderModel,
) -> Option<(String, Vec<Value>)> {
    let mut efforts: Vec<String> = gateway_model
        .supported_reasoning_levels
        .iter()
        .filter_map(|level| grok_canonical_reasoning_effort(level))
        .collect();
    if gateway_model.thinking_off_control() && !efforts.iter().any(|effort| effort == "none") {
        efforts.push("none".to_string());
    }
    if efforts.is_empty() {
        return None;
    }
    let default = grok_snap_default(&efforts, gateway_model.default_reasoning_level.as_deref());
    let menu = efforts
        .into_iter()
        .map(|effort| {
            let mut entry = Table::new();
            entry.insert("id".to_string(), Value::String(effort.clone()));
            entry.insert("value".to_string(), Value::String(effort.clone()));
            entry.insert(
                "label".to_string(),
                Value::String(grok_reasoning_effort_label(&effort).to_string()),
            );
            entry.insert("default".to_string(), Value::Boolean(effort == default));
            Value::Table(entry)
        })
        .collect();
    Some((default, menu))
}

fn insert_owned_model(
    models: &mut Table,
    picker_key: &str,
    gateway_model: &GatewayClientProviderModel,
    provider_id: &str,
) {
    let mut table = Table::new();
    table.insert("model".to_string(), Value::String(gateway_model.id.clone()));
    table.insert(
        "name".to_string(),
        Value::String(grok_model_display_name(&gateway_model.display_name)),
    );
    table.insert(
        "model_provider".to_string(),
        Value::String(provider_id.to_string()),
    );
    if let Some(window) = gateway_model.context_window {
        table.insert(
            "context_window".to_string(),
            Value::Integer(i64::from(window)),
        );
    }
    if let Some((default, menu)) = grok_reasoning_effort_menu(gateway_model) {
        table.insert(
            "supports_reasoning_effort".to_string(),
            Value::Boolean(true),
        );
        table.insert("reasoning_effort".to_string(), Value::String(default));
        table.insert("reasoning_efforts".to_string(), Value::Array(menu));
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
    let kept: Vec<&GatewayClientProviderGroup> = groups
        .providers
        .iter()
        .filter(|group| !grok_skips_group(group, providers))
        .collect();
    let kept_ids: Vec<String> = kept
        .iter()
        .map(|group| group.client_provider_id.clone())
        .collect();
    let mut root = parse_grok_table(current.unwrap_or(""))?;
    grok_owned_table_conflict(&root, &kept_ids, settings)?;
    strip_owned_grok_tables(&mut root);

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
                    gateway_model,
                    &group.client_provider_id,
                );
            }
        }
    }
    grok_owned_default_subagent(&mut root, current, settings, providers, model)?;
    grok_toml_to_string(&root)
}

fn grok_owned_default_subagent(
    root: &mut Table,
    current: Option<&str>,
    settings: &Settings,
    providers: &[Provider],
    model: &str,
) -> Result<(), String> {
    let pin =
        super::super::resolve_client_default_subagent_pin(settings, providers, "grok", model)?;
    let baseline_text = super::super::rollback_file_text(CLIENT_ID, "config.toml", current);
    let baseline = baseline_text
        .as_deref()
        .and_then(|text| parse_grok_table(text).ok());
    apply_grok_subagent_tables(root, baseline.as_ref(), pin.as_ref());
    Ok(())
}

fn apply_grok_subagent_tables(
    root: &mut Table,
    baseline: Option<&Table>,
    pin: Option<&super::super::ClientDefaultSubagentPin>,
) {
    match pin {
        Some(pin) => {
            let models = nested_table_mut(root, &["subagents", "models"]);
            let picker = pin.grok_picker_key();
            for spawn_type in super::super::GROK_SPAWN_TYPES {
                models.insert((*spawn_type).to_string(), Value::String(picker.clone()));
            }
            for spawn_type in super::super::GROK_SPAWN_TYPES {
                if grok_role_can_carry_effort(root, spawn_type) {
                    let role = nested_table_mut(root, &["subagents", "roles", spawn_type]);
                    role.insert(
                        "reasoning_effort".to_string(),
                        Value::String(pin.effort.clone()),
                    );
                }
            }
        }
        None if grok_spawn_models_are_owned(root) => restore_grok_subagent_tables(root, baseline),
        None => {}
    }
}

fn grok_spawn_models_are_owned(root: &Table) -> bool {
    let Some(models) = root
        .get("subagents")
        .and_then(Value::as_table)
        .and_then(|table| table.get("models"))
        .and_then(Value::as_table)
    else {
        return false;
    };
    super::super::GROK_SPAWN_TYPES.iter().any(|spawn_type| {
        models
            .get(*spawn_type)
            .and_then(Value::as_str)
            .is_some_and(|value| value.starts_with("codexhub-"))
    })
}

fn grok_role_can_carry_effort(root: &Table, spawn_type: &str) -> bool {
    let Some(roles) = root
        .get("subagents")
        .and_then(Value::as_table)
        .and_then(|table| table.get("roles"))
        .and_then(Value::as_table)
    else {
        return true;
    };
    match roles.get(spawn_type) {
        None => true,
        Some(Value::Table(_)) => true,
        Some(_) => false,
    }
}

const GROK_SHADOW_MARKER: &str = "x-codexhub-default-subagent: true";

fn grok_agents_dir(config_path: &Path) -> PathBuf {
    config_path
        .parent()
        .map(|parent| parent.join("agents"))
        .unwrap_or_else(|| PathBuf::from("agents"))
}

pub(in crate::gateway) fn grok_shadow_agent_plan(
    config_path: &Path,
    current: Option<&Table>,
    settings: &Settings,
    providers: &[Provider],
    model: &str,
) -> Result<Vec<(PathBuf, Option<String>)>, String> {
    let pin =
        super::super::resolve_client_default_subagent_pin(settings, providers, "grok", model)?;
    let agents_dir = grok_agents_dir(config_path);
    let mut files = Vec::new();
    for spawn_type in super::super::GROK_SPAWN_TYPES {
        let path = agents_dir.join(format!("{spawn_type}.md"));
        match &pin {
            Some(pin)
                if current.is_some_and(|table| !grok_role_can_carry_effort(table, spawn_type)) =>
            {
                files.push((
                    path,
                    Some(format!(
                        "---\nname: {spawn_type}\nmodel: {}\nreasoning_effort: {}\n{GROK_SHADOW_MARKER}\n---\n",
                        pin.grok_picker_key(),
                        pin.effort
                    )),
                ));
            }
            Some(_) if grok_shadow_is_ours(&path) => files.push((path, None)),
            None => match rollback_file_text(CLIENT_ID, &format!("agents/{spawn_type}.md"), None) {
                Some(content) if !content.is_empty() => files.push((path, Some(content))),
                Some(_) => files.push((path, None)),
                None if grok_shadow_is_ours(&path) => files.push((path, None)),
                None => {}
            },
            _ => {}
        }
    }
    Ok(files)
}

fn grok_shadow_is_ours(path: &Path) -> bool {
    fs::read_to_string(path)
        .ok()
        .is_some_and(|text| text.contains(GROK_SHADOW_MARKER))
}

fn publish_grok_shadow_agents(files: &[(PathBuf, Option<String>)]) -> Result<(), String> {
    for (path, content) in files {
        match content {
            Some(text) => {
                if let Some(parent) = path.parent() {
                    fs::create_dir_all(parent).map_err(|error| {
                        format!("failed to create Grok agents directory: {error}")
                    })?;
                }
                write_text_replace(path, text)
                    .map_err(|_| "failed to write Grok shadow agent".to_string())?;
            }
            None if path.exists() => {
                fs::remove_file(path)
                    .map_err(|error| format!("failed to remove Grok shadow agent: {error}"))?;
            }
            None => {}
        }
    }
    Ok(())
}

fn clear_owned_grok_shadow_agents(config_path: &Path) -> Result<(), String> {
    let agents_dir = grok_agents_dir(config_path);
    let files = super::super::GROK_SPAWN_TYPES
        .iter()
        .map(|spawn_type| (agents_dir.join(format!("{spawn_type}.md")), None))
        .filter(|(path, _)| grok_shadow_is_ours(path))
        .collect::<Vec<_>>();
    publish_grok_shadow_agents(&files)
}

pub(in crate::gateway) fn grok_owned_shadows_match(
    config_path: &Path,
    settings: &Settings,
    providers: &[Provider],
    model: &str,
) -> Result<bool, String> {
    let current = fs::read_to_string(config_path)
        .ok()
        .and_then(|text| parse_grok_table(&text).ok());
    let expected =
        grok_shadow_agent_plan(config_path, current.as_ref(), settings, providers, model)?;
    for (path, content) in expected {
        match content {
            Some(text) => {
                if fs::read_to_string(&path).ok().as_deref() != Some(text.as_str()) {
                    return Ok(false);
                }
            }
            None if grok_shadow_is_ours(&path) => return Ok(false),
            None => {}
        }
    }
    Ok(true)
}

fn nested_table_mut<'a>(root: &'a mut Table, path: &[&str]) -> &'a mut Table {
    let mut current = root;
    for key in path {
        if !matches!(current.get(*key), Some(Value::Table(_))) {
            current.insert((*key).to_string(), Value::Table(Table::new()));
        }
        current = current
            .get_mut(*key)
            .and_then(Value::as_table_mut)
            .expect("nested table");
    }
    current
}

fn restore_grok_subagent_tables(root: &mut Table, baseline: Option<&Table>) {
    let baseline_models = baseline
        .and_then(|table| table.get("subagents"))
        .and_then(Value::as_table)
        .and_then(|table| table.get("models"))
        .and_then(Value::as_table);
    let baseline_roles = baseline
        .and_then(|table| table.get("subagents"))
        .and_then(Value::as_table)
        .and_then(|table| table.get("roles"))
        .and_then(Value::as_table);
    {
        let models = nested_table_mut(root, &["subagents", "models"]);
        for spawn_type in super::super::GROK_SPAWN_TYPES {
            match baseline_models.and_then(|table| table.get(*spawn_type)) {
                Some(value) => {
                    models.insert((*spawn_type).to_string(), value.clone());
                }
                None => {
                    models.remove(*spawn_type);
                }
            }
        }
        if models.is_empty() {
            if let Some(Value::Table(subagents)) = root.get_mut("subagents") {
                subagents.remove("models");
            }
        }
    }
    for spawn_type in super::super::GROK_SPAWN_TYPES {
        match baseline_roles.and_then(|table| table.get(*spawn_type)) {
            Some(value) => {
                let roles = nested_table_mut(root, &["subagents", "roles"]);
                roles.insert((*spawn_type).to_string(), value.clone());
            }
            None => {
                if let Some(Value::Table(subagents)) = root.get_mut("subagents") {
                    if let Some(Value::Table(roles)) = subagents.get_mut("roles") {
                        roles.remove(*spawn_type);
                    }
                }
            }
        }
    }
    if let Some(Value::Table(subagents)) = root.get_mut("subagents") {
        if let Some(Value::Table(roles)) = subagents.get("roles") {
            if roles.is_empty() {
                subagents.remove("roles");
            }
        }
        if subagents.is_empty() {
            root.remove("subagents");
        }
    }
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

fn grok_requirements_files(home: &Path) -> Vec<PathBuf> {
    vec![
        PathBuf::from(GROK_FLEET_REQUIREMENTS),
        home.join("requirements.toml"),
    ]
}

pub(in crate::gateway) fn grok_injected_keys_may_be_hidden(
    home: &Path,
    config_text: Option<&str>,
) -> bool {
    config_text.is_some_and(grok_has_allowed_models)
        || grok_requirements_files(home).iter().any(|path| {
            fs::read_to_string(path)
                .ok()
                .as_deref()
                .is_some_and(grok_has_allowed_models)
        })
}

fn grok_owned_snapshot(
    root: &Table,
) -> (
    BTreeMap<String, Value>,
    BTreeMap<String, Value>,
    BTreeMap<String, Value>,
) {
    (
        grok_owned_section(root, "model_providers"),
        grok_owned_section(root, "model"),
        grok_owned_subagent_slice(root),
    )
}

fn grok_owned_subagent_slice(root: &Table) -> BTreeMap<String, Value> {
    let mut owned = BTreeMap::new();
    if let Some(models) = root
        .get("subagents")
        .and_then(Value::as_table)
        .and_then(|table| table.get("models"))
        .and_then(Value::as_table)
    {
        for spawn_type in super::super::GROK_SPAWN_TYPES {
            if let Some(value) = models.get(*spawn_type) {
                owned.insert(format!("models.{spawn_type}"), value.clone());
            }
        }
    }
    if let Some(roles) = root
        .get("subagents")
        .and_then(Value::as_table)
        .and_then(|table| table.get("roles"))
        .and_then(Value::as_table)
    {
        for spawn_type in super::super::GROK_SPAWN_TYPES {
            if let Some(value) = roles.get(*spawn_type) {
                owned.insert(format!("roles.{spawn_type}"), value.clone());
            }
        }
    }
    owned
}

fn grok_owned_section(root: &Table, key: &str) -> BTreeMap<String, Value> {
    root.get(key)
        .and_then(Value::as_table)
        .map(|table| {
            table
                .iter()
                .filter(|(name, _)| is_codexhub_client_provider_id(name))
                .map(|(name, value)| (name.clone(), value.clone()))
                .collect()
        })
        .unwrap_or_default()
}

pub(in crate::gateway) fn grok_injected_blocks_match(
    written: &str,
    expected: &str,
) -> Result<bool, String> {
    Ok(grok_owned_snapshot(&parse_grok_table(written)?)
        == grok_owned_snapshot(&parse_grok_table(expected)?))
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
    let current_text = fs::read_to_string(config_path).ok();
    let current = current_text.as_deref().map(sanitize_text);
    let next = grok_config_text(current_text.as_deref(), settings, providers, model)?;
    let current_table = current_text
        .as_deref()
        .and_then(|text| parse_grok_table(text).ok());
    let shadows = grok_shadow_agent_plan(
        config_path,
        current_table.as_ref(),
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
        next_redacted: sanitize_text(&super::super::append_planned_file_previews(&next, &shadows)),
        backup_required: config_path.exists(),
        message: "Apply will back up the current Grok CLI config, then surgically add CodexHub Gateway models while preserving your own providers and settings.".to_string(),
    })
}

pub(in crate::gateway) struct GrokApplyPlan {
    pub config_path: PathBuf,
    pub next: String,
    pub skip_snapshot: bool,
    pub shadow_agents: Vec<(PathBuf, Option<String>)>,
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
    let current_table = current
        .as_deref()
        .and_then(|text| parse_grok_table(text).ok());
    Ok(GrokApplyPlan {
        config_path: config_path.to_path_buf(),
        skip_snapshot,
        next,
        shadow_agents: grok_shadow_agent_plan(
            config_path,
            current_table.as_ref(),
            settings,
            providers,
            &model,
        )?,
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
    publish_grok_shadow_agents(&plan.shadow_agents)?;
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
    let agents_dir = grok_agents_dir(config_path);
    let agent_paths = super::super::GROK_SPAWN_TYPES
        .iter()
        .map(|spawn_type| {
            (
                format!("agents/{spawn_type}.md"),
                agents_dir.join(format!("{spawn_type}.md")),
            )
        })
        .collect::<Vec<_>>();
    let mut files: Vec<(&str, &Path)> = vec![("config.toml", config_path)];
    files.extend(
        agent_paths
            .iter()
            .map(|(name, path)| (name.as_str(), path.as_path())),
    );
    ensure_rollback_baseline("grok", backup_roots, &files, |name, text| {
        if name.starts_with("agents/") {
            text.contains(GROK_SHADOW_MARKER)
        } else {
            is_grok_codexhub_config(text)
        }
    })
}

fn restore_grok_agent_files(config_path: &Path, baseline: &RollbackBaseline) -> Result<(), String> {
    let agents_dir = grok_agents_dir(config_path);
    for spawn_type in super::super::GROK_SPAWN_TYPES {
        let key = format!("agents/{spawn_type}.md");
        let path = agents_dir.join(format!("{spawn_type}.md"));
        match baseline.files.get(&key) {
            Some(BaselineFile::Snapshot { content }) => {
                if let Some(parent) = path.parent() {
                    fs::create_dir_all(parent).map_err(|error| {
                        format!("failed to restore Grok agents directory: {error}")
                    })?;
                }
                write_text_replace(&path, content)
                    .map_err(|_| "failed to restore Grok shadow agent".to_string())?;
            }
            Some(BaselineFile::Absent) => {
                if path.exists() {
                    fs::remove_file(&path)
                        .map_err(|error| format!("failed to remove Grok shadow agent: {error}"))?;
                }
            }
            None if grok_shadow_is_ours(&path) => {
                fs::remove_file(&path)
                    .map_err(|error| format!("failed to remove Grok shadow agent: {error}"))?;
            }
            None => {}
        }
    }
    Ok(())
}

pub(in crate::gateway) fn restore_grok_from_baseline(
    config_path: &Path,
    baseline: &RollbackBaseline,
) -> Result<GatewayClientApplyResult, String> {
    let result = match baseline.files.get("config.toml") {
        Some(BaselineFile::Snapshot { content }) => {
            write_text_replace(config_path, content)
                .map_err(|_| "failed to restore Grok config from baseline".to_string())?;
            GatewayClientApplyResult {
                client_id: CLIENT_ID.to_string(),
                applied: true,
                config_path: None,
                backup_path: None,
                message: "Grok CLI official config restored from canonical baseline.".to_string(),
            }
        }
        Some(BaselineFile::Absent) => {
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
            GatewayClientApplyResult {
                client_id: CLIENT_ID.to_string(),
                applied: true,
                config_path: None,
                backup_path: None,
                message:
                    "Grok CLI CodexHub entries removed; original baseline recorded it as absent."
                        .to_string(),
            }
        }
        None => return Err("rollback baseline is incomplete".to_string()),
    };
    restore_grok_agent_files(config_path, baseline)?;
    Ok(result)
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
    clear_owned_grok_shadow_agents(config_path)?;
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
        return restore_grok_from_baseline(config_path, &baseline);
    }
    let _ = adopt_legacy_baseline_locked("grok", backup_roots)?;
    if let Some(baseline) = read_rollback_baseline("grok")? {
        let mut result = restore_grok_from_baseline(config_path, &baseline)?;
        result.message =
            "Grok CLI official config restored from legacy-adopted baseline.".to_string();
        return Ok(result);
    }
    grok_ownership_bounded_cleanup(config_path)
}
