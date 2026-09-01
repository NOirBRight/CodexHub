use super::super::gateway_client_provider_groups;
use crate::{Provider, Settings};
use serde_json::{json, Map, Value};

pub(in crate::gateway) fn opencode_reasoning_variants(
    supported_reasoning_levels: &[String],
) -> Map<String, serde_json::Value> {
    supported_reasoning_levels
        .iter()
        .map(|level| (level.clone(), json!({ "reasoningEffort": level.clone() })))
        .collect::<Map<_, _>>()
}

pub(in crate::gateway) fn opencode_config_text(
    current: Option<&str>,
    settings: &Settings,
    providers: &[Provider],
    model: &str,
) -> Result<String, String> {
    // Provider Injection (ADR-0004 / #435): surgical merge. Preserve every
    // user-owned provider and setting in the existing opencode.json; only
    // the CodexHub provider entry is inserted/updated. model/small_model
    // stay user-owned (never forced).
    let groups = gateway_client_provider_groups(settings, providers, model)?;
    let mut provider_map = Map::new();
    for group in &groups.providers {
        let mut models = Map::new();
        for gateway_model in &group.models {
            let mut entry = json!({
                "name": gateway_model.display_name,
                "modalities": {
                    "input": gateway_model.input_modalities,
                },
            });
            if !gateway_model.supported_reasoning_levels.is_empty() {
                let default_effort = gateway_model
                    .default_reasoning_level
                    .as_ref()
                    .filter(|default| {
                        gateway_model
                            .supported_reasoning_levels
                            .iter()
                            .any(|level| level == *default)
                    })
                    .cloned()
                    .unwrap_or_else(|| gateway_model.supported_reasoning_levels[0].clone());
                let variants =
                    opencode_reasoning_variants(&gateway_model.supported_reasoning_levels);
                if let Some(object) = entry.as_object_mut() {
                    object.insert(
                        "options".to_string(),
                        json!({ "reasoningEffort": default_effort }),
                    );
                    object.insert("variants".to_string(), Value::Object(variants));
                }
            }
            models.insert(gateway_model.id.clone(), entry);
        }
        provider_map.insert(
            group.client_provider_id.clone(),
            json!({
                "name": group.display_name.clone(),
                "npm": group.endpoint_selection.opencode_npm(),
                "options": {
                    "baseURL": group.base_url.clone(),
                    "apiKey": settings.gateway_client_key,
                },
                "models": Value::Object(models),
            }),
        );
    }

    let mut base: Value = match current {
        Some(text) if !text.trim().is_empty() => {
            serde_json::from_str(text).unwrap_or(Value::Object(Map::new()))
        }
        _ => Value::Object(Map::new()),
    };
    if !base.is_object() {
        base = Value::Object(Map::new());
    }
    let object = base.as_object_mut().ok_or_else(|| "OpenCode config root must be a JSON object".to_string())?;
    // Preserve user-owned providers; drop stale codexhub entries.
    if let Some(providers_object) = object.get_mut("provider").and_then(Value::as_object_mut) {
        remove_codexhub_client_provider_entries(providers_object);
        for (key, value) in provider_map {
            providers_object.insert(key, value);
        }
    } else {
        object.insert("provider".to_string(), Value::Object(provider_map));
    }
    serde_json::to_string_pretty(&base)
        .map(|text| format!("{text}
"))
        .map_err(|error| format!("failed to serialize OpenCode config: {error}"))
}

use super::super::{
    adopt_legacy_baseline_locked, ensure_rollback_baseline, executable_version,
    is_codexhub_client_model_selector, is_managed_codexhub_provider_entry, read_rollback_baseline,
    remove_codexhub_client_provider_entries, resolve_gateway_client_model_id, sanitize_text,
    timestamp_millis, write_text_replace, BackupChannel, BaselineFile, GatewayClientApplyResult,
    GatewayClientConfigPreview, LegacySnapshotCandidate,
};
use std::collections::{HashMap, HashSet};
use std::fs;
use std::path::{Path, PathBuf};

pub(in crate::gateway) fn detect_opencode_config_path() -> Option<PathBuf> {
    if let Some(path) = std::env::var_os("CODEXHUB_OPENCODE_CONFIG")
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
    {
        return Some(path);
    }
    let mut candidates = Vec::new();
    if let Some(config_dir) = std::env::var_os("XDG_CONFIG_HOME").map(PathBuf::from) {
        candidates.push(config_dir.join("opencode").join("opencode.json"));
        candidates.push(config_dir.join("opencode").join("opencode.jsonc"));
    }
    if let Some(home) = dirs::home_dir() {
        candidates.push(home.join(".config").join("opencode").join("opencode.json"));
        candidates.push(home.join(".config").join("opencode").join("opencode.jsonc"));
        candidates.push(home.join(".config").join("opencode").join("config.json"));
    }
    if let Some(appdata) = std::env::var_os("APPDATA").map(PathBuf::from) {
        candidates.push(appdata.join("opencode").join("opencode.json"));
        candidates.push(appdata.join("opencode").join("opencode.jsonc"));
        candidates.push(appdata.join("opencode").join("config.json"));
    }
    candidates
        .into_iter()
        .find(|path| path.exists())
        .or_else(|| {
            dirs::home_dir().map(|home| home.join(".config").join("opencode").join("opencode.json"))
        })
}

pub(in crate::gateway) fn detect_opencode_executable_path() -> Option<PathBuf> {
    which::which("opencode")
        .ok()
        .or_else(|| {
            dirs::home_dir().and_then(|home| detect_opencode_executable_path_in_home(&home))
        })
        .or_else(|| {
            opencode_system_executable_candidates()
                .into_iter()
                .find(|path| path.is_file())
        })
}

pub(in crate::gateway) fn detect_opencode_executable_path_in_home(home: &Path) -> Option<PathBuf> {
    let executable = if cfg!(windows) {
        "opencode.exe"
    } else {
        "opencode"
    };
    [
        home.join(".opencode").join("bin").join(executable),
        home.join(".local").join("bin").join(executable),
    ]
    .into_iter()
    .find(|path| path.is_file())
}

pub(in crate::gateway) fn opencode_system_executable_candidates() -> Vec<PathBuf> {
    if cfg!(target_os = "linux") {
        vec![
            PathBuf::from("/opt/OpenCode/ai.opencode.desktop"),
            PathBuf::from("/opt/opencode/opencode"),
        ]
    } else {
        Vec::new()
    }
}

pub(in crate::gateway) fn detect_opencode_version() -> Option<String> {
    which::which("opencode")
        .ok()
        .or_else(|| {
            dirs::home_dir().and_then(|home| detect_opencode_executable_path_in_home(&home))
        })
        .as_deref()
        .and_then(executable_version)
}

pub(in crate::gateway) fn is_valid_opencode_legacy_shape(text: &str) -> bool {
    let Ok(value) = serde_json::from_str::<Value>(text) else {
        return false;
    };
    let Some(object) = value.as_object() else {
        return false;
    };
    object.get("model").is_none_or(Value::is_string)
        && object.get("small_model").is_none_or(Value::is_string)
        && object.get("provider").is_none_or(Value::is_object)
        && object
            .get("provider")
            .and_then(Value::as_object)
            .is_none_or(|providers| providers.values().all(Value::is_object))
        && (object.contains_key("model")
            || object.contains_key("small_model")
            || object.contains_key("provider"))
}

pub(in crate::gateway) fn adopt_legacy_opencode_snapshot_files(
    backup_root: &Path,
    channel: BackupChannel,
) -> Result<Vec<LegacySnapshotCandidate>, String> {
    let entries = match fs::read_dir(backup_root) {
        Ok(entries) => entries,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(Vec::new()),
        Err(_) => return Err("failed to read legacy OpenCode backup directory".to_string()),
    };
    let mut candidates = Vec::new();
    for entry in entries {
        let entry = entry.map_err(|_| "failed to read legacy OpenCode backup entry".to_string())?;
        let path = entry.path();
        if !path.is_file() {
            continue;
        }
        let text = fs::read_to_string(&path)
            .map_err(|_| "failed to read legacy OpenCode backup".to_string())?;
        if !is_valid_opencode_legacy_shape(&text) {
            return Err("legacy OpenCode backup has unexpected shape".to_string());
        }
        if is_opencode_codexhub_config(&text) {
            continue;
        }
        let modified = entry
            .metadata()
            .and_then(|metadata| metadata.modified())
            .map_err(|_| "failed to read legacy OpenCode backup metadata".to_string())?;
        let name = path
            .file_name()
            .and_then(|name| name.to_str())
            .unwrap_or("")
            .to_string();
        let mut files = HashMap::new();
        files.insert(
            "opencode.json".to_string(),
            BaselineFile::Snapshot { content: text },
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

pub(in crate::gateway) fn record_opencode_rollback_baseline(
    config_path: &Path,
    backup_roots: &[(PathBuf, BackupChannel)],
) -> Result<(), String> {
    ensure_rollback_baseline(
        "opencode",
        backup_roots,
        &[("opencode.json", config_path)],
        |_, text| is_opencode_codexhub_config(text),
    )
}

pub(in crate::gateway) fn restore_opencode_from_baseline(
    config_path: &Path,
    file: &BaselineFile,
) -> Result<GatewayClientApplyResult, String> {
    match file {
        BaselineFile::Snapshot { content } => {
            write_text_replace(config_path, content)
                .map_err(|_| "failed to restore OpenCode config from baseline".to_string())?;
            Ok(GatewayClientApplyResult {
                client_id: "opencode".to_string(),
                applied: true,
                config_path: None,
                backup_path: None,
                message: "OpenCode official config restored from canonical baseline.".to_string(),
            })
        }
        BaselineFile::Absent => {
            if config_path.exists() {
                let text = fs::read_to_string(config_path).unwrap_or_default();
                if !is_opencode_codexhub_config(&text) {
                    return Err(
                        "OpenCode config exists but is not managed by CodexHub; refusing removal."
                            .to_string(),
                    );
                }
                fs::remove_file(config_path)
                    .map_err(|_| "failed to remove restored-absent OpenCode config".to_string())?;
            }
            Ok(GatewayClientApplyResult {
                client_id: "opencode".to_string(),
                applied: true,
                config_path: None,
                backup_path: None,
                message: "OpenCode config removed; original baseline recorded it as absent."
                    .to_string(),
            })
        }
    }
}

pub(in crate::gateway) fn opencode_ownership_bounded_cleanup(
    config_path: &Path,
) -> Result<GatewayClientApplyResult, String> {
    if !config_path.exists() {
        return Ok(GatewayClientApplyResult {
            client_id: "opencode".to_string(),
            applied: true,
            config_path: None,
            backup_path: None,
            message: "OpenCode config was already absent.".to_string(),
        });
    }
    let text = fs::read_to_string(config_path)
        .map_err(|_| "failed to read OpenCode config for cleanup.".to_string())?;
    if !is_opencode_codexhub_config(&text) {
        return Err("OpenCode config is not managed by CodexHub; refusing cleanup.".to_string());
    }
    let mut value = serde_json::from_str::<Value>(&text)
        .map_err(|_| "OpenCode config is not valid JSON; refusing cleanup.".to_string())?;
    let object = value.as_object_mut().ok_or_else(|| {
        "OpenCode config root must be a JSON object; refusing cleanup.".to_string()
    })?;

    // Validate exact shapes before any mutation.
    if object.get("model").is_some_and(|value| !value.is_string()) {
        return Err("OpenCode model has an unexpected shape; refusing cleanup.".to_string());
    }
    if object
        .get("small_model")
        .is_some_and(|value| !value.is_string())
    {
        return Err("OpenCode small_model has an unexpected shape; refusing cleanup.".to_string());
    }
    if object
        .get("provider")
        .is_some_and(|value| !value.is_object())
    {
        return Err("OpenCode provider has an unexpected shape; refusing cleanup.".to_string());
    }
    if object
        .get("provider")
        .and_then(Value::as_object)
        .is_some_and(|providers| providers.values().any(|value| !value.is_object()))
    {
        return Err(
            "OpenCode provider map contains malformed entries; refusing cleanup.".to_string(),
        );
    }

    let model_managed = object.get("model").is_none_or(|value| {
        value
            .as_str()
            .is_some_and(is_codexhub_client_model_selector)
    });
    let small_model_managed = object.get("small_model").is_none_or(|value| {
        value
            .as_str()
            .is_some_and(is_codexhub_client_model_selector)
    });
    let providers_managed = object.get("provider").is_none_or(|value| {
        value.as_object().is_some_and(|providers| {
            providers.is_empty()
                || providers
                    .iter()
                    .all(|(key, value)| is_managed_codexhub_provider_entry(key, value))
        })
    });

    let allowed_keys: HashSet<&str> = [
        "$schema",
        "model",
        "small_model",
        "provider",
        "codexhub_managed",
    ]
    .iter()
    .copied()
    .collect();
    let only_allowed_keys = object.keys().all(|key| allowed_keys.contains(key.as_str()));
    let all_present_managed = model_managed && small_model_managed && providers_managed;

    if only_allowed_keys && all_present_managed {
        fs::remove_file(config_path)
            .map_err(|_| "failed to remove CodexHub-owned OpenCode config.".to_string())?;
        return Ok(GatewayClientApplyResult {
            client_id: "opencode".to_string(),
            applied: true,
            config_path: None,
            backup_path: None,
            message: "OpenCode CodexHub config removed.".to_string(),
        });
    }

    object.remove("codexhub_managed");
    if model_managed {
        object.remove("model");
    }
    if small_model_managed {
        object.remove("small_model");
    }
    if let Some(providers) = object.get_mut("provider").and_then(Value::as_object_mut) {
        remove_codexhub_client_provider_entries(providers);
        if providers.is_empty() {
            object.remove("provider");
        }
    }

    if object.is_empty() {
        fs::remove_file(config_path)
            .map_err(|_| "failed to remove cleaned OpenCode config.".to_string())?;
        Ok(GatewayClientApplyResult {
            client_id: "opencode".to_string(),
            applied: true,
            config_path: None,
            backup_path: None,
            message: "OpenCode CodexHub config removed.".to_string(),
        })
    } else {
        let next = serde_json::to_string_pretty(&value)
            .map(|text| format!("{text}\n"))
            .map_err(|error| format!("failed to serialize cleaned OpenCode config: {error}"))?;
        write_text_replace(config_path, &next)
            .map_err(|_| "failed to write cleaned OpenCode config".to_string())?;
        Ok(GatewayClientApplyResult {
            client_id: "opencode".to_string(),
            applied: true,
            config_path: None,
            backup_path: None,
            message: "OpenCode CodexHub entries removed while preserving unrelated config."
                .to_string(),
        })
    }
}

pub(in crate::gateway) fn preview_opencode_config_with_path(
    config_path: &Path,
    settings: &Settings,
    providers: &[Provider],
    model: &str,
) -> Result<GatewayClientConfigPreview, String> {
    let current = fs::read_to_string(config_path)
        .ok()
        .map(|text| sanitize_text(&text));
    let next = opencode_config_text(current.as_deref(), settings, providers, model)?;
    Ok(GatewayClientConfigPreview {
        client_id: "opencode".to_string(),
        can_apply: config_path.exists(),
        strategy: "provider_injection".to_string(),
        config_path: Some(config_path.to_path_buf()),
        current_redacted: current,
        next_redacted: sanitize_text(&next),
        backup_required: true,
        message: if config_path.exists() {
            "Apply will back up the current OpenCode config, then surgically add the CodexHub provider while preserving your own providers and settings.".to_string()
        } else {
            "OpenCode config does not exist yet; auto-apply is disabled until there is an official config to back up.".to_string()
        },
    })
}

pub(in crate::gateway) struct OpenCodeApplyPlan {
    pub config_path: PathBuf,
    pub next: String,
    pub skip_snapshot: bool,
}

pub(in crate::gateway) enum OpenCodeApplyDecision {
    NotApplied(GatewayClientApplyResult),
    Apply(OpenCodeApplyPlan),
}

/// Pure next-text plan. Does not create backups or write the target file.
pub(in crate::gateway) fn plan_opencode_apply(
    config_path: &Path,
    settings: &Settings,
    providers: &[Provider],
    model: &str,
) -> Result<OpenCodeApplyDecision, String> {
    let model = resolve_gateway_client_model_id(settings, providers, model)?;
    if !config_path.exists() {
        return Ok(OpenCodeApplyDecision::NotApplied(GatewayClientApplyResult {
            client_id: "opencode".to_string(),
            applied: false,
            config_path: Some(config_path.to_path_buf()),
            backup_path: None,
            message: "OpenCode config was not found; refusing managed overwrite without an official config backup.".to_string(),
        }));
    }
    let current = fs::read_to_string(config_path)
        .map_err(|error| format!("failed to read OpenCode config: {error}"))?;
    let next = opencode_config_text(Some(&current), settings, providers, &model)?;
    Ok(OpenCodeApplyDecision::Apply(OpenCodeApplyPlan {
        config_path: config_path.to_path_buf(),
        skip_snapshot: is_opencode_codexhub_config(&current),
        next,
    }))
}

#[cfg(test)]
pub(in crate::gateway) fn apply_opencode_config_with_paths(
    config_path: &Path,
    backup_roots: &[(PathBuf, BackupChannel)],
    settings: &Settings,
    providers: &[Provider],
    model: &str,
) -> Result<GatewayClientApplyResult, String> {
    match plan_opencode_apply(config_path, settings, providers, model)? {
        OpenCodeApplyDecision::NotApplied(result) => Ok(result),
        OpenCodeApplyDecision::Apply(plan) => publish_opencode_apply(&plan, backup_roots),
    }
}

pub(in crate::gateway) fn publish_opencode_apply(
    plan: &OpenCodeApplyPlan,
    backup_roots: &[(PathBuf, BackupChannel)],
) -> Result<GatewayClientApplyResult, String> {
    let (backup_root, _) = backup_roots
        .first()
        .ok_or_else(|| "OpenCode apply requires at least one backup root".to_string())?;
    fs::create_dir_all(backup_root)
        .map_err(|error| format!("failed to create OpenCode backup directory: {error}"))?;
    let backup_path = if plan.skip_snapshot {
        None
    } else {
        let path = backup_root.join(format!("opencode-{}.json", timestamp_millis()));
        fs::copy(&plan.config_path, &path)
            .map_err(|error| format!("failed to back up OpenCode config: {error}"))?;
        Some(path)
    };
    record_opencode_rollback_baseline(&plan.config_path, backup_roots)?;
    write_text_replace(&plan.config_path, &plan.next)
        .map_err(|_| "failed to write managed OpenCode config".to_string())?;
    Ok(GatewayClientApplyResult {
        client_id: "opencode".to_string(),
        applied: true,
        config_path: Some(plan.config_path.clone()),
        backup_path,
        message: "OpenCode now routes through CodexHub Gateway.".to_string(),
    })
}

#[cfg(test)]
pub(in crate::gateway) fn restore_latest_backup(
    client_id: &str,
    config_path: &Path,
    backup_root: &Path,
) -> Result<GatewayClientApplyResult, String> {
    let latest = fs::read_dir(backup_root)
        .map_err(|error| {
            format!(
                "failed to read backup directory {}: {error}",
                backup_root.display()
            )
        })?
        .filter_map(Result::ok)
        .filter_map(|entry| {
            let metadata = entry.metadata().ok()?;
            let modified = metadata.modified().ok()?;
            let path = entry.path();
            let text = fs::read_to_string(&path).ok()?;
            (!is_opencode_codexhub_config(&text)).then_some((modified, path))
        })
        .max_by_key(|(modified, _)| *modified)
        .map(|(_, path)| path)
        .ok_or_else(|| format!("no clean official backup is available for {client_id}"))?;
    let text = fs::read_to_string(&latest)
        .map_err(|error| format!("failed to read backup {}: {error}", latest.display()))?;
    let clean_text = strip_opencode_invalid_keys(&text)?;
    write_text_replace(config_path, &clean_text)?;
    Ok(GatewayClientApplyResult {
        client_id: client_id.to_string(),
        applied: true,
        config_path: Some(config_path.to_path_buf()),
        backup_path: Some(latest),
        message: "OpenCode official config restored.".to_string(),
    })
}

pub(in crate::gateway) fn restore_opencode_config_with_backup_roots(
    config_path: &Path,
    backup_roots: &[(PathBuf, BackupChannel)],
) -> Result<GatewayClientApplyResult, String> {
    if let Some(baseline) = read_rollback_baseline("opencode")? {
        return match baseline.files.get("opencode.json") {
            Some(file) => restore_opencode_from_baseline(config_path, file),
            None => Err("rollback baseline is incomplete".to_string()),
        };
    }
    let _ = adopt_legacy_baseline_locked("opencode", backup_roots)?;
    if let Some(baseline) = read_rollback_baseline("opencode")? {
        return match baseline.files.get("opencode.json") {
            Some(file) => {
                let mut result = restore_opencode_from_baseline(config_path, file)?;
                result.message =
                    "OpenCode official config restored from legacy-adopted baseline.".to_string();
                Ok(result)
            }
            None => Err("rollback baseline is incomplete".to_string()),
        };
    }
    opencode_ownership_bounded_cleanup(config_path)
}

pub(in crate::gateway) fn is_opencode_codexhub_config(text: &str) -> bool {
    let Ok(value) = serde_json::from_str::<Value>(text) else {
        return text.contains("\"codexhub_managed\"")
            || text.contains("\"codexhub\"")
            || text.contains("\"codexhub-");
    };
    value
        .get("codexhub_managed")
        .and_then(Value::as_bool)
        .unwrap_or(false)
        || value
            .get("model")
            .and_then(Value::as_str)
            .is_some_and(is_codexhub_client_model_selector)
        || value
            .get("small_model")
            .and_then(Value::as_str)
            .is_some_and(is_codexhub_client_model_selector)
        || value
            .get("provider")
            .and_then(Value::as_object)
            .is_some_and(|providers| {
                providers
                    .iter()
                    .any(|(key, value)| is_managed_codexhub_provider_entry(key, value))
            })
}

#[cfg(test)]
pub(in crate::gateway) fn strip_opencode_invalid_keys(text: &str) -> Result<String, String> {
    let mut value = serde_json::from_str::<Value>(text)
        .map_err(|error| format!("failed to parse OpenCode config backup: {error}"))?;
    if let Some(object) = value.as_object_mut() {
        object.remove("codexhub_managed");
    }
    serde_json::to_string_pretty(&value)
        .map(|text| format!("{text}\n"))
        .map_err(|error| format!("failed to serialize cleaned OpenCode config: {error}"))
}
