use crate::{AppStatus, Provider, Settings};
use serde::{Deserialize, Serialize};
use std::ffi::OsString;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;

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
    pub(crate) fn runtime() -> Result<Self, String> {
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

    pub(crate) fn codex_dir(&self) -> &Path {
        &self.codex_dir
    }

    pub(crate) fn proxy_dir(&self) -> PathBuf {
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
            .join("codexhub-model-catalog.json")
    }

    fn config_overlay_script(&self) -> PathBuf {
        self.repo_root.join("src-python").join("config_overlay.py")
    }

    pub(crate) fn history_overlay_script(&self) -> PathBuf {
        self.repo_root.join("src-python").join("history_overlay.py")
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

pub(crate) struct ProcessCommandRunner;

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
    unified_codex_history: Option<bool>,
    auto_start_proxy: Option<bool>,
    include_official_models: Option<bool>,
    auto_sync_catalog: Option<bool>,
    auto_sync_clients: Option<bool>,
    default_codex_route: Option<String>,
    gateway_bind_address: Option<String>,
    gateway_client_key: Option<String>,
    gateway_enable_models: Option<bool>,
    gateway_enable_responses: Option<bool>,
    gateway_enable_chat_completions: Option<bool>,
    gateway_request_timeout_seconds: Option<u32>,
    gateway_auto_retry_enabled: Option<bool>,
    gateway_auto_retry_max_attempts: Option<u32>,
    gateway_image_proxy_enabled: Option<bool>,
    gateway_image_proxy_model: Option<String>,
    gateway_fast_model_variants: Option<Vec<String>>,
    official_disabled_models: Option<Vec<String>>,
    official_model_sort_order: Option<Vec<String>>,
    official_provider_sort_order: Option<i32>,
    proxy_port: Option<u16>,
}

impl SettingsDocument {
    fn into_settings(self) -> Settings {
        let defaults = Settings::default();
        Settings {
            auto_sync_history: self.auto_sync_history.unwrap_or(defaults.auto_sync_history),
            unified_codex_history: self
                .unified_codex_history
                .unwrap_or(defaults.unified_codex_history),
            auto_start_proxy: self.auto_start_proxy.unwrap_or(defaults.auto_start_proxy),
            include_official_models: self
                .include_official_models
                .unwrap_or(defaults.include_official_models),
            auto_sync_catalog: self.auto_sync_catalog.unwrap_or(defaults.auto_sync_catalog),
            auto_sync_clients: self
                .auto_sync_clients
                .or(self.auto_sync_catalog)
                .unwrap_or(defaults.auto_sync_clients),
            default_codex_route: self
                .default_codex_route
                .filter(|value| matches!(value.as_str(), "official" | "hub"))
                .unwrap_or(defaults.default_codex_route),
            gateway_bind_address: self
                .gateway_bind_address
                .filter(|value| value == "127.0.0.1")
                .unwrap_or(defaults.gateway_bind_address),
            gateway_client_key: self
                .gateway_client_key
                .filter(|value| !value.trim().is_empty())
                .unwrap_or(defaults.gateway_client_key),
            gateway_enable_models: self
                .gateway_enable_models
                .unwrap_or(defaults.gateway_enable_models),
            gateway_enable_responses: self
                .gateway_enable_responses
                .unwrap_or(defaults.gateway_enable_responses),
            gateway_enable_chat_completions: self
                .gateway_enable_chat_completions
                .unwrap_or(defaults.gateway_enable_chat_completions),
            gateway_request_timeout_seconds: self
                .gateway_request_timeout_seconds
                .map(|value| value.clamp(5, 600))
                .unwrap_or(defaults.gateway_request_timeout_seconds),
            gateway_auto_retry_enabled: self
                .gateway_auto_retry_enabled
                .unwrap_or(defaults.gateway_auto_retry_enabled),
            gateway_auto_retry_max_attempts: self
                .gateway_auto_retry_max_attempts
                .map(sanitize_gateway_auto_retry_max_attempts)
                .unwrap_or(defaults.gateway_auto_retry_max_attempts),
            gateway_image_proxy_enabled: self
                .gateway_image_proxy_enabled
                .unwrap_or(defaults.gateway_image_proxy_enabled),
            gateway_image_proxy_model: self
                .gateway_image_proxy_model
                .map(|value| value.trim().to_string())
                .filter(|value| !value.is_empty())
                .unwrap_or(defaults.gateway_image_proxy_model),
            gateway_fast_model_variants: self
                .gateway_fast_model_variants
                .map(sanitize_fast_model_variants)
                .unwrap_or(defaults.gateway_fast_model_variants),
            official_disabled_models: self
                .official_disabled_models
                .map(sanitize_model_ids)
                .unwrap_or(defaults.official_disabled_models),
            official_model_sort_order: self
                .official_model_sort_order
                .unwrap_or(defaults.official_model_sort_order),
            official_provider_sort_order: self
                .official_provider_sort_order
                .unwrap_or(defaults.official_provider_sort_order),
            proxy_port: self.proxy_port.unwrap_or(defaults.proxy_port),
        }
    }
}

fn sanitize_gateway_auto_retry_max_attempts(value: u32) -> u8 {
    value.clamp(1, 30) as u8
}

fn sanitize_fast_model_variants(values: Vec<String>) -> Vec<String> {
    const ALLOWED: &[&str] = &["openai/gpt-5.5", "openai/gpt-5.4"];
    let mut output = Vec::new();
    for value in values {
        if ALLOWED.contains(&value.as_str()) && !output.contains(&value) {
            output.push(value);
        }
    }
    output
}

fn sanitize_model_ids(values: Vec<String>) -> Vec<String> {
    let mut output = Vec::new();
    for value in values {
        let value = value.trim().to_string();
        if !value.is_empty() && !output.contains(&value) {
            output.push(value);
        }
    }
    output
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
    _auto_sync: bool,
    paths: &ConfigPaths,
    python: &Path,
    runner: &dyn CommandRunner,
) -> Result<AppStatus, String> {
    if mode != "official" && mode != "custom" {
        return Err(format!(
            "unsupported mode: {mode}; expected official or custom"
        ));
    }

    let settings = match get_settings_with_paths(paths) {
        Ok(settings) => settings,
        Err(error) if mode == "official" => {
            log::warn!("failed to read settings while switching official; using defaults: {error}");
            Settings::default()
        }
        Err(error) => return Err(error),
    };
    ensure_mode_switch_directories(paths)?;

    let overlay_result = if mode == "official" {
        let mut args = vec![
            "restore".to_string(),
            "--config".to_string(),
            paths.codex_config_path().to_string_lossy().into_owned(),
            "--backup".to_string(),
            paths.config_backup_path().to_string_lossy().into_owned(),
        ];
        if settings.unified_codex_history {
            args.push("--unified-history".to_string());
        }
        run_python_script(
            "config overlay restore",
            python,
            paths.config_overlay_script(),
            args,
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
    overlay_result?;

    Ok(AppStatus {
        mode: mode.to_string(),
        proxy_running: false,
        proxy_port: settings.proxy_port,
        proxy_build: None,
        message: format!("Switched to {mode} mode; proxy lifecycle is handled separately"),
        history_sync_status: None,
        history_sync_message: None,
    })
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

pub(crate) fn run_python_script(
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

pub(crate) fn format_command_failure(
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

pub(crate) fn find_python() -> PathBuf {
    which::which("python").unwrap_or_else(|_| PathBuf::from("python"))
}

#[cfg(test)]
mod tests {
    use super::{
        get_providers_with_paths, get_settings_with_paths, save_providers_with_paths,
        save_settings_with_paths, switch_mode_with_paths, CommandOutcome, CommandRunner,
        ConfigPaths,
    };
    use crate::{Model, Provider, Settings, UpstreamFormat};
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
            upstream_format: Some(UpstreamFormat::ChatCompletions),
            display_prefix: Some("Volc".to_string()),
            sort_order: Some(2),
            enabled: true,
            locked: false,
            models: vec![
                Model {
                    id: "glm-5.2".to_string(),
                    display_name: Some("Volc GLM-5.2".to_string()),
                    upstream_model: Some("ep-20260629".to_string()),
                    aliases: vec!["GLM-5.2".to_string(), "legacy-glm52".to_string()],
                    context_window: Some(1_024_000),
                    max_output_tokens: Some(8_192),
                    input_modalities: Some(vec!["text".to_string(), "image".to_string()]),
                    supported_reasoning_levels: Some(vec![
                        "low".to_string(),
                        "medium".to_string(),
                        "high".to_string(),
                        "xhigh".to_string(),
                    ]),
                    default_reasoning_level: Some("high".to_string()),
                    sort_order: Some(1),
                    enabled: true,
                    ..Model::default()
                },
                Model {
                    id: "minimax-m3".to_string(),
                    display_name: None,
                    upstream_model: None,
                    context_window: None,
                    max_output_tokens: Some(8_192),
                    input_modalities: None,
                    supported_reasoning_levels: None,
                    default_reasoning_level: None,
                    sort_order: Some(2),
                    enabled: false,
                    ..Model::default()
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
        assert!(written.contains("upstream_format = \"chat_completions\""));
        assert!(written.contains("upstream_model = \"ep-20260629\""));
        assert!(written.contains("aliases"));
        assert!(!written.contains("aliases = []"));
        assert!(written.contains("\"GLM-5.2\""));
        assert_eq!(
            loaded[0].models[0].aliases,
            vec!["GLM-5.2".to_string(), "legacy-glm52".to_string()]
        );
        assert!(written.contains("input_modalities"));
        assert!(written.contains("\"image\""));
        assert!(written.contains("supported_reasoning_levels"));
        assert!(written.contains("\"xhigh\""));
        assert!(written.contains("default_reasoning_level = \"high\""));
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
            unified_codex_history: false,
            auto_start_proxy: false,
            include_official_models: false,
            auto_sync_catalog: false,
            auto_sync_clients: false,
            default_codex_route: "official".to_string(),
            gateway_bind_address: "127.0.0.1".to_string(),
            gateway_client_key: "local-test-key".to_string(),
            gateway_enable_models: false,
            gateway_enable_responses: true,
            gateway_enable_chat_completions: false,
            gateway_request_timeout_seconds: 90,
            gateway_auto_retry_enabled: false,
            gateway_auto_retry_max_attempts: 7,
            gateway_image_proxy_enabled: true,
            gateway_image_proxy_model: "minimax-cn/MiniMax-M3".to_string(),
            gateway_fast_model_variants: vec!["openai/gpt-5.5".to_string()],
            official_disabled_models: vec!["openai/gpt-5.4-mini".to_string()],
            official_model_sort_order: vec![
                "openai/gpt-5.4".to_string(),
                "openai/gpt-5.5".to_string(),
            ],
            official_provider_sort_order: 3,
            proxy_port: 4555,
        };
        let saved = save_settings_with_paths(custom.clone(), &paths).expect("settings save");
        let loaded = get_settings_with_paths(&paths).expect("settings load");

        assert_settings_eq(&saved, &custom);
        assert_settings_eq(&loaded, &custom);
        let written = fs::read_to_string(paths.settings_path()).expect("settings text");
        assert!(written.contains("\"proxy_port\": 4555"));
        assert!(written.contains("\"gateway_request_timeout_seconds\": 90"));
        assert!(written.contains("\"gateway_auto_retry_enabled\": false"));
        assert!(written.contains("\"gateway_auto_retry_max_attempts\": 7"));
        assert!(written.contains("\"gateway_image_proxy_enabled\": true"));
        assert!(written.contains("\"gateway_image_proxy_model\": \"minimax-cn/MiniMax-M3\""));
        assert!(written.contains("\"gateway_fast_model_variants\""));
        assert!(written.contains("\"official_disabled_models\""));
        assert!(written.contains("\"official_model_sort_order\""));
        assert!(written.contains("\"official_provider_sort_order\": 3"));
        assert!(written.contains("\"auto_sync_clients\": false"));
        assert!(written.contains("\"unified_codex_history\": false"));
    }

    #[test]
    fn gateway_retry_and_image_proxy_settings_default_and_clamp() {
        let root = temp_root("gateway-runtime-settings");
        let paths = test_paths(&root);
        fs::create_dir_all(paths.settings_path().parent().unwrap()).unwrap();
        fs::write(
            paths.settings_path(),
            r#"{
              "gateway_auto_retry_max_attempts": 99,
              "gateway_image_proxy_enabled": true,
              "gateway_image_proxy_model": "  minimax-cn/MiniMax-M3  ",
              "proxy_port": 4555
            }"#,
        )
        .unwrap();

        let loaded = get_settings_with_paths(&paths).expect("settings load");

        assert!(loaded.gateway_auto_retry_enabled);
        assert_eq!(loaded.gateway_auto_retry_max_attempts, 30);
        assert!(loaded.gateway_image_proxy_enabled);
        assert_eq!(loaded.gateway_image_proxy_model, "minimax-cn/MiniMax-M3");
    }

    #[test]
    fn gateway_retry_attempts_clamp_to_minimum() {
        let root = temp_root("gateway-runtime-settings-min");
        let paths = test_paths(&root);
        fs::create_dir_all(paths.settings_path().parent().unwrap()).unwrap();
        fs::write(
            paths.settings_path(),
            r#"{
              "gateway_auto_retry_max_attempts": 0,
              "proxy_port": 4555
            }"#,
        )
        .unwrap();

        let loaded = get_settings_with_paths(&paths).expect("settings load");

        assert_eq!(loaded.gateway_auto_retry_max_attempts, 1);
    }

    #[test]
    fn legacy_auto_sync_catalog_loads_as_auto_sync_clients() {
        let root = temp_root("legacy-auto-sync-catalog");
        let paths = test_paths(&root);
        fs::create_dir_all(paths.settings_path().parent().unwrap()).unwrap();
        fs::write(
            paths.settings_path(),
            r#"{
              "auto_sync_catalog": false,
              "proxy_port": 4555
            }"#,
        )
        .unwrap();

        let loaded = get_settings_with_paths(&paths).expect("legacy settings load");

        assert!(!loaded.auto_sync_catalog);
        assert!(!loaded.auto_sync_clients);
        assert_eq!(loaded.proxy_port, 4555);
    }

    #[test]
    fn unified_history_setting_false_is_preserved() {
        let root = temp_root("unified-history-disabled");
        let paths = test_paths(&root);
        fs::create_dir_all(paths.settings_path().parent().unwrap()).unwrap();
        fs::write(
            paths.settings_path(),
            r#"{
              "unified_codex_history": false,
              "proxy_port": 4555
            }"#,
        )
        .unwrap();

        let loaded = get_settings_with_paths(&paths).expect("legacy settings load");

        assert!(!loaded.unified_codex_history);
        assert_eq!(loaded.proxy_port, 4555);
    }

    #[test]
    fn missing_unified_history_defaults_true_and_serializes() {
        let root = temp_root("unified-history-default-true");
        let paths = test_paths(&root);
        fs::create_dir_all(paths.settings_path().parent().unwrap()).unwrap();
        fs::write(
            paths.settings_path(),
            r#"{
              "proxy_port": 4555
            }"#,
        )
        .unwrap();

        let loaded = get_settings_with_paths(&paths).expect("settings load");
        let value = serde_json::to_value(&loaded).expect("settings serialize");

        assert!(loaded.unified_codex_history);
        assert_eq!(value["unified_codex_history"], serde_json::json!(true));
    }

    #[test]
    fn switch_mode_custom_applies_config_overlay_without_history_sync() {
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
        assert_eq!(commands.len(), 1);
        assert_eq!(
            commands[0].args[0],
            paths.config_overlay_script().to_string_lossy()
        );
        assert_contains_sequence(&commands[0].args, &["apply"]);
        assert_arg_value(&commands[0].args, "--config", &paths.codex_config_path());
        assert_arg_value(&commands[0].args, "--backup", &paths.config_backup_path());
        assert_arg_value(
            &commands[0].args,
            "--catalog",
            &paths.generated_catalog_path(),
        );
        assert_arg_literal(&commands[0].args, "--base-url", "http://127.0.0.1:4555");
        assert!(!commands[0].args.iter().any(|arg| arg == "normalize-fast"));
        assert_eq!(status.history_sync_status, None);
        assert_eq!(status.history_sync_message, None);
    }

    #[test]
    fn switch_mode_official_uses_unified_history_bucket_by_default() {
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
        assert!(commands[0]
            .args
            .iter()
            .any(|arg| arg == "--unified-history"));
    }

    #[test]
    fn switch_mode_official_skips_unified_history_when_setting_is_disabled() {
        let root = temp_root("switch-official-unified-history");
        let paths = test_paths(&root);
        save_settings_with_paths(
            Settings {
                unified_codex_history: false,
                proxy_port: 4555,
                ..Settings::default()
            },
            &paths,
        )
        .expect("settings save");
        let runner = RecordingRunner::successful();

        let status =
            switch_mode_with_paths("official", true, &paths, Path::new("python-test"), &runner)
                .expect("switch official");

        assert_eq!(status.mode, "official");
        assert_eq!(status.proxy_port, 4555);
        let commands = runner.commands.borrow();
        assert_eq!(commands.len(), 1);
        assert_contains_sequence(&commands[0].args, &["restore"]);
        assert!(!commands[0]
            .args
            .iter()
            .any(|arg| arg == "--unified-history"));
        assert!(!commands[0].args.iter().any(|arg| arg == "normalize-fast"));
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
    fn switch_mode_does_not_run_history_when_overlay_fails() {
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
        let runner = RecordingRunner::failed(23, "overlay stdout", "overlay stderr");

        let error =
            switch_mode_with_paths("custom", true, &paths, Path::new("python-test"), &runner)
                .expect_err("overlay should fail");

        let commands = runner.commands.borrow();
        assert_eq!(commands.len(), 1);
        assert_contains_sequence(&commands[0].args, &["apply"]);
        assert!(!commands[0].args.iter().any(|arg| arg == "normalize-fast"));
        assert!(error.contains("config overlay apply failed"));
        assert!(!error.contains("history backup root"));
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
        fn run(&self, _program: &Path, args: &[String]) -> Result<CommandOutcome, String> {
            self.commands.borrow_mut().push(RecordedCommand {
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
        assert_eq!(left.unified_codex_history, right.unified_codex_history);
        assert_eq!(left.auto_start_proxy, right.auto_start_proxy);
        assert_eq!(left.include_official_models, right.include_official_models);
        assert_eq!(left.auto_sync_catalog, right.auto_sync_catalog);
        assert_eq!(left.auto_sync_clients, right.auto_sync_clients);
        assert_eq!(left.default_codex_route, right.default_codex_route);
        assert_eq!(left.gateway_bind_address, right.gateway_bind_address);
        assert_eq!(left.gateway_client_key, right.gateway_client_key);
        assert_eq!(left.gateway_enable_models, right.gateway_enable_models);
        assert_eq!(
            left.gateway_enable_responses,
            right.gateway_enable_responses
        );
        assert_eq!(
            left.gateway_enable_chat_completions,
            right.gateway_enable_chat_completions
        );
        assert_eq!(
            left.gateway_request_timeout_seconds,
            right.gateway_request_timeout_seconds
        );
        assert_eq!(
            left.gateway_auto_retry_enabled,
            right.gateway_auto_retry_enabled
        );
        assert_eq!(
            left.gateway_auto_retry_max_attempts,
            right.gateway_auto_retry_max_attempts
        );
        assert_eq!(
            left.gateway_image_proxy_enabled,
            right.gateway_image_proxy_enabled
        );
        assert_eq!(
            left.gateway_image_proxy_model,
            right.gateway_image_proxy_model
        );
        assert_eq!(
            left.gateway_fast_model_variants,
            right.gateway_fast_model_variants
        );
        assert_eq!(
            left.official_disabled_models,
            right.official_disabled_models
        );
        assert_eq!(
            left.official_model_sort_order,
            right.official_model_sort_order
        );
        assert_eq!(
            left.official_provider_sort_order,
            right.official_provider_sort_order
        );
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
