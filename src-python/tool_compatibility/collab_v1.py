"""Collaboration V1 names, forbidden fields, and field validation."""

from __future__ import annotations

from typing import Any, Mapping

from .contracts import ToolCompatibilityError


V1_NAMES = frozenset(
    {"spawn_agent", "send_input", "wait_agent", "close_agent", "resume_agent"}
)
V1_FORBIDDEN = frozenset({"task_path", "continuation_id", "task_name", "fork_turns"})
V1_NAMESPACE = "multi_agent_v1"
V1_FLAT_PREFIX = "multi_agent_v1__"


def validate_v1_fields(fields: Mapping[str, Any]) -> None:
    if V1_FORBIDDEN.intersection(fields):
        raise ToolCompatibilityError("tool_compatibility_boundary", "mixed_v1_v2_fields")


def is_opaque_v1_history_item(item: Mapping[str, Any]) -> bool:
    namespace = item.get("namespace")
    name = item.get("name")
    return (
        (namespace == V1_NAMESPACE and name in V1_NAMES)
        or (
            namespace is None
            and isinstance(name, str)
            and name.startswith(V1_FLAT_PREFIX)
            and name.removeprefix(V1_FLAT_PREFIX) in V1_NAMES
        )
    )
