#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod app_flavor;
mod app_server;
mod app_updates;
mod autostart;
mod build_info;
mod catalog;
mod cli;
mod codex_desktop;
mod config;
mod desktop_commands;
mod diagnostics;
mod file_transaction;
mod gateway;
mod gateway_lifecycle;
mod gateway_transaction;
mod history;
mod injection;
#[cfg(target_os = "linux")]
mod linux_window;
#[cfg(test)]
mod lock_test_fixtures;
mod models;
mod official_refresh;
mod openai_usage;
mod proxy;
mod routing_owner;
mod runtime_paths;
mod safe_file;
mod web_bridge;
mod xai_auth;

use desktop_commands::{
    open_codex_app, restart_proxy, set_codex_context_guard, start_proxy, stop_proxy, switch_mode,
};
pub(crate) use desktop_commands::{
    generate_catalog_coordinated, refresh_official_models_coordinated,
    refresh_official_models_published_coordinated,
    save_official_multi_agent_version_coordinated, sync_catalog_coordinated,
};
use serde::{Deserialize, Serialize};
use std::sync::atomic::{AtomicU64, Ordering};
use tauri::{image::Image, AppHandle, Emitter, Manager, RunEvent, WindowEvent};

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
const TRAY_CODEX_SWITCH_REQUEST_EVENT: &str = "codexhub:request-codex-switch";

#[derive(Debug, Clone, Serialize)]
struct TrayToast {
    id: String,
    text: String,
    tone: String,
}

#[derive(Debug, Clone, Copy, Default, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum CatalogVisibility {
    // Provider models and older persisted metadata were user-listable before
    // visibility became explicit; upstream parsers assign Unknown explicitly.
    #[default]
    List,
    Hide,
    Unknown,
}

impl<'de> Deserialize<'de> for CatalogVisibility {
    fn deserialize<D>(deserializer: D) -> Result<Self, D::Error>
    where
        D: serde::Deserializer<'de>,
    {
        let value = Option::<serde_json::Value>::deserialize(deserializer)?;
        Ok(
            match value
                .as_ref()
                .and_then(serde_json::Value::as_str)
                .map(str::trim)
                .map(str::to_ascii_lowercase)
                .as_deref()
            {
                Some("list") => Self::List,
                Some("hide") => Self::Hide,
                _ => Self::Unknown,
            },
        )
    }
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Model {
    pub id: String,
    pub display_name: Option<String>,
    pub upstream_model: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub tool_surface_strategy: Option<ToolSurfaceStrategy>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub multi_agent_version: Option<String>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub aliases: Vec<String>,
    pub source_kind: Option<String>,
    #[serde(default)]
    pub locked: bool,
    #[serde(default = "default_enabled")]
    pub codex_enabled: bool,
    #[serde(default = "default_enabled")]
    pub gateway_exported: bool,
    #[serde(default)]
    pub visibility: CatalogVisibility,
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
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub thinking_mode: Option<String>,
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
            multi_agent_version: None,
            aliases: Vec::new(),
            source_kind: None,
            locked: false,
            codex_enabled: true,
            gateway_exported: true,
            visibility: CatalogVisibility::default(),
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
            thinking_mode: None,
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
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub auth_capabilities: Option<Vec<String>>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub onboarding_hint: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub discovery_policy: Option<String>,
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
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub codex_restart_result: Option<codex_desktop::CodexRestartResult>,
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
            codex_restart_result: None,
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

fn show_main_window(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        #[cfg(target_os = "linux")]
        linux_window::reveal_on_taskbar(&window);
        let _ = window.show();
        let _ = window.unminimize();
        let _ = window.set_focus();
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum AppLifecycleAction {
    CloseToTray,
    TrayExit,
    UpdateRestart,
}

impl AppLifecycleAction {
    const fn requires_gateway_cleanup(self) -> bool {
        matches!(
            self,
            Self::CloseToTray | Self::TrayExit | Self::UpdateRestart
        )
    }

    const fn label(self) -> &'static str {
        match self {
            Self::CloseToTray => "close to tray",
            Self::TrayExit => "tray Exit",
            Self::UpdateRestart => "update restart",
        }
    }
}

pub(crate) fn run_app_lifecycle_action<Cleanup, Action, Output>(
    lifecycle_action: AppLifecycleAction,
    cleanup_gateway: Cleanup,
    action: Action,
) -> Output
where
    Cleanup: FnOnce() -> Result<bool, String>,
    Action: FnOnce() -> Output,
{
    if lifecycle_action.requires_gateway_cleanup() {
        match cleanup_gateway() {
            Ok(true) => log::info!(
                "{} stopped the managed Gateway before the app transitioned",
                lifecycle_action.label()
            ),
            Ok(false) => log::debug!(
                "{} found no managed Gateway to stop before the app transitioned",
                lifecycle_action.label()
            ),
            Err(error) => log::warn!(
                "{} continued after bounded managed Gateway cleanup failed: {error}",
                lifecycle_action.label()
            ),
        }
    }
    action()
}

fn run_tray_action(app: &AppHandle, id: &str) {
    match id {
        TRAY_SHOW => show_main_window(app),
        TRAY_CONNECT_OFFICIAL => {
            request_tray_codex_switch(app, "official");
        }
        TRAY_CONNECT_HUB => {
            request_tray_codex_switch(app, "custom");
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
        TRAY_EXIT => run_app_lifecycle_action(
            AppLifecycleAction::TrayExit,
            proxy::stop_for_app_close,
            || app.exit(0),
        ),
        _ => {}
    }
}

fn request_tray_codex_switch(app: &AppHandle, mode: &str) {
    show_main_window(app);
    if let Err(error) = app.emit(TRAY_CODEX_SWITCH_REQUEST_EVENT, mode) {
        log::warn!("failed to transfer tray Codex switch request to the main window: {error}");
    }
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
        text: format!(
            "{} {action}...",
            gateway_retirement_warning_for_locale(&locale)
        ),
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

    let icon = Image::from_bytes(include_bytes!("../icons/128x128.png"))?;
    let tray = TrayIconBuilder::with_id("codexhub")
        .tooltip("CodexHub")
        .icon(icon)
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

    tray.build(app)?;
    Ok(())
}

fn run_gui() {
    #[cfg(target_os = "linux")]
    linux_window::initialize_identity();

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
            #[cfg(target_os = "linux")]
            linux_window::install(app);
            #[cfg(desktop)]
            if let Err(error) = setup_tray(app) {
                log::error!("failed to setup tray icon: {error}");
            }
            gateway::start_telemetry_ingester();
            web_bridge::start_background(app.handle().clone())?;
            start_gateway_on_launch();
            Ok(())
        })
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = run_app_lifecycle_action(
                    AppLifecycleAction::CloseToTray,
                    || Ok(false),
                    || window.hide(),
                );
            }
        })
        .invoke_handler(tauri_handlers!())
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
        // Migrate legacy top-level context caps before reading startup
        // settings.  This path must run even when settings.json is damaged or
        // the Official snapshot is fresh enough to skip network refresh.
        match codex_desktop::coordinate_unattended(config::migrate_legacy_context_guard) {
            Ok(Some(_)) => {}
            Ok(None) => log::info!(
                "deferred startup context guard migration while Codex Desktop is running"
            ),
            Err(error) => log::warn!("startup context guard migration failed: {error}"),
        }
        let Ok(settings) = config::get_settings() else {
            return;
        };
        let start = || {
            // Keep Gateway lifecycle startup independent from the network-bound
            // Official refresh; the refresh is scheduled after this callback.
            proxy::start_after(|| {
                launch_ready.signal();
                Ok(())
            })
        };
        if let Err(error) = start_gateway_after_startup(settings.auto_start_gateway, start) {
            eprintln!("failed to start CodexHub gateway on app launch: {error}");
        }
        launch_ready.signal();
        spawn_startup_official_refresh();
    });
    // Do not expose the initial window until automatic startup either owns the
    // cross-process gate (and publishes Starting) or has already completed.
    let _ = ready_rx.recv();
}

fn spawn_startup_official_refresh() {
    if let Err(error) = std::thread::Builder::new()
        .name("codexhub-startup-official-refresh".to_string())
        .spawn(|| {
            if let Err(error) = official_refresh::refresh_at_startup() {
                log::warn!("startup Official model refresh failed: {error}");
            }
        })
    {
        log::warn!("failed to start background Official model refresh: {error}");
    }
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
        gateway_retirement_warning_for_locale, run_app_lifecycle_action,
        start_gateway_after_startup, tray_loading_toast, tray_retiring_gateway_loading_toast,
        tray_toast_for, AppLifecycleAction, AppStatus,
    };
    use crate::desktop_commands::handlers::{
        finish_app_status_switch, finish_catalog_write, prepare_then_commit_official_multi_agent,
    };
    use std::cell::{Cell, RefCell};

    #[test]
    fn failed_switch_reopened_is_a_public_structured_status() {
        let coordinated = crate::codex_desktop::CoordinatedSwitch {
            value: None,
            restart_result: crate::codex_desktop::CodexRestartResult::SwitchFailedReopened,
            switch_error: Some("atomic write rejected".to_string()),
        };

        let status = finish_app_status_switch(coordinated, || Ok(AppStatus::scaffold("official")))
            .expect("structured failure status");

        assert_eq!(
            status.codex_restart_result,
            Some(crate::codex_desktop::CodexRestartResult::SwitchFailedReopened)
        );
        assert!(status.message.contains("original Codex Desktop was reopened"));
        assert!(status.message.contains("atomic write rejected"));
    }

    #[test]
    fn catalog_publication_reports_partial_success_when_codex_relaunch_fails() {
        let coordinated = crate::codex_desktop::CoordinatedSwitch {
            value: Some("published"),
            restart_result: crate::codex_desktop::CodexRestartResult::SwitchedRelaunchFailed,
            switch_error: Some("launcher unavailable".to_string()),
        };

        let error = finish_catalog_write(coordinated).expect_err("partial success must be visible");

        assert!(error.starts_with(crate::codex_desktop::SWITCH_RELAUNCH_FAILED_ERROR));
        assert!(error.contains("catalog publication succeeded"));
        assert!(error.contains("launcher unavailable"));
        assert!(error.contains("Start Codex Desktop manually"));
    }

    #[test]
    fn collaboration_prepares_before_final_gate_and_skips_publish_if_codex_reappears() {
        let prepares = Cell::new(0);
        let writes = Cell::new(0);
        let result = prepare_then_commit_official_multi_agent(
            || {
                prepares.set(prepares.get() + 1);
                Ok::<&str, String>("prepared-catalog")
            },
            |_prepared| {
                writes.set(writes.get() + 1);
                Ok(())
            },
            |_prepared, _publish| Ok(None),
        )
        .expect("Codex reappearance should cancel only the final publication");

        assert!(result.is_none());
        assert_eq!(prepares.get(), 1);
        assert_eq!(writes.get(), 0);
    }

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

        let retiring =
            tray_retiring_gateway_loading_toast("same-toast".to_string(), "Stop Gateway");
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

    #[test]
    fn close_to_tray_cleans_up_the_gateway_before_hiding() {
        let cleanup_calls = Cell::new(0);
        let action_calls = Cell::new(0);

        run_app_lifecycle_action(
            AppLifecycleAction::CloseToTray,
            || {
                cleanup_calls.set(cleanup_calls.get() + 1);
                Ok(true)
            },
            || action_calls.set(action_calls.get() + 1),
        );

        assert_eq!(cleanup_calls.get(), 1);
        assert_eq!(action_calls.get(), 1);
    }

    #[test]
    fn tray_exit_and_update_restart_cleanup_before_the_terminal_action() {
        for lifecycle_action in [
            AppLifecycleAction::TrayExit,
            AppLifecycleAction::UpdateRestart,
        ] {
            let events = RefCell::new(Vec::new());

            run_app_lifecycle_action(
                lifecycle_action,
                || {
                    events.borrow_mut().push("cleanup");
                    Ok(true)
                },
                || events.borrow_mut().push("terminal"),
            );

            assert_eq!(events.into_inner(), vec!["cleanup", "terminal"]);
        }
    }

    #[test]
    fn bounded_cleanup_failure_does_not_block_an_orderly_terminal_action() {
        let events = RefCell::new(Vec::new());

        run_app_lifecycle_action(
            AppLifecycleAction::TrayExit,
            || {
                events.borrow_mut().push("cleanup");
                Err("Gateway identity changed".to_string())
            },
            || events.borrow_mut().push("terminal"),
        );

        assert_eq!(events.into_inner(), vec!["cleanup", "terminal"]);
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
            codex_restart_result: None,
        }
    }
}
