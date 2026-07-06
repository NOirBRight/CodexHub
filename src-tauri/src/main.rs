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
use std::process::Command;
use tauri::{AppHandle, Manager, Window, WindowEvent};

#[cfg(desktop)]
use tauri::{
    menu::MenuBuilder,
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
};

const TRAY_SHOW: &str = "show";
const TRAY_CONNECT_OFFICIAL: &str = "connect_official";
const TRAY_CONNECT_HUB: &str = "connect_hub";
const TRAY_START_GATEWAY: &str = "start_gateway";
const TRAY_STOP_GATEWAY: &str = "stop_gateway";
const TRAY_RESTART_GATEWAY: &str = "restart_gateway";
const TRAY_RESTART_CODEX_APP: &str = "restart_codex_app";
const TRAY_EXIT: &str = "exit";

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Model {
    pub id: String,
    pub display_name: Option<String>,
    pub upstream_model: Option<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub aliases: Vec<String>,
    pub source_kind: Option<String>,
    #[serde(default)]
    pub locked: bool,
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
            aliases: Vec::new(),
            source_kind: None,
            locked: false,
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
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub available_upstream_formats: Option<Vec<UpstreamFormat>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub reports_cached_input_tokens: Option<bool>,
    pub display_prefix: Option<String>,
    pub sort_order: Option<i32>,
    #[serde(default = "default_enabled")]
    pub enabled: bool,
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
    AnthropicMessages,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AppStatus {
    pub mode: String,
    pub proxy_running: bool,
    pub proxy_port: u16,
    pub proxy_build: Option<String>,
    pub message: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub history_sync_status: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub history_sync_message: Option<String>,
}

impl AppStatus {
    pub fn scaffold(message: impl Into<String>) -> Self {
        Self {
            mode: "unknown".to_string(),
            proxy_running: false,
            proxy_port: 9099,
            proxy_build: None,
            message: message.into(),
            history_sync_status: None,
            history_sync_message: None,
        }
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Settings {
    #[serde(default)]
    pub locale: String,
    pub auto_sync_history: bool,
    #[serde(default = "default_enabled")]
    pub unified_codex_history: bool,
    pub auto_start_proxy: bool,
    pub include_official_models: bool,
    pub auto_sync_catalog: bool,
    #[serde(default = "default_enabled")]
    pub auto_sync_clients: bool,
    pub default_codex_route: String,
    pub gateway_bind_address: String,
    pub gateway_client_key: String,
    pub gateway_enable_models: bool,
    pub gateway_enable_responses: bool,
    pub gateway_enable_chat_completions: bool,
    pub gateway_request_timeout_seconds: u32,
    #[serde(default = "default_enabled")]
    pub gateway_auto_retry_enabled: bool,
    #[serde(default = "default_gateway_auto_retry_max_attempts")]
    pub gateway_auto_retry_max_attempts: u8,
    #[serde(default)]
    pub gateway_image_proxy_enabled: bool,
    #[serde(default)]
    pub gateway_image_proxy_model: String,
    #[serde(default = "default_fast_model_variants")]
    pub gateway_fast_model_variants: Vec<String>,
    #[serde(default)]
    pub official_disabled_models: Vec<String>,
    pub official_model_sort_order: Vec<String>,
    pub official_provider_sort_order: i32,
    pub proxy_port: u16,
}

fn default_fast_model_variants() -> Vec<String> {
    vec!["openai/gpt-5.5".to_string(), "openai/gpt-5.4".to_string()]
}

impl Default for Settings {
    fn default() -> Self {
        Self {
            locale: String::new(),
            auto_sync_history: false,
            unified_codex_history: true,
            auto_start_proxy: true,
            include_official_models: true,
            auto_sync_catalog: true,
            auto_sync_clients: true,
            default_codex_route: "hub".to_string(),
            gateway_bind_address: "127.0.0.1".to_string(),
            gateway_client_key: "codexhub-proxy".to_string(),
            gateway_enable_models: true,
            gateway_enable_responses: true,
            gateway_enable_chat_completions: true,
            gateway_request_timeout_seconds: 300,
            gateway_auto_retry_enabled: true,
            gateway_auto_retry_max_attempts: default_gateway_auto_retry_max_attempts(),
            gateway_image_proxy_enabled: false,
            gateway_image_proxy_model: String::new(),
            gateway_fast_model_variants: default_fast_model_variants(),
            official_disabled_models: Vec::new(),
            official_model_sort_order: Vec::new(),
            official_provider_sort_order: 0,
            proxy_port: 9099,
        }
    }
}

fn default_enabled() -> bool {
    true
}

fn default_gateway_auto_retry_max_attempts() -> u8 {
    30
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
fn test_model_endpoint(
    base_url: String,
    api_key: String,
    model: String,
    upstream_format: UpstreamFormat,
) -> Result<serde_json::Value, String> {
    models::test_model_endpoint(&base_url, &api_key, &model, &upstream_format)
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
fn gateway_usage_summary(
    start_ts: Option<String>,
    end_ts: Option<String>,
) -> Result<gateway::GatewayUsageSummary, String> {
    gateway::gateway_usage_summary(start_ts, end_ts)
}

#[tauri::command]
fn gateway_usage_snapshot(
    limit: Option<usize>,
    start_ts: Option<String>,
    end_ts: Option<String>,
) -> Result<gateway::GatewayUsageSnapshot, String> {
    gateway::gateway_usage_snapshot(limit, start_ts, end_ts)
}

#[tauri::command]
fn gateway_usage_events(
    limit: Option<usize>,
    start_ts: Option<String>,
    end_ts: Option<String>,
) -> Result<Vec<gateway::GatewayUsageEvent>, String> {
    gateway::gateway_usage_events(limit, start_ts, end_ts)
}

#[tauri::command]
fn gateway_copy_client_config(
    client_kind: Option<String>,
    model: Option<String>,
) -> Result<gateway::GatewayClientConfig, String> {
    gateway::gateway_copy_client_config(client_kind, model)
}

#[tauri::command]
fn list_gateway_clients(
    include_versions: Option<bool>,
) -> Result<Vec<gateway::GatewayClientInfo>, String> {
    gateway::list_gateway_clients(include_versions.unwrap_or(false))
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
fn sync_gateway_clients(
    model: Option<String>,
) -> Result<gateway::GatewayClientSyncSummary, String> {
    gateway::sync_gateway_clients(model)
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
fn migrate_official_history_to_unified() -> Result<String, String> {
    history::migrate_official_history_to_unified()
}

#[tauri::command]
fn restore_official_history_from_unified() -> Result<String, String> {
    history::restore_official_history_from_unified()
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

#[tauri::command]
fn window_minimize(window: Window) -> Result<(), String> {
    window
        .minimize()
        .map_err(|error| format!("failed to minimize window: {error}"))
}

#[tauri::command]
fn window_toggle_maximize(window: Window) -> Result<(), String> {
    let maximized = window
        .is_maximized()
        .map_err(|error| format!("failed to read window state: {error}"))?;
    if maximized {
        window
            .unmaximize()
            .map_err(|error| format!("failed to restore window: {error}"))
    } else {
        window
            .maximize()
            .map_err(|error| format!("failed to maximize window: {error}"))
    }
}

#[tauri::command]
fn window_close_to_tray(window: Window) -> Result<(), String> {
    window
        .hide()
        .map_err(|error| format!("failed to hide window to tray: {error}"))
}

fn show_main_window(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
    }
}

fn run_tray_action(app: &AppHandle, id: &str) {
    match id {
        TRAY_SHOW => show_main_window(app),
        TRAY_CONNECT_OFFICIAL => {
            let _ = config::switch_mode("official", false);
        }
        TRAY_CONNECT_HUB => {
            let _ = config::switch_mode("custom", false);
        }
        TRAY_START_GATEWAY => {
            let _ = proxy::start();
        }
        TRAY_STOP_GATEWAY => {
            let _ = proxy::stop();
        }
        TRAY_RESTART_GATEWAY => {
            let _ = proxy::restart();
        }
        TRAY_RESTART_CODEX_APP => {
            let _ = restart_codex_app();
        }
        TRAY_EXIT => app.exit(0),
        _ => {}
    }
}

#[cfg(target_os = "windows")]
fn restart_codex_app() -> Result<String, String> {
    let script = r#"
$ErrorActionPreference = 'Stop'
$app = Get-StartApps |
  Where-Object { $_.AppID -like 'OpenAI.Codex_*' -or $_.Name -eq 'Codex' } |
  Select-Object -First 1
if (-not $app) {
  throw 'Codex App is not installed or does not expose a Start menu AppID.'
}
Get-Process -Name Codex -ErrorAction SilentlyContinue | Stop-Process -Force
Start-Sleep -Milliseconds 800
Start-Process ('shell:AppsFolder\' + $app.AppID)
Write-Output ('Restarted Codex App via ' + $app.AppID)
"#;

    let mut command = Command::new("powershell");
    command.args([
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        script,
    ]);
    #[cfg(target_os = "windows")]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x08000000;
        command.creation_flags(CREATE_NO_WINDOW);
    }

    let output = command
        .output()
        .map_err(|error| format!("failed to run Codex App restart command: {error}"))?;
    if output.status.success() {
        let stdout = String::from_utf8_lossy(&output.stdout).trim().to_string();
        Ok(if stdout.is_empty() {
            "Restarted Codex App".to_string()
        } else {
            stdout
        })
    } else {
        let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
        Err(if stderr.is_empty() {
            format!(
                "Codex App restart failed with exit code {}",
                output.status.code().unwrap_or(-1)
            )
        } else {
            stderr
        })
    }
}

#[cfg(not(target_os = "windows"))]
fn restart_codex_app() -> Result<String, String> {
    Err("Restart Codex App is currently implemented on Windows only.".to_string())
}

#[cfg(desktop)]
fn setup_tray(app: &tauri::App) -> Result<(), Box<dyn std::error::Error>> {
    let menu = MenuBuilder::new(app)
        .text(TRAY_SHOW, "Show CodexHub")
        .separator()
        .text(TRAY_CONNECT_OFFICIAL, "Connect Codex to Official")
        .text(TRAY_CONNECT_HUB, "Connect Codex to CodexHub")
        .separator()
        .text(TRAY_START_GATEWAY, "Start Gateway")
        .text(TRAY_STOP_GATEWAY, "Stop Gateway")
        .text(TRAY_RESTART_GATEWAY, "Restart Gateway")
        .separator()
        .text(TRAY_RESTART_CODEX_APP, "Restart Codex App")
        .text(TRAY_EXIT, "Exit")
        .build()?;

    let mut tray = TrayIconBuilder::with_id("codexhub")
        .tooltip("CodexHub")
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(|app, event| run_tray_action(app, event.id().as_ref()))
        .on_tray_icon_event(|tray, event| match event {
            TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            }
            | TrayIconEvent::DoubleClick {
                button: MouseButton::Left,
                ..
            } => show_main_window(tray.app_handle()),
            _ => {}
        });

    if let Some(icon) = app.default_window_icon() {
        tray = tray.icon(icon.clone());
    }

    tray.build(app)?;
    Ok(())
}

fn run_gui() {
    tauri::Builder::default()
        .plugin(tauri_plugin_shell::init())
        .setup(|app| {
            #[cfg(desktop)]
            setup_tray(app)?;
            gateway::start_telemetry_ingester();
            Ok(())
        })
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
            }
        })
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
            test_model_endpoint,
            gateway_status,
            gateway_test_request,
            gateway_recent_events,
            gateway_usage_summary,
            gateway_usage_snapshot,
            gateway_usage_events,
            gateway_copy_client_config,
            list_gateway_clients,
            preview_gateway_client_config,
            apply_gateway_client_config,
            restore_gateway_client_config,
            switch_gateway_client_route,
            sync_gateway_clients,
            subagent_matrix_status,
            generate_catalog,
            list_models,
            refresh_model_metadata,
            list_model_metadata,
            save_model_metadata_override,
            sync_history,
            migrate_official_history_to_unified,
            restore_official_history_from_unified,
            sync_catalog,
            set_autostart,
            remove_autostart,
            window_minimize,
            window_toggle_maximize,
            window_close_to_tray
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
