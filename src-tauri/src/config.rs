use crate::{runtime_paths, safe_file, AppStatus, Provider, Settings};
use serde::{Deserialize, Serialize};
use std::collections::HashSet;
use std::ffi::OsString;
use std::fs;
use std::path::{Path, PathBuf};

pub fn get_providers() -> Result<Vec<Provider>, String> {
    get_providers_with_paths(&ConfigPaths::runtime()?)
}

pub fn get_bundled_providers() -> Result<Vec<Provider>, String> {
    let mut candidates = Vec::new();
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            candidates.push(dir.join("config").join("providers.toml"));
        }
    }
    if let Ok(cwd) = std::env::current_dir() {
        candidates.push(cwd.join("config").join("providers.toml"));
    }
    let paths = ConfigPaths::runtime()?;
    candidates.push(paths.bundled_providers_path());
    let mut last_error = String::from("bundled providers.toml not found");
    for path in candidates {
        if !path.exists() {
            continue;
        }
        match load_providers_from_path(&path) {
            Ok(providers) if !providers.is_empty() => return Ok(providers),
            Ok(_) => last_error = format!("bundled providers.toml is empty: {}", path.display()),
            Err(error) => last_error = error,
        }
    }
    Err(last_error)
}

pub fn save_providers(providers: Vec<Provider>) -> Result<Vec<Provider>, String> {
    crate::codex_desktop::serialize_config_writer(|| {
        save_providers_with_paths(providers, &ConfigPaths::runtime()?)
    })
}

pub fn get_settings() -> Result<Settings, String> {
    get_settings_with_paths(&ConfigPaths::runtime()?)
}

pub fn save_settings(settings: Settings) -> Result<Settings, String> {
    crate::codex_desktop::serialize_config_writer(|| {
        save_settings_with_paths(settings, &ConfigPaths::runtime()?)
    })
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct CodexContextGuardStatus {
    pub enabled: bool,
    pub codex_enabled: bool,
    pub gateway_enabled: bool,
    pub model_context_window: Option<u32>,
    pub model_auto_compact_token_limit: Option<u32>,
    pub global_override_conflict: bool,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub codex_restart_result: Option<crate::codex_desktop::CodexRestartResult>,
}

#[derive(Debug, Deserialize)]
struct CodexConfigContextGuardStatus {
    enabled: bool,
    model_context_window: Option<u32>,
    model_auto_compact_token_limit: Option<u32>,
    #[serde(default)]
    global_override_conflict: bool,
}

pub fn get_codex_context_guard_status() -> Result<CodexContextGuardStatus, String> {
    let paths = ConfigPaths::runtime()?;
    let python = find_python()?;
    get_codex_context_guard_status_with_paths(&paths, &python, &ProcessCommandRunner)
}

pub fn set_codex_context_guard(enabled: bool) -> Result<CodexContextGuardStatus, String> {
    let paths = ConfigPaths::runtime()?;
    let python = find_python()?;
    set_codex_context_guard_with_paths(enabled, &paths, &python, &ProcessCommandRunner)
}

/// Reapply only the CodexHub-managed runtime context projection after a new
/// Official catalog snapshot has published.  This intentionally ignores
/// unowned and cross-channel Codex configuration.
pub(crate) fn republish_managed_codex_context_budget() -> Result<bool, String> {
    let paths = ConfigPaths::runtime()?;
    let python = find_python()?;
    republish_managed_codex_context_budget_with_paths(&paths, &python, &ProcessCommandRunner)
}

pub(crate) fn managed_codex_projection_transaction_paths() -> Result<Vec<PathBuf>, String> {
    let paths = ConfigPaths::runtime()?;
    Ok(managed_codex_projection_transaction_paths_with_paths(
        &paths,
        crate::app_flavor::current().routing_owner(),
    ))
}

fn managed_codex_projection_transaction_paths_with_paths(
    paths: &ConfigPaths,
    current_owner: crate::app_flavor::RoutingOwner,
) -> Vec<PathBuf> {
    let mut transaction_paths = vec![
        paths.codex_config_path(),
        paths.context_guard_state_path(),
        paths.settings_path(),
    ];
    for target_owner in [
        crate::app_flavor::RoutingOwner::Release,
        crate::app_flavor::RoutingOwner::Beta,
    ] {
        let backup = paths.config_backup_path_for_target_owner(current_owner, target_owner);
        if !transaction_paths.contains(&backup) {
            transaction_paths.push(backup.clone());
        }
        let takeover_metadata = takeover_metadata_path(&backup);
        if !transaction_paths.contains(&takeover_metadata) {
            transaction_paths.push(takeover_metadata);
        }
    }
    transaction_paths
}

fn takeover_metadata_path(backup_path: &Path) -> PathBuf {
    let mut name = backup_path
        .file_name()
        .unwrap_or_default()
        .to_os_string();
    name.push(".takeover.json");
    backup_path.with_file_name(name)
}

/// Migrate legacy CodexHub-owned global context values without changing the
/// active route.  This is deliberately independent of the overlay owner and
/// selected model so an upgraded install is repaired even when a Stable/Beta
/// backup belongs to the other channel or the active model is third-party.
pub(crate) fn migrate_legacy_context_guard() -> Result<bool, String> {
    let paths = ConfigPaths::runtime()?;
    let python = find_python()?;
    migrate_legacy_context_guard_with_paths(&paths, &python, &ProcessCommandRunner)
}

pub fn switch_mode_with_takeover(
    mode: &str,
    auto_sync: bool,
    force_takeover: bool,
) -> Result<AppStatus, String> {
    let paths = ConfigPaths::runtime()?;
    let python = find_python()?;
    let runner = ProcessCommandRunner;

    if mode == "custom" {
        crate::catalog::generate_catalog_with_existing_lock()?;
    }
    let mut status =
        switch_mode_with_paths_takeover(mode, auto_sync, force_takeover, &paths, &python, &runner)?;
    merge_post_switch_gateway_status(&mut status, crate::proxy::status());
    let settings = get_settings_with_paths(&paths).unwrap_or_default();
    let target_provider = if mode == "custom" || settings.unified_codex_history {
        "custom"
    } else {
        "openai"
    };
    apply_history_sync_result(
        &mut status,
        crate::history::reconcile_after_confirmed_route_switch(Some(target_provider)),
    );

    Ok(status)
}

fn merge_post_switch_gateway_status(status: &mut AppStatus, lifecycle: Result<AppStatus, String>) {
    match lifecycle {
        Ok(lifecycle) => {
            status.proxy_running = lifecycle.proxy_running;
            status.proxy_port = lifecycle.proxy_port;
            status.proxy_build = lifecycle.proxy_build;
            status.gateway_lifecycle = lifecycle.gateway_lifecycle;
        }
        Err(error) => {
            // The route overlay is already durably committed at this point.
            // A best-effort Gateway readback must not turn that success into a
            // switch failure, otherwise the lifecycle coordinator would reopen
            // Codex under the newly written route while reporting it as old.
            status.message = format!(
                "{}; route committed, but Gateway status readback failed: {error}",
                status.message
            );
        }
    }
}

fn apply_history_sync_result(
    status: &mut AppStatus,
    result: Result<crate::history::UnifiedHistoryResult, String>,
) {
    match result {
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

    pub(crate) fn context_guard_state_path(&self) -> PathBuf {
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

const COMMITTED_CLEANUP_WARNING_PREFIX: &str =
    "warning: route committed; backup cleanup deferred (";

impl CommandOutcome {
    fn committed_cleanup_warning(&self) -> Option<&str> {
        self.stderr
            .lines()
            .map(str::trim)
            .find(|line| line.starts_with(COMMITTED_CLEANUP_WARNING_PREFIX))
    }
}

pub(crate) trait CommandRunner {
    fn run(&self, program: &Path, args: &[String]) -> Result<CommandOutcome, String>;
}

pub(crate) struct ProcessCommandRunner;

impl CommandRunner for ProcessCommandRunner {
    fn run(&self, program: &Path, args: &[String]) -> Result<CommandOutcome, String> {
        let mut command = runtime_paths::configured_python_command(program);
        command.args(args);
        crate::runtime_paths::configure_no_window(&mut command);
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
    load_providers_from_path(&path)
}

#[cfg(test)]
pub(crate) fn get_bundled_providers_with_paths(
    paths: &ConfigPaths,
) -> Result<Vec<Provider>, String> {
    load_providers_from_path(&paths.bundled_providers_path())
}

fn load_providers_from_path(path: &Path) -> Result<Vec<Provider>, String> {
    let text = fs::read_to_string(path)
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

pub(crate) fn get_settings_with_paths(paths: &ConfigPaths) -> Result<Settings, String> {
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
    let codex_status: CodexConfigContextGuardStatus = serde_json::from_str(outcome.stdout.trim())
        .map_err(|error| {
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
    let backup_path = paths.config_backup_path_for_target_owner(current_app_owner, target_owner);
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
            paths
                .generated_catalog_path()
                .to_string_lossy()
                .into_owned(),
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
            "--context-guard-state".to_string(),
            paths
                .context_guard_state_path()
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

pub(crate) fn migrate_legacy_context_guard_with_paths(
    paths: &ConfigPaths,
    python: &Path,
    runner: &dyn CommandRunner,
) -> Result<bool, String> {
    ensure_mode_switch_directories(paths)?;
    let config_path = paths.codex_config_path();
    let current_app_owner = crate::app_flavor::current().routing_owner();
    let backup_owner = fs::read_to_string(&config_path)
        .ok()
        .as_deref()
        .and_then(codex_overlay_owner)
        .unwrap_or(current_app_owner);
    let selected_backup =
        paths.config_backup_path_for_target_owner(current_app_owner, backup_owner);
    let mut backup_paths = vec![selected_backup];
    // Stable and Beta may leave a backup in the other runtime directory after
    // a channel switch.  Startup migration is deliberately independent of the
    // active overlay owner, so inspect every existing CodexHub backup once.
    for target_owner in [
        crate::app_flavor::RoutingOwner::Release,
        crate::app_flavor::RoutingOwner::Beta,
    ] {
        let candidate = paths.config_backup_path_for_target_owner(current_app_owner, target_owner);
        if candidate.exists() && !backup_paths.contains(&candidate) {
            backup_paths.push(candidate);
        }
    }
    let before_config = fs::read(&config_path).unwrap_or_default();
    let before_backups = backup_paths
        .iter()
        .map(|path| fs::read(path).unwrap_or_default())
        .collect::<Vec<_>>();
    let mut args = vec![
        "migrate-context-guard".to_string(),
        "--config".to_string(),
        config_path.to_string_lossy().into_owned(),
    ];
    for backup_path in &backup_paths {
        args.extend([
            "--backup".to_string(),
            backup_path.to_string_lossy().into_owned(),
        ]);
    }
    args.extend([
        "--context-guard-state".to_string(),
        paths
            .context_guard_state_path()
            .to_string_lossy()
            .into_owned(),
    ]);
    run_python_script(
        "legacy context guard migration",
        python,
        paths.config_overlay_script(),
        args,
        runner,
    )?;
    let backups_changed = backup_paths
        .iter()
        .zip(before_backups)
        .any(|(path, before)| before != fs::read(path).unwrap_or_default());
    Ok(before_config != fs::read(&config_path).unwrap_or_default() || backups_changed)
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
        global_override_conflict: codex_status.global_override_conflict,
        codex_restart_result: None,
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
    let catalog_path = paths.generated_catalog_path();
    switch_mode_with_paths_takeover_as_owner_and_catalog(
        current_app_owner,
        mode,
        force_takeover,
        paths,
        Some(&catalog_path),
        python,
        runner,
    )
}

fn switch_mode_with_paths_takeover_as_owner_and_catalog(
    current_app_owner: crate::app_flavor::RoutingOwner,
    mode: &str,
    force_takeover: bool,
    paths: &ConfigPaths,
    catalog_path: Option<&Path>,
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
    let overlay_kind = crate::routing_owner::MutationKind::CodexOverlay {
        mode: if mode == "custom" {
            crate::routing_owner::OverlayMode::Hub
        } else {
            crate::routing_owner::OverlayMode::Official
        },
    };
    crate::routing_owner::permit(
        current_app_owner,
        target_owner,
        overlay_kind,
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
            "--context-guard-state".to_string(),
            paths
                .context_guard_state_path()
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
            "--context-guard-state".to_string(),
            paths
                .context_guard_state_path()
                .to_string_lossy()
                .into_owned(),
        ];
        if let Some(catalog_path) = catalog_path {
            args.extend([
                "--catalog".to_string(),
                catalog_path.to_string_lossy().into_owned(),
            ]);
        }
        args.extend([
            "--base-url".to_string(),
            format!("http://127.0.0.1:{}", settings.proxy_port),
            "--gateway-key".to_string(),
            settings.gateway_client_key.clone(),
            "--owner".to_string(),
            match current_app_owner {
                crate::app_flavor::RoutingOwner::Beta => "beta".to_string(),
                _ => "release".to_string(),
            },
        ]);
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
    let overlay_result = overlay_result?;
    let cleanup_warning = overlay_result.committed_cleanup_warning();

    Ok(AppStatus {
        mode: mode.to_string(),
        proxy_running: false,
        proxy_port: settings.proxy_port,
        proxy_build: None,
        message: match cleanup_warning {
            Some(warning) => format!(
                "Switched to {mode} mode; Gateway lifecycle is handled separately; {warning}"
            ),
            None => format!("Switched to {mode} mode; Gateway lifecycle is handled separately"),
        },
        gateway_lifecycle: crate::gateway_transaction::GatewayLifecyclePhase::Unavailable,
        history_sync_status: None,
        history_sync_message: None,
        codex_restart_result: None,
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
        if let Some(warning) = outcome.committed_cleanup_warning() {
            log::warn!("{label} completed with deferred cleanup: {warning}");
        }
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

pub(crate) fn find_python() -> Result<PathBuf, String> {
    let resource_root = runtime_paths::resource_root().ok();
    runtime_paths::find_python(resource_root.as_deref())
}

/// Populate the isolated `repo` directory of `paths` with the production
/// Codex overlay resources so `apply_codex_config_isolated` can invoke the
/// real `config_overlay.py` (and its `src-python` siblings) without host
/// discovery. Copies `src-python/*.py`, the `tool_compatibility` and `gateway_compat` packages, and
/// the bundled `config/providers.toml` referenced by
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
        for entry in fs::read_dir(&src_python_source)
            .map_err(|error| format!("failed to read production src-python: {error}"))?
        {
            let entry =
                entry.map_err(|error| format!("failed to read src-python entry: {error}"))?;
            let path = entry.path();
            if path.extension().and_then(|value| value.to_str()) == Some("py") {
                let name = path
                    .file_name()
                    .ok_or_else(|| "src-python entry has no file name".to_string())?;
                fs::copy(&path, src_python_target.join(name)).map_err(|error| {
                    format!(
                        "failed to copy production src-python module {}: {error}",
                        path.display()
                    )
                })?;
            }
        }
        for package_name in ["tool_compatibility", "gateway_compat"] {
            let package_source = src_python_source.join(package_name);
            if package_source.is_dir() {
                let package_target = src_python_target.join(package_name);
                fs::create_dir_all(&package_target).map_err(|error| {
                    format!("failed to create isolated {package_name} package: {error}")
                })?;
                for entry in fs::read_dir(&package_source).map_err(|error| {
                    format!("failed to read production {package_name} package: {error}")
                })? {
                    let entry = entry
                        .map_err(|error| format!("failed to read {package_name} entry: {error}"))?;
                    let path = entry.path();
                    if path.extension().and_then(|value| value.to_str()) == Some("py") {
                        let name = path
                            .file_name()
                            .ok_or_else(|| format!("{package_name} entry has no file name"))?;
                        fs::copy(&path, package_target.join(name)).map_err(|error| {
                            format!(
                                "failed to copy production {package_name} module {}: {error}",
                                path.display()
                            )
                        })?;
                    }
                }
            }
        }
    }
    let config_target = isolated_repo.join("config");
    fs::create_dir_all(&config_target)
        .map_err(|error| format!("failed to create isolated config dir: {error}"))?;
    let bundled_providers_source = production_root.join("config").join("providers.toml");
    if bundled_providers_source.is_file() {
        fs::copy(&bundled_providers_source, paths.bundled_providers_path())
            .map_err(|error| format!("failed to copy production providers.toml: {error}"))?;
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
    catalog_path: Option<&Path>,
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
    let overlay_args_relative = build_codex_overlay_args_relative(paths, mode, model, catalog_path);
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
    catalog_path: Option<&Path>,
    python: &Path,
    runner: &dyn CommandRunner,
) -> Result<crate::AppStatus, String> {
    let _ = model; // The overlay derives the selected model from config.toml.
    let current_app_owner = crate::app_flavor::current().routing_owner();
    switch_mode_with_paths_takeover_as_owner_and_catalog(
        current_app_owner,
        mode,
        force_takeover,
        paths,
        catalog_path,
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
            provider, CODEX_OVERLAY_PROVIDER_ID
        ));
    }
    let wire_api = section_toml_value(
        &text,
        &format!("model_providers.{CODEX_OVERLAY_PROVIDER_ID}"),
        "wire_api",
    );
    if wire_api.as_deref() != Some(CODEX_OVERLAY_ROUTE_PROTOCOL) {
        return Err(format!(
            "readback failed: Codex config custom wire_api is {:?}; expected {:?}",
            wire_api, CODEX_OVERLAY_ROUTE_PROTOCOL
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

fn build_codex_overlay_args_relative(
    paths: &ConfigPaths,
    mode: &str,
    _model: &str,
    catalog_path: Option<&Path>,
) -> Vec<String> {
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
    let command = if mode == "official" {
        "restore"
    } else {
        "apply"
    };
    let mut args = vec![
        command.to_string(),
        "--config".to_string(),
        config_name,
        "--backup".to_string(),
        backup_name,
    ];
    if mode == "custom" && catalog_path.is_some() {
        let catalog_name = paths
            .generated_catalog_path()
            .file_name()
            .and_then(|name| name.to_str())
            .unwrap_or("codexhub-model-catalog.json")
            .to_string();
        args.extend(["--catalog".to_string(), catalog_name]);
    }
    args
}

#[cfg(test)]
mod tests;
