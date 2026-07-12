#!/usr/bin/env python3
"""Summarize Gateway transport failure recurrence from telemetry JSONL."""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


REQUEST_METADATA_FIELDS = (
    "client_id",
    "provider_id",
    "upstream",
    "model",
    "model_canonical",
    "content_length",
    "decoded_content_length",
    "is_stream",
)

DEFAULT_TIME_WINDOW_MINUTES = 5

FAILURE_EVENTS = {
    "official_passthrough_stream_closed",
    "upstream_retry",
    "request_error",
}

TRANSPORT_FAILURE_PHASES = {
    "tcp_connect",
    "tls_handshake",
    "request_write",
    "upstream_open",
    "upstream_read",
    "stream_body",
    "downstream_write",
}

LEGACY_TRANSPORT_FAILURES = (
    (
        ("unexpected_eof", "ssleoferror", "eof occurred in violation"),
        "tls_handshake",
        "tls_eof",
    ),
    (
        ("timed out", "timeout", "winerror 10060"),
        "tcp_connect",
        "connect_timeout",
    ),
    (
        ("connection reset", "connectionreseterror", "winerror 10054"),
        "request_write",
        "connection_reset",
    ),
)

ADDITIONAL_TRANSPORT_DETAIL_SIGNALS = (
    "connection refused",
    "winerror 10061",
    "name or service not known",
    "nodename nor servname provided",
    "temporary failure in name resolution",
    "getaddrinfo failed",
)

GENERIC_TRANSPORT_ERRORS = {
    "URLError",
    "OSError",
}

NON_TRANSPORT_FAILURE_CLASSES = {
    "capacity",
    "provider_throttle",
    "provider_overloaded",
}

NON_TRANSPORT_DETAIL_SIGNALS = (
    "rate limit",
    "too many requests",
    "overloaded",
    "capacity",
    "retry-after",
)


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSONL: {exc}") from exc
            if isinstance(payload, dict):
                events.append(payload)
    return events


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _is_missing_metadata(value: Any) -> bool:
    if value in (None, ""):
        return True
    return isinstance(value, str) and value.lower() == "unknown"


def _size_bucket(value: Any) -> str:
    length = _as_int(value)
    if length is None:
        return "unknown"
    if length < 64 * 1024:
        return "<64KB"
    if length < 256 * 1024:
        return "64KB-256KB"
    if length < 512 * 1024:
        return "256KB-512KB"
    if length < 1024 * 1024:
        return "512KB-1MB"
    return ">=1MB"


def _provider_scope(provider_id: str, upstream: str | None = None) -> str:
    if provider_id == "official" or upstream == "official":
        return "official"
    return "third_party"


def _parse_timestamp(value: str | None) -> datetime | None:
    if value is None:
        return None
    normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _event_error_detail(event: Mapping[str, Any]) -> str:
    return f"{event.get('error', '')} {event.get('detail', '')}".lower()


def _legacy_transport_failure(event: Mapping[str, Any]) -> tuple[str, str] | None:
    detail = _event_error_detail(event)
    for signals, phase, failure_class in LEGACY_TRANSPORT_FAILURES:
        if any(signal in detail for signal in signals):
            return phase, failure_class
    return None


def _time_window(event: Mapping[str, Any], window_minutes: int) -> str:
    timestamp = _parse_timestamp(_string(event.get("ts")))
    if timestamp is None:
        return "unknown"
    window_seconds = window_minutes * 60
    start_seconds = int(timestamp.timestamp()) // window_seconds * window_seconds
    start = datetime.fromtimestamp(start_seconds, tz=timezone.utc)
    end = start + timedelta(minutes=window_minutes)
    return f"{_format_timestamp(start)}/{_format_timestamp(end)}"


def _infer_failure_phase(event: Mapping[str, Any]) -> str:
    explicit = _string(event.get("failure_phase"))
    if explicit:
        return explicit
    event_name = _string(event.get("event"))
    error_name = _string(event.get("error"))
    if event_name == "official_passthrough_stream_closed":
        return "stream_body"
    legacy_failure = _legacy_transport_failure(event)
    if legacy_failure is not None:
        return legacy_failure[0]
    if (
        event_name == "request_error"
        and error_name in GENERIC_TRANSPORT_ERRORS
    ):
        return "tcp_connect"
    return "unknown"


def _infer_failure_class(event: Mapping[str, Any]) -> str:
    explicit = _string(event.get("failure_class"))
    if explicit and explicit.lower() != "unknown":
        return explicit
    legacy_failure = _legacy_transport_failure(event)
    if legacy_failure is not None:
        return legacy_failure[1]
    return "unknown"


def _infer_failure_side(event: Mapping[str, Any]) -> str:
    explicit = _string(event.get("failure_side"))
    if explicit:
        return explicit
    if _string(event.get("event")) == "official_passthrough_stream_closed":
        return "upstream_read"
    return "upstream_open"


def _has_transport_detail_signal(event: Mapping[str, Any]) -> bool:
    explicit_phase = _string(event.get("failure_phase"))
    if explicit_phase in TRANSPORT_FAILURE_PHASES:
        return True
    if _legacy_transport_failure(event) is not None:
        return True
    detail = _event_error_detail(event)
    return any(signal in detail for signal in ADDITIONAL_TRANSPORT_DETAIL_SIGNALS)


def _is_non_transport_capacity_retry(event: Mapping[str, Any]) -> bool:
    failure_class = (_string(event.get("failure_class")) or "").lower()
    if failure_class in NON_TRANSPORT_FAILURE_CLASSES:
        return True
    detail = _event_error_detail(event)
    return any(signal in detail for signal in NON_TRANSPORT_DETAIL_SIGNALS)


def _is_failure_event(event: Mapping[str, Any]) -> bool:
    event_name = _string(event.get("event"))
    if event_name not in FAILURE_EVENTS:
        return False
    if event_name == "official_passthrough_stream_closed":
        return True
    if event_name == "upstream_retry":
        if _string(event.get("failure_phase")) in TRANSPORT_FAILURE_PHASES:
            return True
        if _is_non_transport_capacity_retry(event):
            return False
        return _has_transport_detail_signal(event)
    if event_name == "request_error":
        error_name = _string(event.get("error"))
        if error_name in GENERIC_TRANSPORT_ERRORS:
            return True
        return _has_transport_detail_signal(event)
    return False


def _parse_window_bound(value: str | None, name: str) -> datetime | None:
    if value is None:
        return None
    parsed = _parse_timestamp(value)
    if parsed is None:
        raise ValueError(f"{name} must be a valid ISO 8601 timestamp")
    return parsed


def _window_status(
    event: Mapping[str, Any],
    since: datetime | None,
    until: datetime | None,
) -> str:
    timestamp_text = _string(event.get("ts"))
    if timestamp_text is None:
        return "included" if since is None and until is None else "missing_timestamp"
    timestamp = _parse_timestamp(timestamp_text)
    if timestamp is None:
        return "included" if since is None and until is None else "invalid_timestamp"
    if since is not None and timestamp < since:
        return "before_since"
    if until is not None and timestamp > until:
        return "after_until"
    return "included"


def _enrich(event: Mapping[str, Any], start_by_request: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    enriched = dict(event)
    request_id = _string(event.get("request_id"))
    if request_id is None:
        return enriched
    start = start_by_request.get(request_id)
    if start is None:
        return enriched
    for field in REQUEST_METADATA_FIELDS:
        if _is_missing_metadata(enriched.get(field)) and not _is_missing_metadata(start.get(field)):
            enriched[field] = start[field]
    return enriched


def analyze_events(
    events: Iterable[Mapping[str, Any]],
    *,
    since: str | None = None,
    until: str | None = None,
    window_minutes: int = DEFAULT_TIME_WINDOW_MINUTES,
) -> dict[str, Any]:
    if window_minutes < 1:
        raise ValueError("window_minutes must be at least 1")
    since_timestamp = _parse_window_bound(since, "since")
    until_timestamp = _parse_window_bound(until, "until")
    if since_timestamp is not None and until_timestamp is not None and since_timestamp > until_timestamp:
        raise ValueError("since must not be after until")
    materialized = [dict(event) for event in events]
    start_by_request = {
        str(event["request_id"]): event
        for event in materialized
        if event.get("event") == "request_start" and event.get("request_id")
    }
    groups: dict[tuple[str, ...], dict[str, Any]] = {}
    failure_count = 0
    skipped_out_of_window_count = 0
    skipped_missing_timestamp_count = 0
    skipped_invalid_timestamp_count = 0

    for raw_event in materialized:
        if not _is_failure_event(raw_event):
            continue
        window_status = _window_status(raw_event, since_timestamp, until_timestamp)
        if window_status != "included":
            if window_status in {"before_since", "after_until"}:
                skipped_out_of_window_count += 1
            elif window_status == "missing_timestamp":
                skipped_missing_timestamp_count += 1
            else:
                skipped_invalid_timestamp_count += 1
            continue
        event = _enrich(raw_event, start_by_request)
        provider_id = _string(event.get("provider_id")) or _string(event.get("upstream")) or "unknown"
        provider_scope = _provider_scope(provider_id, _string(event.get("upstream")))
        model_canonical = _string(event.get("model_canonical")) or _string(event.get("model")) or "unknown"
        client_id = _string(event.get("client_id")) or "unknown"
        event_name = _string(event.get("event")) or "unknown"
        failure_phase = _infer_failure_phase(event)
        failure_side = _infer_failure_side(event)
        failure_class = _infer_failure_class(event)
        error = _string(event.get("error")) or "unknown"
        time_window = _time_window(event, window_minutes)
        size_value = event.get("content_length")
        if size_value in (None, ""):
            size_value = event.get("decoded_content_length")
        size_bucket = _size_bucket(size_value)
        key = (
            provider_scope,
            provider_id,
            model_canonical,
            client_id,
            event_name,
            failure_phase,
            failure_side,
            failure_class,
            error,
            size_bucket,
            time_window,
        )
        group = groups.get(key)
        if group is None:
            group = {
                "provider_scope": provider_scope,
                "provider_id": provider_id,
                "model_canonical": model_canonical,
                "client_id": client_id,
                "event": event_name,
                "failure_phase": failure_phase,
                "failure_side": failure_side,
                "failure_class": failure_class,
                "error": error,
                "size_bucket": size_bucket,
                "time_window": time_window,
                "count": 0,
                "request_ids": [],
                "statuses": [],
                "min_duration_ms": None,
                "max_duration_ms": None,
                "total_lines_streamed": 0,
                "total_bytes_streamed": 0,
                "examples": [],
            }
            groups[key] = group
        group["count"] += 1
        failure_count += 1
        request_id = _string(event.get("request_id"))
        if request_id and request_id not in group["request_ids"]:
            group["request_ids"].append(request_id)
        status = _as_int(event.get("status"))
        if status is not None and status not in group["statuses"]:
            group["statuses"].append(status)
        duration_ms = _as_int(event.get("duration_ms"))
        if duration_ms is not None:
            current_min = group["min_duration_ms"]
            current_max = group["max_duration_ms"]
            group["min_duration_ms"] = duration_ms if current_min is None else min(current_min, duration_ms)
            group["max_duration_ms"] = duration_ms if current_max is None else max(current_max, duration_ms)
        if len(group["examples"]) < 3:
            example = {
                "request_id": request_id,
                "content_length": _as_int(size_value),
                "duration_ms": duration_ms,
                "error": _string(event.get("error")),
                "detail": _string(event.get("detail")),
            }
            group["examples"].append({key: value for key, value in example.items() if value is not None})
        group["total_lines_streamed"] += _as_int(event.get("lines_streamed")) or 0
        group["total_bytes_streamed"] += _as_int(event.get("bytes_streamed")) or 0

    ordered_groups = sorted(
        groups.values(),
        key=lambda item: (
            -int(item["count"]),
            item["provider_scope"],
            item["provider_id"],
            item["model_canonical"],
            item["client_id"],
            item["event"],
            item["failure_phase"],
            item["failure_side"],
            item["failure_class"],
            item["error"],
            item["size_bucket"],
            item["time_window"],
        ),
    )
    return {
        "since": since,
        "until": until,
        "time_window_minutes": window_minutes,
        "failure_count": failure_count,
        "skipped_out_of_window_count": skipped_out_of_window_count,
        "skipped_missing_timestamp_count": skipped_missing_timestamp_count,
        "skipped_invalid_timestamp_count": skipped_invalid_timestamp_count,
        "group_count": len(ordered_groups),
        "groups": ordered_groups,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="Telemetry JSONL file to analyze.")
    parser.add_argument("--since", default=None, help="Inclusive ISO timestamp lower bound.")
    parser.add_argument("--until", default=None, help="Inclusive ISO timestamp upper bound.")
    parser.add_argument(
        "--window-minutes",
        type=int,
        default=DEFAULT_TIME_WINDOW_MINUTES,
        help="Group failures into UTC time windows of this size (default: 5).",
    )
    parser.add_argument("--json", action="store_true", help="Print JSON. This is the default output.")
    args = parser.parse_args(argv)

    if args.window_minutes < 1:
        parser.error("--window-minutes must be at least 1")
    try:
        report = analyze_events(
            load_jsonl(Path(args.input)),
            since=args.since,
            until=args.until,
            window_minutes=args.window_minutes,
        )
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
