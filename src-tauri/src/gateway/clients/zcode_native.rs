use super::zcode::{detect_zcode_config_targets, ZcodeConfigTargets};
use crate::gateway::{
    client_rollback_provenance_dir_resolved, gateway_client_config_write_lock,
    is_codexhub_client_model_selector, ClientDefaultSubagentPin, NativeSubagentOption,
    NativeSubagentSettings, ZCODE_SPAWN_AGENTS,
};
use crate::{file_transaction, safe_file};
use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};
use std::{
    fs,
    path::{Path, PathBuf},
};

#[derive(Serialize, Deserialize)]
struct NativePin {
    config_path: PathBuf,
    active: bool,
    agents: Map<String, Value>,
}

fn state_path(targets: &ZcodeConfigTargets) -> PathBuf {
    targets.v2_config_path.with_file_name("agents-state.json")
}

fn pin_path() -> PathBuf {
    client_rollback_provenance_dir_resolved().join("zcode/native-default-subagent.json")
}

fn read_object(path: &Path) -> Result<Value, String> {
    match fs::read_to_string(path) {
        Ok(text) => {
            let value: Value = serde_json::from_str(&text)
                .map_err(|_| "invalid ZCode configuration".to_string())?;
            if !value.is_object() {
                return Err("ZCode configuration must be an object".into());
            }
            Ok(value)
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(json!({})),
        Err(error) => Err(format!("failed to read ZCode configuration: {error}")),
    }
}

fn read_pin(targets: &ZcodeConfigTargets) -> Result<Option<NativePin>, String> {
    match fs::read_to_string(pin_path()) {
        Ok(text) => {
            let pin: NativePin = serde_json::from_str(&text)
                .map_err(|_| "invalid ZCode native Default subagent state".to_string())?;
            Ok((pin.config_path == targets.v2_config_path).then_some(pin))
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(format!(
            "failed to read ZCode native Default subagent state: {error}"
        )),
    }
}

pub(in crate::gateway) fn native_active(targets: &ZcodeConfigTargets) -> Result<bool, String> {
    Ok(read_pin(targets)?.is_some_and(|pin| pin.active))
}

fn encode_component(value: &str) -> String {
    value
        .bytes()
        .map(|byte| {
            if byte.is_ascii_alphanumeric() || b"-_.!~*'()".contains(&byte) {
                char::from(byte).to_string()
            } else {
                format!("%{byte:02X}")
            }
        })
        .collect()
}

fn decode_component(value: &str) -> String {
    let mut bytes = Vec::new();
    let mut index = 0;
    while index < value.len() {
        if value.as_bytes()[index] == b'%' && index + 2 < value.len() {
            if let Some(byte) = value
                .get(index + 1..index + 3)
                .and_then(|hex| u8::from_str_radix(hex, 16).ok())
            {
                bytes.push(byte);
                index += 3;
                continue;
            }
            return value.to_string();
        }
        bytes.push(value.as_bytes()[index]);
        index += 1;
    }
    String::from_utf8(bytes).unwrap_or_else(|_| value.to_string())
}

fn legacy_selection(model: &str, effort: &str) -> Value {
    let model = model.strip_prefix("custom:").unwrap_or(model);
    let parts = if model.contains('/') {
        model.split_once('/')
    } else if let Some(rest) = model.strip_prefix("builtin:") {
        rest.split_once(':')
            .map(|(provider, model)| (&model[..provider.len() + 8], model))
    } else {
        model.split_once(':')
    };
    let Some((provider, model)) = parts else {
        return Value::Null;
    };
    let mut selection =
        json!({"providerId": decode_component(provider), "modelId": decode_component(model)});
    if !effort.is_empty() {
        selection["options"] = json!({"reasoningLevel": effort});
    }
    selection
}

fn selections(state: &Value) -> Map<String, Value> {
    ZCODE_SPAWN_AGENTS
        .iter()
        .map(|(name, _)| {
            let value = if state.get("builtInModelSelectionOverrides").is_some() {
                state
                    .get("builtInModelSelectionOverrides")
                    .and_then(|map| map.get(*name))
                    .cloned()
                    .unwrap_or(Value::Null)
            } else {
                legacy_selection(
                    state
                        .get("builtInModelOverrides")
                        .and_then(|map| map.get(*name))
                        .and_then(Value::as_str)
                        .unwrap_or_default(),
                    state
                        .get("builtInThoughtLevelOverrides")
                        .and_then(|map| map.get(*name))
                        .and_then(Value::as_str)
                        .unwrap_or_default(),
                )
            };
            ((*name).to_string(), value)
        })
        .collect()
}

fn selector(value: &Value) -> String {
    match (
        value.get("providerId").and_then(Value::as_str),
        value.get("modelId").and_then(Value::as_str),
    ) {
        (Some(provider), Some(model)) => format!("{provider}/{model}"),
        _ => String::new(),
    }
}

fn owned(state: &Value) -> bool {
    selections(state)
        .values()
        .any(|value| is_codexhub_client_model_selector(&selector(value)))
}

fn apply_selections(state: &mut Value, agents: &Map<String, Value>) -> Result<(), String> {
    let root = state
        .as_object_mut()
        .ok_or("ZCode agent state must be an object")?;
    let structured = root.contains_key("builtInModelSelectionOverrides")
        || !root.contains_key("builtInModelOverrides");
    for (name, _) in ZCODE_SPAWN_AGENTS {
        let selection = agents.get(*name).unwrap_or(&Value::Null);
        for key in [
            "builtInModelSelectionOverrides",
            "builtInModelOverrides",
            "builtInThoughtLevelOverrides",
        ] {
            if !root.contains_key(key)
                && (selection.is_null() || structured != (key == "builtInModelSelectionOverrides"))
            {
                continue;
            }
            let map = root
                .entry(key)
                .or_insert_with(|| json!({}))
                .as_object_mut()
                .ok_or("ZCode builtin overrides must be an object")?;
            map.remove(*name);
            if selection.is_null() || structured != (key == "builtInModelSelectionOverrides") {
                continue;
            }
            if structured {
                map.insert((*name).into(), selection.clone());
            } else if key == "builtInModelOverrides" {
                let id = selector(selection);
                let (provider, model) =
                    id.split_once('/').ok_or("invalid ZCode model selection")?;
                map.insert(
                    (*name).into(),
                    json!(format!(
                        "custom:{}:{}",
                        encode_component(provider),
                        encode_component(model)
                    )),
                );
            } else if let Some(effort) = selection
                .pointer("/options/reasoningLevel")
                .and_then(Value::as_str)
            {
                map.insert((*name).into(), json!(effort));
            }
        }
    }
    Ok(())
}

fn native_option(provider: &str, model_id: &str, metadata: &Value) -> NativeSubagentOption {
    let efforts = if metadata
        .pointer("/reasoning/enabled")
        .and_then(Value::as_bool)
        == Some(true)
    {
        metadata
            .pointer("/reasoning/variants")
            .and_then(Value::as_array)
            .map(|values| {
                values
                    .iter()
                    .filter_map(Value::as_str)
                    .map(str::to_string)
                    .collect()
            })
            .unwrap_or_default()
    } else {
        Vec::new()
    };
    let default_effort = metadata
        .pointer("/reasoning/defaultVariant")
        .and_then(Value::as_str)
        .filter(|value| efforts.iter().any(|effort| effort == value))
        .unwrap_or_default()
        .to_string();
    let id = format!("{provider}/{model_id}");
    NativeSubagentOption {
        label: metadata
            .get("name")
            .and_then(Value::as_str)
            .map(|name| format!("{id} · {name}"))
            .unwrap_or_else(|| id.clone()),
        id,
        efforts,
        default_effort,
    }
}

fn model_options(
    targets: &ZcodeConfigTargets,
    state: &Value,
) -> Result<Vec<NativeSubagentOption>, String> {
    let mut options = Vec::new();
    let config = read_object(&targets.v2_config_path)?;
    if let Some(providers) = config.get("provider").and_then(Value::as_object) {
        for (provider_id, provider) in providers {
            if is_codexhub_client_model_selector(&format!("{provider_id}/"))
                || provider.get("enabled").and_then(Value::as_bool) == Some(false)
            {
                continue;
            }
            if let Some(models) = provider.get("models").and_then(Value::as_object) {
                for (model_id, model) in models {
                    if model.get("enabled").and_then(Value::as_bool) != Some(false) {
                        options.push(native_option(provider_id, model_id, model));
                    }
                }
            }
        }
    }
    let cache = read_object(&targets.v2_cache_path)?;
    if let Some(providers) = cache.get("providers").and_then(Value::as_array) {
        for provider in providers {
            let Some(provider_id) = provider.get("id").and_then(Value::as_str) else {
                continue;
            };
            if is_codexhub_client_model_selector(&format!("{provider_id}/"))
                || provider.get("enabled").and_then(Value::as_bool) == Some(false)
            {
                continue;
            }
            if let Some(models) = provider.get("models").and_then(Value::as_array) {
                for model in models {
                    if model.get("enabled").and_then(Value::as_bool) == Some(false) {
                        continue;
                    }
                    if let Some(model_id) = model.get("id").and_then(Value::as_str) {
                        let option = native_option(provider_id, model_id, model);
                        if !options.iter().any(|entry| entry.id == option.id) {
                            options.push(option);
                        }
                    }
                }
            }
        }
    }
    for selection in selections(state).values() {
        let id = selector(selection);
        if !id.is_empty()
            && !is_codexhub_client_model_selector(&id)
            && !options.iter().any(|entry| entry.id == id)
        {
            let effort = selection
                .pointer("/options/reasoningLevel")
                .and_then(Value::as_str)
                .unwrap_or_default()
                .to_string();
            options.push(NativeSubagentOption {
                label: id.clone(),
                id,
                efforts: if effort.is_empty() {
                    vec![]
                } else {
                    vec![effort.clone()]
                },
                default_effort: effort,
            });
        }
    }
    Ok(options)
}

pub fn read_zcode_default_subagent() -> Result<NativeSubagentSettings, String> {
    read_with_targets(&detect_zcode_config_targets())
}

pub(in crate::gateway) fn read_with_targets(
    targets: &ZcodeConfigTargets,
) -> Result<NativeSubagentSettings, String> {
    let state = read_object(&state_path(targets))?;
    let agents = selections(&state);
    let selection = agents.get("general-purpose").unwrap_or(&Value::Null);
    let model = selector(selection);
    Ok(NativeSubagentSettings {
        native: !owned(&state),
        model,
        effort: selection
            .pointer("/options/reasoningLevel")
            .and_then(Value::as_str)
            .unwrap_or_default()
            .to_string(),
        options: model_options(targets, &state)?,
    })
}

pub fn save_zcode_default_subagent(
    model: String,
    effort: String,
    native: bool,
) -> Result<NativeSubagentSettings, String> {
    let _guard = gateway_client_config_write_lock()
        .lock()
        .map_err(|_| "gateway client config write lock is poisoned")?;
    save_with_targets(&detect_zcode_config_targets(), model, effort, native)
}

pub(in crate::gateway) fn save_with_targets(
    targets: &ZcodeConfigTargets,
    model: String,
    effort: String,
    native: bool,
) -> Result<NativeSubagentSettings, String> {
    let mut state = read_object(&state_path(targets))?;
    let mut pin = read_pin(targets)?.unwrap_or(NativePin {
        config_path: targets.v2_config_path.clone(),
        active: false,
        agents: selections(&state),
    });
    if !native && !owned(&state) {
        pin.agents = selections(&state);
    }
    if native {
        if !model.is_empty() {
            let options = model_options(targets, &state)?;
            let option = options
                .iter()
                .find(|option| option.id == model)
                .ok_or("model is not available in ZCode")?;
            if !effort.is_empty() && !option.efforts.contains(&effort) {
                return Err("thinking effort is not available for this ZCode model".into());
            }
        } else if !effort.is_empty() {
            return Err("ZCode inherited model cannot have a thinking effort".into());
        }
        let selection = if model.is_empty() {
            Value::Null
        } else {
            let (provider, model) = model
                .split_once('/')
                .ok_or("invalid ZCode model selection")?;
            let mut value = json!({"providerId": provider, "modelId": model});
            if !effort.is_empty() {
                value["options"] = json!({"reasoningLevel": effort});
            }
            value
        };
        pin.agents = ZCODE_SPAWN_AGENTS
            .iter()
            .map(|(name, _)| ((*name).into(), selection.clone()))
            .collect();
        apply_selections(&mut state, &pin.agents)?;
    }
    pin.active = native;
    let mut paths = vec![state_path(targets), pin_path()];
    paths.extend(
        ZCODE_SPAWN_AGENTS
            .iter()
            .map(|(_, filename)| super::zcode::zcode_agents_dir(targets).join(filename)),
    );
    file_transaction::with_text_file_rollback(&paths, || {
        safe_file::write_text_atomic(
            &pin_path(),
            &serde_json::to_string_pretty(&pin).map_err(|error| error.to_string())?,
        )?;
        if native {
            // Earlier Gateway versions used marked Markdown shadows. Restore only
            // those shadows; real builtin settings live in agents-state.json.
            super::zcode::clear_owned_zcode_agent_files(targets)?;
            safe_file::write_text_atomic(
                &state_path(targets),
                &serde_json::to_string_pretty(&state).map_err(|error| error.to_string())?,
            )?;
        }
        read_with_targets(targets)
    })
    .map_err(|error| error.to_string())
}

pub(in crate::gateway) fn gateway_state_file(
    targets: &ZcodeConfigTargets,
    pin: Option<&ClientDefaultSubagentPin>,
) -> Result<Option<(PathBuf, Option<String>)>, String> {
    let mut state = read_object(&state_path(targets))?;
    let Some(pin) = pin else {
        if !owned(&state) {
            return Ok(None);
        }
        apply_selections(
            &mut state,
            &read_pin(targets)?.map(|pin| pin.agents).unwrap_or_default(),
        )?;
        return Ok(Some((
            state_path(targets),
            Some(serde_json::to_string_pretty(&state).map_err(|error| error.to_string())?),
        )));
    };
    let mut selection = json!({"providerId": pin.client_provider_id, "modelId": pin.short_id});
    if !pin.effort.is_empty() {
        selection["options"] = json!({"reasoningLevel": pin.zcode_thought_level()});
    }
    apply_selections(
        &mut state,
        &ZCODE_SPAWN_AGENTS
            .iter()
            .map(|(name, _)| ((*name).into(), selection.clone()))
            .collect(),
    )?;
    Ok(Some((
        state_path(targets),
        Some(serde_json::to_string_pretty(&state).map_err(|error| error.to_string())?),
    )))
}

pub(in crate::gateway) fn capture_before_publish(
    targets: &ZcodeConfigTargets,
) -> Result<(), String> {
    let state = read_object(&state_path(targets))?;
    if owned(&state) {
        return Ok(());
    }
    let active = native_active(targets)?;
    let pin = NativePin {
        config_path: targets.v2_config_path.clone(),
        active,
        agents: selections(&state),
    };
    safe_file::write_text_atomic(
        &pin_path(),
        &serde_json::to_string_pretty(&pin).map_err(|error| error.to_string())?,
    )
}

pub(in crate::gateway) fn restore_native_state(targets: &ZcodeConfigTargets) -> Result<(), String> {
    let mut state = read_object(&state_path(targets))?;
    if !owned(&state) {
        return Ok(());
    }
    let agents = read_pin(targets)?.map(|pin| pin.agents).unwrap_or_default();
    apply_selections(&mut state, &agents)?;
    safe_file::write_text_atomic(
        &state_path(targets),
        &serde_json::to_string_pretty(&state).map_err(|error| error.to_string())?,
    )
}

pub(in crate::gateway) fn preserve_native_cache(
    path: &Path,
    gateway_text: &str,
) -> Result<String, String> {
    let existing = read_object(path)?;
    let mut next: Value = serde_json::from_str(gateway_text).map_err(|error| error.to_string())?;
    let providers = next
        .get_mut("providers")
        .and_then(Value::as_array_mut)
        .ok_or("invalid ZCode model cache")?;
    if let Some(native) = existing.get("providers").and_then(Value::as_array) {
        providers.extend(
            native
                .iter()
                .filter(|provider| {
                    provider
                        .get("id")
                        .and_then(Value::as_str)
                        .is_some_and(|id| !is_codexhub_client_model_selector(&format!("{id}/")))
                })
                .cloned(),
        );
    }
    serde_json::to_string_pretty(&next)
        .map(|text| format!("{text}\n"))
        .map_err(|error| error.to_string())
}

#[cfg(test)]
mod native_zcode_tests {
    use super::*;
    use crate::gateway::{
        apply_gateway_client_config_isolated, isolated_client_apply_targets,
        validate_isolated_root, with_rollback_provenance_dir_override, IsolatedClientApplyInput,
    };
    use crate::{Provider, Settings};

    fn fixture(root: &Path, legacy: bool) -> ZcodeConfigTargets {
        let targets = ZcodeConfigTargets {
            catalog_path: root.join("zcode/codexhub.json"),
            v2_config_path: root.join("zcode/config.json"),
            v2_cache_path: root.join("zcode/bots-model-cache.v2.json"),
        };
        fs::create_dir_all(targets.v2_config_path.parent().unwrap()).unwrap();
        let config = json!({"mainModel": "native/parent", "provider": {"native": {"apiKey": "fixture-only", "models": {"child": {"name": "Child", "reasoning": {"enabled": true, "variants": ["off", "high"], "defaultVariant": "high"}}, "parent": {}}}}});
        fs::write(&targets.v2_config_path, config.to_string()).unwrap();
        fs::write(
            &targets.v2_cache_path,
            json!({"providers": [{"id": "cache-native", "models": [{"id": "cached"}]}]})
                .to_string(),
        )
        .unwrap();
        let state = if legacy {
            json!({"builtInModelOverrides": {}, "disabledAgentIds": ["user:custom"], "pluginAgentModelOverrides": {"plugin:keep": "unchanged"}})
        } else {
            json!({"builtInModelSelectionOverrides": {}, "disabledAgentIds": ["user:custom"], "pluginAgentModelSelectionOverrides": {"plugin:keep": {"providerId": "foreign", "modelId": "keep"}}})
        };
        fs::write(state_path(&targets), state.to_string()).unwrap();
        let custom = super::super::zcode::zcode_agents_dir(&targets).join("custom.md");
        fs::create_dir_all(custom.parent().unwrap()).unwrap();
        fs::write(
            custom,
            "---\nname: custom\ndescription: keep\nmodel: foreign/custom\n---\n# unchanged body\n",
        )
        .unwrap();
        targets
    }

    fn input() -> IsolatedClientApplyInput {
        let providers: Vec<Provider> =
            toml::from_str::<toml::Value>(include_str!("../../../../config/providers.toml"))
                .unwrap()
                .get("providers")
                .unwrap()
                .clone()
                .try_into()
                .unwrap();
        let provider = providers
            .into_iter()
            .find(|provider| provider.enabled && !provider.models.is_empty())
            .unwrap();
        let model = format!("{}/{}", provider.id, provider.models[0].id);
        IsolatedClientApplyInput {
            client_id: "zcode".into(),
            model: Some(model.clone()),
            settings: Settings {
                include_official_models: false,
                zcode_default_subagent_model: model,
                zcode_default_subagent_reasoning_effort: "high".into(),
                ..Settings::default()
            },
            providers: vec![provider],
            catalog_path: None,
            backup_subdir: None,
        }
    }

    fn restore(targets: &ZcodeConfigTargets, root: &Path) {
        let isolated = crate::gateway::validate_existing_isolated_root(root).unwrap();
        let paths = isolated_client_apply_targets(&isolated, "zcode").unwrap();
        super::super::zcode::restore_zcode_config_with_targets(targets, paths.backup_path())
            .unwrap();
    }

    #[test]
    fn native_zcode_actual_builtin_save_readback_and_reset_preserve_other_state() {
        for legacy in [false, true] {
            let root = std::env::temp_dir().join(format!(
                "codexhub-native-zcode-{}-{}",
                std::process::id(),
                crate::gateway::timestamp_millis()
            ));
            let targets = fixture(root.as_path(), legacy);
            let original_config = fs::read(&targets.v2_config_path).unwrap();
            let original_state = read_object(&state_path(&targets)).unwrap();
            let custom = super::super::zcode::zcode_agents_dir(&targets).join("custom.md");
            let original_custom = fs::read(&custom).unwrap();
            with_rollback_provenance_dir_override(
                Some(root.as_path().join("rollback-provenance")),
                || {
                    let saved =
                        save_with_targets(&targets, "native/child".into(), "high".into(), true)
                            .unwrap();
                    assert_eq!(
                        (saved.model.as_str(), saved.effort.as_str()),
                        ("native/child", "high")
                    );
                    assert!(saved.native);
                    assert_eq!(read_with_targets(&targets).unwrap().model, saved.model);
                    let actual = read_object(&state_path(&targets)).unwrap();
                    assert_eq!(
                        actual["disabledAgentIds"],
                        original_state["disabledAgentIds"]
                    );
                    assert_eq!(
                        actual["pluginAgentModelSelectionOverrides"],
                        original_state["pluginAgentModelSelectionOverrides"]
                    );
                    assert_eq!(
                        actual["pluginAgentModelOverrides"],
                        original_state["pluginAgentModelOverrides"]
                    );
                    for selection in selections(&actual).values() {
                        assert_eq!(selector(selection), "native/child");
                        assert_eq!(
                            selection.pointer("/options/reasoningLevel").unwrap(),
                            "high"
                        );
                    }
                    assert_eq!(fs::read(&targets.v2_config_path).unwrap(), original_config);
                    assert_eq!(fs::read(&custom).unwrap(), original_custom);
                assert!(!saved
                    .options
                        .iter()
                    .any(|option| option.id.starts_with("codexhub")));
                // ZCode's existing custom: identity encoding must retain
                // provider colons and literal model separators.
                let mut config = read_object(&targets.v2_config_path).unwrap();
                config["provider"]["builtin:test"] = json!({"models": {"tag:model$literal": {}}});
                fs::write(&targets.v2_config_path, config.to_string()).unwrap();
                let encoded = save_with_targets(&targets, "builtin:test/tag:model$literal".into(), "".into(), true).unwrap();
                assert_eq!(encoded.model, "builtin:test/tag:model$literal");
                save_with_targets(&targets, "".into(), "".into(), true).unwrap();
                    assert!(selections(&read_object(&state_path(&targets)).unwrap())
                        .values()
                        .all(Value::is_null));
                    assert_eq!(fs::read(&custom).unwrap(), original_custom);
                },
            );
        }
    }

    #[test]
    fn native_zcode_connect_republish_detach_preserves_native_manual_builtin_edits() {
        let root = std::env::temp_dir().join(format!(
            "codexhub-native-zcode-{}-{}",
            std::process::id(),
            crate::gateway::timestamp_millis()
        ));
        let isolated = validate_isolated_root(root.as_path()).unwrap();
        let targets = fixture(root.as_path(), false);
        let input = input();
        with_rollback_provenance_dir_override(
            Some(root.as_path().join("rollback-provenance")),
            || {
                save_with_targets(&targets, "native/child".into(), "high".into(), true).unwrap();
                let mut state = read_object(&state_path(&targets)).unwrap();
                state["builtInModelSelectionOverrides"]["Explore"] =
                    json!({"providerId": "native", "modelId": "parent"});
                fs::write(state_path(&targets), state.to_string()).unwrap();
            },
        );
        for _ in 0..2 {
            apply_gateway_client_config_isolated(&isolated, &input).unwrap();
        }
        with_rollback_provenance_dir_override(
            Some(root.as_path().join("rollback-provenance")),
            || {
                let state = read_object(&state_path(&targets)).unwrap();
                assert_eq!(selector(&selections(&state)["Explore"]), "native/parent");
                assert_eq!(read_with_targets(&targets).unwrap().model, "native/child");
                assert!(read_with_targets(&targets)
                    .unwrap()
                    .options
                    .iter()
                    .any(|option| option.id == "cache-native/cached"));
                // Saving while connected changes the builtin selection immediately.
                save_with_targets(&targets, "native/parent".into(), "".into(), true).unwrap();
                restore(&targets, root.as_path());
                assert_eq!(read_with_targets(&targets).unwrap().model, "native/parent");
            },
        );
    }

    #[test]
    fn native_zcode_gateway_switch_detaches_to_latest_native_and_resets_survive() {
        let root = std::env::temp_dir().join(format!(
            "codexhub-native-zcode-{}-{}",
            std::process::id(),
            crate::gateway::timestamp_millis()
        ));
        let isolated = validate_isolated_root(root.as_path()).unwrap();
        let targets = fixture(root.as_path(), true);
        let input = input();
        with_rollback_provenance_dir_override(
            Some(root.as_path().join("rollback-provenance")),
            || {
                save_with_targets(&targets, "native/child".into(), "high".into(), true).unwrap();
                save_with_targets(&targets, "".into(), "".into(), false).unwrap();
            },
        );
        apply_gateway_client_config_isolated(&isolated, &input).unwrap();
        with_rollback_provenance_dir_override(
            Some(root.as_path().join("rollback-provenance")),
            || {
                assert!(!read_with_targets(&targets).unwrap().native);
                restore(&targets, root.as_path());
                assert_eq!(read_with_targets(&targets).unwrap().model, "native/child");
                save_with_targets(&targets, "".into(), "".into(), true).unwrap();
            },
        );
        apply_gateway_client_config_isolated(&isolated, &input).unwrap();
        with_rollback_provenance_dir_override(
            Some(root.as_path().join("rollback-provenance")),
            || {
                restore(&targets, root.as_path());
                assert!(read_with_targets(&targets).unwrap().model.is_empty());
            },
        );
    }
}
