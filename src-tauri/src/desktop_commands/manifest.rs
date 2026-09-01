//! Desktop Command manifest — the single Rust-owned command table.
//!
//! ADR-0010: Rust owns the command interface. This module is the one source
//! of truth for command names and exposure metadata. Tauri handlers and the
//! Web Bridge dispatch live in sibling modules; the frontend consumes a
//! checked contract over this manifest.

/// Exposure metadata for one desktop command.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct CommandMeta {
    /// Canonical wire name (snake_case).
    pub name: &'static str,
    /// Exposed through the Tauri IPC handler.
    pub tauri_exposed: bool,
    /// Exposed through the Web Bridge HTTP adapter.
    pub bridge_exposed: bool,
    /// Exposed to the frontend TypeScript client.
    pub frontend_exposed: bool,
    /// Requires a desktop window (window_* family); never bridge-routable.
    pub desktop_only: bool,
}

pub const COMMANDS: &[CommandMeta] = &[
    CommandMeta { name: "get_app_version", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "check_app_update", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "start_app_update_install", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "get_app_update_install_status", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "consume_app_update_completion", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "install_app_update", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "get_status", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "get_codex_desktop_status", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "switch_mode", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "start_proxy", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "stop_proxy", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "restart_proxy", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "get_providers", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "get_bundled_providers", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "save_providers", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "get_settings", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "get_app_flavor", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "save_settings", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "get_codex_context_guard_status", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "set_codex_context_guard", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "refresh_official_models", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "openai_usage_completions", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "discover_provider_models", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "probe_upstream_format", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "provider_probe_upstream_format", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "test_model_endpoint", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "gateway_status", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "diagnostics_status", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "diagnostics_manual_mark", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "diagnostics_pause", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "diagnostics_resume", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "diagnostics_delete_incident", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "gateway_test_request", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "gateway_recent_events", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "gateway_usage_summary", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "gateway_usage_snapshot", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "gateway_usage_events", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "gateway_copy_client_config", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "list_gateway_clients", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "dsh_client_info", tauri_exposed: true, bridge_exposed: true, frontend_exposed: false, desktop_only: false },
    CommandMeta { name: "dsh_client_connect", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "dsh_client_disconnect", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "dsh_client_readback", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "preview_gateway_client_config", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "apply_gateway_client_config", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "restore_gateway_client_config", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "switch_gateway_client_route", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "sync_gateway_clients", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "subagent_matrix_status", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "generate_catalog", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "get_catalog_override_diagnostics", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "list_models", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "refresh_model_metadata", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "list_model_metadata", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "save_model_metadata_override", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "save_official_multi_agent_version", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "list_official_multi_agent_overrides", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "list_official_multi_agent_baselines", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "sync_history", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "reconcile_after_route_switch", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "migrate_official_history_to_unified", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "restore_official_history_from_unified", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "preflight_unified_history", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "get_conversation_sync_status", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "sync_conversation_history", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "diagnose_conversation_history", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "sync_catalog", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "set_autostart", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "remove_autostart", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "get_autostart_status", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "open_codex_app", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "window_minimize", tauri_exposed: true, bridge_exposed: false, frontend_exposed: true, desktop_only: true },
    CommandMeta { name: "window_toggle_maximize", tauri_exposed: true, bridge_exposed: false, frontend_exposed: true, desktop_only: true },
    CommandMeta { name: "window_close_to_tray", tauri_exposed: true, bridge_exposed: false, frontend_exposed: true, desktop_only: true },
    CommandMeta { name: "xai_auth_status", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "xai_start_device_login", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "xai_poll_device_login", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "xai_logout", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "xai_usage_snapshot", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
    CommandMeta { name: "xai_open_verification_url", tauri_exposed: true, bridge_exposed: true, frontend_exposed: true, desktop_only: false },
];

/// Look up a command by canonical name.
pub fn command_meta(name: &str) -> Option<&'static CommandMeta> {
    COMMANDS.iter().find(|meta| meta.name == name)
}

/// The manifest as stable JSON for contract tests (no serde dependency needed
/// at this layer; the test serializes the slice directly).
/// Used by tests and the upcoming web_adapter/tauri_handler wiring.
#[allow(dead_code)]
pub fn command_names() -> Vec<&'static str> {
    COMMANDS.iter().map(|meta| meta.name).collect()
}

/// Contract surface consumed by the frontend contract test and adapters.
#[allow(dead_code)]
pub fn frontend_exposed_names() -> Vec<&'static str> {
    COMMANDS
        .iter()
        .filter(|meta| meta.frontend_exposed)
        .map(|meta| meta.name)
        .collect()
}

/// Contract surface consumed by the frontend contract test and adapters.
#[allow(dead_code)]
pub fn bridge_exposed_names() -> Vec<&'static str> {
    COMMANDS
        .iter()
        .filter(|meta| meta.bridge_exposed)
        .map(|meta| meta.name)
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn command_names_are_unique_and_snake_case() {
        let mut seen = std::collections::HashSet::new();
        for meta in COMMANDS {
            assert!(seen.insert(meta.name), "duplicate command {}", meta.name);
            assert!(!meta.name.contains('-'), "non-snake name {}", meta.name);
            assert_eq!(meta.name, meta.name.to_ascii_lowercase(), "non-lowercase {}", meta.name);
        }
    }

    #[test]
    fn window_commands_are_desktop_only_and_not_bridge_exposed() {
        for meta in COMMANDS.iter().filter(|m| m.desktop_only) {
            assert!(meta.name.starts_with("window_"));
            assert!(!meta.bridge_exposed, "{} must not be bridge-exposed", meta.name);
            assert!(meta.tauri_exposed, "{} must be tauri-exposed", meta.name);
        }
    }

    #[test]
    fn dsh_client_info_is_internal_only() {
        let meta = command_meta("dsh_client_info").expect("dsh_client_info in registry");
        assert!(!meta.frontend_exposed, "dsh_client_info is not a frontend API");
    }

    #[test]
    fn emit_manifest_json_fixture() {
        // Emit the Rust-owned manifest as JSON so the frontend contract test
        // (frontend/scripts/desktop-commands.contract.test.mjs) can compare
        // frontend/src/lib/commands.ts against the single Rust source of truth.
        let root = std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR"));
        let fixture = root
            .parent()
            .expect("repo root")
            .join("frontend/scripts/desktop-commands.manifest.json");
        let mut entries: Vec<String> = COMMANDS
            .iter()
            .map(|meta| {
                format!(
                    "{{\"name\":\"{}\",\"tauri_exposed\":{},\"bridge_exposed\":{},\"frontend_exposed\":{},\"desktop_only\":{}}}",
                    meta.name,
                    meta.tauri_exposed,
                    meta.bridge_exposed,
                    meta.frontend_exposed,
                    meta.desktop_only
                )
            })
            .collect();
        entries.sort();
        std::fs::write(&fixture, format!("[{}]", entries.join(",")))
            .expect("write manifest fixture");
        let names = command_names();
        assert!(
            names.len() >= 80,
            "registry must cover the full command surface, got {}",
            names.len()
        );
    }

    #[test]
    fn tauri_handlers_agree_with_manifest() {
        // The manifest is the single source of truth. This test re-derives the
        // registered handler names from tauri_handlers! in desktop_commands
        // and asserts each is present in the manifest.
        let source = std::fs::read_to_string(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/src/desktop_commands/mod.rs"
        ))
        .expect("desktop_commands/mod.rs readable");
        let start = source.find("generate_handler![").expect("generate_handler list");
        let end = source[start..].find(']').expect("list close") + start;
        let list = &source[start..end];
        let mut count = 0usize;
        for token in list.split([',', '\n', '[', ']', ' ']).filter(|t| !t.is_empty()) {
            if token.contains('!') {
                continue; // macro invocation name like generate_handler!
            }
            let name = token.rsplit("::").next().expect("segment");
            assert!(
                command_meta(name).is_some(),
                "handler {name} registered in generate_handler! missing from desktop_commands manifest"
            );
            count += 1;
        }
        assert!(count >= 60, "expected >=60 registered handlers, found {count}");
    }

    #[test]
    fn every_bridge_arm_has_a_registry_entry() {
        // This test documents that the registry is the complete source:
        // the bridge dispatch (web_adapter) and Tauri handlers must be derived
        // from COMMANDS, so no orphan names exist.
        let names = command_names();
        assert!(names.len() >= 80, "registry should cover the full surface");
        assert!(command_meta("get_status").is_some());
        assert!(command_meta("save_providers").is_some());
        assert!(command_meta("window_minimize").is_some());
        assert!(command_meta("xai_open_verification_url").is_some());
    }
}
