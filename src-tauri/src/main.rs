#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

mod autostart;
mod catalog;
mod cli;
mod config;
mod history;
mod models;
mod proxy;

use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Model {
    pub id: String,
    pub display_name: Option<String>,
    pub upstream_model: Option<String>,
    pub context_window: Option<u32>,
    pub max_output_tokens: Option<u32>,
    pub sort_order: Option<i32>,
    #[serde(default = "default_enabled")]
    pub enabled: bool,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Provider {
    pub id: String,
    pub name: String,
    pub base_url: String,
    pub api_key: Option<String>,
    pub display_prefix: Option<String>,
    pub sort_order: Option<i32>,
    #[serde(default = "default_enabled")]
    pub enabled: bool,
    #[serde(default)]
    pub models: Vec<Model>,
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
    pub proxy_port: u16,
}

impl Default for Settings {
    fn default() -> Self {
        Self {
            auto_sync_history: true,
            auto_start_proxy: true,
            include_official_models: true,
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
fn switch_mode(mode: String) -> Result<AppStatus, String> {
    config::switch_mode(&mode)
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
fn discover_provider_models(provider_id: String) -> Result<Vec<Model>, String> {
    models::discover_provider_models(&provider_id)
}

#[tauri::command]
fn generate_catalog() -> Result<String, String> {
    catalog::generate_catalog()
}

#[tauri::command]
fn list_models() -> Result<Vec<Model>, String> {
    models::list_models()
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
            generate_catalog,
            list_models,
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
        if first_arg != "app" {
            std::process::exit(cli::run(&args));
        }
    }

    run_gui();
}
