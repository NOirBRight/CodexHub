use super::super::{
    endpoints, ensure_rollback_baseline, executable_version, is_this_app_gateway_url,
    resolve_gateway_client_model_id, route_owner_from_endpoint, sanitize_text, write_text_replace,
    BackupChannel, BaselineFile, GatewayClientApplyResult, GatewayClientConfigPreview,
};
use crate::app_flavor::RoutingOwner;
use crate::{Provider, Settings};
use serde_json::{json, Map, Value};
use std::cell::RefCell;
use std::collections::BTreeMap;
use std::fs;
use std::path::{Path, PathBuf};

const CLIENT_ID: &str = "claude";
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
];

thread_local! {
    static PENDING_ROLE_MAPPINGS: RefCell<BTreeMap<String, String>> = const { RefCell::new(BTreeMap::new()) };
}

pub(in crate::gateway) fn set_pending_role_mappings(mappings: BTreeMap<String, String>) {
    PENDING_ROLE_MAPPINGS.with(|slot| *slot.borrow_mut() = mappings);
}

fn pending_role_mappings() -> BTreeMap<String, String> {
    PENDING_ROLE_MAPPINGS.with(|slot| slot.borrow().clone())
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
    let Some(env) = env_object(&value) else {
        return false;
    };
    env.contains_key("ANTHROPIC_BASE_URL")
        && env.contains_key("ANTHROPIC_AUTH_TOKEN")
        && env
            .get("CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY")
            .and_then(Value::as_str)
            == Some("1")
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

fn mask_env_secrets(text: &str) -> String {
    let Ok(mut value) = serde_json::from_str::<Value>(text) else {
        return sanitize_text(text);
    };
    if let Some(env) = value.get_mut("env").and_then(Value::as_object_mut) {
        if let Some(token) = env.get_mut("ANTHROPIC_AUTH_TOKEN") {
            if token.as_str().is_some_and(|raw| !raw.is_empty()) {
                *token = Value::String("***".to_string());
            }
        }
    }
    serde_json::to_string_pretty(&value).unwrap_or_else(|_| sanitize_text(text))
}

pub(in crate::gateway) fn claude_settings_text(
    current: Option<&str>,
    settings: &Settings,
    providers: &[Provider],
    model: &str,
) -> Result<String, String> {
    let model = resolve_gateway_client_model_id(settings, providers, model)?;
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
        Value::String(endpoints(settings.proxy_port).base_url),
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
    let role_map: BTreeMap<&str, &str> = ROLE_ENV.iter().copied().collect();
    for (role, canonical) in pending_role_mappings() {
        let Some(env_key) = role_map.get(role.as_str()) else {
            return Err(format!("unknown Claude Code role: {role}"));
        };
        if canonical.trim().is_empty() {
            env_map.remove(*env_key);
            continue;
        }
        let resolved = resolve_gateway_client_model_id(settings, providers, &canonical)?;
        env_map.insert(
            (*env_key).to_string(),
            Value::String(projected_claude_model_id(&resolved)),
        );
    }
    serde_json::to_string_pretty(&root)
        .map_err(|error| format!("failed to serialize Claude settings.json: {error}"))
}

pub(in crate::gateway) fn preview_claude_config_with_path(
    config_path: &Path,
    settings: &Settings,
    providers: &[Provider],
    model: &str,
) -> Result<GatewayClientConfigPreview, String> {
    let current = fs::read_to_string(config_path).ok();
    let next = claude_settings_text(current.as_deref(), settings, providers, model)?;
    Ok(GatewayClientConfigPreview {
        client_id: CLIENT_ID.to_string(),
        can_apply: true,
        strategy: "managed_key_set".to_string(),
        config_path: Some(config_path.to_path_buf()),
        current_redacted: current.as_deref().map(mask_env_secrets),
        next_redacted: mask_env_secrets(&next),
        backup_required: config_path.exists(),
        message: "Connect changes this user's Claude Code default route and default model for newly launched sessions. Restart Claude Code after applying.".to_string(),
    })
}

pub(in crate::gateway) struct ClaudeApplyPlan {
    pub config_path: PathBuf,
    pub next: String,
    pub skip_snapshot: bool,
}

pub(in crate::gateway) fn plan_claude_apply(
    config_path: &Path,
    settings: &Settings,
    providers: &[Provider],
    model: &str,
) -> Result<ClaudeApplyPlan, String> {
    let current = if config_path.exists() {
        Some(
            fs::read_to_string(config_path)
                .map_err(|error| format!("failed to read Claude settings.json: {error}"))?,
        )
    } else {
        None
    };
    let skip_snapshot = current
        .as_deref()
        .is_none_or(|text| text.trim().is_empty() || is_claude_codexhub_config(text));
    let next = claude_settings_text(current.as_deref(), settings, providers, model)?;
    Ok(ClaudeApplyPlan {
        config_path: config_path.to_path_buf(),
        skip_snapshot,
        next,
    })
}

pub(in crate::gateway) fn publish_claude_apply(
    plan: &ClaudeApplyPlan,
    backup_roots: &[(PathBuf, BackupChannel)],
) -> Result<GatewayClientApplyResult, String> {
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
        message: "Claude Code now routes new sessions through the CodexHub Gateway. Restart Claude Code.".to_string(),
    })
}

pub(in crate::gateway) fn restore_claude_from_baseline(
    config_path: &Path,
    file: &BaselineFile,
) -> Result<GatewayClientApplyResult, String> {
    match file {
        BaselineFile::Snapshot { content } => {
            write_text_replace(config_path, content)
                .map_err(|_| "failed to restore Claude settings.json from baseline".to_string())?;
            Ok(GatewayClientApplyResult {
                client_id: CLIENT_ID.to_string(),
                applied: true,
                config_path: None,
                backup_path: None,
                message: "Claude Code settings restored from canonical baseline.".to_string(),
            })
        }
        BaselineFile::Absent => {
            if config_path.exists() {
                fs::remove_file(config_path).map_err(|error| {
                    format!("failed to remove managed Claude settings.json: {error}")
                })?;
            }
            Ok(GatewayClientApplyResult {
                client_id: CLIENT_ID.to_string(),
                applied: true,
                config_path: None,
                backup_path: None,
                message: "Managed Claude Code settings removed.".to_string(),
            })
        }
    }
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
                let next = serde_json::to_string_pretty(&value)
                    .map_err(|error| format!("failed to serialize Claude settings.json: {error}"))?;
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
    fn projected_ids_match_claude_filter() {
        assert_eq!(
            projected_claude_model_id("volc/glm-5.2"),
            "claude-codexhub-volc-glm-5.2"
        );
        assert_eq!(projected_claude_model_id("claude-sonnet"), "claude-sonnet");
    }

    #[test]
    fn settings_merge_preserves_foreign_keys_and_masks_token() {
        let current = r#"{"env":{"EDITOR":"vim","ANTHROPIC_AUTH_TOKEN":"old"},"theme":"dark"}"#;
        let next = claude_settings_text(Some(current), &settings(), &[], "gpt-5.5").unwrap();
        assert!(next.contains("\"EDITOR\": \"vim\""));
        assert!(next.contains("\"theme\": \"dark\""));
        assert!(next.contains("gateway-secret-key"));
        assert!(next.contains("ANTHROPIC_BASE_URL"));
        assert!(next.contains("CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"));
        let masked = mask_env_secrets(&next);
        assert!(!masked.contains("gateway-secret-key"));
        assert!(masked.contains("***"));
    }

    #[test]
    fn empty_mappings_are_not_required_to_write_settings() {
        let next = claude_settings_text(None, &settings(), &[], "gpt-5.5").unwrap();
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
        ] {
            assert!(env.contains_key(key), "{key}");
        }
        assert!(!env.contains_key("ANTHROPIC_DEFAULT_HAIKU_MODEL"));
    }

    #[test]
    fn role_mappings_write_projected_env_keys() {
        set_pending_role_mappings(BTreeMap::from([(
            "haiku".to_string(),
            "gpt-5.5".to_string(),
        )]));
        let next = claude_settings_text(None, &settings(), &[], "gpt-5.5").unwrap();
        let value: Value = serde_json::from_str(&next).unwrap();
        let env = value.get("env").unwrap().as_object().unwrap();
        assert_eq!(
            env.get("ANTHROPIC_DEFAULT_HAIKU_MODEL")
                .and_then(Value::as_str),
            Some("claude-codexhub-gpt-5.5")
        );
        set_pending_role_mappings(BTreeMap::new());
    }

    #[test]
    fn restore_without_baseline_removes_only_managed_keys() {
        let dir = std::env::temp_dir().join(format!(
            "codexhub-claude-restore-{}",
            std::process::id()
        ));
        fs::create_dir_all(&dir).unwrap();
        let path = dir.join("settings.json");
        set_pending_role_mappings(BTreeMap::from([(
            "haiku".to_string(),
            "gpt-5.5".to_string(),
        )]));
        let next = claude_settings_text(
            Some(r#"{"env":{"EDITOR":"vim"},"theme":"dark"}"#),
            &settings(),
            &[],
            "gpt-5.5",
        )
        .unwrap();
        fs::write(&path, next).unwrap();
        restore_claude_config_with_backup_roots(&path, &[]).unwrap();
        let restored: Value = serde_json::from_str(&fs::read_to_string(&path).unwrap()).unwrap();
        assert_eq!(
            restored
                .pointer("/env/EDITOR")
                .and_then(Value::as_str),
            Some("vim")
        );
        assert_eq!(restored.get("theme").and_then(Value::as_str), Some("dark"));
        assert!(restored.pointer("/env/ANTHROPIC_AUTH_TOKEN").is_none());
        assert!(restored.pointer("/env/ANTHROPIC_DEFAULT_HAIKU_MODEL").is_none());
        set_pending_role_mappings(BTreeMap::new());
        let _ = fs::remove_dir_all(dir);
    }
}
