//! Desktop Command seam (ADR-0010).
//!
//! The registry below is the only command-name inventory. It expands to the
//! typed command enum, manifest helpers, and the static Tauri handler list.
//! Transport adapters consume the enum instead of maintaining another list.

pub mod handlers;
pub mod manifest;
pub mod web_adapter;

pub use handlers::*;
pub(crate) use handlers::{
    generate_catalog_coordinated, refresh_official_models_coordinated,
    refresh_official_models_published_coordinated,
    save_official_multi_agent_version_coordinated, sync_catalog_coordinated,
};
pub use web_adapter::dispatch_web;

/// Metadata for one command row. The command row also carries the Tauri
/// handler path, so registration cannot drift from the manifest.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct CommandMeta {
    pub command: Command,
    pub name: &'static str,
    pub tauri_exposed: bool,
    pub bridge_exposed: bool,
    pub frontend_exposed: bool,
    pub desktop_only: bool,
    pub argument_aliases: &'static [(&'static str, &'static str)],
}

const NO_ALIASES: &[(&str, &str)] = &[];
const ALIASES_SWITCH_MODE: &[(&str, &str)] = &[
    ("autoSync", "auto_sync"),
    ("forceTakeover", "force_takeover"),
    ("restartCodex", "restart_codex"),
];
const ALIASES_RESTART: &[(&str, &str)] = &[("restartCodex", "restart_codex")];
const ALIASES_USAGE: &[(&str, &str)] = &[
    ("startTime", "start_time"),
    ("endTime", "end_time"),
    ("forceRefresh", "force_refresh"),
];
const ALIASES_PROVIDER_ID: &[(&str, &str)] = &[("providerId", "provider_id")];
const ALIASES_DISCOVER: &[(&str, &str)] = &[
    ("baseUrl", "base_url"),
    ("apiKey", "api_key"),
    ("providerId", "provider_id"),
];
const ALIASES_TIME_WINDOW: &[(&str, &str)] = &[
    ("sinceTs", "since_ts"),
    ("startTs", "start_ts"),
    ("endTs", "end_ts"),
];
const ALIASES_CLIENT_KIND: &[(&str, &str)] = &[("clientKind", "client_kind")];
const ALIASES_BASE_URL_API_KEY: &[(&str, &str)] = &[("baseUrl", "base_url"), ("apiKey", "api_key")];
const ALIASES_TEST_MODEL_ENDPOINT: &[(&str, &str)] = &[
    ("baseUrl", "base_url"),
    ("apiKey", "api_key"),
    ("upstreamFormat", "upstream_format"),
];
const ALIASES_CLIENT_ID: &[(&str, &str)] = &[("clientId", "client_id")];
const ALIASES_INCLUDE_VERSIONS: &[(&str, &str)] = &[("includeVersions", "include_versions")];
const ALIASES_MODEL_ID_RESTART: &[(&str, &str)] =
    &[("modelId", "model_id"), ("restartCodex", "restart_codex")];
const ALIASES_TARGET_PROVIDER: &[(&str, &str)] = &[("targetProvider", "target_provider")];
const ALIASES_PREFLIGHT: &[(&str, &str)] = &[
    ("applyRepairs", "apply_repairs"),
    ("requestRestart", "request_restart"),
    ("targetUnified", "target_unified"),
];
const ALIASES_FULL_SCAN: &[(&str, &str)] = &[("fullScan", "full_scan")];
const ALIASES_DEVICE_JSON: &[(&str, &str)] = &[("deviceJson", "device_json")];
const ALIASES_SWITCH_ROUTE: &[(&str, &str)] = &[
    ("clientId", "client_id"),
    ("forceTakeover", "force_takeover"),
];

macro_rules! desktop_command_registry {
    ($callback:ident) => {
        $callback! {
            GetAppVersion => "get_app_version" => $crate::app_updates::get_app_version, true, true, true, false, NO_ALIASES;
            CheckAppUpdate => "check_app_update" => $crate::app_updates::check_app_update, true, true, true, false, NO_ALIASES;
            StartAppUpdateInstall => "start_app_update_install" => $crate::app_updates::start_app_update_install, true, true, true, false, NO_ALIASES;
            GetAppUpdateInstallStatus => "get_app_update_install_status" => $crate::app_updates::get_app_update_install_status, true, true, true, false, NO_ALIASES;
            ConsumeAppUpdateCompletion => "consume_app_update_completion" => $crate::app_updates::consume_app_update_completion, true, true, true, false, NO_ALIASES;
            InstallAppUpdate => "install_app_update" => $crate::app_updates::install_app_update, true, true, true, false, NO_ALIASES;
            GetStatus => "get_status" => $crate::desktop_commands::get_status, true, true, true, false, NO_ALIASES;
            GetCodexDesktopStatus => "get_codex_desktop_status" => $crate::desktop_commands::get_codex_desktop_status, true, true, true, false, NO_ALIASES;
            SwitchMode => "switch_mode" => $crate::desktop_commands::switch_mode, true, true, true, false, ALIASES_SWITCH_MODE;
            StartProxy => "start_proxy" => $crate::desktop_commands::start_proxy, true, true, true, false, NO_ALIASES;
            StopProxy => "stop_proxy" => $crate::desktop_commands::stop_proxy, true, true, true, false, NO_ALIASES;
            RestartProxy => "restart_proxy" => $crate::desktop_commands::restart_proxy, true, true, true, false, NO_ALIASES;
            GetProviders => "get_providers" => $crate::desktop_commands::get_providers, true, true, true, false, NO_ALIASES;
            GetBundledProviders => "get_bundled_providers" => $crate::desktop_commands::get_bundled_providers, true, true, true, false, NO_ALIASES;
            SaveProviders => "save_providers" => $crate::desktop_commands::save_providers, true, true, true, false, NO_ALIASES;
            GetSettings => "get_settings" => $crate::desktop_commands::get_settings, true, true, true, false, NO_ALIASES;
            GetAppFlavor => "get_app_flavor" => $crate::desktop_commands::get_app_flavor, true, true, true, false, NO_ALIASES;
            SaveSettings => "save_settings" => $crate::desktop_commands::save_settings, true, true, true, false, NO_ALIASES;
            GetCodexContextGuardStatus => "get_codex_context_guard_status" => $crate::desktop_commands::get_codex_context_guard_status, true, true, true, false, NO_ALIASES;
            SetCodexContextGuard => "set_codex_context_guard" => $crate::desktop_commands::set_codex_context_guard, true, true, true, false, ALIASES_RESTART;
            RefreshOfficialModels => "refresh_official_models" => $crate::desktop_commands::refresh_official_models, true, true, true, false, ALIASES_RESTART;
            OpenaiUsageCompletions => "openai_usage_completions" => $crate::desktop_commands::openai_usage_completions, true, true, true, false, ALIASES_USAGE;
            DiscoverProviderModels => "discover_provider_models" => $crate::desktop_commands::discover_provider_models, true, true, true, false, ALIASES_DISCOVER;
            ProbeUpstreamFormat => "probe_upstream_format" => $crate::desktop_commands::probe_upstream_format, true, true, true, false, ALIASES_BASE_URL_API_KEY;
            ProviderProbeUpstreamFormat => "provider_probe_upstream_format" => $crate::desktop_commands::provider_probe_upstream_format, true, true, true, false, ALIASES_PROVIDER_ID;
            TestModelEndpoint => "test_model_endpoint" => $crate::desktop_commands::test_model_endpoint, true, true, true, false, ALIASES_TEST_MODEL_ENDPOINT;
            GatewayStatus => "gateway_status" => $crate::desktop_commands::gateway_status, true, true, true, false, NO_ALIASES;
            DiagnosticsStatus => "diagnostics_status" => $crate::desktop_commands::diagnostics_status, true, true, true, false, NO_ALIASES;
            DiagnosticsManualMark => "diagnostics_manual_mark" => $crate::desktop_commands::diagnostics_manual_mark, true, true, true, false, NO_ALIASES;
            DiagnosticsPause => "diagnostics_pause" => $crate::desktop_commands::diagnostics_pause, true, true, true, false, NO_ALIASES;
            DiagnosticsResume => "diagnostics_resume" => $crate::desktop_commands::diagnostics_resume, true, true, true, false, NO_ALIASES;
            DiagnosticsDeleteIncident => "diagnostics_delete_incident" => $crate::desktop_commands::diagnostics_delete_incident, true, true, true, false, NO_ALIASES;
            GatewayTestRequest => "gateway_test_request" => $crate::desktop_commands::gateway_test_request, true, true, true, false, NO_ALIASES;
            GatewayRecentEvents => "gateway_recent_events" => $crate::desktop_commands::gateway_recent_events, true, true, true, false, ALIASES_TIME_WINDOW;
            GatewayUsageSummary => "gateway_usage_summary" => $crate::desktop_commands::gateway_usage_summary, true, true, true, false, ALIASES_TIME_WINDOW;
            GatewayUsageSnapshot => "gateway_usage_snapshot" => $crate::desktop_commands::gateway_usage_snapshot, true, true, true, false, ALIASES_TIME_WINDOW;
            GatewayUsageEvents => "gateway_usage_events" => $crate::desktop_commands::gateway_usage_events, true, true, true, false, ALIASES_TIME_WINDOW;
            GatewayCopyClientConfig => "gateway_copy_client_config" => $crate::desktop_commands::gateway_copy_client_config, true, true, true, false, ALIASES_CLIENT_KIND;
            ListGatewayClients => "list_gateway_clients" => $crate::desktop_commands::list_gateway_clients, true, true, true, false, ALIASES_INCLUDE_VERSIONS;
            DshClientInfo => "dsh_client_info" => $crate::desktop_commands::dsh_client_info, true, true, false, false, NO_ALIASES;
            DshClientConnect => "dsh_client_connect" => $crate::desktop_commands::dsh_client_connect, true, true, true, false, NO_ALIASES;
            DshClientDisconnect => "dsh_client_disconnect" => $crate::desktop_commands::dsh_client_disconnect, true, true, true, false, NO_ALIASES;
            DshClientReadback => "dsh_client_readback" => $crate::desktop_commands::dsh_client_readback, true, true, true, false, NO_ALIASES;
            PreviewGatewayClientConfig => "preview_gateway_client_config" => $crate::desktop_commands::preview_gateway_client_config, true, true, true, false, ALIASES_CLIENT_ID;
            ApplyGatewayClientConfig => "apply_gateway_client_config" => $crate::desktop_commands::apply_gateway_client_config, true, true, true, false, ALIASES_CLIENT_ID;
            RestoreGatewayClientConfig => "restore_gateway_client_config" => $crate::desktop_commands::restore_gateway_client_config, true, true, true, false, ALIASES_CLIENT_ID;
            SwitchGatewayClientRoute => "switch_gateway_client_route" => $crate::desktop_commands::switch_gateway_client_route, true, true, true, false, ALIASES_SWITCH_ROUTE;
            SyncGatewayClients => "sync_gateway_clients" => $crate::desktop_commands::sync_gateway_clients, true, true, true, false, NO_ALIASES;
            SubagentMatrixStatus => "subagent_matrix_status" => $crate::desktop_commands::subagent_matrix_status, true, true, true, false, NO_ALIASES;
            GenerateCatalog => "generate_catalog" => $crate::desktop_commands::generate_catalog, true, true, true, false, ALIASES_RESTART;
            GetCatalogOverrideDiagnostics => "get_catalog_override_diagnostics" => $crate::desktop_commands::get_catalog_override_diagnostics, true, true, true, false, NO_ALIASES;
            ListModels => "list_models" => $crate::desktop_commands::list_models, true, true, true, false, NO_ALIASES;
            RefreshModelMetadata => "refresh_model_metadata" => $crate::desktop_commands::refresh_model_metadata, true, true, true, false, NO_ALIASES;
            ListModelMetadata => "list_model_metadata" => $crate::desktop_commands::list_model_metadata, true, true, true, false, NO_ALIASES;
            SaveModelMetadataOverride => "save_model_metadata_override" => $crate::desktop_commands::save_model_metadata_override, true, true, true, false, NO_ALIASES;
            SaveOfficialMultiAgentVersion => "save_official_multi_agent_version" => $crate::desktop_commands::save_official_multi_agent_version, true, true, true, false, ALIASES_MODEL_ID_RESTART;
            ListOfficialMultiAgentOverrides => "list_official_multi_agent_overrides" => $crate::desktop_commands::list_official_multi_agent_overrides, true, true, true, false, NO_ALIASES;
            ListOfficialMultiAgentBaselines => "list_official_multi_agent_baselines" => $crate::desktop_commands::list_official_multi_agent_baselines, true, true, true, false, NO_ALIASES;
            SyncHistory => "sync_history" => $crate::desktop_commands::sync_history, true, true, true, false, ALIASES_TARGET_PROVIDER;
            ReconcileAfterRouteSwitch => "reconcile_after_route_switch" => $crate::desktop_commands::reconcile_after_route_switch, true, true, true, false, ALIASES_TARGET_PROVIDER;
            MigrateOfficialHistoryToUnified => "migrate_official_history_to_unified" => $crate::desktop_commands::migrate_official_history_to_unified, true, true, true, false, NO_ALIASES;
            RestoreOfficialHistoryFromUnified => "restore_official_history_from_unified" => $crate::desktop_commands::restore_official_history_from_unified, true, true, true, false, NO_ALIASES;
            PreflightUnifiedHistory => "preflight_unified_history" => $crate::desktop_commands::preflight_unified_history, true, true, true, false, ALIASES_PREFLIGHT;
            GetConversationSyncStatus => "get_conversation_sync_status" => $crate::desktop_commands::get_conversation_sync_status, true, true, true, false, NO_ALIASES;
            SyncConversationHistory => "sync_conversation_history" => $crate::desktop_commands::sync_conversation_history, true, true, true, false, ALIASES_TARGET_PROVIDER;
            DiagnoseConversationHistory => "diagnose_conversation_history" => $crate::desktop_commands::diagnose_conversation_history, true, true, true, false, ALIASES_FULL_SCAN;
            SyncCatalog => "sync_catalog" => $crate::desktop_commands::sync_catalog, true, true, true, false, ALIASES_RESTART;
            SetAutostart => "set_autostart" => $crate::desktop_commands::set_autostart, true, true, true, false, NO_ALIASES;
            RemoveAutostart => "remove_autostart" => $crate::desktop_commands::remove_autostart, true, true, true, false, NO_ALIASES;
            GetAutostartStatus => "get_autostart_status" => $crate::desktop_commands::get_autostart_status, true, true, true, false, NO_ALIASES;
            OpenCodexApp => "open_codex_app" => $crate::desktop_commands::open_codex_app, true, true, true, false, NO_ALIASES;
            WindowMinimize => "window_minimize" => $crate::desktop_commands::window_minimize, true, false, true, true, NO_ALIASES;
            WindowToggleMaximize => "window_toggle_maximize" => $crate::desktop_commands::window_toggle_maximize, true, false, true, true, NO_ALIASES;
            WindowCloseToTray => "window_close_to_tray" => $crate::desktop_commands::window_close_to_tray, true, false, true, true, NO_ALIASES;
            XaiAuthStatus => "xai_auth_status" => $crate::xai_auth::xai_auth_status, true, true, true, false, NO_ALIASES;
            XaiStartDeviceLogin => "xai_start_device_login" => $crate::xai_auth::xai_start_device_login, true, true, true, false, NO_ALIASES;
            XaiPollDeviceLogin => "xai_poll_device_login" => $crate::xai_auth::xai_poll_device_login, true, true, true, false, ALIASES_DEVICE_JSON;
            XaiLogout => "xai_logout" => $crate::xai_auth::xai_logout, true, true, true, false, NO_ALIASES;
            XaiUsageSnapshot => "xai_usage_snapshot" => $crate::xai_auth::xai_usage_snapshot, true, true, true, false, NO_ALIASES;
            XaiOpenVerificationUrl => "xai_open_verification_url" => $crate::xai_auth::xai_open_verification_url, true, true, true, false, NO_ALIASES;
        }
    };
}

macro_rules! define_registry {
    ($($variant:ident => $name:literal => $handler:path, $tauri:literal, $bridge:literal, $frontend:literal, $desktop:literal, $aliases:expr;)*) => {
        #[derive(Debug, Clone, Copy, PartialEq, Eq)]
        pub enum Command {
            $($variant,)*
        }

        pub const COMMANDS: &[CommandMeta] = &[
            $(CommandMeta {
                command: Command::$variant,
                name: $name,
                tauri_exposed: $tauri,
                bridge_exposed: $bridge,
                frontend_exposed: $frontend,
                desktop_only: $desktop,
                argument_aliases: $aliases,
            },)*
        ];

        pub fn parse_command(name: &str) -> Option<Command> {
            match name {
                $($name => Some(Command::$variant),)*
                _ => None,
            }
        }

        pub fn command_meta(name: &str) -> Option<&'static CommandMeta> {
            COMMANDS.iter().find(|meta| meta.name == name)
        }

        #[allow(dead_code)]
        pub fn command_names() -> Vec<&'static str> {
            COMMANDS.iter().map(|meta| meta.name).collect()
        }

        #[allow(dead_code)]
        pub fn frontend_exposed_names() -> Vec<&'static str> {
            COMMANDS.iter().filter(|meta| meta.frontend_exposed).map(|meta| meta.name).collect()
        }

        #[allow(dead_code)]
        pub fn bridge_exposed_names() -> Vec<&'static str> {
            COMMANDS.iter().filter(|meta| meta.bridge_exposed).map(|meta| meta.name).collect()
        }

        #[allow(dead_code)]
        pub fn command_manifest_json() -> String {
            let entries: Vec<serde_json::Value> = COMMANDS.iter().map(|meta| {
                serde_json::json!({
                    "name": meta.name,
                    "tauri_exposed": meta.tauri_exposed,
                    "bridge_exposed": meta.bridge_exposed,
                    "frontend_exposed": meta.frontend_exposed,
                    "desktop_only": meta.desktop_only,
                    "argument_aliases": meta.argument_aliases,
                })
            }).collect();
            serde_json::to_string(&entries).expect("desktop command manifest is serializable")
        }
    };
}

macro_rules! define_tauri_handlers {
    ($($variant:ident => $name:literal => $handler:path, $tauri:literal, $bridge:literal, $frontend:literal, $desktop:literal, $aliases:expr;)*) => {
        /// Static Tauri handler inventory generated from the command registry.
        #[macro_export]
        macro_rules! tauri_handlers {
            () => {
                ::tauri::generate_handler![$($handler),*]
            };
        }
    };
}

desktop_command_registry!(define_registry);
desktop_command_registry!(define_tauri_handlers);

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn registry_is_unique_and_typed() {
        let mut seen = std::collections::HashSet::new();
        for meta in COMMANDS {
            assert!(seen.insert(meta.name), "duplicate command {}", meta.name);
            assert_eq!(parse_command(meta.name), Some(meta.command));
        }
        assert!(COMMANDS.len() >= 80);
    }

    #[test]
    fn window_commands_are_desktop_only_and_not_bridge_exposed() {
        for meta in COMMANDS.iter().filter(|meta| meta.desktop_only) {
            assert!(meta.name.starts_with("window_"));
            assert!(!meta.bridge_exposed);
            assert!(meta.tauri_exposed);
        }
    }
}

#[cfg(test)]
mod registry_manifest_tests {
    use super::*;

    #[test]
    fn emit_manifest_json_fixture() {
        let output = std::env::var_os("CODEXHUB_COMMAND_MANIFEST_OUT")
            .map(std::path::PathBuf::from)
            .unwrap_or_else(|| {
                std::env::temp_dir().join(format!(
                    "codexhub-command-manifest-{}.json",
                    std::process::id()
                ))
            });
        std::fs::write(&output, command_manifest_json()).expect("write command manifest output");
        println!("command manifest: {}", output.display());
    }
}
