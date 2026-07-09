"""Pure Codex semantic compatibility helpers.

This module is intentionally free of Gateway HTTP handler, upstream I/O,
telemetry, and retry dependencies. It owns data-shape normalization that makes
third-party tool calls fit Codex expectations.
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping

MULTI_AGENT_TOOL_NAMES = {
    "spawn_agent",
    "send_input",
    "wait_agent",
    "close_agent",
    "resume_agent",
}

MULTI_AGENT_DISCOVERY_QUERY = "spawn_agent multi_agent subagent native Codex"


def json_object_from_arguments(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return dict(value)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        try:
            parsed, _end = json.JSONDecoder().raw_decode(text)
        except json.JSONDecodeError:
            return None
    return dict(parsed) if isinstance(parsed, dict) else None


def json_argument_string_needs_repair(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parsed = json.loads(value.strip())
    except json.JSONDecodeError:
        parsed_obj = json_object_from_arguments(value)
        return parsed_obj is not None
    return not isinstance(parsed, dict)


def dump_arguments_like(original: Any, arguments: Mapping[str, Any]) -> Any:
    if isinstance(original, str):
        return json.dumps(arguments, ensure_ascii=True, separators=(",", ":"))
    return dict(arguments)


def json_string_value(value: str) -> Any:
    text = value.strip()
    if not text:
        return value
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return value


def coerce_targets(value: Any) -> tuple[Any, bool]:
    if isinstance(value, str):
        parsed = json_string_value(value)
        if isinstance(parsed, list):
            return parsed, True
        if isinstance(parsed, str):
            return [parsed], True
        return [value], True
    return value, False


def coerce_target(value: Any) -> tuple[Any, bool]:
    if isinstance(value, str):
        parsed = json_string_value(value)
        if isinstance(parsed, list) and parsed:
            return parsed[0], True
        if isinstance(parsed, str) and parsed != value:
            return parsed, True
        return value, False
    if isinstance(value, list) and value:
        return value[0], True
    return value, False


def coerce_number(value: Any) -> tuple[Any, bool]:
    if isinstance(value, str):
        text = value.strip()
        if re.fullmatch(r"[+-]?\d+", text):
            return int(text), True
        if re.fullmatch(r"[+-]?(?:\d+\.\d*|\d*\.\d+)", text):
            return float(text), True
    return value, False


def infer_multi_agent_tool_name(arguments: Mapping[str, Any]) -> str | None:
    if "targets" in arguments:
        return "wait_agent"
    if "target" in arguments:
        return "send_input" if "message" in arguments else "close_agent"
    if "id" in arguments:
        return "resume_agent"
    if any(key in arguments for key in ("agent_type", "fork_context", "message", "prompt", "input")):
        return "spawn_agent"
    return None


def normalize_tool_search_arguments(value: Any) -> dict[str, Any] | None:
    arguments = json_object_from_arguments(value)
    if arguments is None:
        return None

    query = arguments.get("query")
    if not isinstance(query, str) or not query.strip():
        return None

    normalized: dict[str, Any] = {"query": query}
    limit = arguments.get("limit")
    if isinstance(limit, str) and limit.strip().isdigit():
        limit = int(limit.strip())
    if isinstance(limit, int) and limit > 0:
        normalized["limit"] = limit
    return normalized


def multi_agent_discovery_arguments(value: Any) -> dict[str, Any] | None:
    arguments = json_object_from_arguments(value)
    if arguments is None:
        return None

    if arguments:
        return None

    return {"query": MULTI_AGENT_DISCOVERY_QUERY, "limit": 8}


def normalize_multi_agent_arguments(
    value: Any,
    tool_name: str | None,
) -> tuple[Any, str | None, bool]:
    arguments = json_object_from_arguments(value)
    if arguments is None:
        return value, tool_name, False

    changed = False
    resolved_tool_name = tool_name
    if resolved_tool_name is None:
        for key in ("", "tool", "function", "name", "action", "ns_tool", "operation", "method", "tool_name"):
            candidate = arguments.get(key)
            if isinstance(candidate, str) and candidate in MULTI_AGENT_TOOL_NAMES:
                resolved_tool_name = candidate
                arguments.pop(key, None)
                changed = True
                break
    if resolved_tool_name is None:
        resolved_tool_name = infer_multi_agent_tool_name(arguments)

    changed = changed or json_argument_string_needs_repair(value)

    if resolved_tool_name == "spawn_agent":
        if "message" not in arguments:
            for alias in ("prompt", "input"):
                alias_value = arguments.get(alias)
                if isinstance(alias_value, str) and alias_value.strip():
                    arguments["message"] = alias_value
                    changed = True
                    break
        if "message" in arguments:
            for alias in ("prompt", "input"):
                if alias in arguments:
                    arguments.pop(alias, None)
                    changed = True
        if "name" in arguments:
            name_value = arguments.get("name")
            if "nickname" not in arguments and isinstance(name_value, str) and name_value.strip() and name_value not in MULTI_AGENT_TOOL_NAMES:
                arguments["nickname"] = name_value
                changed = True
            arguments.pop("name", None)
            changed = True
        if "agent_type" in arguments:
            arguments.pop("agent_type", None)
            changed = True
        if "fork_context" not in arguments:
            arguments["fork_context"] = False
            changed = True

    for key in ("fork_context", "interrupt"):
        item = arguments.get(key)
        if isinstance(item, str) and item.lower() in {"true", "false"}:
            arguments[key] = item.lower() == "true"
            changed = True

    if "targets" in arguments:
        coerced, item_changed = coerce_targets(arguments["targets"])
        if item_changed:
            arguments["targets"] = coerced
            changed = True
    if "target" in arguments:
        coerced, item_changed = coerce_target(arguments["target"])
        if item_changed:
            arguments["target"] = coerced
            changed = True
    if resolved_tool_name == "close_agent" and "target" not in arguments and "targets" in arguments:
        target_value = arguments.get("targets")
        if isinstance(target_value, list) and target_value:
            arguments["target"] = target_value[0]
            arguments.pop("targets", None)
            changed = True
    if resolved_tool_name == "wait_agent" and "targets" not in arguments and "target" in arguments:
        target_value = arguments.get("target")
        arguments["targets"] = target_value if isinstance(target_value, list) else [target_value]
        arguments.pop("target", None)
        changed = True
    if "timeout_ms" in arguments:
        coerced, item_changed = coerce_number(arguments["timeout_ms"])
        if item_changed:
            arguments["timeout_ms"] = coerced
            changed = True

    if not changed:
        return value, resolved_tool_name, False
    return dump_arguments_like(value, arguments), resolved_tool_name, True
