use crate::{runtime_paths, safe_file, AppStatus, Provider, Settings};
use serde::{Deserialize, Serialize};
use std::collections::HashSet;
use std::ffi::OsString;
use std::fs;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::{error::Error, fmt};

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

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CodexContextGuardStatus {
    pub enabled: bool,
    pub codex_enabled: bool,
    pub gateway_enabled: bool,
    pub model_context_window: Option<u32>,
    pub model_auto_compact_token_limit: Option<u32>,
}

#[derive(Debug, Deserialize)]
struct CodexConfigContextGuardStatus {
    enabled: bool,
    model_context_window: Option<u32>,
    model_auto_compact_token_limit: Option<u32>,
}

pub fn get_codex_context_guard_status() -> Result<CodexContextGuardStatus, String> {
    let paths = ConfigPaths::runtime()?;
    let python = find_python();
    get_codex_context_guard_status_with_paths(&paths, &python, &ProcessCommandRunner)
}

pub fn set_codex_context_guard(enabled: bool) -> Result<CodexContextGuardStatus, String> {
    let paths = ConfigPaths::runtime()?;
    let python = find_python();
    set_codex_context_guard_with_paths(enabled, &paths, &python, &ProcessCommandRunner)
}

/// Reapply only the CodexHub-managed runtime context projection after a new
/// Official catalog snapshot has published.  This intentionally ignores
/// unowned and cross-channel Codex configuration.
pub(crate) fn republish_managed_codex_context_budget() -> Result<bool, String> {
    let paths = ConfigPaths::runtime()?;
    let python = find_python();
    republish_managed_codex_context_budget_with_paths(&paths, &python, &ProcessCommandRunner)
}

pub fn switch_mode_with_takeover(
    mode: &str,
    auto_sync: bool,
    force_takeover: bool,
) -> Result<AppStatus, String> {
    let paths = ConfigPaths::runtime()?;
    let python = find_python();
    let runner = ProcessCommandRunner;

    let mut status =
        switch_mode_with_paths_takeover(mode, auto_sync, force_takeover, &paths, &python, &runner)?;
    let lifecycle = crate::proxy::status()?;
    status.proxy_running = lifecycle.proxy_running;
    status.proxy_port = lifecycle.proxy_port;
    status.proxy_build = lifecycle.proxy_build;
    status.gateway_lifecycle = lifecycle.gateway_lifecycle;
    let settings = get_settings_with_paths(&paths).unwrap_or_default();
    let target_provider = if mode == "custom" || settings.unified_codex_history {
        "custom"
    } else {
        "openai"
    };

    match crate::history::reconcile_after_confirmed_route_switch(Some(target_provider)) {
        Ok(result) => {
            status.history_sync_status = Some(result.status.as_str().to_string());
            status.history_sync_message = result.error.or(result.reason).or_else(|| {
                (result.changed_rows > 0 || result.changed_files > 0).then(|| {
                    format!(
                        "changed {} history rows and {} files",
                        result.changed_rows, result.changed_files
                    )
                })
            });
        }
        Err(error) => {
            status.history_sync_status = Some("conflict".to_string());
            status.history_sync_message = Some(error);
        }
    }

    Ok(status)
}

#[derive(Debug, Clone)]
pub(crate) struct ConfigPaths {
    runtime_dir: PathBuf,
    codex_target_dir: PathBuf,
    repo_root: PathBuf,
}

impl ConfigPaths {
    pub(crate) fn runtime() -> Result<Self, String> {
        let runtime_dir = runtime_paths::runtime_home_dir()?;
        let codex_target_dir = runtime_paths::codex_target_home_dir()?;
        let repo_root = runtime_paths::resource_root()?;

        Ok(Self::new_isolated(runtime_dir, codex_target_dir, repo_root))
    }

    #[cfg(test)]
    pub(crate) fn new(codex_dir: impl Into<PathBuf>, repo_root: impl Into<PathBuf>) -> Self {
        let codex_dir = codex_dir.into();
        Self {
            runtime_dir: codex_dir.clone(),
            codex_target_dir: codex_dir,
            repo_root: repo_root.into(),
        }
    }

    pub(crate) fn new_isolated(
        runtime_dir: impl Into<PathBuf>,
        codex_target_dir: impl Into<PathBuf>,
        repo_root: impl Into<PathBuf>,
    ) -> Self {
        Self {
            runtime_dir: runtime_dir.into(),
            codex_target_dir: codex_target_dir.into(),
            repo_root: repo_root.into(),
        }
    }

    pub(crate) fn codex_dir(&self) -> &Path {
        &self.codex_target_dir
    }

    pub(crate) fn proxy_dir(&self) -> PathBuf {
        self.runtime_dir.join("proxy")
    }

    fn runtime_providers_path(&self) -> PathBuf {
        self.proxy_dir().join("config").join("providers.toml")
    }

    fn bundled_providers_path(&self) -> PathBuf {
        self.repo_root.join("config").join("providers.toml")
    }

    pub(crate) fn codex_config_path(&self) -> PathBuf {
        self.codex_target_dir.join("config.toml")
    }

    /// CLI accessor for the isolated runtime providers path. Used by the
    /// headless managed-client CLI to seed caller-supplied providers beneath
    /// the isolated root without host discovery.
    pub(crate) fn runtime_providers_path_for_cli(&self) -> PathBuf {
        self.runtime_providers_path()
    }

    /// CLI accessor for the isolated generated catalog path. The caller may
    /// import an explicitly supplied candidate catalog here only after the
    /// fresh isolated root has been validated.
    pub(crate) fn generated_catalog_path_for_cli(&self) -> PathBuf {
        self.generated_catalog_path()
    }

    pub(crate) fn settings_path(&self) -> PathBuf {
        self.proxy_dir().join("settings.json")
    }

    pub(crate) fn config_backup_path(&self) -> PathBuf {
        self.config_backup_path_for_owner(crate::app_flavor::current().routing_owner())
    }

    fn context_guard_state_path(&self) -> PathBuf {
        self.proxy_dir().join("context-guard-state.json")
    }

    pub(crate) fn config_backup_path_for_owner(
        &self,
        owner: crate::app_flavor::RoutingOwner,
    ) -> PathBuf {
        self.config_backup_path_for_runtime(&self.runtime_dir, owner)
    }

    fn config_backup_path_for_target_owner(
        &self,
        current_app_owner: crate::app_flavor::RoutingOwner,
        target_owner: crate::app_flavor::RoutingOwner,
    ) -> PathBuf {
        if target_owner == current_app_owner {
            return self.config_backup_path_for_owner(target_owner);
        }
        let flavor = match target_owner {
            crate::app_flavor::RoutingOwner::Release => {
                Some(crate::app_flavor::RuntimeFlavor::Stable)
            }
            crate::app_flavor::RoutingOwner::Beta => Some(crate::app_flavor::RuntimeFlavor::Beta),
            crate::app_flavor::RoutingOwner::Official
            | crate::app_flavor::RoutingOwner::UnknownExternal => None,
        };
        let runtime_dir = self
            .codex_target_dir
            .parent()
            .and_then(|home| {
                flavor.map(|flavor| runtime_paths::homes_for_flavor(home, flavor).runtime)
            })
            .unwrap_or_else(|| self.runtime_dir.clone());
        self.config_backup_path_for_runtime(&runtime_dir, target_owner)
    }

    fn config_backup_path_for_runtime(
        &self,
        runtime_dir: &Path,
        owner: crate::app_flavor::RoutingOwner,
    ) -> PathBuf {
        let name = match owner {
            crate::app_flavor::RoutingOwner::Beta => "config.toml.beta.backup",
            _ => "config.toml.release.backup",
        };
        runtime_dir.join("proxy").join(name)
    }

    fn generated_catalog_path(&self) -> PathBuf {
        self.runtime_dir
            .join("model-catalogs")
            .join("codexhub-model-catalog.json")
    }

    pub(crate) fn config_overlay_script(&self) -> PathBuf {
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
        let mut command = Command::new(program);
        command.args(args);
        configure_no_window(&mut command);
        let output = command
            .output()
            .map_err(|error| format!("failed to start {}: {error}", program.display()))?;

        Ok(CommandOutcome {
            code: output.status.code(),
            stdout: String::from_utf8_lossy(&output.stdout).to_string(),
            stderr: String::from_utf8_lossy(&output.stderr).to_string(),
        })
    }
}

pub(crate) fn configure_no_window(command: &mut Command) {
    #[cfg(target_os = "windows")]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        command.creation_flags(CREATE_NO_WINDOW);
    }
    #[cfg(not(target_os = "windows"))]
    {
        let _ = command;
    }
}

#[derive(Debug, Serialize, Deserialize)]
struct ProvidersDocument {
    #[serde(default)]
    providers: Vec<Provider>,
}

#[derive(Debug, Deserialize)]
struct SettingsDocument {
    locale: Option<String>,
    auto_sync_history: Option<bool>,
    unified_codex_history: Option<bool>,
    auto_start_software: Option<bool>,
    auto_start_gateway: Option<bool>,
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
    openai_context_guard_enabled: Option<bool>,
    gateway_fast_model_variants: Option<Vec<String>>,
    official_disabled_models: Option<Vec<String>>,
    official_model_sort_order: Option<Vec<String>>,
    official_provider_sort_order: Option<i32>,
    proxy_port: Option<u16>,
}

impl SettingsDocument {
    fn into_settings(self, known_official_models: &HashSet<String>) -> Settings {
        let defaults = Settings::default();
        Settings {
            locale: self.locale.unwrap_or_default(),
            auto_sync_history: self.auto_sync_history.unwrap_or(defaults.auto_sync_history),
            unified_codex_history: self
                .unified_codex_history
                .unwrap_or(defaults.unified_codex_history),
            auto_start_software: self
                .auto_start_software
                .or(self.auto_start_proxy)
                .unwrap_or(defaults.auto_start_software),
            auto_start_gateway: self
                .auto_start_gateway
                .unwrap_or(defaults.auto_start_gateway),
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
            openai_context_guard_enabled: self
                .openai_context_guard_enabled
                .unwrap_or(defaults.openai_context_guard_enabled),
            gateway_fast_model_variants: self
                .gateway_fast_model_variants
                .map(sanitize_fast_model_variants)
                .unwrap_or(defaults.gateway_fast_model_variants),
            official_disabled_models: self
                .official_disabled_models
                .map(|values| sanitize_model_ids_with_known(values, known_official_models))
                .unwrap_or(defaults.official_disabled_models),
            official_model_sort_order: self
                .official_model_sort_order
                .map(|values| sanitize_model_ids_with_known(values, known_official_models))
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
    const ALLOWED: &[&str] = &["gpt-5.5", "gpt-5.4"];
    sanitize_model_ids(values)
        .into_iter()
        .filter(|value| ALLOWED.contains(&value.as_str()))
        .collect()
}

fn sanitize_model_ids(values: Vec<String>) -> Vec<String> {
    sanitize_model_ids_with_known(values, &static_official_model_ids())
}

fn sanitize_model_ids_with_known(
    values: Vec<String>,
    known_official_models: &HashSet<String>,
) -> Vec<String> {
    let mut output = Vec::new();
    for value in values {
        let Some(value) = normalize_official_model_id(&value, known_official_models) else {
            continue;
        };
        if !value.is_empty() && !output.contains(&value) {
            output.push(value);
        }
    }
    output
}

pub(crate) fn normalize_official_model_id(
    value: &str,
    known_official_models: &HashSet<String>,
) -> Option<String> {
    let value = value.trim();
    if let Some(bare) = value
        .strip_prefix("openai/")
        .filter(|bare| bare.starts_with("gpt-"))
    {
        return known_official_models
            .contains(bare)
            .then(|| bare.to_string());
    }
    Some(value.to_string())
}

fn static_official_model_ids() -> HashSet<String> {
    ["gpt-5.5", "gpt-5.4", "gpt-5.4-mini", "gpt-5.3-codex-spark"]
        .into_iter()
        .map(str::to_string)
        .collect()
}

pub(crate) fn known_official_model_ids(paths: &ConfigPaths) -> HashSet<String> {
    let mut known = static_official_model_ids();
    let policy_path = paths.repo_root.join("config").join("catalog_policy.toml");
    if let Ok(text) = fs::read_to_string(policy_path) {
        if let Ok(policy) = toml::from_str::<toml::Value>(&text) {
            if let Some(models) = policy
                .get("visibility")
                .and_then(|visibility| visibility.get("official_models"))
                .and_then(toml::Value::as_array)
            {
                for model in models.iter().filter_map(toml::Value::as_str) {
                    insert_known_official_model(&mut known, model);
                }
            }
        }
    }

    for path in [paths
        .runtime_dir
        .join("model-catalogs")
        .join("openai-plus-ollama-cloud.json")]
    {
        let Ok(text) = fs::read_to_string(path) else {
            continue;
        };
        let Ok(catalog) = serde_json::from_str::<serde_json::Value>(&text) else {
            continue;
        };
        let Some(models) = catalog.get("models").and_then(serde_json::Value::as_array) else {
            continue;
        };
        for model in models {
            if let Some(slug) = model.get("slug").and_then(serde_json::Value::as_str) {
                insert_known_official_model(&mut known, slug);
            }
        }
    }
    known
}

fn insert_known_official_model(known: &mut HashSet<String>, value: &str) {
    let value = value.trim();
    let bare = value.strip_prefix("openai/").unwrap_or(value);
    if bare.starts_with("gpt-") {
        known.insert(bare.to_string());
    }
}

fn sanitize_locale(value: String) -> String {
    match value.trim() {
        "zh-CN" => "zh-CN".to_string(),
        "en-US" => "en-US".to_string(),
        _ => "en-US".to_string(),
    }
}

fn sanitize_settings_for_save(
    mut settings: Settings,
    known_official_models: &HashSet<String>,
) -> Settings {
    settings.locale = sanitize_locale(settings.locale);
    settings.gateway_fast_model_variants =
        sanitize_fast_model_variants(settings.gateway_fast_model_variants);
    settings.official_disabled_models =
        sanitize_model_ids_with_known(settings.official_disabled_models, known_official_models);
    settings.official_model_sort_order =
        sanitize_model_ids_with_known(settings.official_model_sort_order, known_official_models);
    settings
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

    let mut providers = document.providers;
    for provider in &mut providers {
        for model in &mut provider.models {
            crate::models::apply_resolved_model_limits(&provider.id, model);
        }
    }

    Ok(providers)
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
    safe_file::write_text_atomic(&path, &text)
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

    Ok(document.into_settings(&known_official_model_ids(paths)))
}

fn save_settings_with_paths(settings: Settings, paths: &ConfigPaths) -> Result<Settings, String> {
    let settings = sanitize_settings_for_save(settings, &known_official_model_ids(paths));
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
    safe_file::write_text_atomic(&path, &format!("{text}\n"))
        .map_err(|error| format!("failed to write settings JSON {}: {error}", path.display()))?;

    Ok(settings)
}

fn get_codex_context_guard_status_with_paths(
    paths: &ConfigPaths,
    python: &Path,
    runner: &dyn CommandRunner,
) -> Result<CodexContextGuardStatus, String> {
    let outcome = run_python_script(
        "context guard status",
        python,
        paths.config_overlay_script(),
        vec![
            "context-guard-status".to_string(),
            "--config".to_string(),
            paths.codex_config_path().to_string_lossy().into_owned(),
            "--state".to_string(),
            paths
                .context_guard_state_path()
                .to_string_lossy()
                .into_owned(),
        ],
        runner,
    )?;
    let codex_status: CodexConfigContextGuardStatus =
        serde_json::from_str(outcome.stdout.trim()).map_err(|error| {
            format!(
                "failed to parse context guard status JSON: {error}; stdout: {}",
                outcome.stdout.trim()
            )
        })?;
    let gateway_enabled = get_settings_with_paths(paths)?.openai_context_guard_enabled;
    Ok(combined_context_guard_status(codex_status, gateway_enabled))
}

fn set_codex_context_guard_with_paths(
    enabled: bool,
    paths: &ConfigPaths,
    python: &Path,
    runner: &dyn CommandRunner,
) -> Result<CodexContextGuardStatus, String> {
    ensure_mode_switch_directories(paths)?;
    let mut settings = get_settings_with_paths(paths)?;
    let current_app_owner = crate::app_flavor::current().routing_owner();
    let target_owner = fs::read_to_string(paths.codex_config_path())
        .ok()
        .as_deref()
        .and_then(codex_overlay_owner)
        .unwrap_or(current_app_owner);
    let backup_path =
        paths.config_backup_path_for_target_owner(current_app_owner, target_owner);
    let script_args = |value: bool| {
        vec![
            "context-guard-set".to_string(),
            "--config".to_string(),
            paths.codex_config_path().to_string_lossy().into_owned(),
            "--backup".to_string(),
            backup_path.to_string_lossy().into_owned(),
            "--state".to_string(),
            paths
                .context_guard_state_path()
                .to_string_lossy()
                .into_owned(),
            "--catalog".to_string(),
            paths.generated_catalog_path().to_string_lossy().into_owned(),
            "--enabled".to_string(),
            value.to_string(),
        ]
    };
    let rollback = || {
        let _ = run_python_script(
            "rollback context guard",
            python,
            paths.config_overlay_script(),
            script_args(!enabled),
            runner,
        );
    };
    let outcome = run_python_script(
        "set context guard",
        python,
        paths.config_overlay_script(),
        script_args(enabled),
        runner,
    )?;
    let codex_status: CodexConfigContextGuardStatus =
        match serde_json::from_str(outcome.stdout.trim()) {
            Ok(status) => status,
            Err(error) => {
                rollback();
                return Err(format!(
                    "failed to parse context guard status JSON: {error}; stdout: {}",
                    outcome.stdout.trim()
                ));
            }
        };
    if codex_status.enabled != enabled {
        rollback();
        return Err(format!(
            "context guard did not reach requested state; requested {enabled}, reported {}",
            codex_status.enabled
        ));
    }

    settings.openai_context_guard_enabled = enabled;
    if let Err(error) = save_settings_with_paths(settings, paths) {
        rollback();
        return Err(error);
    }

    Ok(combined_context_guard_status(codex_status, enabled))
}

fn republish_managed_codex_context_budget_with_paths(
    paths: &ConfigPaths,
    python: &Path,
    runner: &dyn CommandRunner,
) -> Result<bool, String> {
    ensure_mode_switch_directories(paths)?;
    let settings = get_settings_with_paths(paths)?;
    let config_path = paths.codex_config_path();
    let before = fs::read_to_string(&config_path).unwrap_or_default();
    let current_owner = crate::app_flavor::current().routing_owner();
    let managed_owner = codex_overlay_owner(&before);

    if managed_owner == Some(current_owner) {
        let args = vec![
            "apply".to_string(),
            "--config".to_string(),
            config_path.to_string_lossy().into_owned(),
            "--backup".to_string(),
            paths
                .config_backup_path_for_owner(current_owner)
                .to_string_lossy()
                .into_owned(),
            "--catalog".to_string(),
            paths
                .generated_catalog_path()
                .to_string_lossy()
                .into_owned(),
            "--base-url".to_string(),
            format!("http://127.0.0.1:{}", settings.proxy_port),
            "--gateway-key".to_string(),
            settings.gateway_client_key.clone(),
            "--owner".to_string(),
            match current_owner {
                crate::app_flavor::RoutingOwner::Beta => "beta".to_string(),
                _ => "release".to_string(),
            },
        ];
        run_python_script(
            "republish managed Codex context budget",
            python,
            paths.config_overlay_script(),
            args,
            runner,
        )?;
    }

    // The optional user-facing context guard has separate managed-state
    // bookkeeping.  Refresh it only for an explicit Official selection; an
    // unrelated third-party selection must remain untouched.
    let after_overlay = fs::read_to_string(&config_path).unwrap_or_default();
    if settings.openai_context_guard_enabled && top_level_model_is_official(&after_overlay) {
        set_codex_context_guard_with_paths(true, paths, python, runner)?;
    }

    Ok(before != fs::read_to_string(config_path).unwrap_or_default())
}

fn top_level_model_is_official(text: &str) -> bool {
    for line in text.lines() {
        let trimmed = line.trim();
        if trimmed.starts_with('[') {
            break;
        }
        let Some((key, value)) = trimmed.split_once('=') else {
            continue;
        };
        if key.trim() != "model" {
            continue;
        }
        let selected = value
            .trim()
            .trim_matches(|character| character == '\'' || character == '"');
        let selected = selected.strip_prefix("openai/").unwrap_or(selected);
        return selected.starts_with("gpt-");
    }
    false
}

fn combined_context_guard_status(
    codex_status: CodexConfigContextGuardStatus,
    gateway_enabled: bool,
) -> CodexContextGuardStatus {
    CodexContextGuardStatus {
        enabled: codex_status.enabled && gateway_enabled,
        codex_enabled: codex_status.enabled,
        gateway_enabled,
        model_context_window: codex_status.model_context_window,
        model_auto_compact_token_limit: codex_status.model_auto_compact_token_limit,
    }
}

#[cfg(test)]
pub(crate) fn switch_mode_with_paths(
    mode: &str,
    _auto_sync: bool,
    paths: &ConfigPaths,
    python: &Path,
    runner: &dyn CommandRunner,
) -> Result<AppStatus, String> {
    switch_mode_with_paths_takeover(mode, _auto_sync, false, paths, python, runner)
}

pub(crate) fn switch_mode_with_paths_takeover(
    mode: &str,
    _auto_sync: bool,
    force_takeover: bool,
    paths: &ConfigPaths,
    python: &Path,
    runner: &dyn CommandRunner,
) -> Result<AppStatus, String> {
    switch_mode_with_paths_takeover_as_owner(
        crate::app_flavor::current().routing_owner(),
        mode,
        force_takeover,
        paths,
        python,
        runner,
    )
}

fn switch_mode_with_paths_takeover_as_owner(
    current_app_owner: crate::app_flavor::RoutingOwner,
    mode: &str,
    force_takeover: bool,
    paths: &ConfigPaths,
    python: &Path,
    runner: &dyn CommandRunner,
) -> Result<AppStatus, String> {
    if mode != "official" && mode != "custom" {
        return Err(format!(
            "unsupported mode: {mode}; expected official or custom"
        ));
    }

    let target_owner = fs::read_to_string(paths.codex_config_path())
        .ok()
        .as_deref()
        .and_then(codex_overlay_owner);
    ensure_codex_owner_mutation_allowed(
        current_app_owner,
        target_owner,
        mode,
        force_takeover,
    )
    .map_err(|error| error.to_string())?;

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
        let backup_owner = target_owner.unwrap_or(current_app_owner);
        let mut args = vec![
            "restore".to_string(),
            "--config".to_string(),
            paths.codex_config_path().to_string_lossy().into_owned(),
            "--backup".to_string(),
            paths
                .config_backup_path_for_target_owner(current_app_owner, backup_owner)
                .to_string_lossy()
                .into_owned(),
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
        let mut args = vec![
            "apply".to_string(),
            "--config".to_string(),
            paths.codex_config_path().to_string_lossy().into_owned(),
            "--backup".to_string(),
            paths
                .config_backup_path_for_owner(current_app_owner)
                .to_string_lossy()
                .into_owned(),
            "--catalog".to_string(),
            paths.generated_catalog_path().to_string_lossy().into_owned(),
            "--base-url".to_string(),
            format!("http://127.0.0.1:{}", settings.proxy_port),
            "--gateway-key".to_string(),
            settings.gateway_client_key.clone(),
            "--owner".to_string(),
            match current_app_owner {
                crate::app_flavor::RoutingOwner::Beta => "beta".to_string(),
                _ => "release".to_string(),
            },
        ];
        if force_takeover && target_owner != Some(current_app_owner) {
            args.push("--takeover".to_string());
        }
        run_python_script(
            "config overlay apply",
            python,
            paths.config_overlay_script(),
            args,
            runner,
        )
    };
    overlay_result?;

    Ok(AppStatus {
        mode: mode.to_string(),
        proxy_running: false,
        proxy_port: settings.proxy_port,
        proxy_build: None,
        message: format!("Switched to {mode} mode; Gateway lifecycle is handled separately"),
        gateway_lifecycle: crate::gateway_transaction::GatewayLifecyclePhase::Unavailable,
        history_sync_status: None,
        history_sync_message: None,
    })
}

pub(crate) fn codex_overlay_owner(text: &str) -> Option<crate::app_flavor::RoutingOwner> {
    text.lines().find_map(|line| {
        let owner = line.trim().strip_prefix("# owner = ")?.trim();
        match owner {
            "release" => Some(crate::app_flavor::RoutingOwner::Release),
            "beta" => Some(crate::app_flavor::RoutingOwner::Beta),
            _ => None,
        }
    })
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum CodexOwnerMutationError {
    TakeoverRequired {
        current_app_owner: crate::app_flavor::RoutingOwner,
        current_target_owner: Option<crate::app_flavor::RoutingOwner>,
    },
    OwnerMismatch {
        current_app_owner: crate::app_flavor::RoutingOwner,
        current_target_owner: Option<crate::app_flavor::RoutingOwner>,
    },
}

impl CodexOwnerMutationError {
    fn code(self) -> &'static str {
        match self {
            Self::TakeoverRequired { .. } => "route.takeover_required",
            Self::OwnerMismatch { .. } => "route.owner_mismatch",
        }
    }
}

impl fmt::Display for CodexOwnerMutationError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        let (current_app_owner, current_target_owner) = match self {
            Self::TakeoverRequired {
                current_app_owner,
                current_target_owner,
            }
            | Self::OwnerMismatch {
                current_app_owner,
                current_target_owner,
            } => (current_app_owner, current_target_owner),
        };
        write!(
            formatter,
            "{}: Codex target owner is {:?}; current channel owner is {:?}",
            self.code(),
            current_target_owner,
            current_app_owner
        )
    }
}

impl Error for CodexOwnerMutationError {}

fn ensure_codex_owner_mutation_allowed(
    current_app_owner: crate::app_flavor::RoutingOwner,
    current_target_owner: Option<crate::app_flavor::RoutingOwner>,
    mode: &str,
    force_takeover: bool,
) -> Result<(), CodexOwnerMutationError> {
    if current_target_owner == Some(current_app_owner) {
        return Ok(());
    }
    if force_takeover {
        return Ok(());
    }

    if mode == "custom" {
        let stable_backward_compatible_target = current_app_owner
            == crate::app_flavor::RoutingOwner::Release
            && matches!(
                current_target_owner,
                None | Some(crate::app_flavor::RoutingOwner::Official)
            );
        if stable_backward_compatible_target {
            return Ok(());
        }
        return Err(CodexOwnerMutationError::TakeoverRequired {
            current_app_owner,
            current_target_owner,
        });
    }

    let stable_backward_compatible_disconnect = current_app_owner
        == crate::app_flavor::RoutingOwner::Release
        && matches!(
            current_target_owner,
            None | Some(crate::app_flavor::RoutingOwner::Official)
        );
    if stable_backward_compatible_disconnect {
        return Ok(());
    }
    Err(CodexOwnerMutationError::OwnerMismatch {
        current_app_owner,
        current_target_owner,
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
    let resource_root = runtime_paths::resource_root().ok();
    runtime_paths::find_python(resource_root.as_deref())
}

/// Populate the isolated `repo` directory of `paths` with the production
/// Codex overlay resources so `apply_codex_config_isolated` can invoke the
/// real `config_overlay.py` (and its `src-python` siblings) without host
/// discovery. Only the files the overlay actually imports are copied:
/// `src-python/*.py` and the bundled `config/providers.toml` referenced by
/// `providers_config.DEFAULT_PROVIDERS_PATH`. This mirrors the existing
/// `copy_python_sources_to_temp_repo` test helper but is production-safe and
/// confined to the isolated root.
pub(crate) fn populate_isolated_repo_resources(paths: &ConfigPaths) -> Result<(), String> {
    let production_root = runtime_paths::resource_root()?;
    let isolated_repo = &paths.repo_root;
    let src_python_target = isolated_repo.join("src-python");
    fs::create_dir_all(&src_python_target)
        .map_err(|error| format!("failed to create isolated src-python: {error}"))?;
    let src_python_source = production_root.join("src-python");
    if src_python_source.is_dir() {
        for entry in fs::read_dir(&src_python_source).map_err(|error| {
            format!("failed to read production src-python: {error}")
        })? {
            let entry = entry.map_err(|error| format!("failed to read src-python entry: {error}"))?;
            let path = entry.path();
            if path.extension().and_then(|value| value.to_str()) == Some("py") {
                let name = path.file_name().ok_or_else(|| "src-python entry has no file name".to_string())?;
                fs::copy(&path, src_python_target.join(name)).map_err(|error| {
                    format!("failed to copy production src-python module {}: {error}", path.display())
                })?;
            }
        }
    }
    let config_target = isolated_repo.join("config");
    fs::create_dir_all(&config_target)
        .map_err(|error| format!("failed to create isolated config dir: {error}"))?;
    let bundled_providers_source = production_root.join("config").join("providers.toml");
    if bundled_providers_source.is_file() {
        fs::copy(&bundled_providers_source, paths.bundled_providers_path()).map_err(|error| {
            format!("failed to copy production providers.toml: {error}")
        })?;
    }
    Ok(())
}

/// Load settings from a caller-supplied isolated `ConfigPaths`. Used by the
/// headless managed-client CLI to avoid any current-user discovery.
pub(crate) fn get_settings_from_paths(paths: &ConfigPaths) -> Result<Settings, String> {
    get_settings_with_paths(paths)
}

/// Load providers from a caller-supplied isolated `ConfigPaths`.
pub(crate) fn get_providers_from_paths(paths: &ConfigPaths) -> Result<Vec<Provider>, String> {
    get_providers_with_paths(paths)
}

// ----- Isolated Codex managed-client seam -----
//
// The CLI's Codex preview/apply/readback path constructs an isolated `ConfigPaths`
// and delegates to the existing production `switch_mode_with_paths_takeover_as_owner`
// + Python `config_overlay.py` serializer. This never ports the Codex TOML
// generator into Rust; it only wires an isolated root into the existing path.

#[derive(Debug, Clone, serde::Serialize)]
pub struct IsolatedCodexPreview {
    pub client_id: String,
    pub selector: String,
    pub model: String,
    pub route_protocol: String,
    pub target_names: Vec<String>,
    pub overlay_args_relative: Vec<String>,
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct IsolatedCodexReadback {
    pub client_id: String,
    pub ok: bool,
    pub selector: String,
    pub model: String,
    pub route_protocol: String,
}

/// The Codex overlay (`config_overlay.py`) always binds the proxy provider
/// to `model_provider = "custom"` with `wire_api = "responses"`, routing
/// through the CodexHub Gateway. This is the real, production-anchored route
/// protocol for Codex — it is not derived from the caller-supplied
/// `--model` and it is not a placeholder.
const CODEX_OVERLAY_ROUTE_PROTOCOL: &str = "responses";
const CODEX_OVERLAY_PROVIDER_ID: &str = "custom";

pub(crate) fn preview_codex_config_isolated(
    paths: &ConfigPaths,
    mode: &str,
    model: &str,
) -> Result<IsolatedCodexPreview, String> {
    if mode != "custom" && mode != "official" {
        return Err(format!("unsupported Codex mode: {mode}"));
    }
    let target = paths.codex_config_path();
    let target_names = vec![target
        .file_name()
        .and_then(|name| name.to_str())
        .unwrap_or("config.toml")
        .to_string()];
    // Build the overlay args that apply would invoke, expressed as relative
    // tokens so the structured output never leaks absolute paths.
    let overlay_args_relative = build_codex_overlay_args_relative(paths, mode, model);
    Ok(IsolatedCodexPreview {
        client_id: "codex".to_string(),
        selector: format!("{CODEX_OVERLAY_PROVIDER_ID}/{model}"),
        model: model.to_string(),
        route_protocol: CODEX_OVERLAY_ROUTE_PROTOCOL.to_string(),
        target_names,
        overlay_args_relative,
    })
}

pub(crate) fn apply_codex_config_isolated(
    paths: &ConfigPaths,
    mode: &str,
    force_takeover: bool,
    model: &str,
    python: &Path,
    runner: &dyn CommandRunner,
) -> Result<crate::AppStatus, String> {
    let _ = model; // The overlay derives the selected model from config.toml.
    let current_app_owner = crate::app_flavor::current().routing_owner();
    switch_mode_with_paths_takeover_as_owner(
        current_app_owner,
        mode,
        force_takeover,
        paths,
        python,
        runner,
    )
}

pub(crate) fn readback_codex_config_isolated(
    paths: &ConfigPaths,
    model: &str,
) -> Result<IsolatedCodexReadback, String> {
    let config_path = paths.codex_config_path();
    if !config_path.exists() {
        return Err(format!(
            "readback failed: missing Codex config {}",
            config_path
                .file_name()
                .and_then(|name| name.to_str())
                .unwrap_or("config.toml")
        ));
    }
    let text = fs::read_to_string(&config_path)
        .map_err(|error| format!("readback failed: cannot read Codex config: {error}"))?;
    // Fail closed on stale or absent owner marker — the overlay always writes
    // a `# owner = release|beta` marker. An unknown/missing owner means the
    // config was not produced by this app's overlay.
    let owner = codex_overlay_owner(&text);
    let current_owner = crate::app_flavor::current().routing_owner();
    if owner != Some(current_owner) {
        return Err(format!(
            "readback failed: Codex config owner is stale or absent (expected {:?}, got {:?})",
            current_owner, owner
        ));
    }
    // F4: verify the real provider/route binding the overlay writes. The
    // overlay always binds `model_provider = "custom"` with
    // `wire_api = "responses"`; an absent or mismatched provider means the
    // config was not produced by this app's overlay (e.g. a hand-edited or
    // stale file), so fail closed instead of reporting a fabricated route.
    let provider = top_level_toml_value(&text, "model_provider");
    if provider.as_deref() != Some(CODEX_OVERLAY_PROVIDER_ID) {
        return Err(format!(
            "readback failed: Codex config model_provider is {:?}; expected {:?}",
            provider,
            CODEX_OVERLAY_PROVIDER_ID
        ));
    }
    let wire_api = section_toml_value(&text, &format!("model_providers.{CODEX_OVERLAY_PROVIDER_ID}"), "wire_api");
    if wire_api.as_deref() != Some(CODEX_OVERLAY_ROUTE_PROTOCOL) {
        return Err(format!(
            "readback failed: Codex config custom wire_api is {:?}; expected {:?}",
            wire_api,
            CODEX_OVERLAY_ROUTE_PROTOCOL
        ));
    }
    Ok(IsolatedCodexReadback {
        client_id: "codex".to_string(),
        ok: true,
        selector: format!("{CODEX_OVERLAY_PROVIDER_ID}/{model}"),
        model: model.to_string(),
        route_protocol: CODEX_OVERLAY_ROUTE_PROTOCOL.to_string(),
    })
}

/// Read a top-level `key = "value"` (TOML basic or literal string) from a
/// config text. Used by the Codex readback verifier to confirm the overlay's
/// `model_provider` binding without pulling in a full TOML parser dependency.
fn top_level_toml_value(text: &str, key: &str) -> Option<String> {
    for line in text.lines() {
        let trimmed = line.trim();
        if trimmed.starts_with('[') {
            continue;
        }
        let Some(rest) = trimmed.strip_prefix(key) else {
            continue;
        };
        let rest = rest.trim_start();
        let Some(rest) = rest.strip_prefix('=') else {
            continue;
        };
        return parse_toml_string_value(rest.trim());
    }
    None
}

/// Read a `[section]` `key = "value"` from a config text. Scans only after the
/// last `[section]` header that matches, so later re-declarations win like TOML.
fn section_toml_value(text: &str, section: &str, key: &str) -> Option<String> {
    let header = format!("[{section}]");
    let mut in_section = false;
    for line in text.lines() {
        let trimmed = line.trim();
        if trimmed.starts_with('[') {
            in_section = trimmed == header;
            continue;
        }
        if !in_section {
            continue;
        }
        let Some(rest) = trimmed.strip_prefix(key) else {
            continue;
        };
        let rest = rest.trim_start();
        let Some(rest) = rest.strip_prefix('=') else {
            continue;
        };
        return parse_toml_string_value(rest.trim());
    }
    None
}

fn parse_toml_string_value(value: &str) -> Option<String> {
    let value = value.split('#').next().unwrap_or(value).trim();
    if let Some(rest) = value.strip_prefix('"').and_then(|v| v.strip_suffix('"')) {
        Some(rest.replace("\\\"", "\"").replace("\\\\", "\\"))
    } else {
        value
            .strip_prefix('\'')
            .and_then(|v| v.strip_suffix('\''))
            .map(|rest| rest.replace("''", "'"))
    }
}

fn build_codex_overlay_args_relative(paths: &ConfigPaths, mode: &str, _model: &str) -> Vec<String> {
    // The structured preview reports the overlay *shape* using relative tokens
    // so absolute paths never leak. The actual apply path resolves them
    // through `switch_mode_with_paths_takeover_as_owner`.
    let config_name = paths
        .codex_config_path()
        .file_name()
        .and_then(|name| name.to_str())
        .unwrap_or("config.toml")
        .to_string();
    let backup_name = paths
        .config_backup_path()
        .file_name()
        .and_then(|name| name.to_str())
        .unwrap_or("config.toml.release.backup")
        .to_string();
    let catalog_name = paths
        .generated_catalog_path()
        .file_name()
        .and_then(|name| name.to_str())
        .unwrap_or("codexhub-model-catalog.json")
        .to_string();
    let command = if mode == "official" { "restore" } else { "apply" };
    vec![
        command.to_string(),
        "--config".to_string(),
        config_name,
        "--backup".to_string(),
        backup_name,
        "--catalog".to_string(),
        catalog_name,
    ]
}

#[cfg(test)]
mod tests {
    use super::{
        codex_overlay_owner, ensure_codex_owner_mutation_allowed,
        get_codex_context_guard_status_with_paths, get_providers_with_paths,
        get_settings_with_paths, republish_managed_codex_context_budget_with_paths,
        save_providers_with_paths, save_settings_with_paths, set_codex_context_guard_with_paths,
        switch_mode_with_paths, switch_mode_with_paths_takeover_as_owner,
        top_level_model_is_official, CommandOutcome, CommandRunner, ConfigPaths,
        ProcessCommandRunner,
    };
    use crate::{Model, Provider, Settings, ToolProtocol, ToolSurfaceStrategy, UpstreamFormat};
    use std::cell::RefCell;
    use std::fs;
    use std::path::{Path, PathBuf};
    use std::time::{SystemTime, UNIX_EPOCH};

    #[test]
    fn providers_toml_roundtrip_preserves_all_provider_and_model_fields() {
        let root = temp_root("providers-roundtrip");
        let paths = test_paths(&root);
        let providers = vec![Provider {
            id: "volc-roundtrip".to_string(),
            name: "Volcengine".to_string(),
            base_url: "https://ark.cn-beijing.volces.com/api/coding/v3".to_string(),
            api_key: Some("{env:VOLCENGINE_API_KEY}".to_string()),
            upstream_format: Some(UpstreamFormat::ChatCompletions),
            available_upstream_formats: Some(vec![
                UpstreamFormat::Responses,
                UpstreamFormat::ChatCompletions,
            ]),
            tool_protocol: Some(ToolProtocol::ChatTools),
            tool_surface_strategy: Some(ToolSurfaceStrategy::Eager),
            reports_cached_input_tokens: Some(true),
            supports_developer_role: None,
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
                    tool_surface_strategy: Some(ToolSurfaceStrategy::DeferredCore),
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
        assert!(written.contains("available_upstream_formats"));
        assert!(written.contains("tool_protocol = \"chat_tools\""));
        assert!(written.contains("tool_surface_strategy = \"eager\""));
        assert!(written.contains("reports_cached_input_tokens = true"));
        assert!(written.contains("\"responses\""));
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
        assert!(written.contains("tool_surface_strategy = \"deferred_core\""));
    }

    #[test]
    fn providers_toml_roundtrip_preserves_anthropic_endpoint_selection() {
        let root = temp_root("providers-anthropic-format");
        let paths = test_paths(&root);
        let providers = vec![Provider {
            id: "anthropic-direct".to_string(),
            name: "Anthropic Direct".to_string(),
            base_url: "https://api.anthropic.com".to_string(),
            api_key: Some("{env:ANTHROPIC_API_KEY}".to_string()),
            upstream_format: Some(UpstreamFormat::AnthropicMessages),
            available_upstream_formats: Some(vec![UpstreamFormat::AnthropicMessages]),
            tool_protocol: Some(ToolProtocol::None),
            tool_surface_strategy: None,
            reports_cached_input_tokens: None,
            supports_developer_role: None,
            display_prefix: Some("anthropic/".to_string()),
            sort_order: Some(3),
            enabled: true,
            locked: false,
            models: vec![Model {
                id: "claude-sonnet-4-20250514".to_string(),
                enabled: true,
                ..Model::default()
            }],
        }];

        save_providers_with_paths(providers.clone(), &paths).expect("providers save");
        let loaded = get_providers_with_paths(&paths).expect("providers load");
        let written = fs::read_to_string(paths.runtime_providers_path()).expect("providers text");

        assert_json_eq(&loaded, &providers);
        assert!(written.contains("upstream_format = \"anthropic_messages\""));
        assert!(written.contains("available_upstream_formats = [\"anthropic_messages\"]"));
        assert!(written.contains("tool_protocol = \"none\""));
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
    fn get_providers_applies_resolved_limits_used_by_the_gateway() {
        let root = temp_root("providers-resolved-limits");
        let paths = test_paths(&root);
        fs::create_dir_all(paths.bundled_providers_path().parent().unwrap()).unwrap();
        fs::write(
            paths.bundled_providers_path(),
            r#"
[[providers]]
id = "ollama-cloud"
name = "Ollama Cloud"
base_url = "https://ollama.com/v1"

  [[providers.models]]
  id = "glm-5.2"

[[providers]]
id = "volc"
name = "Volcengine"
base_url = "https://ark.cn-beijing.volces.com/api/coding/v3"

  [[providers.models]]
  id = "minimax-m3"
"#,
        )
        .unwrap();

        let loaded = get_providers_with_paths(&paths).expect("providers with resolved limits");

        assert_eq!(loaded[0].models[0].context_window, Some(1_000_000));
        assert_eq!(loaded[0].models[0].max_context_window, Some(1_000_000));
        assert_eq!(loaded[1].models[0].context_window, Some(1_000_000));
        assert_eq!(loaded[1].models[0].max_context_window, Some(1_000_000));
        assert_eq!(
            loaded[0].models[0].effective_source.as_deref(),
            Some("provider_spec")
        );
        assert_eq!(
            loaded[1].models[0].max_source.as_deref(),
            Some("https://www.volcengine.com/docs/82379")
        );
    }

    #[test]
    fn settings_missing_file_returns_defaults_and_roundtrips_saved_values() {
        let root = temp_root("settings-roundtrip");
        let paths = test_paths(&root);

        let defaults = get_settings_with_paths(&paths).expect("default settings");
        assert_settings_eq(&defaults, &Settings::default());
        assert_eq!(
            defaults.proxy_port,
            crate::app_flavor::default_gateway_port()
        );

        let custom = Settings {
            locale: "zh-CN".to_string(),
            auto_sync_history: false,
            unified_codex_history: false,
            auto_start_software: false,
            auto_start_gateway: false,
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
            openai_context_guard_enabled: true,
            gateway_fast_model_variants: vec!["gpt-5.5".to_string()],
            official_disabled_models: vec!["gpt-5.4-mini".to_string()],
            official_model_sort_order: vec!["gpt-5.4".to_string(), "gpt-5.5".to_string()],
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
        assert!(written.contains("\"openai_context_guard_enabled\": true"));
        assert!(written.contains("\"gateway_fast_model_variants\""));
        assert!(written.contains("\"official_disabled_models\""));
        assert!(written.contains("\"official_model_sort_order\""));
        assert!(written.contains("\"official_provider_sort_order\": 3"));
        assert!(written.contains("\"auto_sync_clients\": false"));
        assert!(written.contains("\"auto_start_software\": false"));
        assert!(written.contains("\"auto_start_gateway\": false"));
        assert!(written.contains("\"unified_codex_history\": false"));
        assert!(written.contains("\"locale\": \"zh-CN\""));
    }

    #[test]
    fn legacy_official_model_ids_are_normalized_on_load_and_save() {
        let root = temp_root("legacy-official-model-ids");
        let paths = test_paths(&root);
        fs::create_dir_all(paths.settings_path().parent().unwrap()).unwrap();
        fs::write(
            paths.settings_path(),
            r#"{
              "gateway_fast_model_variants": [
                " openai/gpt-5.5 ",
                "gpt-5.5",
                "openai/gpt-5.4",
                "ollama-cloud/glm-5.2"
              ],
              "official_disabled_models": [
                " openai/gpt-5.4-mini ",
                "gpt-5.4-mini",
                "ollama-cloud/glm-5.2"
              ],
              "official_model_sort_order": [
                "openai/gpt-5.5",
                " gpt-5.5 ",
                "ollama-cloud/glm-5.2"
              ]
            }"#,
        )
        .unwrap();

        let loaded = get_settings_with_paths(&paths).expect("legacy settings load");

        assert_eq!(
            loaded.gateway_fast_model_variants,
            vec!["gpt-5.5".to_string(), "gpt-5.4".to_string()]
        );
        assert_eq!(
            loaded.official_disabled_models,
            vec![
                "gpt-5.4-mini".to_string(),
                "ollama-cloud/glm-5.2".to_string()
            ]
        );
        assert_eq!(
            loaded.official_model_sort_order,
            vec!["gpt-5.5".to_string(), "ollama-cloud/glm-5.2".to_string()]
        );

        let saved = save_settings_with_paths(
            Settings {
                gateway_fast_model_variants: vec![
                    "openai/gpt-5.5".to_string(),
                    " gpt-5.4 ".to_string(),
                ],
                official_disabled_models: vec![
                    "openai/gpt-5.4".to_string(),
                    " gpt-5.4 ".to_string(),
                ],
                official_model_sort_order: vec![
                    "openai/gpt-5.5".to_string(),
                    " gpt-5.5 ".to_string(),
                ],
                ..Settings::default()
            },
            &paths,
        )
        .expect("legacy settings save");

        assert_eq!(
            saved.gateway_fast_model_variants,
            vec!["gpt-5.5".to_string(), "gpt-5.4".to_string()]
        );
        assert_eq!(saved.official_disabled_models, vec!["gpt-5.4".to_string()]);
        assert_eq!(saved.official_model_sort_order, vec!["gpt-5.5".to_string()]);
        let written = fs::read_to_string(paths.settings_path()).expect("normalized settings text");
        assert!(!written.contains("openai/gpt-"));
    }

    #[test]
    fn shared_model_identity_vectors_reject_only_unknown_official_aliases() {
        let fixture: serde_json::Value = serde_json::from_str(include_str!(
            "../../tests/fixtures/model_identity_vectors.json"
        ))
        .expect("identity fixture");
        let inputs = fixture["vectors"]
            .as_array()
            .unwrap()
            .iter()
            .map(|vector| vector["input"].as_str().unwrap().to_string())
            .collect();
        let mut expected = Vec::<String>::new();
        for value in fixture["vectors"]
            .as_array()
            .unwrap()
            .iter()
            .filter_map(|vector| vector["expected"].as_str())
        {
            if !expected.iter().any(|existing| existing == value) {
                expected.push(value.to_string());
            }
        }

        assert_eq!(super::sanitize_model_ids(inputs), expected);
    }

    #[test]
    fn settings_accept_current_catalog_alias_and_reject_unknown_official_alias() {
        let root = temp_root("current-official-alias");
        let paths = test_paths(&root);
        let catalog_path = root
            .join("codex-home")
            .join("model-catalogs")
            .join("openai-plus-ollama-cloud.json");
        fs::create_dir_all(catalog_path.parent().unwrap()).unwrap();
        fs::write(
            &catalog_path,
            r#"{"models":[{"slug":"gpt-5.6-sol","display_name":"GPT-5.6-Sol"}]}"#,
        )
        .unwrap();
        fs::create_dir_all(paths.settings_path().parent().unwrap()).unwrap();
        fs::write(
            paths.settings_path(),
            r#"{
              "official_disabled_models": [
                "openai/gpt-5.6-sol",
                "openai/gpt-9.9-unknown",
                "acme/gpt-5.6-sol"
              ]
            }"#,
        )
        .unwrap();

        let loaded = get_settings_with_paths(&paths).expect("settings load");

        assert_eq!(
            loaded.official_disabled_models,
            vec!["gpt-5.6-sol".to_string(), "acme/gpt-5.6-sol".to_string()]
        );
        let saved = save_settings_with_paths(loaded, &paths).expect("settings save");
        assert_eq!(
            saved.official_disabled_models,
            vec!["gpt-5.6-sol".to_string(), "acme/gpt-5.6-sol".to_string()]
        );
        let written = fs::read_to_string(paths.settings_path()).unwrap();
        assert!(!written.contains("openai/gpt-"));
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn generated_and_bundled_catalogs_do_not_authorize_legacy_aliases() {
        let root = temp_root("untrusted-official-alias-catalogs");
        let paths = test_paths(&root);
        let generated_path = paths.generated_catalog_path();
        fs::create_dir_all(generated_path.parent().unwrap()).unwrap();
        fs::write(
            generated_path,
            r#"{"models":[{"slug":"gpt-forged-generated"}]}"#,
        )
        .unwrap();
        let bundled_path = root
            .join("repo-root")
            .join("model-catalogs")
            .join("openai-plus-ollama-cloud.json");
        fs::create_dir_all(bundled_path.parent().unwrap()).unwrap();
        fs::write(
            bundled_path,
            r#"{"models":[{"slug":"gpt-forged-bundled"}]}"#,
        )
        .unwrap();
        fs::create_dir_all(paths.settings_path().parent().unwrap()).unwrap();
        fs::write(
            paths.settings_path(),
            r#"{
              "official_disabled_models": [
                "openai/gpt-forged-generated",
                "openai/gpt-forged-bundled"
              ]
            }"#,
        )
        .unwrap();

        let loaded = get_settings_with_paths(&paths).expect("settings load");

        assert!(loaded.official_disabled_models.is_empty());
        let _ = fs::remove_dir_all(root);
    }

    #[test]
    fn legacy_auto_start_proxy_migrates_to_software_autostart_only() {
        let root = temp_root("legacy-autostart-split");
        let paths = test_paths(&root);
        fs::create_dir_all(paths.settings_path().parent().unwrap()).unwrap();
        fs::write(
            paths.settings_path(),
            r#"{
              "auto_start_proxy": false,
              "proxy_port": 4555
            }"#,
        )
        .unwrap();

        let loaded = get_settings_with_paths(&paths).expect("settings load");

        assert!(!loaded.auto_start_software);
        assert!(loaded.auto_start_gateway);
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
    fn missing_locale_loads_as_frontend_resolved_default_marker() {
        let root = temp_root("settings-missing-locale");
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

        assert_eq!(loaded.locale, "");
        assert_eq!(loaded.proxy_port, 4555);
    }

    #[test]
    fn invalid_locale_saves_as_english_default() {
        let root = temp_root("settings-invalid-locale");
        let paths = test_paths(&root);

        let saved = save_settings_with_paths(
            Settings {
                locale: "fr-FR".to_string(),
                ..Settings::default()
            },
            &paths,
        )
        .expect("settings save");
        let written = fs::read_to_string(paths.settings_path()).expect("settings text");

        assert_eq!(saved.locale, "en-US");
        assert!(written.contains("\"locale\": \"en-US\""));
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
        assert_eq!(
            status.gateway_lifecycle,
            crate::gateway_transaction::GatewayLifecyclePhase::Unavailable
        );
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
        assert_arg_literal(&commands[0].args, "--gateway-key", "codexhub-proxy");
        assert_arg_literal(&commands[0].args, "--owner", "release");
        assert_eq!(
            paths
                .config_backup_path()
                .file_name()
                .and_then(|name| name.to_str()),
            Some("config.toml.release.backup")
        );
        assert!(!commands[0].args.iter().any(|arg| arg == "normalize-fast"));
        assert_eq!(status.history_sync_status, None);
        assert_eq!(status.history_sync_message, None);
    }

    #[test]
    fn isolated_paths_keep_beta_runtime_artifacts_out_of_codex_target() {
        let root = temp_root("isolated-beta-paths");
        let runtime = root.join(".codexhub-beta");
        let target = root.join(".codex");
        let paths = ConfigPaths::new_isolated(&runtime, &target, root.join("repo"));

        assert_eq!(paths.settings_path(), runtime.join("proxy/settings.json"));
        assert_eq!(paths.config_backup_path(), runtime.join("proxy/config.toml.release.backup"));
        assert_eq!(paths.codex_config_path(), target.join("config.toml"));
        assert_eq!(paths.generated_catalog_path(), runtime.join("model-catalogs/codexhub-model-catalog.json"));
    }

    #[test]
    fn codex_cross_channel_change_requires_explicit_takeover() {
        assert!(ensure_codex_owner_mutation_allowed(
            crate::app_flavor::RoutingOwner::Beta,
            Some(crate::app_flavor::RoutingOwner::Release),
            "custom",
            false,
        )
        .is_err());
        assert!(ensure_codex_owner_mutation_allowed(
            crate::app_flavor::RoutingOwner::Beta,
            Some(crate::app_flavor::RoutingOwner::Release),
            "custom",
            true,
        )
        .is_ok());
        assert!(ensure_codex_owner_mutation_allowed(
            crate::app_flavor::RoutingOwner::Beta,
            Some(crate::app_flavor::RoutingOwner::Release),
            "official",
            true,
        )
        .is_ok());
    }

    #[test]
    fn beta_can_explicitly_switch_stable_owned_codex_to_unified_official() {
        let root = temp_root("beta-force-stable-to-official");
        let target = root.join(".codex");
        let repo = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .unwrap()
            .to_path_buf();
        let stable_paths = ConfigPaths::new_isolated(&target, &target, &repo);
        let beta_paths = ConfigPaths::new_isolated(root.join(".codexhub-beta"), &target, repo);
        fs::create_dir_all(&target).unwrap();
        fs::write(
            stable_paths.codex_config_path(),
            b"model = \"gpt-5.4\"\nmodel_reasoning_effort = \"high\"\n",
        )
        .unwrap();
        let catalog_path = stable_paths.generated_catalog_path();
        fs::create_dir_all(catalog_path.parent().unwrap()).unwrap();
        fs::write(
            catalog_path,
            r#"{
  "models": [
    {
      "slug": "gpt-5.4",
      "codex_proxy_metadata": {
        "provider": "openai",
        "upstream_name": "official",
        "official_context_budget": {
          "source": "current_direct_official",
          "freshness": "fresh",
          "model_context_window": 300000,
          "effective_context_window_percent": 100,
          "effective_context_window": 300000,
          "model_auto_compact_token_limit": 270000
        }
      }
    }
  ]
}"#,
        )
        .unwrap();
        save_settings_with_paths(Settings::default(), &stable_paths).unwrap();
        save_settings_with_paths(Settings::default(), &beta_paths).unwrap();
        let python = super::find_python();
        let runner = ProcessCommandRunner;

        switch_mode_with_paths_takeover_as_owner(
            crate::app_flavor::RoutingOwner::Release,
            "custom",
            false,
            &stable_paths,
            &python,
            &runner,
        )
        .unwrap();

        switch_mode_with_paths_takeover_as_owner(
            crate::app_flavor::RoutingOwner::Beta,
            "official",
            true,
            &beta_paths,
            &python,
            &runner,
        )
        .unwrap();

        let restored = fs::read_to_string(beta_paths.codex_config_path()).unwrap();
        assert!(restored.contains("model_provider = \"custom\""));
        assert!(restored.contains("name = \"OpenAI\""));
        assert!(restored.contains("model = \"gpt-5.4\""));
        assert!(restored.contains("model_reasoning_effort = \"high\""));
        assert!(!restored.contains("# owner = release"));
        assert!(!restored.contains("base_url"));
    }

    #[test]
    fn beta_requires_explicit_takeover_for_unowned_and_official_codex() {
        for owner in [None, Some(crate::app_flavor::RoutingOwner::Official)] {
            let error = ensure_codex_owner_mutation_allowed(
                crate::app_flavor::RoutingOwner::Beta,
                owner,
                "custom",
                false,
            )
            .expect_err("Beta must never silently claim real Codex");
            assert_eq!(error.code(), "route.takeover_required");

            assert!(ensure_codex_owner_mutation_allowed(
                crate::app_flavor::RoutingOwner::Beta,
                owner,
                "custom",
                true,
            )
            .is_ok());
        }
    }

    #[test]
    fn stable_keeps_backward_compatible_unowned_and_official_connect() {
        for owner in [None, Some(crate::app_flavor::RoutingOwner::Official)] {
            assert!(ensure_codex_owner_mutation_allowed(
                crate::app_flavor::RoutingOwner::Release,
                owner,
                "custom",
                false,
            )
            .is_ok());
        }
    }

    #[test]
    fn beta_backend_takeover_chain_with_default_unified_history_preserves_the_custom_bucket() {
        for (name, original) in [
            ("unowned", b"model_reasoning_effort = \"high\"\r\n".as_slice()),
            (
                "official",
                b"model_provider = \"openai\"\nmodel_reasoning_effort = \"medium\"\n".as_slice(),
            ),
            (
                "stable",
                b"# BEGIN CODEX PROXY SESSION CONFIG\n# owner = release\n# END CODEX PROXY SESSION CONFIG\nmodel_reasoning_effort = \"high\"\n".as_slice(),
            ),
        ] {
            let root = temp_root(name);
            let runtime = root.join(".codexhub-beta");
            let target = root.join(".codex");
            let repo = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
                .parent()
                .unwrap()
                .to_path_buf();
            let paths = ConfigPaths::new_isolated(&runtime, &target, repo);
            fs::create_dir_all(&target).unwrap();
            fs::write(paths.codex_config_path(), original).unwrap();
            save_settings_with_paths(
                Settings {
                    proxy_port: 9109,
                    ..Settings::default()
                },
                &paths,
            )
            .unwrap();
            let python = super::find_python();
            let runner = ProcessCommandRunner;

            let rejected = switch_mode_with_paths_takeover_as_owner(
                crate::app_flavor::RoutingOwner::Beta,
                "custom",
                false,
                &paths,
                &python,
                &runner,
            )
            .expect_err("normal Beta connect must be rejected");
            assert!(rejected.contains("route.takeover_required"));
            assert_eq!(fs::read(paths.codex_config_path()).unwrap(), original);
            assert!(!paths.config_backup_path_for_owner(crate::app_flavor::RoutingOwner::Beta).exists());

            switch_mode_with_paths_takeover_as_owner(
                crate::app_flavor::RoutingOwner::Beta,
                "custom",
                true,
                &paths,
                &python,
                &runner,
            )
            .unwrap();
            switch_mode_with_paths_takeover_as_owner(
                crate::app_flavor::RoutingOwner::Beta,
                "custom",
                false,
                &paths,
                &python,
                &runner,
            )
            .unwrap();
            switch_mode_with_paths_takeover_as_owner(
                crate::app_flavor::RoutingOwner::Beta,
                "official",
                false,
                &paths,
                &python,
                &runner,
            )
            .unwrap();

            let restored = fs::read_to_string(paths.codex_config_path()).unwrap();
            if name == "stable" {
                assert_eq!(restored.as_bytes(), original);
            } else {
                assert!(restored.contains("model_provider = \"custom\""));
                assert!(restored.contains("[model_providers.custom]"));
                assert!(restored.contains("name = \"OpenAI\""));
                assert!(!restored.contains("base_url"));
                assert!(restored.contains("model_reasoning_effort"));
            }
            assert!(!paths.config_backup_path_for_owner(crate::app_flavor::RoutingOwner::Beta).exists());
        }
    }

    #[test]
    fn stable_normal_connect_then_official_reconciles_unified_history() {
        let root = temp_root("stable-normal-unified-restore");
        let runtime = root.join(".codexhub");
        let target = root.join(".codex");
        let repo = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .unwrap()
            .to_path_buf();
        let paths = ConfigPaths::new_isolated(&runtime, &target, repo);
        fs::create_dir_all(&target).unwrap();
        fs::write(
            paths.codex_config_path(),
            b"model_reasoning_effort = \"high\"\r\n",
        )
        .unwrap();
        save_settings_with_paths(Settings::default(), &paths).unwrap();
        let python = super::find_python();
        let runner = ProcessCommandRunner;

        switch_mode_with_paths_takeover_as_owner(
            crate::app_flavor::RoutingOwner::Release,
            "custom",
            false,
            &paths,
            &python,
            &runner,
        )
        .unwrap();
        switch_mode_with_paths_takeover_as_owner(
            crate::app_flavor::RoutingOwner::Release,
            "official",
            false,
            &paths,
            &python,
            &runner,
        )
        .unwrap();

        let restored = fs::read_to_string(paths.codex_config_path()).unwrap();
        assert!(restored.contains("model_provider = \"custom\""));
        assert!(restored.contains("[model_providers.custom]"));
        assert!(restored.contains("name = \"OpenAI\""));
        assert!(restored.contains("requires_openai_auth = true"));
        assert!(!paths.config_backup_path().exists());
    }

    #[test]
    fn stable_same_owner_force_with_missing_backup_disconnects_to_unified_official() {
        let root = temp_root("stable-same-owner-force");
        let runtime = root.join(".codexhub");
        let target = root.join(".codex");
        let repo = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .unwrap()
            .to_path_buf();
        let paths = ConfigPaths::new_isolated(&runtime, &target, repo);
        fs::create_dir_all(&target).unwrap();
        save_settings_with_paths(Settings::default(), &paths).unwrap();
        let python = super::find_python();
        let runner = ProcessCommandRunner;

        switch_mode_with_paths_takeover_as_owner(
            crate::app_flavor::RoutingOwner::Release,
            "custom",
            false,
            &paths,
            &python,
            &runner,
        )
        .unwrap();
        fs::remove_file(paths.config_backup_path()).unwrap();
        switch_mode_with_paths_takeover_as_owner(
            crate::app_flavor::RoutingOwner::Release,
            "custom",
            true,
            &paths,
            &python,
            &runner,
        )
        .unwrap();

        let metadata = paths
            .config_backup_path()
            .with_file_name("config.toml.release.backup.takeover.json");
        assert!(!metadata.exists());

        switch_mode_with_paths_takeover_as_owner(
            crate::app_flavor::RoutingOwner::Release,
            "official",
            false,
            &paths,
            &python,
            &runner,
        )
        .unwrap();

        let restored = fs::read_to_string(paths.codex_config_path()).unwrap();
        assert!(restored.contains("name = \"OpenAI\""));
        assert!(!restored.contains("name = \"Codex Proxy\""));
        assert!(!restored.contains("base_url"));
    }

    #[test]
    fn codex_foreign_disconnect_requires_explicit_takeover() {
        assert!(ensure_codex_owner_mutation_allowed(
            crate::app_flavor::RoutingOwner::Beta,
            Some(crate::app_flavor::RoutingOwner::Release),
            "official",
            false,
        )
        .is_err());
        assert!(ensure_codex_owner_mutation_allowed(
            crate::app_flavor::RoutingOwner::Beta,
            Some(crate::app_flavor::RoutingOwner::Release),
            "official",
            true,
        )
        .is_ok());
        assert!(ensure_codex_owner_mutation_allowed(
            crate::app_flavor::RoutingOwner::Beta,
            Some(crate::app_flavor::RoutingOwner::Beta),
            "official",
            false,
        )
        .is_ok());
    }

    #[test]
    fn codex_overlay_owner_is_detected_from_managed_marker() {
        let text = "# BEGIN CODEX PROXY SESSION CONFIG\n# owner = beta\n# END CODEX PROXY SESSION CONFIG\n";
        assert_eq!(
            codex_overlay_owner(text),
            Some(crate::app_flavor::RoutingOwner::Beta)
        );
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
        assert_eq!(
            paths
                .config_backup_path()
                .file_name()
                .and_then(|name| name.to_str()),
            Some("config.toml.release.backup")
        );
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

    #[test]
    fn context_guard_command_keeps_codex_and_gateway_state_in_sync() {
        let root = temp_root("context-guard-command");
        let paths = test_paths(&root);
        let status_json = r#"{"enabled":true,"model_context_window":272000,"model_auto_compact_token_limit":240000}"#;
        let set_runner = RecordingRunner::sequence(vec![CommandOutcome {
            code: Some(0),
            stdout: status_json.to_string(),
            stderr: String::new(),
        }]);

        let status = set_codex_context_guard_with_paths(
            true,
            &paths,
            Path::new("python-test"),
            &set_runner,
        )
        .expect("context guard enabled");

        assert!(status.enabled);
        assert!(status.codex_enabled);
        assert!(status.gateway_enabled);
        assert_eq!(status.model_context_window, Some(272_000));
        assert_eq!(status.model_auto_compact_token_limit, Some(240_000));
        assert!(
            get_settings_with_paths(&paths)
                .expect("saved settings")
                .openai_context_guard_enabled
        );
        let set_commands = set_runner.commands.borrow();
        assert_eq!(set_commands.len(), 1);
        assert_contains_sequence(
            &set_commands[0].args,
            &[
                "context-guard-set",
                "--config",
                "--backup",
                "--state",
                "--enabled",
                "true",
            ],
        );
        drop(set_commands);

        let get_runner = RecordingRunner::sequence(vec![CommandOutcome {
            code: Some(0),
            stdout: status_json.to_string(),
            stderr: String::new(),
        }]);
        let refreshed = get_codex_context_guard_status_with_paths(
            &paths,
            Path::new("python-test"),
            &get_runner,
        )
        .expect("context guard status");
        assert!(refreshed.enabled);
        assert_contains_sequence(
            &get_runner.commands.borrow()[0].args,
            &["context-guard-status", "--config"],
        );
    }

    #[test]
    fn runtime_projection_recognizes_only_an_explicit_official_top_level_model() {
        assert!(top_level_model_is_official("model = \"gpt-5.6-terra\"\n"));
        assert!(top_level_model_is_official(
            "model = 'openai/gpt-5.6-terra'\n"
        ));
        assert!(!top_level_model_is_official("model = \"volc/glm-5.2\"\n"));
        assert!(!top_level_model_is_official(
            "[profiles.work]\nmodel = \"gpt-5.6-terra\"\n"
        ));
    }

    #[test]
    fn refreshed_budget_reapplies_the_owned_codex_overlay_from_the_published_catalog() {
        let root = temp_root("republish-owned-context-budget");
        let paths = test_paths(&root);
        fs::create_dir_all(paths.codex_config_path().parent().unwrap()).unwrap();
        fs::write(
            paths.codex_config_path(),
            concat!(
                "# BEGIN CODEX PROXY SESSION CONFIG\n",
                "# owner = release\n",
                "# END CODEX PROXY SESSION CONFIG\n",
                "model = \"gpt-5.6-terra\"\n",
            ),
        )
        .unwrap();
        save_settings_with_paths(Settings::default(), &paths).expect("settings save");
        let runner = RecordingRunner::successful();

        let changed = republish_managed_codex_context_budget_with_paths(
            &paths,
            Path::new("python-test"),
            &runner,
        )
        .expect("owned runtime projection");

        assert!(
            !changed,
            "the recording runner leaves the config text unchanged"
        );
        let commands = runner.commands.borrow();
        assert_eq!(commands.len(), 1);
        assert_contains_sequence(
            &commands[0].args,
            &[
                "apply",
                "--config",
                "--backup",
                "--catalog",
                "--owner",
                "release",
            ],
        );
        assert_arg_value(
            &commands[0].args,
            "--catalog",
            &paths.generated_catalog_path(),
        );
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
        assert_eq!(left.locale, right.locale);
        assert_eq!(left.auto_sync_history, right.auto_sync_history);
        assert_eq!(left.unified_codex_history, right.unified_codex_history);
        assert_eq!(left.auto_start_software, right.auto_start_software);
        assert_eq!(left.auto_start_gateway, right.auto_start_gateway);
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
            left.openai_context_guard_enabled,
            right.openai_context_guard_enabled
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

    mod isolated_codex_managed_config {
        use super::super::{
            apply_codex_config_isolated, populate_isolated_repo_resources,
            preview_codex_config_isolated, readback_codex_config_isolated,
        };
        use super::{
            assert_arg_literal, assert_arg_value, assert_contains_sequence, save_settings_with_paths,
            temp_root, CommandOutcome, CommandRunner, ConfigPaths, RecordedCommand, Settings,
        };
        use std::cell::RefCell;
        use std::fs;
        use std::path::Path;

        struct RecordingRunner {
            commands: RefCell<Vec<RecordedCommand>>,
        }

        impl RecordingRunner {
            fn successful() -> Self {
                Self {
                    commands: RefCell::new(Vec::new()),
                }
            }
        }

        impl CommandRunner for RecordingRunner {
            fn run(&self, _program: &Path, args: &[String]) -> Result<CommandOutcome, String> {
                self.commands.borrow_mut().push(RecordedCommand {
                    args: args.to_vec(),
                });
                Ok(CommandOutcome {
                    code: Some(0),
                    stdout: String::new(),
                    stderr: String::new(),
                })
            }
        }

        fn isolated_paths(root: &Path) -> ConfigPaths {
            ConfigPaths::new_isolated(
                root.join("runtime"),
                root.join("codex-target"),
                root.join("repo"),
            )
        }

        /// A config.toml that mirrors the production overlay output: the
        /// `# owner = release|beta` marker, `model_provider = "custom"`, and
        /// `[model_providers.custom]` with `wire_api = "responses"`. Used by
        /// readback tests that need a fully-bound, overlay-produced file.
        fn overlay_managed_config(owner: &str, model: &str) -> String {
            format!(
                "# owner = {owner}\n\
                 model = \"{model}\"\n\
                 model_provider = \"custom\"\n\
                 openai_base_url = \"http://127.0.0.1:9099/v1\"\n\
                 [model_providers.custom]\n\
                 name = \"Codex Proxy\"\n\
                 requires_openai_auth = true\n\
                 supports_websockets = true\n\
                 wire_api = \"responses\"\n",
            )
        }

        #[test]
        fn codex_preview_under_isolated_root_reports_relative_target_and_no_secret() {
            let root = temp_root("codex-preview-isolated");
            let paths = isolated_paths(&root);
            let preview = preview_codex_config_isolated(&paths, "custom", "gpt-5.6-luna").unwrap();

            assert_eq!(preview.client_id, "codex");
            // F4: the Codex preview now reports the real overlay provider/route
            // binding (model_provider = "custom", wire_api = "responses").
            assert_eq!(preview.selector, "custom/gpt-5.6-luna");
            assert_eq!(preview.model, "gpt-5.6-luna");
            assert_eq!(preview.route_protocol, "responses");
            assert!(preview.target_names.iter().all(|name| {
                !name.contains(':') && !name.starts_with('/') && !name.starts_with('\\')
            }));
            let json = serde_json::to_string(&preview).unwrap();
            assert!(
                !json.contains(&root.to_string_lossy().to_string()),
                "absolute path leaked: {json}"
            );
        }

        #[test]
        fn codex_apply_under_isolated_root_invokes_overlay_with_isolated_paths() {
            let root = temp_root("codex-apply-isolated");
            let paths = isolated_paths(&root);
            fs::create_dir_all(paths.proxy_dir()).unwrap();
            // Settings required by switch_mode to build base-url and gateway-key.
            save_settings_with_paths(
                Settings {
                    proxy_port: 9099,
                    gateway_client_key: "isolated-key".to_string(),
                    ..Settings::default()
                },
                &paths,
            )
            .unwrap();
            let runner = RecordingRunner::successful();

            let result =
                apply_codex_config_isolated(&paths, "custom", false, "gpt-5.6-luna", Path::new("python-test"), &runner)
                    .unwrap();
            assert_eq!(result.mode, "custom");

            let commands = runner.commands.borrow();
            assert_eq!(commands.len(), 1);
            assert_contains_sequence(&commands[0].args, &["apply"]);
            assert_arg_value(&commands[0].args, "--config", &paths.codex_config_path());
            assert_arg_value(&commands[0].args, "--backup", &paths.config_backup_path());
            assert_arg_value(&commands[0].args, "--catalog", &paths.generated_catalog_path());
            assert_arg_literal(&commands[0].args, "--base-url", "http://127.0.0.1:9099");
            assert_arg_literal(&commands[0].args, "--gateway-key", "isolated-key");
            // All config/backup/catalog paths stay beneath the isolated root.
            assert!(paths.codex_config_path().starts_with(&root));
            assert!(paths.config_backup_path().starts_with(&root));
            assert!(paths.generated_catalog_path().starts_with(&root));
        }

        // F3: the isolated Codex apply path must populate the isolated repo
        // with the real production overlay resources (src-python modules and
        // the bundled providers.toml) so `config_overlay.py` and its sibling
        // imports resolve without host discovery. The apply step invokes the
        // production Python overlay by absolute script path; without this
        // population the script and its imports would not exist under the
        // isolated root.
        #[test]
        fn populate_isolated_repo_resources_copies_production_overlay_modules() {
            let root = temp_root("codex-populate-repo");
            let paths = isolated_paths(&root);
            populate_isolated_repo_resources(&paths).unwrap();

            // The overlay script itself must exist under the isolated repo.
            let overlay = paths.config_overlay_script();
            assert!(
                overlay.is_file(),
                "config_overlay.py must be copied to isolated repo: {}",
                overlay.display()
            );
            // The sibling modules the overlay imports must also be present.
            for module in ["atomic_io.py", "model_limits.py"] {
                let module_path = paths.repo_root.join("src-python").join(module);
                assert!(
                    module_path.is_file(),
                    "overlay sibling module {module} must be copied: {}",
                    module_path.display()
                );
            }
            // The bundled providers.toml referenced by providers_config must
            // exist beneath the isolated repo so no host config/ discovery
            // leaks into the isolated apply path.
            assert!(
                paths.bundled_providers_path().is_file(),
                "bundled providers.toml must be copied to isolated repo"
            );
            // Everything copied stays beneath the isolated root.
            assert!(overlay.starts_with(&root));
            assert!(paths.bundled_providers_path().starts_with(&root));
        }

        #[test]
        fn codex_readback_under_isolated_root_verifies_overlay_owner_marker() {
            let root = temp_root("codex-readback-isolated");
            let paths = isolated_paths(&root);
            fs::create_dir_all(paths.codex_config_path().parent().unwrap()).unwrap();
            // No config.toml present -> readback fails closed (missing).
            let error = readback_codex_config_isolated(&paths, "gpt-5.6-luna").unwrap_err();
            assert!(
                error.contains("missing") || error.contains("absent"),
                "unexpected error: {error}"
            );

            // Write a fully-bound overlay-produced config.toml; readback must
            // confirm owner, model_provider, and wire_api all match.
            let owner_marker = match crate::app_flavor::current().routing_owner() {
                crate::app_flavor::RoutingOwner::Beta => "beta",
                _ => "release",
            };
            fs::write(
                paths.codex_config_path(),
                overlay_managed_config(owner_marker, "gpt-5.6-luna"),
            )
            .unwrap();
            let readback = readback_codex_config_isolated(&paths, "gpt-5.6-luna").unwrap();
            assert_eq!(readback.client_id, "codex");
            assert!(readback.ok);
            // F4: readback surfaces the real overlay route binding.
            assert_eq!(readback.selector, "custom/gpt-5.6-luna");
            assert_eq!(readback.route_protocol, "responses");
        }

        #[test]
        fn codex_readback_fails_closed_on_mismatched_stale_owner_marker() {
            let root = temp_root("codex-readback-stale-owner");
            let paths = isolated_paths(&root);
            fs::create_dir_all(paths.codex_config_path().parent().unwrap()).unwrap();
            // The current app flavor is Stable (RoutingOwner::Release); a beta
            // owner marker is a stale, cross-channel owner that readback must
            // reject without mutating the file. The provider binding is
            // production-shaped so the failure is owner-only, not provider.
            fs::write(
                paths.codex_config_path(),
                overlay_managed_config("beta", "gpt-5.6-luna"),
            )
            .unwrap();
            let error = readback_codex_config_isolated(&paths, "gpt-5.6-luna").unwrap_err();
            assert!(
                error.contains("stale") || error.contains("owner"),
                "unexpected error: {error}"
            );
            // Readback must fail closed without altering the file.
            assert_eq!(
                fs::read_to_string(paths.codex_config_path()).unwrap(),
                overlay_managed_config("beta", "gpt-5.6-luna"),
            );
        }

        #[test]
        fn codex_readback_fails_closed_on_absent_owner_marker() {
            let root = temp_root("codex-readback-absent-owner");
            let paths = isolated_paths(&root);
            fs::create_dir_all(paths.codex_config_path().parent().unwrap()).unwrap();
            // A config.toml with no `# owner = ...` marker was not produced
            // by this app's overlay; readback must fail closed. (The provider
            // here is intentionally "openai", not "custom", so a future
            // relaxation of the owner check would still fail on provider.)
            fs::write(
                paths.codex_config_path(),
                "model = \"gpt-5.6-luna\"\nmodel_provider = \"openai\"\n",
            )
            .unwrap();
            let error = readback_codex_config_isolated(&paths, "gpt-5.6-luna").unwrap_err();
            assert!(
                error.contains("stale") || error.contains("absent") || error.contains("owner"),
                "unexpected error: {error}"
            );
            assert_eq!(
                fs::read_to_string(paths.codex_config_path()).unwrap(),
                "model = \"gpt-5.6-luna\"\nmodel_provider = \"openai\"\n",
            );
        }

        #[test]
        fn codex_readback_fails_closed_when_provider_binding_is_absent() {
            let root = temp_root("codex-readback-no-provider");
            let paths = isolated_paths(&root);
            fs::create_dir_all(paths.codex_config_path().parent().unwrap()).unwrap();
            // Owner marker is valid but model_provider is missing — this was
            // not produced by the overlay; readback must fail closed.
            let owner_marker = match crate::app_flavor::current().routing_owner() {
                crate::app_flavor::RoutingOwner::Beta => "beta",
                _ => "release",
            };
            fs::write(
                paths.codex_config_path(),
                format!("# owner = {owner_marker}\nmodel = \"gpt-5.6-luna\"\n"),
            )
            .unwrap();
            let error = readback_codex_config_isolated(&paths, "gpt-5.6-luna").unwrap_err();
            assert!(
                error.contains("model_provider") || error.contains("provider"),
                "unexpected error: {error}"
            );
        }

        #[test]
        fn codex_readback_fails_closed_when_wire_api_is_not_responses() {
            let root = temp_root("codex-readback-wrong-wire-api");
            let paths = isolated_paths(&root);
            fs::create_dir_all(paths.codex_config_path().parent().unwrap()).unwrap();
            // Owner + provider are valid but wire_api is "chat_completions"
            // — the overlay always writes "responses", so this is a stale or
            // hand-edited config; readback must fail closed.
            let owner_marker = match crate::app_flavor::current().routing_owner() {
                crate::app_flavor::RoutingOwner::Beta => "beta",
                _ => "release",
            };
            fs::write(
                paths.codex_config_path(),
                format!(
                    "# owner = {owner_marker}\n\
                     model = \"gpt-5.6-luna\"\n\
                     model_provider = \"custom\"\n\
                     [model_providers.custom]\n\
                     name = \"Codex Proxy\"\n\
                     wire_api = \"chat_completions\"\n",
                ),
            )
            .unwrap();
            let error = readback_codex_config_isolated(&paths, "gpt-5.6-luna").unwrap_err();
            assert!(
                error.contains("wire_api") || error.contains("responses"),
                "unexpected error: {error}"
            );
        }
    }
}
