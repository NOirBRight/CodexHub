use crate::{AppStatus, Provider, Settings};
use serde::{Deserialize, Serialize};
use std::ffi::OsString;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::{SystemTime, UNIX_EPOCH};

pub fn get_providers() -> Result<Vec<Provider>, String> {
    get_providers_with_paths(&ConfigPaths::runtime()?)
}

pub fn save_providers(providers: Vec<Provider>) -> Result<Vec<Provider>, String> {
    save_providers_with_paths(providers, &ConfigPaths::runtime()?)
}

pub fn get_settings() -> Result<Settings, String> {
    get_settings_with_paths(&ConfigPaths::runtime()?)
}

pub fn save_settings(settings: Settings) -> Result<Settings, String> {
    save_settings_with_paths(settings, &ConfigPaths::runtime()?)
}

pub fn switch_mode(mode: &str, auto_sync: bool) -> Result<AppStatus, String> {
    let paths = ConfigPaths::runtime()?;
    let python = find_python();
    let runner = ProcessCommandRunner;

    switch_mode_with_paths(mode, auto_sync, &paths, &python, &runner)
}

#[derive(Debug, Clone)]
pub(crate) struct ConfigPaths {
    codex_dir: PathBuf,
    repo_root: PathBuf,
}

impl ConfigPaths {
    fn runtime() -> Result<Self, String> {
        let codex_dir = match std::env::var_os("CODEX_HOME").filter(|value| !value.is_empty()) {
            Some(value) => PathBuf::from(value),
            None => dirs::home_dir()
                .ok_or_else(|| "failed to resolve user home directory".to_string())?
                .join(".codex"),
        };
        let repo_root = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .ok_or_else(|| "failed to resolve CodexHub repo root".to_string())?
            .to_path_buf();

        Ok(Self::new(codex_dir, repo_root))
    }

    pub(crate) fn new(codex_dir: impl Into<PathBuf>, repo_root: impl Into<PathBuf>) -> Self {
        Self {
            codex_dir: codex_dir.into(),
            repo_root: repo_root.into(),
        }
    }

    fn codex_dir(&self) -> &Path {
        &self.codex_dir
    }

    fn proxy_dir(&self) -> PathBuf {
        self.codex_dir.join("proxy")
    }

    fn runtime_providers_path(&self) -> PathBuf {
        self.proxy_dir().join("config").join("providers.toml")
    }

    fn bundled_providers_path(&self) -> PathBuf {
        self.repo_root.join("config").join("providers.toml")
    }

    fn settings_path(&self) -> PathBuf {
        self.proxy_dir().join("settings.json")
    }

    fn codex_config_path(&self) -> PathBuf {
        self.codex_dir.join("config.toml")
    }

    fn config_backup_path(&self) -> PathBuf {
        self.proxy_dir().join("config.toml.backup")
    }

    fn generated_catalog_path(&self) -> PathBuf {
        self.codex_dir
            .join("model-catalogs")
            .join("codex-proxy-official-ollama.json")
    }

    fn config_overlay_script(&self) -> PathBuf {
        self.repo_root.join("src-python").join("config_overlay.py")
    }

    fn history_overlay_script(&self) -> PathBuf {
        self.repo_root.join("src-python").join("history_overlay.py")
    }

    fn history_backup_root(&self, mode: &str) -> PathBuf {
        let direction = match mode {
            "custom" => "openai-to-custom",
            "official" => "custom-to-openai",
            _ => "unknown",
        };
        let stamp = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|duration| duration.as_millis())
            .unwrap_or_default();

        self.proxy_dir()
            .join(format!("history-{direction}-{stamp}"))
    }
}

#[derive(Debug, Clone)]
pub(crate) struct CommandOutcome {
    pub(crate) code: Option<i32>,
    pub(crate) stdout: String,
    pub(crate) stderr: String,
}

pub(crate) trait CommandRunner {
    fn run(&self, program: &Path, args: &[String]) -> Result<CommandOutcome, String>;
}

struct ProcessCommandRunner;

impl CommandRunner for ProcessCommandRunner {
    fn run(&self, program: &Path, args: &[String]) -> Result<CommandOutcome, String> {
        let output = Command::new(program)
            .args(args)
            .output()
            .map_err(|error| format!("failed to start {}: {error}", program.display()))?;

        Ok(CommandOutcome {
            code: output.status.code(),
            stdout: String::from_utf8_lossy(&output.stdout).to_string(),
            stderr: String::from_utf8_lossy(&output.stderr).to_string(),
        })
    }
}

#[derive(Debug, Serialize, Deserialize)]
struct ProvidersDocument {
    #[serde(default)]
    providers: Vec<Provider>,
}

#[derive(Debug, Deserialize)]
struct SettingsDocument {
    auto_sync_history: Option<bool>,
    auto_start_proxy: Option<bool>,
    include_official_models: Option<bool>,
    proxy_port: Option<u16>,
}

impl SettingsDocument {
    fn into_settings(self) -> Settings {
        let defaults = Settings::default();
        Settings {
            auto_sync_history: self.auto_sync_history.unwrap_or(defaults.auto_sync_history),
            auto_start_proxy: self.auto_start_proxy.unwrap_or(defaults.auto_start_proxy),
            include_official_models: self
                .include_official_models
                .unwrap_or(defaults.include_official_models),
            proxy_port: self.proxy_port.unwrap_or(defaults.proxy_port),
        }
    }
}

fn get_providers_with_paths(paths: &ConfigPaths) -> Result<Vec<Provider>, String> {
    let path = if paths.runtime_providers_path().exists() {
        paths.runtime_providers_path()
    } else {
        paths.bundled_providers_path()
    };

    let text = fs::read_to_string(&path)
        .map_err(|error| format!("failed to read providers TOML {}: {error}", path.display()))?;
    let document: ProvidersDocument = toml::from_str(&text)
        .map_err(|error| format!("failed to parse providers TOML {}: {error}", path.display()))?;

    Ok(document.providers)
}

fn save_providers_with_paths(
    providers: Vec<Provider>,
    paths: &ConfigPaths,
) -> Result<Vec<Provider>, String> {
    let path = paths.runtime_providers_path();
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|error| {
            format!(
                "failed to create provider config directory {}: {error}",
                parent.display()
            )
        })?;
    }

    let document = ProvidersDocument {
        providers: providers.clone(),
    };
    let text = toml::to_string_pretty(&document)
        .map_err(|error| format!("failed to serialize providers TOML: {error}"))?;
    fs::write(&path, text)
        .map_err(|error| format!("failed to write providers TOML {}: {error}", path.display()))?;

    Ok(providers)
}

fn get_settings_with_paths(paths: &ConfigPaths) -> Result<Settings, String> {
    let path = paths.settings_path();
    if !path.exists() {
        return Ok(Settings::default());
    }

    let text = fs::read_to_string(&path)
        .map_err(|error| format!("failed to read settings JSON {}: {error}", path.display()))?;
    let document: SettingsDocument = serde_json::from_str(&text)
        .map_err(|error| format!("failed to parse settings JSON {}: {error}", path.display()))?;

    Ok(document.into_settings())
}

fn save_settings_with_paths(settings: Settings, paths: &ConfigPaths) -> Result<Settings, String> {
    let path = paths.settings_path();
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|error| {
            format!(
                "failed to create settings directory {}: {error}",
                parent.display()
            )
        })?;
    }

    let text = serde_json::to_string_pretty(&settings)
        .map_err(|error| format!("failed to serialize settings JSON: {error}"))?;
    fs::write(&path, format!("{text}\n"))
        .map_err(|error| format!("failed to write settings JSON {}: {error}", path.display()))?;

    Ok(settings)
}

pub(crate) fn switch_mode_with_paths(
    mode: &str,
    auto_sync: bool,
    paths: &ConfigPaths,
    python: &Path,
    runner: &dyn CommandRunner,
) -> Result<AppStatus, String> {
    if mode != "official" && mode != "custom" {
        return Err(format!(
            "unsupported mode: {mode}; expected official or custom"
        ));
    }

    let settings = if mode == "custom" {
        get_settings_with_paths(paths)?
    } else {
        Settings::default()
    };
    ensure_mode_switch_directories(paths)?;

    let mut history_backup_root = None;
    if auto_sync {
        let target = if mode == "official" {
            "openai"
        } else {
            "custom"
        };
        let backup_root = paths.history_backup_root(mode);
        run_python_script(
            "history overlay normalize",
            python,
            paths.history_overlay_script(),
            vec![
                "normalize-fast".to_string(),
                "--codex-dir".to_string(),
                paths.codex_dir().to_string_lossy().into_owned(),
                "--backup-root".to_string(),
                backup_root.to_string_lossy().into_owned(),
                "--target".to_string(),
                target.to_string(),
            ],
            runner,
        )?;
        history_backup_root = Some(backup_root);
    }

    let overlay_result = if mode == "official" {
        run_python_script(
            "config overlay restore",
            python,
            paths.config_overlay_script(),
            vec![
                "restore".to_string(),
                "--config".to_string(),
                paths.codex_config_path().to_string_lossy().into_owned(),
                "--backup".to_string(),
                paths.config_backup_path().to_string_lossy().into_owned(),
            ],
            runner,
        )
    } else {
        run_python_script(
            "config overlay apply",
            python,
            paths.config_overlay_script(),
            vec![
                "apply".to_string(),
                "--config".to_string(),
                paths.codex_config_path().to_string_lossy().into_owned(),
                "--backup".to_string(),
                paths.config_backup_path().to_string_lossy().into_owned(),
                "--catalog".to_string(),
                paths
                    .generated_catalog_path()
                    .to_string_lossy()
                    .into_owned(),
                "--base-url".to_string(),
                format!("http://127.0.0.1:{}", settings.proxy_port),
            ],
            runner,
        )
    };
    overlay_result
        .map_err(|error| add_history_backup_context(error, history_backup_root.as_deref()))?;

    Ok(AppStatus {
        mode: mode.to_string(),
        proxy_running: false,
        proxy_port: settings.proxy_port,
        proxy_build: None,
        message: format!("Switched to {mode} mode; proxy lifecycle is handled separately"),
    })
}

fn add_history_backup_context(error: String, history_backup_root: Option<&Path>) -> String {
    match history_backup_root {
        Some(path) => format!("{error}\nhistory backup root: {}", path.display()),
        None => error,
    }
}

fn ensure_mode_switch_directories(paths: &ConfigPaths) -> Result<(), String> {
    for directory in [
        paths.codex_dir().to_path_buf(),
        paths.proxy_dir(),
        paths
            .generated_catalog_path()
            .parent()
            .unwrap_or(paths.codex_dir())
            .to_path_buf(),
    ] {
        fs::create_dir_all(&directory)
            .map_err(|error| format!("failed to create {}: {error}", directory.display()))?;
    }

    Ok(())
}

fn run_python_script(
    label: &str,
    python: &Path,
    script: PathBuf,
    script_args: Vec<String>,
    runner: &dyn CommandRunner,
) -> Result<CommandOutcome, String> {
    let mut args = vec![script.to_string_lossy().into_owned()];
    args.extend(script_args);

    let outcome = runner
        .run(python, &args)
        .map_err(|error| format!("{label} failed to start: {error}"))?;

    if outcome.code == Some(0) {
        return Ok(outcome);
    }

    Err(format_command_failure(label, python, &args, &outcome))
}

fn format_command_failure(
    label: &str,
    program: &Path,
    args: &[String],
    outcome: &CommandOutcome,
) -> String {
    let exit = match outcome.code {
        Some(code) => format!("exit code {code}"),
        None => "no exit code".to_string(),
    };

    format!(
        "{label} failed with {exit}\ncommand: {}\nstdout:\n{}\nstderr:\n{}",
        command_line(program, args),
        outcome.stdout.trim_end(),
        outcome.stderr.trim_end()
    )
}

fn command_line(program: &Path, args: &[String]) -> String {
    let mut parts = vec![program.to_string_lossy().into_owned()];
    parts.extend(args.iter().cloned());
    parts
        .into_iter()
        .map(|part| quote_command_part(OsString::from(part)))
        .collect::<Vec<_>>()
        .join(" ")
}

fn quote_command_part(part: OsString) -> String {
    let text = part.to_string_lossy();
    if text.is_empty()
        || text
            .chars()
            .any(|character| character.is_whitespace() || character == '"')
    {
        format!("\"{}\"", text.replace('"', "\\\""))
    } else {
        text.into_owned()
    }
}

fn find_python() -> PathBuf {
    which::which("python").unwrap_or_else(|_| PathBuf::from("python"))
}

#[cfg(test)]
mod tests {
    use super::{
        get_providers_with_paths, get_settings_with_paths, save_providers_with_paths,
        save_settings_with_paths, switch_mode_with_paths, CommandOutcome, CommandRunner,
        ConfigPaths,
    };
    use crate::{Model, Provider, Settings};
    use std::cell::RefCell;
    use std::fs;
    use std::path::{Path, PathBuf};
    use std::time::{SystemTime, UNIX_EPOCH};

    #[test]
    fn public_switch_mode_exposes_auto_sync_parameter() {
        let _switch: fn(&str, bool) -> Result<crate::AppStatus, String> = super::switch_mode;
    }

    #[test]
    fn providers_toml_roundtrip_preserves_all_provider_and_model_fields() {
        let root = temp_root("providers-roundtrip");
        let paths = test_paths(&root);
        let providers = vec![Provider {
            id: "volc".to_string(),
            name: "Volcengine".to_string(),
            base_url: "https://ark.cn-beijing.volces.com/api/coding/v3".to_string(),
            api_key: Some("{env:VOLCENGINE_API_KEY}".to_string()),
            display_prefix: Some("Volc".to_string()),
            sort_order: Some(2),
            enabled: true,
            models: vec![
                Model {
                    id: "glm-5.2".to_string(),
                    display_name: Some("Volc GLM-5.2".to_string()),
                    upstream_model: Some("ep-20260629".to_string()),
                    context_window: Some(1_024_000),
                    max_output_tokens: Some(8_192),
                    sort_order: Some(1),
                    enabled: true,
                },
                Model {
                    id: "minimax-m3".to_string(),
                    display_name: None,
                    upstream_model: None,
                    context_window: None,
                    max_output_tokens: Some(8_192),
                    sort_order: Some(2),
                    enabled: false,
                },
            ],
        }];

        let saved = save_providers_with_paths(providers.clone(), &paths).expect("providers save");
        let loaded = get_providers_with_paths(&paths).expect("providers load");

        assert_json_eq(&saved, &providers);
        assert_json_eq(&loaded, &providers);
        let written = fs::read_to_string(paths.runtime_providers_path()).expect("providers text");
        assert!(written.contains("[[providers]]"));
        assert!(written.contains("[[providers.models]]"));
        assert!(written.contains("upstream_model = \"ep-20260629\""));
    }

    #[test]
    fn get_providers_falls_back_to_bundled_config_when_runtime_config_is_missing() {
        let root = temp_root("providers-fallback");
        let paths = test_paths(&root);
        fs::create_dir_all(paths.bundled_providers_path().parent().unwrap()).unwrap();
        fs::write(
            paths.bundled_providers_path(),
            r#"
[[providers]]
id = "bundled"
name = "Bundled Provider"
base_url = "https://example.test/v1"
api_key = "{env:BUNDLED_API_KEY}"
sort_order = 7

  [[providers.models]]
  id = "model-a"
  context_window = 123
"#,
        )
        .unwrap();

        let loaded = get_providers_with_paths(&paths).expect("fallback providers");

        assert_eq!(loaded.len(), 1);
        assert_eq!(loaded[0].id, "bundled");
        assert_eq!(loaded[0].models[0].id, "model-a");
        assert!(loaded[0].enabled);
        assert!(loaded[0].models[0].enabled);
    }

    #[test]
    fn settings_missing_file_returns_defaults_and_roundtrips_saved_values() {
        let root = temp_root("settings-roundtrip");
        let paths = test_paths(&root);

        let defaults = get_settings_with_paths(&paths).expect("default settings");
        assert_settings_eq(&defaults, &Settings::default());

        let custom = Settings {
            auto_sync_history: false,
            auto_start_proxy: false,
            include_official_models: false,
            proxy_port: 4555,
        };
        let saved = save_settings_with_paths(custom.clone(), &paths).expect("settings save");
        let loaded = get_settings_with_paths(&paths).expect("settings load");

        assert_settings_eq(&saved, &custom);
        assert_settings_eq(&loaded, &custom);
        let written = fs::read_to_string(paths.settings_path()).expect("settings text");
        assert!(written.contains("\"proxy_port\": 4555"));
    }

    #[test]
    fn switch_mode_custom_normalizes_history_then_applies_config_overlay() {
        let root = temp_root("switch-custom");
        let paths = test_paths(&root);
        save_settings_with_paths(
            Settings {
                proxy_port: 4555,
                ..Settings::default()
            },
            &paths,
        )
        .expect("settings save");
        let runner = RecordingRunner::successful();

        let status =
            switch_mode_with_paths("custom", true, &paths, Path::new("python-test"), &runner)
                .expect("switch custom");

        assert_eq!(status.mode, "custom");
        assert_eq!(status.proxy_port, 4555);
        assert!(!status.proxy_running);
        assert!(status.message.contains("custom"));

        let commands = runner.commands.borrow();
        assert_eq!(commands.len(), 2);
        assert_eq!(commands[0].program, PathBuf::from("python-test"));
        assert_eq!(
            commands[0].args[0],
            paths.history_overlay_script().to_string_lossy()
        );
        assert_contains_sequence(&commands[0].args, &["normalize-fast"]);
        assert_arg_value(&commands[0].args, "--codex-dir", paths.codex_dir());
        assert_arg_value_prefix(&commands[0].args, "--backup-root", &paths.proxy_dir());
        assert_arg_literal(&commands[0].args, "--target", "custom");

        assert_eq!(
            commands[1].args[0],
            paths.config_overlay_script().to_string_lossy()
        );
        assert_contains_sequence(&commands[1].args, &["apply"]);
        assert_arg_value(&commands[1].args, "--config", &paths.codex_config_path());
        assert_arg_value(&commands[1].args, "--backup", &paths.config_backup_path());
        assert_arg_value(
            &commands[1].args,
            "--catalog",
            &paths.generated_catalog_path(),
        );
        assert_arg_literal(&commands[1].args, "--base-url", "http://127.0.0.1:4555");
    }

    #[test]
    fn switch_mode_official_can_skip_history_and_restores_config_overlay() {
        let root = temp_root("switch-official");
        let paths = test_paths(&root);
        let runner = RecordingRunner::successful();

        let status =
            switch_mode_with_paths("official", false, &paths, Path::new("python-test"), &runner)
                .expect("switch official");

        assert_eq!(status.mode, "official");
        assert_eq!(status.proxy_port, Settings::default().proxy_port);

        let commands = runner.commands.borrow();
        assert_eq!(commands.len(), 1);
        assert_eq!(
            commands[0].args[0],
            paths.config_overlay_script().to_string_lossy()
        );
        assert_contains_sequence(&commands[0].args, &["restore"]);
        assert_arg_value(&commands[0].args, "--config", &paths.codex_config_path());
        assert_arg_value(&commands[0].args, "--backup", &paths.config_backup_path());
    }

    #[test]
    fn switch_mode_official_without_history_ignores_corrupt_settings() {
        let root = temp_root("switch-official-corrupt-settings");
        let paths = test_paths(&root);
        fs::create_dir_all(paths.settings_path().parent().unwrap()).unwrap();
        fs::write(paths.settings_path(), "{not json").unwrap();
        let runner = RecordingRunner::successful();

        let status =
            switch_mode_with_paths("official", false, &paths, Path::new("python-test"), &runner)
                .expect("switch official");

        assert_eq!(status.mode, "official");
        assert_eq!(status.proxy_port, Settings::default().proxy_port);
        let commands = runner.commands.borrow();
        assert_eq!(commands.len(), 1);
        assert_contains_sequence(&commands[0].args, &["restore"]);
    }

    #[test]
    fn switch_mode_reports_history_backup_when_overlay_fails_after_history() {
        let root = temp_root("switch-overlay-fails-after-history");
        let paths = test_paths(&root);
        save_settings_with_paths(
            Settings {
                proxy_port: 4555,
                ..Settings::default()
            },
            &paths,
        )
        .expect("settings save");
        let runner = RecordingRunner::sequence(vec![
            CommandOutcome {
                code: Some(0),
                stdout: "history ok".to_string(),
                stderr: String::new(),
            },
            CommandOutcome {
                code: Some(23),
                stdout: "overlay stdout".to_string(),
                stderr: "overlay stderr".to_string(),
            },
        ]);

        let error =
            switch_mode_with_paths("custom", true, &paths, Path::new("python-test"), &runner)
                .expect_err("overlay should fail");

        let commands = runner.commands.borrow();
        assert_eq!(commands.len(), 2);
        let backup_root = arg_value(&commands[0].args, "--backup-root").to_string();
        drop(commands);
        assert!(error.contains("config overlay apply failed"));
        assert!(error.contains("history backup root"));
        assert!(error.contains(&backup_root));
        assert!(error.contains("overlay stderr"));
    }

    #[test]
    fn switch_mode_returns_stdout_stderr_context_when_python_fails() {
        let root = temp_root("switch-failure");
        let paths = test_paths(&root);
        let runner = RecordingRunner::failed(17, "printed stdout", "printed stderr");

        let error =
            switch_mode_with_paths("official", false, &paths, Path::new("python-test"), &runner)
                .expect_err("switch should fail");

        assert!(error.contains("config overlay restore failed"));
        assert!(error.contains("exit code 17"));
        assert!(error.contains("printed stdout"));
        assert!(error.contains("printed stderr"));
    }

    #[derive(Debug, Clone)]
    struct RecordedCommand {
        program: PathBuf,
        args: Vec<String>,
    }

    struct RecordingRunner {
        commands: RefCell<Vec<RecordedCommand>>,
        outcomes: RefCell<Vec<CommandOutcome>>,
    }

    impl RecordingRunner {
        fn successful() -> Self {
            Self::sequence(vec![CommandOutcome {
                code: Some(0),
                stdout: "ok".to_string(),
                stderr: String::new(),
            }])
        }

        fn failed(code: i32, stdout: &str, stderr: &str) -> Self {
            Self::sequence(vec![CommandOutcome {
                code: Some(code),
                stdout: stdout.to_string(),
                stderr: stderr.to_string(),
            }])
        }

        fn sequence(outcomes: Vec<CommandOutcome>) -> Self {
            Self {
                commands: RefCell::new(Vec::new()),
                outcomes: RefCell::new(outcomes),
            }
        }
    }

    impl CommandRunner for RecordingRunner {
        fn run(&self, program: &Path, args: &[String]) -> Result<CommandOutcome, String> {
            self.commands.borrow_mut().push(RecordedCommand {
                program: program.to_path_buf(),
                args: args.to_vec(),
            });
            let mut outcomes = self.outcomes.borrow_mut();
            let outcome = if outcomes.len() > 1 {
                outcomes.remove(0)
            } else {
                outcomes
                    .first()
                    .cloned()
                    .expect("recording runner requires at least one outcome")
            };
            Ok(outcome)
        }
    }

    fn test_paths(root: &Path) -> ConfigPaths {
        ConfigPaths::new(root.join("codex-home"), root.join("repo-root"))
    }

    fn temp_root(name: &str) -> PathBuf {
        let suffix = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let path = std::env::temp_dir().join(format!(
            "codexhub-config-{name}-{}-{suffix}",
            std::process::id()
        ));
        let _ = fs::remove_dir_all(&path);
        fs::create_dir_all(&path).unwrap();
        path
    }

    fn assert_json_eq<T: serde::Serialize>(left: &T, right: &T) {
        assert_eq!(
            serde_json::to_value(left).unwrap(),
            serde_json::to_value(right).unwrap()
        );
    }

    fn assert_settings_eq(left: &Settings, right: &Settings) {
        assert_eq!(left.auto_sync_history, right.auto_sync_history);
        assert_eq!(left.auto_start_proxy, right.auto_start_proxy);
        assert_eq!(left.include_official_models, right.include_official_models);
        assert_eq!(left.proxy_port, right.proxy_port);
    }

    fn assert_contains_sequence(args: &[String], values: &[&str]) {
        let mut position = 0;
        for value in values {
            position = args[position..]
                .iter()
                .position(|arg| arg == value)
                .map(|offset| position + offset + 1)
                .unwrap_or_else(|| panic!("missing argument {value:?} in {args:?}"));
        }
    }

    fn assert_arg_value(args: &[String], name: &str, expected: &Path) {
        assert_arg_literal(args, name, &expected.to_string_lossy());
    }

    fn assert_arg_value_prefix(args: &[String], name: &str, expected_prefix: &Path) {
        let value = arg_value(args, name);
        assert!(
            Path::new(value).starts_with(expected_prefix),
            "expected {name} value {value:?} to start with {expected_prefix:?}"
        );
    }

    fn assert_arg_literal(args: &[String], name: &str, expected: &str) {
        assert_eq!(arg_value(args, name), expected);
    }

    fn arg_value<'a>(args: &'a [String], name: &str) -> &'a str {
        let index = args
            .iter()
            .position(|arg| arg == name)
            .unwrap_or_else(|| panic!("missing argument {name:?} in {args:?}"));
        args.get(index + 1)
            .unwrap_or_else(|| panic!("missing value for {name:?} in {args:?}"))
    }
}
