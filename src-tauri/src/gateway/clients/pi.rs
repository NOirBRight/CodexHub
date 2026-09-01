use super::super::{
    gateway_client_provider_groups, read_json_file_or_empty,
    remove_codexhub_client_provider_entries, GatewayClientProviderGroup,
    GatewayClientProviderModel,
};
use crate::{Provider, Settings};
use serde_json::{json, Value};
use std::path::Path;

pub(in crate::gateway) fn pi_settings_text(
    settings_path: &Path,
    _settings: &Settings,
    _providers: &[Provider],
    _model: &str,
) -> Result<String, String> {
    // Provider Injection (ADR-0004 Q1): settings.json is user-owned.
    // Never force defaultProvider/defaultModel and never strip enabledModels.
    let mut value = read_json_file_or_empty(settings_path, "Pi settings")?;
    if !value.is_object() {
        value = json!({});
    }
    serde_json::to_string_pretty(&value)
        .map(|text| format!("{text}\n"))
        .map_err(|error| format!("failed to serialize Pi settings: {error}"))
}

pub(in crate::gateway) fn pi_models_text(
    models_path: &Path,
    settings: &Settings,
    providers: &[Provider],
    model: &str,
) -> Result<String, String> {
    let groups = gateway_client_provider_groups(settings, providers, model)?;
    let mut value = read_json_file_or_empty(models_path, "Pi models")?;
    if !value.is_object() {
        value = json!({});
    }
    let object = value
        .as_object_mut()
        .ok_or_else(|| "Pi models root must be a JSON object".to_string())?;
    let provider_root = object
        .entry("providers".to_string())
        .or_insert_with(|| json!({}));
    if !provider_root.is_object() {
        *provider_root = json!({});
    }
    let providers_object = provider_root
        .as_object_mut()
        .ok_or_else(|| "Pi providers root must be a JSON object".to_string())?;
    remove_codexhub_client_provider_entries(providers_object);
    for group in &groups.providers {
        providers_object.insert(
            group.client_provider_id.clone(),
            codexhub_pi_provider_value(settings, group),
        );
    }
    serde_json::to_string_pretty(&value)
        .map(|text| format!("{text}\n"))
        .map_err(|error| format!("failed to serialize Pi models: {error}"))
}

fn codexhub_pi_provider_value(settings: &Settings, group: &GatewayClientProviderGroup) -> Value {
    let models = group
        .models
        .iter()
        .map(codexhub_pi_model_value)
        .collect::<Vec<_>>();
    json!({
        "baseUrl": group.base_url.clone(),
        "api": group.endpoint_selection.pi_api(),
        "apiKey": settings.gateway_client_key,
        "authHeader": true,
        "compat": {
            "supportsDeveloperRole": group.supports_developer_role,
            "supportsReasoningEffort": true,
            "supportsUsageInStreaming": true,
        },
        "models": models,
    })
}

fn codexhub_pi_model_value(model: &GatewayClientProviderModel) -> Value {
    let mut value = json!({
        "id": model.id.clone(),
        "name": model.display_name.clone(),
        "reasoning": !model.supported_reasoning_levels.is_empty(),
        "input": model.input_modalities.clone(),
        "headers": {
            "x-codex-client-id": "pi",
        },
        "maxTokens": 32768,
        "cost": {
            "input": 0,
            "output": 0,
            "cacheRead": 0,
            "cacheWrite": 0,
        },
    });
    if let (Some(object), Some(context_window)) = (value.as_object_mut(), model.context_window) {
        object.insert("contextWindow".to_string(), json!(context_window));
    }
    value
}

use super::super::{
    adopt_legacy_baseline_locked, combined_current_preview, combined_named_text,
    create_snapshot_backup, ensure_rollback_baseline,
    first_provider_base_url_from_json_object_text, is_codexhub_client_model_selector,
    is_managed_codexhub_provider_entry, is_recognized_codexhub_client_provider_id,
    managed_provider_base_url_from_json_object_text, read_rollback_baseline,
    resolve_gateway_client_model_id, route_owner_from_endpoint, sanitize_text, write_text_replace,
    BackupChannel, BaselineFile, GatewayClientApplyResult, GatewayClientConfigPreview,
    LegacySnapshotCandidate, RollbackBaseline,
};
use crate::app_flavor::RoutingOwner;
use std::collections::HashMap;
use std::fs;
use std::path::PathBuf;

#[derive(Debug, Clone)]
pub(in crate::gateway) struct PiConfigPaths {
    pub(in crate::gateway) settings_path: PathBuf,
    pub(in crate::gateway) models_path: PathBuf,
}

pub(in crate::gateway) fn detect_pi_config_paths() -> PiConfigPaths {
    let agent_dir = if let Some(path) = std::env::var_os("CODEXHUB_PI_AGENT_DIR")
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
    {
        path
    } else if let Some(path) = std::env::var_os("CODEXHUB_PI_CONFIG")
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
    {
        return PiConfigPaths {
            models_path: path
                .parent()
                .map(|parent| parent.join("models.json"))
                .unwrap_or_else(|| PathBuf::from("models.json")),
            settings_path: path,
        };
    } else {
        dirs::home_dir()
            .map(|home| home.join(".pi").join("agent"))
            .unwrap_or_else(|| PathBuf::from("~/.pi/agent"))
    };
    PiConfigPaths {
        settings_path: agent_dir.join("settings.json"),
        models_path: agent_dir.join("models.json"),
    }
}

pub(in crate::gateway) fn detect_pi_route_details(
    paths: &PiConfigPaths,
    current_owner: RoutingOwner,
    current_port: u16,
) -> (RoutingOwner, Option<String>) {
    let settings_text = fs::read_to_string(&paths.settings_path).ok();
    let models_text = fs::read_to_string(&paths.models_path).ok();
    let managed = pi_route_mode(paths) == "hub";
    let route_endpoint = models_text.as_deref().and_then(|text| {
        managed_provider_base_url_from_json_object_text(text, "/providers")
            .unwrap_or_else(|| first_provider_base_url_from_json_object_text(text, "/providers"))
    });
    let existing_config = settings_text.is_some() || models_text.is_some();
    (
        route_owner_from_endpoint(
            route_endpoint.as_deref(),
            managed,
            existing_config,
            current_owner,
            current_port,
        ),
        route_endpoint,
    )
}

pub(in crate::gateway) fn pi_route_mode(paths: &PiConfigPaths) -> &'static str {
    let settings = fs::read_to_string(&paths.settings_path).ok();
    let models = fs::read_to_string(&paths.models_path).ok();
    match (settings.as_deref(), models.as_deref()) {
        (Some(settings), Some(models)) => {
            if is_pi_codexhub_config(settings, models) {
                "hub"
            } else {
                "official"
            }
        }
        (Some(settings), None) => {
            if is_pi_settings_codexhub_config(settings) {
                "hub"
            } else {
                "official"
            }
        }
        (None, Some(models)) => {
            if is_pi_models_codexhub_config(models) {
                "hub"
            } else {
                "official"
            }
        }
        (None, None) => "unknown",
    }
}

pub(in crate::gateway) fn is_valid_pi_settings_legacy_shape(text: &str) -> bool {
    let Ok(value) = serde_json::from_str::<Value>(text) else {
        return false;
    };
    let Some(object) = value.as_object() else {
        return false;
    };
    object.get("defaultProvider").is_none_or(Value::is_string)
        && object.get("defaultModel").is_none_or(Value::is_string)
        && object.get("enabledModels").is_none_or(Value::is_array)
        && object
            .get("enabledModels")
            .and_then(Value::as_array)
            .is_none_or(|models| models.iter().all(Value::is_string))
        && (object.contains_key("defaultProvider")
            || object.contains_key("defaultModel")
            || object.contains_key("enabledModels"))
}

pub(in crate::gateway) fn is_valid_pi_models_legacy_shape(text: &str) -> bool {
    let Ok(value) = serde_json::from_str::<Value>(text) else {
        return false;
    };
    let Some(object) = value.as_object() else {
        return false;
    };
    object.get("providers").is_some_and(Value::is_object)
        && object
            .get("providers")
            .and_then(Value::as_object)
            .is_some_and(|providers| providers.values().all(Value::is_object))
}

pub(in crate::gateway) fn adopt_legacy_pi_snapshot_files(
    backup_root: &Path,
    channel: BackupChannel,
) -> Result<Vec<LegacySnapshotCandidate>, String> {
    let entries = match fs::read_dir(backup_root) {
        Ok(entries) => entries,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(_) => return Err("failed to read legacy Pi backup directory".to_string()),
    };
    let mut candidates = Vec::new();
    for entry in entries {
        let entry = entry.map_err(|_| "failed to read legacy Pi backup entry".to_string())?;
        let path = entry.path();
        if !path.is_dir() {
            continue;
        }
        let settings_path = path.join("settings.json");
        let models_path = path.join("models.json");
        if !settings_path.exists() || !models_path.exists() {
            return Err("legacy Pi snapshot is incomplete".to_string());
        }
        let settings = fs::read_to_string(&settings_path)
            .map_err(|_| "failed to read legacy Pi settings backup".to_string())?;
        let models = fs::read_to_string(&models_path)
            .map_err(|_| "failed to read legacy Pi models backup".to_string())?;
        if !is_valid_pi_settings_legacy_shape(&settings) {
            return Err("legacy Pi settings backup has unexpected shape".to_string());
        }
        if !is_valid_pi_models_legacy_shape(&models) {
            return Err("legacy Pi models backup has unexpected shape".to_string());
        }
        if is_pi_codexhub_config(&settings, &models) {
            continue;
        }
        let settings_modified = fs::metadata(&settings_path)
            .and_then(|metadata| metadata.modified())
            .map_err(|_| "failed to read legacy Pi settings metadata".to_string())?;
        let models_modified = fs::metadata(&models_path)
            .and_then(|metadata| metadata.modified())
            .map_err(|_| "failed to read legacy Pi models metadata".to_string())?;
        let modified = std::cmp::max(settings_modified, models_modified);
        let name = path
            .file_name()
            .and_then(|name| name.to_str())
            .unwrap_or("")
            .to_string();
        let mut files = HashMap::new();
        files.insert(
            "settings.json".to_string(),
            BaselineFile::Snapshot { content: settings },
        );
        files.insert(
            "models.json".to_string(),
            BaselineFile::Snapshot { content: models },
        );
        candidates.push(LegacySnapshotCandidate {
            modified,
            channel,
            name,
            files,
        });
    }
    Ok(candidates)
}

pub(in crate::gateway) fn record_pi_rollback_baseline(
    settings_path: &Path,
    models_path: &Path,
    backup_roots: &[(PathBuf, BackupChannel)],
) -> Result<(), String> {
    ensure_rollback_baseline(
        "pi",
        backup_roots,
        &[
            ("settings.json", settings_path),
            ("models.json", models_path),
        ],
        |name, text| match name {
            "settings.json" => is_pi_settings_codexhub_config(text),
            "models.json" => is_pi_models_codexhub_config(text),
            _ => false,
        },
    )
}

pub(in crate::gateway) fn restore_pi_from_baseline(
    settings_path: &Path,
    models_path: &Path,
    baseline: &RollbackBaseline,
) -> Result<GatewayClientApplyResult, String> {
    let targets = [
        ("settings.json", settings_path),
        ("models.json", models_path),
    ];
    for (name, _) in targets {
        if !baseline.files.contains_key(name) {
            return Err("rollback baseline is incomplete".to_string());
        }
    }
    let mut restored_any = false;
    let mut removed_any = false;
    for (name, path) in targets {
        match baseline.files.get(name) {
            Some(BaselineFile::Snapshot { content }) => {
                if let Some(parent) = path.parent() {
                    fs::create_dir_all(parent)
                        .map_err(|_| "failed to create directory for Pi restore".to_string())?;
                }
                write_text_replace(path, content)
                    .map_err(|_| "failed to restore Pi config from baseline".to_string())?;
                restored_any = true;
            }
            Some(BaselineFile::Absent) if path.exists() => {
                let text = fs::read_to_string(path).unwrap_or_default();
                let managed = match name {
                    "settings.json" => is_pi_settings_codexhub_config(&text),
                    "models.json" => is_pi_models_codexhub_config(&text),
                    _ => false,
                };
                if !managed {
                    return Err(
                        "Pi target exists but is not managed by CodexHub; refusing removal."
                            .to_string(),
                    );
                }
                fs::remove_file(path)
                    .map_err(|_| "failed to remove restored-absent Pi target".to_string())?;
                removed_any = true;
            }
            Some(BaselineFile::Absent) | None => {}
        }
    }
    Ok(GatewayClientApplyResult {
        client_id: "pi".to_string(),
        applied: true,
        config_path: None,
        backup_path: None,
        message: match (restored_any, removed_any) {
            (true, true) => {
                "Pi official config restored from canonical baseline; absent targets removed."
                    .to_string()
            }
            (false, true) => {
                "Pi config removed; original baseline recorded targets as absent.".to_string()
            }
            _ => "Pi official config restored from canonical baseline.".to_string(),
        },
    })
}

pub(in crate::gateway) fn pi_ownership_bounded_cleanup(
    settings_path: &Path,
    models_path: &Path,
) -> Result<GatewayClientApplyResult, String> {
    // settings.json is user-owned under Provider Injection; detach never
    // reads or mutates it. The Injected Block lives only in models.json.
    let _ = settings_path;
    let models_exists = models_path.exists();
    if !models_exists {
        return Ok(GatewayClientApplyResult {
            client_id: "pi".to_string(),
            applied: true,
            config_path: None,
            backup_path: None,
            message: "Pi config was already absent.".to_string(),
        });
    }

    let models_text = fs::read_to_string(models_path)
        .map_err(|_| "failed to read Pi models for cleanup.".to_string())?;
    let mut models_value: Value = serde_json::from_str(&models_text)
        .map_err(|_| "Pi models is not valid JSON; refusing cleanup.".to_string())?;

    let models_object = models_value
        .as_object()
        .ok_or_else(|| "Pi models root must be a JSON object; refusing cleanup.".to_string())?;

    if models_object
        .get("providers")
        .is_some_and(|value| !value.is_object() && !value.is_null())
    {
        return Err("Pi providers has an unexpected shape; refusing cleanup.".to_string());
    }
    if models_object
        .get("providers")
        .and_then(Value::as_object)
        .is_some_and(|providers| providers.values().any(|value| !value.is_object()))
    {
        return Err("Pi providers map contains malformed entries; refusing cleanup.".to_string());
    }

    let providers_object = models_object.get("providers").and_then(Value::as_object);
    let providers_has_managed = providers_object.is_some_and(|providers| {
        providers
            .iter()
            .any(|(key, value)| is_managed_codexhub_provider_entry(key, value))
    });

    if !providers_has_managed {
        return Ok(GatewayClientApplyResult {
            client_id: "pi".to_string(),
            applied: true,
            config_path: None,
            backup_path: None,
            message: "Pi CodexHub config was already absent.".to_string(),
        });
    }

    let models_object = models_value.as_object_mut().unwrap();
    if let Some(providers) = models_object
        .get_mut("providers")
        .and_then(Value::as_object_mut)
    {
        remove_codexhub_client_provider_entries(providers);
        if providers.is_empty() {
            models_object.remove("providers");
        }
    }

    let mut mutated = false;
    if models_object.is_empty() {
        if models_exists {
            fs::remove_file(models_path)
                .map_err(|_| "failed to remove cleaned Pi models.".to_string())?;
            mutated = true;
        }
    } else {
        let next = serde_json::to_string_pretty(&models_value)
            .map(|text| format!("{text}\n"))
            .map_err(|error| format!("failed to serialize cleaned Pi models: {error}"))?;
        write_text_replace(models_path, &next)
            .map_err(|_| "failed to write cleaned Pi models".to_string())?;
        mutated = true;
    }

    Ok(GatewayClientApplyResult {
        client_id: "pi".to_string(),
        applied: true,
        config_path: None,
        backup_path: None,
        message: if mutated {
            "Pi CodexHub entries removed while preserving unrelated config.".to_string()
        } else {
            "Pi CodexHub config was already absent.".to_string()
        },
    })
}

pub(in crate::gateway) fn preview_pi_config_with_paths(
    settings_path: &Path,
    models_path: &Path,
    settings: &Settings,
    providers: &[Provider],
    model: &str,
) -> Result<GatewayClientConfigPreview, String> {
    let current = combined_current_preview(&[
        ("settings.json", settings_path),
        ("models.json", models_path),
    ]);
    let next_settings = pi_settings_text(settings_path, settings, providers, model)?;
    let next_models = pi_models_text(models_path, settings, providers, model)?;
    Ok(GatewayClientConfigPreview {
        client_id: "pi".to_string(),
        can_apply: true,
        strategy: "managed_native_config".to_string(),
        config_path: Some(settings_path.to_path_buf()),
        current_redacted: current.map(|text| sanitize_text(&text)),
        next_redacted: sanitize_text(&combined_named_text(&[
            ("settings.json", &next_settings),
            ("models.json", &next_models),
        ])),
        backup_required: settings_path.exists() || models_path.exists(),
        message: "Apply will inject the CodexHub provider into Pi models.json without changing model selection."
            .to_string(),
    })
}

pub(in crate::gateway) struct PiApplyPlan {
    pub settings_path: PathBuf,
    pub models_path: PathBuf,
    pub next_models: String,
    pub skip_snapshot: bool,
}

/// Pure next-text plan. Does not create backups or write the target files.
pub(in crate::gateway) fn plan_pi_apply(
    settings_path: &Path,
    models_path: &Path,
    settings: &Settings,
    providers: &[Provider],
    model: &str,
) -> Result<PiApplyPlan, String> {
    let model = resolve_gateway_client_model_id(settings, providers, model)?;
    let current_settings = fs::read_to_string(settings_path).unwrap_or_default();
    let current_models = fs::read_to_string(models_path).unwrap_or_default();
    let next_models = pi_models_text(models_path, settings, providers, &model)?;
    Ok(PiApplyPlan {
        settings_path: settings_path.to_path_buf(),
        models_path: models_path.to_path_buf(),
        skip_snapshot: is_pi_codexhub_config(&current_settings, &current_models),
        next_models,
    })
}

#[cfg(test)]
pub(in crate::gateway) fn apply_pi_config_with_paths(
    settings_path: &Path,
    models_path: &Path,
    backup_roots: &[(PathBuf, BackupChannel)],
    settings: &Settings,
    providers: &[Provider],
    model: &str,
) -> Result<GatewayClientApplyResult, String> {
    publish_pi_apply(
        &plan_pi_apply(settings_path, models_path, settings, providers, model)?,
        backup_roots,
    )
}

pub(in crate::gateway) fn publish_pi_apply(
    plan: &PiApplyPlan,
    backup_roots: &[(PathBuf, BackupChannel)],
) -> Result<GatewayClientApplyResult, String> {
    let (backup_root, _) = backup_roots
        .first()
        .ok_or_else(|| "Pi apply requires at least one backup root".to_string())?;
    let backup_path = create_snapshot_backup(
        "pi",
        backup_root,
        &[
            ("settings.json", &plan.settings_path),
            ("models.json", &plan.models_path),
        ],
        plan.skip_snapshot,
    )
    .map_err(|_| "failed to create Pi backup snapshot".to_string())?;
    record_pi_rollback_baseline(&plan.settings_path, &plan.models_path, backup_roots)?;
    write_text_replace(&plan.models_path, &plan.next_models)
        .map_err(|_| "failed to write managed Pi models".to_string())?;
    Ok(GatewayClientApplyResult {
        client_id: "pi".to_string(),
        applied: true,
        config_path: Some(plan.models_path.clone()),
        backup_path,
        message: "Pi now has the CodexHub provider available. Model selection is unchanged."
            .to_string(),
    })
}

pub(in crate::gateway) fn restore_pi_config_with_paths(
    settings_path: &Path,
    models_path: &Path,
    backup_roots: &[(PathBuf, BackupChannel)],
) -> Result<GatewayClientApplyResult, String> {
    if let Some(baseline) = read_rollback_baseline("pi")? {
        return restore_pi_from_baseline(settings_path, models_path, &baseline);
    }
    let _ = adopt_legacy_baseline_locked("pi", backup_roots)?;
    if let Some(baseline) = read_rollback_baseline("pi")? {
        let mut result = restore_pi_from_baseline(settings_path, models_path, &baseline)?;
        result.message = "Pi official config restored from legacy-adopted baseline.".to_string();
        return Ok(result);
    }
    pi_ownership_bounded_cleanup(settings_path, models_path)
}

pub(in crate::gateway) fn is_pi_codexhub_config(settings_text: &str, models_text: &str) -> bool {
    is_pi_settings_codexhub_config(settings_text) || is_pi_models_codexhub_config(models_text)
}

pub(in crate::gateway) fn is_pi_settings_codexhub_config(text: &str) -> bool {
    let Ok(value) = serde_json::from_str::<Value>(text) else {
        return text.contains("\"codexhub\"") || text.contains("\"codexhub-");
    };
    value
        .get("defaultProvider")
        .and_then(Value::as_str)
        .is_some_and(is_recognized_codexhub_client_provider_id)
        || value
            .get("enabledModels")
            .and_then(Value::as_array)
            .is_some_and(|models| {
                models
                    .iter()
                    .filter_map(Value::as_str)
                    .any(is_codexhub_client_model_selector)
            })
}

pub(in crate::gateway) fn is_pi_models_codexhub_config(text: &str) -> bool {
    let Ok(value) = serde_json::from_str::<Value>(text) else {
        return text.contains("\"codexhub\"") || text.contains("\"codexhub-");
    };
    value
        .get("providers")
        .and_then(Value::as_object)
        .is_some_and(|providers| {
            providers
                .iter()
                .any(|(key, value)| is_managed_codexhub_provider_entry(key, value))
        })
}
