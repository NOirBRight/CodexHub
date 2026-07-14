//! Versioned, content-free controls for the debug Gateway diagnostic recorder.
//!
//! The Gateway child owns recorder state.  This module only exchanges bounded
//! control records under the existing runtime home; it never opens another
//! listener, reads artifact contents, or participates in request traffic.

use crate::{build_info, runtime_paths, safe_file};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::fs;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::thread;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

const CONTROL_SCHEMA_VERSION: u32 = 1;
const CONTROL_TIMEOUT: Duration = Duration::from_secs(2);
const CONTROL_POLL_INTERVAL: Duration = Duration::from_millis(40);
const CONTROL_EXPIRY_MILLIS: u64 = 5_000;
const DIAGNOSTICS_UNAVAILABLE: &str = "Debug diagnostics are available only in a debug build.";
const DIAGNOSTICS_NOT_READY: &str =
    "Debug diagnostics are not ready. Start the Gateway and try again.";

static REQUEST_SEQUENCE: AtomicU64 = AtomicU64::new(1);

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct DiagnosticsStatus {
    pub active: bool,
    pub paused: bool,
    pub flavor: String,
    pub rolling_bytes: u64,
    pub rolling_window_seconds: u64,
    pub incident_count: usize,
    pub incident_ids: Vec<String>,
    pub last_marker_category: Option<String>,
    pub last_marker_at_ms: Option<u64>,
    pub rolling_evicted_segments: u64,
    pub incident_evicted_count: u64,
    pub truncated: bool,
    pub schema_version: u32,
    pub writer_failure_count: u64,
    pub writer_queue_dropped_records: u64,
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub(crate) struct DiagnosticsActionResult {
    pub status: DiagnosticsStatus,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub accepted: Option<bool>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub incident_id: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub deleted: Option<bool>,
}

#[derive(Debug, Deserialize)]
#[serde(deny_unknown_fields)]
struct ControlResponse {
    schema_version: u32,
    request_id: String,
    ok: bool,
    status: DiagnosticsStatus,
    #[serde(default)]
    code: Option<String>,
    #[serde(default)]
    result: Option<Value>,
}

pub(crate) fn status() -> Result<DiagnosticsStatus, String> {
    let response = request("status", None)?;
    if response.code.is_some() || response.result.is_some() {
        return Err("Debug diagnostics returned an invalid status response.".to_string());
    }
    Ok(response.status)
}

pub(crate) fn manual_mark() -> Result<DiagnosticsActionResult, String> {
    let response = request("mark", None)?;
    if response.code.is_some() {
        return Err("Debug diagnostics returned an invalid mark response.".to_string());
    }
    let result = response
        .result
        .as_ref()
        .and_then(Value::as_object)
        .ok_or_else(|| "Debug diagnostics returned an invalid mark response.".to_string())?;
    if result.len() != 2 || !result.contains_key("accepted") || !result.contains_key("incident_id") {
        return Err("Debug diagnostics returned an invalid mark response.".to_string());
    }
    let accepted = result
        .get("accepted")
        .and_then(Value::as_bool)
        .ok_or_else(|| "Debug diagnostics returned an invalid mark response.".to_string())?;
    let incident_id = result
        .get("incident_id")
        .and_then(Value::as_str)
        .map(str::to_owned);
    if incident_id.as_deref().is_some_and(|value| !is_safe_incident_id(value))
        || (accepted && incident_id.is_none())
    {
        return Err("Debug diagnostics returned an invalid mark response.".to_string());
    }
    Ok(DiagnosticsActionResult {
        status: response.status,
        accepted: Some(accepted),
        incident_id,
        deleted: None,
    })
}

pub(crate) fn pause() -> Result<DiagnosticsActionResult, String> {
    action_without_result(request("pause", None)?)
}

pub(crate) fn resume() -> Result<DiagnosticsActionResult, String> {
    action_without_result(request("resume", None)?)
}

pub(crate) fn delete_incident(incident_id: String) -> Result<DiagnosticsActionResult, String> {
    if !is_safe_incident_id(&incident_id) {
        return Err("Invalid diagnostic incident identifier.".to_string());
    }
    let response = request("delete", Some(&incident_id))?;
    if response.code.is_some() {
        return Err("Debug diagnostics returned an invalid delete response.".to_string());
    }
    let deleted = response
        .result
        .as_ref()
        .and_then(Value::as_object)
        .filter(|result| result.len() == 1 && result.contains_key("deleted"))
        .and_then(|result| result.get("deleted"))
        .and_then(Value::as_bool)
        .ok_or_else(|| "Debug diagnostics returned an invalid delete response.".to_string())?;
    Ok(DiagnosticsActionResult {
        status: response.status,
        accepted: None,
        incident_id: Some(incident_id),
        deleted: Some(deleted),
    })
}

fn action_without_result(response: ControlResponse) -> Result<DiagnosticsActionResult, String> {
    if response.code.is_some() || response.result.is_some() {
        return Err("Debug diagnostics returned an invalid control response.".to_string());
    }
    Ok(DiagnosticsActionResult {
        status: response.status,
        accepted: None,
        incident_id: None,
        deleted: None,
    })
}

fn request(operation: &str, incident_id: Option<&str>) -> Result<ControlResponse, String> {
    if !build_info::current().diagnostics_enabled {
        return Err(DIAGNOSTICS_UNAVAILABLE.to_string());
    }
    let control_dir = runtime_paths::runtime_home_dir()?.join("diagnostics").join("control");
    request_with_control_dir(&control_dir, operation, incident_id, CONTROL_TIMEOUT)
}

fn request_with_control_dir(
    control_dir: &Path,
    operation: &str,
    incident_id: Option<&str>,
    timeout: Duration,
) -> Result<ControlResponse, String> {
    let request_id = next_request_id();
    let requests = control_dir.join("requests");
    let responses = control_dir.join("responses");
    let request_path = requests.join(format!("{request_id}.json"));
    let response_path = responses.join(format!("{request_id}.json"));
    fs::create_dir_all(&responses).map_err(|error| {
        format!(
            "failed to prepare diagnostic control response directory {}: {error}",
            responses.display()
        )
    })?;
    let _ = fs::remove_file(&response_path);

    let now_ms = unix_millis();
    let mut value = json!({
        "schema_version": CONTROL_SCHEMA_VERSION,
        "request_id": request_id,
        "operation": operation,
        "expires_at_ms": now_ms.saturating_add(CONTROL_EXPIRY_MILLIS),
    });
    if let Some(incident_id) = incident_id {
        value["incident_id"] = Value::String(incident_id.to_string());
    }
    let serialized = serde_json::to_string(&value)
        .map_err(|error| format!("failed to encode diagnostic control request: {error}"))?;
    safe_file::write_text_atomic(&request_path, &serialized)?;

    let started = Instant::now();
    loop {
        match fs::read_to_string(&response_path) {
            Ok(text) => {
                let _ = fs::remove_file(&response_path);
                let response = parse_response(&text, &request_id)?;
                if response.ok {
                    return Ok(response);
                }
                return Err(match response.code.as_deref() {
                    Some("invalid_request") => "Debug diagnostics rejected the control request.".to_string(),
                    _ => DIAGNOSTICS_NOT_READY.to_string(),
                });
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => {
                let _ = fs::remove_file(&request_path);
                return Err(format!("failed to read diagnostic control response: {error}"));
            }
        }
        if started.elapsed() >= timeout {
            let _ = fs::remove_file(&request_path);
            return Err(DIAGNOSTICS_NOT_READY.to_string());
        }
        thread::sleep(CONTROL_POLL_INTERVAL);
    }
}

fn parse_response(text: &str, expected_request_id: &str) -> Result<ControlResponse, String> {
    let response = serde_json::from_str::<ControlResponse>(text)
        .map_err(|_| "Debug diagnostics returned an invalid control response.".to_string())?;
    if response.schema_version != CONTROL_SCHEMA_VERSION
        || response.request_id != expected_request_id
        || !is_safe_request_id(&response.request_id)
    {
        return Err("Debug diagnostics returned an invalid control response.".to_string());
    }
    validate_status(&response.status)?;
    Ok(response)
}

fn validate_status(status: &DiagnosticsStatus) -> Result<(), String> {
    if status.schema_version != CONTROL_SCHEMA_VERSION
        || status.flavor != "debug"
        || status.incident_count != status.incident_ids.len()
        || status
            .incident_ids
            .iter()
            .any(|incident_id| !is_safe_incident_id(incident_id))
    {
        return Err("Debug diagnostics returned an invalid status response.".to_string());
    }
    Ok(())
}

fn next_request_id() -> String {
    let sequence = REQUEST_SEQUENCE.fetch_add(1, Ordering::Relaxed);
    format!("c{:016x}{:016x}", unix_millis(), sequence)
}

fn unix_millis() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|duration| duration.as_millis().min(u128::from(u64::MAX)) as u64)
        .unwrap_or_default()
}

fn is_safe_request_id(value: &str) -> bool {
    value.len() >= 17
        && value.len() <= 65
        && value.starts_with('c')
        && value[1..].bytes().all(|byte| byte.is_ascii_hexdigit())
}

fn is_safe_incident_id(value: &str) -> bool {
    let Some(digits) = value.strip_prefix('i') else {
        return false;
    };
    digits.len() >= 6 && digits.bytes().all(|byte| byte.is_ascii_digit())
}

#[cfg(test)]
mod tests {
    use super::{
        is_safe_incident_id, is_safe_request_id, next_request_id, parse_response,
        request_with_control_dir, CONTROL_SCHEMA_VERSION,
    };
    use serde_json::json;
    use std::fs;
    use std::path::PathBuf;
    use std::thread;
    use std::time::{Duration, SystemTime, UNIX_EPOCH};

    #[test]
    fn control_identifiers_are_opaque_and_bounded() {
        assert!(is_safe_request_id(&next_request_id()));
        assert!(is_safe_incident_id("i000001"));
        assert!(!is_safe_incident_id("i00001"));
        assert!(!is_safe_incident_id("i000001/escape"));
    }

    #[cfg(not(feature = "debug-diagnostics"))]
    #[test]
    fn normal_build_rejects_every_diagnostics_control_before_touching_runtime_state() {
        assert_eq!(
            super::status().expect_err("normal flavor should not expose diagnostics"),
            super::DIAGNOSTICS_UNAVAILABLE
        );
    }

    #[test]
    fn control_response_fails_closed_for_unknown_status_fields() {
        let request_id = "c0000000000000000000000000000001";
        let response = json!({
            "schema_version": CONTROL_SCHEMA_VERSION,
            "request_id": request_id,
            "ok": true,
            "status": {
                "active": true,
                "paused": false,
                "flavor": "debug",
                "rolling_bytes": 0,
                "rolling_window_seconds": 7200,
                "incident_count": 0,
                "incident_ids": [],
                "last_marker_category": null,
                "last_marker_at_ms": null,
                "rolling_evicted_segments": 0,
                "incident_evicted_count": 0,
                "truncated": false,
                "schema_version": CONTROL_SCHEMA_VERSION,
                "writer_failure_count": 0,
                "writer_queue_dropped_records": 0,
                "unexpected": "blocked"
            }
        });

        assert!(parse_response(&response.to_string(), request_id).is_err());
    }

    #[test]
    fn control_round_trip_uses_only_versioned_content_free_fields() {
        let root = temp_root("control-round-trip");
        let responder_root = root.clone();
        let responder = thread::spawn(move || {
            let request_dir = responder_root.join("requests");
            let request_path = wait_for_request(&request_dir);
            let request: serde_json::Value =
                serde_json::from_str(&fs::read_to_string(&request_path).unwrap()).unwrap();
            let object = request.as_object().unwrap();
            assert_eq!(object.len(), 4);
            assert!(object.contains_key("schema_version"));
            assert!(object.contains_key("request_id"));
            assert!(object.contains_key("operation"));
            assert!(object.contains_key("expires_at_ms"));
            assert_eq!(object.get("operation").and_then(|value| value.as_str()), Some("status"));
            let request_id = object
                .get("request_id")
                .and_then(|value| value.as_str())
                .unwrap();
            let response = json!({
                "schema_version": CONTROL_SCHEMA_VERSION,
                "request_id": request_id,
                "ok": true,
                "status": valid_status(),
            });
            let response_dir = responder_root.join("responses");
            fs::create_dir_all(&response_dir).unwrap();
            fs::write(
                response_dir.join(format!("{request_id}.json")),
                response.to_string(),
            )
            .unwrap();
        });

        let response = request_with_control_dir(&root, "status", None, Duration::from_secs(2))
            .expect("control response");
        assert!(response.ok);
        assert!(response.status.active);
        responder.join().unwrap();
        let _ = fs::remove_dir_all(root);
    }

    fn valid_status() -> serde_json::Value {
        json!({
            "active": true,
            "paused": false,
            "flavor": "debug",
            "rolling_bytes": 0,
            "rolling_window_seconds": 7200,
            "incident_count": 0,
            "incident_ids": [],
            "last_marker_category": null,
            "last_marker_at_ms": null,
            "rolling_evicted_segments": 0,
            "incident_evicted_count": 0,
            "truncated": false,
            "schema_version": CONTROL_SCHEMA_VERSION,
            "writer_failure_count": 0,
            "writer_queue_dropped_records": 0
        })
    }

    fn wait_for_request(request_dir: &std::path::Path) -> PathBuf {
        let deadline = std::time::Instant::now() + Duration::from_secs(2);
        loop {
            if let Ok(mut paths) = fs::read_dir(request_dir) {
                if let Some(path) = paths
                    .find_map(Result::ok)
                    .map(|entry| entry.path())
                    .filter(|path| path.extension().and_then(|value| value.to_str()) == Some("json"))
                {
                    return path;
                }
            }
            assert!(std::time::Instant::now() < deadline, "control request should arrive");
            thread::sleep(Duration::from_millis(10));
        }
    }

    fn temp_root(name: &str) -> PathBuf {
        let nonce = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        std::env::temp_dir().join(format!("codexhub-diagnostics-{name}-{nonce}"))
    }
}
