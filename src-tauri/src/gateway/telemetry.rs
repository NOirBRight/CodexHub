use super::{
    non_empty_str, sanitize_text, GatewayEvent, GatewayUsageEvent, GatewayUsageSnapshot,
    GatewayUsageSummary, TelemetryStatus,
};
use crate::{config, models};
use rusqlite::{params, Connection, OptionalExtension};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::collections::{HashMap, HashSet};
use std::fs;
use std::io::{BufRead, Read, Seek};
use std::path::{Path, PathBuf};
use std::sync::{Mutex, OnceLock};
use std::thread;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

const EVENT_READ_LIMIT_BYTES: u64 = 4 * 1024 * 1024;
const TELEMETRY_INGEST_BATCH_LINES: usize = 1000;
const TELEMETRY_INGEST_BATCH_BYTES: u64 = 1024 * 1024;
const TELEMETRY_INGEST_INTERVAL: Duration = Duration::from_secs(2);

const OFFICIAL_FAST_PRICING: &[(&str, f64, f64, f64)] = &[
    ("gpt-5.5-fast", 12.50, 1.25, 75.00),
    ("gpt-5.4-fast", 5.00, 0.50, 30.00),
];

static TELEMETRY_INGEST_LOCK: OnceLock<Mutex<()>> = OnceLock::new();
static TELEMETRY_INGESTER_STARTED: OnceLock<()> = OnceLock::new();

#[derive(Debug, Clone, Default)]
pub(crate) struct UsageTimeWindow {
    start_ts: Option<String>,
    end_ts: Option<String>,
}

impl UsageTimeWindow {
    pub(crate) fn new(start_ts: Option<String>, end_ts: Option<String>) -> Self {
        Self {
            start_ts: non_empty_owned(start_ts),
            end_ts: non_empty_owned(end_ts),
        }
    }

    fn is_bounded(&self) -> bool {
        self.start_ts.is_some() || self.end_ts.is_some()
    }
}

#[derive(Debug, Clone, Copy)]
pub(crate) struct UsagePricing {
    pub(crate) input_per_million: f64,
    pub(crate) cached_input_per_million: Option<f64>,
    pub(crate) output_per_million: f64,
}

pub(super) fn gateway_recent_events(
    codex_home: PathBuf,
    limit: Option<usize>,
    since_ts: Option<String>,
) -> Result<Vec<GatewayEvent>, String> {
    let limit = limit.unwrap_or(20).clamp(1, 5_000);
    Ok(read_recent_events(
        codex_home,
        limit,
        None,
        since_ts.as_deref(),
    ))
}

pub(super) fn gateway_usage_summary(
    codex_home: PathBuf,
    start_ts: Option<String>,
    end_ts: Option<String>,
) -> Result<GatewayUsageSummary, String> {
    let paths = GatewayTelemetryPaths::runtime(codex_home);
    ensure_telemetry_sqlite_ready(&paths.sqlite_db)?;
    let pricing = usage_pricing_by_model();
    let window = UsageTimeWindow::new(start_ts, end_ts);
    read_usage_summary_from_sqlite_path_with_pricing_and_window(&paths.sqlite_db, &pricing, &window)
}

pub(super) fn gateway_usage_snapshot(
    codex_home: PathBuf,
    limit: Option<usize>,
    start_ts: Option<String>,
    end_ts: Option<String>,
) -> Result<GatewayUsageSnapshot, String> {
    let paths = GatewayTelemetryPaths::runtime(codex_home);
    gateway_usage_snapshot_for_paths(&paths.event_log, &paths.sqlite_db, limit, start_ts, end_ts)
}

pub(super) fn gateway_usage_events(
    codex_home: PathBuf,
    limit: Option<usize>,
    start_ts: Option<String>,
    end_ts: Option<String>,
) -> Result<Vec<GatewayUsageEvent>, String> {
    let window = UsageTimeWindow::new(start_ts, end_ts);
    let limit = match limit {
        Some(value) => value.clamp(1, 500),
        None if window.is_bounded() => usize::MAX,
        None => 100,
    };
    let paths = GatewayTelemetryPaths::runtime(codex_home);
    ensure_telemetry_sqlite_ready(&paths.sqlite_db)?;
    read_usage_events_from_sqlite_path_with_window(&paths.sqlite_db, limit, &window)
}

pub(super) fn read_recent_events(
    codex_home: PathBuf,
    limit: usize,
    filter: Option<fn(&GatewayEvent) -> bool>,
    since_ts: Option<&str>,
) -> Vec<GatewayEvent> {
    let paths = GatewayTelemetryPaths::runtime(codex_home);
    let text = match read_event_log_text(&paths.event_log) {
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
        if let Some(since_ts) = since_ts {
            let Some(event_ts) = event.ts.as_deref() else {
                continue;
            };
            if event_ts < since_ts {
                break;
            }
        }
        if filter.map(|predicate| predicate(&event)).unwrap_or(true) {
            events.push(event);
        }
    }
    events.reverse();
    events
}

fn event_log_path(codex_home: &Path) -> PathBuf {
    codex_home.join("proxy").join("codex-proxy-events.jsonl")
}

fn telemetry_db_path(codex_home: &Path) -> PathBuf {
    codex_home
        .join("proxy")
        .join("codex-proxy-telemetry.sqlite")
}

#[derive(Debug, Clone)]
struct GatewayTelemetryPaths {
    event_log: PathBuf,
    sqlite_db: PathBuf,
}

impl GatewayTelemetryPaths {
    fn runtime(codex_home: PathBuf) -> Self {
        Self {
            event_log: event_log_path(&codex_home),
            sqlite_db: telemetry_db_path(&codex_home),
        }
    }
}

pub(super) fn start_telemetry_ingester(codex_home: fn() -> PathBuf) {
    TELEMETRY_INGESTER_STARTED.get_or_init(|| {
        let _ = thread::Builder::new()
            .name("codexhub-telemetry-ingester".to_string())
            .spawn(move || loop {
                if let Err(error) = ingest_telemetry_once(codex_home()) {
                    let paths = GatewayTelemetryPaths::runtime(codex_home());
                    let _ = record_telemetry_ingest_error(&paths.sqlite_db, &error);
                }
                thread::sleep(TELEMETRY_INGEST_INTERVAL);
            });
    });
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
    Ok(())
}

fn telemetry_ingest_lock() -> &'static Mutex<()> {
    TELEMETRY_INGEST_LOCK.get_or_init(|| Mutex::new(()))
}

fn ingest_telemetry_once(codex_home: PathBuf) -> Result<TelemetryStatus, String> {
    let paths = GatewayTelemetryPaths::runtime(codex_home);
    ingest_telemetry_once_for_paths(&paths.event_log, &paths.sqlite_db)
}

pub(crate) fn ingest_telemetry_once_for_paths(
    event_path: &Path,
    db_path: &Path,
) -> Result<TelemetryStatus, String> {
    let _guard = telemetry_ingest_lock()
        .lock()
        .map_err(|_| "telemetry ingest lock is poisoned".to_string())?;
    ensure_telemetry_sqlite_ready(db_path)?;
    let mut connection = open_telemetry_connection(db_path)?;
    initialize_telemetry_db(&connection)?;
    let event_log_size = event_log_size(event_path);
    let meta_offset = telemetry_meta_u64(&connection, "last_ingested_offset")?;
    let mut indexed_offset = meta_offset
        .or_else(|| {
            telemetry_meta_u64(&connection, "last_backfill_size")
                .ok()
                .flatten()
        })
        .unwrap_or(0);
    if indexed_offset > event_log_size {
        indexed_offset = 0;
    }
    let (events, next_offset) = read_telemetry_ingest_batch(
        event_path,
        indexed_offset,
        TELEMETRY_INGEST_BATCH_LINES,
        TELEMETRY_INGEST_BATCH_BYTES,
    )?;
    if next_offset != indexed_offset || meta_offset != Some(indexed_offset) {
        write_telemetry_ingest_batch(&mut connection, &events, next_offset)?;
    }
    telemetry_status_for_paths(event_path, db_path)
}

fn read_telemetry_ingest_batch(
    event_path: &Path,
    start_offset: u64,
    max_lines: usize,
    max_bytes: u64,
) -> Result<(Vec<Value>, u64), String> {
    let file = match fs::File::open(event_path) {
        Ok(file) => file,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return Ok((Vec::new(), 0));
        }
        Err(error) => {
            return Err(format!(
                "failed to open telemetry event log {}: {error}",
                event_path.display()
            ));
        }
    };
    let mut reader = std::io::BufReader::new(file);
    reader
        .seek(std::io::SeekFrom::Start(start_offset))
        .map_err(|error| {
            format!(
                "failed to seek telemetry event log {}: {error}",
                event_path.display()
            )
        })?;

    let mut events = Vec::new();
    let mut next_offset = start_offset;
    let mut read_bytes = 0_u64;
    let mut read_lines = 0_usize;
    loop {
        if read_lines >= max_lines || read_bytes >= max_bytes {
            break;
        }
        let mut line = Vec::new();
        let count = reader.read_until(b'\n', &mut line).map_err(|error| {
            format!(
                "failed to read telemetry event log {}: {error}",
                event_path.display()
            )
        })?;
        if count == 0 {
            break;
        }
        if !line.ends_with(b"\n") {
            break;
        }
        next_offset = next_offset.saturating_add(count as u64);
        read_bytes = read_bytes.saturating_add(count as u64);
        read_lines += 1;
        let text = String::from_utf8_lossy(&line);
        let trimmed = text.trim();
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
        events.push(value);
    }
    Ok((events, next_offset))
}

fn write_telemetry_ingest_batch(
    connection: &mut Connection,
    events: &[Value],
    next_offset: u64,
) -> Result<(), String> {
    let now = telemetry_now_marker();
    connection
        .execute_batch("BEGIN IMMEDIATE TRANSACTION")
        .map_err(|error| format!("failed to begin telemetry ingest transaction: {error}"))?;
    let result = (|| {
        for event in events {
            write_json_event_to_sqlite(connection, event)?;
        }
        connection
            .execute(
                "INSERT INTO telemetry_meta (key, value) VALUES ('last_ingested_offset', ?) \
                 ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                params![next_offset.to_string()],
            )
            .map_err(|error| format!("failed to update telemetry ingest offset: {error}"))?;
        connection
            .execute(
                "INSERT INTO telemetry_meta (key, value) VALUES ('last_indexed_at', ?) \
                 ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                params![now],
            )
            .map_err(|error| format!("failed to update telemetry indexed marker: {error}"))?;
        connection
            .execute(
                "DELETE FROM telemetry_meta WHERE key = 'last_ingest_error'",
                [],
            )
            .map_err(|error| format!("failed to clear telemetry ingest error: {error}"))?;
        Ok::<(), String>(())
    })();
    match result {
        Ok(()) => connection
            .execute_batch("COMMIT")
            .map_err(|error| format!("failed to commit telemetry ingest transaction: {error}")),
        Err(error) => {
            let _ = connection.execute_batch("ROLLBACK");
            Err(error)
        }
    }
}

fn telemetry_status_for_paths(
    event_path: &Path,
    db_path: &Path,
) -> Result<TelemetryStatus, String> {
    ensure_telemetry_sqlite_ready(db_path)?;
    let connection = open_telemetry_connection(db_path)?;
    initialize_telemetry_db(&connection)?;
    let event_log_size = event_log_size(event_path);
    let mut indexed_offset = telemetry_meta_u64(&connection, "last_ingested_offset")?
        .or_else(|| {
            telemetry_meta_u64(&connection, "last_backfill_size")
                .ok()
                .flatten()
        })
        .unwrap_or(0);
    if indexed_offset > event_log_size {
        indexed_offset = 0;
    }
    let lag_bytes = event_log_size.saturating_sub(indexed_offset);
    Ok(TelemetryStatus {
        event_log_size,
        indexed_offset,
        lag_bytes,
        backfill_pending: lag_bytes > 0,
        last_indexed_at: telemetry_meta_value(&connection, "last_indexed_at")?.or_else(|| {
            telemetry_meta_value(&connection, "last_backfill_at")
                .ok()
                .flatten()
        }),
        last_error: telemetry_meta_value(&connection, "last_ingest_error")?,
    })
}

fn telemetry_meta_u64(connection: &Connection, key: &str) -> Result<Option<u64>, String> {
    Ok(telemetry_meta_value(connection, key)?.and_then(|value| value.parse::<u64>().ok()))
}

fn event_log_size(event_path: &Path) -> u64 {
    fs::metadata(event_path)
        .map(|metadata| metadata.len())
        .unwrap_or(0)
}

fn record_telemetry_ingest_error(db_path: &Path, error: &str) -> Result<(), String> {
    ensure_telemetry_sqlite_ready(db_path)?;
    let connection = open_telemetry_connection(db_path)?;
    initialize_telemetry_db(&connection)?;
    connection
        .execute(
            "INSERT INTO telemetry_meta (key, value) VALUES ('last_ingest_error', ?) \
             ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            params![error.chars().take(500).collect::<String>()],
        )
        .map_err(|error| format!("failed to record telemetry ingest error: {error}"))?;
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

pub(crate) fn initialize_telemetry_db(connection: &Connection) -> Result<(), String> {
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
                reports_cached_input_tokens INTEGER,
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
        ("reports_cached_input_tokens", "INTEGER"),
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

#[cfg(test)]
pub(crate) fn backfill_event_log_to_sqlite_path(
    event_path: &Path,
    db_path: &Path,
) -> Result<(), String> {
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
    if event != "request_start"
        && event != "request_complete"
        && event != "request_error"
        && event != "usage_observed"
    {
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
    let mut usage_source = string_field(value, "usage_source");
    let mut usage_missing_reason = string_field(value, "usage_missing_reason");
    if usage_source.as_deref() == Some("missing") {
        let existing_usage_source: Option<String> = connection
            .query_row(
                "SELECT usage_source FROM gateway_requests WHERE request_id = ?",
                params![request_id],
                |row| row.get(0),
            )
            .optional()
            .map_err(|error| format!("failed to read existing usage source: {error}"))?
            .flatten();
        if existing_usage_source
            .as_deref()
            .is_some_and(|source| source != "missing")
        {
            usage_source = None;
            usage_missing_reason = None;
        }
    }
    let clear_usage_missing_reason: i64 = if usage_source
        .as_deref()
        .is_some_and(|source| source != "missing")
    {
        1
    } else {
        0
    };

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
                reports_cached_input_tokens = COALESCE(?, reports_cached_input_tokens),
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
                usage_missing_reason = CASE WHEN ? THEN NULL ELSE COALESCE(?, usage_missing_reason) END,
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
                bool_or_i64_field(value, "reports_cached_input_tokens"),
                string_field(value, "inbound_format"),
                model,
                model_requested,
                model_canonical,
                string_field(value, "provider_config_hash"),
                string_field(value, "request_body_hmac"),
                string_field(value, "request_prefix_hmac"),
                value.get("prefix_bytes").and_then(Value::as_i64),
                string_field(value, "prompt_cache_key_hash"),
                usage_source,
                clear_usage_missing_reason,
                usage_missing_reason,
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

#[cfg(test)]
pub(crate) fn read_usage_summary_from_sqlite_path_with_pricing(
    path: &Path,
    pricing: &HashMap<String, UsagePricing>,
) -> Result<GatewayUsageSummary, String> {
    read_usage_summary_from_sqlite_path_with_pricing_and_window(
        path,
        pricing,
        &UsageTimeWindow::default(),
    )
}

pub(crate) fn read_usage_summary_from_sqlite_path_with_pricing_and_window(
    path: &Path,
    pricing: &HashMap<String, UsagePricing>,
    window: &UsageTimeWindow,
) -> Result<GatewayUsageSummary, String> {
    let events = read_usage_events_from_sqlite_path_with_window(path, usize::MAX, window)?;
    Ok(read_usage_summary_from_events_with_pricing(
        &events, pricing,
    ))
}

pub(crate) fn gateway_usage_snapshot_for_paths(
    event_path: &Path,
    db_path: &Path,
    limit: Option<usize>,
    start_ts: Option<String>,
    end_ts: Option<String>,
) -> Result<GatewayUsageSnapshot, String> {
    ensure_telemetry_sqlite_ready(db_path)?;
    let window = UsageTimeWindow::new(start_ts, end_ts);
    let pricing = usage_pricing_by_model();
    let event_limit = match limit {
        Some(value) => value.clamp(1, 500),
        None if window.is_bounded() => usize::MAX,
        None => 100,
    };
    let summary =
        read_usage_summary_from_sqlite_path_with_pricing_and_window(db_path, &pricing, &window)?;
    let events = read_usage_events_from_sqlite_path_with_window(db_path, event_limit, &window)?;
    let telemetry_status = telemetry_status_for_paths(event_path, db_path)?;
    Ok(GatewayUsageSnapshot {
        summary,
        events,
        telemetry_status,
    })
}

#[cfg(test)]
pub(crate) fn read_usage_events_from_sqlite_path(
    path: &Path,
    limit: usize,
) -> Result<Vec<GatewayUsageEvent>, String> {
    read_usage_events_from_sqlite_path_with_window(path, limit, &UsageTimeWindow::default())
}

pub(crate) fn read_usage_events_from_sqlite_path_with_window(
    path: &Path,
    limit: usize,
    window: &UsageTimeWindow,
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
                COALESCE(client_id, 'unknown') AS client_id,
                COALESCE(client_inference_source, 'unknown') AS client_inference_source,
                reports_cached_input_tokens,
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
              AND (?1 IS NULL OR completed_ts >= ?1)
              AND (?2 IS NULL OR completed_ts <= ?2)
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
            LIMIT ?3
            "#,
        )
        .map_err(|error| format!("failed to prepare telemetry usage query: {error}"))?;
    let rows = statement
        .query_map(
            params![window.start_ts.as_deref(), window.end_ts.as_deref(), limit],
            |row| {
                Ok(GatewayUsageEvent {
                    ts: row.get(0)?,
                    request_id: row.get(1)?,
                    model: normalize_usage_model(row.get(3)?, row.get(2)?),
                    upstream: row.get(3)?,
                    client_id: row.get(4)?,
                    client_inference_source: row.get(5)?,
                    reports_cached_input_tokens: optional_i64_to_bool(
                        row.get::<_, Option<i64>>(6)?,
                    ),
                    status: row.get(7)?,
                    duration_ms: row.get(8)?,
                    usage_source: row
                        .get::<_, Option<String>>(9)?
                        .unwrap_or_else(|| "missing".to_string()),
                    usage_missing_reason: row.get(10)?,
                    input_tokens: optional_i64_to_u64(row.get::<_, Option<i64>>(11)?),
                    output_tokens: optional_i64_to_u64(row.get::<_, Option<i64>>(12)?),
                    total_tokens: optional_i64_to_u64(row.get::<_, Option<i64>>(13)?),
                    cached_input_tokens: optional_i64_to_u64(row.get::<_, Option<i64>>(14)?),
                    reasoning_tokens: optional_i64_to_u64(row.get::<_, Option<i64>>(15)?),
                })
            },
        )
        .map_err(|error| format!("failed to read telemetry usage rows: {error}"))?;
    let mut events = Vec::new();
    for row in rows {
        events.push(row.map_err(|error| format!("failed to decode telemetry usage row: {error}"))?);
    }
    events.reverse();
    Ok(events)
}

fn read_event_log_text(path: &Path) -> Result<String, String> {
    match fs::metadata(path).and_then(|metadata| {
        if metadata.len() > EVENT_READ_LIMIT_BYTES {
            let file = fs::File::open(path)?;
            let start = metadata.len().saturating_sub(EVENT_READ_LIMIT_BYTES);
            let mut reader = std::io::BufReader::new(file);
            use std::io::Seek;
            reader.seek(std::io::SeekFrom::Start(start))?;
            let mut text = String::new();
            reader.read_to_string(&mut text)?;
            Ok(text)
        } else {
            fs::read_to_string(path)
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
pub(crate) fn read_usage_summary_from_text(text: &str) -> GatewayUsageSummary {
    let pricing = usage_pricing_by_model();
    read_usage_summary_from_text_with_pricing(text, &pricing)
}

#[cfg(test)]
pub(crate) fn read_usage_summary_from_text_with_pricing(
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
    let cache_capable_providers = cache_usage_capable_provider_aliases();
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
    let cached_input_tokens = sum_optional(
        events
            .iter()
            .filter(|event| event_reports_cache_usage(event, &cache_capable_providers))
            .map(|event| event.cached_input_tokens),
    );
    let mut cache_known_input_tokens = 0_u64;
    let mut cache_known_cached_tokens = 0_u64;
    for event in events {
        if !event_reports_cache_usage(event, &cache_capable_providers) {
            continue;
        }
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
    let cost = estimate_usage_cost(events, pricing, &cache_capable_providers);

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

fn event_reports_cache_usage(
    event: &GatewayUsageEvent,
    cache_capable_providers: &HashSet<String>,
) -> bool {
    if let Some(reports) = event.reports_cached_input_tokens {
        return reports;
    }
    if event
        .upstream
        .as_deref()
        .is_some_and(|upstream| cache_capable_providers.contains(&cache_provider_key(upstream)))
    {
        return true;
    }
    event
        .model
        .as_deref()
        .is_some_and(cache_usage_capable_model)
}

fn cache_usage_capable_provider_aliases() -> HashSet<String> {
    let mut aliases = HashSet::from([
        cache_provider_key("official"),
        cache_provider_key("openai"),
        cache_provider_key("official_openai"),
    ]);
    if let Ok(providers) = config::get_providers() {
        for provider in providers {
            if provider.reports_cached_input_tokens != Some(true) {
                continue;
            }
            insert_provider_aliases(&mut aliases, &provider.id);
        }
    }
    aliases
}

fn insert_provider_aliases(aliases: &mut HashSet<String>, provider_id: &str) {
    aliases.insert(cache_provider_key(provider_id));
    match provider_id {
        "volc" => {
            aliases.insert(cache_provider_key("volcengine"));
        }
        "minimax-cn" => {
            aliases.insert(cache_provider_key("minimax_cn"));
        }
        _ => {}
    }
}

fn cache_provider_key(value: &str) -> String {
    value.trim().to_ascii_lowercase().replace(['-', ' '], "_")
}

fn cache_usage_capable_model(value: &str) -> bool {
    let normalized = value.trim().to_ascii_lowercase();
    normalized.starts_with("openai/")
}

fn optional_i64_to_u64(value: Option<i64>) -> Option<u64> {
    value.and_then(|item| u64::try_from(item).ok())
}

fn optional_i64_to_bool(value: Option<i64>) -> Option<bool> {
    value.map(|item| item != 0)
}

fn normalize_usage_model(upstream: Option<String>, model: Option<String>) -> Option<String> {
    let model = model?;
    let trimmed = model.trim();
    if trimmed.is_empty() {
        return None;
    }
    let upstream = upstream.as_deref().unwrap_or_default();
    if upstream == "official" && !trimmed.contains('/') && is_official_usage_model(trimmed) {
        return Some(format!("openai/{trimmed}"));
    }
    Some(trimmed.to_string())
}

fn is_official_usage_model(model: &str) -> bool {
    model.starts_with("gpt-")
        || model.starts_with("codex-")
        || model.starts_with("chatgpt-")
        || model
            .strip_prefix('o')
            .and_then(|rest| rest.chars().next())
            .is_some_and(|char| char.is_ascii_digit())
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
    cache_capable_providers: &HashSet<String>,
) -> UsageCostEstimate {
    let mut estimated_cost_usd = 0.0_f64;
    let mut priced_requests = 0_u64;
    let mut missing_usage_requests = 0_u64;
    let mut missing_pricing_requests = 0_u64;
    let mut cached_priced_as_input_requests = 0_u64;
    let mut estimated_cached_input_requests = 0_u64;
    let average_cache_hit_ratio = average_cache_hit_ratio(events, cache_capable_providers);

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

        let cached_tokens = match (
            event_reports_cache_usage(event, cache_capable_providers),
            event.cached_input_tokens,
            model_pricing.cached_input_per_million,
            average_cache_hit_ratio,
        ) {
            (true, Some(tokens), _, _) => tokens.min(input_tokens),
            (_, None, Some(_), Some(ratio)) | (false, Some(_), Some(_), Some(ratio))
                if input_tokens > 0 =>
            {
                estimated_cached_input_requests = estimated_cached_input_requests.saturating_add(1);
                ((input_tokens as f64 * ratio).round() as u64).min(input_tokens)
            }
            _ => 0,
        };
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
    if estimated_cached_input_requests > 0 {
        let rate = average_cache_hit_ratio.unwrap_or_default() * 100.0;
        label_parts.push(format!(
            "{estimated_cached_input_requests} requests estimated cached input at {rate:.1}% average hit rate"
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

fn average_cache_hit_ratio(
    events: &[GatewayUsageEvent],
    cache_capable_providers: &HashSet<String>,
) -> Option<f64> {
    let mut input_tokens = 0_u64;
    let mut cached_tokens = 0_u64;
    for event in events {
        if !event_reports_cache_usage(event, cache_capable_providers) {
            continue;
        }
        if let (Some(input), Some(cached)) = (event.input_tokens, event.cached_input_tokens) {
            if input > 0 {
                input_tokens = input_tokens.saturating_add(input);
                cached_tokens = cached_tokens.saturating_add(cached.min(input));
            }
        }
    }
    (input_tokens > 0).then_some(cached_tokens as f64 / input_tokens as f64)
}

pub(crate) fn usage_pricing_by_model() -> HashMap<String, UsagePricing> {
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

pub(crate) fn lookup_usage_pricing(
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

fn non_empty_owned(value: Option<String>) -> Option<String> {
    value.and_then(|item| non_empty_str(&item).map(ToOwned::to_owned))
}

#[cfg(test)]
pub(crate) fn read_usage_events_from_text(text: &str, limit: usize) -> Vec<GatewayUsageEvent> {
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
            client_id: string_field(&value, "client_id"),
            client_inference_source: string_field(&value, "client_inference_source"),
            reports_cached_input_tokens: value
                .get("reports_cached_input_tokens")
                .and_then(Value::as_bool),
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

pub(crate) fn sanitize_event(value: &Value) -> GatewayEvent {
    GatewayEvent {
        ts: string_field(value, "ts"),
        event: string_field(value, "event"),
        request_id: string_field(value, "request_id"),
        client_request_id: string_field(value, "client_request_id"),
        query_id: string_field(value, "query_id"),
        session_id: string_field(value, "session_id"),
        client_id: string_field(value, "client_id"),
        path: string_field(value, "path"),
        method: string_field(value, "method"),
        model: string_field(value, "model"),
        upstream: string_field(value, "upstream"),
        provider_id: string_field(value, "provider_id"),
        upstream_format: string_field(value, "upstream_format"),
        inbound_format: string_field(value, "inbound_format"),
        request_kind: string_field(value, "request_kind"),
        route_reason: string_field(value, "route_reason"),
        route_mode: string_field(value, "route_mode"),
        failure_class: string_field(value, "failure_class"),
        retryable: value.get("retryable").and_then(Value::as_bool),
        attempt: value.get("attempt").and_then(Value::as_i64),
        max_attempts: value.get("max_attempts").and_then(Value::as_i64),
        delay_ms: value.get("delay_ms").and_then(Value::as_i64),
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

pub(crate) fn classify_event(value: &Value) -> String {
    let event = value.get("event").and_then(Value::as_str).unwrap_or("");
    let upstream = value.get("upstream").and_then(Value::as_str).unwrap_or("");
    let detail = value
        .get("detail")
        .and_then(Value::as_str)
        .unwrap_or("")
        .to_ascii_lowercase();
    match event {
        "upstream_retry" => "recovery".to_string(),
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
