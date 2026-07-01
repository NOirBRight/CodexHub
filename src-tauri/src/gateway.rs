use crate::{config, models, Provider, Settings};
use reqwest::blocking::Client;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::fs;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

const REQUEST_TIMEOUT: Duration = Duration::from_secs(30);
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
        .timeout(REQUEST_TIMEOUT)
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
    let text = read_event_log_text()?;
    Ok(read_usage_summary_from_text(&text))
}

pub fn gateway_usage_events(limit: Option<usize>) -> Result<Vec<GatewayUsageEvent>, String> {
    let limit = limit.unwrap_or(100).clamp(1, 500);
    let text = read_event_log_text()?;
    Ok(read_usage_events_from_text(&text, limit))
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

pub fn list_gateway_clients() -> Result<Vec<GatewayClientInfo>, String> {
    let opencode_path = detect_opencode_config_path();
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
    clients.push(GatewayClientInfo {
        id: "opencode".to_string(),
        name: "OpenCode".to_string(),
        kind: "Terminal client".to_string(),
        installed: opencode_path.as_ref().map(|path| path.exists()).unwrap_or(false),
        auto_apply_supported: opencode_path.is_some(),
        config_path: opencode_path,
        route_mode: "unknown".to_string(),
        status: "Managed overwrite with backup is supported when config exists.".to_string(),
        current_version: None,
        latest_version: None,
    });
    for (id, name, kind, path) in [
        ("zcode", "ZCode", "IDE extension", "%APPDATA%/ZCode/settings.json"),
        ("pi", "Pi", "Compact CLI", "~/.config/pi/config.json"),
        ("omp", "OMP", "Prompt runtime", "~/.config/omp/settings.json"),
    ] {
        clients.push(GatewayClientInfo {
            id: id.to_string(),
            name: name.to_string(),
            kind: kind.to_string(),
            installed: false,
            auto_apply_supported: false,
            config_path: Some(PathBuf::from(path)),
            route_mode: "official".to_string(),
            status: "Preview/copy only until a schema-safe adapter is implemented.".to_string(),
            current_version: None,
            latest_version: None,
        });
    }
    Ok(clients)
}

pub fn preview_gateway_client_config(
    client_id: String,
    model: Option<String>,
) -> Result<GatewayClientConfigPreview, String> {
    let settings = config::get_settings()?;
    let model = model.unwrap_or_else(|| DEFAULT_MODEL.to_string());
    let id = normalize_client_id(&client_id);
    if id == "opencode" {
        let path = detect_opencode_config_path()
            .ok_or_else(|| "OpenCode config path could not be resolved".to_string())?;
        return preview_opencode_config_with_path(&path, &settings, &model);
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
    let model = model.unwrap_or_else(|| DEFAULT_MODEL.to_string());
    let id = normalize_client_id(&client_id);
    if id != "opencode" {
        return Ok(GatewayClientApplyResult {
            client_id: id,
            applied: false,
            config_path: None,
            backup_path: None,
            message: "This client is copy-only until a schema-safe adapter is implemented.".to_string(),
        });
    }
    let path = detect_opencode_config_path()
        .ok_or_else(|| "OpenCode config path could not be resolved".to_string())?;
    apply_opencode_config_with_paths(&path, &client_backup_root("opencode"), &settings, &model)
}

pub fn restore_gateway_client_config(client_id: String) -> Result<GatewayClientApplyResult, String> {
    let id = normalize_client_id(&client_id);
    if id != "opencode" {
        return Ok(GatewayClientApplyResult {
            client_id: id,
            applied: false,
            config_path: None,
            backup_path: None,
            message: "Restore is only implemented for OpenCode in this release.".to_string(),
        });
    }
    let path = detect_opencode_config_path()
        .ok_or_else(|| "OpenCode config path could not be resolved".to_string())?;
    restore_latest_backup("opencode", &path, &client_backup_root("opencode"))
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
            .map(|item| item.upstream_model.clone().unwrap_or_else(|| item.id.clone()))
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
                        if stream && first_token_ms.is_none() && has_nonempty_payload(&buffer[..count]) {
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
            issue: Some("Codex auth file is missing; log in with Codex CLI or Codex App first.".to_string()),
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
        Some("Codex auth mode is not chatgpt; Gateway requires local Codex/ChatGPT auth.".to_string())
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

fn official_models() -> Vec<GatewayModel> {
    OFFICIAL_MODELS
        .iter()
        .map(|(id, display_name, context_window)| GatewayModel {
            id: (*id).to_string(),
            display_name: (*display_name).to_string(),
            source: "Official Codex subscription".to_string(),
            source_kind: "official".to_string(),
            supports_responses: true,
            supports_chat_completions: true,
            context_window: *context_window,
        })
        .collect()
}

fn gateway_models_from_config(settings: &Settings, providers: &[Provider]) -> Vec<GatewayModel> {
    let mut output = Vec::new();
    if settings.include_official_models {
        output.extend(official_models());
    }
    for provider in providers {
        if !provider.enabled || provider.hidden {
            continue;
        }
        for model in &provider.models {
            if !model.enabled || model.hidden || !model.gateway_exported {
                continue;
            }
            output.push(GatewayModel {
                id: model.id.clone(),
                display_name: model
                    .display_name
                    .clone()
                    .unwrap_or_else(|| model.id.clone()),
                source: provider.name.clone(),
                source_kind: "external".to_string(),
                supports_responses: provider
                    .upstream_format
                    .as_ref()
                    .map(|format| !matches!(format, crate::UpstreamFormat::ChatCompletions))
                    .unwrap_or(true),
                supports_chat_completions: true,
                context_window: model.context_window.unwrap_or_default(),
            });
        }
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

fn read_recent_events(limit: usize, filter: Option<fn(&GatewayEvent) -> bool>) -> Vec<GatewayEvent> {
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
        Err(error) => Err(format!("failed to read event log {}: {error}", path.display())),
    }
}

fn read_usage_summary_from_text(text: &str) -> GatewayUsageSummary {
    let events = read_usage_events_from_text(text, usize::MAX);
    let requests = events.len() as u64;
    let successful_requests = events
        .iter()
        .filter(|event| event.status.map(|status| (200..300).contains(&status)).unwrap_or(false))
        .count() as u64;
    let missing_usage_requests = events
        .iter()
        .filter(|event| event.usage_source == "missing")
        .count() as u64;
    let input_tokens = sum_optional(events.iter().map(|event| event.input_tokens));
    let output_tokens = sum_optional(events.iter().map(|event| event.output_tokens));
    let total_tokens = sum_optional(events.iter().map(|event| event.total_tokens)).or_else(|| {
        match (input_tokens, output_tokens) {
            (Some(input), Some(output)) => Some(input + output),
            _ => None,
        }
    });
    let cached_input_tokens = sum_optional(events.iter().map(|event| event.cached_input_tokens));
    let cache_hit_rate = match (cached_input_tokens, input_tokens) {
        (Some(cached), Some(input)) if input > 0 => {
            Some(((cached as f64 / input as f64) * 1000.0).round() / 10.0)
        }
        _ => None,
    };

    GatewayUsageSummary {
        requests,
        successful_requests,
        missing_usage_requests,
        total_tokens,
        input_tokens,
        output_tokens,
        cached_input_tokens,
        cache_hit_rate,
        estimated_cost_usd: None,
        cost_label: "Unknown until pricing metadata is available".to_string(),
    }
}

fn read_usage_events_from_text(text: &str, limit: usize) -> Vec<GatewayUsageEvent> {
    let mut events = Vec::new();
    for line in text.lines().rev() {
        if events.len() >= limit {
            break;
        }
        let Ok(value) = serde_json::from_str::<Value>(line) else {
            continue;
        };
        if value.get("event").and_then(Value::as_str) != Some("request_complete") {
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
            cached_input_tokens: value.get("usage_cached_input_tokens").and_then(Value::as_u64),
            reasoning_tokens: value.get("usage_reasoning_tokens").and_then(Value::as_u64),
        });
    }
    events.reverse();
    events
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
        "request_error" if upstream == "official" || detail.contains("codex auth") || detail.contains("token") => {
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
    candidates.into_iter().find(|path| path.exists()).or_else(|| {
        dirs::home_dir().map(|home| home.join(".config").join("opencode").join("opencode.json"))
    })
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
    model: &str,
) -> Result<GatewayClientConfigPreview, String> {
    let current = fs::read_to_string(config_path).ok().map(|text| sanitize_text(&text));
    let next = opencode_config_text(settings, model)?;
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

fn apply_opencode_config_with_paths(
    config_path: &Path,
    backup_root: &Path,
    settings: &Settings,
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
    let backup_path = backup_root.join(format!("opencode-{}.json", timestamp_millis()));
    fs::copy(config_path, &backup_path).map_err(|error| {
        format!(
            "failed to back up OpenCode config {} to {}: {error}",
            config_path.display(),
            backup_path.display()
        )
    })?;
    let next = opencode_config_text(settings, model)?;
    write_text_replace(config_path, &next)?;
    Ok(GatewayClientApplyResult {
        client_id: "opencode".to_string(),
        applied: true,
        config_path: Some(config_path.to_path_buf()),
        backup_path: Some(backup_path),
        message: "OpenCode now routes through CodexHub Gateway. The previous config was backed up.".to_string(),
    })
}

fn restore_latest_backup(
    client_id: &str,
    config_path: &Path,
    backup_root: &Path,
) -> Result<GatewayClientApplyResult, String> {
    let latest = fs::read_dir(backup_root)
        .map_err(|error| format!("failed to read backup directory {}: {error}", backup_root.display()))?
        .filter_map(Result::ok)
        .filter_map(|entry| {
            let metadata = entry.metadata().ok()?;
            let modified = metadata.modified().ok()?;
            Some((modified, entry.path()))
        })
        .max_by_key(|(modified, _)| *modified)
        .map(|(_, path)| path)
        .ok_or_else(|| format!("no backup is available for {client_id}"))?;
    let text = fs::read_to_string(&latest)
        .map_err(|error| format!("failed to read backup {}: {error}", latest.display()))?;
    write_text_replace(config_path, &text)?;
    Ok(GatewayClientApplyResult {
        client_id: client_id.to_string(),
        applied: true,
        config_path: Some(config_path.to_path_buf()),
        backup_path: Some(latest),
        message: "Restored the latest official client config backup.".to_string(),
    })
}

fn opencode_config_text(settings: &Settings, model: &str) -> Result<String, String> {
    let base_url = endpoints(settings.proxy_port).base_url;
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
                "models": {
                    model: {
                        "name": model,
                    }
                }
            }
        },
        "codexhub_managed": {
            "strategy": "managed_overwrite",
            "route": "hub",
        }
    });
    serde_json::to_string_pretty(&body)
        .map(|text| format!("{text}\n"))
        .map_err(|error| format!("failed to serialize OpenCode config: {error}"))
}

fn write_text_replace(path: &Path, text: &str) -> Result<(), String> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent).map_err(|error| {
            format!("failed to create config directory {}: {error}", parent.display())
        })?;
    }
    let temp_path = path.with_extension("tmp-codexhub");
    fs::write(&temp_path, text)
        .map_err(|error| format!("failed to write temp config {}: {error}", temp_path.display()))?;
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
    text.lines()
        .any(|line| line.starts_with("data:") && line.trim() != "data:" && line.trim() != "data: [DONE]")
}

#[cfg(test)]
mod tests {
    use super::{
        apply_opencode_config_with_paths, gateway_models_from_config, read_usage_summary_from_text,
        sanitize_event, sanitize_text,
    };
    use crate::{Model, Provider, Settings};
    use serde_json::json;
    use std::fs;
    use std::path::PathBuf;
    use std::time::{SystemTime, UNIX_EPOCH};

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
    fn gateway_models_export_selected_non_hidden_hub_models() {
        let settings = Settings::default();
        let providers = vec![Provider {
            id: "minimax".to_string(),
            name: "MiniMax".to_string(),
            base_url: "https://api.minimax.chat/v1".to_string(),
            api_key: None,
            upstream_format: None,
            display_prefix: Some("minimax/".to_string()),
            sort_order: None,
            enabled: true,
            hidden: false,
            locked: false,
            models: vec![
                Model {
                    id: "minimax/minimax-m3".to_string(),
                    display_name: Some("MiniMax M3".to_string()),
                    context_window: Some(1_000_000),
                    gateway_exported: true,
                    hidden: false,
                    ..Model::default()
                },
                Model {
                    id: "minimax/minimax-m3-lite".to_string(),
                    gateway_exported: false,
                    hidden: false,
                    ..Model::default()
                },
                Model {
                    id: "minimax/hidden".to_string(),
                    gateway_exported: true,
                    hidden: true,
                    ..Model::default()
                },
            ],
        }];

        let models = gateway_models_from_config(&settings, &providers);

        assert!(models.iter().any(|model| model.id == "openai/gpt-5.5"));
        assert!(models.iter().any(|model| model.id == "minimax/minimax-m3"));
        assert!(!models.iter().any(|model| model.id == "minimax/minimax-m3-lite"));
        assert!(!models.iter().any(|model| model.id == "minimax/hidden"));
    }

    #[test]
    fn usage_summary_counts_missing_usage_without_estimating_tokens() {
        let text = [
            r#"{"event":"request_complete","model":"openai/gpt-5.5","status":200,"duration_ms":120,"usage_source":"upstream","usage_input_tokens":10,"usage_output_tokens":4,"usage_cached_input_tokens":3}"#,
            r#"{"event":"request_complete","model":"ollama/glm-5.2","status":200,"duration_ms":90,"usage_source":"missing","usage_missing_reason":"upstream_missing_usage"}"#,
        ]
        .join("\n");

        let summary = read_usage_summary_from_text(&text);

        assert_eq!(summary.requests, 2);
        assert_eq!(summary.total_tokens, Some(14));
        assert_eq!(summary.cache_hit_rate, Some(30.0));
        assert_eq!(summary.missing_usage_requests, 1);
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
            "openai/gpt-5.5",
        )
        .unwrap();

        assert!(result.applied);
        assert!(result.backup_path.unwrap().exists());
        let written = fs::read_to_string(&config_path).unwrap();
        assert!(written.contains("codexhub"));
        assert!(written.contains("openai/gpt-5.5"));
        assert!(written.contains("codexhub-proxy"));
    }

    fn unique_temp_dir(prefix: &str) -> PathBuf {
        let millis = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_millis();
        std::env::temp_dir().join(format!("{prefix}-{millis}-{}", std::process::id()))
    }
}
