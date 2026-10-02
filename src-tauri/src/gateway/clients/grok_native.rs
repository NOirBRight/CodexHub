use super::grok::{
    detect_grok_config_path, grok_agents_dir, grok_role_can_carry_effort, grok_shadow_is_ours,
    grok_spawn_models_are_owned, grok_toml_to_string, nested_table_mut, parse_grok_table,
    publish_grok_shadow_agents,
};
use crate::gateway::{
    client_rollback_provenance_dir_resolved, gateway_client_config_write_lock, rollback_file_text,
    NativeSubagentOption, NativeSubagentSettings, GROK_SPAWN_TYPES,
};
use crate::{file_transaction, safe_file};
use serde::{Deserialize, Serialize};
use serde_json::Value as Json;
use std::{
    collections::BTreeMap,
    fs,
    path::{Path, PathBuf},
};
use toml::{Table, Value};

#[derive(Clone, Serialize, Deserialize)]
struct NativePin {
    config_path: PathBuf,
    active: bool,
    models: Table,
    roles: Table,
    files: BTreeMap<String, Option<String>>,
}

fn pin_path() -> PathBuf {
    client_rollback_provenance_dir_resolved()
        .join("grok")
        .join("native-default-subagent.json")
}
fn read_pin(path: &Path) -> Result<Option<NativePin>, String> {
    match fs::read_to_string(pin_path()) {
        Ok(text) => {
            let pin: NativePin = serde_json::from_str(&text)
                .map_err(|_| "Grok native Default subagent state is invalid".to_string())?;
            Ok((pin.config_path == path).then_some(pin))
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(format!(
            "failed to read Grok native Default subagent: {error}"
        )),
    }
}
fn read_config(path: &Path) -> Result<Table, String> {
    match fs::read_to_string(path) {
        Ok(text) => parse_grok_table(&text),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(Table::new()),
        Err(error) => Err(format!("failed to read Grok configuration: {error}")),
    }
}
fn option(id: String, metadata: &Json) -> NativeSubagentOption {
    let mut efforts: Vec<String> = metadata
        .get("reasoning_efforts")
        .and_then(Json::as_array)
        .filter(|_| {
            metadata
                .get("supports_reasoning_effort")
                .and_then(Json::as_bool)
                != Some(false)
        })
        .map(|menu| {
            menu.iter()
                .filter_map(|item| {
                    item.get("value")
                        .and_then(Json::as_str)
                        .or_else(|| item.get("id").and_then(Json::as_str))
                })
                .map(str::to_string)
                .collect()
        })
        .unwrap_or_default();
    if !efforts.is_empty() {
        efforts.insert(0, String::new());
    }
    NativeSubagentOption {
        label: metadata
            .get("name")
            .and_then(Json::as_str)
            .map(|name| format!("{id} · {name}"))
            .unwrap_or_else(|| id.clone()),
        id,
        efforts,
        default_effort: String::new(),
    }
}
fn model_options(path: &Path, root: &Table) -> Vec<NativeSubagentOption> {
    let mut options = BTreeMap::new();
    // Only model metadata is returned. Cache entries can also contain credentials.
    if let Some(cache) = path
        .parent()
        .and_then(|home| fs::read_to_string(home.join("models_cache.json")).ok())
        .and_then(|text| serde_json::from_str::<Json>(&text).ok())
    {
        if let Some(models) = cache.get("models").and_then(Json::as_object) {
            for (id, entry) in models {
                let Some(info) = entry.get("info") else {
                    continue;
                };
                if id.starts_with("codexhub")
                    || info.get("hidden").and_then(Json::as_bool) == Some(true)
                {
                    continue;
                }
                options.insert(id.clone(), option(id.clone(), info));
            }
        }
    }
    if let Some(models) = root.get("model").and_then(Value::as_table) {
        for (id, model) in models {
            if id.starts_with("codexhub") {
                continue;
            }
            if let Ok(metadata) = serde_json::to_value(model) {
                options.insert(id.clone(), option(id.clone(), &metadata));
            }
        }
    }
    if let Some(models) = root
        .get("subagents")
        .and_then(Value::as_table)
        .and_then(|subagents| subagents.get("models"))
        .and_then(Value::as_table)
    {
        for name in GROK_SPAWN_TYPES {
            if let Some(id) = models
                .get(*name)
                .and_then(Value::as_str)
                .filter(|id| !id.starts_with("codexhub"))
            {
                options
                    .entry(id.into())
                    .or_insert_with(|| option(id.into(), &Json::Null));
            }
        }
    }
    let (selected, _) = selection(path, root);
    if !selected.is_empty() && !selected.starts_with("codexhub") {
        options
            .entry(selected.clone())
            .or_insert_with(|| option(selected, &Json::Null));
    }
    options.into_values().collect()
}
fn role<'a>(root: &'a Table, name: &str) -> Option<&'a Table> {
    root.get("subagents")?
        .as_table()?
        .get("roles")?
        .as_table()?
        .get(name)?
        .as_table()
}
fn header_value(text: &str, key: &str) -> Option<String> {
    let text = text
        .strip_prefix("---\n")
        .or_else(|| text.strip_prefix("---\r\n"))?;
    text.lines()
        .take_while(|line| *line != "---")
        .find_map(|line| {
            line.strip_prefix(&format!("{key}:"))
                .map(|value| value.trim().trim_matches(['\'', '"']).to_string())
        })
}
fn selection(path: &Path, root: &Table) -> (String, String) {
    for name in GROK_SPAWN_TYPES {
        let text = fs::read_to_string(grok_agents_dir(path).join(format!("{name}.md")))
            .unwrap_or_default();
        let model = role(root, name)
            .and_then(|role| role.get("model"))
            .and_then(Value::as_str)
            .map(str::to_string)
            .or_else(|| {
                root.get("subagents")?
                    .as_table()?
                    .get("models")?
                    .as_table()?
                    .get(*name)?
                    .as_str()
                    .map(str::to_string)
            })
            .or_else(|| header_value(&text, "model"));
        if let Some(model) = model {
            let effort = role(root, name)
                .and_then(|role| role.get("reasoning_effort"))
                .and_then(Value::as_str)
                .map(str::to_string)
                .or_else(|| header_value(&text, "effort"))
                .unwrap_or_default();
            return (model, effort);
        }
    }
    (String::new(), String::new())
}
pub fn read_grok_default_subagent() -> Result<NativeSubagentSettings, String> {
    read_grok_default_subagent_with_path(&detect_grok_config_path())
}
pub(in crate::gateway) fn read_grok_default_subagent_with_path(
    path: &Path,
) -> Result<NativeSubagentSettings, String> {
    let root = read_config(path)?;
    let (model, effort) = selection(path, &root);
    Ok(NativeSubagentSettings {
        native: !model.starts_with("codexhub"),
        model,
        effort,
        options: model_options(path, &root),
    })
}
fn snapshot(path: &Path, root: &Table, active: bool) -> NativePin {
    let mut models = Table::new();
    let mut roles = Table::new();
    let mut files = BTreeMap::new();
    for name in GROK_SPAWN_TYPES {
        if let Some(model) = root
            .get("subagents")
            .and_then(Value::as_table)
            .and_then(|subagents| subagents.get("models"))
            .and_then(Value::as_table)
            .and_then(|models| models.get(*name))
        {
            models.insert((*name).into(), model.clone());
        }
        if let Some(value) = root
            .get("subagents")
            .and_then(Value::as_table)
            .and_then(|subagents| subagents.get("roles"))
            .and_then(Value::as_table)
            .and_then(|roles| roles.get(*name))
        {
            roles.insert((*name).into(), value.clone());
        }
        files.insert(
            (*name).into(),
            fs::read_to_string(grok_agents_dir(path).join(format!("{name}.md"))).ok(),
        );
    }
    NativePin {
        config_path: path.into(),
        active,
        models,
        roles,
        files,
    }
}
fn apply_pin(root: &mut Table, pin: &NativePin) {
    for name in GROK_SPAWN_TYPES {
        let models = nested_table_mut(root, &["subagents", "models"]);
        match pin.models.get(*name) {
            Some(value) => {
                models.insert((*name).into(), value.clone());
            }
            None => {
                models.remove(*name);
            }
        }
        let roles = nested_table_mut(root, &["subagents", "roles"]);
        match pin.roles.get(*name) {
            Some(value) => {
                roles.insert((*name).into(), value.clone());
            }
            None => {
                roles.remove(*name);
            }
        }
    }
}
pub(in crate::gateway) fn apply_saved_native(
    path: &Path,
    root: &mut Table,
    detached: bool,
) -> Result<bool, String> {
    let Some(pin) = read_pin(path)? else {
        return Ok(false);
    };
    if !pin.active && !detached {
        return Ok(false);
    }
    if detached || grok_spawn_models_are_owned(root) {
        apply_pin(root, &pin);
    }
    Ok(true)
}
pub(in crate::gateway) fn native_active(path: &Path) -> Result<bool, String> {
    Ok(read_pin(path)?.is_some_and(|pin| pin.active))
}
pub(in crate::gateway) fn has_native_state(path: &Path) -> Result<bool, String> {
    Ok(read_pin(path)?.is_some())
}
pub(in crate::gateway) fn capture_native_before_publish(path: &Path) -> Result<(), String> {
    let Some(previous) = read_pin(path)? else {
        return Ok(());
    };
    let root = read_config(path)?;
    if grok_spawn_models_are_owned(&root) {
        return Ok(());
    }
    let pin = snapshot(path, &root, previous.active);
    safe_file::write_text_atomic(
        &pin_path(),
        &serde_json::to_string_pretty(&pin).map_err(|error| error.to_string())?,
    )
}
pub(in crate::gateway) fn native_files(
    path: &Path,
) -> Result<Vec<(PathBuf, Option<String>)>, String> {
    Ok(read_pin(path)?
        .map(|pin| {
            pin.files
                .into_iter()
                .map(|(name, text)| (grok_agents_dir(path).join(format!("{name}.md")), text))
                .collect()
        })
        .unwrap_or_default())
}
pub(in crate::gateway) fn restored_native_text(path: &Path, text: &str) -> Result<String, String> {
    let mut root = parse_grok_table(text)?;
    if apply_saved_native(path, &mut root, true)? {
        grok_toml_to_string(&root)
    } else {
        Ok(text.into())
    }
}

// Preserve the agent's prompt and every unrelated frontmatter field. Grok calls
// the definition's native effort field `effort`, unlike TOML role overrides.
fn edit_agent(text: &str, model: &str, effort: &str) -> Result<String, String> {
    let newline = if text.contains("\r\n") { "\r\n" } else { "\n" };
    let Some(rest) = text.strip_prefix(&format!("---{newline}")) else {
        return Err("Grok builtin agent must have YAML frontmatter".into());
    };
    let Some((header, body)) = rest.split_once(&format!("{newline}---")) else {
        return Err("Grok builtin agent frontmatter is incomplete".into());
    };
    let mut lines: Vec<String> = header
        .lines()
        .filter(|line| {
            !line.starts_with("model:")
                && !line.starts_with("effort:")
                && !line.starts_with("x-codexhub-default-subagent:")
        })
        .map(str::to_string)
        .collect();
    if !model.is_empty() {
        lines.push(format!(
            "model: {}",
            serde_json::to_string(model).map_err(|error| error.to_string())?
        ));
    }
    if !effort.is_empty() {
        lines.push(format!(
            "effort: {}",
            serde_json::to_string(effort).map_err(|error| error.to_string())?
        ));
    }
    Ok(format!(
        "---{newline}{}{newline}---{body}",
        lines.join(newline)
    ))
}

type AgentFileUpdates = Vec<(PathBuf, Option<String>)>;

pub(in crate::gateway) fn gateway_definition_plan(
    path: &Path,
    root: &Table,
    model: &str,
    effort: &str,
) -> Result<Option<AgentFileUpdates>, String> {
    let Some(pin) = read_pin(path)? else {
        return Ok(None);
    };
    if !GROK_SPAWN_TYPES
        .iter()
        .any(|name| !grok_role_can_carry_effort(root, name))
    {
        return Ok(None);
    }
    let mut files = Vec::new();
    for name in GROK_SPAWN_TYPES {
        let agent_path = grok_agents_dir(path).join(format!("{name}.md"));
        let source = (!grok_shadow_is_ours(&agent_path))
            .then(|| fs::read_to_string(&agent_path).ok())
            .flatten()
            .or_else(|| pin.files.get(*name).cloned().flatten())
            .or_else(|| {
                fs::read_to_string(
                    path.parent()?
                        .join("bundled/agents")
                        .join(format!("{name}.md")),
                )
                .ok()
            })
            .ok_or_else(|| format!("Grok builtin {name} agent definition is unavailable"))?;
        let text = edit_agent(&source, model, effort)?;
        let text = text.replacen("---", "---\nx-codexhub-default-subagent: true", 1);
        files.push((grok_agents_dir(path).join(format!("{name}.md")), Some(text)));
    }
    Ok(Some(files))
}
pub fn save_grok_default_subagent(
    model: String,
    effort: String,
    native: bool,
) -> Result<NativeSubagentSettings, String> {
    let _guard = gateway_client_config_write_lock()
        .lock()
        .map_err(|_| "gateway client config write lock is poisoned")?;
    save_grok_default_subagent_with_path(&detect_grok_config_path(), model, effort, native)
}
pub(in crate::gateway) fn save_grok_default_subagent_with_path(
    path: &Path,
    model: String,
    effort: String,
    native: bool,
) -> Result<NativeSubagentSettings, String> {
    if !native {
        capture_native_before_publish(path)?;
    }
    let mut root = read_config(path)?;
    let old_pin = read_pin(path)?;
    if !native && old_pin.as_ref().is_none_or(|pin| !pin.active) {
        return read_grok_default_subagent_with_path(path);
    }
    if native {
        if !model.is_empty() {
            let options = model_options(path, &root);
            let option = options
                .iter()
                .find(|option| option.id == model)
                .ok_or("model is not available in Grok CLI")?;
            if !effort.is_empty() && !option.efforts.contains(&effort) {
                return Err("effort is not available for this Grok model".into());
            }
        } else if !effort.is_empty() {
            return Err("Grok default model cannot have an effort".into());
        }
    }
    let mut files = Vec::new();
    if native {
        // Keep non-table role values intact. Such legacy configuration cannot
        // carry typed role overrides, so use real builtin definitions instead.
        let definitions_required = GROK_SPAWN_TYPES
            .iter()
            .any(|name| !grok_role_can_carry_effort(&root, name));
        for name in GROK_SPAWN_TYPES {
            let models = nested_table_mut(&mut root, &["subagents", "models"]);
            if model.is_empty() {
                models.remove(*name);
            } else {
                models.insert((*name).into(), Value::String(model.clone()));
            }
            let compatible = grok_role_can_carry_effort(&root, name);
            if compatible {
                let role = nested_table_mut(&mut root, &["subagents", "roles", name]);
                for (key, value) in [("model", &model), ("reasoning_effort", &effort)] {
                    if value.is_empty() {
                        role.remove(key);
                    } else {
                        role.insert(key.into(), Value::String(value.clone()));
                    }
                }
            }
            let agent_path = grok_agents_dir(path).join(format!("{name}.md"));
            let text = if grok_shadow_is_ours(&agent_path) {
                old_pin
                    .as_ref()
                    .and_then(|pin| pin.files.get(*name))
                    .cloned()
                    .flatten()
                    .or_else(|| {
                        rollback_file_text("grok", &format!("agents/{name}.md"), None)
                            .filter(|text| !text.is_empty())
                    })
            } else {
                fs::read_to_string(&agent_path).ok()
            };
            let text = if text.is_none() && definitions_required && !model.is_empty() {
                Some(
                    fs::read_to_string(
                        path.parent()
                            .unwrap_or(Path::new("."))
                            .join("bundled/agents")
                            .join(format!("{name}.md")),
                    )
                    .map_err(|_| format!("Grok builtin {name} agent definition is unavailable"))?,
                )
            } else {
                text
            };
            if let Some(text) = text {
                files.push((agent_path, Some(edit_agent(&text, &model, &effort)?)));
            } else if grok_shadow_is_ours(&agent_path) {
                files.push((agent_path, None));
            }
        }
    }
    let mut paths = vec![path.to_path_buf(), pin_path()];
    paths.extend(files.iter().map(|(path, _)| path.clone()));
    file_transaction::with_text_file_rollback(&paths, || {
        if native {
            safe_file::write_text_atomic(path, &grok_toml_to_string(&root)?)?;
            publish_grok_shadow_agents(&files)?;
        }
        let mut pin = old_pin
            .clone()
            .unwrap_or_else(|| snapshot(path, &root, native));
        if native {
            pin = snapshot(path, &root, true);
        } else {
            pin.active = false;
        }
        safe_file::write_text_atomic(
            &pin_path(),
            &serde_json::to_string_pretty(&pin).map_err(|error| error.to_string())?,
        )?;
        read_grok_default_subagent_with_path(path)
    })
    .map_err(|error| error.to_string())
}

#[cfg(test)]
mod native_grok_tests {
    use super::super::grok::restore_grok_config_with_backup_roots;
    use super::*;
    use crate::gateway::{
        apply_gateway_client_config_isolated, isolated_client_apply_targets,
        validate_existing_isolated_root, validate_isolated_root,
        with_rollback_provenance_dir_override, BackupChannel, IsolatedClientApplyInput,
    };
    use crate::{Provider, Settings};

    fn root() -> PathBuf {
        std::env::temp_dir().join(format!(
            "codexhub-native-grok-{}-{}",
            std::process::id(),
            crate::gateway::timestamp_millis()
        ))
    }
    fn fixture(path: &Path) -> Table {
        fs::create_dir_all(path.parent().unwrap().join("agents")).unwrap();
        let text = r#"
[models]
default = "grok-parent"
[model.native-child]
model = "native-child"
name = "Native child"
supports_reasoning_effort = true
reasoning_efforts = [{id = "low", value = "low"}, {id = "high", value = "high", default = true}]
[model_providers.native]
base_url = "https://native.example/v1"
api_key = "fixture-secret"
[subagents.models]
explore = "native-before"
custom = "custom-model"
[subagents.roles.explore]
description = "keep role"
reasoning_effort = "low"
[subagents.roles.custom]
model = "custom-model"
reasoning_effort = "low"
"#;
        fs::write(path, text).unwrap();
        fs::write(grok_agents_dir(path).join("explore.md"), "---\nname: explore\ndescription: keep description\npermission_mode: plan\nmodel: native-before\neffort: low\n---\nKeep the entire prompt.\n").unwrap();
        parse_grok_table(text).unwrap()
    }
    fn gateway_input() -> IsolatedClientApplyInput {
        let providers: Vec<Provider> =
            toml::from_str::<Value>(include_str!("../../../../config/providers.toml"))
                .unwrap()
                .get("providers")
                .unwrap()
                .clone()
                .try_into()
                .unwrap();
        let provider = providers
            .into_iter()
            .find(|provider| {
                provider.enabled && provider.id != "xai" && !provider.models.is_empty()
            })
            .unwrap();
        let model = format!("{}/{}", provider.id, provider.models[0].id);
        IsolatedClientApplyInput {
            client_id: "grok".into(),
            model: Some(model.clone()),
            settings: Settings {
                include_official_models: false,
                grok_default_subagent_model: model,
                grok_default_subagent_reasoning_effort: "high".into(),
                ..Settings::default()
            },
            providers: vec![provider],
            catalog_path: None,
            backup_subdir: None,
        }
    }
    fn save(
        path: &Path,
        model: String,
        effort: String,
        native: bool,
    ) -> Result<NativeSubagentSettings, String> {
        with_rollback_provenance_dir_override(
            Some(
                path.parent()
                    .unwrap()
                    .parent()
                    .unwrap()
                    .join("rollback-provenance"),
            ),
            || save_grok_default_subagent_with_path(path, model, effort, native),
        )
    }
    fn restore(
        dir: &Path,
        path: &Path,
    ) -> Result<crate::gateway::GatewayClientApplyResult, String> {
        with_rollback_provenance_dir_override(Some(dir.join("rollback-provenance")), || {
            restore_grok_config_with_backup_roots(
                path,
                &[(dir.join("backups"), BackupChannel::Stable)],
            )
        })
    }
    fn isolated(root: &Path, run: impl FnOnce(&Path)) {
        fs::create_dir_all(root).unwrap();
        with_rollback_provenance_dir_override(Some(root.join("rollback-provenance")), || {
            let targets =
                isolated_client_apply_targets(&validate_isolated_root(root).unwrap(), "grok")
                    .unwrap();
            run(&targets.writable_paths()[0]);
        });
        fs::remove_dir_all(root).unwrap();
    }
    #[test]
    fn native_grok_disconnected_save_readback_reset_preserves_parent_and_roles() {
        isolated(&root(), |path| {
            let before = fixture(path);
            let written = save(path, "native-child".into(), "high".into(), true).unwrap();
            assert!(written.native);
            assert_eq!(
                (written.model.as_str(), written.effort.as_str()),
                ("native-child", "high")
            );
            let after = read_config(path).unwrap();
            for key in ["models", "model", "model_providers"] {
                assert_eq!(before.get(key), after.get(key));
            }
            assert_eq!(
                role(&after, "explore")
                    .unwrap()
                    .get("description")
                    .unwrap()
                    .as_str(),
                Some("keep role")
            );
            assert_eq!(role(&before, "custom"), role(&after, "custom"));
            let file = fs::read_to_string(grok_agents_dir(path).join("explore.md")).unwrap();
            assert!(file.contains("permission_mode: plan\n"));
            assert!(file.ends_with("Keep the entire prompt.\n"));
            assert_eq!(header_value(&file, "effort"), Some("high".into()));
            assert_eq!(
                read_grok_default_subagent_with_path(path).unwrap().model,
                written.model
            );
            let reset = save(path, "".into(), "".into(), true).unwrap();
            assert!(reset.native && reset.model.is_empty() && reset.effort.is_empty());
            let file = fs::read_to_string(grok_agents_dir(path).join("explore.md")).unwrap();
            assert!(file.ends_with("Keep the entire prompt.\n"));
            assert!(header_value(&file, "model").is_none());
            assert!(save(path, "native-child".into(), "max".into(), true).is_err());
        });
    }
    #[test]
    fn native_grok_cache_keeps_real_xai_models_and_only_advertised_efforts() {
        isolated(&root(), |path| {
            fixture(path);
            fs::write(path.parent().unwrap().join("models_cache.json"), serde_json::json!({"models": {
                "grok-native-fast": {"api_key": "fixture-private", "info": {"supports_reasoning_effort": true,
                    "reasoning_efforts": [{"id": "medium", "value": "medium"}] }},
                "grok-no-effort": {"info": {"supports_reasoning_effort": false}},
                "grok-hidden": {"info": {"hidden": true}},
                "codexhub-owned": {"info": {}}
            }}).to_string()).unwrap();
            let result = read_grok_default_subagent_with_path(path).unwrap();
            assert_eq!(
                result
                    .options
                    .iter()
                    .find(|option| option.id == "grok-native-fast")
                    .unwrap()
                    .efforts,
                ["", "medium"]
            );
            assert!(result
                .options
                .iter()
                .find(|option| option.id == "grok-no-effort")
                .unwrap()
                .efforts
                .is_empty());
            assert!(!result
                .options
                .iter()
                .any(|option| option.id == "grok-hidden" || option.id == "codexhub-owned"));
            assert!(!serde_json::to_string(&result)
                .unwrap()
                .contains("fixture-private"));
            save(path, "grok-native-fast".into(), "medium".into(), true).unwrap();
        });
    }
    #[test]
    fn native_grok_connect_republish_disconnect_preserves_latest_manual_native_edit() {
        let dir = root();
        isolated(&dir, |path| {
            fixture(path);
            save(path, "native-child".into(), "high".into(), true).unwrap();
            apply_gateway_client_config_isolated(
                &validate_existing_isolated_root(&dir).unwrap(),
                &gateway_input(),
            )
            .unwrap();
            let mut edited = read_config(path).unwrap();
            nested_table_mut(&mut edited, &["subagents", "models"])
                .insert("explore".into(), Value::String("native-manual".into()));
            let role_table = nested_table_mut(&mut edited, &["subagents", "roles", "explore"]);
            role_table.insert("model".into(), Value::String("native-manual".into()));
            role_table.insert("reasoning_effort".into(), Value::String("low".into()));
            role_table.insert(
                "description".into(),
                Value::String("new user role description".into()),
            );
            fs::write(path, grok_toml_to_string(&edited).unwrap()).unwrap();
            let file_path = grok_agents_dir(path).join("explore.md");
            let file = fs::read_to_string(&file_path)
                .unwrap()
                .replace("Keep the entire prompt.", "New user prompt.");
            fs::write(&file_path, &file).unwrap();
            apply_gateway_client_config_isolated(
                &validate_existing_isolated_root(&dir).unwrap(),
                &gateway_input(),
            )
            .unwrap();
            assert_eq!(
                role(&read_config(path).unwrap(), "explore")
                    .unwrap()
                    .get("model")
                    .unwrap()
                    .as_str(),
                Some("native-manual")
            );
            restore(&dir, path).unwrap();
            let restored = read_config(path).unwrap();
            assert_eq!(
                role(&restored, "explore")
                    .unwrap()
                    .get("model")
                    .unwrap()
                    .as_str(),
                Some("native-manual")
            );
            assert_eq!(
                role(&restored, "explore")
                    .unwrap()
                    .get("description")
                    .unwrap()
                    .as_str(),
                Some("new user role description")
            );
            assert_eq!(fs::read_to_string(file_path).unwrap(), file);
            assert!(!fs::read_to_string(path).unwrap().contains("codexhub-"));
        });
    }
    #[test]
    fn native_grok_connected_gateway_pin_detach_restores_native_baseline_and_agent_body() {
        let dir = root();
        isolated(&dir, |path| {
            fixture(path);
            apply_gateway_client_config_isolated(
                &validate_existing_isolated_root(&dir).unwrap(),
                &gateway_input(),
            )
            .unwrap();
            save(path, "native-child".into(), "low".into(), true).unwrap();
            let native = fs::read_to_string(grok_agents_dir(path).join("explore.md")).unwrap();
            save(path, "".into(), "".into(), false).unwrap();
            apply_gateway_client_config_isolated(
                &validate_existing_isolated_root(&dir).unwrap(),
                &gateway_input(),
            )
            .unwrap();
            assert!(!read_grok_default_subagent_with_path(path).unwrap().native);
            restore(&dir, path).unwrap();
            let readback = read_grok_default_subagent_with_path(path).unwrap();
            assert!(readback.native);
            assert_eq!(
                (readback.model.as_str(), readback.effort.as_str()),
                ("native-child", "low")
            );
            assert_eq!(
                fs::read_to_string(grok_agents_dir(path).join("explore.md")).unwrap(),
                native
            );
            // The explicit Gateway preference stays active on reconnect. A
            // disconnected native hand-edit becomes the latest detach baseline.
            let mut changed = read_config(path).unwrap();
            for name in GROK_SPAWN_TYPES {
                nested_table_mut(&mut changed, &["subagents", "models"])
                    .insert((*name).into(), Value::String("native-manual".into()));
                nested_table_mut(&mut changed, &["subagents", "roles", name])
                    .insert("model".into(), Value::String("native-manual".into()));
            }
            fs::write(path, grok_toml_to_string(&changed).unwrap()).unwrap();
            apply_gateway_client_config_isolated(
                &validate_existing_isolated_root(&dir).unwrap(),
                &gateway_input(),
            )
            .unwrap();
            assert!(!read_grok_default_subagent_with_path(path).unwrap().native);
            restore(&dir, path).unwrap();
            assert_eq!(
                read_grok_default_subagent_with_path(path).unwrap().model,
                "native-manual"
            );
        });
    }
    #[test]
    fn native_grok_replaces_owned_shadow_and_preserves_non_table_role() {
        let dir = root();
        isolated(&dir, |path| {
            fixture(path);
            let mut config = read_config(path).unwrap();
            nested_table_mut(&mut config, &["subagents", "roles"]).insert(
                "explore".into(),
                Value::String("custom-role-reference".into()),
            );
            fs::write(path, grok_toml_to_string(&config).unwrap()).unwrap();
            let bundled = path.parent().unwrap().join("bundled/agents");
            fs::create_dir_all(&bundled).unwrap();
            for name in GROK_SPAWN_TYPES {
                fs::write(bundled.join(format!("{name}.md")), format!("---\nname: {name}\ndescription: builtin\npermission_mode: plan\n---\nBuiltin prompt {name}.\n")).unwrap();
            }
            let original = fs::read_to_string(grok_agents_dir(path).join("explore.md")).unwrap();
            apply_gateway_client_config_isolated(
                &validate_existing_isolated_root(&dir).unwrap(),
                &gateway_input(),
            )
            .unwrap();
            assert!(grok_shadow_is_ours(
                &grok_agents_dir(path).join("explore.md")
            ));
            save(path, "native-child".into(), "high".into(), true).unwrap();
            let after = read_config(path).unwrap();
            assert_eq!(
                after["subagents"]["roles"]["explore"].as_str(),
                Some("custom-role-reference")
            );
            let explore = fs::read_to_string(grok_agents_dir(path).join("explore.md")).unwrap();
            assert!(explore.ends_with("Keep the entire prompt.\n"));
            assert!(!explore.contains("x-codexhub-default-subagent"));
            for name in GROK_SPAWN_TYPES {
                let file =
                    fs::read_to_string(grok_agents_dir(path).join(format!("{name}.md"))).unwrap();
                assert_eq!(header_value(&file, "model"), Some("native-child".into()));
                assert_eq!(header_value(&file, "effort"), Some("high".into()));
            }
            apply_gateway_client_config_isolated(
                &validate_existing_isolated_root(&dir).unwrap(),
                &gateway_input(),
            )
            .unwrap();
            save(path, "".into(), "".into(), false).unwrap();
            apply_gateway_client_config_isolated(
                &validate_existing_isolated_root(&dir).unwrap(),
                &gateway_input(),
            )
            .unwrap();
            for name in GROK_SPAWN_TYPES {
                let text =
                    fs::read_to_string(grok_agents_dir(path).join(format!("{name}.md"))).unwrap();
                assert!(header_value(&text, "model")
                    .unwrap()
                    .starts_with("codexhub-"));
                assert_eq!(header_value(&text, "effort"), Some("high".into()));
            }
            restore(&dir, path).unwrap();
            assert_eq!(
                fs::read_to_string(grok_agents_dir(path).join("explore.md")).unwrap(),
                explore
            );
            save(path, "".into(), "".into(), true).unwrap();
            assert_eq!(
                header_value(
                    &fs::read_to_string(grok_agents_dir(path).join("explore.md")).unwrap(),
                    "model"
                ),
                None
            );
            assert!(original.ends_with("Keep the entire prompt.\n"));
        });
    }
    #[test]
    fn native_grok_connected_save_survives_absent_config_baseline() {
        let dir = root();
        isolated(&dir, |path| {
            fs::create_dir_all(path.parent().unwrap()).unwrap();
            fs::write(path.parent().unwrap().join("models_cache.json"), r#"{"models":{"grok-native":{"info":{"supports_reasoning_effort":true,"reasoning_efforts":[{"id":"high","value":"high"}]}}}}"#).unwrap();
            apply_gateway_client_config_isolated(
                &validate_existing_isolated_root(&dir).unwrap(),
                &gateway_input(),
            )
            .unwrap();
            save(path, "grok-native".into(), "high".into(), true).unwrap();
            apply_gateway_client_config_isolated(
                &validate_existing_isolated_root(&dir).unwrap(),
                &gateway_input(),
            )
            .unwrap();
            restore(&dir, path).unwrap();
            let readback = read_grok_default_subagent_with_path(path).unwrap();
            assert_eq!(
                (readback.model.as_str(), readback.effort.as_str()),
                ("grok-native", "high")
            );
            assert!(!fs::read_to_string(path).unwrap().contains("codexhub"));
            assert!(!read_config(path).unwrap().contains_key("models"));
        });
    }
}
