use super::opencode::{detect_opencode_config_path, detect_opencode_executable_path};
use crate::gateway::{
    client_rollback_provenance_dir_resolved, gateway_client_config_write_lock,
    is_codexhub_client_model_selector, NativeSubagentOption, NativeSubagentSettings,
    OPENCODE_SPAWN_AGENTS,
};
use crate::{file_transaction, safe_file};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value};
use std::{
    fs,
    io::Read,
    path::{Path, PathBuf},
    process::{Command, Stdio},
    thread,
    time::{Duration, Instant},
};

#[derive(Debug, Clone, Serialize, Deserialize)]
struct NativePin {
    config_path: PathBuf,
    active: bool,
    agents: Map<String, Value>,
}

fn pin_path() -> PathBuf {
    client_rollback_provenance_dir_resolved()
        .join("opencode")
        .join("native-default-subagent.json")
}

fn read_pin(path: &Path) -> Result<Option<NativePin>, String> {
    match fs::read_to_string(pin_path()) {
        Ok(text) => {
            let pin: NativePin = serde_json::from_str(&text)
                .map_err(|_| "OpenCode native Default subagent state is invalid".to_string())?;
            Ok((pin.config_path == path).then_some(pin))
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(format!(
            "failed to read OpenCode native Default subagent: {error}"
        )),
    }
}

// OpenCode accepts JSONC. Remove comments and trailing commas without touching strings.
pub(in crate::gateway) fn parse_config(text: &str) -> Result<Value, String> {
    let mut bytes = text.as_bytes().to_vec();
    let mut i = 0;
    let mut string = false;
    while i < bytes.len() {
        if string {
            if bytes[i] == b'\\' {
                i += 2;
                continue;
            }
            if bytes[i] == b'"' {
                string = false;
            }
        } else if bytes[i] == b'"' {
            string = true;
        } else if bytes[i] == b'/' && bytes.get(i + 1) == Some(&b'/') {
            while i < bytes.len() && bytes[i] != b'\n' {
                bytes[i] = b' ';
                i += 1;
            }
            continue;
        } else if bytes[i] == b'/' && bytes.get(i + 1) == Some(&b'*') {
            bytes[i] = b' ';
            bytes[i + 1] = b' ';
            i += 2;
            while i < bytes.len() {
                if bytes[i] == b'*' && bytes.get(i + 1) == Some(&b'/') {
                    bytes[i] = b' ';
                    bytes[i + 1] = b' ';
                    i += 2;
                    break;
                }
                bytes[i] = b' ';
                i += 1;
            }
            continue;
        }
        i += 1;
    }
    string = false;
    i = 0;
    while i < bytes.len() {
        if string {
            if bytes[i] == b'\\' {
                i += 2;
                continue;
            }
            if bytes[i] == b'"' {
                string = false;
            }
        } else if bytes[i] == b'"' {
            string = true;
        } else if bytes[i] == b',' {
            let next = bytes[i + 1..]
                .iter()
                .copied()
                .find(|byte| !byte.is_ascii_whitespace());
            if matches!(next, Some(b'}' | b']')) {
                bytes[i] = b' ';
            }
        }
        i += 1;
    }
    let value: Value = serde_json::from_slice(&bytes)
        .map_err(|error| format!("invalid OpenCode configuration: {error}"))?;
    if !value.is_object() {
        return Err("OpenCode configuration must be an object".into());
    }
    Ok(value)
}

fn read_config(path: &Path) -> Result<Value, String> {
    match fs::read_to_string(path) {
        Ok(text) => parse_config(&text),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(Value::Object(Map::new())),
        Err(error) => Err(format!("failed to read OpenCode configuration: {error}")),
    }
}

fn cli_models(path: &Path) -> Vec<NativeSubagentOption> {
    let Some(executable) = detect_opencode_executable_path() else {
        return Vec::new();
    };
    let mut command = if executable
        .extension()
        .is_some_and(|ext| ext.eq_ignore_ascii_case("cmd"))
    {
        let mut command = Command::new("cmd");
        command.arg("/C").arg(executable);
        command
    } else {
        Command::new(executable)
    };
    command.env("OPENCODE_CONFIG", path);
    command
        .args(["models", "--verbose"])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null());
    crate::runtime_paths::configure_no_window(&mut command);
    let Ok(mut child) = command.spawn() else {
        return Vec::new();
    };
    let Some(stdout) = child.stdout.take() else {
        return Vec::new();
    };
    let reader = thread::spawn(move || {
        let mut text = String::new();
        let _ = stdout.take(2 * 1024 * 1024).read_to_string(&mut text);
        text
    });
    let deadline = Instant::now() + Duration::from_secs(3);
    loop {
        match child.try_wait() {
            Ok(Some(status)) if status.success() => break,
            Ok(None) if Instant::now() < deadline => thread::sleep(Duration::from_millis(20)),
            _ => {
                let _ = child.kill();
                let _ = child.wait();
                return Vec::new();
            }
        }
    }
    parse_cli_models(&reader.join().unwrap_or_default())
}

fn parse_cli_models(text: &str) -> Vec<NativeSubagentOption> {
    let mut remaining = text;
    let mut options = Vec::new();
    while let Some((line, rest)) = remaining.split_once('\n') {
        remaining = rest;
        let id = line.trim();
        if !id.contains('/')
            || id.contains(char::is_whitespace)
            || is_codexhub_client_model_selector(id)
        {
            continue;
        }
        let mut stream = serde_json::Deserializer::from_str(remaining).into_iter::<Value>();
        let metadata = match stream.next() {
            Some(Ok(value)) => {
                remaining = &remaining[stream.byte_offset()..];
                value
            }
            _ => Value::Null,
        };
        options.push(native_option(id.to_string(), &metadata));
    }
    options
}

fn native_option(id: String, metadata: &Value) -> NativeSubagentOption {
    let mut efforts: Vec<String> = metadata
        .get("variants")
        .and_then(Value::as_object)
        .map(|variants| {
            variants
                .iter()
                .filter(|(_, value)| value.get("disabled").and_then(Value::as_bool) != Some(true))
                .map(|(name, _)| name.clone())
                .collect()
        })
        .unwrap_or_default();
    if !efforts.is_empty() {
        efforts.insert(0, String::new());
    }
    NativeSubagentOption {
        label: metadata
            .get("name")
            .and_then(Value::as_str)
            .map(|name| format!("{id} · {name}"))
            .unwrap_or_else(|| id.clone()),
        id,
        efforts,
        default_effort: String::new(),
    }
}
fn model_options(value: &Value, cli: &[NativeSubagentOption]) -> Vec<NativeSubagentOption> {
    let mut options = Vec::new();
    if let Some(providers) = value.get("provider").and_then(Value::as_object) {
        for (provider_id, provider) in providers {
            if is_codexhub_client_model_selector(&format!("{provider_id}/")) {
                continue;
            }
            let Some(models) = provider.get("models").and_then(Value::as_object) else {
                continue;
            };
            for (model_id, model) in models {
                let id = format!("{provider_id}/{model_id}");
                options.push(native_option(id, model));
            }
        }
    }
    // `opencode models` lists models available through the client's configured/authenticated providers.
    // Existing native selections also remain selectable when the CLI is unavailable.
    for option in cli {
        if let Some(configured) = options
            .iter_mut()
            .find(|configured| configured.id == option.id)
        {
            if configured.efforts.is_empty() {
                configured.efforts = option.efforts.clone();
            }
        } else {
            options.push(option.clone());
        }
    }
    let mut ids = Vec::new();
    for name in OPENCODE_SPAWN_AGENTS {
        if let Some(id) = value
            .get("agent")
            .and_then(|agent| agent.get(*name))
            .and_then(|agent| agent.get("model"))
            .and_then(Value::as_str)
        {
            ids.push(id.to_string());
        }
    }
    for key in ["model", "small_model"] {
        if let Some(id) = value.get(key).and_then(Value::as_str) {
            ids.push(id.to_string());
        }
    }
    for id in ids {
        if !is_codexhub_client_model_selector(&id)
            && id.contains('/')
            && !options.iter().any(|option| option.id == id)
        {
            options.push(NativeSubagentOption {
                label: id.clone(),
                id,
                efforts: Vec::new(),
                default_effort: String::new(),
            });
        }
    }
    options.retain(|option| {
        let provider = option.id.split('/').next().unwrap_or_default();
        let disabled = value
            .get("disabled_providers")
            .and_then(Value::as_array)
            .is_some_and(|providers| providers.iter().any(|item| item.as_str() == Some(provider)));
        let enabled = value
            .get("enabled_providers")
            .and_then(Value::as_array)
            .is_none_or(|providers| providers.iter().any(|item| item.as_str() == Some(provider)));
        !disabled && enabled && !is_codexhub_client_model_selector(&option.id)
    });
    options
}

fn selection(value: &Value) -> (String, String) {
    OPENCODE_SPAWN_AGENTS
        .iter()
        .find_map(|name| {
            let agent = value.get("agent")?.get(*name)?;
            let model = agent.get("model")?.as_str()?;
            Some((
                model.to_string(),
                agent
                    .get("variant")
                    .and_then(Value::as_str)
                    .unwrap_or_default()
                    .to_string(),
            ))
        })
        .unwrap_or_default()
}

fn apply_pin(value: &mut Value, pin: &NativePin) -> Result<(), String> {
    let root = value
        .as_object_mut()
        .ok_or("OpenCode configuration must be an object")?;
    if pin
        .agents
        .values()
        .all(|agent| agent.as_object().is_none_or(Map::is_empty))
        && !root.contains_key("agent")
    {
        return Ok(());
    }
    let agents = root
        .entry("agent")
        .or_insert_with(|| Value::Object(Map::new()))
        .as_object_mut()
        .ok_or("OpenCode agent configuration must be an object")?;
    for name in OPENCODE_SPAWN_AGENTS {
        let saved = pin.agents.get(*name).and_then(Value::as_object);
        if saved.is_none_or(Map::is_empty) && !agents.contains_key(*name) {
            continue;
        }
        let agent = agents
            .entry((*name).to_string())
            .or_insert_with(|| Value::Object(Map::new()))
            .as_object_mut()
            .ok_or_else(|| format!("OpenCode {name} agent configuration must be an object"))?;
        for key in ["model", "variant"] {
            match saved.and_then(|agent| agent.get(key)) {
                Some(value) => {
                    agent.insert(key.into(), value.clone());
                }
                None => {
                    agent.remove(key);
                }
            }
        }
        if agent.is_empty() {
            agents.remove(*name);
        }
    }
    if agents.is_empty() {
        root.remove("agent");
    }
    Ok(())
}

pub(in crate::gateway) fn apply_saved_native(
    path: &Path,
    value: &mut Value,
    detached: bool,
) -> Result<bool, String> {
    let Some(pin) = read_pin(path)? else {
        return Ok(false);
    };
    if !pin.active && !detached {
        return Ok(false);
    }
    // Native settings are user-owned: a manual edit after Save takes precedence
    // over both the old native snapshot and the stale Gateway preference.
    if !detached
        && value.as_object().is_some_and(|root| {
            !crate::gateway::json_spawn_models_are_owned(root, OPENCODE_SPAWN_AGENTS)
        })
    {
        return Ok(true);
    }
    apply_pin(value, &pin)?;
    Ok(true)
}

pub fn read_opencode_default_subagent() -> Result<NativeSubagentSettings, String> {
    let path = detect_opencode_config_path().ok_or("OpenCode config path is unavailable")?;
    read_opencode_default_subagent_with_path(&path, &cli_models(&path))
}

pub(in crate::gateway) fn read_opencode_default_subagent_with_path(
    path: &Path,
    cli: &[NativeSubagentOption],
) -> Result<NativeSubagentSettings, String> {
    let value = read_config(path)?;
    let (model, effort) = selection(&value);
    Ok(NativeSubagentSettings {
        native: !is_codexhub_client_model_selector(&model),
        model,
        effort,
        options: model_options(&value, cli),
    })
}

pub fn save_opencode_default_subagent(
    model: String,
    effort: String,
    native: bool,
) -> Result<NativeSubagentSettings, String> {
    let _guard = gateway_client_config_write_lock()
        .lock()
        .map_err(|_| "gateway client config write lock is poisoned")?;
    let path = detect_opencode_config_path().ok_or("OpenCode config path is unavailable")?;
    save_opencode_default_subagent_with_path(&path, model, effort, native, &cli_models(&path))
}

pub(in crate::gateway) fn save_opencode_default_subagent_with_path(
    path: &Path,
    model: String,
    effort: String,
    native: bool,
    cli: &[NativeSubagentOption],
) -> Result<NativeSubagentSettings, String> {
    if !native {
        capture_native_before_publish(path)?;
    }
    let mut value = read_config(path)?;
    let mut pin = read_pin(path)?.unwrap_or(NativePin {
        config_path: path.to_path_buf(),
        active: false,
        agents: Map::new(),
    });
    if native {
        if !model.is_empty() {
            let options = model_options(&value, cli);
            let option = options
                .iter()
                .find(|option| option.id == model)
                .ok_or("model is not available in OpenCode")?;
            if !effort.is_empty() && !option.efforts.contains(&effort) {
                return Err("variant is not available for this OpenCode model".into());
            }
        } else if !effort.is_empty() {
            return Err("OpenCode default model cannot have a variant".into());
        }
        let mut target = Map::new();
        if !model.is_empty() {
            target.insert("model".into(), Value::String(model));
        }
        if !effort.is_empty() {
            target.insert("variant".into(), Value::String(effort));
        }
        pin.agents = OPENCODE_SPAWN_AGENTS
            .iter()
            .map(|name| ((*name).to_string(), Value::Object(target.clone())))
            .collect();
        apply_pin(&mut value, &pin)?;
    } else if !pin.active {
        return read_opencode_default_subagent_with_path(path, cli);
    }
    pin.active = native;
    let state_path = pin_path();
    let state_text = serde_json::to_string_pretty(&pin).map_err(|error| error.to_string())?;
    let config_text = serde_json::to_string_pretty(&value).map_err(|error| error.to_string())?;
    file_transaction::with_text_file_rollback(&[path.to_path_buf(), state_path.clone()], || {
        safe_file::write_text_atomic(&state_path, &state_text)?;
        if native {
            safe_file::write_text_atomic(path, &format!("{config_text}\n"))?;
        }
        read_opencode_default_subagent_with_path(path, cli)
    })
    .map_err(|error| error.to_string())
}

pub(in crate::gateway) fn capture_native_before_publish(path: &Path) -> Result<(), String> {
    let Some(mut pin) = read_pin(path)? else {
        return Ok(());
    };
    let value = read_config(path)?;
    if value.as_object().is_some_and(|root| {
        crate::gateway::json_spawn_models_are_owned(root, OPENCODE_SPAWN_AGENTS)
    }) {
        return Ok(());
    }
    pin.agents = OPENCODE_SPAWN_AGENTS
        .iter()
        .map(|name| {
            let agent = value.get("agent").and_then(|agents| agents.get(*name));
            let mut saved = Map::new();
            for key in ["model", "variant"] {
                if let Some(value) = agent.and_then(|agent| agent.get(key)) {
                    saved.insert(key.into(), value.clone());
                }
            }
            ((*name).to_string(), Value::Object(saved))
        })
        .collect();
    safe_file::write_text_atomic(
        &pin_path(),
        &serde_json::to_string_pretty(&pin).map_err(|error| error.to_string())?,
    )
}

pub(in crate::gateway) fn restored_native_text(path: &Path, text: &str) -> Result<String, String> {
    let mut value = parse_config(text)?;
    if apply_saved_native(path, &mut value, true)? {
        return serde_json::to_string_pretty(&value)
            .map(|text| format!("{text}\n"))
            .map_err(|error| error.to_string());
    }
    Ok(text.to_string())
}

#[cfg(test)]
mod native_opencode_tests {
    use super::*;
    use crate::gateway::{
        apply_gateway_client_config_isolated, isolated_client_apply_targets,
        restore_opencode_config_with_backup_roots, validate_isolated_root,
        with_rollback_provenance_dir_override, BackupChannel, IsolatedClientApplyInput,
    };
    use crate::{Provider, Settings};

    fn root() -> PathBuf {
        std::env::temp_dir().join(format!(
            "codexhub-native-opencode-{}-{}",
            std::process::id(),
            crate::gateway::timestamp_millis()
        ))
    }

    fn fixture(path: &Path) -> Value {
        let value = serde_json::json!({
            "model": "native/parent", "small_model": "native/small", "theme": "unchanged",
            "provider": { "native": { "options": { "apiKey": "fixture-native-key", "baseURL": "https://native.example/v1" },
                "models": { "child": { "variants": { "low": {}, "high": {}, "hidden": {"disabled": true} } } } } },
            "agent": {
                "build": { "model": "native/build" }, "plan": { "model": "native/plan" },
                "general": { "model": "native/before", "prompt": "keep prompt", "temperature": 0.2 },
                "custom": { "model": "native/custom", "mode": "subagent" }
            }
        });
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, serde_json::to_string_pretty(&value).unwrap()).unwrap();
        value
    }

    fn gateway_input() -> IsolatedClientApplyInput {
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
        let settings = Settings {
            include_official_models: false,
            opencode_default_subagent_model: model.clone(),
            opencode_default_subagent_reasoning_effort: "high".into(),
            ..Settings::default()
        };
        IsolatedClientApplyInput {
            client_id: "opencode".into(),
            model: Some(model),
            settings,
            providers: vec![provider],
            catalog_path: None,
            backup_subdir: None,
        }
    }

    #[test]
    fn native_opencode_save_readback_preserves_provider_main_and_custom_agents() {
        let root = root();
        let path = root.join("opencode.json");
        let original = fixture(&path);
        with_rollback_provenance_dir_override(Some(root.join("provenance")), || {
            let saved = save_opencode_default_subagent_with_path(
                &path,
                "native/child".into(),
                "high".into(),
                true,
                &[],
            )
            .unwrap();
            assert!(saved.native);
            assert_eq!(
                (saved.model.as_str(), saved.effort.as_str()),
                ("native/child", "high")
            );
            let readback = read_opencode_default_subagent_with_path(&path, &[]).unwrap();
            assert_eq!(readback.model, saved.model);
            let actual = read_config(&path).unwrap();
            for key in ["model", "small_model", "provider", "theme"] {
                assert_eq!(actual[key], original[key]);
            }
            for name in ["build", "plan", "custom"] {
                assert_eq!(actual["agent"][name], original["agent"][name]);
            }
            for name in OPENCODE_SPAWN_AGENTS {
                assert_eq!(actual["agent"][*name]["model"], "native/child");
                assert_eq!(actual["agent"][*name]["variant"], "high");
            }
            assert_eq!(actual["agent"]["general"]["prompt"], "keep prompt");
            assert_eq!(actual["agent"]["general"]["temperature"], 0.2);
            assert!(!actual["provider"]
                .as_object()
                .unwrap()
                .keys()
                .any(|id| id.starts_with("codexhub")));
            assert_eq!(
                readback
                    .options
                    .iter()
                    .find(|option| option.id == "native/child")
                    .unwrap()
                    .efforts,
                ["", "low", "high"]
            );
        });
    }

    #[test]
    fn native_opencode_saves_survive_stale_gateway_connect_republish_and_detach() {
        let root = root();
        let isolated = validate_isolated_root(&root).unwrap();
        let path = root.join("opencode/opencode.json");
        fixture(&path);
        let input = gateway_input();
        with_rollback_provenance_dir_override(Some(root.join("rollback-provenance")), || {
            save_opencode_default_subagent_with_path(
                &path,
                "native/child".into(),
                "high".into(),
                true,
                &[],
            )
            .unwrap();
        });
        apply_gateway_client_config_isolated(&isolated, &input).unwrap();
        assert_eq!(
            read_config(&path).unwrap()["agent"]["general"]["model"],
            "native/child"
        );
        // A connected native save must replace the old rollback choice as well.
        with_rollback_provenance_dir_override(Some(root.join("rollback-provenance")), || {
            save_opencode_default_subagent_with_path(
                &path,
                "native/parent".into(),
                String::new(),
                true,
                &[],
            )
            .unwrap();
        });
        apply_gateway_client_config_isolated(&isolated, &input).unwrap();
        let targets = isolated_client_apply_targets(&isolated, "opencode").unwrap();
        with_rollback_provenance_dir_override(Some(root.join("rollback-provenance")), || {
            restore_opencode_config_with_backup_roots(
                &path,
                &[(targets.backup_path().to_path_buf(), BackupChannel::Stable)],
            )
            .unwrap();
        });
        let actual = read_config(&path).unwrap();
        assert_eq!(actual["agent"]["general"]["model"], "native/parent");
        assert!(actual["agent"]["general"].get("variant").is_none());
        assert_eq!(actual["model"], "native/parent");
        assert_eq!(actual["agent"]["custom"]["model"], "native/custom");
        assert!(actual["provider"].get("codexhub").is_none());
    }

    #[test]
    fn native_opencode_gateway_pin_detaches_to_latest_native_and_default_reset_survives() {
        let root = root();
        let isolated = validate_isolated_root(&root).unwrap();
        let path = root.join("opencode/opencode.json");
        fixture(&path);
        let input = gateway_input();
        apply_gateway_client_config_isolated(&isolated, &input).unwrap();
        with_rollback_provenance_dir_override(Some(root.join("rollback-provenance")), || {
            save_opencode_default_subagent_with_path(
                &path,
                "native/child".into(),
                "low".into(),
                true,
                &[],
            )
            .unwrap();
            save_opencode_default_subagent_with_path(
                &path,
                String::new(),
                String::new(),
                false,
                &[],
            )
            .unwrap();
        });
        apply_gateway_client_config_isolated(&isolated, &input).unwrap();
        let gateway = read_config(&path).unwrap();
        assert!(gateway["agent"]["general"]["model"]
            .as_str()
            .unwrap()
            .starts_with("codexhub"));
        assert!(gateway["agent"]["general"].get("variant").is_none());
        let targets = isolated_client_apply_targets(&isolated, "opencode").unwrap();
        with_rollback_provenance_dir_override(Some(root.join("rollback-provenance")), || {
            restore_opencode_config_with_backup_roots(
                &path,
                &[(targets.backup_path().to_path_buf(), BackupChannel::Stable)],
            )
            .unwrap();
            assert_eq!(
                read_config(&path).unwrap()["agent"]["general"]["model"],
                "native/child"
            );
            let reset = save_opencode_default_subagent_with_path(
                &path,
                String::new(),
                String::new(),
                true,
                &[],
            )
            .unwrap();
            assert_eq!(reset.model, "");
        });
        apply_gateway_client_config_isolated(&isolated, &input).unwrap();
        with_rollback_provenance_dir_override(Some(root.join("rollback-provenance")), || {
            restore_opencode_config_with_backup_roots(
                &path,
                &[(targets.backup_path().to_path_buf(), BackupChannel::Stable)],
            )
            .unwrap();
        });
        let actual = read_config(&path).unwrap();
        for name in OPENCODE_SPAWN_AGENTS {
            assert!(actual["agent"][*name].get("model").is_none());
        }
        assert_eq!(actual["agent"]["general"]["prompt"], "keep prompt");
        assert_eq!(actual["agent"]["custom"]["model"], "native/custom");
    }

    #[test]
    fn native_opencode_inactive_gateway_pin_restores_latest_disconnected_native_edit() {
        let root = root();
        let isolated = validate_isolated_root(&root).unwrap();
        let path = root.join("opencode/opencode.json");
        fixture(&path);
        let input = gateway_input();
        let targets = isolated_client_apply_targets(&isolated, "opencode").unwrap();
        let restore = || restore_opencode_config_with_backup_roots(&path, &[(targets.backup_path().to_path_buf(), BackupChannel::Stable)]).unwrap();
        with_rollback_provenance_dir_override(Some(root.join("rollback-provenance")), || {
            save_opencode_default_subagent_with_path(&path, "native/child".into(), "high".into(), true, &[]).unwrap();
            save_opencode_default_subagent_with_path(&path, "".into(), "".into(), false, &[]).unwrap();
        });
        apply_gateway_client_config_isolated(&isolated, &input).unwrap();
        with_rollback_provenance_dir_override(Some(root.join("rollback-provenance")), restore);
        let mut manual = read_config(&path).unwrap();
        manual["agent"]["general"]["model"] = serde_json::json!("native/manually-changed");
        manual["agent"]["general"]["variant"] = serde_json::json!("low");
        fs::write(&path, manual.to_string()).unwrap();
        apply_gateway_client_config_isolated(&isolated, &input).unwrap();
        assert!(read_config(&path).unwrap()["agent"]["general"]["model"].as_str().unwrap().starts_with("codexhub"));
        with_rollback_provenance_dir_override(Some(root.join("rollback-provenance")), restore);
        assert_eq!(read_config(&path).unwrap()["agent"]["general"]["model"], "native/manually-changed");
        assert_eq!(read_config(&path).unwrap()["agent"]["general"]["variant"], "low");
    }

    #[test]
    fn native_opencode_manual_native_edits_take_precedence_on_connect_and_detach() {
        let root = root();
        let isolated = validate_isolated_root(&root).unwrap();
        let path = root.join("opencode/opencode.json");
        fixture(&path);
        with_rollback_provenance_dir_override(Some(root.join("rollback-provenance")), || {
            save_opencode_default_subagent_with_path(
                &path,
                "native/child".into(),
                "high".into(),
                true,
                &[],
            )
            .unwrap();
        });
        let mut manual = read_config(&path).unwrap();
        manual["agent"]["general"]["model"] = Value::String("native/manually-changed".into());
        manual["agent"]["general"]["variant"] = Value::String("low".into());
        fs::write(&path, serde_json::to_string_pretty(&manual).unwrap()).unwrap();
        let input = gateway_input();
        apply_gateway_client_config_isolated(&isolated, &input).unwrap();
        assert_eq!(
            read_config(&path).unwrap()["agent"]["general"]["model"],
            "native/manually-changed"
        );
        let targets = isolated_client_apply_targets(&isolated, "opencode").unwrap();
        with_rollback_provenance_dir_override(Some(root.join("rollback-provenance")), || {
            save_opencode_default_subagent_with_path(
                &path,
                String::new(),
                String::new(),
                false,
                &[],
            )
            .unwrap();
        });
        apply_gateway_client_config_isolated(&isolated, &input).unwrap();
        with_rollback_provenance_dir_override(Some(root.join("rollback-provenance")), || {
            restore_opencode_config_with_backup_roots(
                &path,
                &[(targets.backup_path().to_path_buf(), BackupChannel::Stable)],
            )
            .unwrap();
        });
        let actual = read_config(&path).unwrap();
        assert_eq!(actual["agent"], manual["agent"]);
    }

    #[test]
    fn native_opencode_jsonc_and_native_catalog_do_not_require_gateway_models() {
        let root = root();
        let path = root.join("opencode.jsonc");
        fs::create_dir_all(&root).unwrap();
        fs::write(&path, "{\n// own config\n\"model\":\"native/parent\",\n\"theme\":\"https://example/*literal*/\",\n}").unwrap();
        let cli = parse_cli_models("native/child\n{\"name\":\"Child\",\"variants\":{\"high\":{}}}\ncodexhub/native\n{\"name\":\"Injected\"}\n");
        with_rollback_provenance_dir_override(Some(root.join("provenance")), || {
            let saved = save_opencode_default_subagent_with_path(
                &path,
                "native/child".into(),
                "high".into(),
                true,
                &cli,
            )
            .unwrap();
            assert_eq!(saved.model, "native/child");
            assert!(!saved
                .options
                .iter()
                .any(|option| option.id.starts_with("codexhub")));
            assert_eq!(
                read_config(&path).unwrap()["theme"],
                "https://example/*literal*/"
            );
            let original = fs::read_to_string(&path).unwrap();
            assert!(save_opencode_default_subagent_with_path(
                &path,
                "native/child".into(),
                "unsupported".into(),
                true,
                &cli
            )
            .is_err());
            assert_eq!(fs::read_to_string(&path).unwrap(), original);
        });
    }
}
