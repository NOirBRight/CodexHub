"""Collaboration V1 repair: names, forbidden fields, and flattened-name identity.

This module must not import the V2 adapter. V1 repair cannot execute V2 paths.
"""

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


def matches_flattened_native_identity(
    item: Mapping[str, Any],
    original_name: str | None,
) -> bool:
    """Return whether a namespaced call matches a flattened V1-style native name."""

    namespace = item.get("namespace")
    name = item.get("name")
    return (
        isinstance(namespace, str)
        and namespace
        and isinstance(name, str)
        and name
        and f"{namespace}__{name}" == original_name
    )


def is_legacy_flattened_spawn(
    item: Mapping[str, Any],
    original_name: str | None,
) -> bool:
    """Recognize the flattened V1 spawn item emitted without an ``added`` event."""

    return (
        original_name == f"{V1_FLAT_PREFIX}spawn_agent"
        and item.get("type") == "function_call"
        and item.get("namespace") == V1_NAMESPACE
        and item.get("name") == "spawn_agent"
    )
