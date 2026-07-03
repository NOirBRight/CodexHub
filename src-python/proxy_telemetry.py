from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Any, Mapping

EVENT_SCHEMA_VERSION = 2
TELEMETRY_DB_FILENAME = "codex-proxy-telemetry.sqlite"
TELEMETRY_SECRET_FILENAME = "telemetry-secret"
REQUEST_PREFIX_BYTES = 65536
RUNTIME_SQLITE_TIMEOUT_SECONDS = 0.25
RUNTIME_SQLITE_BUSY_TIMEOUT_MS = 250
BACKFILL_SQLITE_TIMEOUT_SECONDS = 5.0
BACKFILL_SQLITE_BUSY_TIMEOUT_MS = 5000

SENSITIVE_FIELD_NAMES = {
    "api-key",
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "openai-api-key",
    "proxy-authorization",
    "set-cookie",
    "x-api-key",
}

REQUEST_COLUMNS = [
    "schema_version",
    "first_ts",
    "completed_ts",
    "method",
    "path",
    "status",
    "duration_ms",
    "is_stream",
    "content_length",
    "decoded_content_length",
    "content_type",
    "content_encoding",
    "content_decoded",
    "client_id",
    "client_inference_source",
    "user_agent_hash",
    "thread_id",
    "session_id",
    "window_id",
    "turn_id",
    "request_kind",
    "thread_source",
    "route_mode",
    "route_reason",
    "provider_id",
    "upstream",
    "upstream_format",
    "inbound_format",
    "model",
    "model_requested",
    "model_canonical",
    "provider_config_hash",
    "request_body_hmac",
    "request_prefix_hmac",
    "prefix_bytes",
    "prompt_cache_key_hash",
    "usage_source",
    "usage_missing_reason",
    "usage_input_tokens",
    "usage_cached_input_tokens",
    "usage_output_tokens",
    "usage_total_tokens",
    "usage_reasoning_tokens",
    "payload_json",
    "created_at",
    "updated_at",
]

INTEGER_COLUMNS = {
    "schema_version",
    "status",
    "duration_ms",
    "is_stream",
    "content_length",
    "decoded_content_length",
    "content_decoded",
    "prefix_bytes",
    "usage_input_tokens",
    "usage_cached_input_tokens",
    "usage_output_tokens",
    "usage_total_tokens",
    "usage_reasoning_tokens",
}


def _request_column_type(column: str) -> str:
    if column in INTEGER_COLUMNS:
        return "INTEGER"
    if column in {"created_at", "updated_at"}:
        return "TEXT NOT NULL DEFAULT ''"
    return "TEXT"


def telemetry_db_path(codex_home: Path) -> Path:
    return codex_home / "proxy" / TELEMETRY_DB_FILENAME


def telemetry_secret_path(codex_home: Path) -> Path:
    return codex_home / "proxy" / TELEMETRY_SECRET_FILENAME


def prepare_event_payload(event: str, fields: Mapping[str, Any], codex_home: Path) -> dict[str, Any]:
    payload = sanitize_mapping(
        {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "event": event,
            "schema_version": EVENT_SCHEMA_VERSION,
            **fields,
        }
    )
    _apply_field_defaults(payload, codex_home)
    return payload


def enrich_request_observability(
    *,
    body: bytes,
    codex_home: Path,
    upstream: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    prefix = body[:REQUEST_PREFIX_BYTES]
    fields: dict[str, Any] = {
        "request_body_hmac": telemetry_hmac(codex_home, b"body", body),
        "request_prefix_hmac": telemetry_hmac(codex_home, b"prefix", prefix),
        "prefix_bytes": len(prefix),
    }
    cache_key = _extract_prompt_cache_key(body)
    if cache_key:
        fields["prompt_cache_key_hash"] = telemetry_hmac(codex_home, b"prompt-cache-key", cache_key.encode("utf-8"))
    if upstream:
        provider_config = {
            "name": upstream.get("name"),
            "base_url": upstream.get("base_url"),
            "upstream_format": upstream.get("upstream_format"),
            "auth": upstream.get("auth"),
        }
        fields["provider_config_hash"] = telemetry_hmac(
            codex_home,
            b"provider-config",
            json.dumps(provider_config, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8"),
        )
    return fields


def telemetry_hmac(codex_home: Path, label: bytes, data: bytes) -> str:
    secret = _load_or_create_secret(codex_home)
    return hmac.new(secret, label + b"\0" + data, hashlib.sha256).hexdigest()


def sanitize_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in value.items():
        if _is_sensitive_key(key):
            continue
        result[key] = _sanitize_value(item)
    return result


def write_event_to_sqlite(db_path: Path, payload: Mapping[str, Any]) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    payload_json = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    event_hash = stable_event_hash(payload, payload_json)
    connection = sqlite3.connect(db_path, timeout=RUNTIME_SQLITE_TIMEOUT_SECONDS)
    try:
        initialize_db(connection, busy_timeout_ms=RUNTIME_SQLITE_BUSY_TIMEOUT_MS)
        connection.execute(
            """
            INSERT OR IGNORE INTO gateway_events (event_hash, ts, event, request_id, payload_json)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                event_hash,
                _string(payload.get("ts")) or "",
                _string(payload.get("event")) or "",
                _string(payload.get("request_id")),
                payload_json,
            ),
        )
        _upsert_request(connection, payload, payload_json)
        connection.commit()
    finally:
        connection.close()


def backfill_event_log_to_sqlite(event_log_path: Path, db_path: Path) -> int:
    if not event_log_path.exists():
        return 0
    count = 0
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, timeout=BACKFILL_SQLITE_TIMEOUT_SECONDS)
    try:
        initialize_db(connection, busy_timeout_ms=BACKFILL_SQLITE_BUSY_TIMEOUT_MS)
        with event_log_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or not line.startswith("{"):
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, dict):
                    continue
                sanitized = sanitize_mapping(payload)
                sanitized.setdefault("schema_version", EVENT_SCHEMA_VERSION)
                payload_json = json.dumps(sanitized, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
                event_hash = stable_event_hash(sanitized, payload_json)
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO gateway_events (event_hash, ts, event, request_id, payload_json)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        event_hash,
                        _string(sanitized.get("ts")) or "",
                        _string(sanitized.get("event")) or "",
                        _string(sanitized.get("request_id")),
                        payload_json,
                    ),
                )
                _upsert_request(connection, sanitized, payload_json)
                if cursor.rowcount:
                    count += 1
        connection.execute(
            """
            INSERT INTO telemetry_meta (key, value)
            VALUES ('last_backfill_at', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),),
        )
        connection.execute(
            """
            INSERT INTO telemetry_meta (key, value)
            VALUES ('last_backfill_size', ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            (str(event_log_path.stat().st_size if event_log_path.exists() else 0),),
        )
        connection.commit()
    finally:
        connection.close()
    return count


def initialize_db(connection: sqlite3.Connection, *, busy_timeout_ms: int = BACKFILL_SQLITE_BUSY_TIMEOUT_MS) -> None:
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute(f"PRAGMA busy_timeout={max(0, int(busy_timeout_ms))}")
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS gateway_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_hash TEXT NOT NULL UNIQUE,
            ts TEXT NOT NULL,
            event TEXT NOT NULL,
            request_id TEXT,
            payload_json TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
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
        )
        """
    )
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS telemetry_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    _ensure_gateway_request_columns(connection)
    connection.execute("CREATE INDEX IF NOT EXISTS idx_gateway_requests_completed_ts ON gateway_requests(completed_ts)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_gateway_requests_provider_model ON gateway_requests(provider_id, model_canonical)")
    connection.execute("CREATE INDEX IF NOT EXISTS idx_gateway_requests_window ON gateway_requests(window_id)")


def stable_event_hash(_payload: Mapping[str, Any], payload_json: str) -> str:
    return hashlib.sha256(payload_json.encode("utf-8")).hexdigest()


def _ensure_gateway_request_columns(connection: sqlite3.Connection) -> None:
    existing = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(gateway_requests)").fetchall()
        if len(row) > 1
    }
    for column in REQUEST_COLUMNS:
        if column in existing:
            continue
        connection.execute(f"ALTER TABLE gateway_requests ADD COLUMN {column} {_request_column_type(column)}")


def _upsert_request(connection: sqlite3.Connection, payload: Mapping[str, Any], payload_json: str) -> None:
    request_id = _string(payload.get("request_id"))
    event = _string(payload.get("event"))
    if not request_id or event not in {"request_start", "request_complete", "request_error"}:
        return

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    connection.execute(
        """
        INSERT OR IGNORE INTO gateway_requests (request_id, created_at, updated_at)
        VALUES (?, ?, ?)
        """,
        (request_id, now, now),
    )

    values = _request_values(payload, payload_json)
    if event == "request_start":
        values["first_ts"] = _string(payload.get("ts"))
    else:
        values["completed_ts"] = _string(payload.get("ts"))
    values["updated_at"] = now

    assignments = []
    parameters: list[Any] = []
    for column in REQUEST_COLUMNS:
        if column == "created_at" or column not in values:
            continue
        value = values[column]
        if value is None:
            continue
        assignments.append(f"{column} = ?")
        parameters.append(value)
    if not assignments:
        return
    parameters.append(request_id)
    connection.execute(
        f"UPDATE gateway_requests SET {', '.join(assignments)} WHERE request_id = ?",
        parameters,
    )


def _request_values(payload: Mapping[str, Any], payload_json: str) -> dict[str, Any]:
    values: dict[str, Any] = {"payload_json": payload_json}
    event = _string(payload.get("event"))
    for column in REQUEST_COLUMNS:
        if column in {"first_ts", "completed_ts", "created_at", "updated_at", "payload_json"}:
            continue
        if column in payload:
            value = _column_value(column, payload.get(column))
            if event != "request_start" and column in {"client_id", "client_inference_source"} and value == "unknown":
                continue
            values[column] = value

    upstream = _string(payload.get("upstream"))
    model = _string(payload.get("model"))
    if "provider_id" not in values and upstream:
        values["provider_id"] = upstream
    if "model_canonical" not in values and model:
        values["model_canonical"] = model
    if "model_requested" not in values and model:
        values["model_requested"] = model
    if "route_mode" not in values:
        values["route_mode"] = _route_mode(upstream)
    if event == "request_start" and "client_id" not in values:
        values["client_id"] = "unknown"
    if event == "request_start" and "client_inference_source" not in values:
        values["client_inference_source"] = "unknown"
    if "schema_version" not in values:
        values["schema_version"] = EVENT_SCHEMA_VERSION
    return values


def _apply_field_defaults(payload: dict[str, Any], codex_home: Path) -> None:
    upstream = _string(payload.get("upstream"))
    model = _string(payload.get("model"))
    payload.setdefault("provider_id", upstream)
    payload.setdefault("route_mode", _route_mode(upstream))
    payload.setdefault("model_requested", model)
    payload.setdefault("model_canonical", model)
    payload.setdefault("client_id", "unknown")
    payload.setdefault("client_inference_source", "unknown")
    user_agent = _string(payload.pop("user_agent", None))
    if user_agent and "user_agent_hash" not in payload:
        payload["user_agent_hash"] = telemetry_hmac(codex_home, b"user-agent", user_agent.encode("utf-8"))


def _load_or_create_secret(codex_home: Path) -> bytes:
    path = telemetry_secret_path(codex_home)
    try:
        raw = path.read_text(encoding="utf-8").strip()
        if raw:
            return raw.encode("utf-8")
    except OSError:
        pass
    secret = secrets.token_hex(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(secret, encoding="utf-8")
    return secret.encode("utf-8")


def _extract_prompt_cache_key(body: bytes) -> str | None:
    try:
        payload = json.loads(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    value = payload.get("prompt_cache_key")
    return value if isinstance(value, str) and value else None


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return sanitize_mapping(value)
    if isinstance(value, list):
        return [_sanitize_value(item) for item in value]
    return value


def _is_sensitive_key(key: Any) -> bool:
    return isinstance(key, str) and key.strip().lower() in SENSITIVE_FIELD_NAMES


def _route_mode(upstream: str | None) -> str | None:
    if not upstream:
        return None
    if upstream == "official":
        return "official"
    if upstream == "local":
        return "local"
    return "codexhub"


def _column_value(column: str, value: Any) -> Any:
    if value is None:
        return None
    if column in INTEGER_COLUMNS:
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        if isinstance(value, float):
            return int(value)
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            return int(value)
        return None
    return _string(value)


def _string(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)
