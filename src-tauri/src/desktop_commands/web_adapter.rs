//! Web Bridge command adapter (ADR-0010).
//!
//! Argument decoding and command dispatch live here. web_bridge.rs owns the
//! HTTP envelope (origin, body, response) and calls dispatch_web.

use crate::{
    app_updates, autostart, catalog, config, gateway, history, models, openai_usage, proxy, xai_auth,
};
use serde_json::Value;
use tauri::AppHandle;

pub fn dispatch_web(
    command: &str,
    args: &Value,
    app: Option<AppHandle>,
) -> Result<Value, String> {
    if crate::desktop_commands::command_meta(command).is_none() {
        return Err(format!("unknown CodexHub command: {command}"));
    }
    match command {
        "get_app_version" => to_value(Ok(app_updates::get_app_version(desktop_app(&app)?))),
        "check_app_update" => to_value(tauri::async_runtime::block_on(
            app_updates::check_app_update(desktop_app(&app)?),
        )),
        "start_app_update_install" => {
            to_value(app_updates::start_app_update_install(desktop_app(&app)?))
        }
        "get_app_update_install_status" => to_value(Ok(
            app_updates::get_app_update_install_status(desktop_app(&app)?),
        )),
        "consume_app_update_completion" => to_value(app_updates::consume_app_update_completion(
            desktop_app(&app)?,
        )),
        "install_app_update" => to_value(tauri::async_runtime::block_on(
            app_updates::install_app_update(desktop_app(&app)?),
        )),
        "get_status" => to_value(proxy::status()),
        "get_codex_desktop_status" => to_value(crate::codex_desktop::status()),
        "switch_mode" => {
            let mode = string_arg(args, "mode")?;
            let auto_sync = bool_arg(args, "autoSync")?;
            let force_takeover =
                optional_bool_arg(args, &["forceTakeover", "force_takeover"])
                    .unwrap_or(false);
            let restart_codex =
                optional_bool_arg(args, &["restartCodex", "restart_codex"])
                    .unwrap_or(false);
            to_value(crate::switch_mode(
                mode,
                auto_sync,
                Some(force_takeover),
                Some(restart_codex),
            ))
        }
        "start_proxy" => to_value(crate::start_proxy()),
        "stop_proxy" => to_value(proxy::stop()),
        "restart_proxy" => to_value(crate::restart_proxy()),
        "get_providers" => to_value(config::get_providers()),
        "get_bundled_providers" => to_value(config::get_bundled_providers()),
        "save_providers" => {
            let providers = serde_json::from_value(
                args
                    .get("providers")
                    .cloned()
                    .ok_or_else(|| "providers argument is required".to_string())?,
            )
            .map_err(|error| format!("invalid providers argument: {error}"))?;
            to_value(config::save_providers(providers))
        }
        "get_settings" => to_value(config::get_settings().and_then(autostart::reconcile_settings)),
        "get_app_flavor" => to_value(Ok(crate::app_flavor::current_info())),
        "save_settings" => {
            let settings = serde_json::from_value(
                args
                    .get("settings")
                    .cloned()
                    .ok_or_else(|| "settings argument is required".to_string())?,
            )
            .map_err(|error| format!("invalid settings argument: {error}"))?;
            to_value(config::save_settings(settings))
        }
        "get_codex_context_guard_status" => to_value(config::get_codex_context_guard_status()),
        "set_codex_context_guard" => {
            let enabled = bool_arg(args, "enabled")?;
            let restart_codex =
                optional_bool_arg(args, &["restartCodex", "restart_codex"])
                    .unwrap_or(false);
            to_value(crate::set_codex_context_guard(enabled, Some(restart_codex)))
        }
        "refresh_official_models" => {
            let restart_codex =
                optional_bool_arg(args, &["restartCodex", "restart_codex"])
                    .unwrap_or(false);
            to_value(crate::refresh_official_models_coordinated(restart_codex))
        }
        "openai_usage_completions" => {
            let start_time = optional_u64_arg(args, &["startTime", "start_time"]);
            let end_time = optional_u64_arg(args, &["endTime", "end_time"]);
            let force_refresh =
                optional_bool_arg(args, &["forceRefresh", "force_refresh"]);
            to_value(openai_usage::openai_usage_completions(
                start_time,
                end_time,
                force_refresh,
            ))
        }
        "discover_provider_models" => {
            let base_url = string_arg(args, "baseUrl")?;
            let api_key = string_arg(args, "apiKey")?;
            let provider_id = optional_string_arg(args, &["providerId", "provider_id"]);
            to_value(models::discover_provider_models(
                &base_url,
                &api_key,
                provider_id.as_deref(),
            ))
        }
        "probe_upstream_format" => {
            let base_url = string_arg(args, "baseUrl")?;
            let api_key = string_arg(args, "apiKey")?;
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
        "provider_probe_upstream_format" => {
            let provider_id = string_arg(args, "providerId")?;
            let model = args
                .get("model")
                .and_then(Value::as_str)
                .map(ToOwned::to_owned);
            to_value(gateway::provider_probe_upstream_format(provider_id, model))
        }
        "test_model_endpoint" => {
            let base_url = string_arg(args, "baseUrl")?;
            let api_key = string_arg(args, "apiKey")?;
            let model = string_arg(args, "model")?;
            let upstream_format = serde_json::from_value(
                args
                    .get("upstreamFormat")
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
        "gateway_status" => to_value(gateway::gateway_status()),
        "gateway_test_request" => {
            let kind = serde_json::from_value(
                args
                    .get("kind")
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
        "gateway_recent_events" => {
            let limit = args
                .get("limit")
                .and_then(Value::as_u64)
                .and_then(|value| usize::try_from(value).ok());
            let since_ts = optional_string_arg(args, &["sinceTs", "since_ts"]);
            to_value(gateway::gateway_recent_events(limit, since_ts))
        }
        "gateway_usage_summary" => {
            let start_ts = optional_string_arg(args, &["startTs", "start_ts"]);
            let end_ts = optional_string_arg(args, &["endTs", "end_ts"]);
            to_value(gateway::gateway_usage_summary(start_ts, end_ts))
        }
        "gateway_usage_snapshot" => {
            let limit = args
                .get("limit")
                .and_then(Value::as_u64)
                .and_then(|value| usize::try_from(value).ok());
            let start_ts = optional_string_arg(args, &["startTs", "start_ts"]);
            let end_ts = optional_string_arg(args, &["endTs", "end_ts"]);
            to_value(gateway::gateway_usage_snapshot(limit, start_ts, end_ts))
        }
        "gateway_usage_events" => {
            let limit = args
                .get("limit")
                .and_then(Value::as_u64)
                .and_then(|value| usize::try_from(value).ok());
            let start_ts = optional_string_arg(args, &["startTs", "start_ts"]);
            let end_ts = optional_string_arg(args, &["endTs", "end_ts"]);
            to_value(gateway::gateway_usage_events(limit, start_ts, end_ts))
        }
        "gateway_copy_client_config" => {
            let client_kind = args
                .get("clientKind")
                .and_then(Value::as_str)
                .map(ToOwned::to_owned);
            let model = args
                .get("model")
                .and_then(Value::as_str)
                .map(ToOwned::to_owned);
            to_value(gateway::gateway_copy_client_config(client_kind, model))
        }
        "list_gateway_clients" => {
            let include_versions = args
                .get("includeVersions")
                .or_else(|| args.get("include_versions"))
                .and_then(Value::as_bool)
                .unwrap_or(false);
            to_value(gateway::list_gateway_clients(include_versions))
        }
        "dsh_client_info" => to_value(Ok(gateway::detect_dsh_client())),
        "dsh_client_connect" => to_value(gateway::dsh_client_connect()),
        "dsh_client_disconnect" => to_value(gateway::dsh_client_disconnect()),
        "dsh_client_readback" => to_value(gateway::dsh_client_readback()),
        "preview_gateway_client_config" => {
            let client_id = string_arg(args, "clientId")?;
            let model = args
                .get("model")
                .and_then(Value::as_str)
                .map(ToOwned::to_owned);
            to_value(gateway::preview_gateway_client_config(client_id, model))
        }
        "apply_gateway_client_config" => {
            let client_id = string_arg(args, "clientId")?;
            let model = args
                .get("model")
                .and_then(Value::as_str)
                .map(ToOwned::to_owned);
            to_value(gateway::apply_gateway_client_config(client_id, model))
        }
        "restore_gateway_client_config" => {
            let client_id = string_arg(args, "clientId")?;
            to_value(gateway::restore_gateway_client_config(client_id))
        }
        "switch_gateway_client_route" => {
            let client_id = string_arg(args, "clientId")?;
            let mode = string_arg(args, "mode")?;
            let model = args
                .get("model")
                .and_then(Value::as_str)
                .map(ToOwned::to_owned);
            let force_takeover =
                optional_bool_arg(args, &["forceTakeover", "force_takeover"]);
            to_value(gateway::switch_gateway_client_route(
                client_id,
                mode,
                model,
                force_takeover,
            ))
        }
        "sync_gateway_clients" => {
            let model = args
                .get("model")
                .and_then(Value::as_str)
                .map(ToOwned::to_owned);
            to_value(gateway::sync_gateway_clients(model))
        }
        "subagent_matrix_status" => to_value(gateway::subagent_matrix_status()),
        "generate_catalog" => {
            let restart_codex = optional_bool_arg(
                args,
                &["restartCodex", "restart_codex"],
            )
            .unwrap_or(false);
            to_value(crate::generate_catalog_coordinated(restart_codex))
        }
        "get_catalog_override_diagnostics" => to_value(catalog::catalog_override_diagnostics()),
        "list_models" => to_value(models::list_models()),
        "refresh_model_metadata" => to_value(models::refresh_model_metadata()),
        "list_model_metadata" => to_value(models::list_model_metadata()),
        "save_model_metadata_override" => {
            let model = serde_json::from_value(
                args
                    .get("model")
                    .cloned()
                    .ok_or_else(|| "model argument is required".to_string())?,
            )
            .map_err(|error| format!("invalid model argument: {error}"))?;
            to_value(models::save_model_metadata_override(model))
        }
        "save_official_multi_agent_version" => {
            let model_id = optional_string_arg(args, &["modelId", "model_id"])
                .ok_or_else(|| "modelId argument is required".to_string())?;
            let version = args
                .get("version")
                .and_then(Value::as_str)
                .map(str::to_string);
            let restart_codex = optional_bool_arg(
                args,
                &["restartCodex", "restart_codex"],
            )
            .unwrap_or(false);
            to_value(crate::save_official_multi_agent_version_coordinated(
                model_id,
                version,
                restart_codex,
            ))
        }
        "list_official_multi_agent_overrides" => {
            to_value(models::list_official_multi_agent_overrides())
        }
        "list_official_multi_agent_baselines" => {
            to_value(models::list_official_multi_agent_baselines())
        }
        "sync_history" => {
            let target_provider = args
                .get("targetProvider")
                .and_then(Value::as_str)
                .map(ToOwned::to_owned);
            to_value(history::sync_history(target_provider.as_deref()))
        }
        "reconcile_after_route_switch" => {
            let target_provider = args
                .get("targetProvider")
                .and_then(Value::as_str)
                .map(ToOwned::to_owned);
            to_value(history::reconcile_after_route_switch(
                target_provider.as_deref(),
            ))
        }
        "migrate_official_history_to_unified" => {
            to_value(history::migrate_official_history_to_unified())
        }
        "restore_official_history_from_unified" => {
            to_value(history::restore_official_history_from_unified())
        }
        "preflight_unified_history" => {
            let apply_repairs = optional_bool_arg(
                args,
                &[
                    "applyRepairs",
                    "apply_repairs",
                    "requestRestart",
                    "request_restart",
                ],
            )
            .unwrap_or(false);
            let target_unified =
                optional_bool_arg(args, &["targetUnified", "target_unified"]);
            to_value(history::preflight_unified_history(
                apply_repairs,
                target_unified,
            ))
        }
        "get_conversation_sync_status" => to_value(history::preflight_unified_history(false, None)),
        "sync_conversation_history" => {
            let target_provider = args.get("targetProvider").and_then(Value::as_str);
            to_value(history::preflight_unified_history(
                true,
                target_provider.map(|value| value != "openai"),
            ))
        }
        "diagnose_conversation_history" => {
            let full_scan =
                optional_bool_arg(args, &["fullScan", "full_scan"]).unwrap_or(true);
            to_value(history::diagnose_unified_history(full_scan))
        }
        "sync_catalog" => {
            let restart_codex = optional_bool_arg(
                args,
                &["restartCodex", "restart_codex"],
            )
            .unwrap_or(false);
            to_value(crate::sync_catalog_coordinated(restart_codex))
        }
        "set_autostart" => to_value(autostart::set_autostart(bool_arg(
            args,
            "enabled",
        )?)),
        "remove_autostart" => to_value(autostart::remove_autostart()),
        "get_autostart_status" => to_value(autostart::get_autostart_status()),
        "open_codex_app" => to_value(crate::open_codex_app()),
        "xai_auth_status" => to_value(xai_auth::xai_auth_status_blocking()),
        "xai_start_device_login" => to_value(xai_auth::xai_start_device_login_blocking()),
        "xai_poll_device_login" => {
            let device_json = optional_string_arg(args, &["deviceJson", "device_json"])
                .ok_or_else(|| "deviceJson argument is required".to_string())?;
            to_value(xai_auth::xai_poll_device_login_blocking(device_json))
        }
        "xai_logout" => to_value(xai_auth::xai_logout_blocking()),
        "xai_usage_snapshot" => to_value(xai_auth::xai_usage_snapshot_blocking()),
        "xai_open_verification_url" => {
            let url = optional_string_arg(args, &["url"])
                .ok_or_else(|| "url argument is required".to_string())?;
            to_value(xai_auth::xai_open_verification_url_blocking(url))
        }
        command => Err(format!("unknown CodexHub command: {command}")),
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
fn string_arg(args: &Value, name: &str) -> Result<String, String> {
    args.get(name)
        .and_then(Value::as_str)
        .map(ToOwned::to_owned)
        .ok_or_else(|| format!("{name} argument is required"))
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
