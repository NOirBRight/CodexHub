use crate::{autostart, catalog, config, gateway, history, models, openai_usage, proxy};
use serde::Deserialize;
use serde_json::{json, Value};
use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};

const DEFAULT_ADDR: &str = "127.0.0.1:1421";
const INVOKE_PATH: &str = "/api/invoke";
const MAX_BODY_BYTES: usize = 1024 * 1024;

#[derive(Debug, Deserialize)]
struct InvokeRequest {
    command: String,
    #[serde(default)]
    args: Value,
}

pub fn run(args: &[String]) -> i32 {
    let addr = parse_addr(args).unwrap_or_else(|| DEFAULT_ADDR.to_string());
    gateway::start_telemetry_ingester();
    match TcpListener::bind(&addr) {
        Ok(listener) => {
            println!("CodexHub web bridge listening on http://{addr}");
            for stream in listener.incoming() {
                match stream {
                    Ok(stream) => {
                        std::thread::spawn(move || handle_stream(stream));
                    }
                    Err(error) => eprintln!("web bridge connection failed: {error}"),
                }
            }
            0
        }
        Err(error) => {
            eprintln!("failed to bind CodexHub web bridge on {addr}: {error}");
            1
        }
    }
}

fn parse_addr(args: &[String]) -> Option<String> {
    let mut index = 0;
    while index < args.len() {
        if args[index] == "--addr" {
            return args.get(index + 1).cloned();
        }
        if args[index] == "--port" {
            return args.get(index + 1).map(|port| format!("127.0.0.1:{port}"));
        }
        index += 1;
    }
    None
}

fn handle_stream(mut stream: TcpStream) {
    let response = match read_request(&mut stream).and_then(handle_request) {
        Ok(response) => response,
        Err(error) => BridgeResponse::error(500, error),
    };
    let _ = stream.write_all(&response.into_bytes());
}

fn read_request(stream: &mut TcpStream) -> Result<BridgeRequest, String> {
    let mut buffer = Vec::new();
    let mut chunk = [0_u8; 4096];
    let header_end = loop {
        let count = stream
            .read(&mut chunk)
            .map_err(|error| format!("failed to read bridge request: {error}"))?;
        if count == 0 {
            return Err("empty bridge request".to_string());
        }
        buffer.extend_from_slice(&chunk[..count]);
        if buffer.len() > MAX_BODY_BYTES {
            return Err("bridge request is too large".to_string());
        }
        if let Some(position) = find_header_end(&buffer) {
            break position;
        }
    };

    let header_text = String::from_utf8_lossy(&buffer[..header_end]).to_string();
    let mut lines = header_text.split("\r\n");
    let request_line = lines
        .next()
        .ok_or_else(|| "bridge request line is missing".to_string())?;
    let mut parts = request_line.split_whitespace();
    let method = parts
        .next()
        .ok_or_else(|| "bridge request method is missing".to_string())?
        .to_string();
    let path = parts
        .next()
        .ok_or_else(|| "bridge request path is missing".to_string())?
        .to_string();

    let mut origin = None;
    let mut content_length = 0_usize;
    for line in lines {
        if let Some((name, value)) = line.split_once(':') {
            let name = name.trim().to_ascii_lowercase();
            let value = value.trim();
            if name == "origin" {
                origin = Some(value.to_string());
            } else if name == "content-length" {
                content_length = value
                    .parse::<usize>()
                    .map_err(|_| "invalid bridge content-length".to_string())?;
            }
        }
    }
    if content_length > MAX_BODY_BYTES {
        return Err("bridge request is too large".to_string());
    }

    let body_start = header_end + 4;
    while buffer.len().saturating_sub(body_start) < content_length {
        let count = stream
            .read(&mut chunk)
            .map_err(|error| format!("failed to read bridge body: {error}"))?;
        if count == 0 {
            break;
        }
        buffer.extend_from_slice(&chunk[..count]);
        if buffer.len().saturating_sub(body_start) > MAX_BODY_BYTES {
            return Err("bridge request is too large".to_string());
        }
    }

    let body = buffer
        .get(body_start..body_start + content_length)
        .unwrap_or_default()
        .to_vec();

    Ok(BridgeRequest {
        method,
        path,
        origin,
        body,
    })
}

fn find_header_end(buffer: &[u8]) -> Option<usize> {
    buffer.windows(4).position(|window| window == b"\r\n\r\n")
}

fn handle_request(request: BridgeRequest) -> Result<BridgeResponse, String> {
    if !origin_allowed(request.origin.as_deref()) {
        return Ok(BridgeResponse::error(
            403,
            "origin is not allowed for CodexHub web bridge".to_string(),
        ));
    }
    if request.method == "OPTIONS" {
        return Ok(BridgeResponse::empty(204));
    }
    if request.method != "POST" || request.path != INVOKE_PATH {
        return Ok(BridgeResponse::error(
            404,
            "unknown CodexHub web bridge route".to_string(),
        ));
    }

    let invoke: InvokeRequest = serde_json::from_slice(&request.body)
        .map_err(|error| format!("invalid bridge invoke JSON: {error}"))?;
    let value = match dispatch(invoke) {
        Ok(value) => value,
        Err(error) => return Ok(BridgeResponse::error(500, error)),
    };
    Ok(BridgeResponse::json(
        200,
        json!({ "ok": true, "value": value }),
    ))
}

fn origin_allowed(origin: Option<&str>) -> bool {
    let Some(origin) = origin else {
        return true;
    };
    let Some(port) = origin
        .strip_prefix("http://127.0.0.1:")
        .or_else(|| origin.strip_prefix("http://localhost:"))
        .and_then(|port| port.parse::<u16>().ok())
    else {
        return false;
    };
    port >= 1024
}

fn dispatch(request: InvokeRequest) -> Result<Value, String> {
    match request.command.as_str() {
        "get_status" => to_value(proxy::status()),
        "switch_mode" => {
            let mode = string_arg(&request.args, "mode")?;
            let auto_sync = bool_arg(&request.args, "autoSync")?;
            to_value(config::switch_mode(&mode, auto_sync))
        }
        "start_proxy" => to_value(proxy::start()),
        "stop_proxy" => to_value(proxy::stop()),
        "restart_proxy" => to_value(proxy::restart()),
        "get_providers" => to_value(config::get_providers()),
        "save_providers" => {
            let providers = serde_json::from_value(
                request
                    .args
                    .get("providers")
                    .cloned()
                    .ok_or_else(|| "providers argument is required".to_string())?,
            )
            .map_err(|error| format!("invalid providers argument: {error}"))?;
            to_value(config::save_providers(providers))
        }
        "get_settings" => to_value(config::get_settings()),
        "save_settings" => {
            let settings = serde_json::from_value(
                request
                    .args
                    .get("settings")
                    .cloned()
                    .ok_or_else(|| "settings argument is required".to_string())?,
            )
            .map_err(|error| format!("invalid settings argument: {error}"))?;
            to_value(config::save_settings(settings))
        }
        "refresh_official_models" => to_value(models::refresh_official_models()),
        "openai_usage_completions" => {
            let start_time = optional_u64_arg(&request.args, &["startTime", "start_time"]);
            let end_time = optional_u64_arg(&request.args, &["endTime", "end_time"]);
            let force_refresh =
                optional_bool_arg(&request.args, &["forceRefresh", "force_refresh"]);
            to_value(openai_usage::openai_usage_completions(
                start_time,
                end_time,
                force_refresh,
            ))
        }
        "discover_provider_models" => {
            let base_url = string_arg(&request.args, "baseUrl")?;
            let api_key = string_arg(&request.args, "apiKey")?;
            to_value(models::discover_provider_models(&base_url, &api_key))
        }
        "probe_upstream_format" => {
            let base_url = string_arg(&request.args, "baseUrl")?;
            let api_key = string_arg(&request.args, "apiKey")?;
            let model = request
                .args
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
            let provider_id = string_arg(&request.args, "providerId")?;
            let model = request
                .args
                .get("model")
                .and_then(Value::as_str)
                .map(ToOwned::to_owned);
            to_value(gateway::provider_probe_upstream_format(provider_id, model))
        }
        "test_model_endpoint" => {
            let base_url = string_arg(&request.args, "baseUrl")?;
            let api_key = string_arg(&request.args, "apiKey")?;
            let model = string_arg(&request.args, "model")?;
            let upstream_format = serde_json::from_value(
                request
                    .args
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
                request
                    .args
                    .get("kind")
                    .cloned()
                    .ok_or_else(|| "kind argument is required".to_string())?,
            )
            .map_err(|error| format!("invalid gateway test kind: {error}"))?;
            let model = request
                .args
                .get("model")
                .and_then(Value::as_str)
                .map(ToOwned::to_owned);
            to_value(gateway::gateway_test_request(kind, model))
        }
        "gateway_recent_events" => {
            let limit = request
                .args
                .get("limit")
                .and_then(Value::as_u64)
                .and_then(|value| usize::try_from(value).ok());
            let since_ts = optional_string_arg(&request.args, &["sinceTs", "since_ts"]);
            to_value(gateway::gateway_recent_events(limit, since_ts))
        }
        "gateway_usage_summary" => {
            let start_ts = optional_string_arg(&request.args, &["startTs", "start_ts"]);
            let end_ts = optional_string_arg(&request.args, &["endTs", "end_ts"]);
            to_value(gateway::gateway_usage_summary(start_ts, end_ts))
        }
        "gateway_usage_snapshot" => {
            let limit = request
                .args
                .get("limit")
                .and_then(Value::as_u64)
                .and_then(|value| usize::try_from(value).ok());
            let start_ts = optional_string_arg(&request.args, &["startTs", "start_ts"]);
            let end_ts = optional_string_arg(&request.args, &["endTs", "end_ts"]);
            to_value(gateway::gateway_usage_snapshot(limit, start_ts, end_ts))
        }
        "gateway_usage_events" => {
            let limit = request
                .args
                .get("limit")
                .and_then(Value::as_u64)
                .and_then(|value| usize::try_from(value).ok());
            let start_ts = optional_string_arg(&request.args, &["startTs", "start_ts"]);
            let end_ts = optional_string_arg(&request.args, &["endTs", "end_ts"]);
            to_value(gateway::gateway_usage_events(limit, start_ts, end_ts))
        }
        "gateway_copy_client_config" => {
            let client_kind = request
                .args
                .get("clientKind")
                .and_then(Value::as_str)
                .map(ToOwned::to_owned);
            let model = request
                .args
                .get("model")
                .and_then(Value::as_str)
                .map(ToOwned::to_owned);
            to_value(gateway::gateway_copy_client_config(client_kind, model))
        }
        "list_gateway_clients" => {
            let include_versions = request
                .args
                .get("includeVersions")
                .or_else(|| request.args.get("include_versions"))
                .and_then(Value::as_bool)
                .unwrap_or(false);
            to_value(gateway::list_gateway_clients(include_versions))
        }
        "preview_gateway_client_config" => {
            let client_id = string_arg(&request.args, "clientId")?;
            let model = request
                .args
                .get("model")
                .and_then(Value::as_str)
                .map(ToOwned::to_owned);
            to_value(gateway::preview_gateway_client_config(client_id, model))
        }
        "apply_gateway_client_config" => {
            let client_id = string_arg(&request.args, "clientId")?;
            let model = request
                .args
                .get("model")
                .and_then(Value::as_str)
                .map(ToOwned::to_owned);
            to_value(gateway::apply_gateway_client_config(client_id, model))
        }
        "restore_gateway_client_config" => {
            let client_id = string_arg(&request.args, "clientId")?;
            to_value(gateway::restore_gateway_client_config(client_id))
        }
        "switch_gateway_client_route" => {
            let client_id = string_arg(&request.args, "clientId")?;
            let mode = string_arg(&request.args, "mode")?;
            let model = request
                .args
                .get("model")
                .and_then(Value::as_str)
                .map(ToOwned::to_owned);
            to_value(gateway::switch_gateway_client_route(client_id, mode, model))
        }
        "sync_gateway_clients" => {
            let model = request
                .args
                .get("model")
                .and_then(Value::as_str)
                .map(ToOwned::to_owned);
            to_value(gateway::sync_gateway_clients(model))
        }
        "subagent_matrix_status" => to_value(gateway::subagent_matrix_status()),
        "generate_catalog" => to_value(catalog::generate_catalog()),
        "list_models" => to_value(models::list_models()),
        "refresh_model_metadata" => to_value(models::refresh_model_metadata()),
        "list_model_metadata" => to_value(models::list_model_metadata()),
        "save_model_metadata_override" => {
            let model = serde_json::from_value(
                request
                    .args
                    .get("model")
                    .cloned()
                    .ok_or_else(|| "model argument is required".to_string())?,
            )
            .map_err(|error| format!("invalid model argument: {error}"))?;
            to_value(models::save_model_metadata_override(model))
        }
        "sync_history" => {
            let target_provider = request
                .args
                .get("targetProvider")
                .and_then(Value::as_str)
                .map(ToOwned::to_owned);
            to_value(history::sync_history(target_provider.as_deref()))
        }
        "migrate_official_history_to_unified" => {
            to_value(history::migrate_official_history_to_unified())
        }
        "restore_official_history_from_unified" => {
            to_value(history::restore_official_history_from_unified())
        }
        "sync_catalog" => to_value(catalog::sync_catalog()),
        "set_autostart" => to_value(autostart::set_autostart(bool_arg(
            &request.args,
            "enabled",
        )?)),
        "remove_autostart" => to_value(autostart::remove_autostart()),
        "open_codex_app" => to_value(crate::open_codex_app()),
        command => Err(format!("unknown CodexHub command: {command}")),
    }
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

fn optional_string_arg(args: &Value, names: &[&str]) -> Option<String> {
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

fn optional_bool_arg(args: &Value, names: &[&str]) -> Option<bool> {
    names
        .iter()
        .find_map(|name| args.get(*name).and_then(Value::as_bool))
}

#[derive(Debug)]
struct BridgeRequest {
    method: String,
    path: String,
    origin: Option<String>,
    body: Vec<u8>,
}

#[derive(Debug)]
struct BridgeResponse {
    status: u16,
    body: Vec<u8>,
}

impl BridgeResponse {
    fn empty(status: u16) -> Self {
        Self {
            status,
            body: Vec::new(),
        }
    }

    fn json(status: u16, value: Value) -> Self {
        Self {
            status,
            body: serde_json::to_vec(&value).unwrap_or_default(),
        }
    }

    fn error(status: u16, error: String) -> Self {
        Self::json(status, json!({ "ok": false, "error": error }))
    }

    fn into_bytes(self) -> Vec<u8> {
        let reason = match self.status {
            200 => "OK",
            204 => "No Content",
            403 => "Forbidden",
            404 => "Not Found",
            _ => "Internal Server Error",
        };
        let header = format!(
            "HTTP/1.1 {} {}\r\ncontent-type: application/json\r\ncontent-length: {}\r\naccess-control-allow-origin: *\r\naccess-control-allow-methods: POST, OPTIONS\r\naccess-control-allow-headers: content-type\r\nconnection: close\r\n\r\n",
            self.status,
            reason,
            self.body.len()
        );
        let mut bytes = header.into_bytes();
        bytes.extend_from_slice(&self.body);
        bytes
    }
}

#[cfg(test)]
mod tests {
    use super::{handle_request, origin_allowed, BridgeRequest};
    use serde_json::json;

    #[test]
    fn origin_policy_allows_only_dev_frontend() {
        assert!(origin_allowed(None));
        assert!(origin_allowed(Some("http://127.0.0.1:1420")));
        assert!(origin_allowed(Some("http://localhost:1420")));
        assert!(!origin_allowed(Some("http://example.com")));
    }

    #[test]
    fn options_preflight_succeeds() {
        let response = handle_request(BridgeRequest {
            method: "OPTIONS".to_string(),
            path: "/api/invoke".to_string(),
            origin: Some("http://127.0.0.1:1420".to_string()),
            body: Vec::new(),
        })
        .expect("preflight");

        assert_eq!(response.status, 204);
    }

    #[test]
    fn unknown_command_returns_error() {
        let response = handle_request(BridgeRequest {
            method: "POST".to_string(),
            path: "/api/invoke".to_string(),
            origin: Some("http://127.0.0.1:1420".to_string()),
            body: serde_json::to_vec(&json!({
                "command": "missing_command",
                "args": {}
            }))
            .unwrap(),
        })
        .expect("invoke");

        assert_eq!(response.status, 500);
        assert!(String::from_utf8_lossy(&response.body).contains("missing_command"));
    }
}
