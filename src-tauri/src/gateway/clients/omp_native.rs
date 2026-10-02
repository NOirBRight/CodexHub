use super::omp::{detect_omp_config_paths, rewrite_omp_bundled_agent_overrides, OmpConfigPaths};
use crate::gateway::{
    client_rollback_provenance_dir_resolved, gateway_client_config_write_lock,
    is_codexhub_client_model_selector, NativeSubagentOption, NativeSubagentSettings,
    OMP_BUNDLED_AGENTS,
};
use crate::{file_transaction, safe_file};
use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};
use std::{
    fs,
    path::{Path, PathBuf},
    process::{Command, Stdio},
    time::Duration,
};

const THINKING_SUFFIXES: &[&str] = &[
    "off", "minimal", "low", "medium", "high", "xhigh", "max", "auto",
];

#[derive(Clone, Serialize, Deserialize)]
struct NativePin {
    config_path: PathBuf,
    active: bool,
    overrides: Map<String, Value>,
}

fn pin_path() -> PathBuf {
    client_rollback_provenance_dir_resolved()
        .join("omp")
        .join("native-default-subagent.json")
}

fn read_pin(path: &Path) -> Result<Option<NativePin>, String> {
    match fs::read_to_string(pin_path()) {
        Ok(text) => {
            let pin: NativePin = serde_json::from_str(&text)
                .map_err(|_| "OMP native Default subagent state is invalid")?;
            Ok((pin.config_path == path).then_some(pin))
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(format!(
            "failed to read OMP native Default subagent: {error}"
        )),
    }
}

fn read_text(path: &Path) -> Result<String, String> {
    match fs::read_to_string(path) {
        Ok(text) => Ok(text),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(String::new()),
        Err(error) => Err(format!("failed to read OMP configuration: {error}")),
    }
}

fn parse(text: &str) -> Result<Value, String> {
    if text.trim().is_empty() {
        return Ok(json!({}));
    }
    let value: Value =
        serde_yaml::from_str(text).map_err(|error| format!("invalid OMP YAML: {error}"))?;
    if !value.is_object() {
        return Err("OMP configuration must be a YAML mapping".into());
    }
    Ok(value)
}

fn bundled_overrides(value: &Value) -> Map<String, Value> {
    OMP_BUNDLED_AGENTS
        .iter()
        .filter_map(|name| {
            value
                .pointer("/task/agentModelOverrides")?
                .get(*name)
                .map(|value| ((*name).to_string(), value.clone()))
        })
        .collect()
}

fn has_gateway_overrides(overrides: &Map<String, Value>) -> bool {
    overrides.values().any(|value| {
        value
            .as_str()
            .is_some_and(is_codexhub_client_model_selector)
            || value.as_array().is_some_and(|models| {
                models
                    .iter()
                    .filter_map(Value::as_str)
                    .any(is_codexhub_client_model_selector)
            })
    })
}

fn split_selector(selector: &str) -> (String, String) {
    match selector.rsplit_once(':') {
        Some((model, effort)) if THINKING_SUFFIXES.contains(&effort) => {
            (model.into(), effort.into())
        }
        _ => (selector.into(), String::new()),
    }
}

fn selection(value: &Value, options: &[NativeSubagentOption]) -> (String, String) {
    let overrides = bundled_overrides(value);
    OMP_BUNDLED_AGENTS
        .iter()
        .find_map(|name| {
            let entry = overrides.get(*name)?;
            let selector = entry
                .as_str()
                .or_else(|| entry.as_array()?.first()?.as_str())?;
            Some(if options.iter().any(|option| option.id == selector) {
                (selector.to_string(), String::new())
            } else {
                split_selector(selector)
            })
        })
        .unwrap_or_default()
}

fn cli_models(paths: &OmpConfigPaths) -> Vec<NativeSubagentOption> {
    let Ok(executable) = which::which("omp") else {
        return Vec::new();
    };
    let mut command = Command::new(executable);
    command
        .args(["models", "--json", "--no-extensions"])
        .env(
            "PI_CODING_AGENT_DIR",
            paths.config_path.parent().unwrap_or(Path::new(".")),
        )
        .stdin(Stdio::null())
        .stderr(Stdio::null());
    if paths.config_path.exists() {
        command.arg("--config").arg(&paths.config_path);
    }
    let Some(output) = crate::gateway::command_output_no_window_with_timeout(
        &mut command,
        Duration::from_secs(10),
    ) else {
        return Vec::new();
    };
    if !output.status.success() {
        return Vec::new();
    }
    let Ok(value) = serde_json::from_slice::<Value>(&output.stdout) else {
        return Vec::new();
    };
    value
        .get("models")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(|model| {
            if model
                .get("kind")
                .and_then(Value::as_str)
                .is_some_and(|kind| kind != "chat")
            {
                return None;
            }
            let id = model.get("selector").and_then(Value::as_str)?.to_string();
            (!is_codexhub_client_model_selector(&id)).then(|| native_option(id, model))
        })
        .collect()
}

fn native_option(id: String, metadata: &Value) -> NativeSubagentOption {
    let mut efforts: Vec<String> = metadata
        .get("thinking")
        .and_then(|thinking| {
            thinking
                .as_array()
                .or_else(|| thinking.get("efforts")?.as_array())
        })
        .map(|levels| {
            levels
                .iter()
                .filter_map(Value::as_str)
                .map(ToOwned::to_owned)
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

fn model_options(
    paths: &OmpConfigPaths,
    config: &Value,
    cli: &[NativeSubagentOption],
) -> Result<Vec<NativeSubagentOption>, String> {
    let models = parse(&read_text(&paths.models_path)?)?;
    let mut options = cli.to_vec();
    if let Some(providers) = models
        .get("providers")
        .and_then(Value::as_object)
        .filter(|_| cli.is_empty())
    {
        for (provider, metadata) in providers {
            if is_codexhub_client_model_selector(&format!("{provider}/")) {
                continue;
            }
            if config
                .get("disabledProviders")
                .and_then(Value::as_array)
                .is_some_and(|disabled| {
                    disabled
                        .iter()
                        .any(|value| value.as_str() == Some(provider))
                })
            {
                continue;
            }
            for model in metadata
                .get("models")
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
            {
                let Some(id) = model.get("id").and_then(Value::as_str) else {
                    continue;
                };
                let id = format!("{provider}/{id}");
                if !options.iter().any(|option| option.id == id) {
                    options.push(native_option(id, model));
                }
            }
        }
    }
    let (model, effort) = selection(config, &options);
    if !model.is_empty()
        && !is_codexhub_client_model_selector(&model)
        && !options.iter().any(|option| option.id == model)
    {
        options.push(NativeSubagentOption {
            id: model.clone(),
            label: model,
            efforts: if effort.is_empty() {
                vec![]
            } else {
                vec![String::new(), effort]
            },
            default_effort: String::new(),
        });
    }
    Ok(options)
}

fn rewrite(text: &str, overrides: &Map<String, Value>) -> Result<String, String> {
    let value = parse(text)?;
    if value
        .get("task")
        .is_some_and(|task| !task.is_null() && !task.is_object())
    {
        return Err("OMP task configuration must be a mapping".into());
    }
    if value
        .pointer("/task/agentModelOverrides")
        .is_some_and(|overrides| !overrides.is_null() && !overrides.is_object())
    {
        return Err("OMP task.agentModelOverrides must be a mapping".into());
    }
    // The conventional block form retains comments and foreign task settings.
    // Inline YAML mappings need to be expanded only within the task section.
    let mut lines: Vec<String> = text.lines().map(ToOwned::to_owned).collect();
    if let Some(start) = lines
        .iter()
        .position(|line| crate::gateway::is_top_level_yaml_key(line, "task"))
    {
        let end = (start + 1..lines.len())
            .find(|index| crate::gateway::is_any_top_level_yaml_key(&lines[*index]))
            .unwrap_or(lines.len());
        let inline = lines[start].split_once(':').is_some_and(|(_, tail)| {
            !tail.trim().is_empty() && !tail.trim_start().starts_with('#')
        }) || lines[start + 1..end].iter().any(|line| {
            line.trim_start()
                .strip_prefix("agentModelOverrides:")
                .is_some_and(|tail| !tail.trim().is_empty() && !tail.trim_start().starts_with('#'))
        });
        if inline {
            let rendered = serde_yaml::to_string(
                &json!({"task":value.get("task").cloned().unwrap_or_else(|| json!({}))}),
            )
            .map_err(|error| error.to_string())?;
            lines.splice(start..end, rendered.lines().map(ToOwned::to_owned));
            let normalized = format!(
                "{}\n",
                lines.join(if text.contains("\r\n") { "\r\n" } else { "\n" })
            );
            return Ok(rewrite_omp_bundled_agent_overrides(&normalized, overrides));
        }
    }
    Ok(rewrite_omp_bundled_agent_overrides(text, overrides))
}

pub(in crate::gateway) fn apply_saved_native(
    path: &Path,
    text: &str,
    detached: bool,
) -> Result<Option<String>, String> {
    let Some(pin) = read_pin(path)? else {
        return Ok(None);
    };
    if !pin.active && !detached {
        return Ok(None);
    }
    if !detached && !has_gateway_overrides(&bundled_overrides(&parse(text)?)) {
        return Ok(Some(text.to_string()));
    }
    Ok(Some(rewrite(text, &pin.overrides)?))
}

pub(in crate::gateway) fn capture_native_before_publish(path: &Path) -> Result<(), String> {
    let Some(mut pin) = read_pin(path)? else {
        return Ok(());
    };
    if !pin.active {
        return Ok(());
    }
    let current = bundled_overrides(&parse(&read_text(path)?)?);
    if has_gateway_overrides(&current) {
        return Ok(());
    }
    pin.overrides = current;
    safe_file::write_text_atomic(
        &pin_path(),
        &serde_json::to_string_pretty(&pin).map_err(|error| error.to_string())?,
    )
}

pub fn read_omp_default_subagent() -> Result<NativeSubagentSettings, String> {
    let paths = detect_omp_config_paths();
    read_with_paths(&paths, &cli_models(&paths))
}

pub(in crate::gateway) fn read_with_paths(
    paths: &OmpConfigPaths,
    cli: &[NativeSubagentOption],
) -> Result<NativeSubagentSettings, String> {
    let config = parse(&read_text(&paths.config_path)?)?;
    let options = model_options(paths, &config, cli)?;
    let (model, effort) = selection(&config, &options);
    Ok(NativeSubagentSettings {
        native: !is_codexhub_client_model_selector(&model),
        model,
        effort,
        options,
    })
}

pub fn save_omp_default_subagent(
    model: String,
    effort: String,
    native: bool,
) -> Result<NativeSubagentSettings, String> {
    let _guard = gateway_client_config_write_lock()
        .lock()
        .map_err(|_| "gateway client config write lock is poisoned")?;
    let paths = detect_omp_config_paths();
    save_with_paths(&paths, model, effort, native, &cli_models(&paths))
}

pub(in crate::gateway) fn save_with_paths(
    paths: &OmpConfigPaths,
    model: String,
    effort: String,
    native: bool,
    cli: &[NativeSubagentOption],
) -> Result<NativeSubagentSettings, String> {
    let text = read_text(&paths.config_path)?;
    let config = parse(&text)?;
    let mut pin = read_pin(&paths.config_path)?.unwrap_or(NativePin {
        config_path: paths.config_path.clone(),
        active: false,
        overrides: Map::new(),
    });
    let next = if native {
        if !model.is_empty() {
            let choices = model_options(paths, &config, cli)?;
            let choice = choices
                .iter()
                .find(|option| option.id == model)
                .ok_or("model is not available in OMP")?;
            if !effort.is_empty() && !choice.efforts.contains(&effort) {
                return Err("thinking level is not available for this OMP model".into());
            }
        } else if !effort.is_empty() {
            return Err("OMP default model cannot have a thinking level".into());
        }
        let selector = if effort.is_empty() {
            model.clone()
        } else {
            format!("{model}:{effort}")
        };
        pin.overrides = if model.is_empty() {
            Map::new()
        } else {
            OMP_BUNDLED_AGENTS
                .iter()
                .map(|name| ((*name).to_string(), json!(selector)))
                .collect()
        };
        Some(rewrite(&text, &pin.overrides)?)
    } else {
        if !pin.active {
            return read_with_paths(paths, cli);
        }
        let current = bundled_overrides(&config);
        if !has_gateway_overrides(&current) {
            pin.overrides = current;
        }
        None
    };
    pin.active = native;
    let state_path = pin_path();
    file_transaction::with_text_file_rollback(
        &[paths.config_path.clone(), state_path.clone()],
        || {
            safe_file::write_text_atomic(
                &state_path,
                &serde_json::to_string_pretty(&pin).map_err(|error| error.to_string())?,
            )?;
            if let Some(next) = &next {
                safe_file::write_text_atomic(&paths.config_path, next)?;
            }
            let readback = read_with_paths(paths, cli)?;
            if native && (readback.model != model || readback.effort != effort) {
                return Err(
                    "OMP Default subagent readback did not match the saved selection".into(),
                );
            }
            Ok(readback)
        },
    )
    .map_err(|error| error.to_string())
}

pub(in crate::gateway) fn restore_saved_native(
    path: &Path,
    live: Option<&str>,
) -> Result<(), String> {
    if read_pin(path)?.is_none() {
        return Ok(());
    }
    let restored = read_text(path)?;
    let text = if let Some(live) = live {
        // Task settings are user-owned except for the seven bundled model
        // overrides. Keep current custom agents and task settings across detach.
        let mut lines: Vec<String> = restored.lines().map(ToOwned::to_owned).collect();
        let live_lines: Vec<String> = live.lines().map(ToOwned::to_owned).collect();
        let task_range = |lines: &[String]| {
            lines
                .iter()
                .position(|line| crate::gateway::is_top_level_yaml_key(line, "task"))
                .map(|start| {
                    let end = (start + 1..lines.len())
                        .find(|index| crate::gateway::is_any_top_level_yaml_key(&lines[*index]))
                        .unwrap_or(lines.len());
                    start..end
                })
        };
        let block = task_range(&live_lines)
            .map(|range| live_lines[range].to_vec())
            .unwrap_or_default();
        if let Some(range) = task_range(&lines) {
            lines.splice(range, block);
        } else {
            lines.extend(block);
        }
        format!(
            "{}\n",
            lines.join(if restored.contains("\r\n") {
                "\r\n"
            } else {
                "\n"
            })
        )
    } else {
        restored
    };
    if let Some(next) = apply_saved_native(path, &text, true)? {
        safe_file::write_text_atomic(path, &next)?;
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::gateway::{
        apply_gateway_client_config_isolated, restore_omp_config_with_paths,
        validate_isolated_root, with_rollback_provenance_dir_override, IsolatedClientApplyInput,
    };
    use crate::{Provider, Settings};

    fn root() -> PathBuf {
        std::env::temp_dir().join(format!(
            "codexhub-native-omp-{}-{}",
            std::process::id(),
            crate::gateway::timestamp_millis()
        ))
    }

    fn fixture(root: &Path) -> OmpConfigPaths {
        let paths = OmpConfigPaths {
            config_path: root.join("omp/config.yml"),
            models_path: root.join("omp/models.yml"),
        };
        fs::create_dir_all(paths.config_path.parent().unwrap()).unwrap();
        fs::write(&paths.config_path, "# keep comment\nmodelRoles:\n  default: native/parent\n  smol: native/small\ntask:\n  maxConcurrency: 3\n  agentModelOverrides:\n    scout: native/before\n    custom-agent: native/custom\ntheme: dark\n").unwrap();
        fs::write(&paths.models_path, "providers:\n  native:\n    apiKey: existing-native-key\n    baseUrl: https://native.example/v1\n    api: openai-responses\n    models:\n      - id: child:cloud\n        name: Child\n        reasoning: true\n        thinking:\n          mode: effort\n          efforts: [low, high]\n      - id: plain\n        reasoning: false\n      - id: literal:max\n        reasoning: false\n").unwrap();
        paths
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
        IsolatedClientApplyInput {
            client_id: "omp".into(),
            model: Some(model.clone()),
            settings: Settings {
                include_official_models: false,
                omp_default_subagent_model: model,
                omp_default_subagent_reasoning_effort: "high".into(),
                ..Settings::default()
            },
            providers: vec![provider],
            catalog_path: None,
            backup_subdir: None,
        }
    }

    #[test]
    fn native_omp_save_readback_preserves_main_auth_models_foreign_task_and_agents() {
        let root = root();
        let paths = fixture(&root);
        let custom_agent = root.join("omp/custom-agent.md");
        fs::write(&custom_agent, "model: native/custom\nuser prompt").unwrap();
        with_rollback_provenance_dir_override(Some(root.join("rollback-provenance")), || {
            let before = parse(&read_text(&paths.config_path).unwrap()).unwrap();
            let models = fs::read(&paths.models_path).unwrap();
            let choices = read_with_paths(&paths, &[]).unwrap();
            let native = choices
                .options
                .iter()
                .find(|choice| choice.id == "native/child:cloud")
                .unwrap();
            assert!(native.efforts.contains(&"high".into()));
            assert!(choices
                .options
                .iter()
                .find(|choice| choice.id == "native/plain")
                .unwrap()
                .efforts
                .is_empty());
            let written = save_with_paths(
                &paths,
                "native/child:cloud".into(),
                "high".into(),
                true,
                &[],
            )
            .unwrap();
            assert!(written.native);
            assert_eq!(written.model, "native/child:cloud");
            assert_eq!(written.effort, "high");
            let actual = parse(&read_text(&paths.config_path).unwrap()).unwrap();
            for name in OMP_BUNDLED_AGENTS {
                assert_eq!(
                    actual["task"]["agentModelOverrides"][*name],
                    "native/child:cloud:high"
                );
            }
            assert_eq!(actual["modelRoles"], before["modelRoles"]);
            assert_eq!(
                actual["task"]["maxConcurrency"],
                before["task"]["maxConcurrency"]
            );
            assert_eq!(
                actual["task"]["agentModelOverrides"]["custom-agent"],
                before["task"]["agentModelOverrides"]["custom-agent"]
            );
            assert!(read_text(&paths.config_path)
                .unwrap()
                .starts_with("# keep comment\n"));
            assert_eq!(fs::read(&paths.models_path).unwrap(), models);
            assert_eq!(
                fs::read_to_string(&custom_agent).unwrap(),
                "model: native/custom\nuser prompt"
            );
            let reopened = read_with_paths(&paths, &[]).unwrap();
            assert_eq!(reopened.model, written.model);
            assert_eq!(reopened.effort, written.effort);
            let literal =
                save_with_paths(&paths, "native/literal:max".into(), "".into(), true, &[]).unwrap();
            assert_eq!(literal.model, "native/literal:max");
            assert!(literal.effort.is_empty());
            let bytes = fs::read(&paths.config_path).unwrap();
            assert!(
                save_with_paths(&paths, "native/plain".into(), "high".into(), true, &[]).is_err()
            );
            assert_eq!(fs::read(&paths.config_path).unwrap(), bytes);
            save_with_paths(&paths, "".into(), "".into(), true, &[]).unwrap();
            let reset = parse(&read_text(&paths.config_path).unwrap()).unwrap();
            assert!(bundled_overrides(&reset).is_empty());
            assert_eq!(
                reset["task"]["agentModelOverrides"]["custom-agent"],
                "native/custom"
            );
            assert_eq!(reset["modelRoles"], before["modelRoles"]);
        });
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn native_omp_lifecycle_retains_native_and_restores_it_after_gateway_pin() {
        let root = root();
        let isolated = validate_isolated_root(&root).unwrap();
        let paths = fixture(&root);
        macro_rules! native {
            ($body:expr) => {
                with_rollback_provenance_dir_override(
                    Some(root.join("rollback-provenance")),
                    || $body,
                )
            };
        }
        {
            native!(save_with_paths(
                &paths,
                "native/child:cloud".into(),
                "low".into(),
                true,
                &[]
            ))
            .unwrap();
            let input = gateway_input();
            for _ in 0..2 {
                apply_gateway_client_config_isolated(&isolated, &input).unwrap();
                let readback = read_with_paths(&paths, &[]).unwrap();
                assert_eq!(readback.model, "native/child:cloud");
                assert_eq!(readback.effort, "low");
            }
            native!(save_with_paths(&paths, "".into(), "".into(), false, &[])).unwrap();
            apply_gateway_client_config_isolated(&isolated, &input).unwrap();
            assert!(!read_with_paths(&paths, &[]).unwrap().native);
            // Native save while connected keeps provider injection/main selection.
            let main =
                parse(&read_text(&paths.config_path).unwrap()).unwrap()["modelRoles"].clone();
            let models = fs::read(&paths.models_path).unwrap();
            native!(save_with_paths(
                &paths,
                "native/child:cloud".into(),
                "high".into(),
                true,
                &[],
            ))
            .unwrap();
            assert_eq!(
                parse(&read_text(&paths.config_path).unwrap()).unwrap()["modelRoles"],
                main
            );
            assert_eq!(fs::read(&paths.models_path).unwrap(), models);
            let live = read_text(&paths.config_path)
                .unwrap()
                .replace("custom-agent: native/custom", "custom-agent: native/later");
            fs::write(&paths.config_path, live).unwrap();
            apply_gateway_client_config_isolated(&isolated, &input).unwrap();
            native!(restore_omp_config_with_paths(
                &paths.config_path,
                &paths.models_path,
                &root.join("backups"),
            ))
            .unwrap();
            let readback = read_with_paths(&paths, &[]).unwrap();
            assert_eq!(readback.model, "native/child:cloud");
            assert_eq!(readback.effort, "high");
            assert_eq!(
                parse(&read_text(&paths.config_path).unwrap()).unwrap()["task"]
                    ["agentModelOverrides"]["custom-agent"],
                "native/later"
            );
            // Gateway detach restores the last independent native setting, not
            // the older rollback snapshot, even when no new native save occurs.
            native!(save_with_paths(&paths, "".into(), "".into(), false, &[])).unwrap();
            apply_gateway_client_config_isolated(&isolated, &input).unwrap();
            native!(restore_omp_config_with_paths(
                &paths.config_path,
                &paths.models_path,
                &root.join("backups"),
            ))
            .unwrap();
            assert_eq!(
                read_with_paths(&paths, &[]).unwrap().model,
                "native/child:cloud"
            );
            // Native edits made outside the Hub remain authoritative on detach.
            native!(save_with_paths(
                &paths,
                "native/child:cloud".into(),
                "high".into(),
                true,
                &[]
            ))
            .unwrap();
            apply_gateway_client_config_isolated(&isolated, &input).unwrap();
            let live = read_text(&paths.config_path)
                .unwrap()
                .replace("native/child:cloud:high", "native/plain");
            fs::write(&paths.config_path, live).unwrap();
            native!(restore_omp_config_with_paths(
                &paths.config_path,
                &paths.models_path,
                &root.join("backups")
            ))
            .unwrap();
            assert_eq!(read_with_paths(&paths, &[]).unwrap().model, "native/plain");
            native!(save_with_paths(&paths, "".into(), "".into(), true, &[])).unwrap();
            apply_gateway_client_config_isolated(&isolated, &input).unwrap();
            native!(restore_omp_config_with_paths(
                &paths.config_path,
                &paths.models_path,
                &root.join("backups"),
            ))
            .unwrap();
            assert!(read_with_paths(&paths, &[]).unwrap().model.is_empty());
        }
        fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn native_omp_save_supports_yaml_inline_mappings_and_prioritized_agent_models() {
        let root = root();
        let paths = fixture(&root);
        with_rollback_provenance_dir_override(Some(root.join("rollback-provenance")), || {
            for text in [
                "modelRoles: {default: native/parent}\ntask: {maxConcurrency: 3, agentModelOverrides: {scout: [native/old, native/fallback], custom-agent: native/custom}}\n",
                "modelRoles:\n  default: native/parent\ntask:\n  agentModelOverrides:\n    scout:\n      - native/old\n      - native/fallback\n    custom-agent:\n      - native/custom\n      - native/custom-fallback\n",
            ] {
                fs::write(&paths.config_path, text).unwrap();
                let before = parse(text).unwrap();
                save_with_paths(&paths, "native/child:cloud".into(), "high".into(), true, &[]).unwrap();
                let actual = parse(&read_text(&paths.config_path).unwrap()).unwrap();
                assert_eq!(actual["modelRoles"], before["modelRoles"]);
                assert_eq!(actual["task"]["agentModelOverrides"]["custom-agent"], before["task"]["agentModelOverrides"]["custom-agent"]);
                assert_eq!(actual["task"]["agentModelOverrides"]["scout"], "native/child:cloud:high");
            }
        });
        fs::remove_dir_all(root).unwrap();
    }
}
