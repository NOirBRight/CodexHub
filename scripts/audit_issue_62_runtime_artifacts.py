#!/usr/bin/env python3
"""Sanitize the bounded read-only runtime evidence used by Issue #62."""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


TRANSPORT_TARGET = "codex_http_client::transport"
POST_BODY_PATTERN = re.compile(
    r": POST to (?P<endpoint>https?://.+?): (?P<body>\{.*\})\s*$",
    re.DOTALL,
)
KNOWN_INPUT_ITEM_TYPES = {
    "additional_tools",
    "agent_message",
    "compaction",
    "compaction_trigger",
    "computer_initialize_state",
    "custom_tool_call",
    "custom_tool_call_output",
    "function_call",
    "function_call_output",
    "local_shell_call",
    "message",
    "reasoning",
    "tool_search_call",
    "tool_search_output",
    "web_search_call",
}
ROUTE_FIELDS = (
    "upstream",
    "route_mode",
    "behavior_profile",
    "inbound_format",
    "upstream_format",
    "wire_format_adapter",
    "codex_semantic_adapter",
    "repair_policy",
)


def _is_response_body_fingerprint_field(name: Any) -> bool:
    if not isinstance(name, str):
        return False
    normalized = name.lower()
    return "response_body" in normalized and any(
        marker in normalized
        for marker in ("digest", "fingerprint", "hash", "hmac", "sha")
    )


def _read_only_connection(path: Path) -> sqlite3.Connection:
    uri = f"{path.resolve().as_uri()}?mode=ro"
    return sqlite3.connect(uri, uri=True)


def _parse_iso_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _unix_seconds(value: str) -> int:
    return int(_parse_iso_timestamp(value).timestamp())


def _endpoint_class(endpoint: str) -> str:
    parsed = urlparse(endpoint)
    host = (parsed.hostname or "").lower()
    if host in {"127.0.0.1", "localhost", "::1"} and parsed.port == 9099:
        return "codexhub_local"
    if host == "chatgpt.com" or host.endswith(".chatgpt.com"):
        return "official_direct"
    return "other"


def _sanitize_tool_choice(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if not isinstance(value, dict):
        return "unclassified"
    sanitized: dict[str, Any] = {}
    if isinstance(value.get("type"), str):
        sanitized["type"] = value["type"]
    if isinstance(value.get("name"), str):
        sanitized["name"] = value["name"]
    function = value.get("function")
    if isinstance(function, dict) and isinstance(function.get("name"), str):
        sanitized["function_name"] = function["name"]
    return sanitized or "unclassified"


def _sanitize_tool(tool: dict[str, Any]) -> dict[str, Any]:
    tool_type = tool.get("type") if isinstance(tool.get("type"), str) else None
    name = tool.get("name") if isinstance(tool.get("name"), str) else None
    sanitized: dict[str, Any] = {
        "defer_loading_present": "defer_loading" in tool or "deferLoading" in tool,
        "name": name,
        "type": tool_type,
    }
    if "defer_loading" in tool or "deferLoading" in tool:
        sanitized["defer_loading"] = bool(
            tool.get("defer_loading", tool.get("deferLoading"))
        )
    if tool_type == "namespace":
        sanitized["namespace_tools"] = [
            nested["name"]
            for nested in tool.get("tools", [])
            if isinstance(nested, dict) and isinstance(nested.get("name"), str)
        ]
    if tool_type == "tool_search" and isinstance(tool.get("execution"), str):
        sanitized["execution"] = tool["execution"]
    return sanitized


def _sanitize_request_plan(payload: dict[str, Any]) -> dict[str, Any]:
    item_types: Counter[str] = Counter()
    tool_surface: list[dict[str, Any]] = []
    for item in payload.get("input", []):
        if not isinstance(item, dict):
            item_types["<non_object>"] += 1
            continue
        item_type = item.get("type") if isinstance(item.get("type"), str) else "<missing>"
        item_types[item_type] += 1
        if item_type != "additional_tools":
            continue
        tool_surface.extend(
            _sanitize_tool(tool)
            for tool in item.get("tools", [])
            if isinstance(tool, dict)
        )

    tool_surface.extend(
        _sanitize_tool(tool)
        for tool in payload.get("tools", [])
        if isinstance(tool, dict)
    )
    return {
        "input_item_type_counts": dict(sorted(item_types.items())),
        "parallel_tool_calls": payload.get("parallel_tool_calls")
        if isinstance(payload.get("parallel_tool_calls"), bool)
        else None,
        "stream": payload.get("stream") if isinstance(payload.get("stream"), bool) else None,
        "tool_choice": _sanitize_tool_choice(payload.get("tool_choice")),
        "tool_surface": tool_surface,
        "top_level_field_presence": sorted(payload),
    }


def _codex_request_evidence(
    path: Path,
    *,
    model: str,
    gateway_started_at: str,
    app_server_started_at: str,
    snapshot_ended_at: str,
) -> dict[str, Any]:
    gateway_cutoff = _unix_seconds(gateway_started_at)
    app_server_cutoff = _unix_seconds(app_server_started_at)
    snapshot_cutoff = _unix_seconds(snapshot_ended_at)
    query_cutoff = min(gateway_cutoff, app_server_cutoff)
    plan_counts: Counter[str] = Counter()
    sanitized_plans: dict[str, dict[str, Any]] = {}
    observed_item_types: Counter[str] = Counter()
    current_endpoint_classes: Counter[str] = Counter()
    top_level_fields: set[str] = set()
    transport_log_rows = 0

    connection = _read_only_connection(path)
    try:
        rows = connection.execute(
            """
            SELECT ts, feedback_log_body
            FROM logs
            WHERE target = ? AND ts >= ? AND ts <= ?
            ORDER BY ts, id
            """,
            (TRANSPORT_TARGET, query_cutoff, snapshot_cutoff),
        )
        for ts, log_body in rows:
            if not isinstance(log_body, str):
                continue
            match = POST_BODY_PATTERN.search(log_body)
            if match is None:
                continue
            endpoint_class = _endpoint_class(match.group("endpoint"))
            try:
                payload = json.loads(match.group("body"))
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict) or payload.get("model") != model:
                continue

            if ts >= app_server_cutoff:
                current_endpoint_classes[endpoint_class] += 1
            if ts < gateway_cutoff or endpoint_class != "codexhub_local":
                continue

            transport_log_rows += 1
            sanitized = _sanitize_request_plan(payload)
            for item_type, count in sanitized["input_item_type_counts"].items():
                observed_item_types[item_type] += count
            top_level_fields.update(sanitized["top_level_field_presence"])
            plan_shape = {
                key: value
                for key, value in sanitized.items()
                if key not in {"input_item_type_counts", "top_level_field_presence"}
            }
            key = json.dumps(
                plan_shape,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            )
            plan_counts[key] += 1
            sanitized_plans[key] = plan_shape
    finally:
        connection.close()

    variants = []
    surface_names: dict[str, str] = {}
    tool_surfaces: dict[str, list[dict[str, Any]]] = {}
    for index, (key, count) in enumerate(
        sorted(plan_counts.items(), key=lambda item: (-item[1], item[0])),
        start=1,
    ):
        variant = dict(sanitized_plans[key])
        surface = variant.pop("tool_surface")
        surface_key = json.dumps(surface, sort_keys=True, separators=(",", ":"))
        surface_name = surface_names.get(surface_key)
        if surface_name is None:
            surface_name = f"surface_{len(surface_names) + 1:02d}"
            surface_names[surface_key] = surface_name
            tool_surfaces[surface_name] = surface
        variant["tool_surface"] = surface_name
        variant["plan"] = f"plan_{index:02d}"
        variant["transport_log_rows"] = count
        variants.append(variant)

    unclassified = sorted(set(observed_item_types) - KNOWN_INPUT_ITEM_TYPES)
    return {
        "current_request_endpoint_classes": dict(sorted(current_endpoint_classes.items())),
        "model_visible_request_plan": {
            "model": model,
            "observed_input_item_type_counts": dict(sorted(observed_item_types.items())),
            "plan_variants": variants,
            "tool_surfaces": tool_surfaces,
            "top_level_field_presence": sorted(top_level_fields),
            "transport_log_rows": transport_log_rows,
            "unclassified_item_types": unclassified,
        },
    }


def _safe_route(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        field: payload.get(field)
        if isinstance(payload.get(field), (str, bool, int, float))
        else None
        for field in ROUTE_FIELDS
    }


def _gateway_evidence(
    path: Path,
    *,
    gateway_started_at: str,
    app_server_started_at: str,
    snapshot_ended_at: str,
) -> dict[str, Any]:
    request_starts = 0
    streaming_requests = 0
    non_streaming_requests = 0
    prefix_equal = 0
    prefix_mismatch = 0
    prefix_unavailable = 0
    full_body_hmac_pairs = 0
    full_body_hmac_both_skipped = 0
    gateway_requests_after_app_server_start = 0
    sse_event_types: Counter[str] = Counter()
    route_counts: Counter[str] = Counter()
    routes: dict[str, dict[str, Any]] = {}
    response_fingerprint_keys: set[str] = set()
    app_server_time = _parse_iso_timestamp(app_server_started_at)

    connection = _read_only_connection(path)
    try:
        rows = connection.execute(
            """
            SELECT ts, event, payload_json
            FROM gateway_events
            WHERE ts >= ? AND ts <= ?
            ORDER BY event_id
            """,
            (gateway_started_at, snapshot_ended_at),
        )
        for event_ts, event, payload_json in rows:
            try:
                payload = json.loads(payload_json)
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            response_fingerprint_keys.update(
                key
                for key in payload
                if _is_response_body_fingerprint_field(key)
            )
            if payload.get("upstream") != "official":
                continue
            if event == "request_start":
                request_starts += 1
                is_stream = payload.get("is_stream")
                if is_stream is True or is_stream == 1:
                    streaming_requests += 1
                elif is_stream is False or is_stream == 0:
                    non_streaming_requests += 1

                caller_prefix = payload.get("caller_request_prefix_hmac")
                upstream_prefix = payload.get("upstream_request_prefix_hmac")
                if isinstance(caller_prefix, str) and isinstance(upstream_prefix, str):
                    if caller_prefix == upstream_prefix:
                        prefix_equal += 1
                    else:
                        prefix_mismatch += 1
                else:
                    prefix_unavailable += 1

                if isinstance(payload.get("caller_request_body_hmac"), str) and isinstance(
                    payload.get("upstream_request_body_hmac"), str
                ):
                    full_body_hmac_pairs += 1
                if (
                    payload.get("caller_request_body_hmac_skipped") is True
                    and payload.get("upstream_request_body_hmac_skipped") is True
                ):
                    full_body_hmac_both_skipped += 1

                route = _safe_route(payload)
                route_key = json.dumps(route, sort_keys=True, separators=(",", ":"))
                route_counts[route_key] += 1
                routes[route_key] = route
                try:
                    if _parse_iso_timestamp(event_ts) >= app_server_time:
                        gateway_requests_after_app_server_start += 1
                except (TypeError, ValueError):
                    pass
            elif event == "request_complete":
                values = payload.get("sse_event_types")
                if isinstance(values, list):
                    for value in values:
                        if isinstance(value, str):
                            sse_event_types[value] += 1

        table_names = {
            row[1]
            for row in connection.execute("PRAGMA table_info(gateway_requests)")
            if len(row) > 1 and isinstance(row[1], str)
        }
        response_fingerprint_keys.update(
            name
            for name in table_names
            if _is_response_body_fingerprint_field(name)
        )
    finally:
        connection.close()

    route_variants = []
    for index, (key, count) in enumerate(
        sorted(route_counts.items(), key=lambda item: (-item[1], item[0])),
        start=1,
    ):
        route_variants.append(
            {"request_starts": count, "route": f"route_{index:02d}", **routes[key]}
        )

    return {
        "gateway_requests_after_app_server_start": gateway_requests_after_app_server_start,
        "gateway_identity_route": {
            "full_body_hmac_both_skipped": full_body_hmac_both_skipped,
            "full_body_hmac_pairs": full_body_hmac_pairs,
            "non_streaming_requests": non_streaming_requests,
            "observed_sse_event_type_counts": dict(sorted(sse_event_types.items())),
            "prefix_equal": prefix_equal,
            "prefix_mismatch": prefix_mismatch,
            "prefix_unavailable": prefix_unavailable,
            "request_starts": request_starts,
            "response_body_fingerprint_fields_present": bool(response_fingerprint_keys),
            "route_variants": route_variants,
            "streaming_requests": streaming_requests,
        },
    }


def audit_artifacts(
    *,
    codex_log_db: Path,
    gateway_db: Path,
    model: str,
    gateway_started_at: str,
    app_server_started_at: str,
    config_written_at: str,
    catalog_written_at: str,
    snapshot_ended_at: str,
) -> dict[str, Any]:
    codex = _codex_request_evidence(
        codex_log_db,
        model=model,
        gateway_started_at=gateway_started_at,
        app_server_started_at=app_server_started_at,
        snapshot_ended_at=snapshot_ended_at,
    )
    gateway = _gateway_evidence(
        gateway_db,
        gateway_started_at=gateway_started_at,
        app_server_started_at=app_server_started_at,
        snapshot_ended_at=snapshot_ended_at,
    )

    app_server_time = _parse_iso_timestamp(app_server_started_at)
    config_after_start = _parse_iso_timestamp(config_written_at) > app_server_time
    catalog_before_start = _parse_iso_timestamp(catalog_written_at) <= app_server_time
    current_endpoint_classes = codex["current_request_endpoint_classes"]
    current_codexhub_rows = current_endpoint_classes.get("codexhub_local", 0)
    clean_cold_start_proven = (
        not config_after_start
        and catalog_before_start
        and current_codexhub_rows > 0
        and gateway["gateway_requests_after_app_server_start"] > 0
    )

    planner = codex["model_visible_request_plan"]
    identity = gateway["gateway_identity_route"]
    choice_observed = any(
        variant.get("tool_choice") is not None
        and isinstance(variant.get("parallel_tool_calls"), bool)
        for variant in planner["plan_variants"]
    )
    full_pre_post_met = (
        identity["request_starts"] > 0
        and identity["full_body_hmac_pairs"] == identity["request_starts"]
        and identity["response_body_fingerprint_fields_present"]
    )
    zero_unclassified = (
        not planner["unclassified_item_types"] and identity["prefix_mismatch"] == 0
    )

    return {
        "capture_kind": "sanitized_bounded_read_only_audit",
        "gate_classification": {
            "choice_controls": "observed" if choice_observed else "unclassified",
            "clean_cold_start_current_binding": "met"
            if clean_cold_start_proven
            else "live_control_required",
            "complete_contributors_runtime_gate": "partial",
            "full_pre_post_request_response": "met"
            if full_pre_post_met
            else "live_control_required",
            "non_direct_states": "live_control_required",
            "non_streaming": "observed"
            if identity["non_streaming_requests"] > 0
            else "live_control_required",
            "zero_unclassified_identity": "partial"
            if zero_unclassified and not full_pre_post_met
            else ("met" if zero_unclassified else "not_met"),
        },
        "gateway_identity_route": identity,
        "model_visible_request_plan": planner,
        "runtime_timeline": {
            "catalog_written_before_app_server_start": catalog_before_start,
            "clean_cold_start_for_current_binding_proven": clean_cold_start_proven,
            "config_written_after_app_server_start": config_after_start,
            "current_request_endpoint_classes": current_endpoint_classes,
            "gateway_requests_after_app_server_start": gateway[
                "gateway_requests_after_app_server_start"
            ],
        },
        "sanitization": {
            "emits_existing_hash_values": False,
            "emits_full_bodies": False,
            "emits_headers_or_credentials": False,
            "emits_paths": False,
            "emits_prompt_arguments_or_outputs": False,
            "emits_session_task_or_call_identifiers": False,
            "output_classes": [
                "booleans",
                "counts",
                "enums",
                "field_presence",
                "schema_names",
            ],
        },
        "schema_version": 1,
        "sources": {
            "codex_transport_requests": "read_only_sqlite",
            "gateway_events": "read_only_sqlite",
            "merged_baseline": "PR_95",
        },
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-log-db", type=Path, required=True)
    parser.add_argument("--gateway-db", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--gateway-started-at", required=True)
    parser.add_argument("--app-server-started-at", required=True)
    parser.add_argument("--config-written-at", required=True)
    parser.add_argument("--catalog-written-at", required=True)
    parser.add_argument("--snapshot-ended-at", required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    audit = audit_artifacts(
        codex_log_db=args.codex_log_db,
        gateway_db=args.gateway_db,
        model=args.model,
        gateway_started_at=args.gateway_started_at,
        app_server_started_at=args.app_server_started_at,
        config_written_at=args.config_written_at,
        catalog_written_at=args.catalog_written_at,
        snapshot_ended_at=args.snapshot_ended_at,
    )
    print(json.dumps(audit, ensure_ascii=True, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
