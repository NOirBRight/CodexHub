#!/usr/bin/env python3
"""Summarize Gateway transport failure recurrence from telemetry JSONL."""

from __future__ import annotations

import argparse
import json
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any


REQUEST_METADATA_FIELDS = (
    "client_id",
    "provider_id",
    "upstream",
    "model",
    "model_canonical",
    "window_id",
    "content_length",
    "decoded_content_length",
    "is_stream",
)

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

TRANSPORT_DETAIL_SIGNALS = (
    "unexpected_eof",
    "ssleoferror",
    "eof occurred in violation",
    "timed out",
    "timeout",
    "winerror 10060",
    "connection reset",
    "connectionreseterror",
    "winerror 10054",
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


def _infer_failure_phase(event: Mapping[str, Any]) -> str:
    explicit = _string(event.get("failure_phase"))
    if explicit:
        return explicit
    event_name = _string(event.get("event"))
    error_name = _string(event.get("error"))
    detail = f"{event.get('error', '')} {event.get('detail', '')}".lower()
    if event_name == "official_passthrough_stream_closed":
        return "stream_body"
    if "unexpected_eof" in detail or "ssleoferror" in detail or "eof occurred in violation" in detail:
        return "tls_handshake"
    if "timed out" in detail or "timeout" in detail or "winerror 10060" in detail:
        return "tcp_connect"
    if "connection reset" in detail or "connectionreseterror" in detail or "winerror 10054" in detail:
        return "request_write"
    if (
        event_name == "request_error"
        and error_name in GENERIC_TRANSPORT_ERRORS
    ):
        return "tcp_connect"
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
    detail = f"{event.get('error', '')} {event.get('detail', '')}".lower()
    if any(signal in detail for signal in TRANSPORT_DETAIL_SIGNALS):
        return True
    return False


def _is_non_transport_capacity_retry(event: Mapping[str, Any]) -> bool:
    failure_class = (_string(event.get("failure_class")) or "").lower()
    if failure_class in NON_TRANSPORT_FAILURE_CLASSES:
        return True
    detail = f"{event.get('error', '')} {event.get('detail', '')}".lower()
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


def _in_window(event: Mapping[str, Any], since: str | None, until: str | None) -> bool:
    ts = _string(event.get("ts"))
    if ts is None:
        return True
    if since is not None and ts < since:
        return False
    if until is not None and ts > until:
        return False
    return True


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
) -> dict[str, Any]:
    materialized = [dict(event) for event in events]
    start_by_request = {
        str(event["request_id"]): event
        for event in materialized
        if event.get("event") == "request_start" and event.get("request_id")
    }
    groups: dict[tuple[str, ...], dict[str, Any]] = {}
    failure_count = 0

    for raw_event in materialized:
        if not _in_window(raw_event, since, until) or not _is_failure_event(raw_event):
            continue
        event = _enrich(raw_event, start_by_request)
        provider_id = _string(event.get("provider_id")) or _string(event.get("upstream")) or "unknown"
        provider_scope = _provider_scope(provider_id, _string(event.get("upstream")))
        model_canonical = _string(event.get("model_canonical")) or _string(event.get("model")) or "unknown"
        client_id = _string(event.get("client_id")) or "unknown"
        event_name = _string(event.get("event")) or "unknown"
        failure_phase = _infer_failure_phase(event)
        failure_side = _infer_failure_side(event)
        error = _string(event.get("error")) or "unknown"
        size_value = event.get("content_length")
        if size_value in (None, ""):
            size_value = event.get("decoded_content_length")
        size_bucket = _size_bucket(size_value)
        window_id = _string(event.get("window_id")) or "unknown"
        key = (
            provider_scope,
            provider_id,
            model_canonical,
            client_id,
            event_name,
            failure_phase,
            failure_side,
            error,
            size_bucket,
            window_id,
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
                "error": error,
                "size_bucket": size_bucket,
                "window_id": window_id,
                "count": 0,
                "request_ids": [],
                "statuses": [],
                "min_duration_ms": None,
                "max_duration_ms": None,
                "total_lines_streamed": 0,
                "total_bytes_streamed": 0,
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
            item["error"],
            item["size_bucket"],
            item["window_id"],
        ),
    )
    return {
        "since": since,
        "until": until,
        "failure_count": failure_count,
        "group_count": len(ordered_groups),
        "groups": ordered_groups,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="Telemetry JSONL file to analyze.")
    parser.add_argument("--since", default=None, help="Inclusive ISO timestamp lower bound.")
    parser.add_argument("--until", default=None, help="Inclusive ISO timestamp upper bound.")
    parser.add_argument("--json", action="store_true", help="Print JSON. This is the default output.")
    args = parser.parse_args(argv)

    report = analyze_events(load_jsonl(Path(args.input)), since=args.since, until=args.until)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
