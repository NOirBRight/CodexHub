//! Desktop Command seam (ADR-0010).
//!
//! Rust owns the command interface. manifest is the single command table;
//! Tauri handlers and the Web Bridge adapter are thin layers over it.

pub mod handlers;
pub mod manifest;
pub mod web_adapter;

pub use handlers::*;
pub(crate) use handlers::{
    generate_catalog_coordinated, refresh_official_models_coordinated,
    save_official_multi_agent_version_coordinated, sync_catalog_coordinated,
};
pub use manifest::command_meta;
pub use web_adapter::dispatch_web;

/// Static Tauri handler inventory (ADR-0010). main.rs installs this and does
/// not own a second command list.
#[macro_export]
macro_rules! tauri_handlers {
    () => {
        ::tauri::generate_handler![
            $crate::app_updates::get_app_version,
            $crate::app_updates::check_app_update,
            $crate::app_updates::start_app_update_install,
            $crate::app_updates::get_app_update_install_status,
            $crate::app_updates::consume_app_update_completion,
            $crate::app_updates::install_app_update,
            $crate::desktop_commands::get_status,
            $crate::desktop_commands::get_codex_desktop_status,
            $crate::desktop_commands::switch_mode,
            $crate::desktop_commands::start_proxy,
            $crate::desktop_commands::stop_proxy,
            $crate::desktop_commands::restart_proxy,
            $crate::desktop_commands::get_providers,
            $crate::desktop_commands::get_bundled_providers,
            $crate::desktop_commands::save_providers,
            $crate::desktop_commands::get_settings,
            $crate::desktop_commands::get_app_flavor,
            $crate::desktop_commands::save_settings,
            $crate::desktop_commands::get_codex_context_guard_status,
            $crate::desktop_commands::set_codex_context_guard,
            $crate::desktop_commands::refresh_official_models,
            $crate::desktop_commands::openai_usage_completions,
            $crate::desktop_commands::discover_provider_models,
            $crate::desktop_commands::probe_upstream_format,
            $crate::desktop_commands::provider_probe_upstream_format,
            $crate::desktop_commands::test_model_endpoint,
            $crate::desktop_commands::gateway_status,
            $crate::desktop_commands::diagnostics_status,
            $crate::desktop_commands::diagnostics_manual_mark,
            $crate::desktop_commands::diagnostics_pause,
            $crate::desktop_commands::diagnostics_resume,
            $crate::desktop_commands::diagnostics_delete_incident,
            $crate::desktop_commands::gateway_test_request,
            $crate::desktop_commands::gateway_recent_events,
            $crate::desktop_commands::gateway_usage_summary,
            $crate::desktop_commands::gateway_usage_snapshot,
            $crate::desktop_commands::gateway_usage_events,
            $crate::desktop_commands::gateway_copy_client_config,
            $crate::desktop_commands::list_gateway_clients,
            $crate::desktop_commands::dsh_client_info,
            $crate::desktop_commands::dsh_client_connect,
            $crate::desktop_commands::dsh_client_disconnect,
            $crate::desktop_commands::dsh_client_readback,
            $crate::desktop_commands::preview_gateway_client_config,
            $crate::desktop_commands::apply_gateway_client_config,
            $crate::desktop_commands::restore_gateway_client_config,
            $crate::desktop_commands::switch_gateway_client_route,
            $crate::desktop_commands::sync_gateway_clients,
            $crate::desktop_commands::subagent_matrix_status,
            $crate::desktop_commands::generate_catalog,
            $crate::desktop_commands::get_catalog_override_diagnostics,
            $crate::desktop_commands::list_models,
            $crate::desktop_commands::refresh_model_metadata,
            $crate::desktop_commands::list_model_metadata,
            $crate::desktop_commands::save_model_metadata_override,
            $crate::desktop_commands::save_official_multi_agent_version,
            $crate::desktop_commands::list_official_multi_agent_overrides,
            $crate::desktop_commands::list_official_multi_agent_baselines,
            $crate::desktop_commands::sync_history,
            $crate::desktop_commands::reconcile_after_route_switch,
            $crate::desktop_commands::migrate_official_history_to_unified,
            $crate::desktop_commands::restore_official_history_from_unified,
            $crate::desktop_commands::preflight_unified_history,
            $crate::desktop_commands::get_conversation_sync_status,
            $crate::desktop_commands::sync_conversation_history,
            $crate::desktop_commands::diagnose_conversation_history,
            $crate::desktop_commands::sync_catalog,
            $crate::desktop_commands::set_autostart,
            $crate::desktop_commands::remove_autostart,
            $crate::desktop_commands::get_autostart_status,
            $crate::desktop_commands::open_codex_app,
            $crate::desktop_commands::window_minimize,
            $crate::desktop_commands::window_toggle_maximize,
            $crate::desktop_commands::window_close_to_tray,
            $crate::xai_auth::xai_auth_status,
            $crate::xai_auth::xai_start_device_login,
            $crate::xai_auth::xai_poll_device_login,
            $crate::xai_auth::xai_logout,
            $crate::xai_auth::xai_usage_snapshot,
            $crate::xai_auth::xai_open_verification_url
        ]
    };
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn first_handler_batch_is_in_the_manifest() {
        for name in [
            "get_codex_desktop_status",
            "start_proxy",
            "stop_proxy",
            "restart_proxy",
            "get_providers",
            "get_bundled_providers",
            "save_providers",
            "get_settings",
            "get_app_flavor",
            "save_settings",
            "get_codex_context_guard_status",
            "get_catalog_override_diagnostics",
            "list_models",
            "refresh_model_metadata",
            "list_model_metadata",
            "save_model_metadata_override",
            "get_status",
            "gateway_status",
            "diagnostics_status",
            "preview_gateway_client_config",
            "dsh_client_info",
            "subagent_matrix_status",
            "get_autostart_status",
            "sync_history",
            "reconcile_after_route_switch",
            "get_conversation_sync_status",
            "diagnose_conversation_history",
            "refresh_official_models",
            "generate_catalog",
            "sync_catalog",
            "save_official_multi_agent_version",
            "switch_mode",
            "set_codex_context_guard",
            "open_codex_app",
            "window_minimize",
            "window_toggle_maximize",
            "window_close_to_tray",
        ] {
            let meta = command_meta(name).unwrap_or_else(|| panic!("{name} missing from manifest"));
            assert!(meta.tauri_exposed, "{name} must stay Tauri-exposed");
        }
    }

    #[test]
    fn get_app_flavor_handler_returns_current_info() {
        let flavor = get_app_flavor();
        assert!(!flavor.product_name.is_empty());
    }

    #[test]
    fn linux_window_restore_defaults_to_windowed() {
        let state = super::handlers::LinuxWindowRestore::default();
        assert!(!state.maximized);
        assert!(state.size.is_none());
        assert!(state.position.is_none());
    }
}
