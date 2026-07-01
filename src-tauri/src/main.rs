mod autostart;
mod catalog;
mod cli;
mod config;
mod gateway;
mod history;
mod models;
mod proxy;
mod web_bridge;

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Model {
    pub id: String,
    pub display_name: Option<String>,
    pub upstream_model: Option<String>,
    pub source_kind: Option<String>,
    #[serde(default)]
    pub locked: bool,
    #[serde(default)]
    pub hidden: bool,
    #[serde(default = "default_enabled")]
    pub codex_enabled: bool,
    #[serde(default = "default_enabled")]
    pub gateway_exported: bool,
    pub context_window: Option<u32>,
    pub max_output_tokens: Option<u32>,
    pub input_modalities: Option<Vec<String>>,
    pub supported_reasoning_levels: Option<Vec<String>>,
    pub default_reasoning_level: Option<String>,
    pub pricing: Option<ModelPricing>,
    pub metadata_provenance: Option<MetadataProvenance>,
    pub sort_order: Option<i32>,
    #[serde(default = "default_enabled")]
    pub enabled: bool,
}

impl Default for Model {
    fn default() -> Self {
        Self {
            id: String::new(),
            display_name: None,
            upstream_model: None,
            source_kind: None,
            locked: false,
            hidden: false,
            codex_enabled: true,
            gateway_exported: true,
            context_window: None,
            max_output_tokens: None,
            input_modalities: None,
            supported_reasoning_levels: None,
            default_reasoning_level: None,
            pricing: None,
            metadata_provenance: None,
            sort_order: None,
            enabled: true,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct ModelPricing {
    pub input_per_million: Option<f64>,
    pub cached_input_per_million: Option<f64>,
    pub output_per_million: Option<f64>,
    pub currency: String,
    pub source: String,
    pub estimate: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
pub struct MetadataProvenance {
    pub source: String,
    pub source_url: Option<String>,
    pub fetched_at: Option<String>,
    pub confidence: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Provider {
    pub id: String,
    pub name: String,
    pub base_url: String,
    pub api_key: Option<String>,
    pub upstream_format: Option<UpstreamFormat>,
    pub display_prefix: Option<String>,
    pub sort_order: Option<i32>,
    #[serde(default = "default_enabled")]
    pub enabled: bool,
    #[serde(default)]
    pub hidden: bool,
    #[serde(default)]
    pub locked: bool,
    #[serde(default)]
    pub models: Vec<Model>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum UpstreamFormat {
    Auto,
    Responses,
    ChatCompletions,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AppStatus {
    pub mode: String,
    pub proxy_running: bool,
    pub proxy_port: u16,
    pub proxy_build: Option<String>,
    pub message: String,
}

impl AppStatus {
    pub fn scaffold(message: impl Into<String>) -> Self {
        Self {
            mode: "unknown".to_string(),
            proxy_running: false,
            proxy_port: 9099,
            proxy_build: None,
            message: message.into(),
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Settings {
    pub auto_sync_history: bool,
    pub auto_start_proxy: bool,
    pub include_official_models: bool,
    pub auto_sync_catalog: bool,
    pub default_codex_route: String,
    pub gateway_bind_address: String,
    pub gateway_client_key: String,
    pub gateway_enable_models: bool,
    pub gateway_enable_responses: bool,
    pub gateway_enable_chat_completions: bool,
    pub official_model_sort_order: Vec<String>,
    pub official_provider_sort_order: i32,
    pub proxy_port: u16,
}

impl Default for Settings {
    fn default() -> Self {
        Self {
            auto_sync_history: true,
            auto_start_proxy: true,
            include_official_models: true,
            auto_sync_catalog: true,
            default_codex_route: "hub".to_string(),
            gateway_bind_address: "127.0.0.1".to_string(),
            gateway_client_key: "codexhub-proxy".to_string(),
            gateway_enable_models: true,
            gateway_enable_responses: true,
            gateway_enable_chat_completions: true,
            official_model_sort_order: Vec::new(),
            official_provider_sort_order: 0,
            proxy_port: 9099,
        }
    }
}

fn default_enabled() -> bool {
    true
}

#[tauri::command]
fn get_status() -> Result<AppStatus, String> {
    proxy::status()
}

#[tauri::command]
fn switch_mode(mode: String, auto_sync: bool) -> Result<AppStatus, String> {
    config::switch_mode(&mode, auto_sync)
}

#[tauri::command]
fn start_proxy() -> Result<AppStatus, String> {
    proxy::start()
}

#[tauri::command]
fn stop_proxy() -> Result<AppStatus, String> {
    proxy::stop()
}

#[tauri::command]
fn restart_proxy() -> Result<AppStatus, String> {
    proxy::restart()
}

#[tauri::command]
fn get_providers() -> Result<Vec<Provider>, String> {
    config::get_providers()
}

#[tauri::command]
fn save_providers(providers: Vec<Provider>) -> Result<Vec<Provider>, String> {
    config::save_providers(providers)
}

#[tauri::command]
fn get_settings() -> Result<Settings, String> {
    config::get_settings()
}

#[tauri::command]
fn save_settings(settings: Settings) -> Result<Settings, String> {
    config::save_settings(settings)
}

#[tauri::command]
fn refresh_official_models() -> Result<Vec<Model>, String> {
    models::refresh_official_models()
}

#[tauri::command]
fn discover_provider_models(base_url: String, api_key: String) -> Result<Vec<Model>, String> {
    models::discover_provider_models(&base_url, &api_key)
}

#[tauri::command]
fn probe_upstream_format(
    base_url: String,
    api_key: String,
    model: Option<String>,
) -> Result<serde_json::Value, String> {
    models::probe_upstream_format(&base_url, &api_key, model.as_deref())
}

#[tauri::command]
fn provider_probe_upstream_format(
    provider_id: String,
    model: Option<String>,
) -> Result<serde_json::Value, String> {
    gateway::provider_probe_upstream_format(provider_id, model)
}

#[tauri::command]
fn gateway_status() -> Result<gateway::GatewayStatus, String> {
    gateway::gateway_status()
}

#[tauri::command]
fn gateway_test_request(
    kind: gateway::GatewayTestKind,
    model: Option<String>,
) -> Result<gateway::GatewayTestResult, String> {
    gateway::gateway_test_request(kind, model)
}

#[tauri::command]
fn gateway_recent_events(limit: Option<usize>) -> Result<Vec<gateway::GatewayEvent>, String> {
    gateway::gateway_recent_events(limit)
}

#[tauri::command]
fn gateway_usage_summary() -> Result<gateway::GatewayUsageSummary, String> {
    gateway::gateway_usage_summary()
}

#[tauri::command]
fn gateway_usage_events(limit: Option<usize>) -> Result<Vec<gateway::GatewayUsageEvent>, String> {
    gateway::gateway_usage_events(limit)
}

#[tauri::command]
fn gateway_copy_client_config(
    client_kind: Option<String>,
    model: Option<String>,
) -> Result<gateway::GatewayClientConfig, String> {
    gateway::gateway_copy_client_config(client_kind, model)
}

#[tauri::command]
fn list_gateway_clients() -> Result<Vec<gateway::GatewayClientInfo>, String> {
    gateway::list_gateway_clients()
}

#[tauri::command]
fn preview_gateway_client_config(
    client_id: String,
    model: Option<String>,
) -> Result<gateway::GatewayClientConfigPreview, String> {
    gateway::preview_gateway_client_config(client_id, model)
}

#[tauri::command]
fn apply_gateway_client_config(
    client_id: String,
    model: Option<String>,
) -> Result<gateway::GatewayClientApplyResult, String> {
    gateway::apply_gateway_client_config(client_id, model)
}

#[tauri::command]
fn restore_gateway_client_config(
    client_id: String,
) -> Result<gateway::GatewayClientApplyResult, String> {
    gateway::restore_gateway_client_config(client_id)
}

#[tauri::command]
fn switch_gateway_client_route(
    client_id: String,
    mode: String,
    model: Option<String>,
) -> Result<gateway::GatewayClientApplyResult, String> {
    gateway::switch_gateway_client_route(client_id, mode, model)
}

#[tauri::command]
fn subagent_matrix_status() -> Result<gateway::SubagentMatrixStatus, String> {
    gateway::subagent_matrix_status()
}

#[tauri::command]
fn generate_catalog() -> Result<Vec<Model>, String> {
    catalog::generate_catalog()
}

#[tauri::command]
fn list_models() -> Result<Vec<Model>, String> {
    models::list_models()
}

#[tauri::command]
fn refresh_model_metadata() -> Result<Vec<Model>, String> {
    models::refresh_model_metadata()
}

#[tauri::command]
fn list_model_metadata() -> Result<Vec<Model>, String> {
    models::list_model_metadata()
}

#[tauri::command]
fn save_model_metadata_override(model: Model) -> Result<Model, String> {
    models::save_model_metadata_override(model)
}

#[tauri::command]
fn sync_history(target_provider: Option<String>) -> Result<String, String> {
    history::sync_history(target_provider.as_deref())
}

#[tauri::command]
fn sync_catalog() -> Result<String, String> {
    catalog::sync_catalog()
}

#[tauri::command]
fn set_autostart(enabled: bool) -> Result<String, String> {
    autostart::set_autostart(enabled)
}

#[tauri::command]
fn remove_autostart() -> Result<String, String> {
    autostart::remove_autostart()
}

fn run_gui() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .invoke_handler(tauri::generate_handler![
            get_status,
            switch_mode,
            start_proxy,
            stop_proxy,
            restart_proxy,
            get_providers,
            save_providers,
            get_settings,
            save_settings,
            refresh_official_models,
            discover_provider_models,
            probe_upstream_format,
            provider_probe_upstream_format,
            gateway_status,
            gateway_test_request,
            gateway_recent_events,
            gateway_usage_summary,
            gateway_usage_events,
            gateway_copy_client_config,
            list_gateway_clients,
            preview_gateway_client_config,
            apply_gateway_client_config,
            restore_gateway_client_config,
            switch_gateway_client_route,
            subagent_matrix_status,
            generate_catalog,
            list_models,
            refresh_model_metadata,
            list_model_metadata,
            save_model_metadata_override,
            sync_history,
            sync_catalog,
            set_autostart,
            remove_autostart
        ])
        .run(tauri::generate_context!())
        .expect("error while running CodexHub Tauri application");
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();

    if let Some(first_arg) = args.first() {
        if first_arg == "web-bridge" {
            std::process::exit(web_bridge::run(&args[1..]));
        }
        if first_arg != "app" {
            std::process::exit(cli::run(&args));
        }
    }

    run_gui();
}
