//! Web Bridge command adapter (ADR-0010).
//!
//! Argument decoding and command dispatch live here. web_bridge.rs owns the
//! HTTP envelope (origin, body, response) and calls dispatch_web.

use crate::{
    app_updates, autostart, catalog, config, gateway, history, models, openai_usage, proxy,
    xai_auth,
};
use serde_json::Value;
use tauri::AppHandle;

use crate::desktop_commands::Command;

pub fn dispatch_web(command: &str, args: &Value, app: Option<AppHandle>) -> Result<Value, String> {
    let command_name = command;
    let meta = crate::desktop_commands::command_meta(command_name)
        .ok_or_else(|| format!("unknown CodexHub command: {command_name}"))?;
    if !meta.bridge_exposed {
        return Err(format!("unknown CodexHub command: {command_name}"));
    }
    let command =
        crate::desktop_commands::parse_command(command_name).expect("manifest metadata must parse");
    match command {
        Command::GetAppVersion => to_value(Ok(app_updates::get_app_version(desktop_app(&app)?))),
        Command::CheckAppUpdate => to_value(tauri::async_runtime::block_on(
            app_updates::check_app_update(desktop_app(&app)?),
        )),
        Command::StartAppUpdateInstall => {
            to_value(app_updates::start_app_update_install(desktop_app(&app)?))
        }
        Command::GetAppUpdateInstallStatus => to_value(Ok(
            app_updates::get_app_update_install_status(desktop_app(&app)?),
        )),
        Command::ConsumeAppUpdateCompletion => to_value(
            app_updates::consume_app_update_completion(desktop_app(&app)?),
        ),
        Command::InstallAppUpdate => to_value(tauri::async_runtime::block_on(
            app_updates::install_app_update(desktop_app(&app)?),
        )),
        Command::GetStatus => to_value(proxy::status()),
        Command::GetCodexDesktopStatus => to_value(crate::codex_desktop::status()),
        Command::SwitchMode => {
            let mode = registry_string_arg(args, command, "mode")?;
            let auto_sync = registry_bool_arg(args, command, "auto_sync")?;
            let force_takeover =
                registry_optional_bool_arg(args, command, "force_takeover").unwrap_or(false);
            let restart_codex =
                registry_optional_bool_arg(args, command, "restart_codex").unwrap_or(false);
            to_value(crate::switch_mode(
                mode,
                auto_sync,
                Some(force_takeover),
                Some(restart_codex),
            ))
        }
        Command::StartProxy => to_value(crate::start_proxy()),
        Command::StopProxy => to_value(proxy::stop()),
        Command::RestartProxy => to_value(crate::restart_proxy()),
        Command::GetProviders => to_value(config::get_providers()),
        Command::GetBundledProviders => to_value(config::get_bundled_providers()),
        Command::SaveProviders => {
            let providers = serde_json::from_value(
                args.get("providers")
                    .cloned()
                    .ok_or_else(|| "providers argument is required".to_string())?,
            )
            .map_err(|error| format!("invalid providers argument: {error}"))?;
            to_value(config::save_providers(providers))
        }
        Command::GetSettings => {
            to_value(config::get_settings().and_then(autostart::reconcile_settings))
        }
        Command::GetAppFlavor => to_value(Ok(crate::app_flavor::current_info())),
        Command::SaveSettings => {
            let settings = serde_json::from_value(
                args.get("settings")
                    .cloned()
                    .ok_or_else(|| "settings argument is required".to_string())?,
            )
            .map_err(|error| format!("invalid settings argument: {error}"))?;
            to_value(config::save_settings(settings))
        }
        Command::GetCodexContextGuardStatus => to_value(config::get_codex_context_guard_status()),
        Command::SetCodexContextGuard => {
            let enabled = bool_arg(args, "enabled")?;
            let restart_codex =
                registry_optional_bool_arg(args, command, "restart_codex").unwrap_or(false);
            to_value(crate::set_codex_context_guard(enabled, Some(restart_codex)))
        }
        Command::RefreshOfficialModels => {
            let restart_codex =
                registry_optional_bool_arg(args, command, "restart_codex").unwrap_or(false);
            to_value(crate::refresh_official_models_coordinated(restart_codex))
        }
        Command::OpenaiUsageCompletions => {
            let start_time = registry_optional_u64_arg(args, command, "start_time");
            let end_time = registry_optional_u64_arg(args, command, "end_time");
            let force_refresh = registry_optional_bool_arg(args, command, "force_refresh");
            to_value(openai_usage::openai_usage_completions(
                start_time,
                end_time,
                force_refresh,
            ))
        }
        Command::DiscoverProviderModels => {
            let base_url = registry_string_arg(args, command, "base_url")?;
            let api_key = registry_string_arg(args, command, "api_key")?;
            let provider_id = registry_optional_string_arg(args, command, "provider_id");
            to_value(models::discover_provider_models(
                &base_url,
                &api_key,
                provider_id.as_deref(),
            ))
        }
        Command::ProbeUpstreamFormat => {
            let base_url = registry_string_arg(args, command, "base_url")?;
            let api_key = registry_string_arg(args, command, "api_key")?;
            let model = args
                .get("model")
                .and_then(Value::as_str)
                .map(ToOwned::to_owned);
            to_value(models::probe_upstream_format(
                &base_url,
                &api_key,
                model.as_deref(),
            ))
        }
        Command::ProviderProbeUpstreamFormat => {
            let provider_id = registry_string_arg(args, command, "provider_id")?;
            let model = args
                .get("model")
                .and_then(Value::as_str)
                .map(ToOwned::to_owned);
            to_value(gateway::provider_probe_upstream_format(provider_id, model))
        }
        Command::TestModelEndpoint => {
            let base_url = registry_string_arg(args, command, "base_url")?;
            let api_key = registry_string_arg(args, command, "api_key")?;
            let model = registry_string_arg(args, command, "model")?;
            let upstream_format = serde_json::from_value(
                registry_value(args, command, "upstream_format")
                    .cloned()
                    .ok_or_else(|| "upstreamFormat argument is required".to_string())?,
            )
            .map_err(|error| format!("invalid upstreamFormat argument: {error}"))?;
            to_value(models::test_model_endpoint(
                &base_url,
                &api_key,
                &model,
                &upstream_format,
            ))
        }
        Command::GatewayStatus => to_value(gateway::gateway_status()),
        Command::GatewayTestRequest => {
            let kind = serde_json::from_value(
                args.get("kind")
                    .cloned()
                    .ok_or_else(|| "kind argument is required".to_string())?,
            )
            .map_err(|error| format!("invalid gateway test kind: {error}"))?;
            let model = args
                .get("model")
                .and_then(Value::as_str)
                .map(ToOwned::to_owned);
            to_value(gateway::gateway_test_request(kind, model))
        }
        Command::GatewayRecentEvents => {
            let limit = args
                .get("limit")
                .and_then(Value::as_u64)
                .and_then(|value| usize::try_from(value).ok());
            let since_ts = registry_optional_string_arg(args, command, "since_ts");
            to_value(gateway::gateway_recent_events(limit, since_ts))
        }
        Command::GatewayUsageSummary => {
            let start_ts = registry_optional_string_arg(args, command, "start_ts");
            let end_ts = registry_optional_string_arg(args, command, "end_ts");
            to_value(gateway::gateway_usage_summary(start_ts, end_ts))
        }
        Command::GatewayUsageSnapshot => {
            let limit = args
                .get("limit")
                .and_then(Value::as_u64)
                .and_then(|value| usize::try_from(value).ok());
            let start_ts = registry_optional_string_arg(args, command, "start_ts");
            let end_ts = registry_optional_string_arg(args, command, "end_ts");
            to_value(gateway::gateway_usage_snapshot(limit, start_ts, end_ts))
        }
        Command::GatewayUsageEvents => {
            let limit = args
                .get("limit")
                .and_then(Value::as_u64)
                .and_then(|value| usize::try_from(value).ok());
            let start_ts = registry_optional_string_arg(args, command, "start_ts");
            let end_ts = registry_optional_string_arg(args, command, "end_ts");
            to_value(gateway::gateway_usage_events(limit, start_ts, end_ts))
        }
        Command::GatewayCopyClientConfig => {
            let client_kind = registry_optional_string_arg(args, command, "client_kind");
            let model = args
                .get("model")
                .and_then(Value::as_str)
                .map(ToOwned::to_owned);
            to_value(gateway::gateway_copy_client_config(client_kind, model))
        }
        Command::ListGatewayClients => {
            let include_versions =
                registry_optional_bool_arg(args, command, "include_versions").unwrap_or(false);
            to_value(gateway::list_gateway_clients(include_versions))
        }
        Command::DshClientInfo => to_value(Ok(gateway::detect_dsh_client())),
        Command::DshClientConnect => to_value(gateway::dsh_client_connect()),
        Command::DshClientDisconnect => to_value(gateway::dsh_client_disconnect()),
        Command::DshClientReadback => to_value(gateway::dsh_client_readback()),
        Command::PreviewGatewayClientConfig => {
            let client_id = registry_string_arg(args, command, "client_id")?;
            let model = args
                .get("model")
                .and_then(Value::as_str)
                .map(ToOwned::to_owned);
            to_value(gateway::preview_gateway_client_config(client_id, model))
        }
        Command::ApplyGatewayClientConfig => {
            let client_id = registry_string_arg(args, command, "client_id")?;
            let model = args
                .get("model")
                .and_then(Value::as_str)
                .map(ToOwned::to_owned);
            to_value(gateway::apply_gateway_client_config(client_id, model))
        }
        Command::RestoreGatewayClientConfig => {
            let client_id = registry_string_arg(args, command, "client_id")?;
            to_value(gateway::restore_gateway_client_config(client_id))
        }
        Command::SwitchGatewayClientRoute => {
            let client_id = registry_string_arg(args, command, "client_id")?;
            let mode = registry_string_arg(args, command, "mode")?;
            let model = args
                .get("model")
                .and_then(Value::as_str)
                .map(ToOwned::to_owned);
            let force_takeover = registry_optional_bool_arg(args, command, "force_takeover");
            to_value(gateway::switch_gateway_client_route(
                client_id,
                mode,
                model,
                force_takeover,
            ))
        }
        Command::SyncGatewayClients => {
            let model = args
                .get("model")
                .and_then(Value::as_str)
                .map(ToOwned::to_owned);
            to_value(gateway::sync_gateway_clients(model))
        }
        Command::SubagentMatrixStatus => to_value(gateway::subagent_matrix_status()),
        Command::GenerateCatalog => {
            let restart_codex =
                registry_optional_bool_arg(args, command, "restart_codex").unwrap_or(false);
            to_value(crate::generate_catalog_coordinated(restart_codex))
        }
        Command::GetCatalogOverrideDiagnostics => to_value(catalog::catalog_override_diagnostics()),
        Command::ListModels => to_value(models::list_models()),
        Command::RefreshModelMetadata => to_value(models::refresh_model_metadata()),
        Command::ListModelMetadata => to_value(models::list_model_metadata()),
        Command::SaveModelMetadataOverride => {
            let model = serde_json::from_value(
                args.get("model")
                    .cloned()
                    .ok_or_else(|| "model argument is required".to_string())?,
            )
            .map_err(|error| format!("invalid model argument: {error}"))?;
            to_value(models::save_model_metadata_override(model))
        }
        Command::SaveOfficialMultiAgentVersion => {
            let model_id = registry_optional_string_arg(args, command, "model_id")
                .ok_or_else(|| "modelId argument is required".to_string())?;
            let version = args
                .get("version")
                .and_then(Value::as_str)
                .map(str::to_string);
            let restart_codex =
                registry_optional_bool_arg(args, command, "restart_codex").unwrap_or(false);
            to_value(crate::save_official_multi_agent_version_coordinated(
                model_id,
                version,
                restart_codex,
            ))
        }
        Command::ListOfficialMultiAgentOverrides => {
            to_value(models::list_official_multi_agent_overrides())
        }
        Command::ListOfficialMultiAgentBaselines => {
            to_value(models::list_official_multi_agent_baselines())
        }
        Command::SyncHistory => {
            let target_provider = registry_optional_string_arg(args, command, "target_provider");
            to_value(history::sync_history(target_provider.as_deref()))
        }
        Command::ReconcileAfterRouteSwitch => {
            let target_provider = registry_optional_string_arg(args, command, "target_provider");
            to_value(history::reconcile_after_route_switch(
                target_provider.as_deref(),
            ))
        }
        Command::MigrateOfficialHistoryToUnified => {
            to_value(history::migrate_official_history_to_unified())
        }
        Command::RestoreOfficialHistoryFromUnified => {
            to_value(history::restore_official_history_from_unified())
        }
        Command::PreflightUnifiedHistory => {
            let apply_repairs = registry_optional_bool_arg(args, command, "apply_repairs")
                .or_else(|| registry_optional_bool_arg(args, command, "request_restart"))
                .unwrap_or(false);
            let target_unified = registry_optional_bool_arg(args, command, "target_unified");
            to_value(history::preflight_unified_history(
                apply_repairs,
                target_unified,
            ))
        }
        Command::GetConversationSyncStatus => {
            to_value(history::preflight_unified_history(false, None))
        }
        Command::SyncConversationHistory => {
            let target_provider = registry_optional_string_arg(args, command, "target_provider");
            to_value(history::preflight_unified_history(
                true,
                target_provider.map(|value| value != "openai"),
            ))
        }
        Command::DiagnoseConversationHistory => {
            let full_scan = registry_optional_bool_arg(args, command, "full_scan").unwrap_or(true);
            to_value(history::diagnose_unified_history(full_scan))
        }
        Command::SyncCatalog => {
            let restart_codex =
                registry_optional_bool_arg(args, command, "restart_codex").unwrap_or(false);
            to_value(crate::sync_catalog_coordinated(restart_codex))
        }
        Command::SetAutostart => to_value(autostart::set_autostart(registry_bool_arg(
            args, command, "enabled",
        )?)),
        Command::RemoveAutostart => to_value(autostart::remove_autostart()),
        Command::GetAutostartStatus => to_value(autostart::get_autostart_status()),
        Command::OpenCodexApp => to_value(crate::open_codex_app()),
        Command::XaiAuthStatus => to_value(xai_auth::xai_auth_status_blocking()),
        Command::XaiStartDeviceLogin => to_value(xai_auth::xai_start_device_login_blocking()),
        Command::XaiPollDeviceLogin => {
            let device_json = registry_optional_string_arg(args, command, "device_json")
                .ok_or_else(|| "deviceJson argument is required".to_string())?;
            to_value(xai_auth::xai_poll_device_login_blocking(device_json))
        }
        Command::XaiLogout => to_value(xai_auth::xai_logout_blocking()),
        Command::XaiUsageSnapshot => to_value(xai_auth::xai_usage_snapshot_blocking()),
        Command::XaiOpenVerificationUrl => {
            let url = optional_string_arg(args, &["url"])
                .ok_or_else(|| "url argument is required".to_string())?;
            to_value(xai_auth::xai_open_verification_url_blocking(url))
        }
        // These commands are registered for the desktop handler or retained
        // as an internal compatibility entry, but deliberately have no Web
        // Bridge implementation. Keep them explicit so adding a registry row
        // cannot silently produce an untyped fallback arm.
        Command::DiagnosticsStatus
        | Command::DiagnosticsManualMark
        | Command::DiagnosticsPause
        | Command::DiagnosticsResume
        | Command::DiagnosticsDeleteIncident
        | Command::WindowMinimize
        | Command::WindowToggleMaximize
        | Command::WindowCloseToTray => Err(format!("unknown CodexHub command: {command_name}")),
    }
}

fn desktop_app(app: &Option<AppHandle>) -> Result<AppHandle, String> {
    app.clone()
        .ok_or_else(|| "desktop app context is unavailable for this bridge command".to_string())
}

fn to_value<T: serde::Serialize>(result: Result<T, String>) -> Result<Value, String> {
    result.and_then(|value| {
        serde_json::to_value(value).map_err(|error| format!("failed to encode response: {error}"))
    })
}

/// Resolve a wire argument from the aliases declared on the registry row.
/// Aliases are tried in declaration order, then the canonical snake_case key,
/// so legacy camelCase callers retain their precedence without another list
/// of command-specific names in this adapter.
fn registry_argument_names(command: Command, canonical: &str) -> Vec<&str> {
    let mut names = crate::desktop_commands::COMMANDS
        .iter()
        .find(|meta| meta.command == command)
        .map(|meta| {
            meta.argument_aliases
                .iter()
                .filter_map(|(alias, target)| (*target == canonical).then_some(*alias))
                .chain(std::iter::once(canonical))
                .collect()
        })
        .unwrap_or_else(|| vec![canonical]);
    names.dedup();
    names
}

fn registry_string_arg(args: &Value, command: Command, canonical: &str) -> Result<String, String> {
    let names = registry_argument_names(command, canonical);
    optional_string_arg(args, &names).ok_or_else(|| format!("{} argument is required", names[0]))
}

fn registry_bool_arg(args: &Value, command: Command, canonical: &str) -> Result<bool, String> {
    let names = registry_argument_names(command, canonical);
    names
        .iter()
        .find_map(|name| args.get(*name).and_then(Value::as_bool))
        .ok_or_else(|| format!("{} argument is required", names[0]))
}

fn registry_value<'a>(args: &'a Value, command: Command, canonical: &str) -> Option<&'a Value> {
    registry_argument_names(command, canonical)
        .iter()
        .find_map(|name| args.get(*name))
}

fn registry_optional_string_arg(args: &Value, command: Command, canonical: &str) -> Option<String> {
    let names = registry_argument_names(command, canonical);
    optional_string_arg(args, &names)
}

fn registry_optional_u64_arg(args: &Value, command: Command, canonical: &str) -> Option<u64> {
    let names = registry_argument_names(command, canonical);
    optional_u64_arg(args, &names)
}

fn registry_optional_bool_arg(args: &Value, command: Command, canonical: &str) -> Option<bool> {
    let names = registry_argument_names(command, canonical);
    optional_bool_arg(args, &names)
}

fn bool_arg(args: &Value, name: &str) -> Result<bool, String> {
    args.get(name)
        .and_then(Value::as_bool)
        .ok_or_else(|| format!("{name} argument is required"))
}

pub(crate) fn optional_string_arg(args: &Value, names: &[&str]) -> Option<String> {
    names.iter().find_map(|name| {
        args.get(*name)
            .and_then(Value::as_str)
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .map(ToOwned::to_owned)
    })
}

fn optional_u64_arg(args: &Value, names: &[&str]) -> Option<u64> {
    names
        .iter()
        .find_map(|name| args.get(*name).and_then(Value::as_u64))
}

pub(crate) fn optional_bool_arg(args: &Value, names: &[&str]) -> Option<bool> {
    names
        .iter()
        .find_map(|name| args.get(*name).and_then(Value::as_bool))
}
