use crate::{app_flavor::RoutingOwner, config, runtime_paths, safe_file, Provider, Settings};
use reqwest::blocking::Client;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::collections::HashSet;
use std::fs;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::sync::{Mutex, OnceLock};
use std::thread;
use std::time::{Duration, Instant};

mod backup;
mod clients;
mod inject;
mod managed_clients;
mod isolated;
mod providers;
mod readback;

pub(in crate::gateway) use backup::*;
pub(in crate::gateway) use inject::*;
#[allow(unused_imports)]
pub use isolated::{
    apply_gateway_client_config_isolated, apply_gateway_client_config_isolated_with_provenance,
    isolated_client_apply_targets, isolated_client_preview, isolated_managed_client_ids,
    readback_gateway_client_config_isolated, route_protocol_for_selection,
    validate_existing_isolated_root, validate_isolated_root, IsolatedClientApplyInput,
    IsolatedClientApplyResult, IsolatedClientApplyTargets, IsolatedClientPreview,
    IsolatedClientReadback, IsolatedClientRoot,
};
mod telemetry;

pub use clients::dsh::{
    detect_dsh_client, dsh_client_connect, dsh_client_disconnect, dsh_client_readback,
    DshClientInfo,
};
pub use providers::provider_probe_upstream_format;
pub use readback::verify_apply_readback;

use clients::codex::read_codex_auth_status;
use clients::omp::{
    detect_omp_config_paths, detect_omp_route_details,
};
#[cfg(test)]
use clients::omp::{
    apply_omp_config_with_paths, omp_config_text, omp_models_yml_text, omp_route_mode,
    plan_omp_apply, publish_omp_apply, restore_omp_config_with_paths,
    OmpConfigPaths,
};
use clients::opencode::{
    detect_opencode_config_path, detect_opencode_executable_path,
    detect_opencode_version, is_opencode_codexhub_config,
};
#[cfg(test)]
use clients::opencode::{
    apply_opencode_config_with_paths, detect_opencode_executable_path_in_home, opencode_config_text,
    plan_opencode_apply, OpenCodeApplyDecision,
    opencode_ownership_bounded_cleanup, opencode_reasoning_variants, restore_latest_backup,
    restore_opencode_config_with_backup_roots,
};
#[cfg(all(test, target_os = "linux"))]
use clients::opencode::opencode_system_executable_candidates;
use clients::pi::{
    detect_pi_config_paths, detect_pi_route_details,
};
#[cfg(test)]
use clients::pi::{
    apply_pi_config_with_paths, pi_models_text, pi_ownership_bounded_cleanup, pi_settings_text,
    plan_pi_apply, restore_pi_config_with_paths,
};
use clients::zcode::{
    detect_zcode_config_targets, detect_zcode_executable_path,
    detect_zcode_route_details, detect_zcode_store_path, zcode_latest_version,
    zcode_route_mode_with_expected,
};
#[cfg(test)]
use clients::zcode::{
    apply_zcode_config_with_targets, plan_zcode_apply, restore_zcode_config_with_targets,
    zcode_catalog_text,
    zcode_route_mode, zcode_targets_from_writable, zcode_v2_cache_text, zcode_v2_config_text,
    zcode_v2_root_from_catalog_path, zcode_v2_root_from_settings_path, ZcodeConfigTargets,
};

#[cfg(test)]
pub(crate) use telemetry::{
    backfill_event_log_to_sqlite_path, classify_event, gateway_usage_snapshot_for_paths,
    ingest_telemetry_once_for_paths, initialize_telemetry_db, lookup_usage_pricing,
    read_usage_events_from_sqlite_path, read_usage_events_from_sqlite_path_with_window,
    read_usage_events_from_text, read_usage_summary_from_sqlite_path_with_pricing,
    read_usage_summary_from_sqlite_path_with_pricing_and_window, read_usage_summary_from_text,
    read_usage_summary_from_text_with_pricing, reset_telemetry_sqlite_ready_calls,
    sanitize_event, telemetry_sqlite_ready_calls, usage_pricing_by_model, UsagePricing,
    UsageTimeWindow,
};

const HEALTH_TIMEOUT: Duration = Duration::from_millis(900);
const VERSION_PROBE_TIMEOUT: Duration = Duration::from_secs(2);

static GATEWAY_CLIENT_CONFIG_WRITE_LOCK: OnceLock<Mutex<()>> = OnceLock::new();
static GATEWAY_CLIENT_SYNC_STATE_LOCK: OnceLock<Mutex<()>> = OnceLock::new();

#[allow(dead_code)]
const SUBAGENT_FEATURES: &[&str] = &[
    "third-party-tool-search-call-shim",
    "third-party-explicit-codex-native-tools",
    "third-party-spawn-hidden-while-agent-open",
    "third-party-multi-agent-wait-close-argument-shim",
    "third-party-single-loop-completion-gate",
];

#[derive(Debug, Clone, Serialize)]
pub struct GatewayStatus {
    pub proxy_running: bool,
    pub host: String,
    pub port: u16,
    pub build: Option<String>,
    pub features: Vec<String>,
    pub has_chat_completions_gateway: bool,
    pub codex_auth: CodexAuthStatus,
    pub endpoints: GatewayEndpoints,
    pub official_models: Vec<GatewayModel>,
    pub diagnostics: Vec<GatewayDiagnostic>,
}

#[derive(Debug, Clone, Serialize)]
pub struct CodexAuthStatus {
    pub auth_file_present: bool,
    pub logged_in: bool,
    pub auth_mode: Option<String>,
    pub account_id_present: bool,
    pub access_token_present: bool,
    pub refresh_token_present: bool,
    pub token_refresh_status: String,
    pub last_refresh: Option<String>,
    pub issue: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct GatewayEndpoints {
    pub base_url: String,
    pub models: String,
    pub responses: String,
    pub chat_completions: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct GatewayModel {
    pub id: String,
    pub display_name: String,
    pub source: String,
    pub source_kind: String,
    pub supports_responses: bool,
    pub supports_chat_completions: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub context_window: Option<u32>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub input_modalities: Option<Vec<String>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub supported_reasoning_levels: Option<Vec<String>>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub default_reasoning_level: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct GatewayUsageSummary {
    pub requests: u64,
    pub successful_requests: u64,
    pub missing_usage_requests: u64,
    pub total_tokens: Option<u64>,
    pub input_tokens: Option<u64>,
    pub output_tokens: Option<u64>,
    pub cached_input_tokens: Option<u64>,
    pub cache_hit_rate: Option<f64>,
    pub estimated_cost_usd: Option<f64>,
    pub cost_label: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct GatewayUsageEvent {
    pub ts: Option<String>,
    pub request_id: Option<String>,
    pub model: Option<String>,
    pub upstream: Option<String>,
    pub client_id: Option<String>,
    pub client_inference_source: Option<String>,
    pub reports_cached_input_tokens: Option<bool>,
    pub status: Option<i64>,
    pub duration_ms: Option<i64>,
    pub usage_source: String,
    pub usage_missing_reason: Option<String>,
    pub input_tokens: Option<u64>,
    pub output_tokens: Option<u64>,
    pub total_tokens: Option<u64>,
    pub cached_input_tokens: Option<u64>,
    pub reasoning_tokens: Option<u64>,
}

#[derive(Debug, Clone, Serialize)]
pub struct TelemetryStatus {
    pub event_log_size: u64,
    pub indexed_offset: u64,
    pub lag_bytes: u64,
    pub backfill_pending: bool,
    pub last_indexed_at: Option<String>,
    pub last_error: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct GatewayUsageSnapshot {
    pub summary: GatewayUsageSummary,
    pub events: Vec<GatewayUsageEvent>,
    pub telemetry_status: TelemetryStatus,
}

#[derive(Debug, Clone, Serialize)]
pub struct GatewayDiagnostic {
    pub level: String,
    pub category: String,
    pub message: String,
}

#[derive(Debug, Clone, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum GatewayTestKind {
    Health,
    Models,
    ChatCompletions,
    ChatCompletionsStream,
    ResponsesStream,
}

#[derive(Debug, Clone, Serialize)]
pub struct GatewayTestResult {
    pub ok: bool,
    pub kind: String,
    pub endpoint: String,
    pub method: String,
    pub model: Option<String>,
    pub status: Option<u16>,
    pub latency_ms: u128,
    pub first_token_ms: Option<u128>,
    pub sanitized_body: Option<String>,
    pub error: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct GatewayClientConfig {
    pub base_url: String,
    pub api_key: String,
    pub model: String,
    pub json: String,
    pub curl_test: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct GatewayClientInfo {
    pub id: String,
    pub name: String,
    pub kind: String,
    pub installed: bool,
    pub auto_apply_supported: bool,
    pub config_path: Option<PathBuf>,
    pub route_owner: RoutingOwner,
    pub route_endpoint: Option<String>,
    pub managed_by_current_app: bool,
    pub route_mode: String,
    pub status: String,
    pub versions_checked: bool,
    pub current_version: Option<String>,
    pub latest_version: Option<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct GatewayClientConfigPreview {
    pub client_id: String,
    pub can_apply: bool,
    pub strategy: String,
    pub config_path: Option<PathBuf>,
    pub current_redacted: Option<String>,
    pub next_redacted: String,
    pub backup_required: bool,
    pub message: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct GatewayClientApplyResult {
    pub client_id: String,
    pub applied: bool,
    pub config_path: Option<PathBuf>,
    pub backup_path: Option<PathBuf>,
    pub message: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct GatewayClientSyncItem {
    pub client_id: String,
    pub name: String,
    pub status: String,
    pub applied: bool,
    pub skipped: bool,
    pub message: String,
    pub config_path: Option<PathBuf>,
    pub backup_path: Option<PathBuf>,
}

#[derive(Debug, Clone, Serialize)]
pub struct GatewayClientSyncSummary {
    pub applied: u32,
    pub skipped: u32,
    pub failed: u32,
    pub results: Vec<GatewayClientSyncItem>,
    pub message: String,
}

#[derive(Debug, Default, Deserialize, Serialize)]
struct GatewayClientSyncState {
    pending_client_ids: Vec<String>,
}

#[derive(Debug, Clone, Serialize)]
pub struct GatewayEvent {
    pub ts: Option<String>,
    pub event: Option<String>,
    pub request_id: Option<String>,
    pub client_request_id: Option<String>,
    pub query_id: Option<String>,
    pub session_id: Option<String>,
    pub client_id: Option<String>,
    pub path: Option<String>,
    pub method: Option<String>,
    pub model: Option<String>,
    pub upstream: Option<String>,
    pub provider_id: Option<String>,
    pub upstream_format: Option<String>,
    pub inbound_format: Option<String>,
    pub request_kind: Option<String>,
    pub route_reason: Option<String>,
    pub route_mode: Option<String>,
    pub failure_class: Option<String>,
    pub retryable: Option<bool>,
    pub attempt: Option<i64>,
    pub max_attempts: Option<i64>,
    pub delay_ms: Option<i64>,
    pub status: Option<i64>,
    pub duration_ms: Option<i64>,
    pub error: Option<String>,
    pub detail: Option<String>,
    pub category: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct SubagentMatrixStatus {
    pub readiness: Vec<SubagentReadiness>,
    pub rows: Vec<SubagentMatrixRow>,
    pub recent_events: Vec<GatewayEvent>,
    pub message: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct SubagentReadiness {
    pub step: String,
    pub ready: bool,
    pub feature: String,
}

#[derive(Debug, Clone, Serialize)]
pub struct SubagentMatrixRow {
    pub model: String,
    pub provider: String,
    pub thread_id: Option<String>,
    pub child_agent_id: Option<String>,
    pub wait_timed_out: Option<bool>,
    pub close_succeeded: Option<bool>,
    pub child_output_ok: Option<bool>,
    pub status: String,
    pub detail: String,
}

#[derive(Debug, Deserialize)]
struct HealthResponse {
    ok: Option<bool>,
    build: Option<String>,
    features: Option<Vec<String>>,
}

pub fn gateway_status() -> Result<GatewayStatus, String> {
    let settings = config::get_settings()?;
    let endpoints = endpoints(settings.proxy_port);
    let health = read_health(settings.proxy_port, HEALTH_TIMEOUT)?;
    let features = health
        .as_ref()
        .and_then(|value| value.features.clone())
        .unwrap_or_default();
    let proxy_running = health
        .as_ref()
        .map(|value| value.ok.unwrap_or(false))
        .unwrap_or(false);
    let has_chat_completions_gateway = features
        .iter()
        .any(|feature| feature == "chat-completions-gateway");
    let codex_auth = read_codex_auth_status();
    let diagnostics = gateway_diagnostics(proxy_running, has_chat_completions_gateway, &codex_auth);
    let providers = config::get_providers().unwrap_or_default();

    Ok(GatewayStatus {
        proxy_running,
        host: settings.gateway_bind_address.clone(),
        port: settings.proxy_port,
        build: health.and_then(|value| value.build),
        features,
        has_chat_completions_gateway,
        codex_auth,
        endpoints,
        official_models: gateway_models_from_config(&settings, &providers),
        diagnostics,
    })
}

pub fn gateway_test_request(
    kind: GatewayTestKind,
    model: Option<String>,
) -> Result<GatewayTestResult, String> {
    let settings = config::get_settings()?;
    gateway_test_request_with_settings(kind, model, &settings)
}

fn gateway_test_request_with_settings(
    kind: GatewayTestKind,
    model: Option<String>,
    settings: &Settings,
) -> Result<GatewayTestResult, String> {
    let model = model
        .filter(|value| !value.trim().is_empty())
        .unwrap_or_else(|| DEFAULT_MODEL.to_string());
    let endpoints = endpoints(settings.proxy_port);
    let client = Client::builder()
        .timeout(Duration::from_secs(
            settings.gateway_request_timeout_seconds.clamp(5, 600) as u64,
        ))
        .build()
        .map_err(|error| format!("failed to build HTTP client: {error}"))?;

    match kind {
        GatewayTestKind::Health => request_get(&client, "health", &health_url(settings.proxy_port)),
        GatewayTestKind::Models => request_get(&client, "models", &endpoints.models),
        GatewayTestKind::ChatCompletions => request_json(
            &client,
            "chat_completions",
            &endpoints.chat_completions,
            Some(model.clone()),
            Some(settings),
            json!({
                "model": model,
                "messages": [{"role": "user", "content": "Say hello in one word."}],
                "stream": false
            }),
            false,
        ),
        GatewayTestKind::ChatCompletionsStream => request_json(
            &client,
            "chat_completions_stream",
            &endpoints.chat_completions,
            Some(model.clone()),
            Some(settings),
            json!({
                "model": model,
                "messages": [{"role": "user", "content": "Say hello in one word."}],
                "stream": true
            }),
            true,
        ),
        GatewayTestKind::ResponsesStream => request_json(
            &client,
            "responses_stream",
            &endpoints.responses,
            Some(model.clone()),
            Some(settings),
            json!({
                "model": model,
                "input": "Say hello in one word.",
                "stream": true,
                "store": false
            }),
            true,
        ),
    }
}

fn non_empty_str(value: &str) -> Option<&str> {
    let trimmed = value.trim();
    if trimmed.is_empty() {
        None
    } else {
        Some(trimmed)
    }
}

pub fn gateway_recent_events(
    limit: Option<usize>,
    since_ts: Option<String>,
) -> Result<Vec<GatewayEvent>, String> {
    telemetry::gateway_recent_events(runtime_home(), limit, since_ts)
}

pub fn gateway_usage_summary(
    start_ts: Option<String>,
    end_ts: Option<String>,
) -> Result<GatewayUsageSummary, String> {
    telemetry::gateway_usage_summary(runtime_home(), start_ts, end_ts)
}

pub fn gateway_usage_snapshot(
    limit: Option<usize>,
    start_ts: Option<String>,
    end_ts: Option<String>,
) -> Result<GatewayUsageSnapshot, String> {
    telemetry::gateway_usage_snapshot(runtime_home(), limit, start_ts, end_ts)
}

pub fn gateway_usage_events(
    limit: Option<usize>,
    start_ts: Option<String>,
    end_ts: Option<String>,
) -> Result<Vec<GatewayUsageEvent>, String> {
    telemetry::gateway_usage_events(runtime_home(), limit, start_ts, end_ts)
}

pub fn start_telemetry_ingester() {
    telemetry::start_telemetry_ingester(runtime_home);
}

pub fn gateway_copy_client_config(
    _client_kind: Option<String>,
    model: Option<String>,
) -> Result<GatewayClientConfig, String> {
    let settings = config::get_settings()?;
    let model = model
        .filter(|value| !value.trim().is_empty())
        .unwrap_or_else(|| DEFAULT_MODEL.to_string());
    let base_url = endpoints(settings.proxy_port).base_url;
    let api_key = settings.gateway_client_key;
    let body = json!({
        "baseURL": base_url,
        "apiKey": api_key,
        "model": model,
    });
    let json_text = serde_json::to_string_pretty(&body)
        .map_err(|error| format!("failed to serialize client config: {error}"))?;
    let curl_test = format!(
        "curl -s -X POST {base_url}/chat/completions -H \"Authorization: Bearer {api_key}\" -H \"Content-Type: application/json\" -d '{{\"model\":\"{model}\",\"messages\":[{{\"role\":\"user\",\"content\":\"Say hello in one word.\"}}],\"stream\":false}}'"
    );

    Ok(GatewayClientConfig {
        base_url,
        api_key,
        model,
        json: json_text,
        curl_test,
    })
}

pub fn list_gateway_clients(include_versions: bool) -> Result<Vec<GatewayClientInfo>, String> {
    let settings = config::get_settings()?;
    let providers = config::get_providers()?;
    let current_owner = crate::app_flavor::current().routing_owner();
    let pending_client_ids = read_pending_client_ids();
    let opencode_path = detect_opencode_config_path();
    let opencode_executable = detect_opencode_executable_path();
    let opencode_installed = opencode_path
        .as_ref()
        .map(|path| path.exists())
        .unwrap_or(false)
        || opencode_executable.is_some();
    let generic_route_owner = current_owner;
    let mut clients = vec![GatewayClientInfo {
        id: "generic".to_string(),
        name: "Generic OpenAI-compatible".to_string(),
        kind: "Copy-only".to_string(),
        installed: true,
        auto_apply_supported: false,
        config_path: None,
        route_owner: generic_route_owner,
        route_endpoint: Some(endpoints(settings.proxy_port).base_url),
        managed_by_current_app: generic_route_owner == current_owner,
        route_mode: route_mode_for_owner(generic_route_owner, current_owner, false).to_string(),
        status: "Copy config is always available.".to_string(),
        versions_checked: false,
        current_version: None,
        latest_version: None,
    }];
    let opencode_owner_details = opencode_path
        .as_ref()
        .and_then(|path| fs::read_to_string(path).ok())
        .map(|text| {
            detect_route_details_from_json_provider_object(
                &text,
                "/provider",
                is_opencode_codexhub_config(&text),
                true,
                current_owner,
                settings.proxy_port,
            )
        })
        .unwrap_or((RoutingOwner::UnknownExternal, None));
    let opencode_route_mode = route_mode_for_owner(
        opencode_owner_details.0,
        current_owner,
        pending_sync_is_stale(
            pending_client_ids.contains("opencode"),
            opencode_owner_details.0,
            current_owner,
        ),
    );
    clients.push(GatewayClientInfo {
        id: "opencode".to_string(),
        name: "OpenCode".to_string(),
        kind: "Terminal client".to_string(),
        installed: opencode_installed,
        auto_apply_supported: opencode_path
            .as_ref()
            .map(|path| path.exists())
            .unwrap_or(false),
        config_path: opencode_path,
        route_owner: opencode_owner_details.0,
        route_endpoint: opencode_owner_details.1,
        managed_by_current_app: opencode_owner_details.0 == current_owner,
        route_mode: opencode_route_mode.to_string(),
        status: "Managed overwrite with backup is supported when config exists.".to_string(),
        versions_checked: include_versions && opencode_installed,
        current_version: include_versions.then(detect_opencode_version).flatten(),
        latest_version: (include_versions && opencode_installed)
            .then(|| npm_latest_version("opencode-ai"))
            .flatten(),
    });
    let zcode_targets = detect_zcode_config_targets();
    let zcode_store_path = detect_zcode_store_path();
    let zcode_executable = detect_zcode_executable_path();
    let zcode_installed = zcode_targets.catalog_path.exists()
        || zcode_targets.v2_config_path.exists()
        || zcode_targets.v2_cache_path.exists()
        || zcode_targets
            .v2_config_path
            .parent()
            .map(Path::exists)
            .unwrap_or(false)
        || zcode_store_path.exists()
        || zcode_executable.is_some()
        || command_exists(&["zcode", "ZCode", "ZCode.exe"]);
    // Drift validation must use the same currently exported model that an
    // apply/sync operation would resolve.  The historical builtin default
    // (`gpt-5.5`) is not guaranteed to be exported (for example when the
    // subscription catalog is unavailable), which otherwise labels a valid
    // ZCode injection as stale forever.
    let zcode_expected_model = default_gateway_client_sync_model(&settings, &providers)
        .unwrap_or_else(|_| DEFAULT_MODEL.to_string());
    let zcode_expected_stale = zcode_route_mode_with_expected(
        &zcode_targets,
        &settings,
        &providers,
        &zcode_expected_model,
    ) == "stale";
    let zcode_route_details =
        detect_zcode_route_details(&zcode_targets, current_owner, settings.proxy_port);
    let zcode_stale = zcode_expected_stale
        || pending_sync_is_stale(
            pending_client_ids.contains("zcode"),
            zcode_route_details.0,
            current_owner,
        );
    let zcode_route_mode = route_mode_for_owner(zcode_route_details.0, current_owner, zcode_stale);
    clients.push(GatewayClientInfo {
        id: "zcode".to_string(),
        name: "ZCode".to_string(),
        kind: "IDE extension".to_string(),
        installed: zcode_installed,
        auto_apply_supported: zcode_installed,
        config_path: Some(zcode_targets.v2_config_path.clone()),
        route_owner: zcode_route_details.0,
        route_endpoint: zcode_route_details.1,
        managed_by_current_app: zcode_route_details.0 == current_owner,
        route_mode: zcode_route_mode.to_string(),
        status: gateway_client_status(zcode_installed, zcode_route_mode),
        versions_checked: include_versions && zcode_installed,
        current_version: include_versions
            .then(|| {
                command_version(&["zcode", "ZCode", "ZCode.exe"])
                    .or_else(|| zcode_executable.as_deref().and_then(windows_file_version))
            })
            .flatten(),
        latest_version: (include_versions && zcode_installed)
            .then(zcode_latest_version)
            .flatten(),
    });

    let pi_paths = detect_pi_config_paths();
    let pi_installed = pi_paths.settings_path.exists()
        || pi_paths.models_path.exists()
        || pi_paths
            .settings_path
            .parent()
            .map(Path::exists)
            .unwrap_or(false)
        || command_exists(&["pi"]);
    let pi_route_details = detect_pi_route_details(&pi_paths, current_owner, settings.proxy_port);
    let pi_route_mode = route_mode_for_owner(
        pi_route_details.0,
        current_owner,
        pending_sync_is_stale(
            pending_client_ids.contains("pi"),
            pi_route_details.0,
            current_owner,
        ),
    );
    clients.push(GatewayClientInfo {
        id: "pi".to_string(),
        name: "Pi".to_string(),
        kind: "Compact CLI".to_string(),
        installed: pi_installed,
        auto_apply_supported: pi_installed,
        config_path: Some(pi_paths.settings_path),
        route_owner: pi_route_details.0,
        route_endpoint: pi_route_details.1,
        managed_by_current_app: pi_route_details.0 == current_owner,
        route_mode: pi_route_mode.to_string(),
        status: gateway_client_status(pi_installed, pi_route_mode),
        versions_checked: include_versions && pi_installed,
        current_version: include_versions.then(|| command_version(&["pi"])).flatten(),
        latest_version: (include_versions && pi_installed)
            .then(|| npm_latest_version("@earendil-works/pi-coding-agent"))
            .flatten(),
    });

    let omp_paths = detect_omp_config_paths();
    let omp_installed = omp_paths.config_path.exists()
        || omp_paths.models_path.exists()
        || omp_paths
            .config_path
            .parent()
            .map(Path::exists)
            .unwrap_or(false)
        || command_exists(&["omp"]);
    let omp_route_details =
        detect_omp_route_details(&omp_paths, current_owner, settings.proxy_port);
    let omp_route_mode = route_mode_for_owner(
        omp_route_details.0,
        current_owner,
        pending_sync_is_stale(
            pending_client_ids.contains("omp"),
            omp_route_details.0,
            current_owner,
        ),
    );
    clients.push(GatewayClientInfo {
        id: "omp".to_string(),
        name: "OMP".to_string(),
        kind: "Prompt runtime".to_string(),
        installed: omp_installed,
        auto_apply_supported: omp_installed,
        config_path: Some(omp_paths.config_path),
        route_owner: omp_route_details.0,
        route_endpoint: omp_route_details.1,
        managed_by_current_app: omp_route_details.0 == current_owner,
        route_mode: omp_route_mode.to_string(),
        status: gateway_client_status(omp_installed, omp_route_mode),
        versions_checked: include_versions && omp_installed,
        current_version: include_versions
            .then(|| command_version(&["omp"]))
            .flatten(),
        latest_version: (include_versions && omp_installed)
            .then(|| npm_latest_version("@oh-my-pi/pi-coding-agent"))
            .flatten(),
    });
    let dsh = detect_dsh_client();
    let dsh_report = if dsh.installed {
        dsh_client_readback().ok()
    } else {
        None
    };
    let dsh_unreadable = dsh.installed && dsh_report.is_none();
    let dsh_connected = dsh_report.as_ref().is_some_and(|report| report.connected);
    let dsh_block_present = dsh_report
        .as_ref()
        .is_some_and(|report| report.block_present);
    let dsh_route_mode = if !dsh.installed {
        "official"
    } else if dsh_connected {
        "hub"
    } else if dsh_block_present || dsh_unreadable {
        "stale"
    } else {
        "official"
    };
    let mut dsh_status_parts = vec![format!("DSH {}", dsh.qualification)];
    dsh_status_parts.push("hot reload".to_owned());
    dsh_status_parts.extend(dsh.drift_details.iter().cloned());
    if let Some(report) = &dsh_report {
        dsh_status_parts.extend(report.drift_details.iter().cloned());
    }
    clients.push(GatewayClientInfo {
        id: "dsh".to_owned(),
        name: "DeepSeek Harness".to_owned(),
        kind: "Agent runtime".to_owned(),
        installed: dsh.installed,
        auto_apply_supported: dsh.installed,
        config_path: Some(dsh.config_path),
        route_owner: if dsh_connected || dsh_block_present {
            current_owner
        } else {
            RoutingOwner::Official
        },
        route_endpoint: Some(endpoints(settings.proxy_port).base_url),
        managed_by_current_app: dsh_connected || dsh_block_present,
        route_mode: dsh_route_mode.to_owned(),
        status: dsh_status_parts.join("; "),
        versions_checked: include_versions && dsh.installed,
        current_version: dsh.version,
        latest_version: None,
    });
    Ok(clients)
}

pub fn preview_gateway_client_config(
    client_id: String,
    model: Option<String>,
) -> Result<GatewayClientConfigPreview, String> {
    let settings = config::get_settings()?;
    let providers = config::get_providers()?;
    let model = model.unwrap_or_else(|| DEFAULT_MODEL.to_string());
    let id = normalize_client_id(&client_id);
    managed_clients::preview_native(&id, &settings, &providers, &model)
}

pub fn apply_gateway_client_config(
    client_id: String,
    model: Option<String>,
) -> Result<GatewayClientApplyResult, String> {
    let id = normalize_client_id(&client_id);
    if !gateway_client_supports_native_apply(&id) {
        return Ok(GatewayClientApplyResult {
            client_id: id,
            applied: false,
            config_path: None,
            backup_path: None,
            message: "This client is copy-only; no native adapter is registered.".to_string(),
        });
    }
    let result = with_gateway_client_mutation_owner_gate(id, false, move |id, _| {
        apply_gateway_client_config_locked(id, model)
    })?;
    clear_pending_sync_after_successful_apply(result)
}

fn apply_gateway_client_config_locked(
    client_id: String,
    model: Option<String>,
) -> Result<GatewayClientApplyResult, String> {
    let _guard = gateway_client_config_write_lock()
        .lock()
        .map_err(|_| "gateway client config write lock is poisoned".to_string())?;
    let settings = config::get_settings()?;
    let providers = config::get_providers()?;
    let model = model.unwrap_or_else(|| DEFAULT_MODEL.to_string());
    managed_clients::apply_native(client_id, &settings, &providers, &model)
}

pub fn restore_gateway_client_config(
    client_id: String,
) -> Result<GatewayClientApplyResult, String> {
    let id = normalize_client_id(&client_id);
    if !gateway_client_supports_native_apply(&id) {
        return Ok(GatewayClientApplyResult {
            client_id: id,
            applied: false,
            config_path: None,
            backup_path: None,
            message: "Restore is not available for this copy-only client.".to_string(),
        });
    }
    let result = with_gateway_client_mutation_owner_gate(id, false, |id, owner| {
        restore_gateway_client_config_locked(id, owner)
    })?;
    clear_pending_sync_after_successful_apply(result)
}

fn restore_gateway_client_config_locked(
    client_id: String,
    backup_owner: RoutingOwner,
) -> Result<GatewayClientApplyResult, String> {
    let _guard = gateway_client_config_write_lock()
        .lock()
        .map_err(|_| "gateway client config write lock is poisoned".to_string())?;
    let backup_roots = client_backup_roots_for_restore(&client_id, backup_owner);
    managed_clients::restore_native(client_id, &backup_roots)
}

fn gateway_client_config_write_lock() -> &'static Mutex<()> {
    GATEWAY_CLIENT_CONFIG_WRITE_LOCK.get_or_init(|| Mutex::new(()))
}

/// Reject a hard-linked output file. A produced managed-client config must be
/// owned by exactly one path beneath the isolated root; a hard link means a
/// second name elsewhere owns the same inode, which breaks the single-owner
/// namespace invariant the readback verifier is responsible for.
pub(crate) fn reject_hard_link(path: &Path) -> Result<(), String> {
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        let metadata = fs::metadata(path)
            .map_err(|error| format!("readback failed: cannot stat {}: {error}", path.display()))?;
        if metadata.nlink() != 1 {
            return Err(format!(
                "readback failed: {} is a hard link (nlink={}); refusing single-owner namespace violation",
                path.file_name().and_then(|n| n.to_str()).unwrap_or("unknown"),
                metadata.nlink()
            ));
        }
    }
    #[cfg(windows)]
    {
        use std::fs::File;
        use std::os::windows::io::AsRawHandle;
        // GetFileInformationByHandle is the only std-friendly way to read
        // number_of_links on Windows; the std MetadataExt does not expose it.
        #[repr(C)]
        #[derive(Default)]
        struct ByHandleFileInformation {
            file_attributes: u32,
            creation_time_low: u32,
            creation_time_high: u32,
            last_access_time_low: u32,
            last_access_time_high: u32,
            last_write_time_low: u32,
            last_write_time_high: u32,
            volume_serial_number: u32,
            file_size_high: u32,
            file_size_low: u32,
            number_of_links: u32,
            file_index_high: u32,
            file_index_low: u32,
        }
        extern "system" {
            fn GetFileInformationByHandle(
                handle: *mut std::ffi::c_void,
                info: *mut ByHandleFileInformation,
            ) -> i32;
        }
        let file = File::open(path)
            .map_err(|error| format!("readback failed: cannot open {}: {error}", path.display()))?;
        let mut info = ByHandleFileInformation::default();
        let result =
            unsafe { GetFileInformationByHandle(file.as_raw_handle() as *mut _, &mut info) };
        if result == 0 || info.number_of_links != 1 {
            return Err(format!(
                "readback failed: {} is a hard link (nlink={}); refusing single-owner namespace violation",
                path.file_name().and_then(|n| n.to_str()).unwrap_or("unknown"),
                info.number_of_links
            ));
        }
    }
    #[cfg(not(any(unix, windows)))]
    {
        let _ = path;
    }
    Ok(())
}

fn gateway_client_route_model(
    requested: Option<String>,
    settings: &Settings,
    providers: &[Provider],
) -> Result<String, String> {
    if let Some(requested) = requested
        .as_deref()
        .map(str::trim)
        .filter(|value| !value.is_empty())
    {
        if let Ok(resolved) = resolve_gateway_client_model_id(settings, providers, requested) {
            return Ok(resolved);
        }
    }
    default_gateway_client_sync_model(settings, providers)
}

pub fn switch_gateway_client_route(
    client_id: String,
    mode: String,
    model: Option<String>,
    force_takeover: Option<bool>,
) -> Result<GatewayClientApplyResult, String> {
    let current_app_owner = crate::app_flavor::current().routing_owner();
    let next_owner = match mode.as_str() {
        "official" => RoutingOwner::Official,
        "release" => RoutingOwner::Release,
        "beta" => RoutingOwner::Beta,
        "hub" => current_app_owner,
        other => return Err(format!("unsupported routing owner: {other}")),
    };
    let model = if next_owner == current_app_owner {
        let settings = config::get_settings()?;
        let providers = config::get_providers()?;
        Some(gateway_client_route_model(model, &settings, &providers)?)
    } else {
        model
    };
    let result = with_gateway_client_mutation_owner_gate(
        normalize_client_id(&client_id),
        force_takeover.unwrap_or(false),
        move |id, current_target_owner| {
            if next_owner == RoutingOwner::Official {
                restore_gateway_client_config_locked(id, current_target_owner)
            } else if next_owner == current_app_owner {
                apply_gateway_client_config_locked(id, model)
            } else {
                Err(format!(
                    "{} builds can only apply {} routes.",
                    owner_label(current_app_owner),
                    owner_label(current_app_owner)
                ))
            }
        },
    )?;
    clear_pending_sync_after_successful_apply(result)
}

pub fn sync_gateway_clients(model: Option<String>) -> Result<GatewayClientSyncSummary, String> {
    let settings = config::get_settings()?;
    let providers = config::get_providers()?;
    let model = Some(gateway_client_sync_model_arg(model, &settings, &providers)?);
    let clients = list_gateway_clients(false)?;
    let summary = sync_gateway_clients_from_infos(clients, model, apply_gateway_client_config);
    persist_pending_client_syncs(&summary)?;
    Ok(summary)
}

fn sync_gateway_clients_from_infos<F>(
    clients: Vec<GatewayClientInfo>,
    model: Option<String>,
    mut apply_client: F,
) -> GatewayClientSyncSummary
where
    F: FnMut(String, Option<String>) -> Result<GatewayClientApplyResult, String>,
{
    let mut applied = 0_u32;
    let mut skipped = 0_u32;
    let mut failed = 0_u32;
    let mut results = Vec::new();

    for client in clients {
        let skip_reason = gateway_client_sync_skip_reason(&client);
        if let Some(message) = skip_reason {
            skipped = skipped.saturating_add(1);
            results.push(GatewayClientSyncItem {
                client_id: client.id,
                name: client.name,
                status: "skipped".to_string(),
                applied: false,
                skipped: true,
                message,
                config_path: client.config_path,
                backup_path: None,
            });
            continue;
        }

        match apply_client(client.id.clone(), model.clone()) {
            Ok(result) => {
                if result.applied {
                    applied = applied.saturating_add(1);
                    results.push(GatewayClientSyncItem {
                        client_id: result.client_id,
                        name: client.name,
                        status: "applied".to_string(),
                        applied: true,
                        skipped: false,
                        message: result.message,
                        config_path: result.config_path,
                        backup_path: result.backup_path,
                    });
                } else {
                    skipped = skipped.saturating_add(1);
                    results.push(GatewayClientSyncItem {
                        client_id: result.client_id,
                        name: client.name,
                        status: "skipped".to_string(),
                        applied: false,
                        skipped: true,
                        message: result.message,
                        config_path: result.config_path,
                        backup_path: result.backup_path,
                    });
                }
            }
            Err(error) => {
                failed = failed.saturating_add(1);
                results.push(GatewayClientSyncItem {
                    client_id: client.id,
                    name: client.name,
                    status: "failed".to_string(),
                    applied: false,
                    skipped: false,
                    message: error,
                    config_path: client.config_path,
                    backup_path: None,
                });
            }
        }
    }

    let message = if failed > 0 {
        format!("Synced {applied} bound Gateway client(s); {failed} failed; skipped {skipped}")
    } else if applied > 0 {
        format!("Synced {applied} bound Gateway client(s); skipped {skipped}")
    } else {
        "No bound Gateway clients needed sync".to_string()
    };

    GatewayClientSyncSummary {
        applied,
        skipped,
        failed,
        results,
        message,
    }
}

fn gateway_client_sync_state_path() -> Result<PathBuf, String> {
    Ok(runtime_paths::runtime_home_dir()?
        .join("proxy")
        .join("gateway-client-sync-state.json"))
}

fn read_pending_client_ids() -> HashSet<String> {
    let Ok(path) = gateway_client_sync_state_path() else {
        return HashSet::new();
    };
    read_pending_client_ids_from_path(&path)
}

fn read_pending_client_ids_from_path(path: &Path) -> HashSet<String> {
    let Ok(text) = fs::read_to_string(path) else {
        return HashSet::new();
    };
    serde_json::from_str::<GatewayClientSyncState>(&text)
        .map(|state| state.pending_client_ids.into_iter().collect())
        .unwrap_or_default()
}

fn write_pending_client_ids(pending: &HashSet<String>) -> Result<(), String> {
    let path = gateway_client_sync_state_path()?;
    write_pending_client_ids_to_path(&path, pending)
}

fn write_pending_client_ids_to_path(path: &Path, pending: &HashSet<String>) -> Result<(), String> {
    if pending.is_empty() {
        return match fs::remove_file(path) {
            Ok(()) => Ok(()),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(()),
            Err(error) => Err(format!(
                "failed to remove Gateway client sync state {}: {error}",
                path.display()
            )),
        };
    }
    let mut pending_client_ids = pending.iter().cloned().collect::<Vec<_>>();
    pending_client_ids.sort();
    let text = serde_json::to_string_pretty(&GatewayClientSyncState { pending_client_ids })
        .map_err(|error| format!("failed to serialize Gateway client sync state: {error}"))?;
    safe_file::write_text_atomic(path, &format!("{text}\n"))
}

fn update_pending_client_ids_after_sync(
    pending: &mut HashSet<String>,
    summary: &GatewayClientSyncSummary,
) {
    for result in &summary.results {
        if result.applied {
            pending.remove(&result.client_id);
        } else if result.status == "failed" {
            pending.insert(result.client_id.clone());
        }
    }
}

fn persist_pending_client_syncs(summary: &GatewayClientSyncSummary) -> Result<(), String> {
    let _guard = GATEWAY_CLIENT_SYNC_STATE_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .map_err(|_| "Gateway client sync state lock is poisoned".to_string())?;
    let mut pending = read_pending_client_ids();
    update_pending_client_ids_after_sync(&mut pending, summary);
    write_pending_client_ids(&pending)
}

fn clear_pending_sync_after_successful_apply(
    result: GatewayClientApplyResult,
) -> Result<GatewayClientApplyResult, String> {
    if result.applied {
        clear_client_sync_pending(&result.client_id)?;
    }
    Ok(result)
}

fn clear_client_sync_pending(client_id: &str) -> Result<(), String> {
    let _guard = GATEWAY_CLIENT_SYNC_STATE_LOCK
        .get_or_init(|| Mutex::new(()))
        .lock()
        .map_err(|_| "Gateway client sync state lock is poisoned".to_string())?;
    let mut pending_client_ids = read_pending_client_ids();
    pending_client_ids.remove(client_id);
    write_pending_client_ids(&pending_client_ids)
}

fn gateway_client_sync_model_arg(
    model: Option<String>,
    settings: &Settings,
    providers: &[Provider],
) -> Result<String, String> {
    if let Some(requested) = model
        .as_deref()
        .map(str::trim)
        .filter(|value| !value.is_empty())
    {
        return resolve_gateway_client_model_id(settings, providers, requested);
    }
    default_gateway_client_sync_model(settings, providers)
}

fn default_gateway_client_sync_model(
    settings: &Settings,
    providers: &[Provider],
) -> Result<String, String> {
    let models = gateway_models_from_config(settings, providers);
    if models.iter().any(|model| model.id == DEFAULT_MODEL) {
        return Ok(DEFAULT_MODEL.to_string());
    }
    for (id, _, _) in OFFICIAL_MODELS {
        if *id == DEFAULT_MODEL {
            continue;
        }
        if models.iter().any(|model| model.id == *id) {
            return Ok((*id).to_string());
        }
    }
    for (_, id, _, _) in OFFICIAL_FAST_VARIANTS {
        if models.iter().any(|model| model.id == *id) {
            return Ok((*id).to_string());
        }
    }
    models
        .into_iter()
        .map(|model| model.id)
        .find(|model| !model.trim().is_empty())
        .ok_or_else(|| "No Gateway models are exported.".to_string())
}

fn gateway_client_sync_skip_reason(client: &GatewayClientInfo) -> Option<String> {
    if !client.installed {
        return Some("Client is not installed.".to_string());
    }
    if !client.auto_apply_supported {
        return Some("Client does not support automatic config sync.".to_string());
    }
    if !client.managed_by_current_app {
        return Some("Client is managed by another CodexHub channel.".to_string());
    }
    if client.route_mode != "hub" && client.route_mode != "stale" {
        return Some("Client is not bound to CodexHub.".to_string());
    }
    None
}

fn gateway_client_supports_native_apply(client_id: &str) -> bool {
    matches!(client_id, "opencode" | "pi" | "omp" | "zcode")
}

fn with_gateway_client_mutation_owner_gate<F>(
    client_id: String,
    force_takeover: bool,
    operation: F,
) -> Result<GatewayClientApplyResult, String>
where
    F: FnOnce(String, RoutingOwner) -> Result<GatewayClientApplyResult, String>,
{
    let current_app_owner = crate::app_flavor::current().routing_owner();
    let current_target_owner = list_gateway_clients(false)?
        .into_iter()
        .find(|client| client.id == client_id)
        .map(|client| client.route_owner)
        .ok_or_else(|| format!("unknown gateway client: {client_id}"))?;
    if !gateway_client_has_existing_config(&client_id) {
        return operation(client_id, current_target_owner);
    }
    ensure_route_owner_mutation_allowed(current_app_owner, current_target_owner, force_takeover)?;
    operation(client_id, current_target_owner)
}

fn gateway_client_has_existing_config(client_id: &str) -> bool {
    managed_clients::has_existing_config(client_id)
}

pub fn subagent_matrix_status() -> Result<SubagentMatrixStatus, String> {
    let status = gateway_status()?;
    let readiness = subagent_readiness(&status.features);
    let recent_events =
        telemetry::read_recent_events(runtime_home(), 20, Some(subagent_event_filter), None);
    let rows = OFFICIAL_MODELS
        .iter()
        .map(|(id, _, _)| SubagentMatrixRow {
            model: (*id).to_string(),
            provider: "official".to_string(),
            thread_id: None,
            child_agent_id: None,
            wait_timed_out: None,
            close_succeeded: None,
            child_output_ok: None,
            status: "not_run_in_ui".to_string(),
            detail: "No recent matrix result file is exposed yet; use recent proxy events for lifecycle evidence.".to_string(),
        })
        .collect();

    Ok(SubagentMatrixStatus {
        readiness,
        rows,
        recent_events,
        message: "Readiness is derived from proxy feature flags; matrix rows are placeholders until a persisted subagent run result is available.".to_string(),
    })
}

fn request_get(client: &Client, kind: &str, endpoint: &str) -> Result<GatewayTestResult, String> {
    let started = Instant::now();
    match client.get(endpoint).send() {
        Ok(response) => {
            let status = response.status().as_u16();
            let body = response.text().unwrap_or_default();
            Ok(GatewayTestResult {
                ok: (200..300).contains(&status),
                kind: kind.to_string(),
                endpoint: endpoint.to_string(),
                method: "GET".to_string(),
                model: None,
                status: Some(status),
                latency_ms: started.elapsed().as_millis(),
                first_token_ms: None,
                sanitized_body: Some(sanitize_text(&body)),
                error: None,
            })
        }
        Err(error) => Ok(GatewayTestResult {
            ok: false,
            kind: kind.to_string(),
            endpoint: endpoint.to_string(),
            method: "GET".to_string(),
            model: None,
            status: None,
            latency_ms: started.elapsed().as_millis(),
            first_token_ms: None,
            sanitized_body: None,
            error: Some(error.without_url().to_string()),
        }),
    }
}

fn request_json(
    client: &Client,
    kind: &str,
    endpoint: &str,
    model: Option<String>,
    local_gateway_settings: Option<&Settings>,
    body: Value,
    stream: bool,
) -> Result<GatewayTestResult, String> {
    let started = Instant::now();
    let mut request = client
        .post(endpoint)
        .header("Content-Type", "application/json")
        .body(body.to_string());
    if let Some(settings) = local_gateway_settings {
        request = attach_local_gateway_authorization(request, endpoint, settings);
    }
    let response = request.send();

    match response {
        Ok(mut response) => {
            let status = response.status().as_u16();
            let mut bytes = Vec::new();
            let mut first_token_ms = None;
            let mut buffer = [0_u8; 1024];
            loop {
                match response.read(&mut buffer) {
                    Ok(0) => break,
                    Ok(count) => {
                        if stream
                            && first_token_ms.is_none()
                            && has_nonempty_payload(&buffer[..count])
                        {
                            first_token_ms = Some(started.elapsed().as_millis());
                        }
                        if bytes.len() < 4096 {
                            bytes.extend_from_slice(&buffer[..count.min(4096 - bytes.len())]);
                        }
                    }
                    Err(error) => {
                        return Ok(GatewayTestResult {
                            ok: false,
                            kind: kind.to_string(),
                            endpoint: endpoint.to_string(),
                            method: "POST".to_string(),
                            model,
                            status: Some(status),
                            latency_ms: started.elapsed().as_millis(),
                            first_token_ms,
                            sanitized_body: Some(sanitize_text(&String::from_utf8_lossy(&bytes))),
                            error: Some(error.to_string()),
                        })
                    }
                }
            }
            Ok(GatewayTestResult {
                ok: (200..300).contains(&status),
                kind: kind.to_string(),
                endpoint: endpoint.to_string(),
                method: "POST".to_string(),
                model,
                status: Some(status),
                latency_ms: started.elapsed().as_millis(),
                first_token_ms,
                sanitized_body: Some(sanitize_text(&String::from_utf8_lossy(&bytes))),
                error: None,
            })
        }
        Err(error) => Ok(GatewayTestResult {
            ok: false,
            kind: kind.to_string(),
            endpoint: endpoint.to_string(),
            method: "POST".to_string(),
            model,
            status: None,
            latency_ms: started.elapsed().as_millis(),
            first_token_ms: None,
            sanitized_body: None,
            error: Some(error.without_url().to_string()),
        }),
    }
}

fn attach_local_gateway_authorization(
    request: reqwest::blocking::RequestBuilder,
    endpoint: &str,
    settings: &Settings,
) -> reqwest::blocking::RequestBuilder {
    let has_authorization = request
        .try_clone()
        .and_then(|request| request.build().ok())
        .is_some_and(|request| request.headers().contains_key("Authorization"));
    if has_authorization {
        return request;
    }
    let Some(key) = local_gateway_client_key_for_endpoint(endpoint, settings) else {
        return request;
    };
    request.header("Authorization", format!("Bearer {key}"))
}

fn local_gateway_client_key_for_endpoint<'a>(
    endpoint: &str,
    settings: &'a Settings,
) -> Option<&'a str> {
    let url = reqwest::Url::parse(endpoint).ok()?;
    if url.scheme() != "http" || url.port_or_known_default() != Some(settings.proxy_port) {
        return None;
    }
    let host = url.host_str()?;
    if !host.eq_ignore_ascii_case("localhost")
        && host
            .parse::<std::net::IpAddr>()
            .ok()
            .is_none_or(|host| !host.is_loopback())
    {
        return None;
    }
    let path = url.path().trim_end_matches('/');
    if path != "/v1" && !path.starts_with("/v1/") {
        return None;
    }
    non_empty_str(&settings.gateway_client_key)
}

fn endpoints(port: u16) -> GatewayEndpoints {
    let base_url = format!("http://127.0.0.1:{port}/v1");
    GatewayEndpoints {
        models: format!("{base_url}/models"),
        responses: format!("{base_url}/responses"),
        chat_completions: format!("{base_url}/chat/completions"),
        base_url,
    }
}

fn health_url(port: u16) -> String {
    format!("http://127.0.0.1:{port}/health")
}

fn read_health(port: u16, timeout: Duration) -> Result<Option<HealthResponse>, String> {
    let client = Client::builder()
        .timeout(timeout)
        .build()
        .map_err(|error| format!("failed to build HTTP client: {error}"))?;
    let response = match client.get(health_url(port)).send() {
        Ok(response) => response,
        Err(_) => return Ok(None),
    };
    if !response.status().is_success() {
        return Ok(None);
    }
    Ok(response.json::<HealthResponse>().ok())
}

fn runtime_home() -> PathBuf {
    crate::runtime_paths::runtime_home_dir()
        .unwrap_or_else(|_| PathBuf::from(crate::app_flavor::current().runtime_home_suffix()))
}

fn runtime_proxy_dir(home: &Path) -> PathBuf {
    home.join("proxy")
}

fn gateway_diagnostics(
    proxy_running: bool,
    has_chat_completions_gateway: bool,
    auth: &CodexAuthStatus,
) -> Vec<GatewayDiagnostic> {
    let mut diagnostics = Vec::new();
    if !proxy_running {
        diagnostics.push(GatewayDiagnostic {
            level: "status".to_string(),
            category: "proxy_state".to_string(),
            message: "Gateway is stopped.".to_string(),
        });
    }
    if proxy_running && !has_chat_completions_gateway {
        diagnostics.push(GatewayDiagnostic {
            level: "error".to_string(),
            category: "gateway_feature".to_string(),
            message: "Proxy health does not report chat-completions-gateway.".to_string(),
        });
    }
    if !auth.logged_in {
        diagnostics.push(GatewayDiagnostic {
            level: "error".to_string(),
            category: "codex_auth".to_string(),
            message: auth.issue.clone().unwrap_or_else(|| {
                "Codex auth is unavailable; Gateway cannot reach official models.".to_string()
            }),
        });
    } else if !auth.account_id_present {
        diagnostics.push(GatewayDiagnostic {
            level: "warning".to_string(),
            category: "codex_auth".to_string(),
            message: "Codex auth is present, but account id is missing.".to_string(),
        });
    }
    if diagnostics.is_empty() {
        diagnostics.push(GatewayDiagnostic {
            level: "ok".to_string(),
            category: "gateway".to_string(),
            message: "Gateway prerequisites are present.".to_string(),
        });
    }
    diagnostics
}
fn subagent_event_filter(event: &GatewayEvent) -> bool {
    matches!(
        event.event.as_deref(),
        Some(
            "explicit_codex_tools_injected"
                | "third_party_tool_call_alias_normalized"
                | "multi_agent_current_state_guidance_injected"
                | "request_error"
                | "upstream_stream_interrupted"
                | "tool_search_discovery_fallback_applied"
        )
    )
}

fn subagent_readiness(features: &[String]) -> Vec<SubagentReadiness> {
    let has = |feature: &str| features.iter().any(|value| value == feature);
    vec![
        SubagentReadiness {
            step: "tool_search".to_string(),
            ready: has("third-party-tool-search-call-shim"),
            feature: "third-party-tool-search-call-shim".to_string(),
        },
        SubagentReadiness {
            step: "spawn_agent".to_string(),
            ready: has("third-party-explicit-codex-native-tools")
                && has("third-party-spawn-hidden-while-agent-open"),
            feature: "third-party-explicit-codex-native-tools + third-party-spawn-hidden-while-agent-open".to_string(),
        },
        SubagentReadiness {
            step: "wait_agent".to_string(),
            ready: has("third-party-multi-agent-wait-close-argument-shim"),
            feature: "third-party-multi-agent-wait-close-argument-shim".to_string(),
        },
        SubagentReadiness {
            step: "close_agent".to_string(),
            ready: has("third-party-single-loop-completion-gate"),
            feature: "third-party-single-loop-completion-gate".to_string(),
        },
    ]
}

fn gateway_client_status(installed: bool, route_mode: &str) -> String {
    if !installed {
        return "Not installed.".to_string();
    }
    if route_mode == "stale" {
        return "CodexHub config is out of date; reapply the CodexHub route.".to_string();
    }
    if route_mode == "hub" {
        "Ready; routed through CodexHub Gateway.".to_string()
    } else {
        "Installed; native config switching is supported.".to_string()
    }
}

fn command_exists(commands: &[&str]) -> bool {
    commands.iter().any(|command| which::which(command).is_ok())
}

fn command_version(commands: &[&str]) -> Option<String> {
    commands
        .iter()
        .filter_map(|command| which::which(command).ok())
        .find_map(|path| executable_version(&path))
}

fn executable_version(path: &Path) -> Option<String> {
    let output = version_output_for_path(path)?;
    let text = if output.stdout.is_empty() {
        String::from_utf8_lossy(&output.stderr)
    } else {
        String::from_utf8_lossy(&output.stdout)
    };
    parse_version_output(&text)
}

fn version_output_for_path(path: &Path) -> Option<std::process::Output> {
    if !is_supported_version_probe_path(path) {
        return None;
    }
    let extension = path
        .extension()
        .and_then(|value| value.to_str())
        .unwrap_or_default()
        .to_ascii_lowercase();
    match extension.as_str() {
        "cmd" => {
            let mut command = Command::new("cmd");
            command.args(["/C", path.to_string_lossy().as_ref(), "--version"]);
            command_output_no_window(command)
        }
        _ => {
            let mut command = Command::new(path);
            command.arg("--version");
            command_output_no_window(command)
        }
    }
}

fn is_supported_version_probe_path(path: &Path) -> bool {
    let extension = path
        .extension()
        .and_then(|value| value.to_str())
        .unwrap_or_default()
        .to_ascii_lowercase();
    match extension.as_str() {
        "" | "exe" => true,
        #[cfg(target_os = "windows")]
        "cmd" | "com" => true,
        _ => false,
    }
}

fn command_output_no_window(mut command: Command) -> Option<std::process::Output> {
    crate::runtime_paths::configure_no_window(&mut command);
    command
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let mut child = command.spawn().ok()?;
    let deadline = Instant::now() + VERSION_PROBE_TIMEOUT;
    loop {
        if child.try_wait().ok()?.is_some() {
            return child.wait_with_output().ok();
        }
        if Instant::now() >= deadline {
            let _ = child.kill();
            let _ = child.wait();
            return None;
        }
        thread::sleep(Duration::from_millis(25));
    }
}

fn parse_version_output(output: &str) -> Option<String> {
    output
        .lines()
        .map(str::trim)
        .filter(|line| !line.is_empty())
        .find_map(|line| {
            let token = line
                .split_whitespace()
                .find(|part| part.chars().any(|char| char.is_ascii_digit()))
                .unwrap_or(line);
            let value = token.rsplit('/').next().unwrap_or(token).trim();
            let value = value
                .trim_start_matches('v')
                .trim_matches(|char: char| char == '"' || char == '\'');
            (!value.is_empty()).then(|| value.to_string())
        })
}

fn npm_latest_version(package_name: &str) -> Option<String> {
    let package_path = if package_name.starts_with('@') {
        package_name.replace('/', "%2F")
    } else {
        package_name.to_string()
    };
    let url = format!("https://registry.npmjs.org/{package_path}/latest");
    let client = Client::builder()
        .timeout(Duration::from_secs(4))
        .build()
        .ok()?;
    let value = client.get(url).send().ok()?.json::<Value>().ok()?;
    value
        .get("version")
        .and_then(Value::as_str)
        .map(ToOwned::to_owned)
}

fn is_exact_semver_like(value: &str) -> bool {
    let mut parts = value.split('.');
    let Some(major) = parts.next() else {
        return false;
    };
    let Some(minor) = parts.next() else {
        return false;
    };
    let Some(patch) = parts.next() else {
        return false;
    };
    parts.next().is_none()
        && !major.is_empty()
        && !minor.is_empty()
        && !patch.is_empty()
        && major.chars().all(|char| char.is_ascii_digit())
        && minor.chars().all(|char| char.is_ascii_digit())
        && patch.chars().all(|char| char.is_ascii_digit())
}

fn windows_app_path(exe_name: &str) -> Option<PathBuf> {
    if !cfg!(windows) {
        return None;
    }
    [
        format!(r"HKCU\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe_name}"),
        format!(r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe_name}"),
        format!(r"HKLM\SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\{exe_name}"),
    ]
    .into_iter()
    .find_map(|key| {
        let mut command = Command::new("reg");
        command.args(["query", &key, "/ve"]);
        let output = command_output_no_window(command)?;
        if !output.status.success() {
            return None;
        }
        let text = String::from_utf8_lossy(&output.stdout);
        text.lines().find_map(|line| {
            let mut parts = line.split_whitespace();
            let _name = parts.next()?;
            let kind = parts.next()?;
            if !kind.starts_with("REG_") {
                return None;
            }
            let value = parts.collect::<Vec<_>>().join(" ");
            let path = PathBuf::from(value.trim());
            path.exists().then_some(path)
        })
    })
}

fn windows_file_version(path: &Path) -> Option<String> {
    if !cfg!(windows) || !path.exists() {
        return None;
    }
    let escaped = path.to_string_lossy().replace('\'', "''");
    let script = format!("(Get-Item -LiteralPath '{escaped}').VersionInfo.ProductVersion");
    let mut command = Command::new("powershell");
    command.args(["-NoProfile", "-Command", &script]);
    let output = command_output_no_window(command)?;
    if !output.status.success() {
        return None;
    }
    let text = String::from_utf8_lossy(&output.stdout);
    parse_version_output(&text)
}

fn has_nonempty_payload(bytes: &[u8]) -> bool {
    let text = String::from_utf8_lossy(bytes);
    text.lines().any(|line| {
        line.starts_with("data:") && line.trim() != "data:" && line.trim() != "data: [DONE]"
    })
}

#[cfg(test)]
mod tests {
    use super::{
        apply_opencode_config_with_paths, gateway_client_provider_groups_from_exported,
        gateway_models_from_config, gateway_models_from_sources, official_gateway_reasoning_levels,
        official_models_from_metadata, omp_models_yml_text, opencode_config_text,
        opencode_reasoning_variants, pi_models_text, pi_settings_text,
        read_usage_events_from_sqlite_path, read_usage_events_from_text,
        read_usage_summary_from_sqlite_path_with_pricing, read_usage_summary_from_text,
        read_usage_summary_from_text_with_pricing, restore_latest_backup, runtime_proxy_dir,
        sanitize_event, sanitize_text, usage_pricing_by_model, zcode_catalog_text, UsagePricing,
    };
    use crate::{Model, Provider, Settings, UpstreamFormat};
    use serde_json::json;
    use std::collections::{BTreeMap, HashMap};
    use std::fs;
    use std::io::{Read, Write};
    use std::net::TcpListener;
    use std::path::{Path, PathBuf};
    use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
    use std::sync::mpsc;
    use std::sync::{Mutex, OnceLock};
    use std::thread;
    #[cfg(target_os = "windows")]
    use std::time::{Duration, Instant};
    use std::time::{SystemTime, UNIX_EPOCH};

    include!("tests/helpers.rs");
    include!("tests/body_01.rs");
    include!("tests/body_02.rs");
    include!("tests/body_03.rs");
    include!("tests/body_04.rs");
}
