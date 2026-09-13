"""Wire-only adapters for Codex Collaboration V1.

The Gateway preserves and translates client-owned Collaboration calls.  It
never derives a workflow from their history, chooses the next tool, or turns a
child completion into a parent terminal state.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from tool_surface_adapter import MULTI_AGENT_DISCOVERY_TOOLS


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
