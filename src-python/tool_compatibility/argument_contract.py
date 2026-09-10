"""Shared request/response argument-boundary helpers."""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from codex_semantic_adapter import strict_json_object

from .collab_v1 import V1_NAMESPACE, normalize_v1_arguments, validate_v1_arguments
from .collab_v2 import V2_NAMESPACE, normalize_v2_native_arguments, validate_v2_native_arguments
from .collab_v1 import validate_v1_fields
from .collab_v2 import validate_v2_fields
from .contracts import ToolCompatibilityEntry, ToolCompatibilityError


def validate_version_fields(
    item: Mapping[str, Any],
    record: Any,
    *,
    skip_arguments: bool = False,
) -> None:
    """Validate protocol-specific fields without normalizing wire arguments."""
    version = getattr(record, "version", None)
    if version == "v1":
        validate_v1_fields(item)
    elif version == "v2":
        validate_v2_fields(item)
    # Paired client parse-error history is retained verbatim.  It is not a new
    # executable call, but the top-level version fields above remain trusted.
    if skip_arguments:
        return
    arguments = item.get("arguments")
    if arguments in (None, ""):
        return
    parsed = strict_json_object(arguments)
    if parsed is None:
        raise ToolCompatibilityError("tool_compatibility_boundary", "malformed_arguments")
    if version == "v1":
        validate_v1_fields(parsed)
    elif version == "v2":
        validate_v2_fields(parsed)


def child_name_for_entry(entry: ToolCompatibilityEntry, name: Any) -> Any:
    if not isinstance(name, str) or name in entry.child_names:
        return name
    return next(
        (child for child in entry.child_names if name == f"{entry.namespace}__{child}"),
        name,
    )


def normalize_namespace_arguments(
    arguments: Any,
    *,
    name: Any,
    version: str | None,
    namespace: str | None,
    child_name: Any = None,
    surface: str,
    preserve_failed: bool = False,
) -> tuple[Any, bool]:
    if (
        preserve_failed
        or not isinstance(arguments, str)
        or not arguments
        or namespace not in {V1_NAMESPACE, V2_NAMESPACE}
        or version not in {"v1", "v2"}
    ):
        return arguments, False
    normalizer = normalize_v1_arguments if version == "v1" else normalize_v2_native_arguments
    return normalizer(
        {"name": name if child_name is None else child_name, "arguments": arguments},
        surface=surface,
    )


def normalize_v1_stream_arguments(
    arguments: str,
    *,
    name: Any,
    adapted: bool,
    surface: str = "stream",
) -> str:
    """Validate a complete V1 stream call and normalize only adapted calls."""
    item = {"name": name, "arguments": arguments}
    if adapted:
        return normalize_v1_arguments(item, surface=surface)[0]
    validate_v1_arguments(item, surface=surface)
    return arguments


def validate_versioned_item(
    item: Mapping[str, Any],
    record: Any,
    *,
    surface: str,
    skip_arguments: bool = False,
) -> None:
    """Apply version fields and native argument checks at one boundary."""
    validate_version_fields(item, record, skip_arguments=skip_arguments)
    if skip_arguments or getattr(record, "family", None) != "namespace":
        return
    version = getattr(record, "version", None)
    namespace = getattr(record, "namespace", None)
    if version == "v2" and namespace == V2_NAMESPACE and item.get("arguments") not in (None, ""):
        validate_v2_native_arguments(item, surface=surface)
    elif version == "v1" and namespace == V1_NAMESPACE and surface != "history" and item.get("arguments") not in (None, ""):
        validate_v1_arguments(item, surface=surface)


def failed_argument_call_ids(items: Iterable[Any]) -> set[str]:
    """Return only call IDs backed by an earlier, real failed call.

    A result-looking item is not evidence by itself.  In particular, an
    output placed before its call (or an output for an unknown call) must not
    grant the later call the ``preserve_failed_arguments`` exception; doing so
    would let malformed new arguments bypass the normalizer.
    """
    seen_calls: set[str] = set()
    failed: set[str] = set()
    for item in items:
        if not isinstance(item, Mapping):
            continue
        item_type = item.get("type")
        call_id = item.get("call_id")
        if item_type in {"function_call", "custom_tool_call"}:
            if isinstance(call_id, str) and call_id:
                seen_calls.add(call_id)
            continue
        if (
            item_type in {"function_call_output", "custom_tool_call_output"}
            and isinstance(call_id, str)
            and call_id in seen_calls
            and isinstance(item.get("output"), str)
            and item["output"].startswith("failed to parse function arguments:")
        ):
            failed.add(call_id)
    return failed


__all__ = [
    "child_name_for_entry",
    "failed_argument_call_ids",
    "normalize_namespace_arguments",
    "normalize_v1_stream_arguments",
    "validate_versioned_item",
    "validate_version_fields",
]
