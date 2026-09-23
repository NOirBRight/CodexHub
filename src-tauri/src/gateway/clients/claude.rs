use super::super::{
    ensure_rollback_baseline, executable_version, is_this_app_gateway_url,
    resolve_gateway_client_model_id, route_owner_from_endpoint, sanitize_text, write_text_replace,
    BackupChannel, BaselineFile, GatewayClientApplyResult, GatewayClientConfigPreview,
};
use crate::app_flavor::RoutingOwner;
use crate::{Provider, Settings};
use serde::Serialize;
use serde_json::{json, Map, Value};
use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};

const CLIENT_ID: &str = "claude";
const MANAGED_MARKER_KEY: &str = "CODEXHUB_MANAGED_CLIENT";
const MANAGED_MARKER_VALUE: &str = "claude";
const ROLE_ENV: &[(&str, &str)] = &[
    ("haiku", "ANTHROPIC_DEFAULT_HAIKU_MODEL"),
    ("sonnet", "ANTHROPIC_DEFAULT_SONNET_MODEL"),
    ("opus", "ANTHROPIC_DEFAULT_OPUS_MODEL"),
    ("fable", "ANTHROPIC_DEFAULT_FABLE_MODEL"),
    ("subagent", "CLAUDE_CODE_SUBAGENT_MODEL"),
];
pub(in crate::gateway) const MANAGED_ENV_KEYS: &[&str] = &[
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_MODEL",
    "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_FABLE_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
    "CODEXHUB_MANAGED_CLIENT",
];

#[derive(Debug, Clone, Serialize)]
pub struct ClaudeClientSettings {
    pub default_model: String,
    pub role_mappings: BTreeMap<String, String>,
    pub conflicts: Vec<String>,
}

pub(in crate::gateway) fn read_claude_settings(
    path: &Path,
    settings: &Settings,
    providers: &[Provider],
) -> ClaudeClientSettings {
    let current = match fs::read_to_string(path) {
        Ok(text) => Some(text),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => None,
        Err(_) => {
            return ClaudeClientSettings {
                default_model: String::new(),
                role_mappings: BTreeMap::new(),
                conflicts: vec!["Cannot read Claude settings.json".into()],
            }
        }
    };
    let value = match current
        .as_deref()
        .map(serde_json::from_str::<Value>)
        .transpose()
    {
        Ok(value) => value.unwrap_or_else(|| json!({})),
        Err(_) => {
            return ClaudeClientSettings {
                default_model: String::new(),
                role_mappings: BTreeMap::new(),
                conflicts: vec!["Cannot parse Claude settings.json".into()],
            }
        }
    };
    let exported = super::super::gateway_models_from_config(settings, providers);
    let canonical = |key: &str| {
        let raw = value
            .pointer(&format!("/env/{key}"))
            .and_then(Value::as_str)
            .unwrap_or("");
        exported
            .iter()
            .find(|model| projected_claude_model_id(&model.id) == raw)
            .map(|model| model.id.clone())
            .unwrap_or_else(|| raw.to_string())
    };
    ClaudeClientSettings {
        default_model: canonical("ANTHROPIC_MODEL"),
        role_mappings: ROLE_ENV
            .iter()
            .map(|(role, key)| (role.to_string(), canonical(key)))
            .collect(),
        conflicts: claude_override_conflicts(Some(&value), settings),
    }
}

pub(in crate::gateway) fn claude_home() -> PathBuf {
    if let Some(path) = std::env::var_os("CODEXHUB_CLAUDE_HOME")
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
    {
        return path;
    }
    if let Some(path) = std::env::var_os("CLAUDE_CONFIG_DIR")
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
    {
        return path;
    }
    dirs::home_dir()
        .unwrap_or_else(|| PathBuf::from("."))
        .join(".claude")
}

pub(in crate::gateway) fn detect_claude_config_path() -> PathBuf {
    claude_home().join("settings.json")
}

pub(in crate::gateway) fn detect_claude_executable_path() -> Option<PathBuf> {
    which::which("claude").ok()
}

pub(in crate::gateway) fn detect_claude_version() -> Option<String> {
    detect_claude_executable_path()
        .as_deref()
        .and_then(executable_version)
}

pub(in crate::gateway) fn claude_installed() -> bool {
    detect_claude_executable_path().is_some() || detect_claude_config_path().exists()
}

pub(in crate::gateway) fn projected_claude_model_id(canonical: &str) -> String {
    let lower = canonical.to_ascii_lowercase();
    if lower.contains("claude") || lower.contains("anthropic") {
        return canonical.to_string();
    }
    let safe: String = canonical
        .chars()
        .map(|ch| {
            if ch.is_ascii_alphanumeric() || matches!(ch, '.' | '_' | '-') {
                ch
            } else {
                '-'
            }
        })
        .collect();
    format!("claude-codexhub-{}", safe.trim_matches('-'))
}

fn env_object(value: &Value) -> Option<&Map<String, Value>> {
    value.get("env").and_then(Value::as_object)
}

pub(in crate::gateway) fn is_claude_codexhub_config(text: &str) -> bool {
    let Ok(value) = serde_json::from_str::<Value>(text) else {
        return false;
    };
    env_object(&value)
        .and_then(|env| env.get(MANAGED_MARKER_KEY))
        .and_then(Value::as_str)
        == Some(MANAGED_MARKER_VALUE)
}

fn claude_owned_base_url(text: &str) -> Option<String> {
    let value: Value = serde_json::from_str(text).ok()?;
    env_object(&value)?
        .get("ANTHROPIC_BASE_URL")?
        .as_str()
        .filter(|url| !url.is_empty())
        .map(ToOwned::to_owned)
}

pub(in crate::gateway) fn detect_claude_route_details(
    current_owner: RoutingOwner,
    current_port: u16,
) -> (RoutingOwner, Option<String>) {
    let path = detect_claude_config_path();
    let text = fs::read_to_string(&path).ok();
    let managed = text.as_deref().is_some_and(is_claude_codexhub_config);
    let route_endpoint = text.as_deref().and_then(claude_owned_base_url);
    let this_app = route_endpoint
        .as_deref()
        .is_some_and(|url| is_this_app_gateway_url(url, current_port));
    (
        route_owner_from_endpoint(
            route_endpoint.as_deref(),
            managed && this_app,
            text.is_some(),
            current_owner,
            current_port,
        ),
        route_endpoint,
    )
}

fn env_key_looks_secret(key: &str) -> bool {
    let key = key.to_ascii_uppercase();
    key.contains("TOKEN")
        || key.contains("SECRET")
        || key.contains("PASSWORD")
        || key.contains("CREDENTIAL")
        || key.contains("AUTHORIZATION")
        || key.contains("API_KEY")
        || key.ends_with("_KEY")
}

fn mask_env_secrets(text: &str) -> String {
    let Ok(mut value) = serde_json::from_str::<Value>(text) else {
        return sanitize_text(text);
    };
    if let Some(env) = value.get_mut("env").and_then(Value::as_object_mut) {
        for (key, token) in env.iter_mut() {
            if env_key_looks_secret(key) && token.as_str().is_some_and(|raw| !raw.is_empty()) {
                *token = Value::String("***".to_string());
            }
        }
    }
    serde_json::to_string_pretty(&value).unwrap_or_else(|_| sanitize_text(text))
}

fn claude_override_conflicts(current: Option<&Value>, settings: &Settings) -> Vec<String> {
    let mut conflicts = Vec::new();
    for key in [
        "ANTHROPIC_API_KEY",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
    ] {
        let process = std::env::var(key).ok();
        let configured = current
            .and_then(|value| value.get("env"))
            .and_then(|env| env.get(key))
            .and_then(Value::as_str);
        if process
            .as_deref()
            .into_iter()
            .chain(configured)
            .any(|value| {
                !value.is_empty()
                    && (key == "ANTHROPIC_API_KEY" || (value != "0" && value != "false"))
            })
        {
            conflicts.push(format!(
                "{key} conflicts with Gateway routing; remove the override before connecting."
            ));
        }
    }
    for (key, expected) in [
        ("ANTHROPIC_AUTH_TOKEN", settings.gateway_client_key.clone()),
        (
            "ANTHROPIC_BASE_URL",
            claude_gateway_base_url(settings.proxy_port),
        ),
    ] {
        if std::env::var(key)
            .ok()
            .is_some_and(|value| !value.is_empty() && value != expected)
        {
            conflicts.push(format!(
                "Process environment {key} conflicts with Gateway routing."
            ));
        }
    }
    if current
        .and_then(|value| value.get("apiKeyHelper"))
        .is_some_and(|value| !value.is_null() && value.as_str() != Some(""))
    {
        conflicts.push(
            "apiKeyHelper conflicts with Gateway authentication; remove it before connecting."
                .into(),
        );
    }
    if current.is_some_and(|value| {
        !value.is_object() || value.get("env").is_some_and(|env| !env.is_object())
    }) {
        conflicts.push("Claude settings.json and env must be JSON objects.".into());
    }
    conflicts
}

fn claude_gateway_base_url(port: u16) -> String {
    // Claude Code appends /v1 to ANTHROPIC_BASE_URL itself.
    format!("http://127.0.0.1:{port}")
}

fn apply_managed_env(
    current: Option<&str>,
    managed: Option<&Map<String, Value>>,
) -> Result<String, String> {
    let mut root = match current.map(str::trim).filter(|text| !text.is_empty()) {
        Some(text) => serde_json::from_str::<Value>(text)
            .map_err(|error| format!("failed to parse Claude settings.json: {error}"))?,
        None => json!({}),
    };
    if !root.is_object() {
        return Err("Claude settings.json must be a JSON object".to_string());
    }
    {
        let env = root
            .as_object_mut()
            .expect("object")
            .entry("env")
            .or_insert_with(|| json!({}));
        let env_map = env
            .as_object_mut()
            .ok_or_else(|| "Claude settings.json env must be an object".to_string())?;
        for key in MANAGED_ENV_KEYS {
            match managed.and_then(|values| values.get(*key)) {
                Some(value) => {
                    env_map.insert((*key).to_string(), value.clone());
                }
                None => {
                    env_map.remove(*key);
                }
            }
        }
        if env_map.is_empty() {
            root.as_object_mut().map(|object| object.remove("env"));
        }
    }
    if root.as_object().is_some_and(Map::is_empty) {
        return Ok(String::new());
    }
    serde_json::to_string_pretty(&root)
        .map_err(|error| format!("failed to serialize Claude settings.json: {error}"))
}

fn resolve_claude_model(
    settings: &Settings,
    providers: &[Provider],
    model: &str,
) -> Result<String, String> {
    let resolved = resolve_gateway_client_model_id(settings, providers, model)?;
    if !super::super::gateway_models_from_config(settings, providers)
        .iter()
        .any(|model| model.id == resolved)
    {
        return Err(format!("Gateway model is not exported: {model}"));
    }
    Ok(resolved)
}

pub(in crate::gateway) fn claude_settings_text(
    current: Option<&str>,
    settings: &Settings,
    providers: &[Provider],
    model: &str,
    role_mappings: &BTreeMap<String, String>,
) -> Result<String, String> {
    let model = resolve_claude_model(settings, providers, model)?;
    let mut root = match current.map(str::trim).filter(|text| !text.is_empty()) {
        Some(text) => serde_json::from_str::<Value>(text)
            .map_err(|error| format!("failed to parse Claude settings.json: {error}"))?,
        None => json!({}),
    };
    if !root.is_object() {
        return Err("Claude settings.json must be a JSON object".to_string());
    }
    let env = root
        .as_object_mut()
        .expect("object")
        .entry("env")
        .or_insert_with(|| json!({}));
    let env_map = env
        .as_object_mut()
        .ok_or_else(|| "Claude settings.json env must be an object".to_string())?;
    env_map.insert(
        "ANTHROPIC_BASE_URL".to_string(),
        Value::String(claude_gateway_base_url(settings.proxy_port)),
    );
    env_map.insert(
        "ANTHROPIC_AUTH_TOKEN".to_string(),
        Value::String(settings.gateway_client_key.clone()),
    );
    env_map.insert(
        "ANTHROPIC_MODEL".to_string(),
        Value::String(projected_claude_model_id(&model)),
    );
    env_map.insert(
        "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY".to_string(),
        Value::String("1".to_string()),
    );
    env_map.insert(
        MANAGED_MARKER_KEY.to_string(),
        Value::String(MANAGED_MARKER_VALUE.to_string()),
    );
    let role_map: BTreeMap<&str, &str> = ROLE_ENV.iter().copied().collect();
    for (role, canonical) in role_mappings {
        let Some(env_key) = role_map.get(role.as_str()) else {
            return Err(format!("unknown Claude Code role: {role}"));
        };
        if canonical.trim().is_empty() {
            env_map.remove(*env_key);
            continue;
        }
        let resolved = resolve_claude_model(settings, providers, canonical)?;
        env_map.insert(
            (*env_key).to_string(),
            Value::String(projected_claude_model_id(&resolved)),
        );
    }
    // Republish preserves mappings, but a removed target must never pass silently.
    let exported = super::super::gateway_models_from_config(settings, providers);
    for (_, key) in ROLE_ENV {
        if let Some(value) = env_map
            .get(*key)
            .and_then(Value::as_str)
            .filter(|value| !value.is_empty())
        {
            if !exported
                .iter()
                .any(|model| model.id == value || projected_claude_model_id(&model.id) == value)
            {
                return Err(format!("Claude mapping {key} is not exported: {value}"));
            }
        }
    }
    serde_json::to_string_pretty(&root)
        .map_err(|error| format!("failed to serialize Claude settings.json: {error}"))
}

pub(in crate::gateway) fn preview_claude_config_with_path(
    config_path: &Path,
    settings: &Settings,
    providers: &[Provider],
    model: &str,
    role_mappings: &BTreeMap<String, String>,
) -> Result<GatewayClientConfigPreview, String> {
    let current = fs::read_to_string(config_path).ok();
    let next = claude_settings_text(
        current.as_deref(),
        settings,
        providers,
        model,
        role_mappings,
    )?;
    let value = current
        .as_deref()
        .and_then(|text| serde_json::from_str::<Value>(text).ok());
    let conflicts = claude_override_conflicts(value.as_ref(), settings);
    Ok(GatewayClientConfigPreview {
        client_id: CLIENT_ID.to_string(),
        can_apply: conflicts.is_empty(),
        strategy: "managed_key_set".to_string(),
        config_path: Some(config_path.to_path_buf()),
        current_redacted: current.as_deref().map(mask_env_secrets),
        next_redacted: mask_env_secrets(&next),
        backup_required: config_path.exists()
            && current
                .as_deref()
                .is_none_or(|text| !is_claude_codexhub_config(text)),
        message: {
            let mut message = "Connect changes this user's Claude Code default route and default model for newly launched sessions. Restart Claude Code after applying.".to_string();
            if !conflicts.is_empty() {
                message.push(' ');
                message.push_str(&conflicts.join(" "));
            }
            message
        },
    })
}

pub(in crate::gateway) struct ClaudeApplyPlan {
    pub config_path: PathBuf,
    pub expected_current: Option<String>,
    pub next: String,
    pub skip_snapshot: bool,
}

pub(in crate::gateway) fn plan_claude_apply(
    config_path: &Path,
    settings: &Settings,
    providers: &[Provider],
    model: &str,
    role_mappings: BTreeMap<String, String>,
) -> Result<ClaudeApplyPlan, String> {
    let current = if config_path.exists() {
        Some(
            fs::read_to_string(config_path)
                .map_err(|error| format!("failed to read Claude settings.json: {error}"))?,
        )
    } else {
        None
    };
    let value = current
        .as_deref()
        .and_then(|text| serde_json::from_str::<Value>(text).ok());
    let conflicts = claude_override_conflicts(value.as_ref(), settings);
    if !conflicts.is_empty() {
        return Err(conflicts.join(" "));
    }
    let skip_snapshot = current
        .as_deref()
        .is_none_or(|text| text.trim().is_empty() || is_claude_codexhub_config(text));
    let next = claude_settings_text(
        current.as_deref(),
        settings,
        providers,
        model,
        &role_mappings,
    )?;
    Ok(ClaudeApplyPlan {
        config_path: config_path.to_path_buf(),
        expected_current: current,
        skip_snapshot,
        next,
    })
}

pub(in crate::gateway) fn publish_claude_apply(
    plan: &ClaudeApplyPlan,
    backup_roots: &[(PathBuf, BackupChannel)],
) -> Result<GatewayClientApplyResult, String> {
    let on_disk = if plan.config_path.exists() {
        Some(
            fs::read_to_string(&plan.config_path)
                .map_err(|error| format!("failed to read Claude settings.json: {error}"))?,
        )
    } else {
        None
    };
    if on_disk.as_deref() != plan.expected_current.as_deref() {
        return Err("Claude settings.json changed while applying; retry Connect".to_string());
    }
    let (backup_root, _) = backup_roots
        .first()
        .ok_or_else(|| "Claude apply requires at least one backup root".to_string())?;
    if let Some(parent) = plan.config_path.parent() {
        fs::create_dir_all(parent)
            .map_err(|error| format!("failed to create Claude config directory: {error}"))?;
    }
    fs::create_dir_all(backup_root)
        .map_err(|error| format!("failed to create Claude backup directory: {error}"))?;
    let backup_path = if plan.skip_snapshot || !plan.config_path.exists() {
        None
    } else {
        let path = backup_root.join(format!(
            "claude-settings-{}.json",
            super::super::timestamp_millis()
        ));
        fs::copy(&plan.config_path, &path)
            .map_err(|error| format!("failed to back up Claude settings.json: {error}"))?;
        Some(path)
    };
    ensure_rollback_baseline(
        CLIENT_ID,
        backup_roots,
        &[("settings.json", &plan.config_path)],
        |_, text| is_claude_codexhub_config(text),
    )?;
    write_text_replace(&plan.config_path, &plan.next)
        .map_err(|_| "failed to write managed Claude settings.json".to_string())?;
    Ok(GatewayClientApplyResult {
        client_id: CLIENT_ID.to_string(),
        applied: true,
        config_path: Some(plan.config_path.clone()),
        backup_path,
        message:
            "Claude Code now routes new sessions through the CodexHub Gateway. Restart Claude Code."
                .to_string(),
    })
}

fn write_restored_settings(config_path: &Path, next: &str) -> Result<(), String> {
    if next.trim().is_empty() {
        if config_path.exists() {
            fs::remove_file(config_path).map_err(|error| {
                format!("failed to remove managed Claude settings.json: {error}")
            })?;
        }
        return Ok(());
    }
    if let Some(parent) = config_path.parent() {
        fs::create_dir_all(parent)
            .map_err(|error| format!("failed to create Claude config directory: {error}"))?;
    }
    write_text_replace(config_path, next)
        .map_err(|_| "failed to restore Claude settings.json".to_string())
}

pub(in crate::gateway) fn restore_claude_from_baseline(
    config_path: &Path,
    file: &BaselineFile,
) -> Result<GatewayClientApplyResult, String> {
    let current = fs::read_to_string(config_path).ok();
    let managed = match file {
        BaselineFile::Snapshot { content } => {
            let snapshot: Value = serde_json::from_str(content)
                .map_err(|error| format!("failed to parse Claude baseline: {error}"))?;
            snapshot.get("env").and_then(Value::as_object).cloned()
        }
        BaselineFile::Absent => None,
    };
    let next = apply_managed_env(current.as_deref(), managed.as_ref())?;
    write_restored_settings(config_path, &next)?;
    Ok(GatewayClientApplyResult {
        client_id: CLIENT_ID.to_string(),
        applied: true,
        config_path: None,
        backup_path: None,
        message: match file {
            BaselineFile::Snapshot { .. } => {
                "Claude Code managed keys restored from canonical baseline.".to_string()
            }
            BaselineFile::Absent => "Managed Claude Code keys removed.".to_string(),
        },
    })
}

pub(in crate::gateway) fn restore_claude_config_with_backup_roots(
    config_path: &Path,
    _backup_roots: &[(PathBuf, BackupChannel)],
) -> Result<GatewayClientApplyResult, String> {
    if let Some(baseline) = super::super::read_rollback_baseline(CLIENT_ID)? {
        return match baseline.files.get("settings.json") {
            Some(file) => restore_claude_from_baseline(config_path, file),
            None => Err("rollback baseline is incomplete".to_string()),
        };
    }
    if config_path.exists() {
        let text = fs::read_to_string(config_path)
            .map_err(|error| format!("failed to read Claude settings.json: {error}"))?;
        if is_claude_codexhub_config(&text) {
            let mut value: Value = serde_json::from_str(&text)
                .map_err(|error| format!("failed to parse Claude settings.json: {error}"))?;
            if let Some(env) = value.get_mut("env").and_then(Value::as_object_mut) {
                for key in MANAGED_ENV_KEYS {
                    env.remove(*key);
                }
                if env.is_empty() {
                    value.as_object_mut().map(|root| root.remove("env"));
                }
            }
            let leftover = value.as_object().is_none_or(|root| root.is_empty());
            if leftover {
                fs::remove_file(config_path).map_err(|error| {
                    format!("failed to remove managed Claude settings.json: {error}")
                })?;
            } else {
                let next = serde_json::to_string_pretty(&value).map_err(|error| {
                    format!("failed to serialize Claude settings.json: {error}")
                })?;
                write_text_replace(config_path, &next)
                    .map_err(|_| "failed to restore Claude settings.json".to_string())?;
            }
        }
    }
    Ok(GatewayClientApplyResult {
        client_id: CLIENT_ID.to_string(),
        applied: true,
        config_path: None,
        backup_path: None,
        message: "Managed Claude Code keys removed.".to_string(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::Settings;

    fn settings() -> Settings {
        Settings {
            proxy_port: 18789,
            gateway_client_key: "gateway-secret-key".to_string(),
            ..Settings::default()
        }
    }

    #[test]
    fn preview_apply_and_readback_use_the_same_draft() {
        let dir = std::env::temp_dir().join(format!(
            "codexhub-claude-regression-1-{}",
            std::process::id()
        ));
        fs::create_dir_all(&dir).unwrap();
        let path = dir.join("settings.json");
        fs::write(&path, r#"{"env":{"EDITOR":"vim"},"theme":"dark"}"#).unwrap();
        let mappings = BTreeMap::from([("haiku".into(), "gpt-5.5".into())]);
        let preview =
            preview_claude_config_with_path(&path, &settings(), &[], "gpt-5.5", &mappings).unwrap();
        let plan = plan_claude_apply(&path, &settings(), &[], "gpt-5.5", mappings).unwrap();
        assert_eq!(preview.next_redacted, mask_env_secrets(&plan.next));
        publish_claude_apply(&plan, &[(dir.join("backup"), BackupChannel::Stable)]).unwrap();
        let saved = read_claude_settings(&path, &settings(), &[]);
        assert_eq!(saved.default_model, "gpt-5.5");
        assert_eq!(saved.role_mappings["haiku"], "gpt-5.5");
        assert!(!serde_json::to_string(&saved)
            .unwrap()
            .contains("gateway-secret-key"));
        let cleared = BTreeMap::from([("haiku".into(), String::new())]);
        let plan = plan_claude_apply(&path, &settings(), &[], "gpt-5.5", cleared).unwrap();
        assert!(!plan.next.contains("ANTHROPIC_DEFAULT_HAIKU_MODEL"));
        assert!(plan.next.contains("EDITOR"));
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn credential_conflicts_block_preview_and_apply_without_writing() {
        let dir = std::env::temp_dir().join(format!(
            "codexhub-claude-regression-2-{}",
            std::process::id()
        ));
        fs::create_dir_all(&dir).unwrap();
        let path = dir.join("settings.json");
        for current in [
            r#"{"env":{"ANTHROPIC_API_KEY":"user-secret"}}"#,
            r#"{"apiKeyHelper":"credential-command"}"#,
        ] {
            fs::write(&path, current).unwrap();
            let preview = preview_claude_config_with_path(
                &path,
                &settings(),
                &[],
                "gpt-5.5",
                &BTreeMap::new(),
            )
            .unwrap();
            assert!(!preview.can_apply);
            assert!(preview.message.contains("conflicts"));
            assert!(
                plan_claude_apply(&path, &settings(), &[], "gpt-5.5", BTreeMap::new()).is_err()
            );
            assert_eq!(fs::read_to_string(&path).unwrap(), current);
        }
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn removed_models_fail_without_changing_settings() {
        let dir = std::env::temp_dir().join(format!(
            "codexhub-claude-regression-3-{}",
            std::process::id()
        ));
        fs::create_dir_all(&dir).unwrap();
        let path = dir.join("settings.json");
        assert!(
            plan_claude_apply(&path, &settings(), &[], "removed/model", BTreeMap::new()).is_err()
        );
        let result = plan_claude_apply(
            &path,
            &settings(),
            &[],
            "gpt-5.5",
            BTreeMap::from([("haiku".into(), "removed/model".into())]),
        );
        assert!(result.is_err());
        assert!(!path.exists());
        fs::write(
            &path,
            r#"{"env":{"ANTHROPIC_DEFAULT_HAIKU_MODEL":"claude-codexhub-removed-model"}}"#,
        )
        .unwrap();
        assert!(plan_claude_apply(&path, &settings(), &[], "gpt-5.5", BTreeMap::new()).is_err());
        let saved = read_claude_settings(&path, &settings(), &[]);
        assert_eq!(
            saved.role_mappings["haiku"],
            "claude-codexhub-removed-model"
        );
        fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn projected_ids_match_claude_filter() {
        assert_eq!(
            projected_claude_model_id("volc/glm-5.2"),
            "claude-codexhub-volc-glm-5.2"
        );
        assert_eq!(projected_claude_model_id("claude-sonnet"), "claude-sonnet");
    }

    #[test]
    fn settings_merge_preserves_foreign_keys_and_masks_token() {
        let _guard = crate::gateway::tests::TEST_ENV_LOCK
            .get_or_init(|| std::sync::Mutex::new(()))
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        let _official_home = crate::gateway::tests::isolated_official_models_home();
        let current = r#"{"env":{"EDITOR":"vim","ANTHROPIC_AUTH_TOKEN":"old","OPENAI_API_KEY":"sk-user"},"theme":"dark"}"#;
        let next =
            claude_settings_text(Some(current), &settings(), &[], "gpt-5.5", &BTreeMap::new())
                .unwrap();
        assert!(next.contains("\"EDITOR\": \"vim\""));
        assert!(next.contains("\"theme\": \"dark\""));
        assert!(next.contains("gateway-secret-key"));
        assert!(next.contains("ANTHROPIC_BASE_URL"));
        let value: Value = serde_json::from_str(&next).unwrap();
        assert_eq!(
            value
                .pointer("/env/ANTHROPIC_BASE_URL")
                .and_then(Value::as_str),
            Some("http://127.0.0.1:18789")
        );
        assert!(next.contains("CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"));
        let masked = mask_env_secrets(&next);
        assert!(!masked.contains("gateway-secret-key"));
        assert!(!masked.contains("sk-user"));
        assert!(masked.contains("***"));
    }

    #[test]
    fn empty_mappings_are_not_required_to_write_settings() {
        let _guard = crate::gateway::tests::TEST_ENV_LOCK
            .get_or_init(|| std::sync::Mutex::new(()))
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        let _official_home = crate::gateway::tests::isolated_official_models_home();
        let next =
            claude_settings_text(None, &settings(), &[], "gpt-5.5", &BTreeMap::new()).unwrap();
        let value: Value = serde_json::from_str(&next).unwrap();
        let env = value.get("env").unwrap().as_object().unwrap();
        assert_eq!(
            env.get("ANTHROPIC_MODEL").and_then(Value::as_str),
            Some("claude-codexhub-gpt-5.5")
        );
        for key in [
            "ANTHROPIC_BASE_URL",
            "ANTHROPIC_AUTH_TOKEN",
            "ANTHROPIC_MODEL",
            "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY",
            "CODEXHUB_MANAGED_CLIENT",
        ] {
            assert!(env.contains_key(key), "{key}");
        }
        assert_eq!(
            env.get("CODEXHUB_MANAGED_CLIENT").and_then(Value::as_str),
            Some("claude")
        );
        assert!(!env.contains_key("ANTHROPIC_DEFAULT_HAIKU_MODEL"));
    }

    #[test]
    fn role_mappings_write_projected_env_keys() {
        let _guard = crate::gateway::tests::TEST_ENV_LOCK
            .get_or_init(|| std::sync::Mutex::new(()))
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        let _official_home = crate::gateway::tests::isolated_official_models_home();
        let mappings = BTreeMap::from([("haiku".to_string(), "gpt-5.5".to_string())]);
        let next = claude_settings_text(None, &settings(), &[], "gpt-5.5", &mappings).unwrap();
        let value: Value = serde_json::from_str(&next).unwrap();
        let env = value.get("env").unwrap().as_object().unwrap();
        assert_eq!(
            env.get("ANTHROPIC_DEFAULT_HAIKU_MODEL")
                .and_then(Value::as_str),
            Some("claude-codexhub-gpt-5.5")
        );
    }

    #[test]
    fn restore_without_baseline_removes_only_managed_keys() {
        let _guard = crate::gateway::tests::TEST_ENV_LOCK
            .get_or_init(|| std::sync::Mutex::new(()))
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        let _official_home = crate::gateway::tests::isolated_official_models_home();
        let dir =
            std::env::temp_dir().join(format!("codexhub-claude-restore-{}", std::process::id()));
        fs::create_dir_all(&dir).unwrap();
        let path = dir.join("settings.json");
        let mappings = BTreeMap::from([("haiku".to_string(), "gpt-5.5".to_string())]);
        let next = claude_settings_text(
            Some(r#"{"env":{"EDITOR":"vim"},"theme":"dark"}"#),
            &settings(),
            &[],
            "gpt-5.5",
            &mappings,
        )
        .unwrap();
        fs::write(&path, next).unwrap();
        restore_claude_config_with_backup_roots(&path, &[]).unwrap();
        let restored: Value = serde_json::from_str(&fs::read_to_string(&path).unwrap()).unwrap();
        assert_eq!(
            restored.pointer("/env/EDITOR").and_then(Value::as_str),
            Some("vim")
        );
        assert_eq!(restored.get("theme").and_then(Value::as_str), Some("dark"));
        assert!(restored.pointer("/env/ANTHROPIC_AUTH_TOKEN").is_none());
        assert!(restored
            .pointer("/env/ANTHROPIC_DEFAULT_HAIKU_MODEL")
            .is_none());
        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn user_settings_without_marker_are_not_owned() {
        let text = r#"{"env":{"ANTHROPIC_BASE_URL":"http://127.0.0.1:9","ANTHROPIC_AUTH_TOKEN":"user","CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY":"1"}}"#;
        assert!(!is_claude_codexhub_config(text));
    }

    #[test]
    fn concurrent_edit_fails_closed_before_write() {
        let _guard = crate::gateway::tests::TEST_ENV_LOCK
            .get_or_init(|| std::sync::Mutex::new(()))
            .lock()
            .unwrap_or_else(|error| error.into_inner());
        let _official_home = crate::gateway::tests::isolated_official_models_home();
        let dir =
            std::env::temp_dir().join(format!("codexhub-claude-concurrent-{}", std::process::id()));
        fs::create_dir_all(&dir).unwrap();
        let path = dir.join("settings.json");
        fs::write(&path, r#"{"theme":"dark"}"#).unwrap();
        let plan = plan_claude_apply(&path, &settings(), &[], "gpt-5.5", BTreeMap::new()).unwrap();
        fs::write(&path, r#"{"theme":"light"}"#).unwrap();
        let backup = dir.join("backup");
        let error = publish_claude_apply(&plan, &[(backup, BackupChannel::Stable)]).unwrap_err();
        assert!(error.contains("changed"), "{error}");
        assert_eq!(fs::read_to_string(&path).unwrap(), r#"{"theme":"light"}"#);
        let _ = fs::remove_dir_all(dir);
    }

    #[test]
    fn restore_from_snapshot_keeps_later_foreign_edits() {
        let current = r#"{"env":{"EDITOR":"nvim","ANTHROPIC_AUTH_TOKEN":"gateway-secret-key","CODEXHUB_MANAGED_CLIENT":"claude"},"theme":"dark"}"#;
        let snapshot = BaselineFile::Snapshot {
            content: r#"{"theme":"dark"}"#.to_string(),
        };
        let dir = std::env::temp_dir().join(format!(
            "codexhub-claude-restore-snap-{}",
            std::process::id()
        ));
        fs::create_dir_all(&dir).unwrap();
        let path = dir.join("settings.json");
        fs::write(&path, current).unwrap();
        restore_claude_from_baseline(&path, &snapshot).unwrap();
        let restored: Value = serde_json::from_str(&fs::read_to_string(&path).unwrap()).unwrap();
        assert_eq!(
            restored.pointer("/env/EDITOR").and_then(Value::as_str),
            Some("nvim")
        );
        assert_eq!(restored.get("theme").and_then(Value::as_str), Some("dark"));
        assert!(restored.pointer("/env/ANTHROPIC_AUTH_TOKEN").is_none());
        assert!(restored.pointer("/env/CODEXHUB_MANAGED_CLIENT").is_none());
        let _ = fs::remove_dir_all(dir);
    }
}
