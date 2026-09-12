"""Wire-only adapters for Codex Collaboration V1.

The Gateway preserves and translates client-owned Collaboration calls.  It
never derives a workflow from their history, chooses the next tool, or turns a
child completion into a parent terminal state.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import collaboration_adapter as _collaboration_adapter
import tool_surface_adapter as _tool_surface

from tool_surface_adapter import MULTI_AGENT_DISCOVERY_TOOLS


def _is_multi_agent_tool_schema(value: Any) -> bool:
    return _tool_surface.is_multi_agent_tool_schema(value)


def _multi_agent_function_call_name(item: Mapping[str, Any]) -> str | None:
    """Classify only a genuine V1 namespace call or its registered alias."""
    return _tool_surface.multi_agent_function_call_name(item)


def _node_repl_function_call_name(item: Mapping[str, Any]) -> str | None:
    return _tool_surface.node_repl_function_call_name(item)


def _bounded_empty_tool_search_terminal_calls(value: Any) -> dict[str, tuple[str, int]]:
    return _tool_surface.bounded_empty_tool_search_terminal_calls(value)


def _terminalize_bounded_empty_tool_search_misses(
    payload: dict[str, Any],
    terminal_calls: Mapping[str, tuple[str, int]],
) -> bool:
    return _tool_surface.terminalize_bounded_empty_tool_search_misses(payload, terminal_calls)


def _restrict_bounded_tool_search_queries(
    payload: dict[str, Any], bounded_queries: set[str]
) -> bool:
    return _tool_surface.restrict_bounded_tool_search_queries(payload, bounded_queries)


def _tool_search_query_digest(query: str) -> bytes:
    return _tool_surface.tool_search_query_digest(query)


def _suppress_bounded_tool_search_calls(
    value: Any, event_context: Mapping[str, Any] | None
) -> tuple[Any, bool]:
    return _tool_surface.suppress_bounded_tool_search_calls(value, event_context)


def _is_multi_agent_discovery_arguments(arguments: Mapping[str, Any] | None) -> bool:
    return _tool_surface.is_multi_agent_discovery_arguments(arguments)


def _worker_caller_carrier_supported(event_context: Mapping[str, Any] | None) -> bool:
    return _collaboration_adapter.worker_caller_carrier_supported(event_context)


def _validate_external_worker_selectors(
    value: Any,
    event_context: Mapping[str, Any] | None,
    *,
    surface: str,
) -> None:
    _collaboration_adapter.validate_external_worker_selectors(
        value, event_context, surface=surface
    )


def _apply_external_worker_response_contract(
    value: Any,
    event_context: Mapping[str, Any] | None,
    *,
    surface: str,
    validate_selectors: bool = True,
    attach_sidecars: bool = True,
    capture_stream_event: bool = True,
) -> tuple[Any, bool]:
    return _collaboration_adapter.apply_external_worker_response_contract(
        value,
        event_context,
        surface=surface,
        validate_selectors=validate_selectors,
        attach_sidecars=attach_sidecars,
        capture_stream_event=capture_stream_event,
    )


def _validate_worker_binding_history(payload: Mapping[str, Any]) -> bool:
    return _collaboration_adapter.validate_worker_binding_history(payload)


def _developer_message(lines: list[str]) -> dict[str, str]:
    return {"type": "message", "role": "developer", "content": "\n".join(lines)}


def _append_value(lines: list[str], label: str, value: Any) -> None:
    if value is None:
        return
    if isinstance(value, str):
        text = value.strip()
    else:
        try:
            text = json.dumps(value, ensure_ascii=True, separators=(",", ":"))
        except (TypeError, ValueError):
            text = str(value).strip()
    if text:
        lines.extend((f"{label}:", text))


def _compatible_multi_agent_call_message(
    item: Mapping[str, Any], tool_name: str
) -> dict[str, str]:
    """Represent a V1 call as neutral, read-only text for text-only routes."""
    lines = [f"Read-only Codex multi_agent_v1.{tool_name} call transcript"]
    _append_value(lines, "call_id", item.get("call_id"))
    _append_value(lines, "arguments", item.get("arguments"))
    return _developer_message(lines)


def _compatible_multi_agent_output_message(
    item: Mapping[str, Any],
    tool_name: str,
    arguments: Mapping[str, Any] | None,
) -> dict[str, str]:
    """Represent a result without prescribing any parent action."""
    _ = arguments
    lines = [f"Read-only Codex multi_agent_v1.{tool_name} result transcript"]
    _append_value(lines, "call_id", item.get("call_id"))
    _append_value(lines, "output", item.get("output"))
    return _developer_message(lines)


def _compatible_node_repl_call_message(item: Mapping[str, Any]) -> dict[str, str]:
    lines = ["Read-only Codex mcp__node_repl.js call transcript"]
    _append_value(lines, "call_id", item.get("call_id"))
    _append_value(lines, "arguments", item.get("arguments"))
    return _developer_message(lines)


def _compatible_node_repl_output_message(
    item: Mapping[str, Any], *, enforce_final: bool = False
) -> dict[str, str]:
    """Keep the legacy argument for callers, but it no longer changes behavior."""
    _ = enforce_final
    lines = ["Read-only Codex mcp__node_repl.js result transcript"]
    _append_value(lines, "call_id", item.get("call_id"))
    _append_value(lines, "output", item.get("output"))
    return _developer_message(lines)


def _has_multi_agent_discovery_tools(value: Any) -> bool:
    """Recognize an actual namespace declaration, never a bare tool or text."""
    return bool(
        isinstance(value, list)
        and any(
            isinstance(item, Mapping)
            and item.get("type") == "namespace"
            and item.get("name") == "multi_agent_v1"
            for item in value
        )
    )


def _multi_agent_discovery_output_item(item: Mapping[str, Any]) -> dict[str, Any]:
    """Fill an explicitly client-owned empty discovery response with its schema."""
    rewritten = dict(item)
    rewritten["tools"] = MULTI_AGENT_DISCOVERY_TOOLS
    rewritten.setdefault("status", "completed")
    rewritten.setdefault("execution", "client")
    return rewritten
