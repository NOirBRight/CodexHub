"""Collaboration V1 repair: names, forbidden fields, and flattened-name identity.

This module must not import the V2 adapter. V1 repair cannot execute V2 paths.
"""

from __future__ import annotations

from typing import Any, Mapping

from .contracts import ToolCompatibilityEntry, ToolCompatibilityError
from .dispositions import NATIVE, NAMESPACE, PLAIN_FUNCTION


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


def flattened_plain_name(item: Mapping[str, Any]) -> str | None:
    namespace = item.get("namespace")
    child_name = item.get("name")
    if isinstance(namespace, str) and namespace and isinstance(child_name, str) and child_name:
        return f"{namespace}__{child_name}"
    return None


class CollaborationV1PlanMixin:
    """V1 flattened-name repair mixed into ``ToolCompatibilityPlan``."""

    __slots__ = ()

    def _flattened_native_plain_entry(
        self,
        item: Mapping[str, Any],
    ) -> ToolCompatibilityEntry | None:
        flattened_name = flattened_plain_name(item)
        if flattened_name is None:
            return None
        flattened_matches = [
            candidate
            for candidate in self.entries
            if candidate.family == PLAIN_FUNCTION
            and candidate.disposition == NATIVE
            and candidate.original_name == flattened_name
        ]
        if len(flattened_matches) > 1:
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "ambiguous_native_identity",
            )
        return flattened_matches[0] if flattened_matches else None

    def _reject_unknown_flattened_identity(
        self,
        item: Mapping[str, Any],
        *,
        surface: str,
    ) -> bool:
        """Return True when a flattened native plain owner already resolved the item."""

        if item.get("type") != "function_call" or flattened_plain_name(item) is None:
            return False
        flattened_name = flattened_plain_name(item)
        flattened_matches = [
            entry
            for entry in self.entries
            if entry.family == PLAIN_FUNCTION
            and entry.original_name == flattened_name
        ]
        if any(entry.disposition == NATIVE for entry in flattened_matches):
            return True
        if flattened_matches:
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "unknown_native_identity",
                surface=surface,
            )
        if any(entry.family == NAMESPACE for entry in self.entries) and self._entry_for_name(
            item.get("name"),
            item.get("namespace"),
            item_type=item.get("type"),
        ) is None:
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "unknown_native_identity",
                surface=surface,
            )
        return False


def validate_plain_native_item(
    item: Mapping[str, Any],
    entry: ToolCompatibilityEntry,
    *,
    surface: str,
) -> None:
    item_type = item.get("type")
    namespace = item.get("namespace")
    plain_shape = namespace is None and item.get("name") == entry.original_name
    flattened_shape = matches_flattened_native_identity(item, entry.original_name)
    if item_type != "function_call" or not (plain_shape or flattened_shape):
        raise ToolCompatibilityError(
            "tool_compatibility_boundary",
            "unknown_native_identity",
            surface=surface,
        )
