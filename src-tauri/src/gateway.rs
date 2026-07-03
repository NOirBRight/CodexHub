use crate::{config, models, Provider, Settings};
use reqwest::blocking::Client;
use rusqlite::{params, Connection, OptionalExtension};
use serde::{Deserialize, Serialize};
use serde_json::{json, Map, Value};
use sha2::{Digest, Sha256};
use std::collections::{HashMap, HashSet};
use std::fs;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

const HEALTH_TIMEOUT: Duration = Duration::from_millis(900);
const EVENT_READ_LIMIT_BYTES: u64 = 4 * 1024 * 1024;
const DEFAULT_MODEL: &str = "openai/gpt-5.5";

const OFFICIAL_MODELS: &[(&str, &str, u32)] = &[
    ("openai/gpt-5.5", "OpenAI GPT-5.5", 272000),
    ("openai/gpt-5.4", "OpenAI GPT-5.4", 272000),
    ("openai/gpt-5.4-mini", "OpenAI GPT-5.4-Mini", 272000),
    (
        "openai/gpt-5.3-codex-spark",
        "OpenAI GPT-5.3-Codex-Spark",
        128000,
    ),
];

const OFFICIAL_FAST_VARIANTS: &[(&str, &str, &str, u32)] = &[
    (
        "openai/gpt-5.5",
        "openai/gpt-5.5-fast",
        "OpenAI GPT-5.5 Fast",
        272000,
    ),
    (
        "openai/gpt-5.4",
        "openai/gpt-5.4-fast",
        "OpenAI GPT-5.4 Fast",
        272000,
    ),
];

const OFFICIAL_FAST_PRICING: &[(&str, f64, f64, f64)] = &[
    ("openai/gpt-5.5-fast", 12.50, 1.25, 75.00),
    ("openai/gpt-5.4-fast", 5.00, 0.50, 30.00),
];

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
    pub context_window: u32,
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

#[derive(Debug, Clone, Copy)]
struct UsagePricing {
    input_per_million: f64,
    cached_input_per_million: Option<f64>,
    output_per_million: f64,
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
    pub route_mode: String,
    pub status: String,
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

#[derive(Debug, Clone)]
struct PiConfigPaths {
    settings_path: PathBuf,
    models_path: PathBuf,
}

#[derive(Debug, Clone)]
struct OmpConfigPaths {
    config_path: PathBuf,
    models_path: PathBuf,
}

#[derive(Debug, Clone, Serialize)]
pub struct GatewayEvent {
    pub ts: Option<String>,
    pub event: Option<String>,
    pub request_id: Option<String>,
    pub path: Option<String>,
    pub method: Option<String>,
    pub model: Option<String>,
    pub upstream: Option<String>,
    pub upstream_format: Option<String>,
    pub inbound_format: Option<String>,
    pub route_reason: Option<String>,
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

pub fn gateway_recent_events(limit: Option<usize>) -> Result<Vec<GatewayEvent>, String> {
    let limit = limit.unwrap_or(20).clamp(1, 100);
    Ok(read_recent_events(limit, None))
}

pub fn gateway_usage_summary() -> Result<GatewayUsageSummary, String> {
    let db_path = telemetry_db_path();
    ensure_telemetry_sqlite_ready(&db_path)?;
    let pricing = usage_pricing_by_model();
    read_usage_summary_from_sqlite_path_with_pricing(&db_path, &pricing)
}

pub fn gateway_usage_events(limit: Option<usize>) -> Result<Vec<GatewayUsageEvent>, String> {
    let limit = limit.unwrap_or(100).clamp(1, 500);
    let db_path = telemetry_db_path();
    ensure_telemetry_sqlite_ready(&db_path)?;
    read_usage_events_from_sqlite_path(&db_path, limit)
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
        "curl -s -X POST {base_url}/chat/completions -H \"Content-Type: application/json\" -d '{{\"model\":\"{model}\",\"messages\":[{{\"role\":\"user\",\"content\":\"Say hello in one word.\"}}],\"stream\":false}}'"
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
    let opencode_path = detect_opencode_config_path();
    let opencode_installed = opencode_path
        .as_ref()
        .map(|path| path.exists())
        .unwrap_or(false)
        || command_exists(&["opencode"]);
    let mut clients = vec![GatewayClientInfo {
        id: "generic".to_string(),
        name: "Generic OpenAI-compatible".to_string(),
        kind: "Copy-only".to_string(),
        installed: true,
        auto_apply_supported: false,
        config_path: None,
        route_mode: "copy_only".to_string(),
        status: "Copy config is always available.".to_string(),
        current_version: None,
        latest_version: None,
    }];
    let opencode_route_mode = opencode_path
        .as_ref()
        .and_then(|path| fs::read_to_string(path).ok())
        .map(|text| {
            if is_opencode_codexhub_config(&text) {
                "hub"
            } else {
                "official"
            }
        })
        .unwrap_or("unknown");
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
        route_mode: opencode_route_mode.to_string(),
        status: "Managed overwrite with backup is supported when config exists.".to_string(),
        current_version: include_versions
            .then(|| command_version(&["opencode"]))
            .flatten(),
        latest_version: (include_versions && opencode_installed)
            .then(|| npm_latest_version("opencode-ai"))
            .flatten(),
    });
    let zcode_path = detect_zcode_config_path();
    let zcode_store_path = detect_zcode_store_path();
    let zcode_executable = detect_zcode_executable_path();
    let zcode_installed = zcode_path.exists()
        || zcode_store_path.exists()
        || zcode_executable.is_some()
        || command_exists(&["zcode", "ZCode", "ZCode.exe"]);
    let zcode_route_mode = route_mode_from_text_file(&zcode_path, is_zcode_codexhub_config);
    clients.push(GatewayClientInfo {
        id: "zcode".to_string(),
        name: "ZCode".to_string(),
        kind: "IDE extension".to_string(),
        installed: zcode_installed,
        auto_apply_supported: zcode_installed,
        config_path: Some(zcode_path),
        route_mode: zcode_route_mode.to_string(),
        status: gateway_client_status(zcode_installed, zcode_route_mode),
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
    let pi_route_mode = pi_route_mode(&pi_paths);
    clients.push(GatewayClientInfo {
        id: "pi".to_string(),
        name: "Pi".to_string(),
        kind: "Compact CLI".to_string(),
        installed: pi_installed,
        auto_apply_supported: pi_installed,
        config_path: Some(pi_paths.settings_path),
        route_mode: pi_route_mode.to_string(),
        status: gateway_client_status(pi_installed, pi_route_mode),
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
    let omp_route_mode = omp_route_mode(&omp_paths);
    clients.push(GatewayClientInfo {
        id: "omp".to_string(),
        name: "OMP".to_string(),
        kind: "Prompt runtime".to_string(),
        installed: omp_installed,
        auto_apply_supported: omp_installed,
        config_path: Some(omp_paths.config_path),
        route_mode: omp_route_mode.to_string(),
        status: gateway_client_status(omp_installed, omp_route_mode),
        current_version: include_versions
            .then(|| command_version(&["omp"]))
            .flatten(),
        latest_version: (include_versions && omp_installed)
            .then(|| npm_latest_version("@oh-my-pi/pi-coding-agent"))
            .flatten(),
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
    if id == "opencode" {
        let path = detect_opencode_config_path()
            .ok_or_else(|| "OpenCode config path could not be resolved".to_string())?;
        return preview_opencode_config_with_path(&path, &settings, &providers, &model);
    }
    if id == "pi" {
        let paths = detect_pi_config_paths();
        return preview_pi_config_with_paths(
            &paths.settings_path,
            &paths.models_path,
            &settings,
            &providers,
            &model,
        );
    }
    if id == "omp" {
        let paths = detect_omp_config_paths();
        return preview_omp_config_with_paths(
            &paths.config_path,
            &paths.models_path,
            &settings,
            &providers,
            &model,
        );
    }
    if id == "zcode" {
        let path = detect_zcode_config_path();
        return preview_zcode_config_with_path(&path, &settings, &providers, &model);
    }
    let config = gateway_copy_client_config(Some(id.clone()), Some(model))?;
    Ok(GatewayClientConfigPreview {
        client_id: id,
        can_apply: false,
        strategy: "copy_only".to_string(),
        config_path: None,
        current_redacted: None,
        next_redacted: config.json,
        backup_required: false,
        message: "Generic and unknown clients are copy-only in this release.".to_string(),
    })
}

pub fn apply_gateway_client_config(
    client_id: String,
    model: Option<String>,
) -> Result<GatewayClientApplyResult, String> {
    let settings = config::get_settings()?;
    let providers = config::get_providers()?;
    let model = model.unwrap_or_else(|| DEFAULT_MODEL.to_string());
    let id = normalize_client_id(&client_id);
    match id.as_str() {
        "opencode" => {
            let path = detect_opencode_config_path()
                .ok_or_else(|| "OpenCode config path could not be resolved".to_string())?;
            apply_opencode_config_with_paths(
                &path,
                &client_backup_root("opencode"),
                &settings,
                &providers,
                &model,
            )
        }
        "pi" => {
            let paths = detect_pi_config_paths();
            apply_pi_config_with_paths(
                &paths.settings_path,
                &paths.models_path,
                &client_backup_root("pi"),
                &settings,
                &providers,
                &model,
            )
        }
        "omp" => {
            let paths = detect_omp_config_paths();
            apply_omp_config_with_paths(
                &paths.config_path,
                &paths.models_path,
                &client_backup_root("omp"),
                &settings,
                &providers,
                &model,
            )
        }
        "zcode" => apply_zcode_config_with_path(
            &detect_zcode_config_path(),
            &client_backup_root("zcode"),
            &settings,
            &providers,
            &model,
        ),
        _ => Ok(GatewayClientApplyResult {
            client_id: id,
            applied: false,
            config_path: None,
            backup_path: None,
            message: "This client is copy-only; no native adapter is registered.".to_string(),
        }),
    }
}

pub fn restore_gateway_client_config(
    client_id: String,
) -> Result<GatewayClientApplyResult, String> {
    let id = normalize_client_id(&client_id);
    match id.as_str() {
        "opencode" => {
            let path = detect_opencode_config_path()
                .ok_or_else(|| "OpenCode config path could not be resolved".to_string())?;
            restore_latest_backup("opencode", &path, &client_backup_root("opencode"))
        }
        "pi" => {
            let paths = detect_pi_config_paths();
            restore_pi_config_with_paths(
                &paths.settings_path,
                &paths.models_path,
                &client_backup_root("pi"),
            )
        }
        "omp" => {
            let paths = detect_omp_config_paths();
            restore_omp_config_with_paths(
                &paths.config_path,
                &paths.models_path,
                &client_backup_root("omp"),
            )
        }
        "zcode" => restore_zcode_config_with_path(
            &detect_zcode_config_path(),
            &client_backup_root("zcode"),
        ),
        _ => Ok(GatewayClientApplyResult {
            client_id: id,
            applied: false,
            config_path: None,
            backup_path: None,
            message: "Restore is not available for this copy-only client.".to_string(),
        }),
    }
}

pub fn switch_gateway_client_route(
    client_id: String,
    mode: String,
    model: Option<String>,
) -> Result<GatewayClientApplyResult, String> {
    if mode == "official" {
        restore_gateway_client_config(client_id)
    } else {
        apply_gateway_client_config(client_id, model)
    }
}

pub fn provider_probe_upstream_format(
    provider_id: String,
    model: Option<String>,
) -> Result<Value, String> {
    let providers = config::get_providers()?;
    let provider = providers
        .iter()
        .find(|candidate| candidate.id == provider_id)
        .ok_or_else(|| format!("provider not found: {provider_id}"))?;
    let probe_model = model.or_else(|| {
        provider
            .models
            .iter()
            .find(|item| item.enabled)
            .or_else(|| provider.models.first())
            .map(|item| {
                item.upstream_model
                    .clone()
                    .unwrap_or_else(|| item.id.clone())
            })
    });
    models::probe_upstream_format(
        &provider.base_url,
        provider.api_key.as_deref().unwrap_or(""),
        probe_model.as_deref(),
    )
}

pub fn subagent_matrix_status() -> Result<SubagentMatrixStatus, String> {
    let status = gateway_status()?;
    let readiness = subagent_readiness(&status.features);
    let recent_events = read_recent_events(20, Some(subagent_event_filter));
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
    body: Value,
    stream: bool,
) -> Result<GatewayTestResult, String> {
    let started = Instant::now();
    let response = client
        .post(endpoint)
        .header("Content-Type", "application/json")
        .body(body.to_string())
        .send();

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

fn read_codex_auth_status() -> CodexAuthStatus {
    let path = codex_home().join("auth.json");
    if !path.exists() {
        return CodexAuthStatus {
            auth_file_present: false,
            logged_in: false,
            auth_mode: None,
            account_id_present: false,
            access_token_present: false,
            refresh_token_present: false,
            token_refresh_status: "missing".to_string(),
            last_refresh: None,
            issue: Some(
                "Codex auth file is missing; log in with Codex CLI or Codex App first.".to_string(),
            ),
        };
    }

    let text = match fs::read_to_string(&path) {
        Ok(text) => text,
        Err(error) => {
            return CodexAuthStatus {
                auth_file_present: true,
                logged_in: false,
                auth_mode: None,
                account_id_present: false,
                access_token_present: false,
                refresh_token_present: false,
                token_refresh_status: "read_error".to_string(),
                last_refresh: None,
                issue: Some(format!("Codex auth file could not be read: {error}")),
            }
        }
    };
    let data: Value = match serde_json::from_str(&text) {
        Ok(data) => data,
        Err(error) => {
            return CodexAuthStatus {
                auth_file_present: true,
                logged_in: false,
                auth_mode: None,
                account_id_present: false,
                access_token_present: false,
                refresh_token_present: false,
                token_refresh_status: "invalid_json".to_string(),
                last_refresh: None,
                issue: Some(format!("Codex auth file is invalid JSON: {error}")),
            }
        }
    };

    let auth_mode = data
        .get("auth_mode")
        .and_then(Value::as_str)
        .map(ToOwned::to_owned);
    let tokens = data.get("tokens").and_then(Value::as_object);
    let access_token_present = tokens
        .and_then(|value| value.get("access_token"))
        .and_then(Value::as_str)
        .map(|value| !value.is_empty())
        .unwrap_or(false);
    let refresh_token_present = tokens
        .and_then(|value| value.get("refresh_token"))
        .and_then(Value::as_str)
        .map(|value| !value.is_empty())
        .unwrap_or(false);
    let account_id_present = tokens
        .and_then(|value| value.get("account_id"))
        .and_then(Value::as_str)
        .map(|value| !value.is_empty())
        .unwrap_or(false);
    let last_refresh = data
        .get("last_refresh")
        .and_then(Value::as_str)
        .map(ToOwned::to_owned);
    let logged_in = auth_mode.as_deref() == Some("chatgpt") && access_token_present;
    let token_refresh_status = if last_refresh.is_some() {
        "last_refresh_recorded"
    } else if refresh_token_present {
        "refresh_token_available"
    } else {
        "unknown"
    }
    .to_string();
    let issue = if auth_mode.as_deref() != Some("chatgpt") {
        Some(
            "Codex auth mode is not chatgpt; Gateway requires local Codex/ChatGPT auth."
                .to_string(),
        )
    } else if !access_token_present {
        Some("Codex auth file has no access token.".to_string())
    } else if !account_id_present {
        Some("Codex auth exists, but account id is missing.".to_string())
    } else {
        None
    };

    CodexAuthStatus {
        auth_file_present: true,
        logged_in,
        auth_mode,
        account_id_present,
        access_token_present,
        refresh_token_present,
        token_refresh_status,
        last_refresh,
        issue,
    }
}

fn codex_home() -> PathBuf {
    std::env::var_os("CODEX_HOME")
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
        .or_else(|| dirs::home_dir().map(|home| home.join(".codex")))
        .unwrap_or_else(|| PathBuf::from(".codex"))
}

fn official_models(settings: &Settings) -> Vec<GatewayModel> {
    let mut models: Vec<GatewayModel> = OFFICIAL_MODELS
        .iter()
        .filter(|(id, _, _)| !official_model_disabled(settings, id))
        .map(|(id, display_name, context_window)| GatewayModel {
            id: (*id).to_string(),
            display_name: (*display_name).to_string(),
            source: "Official Codex subscription".to_string(),
            source_kind: "official".to_string(),
            supports_responses: true,
            supports_chat_completions: true,
            context_window: *context_window,
        })
        .collect();

    for (base_id, id, display_name, context_window) in OFFICIAL_FAST_VARIANTS {
        if official_model_disabled(settings, base_id) {
            continue;
        }
        if settings
            .gateway_fast_model_variants
            .iter()
            .any(|value| value == base_id)
        {
            models.push(GatewayModel {
                id: (*id).to_string(),
                display_name: (*display_name).to_string(),
                source: "Official Codex subscription".to_string(),
                source_kind: "official".to_string(),
                supports_responses: true,
                supports_chat_completions: true,
                context_window: *context_window,
            });
        }
    }

    models
}

fn official_model_disabled(settings: &Settings, id: &str) -> bool {
    let without_prefix = id.strip_prefix("openai/").unwrap_or(id);
    settings.official_disabled_models.iter().any(|value| {
        value == id
            || value == without_prefix
            || value.strip_prefix("openai/").unwrap_or(value) == without_prefix
    })
}

fn gateway_models_from_config(settings: &Settings, providers: &[Provider]) -> Vec<GatewayModel> {
    let mut output = Vec::new();
    if settings.include_official_models {
        output.extend(official_models(settings));
    }
    for provider in providers {
        if !provider.enabled {
            continue;
        }
        for model in &provider.models {
            if !model.enabled || !model.gateway_exported {
                continue;
            }
            let model_id = provider_qualified_model_id(&provider.id, &model.id);
            output.push(GatewayModel {
                id: model_id.clone(),
                display_name: model
                    .display_name
                    .clone()
                    .unwrap_or_else(|| model_id.clone()),
                source: provider.name.clone(),
                source_kind: "external".to_string(),
                supports_responses: provider
                    .upstream_format
                    .as_ref()
                    .map(|format| !matches!(format, crate::UpstreamFormat::ChatCompletions))
                    .unwrap_or(true),
                supports_chat_completions: true,
                context_window: model
                    .context_window
                    .unwrap_or_else(|| gateway_model_context_window(&model_id)),
            });
        }
    }
    output
}

fn provider_qualified_model_id(provider_id: &str, model_id: &str) -> String {
    let provider_id = provider_id.trim();
    let model_id = model_id.trim();
    if provider_id.is_empty()
        || model_id.is_empty()
        || model_id.starts_with(&format!("{provider_id}/"))
    {
        return model_id.to_string();
    }
    format!("{provider_id}/{model_id}")
}

fn gateway_client_models(
    settings: &Settings,
    providers: &[Provider],
    default_model: &str,
) -> Vec<GatewayModel> {
    let default_model = default_model.trim();
    let mut seen = HashSet::new();
    let mut output = Vec::new();
    for model in gateway_models_from_config(settings, providers) {
        if seen.insert(model.id.clone()) {
            output.push(model);
        }
    }

    if !default_model.is_empty() && !seen.contains(default_model) {
        output.insert(
            0,
            GatewayModel {
                id: default_model.to_string(),
                display_name: gateway_model_display_name(default_model),
                source: "Gateway default".to_string(),
                source_kind: "default".to_string(),
                supports_responses: true,
                supports_chat_completions: true,
                context_window: gateway_model_context_window(default_model),
            },
        );
    }
    output
}

fn gateway_diagnostics(
    proxy_running: bool,
    has_chat_completions_gateway: bool,
    auth: &CodexAuthStatus,
) -> Vec<GatewayDiagnostic> {
    let mut diagnostics = Vec::new();
    if !proxy_running {
        diagnostics.push(GatewayDiagnostic {
            level: "error".to_string(),
            category: "proxy".to_string(),
            message: "Proxy is not running; Gateway endpoints are unavailable.".to_string(),
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

fn read_recent_events(
    limit: usize,
    filter: Option<fn(&GatewayEvent) -> bool>,
) -> Vec<GatewayEvent> {
    let text = match read_event_log_text() {
        Ok(text) => text,
        Err(_) => return Vec::new(),
    };

    let mut events = Vec::new();
    for line in text.lines().rev() {
        if events.len() >= limit {
            break;
        }
        let Ok(value) = serde_json::from_str::<Value>(line) else {
            continue;
        };
        let event = sanitize_event(&value);
        if filter.map(|predicate| predicate(&event)).unwrap_or(true) {
            events.push(event);
        }
    }
    events.reverse();
    events
}

fn event_log_path() -> PathBuf {
    codex_home().join("proxy").join("codex-proxy-events.jsonl")
}

fn telemetry_db_path() -> PathBuf {
    codex_home()
        .join("proxy")
        .join("codex-proxy-telemetry.sqlite")
}

fn ensure_telemetry_sqlite_ready(path: &Path) -> Result<(), String> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|error| {
            format!(
                "failed to create telemetry directory {}: {error}",
                parent.display()
            )
        })?;
    }
    let connection = open_telemetry_connection(path)?;
    initialize_telemetry_db(&connection)?;
    let event_log_path = event_log_path();
    let current_event_log_size = fs::metadata(&event_log_path)
        .map(|metadata| metadata.len())
        .unwrap_or(0);
    let backfilled = telemetry_meta_value(&connection, "last_backfill_at")?.is_some();
    let last_backfill_size = telemetry_meta_value(&connection, "last_backfill_size")?
        .and_then(|value| value.parse::<u64>().ok());
    drop(connection);

    if !backfilled || last_backfill_size != Some(current_event_log_size) {
        backfill_event_log_to_sqlite_path(&event_log_path, path)?;
    }
    Ok(())
}

fn open_telemetry_connection(path: &Path) -> Result<Connection, String> {
    let connection = Connection::open(path).map_err(|error| {
        format!(
            "failed to open telemetry sqlite {}: {error}",
            path.display()
        )
    })?;
    connection
        .busy_timeout(Duration::from_millis(5000))
        .map_err(|error| format!("failed to configure telemetry sqlite busy timeout: {error}"))?;
    Ok(connection)
}

fn initialize_telemetry_db(connection: &Connection) -> Result<(), String> {
    connection
        .execute_batch(
            r#"
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS gateway_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_hash TEXT NOT NULL UNIQUE,
                ts TEXT NOT NULL,
                event TEXT NOT NULL,
                request_id TEXT,
                payload_json TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS gateway_requests (
                request_id TEXT PRIMARY KEY,
                schema_version INTEGER,
                first_ts TEXT,
                completed_ts TEXT,
                method TEXT,
                path TEXT,
                status INTEGER,
                duration_ms INTEGER,
                is_stream INTEGER,
                content_length INTEGER,
                decoded_content_length INTEGER,
                content_type TEXT,
                content_encoding TEXT,
                content_decoded INTEGER,
                client_id TEXT,
                client_inference_source TEXT,
                user_agent_hash TEXT,
                thread_id TEXT,
                session_id TEXT,
                window_id TEXT,
                turn_id TEXT,
                request_kind TEXT,
                thread_source TEXT,
                route_mode TEXT,
                route_reason TEXT,
                provider_id TEXT,
                upstream TEXT,
                upstream_format TEXT,
                inbound_format TEXT,
                model TEXT,
                model_requested TEXT,
                model_canonical TEXT,
                provider_config_hash TEXT,
                request_body_hmac TEXT,
                request_prefix_hmac TEXT,
                prefix_bytes INTEGER,
                prompt_cache_key_hash TEXT,
                usage_source TEXT,
                usage_missing_reason TEXT,
                usage_input_tokens INTEGER,
                usage_cached_input_tokens INTEGER,
                usage_output_tokens INTEGER,
                usage_total_tokens INTEGER,
                usage_reasoning_tokens INTEGER,
                payload_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS telemetry_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            "#,
        )
        .map_err(|error| format!("failed to initialize telemetry sqlite schema: {error}"))?;
    ensure_gateway_request_columns(connection)?;
    connection
        .execute_batch(
            r#"
            CREATE INDEX IF NOT EXISTS idx_gateway_requests_completed_ts ON gateway_requests(completed_ts);
            CREATE INDEX IF NOT EXISTS idx_gateway_requests_provider_model ON gateway_requests(provider_id, model_canonical);
            CREATE INDEX IF NOT EXISTS idx_gateway_requests_window ON gateway_requests(window_id);
            "#,
        )
        .map_err(|error| format!("failed to initialize telemetry sqlite indexes: {error}"))?;
    Ok(())
}

fn ensure_gateway_request_columns(connection: &Connection) -> Result<(), String> {
    let mut statement = connection
        .prepare("PRAGMA table_info(gateway_requests)")
        .map_err(|error| format!("failed to inspect telemetry request columns: {error}"))?;
    let rows = statement
        .query_map([], |row| row.get::<_, String>(1))
        .map_err(|error| format!("failed to read telemetry request columns: {error}"))?;
    let mut existing = HashSet::new();
    for row in rows {
        existing.insert(row.map_err(|error| {
            format!("failed to decode telemetry request column metadata: {error}")
        })?);
    }
    for (name, column_type) in gateway_request_column_defs() {
        if existing.contains(*name) {
            continue;
        }
        connection
            .execute(
                &format!("ALTER TABLE gateway_requests ADD COLUMN {name} {column_type}"),
                [],
            )
            .map_err(|error| format!("failed to add telemetry request column {name}: {error}"))?;
    }
    Ok(())
}

fn gateway_request_column_defs() -> &'static [(&'static str, &'static str)] {
    &[
        ("schema_version", "INTEGER"),
        ("first_ts", "TEXT"),
        ("completed_ts", "TEXT"),
        ("method", "TEXT"),
        ("path", "TEXT"),
        ("status", "INTEGER"),
        ("duration_ms", "INTEGER"),
        ("is_stream", "INTEGER"),
        ("content_length", "INTEGER"),
        ("decoded_content_length", "INTEGER"),
        ("content_type", "TEXT"),
        ("content_encoding", "TEXT"),
        ("content_decoded", "INTEGER"),
        ("client_id", "TEXT"),
        ("client_inference_source", "TEXT"),
        ("user_agent_hash", "TEXT"),
        ("thread_id", "TEXT"),
        ("session_id", "TEXT"),
        ("window_id", "TEXT"),
        ("turn_id", "TEXT"),
        ("request_kind", "TEXT"),
        ("thread_source", "TEXT"),
        ("route_mode", "TEXT"),
        ("route_reason", "TEXT"),
        ("provider_id", "TEXT"),
        ("upstream", "TEXT"),
        ("upstream_format", "TEXT"),
        ("inbound_format", "TEXT"),
        ("model", "TEXT"),
        ("model_requested", "TEXT"),
        ("model_canonical", "TEXT"),
        ("provider_config_hash", "TEXT"),
        ("request_body_hmac", "TEXT"),
        ("request_prefix_hmac", "TEXT"),
        ("prefix_bytes", "INTEGER"),
        ("prompt_cache_key_hash", "TEXT"),
        ("usage_source", "TEXT"),
        ("usage_missing_reason", "TEXT"),
        ("usage_input_tokens", "INTEGER"),
        ("usage_cached_input_tokens", "INTEGER"),
        ("usage_output_tokens", "INTEGER"),
        ("usage_total_tokens", "INTEGER"),
        ("usage_reasoning_tokens", "INTEGER"),
        ("payload_json", "TEXT"),
        ("created_at", "TEXT NOT NULL DEFAULT ''"),
        ("updated_at", "TEXT NOT NULL DEFAULT ''"),
    ]
}

fn telemetry_meta_value(connection: &Connection, key: &str) -> Result<Option<String>, String> {
    connection
        .query_row(
            "SELECT value FROM telemetry_meta WHERE key = ?",
            params![key],
            |row| row.get::<_, String>(0),
        )
        .optional()
        .map_err(|error| format!("failed to read telemetry meta {key}: {error}"))
}

fn backfill_event_log_to_sqlite_path(event_path: &Path, db_path: &Path) -> Result<(), String> {
    let connection = open_telemetry_connection(db_path)?;
    initialize_telemetry_db(&connection)?;
    if event_path.exists() {
        let text = fs::read_to_string(event_path).map_err(|error| {
            format!(
                "failed to read event log for telemetry backfill {}: {error}",
                event_path.display()
            )
        })?;
        for line in text.lines() {
            let trimmed = line.trim();
            if !trimmed.starts_with('{') {
                continue;
            }
            let Ok(mut value) = serde_json::from_str::<Value>(trimmed) else {
                continue;
            };
            sanitize_json_value(&mut value);
            if let Value::Object(object) = &mut value {
                object
                    .entry("schema_version".to_string())
                    .or_insert_with(|| Value::Number(2.into()));
            }
            write_json_event_to_sqlite(&connection, &value)?;
        }
    }
    connection
        .execute(
            "INSERT INTO telemetry_meta (key, value) VALUES ('last_backfill_at', ?) \
             ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            params![telemetry_now_marker()],
        )
        .map_err(|error| format!("failed to update telemetry backfill marker: {error}"))?;
    let event_log_size = fs::metadata(event_path)
        .map(|metadata| metadata.len())
        .unwrap_or(0)
        .to_string();
    connection
        .execute(
            "INSERT INTO telemetry_meta (key, value) VALUES ('last_backfill_size', ?) \
             ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            params![event_log_size],
        )
        .map_err(|error| format!("failed to update telemetry backfill size marker: {error}"))?;
    Ok(())
}

fn write_json_event_to_sqlite(connection: &Connection, value: &Value) -> Result<(), String> {
    let payload_json = canonical_json(value)
        .map_err(|error| format!("failed to encode telemetry event payload: {error}"))?;
    let event_hash = stable_event_hash(value, &payload_json);
    connection
        .execute(
            "INSERT OR IGNORE INTO gateway_events (event_hash, ts, event, request_id, payload_json) \
             VALUES (?, ?, ?, ?, ?)",
            params![
                event_hash,
                string_field(value, "ts").unwrap_or_default(),
                string_field(value, "event").unwrap_or_default(),
                string_field(value, "request_id"),
                payload_json,
            ],
        )
        .map_err(|error| format!("failed to write telemetry event: {error}"))?;
    upsert_gateway_request_from_event(connection, value, &payload_json)
}

fn canonical_json(value: &Value) -> Result<String, serde_json::Error> {
    serde_json::to_string(&canonical_json_value(value))
}

fn canonical_json_value(value: &Value) -> Value {
    match value {
        Value::Object(object) => {
            let mut keys: Vec<&String> = object.keys().collect();
            keys.sort();
            let mut sorted = serde_json::Map::new();
            for key in keys {
                if let Some(item) = object.get(key) {
                    sorted.insert(key.clone(), canonical_json_value(item));
                }
            }
            Value::Object(sorted)
        }
        Value::Array(items) => Value::Array(items.iter().map(canonical_json_value).collect()),
        _ => value.clone(),
    }
}

fn stable_event_hash(_value: &Value, payload_json: &str) -> String {
    let mut hasher = Sha256::new();
    hasher.update(payload_json.as_bytes());
    format!("{:x}", hasher.finalize())
}

fn upsert_gateway_request_from_event(
    connection: &Connection,
    value: &Value,
    payload_json: &str,
) -> Result<(), String> {
    let event = value
        .get("event")
        .and_then(Value::as_str)
        .unwrap_or_default();
    if event != "request_start" && event != "request_complete" && event != "request_error" {
        return Ok(());
    }
    let Some(request_id) = string_field(value, "request_id") else {
        return Ok(());
    };
    let now = telemetry_now_marker();
    connection
        .execute(
            "INSERT OR IGNORE INTO gateway_requests (request_id, created_at, updated_at) VALUES (?, ?, ?)",
            params![request_id, now, now],
        )
        .map_err(|error| format!("failed to create telemetry request row: {error}"))?;

    let first_ts = if event == "request_start" {
        string_field(value, "ts")
    } else {
        None
    };
    let completed_ts = if event == "request_complete" || event == "request_error" {
        string_field(value, "ts")
    } else {
        None
    };
    let upstream = string_field(value, "upstream");
    let model = string_field(value, "model");
    let provider_id = string_field(value, "provider_id").or_else(|| upstream.clone());
    let model_canonical = string_field(value, "model_canonical").or_else(|| model.clone());
    let model_requested = string_field(value, "model_requested").or_else(|| model.clone());
    let route_mode =
        string_field(value, "route_mode").or_else(|| route_mode_from_upstream(upstream.as_deref()));

    connection
        .execute(
            r#"
            UPDATE gateway_requests SET
                schema_version = COALESCE(?, schema_version),
                first_ts = COALESCE(?, first_ts),
                completed_ts = COALESCE(?, completed_ts),
                method = COALESCE(?, method),
                path = COALESCE(?, path),
                status = COALESCE(?, status),
                duration_ms = COALESCE(?, duration_ms),
                is_stream = COALESCE(?, is_stream),
                content_length = COALESCE(?, content_length),
                decoded_content_length = COALESCE(?, decoded_content_length),
                content_type = COALESCE(?, content_type),
                content_encoding = COALESCE(?, content_encoding),
                content_decoded = COALESCE(?, content_decoded),
                client_id = COALESCE(NULLIF(?, 'unknown'), client_id),
                client_inference_source = COALESCE(NULLIF(?, 'unknown'), client_inference_source),
                user_agent_hash = COALESCE(?, user_agent_hash),
                thread_id = COALESCE(?, thread_id),
                session_id = COALESCE(?, session_id),
                window_id = COALESCE(?, window_id),
                turn_id = COALESCE(?, turn_id),
                request_kind = COALESCE(?, request_kind),
                thread_source = COALESCE(?, thread_source),
                route_mode = COALESCE(?, route_mode),
                route_reason = COALESCE(?, route_reason),
                provider_id = COALESCE(?, provider_id),
                upstream = COALESCE(?, upstream),
                upstream_format = COALESCE(?, upstream_format),
                inbound_format = COALESCE(?, inbound_format),
                model = COALESCE(?, model),
                model_requested = COALESCE(?, model_requested),
                model_canonical = COALESCE(?, model_canonical),
                provider_config_hash = COALESCE(?, provider_config_hash),
                request_body_hmac = COALESCE(?, request_body_hmac),
                request_prefix_hmac = COALESCE(?, request_prefix_hmac),
                prefix_bytes = COALESCE(?, prefix_bytes),
                prompt_cache_key_hash = COALESCE(?, prompt_cache_key_hash),
                usage_source = COALESCE(?, usage_source),
                usage_missing_reason = COALESCE(?, usage_missing_reason),
                usage_input_tokens = COALESCE(?, usage_input_tokens),
                usage_cached_input_tokens = COALESCE(?, usage_cached_input_tokens),
                usage_output_tokens = COALESCE(?, usage_output_tokens),
                usage_total_tokens = COALESCE(?, usage_total_tokens),
                usage_reasoning_tokens = COALESCE(?, usage_reasoning_tokens),
                payload_json = ?,
                updated_at = ?
            WHERE request_id = ?
            "#,
            params![
                value
                    .get("schema_version")
                    .and_then(Value::as_i64)
                    .or(Some(2)),
                first_ts,
                completed_ts,
                string_field(value, "method"),
                string_field(value, "path"),
                value.get("status").and_then(Value::as_i64),
                value.get("duration_ms").and_then(Value::as_i64),
                bool_or_i64_field(value, "is_stream"),
                value.get("content_length").and_then(Value::as_i64),
                value.get("decoded_content_length").and_then(Value::as_i64),
                string_field(value, "content_type"),
                string_field(value, "content_encoding"),
                bool_or_i64_field(value, "content_decoded"),
                string_field(value, "client_id").unwrap_or_else(|| "unknown".to_string()),
                string_field(value, "client_inference_source")
                    .unwrap_or_else(|| "unknown".to_string()),
                string_field(value, "user_agent_hash"),
                string_field(value, "thread_id"),
                string_field(value, "session_id"),
                string_field(value, "window_id"),
                string_field(value, "turn_id"),
                string_field(value, "request_kind"),
                string_field(value, "thread_source"),
                route_mode,
                string_field(value, "route_reason"),
                provider_id,
                upstream,
                string_field(value, "upstream_format"),
                string_field(value, "inbound_format"),
                model,
                model_requested,
                model_canonical,
                string_field(value, "provider_config_hash"),
                string_field(value, "request_body_hmac"),
                string_field(value, "request_prefix_hmac"),
                value.get("prefix_bytes").and_then(Value::as_i64),
                string_field(value, "prompt_cache_key_hash"),
                string_field(value, "usage_source"),
                string_field(value, "usage_missing_reason"),
                value.get("usage_input_tokens").and_then(Value::as_i64),
                value
                    .get("usage_cached_input_tokens")
                    .and_then(Value::as_i64),
                value.get("usage_output_tokens").and_then(Value::as_i64),
                value.get("usage_total_tokens").and_then(Value::as_i64),
                value.get("usage_reasoning_tokens").and_then(Value::as_i64),
                payload_json,
                now,
                request_id,
            ],
        )
        .map_err(|error| format!("failed to update telemetry request row: {error}"))?;
    Ok(())
}

fn read_usage_summary_from_sqlite_path_with_pricing(
    path: &Path,
    pricing: &HashMap<String, UsagePricing>,
) -> Result<GatewayUsageSummary, String> {
    let events = read_usage_events_from_sqlite_path(path, usize::MAX)?;
    Ok(read_usage_summary_from_events_with_pricing(
        &events, pricing,
    ))
}

fn read_usage_events_from_sqlite_path(
    path: &Path,
    limit: usize,
) -> Result<Vec<GatewayUsageEvent>, String> {
    let connection = open_telemetry_connection(path)?;
    initialize_telemetry_db(&connection)?;
    let limit = if limit == usize::MAX {
        i64::MAX
    } else {
        limit.max(1) as i64
    };
    let mut statement = connection
        .prepare(
            r#"
            SELECT
                completed_ts,
                request_id,
                COALESCE(model_canonical, model, model_requested) AS model,
                COALESCE(provider_id, upstream) AS upstream,
                status,
                duration_ms,
                COALESCE(usage_source, 'missing') AS usage_source,
                usage_missing_reason,
                usage_input_tokens,
                usage_output_tokens,
                usage_total_tokens,
                usage_cached_input_tokens,
                usage_reasoning_tokens
            FROM gateway_requests
            WHERE completed_ts IS NOT NULL
              AND COALESCE(provider_id, upstream, '') != 'local'
              AND COALESCE(route_reason, '') NOT IN (
                  'official_control',
                  'local_responses_probe',
                  'local_responses_websocket_fast_reject'
              )
              AND (
                  path LIKE '/v1/responses%'
                  OR path LIKE '/v1/chat/completions%'
                  OR inbound_format IN ('responses', 'chat_completions')
                  OR usage_input_tokens IS NOT NULL
                  OR usage_output_tokens IS NOT NULL
                  OR usage_total_tokens IS NOT NULL
              )
              AND (
                  status IS NULL
                  OR status < 400
                  OR usage_input_tokens IS NOT NULL
                  OR usage_output_tokens IS NOT NULL
                  OR usage_total_tokens IS NOT NULL
              )
            ORDER BY completed_ts DESC
            LIMIT ?
            "#,
        )
        .map_err(|error| format!("failed to prepare telemetry usage query: {error}"))?;
    let rows = statement
        .query_map(params![limit], |row| {
            Ok(GatewayUsageEvent {
                ts: row.get(0)?,
                request_id: row.get(1)?,
                model: row.get(2)?,
                upstream: row.get(3)?,
                status: row.get(4)?,
                duration_ms: row.get(5)?,
                usage_source: row
                    .get::<_, Option<String>>(6)?
                    .unwrap_or_else(|| "missing".to_string()),
                usage_missing_reason: row.get(7)?,
                input_tokens: optional_i64_to_u64(row.get::<_, Option<i64>>(8)?),
                output_tokens: optional_i64_to_u64(row.get::<_, Option<i64>>(9)?),
                total_tokens: optional_i64_to_u64(row.get::<_, Option<i64>>(10)?),
                cached_input_tokens: optional_i64_to_u64(row.get::<_, Option<i64>>(11)?),
                reasoning_tokens: optional_i64_to_u64(row.get::<_, Option<i64>>(12)?),
            })
        })
        .map_err(|error| format!("failed to read telemetry usage rows: {error}"))?;
    let mut events = Vec::new();
    for row in rows {
        events.push(row.map_err(|error| format!("failed to decode telemetry usage row: {error}"))?);
    }
    events.reverse();
    Ok(events)
}

fn read_event_log_text() -> Result<String, String> {
    let path = event_log_path();
    match fs::metadata(&path).and_then(|metadata| {
        if metadata.len() > EVENT_READ_LIMIT_BYTES {
            let file = fs::File::open(&path)?;
            let start = metadata.len().saturating_sub(EVENT_READ_LIMIT_BYTES);
            let mut reader = std::io::BufReader::new(file);
            use std::io::Seek;
            reader.seek(std::io::SeekFrom::Start(start))?;
            let mut text = String::new();
            reader.read_to_string(&mut text)?;
            Ok(text)
        } else {
            fs::read_to_string(&path)
        }
    }) {
        Ok(text) => Ok(text),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(String::new()),
        Err(error) => Err(format!(
            "failed to read event log {}: {error}",
            path.display()
        )),
    }
}

#[cfg(test)]
fn read_usage_summary_from_text(text: &str) -> GatewayUsageSummary {
    let pricing = usage_pricing_by_model();
    read_usage_summary_from_text_with_pricing(text, &pricing)
}

#[cfg(test)]
fn read_usage_summary_from_text_with_pricing(
    text: &str,
    pricing: &HashMap<String, UsagePricing>,
) -> GatewayUsageSummary {
    let events = read_usage_events_from_text(text, usize::MAX);
    read_usage_summary_from_events_with_pricing(&events, pricing)
}

fn read_usage_summary_from_events_with_pricing(
    events: &[GatewayUsageEvent],
    pricing: &HashMap<String, UsagePricing>,
) -> GatewayUsageSummary {
    let requests = events.len() as u64;
    let successful_requests = events
        .iter()
        .filter(|event| {
            event
                .status
                .map(|status| (200..300).contains(&status))
                .unwrap_or(false)
        })
        .count() as u64;
    let missing_usage_requests = events
        .iter()
        .filter(|event| event.usage_source == "missing")
        .count() as u64;
    let input_tokens = sum_optional(events.iter().map(|event| event.input_tokens));
    let output_tokens = sum_optional(events.iter().map(|event| event.output_tokens));
    let total_tokens =
        sum_optional(events.iter().map(|event| event.total_tokens)).or_else(|| {
            match (input_tokens, output_tokens) {
                (Some(input), Some(output)) => Some(input + output),
                _ => None,
            }
        });
    let cached_input_tokens = sum_optional(events.iter().map(|event| event.cached_input_tokens));
    let mut cache_known_input_tokens = 0_u64;
    let mut cache_known_cached_tokens = 0_u64;
    for event in events {
        if let (Some(input), Some(cached)) = (event.input_tokens, event.cached_input_tokens) {
            if input > 0 {
                cache_known_input_tokens = cache_known_input_tokens.saturating_add(input);
                cache_known_cached_tokens = cache_known_cached_tokens.saturating_add(cached);
            }
        }
    }
    let cache_hit_rate = if cache_known_input_tokens > 0 {
        Some(
            ((cache_known_cached_tokens as f64 / cache_known_input_tokens as f64) * 1000.0).round()
                / 10.0,
        )
    } else {
        None
    };
    let cost = estimate_usage_cost(&events, pricing);

    GatewayUsageSummary {
        requests,
        successful_requests,
        missing_usage_requests,
        total_tokens,
        input_tokens,
        output_tokens,
        cached_input_tokens,
        cache_hit_rate,
        estimated_cost_usd: cost.estimated_cost_usd,
        cost_label: cost.label,
    }
}

fn optional_i64_to_u64(value: Option<i64>) -> Option<u64> {
    value.and_then(|item| u64::try_from(item).ok())
}

fn bool_or_i64_field(value: &Value, key: &str) -> Option<i64> {
    match value.get(key) {
        Some(Value::Bool(item)) => Some(if *item { 1 } else { 0 }),
        Some(Value::Number(item)) => item.as_i64(),
        _ => None,
    }
}

fn route_mode_from_upstream(upstream: Option<&str>) -> Option<String> {
    match upstream {
        Some("official") => Some("official".to_string()),
        Some("local") => Some("local".to_string()),
        Some(_) => Some("codexhub".to_string()),
        None => None,
    }
}

fn telemetry_now_marker() -> String {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_secs().to_string())
        .unwrap_or_else(|_| "0".to_string())
}

fn sanitize_json_value(value: &mut Value) {
    match value {
        Value::Object(object) => {
            let keys: Vec<String> = object.keys().cloned().collect();
            for key in keys {
                if is_sensitive_json_key(&key) {
                    object.remove(&key);
                    continue;
                }
                if let Some(child) = object.get_mut(&key) {
                    sanitize_json_value(child);
                }
            }
        }
        Value::Array(items) => {
            for item in items {
                sanitize_json_value(item);
            }
        }
        _ => {}
    }
}

fn is_sensitive_json_key(key: &str) -> bool {
    matches!(
        key.trim().to_ascii_lowercase().as_str(),
        "authorization"
            | "proxy-authorization"
            | "cookie"
            | "set-cookie"
            | "api-key"
            | "api_key"
            | "apikey"
            | "x-api-key"
            | "openai-api-key"
    )
}

#[derive(Debug, Clone)]
struct UsageCostEstimate {
    estimated_cost_usd: Option<f64>,
    label: String,
}

fn estimate_usage_cost(
    events: &[GatewayUsageEvent],
    pricing: &HashMap<String, UsagePricing>,
) -> UsageCostEstimate {
    let mut estimated_cost_usd = 0.0_f64;
    let mut priced_requests = 0_u64;
    let mut missing_usage_requests = 0_u64;
    let mut missing_pricing_requests = 0_u64;
    let mut cached_priced_as_input_requests = 0_u64;

    for event in events {
        let input_tokens = event.input_tokens.unwrap_or(0);
        let output_tokens = event.output_tokens.unwrap_or(0);
        if input_tokens == 0 && output_tokens == 0 {
            if event.usage_source == "missing" {
                missing_usage_requests = missing_usage_requests.saturating_add(1);
            }
            continue;
        }

        let Some(model) = event.model.as_deref().and_then(non_empty_str) else {
            missing_pricing_requests = missing_pricing_requests.saturating_add(1);
            continue;
        };
        let Some(model_pricing) = lookup_usage_pricing(pricing, model) else {
            missing_pricing_requests = missing_pricing_requests.saturating_add(1);
            continue;
        };

        let cached_tokens = event.cached_input_tokens.unwrap_or(0).min(input_tokens);
        let uncached_tokens = input_tokens.saturating_sub(cached_tokens);
        let cached_rate = match model_pricing.cached_input_per_million {
            Some(value) => value,
            None => {
                if cached_tokens > 0 {
                    cached_priced_as_input_requests =
                        cached_priced_as_input_requests.saturating_add(1);
                }
                model_pricing.input_per_million
            }
        };

        estimated_cost_usd += (uncached_tokens as f64 * model_pricing.input_per_million
            + cached_tokens as f64 * cached_rate
            + output_tokens as f64 * model_pricing.output_per_million)
            / 1_000_000.0;
        priced_requests = priced_requests.saturating_add(1);
    }

    if priced_requests == 0 {
        return UsageCostEstimate {
            estimated_cost_usd: None,
            label: "Unknown until token usage and USD pricing metadata are available".to_string(),
        };
    }

    let mut label_parts = vec!["Estimated from configured USD pricing metadata".to_string()];
    if cached_priced_as_input_requests > 0 {
        label_parts.push(format!(
            "{cached_priced_as_input_requests} requests used input pricing for cached tokens"
        ));
    }
    if missing_pricing_requests > 0 {
        label_parts.push(format!(
            "{missing_pricing_requests} requests missing model pricing"
        ));
    }
    if missing_usage_requests > 0 {
        label_parts.push(format!(
            "{missing_usage_requests} requests missing token usage"
        ));
    }

    UsageCostEstimate {
        estimated_cost_usd: Some(estimated_cost_usd),
        label: label_parts.join("; "),
    }
}

fn usage_pricing_by_model() -> HashMap<String, UsagePricing> {
    let mut pricing_by_model = HashMap::new();
    let Ok(models) = models::list_model_metadata() else {
        return pricing_by_model;
    };

    for model in models {
        let Some(pricing) = model.pricing else {
            continue;
        };
        if !pricing.currency.eq_ignore_ascii_case("usd") {
            continue;
        }
        let (Some(input_per_million), Some(output_per_million)) =
            (pricing.input_per_million, pricing.output_per_million)
        else {
            continue;
        };
        let usage_pricing = UsagePricing {
            input_per_million,
            cached_input_per_million: pricing.cached_input_per_million,
            output_per_million,
        };
        insert_usage_pricing_aliases(&mut pricing_by_model, &model.id, usage_pricing);
        if let Some(upstream_model) = model.upstream_model {
            insert_usage_pricing_aliases(&mut pricing_by_model, &upstream_model, usage_pricing);
        }
    }

    insert_fast_usage_pricing_aliases(&mut pricing_by_model);
    pricing_by_model
}

fn insert_fast_usage_pricing_aliases(pricing_by_model: &mut HashMap<String, UsagePricing>) {
    for (fast_id, input, cached, output) in OFFICIAL_FAST_PRICING {
        let pricing = UsagePricing {
            input_per_million: *input,
            cached_input_per_million: Some(*cached),
            output_per_million: *output,
        };
        insert_usage_pricing_aliases(pricing_by_model, fast_id, pricing);
    }
}

fn lookup_usage_pricing(
    pricing_by_model: &HashMap<String, UsagePricing>,
    model: &str,
) -> Option<UsagePricing> {
    usage_pricing_aliases(model).find_map(|alias| pricing_by_model.get(&alias).copied())
}

fn insert_usage_pricing_aliases(
    pricing_by_model: &mut HashMap<String, UsagePricing>,
    model: &str,
    pricing: UsagePricing,
) {
    for alias in usage_pricing_aliases(model) {
        pricing_by_model.entry(alias).or_insert(pricing);
    }
}

fn usage_pricing_aliases(model: &str) -> impl Iterator<Item = String> + '_ {
    let trimmed = model.trim();
    let without_codexhub = trimmed.strip_prefix("codexhub/").unwrap_or(trimmed);
    let without_openai = without_codexhub
        .strip_prefix("openai/")
        .unwrap_or(without_codexhub);
    [trimmed, without_codexhub, without_openai]
        .into_iter()
        .filter_map(non_empty_str)
        .map(ToOwned::to_owned)
}

fn non_empty_str(value: &str) -> Option<&str> {
    let trimmed = value.trim();
    if trimmed.is_empty() {
        None
    } else {
        Some(trimmed)
    }
}

#[cfg(test)]
fn read_usage_events_from_text(text: &str, limit: usize) -> Vec<GatewayUsageEvent> {
    let mut events = Vec::new();
    for line in text.lines().rev() {
        if events.len() >= limit {
            break;
        }
        let Ok(value) = serde_json::from_str::<Value>(line) else {
            continue;
        };
        if !is_usage_request_complete_event(&value) {
            continue;
        }
        events.push(GatewayUsageEvent {
            ts: string_field(&value, "ts"),
            request_id: string_field(&value, "request_id"),
            model: string_field(&value, "model"),
            upstream: string_field(&value, "upstream"),
            status: value.get("status").and_then(Value::as_i64),
            duration_ms: value.get("duration_ms").and_then(Value::as_i64),
            usage_source: string_field(&value, "usage_source")
                .unwrap_or_else(|| "missing".to_string()),
            usage_missing_reason: string_field(&value, "usage_missing_reason"),
            input_tokens: value.get("usage_input_tokens").and_then(Value::as_u64),
            output_tokens: value.get("usage_output_tokens").and_then(Value::as_u64),
            total_tokens: value.get("usage_total_tokens").and_then(Value::as_u64),
            cached_input_tokens: value
                .get("usage_cached_input_tokens")
                .and_then(Value::as_u64),
            reasoning_tokens: value.get("usage_reasoning_tokens").and_then(Value::as_u64),
        });
    }
    events.reverse();
    events
}

#[cfg(test)]
fn is_usage_request_complete_event(value: &Value) -> bool {
    if value.get("event").and_then(Value::as_str) != Some("request_complete") {
        return false;
    }
    value.get("upstream").and_then(Value::as_str) != Some("local")
}

fn sum_optional(values: impl Iterator<Item = Option<u64>>) -> Option<u64> {
    let mut seen = false;
    let mut total = 0_u64;
    for value in values.flatten() {
        seen = true;
        total = total.saturating_add(value);
    }
    seen.then_some(total)
}

fn sanitize_event(value: &Value) -> GatewayEvent {
    GatewayEvent {
        ts: string_field(value, "ts"),
        event: string_field(value, "event"),
        request_id: string_field(value, "request_id"),
        path: string_field(value, "path"),
        method: string_field(value, "method"),
        model: string_field(value, "model"),
        upstream: string_field(value, "upstream"),
        upstream_format: string_field(value, "upstream_format"),
        inbound_format: string_field(value, "inbound_format"),
        route_reason: string_field(value, "route_reason"),
        status: value.get("status").and_then(Value::as_i64),
        duration_ms: value.get("duration_ms").and_then(Value::as_i64),
        error: string_field(value, "error"),
        detail: string_field(value, "detail").map(|detail| sanitize_text(&detail)),
        category: classify_event(value),
    }
}

fn string_field(value: &Value, field: &str) -> Option<String> {
    value
        .get(field)
        .and_then(Value::as_str)
        .map(|value| value.chars().take(300).collect())
}

fn classify_event(value: &Value) -> String {
    let event = value.get("event").and_then(Value::as_str).unwrap_or("");
    let upstream = value.get("upstream").and_then(Value::as_str).unwrap_or("");
    let detail = value
        .get("detail")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_ascii_lowercase();
    match event {
        "upstream_stream_interrupted" => "streaming".to_string(),
        "explicit_codex_tools_injected"
        | "third_party_tool_call_alias_normalized"
        | "multi_agent_current_state_guidance_injected"
        | "tool_search_discovery_fallback_applied" => "tool_call_subagent".to_string(),
        "request_error"
            if upstream == "official"
                || detail.contains("codex auth")
                || detail.contains("token") =>
        {
            "codex_auth".to_string()
        }
        "request_error" if detail.contains("model") && detail.contains("not") => {
            "model_id".to_string()
        }
        "request_error" if upstream != "official" && !upstream.is_empty() => {
            "external_upstream".to_string()
        }
        "request_error" => "proxy".to_string(),
        _ => "proxy".to_string(),
    }
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

fn sanitize_text(text: &str) -> String {
    let mut output = text.chars().take(1400).collect::<String>();
    let lower = output.to_ascii_lowercase();
    if lower.contains("authorization")
        || lower.contains("access_token")
        || lower.contains("refresh_token")
        || lower.contains("api_key")
        || lower.contains("apikey")
        || lower.contains("bearer ")
    {
        output = "[redacted sensitive response detail]".to_string();
    }
    output
}

fn normalize_client_id(client_id: &str) -> String {
    client_id.trim().to_ascii_lowercase().replace('_', "-")
}

fn detect_opencode_config_path() -> Option<PathBuf> {
    if let Some(path) = std::env::var_os("CODEXHUB_OPENCODE_CONFIG")
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
    {
        return Some(path);
    }
    let mut candidates = Vec::new();
    if let Some(config_dir) = std::env::var_os("XDG_CONFIG_HOME").map(PathBuf::from) {
        candidates.push(config_dir.join("opencode").join("opencode.json"));
    }
    if let Some(home) = dirs::home_dir() {
        candidates.push(home.join(".config").join("opencode").join("opencode.json"));
        candidates.push(home.join(".config").join("opencode").join("config.json"));
    }
    if let Some(appdata) = std::env::var_os("APPDATA").map(PathBuf::from) {
        candidates.push(appdata.join("opencode").join("opencode.json"));
        candidates.push(appdata.join("opencode").join("config.json"));
    }
    candidates
        .into_iter()
        .find(|path| path.exists())
        .or_else(|| {
            dirs::home_dir().map(|home| home.join(".config").join("opencode").join("opencode.json"))
        })
}

fn detect_pi_config_paths() -> PiConfigPaths {
    let agent_dir = if let Some(path) = std::env::var_os("CODEXHUB_PI_AGENT_DIR")
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
    {
        path
    } else if let Some(path) = std::env::var_os("CODEXHUB_PI_CONFIG")
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
    {
        return PiConfigPaths {
            models_path: path
                .parent()
                .map(|parent| parent.join("models.json"))
                .unwrap_or_else(|| PathBuf::from("models.json")),
            settings_path: path,
        };
    } else {
        dirs::home_dir()
            .map(|home| home.join(".pi").join("agent"))
            .unwrap_or_else(|| PathBuf::from("~/.pi/agent"))
    };
    PiConfigPaths {
        settings_path: agent_dir.join("settings.json"),
        models_path: agent_dir.join("models.json"),
    }
}

fn detect_omp_config_paths() -> OmpConfigPaths {
    let agent_dir = if let Some(path) = std::env::var_os("CODEXHUB_OMP_AGENT_DIR")
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
    {
        path
    } else if let Some(path) = std::env::var_os("CODEXHUB_OMP_CONFIG")
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
    {
        return OmpConfigPaths {
            models_path: path
                .parent()
                .map(|parent| parent.join("models.yml"))
                .unwrap_or_else(|| PathBuf::from("models.yml")),
            config_path: path,
        };
    } else {
        dirs::home_dir()
            .map(|home| home.join(".omp").join("agent"))
            .unwrap_or_else(|| PathBuf::from("~/.omp/agent"))
    };
    OmpConfigPaths {
        config_path: agent_dir.join("config.yml"),
        models_path: agent_dir.join("models.yml"),
    }
}

fn route_mode_from_text_file(path: &Path, is_hub: fn(&str) -> bool) -> &'static str {
    fs::read_to_string(path)
        .ok()
        .map(|text| if is_hub(&text) { "hub" } else { "official" })
        .unwrap_or("unknown")
}

fn pi_route_mode(paths: &PiConfigPaths) -> &'static str {
    let settings = fs::read_to_string(&paths.settings_path).ok();
    let models = fs::read_to_string(&paths.models_path).ok();
    match (settings.as_deref(), models.as_deref()) {
        (Some(settings), Some(models)) => {
            if is_pi_codexhub_config(settings, models) {
                "hub"
            } else {
                "official"
            }
        }
        (Some(settings), None) => {
            if is_pi_settings_codexhub_config(settings) {
                "hub"
            } else {
                "official"
            }
        }
        (None, Some(models)) => {
            if is_pi_models_codexhub_config(models) {
                "hub"
            } else {
                "official"
            }
        }
        (None, None) => "unknown",
    }
}

fn omp_route_mode(paths: &OmpConfigPaths) -> &'static str {
    let config = fs::read_to_string(&paths.config_path).ok();
    let models = fs::read_to_string(&paths.models_path).ok();
    match (config.as_deref(), models.as_deref()) {
        (Some(config), Some(models)) => {
            if is_omp_codexhub_config(config, models) {
                "hub"
            } else {
                "official"
            }
        }
        (Some(config), None) => {
            if config.contains("codexhub/") {
                "hub"
            } else {
                "official"
            }
        }
        (None, Some(models)) => {
            if is_omp_models_codexhub_config(models) {
                "hub"
            } else {
                "official"
            }
        }
        (None, None) => "unknown",
    }
}

fn gateway_client_status(installed: bool, route_mode: &str) -> String {
    if !installed {
        return "Not installed.".to_string();
    }
    if route_mode == "hub" {
        "Ready; routed through CodexHub Gateway.".to_string()
    } else {
        "Installed; native config switching is supported.".to_string()
    }
}

fn detect_zcode_config_path() -> PathBuf {
    if let Some(path) = std::env::var_os("CODEXHUB_ZCODE_CONFIG")
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
    {
        return path;
    }
    std::env::var_os("APPDATA")
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
        .map(|path| {
            path.join("ZCode")
                .join("model-providers")
                .join("codexhub.json")
        })
        .unwrap_or_else(|| PathBuf::from("%APPDATA%/ZCode/model-providers/codexhub.json"))
}

fn detect_zcode_store_path() -> PathBuf {
    std::env::var_os("APPDATA")
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
        .map(|path| {
            path.join("ZCode")
                .join("rum-electron-store")
                .join("ZGVmYXVsdA.json")
        })
        .unwrap_or_else(|| PathBuf::from("%APPDATA%/ZCode/rum-electron-store/ZGVmYXVsdA.json"))
}

fn detect_zcode_executable_path() -> Option<PathBuf> {
    let mut candidates = Vec::new();
    if let Some(path) = std::env::var_os("CODEXHUB_ZCODE_EXE")
        .filter(|value| !value.is_empty())
        .map(PathBuf::from)
    {
        candidates.push(path);
    }
    if let Ok(path) = which::which("zcode") {
        candidates.push(path);
    }
    if let Ok(path) = which::which("ZCode") {
        candidates.push(path);
    }
    if let Ok(path) = which::which("ZCode.exe") {
        candidates.push(path);
    }
    if let Some(path) = windows_app_path("ZCode.exe") {
        candidates.push(path);
    }
    if let Some(program_files) = std::env::var_os("ProgramFiles").map(PathBuf::from) {
        candidates.push(program_files.join("ZCode").join("ZCode.exe"));
    }
    if let Some(program_files_x86) = std::env::var_os("ProgramFiles(x86)").map(PathBuf::from) {
        candidates.push(program_files_x86.join("ZCode").join("ZCode.exe"));
    }
    if let Some(system_drive) = std::env::var_os("SystemDrive").map(PathBuf::from) {
        candidates.push(
            system_drive
                .join("Program Files")
                .join("ZCode")
                .join("ZCode.exe"),
        );
    }
    if let Some(local_appdata) = std::env::var_os("LOCALAPPDATA").map(PathBuf::from) {
        candidates.push(
            local_appdata
                .join("Programs")
                .join("ZCode")
                .join("ZCode.exe"),
        );
    }
    candidates.into_iter().find(|path| path.exists())
}

fn command_exists(commands: &[&str]) -> bool {
    commands.iter().any(|command| which::which(command).is_ok())
}

fn command_version(commands: &[&str]) -> Option<String> {
    for command in commands {
        let Ok(path) = which::which(command) else {
            continue;
        };
        let Some(output) = version_output_for_path(&path) else {
            continue;
        };
        let text = if output.stdout.is_empty() {
            String::from_utf8_lossy(&output.stderr)
        } else {
            String::from_utf8_lossy(&output.stdout)
        };
        if let Some(version) = parse_version_output(&text) {
            return Some(version);
        }
    }
    None
}

fn version_output_for_path(path: &Path) -> Option<std::process::Output> {
    let extension = path
        .extension()
        .and_then(|value| value.to_str())
        .unwrap_or_default()
        .to_ascii_lowercase();
    match extension.as_str() {
        "ps1" => Command::new("powershell")
            .args([
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                path.to_string_lossy().as_ref(),
                "--version",
            ])
            .output()
            .ok(),
        "cmd" | "bat" => Command::new("cmd")
            .args(["/C", path.to_string_lossy().as_ref(), "--version"])
            .output()
            .ok(),
        _ => Command::new(path).arg("--version").output().ok(),
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

fn zcode_latest_version() -> Option<String> {
    let client = Client::builder()
        .timeout(Duration::from_secs(8))
        .user_agent("Mozilla/5.0 CodexHub/0.1")
        .build()
        .ok()?;
    let text = client
        .get("https://zcode.z.ai/en/changelog")
        .send()
        .ok()?
        .error_for_status()
        .ok()?
        .text()
        .ok()?;
    zcode_changelog_release_version(&text)
}

fn zcode_changelog_release_version(text: &str) -> Option<String> {
    let marker = "font-mono text-sm\">";
    let mut offset = 0;
    while let Some(relative_start) = text[offset..].find(marker) {
        let start = offset + relative_start + marker.len();
        let end = start + text[start..].find('<')?;
        let candidate = text[start..end].trim();
        if is_exact_semver_like(candidate) {
            return Some(candidate.to_string());
        }
        offset = end;
    }
    None
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
        let output = Command::new("reg")
            .args(["query", &key, "/ve"])
            .output()
            .ok()?;
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
    let output = Command::new("powershell")
        .args(["-NoProfile", "-Command", &script])
        .output()
        .ok()?;
    if !output.status.success() {
        return None;
    }
    let text = String::from_utf8_lossy(&output.stdout);
    parse_version_output(&text)
}

fn client_backup_root(client_id: &str) -> PathBuf {
    codex_home()
        .join("proxy")
        .join("client-backups")
        .join(client_id)
}

fn preview_opencode_config_with_path(
    config_path: &Path,
    settings: &Settings,
    providers: &[Provider],
    model: &str,
) -> Result<GatewayClientConfigPreview, String> {
    let current = fs::read_to_string(config_path)
        .ok()
        .map(|text| sanitize_text(&text));
    let next = opencode_config_text(settings, providers, model)?;
    Ok(GatewayClientConfigPreview {
        client_id: "opencode".to_string(),
        can_apply: config_path.exists(),
        strategy: "managed_overwrite".to_string(),
        config_path: Some(config_path.to_path_buf()),
        current_redacted: current,
        next_redacted: sanitize_text(&next),
        backup_required: true,
        message: if config_path.exists() {
            "Apply will back up the current OpenCode config, then overwrite it with CodexHub managed config.".to_string()
        } else {
            "OpenCode config does not exist yet; auto-apply is disabled until there is an official config to back up.".to_string()
        },
    })
}

fn preview_pi_config_with_paths(
    settings_path: &Path,
    models_path: &Path,
    settings: &Settings,
    providers: &[Provider],
    model: &str,
) -> Result<GatewayClientConfigPreview, String> {
    let current = combined_current_preview(&[
        ("settings.json", settings_path),
        ("models.json", models_path),
    ]);
    let next_settings = pi_settings_text(settings_path, settings, providers, model)?;
    let next_models = pi_models_text(models_path, settings, providers, model)?;
    Ok(GatewayClientConfigPreview {
        client_id: "pi".to_string(),
        can_apply: true,
        strategy: "managed_native_config".to_string(),
        config_path: Some(settings_path.to_path_buf()),
        current_redacted: current.map(|text| sanitize_text(&text)),
        next_redacted: sanitize_text(&combined_named_text(&[
            ("settings.json", &next_settings),
            ("models.json", &next_models),
        ])),
        backup_required: settings_path.exists() || models_path.exists(),
        message: "Apply will snapshot Pi settings/models, then route Pi through CodexHub Gateway."
            .to_string(),
    })
}

fn preview_omp_config_with_paths(
    config_path: &Path,
    models_path: &Path,
    settings: &Settings,
    providers: &[Provider],
    model: &str,
) -> Result<GatewayClientConfigPreview, String> {
    let current =
        combined_current_preview(&[("config.yml", config_path), ("models.yml", models_path)]);
    let current_config = fs::read_to_string(config_path).ok();
    let next_config = omp_config_text(current_config.as_deref(), model);
    let next_models = omp_models_yml_text(settings, providers, model);
    Ok(GatewayClientConfigPreview {
        client_id: "omp".to_string(),
        can_apply: true,
        strategy: "managed_native_config".to_string(),
        config_path: Some(config_path.to_path_buf()),
        current_redacted: current.map(|text| sanitize_text(&text)),
        next_redacted: sanitize_text(&combined_named_text(&[
            ("config.yml", &next_config),
            ("models.yml", &next_models),
        ])),
        backup_required: config_path.exists() || models_path.exists(),
        message: "Apply will snapshot OMP config/models, then route OMP through CodexHub Gateway."
            .to_string(),
    })
}

fn preview_zcode_config_with_path(
    catalog_path: &Path,
    settings: &Settings,
    providers: &[Provider],
    model: &str,
) -> Result<GatewayClientConfigPreview, String> {
    let current = fs::read_to_string(catalog_path)
        .ok()
        .map(|text| sanitize_text(&text));
    let next = zcode_catalog_text(settings, providers, model)?;
    Ok(GatewayClientConfigPreview {
        client_id: "zcode".to_string(),
        can_apply: true,
        strategy: "managed_user_catalog".to_string(),
        config_path: Some(catalog_path.to_path_buf()),
        current_redacted: current,
        next_redacted: sanitize_text(&next),
        backup_required: catalog_path.exists(),
        message: "Apply will write a ZCode user model-provider catalog for CodexHub Gateway."
            .to_string(),
    })
}

fn apply_opencode_config_with_paths(
    config_path: &Path,
    backup_root: &Path,
    settings: &Settings,
    providers: &[Provider],
    model: &str,
) -> Result<GatewayClientApplyResult, String> {
    if !config_path.exists() {
        return Ok(GatewayClientApplyResult {
            client_id: "opencode".to_string(),
            applied: false,
            config_path: Some(config_path.to_path_buf()),
            backup_path: None,
            message: "OpenCode config was not found; refusing managed overwrite without an official config backup.".to_string(),
        });
    }
    fs::create_dir_all(backup_root).map_err(|error| {
        format!(
            "failed to create OpenCode backup directory {}: {error}",
            backup_root.display()
        )
    })?;
    let current = fs::read_to_string(config_path).map_err(|error| {
        format!(
            "failed to read OpenCode config {}: {error}",
            config_path.display()
        )
    })?;
    let backup_path = if is_opencode_codexhub_config(&current) {
        None
    } else {
        let path = backup_root.join(format!("opencode-{}.json", timestamp_millis()));
        fs::copy(config_path, &path).map_err(|error| {
            format!(
                "failed to back up OpenCode config {} to {}: {error}",
                config_path.display(),
                path.display()
            )
        })?;
        Some(path)
    };
    let next = opencode_config_text(settings, providers, model)?;
    write_text_replace(config_path, &next)?;
    Ok(GatewayClientApplyResult {
        client_id: "opencode".to_string(),
        applied: true,
        config_path: Some(config_path.to_path_buf()),
        backup_path,
        message: "OpenCode now routes through CodexHub Gateway.".to_string(),
    })
}

fn apply_pi_config_with_paths(
    settings_path: &Path,
    models_path: &Path,
    backup_root: &Path,
    settings: &Settings,
    providers: &[Provider],
    model: &str,
) -> Result<GatewayClientApplyResult, String> {
    let current_settings = fs::read_to_string(settings_path).unwrap_or_default();
    let current_models = fs::read_to_string(models_path).unwrap_or_default();
    let backup_path = create_snapshot_backup(
        "pi",
        backup_root,
        &[
            ("settings.json", settings_path),
            ("models.json", models_path),
        ],
        is_pi_codexhub_config(&current_settings, &current_models),
    )?;
    let next_settings = pi_settings_text(settings_path, settings, providers, model)?;
    let next_models = pi_models_text(models_path, settings, providers, model)?;
    write_text_replace(settings_path, &next_settings)?;
    write_text_replace(models_path, &next_models)?;
    Ok(GatewayClientApplyResult {
        client_id: "pi".to_string(),
        applied: true,
        config_path: Some(settings_path.to_path_buf()),
        backup_path,
        message: "Pi now routes through CodexHub Gateway.".to_string(),
    })
}

fn apply_omp_config_with_paths(
    config_path: &Path,
    models_path: &Path,
    backup_root: &Path,
    settings: &Settings,
    providers: &[Provider],
    model: &str,
) -> Result<GatewayClientApplyResult, String> {
    let current_config = fs::read_to_string(config_path).unwrap_or_default();
    let current_models = fs::read_to_string(models_path).unwrap_or_default();
    let backup_path = create_snapshot_backup(
        "omp",
        backup_root,
        &[("config.yml", config_path), ("models.yml", models_path)],
        is_omp_codexhub_config(&current_config, &current_models),
    )?;
    let next_config = omp_config_text(Some(&current_config), model);
    let next_models = omp_models_yml_text(settings, providers, model);
    write_text_replace(config_path, &next_config)?;
    write_text_replace(models_path, &next_models)?;
    Ok(GatewayClientApplyResult {
        client_id: "omp".to_string(),
        applied: true,
        config_path: Some(config_path.to_path_buf()),
        backup_path,
        message: "OMP now routes through CodexHub Gateway.".to_string(),
    })
}

fn apply_zcode_config_with_path(
    catalog_path: &Path,
    backup_root: &Path,
    settings: &Settings,
    providers: &[Provider],
    model: &str,
) -> Result<GatewayClientApplyResult, String> {
    let current = fs::read_to_string(catalog_path).unwrap_or_default();
    let backup_path = create_snapshot_backup(
        "zcode",
        backup_root,
        &[("codexhub.json", catalog_path)],
        is_zcode_codexhub_config(&current),
    )?;
    let next = zcode_catalog_text(settings, providers, model)?;
    write_text_replace(catalog_path, &next)?;
    Ok(GatewayClientApplyResult {
        client_id: "zcode".to_string(),
        applied: true,
        config_path: Some(catalog_path.to_path_buf()),
        backup_path,
        message: "ZCode CodexHub user catalog has been written.".to_string(),
    })
}

fn restore_latest_backup(
    client_id: &str,
    config_path: &Path,
    backup_root: &Path,
) -> Result<GatewayClientApplyResult, String> {
    let latest = fs::read_dir(backup_root)
        .map_err(|error| {
            format!(
                "failed to read backup directory {}: {error}",
                backup_root.display()
            )
        })?
        .filter_map(Result::ok)
        .filter_map(|entry| {
            let metadata = entry.metadata().ok()?;
            let modified = metadata.modified().ok()?;
            let path = entry.path();
            let text = fs::read_to_string(&path).ok()?;
            (!is_opencode_codexhub_config(&text)).then_some((modified, path))
        })
        .max_by_key(|(modified, _)| *modified)
        .map(|(_, path)| path)
        .ok_or_else(|| format!("no clean official backup is available for {client_id}"))?;
    let text = fs::read_to_string(&latest)
        .map_err(|error| format!("failed to read backup {}: {error}", latest.display()))?;
    let clean_text = strip_opencode_invalid_keys(&text)?;
    write_text_replace(config_path, &clean_text)?;
    Ok(GatewayClientApplyResult {
        client_id: client_id.to_string(),
        applied: true,
        config_path: Some(config_path.to_path_buf()),
        backup_path: Some(latest),
        message: "OpenCode official config restored.".to_string(),
    })
}

fn restore_pi_config_with_paths(
    settings_path: &Path,
    models_path: &Path,
    backup_root: &Path,
) -> Result<GatewayClientApplyResult, String> {
    let latest = restore_latest_snapshot_backup(
        "pi",
        backup_root,
        &[
            ("settings.json", settings_path),
            ("models.json", models_path),
        ],
        |path| {
            let settings = fs::read_to_string(path.join("settings.json")).unwrap_or_default();
            let models = fs::read_to_string(path.join("models.json")).unwrap_or_default();
            is_pi_codexhub_config(&settings, &models)
        },
    )?;
    Ok(GatewayClientApplyResult {
        client_id: "pi".to_string(),
        applied: true,
        config_path: Some(settings_path.to_path_buf()),
        backup_path: Some(latest),
        message: "Pi official config restored.".to_string(),
    })
}

fn restore_omp_config_with_paths(
    config_path: &Path,
    models_path: &Path,
    backup_root: &Path,
) -> Result<GatewayClientApplyResult, String> {
    let latest = restore_latest_snapshot_backup(
        "omp",
        backup_root,
        &[("config.yml", config_path), ("models.yml", models_path)],
        |path| {
            let config = fs::read_to_string(path.join("config.yml")).unwrap_or_default();
            let models = fs::read_to_string(path.join("models.yml")).unwrap_or_default();
            is_omp_codexhub_config(&config, &models)
        },
    )?;
    Ok(GatewayClientApplyResult {
        client_id: "omp".to_string(),
        applied: true,
        config_path: Some(config_path.to_path_buf()),
        backup_path: Some(latest),
        message: "OMP official config restored.".to_string(),
    })
}

fn restore_zcode_config_with_path(
    catalog_path: &Path,
    backup_root: &Path,
) -> Result<GatewayClientApplyResult, String> {
    let latest = latest_clean_snapshot_backup("zcode", backup_root, |path| {
        let text = fs::read_to_string(path.join("codexhub.json")).unwrap_or_default();
        is_zcode_codexhub_config(&text)
    });
    match latest {
        Ok(path) => {
            restore_snapshot_files(&path, &[("codexhub.json", catalog_path)])?;
            Ok(GatewayClientApplyResult {
                client_id: "zcode".to_string(),
                applied: true,
                config_path: Some(catalog_path.to_path_buf()),
                backup_path: Some(path),
                message: "ZCode user catalog restored.".to_string(),
            })
        }
        Err(_)
            if is_zcode_codexhub_config(&fs::read_to_string(catalog_path).unwrap_or_default()) =>
        {
            fs::remove_file(catalog_path).map_err(|error| {
                format!(
                    "failed to remove ZCode CodexHub catalog {}: {error}",
                    catalog_path.display()
                )
            })?;
            Ok(GatewayClientApplyResult {
                client_id: "zcode".to_string(),
                applied: true,
                config_path: Some(catalog_path.to_path_buf()),
                backup_path: None,
                message: "ZCode CodexHub user catalog removed.".to_string(),
            })
        }
        Err(error) => Err(error),
    }
}

fn opencode_config_text(
    settings: &Settings,
    providers: &[Provider],
    model: &str,
) -> Result<String, String> {
    let base_url = endpoints(settings.proxy_port).base_url;
    let mut models = Map::new();
    for gateway_model in gateway_client_models(settings, providers, model) {
        models.insert(
            gateway_model.id.clone(),
            json!({
                "name": gateway_model.display_name,
            }),
        );
    }
    let body = json!({
        "$schema": "https://opencode.ai/config.json",
        "model": format!("codexhub/{model}"),
        "small_model": format!("codexhub/{model}"),
        "provider": {
            "codexhub": {
                "name": "CodexHub Gateway",
                "npm": "@ai-sdk/openai-compatible",
                "options": {
                    "baseURL": base_url,
                    "apiKey": settings.gateway_client_key,
                },
                "models": Value::Object(models),
            }
        }
    });
    serde_json::to_string_pretty(&body)
        .map(|text| format!("{text}\n"))
        .map_err(|error| format!("failed to serialize OpenCode config: {error}"))
}

fn pi_settings_text(
    settings_path: &Path,
    settings: &Settings,
    providers: &[Provider],
    model: &str,
) -> Result<String, String> {
    let mut value = read_json_file_or_empty(settings_path, "Pi settings")?;
    if !value.is_object() {
        value = json!({});
    }
    let enabled_models = gateway_client_models(settings, providers, model)
        .iter()
        .map(|gateway_model| format!("codexhub/{}", gateway_model.id))
        .collect::<Vec<_>>();
    let object = value
        .as_object_mut()
        .ok_or_else(|| "Pi settings root must be a JSON object".to_string())?;
    object.insert("defaultProvider".to_string(), json!("codexhub"));
    object.insert("defaultModel".to_string(), json!(model));
    object.insert("enabledModels".to_string(), json!(enabled_models));
    serde_json::to_string_pretty(&value)
        .map(|text| format!("{text}\n"))
        .map_err(|error| format!("failed to serialize Pi settings: {error}"))
}

fn pi_models_text(
    models_path: &Path,
    settings: &Settings,
    providers: &[Provider],
    model: &str,
) -> Result<String, String> {
    let mut value = read_json_file_or_empty(models_path, "Pi models")?;
    if !value.is_object() {
        value = json!({});
    }
    let object = value
        .as_object_mut()
        .ok_or_else(|| "Pi models root must be a JSON object".to_string())?;
    let provider_root = object
        .entry("providers".to_string())
        .or_insert_with(|| json!({}));
    if !provider_root.is_object() {
        *provider_root = json!({});
    }
    provider_root
        .as_object_mut()
        .ok_or_else(|| "Pi providers root must be a JSON object".to_string())?
        .insert(
            "codexhub".to_string(),
            codexhub_pi_provider_value(
                settings,
                &gateway_client_models(settings, providers, model),
            ),
        );
    serde_json::to_string_pretty(&value)
        .map(|text| format!("{text}\n"))
        .map_err(|error| format!("failed to serialize Pi models: {error}"))
}

fn codexhub_pi_provider_value(settings: &Settings, models: &[GatewayModel]) -> Value {
    let models = models
        .iter()
        .map(codexhub_pi_model_value)
        .collect::<Vec<_>>();
    json!({
        "baseUrl": endpoints(settings.proxy_port).base_url,
        "api": "openai-completions",
        "apiKey": settings.gateway_client_key,
        "authHeader": true,
        "compat": {
            "supportsDeveloperRole": true,
            "supportsReasoningEffort": true,
            "supportsUsageInStreaming": true,
        },
        "models": models,
    })
}

fn codexhub_pi_model_value(model: &GatewayModel) -> Value {
    json!({
        "id": model.id.clone(),
        "name": model.display_name.clone(),
        "reasoning": true,
        "input": ["text", "image"],
        "contextWindow": model.context_window,
        "maxTokens": 32768,
        "cost": {
            "input": 0,
            "output": 0,
            "cacheRead": 0,
            "cacheWrite": 0,
        },
    })
}

fn omp_config_text(current: Option<&str>, model: &str) -> String {
    let selector = format!("codexhub/{model}");
    let block = [
        "modelRoles:".to_string(),
        format!("  default: {selector}"),
        format!("  vision: {selector}"),
    ];
    let mut output = Vec::new();
    let mut inserted = false;
    let lines = current.unwrap_or_default().lines().collect::<Vec<_>>();
    let mut index = 0;
    while index < lines.len() {
        let line = lines[index];
        if is_top_level_yaml_key(line, "modelRoles") {
            output.extend(block.iter().cloned());
            inserted = true;
            index += 1;
            while index < lines.len() && !is_any_top_level_yaml_key(lines[index]) {
                index += 1;
            }
            continue;
        }
        output.push(line.to_string());
        index += 1;
    }
    if !inserted {
        if !output.is_empty() && output.last().is_some_and(|line| !line.trim().is_empty()) {
            output.push(String::new());
        }
        output.extend(block);
    }
    format!("{}\n", output.join("\n"))
}

fn omp_models_yml_text(settings: &Settings, providers: &[Provider], model: &str) -> String {
    let base_url = yaml_scalar(&endpoints(settings.proxy_port).base_url);
    let api_key = yaml_scalar(&settings.gateway_client_key);
    let mut output = format!(
        "providers:\n  codexhub:\n    baseUrl: {base_url}\n    api: openai-completions\n    apiKey: {api_key}\n    authHeader: true\n    compat:\n      supportsDeveloperRole: true\n      supportsReasoningEffort: true\n      supportsUsageInStreaming: true\n    models:\n"
    );
    for gateway_model in gateway_client_models(settings, providers, model) {
        let model_id = yaml_scalar(&gateway_model.id);
        let model_name = yaml_scalar(&gateway_model.display_name);
        let context_window = gateway_model.context_window;
        output.push_str(&format!(
            "      - id: {model_id}\n        name: {model_name}\n        reasoning: true\n        input:\n          - text\n          - image\n        contextWindow: {context_window}\n        maxTokens: 32768\n        cost:\n          input: 0\n          output: 0\n          cacheRead: 0\n          cacheWrite: 0\n"
        ));
    }
    output
}

fn zcode_catalog_text(
    settings: &Settings,
    providers: &[Provider],
    model: &str,
) -> Result<String, String> {
    let base_url = gateway_base_without_v1(settings);
    let now = timestamp_millis() as u64;
    let models = gateway_client_models(settings, providers, model)
        .iter()
        .map(zcode_model_value)
        .collect::<Vec<_>>();
    let body = json!({
        "schemaVersion": "zcode.model-providers.v2",
        "providers": [{
            "id": "codexhub",
            "name": "CodexHub Gateway",
            "enabled": true,
            "source": "custom",
            "endpoints": {
                "baseURL": base_url,
                "paths": {
                    "openai-compatible": "/v1/chat/completions",
                },
            },
            "apiKeyRequired": true,
            "apiKey": settings.gateway_client_key,
            "defaultKind": "openai-compatible",
            "models": models,
            "createdAt": now,
            "updatedAt": now,
        }],
    });
    serde_json::to_string_pretty(&body)
        .map(|text| format!("{text}\n"))
        .map_err(|error| format!("failed to serialize ZCode catalog: {error}"))
}

fn zcode_model_value(model: &GatewayModel) -> Value {
    json!({
        "id": model.id.clone(),
        "name": model.display_name.clone(),
        "kinds": ["openai-compatible"],
        "defaultKind": "openai-compatible",
        "modalities": {
            "input": ["text", "image"],
            "output": ["text"],
        },
        "contextWindow": model.context_window,
        "maxOutputTokens": 32768,
    })
}

fn is_opencode_codexhub_config(text: &str) -> bool {
    let Ok(value) = serde_json::from_str::<Value>(text) else {
        return text.contains("\"codexhub_managed\"") || text.contains("\"codexhub\"");
    };
    value
        .get("codexhub_managed")
        .and_then(Value::as_bool)
        .unwrap_or(false)
        || value
            .get("model")
            .and_then(Value::as_str)
            .is_some_and(|model| model.starts_with("codexhub/"))
        || value
            .get("small_model")
            .and_then(Value::as_str)
            .is_some_and(|model| model.starts_with("codexhub/"))
        || value
            .get("provider")
            .and_then(|provider| provider.get("codexhub"))
            .is_some()
}

fn strip_opencode_invalid_keys(text: &str) -> Result<String, String> {
    let mut value = serde_json::from_str::<Value>(text)
        .map_err(|error| format!("failed to parse OpenCode config backup: {error}"))?;
    if let Some(object) = value.as_object_mut() {
        object.remove("codexhub_managed");
    }
    serde_json::to_string_pretty(&value)
        .map(|text| format!("{text}\n"))
        .map_err(|error| format!("failed to serialize cleaned OpenCode config: {error}"))
}

fn is_pi_codexhub_config(settings_text: &str, models_text: &str) -> bool {
    is_pi_settings_codexhub_config(settings_text) || is_pi_models_codexhub_config(models_text)
}

fn is_pi_settings_codexhub_config(text: &str) -> bool {
    let Ok(value) = serde_json::from_str::<Value>(text) else {
        return text.contains("\"codexhub\"");
    };
    value
        .get("defaultProvider")
        .and_then(Value::as_str)
        .is_some_and(|provider| provider == "codexhub")
        || value
            .get("enabledModels")
            .and_then(Value::as_array)
            .is_some_and(|models| {
                models
                    .iter()
                    .filter_map(Value::as_str)
                    .any(|model| model.starts_with("codexhub/"))
            })
}

fn is_pi_models_codexhub_config(text: &str) -> bool {
    let Ok(value) = serde_json::from_str::<Value>(text) else {
        return text.contains("\"codexhub\"");
    };
    value
        .get("providers")
        .and_then(|providers| providers.get("codexhub"))
        .is_some()
}

fn is_omp_codexhub_config(config_text: &str, models_text: &str) -> bool {
    config_text.contains("codexhub/") || is_omp_models_codexhub_config(models_text)
}

fn is_omp_models_codexhub_config(text: &str) -> bool {
    text.lines().any(|line| {
        line.trim_start().starts_with("codexhub:")
            || line.contains("codexhub/")
            || line.contains("CodexHub Gateway")
    })
}

fn is_zcode_codexhub_config(text: &str) -> bool {
    let Ok(value) = serde_json::from_str::<Value>(text) else {
        return text.contains("\"codexhub\"") || text.contains("CodexHub Gateway");
    };
    value
        .get("providers")
        .and_then(Value::as_array)
        .is_some_and(|providers| {
            providers.iter().any(|provider| {
                provider
                    .get("id")
                    .and_then(Value::as_str)
                    .is_some_and(|id| id == "codexhub")
                    || provider
                        .get("name")
                        .and_then(Value::as_str)
                        .is_some_and(|name| name == "CodexHub Gateway")
            })
        })
}

fn read_json_file_or_empty(path: &Path, label: &str) -> Result<Value, String> {
    if !path.exists() {
        return Ok(json!({}));
    }
    let text = fs::read_to_string(path)
        .map_err(|error| format!("failed to read {label} {}: {error}", path.display()))?;
    serde_json::from_str::<Value>(&text)
        .map_err(|error| format!("failed to parse {label} {}: {error}", path.display()))
}

fn create_snapshot_backup(
    client_id: &str,
    backup_root: &Path,
    files: &[(&str, &Path)],
    current_is_managed: bool,
) -> Result<Option<PathBuf>, String> {
    if current_is_managed {
        return Ok(None);
    }
    let existing_files = files
        .iter()
        .filter(|(_, path)| path.exists())
        .collect::<Vec<_>>();
    if existing_files.is_empty() {
        return Ok(None);
    }
    fs::create_dir_all(backup_root).map_err(|error| {
        format!(
            "failed to create {client_id} backup directory {}: {error}",
            backup_root.display()
        )
    })?;
    let backup_path = backup_root.join(format!("{client_id}-{}", timestamp_millis()));
    fs::create_dir_all(&backup_path).map_err(|error| {
        format!(
            "failed to create {client_id} backup snapshot {}: {error}",
            backup_path.display()
        )
    })?;
    for (name, source) in existing_files {
        let target = backup_path.join(name);
        fs::copy(source, &target).map_err(|error| {
            format!(
                "failed to back up {client_id} config {} to {}: {error}",
                source.display(),
                target.display()
            )
        })?;
    }
    Ok(Some(backup_path))
}

fn latest_clean_snapshot_backup<F>(
    client_id: &str,
    backup_root: &Path,
    is_managed_snapshot: F,
) -> Result<PathBuf, String>
where
    F: Fn(&Path) -> bool,
{
    fs::read_dir(backup_root)
        .map_err(|error| {
            format!(
                "failed to read backup directory {}: {error}",
                backup_root.display()
            )
        })?
        .filter_map(Result::ok)
        .filter_map(|entry| {
            let metadata = entry.metadata().ok()?;
            if !metadata.is_dir() {
                return None;
            }
            let modified = metadata.modified().ok()?;
            let path = entry.path();
            (!is_managed_snapshot(&path)).then_some((modified, path))
        })
        .max_by_key(|(modified, _)| *modified)
        .map(|(_, path)| path)
        .ok_or_else(|| format!("no clean official backup is available for {client_id}"))
}

fn restore_latest_snapshot_backup<F>(
    client_id: &str,
    backup_root: &Path,
    targets: &[(&str, &Path)],
    is_managed_snapshot: F,
) -> Result<PathBuf, String>
where
    F: Fn(&Path) -> bool,
{
    let latest = latest_clean_snapshot_backup(client_id, backup_root, is_managed_snapshot)?;
    restore_snapshot_files(&latest, targets)?;
    Ok(latest)
}

fn restore_snapshot_files(snapshot_path: &Path, targets: &[(&str, &Path)]) -> Result<(), String> {
    for (name, target) in targets {
        let source = snapshot_path.join(name);
        if source.exists() {
            if let Some(parent) = target.parent() {
                fs::create_dir_all(parent).map_err(|error| {
                    format!(
                        "failed to create config directory {}: {error}",
                        parent.display()
                    )
                })?;
            }
            fs::copy(&source, target).map_err(|error| {
                format!(
                    "failed to restore config {} to {}: {error}",
                    source.display(),
                    target.display()
                )
            })?;
        } else if target.exists() {
            fs::remove_file(target).map_err(|error| {
                format!(
                    "failed to remove restored-absent config {}: {error}",
                    target.display()
                )
            })?;
        }
    }
    Ok(())
}

fn combined_current_preview(files: &[(&str, &Path)]) -> Option<String> {
    let sections = files
        .iter()
        .filter_map(|(name, path)| {
            fs::read_to_string(path)
                .ok()
                .map(|text| format!("{name}:\n{text}"))
        })
        .collect::<Vec<_>>();
    (!sections.is_empty()).then(|| sections.join("\n"))
}

fn combined_named_text(files: &[(&str, &str)]) -> String {
    files
        .iter()
        .map(|(name, text)| format!("{name}:\n{text}"))
        .collect::<Vec<_>>()
        .join("\n")
}

fn gateway_model_context_window(model: &str) -> u32 {
    OFFICIAL_MODELS
        .iter()
        .find_map(|(id, _, context)| (*id == model).then_some(*context))
        .or_else(|| {
            OFFICIAL_FAST_VARIANTS
                .iter()
                .find_map(|(_, id, _, context)| (*id == model).then_some(*context))
        })
        .unwrap_or(200_000)
}

fn gateway_model_display_name(model: &str) -> String {
    OFFICIAL_MODELS
        .iter()
        .find_map(|(id, name, _)| (*id == model).then_some((*name).to_string()))
        .or_else(|| {
            OFFICIAL_FAST_VARIANTS
                .iter()
                .find_map(|(_, id, name, _)| (*id == model).then_some((*name).to_string()))
        })
        .unwrap_or_else(|| model.to_string())
}

fn gateway_base_without_v1(settings: &Settings) -> String {
    let base_url = endpoints(settings.proxy_port).base_url;
    base_url
        .strip_suffix("/v1")
        .unwrap_or(base_url.as_str())
        .to_string()
}

fn yaml_scalar(value: &str) -> String {
    if !value.is_empty()
        && value
            .chars()
            .all(|char| char.is_ascii_alphanumeric() || "-_./:".contains(char))
    {
        value.to_string()
    } else {
        serde_json::to_string(value).unwrap_or_else(|_| "\"\"".to_string())
    }
}

fn is_top_level_yaml_key(line: &str, key: &str) -> bool {
    let trimmed = line.trim();
    !line.starts_with(' ')
        && !line.starts_with('\t')
        && trimmed
            .strip_suffix(':')
            .or_else(|| trimmed.split_once(':').map(|(name, _)| name))
            .is_some_and(|name| name == key)
}

fn is_any_top_level_yaml_key(line: &str) -> bool {
    let trimmed = line.trim();
    !trimmed.is_empty()
        && !line.starts_with(' ')
        && !line.starts_with('\t')
        && trimmed.contains(':')
}

fn write_text_replace(path: &Path, text: &str) -> Result<(), String> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|error| {
            format!(
                "failed to create config directory {}: {error}",
                parent.display()
            )
        })?;
    }
    let temp_path = path.with_extension("tmp-codexhub");
    fs::write(&temp_path, text).map_err(|error| {
        format!(
            "failed to write temp config {}: {error}",
            temp_path.display()
        )
    })?;
    if path.exists() {
        fs::remove_file(path)
            .map_err(|error| format!("failed to replace config {}: {error}", path.display()))?;
    }
    fs::rename(&temp_path, path).map_err(|error| {
        format!(
            "failed to move temp config {} to {}: {error}",
            temp_path.display(),
            path.display()
        )
    })
}

fn timestamp_millis() -> u128 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_millis())
        .unwrap_or_default()
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
        apply_opencode_config_with_paths, gateway_models_from_config, omp_models_yml_text,
        opencode_config_text, pi_models_text, pi_settings_text, read_usage_events_from_sqlite_path,
        read_usage_events_from_text, read_usage_summary_from_sqlite_path_with_pricing,
        read_usage_summary_from_text, read_usage_summary_from_text_with_pricing,
        restore_latest_backup, sanitize_event, sanitize_text, usage_pricing_by_model,
        zcode_catalog_text, UsagePricing,
    };
    use crate::{Model, Provider, Settings};
    use serde_json::json;
    use std::collections::HashMap;
    use std::fs;
    use std::path::PathBuf;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn client_export_test_providers() -> Vec<Provider> {
        vec![Provider {
            id: "minimax".to_string(),
            name: "MiniMax".to_string(),
            base_url: "https://api.minimax.chat/v1".to_string(),
            api_key: None,
            upstream_format: None,
            display_prefix: Some("minimax/".to_string()),
            sort_order: None,
            enabled: true,
            locked: false,
            models: vec![
                Model {
                    id: "minimax-m3".to_string(),
                    display_name: Some("MiniMax M3".to_string()),
                    context_window: Some(1_000_000),
                    gateway_exported: true,
                    ..Model::default()
                },
                Model {
                    id: "minimax-m3-lite".to_string(),
                    gateway_exported: false,
                    ..Model::default()
                },
            ],
        }]
    }

    #[test]
    fn sanitizes_sensitive_text() {
        assert_eq!(
            sanitize_text("Authorization: Bearer secret"),
            "[redacted sensitive response detail]"
        );
    }

    #[test]
    fn event_sanitization_keeps_only_safe_fields() {
        let event = sanitize_event(&json!({
            "ts": "now",
            "event": "request_error",
            "model": "openai/gpt-5.5",
            "Authorization": "Bearer secret",
            "detail": "CodexAuthError",
            "upstream": "official"
        }));
        assert_eq!(event.model.as_deref(), Some("openai/gpt-5.5"));
        assert_eq!(event.category, "codex_auth");
    }

    #[test]
    fn classifies_streaming_events() {
        assert_eq!(
            super::classify_event(&json!({"event": "upstream_stream_interrupted"})),
            "streaming"
        );
    }

    #[test]
    fn gateway_models_export_enabled_gateway_models_ignoring_legacy_hidden() {
        let settings = Settings::default();
        let providers: Vec<Provider> = serde_json::from_value(json!([{
            "id": "minimax",
            "name": "MiniMax",
            "base_url": "https://api.minimax.chat/v1",
            "display_prefix": "minimax/",
            "enabled": true,
            "hidden": true,
            "models": [
                {
                    "id": "minimax-m3",
                    "display_name": "MiniMax M3",
                    "context_window": 1000000,
                    "gateway_exported": true,
                    "hidden": true
                },
                {
                    "id": "minimax-m3-lite",
                    "gateway_exported": false,
                    "hidden": true
                },
                {
                    "id": "disabled",
                    "enabled": false,
                    "gateway_exported": true,
                    "hidden": true
                }
            ]
        }]))
        .unwrap();

        let models = gateway_models_from_config(&settings, &providers);

        assert!(models.iter().any(|model| model.id == "openai/gpt-5.5"));
        assert!(models.iter().any(|model| model.id == "minimax/minimax-m3"));
        assert!(!models
            .iter()
            .any(|model| model.id == "minimax/minimax-m3-lite"));
        assert!(!models.iter().any(|model| model.id == "minimax/disabled"));
    }

    #[test]
    fn gateway_models_skip_disabled_official_models_and_fast_variants() {
        let settings = Settings {
            official_disabled_models: vec!["openai/gpt-5.4".to_string()],
            ..Settings::default()
        };

        let models = gateway_models_from_config(&settings, &[]);

        assert!(models.iter().any(|model| model.id == "openai/gpt-5.5"));
        assert!(models.iter().any(|model| model.id == "openai/gpt-5.5-fast"));
        assert!(!models.iter().any(|model| model.id == "openai/gpt-5.4"));
        assert!(!models.iter().any(|model| model.id == "openai/gpt-5.4-fast"));
    }

    #[test]
    fn opencode_config_exports_all_active_gateway_models() {
        let settings = Settings::default();
        let providers = client_export_test_providers();

        let text = opencode_config_text(&settings, &providers, "openai/gpt-5.5").unwrap();
        let value: serde_json::Value = serde_json::from_str(&text).unwrap();
        let exported = value
            .pointer("/provider/codexhub/models")
            .and_then(serde_json::Value::as_object)
            .unwrap();

        assert_eq!(value["model"], "codexhub/openai/gpt-5.5");
        assert!(exported.contains_key("openai/gpt-5.5"));
        assert!(exported.contains_key("minimax/minimax-m3"));
        assert!(!exported.contains_key("minimax/minimax-m3-lite"));
        assert_eq!(exported["minimax/minimax-m3"]["name"], "MiniMax M3");
    }

    #[test]
    fn pi_config_exports_all_active_gateway_models() {
        let root = unique_temp_dir("codexhub-pi-export");
        let settings_path = root.join("settings.json");
        let models_path = root.join("models.json");
        fs::create_dir_all(root.as_path()).unwrap();
        let settings = Settings::default();
        let providers = client_export_test_providers();

        let settings_text =
            pi_settings_text(&settings_path, &settings, &providers, "openai/gpt-5.5").unwrap();
        let models_text =
            pi_models_text(&models_path, &settings, &providers, "openai/gpt-5.5").unwrap();
        let settings_value: serde_json::Value = serde_json::from_str(&settings_text).unwrap();
        let models_value: serde_json::Value = serde_json::from_str(&models_text).unwrap();
        let enabled = settings_value["enabledModels"].as_array().unwrap();
        let models = models_value
            .pointer("/providers/codexhub/models")
            .and_then(serde_json::Value::as_array)
            .unwrap();

        assert!(enabled
            .iter()
            .any(|value| value == "codexhub/openai/gpt-5.5"));
        assert!(enabled
            .iter()
            .any(|value| value == "codexhub/minimax/minimax-m3"));
        assert!(!enabled
            .iter()
            .any(|value| value == "codexhub/minimax/minimax-m3-lite"));
        assert!(models
            .iter()
            .any(|model| model["id"] == "minimax/minimax-m3"));
        assert!(!models
            .iter()
            .any(|model| model["id"] == "minimax/minimax-m3-lite"));
    }

    #[test]
    fn omp_models_export_all_active_gateway_models() {
        let settings = Settings::default();
        let providers = client_export_test_providers();

        let text = omp_models_yml_text(&settings, &providers, "openai/gpt-5.5");

        assert!(text.contains("id: openai/gpt-5.5"));
        assert!(text.contains("id: minimax/minimax-m3"));
        assert!(text.contains("name: \"MiniMax M3\""));
        assert!(!text.contains("minimax/minimax-m3-lite"));
    }

    #[test]
    fn omp_models_use_valid_context_window_for_external_models_without_metadata() {
        let settings = Settings {
            include_official_models: false,
            ..Settings::default()
        };
        let providers = vec![Provider {
            id: "ollama-cloud".to_string(),
            name: "Ollama Cloud".to_string(),
            base_url: "https://ollama.com/v1".to_string(),
            api_key: None,
            upstream_format: None,
            display_prefix: Some("Ollama".to_string()),
            sort_order: None,
            enabled: true,
            locked: false,
            models: vec![Model {
                id: "nemotron-3-nano:30b".to_string(),
                gateway_exported: true,
                context_window: None,
                ..Model::default()
            }],
        }];

        let text = omp_models_yml_text(&settings, &providers, "ollama-cloud/nemotron-3-nano:30b");

        assert!(text.contains("id: ollama-cloud/nemotron-3-nano:30b"));
        assert!(text.contains("contextWindow: 200000"));
        assert!(!text.contains("contextWindow: 0"));
    }

    #[test]
    fn zcode_catalog_exports_all_active_gateway_models() {
        let settings = Settings::default();
        let providers = client_export_test_providers();

        let text = zcode_catalog_text(&settings, &providers, "openai/gpt-5.5").unwrap();
        let value: serde_json::Value = serde_json::from_str(&text).unwrap();
        let models = value
            .pointer("/providers/0/models")
            .and_then(serde_json::Value::as_array)
            .unwrap();

        assert!(models.iter().any(|model| model["id"] == "openai/gpt-5.5"));
        assert!(models
            .iter()
            .any(|model| model["id"] == "minimax/minimax-m3"));
        assert!(!models
            .iter()
            .any(|model| model["id"] == "minimax/minimax-m3-lite"));
    }

    #[test]
    fn usage_summary_counts_missing_usage_without_estimating_tokens() {
        let text = [
            r#"{"event":"request_complete","model":"openai/gpt-5.5","status":200,"duration_ms":120,"usage_source":"upstream","usage_input_tokens":10,"usage_output_tokens":4,"usage_cached_input_tokens":3}"#,
            r#"{"event":"request_complete","model":"ollama/glm-5.2","status":200,"duration_ms":80,"usage_source":"upstream","usage_input_tokens":5,"usage_output_tokens":2}"#,
            r#"{"event":"request_complete","model":"ollama/glm-5.2","status":200,"duration_ms":90,"usage_source":"missing","usage_missing_reason":"upstream_missing_usage"}"#,
            r#"{"event":"request_complete","method":"GET","model":null,"upstream":"local","route_reason":"local_responses_probe","status":204,"duration_ms":1}"#,
        ]
        .join("\n");

        let summary = read_usage_summary_from_text(&text);
        let events = read_usage_events_from_text(&text, usize::MAX);

        assert_eq!(summary.requests, 3);
        assert_eq!(summary.total_tokens, Some(21));
        assert_eq!(summary.cache_hit_rate, Some(30.0));
        assert_eq!(summary.missing_usage_requests, 1);
        assert_eq!(events.len(), 3);
    }

    #[test]
    fn usage_summary_estimates_cost_from_priced_token_usage() {
        let text = [
            r#"{"event":"request_complete","model":"openai/example","status":200,"duration_ms":120,"usage_source":"upstream","usage_input_tokens":10,"usage_output_tokens":4,"usage_cached_input_tokens":3}"#,
            r#"{"event":"request_complete","model":"fallback","status":200,"duration_ms":80,"usage_source":"upstream","usage_input_tokens":10,"usage_output_tokens":1,"usage_cached_input_tokens":5}"#,
            r#"{"event":"request_complete","model":"missing-price","status":200,"duration_ms":70,"usage_source":"upstream","usage_input_tokens":9,"usage_output_tokens":1}"#,
            r#"{"event":"request_complete","model":"openai/example","status":200,"duration_ms":90,"usage_source":"missing","usage_missing_reason":"upstream_missing_usage"}"#,
        ]
        .join("\n");
        let pricing = HashMap::from([
            (
                "openai/example".to_string(),
                UsagePricing {
                    input_per_million: 2.0,
                    cached_input_per_million: Some(0.2),
                    output_per_million: 8.0,
                },
            ),
            (
                "fallback".to_string(),
                UsagePricing {
                    input_per_million: 1.0,
                    cached_input_per_million: None,
                    output_per_million: 3.0,
                },
            ),
        ]);

        let summary = read_usage_summary_from_text_with_pricing(&text, &pricing);

        let expected =
            ((7.0 * 2.0 + 3.0 * 0.2 + 4.0 * 8.0) + (10.0 * 1.0 + 1.0 * 3.0)) / 1_000_000.0;
        let actual = summary
            .estimated_cost_usd
            .expect("priced requests should produce an estimate");
        assert!((actual - expected).abs() < f64::EPSILON);
        assert!(summary
            .cost_label
            .contains("1 requests used input pricing for cached tokens"));
        assert!(summary
            .cost_label
            .contains("1 requests missing model pricing"));
        assert!(summary
            .cost_label
            .contains("1 requests missing token usage"));
    }

    #[test]
    fn usage_summary_reads_sqlite_requests_as_source_of_truth() {
        let root = unique_temp_dir("codexhub-usage-sqlite");
        fs::create_dir_all(&root).unwrap();
        let db_path = root.join("codex-proxy-telemetry.sqlite");
        let connection = rusqlite::Connection::open(&db_path).unwrap();
        connection
            .execute_batch(
                r#"
                CREATE TABLE gateway_requests (
                    request_id TEXT PRIMARY KEY,
                    completed_ts TEXT,
                    method TEXT,
                    path TEXT,
                    route_reason TEXT,
                    model TEXT,
                    model_requested TEXT,
                    model_canonical TEXT,
                    upstream TEXT,
                    provider_id TEXT,
                    status INTEGER,
                    duration_ms INTEGER,
                    usage_source TEXT,
                    usage_missing_reason TEXT,
                    usage_input_tokens INTEGER,
                    usage_cached_input_tokens INTEGER,
                    usage_output_tokens INTEGER,
                    usage_total_tokens INTEGER,
                    usage_reasoning_tokens INTEGER
                );
                INSERT INTO gateway_requests (
                    request_id, completed_ts, method, path, route_reason, model_canonical, upstream, provider_id, status,
                    duration_ms, usage_source, usage_input_tokens, usage_cached_input_tokens,
                    usage_output_tokens, usage_total_tokens
                ) VALUES
                    ('req-a', '2026-07-03T01:00:00Z', 'POST', '/v1/responses', 'model', 'openai/example', 'official', 'official', 200,
                     120, 'upstream', 10, 3, 4, 14),
                    ('req-b', '2026-07-03T01:00:01Z', 'POST', '/v1/chat/completions', 'model', 'fallback', 'external', 'external', 200,
                     80, 'upstream', 10, 5, 1, 11),
                    ('req-missing', '2026-07-03T01:00:02Z', 'POST', '/v1/responses', 'model', 'openai/example', 'official', 'official', 200,
                     90, 'missing', NULL, NULL, NULL, NULL),
                    ('req-failed', '2026-07-03T01:00:03Z', 'POST', '/v1/responses', 'model', 'openai/example', 'official', 'official', 502,
                     40, 'missing', NULL, NULL, NULL, NULL),
                    ('req-control', '2026-07-03T01:00:04Z', 'GET', '/v1/models', 'official_control', NULL, 'official', 'official', 200,
                     20, 'missing', NULL, NULL, NULL, NULL),
                    ('req-local', '2026-07-03T01:00:05Z', 'GET', '/v1/responses', 'local_responses_probe', NULL, 'local', 'local', 204,
                     1, NULL, NULL, NULL, NULL, NULL);
                "#,
            )
            .unwrap();

        let pricing = HashMap::from([
            (
                "openai/example".to_string(),
                UsagePricing {
                    input_per_million: 2.0,
                    cached_input_per_million: Some(0.2),
                    output_per_million: 8.0,
                },
            ),
            (
                "fallback".to_string(),
                UsagePricing {
                    input_per_million: 1.0,
                    cached_input_per_million: None,
                    output_per_million: 3.0,
                },
            ),
        ]);

        let events = read_usage_events_from_sqlite_path(&db_path, usize::MAX).unwrap();
        let summary = read_usage_summary_from_sqlite_path_with_pricing(&db_path, &pricing).unwrap();

        assert_eq!(events.len(), 3);
        assert_eq!(events[0].request_id.as_deref(), Some("req-a"));
        assert_eq!(summary.requests, 3);
        assert_eq!(summary.successful_requests, 3);
        assert_eq!(summary.total_tokens, Some(25));
        assert_eq!(summary.cache_hit_rate, Some(40.0));
        assert_eq!(summary.missing_usage_requests, 1);
        assert!(summary.estimated_cost_usd.is_some());
    }

    #[test]
    fn telemetry_backfill_imports_jsonl_to_sqlite_idempotently() {
        let root = unique_temp_dir("codexhub-usage-backfill");
        fs::create_dir_all(&root).unwrap();
        let log_path = root.join("codex-proxy-events.jsonl");
        let db_path = root.join("codex-proxy-telemetry.sqlite");
        fs::write(
            &log_path,
            [
                r#"{"ts":"2026-07-03T01:00:00Z","event":"request_start","request_id":"req-backfill","method":"POST","path":"/v1/responses","upstream":"official","model":"openai/gpt-5.5"}"#,
                r#"{"ts":"2026-07-03T01:00:03Z","event":"request_complete","request_id":"req-backfill","status":200,"duration_ms":3000,"usage_source":"upstream","usage_input_tokens":7,"usage_output_tokens":3,"upstream":"official","model":"openai/gpt-5.5"}"#,
            ]
            .join("\n"),
        )
        .unwrap();

        super::backfill_event_log_to_sqlite_path(&log_path, &db_path).unwrap();
        super::backfill_event_log_to_sqlite_path(&log_path, &db_path).unwrap();

        let events = read_usage_events_from_sqlite_path(&db_path, usize::MAX).unwrap();
        let connection = rusqlite::Connection::open(&db_path).unwrap();
        let event_count: i64 = connection
            .query_row("SELECT COUNT(*) FROM gateway_events", [], |row| row.get(0))
            .unwrap();
        let request_count: i64 = connection
            .query_row("SELECT COUNT(*) FROM gateway_requests", [], |row| {
                row.get(0)
            })
            .unwrap();
        let backfill_size: String = connection
            .query_row(
                "SELECT value FROM telemetry_meta WHERE key = 'last_backfill_size'",
                [],
                |row| row.get(0),
            )
            .unwrap();

        assert_eq!(events.len(), 1);
        assert_eq!(events[0].input_tokens, Some(7));
        assert_eq!(event_count, 2);
        assert_eq!(request_count, 1);
        assert_eq!(
            backfill_size,
            fs::metadata(&log_path).unwrap().len().to_string()
        );
    }

    #[test]
    fn telemetry_backfill_preserves_distinct_events_for_same_request_id() {
        let root = unique_temp_dir("codexhub-usage-backfill-duplicate-request");
        fs::create_dir_all(&root).unwrap();
        let log_path = root.join("codex-proxy-events.jsonl");
        let db_path = root.join("codex-proxy-telemetry.sqlite");
        fs::write(
            &log_path,
            [
                r#"{"ts":"2026-07-03T01:00:00Z","event":"request_error","request_id":"req-retry","status":502,"duration_ms":100,"usage_missing_reason":"upstream_error"}"#,
                r#"{"ts":"2026-07-03T01:00:01Z","event":"request_error","request_id":"req-retry","status":504,"duration_ms":200,"usage_missing_reason":"upstream_timeout"}"#,
                r#"{"duration_ms":200,"request_id":"req-retry","event":"request_error","usage_missing_reason":"upstream_timeout","status":504,"ts":"2026-07-03T01:00:01Z"}"#,
            ]
            .join("\n"),
        )
        .unwrap();

        super::backfill_event_log_to_sqlite_path(&log_path, &db_path).unwrap();
        super::backfill_event_log_to_sqlite_path(&log_path, &db_path).unwrap();

        let connection = rusqlite::Connection::open(&db_path).unwrap();
        let event_count: i64 = connection
            .query_row("SELECT COUNT(*) FROM gateway_events", [], |row| row.get(0))
            .unwrap();
        let request_count: i64 = connection
            .query_row("SELECT COUNT(*) FROM gateway_requests", [], |row| {
                row.get(0)
            })
            .unwrap();
        let request: (String, i64, i64, String) = connection
            .query_row(
                "SELECT completed_ts, status, duration_ms, usage_missing_reason FROM gateway_requests WHERE request_id = 'req-retry'",
                [],
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?)),
            )
            .unwrap();

        assert_eq!(event_count, 2);
        assert_eq!(request_count, 1);
        assert_eq!(
            request,
            (
                "2026-07-03T01:00:01Z".to_string(),
                504,
                200,
                "upstream_timeout".to_string()
            )
        );
    }

    #[test]
    fn usage_pricing_includes_openai_aliases_and_priority_fast_rates() {
        let pricing = usage_pricing_by_model();
        let base = super::lookup_usage_pricing(&pricing, "gpt-5.5").expect("gpt-5.5 pricing");
        let namespaced =
            super::lookup_usage_pricing(&pricing, "openai/gpt-5.5").expect("openai alias");
        let fast =
            super::lookup_usage_pricing(&pricing, "openai/gpt-5.5-fast").expect("fast pricing");

        assert_eq!(base.input_per_million, namespaced.input_per_million);
        assert_eq!(fast.input_per_million, 12.50);
        assert_eq!(fast.cached_input_per_million, Some(1.25));
        assert_eq!(fast.output_per_million, 75.00);
    }

    #[test]
    fn usage_pricing_includes_official_cached_input_rates() {
        let pricing = usage_pricing_by_model();

        assert_eq!(
            super::lookup_usage_pricing(&pricing, "gpt-5.5")
                .and_then(|pricing| pricing.cached_input_per_million),
            Some(0.50)
        );
        assert_eq!(
            super::lookup_usage_pricing(&pricing, "gpt-5.4")
                .and_then(|pricing| pricing.cached_input_per_million),
            Some(0.25)
        );
        assert_eq!(
            super::lookup_usage_pricing(&pricing, "gpt-5.4-mini")
                .and_then(|pricing| pricing.cached_input_per_million),
            Some(0.0375)
        );
    }

    #[test]
    fn opencode_apply_creates_backup_before_managed_overwrite() {
        let root = unique_temp_dir("codexhub-opencode");
        let config_path = root.join("opencode.json");
        let backup_root = root.join("backups");
        fs::create_dir_all(root.as_path()).unwrap();
        fs::write(&config_path, r#"{"model":"anthropic/claude-sonnet-4"}"#).unwrap();
        let settings = Settings::default();

        let result = apply_opencode_config_with_paths(
            &config_path,
            &backup_root,
            &settings,
            &[],
            "openai/gpt-5.5",
        )
        .unwrap();

        assert!(result.applied);
        assert!(result.backup_path.unwrap().exists());
        let written = fs::read_to_string(&config_path).unwrap();
        assert!(written.contains("codexhub"));
        assert!(!written.contains("codexhub_managed"));
        assert!(written.contains("openai/gpt-5.5"));
        assert!(written.contains("codexhub-proxy"));
    }

    #[test]
    fn opencode_apply_does_not_back_up_managed_config() {
        let root = unique_temp_dir("codexhub-opencode-managed");
        let config_path = root.join("opencode.json");
        let backup_root = root.join("backups");
        fs::create_dir_all(root.as_path()).unwrap();
        fs::write(
            &config_path,
            r#"{"model":"codexhub/openai/gpt-5.5","provider":{"codexhub":{"name":"CodexHub"}}}"#,
        )
        .unwrap();
        let settings = Settings::default();

        let result = apply_opencode_config_with_paths(
            &config_path,
            &backup_root,
            &settings,
            &[],
            "openai/gpt-5.4",
        )
        .unwrap();

        assert!(result.applied);
        assert!(result.backup_path.is_none());
        assert!(!backup_root
            .read_dir()
            .map(|mut entries| entries.next().is_some())
            .unwrap_or(false));
        let written = fs::read_to_string(&config_path).unwrap();
        assert!(!written.contains("codexhub_managed"));
        assert!(written.contains("openai/gpt-5.4"));
    }

    #[test]
    fn opencode_restore_skips_managed_backups_and_strips_invalid_keys() {
        let root = unique_temp_dir("codexhub-opencode-restore");
        let config_path = root.join("opencode.json");
        let backup_root = root.join("backups");
        fs::create_dir_all(backup_root.as_path()).unwrap();
        fs::write(&config_path, r#"{"model":"codexhub/openai/gpt-5.5"}"#).unwrap();
        let official_backup = backup_root.join("opencode-official.json");
        fs::write(
            &official_backup,
            r#"{"model":"anthropic/claude-sonnet-4","codexhub_managed":false}"#,
        )
        .unwrap();
        std::thread::sleep(std::time::Duration::from_millis(2));
        fs::write(
            backup_root.join("opencode-managed.json"),
            r#"{"model":"codexhub/openai/gpt-5.5","provider":{"codexhub":{"name":"CodexHub"}}}"#,
        )
        .unwrap();

        let result = restore_latest_backup("opencode", &config_path, &backup_root).unwrap();

        assert!(result.applied);
        assert_eq!(
            result.backup_path.as_deref(),
            Some(official_backup.as_path())
        );
        let written = fs::read_to_string(&config_path).unwrap();
        assert!(written.contains("anthropic/claude-sonnet-4"));
        assert!(!written.contains("codexhub_managed"));
        assert!(!written.contains("provider"));
    }

    #[test]
    fn pi_apply_writes_models_and_settings_with_backup() {
        let root = unique_temp_dir("codexhub-pi");
        let settings_path = root.join("settings.json");
        let models_path = root.join("models.json");
        let backup_root = root.join("backups");
        fs::create_dir_all(root.as_path()).unwrap();
        fs::write(
            &settings_path,
            r#"{"defaultProvider":"anthropic","defaultModel":"claude-sonnet-4","enabledModels":["anthropic/*"],"theme":"dark"}"#,
        )
        .unwrap();
        fs::write(
            &models_path,
            r#"{"providers":{"ollama":{"baseUrl":"http://localhost:11434/v1","api":"openai-completions","apiKey":"ollama","models":[{"id":"qwen2.5-coder:7b"}]}}}"#,
        )
        .unwrap();
        let settings = Settings::default();

        let result = super::apply_pi_config_with_paths(
            &settings_path,
            &models_path,
            &backup_root,
            &settings,
            &[],
            "openai/gpt-5.5",
        )
        .unwrap();

        assert!(result.applied);
        let backup_path = result.backup_path.unwrap();
        assert!(backup_path.join("settings.json").exists());
        assert!(backup_path.join("models.json").exists());
        let written_settings: serde_json::Value =
            serde_json::from_str(&fs::read_to_string(&settings_path).unwrap()).unwrap();
        assert_eq!(
            written_settings
                .get("defaultProvider")
                .and_then(serde_json::Value::as_str),
            Some("codexhub")
        );
        assert_eq!(
            written_settings
                .get("defaultModel")
                .and_then(serde_json::Value::as_str),
            Some("openai/gpt-5.5")
        );
        assert_eq!(
            written_settings
                .get("theme")
                .and_then(serde_json::Value::as_str),
            Some("dark")
        );
        assert!(written_settings
            .get("enabledModels")
            .and_then(serde_json::Value::as_array)
            .unwrap()
            .iter()
            .any(|value| value.as_str() == Some("codexhub/openai/gpt-5.5")));

        let written_models: serde_json::Value =
            serde_json::from_str(&fs::read_to_string(&models_path).unwrap()).unwrap();
        assert!(written_models.pointer("/providers/ollama").is_some());
        let provider = written_models.pointer("/providers/codexhub").unwrap();
        assert_eq!(
            provider.get("baseUrl").and_then(serde_json::Value::as_str),
            Some("http://127.0.0.1:9099/v1")
        );
        assert_eq!(
            provider.get("api").and_then(serde_json::Value::as_str),
            Some("openai-completions")
        );
        assert_eq!(
            provider.get("apiKey").and_then(serde_json::Value::as_str),
            Some("codexhub-proxy")
        );
        assert_eq!(
            provider
                .pointer("/models/0/id")
                .and_then(serde_json::Value::as_str),
            Some("openai/gpt-5.5")
        );
    }

    #[test]
    fn pi_restore_skips_managed_snapshot_and_restores_clean_pair() {
        let root = unique_temp_dir("codexhub-pi-restore");
        let settings_path = root.join("settings.json");
        let models_path = root.join("models.json");
        let backup_root = root.join("backups");
        let official_backup = backup_root.join("pi-official");
        let managed_backup = backup_root.join("pi-managed");
        fs::create_dir_all(official_backup.as_path()).unwrap();
        fs::create_dir_all(managed_backup.as_path()).unwrap();
        fs::write(
            &settings_path,
            r#"{"defaultProvider":"codexhub","defaultModel":"openai/gpt-5.5"}"#,
        )
        .unwrap();
        fs::write(
            &models_path,
            r#"{"providers":{"codexhub":{"models":[{"id":"openai/gpt-5.5"}]}}}"#,
        )
        .unwrap();
        fs::write(
            official_backup.join("settings.json"),
            r#"{"defaultProvider":"anthropic","defaultModel":"claude-sonnet-4"}"#,
        )
        .unwrap();
        fs::write(
            official_backup.join("models.json"),
            r#"{"providers":{"anthropic":{"baseUrl":"https://api.anthropic.com","api":"anthropic-messages","apiKey":"key","models":[{"id":"claude-sonnet-4"}]}}}"#,
        )
        .unwrap();
        std::thread::sleep(std::time::Duration::from_millis(2));
        fs::write(
            managed_backup.join("settings.json"),
            r#"{"defaultProvider":"codexhub","defaultModel":"openai/gpt-5.4"}"#,
        )
        .unwrap();
        fs::write(
            managed_backup.join("models.json"),
            r#"{"providers":{"codexhub":{"models":[{"id":"openai/gpt-5.4"}]}}}"#,
        )
        .unwrap();

        let result =
            super::restore_pi_config_with_paths(&settings_path, &models_path, &backup_root)
                .unwrap();

        assert!(result.applied);
        assert_eq!(
            result.backup_path.as_deref(),
            Some(official_backup.as_path())
        );
        let settings = fs::read_to_string(&settings_path).unwrap();
        let models = fs::read_to_string(&models_path).unwrap();
        assert!(settings.contains("anthropic"));
        assert!(models.contains("claude-sonnet-4"));
        assert!(!settings.contains("codexhub"));
        assert!(!models.contains("codexhub"));
    }

    #[test]
    fn omp_apply_writes_models_yml_and_model_roles_with_backup() {
        let root = unique_temp_dir("codexhub-omp");
        let config_path = root.join("config.yml");
        let models_path = root.join("models.yml");
        let backup_root = root.join("backups");
        fs::create_dir_all(root.as_path()).unwrap();
        fs::write(
            &config_path,
            "symbolPreset: unicode\ntheme:\n  dark: titanium\n  light: light\nmodelRoles:\n  default: ollama/qwen\n  vision: ollama/qwen-vision\n",
        )
        .unwrap();
        fs::write(
            &models_path,
            "providers:\n  ollama:\n    baseUrl: http://localhost:11434/v1\n    api: openai-completions\n    apiKey: ollama\n    models:\n      - id: qwen\n",
        )
        .unwrap();
        let settings = Settings::default();

        let result = super::apply_omp_config_with_paths(
            &config_path,
            &models_path,
            &backup_root,
            &settings,
            &[],
            "openai/gpt-5.5",
        )
        .unwrap();

        assert!(result.applied);
        let backup_path = result.backup_path.unwrap();
        assert!(backup_path.join("config.yml").exists());
        assert!(backup_path.join("models.yml").exists());
        let config = fs::read_to_string(&config_path).unwrap();
        assert!(config.contains("symbolPreset: unicode"));
        assert!(config.contains("modelRoles:\n  default: codexhub/openai/gpt-5.5"));
        assert!(config.contains("  vision: codexhub/openai/gpt-5.5"));
        let models = fs::read_to_string(&models_path).unwrap();
        assert!(models.contains("providers:\n  codexhub:"));
        assert!(models.contains("baseUrl: http://127.0.0.1:9099/v1"));
        assert!(models.contains("api: openai-completions"));
        assert!(models.contains("apiKey: codexhub-proxy"));
        assert!(models.contains("id: openai/gpt-5.5"));
    }

    #[test]
    fn zcode_apply_writes_user_catalog_with_schema_safe_provider() {
        let root = unique_temp_dir("codexhub-zcode");
        let catalog_path = root.join("model-providers").join("codexhub.json");
        let backup_root = root.join("backups");
        let settings = Settings::default();

        let result = super::apply_zcode_config_with_path(
            &catalog_path,
            &backup_root,
            &settings,
            &[],
            "openai/gpt-5.5",
        )
        .unwrap();

        assert!(result.applied);
        assert!(result.backup_path.is_none());
        let catalog: serde_json::Value =
            serde_json::from_str(&fs::read_to_string(&catalog_path).unwrap()).unwrap();
        assert_eq!(
            catalog
                .get("schemaVersion")
                .and_then(serde_json::Value::as_str),
            Some("zcode.model-providers.v2")
        );
        let provider = catalog.pointer("/providers/0").unwrap();
        assert_eq!(
            provider.get("id").and_then(serde_json::Value::as_str),
            Some("codexhub")
        );
        assert_eq!(
            provider.get("source").and_then(serde_json::Value::as_str),
            Some("custom")
        );
        assert_eq!(
            provider.get("apiKey").and_then(serde_json::Value::as_str),
            Some("codexhub-proxy")
        );
        assert_eq!(
            provider
                .pointer("/endpoints/baseURL")
                .and_then(serde_json::Value::as_str),
            Some("http://127.0.0.1:9099")
        );
        assert_eq!(
            provider
                .pointer("/endpoints/paths/openai-compatible")
                .and_then(serde_json::Value::as_str),
            Some("/v1/chat/completions")
        );
        assert_eq!(
            provider
                .pointer("/models/0/id")
                .and_then(serde_json::Value::as_str),
            Some("openai/gpt-5.5")
        );
        assert_eq!(
            provider
                .pointer("/models/0/defaultKind")
                .and_then(serde_json::Value::as_str),
            Some("openai-compatible")
        );
        assert!(!fs::read_to_string(&catalog_path)
            .unwrap()
            .contains("codexhub_managed"));
    }

    fn unique_temp_dir(prefix: &str) -> PathBuf {
        let millis = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_millis();
        std::env::temp_dir().join(format!("{prefix}-{millis}-{}", std::process::id()))
    }
}
