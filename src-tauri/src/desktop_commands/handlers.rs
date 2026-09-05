//! Thin Tauri command bodies owned by the Desktop Command seam (ADR-0010).
//!
//! Domain logic stays in config/catalog/models/proxy. This module is the
//! Handler bodies referenced by the registry in `desktop_commands::mod`.

use serde::Serialize;
use std::sync::Mutex;
use tauri::{PhysicalPosition, PhysicalSize, Window};

use crate::{
    app_flavor, autostart, catalog, codex_desktop, config, diagnostics, gateway, history, models,
    official_refresh, openai_usage, proxy, AppStatus, Model, Provider, Settings, UpstreamFormat,
};

#[tauri::command]
pub fn get_codex_desktop_status() -> Result<codex_desktop::CodexDesktopStatus, String> {
    codex_desktop::status()
}

#[tauri::command]
pub fn start_proxy() -> Result<AppStatus, String> {
    proxy::start_after(|| Ok(()))
}

#[tauri::command]
pub fn stop_proxy() -> Result<AppStatus, String> {
    proxy::stop()
}

#[tauri::command]
pub fn restart_proxy() -> Result<AppStatus, String> {
    proxy::restart_after(|| Ok(()))
}

#[tauri::command]
pub fn get_providers() -> Result<Vec<Provider>, String> {
    config::get_providers()
}

#[tauri::command]
pub fn get_bundled_providers() -> Result<Vec<Provider>, String> {
    config::get_bundled_providers()
}

#[tauri::command]
pub fn save_providers(providers: Vec<Provider>) -> Result<Vec<Provider>, String> {
    config::save_providers(providers)
}

#[tauri::command]
pub fn get_settings() -> Result<Settings, String> {
    autostart::reconcile_settings(config::get_settings()?)
}

#[tauri::command]
pub fn get_app_flavor() -> app_flavor::AppFlavorInfo {
    app_flavor::current_info()
}

#[tauri::command]
pub fn save_settings(settings: Settings) -> Result<Settings, String> {
    config::save_settings(settings)
}

#[tauri::command]
pub fn get_codex_context_guard_status() -> Result<config::CodexContextGuardStatus, String> {
    config::get_codex_context_guard_status()
}

#[tauri::command]
pub fn get_catalog_override_diagnostics() -> Result<catalog::CatalogOverrideDiagnostics, String> {
    catalog::catalog_override_diagnostics()
}

#[tauri::command]
pub fn list_models() -> Result<Vec<Model>, String> {
    models::list_models()
}

#[tauri::command]
pub fn refresh_model_metadata() -> Result<Vec<Model>, String> {
    models::refresh_model_metadata()
}

#[tauri::command]
pub fn list_model_metadata() -> Result<Vec<Model>, String> {
    models::list_model_metadata()
}

#[tauri::command]
pub fn save_model_metadata_override(model: Model) -> Result<Model, String> {
    models::save_model_metadata_override(model)
}

pub(crate) async fn run_blocking<T, F>(name: &'static str, task: F) -> Result<T, String>
where
    T: Send + 'static,
    F: FnOnce() -> Result<T, String> + Send + 'static,
{
    tauri::async_runtime::spawn_blocking(task)
        .await
        .map_err(|error| format!("{name} task failed: {error}"))?
}

#[tauri::command]
pub async fn get_status() -> Result<AppStatus, String> {
    run_blocking("get_status", proxy::status).await
}

#[tauri::command]
pub async fn openai_usage_completions(
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
pub fn discover_provider_models(
    base_url: String,
    api_key: String,
    provider_id: Option<String>,
) -> Result<Vec<Model>, String> {
    models::discover_provider_models(&base_url, &api_key, provider_id.as_deref())
}

#[tauri::command]
pub fn probe_upstream_format(
    base_url: String,
    api_key: String,
    model: Option<String>,
) -> Result<serde_json::Value, String> {
    models::probe_upstream_format(&base_url, &api_key, model.as_deref())
}

#[tauri::command]
pub fn provider_probe_upstream_format(
    provider_id: String,
    model: Option<String>,
) -> Result<serde_json::Value, String> {
    gateway::provider_probe_upstream_format(provider_id, model)
}

#[tauri::command]
pub fn test_model_endpoint(
    base_url: String,
    api_key: String,
    model: String,
    upstream_format: UpstreamFormat,
) -> Result<serde_json::Value, String> {
    models::test_model_endpoint(&base_url, &api_key, &model, &upstream_format)
}

#[tauri::command]
pub async fn gateway_status() -> Result<gateway::GatewayStatus, String> {
    run_blocking("gateway_status", gateway::gateway_status).await
}

#[tauri::command]
pub async fn diagnostics_status() -> Result<diagnostics::DiagnosticsStatus, String> {
    run_blocking("diagnostics_status", diagnostics::status).await
}

#[tauri::command]
pub async fn diagnostics_manual_mark() -> Result<diagnostics::DiagnosticsActionResult, String> {
    run_blocking("diagnostics_manual_mark", diagnostics::manual_mark).await
}

#[tauri::command]
pub async fn diagnostics_pause() -> Result<diagnostics::DiagnosticsActionResult, String> {
    run_blocking("diagnostics_pause", diagnostics::pause).await
}

#[tauri::command]
pub async fn diagnostics_resume() -> Result<diagnostics::DiagnosticsActionResult, String> {
    run_blocking("diagnostics_resume", diagnostics::resume).await
}

#[tauri::command]
pub async fn diagnostics_delete_incident(
    incident_id: String,
) -> Result<diagnostics::DiagnosticsActionResult, String> {
    run_blocking("diagnostics_delete_incident", move || {
        diagnostics::delete_incident(incident_id)
    })
    .await
}

#[tauri::command]
pub fn gateway_test_request(
    kind: gateway::GatewayTestKind,
    model: Option<String>,
) -> Result<gateway::GatewayTestResult, String> {
    gateway::gateway_test_request(kind, model)
}

#[tauri::command]
pub async fn gateway_recent_events(
    limit: Option<usize>,
    since_ts: Option<String>,
) -> Result<Vec<gateway::GatewayEvent>, String> {
    run_blocking("gateway_recent_events", move || {
        gateway::gateway_recent_events(limit, since_ts)
    })
    .await
}

#[tauri::command]
pub async fn gateway_usage_summary(
    start_ts: Option<String>,
    end_ts: Option<String>,
) -> Result<gateway::GatewayUsageSummary, String> {
    run_blocking("gateway_usage_summary", move || {
        gateway::gateway_usage_summary(start_ts, end_ts)
    })
    .await
}

#[tauri::command]
pub async fn gateway_usage_snapshot(
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
pub async fn gateway_usage_events(
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
pub fn gateway_copy_client_config(
    client_kind: Option<String>,
    model: Option<String>,
) -> Result<gateway::GatewayClientConfig, String> {
    gateway::gateway_copy_client_config(client_kind, model)
}

#[tauri::command]
pub async fn list_gateway_clients(
    include_versions: Option<bool>,
) -> Result<Vec<gateway::GatewayClientInfo>, String> {
    run_blocking("list_gateway_clients", move || {
        gateway::list_gateway_clients(include_versions.unwrap_or(false))
    })
    .await
}

#[tauri::command]
pub fn preview_gateway_client_config(
    client_id: String,
    model: Option<String>,
) -> Result<gateway::GatewayClientConfigPreview, String> {
    gateway::preview_gateway_client_config(client_id, model)
}

#[tauri::command]
pub fn apply_gateway_client_config(
    client_id: String,
    model: Option<String>,
) -> Result<gateway::GatewayClientApplyResult, String> {
    gateway::apply_gateway_client_config(client_id, model)
}

#[tauri::command]
pub async fn dsh_client_info() -> Result<gateway::DshClientInfo, String> {
    run_blocking("dsh_client_info", || Ok(gateway::detect_dsh_client())).await
}

#[tauri::command]
pub async fn dsh_client_connect() -> Result<crate::injection::DshLifecycleReport, String> {
    run_blocking("dsh_client_connect", gateway::dsh_client_connect).await
}

#[tauri::command]
pub async fn dsh_client_disconnect() -> Result<crate::injection::DshLifecycleReport, String> {
    run_blocking("dsh_client_disconnect", gateway::dsh_client_disconnect).await
}

#[tauri::command]
pub async fn dsh_client_readback() -> Result<crate::injection::DshLifecycleReport, String> {
    run_blocking("dsh_client_readback", gateway::dsh_client_readback).await
}

#[tauri::command]
pub fn restore_gateway_client_config(
    client_id: String,
) -> Result<gateway::GatewayClientApplyResult, String> {
    gateway::restore_gateway_client_config(client_id)
}

#[tauri::command]
pub fn switch_gateway_client_route(
    client_id: String,
    mode: String,
    model: Option<String>,
    force_takeover: Option<bool>,
) -> Result<gateway::GatewayClientApplyResult, String> {
    gateway::switch_gateway_client_route(client_id, mode, model, force_takeover)
}

#[tauri::command]
pub async fn sync_gateway_clients(
    model: Option<String>,
) -> Result<gateway::GatewayClientSyncSummary, String> {
    run_blocking("sync_gateway_clients", move || {
        gateway::sync_gateway_clients(model)
    })
    .await
}

#[tauri::command]
pub fn subagent_matrix_status() -> Result<gateway::SubagentMatrixStatus, String> {
    gateway::subagent_matrix_status()
}

#[tauri::command]
pub fn list_official_multi_agent_overrides() -> Result<std::collections::HashMap<String, String>, String>
{
    models::list_official_multi_agent_overrides()
}

#[tauri::command]
pub fn list_official_multi_agent_baselines() -> Result<std::collections::HashMap<String, String>, String>
{
    models::list_official_multi_agent_baselines()
}

#[tauri::command]
pub fn set_autostart(enabled: bool) -> Result<String, String> {
    autostart::set_autostart(enabled)
}

#[tauri::command]
pub fn remove_autostart() -> Result<String, String> {
    autostart::remove_autostart()
}

#[tauri::command]
pub fn get_autostart_status() -> Result<autostart::AutostartStatus, String> {
    autostart::get_autostart_status()
}

#[tauri::command]
pub async fn sync_history(target_provider: Option<String>) -> Result<String, String> {
    run_blocking("sync_history", move || {
        history::sync_history(target_provider.as_deref())
    })
    .await
}

#[tauri::command]
pub async fn reconcile_after_route_switch(
    target_provider: Option<String>,
) -> Result<history::UnifiedHistoryResult, String> {
    run_blocking("reconcile_after_route_switch", move || {
        history::reconcile_after_route_switch(target_provider.as_deref())
    })
    .await
}

#[tauri::command]
pub async fn migrate_official_history_to_unified() -> Result<String, String> {
    run_blocking("migrate_official_history_to_unified", || {
        history::migrate_official_history_to_unified()
    })
    .await
}

#[tauri::command]
pub async fn restore_official_history_from_unified() -> Result<String, String> {
    run_blocking("restore_official_history_from_unified", || {
        history::restore_official_history_from_unified()
    })
    .await
}

#[tauri::command]
pub async fn preflight_unified_history(
    apply_repairs: bool,
    target_unified: Option<bool>,
) -> Result<history::UnifiedHistoryResult, String> {
    run_blocking("preflight_unified_history", move || {
        history::preflight_unified_history(apply_repairs, target_unified)
    })
    .await
}

#[tauri::command]
pub async fn get_conversation_sync_status() -> Result<history::UnifiedHistoryResult, String> {
    run_blocking("get_conversation_sync_status", || {
        history::preflight_unified_history(false, None)
    })
    .await
}

#[tauri::command]
pub async fn sync_conversation_history(
    target_provider: Option<String>,
) -> Result<history::UnifiedHistoryResult, String> {
    let target_unified = target_provider.as_deref().map(|value| value != "openai");
    run_blocking("sync_conversation_history", move || {
        history::preflight_unified_history(true, target_unified)
    })
    .await
}

#[tauri::command]
pub async fn diagnose_conversation_history(
    full_scan: Option<bool>,
) -> Result<history::UnifiedHistoryResult, String> {
    let full_scan = full_scan.unwrap_or(true);
    run_blocking("diagnose_conversation_history", move || {
        history::diagnose_unified_history(full_scan)
    })
    .await
}

#[tauri::command]
pub async fn refresh_official_models(
    restart_codex: Option<bool>,
) -> Result<official_refresh::OfficialRefreshResult, String> {
    run_blocking("refresh_official_models", move || {
        refresh_official_models_coordinated(restart_codex.unwrap_or(false))
    })
    .await
}

pub(crate) fn refresh_official_models_coordinated(
    restart_codex: bool,
) -> Result<official_refresh::OfficialRefreshResult, String> {
    // The frontend Refresh action is read-only. The legacy boolean remains
    // accepted as an explicit opt-in for callers that intentionally apply the
    // managed Codex overlay in the same transaction.
    if !restart_codex {
        return official_refresh::refresh_current_models();
    }
    refresh_official_models_published_coordinated(restart_codex)
}

pub(crate) fn refresh_official_models_published_coordinated(
    restart_codex: bool,
) -> Result<official_refresh::OfficialRefreshResult, String> {
    let coordinated =
        codex_desktop::coordinate_switch(restart_codex, official_refresh::refresh_manual)?;
    let Some(mut result) = coordinated.value else {
        return Err(format!(
            "codex_desktop_switch_failed_reopened: {}",
            coordinated
                .switch_error
                .unwrap_or_else(|| "Official catalog refresh failed".to_string())
        ));
    };
    result.codex_restart_result = Some(coordinated.restart_result);
    if coordinated.restart_result == codex_desktop::CodexRestartResult::Restarted {
        match official_refresh::acknowledge_codex_restart() {
            Ok(()) => result.restart_required = false,
            Err(error) => log::warn!(
                "Codex Desktop restarted, but the Official restart requirement could not be acknowledged: {error}"
            ),
        }
    }
    Ok(result)
}

#[tauri::command]
pub async fn generate_catalog(restart_codex: Option<bool>) -> Result<Vec<Model>, String> {
    run_blocking("generate_catalog", move || {
        generate_catalog_coordinated(restart_codex.unwrap_or(false))
    })
    .await
}

pub(crate) fn generate_catalog_coordinated(restart_codex: bool) -> Result<Vec<Model>, String> {
    if !restart_codex {
        return codex_desktop::serialize_config_writer(catalog::generate_catalog_with_existing_lock);
    }
    coordinated_catalog_write(restart_codex, catalog::generate_catalog_with_existing_lock)
}

#[tauri::command]
pub async fn save_official_multi_agent_version(
    model_id: String,
    version: Option<String>,
    restart_codex: Option<bool>,
) -> Result<OfficialMultiAgentSaveResult, String> {
    run_blocking("save_official_multi_agent_version", move || {
        save_official_multi_agent_version_coordinated(
            model_id,
            version,
            restart_codex.unwrap_or(false),
        )
    })
    .await
}

#[derive(Debug, Clone, Serialize)]
pub struct OfficialMultiAgentSaveResult {
    model: Model,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    warning: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    codex_restart_result: Option<codex_desktop::CodexRestartResult>,
}

pub(crate) fn save_official_multi_agent_version_coordinated(
    model_id: String,
    version: Option<String>,
    restart_codex: bool,
) -> Result<OfficialMultiAgentSaveResult, String> {
    if !restart_codex {
        let outcome = codex_desktop::serialize_config_writer(|| {
            let prepared = models::prepare_official_multi_agent_version(model_id, version)?;
            models::publish_official_multi_agent_version(prepared).map_err(|error| error.to_string())
        })?;
        return Ok(OfficialMultiAgentSaveResult {
            model: outcome.model,
            warning: outcome.warning,
            codex_restart_result: None,
        });
    }
    let coordinated = codex_desktop::coordinate_switch(restart_codex, || {
        prepare_then_commit_official_multi_agent(
            || {
                models::prepare_official_multi_agent_version(model_id, version)
                    .map_err(codex_desktop::SwitchMutationError::from)
            },
            models::publish_official_multi_agent_version,
            |prepared, publish| codex_desktop::run_if_stopped(|| publish(prepared)),
        )?
        .ok_or_else(|| {
            codex_desktop::SwitchMutationError::from(format!(
                "{}: Codex Desktop started before the Collaboration catalog commit; no catalog or Codex configuration was written",
                codex_desktop::BECAME_RUNNING_ERROR
            ))
        })
    })?;
    let Some(outcome) = coordinated.value else {
        return Err(format!(
            "codex_desktop_switch_failed_reopened: {}",
            coordinated
                .switch_error
                .unwrap_or_else(|| "Collaboration catalog update failed".to_string())
        ));
    };
    Ok(OfficialMultiAgentSaveResult {
        model: outcome.model,
        warning: outcome.warning,
        codex_restart_result: Some(coordinated.restart_result),
    })
}

pub(crate) fn prepare_then_commit_official_multi_agent<Prepared, Output, Prepare, Publish, Gate, Error>(
    prepare: Prepare,
    publish: Publish,
    gate: Gate,
) -> Result<Option<Output>, Error>
where
    Prepare: FnOnce() -> Result<Prepared, Error>,
    Publish: FnOnce(Prepared) -> Result<Output, Error>,
    Gate: FnOnce(Prepared, Publish) -> Result<Option<Output>, Error>,
{
    let prepared = prepare()?;
    gate(prepared, publish)
}

#[tauri::command]
pub fn sync_catalog(restart_codex: Option<bool>) -> Result<String, String> {
    sync_catalog_coordinated(restart_codex.unwrap_or(false))
}

pub(crate) fn sync_catalog_coordinated(restart_codex: bool) -> Result<String, String> {
    coordinated_catalog_write(restart_codex, catalog::sync_catalog_with_existing_lock)
}

fn coordinated_catalog_write<T>(
    restart_codex: bool,
    write: impl FnOnce() -> Result<T, String>,
) -> Result<T, String> {
    let coordinated = codex_desktop::coordinate_switch(restart_codex, write)?;
    finish_catalog_write(coordinated)
}

pub(crate) fn finish_catalog_write<T>(
    coordinated: codex_desktop::CoordinatedSwitch<T>,
) -> Result<T, String> {
    if coordinated.restart_result == codex_desktop::CodexRestartResult::SwitchedRelaunchFailed {
        return Err(format!(
            "{}: catalog publication succeeded, but Codex Desktop could not be reopened ({}). Start Codex Desktop manually.",
            codex_desktop::SWITCH_RELAUNCH_FAILED_ERROR,
            coordinated
                .switch_error
                .unwrap_or_else(|| "unknown relaunch failure".to_string())
        ));
    }
    coordinated.value.ok_or_else(|| {
        format!(
            "codex_desktop_switch_failed_reopened: {}",
            coordinated
                .switch_error
                .unwrap_or_else(|| "catalog publication failed".to_string())
        )
    })
}

#[tauri::command]
pub fn switch_mode(
    mode: String,
    auto_sync: bool,
    force_takeover: Option<bool>,
    restart_codex: Option<bool>,
) -> Result<AppStatus, String> {
    let coordinated = codex_desktop::coordinate_switch(restart_codex.unwrap_or(false), || {
        config::switch_mode_with_takeover(&mode, auto_sync, force_takeover.unwrap_or(false))
    })?;
    finish_app_status_switch(coordinated, proxy::status)
}

pub(crate) fn finish_app_status_switch<Readback>(
    coordinated: codex_desktop::CoordinatedSwitch<AppStatus>,
    readback: Readback,
) -> Result<AppStatus, String>
where
    Readback: FnOnce() -> Result<AppStatus, String>,
{
    let Some(mut status) = coordinated.value else {
        let error = coordinated
            .switch_error
            .unwrap_or_else(|| "configuration switch failed".to_string());
        let mut status = readback().unwrap_or_else(|readback_error| {
            AppStatus::scaffold(format!(
                "configuration switch failed: {error}; status readback failed: {readback_error}"
            ))
        });
        status.message = format!(
            "configuration switch failed; the original Codex Desktop was reopened: {error}"
        );
        status.codex_restart_result = Some(codex_desktop::CodexRestartResult::SwitchFailedReopened);
        return Ok(status);
    };
    status.codex_restart_result = Some(coordinated.restart_result);
    if coordinated.restart_result == codex_desktop::CodexRestartResult::SwitchedRelaunchFailed {
        status.message = format!(
            "{}; configuration switched, but Codex Desktop could not be reopened: {}. Start Codex Desktop manually.",
            status.message,
            coordinated
                .switch_error
                .unwrap_or_else(|| "unknown relaunch failure".to_string())
        );
    }
    Ok(status)
}

#[tauri::command]
pub fn set_codex_context_guard(
    enabled: bool,
    restart_codex: Option<bool>,
) -> Result<config::CodexContextGuardStatus, String> {
    if !restart_codex.unwrap_or(false) {
        return codex_desktop::serialize_config_writer(|| config::set_codex_context_guard(enabled));
    }
    let coordinated = codex_desktop::coordinate_switch(restart_codex.unwrap_or(false), || {
        config::set_codex_context_guard(enabled)
    })?;
    let Some(mut status) = coordinated.value else {
        return Err(format!(
            "codex_desktop_switch_failed_reopened: {}",
            coordinated
                .switch_error
                .unwrap_or_else(|| "context guard update failed".to_string())
        ));
    };
    status.codex_restart_result = Some(coordinated.restart_result);
    Ok(status)
}

#[tauri::command]
pub fn open_codex_app() -> Result<String, String> {
    codex_desktop::launch()?;
    Ok("Opened Codex Desktop".to_string())
}

#[tauri::command]
pub fn window_minimize(window: Window) -> Result<(), String> {
    window
        .minimize()
        .map_err(|error| format!("failed to minimize window: {error}"))
}

#[derive(Clone, Copy, Default)]
pub(crate) struct LinuxWindowRestore {
    pub(crate) maximized: bool,
    pub(crate) size: Option<PhysicalSize<u32>>,
    pub(crate) position: Option<PhysicalPosition<i32>>,
}

static LINUX_WINDOW_RESTORE: Mutex<LinuxWindowRestore> = Mutex::new(LinuxWindowRestore {
    maximized: false,
    size: None,
    position: None,
});

#[tauri::command]
pub fn window_toggle_maximize(window: Window) -> Result<(), String> {
    if cfg!(target_os = "linux") {
        return toggle_linux_window_maximize(window);
    }
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

fn toggle_linux_window_maximize(window: Window) -> Result<(), String> {
    let (restore, size, position) = {
        let mut state = LINUX_WINDOW_RESTORE
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner());
        if state.maximized {
            let size = state.size.take();
            let position = state.position.take();
            state.maximized = false;
            (true, size, position)
        } else {
            state.size = window.inner_size().ok();
            state.position = window.outer_position().ok();
            state.maximized = true;
            (false, None, None)
        }
    };
    if !restore {
        return window
            .maximize()
            .map_err(|error| format!("failed to maximize window: {error}"));
    }
    let unmaximize_error = window.unmaximize().err();
    if let Some(size) = size {
        window
            .set_size(size)
            .map_err(|error| format!("failed to restore window: {error}"))?;
    }
    if let Some(position) = position {
        if let Err(error) = window.set_position(position) {
            log::warn!("failed to restore window position: {error}");
        }
    }
    if size.is_none() {
        if let Some(error) = unmaximize_error {
            return Err(format!("failed to restore window: {error}"));
        }
    }
    Ok(())
}

#[tauri::command]
pub fn window_close_to_tray(window: Window) -> Result<(), String> {
    crate::run_app_lifecycle_action(
        crate::AppLifecycleAction::CloseToTray,
        || Ok(false),
        || {
            window
                .hide()
                .map_err(|error| format!("failed to hide window to tray: {error}"))
        },
    )
}
