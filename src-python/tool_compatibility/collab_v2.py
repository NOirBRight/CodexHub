"""Collaboration V2 names, forbidden fields, and field validation."""

from __future__ import annotations

from typing import Any, Mapping

from .contracts import ToolCompatibilityError


V2_NAMES = frozenset(
    {"spawn_agent", "send_message", "followup_task", "wait_agent", "interrupt_agent", "list_agents"}
)
V2_FORBIDDEN = frozenset({"agent_id", "fork_context"})
V2_NAMESPACE = "collaboration"


def validate_v2_fields(fields: Mapping[str, Any]) -> None:
    if V2_FORBIDDEN.intersection(fields):
        raise ToolCompatibilityError("tool_compatibility_boundary", "mixed_v1_v2_fields")


def is_opaque_v2_history_item(item: Mapping[str, Any]) -> bool:
    return item.get("namespace") == V2_NAMESPACE and item.get("name") in V2_NAMES
