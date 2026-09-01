use crate::gateway;
use serde::Deserialize;
use serde_json::{json, Value};
use std::io::{ErrorKind, Read, Write};
use std::net::{TcpListener, TcpStream};
use std::sync::atomic::{AtomicBool, Ordering};
use tauri::AppHandle;

const INVOKE_PATH: &str = "/api/invoke";
const MAX_BODY_BYTES: usize = 1024 * 1024;
static BACKGROUND_BRIDGE_STARTED: AtomicBool = AtomicBool::new(false);

fn default_addr() -> String {
    crate::app_flavor::bridge_addr()
}

#[derive(Debug, Deserialize)]
struct InvokeRequest {
    command: String,
    #[serde(default)]
    args: Value,
}

pub fn run(args: &[String]) -> i32 {
    let addr = parse_addr(args).unwrap_or_else(default_addr);
    gateway::start_telemetry_ingester();
    match TcpListener::bind(&addr) {
        Ok(listener) => {
            println!("CodexHub web bridge listening on http://{addr}");
            serve(listener, None);
            0
        }
        Err(error) => {
            eprintln!("failed to bind CodexHub web bridge on {addr}: {error}");
            1
        }
    }
}

pub fn start_background(app: AppHandle) -> Result<(), String> {
    if BACKGROUND_BRIDGE_STARTED.swap(true, Ordering::AcqRel) {
        return Ok(());
    }

    std::thread::Builder::new()
        .name("codexhub-web-bridge".to_string())
        .spawn(move || {
            gateway::start_telemetry_ingester();
            let addr = default_addr();
            match TcpListener::bind(&addr) {
                Ok(listener) => serve(listener, Some(app)),
                Err(error) if error.kind() == ErrorKind::AddrInUse => {
                    eprintln!("CodexHub web bridge already listening on http://{addr}");
                }
                Err(error) => {
                    eprintln!("failed to bind CodexHub web bridge on {addr}: {error}");
                }
            }
        })
        .map(|_| ())
        .map_err(|error| {
            BACKGROUND_BRIDGE_STARTED.store(false, Ordering::Release);
            format!("failed to start CodexHub web bridge thread: {error}")
        })
}

fn serve(listener: TcpListener, app: Option<AppHandle>) {
    for stream in listener.incoming() {
        match stream {
            Ok(stream) => {
                let app = app.clone();
                std::thread::spawn(move || handle_stream(stream, app));
            }
            Err(error) => eprintln!("web bridge connection failed: {error}"),
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

fn handle_stream(mut stream: TcpStream, app: Option<AppHandle>) {
    let response = match read_request(&mut stream).and_then(|request| handle_request(request, app))
    {
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

fn handle_request(
    request: BridgeRequest,
    app: Option<AppHandle>,
) -> Result<BridgeResponse, String> {
    if !origin_allowed(request.origin.as_deref()) {
        return Ok(BridgeResponse::typed_error(
            403,
            "origin is not allowed for CodexHub web bridge".to_string(),
            BridgeErrorKind::OriginNotAllowed,
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
    let value = match dispatch(invoke, app) {
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

fn dispatch(request: InvokeRequest, app: Option<AppHandle>) -> Result<Value, String> {
    crate::desktop_commands::dispatch_web(&request.command, &request.args, app)
}

#[derive(Debug, Clone, Copy)]
enum BridgeErrorKind {
    BackendCommand,
    OriginNotAllowed,
}

impl BridgeErrorKind {
    fn code(self) -> &'static str {
        match self {
            Self::BackendCommand => "backend.command",
            Self::OriginNotAllowed => "config.origin",
        }
    }
}

fn bridge_error_payload(status: u16, error: &str, kind: BridgeErrorKind) -> Value {
    json!({
        "code": kind.code(),
        "message": error,
        "source": "web_bridge",
        "retryable": false,
        "details": {
            "status": status,
        },
    })
}
#[cfg(test)]
pub(crate) use crate::desktop_commands::web_adapter::{optional_bool_arg, optional_string_arg};

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
        Self::typed_error(status, error, BridgeErrorKind::BackendCommand)
    }

    fn typed_error(status: u16, error: String, kind: BridgeErrorKind) -> Self {
        let codexhub_error = bridge_error_payload(status, &error, kind);
        Self::json(
            status,
            json!({
                "ok": false,
                "error": error,
                "codexhub_error": codexhub_error,
            }),
        )
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
    use super::{
        handle_request, optional_bool_arg, optional_string_arg, origin_allowed, BridgeRequest,
        BridgeResponse,
    };
    use serde_json::json;

    #[test]
    fn origin_policy_allows_only_dev_frontend() {
        assert!(origin_allowed(None));
        assert!(origin_allowed(Some("http://127.0.0.1:1420")));
        assert!(origin_allowed(Some("http://localhost:1420")));
        assert!(!origin_allowed(Some("http://example.com")));
    }

    #[test]
    fn official_collaboration_save_accepts_camel_case_and_legacy_model_id() {
        assert_eq!(
            optional_string_arg(
                &json!({"modelId": "gpt-5.6-luna"}),
                &["modelId", "model_id"]
            ),
            Some("gpt-5.6-luna".to_string())
        );
        assert_eq!(
            optional_string_arg(
                &json!({"model_id": "gpt-5.6-luna"}),
                &["modelId", "model_id"]
            ),
            Some("gpt-5.6-luna".to_string())
        );
    }

    #[test]
    fn restart_codex_argument_accepts_both_cases_and_defaults_to_none() {
        assert_eq!(
            optional_bool_arg(
                &json!({"restartCodex": true}),
                &["restartCodex", "restart_codex"]
            ),
            Some(true)
        );
        assert_eq!(
            optional_bool_arg(
                &json!({"restart_codex": true}),
                &["restartCodex", "restart_codex"]
            ),
            Some(true)
        );
        assert_eq!(
            optional_bool_arg(&json!({}), &["restartCodex", "restart_codex"]),
            None
        );
    }

    #[test]
    fn options_preflight_succeeds() {
        let response = handle_request(
            BridgeRequest {
                method: "OPTIONS".to_string(),
                path: "/api/invoke".to_string(),
                origin: Some("http://127.0.0.1:1420".to_string()),
                body: Vec::new(),
            },
            None,
        )
        .expect("preflight");

        assert_eq!(response.status, 204);
    }

    #[test]
    fn unknown_command_returns_error() {
        let response = handle_request(
            BridgeRequest {
                method: "POST".to_string(),
                path: "/api/invoke".to_string(),
                origin: Some("http://127.0.0.1:1420".to_string()),
                body: serde_json::to_vec(&json!({
                    "command": "missing_command",
                    "args": {}
                }))
                .unwrap(),
            },
            None,
        )
        .expect("invoke");

        assert_eq!(response.status, 500);
        let body: serde_json::Value = serde_json::from_slice(&response.body).expect("json body");
        assert_eq!(body["error"], "unknown CodexHub command: missing_command");
        assert_eq!(body["codexhub_error"]["code"], "backend.command");
        assert_eq!(
            body["codexhub_error"]["message"],
            "unknown CodexHub command: missing_command"
        );
        assert_eq!(body["codexhub_error"]["source"], "web_bridge");
        assert_eq!(body["codexhub_error"]["retryable"], false);
        assert_eq!(body["codexhub_error"]["details"]["status"], 500);
    }

    #[test]
    fn disallowed_origin_returns_config_origin_error() {
        let response = handle_request(
            BridgeRequest {
                method: "POST".to_string(),
                path: "/api/invoke".to_string(),
                origin: Some("http://example.com".to_string()),
                body: serde_json::to_vec(&json!({
                    "command": "get_app_flavor",
                    "args": {}
                }))
                .unwrap(),
            },
            None,
        )
        .expect("invoke");

        assert_eq!(response.status, 403);
        let body: serde_json::Value = serde_json::from_slice(&response.body).expect("json body");
        assert_eq!(body["codexhub_error"]["code"], "config.origin");
        assert_eq!(body["codexhub_error"]["source"], "web_bridge");
        assert_eq!(body["codexhub_error"]["retryable"], false);
    }

    #[test]
    fn backend_error_text_cannot_impersonate_origin_policy_error() {
        let response = BridgeResponse::error(403, "backend origin lookup failed".to_string());
        let body: serde_json::Value = serde_json::from_slice(&response.body).expect("json body");

        assert_eq!(body["codexhub_error"]["code"], "backend.command");
    }

    #[test]
    fn updater_commands_require_desktop_app_context() {
        let response = handle_request(
            BridgeRequest {
                method: "POST".to_string(),
                path: "/api/invoke".to_string(),
                origin: Some("http://127.0.0.1:1420".to_string()),
                body: serde_json::to_vec(&json!({
                    "command": "get_app_version",
                    "args": {}
                }))
                .unwrap(),
            },
            None,
        )
        .expect("invoke");

        assert_eq!(response.status, 500);
        assert!(String::from_utf8_lossy(&response.body).contains("desktop app context"));
    }

    #[test]
    fn get_app_flavor_returns_build_and_runtime_metadata() {
        let response = handle_request(
            BridgeRequest {
                method: "POST".to_string(),
                path: "/api/invoke".to_string(),
                origin: Some("http://127.0.0.1:1420".to_string()),
                body: serde_json::to_vec(&json!({
                    "command": "get_app_flavor",
                    "args": {}
                }))
                .unwrap(),
            },
            None,
        )
        .expect("invoke");

        assert_eq!(response.status, 200);
        let body: serde_json::Value = serde_json::from_slice(&response.body).expect("json body");
        let value = &body["value"];
        assert_eq!(value["product_name"], "CodexHub");
        assert_eq!(value["gateway_port"], 9099);
        let build = crate::build_info::current();
        assert_eq!(value["build"]["semantic_version"], build.semantic_version);
        assert_eq!(value["build"]["flavor"], build.flavor.as_str());
        assert_eq!(
            value["build"]["diagnostics_enabled"],
            build.diagnostics_enabled
        );
    }
}
