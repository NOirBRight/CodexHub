#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod app_flavor;
mod app_updates;
mod autostart;
mod build_info;
mod catalog;
mod cli;
mod config;
mod diagnostics;
mod gateway;
mod gateway_lifecycle;
mod gateway_transaction;
mod history;
#[cfg(test)]
mod lock_test_fixtures;
mod models;
mod openai_usage;
mod official_refresh;
mod proxy;
mod runtime_paths;
mod safe_file;
mod web_bridge;

use serde::{Deserialize, Serialize};
use std::process::Command;
use std::sync::atomic::{AtomicU64, Ordering};
use tauri::{AppHandle, Emitter, Manager, RunEvent, Window, WindowEvent};

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
const TRAY_EXIT: &str = "exit";
const TRAY_TOAST_EVENT: &str = "codexhub:toast";

#[derive(Debug, Clone, Serialize)]
struct TrayToast {
    id: String,
    text: String,
    tone: String,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Model {
    pub id: String,
    pub display_name: Option<String>,
    pub upstream_model: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tool_surface_strategy: Option<ToolSurfaceStrategy>,
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
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub max_context_window: Option<u32>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub effective_source: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub max_source: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub confidence: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub verified_at: Option<String>,
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
            tool_surface_strategy: None,
            aliases: Vec::new(),
            source_kind: None,
            locked: false,
            codex_enabled: true,
            gateway_exported: true,
            context_window: None,
            max_context_window: None,
            effective_source: None,
            max_source: None,
            confidence: None,
            verified_at: None,
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
    pub tool_protocol: Option<ToolProtocol>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tool_surface_strategy: Option<ToolSurfaceStrategy>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub reports_cached_input_tokens: Option<bool>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub supports_developer_role: Option<bool>,
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

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "snake_case")]
pub enum ToolProtocol {
    Auto,
    ResponsesStructured,
    ChatTools,
    TextCompat,
    None,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum ToolSurfaceStrategy {
    Eager,
    DeferredCore,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct AppStatus {
    pub mode: String,
    pub proxy_running: bool,
    pub proxy_port: u16,
    pub proxy_build: Option<String>,
    pub message: String,
    #[serde(default)]
    pub gateway_lifecycle: gateway_transaction::GatewayLifecyclePhase,
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
            proxy_port: app_flavor::default_gateway_port(),
            proxy_build: None,
            message: message.into(),
            gateway_lifecycle: crate::gateway_transaction::GatewayLifecyclePhase::Unavailable,
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
    pub auto_start_software: bool,
    #[serde(default = "default_enabled")]
    pub auto_start_gateway: bool,
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
    #[serde(default)]
    pub openai_context_guard_enabled: bool,
    #[serde(default = "default_fast_model_variants")]
    pub gateway_fast_model_variants: Vec<String>,
    #[serde(default)]
    pub official_disabled_models: Vec<String>,
    pub official_model_sort_order: Vec<String>,
    pub official_provider_sort_order: i32,
    pub proxy_port: u16,
}

fn default_fast_model_variants() -> Vec<String> {
    vec!["gpt-5.5".to_string(), "gpt-5.4".to_string()]
}

impl Default for Settings {
    fn default() -> Self {
        Self {
            locale: String::new(),
            auto_sync_history: false,
            unified_codex_history: true,
            auto_start_software: true,
            auto_start_gateway: true,
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
            openai_context_guard_enabled: false,
            gateway_fast_model_variants: default_fast_model_variants(),
            official_disabled_models: Vec::new(),
            official_model_sort_order: Vec::new(),
            official_provider_sort_order: 0,
            proxy_port: app_flavor::default_gateway_port(),
        }
    }
}

fn default_enabled() -> bool {
    true
}

fn default_gateway_auto_retry_max_attempts() -> u8 {
    30
}

async fn run_blocking<T, F>(name: &'static str, task: F) -> Result<T, String>
where
    T: Send + 'static,
    F: FnOnce() -> Result<T, String> + Send + 'static,
{
    tauri::async_runtime::spawn_blocking(task)
        .await
        .map_err(|error| format!("{name} task failed: {error}"))?
}

#[tauri::command]
async fn get_status() -> Result<AppStatus, String> {
    run_blocking("get_status", proxy::status).await
}

#[tauri::command]
fn switch_mode(mode: String, auto_sync: bool, force_takeover: Option<bool>) -> Result<AppStatus, String> {
    if mode == "custom" {
        official_refresh::refresh_before_official_activation()?;
    }
    config::switch_mode_with_takeover(&mode, auto_sync, force_takeover.unwrap_or(false))
}

#[tauri::command]
fn start_proxy() -> Result<AppStatus, String> {
    proxy::start_after(official_refresh::refresh_before_official_activation)
}

#[tauri::command]
fn stop_proxy() -> Result<AppStatus, String> {
    proxy::stop()
}

#[tauri::command]
fn restart_proxy() -> Result<AppStatus, String> {
    proxy::restart_after(official_refresh::refresh_before_official_activation)
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
fn get_app_flavor() -> app_flavor::AppFlavorInfo {
    app_flavor::current_info()
}

#[tauri::command]
fn save_settings(settings: Settings) -> Result<Settings, String> {
    config::save_settings(settings)
}

#[tauri::command]
fn get_codex_context_guard_status() -> Result<config::CodexContextGuardStatus, String> {
    config::get_codex_context_guard_status()
}

#[tauri::command]
fn set_codex_context_guard(enabled: bool) -> Result<config::CodexContextGuardStatus, String> {
    config::set_codex_context_guard(enabled)
}

#[tauri::command]
async fn refresh_official_models() -> Result<official_refresh::OfficialRefreshResult, String> {
    run_blocking("refresh_official_models", official_refresh::refresh_manual).await
}

#[tauri::command]
async fn openai_usage_completions(
    start_time: Option<u64>,
    end_time: Option<u64>,
    force_refresh: Option<bool>,
) -> Result<openai_usage::OpenAiUsageSnapshot, String> {
    run_blocking("openai_usage_completions", move || {
        openai_usage::openai_usage_completions(start_time, end_time, force_refresh)
    })
    .await
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
async fn gateway_status() -> Result<gateway::GatewayStatus, String> {
    run_blocking("gateway_status", gateway::gateway_status).await
}

#[tauri::command]
async fn diagnostics_status() -> Result<diagnostics::DiagnosticsStatus, String> {
    run_blocking("diagnostics_status", diagnostics::status).await
}

#[tauri::command]
async fn diagnostics_manual_mark() -> Result<diagnostics::DiagnosticsActionResult, String> {
    run_blocking("diagnostics_manual_mark", diagnostics::manual_mark).await
}

#[tauri::command]
async fn diagnostics_pause() -> Result<diagnostics::DiagnosticsActionResult, String> {
    run_blocking("diagnostics_pause", diagnostics::pause).await
}

#[tauri::command]
async fn diagnostics_resume() -> Result<diagnostics::DiagnosticsActionResult, String> {
    run_blocking("diagnostics_resume", diagnostics::resume).await
}

#[tauri::command]
async fn diagnostics_delete_incident(
    incident_id: String,
) -> Result<diagnostics::DiagnosticsActionResult, String> {
    run_blocking("diagnostics_delete_incident", move || {
        diagnostics::delete_incident(incident_id)
    })
    .await
}

#[tauri::command]
fn gateway_test_request(
    kind: gateway::GatewayTestKind,
    model: Option<String>,
) -> Result<gateway::GatewayTestResult, String> {
    gateway::gateway_test_request(kind, model)
}

#[tauri::command]
async fn gateway_recent_events(
    limit: Option<usize>,
    since_ts: Option<String>,
) -> Result<Vec<gateway::GatewayEvent>, String> {
    run_blocking("gateway_recent_events", move || {
        gateway::gateway_recent_events(limit, since_ts)
    })
    .await
}

#[tauri::command]
async fn gateway_usage_summary(
    start_ts: Option<String>,
    end_ts: Option<String>,
) -> Result<gateway::GatewayUsageSummary, String> {
    run_blocking("gateway_usage_summary", move || {
        gateway::gateway_usage_summary(start_ts, end_ts)
    })
    .await
}

#[tauri::command]
async fn gateway_usage_snapshot(
    limit: Option<usize>,
    start_ts: Option<String>,
    end_ts: Option<String>,
) -> Result<gateway::GatewayUsageSnapshot, String> {
    run_blocking("gateway_usage_snapshot", move || {
        gateway::gateway_usage_snapshot(limit, start_ts, end_ts)
    })
    .await
}

#[tauri::command]
async fn gateway_usage_events(
    limit: Option<usize>,
    start_ts: Option<String>,
    end_ts: Option<String>,
) -> Result<Vec<gateway::GatewayUsageEvent>, String> {
    run_blocking("gateway_usage_events", move || {
        gateway::gateway_usage_events(limit, start_ts, end_ts)
    })
    .await
}

#[tauri::command]
fn gateway_copy_client_config(
    client_kind: Option<String>,
    model: Option<String>,
) -> Result<gateway::GatewayClientConfig, String> {
    gateway::gateway_copy_client_config(client_kind, model)
}

#[tauri::command]
async fn list_gateway_clients(
    include_versions: Option<bool>,
) -> Result<Vec<gateway::GatewayClientInfo>, String> {
    run_blocking("list_gateway_clients", move || {
        gateway::list_gateway_clients(include_versions.unwrap_or(false))
    })
    .await
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
    force_takeover: Option<bool>,
) -> Result<gateway::GatewayClientApplyResult, String> {
    gateway::switch_gateway_client_route(client_id, mode, model, force_takeover)
}

#[tauri::command]
async fn sync_gateway_clients(
    model: Option<String>,
) -> Result<gateway::GatewayClientSyncSummary, String> {
    run_blocking("sync_gateway_clients", move || {
        gateway::sync_gateway_clients(model)
    })
    .await
}

#[tauri::command]
fn subagent_matrix_status() -> Result<gateway::SubagentMatrixStatus, String> {
    gateway::subagent_matrix_status()
}

#[tauri::command]
async fn generate_catalog() -> Result<Vec<Model>, String> {
    run_blocking("generate_catalog", catalog::generate_catalog).await
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
async fn sync_history(target_provider: Option<String>) -> Result<String, String> {
    run_blocking("sync_history", move || {
        history::sync_history(target_provider.as_deref())
    })
    .await
}

#[tauri::command]
async fn reconcile_after_route_switch(
    target_provider: Option<String>,
) -> Result<history::UnifiedHistoryResult, String> {
    run_blocking("reconcile_after_route_switch", move || {
        history::reconcile_after_route_switch(target_provider.as_deref())
    })
    .await
}

#[tauri::command]
async fn migrate_official_history_to_unified() -> Result<String, String> {
    run_blocking("migrate_official_history_to_unified", || {
        history::migrate_official_history_to_unified()
    })
    .await
}

#[tauri::command]
async fn restore_official_history_from_unified() -> Result<String, String> {
    run_blocking("restore_official_history_from_unified", || {
        history::restore_official_history_from_unified()
    })
    .await
}

#[tauri::command]
async fn preflight_unified_history(
    apply_repairs: bool,
    target_unified: Option<bool>,
) -> Result<history::UnifiedHistoryResult, String> {
    run_blocking("preflight_unified_history", move || {
        history::preflight_unified_history(apply_repairs, target_unified)
    })
    .await
}

#[tauri::command]
async fn get_conversation_sync_status() -> Result<history::UnifiedHistoryResult, String> {
    run_blocking("get_conversation_sync_status", || {
        history::preflight_unified_history(false, None)
    })
    .await
}

#[tauri::command]
async fn sync_conversation_history(
    target_provider: Option<String>,
) -> Result<history::UnifiedHistoryResult, String> {
    let target_unified = target_provider.as_deref().map(|value| value != "openai");
    run_blocking("sync_conversation_history", move || {
        history::preflight_unified_history(true, target_unified)
    })
    .await
}

#[tauri::command]
async fn diagnose_conversation_history(
    full_scan: Option<bool>,
) -> Result<history::UnifiedHistoryResult, String> {
    let _full_scan = full_scan.unwrap_or(true);
    run_blocking("diagnose_conversation_history", || {
        history::preflight_unified_history(false, None)
    })
    .await
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
fn open_codex_app() -> Result<String, String> {
    launch_codex_app()
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
            report_tray_action(
                app,
                "Connect Codex to Official",
                switch_mode("official".to_string(), false, None),
            );
        }
        TRAY_CONNECT_HUB => {
            report_tray_action(
                app,
                "Connect Codex to CodexHub",
                switch_mode("custom".to_string(), false, None),
            );
        }
        TRAY_START_GATEWAY => {
            run_tray_lifecycle_action(app, "Start Gateway", start_proxy, false);
        }
        TRAY_STOP_GATEWAY => {
            run_tray_lifecycle_action(app, "Stop Gateway", stop_proxy, true);
        }
        TRAY_RESTART_GATEWAY => {
            run_tray_lifecycle_action(app, "Restart Gateway", restart_proxy, true);
        }
        TRAY_EXIT => app.exit(0),
        _ => {}
    }
}

fn report_tray_action(app: &AppHandle, action: &str, result: Result<AppStatus, String>) {
    let toast = tray_toast_for(next_tray_toast_id(), action, result);
    emit_tray_toast(app, toast);
}

fn run_tray_lifecycle_action(
    app: &AppHandle,
    action: &'static str,
    work: fn() -> Result<AppStatus, String>,
    retires_gateway: bool,
) {
    let toast_id = next_tray_toast_id();
    let loading_toast = if retires_gateway {
        tray_retiring_gateway_loading_toast(toast_id.clone(), action)
    } else {
        tray_loading_toast(toast_id.clone(), action)
    };
    emit_tray_toast(app, loading_toast);
    let app = app.clone();
    std::mem::drop(tauri::async_runtime::spawn_blocking(move || {
        let result = work();
        emit_tray_toast(&app, tray_toast_for(toast_id, action, result));
    }));
}

fn emit_tray_toast(app: &AppHandle, toast: TrayToast) {
    if let Err(error) = app.emit(TRAY_TOAST_EVENT, toast) {
        log::warn!("failed to emit tray action feedback: {error}");
    }
}

fn tray_loading_toast(id: String, action: &str) -> TrayToast {
    TrayToast {
        id,
        text: format!("{action}..."),
        tone: "loading".to_string(),
    }
}

fn tray_retiring_gateway_loading_toast(id: String, action: &str) -> TrayToast {
    let locale = config::get_settings()
        .map(|settings| settings.locale)
        .unwrap_or_default();
    TrayToast {
        id,
        text: format!("{} {action}...", gateway_retirement_warning_for_locale(&locale)),
        tone: "loading".to_string(),
    }
}

fn gateway_retirement_warning_for_locale(locale: &str) -> &'static str {
    if locale == "zh-CN" {
        "活跃的 Codex 任务可能会被中断。"
    } else {
        "Active Codex Tasks may be interrupted."
    }
}

fn tray_toast_for(id: String, action: &str, result: Result<AppStatus, String>) -> TrayToast {
    match result {
        Ok(status) => TrayToast {
            id,
            text: format!("{action}: {}", status.message),
            tone: "success".to_string(),
        },
        Err(error) => TrayToast {
            id,
            text: format!("{action} failed: {error}"),
            tone: "error".to_string(),
        },
    }
}

fn next_tray_toast_id() -> String {
    static NEXT_ID: AtomicU64 = AtomicU64::new(1);
    format!(
        "tray-lifecycle-{}-{}",
        std::process::id(),
        NEXT_ID.fetch_add(1, Ordering::Relaxed)
    )
}

#[cfg(target_os = "windows")]
fn run_codex_app_script(label: &str, script: &str) -> Result<String, String> {
    let mut command = Command::new("powershell");
    command.args([
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        script,
    ]);
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x08000000;
        command.creation_flags(CREATE_NO_WINDOW);
    }

    let output = command
        .output()
        .map_err(|error| format!("failed to run Codex App {label} command: {error}"))?;
    if output.status.success() {
        let stdout = String::from_utf8_lossy(&output.stdout).trim().to_string();
        Ok(if stdout.is_empty() {
            format!("Codex App {label} command completed")
        } else {
            stdout
        })
    } else {
        let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();
        Err(if stderr.is_empty() {
            format!(
                "Codex App {label} failed with exit code {}",
                output.status.code().unwrap_or(-1)
            )
        } else {
            stderr
        })
    }
}

#[cfg(target_os = "windows")]
fn launch_codex_app() -> Result<String, String> {
    let script = r#"
$ErrorActionPreference = 'Stop'
$app = Get-StartApps |
  Where-Object { $_.AppID -like 'OpenAI.Codex_*' -or $_.Name -eq 'Codex' } |
  Select-Object -First 1
if (-not $app) {
  throw 'Codex App is not installed or does not expose a Start menu AppID.'
}
Start-Process ('shell:AppsFolder\' + $app.AppID)
Write-Output ('Opened Codex App via ' + $app.AppID)
"#;

    run_codex_app_script("open", script)
}

#[cfg(not(target_os = "windows"))]
fn launch_codex_app() -> Result<String, String> {
    Err("Open Codex App is currently implemented on Windows only. Run `codex login` from a terminal to sign in.".to_string())
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
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _argv, _cwd| {
            show_main_window(app);
        }))
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(tauri_plugin_shell::init())
        .setup(|app| {
            if let Ok(resource_dir) = app.path().resource_dir() {
                runtime_paths::set_resource_root(resource_dir);
            }
            #[cfg(desktop)]
            setup_tray(app)?;
            gateway::start_telemetry_ingester();
            web_bridge::start_background(app.handle().clone())?;
            start_gateway_on_launch();
            Ok(())
        })
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .invoke_handler(tauri::generate_handler![
            app_updates::get_app_version,
            app_updates::check_app_update,
            app_updates::start_app_update_install,
            app_updates::get_app_update_install_status,
            app_updates::consume_app_update_completion,
            app_updates::install_app_update,
            get_status,
            switch_mode,
            start_proxy,
            stop_proxy,
            restart_proxy,
            get_providers,
            save_providers,
            get_settings,
            get_app_flavor,
            save_settings,
            get_codex_context_guard_status,
            set_codex_context_guard,
            refresh_official_models,
            openai_usage_completions,
            discover_provider_models,
            probe_upstream_format,
            provider_probe_upstream_format,
            test_model_endpoint,
            gateway_status,
            diagnostics_status,
            diagnostics_manual_mark,
            diagnostics_pause,
            diagnostics_resume,
            diagnostics_delete_incident,
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
            reconcile_after_route_switch,
            migrate_official_history_to_unified,
            restore_official_history_from_unified,
            preflight_unified_history,
            get_conversation_sync_status,
            sync_conversation_history,
            diagnose_conversation_history,
            sync_catalog,
            set_autostart,
            remove_autostart,
            open_codex_app,
            window_minimize,
            window_toggle_maximize,
            window_close_to_tray
        ])
        .build(tauri::generate_context!())
        .expect("error while building CodexHub Tauri application");

    app.run(|_app, event| {
            if matches!(event, RunEvent::Resumed) {
                tauri::async_runtime::spawn_blocking(|| {
                    if let Err(error) = official_refresh::refresh_after_resume() {
                        log::warn!("overdue Official model refresh after resume failed: {error}");
                    }
                });
            }
        });
}

fn start_gateway_on_launch() {
    let (ready_tx, ready_rx) = std::sync::mpsc::sync_channel(1);
    std::thread::spawn(move || {
        let mut launch_ready = StartupLaunchReady::new(ready_tx);
        official_refresh::start_scheduled_refresh_loop();
        let Ok(settings) = config::get_settings() else {
            return;
        };
        let start = || {
            proxy::start_after(|| {
                launch_ready.signal();
                if let Err(error) = official_refresh::refresh_at_startup() {
                    log::warn!("startup Official model refresh failed: {error}");
                }
                official_refresh::refresh_before_official_activation()
            })
        };
        if let Err(error) = start_gateway_after_startup(settings.auto_start_gateway, start) {
            eprintln!("failed to start CodexHub gateway on app launch: {error}");
        } else if !settings.auto_start_gateway {
            launch_ready.signal();
            if let Err(error) = official_refresh::refresh_at_startup() {
                log::warn!("startup Official model refresh failed: {error}");
            }
        }
    });
    // Do not expose the initial window until automatic startup either owns the
    // cross-process gate (and publishes Starting) or has already completed.
    let _ = ready_rx.recv();
}

struct StartupLaunchReady(Option<std::sync::mpsc::SyncSender<()>>);

impl StartupLaunchReady {
    fn new(sender: std::sync::mpsc::SyncSender<()>) -> Self {
        Self(Some(sender))
    }

    fn signal(&mut self) {
        if let Some(sender) = self.0.take() {
            let _ = sender.send(());
        }
    }
}

impl Drop for StartupLaunchReady {
    fn drop(&mut self) {
        self.signal();
    }
}

fn start_gateway_after_startup<StartGateway>(
    auto_start_gateway: bool,
    start_gateway: StartGateway,
) -> Result<bool, String>
where
    StartGateway: FnOnce() -> Result<AppStatus, String>,
{
    if !auto_start_gateway {
        return Ok(false);
    }
    start_gateway()?;
    Ok(true)
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

#[cfg(test)]
mod tests {
    use super::{
        gateway_retirement_warning_for_locale, start_gateway_after_startup, tray_loading_toast,
        tray_retiring_gateway_loading_toast, tray_toast_for, AppStatus,
    };
    use std::cell::Cell;

    #[test]
    fn auto_start_propagates_coordinated_precondition_failure_once() {
        let starts = Cell::new(0);
        let error = start_gateway_after_startup(true, || {
            starts.set(starts.get() + 1);
            Err("safe Official snapshot is unavailable".to_string())
        })
        .expect_err("coordinated precondition must block auto-start");

        assert!(error.contains("safe Official snapshot"));
        assert_eq!(starts.get(), 1);
    }

    #[test]
    fn tray_state_actions_always_produce_success_or_error_feedback() {
        let loading = tray_loading_toast("same-toast".to_string(), "Start Gateway");
        assert_eq!(loading.id, "same-toast");
        assert_eq!(loading.tone, "loading");

        let retiring = tray_retiring_gateway_loading_toast("same-toast".to_string(), "Stop Gateway");
        assert_eq!(retiring.id, loading.id);
        assert_eq!(retiring.tone, "loading");
        assert_eq!(
            gateway_retirement_warning_for_locale("zh-CN"),
            "活跃的 Codex 任务可能会被中断。"
        );

        let success = tray_toast_for("same-toast".to_string(), "Start Gateway", Ok(status()));
        assert_eq!(success.id, loading.id);
        assert_eq!(success.tone, "success");
        assert!(success.text.contains("Start Gateway"));

        let failure = tray_toast_for(
            "same-toast".to_string(),
            "Start Gateway",
            Err("safe snapshot unavailable".to_string()),
        );
        assert_eq!(failure.id, loading.id);
        assert_eq!(failure.tone, "error");
        assert!(failure.text.contains("safe snapshot unavailable"));
    }

    fn status() -> AppStatus {
        AppStatus {
            mode: "custom".to_string(),
            proxy_running: false,
            proxy_port: 9099,
            proxy_build: None,
            message: "Gateway state updated".to_string(),
            gateway_lifecycle: crate::gateway_transaction::GatewayLifecyclePhase::Stopped,
            history_sync_status: None,
            history_sync_message: None,
        }
    }
}
