use super::dsh::dsh_executable_path;
use crate::gateway::{
    gateway_client_config_write_lock, NativeSubagentOption, NativeSubagentSettings,
};
use crate::{file_transaction, safe_file};
use serde_yaml::Value;
use std::{
    fs,
    io::Read,
    path::{Path, PathBuf},
    process::{Command, Stdio},
    thread,
    time::{Duration, Instant},
};

const TARGETS: [&str; 2] = ["tool-subagent", "tool-subagent-fork"];
const BEGIN: &str = "# BEGIN CODEXHUB DSH HEADLESS SUBAGENT";
const END: &str = "# END CODEXHUB DSH HEADLESS SUBAGENT";

fn home() -> Result<PathBuf, String> {
    std::env::var_os("DSH_HOME")
        .filter(|v| !v.is_empty())
        .map(PathBuf::from)
        .or_else(|| crate::injection::dsh_descriptor().client_home())
        .ok_or_else(|| "DSH home is unavailable".into())
}

fn composed(home: &Path) -> Result<String, String> {
    let executable = dsh_executable_path().ok_or("DSH is not installed")?;
    let mut command = Command::new(executable);
    command
        .env("DSH_HOME", home)
        .args(["--profile", "headless", "--dump-config"]);
    native_probe(command).map(|text| text.replace("\r\n", "\n"))
}

fn native_probe(mut command: Command) -> Result<String, String> {
    crate::runtime_paths::configure_no_window(&mut command);
    command
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null());
    let mut child = command
        .spawn()
        .map_err(|_| "DSH native configuration probe could not start")?;
    let stdout = child
        .stdout
        .take()
        .ok_or("DSH native probe stdout is unavailable")?;
    let reader = thread::spawn(move || {
        let mut text = String::new();
        stdout
            .take(2 * 1024 * 1024)
            .read_to_string(&mut text)
            .map(|_| text)
    });
    let deadline = Instant::now() + Duration::from_secs(20);
    loop {
        match child.try_wait() {
            Ok(Some(status)) if status.success() => break,
            Ok(None) if Instant::now() < deadline => thread::sleep(Duration::from_millis(20)),
            _ => {
                let _ = child.kill();
                let _ = child.wait();
                return Err("DSH native configuration probe failed or timed out".into());
            }
        }
    }
    reader
        .join()
        .map_err(|_| "DSH native probe reader failed")?
        .map_err(|_| "DSH native probe output cannot be read".into())
}

// Read only the known tool rows: other Cordis rows may contain executable !!js tags.
fn row<'a>(dump: &'a str, id: &str) -> Result<&'a str, String> {
    let marker = format!("- id: {id}\n");
    let start = dump
        .find(&marker)
        .ok_or_else(|| format!("DSH Headless is missing {id}"))?;
    let tail = &dump[start..];
    Ok(&tail[..tail.find("\n- id: ").unwrap_or(tail.len())])
}

fn selections(dump: &str) -> Result<Vec<String>, String> {
    let mut selected = Vec::new();
    for id in TARGETS {
        let value: Value = serde_yaml::from_str(row(dump, id)?)
            .map_err(|_| "DSH subagent configuration cannot be read")?;
        let value = &value[0];
        let options = &value["config"]["agentOptions"];
        let model = match (options["provider"].as_str(), options["model"].as_str()) {
            (Some(provider), Some(model)) => format!("{provider}/{model}"),
            _ => String::new(),
        };
        selected.push(model);
    }
    Ok(selected)
}

fn settings(home: &Path, dump: &str) -> Result<NativeSubagentSettings, String> {
    let model = selections(dump)?.into_iter().next().unwrap_or_default();
    let text = match fs::read_to_string(home.join("settings.yaml")) {
        Ok(text) => text,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => String::new(),
        Err(_) => return Err("DSH settings.yaml cannot be read".into()),
    };
    let value: Value = serde_yaml::from_str(&text).map_err(|_| "DSH settings.yaml is invalid")?;
    let mut options = Vec::new();
    let mut add = |provider: &str, id: &str, label: &str| {
        options.push(NativeSubagentOption {
            id: format!("{provider}/{id}"),
            label: format!("{provider} / {label}"),
            efforts: vec![],
            default_effort: String::new(),
        });
    };
    let pi = adapter_settings(&value, dump, "llm-pi-ai");
    let deepseek = adapter_settings(&value, dump, "llm-deepseek");
    let mut builtin_providers = Vec::new();
    if let Some(providers) = pi["providers"].as_mapping() {
        for (provider, config) in providers {
            let Some(provider) = provider.as_str() else {
                continue;
            };
            if provider == "codexhub" {
                continue;
            }
            if let Some(models) = config["models"].as_sequence() {
                for item in models {
                    if let Some(id) = item["id"].as_str() {
                        add(provider, id, item["name"].as_str().unwrap_or(id));
                    }
                }
            } else {
                builtin_providers.push(provider.to_string());
            }
        }
    }
    if let Some(models) = deepseek["models"].as_sequence() {
        for item in models {
            if let Some(id) = item["id"].as_str() {
                add("deepseek", id, item["name"].as_str().unwrap_or(id));
            }
        }
    }
    if !builtin_providers.is_empty() || deepseek["models"].as_sequence().is_none() {
        let executable = dsh_executable_path().ok_or("DSH is not installed")?;
        let adjacent_node = executable.parent().map(|parent| parent.join("node.exe"));
        let mut command = Command::new(
            adjacent_node
                .filter(|path| path.is_file())
                .unwrap_or_else(|| PathBuf::from("node")),
        );
        command.args([
            "--conditions=import",
            "--input-type=module",
            "-e",
            include_str!("dsh_native_models.mjs"),
        ]);
        command.arg(executable).arg(serde_json::json!({"providers": builtin_providers, "deepseek": deepseek["models"].as_sequence().is_none()}).to_string());
        let metadata: serde_json::Value = serde_json::from_str(&native_probe(command)?)
            .map_err(|_| "DSH native model catalog cannot be read")?;
        for item in metadata
            .as_array()
            .ok_or("DSH native model catalog is invalid")?
        {
            if let (Some(provider), Some(id), Some(name)) = (
                item["provider"].as_str(),
                item["id"].as_str(),
                item["name"].as_str(),
            ) {
                add(provider, id, name);
            }
        }
    }
    if !model.is_empty() && !options.iter().any(|option| option.id == model) {
        options.push(NativeSubagentOption {
            id: model.clone(),
            label: model.clone(),
            efforts: vec![],
            default_effort: String::new(),
        });
    }
    Ok(NativeSubagentSettings {
        model,
        effort: String::new(),
        native: true,
        options,
    })
}

// DSH settings namespaces recursively overlay their Cordis base config.
fn adapter_settings(settings: &Value, dump: &str, id: &str) -> Value {
    fn overlay(base: &mut Value, patch: &Value) {
        if let (Some(base), Some(patch)) = (base.as_mapping_mut(), patch.as_mapping()) {
            for (key, value) in patch {
                overlay(base.entry(key.clone()).or_insert(Value::Null), value);
            }
        } else {
            *base = patch.clone();
        }
    }
    let mut base = row(dump, id)
        .ok()
        .and_then(|row| serde_yaml::from_str::<Value>(row).ok())
        .map(|row| row[0]["config"].clone())
        .unwrap_or(Value::Null);
    if !settings[id].is_null() {
        overlay(&mut base, &settings[id]);
    }
    base
}

pub fn read_dsh_headless_default_subagent() -> Result<NativeSubagentSettings, String> {
    let _guard = gateway_client_config_write_lock()
        .lock()
        .map_err(|_| "client configuration lock poisoned")?;
    let home = home()?;
    settings(&home, &composed(&home)?)
}

// Keep existing rows byte-for-byte. Cordis replaces config at row granularity, so
// overlay the client's composed tool configs, changing only provider/model.
fn patch_rows(dump: &str, model: &str) -> Result<String, String> {
    let mut patch = String::new();
    for id in TARGETS {
        let mut value: Value = serde_yaml::from_str(row(dump, id)?)
            .map_err(|_| "DSH subagent configuration cannot be edited")?;
        let config = value[0]["config"]
            .as_mapping_mut()
            .ok_or("DSH subagent config is not a mapping")?;
        let key = Value::String("agentOptions".into());
        let mut options = match config.remove(&key) {
            Some(Value::Mapping(options)) => options,
            None | Some(Value::Null) => Default::default(),
            Some(_) => return Err("DSH subagent agentOptions is not a mapping".into()),
        };
        for key in ["provider", "model"] {
            options.remove(Value::String(key.into()));
        }
        if !model.is_empty() {
            let (provider, model) = model
                .split_once('/')
                .ok_or("DSH native model requires provider/model")?;
            options.insert(
                Value::String("provider".into()),
                Value::String(provider.into()),
            );
            options.insert(Value::String("model".into()), Value::String(model.into()));
        }
        let raw = row(dump, id)?;
        let mut lines: Vec<&str> = raw.lines().collect();
        // Preserve all other config scalars and executable YAML tags verbatim.
        if let Some(start) = lines
            .iter()
            .position(|line| line.starts_with("    agentOptions:"))
        {
            let end = lines
                .iter()
                .enumerate()
                .skip(start + 1)
                .find(|(_, line)| !line.trim().is_empty() && !line.starts_with("      "))
                .map(|(index, _)| index)
                .unwrap_or(lines.len());
            lines.drain(start..end);
        }
        let insertion = lines
            .iter()
            .position(|line| *line == "  config:")
            .ok_or("DSH subagent config is not a block mapping")?
            + 1;
        let mut rendered = lines[..insertion].join("\n");
        rendered.push('\n');
        if !options.is_empty() {
            rendered.push_str("    agentOptions:\n");
            let yaml = serde_yaml::to_string(&options)
                .map_err(|_| "DSH child options cannot be serialized")?;
            for line in yaml.lines() {
                rendered.push_str("      ");
                rendered.push_str(line);
                rendered.push('\n');
            }
        }
        rendered.push_str(&lines[insertion..].join("\n"));
        rendered.push('\n');
        patch.push_str(&rendered);
    }
    Ok(patch)
}

pub fn save_dsh_headless_default_subagent(model: String) -> Result<NativeSubagentSettings, String> {
    let _guard = gateway_client_config_write_lock()
        .lock()
        .map_err(|_| "client configuration lock poisoned")?;
    let home = home()?;
    save_with_home(&home, model, || composed(&home))
}

pub(in crate::gateway) fn save_with_home(
    home: &Path,
    model: String,
    readback: impl Fn() -> Result<String, String>,
) -> Result<NativeSubagentSettings, String> {
    let dump = readback()?;
    let patch = patch_rows(&dump, &model)?;
    let path = home.join("profiles/headless/cordis.patch.yml");
    let original = match fs::read_to_string(&path) {
        Ok(text) => text,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => String::new(),
        Err(_) => return Err("DSH Headless patch cannot be read".into()),
    };
    let mut text = original;
    if let Some(start) = text.find(BEGIN) {
        let end = text[start..]
            .find(END)
            .ok_or("DSH Headless managed block is incomplete")?
            + start
            + END.len();
        text.replace_range(start..end, &format!("{BEGIN}\n{patch}{END}"));
    } else {
        if !text.is_empty() && !text.ends_with('\n') {
            text.push('\n');
        }
        text.push_str(&format!("{BEGIN}\n{patch}{END}\n"));
    }
    file_transaction::with_text_file_rollback(&[path.clone()], || {
        safe_file::write_text_atomic(&path, &text)?;
        let dump = readback()?;
        let readback = settings(home, &dump)?;
        if selections(&dump)?
            .iter()
            .any(|selection| selection != &model)
        {
            return Err(
                "DSH Headless did not apply the native child model; configuration restored".into(),
            );
        }
        Ok(readback)
    })
    .map_err(|error| error.to_string())
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::injection::{dsh_connect, dsh_disconnect, MaskedSecret, ReadbackExpectation};

    const BASE: &str = "- id: tool-subagent\n  name: '@deepseek-ai/dsh-tool-subagent'\n  config:\n    provider: spawn\n    toolName: subagent\n    persona: !!js fixturePersona\n    toolFilter:\n      deny: [custom-tool]\n    agentOptions:\n      maxTokens: 8192\n- id: tool-subagent-fork\n  name: '@deepseek-ai/dsh-tool-subagent'\n  config:\n    provider: fork\n    toolName: subagent_fork\n    backgroundMode: one-shot\n";

    fn fixture() -> PathBuf {
        let root = std::env::temp_dir().join(format!(
            "codexhub-native-dsh-{}-{}",
            std::process::id(),
            crate::gateway::timestamp_millis()
        ));
        fs::create_dir_all(root.as_path().join("profiles/headless")).unwrap();
        fs::write(
            root.as_path().join("profiles/headless/cordis.patch.yml"),
            "# user comment\n- id: custom-agent\n  config:\n    model: native/custom\n",
        )
        .unwrap();
        fs::write(root.as_path().join("settings.yaml"), "agent-default-model:\n  provider: native\n  model: parent\nllm-deepseek:\n  models: []\nllm-pi-ai:\n  providers:\n    native:\n      api: openai-responses\n      baseURL: https://native.example/v1\n      models:\n        - id: child\n").unwrap();
        fs::write(
            root.as_path().join(".credentials.yaml"),
            "NATIVE_KEY: fixture-only\n",
        )
        .unwrap();
        root
    }

    fn fixture_readback(home: &Path) -> Result<String, String> {
        let text = fs::read_to_string(home.join("profiles/headless/cordis.patch.yml")).unwrap();
        Ok(text
            .split_once(BEGIN)
            .map(|(_, managed)| managed.to_owned())
            .unwrap_or_else(|| BASE.into()))
    }

    #[test]
    fn native_dsh_save_readback_survives_connect_republish_disconnect_and_reset() {
        let root = fixture();
        let home = root.as_path();
        let path = home.join("profiles/headless/cordis.patch.yml");
        let original = fs::read_to_string(&path).unwrap();
        let main = fs::read_to_string(home.join("settings.yaml")).unwrap();
        let credentials = fs::read_to_string(home.join(".credentials.yaml")).unwrap();
        let saved = save_with_home(home, "native/child".into(), || fixture_readback(home)).unwrap();
        assert_eq!(saved.model, "native/child");
        assert!(saved
            .options
            .iter()
            .any(|option| option.id == "native/child"));
        assert!(saved.options.iter().all(|option| option.efforts.is_empty()));
        let patch = fs::read_to_string(&path).unwrap();
        assert!(patch.starts_with(&original));
        assert!(patch.contains("persona: !!js fixturePersona"));
        assert!(patch.contains("maxTokens: 8192"));
        assert!(patch.contains("deny: [custom-tool]"));
        assert_eq!(
            fs::read_to_string(home.join("settings.yaml")).unwrap(),
            main
        );
        assert_eq!(
            fs::read_to_string(home.join(".credentials.yaml")).unwrap(),
            credentials
        );
        let models = vec!["gateway/parent".into()];
        let base_url = "http://127.0.0.1:18080/v1".to_string();
        for _ in 0..2 {
            dsh_connect(
                home,
                base_url.clone(),
                MaskedSecret::new("fixture-only"),
                models.clone(),
            )
            .unwrap();
            assert_eq!(fs::read_to_string(&path).unwrap(), patch);
            assert_eq!(
                settings(home, &fixture_readback(home).unwrap())
                    .unwrap()
                    .model,
                "native/child"
            );
            assert!(!settings(home, &fixture_readback(home).unwrap())
                .unwrap()
                .options
                .iter()
                .any(|option| option.id.starts_with("codexhub/")));
        }
        dsh_disconnect(home, &ReadbackExpectation { base_url, models }).unwrap();
        assert_eq!(fs::read_to_string(&path).unwrap(), patch);
        let reset = save_with_home(home, String::new(), || fixture_readback(home)).unwrap();
        assert!(reset.model.is_empty());
        let patch = fs::read_to_string(&path).unwrap();
        assert!(patch.starts_with(&original));
        assert!(patch.contains("maxTokens: 8192"));
        assert!(patch.contains("persona: !!js fixturePersona"));
    }

    #[test]
    fn native_dsh_failed_effective_readback_restores_user_patch() {
        let root = fixture();
        let path = root.as_path().join("profiles/headless/cordis.patch.yml");
        let original = fs::read_to_string(&path).unwrap();
        assert!(save_with_home(root.as_path(), "native/child".into(), || Ok(BASE.into())).is_err());
        assert_eq!(fs::read_to_string(path).unwrap(), original);
    }

    // Opt-in primary-client evidence; no network or inference. The executable is
    // explicitly supplied by the runner, and DSH_HOME is always a disposable root.
    #[test]
    #[ignore = "requires an explicitly supplied official DSH CLI"]
    fn native_dsh_official_cli_public_save_readback_and_lifecycle() {
        struct Restore(Vec<(&'static str, Option<std::ffi::OsString>)>);
        impl Drop for Restore {
            fn drop(&mut self) {
                for (key, value) in &self.0 {
                    match value {
                        Some(value) => std::env::set_var(key, value),
                        None => std::env::remove_var(key),
                    }
                }
            }
        }
        let executable = std::env::var_os("CODEXHUB_DSH_NATIVE_TEST_EXECUTABLE")
            .expect("supply official DSH executable");
        let root = fixture();
        let home = root.as_path();
        let native_settings = fs::read_to_string(home.join("settings.yaml"))
            .unwrap()
            .replace("llm-deepseek:\n  models: []\n", "")
            + "    anthropic: {}\n";
        fs::write(home.join("settings.yaml"), native_settings).unwrap();
        let _restore = Restore(
            ["DSH_HOME", "CODEXHUB_DSH_EXECUTABLE"]
                .into_iter()
                .map(|key| (key, std::env::var_os(key)))
                .collect(),
        );
        std::env::set_var("DSH_HOME", home);
        std::env::set_var("CODEXHUB_DSH_EXECUTABLE", executable);
        let path = home.join("profiles/headless/cordis.patch.yml");
        let mut user_patch = fs::read_to_string(&path).unwrap();
        user_patch.push_str(&BASE.replace("!!js fixturePersona", "'keep persona'"));
        user_patch.push_str("- id: llm-pi-ai\n  config:\n    providers:\n      profile-native:\n        api: openai-responses\n        models:\n          - id: profile-child\n");
        fs::write(&path, user_patch).unwrap();
        let main = fs::read_to_string(home.join("settings.yaml")).unwrap();
        let credentials = fs::read_to_string(home.join(".credentials.yaml")).unwrap();
        let original = fs::read_to_string(home.join("profiles/headless/cordis.patch.yml")).unwrap();
        assert!(read_dsh_headless_default_subagent()
            .unwrap()
            .model
            .is_empty());
        let saved = save_dsh_headless_default_subagent("native/child".into()).unwrap();
        assert!(saved
            .options
            .iter()
            .any(|option| option.id.starts_with("anthropic/")));
        assert!(saved
            .options
            .iter()
            .any(|option| option.id == "deepseek/deepseek-v4-pro"));
        assert!(saved
            .options
            .iter()
            .any(|option| option.id == "profile-native/profile-child"));
        assert_eq!(
            fs::read_to_string(home.join("settings.yaml")).unwrap(),
            main
        );
        assert_eq!(
            fs::read_to_string(home.join(".credentials.yaml")).unwrap(),
            credentials
        );
        assert_eq!(saved.model, "native/child");
        assert_eq!(
            read_dsh_headless_default_subagent().unwrap().model,
            "native/child"
        );
        let patch = fs::read_to_string(home.join("profiles/headless/cordis.patch.yml")).unwrap();
        assert!(patch.starts_with(&original));
        let base_url = "http://127.0.0.1:18080/v1".to_string();
        let models = vec!["gateway/parent".into()];
        for index in 0..2 {
            dsh_connect(
                home,
                base_url.clone(),
                MaskedSecret::new("fixture-only"),
                models.clone(),
            )
            .unwrap();
            if index == 0 {
                let main = fs::read_to_string(home.join("settings.yaml")).unwrap();
                let credentials = fs::read_to_string(home.join(".credentials.yaml")).unwrap();
                let connected =
                    save_dsh_headless_default_subagent("deepseek/deepseek-v4-flash".into())
                        .unwrap();
                assert_eq!(connected.model, "deepseek/deepseek-v4-flash");
                assert_eq!(
                    fs::read_to_string(home.join("settings.yaml")).unwrap(),
                    main
                );
                assert_eq!(
                    fs::read_to_string(home.join(".credentials.yaml")).unwrap(),
                    credentials
                );
            }
            assert_eq!(
                read_dsh_headless_default_subagent().unwrap().model,
                "deepseek/deepseek-v4-flash"
            );
        }
        dsh_disconnect(home, &ReadbackExpectation { base_url, models }).unwrap();
        assert_eq!(
            read_dsh_headless_default_subagent().unwrap().model,
            "deepseek/deepseek-v4-flash"
        );
        assert!(save_dsh_headless_default_subagent(String::new())
            .unwrap()
            .model
            .is_empty());
        let dump = composed(home).unwrap();
        let spawn: Value = serde_yaml::from_str(row(&dump, "tool-subagent").unwrap()).unwrap();
        assert_eq!(
            spawn[0]["config"]["agentOptions"]["maxTokens"].as_u64(),
            Some(8192)
        );
        assert_eq!(spawn[0]["config"]["persona"].as_str(), Some("keep persona"));
    }
}
