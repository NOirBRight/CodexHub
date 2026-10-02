"""Self-contained, completed flat function history without execution authority."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from protocol_translation import UnsupportedProtocolTranslationError


def completed_flat_function_history(items: Sequence[Any]) -> frozenset[int]:
    """Return exact call/result positions safe to retain with no current tools.

    This recognizes native closed history, not a custom/namespace wire alias.
    It never declares a tool, assigns an execution owner, or repairs arguments.
    Legacy calls without typed IDs/completed status keep their existing policy.
    """
    calls: dict[str, list[int]] = {}
    results: dict[str, list[int]] = {}
    item_ids: dict[str, int] = {}
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            continue
        item_id = item.get("id")
        if isinstance(item_id, str):
            item_ids[item_id] = item_ids.get(item_id, 0) + 1
        call_id = item.get("call_id")
        if not isinstance(call_id, str) or not call_id:
            continue
        kind = item.get("type")
        if kind in {"function_call", "custom_tool_call", "tool_search_call"}:
            calls.setdefault(call_id, []).append(index)
        elif isinstance(kind, str) and (kind.endswith("_call_output") or kind == "tool_search_output"):
            results.setdefault(call_id, []).append(index)
    retained: set[int] = set()
    for call_id, positions in calls.items():
        item = items[positions[0]]
        if item.get("type") != "function_call" or "namespace" in item:
            continue
        if item.get("status") != "completed" or not isinstance(item.get("id"), str) or not item["id"]:
            continue
        paired = results.get(call_id, [])
        if not paired:
            continue
        result = items[paired[0]]
        valid = (
            len(positions) == len(paired) == 1 and positions[0] < paired[0]
            and set(item) <= {"type", "id", "call_id", "name", "arguments", "status"}
            and isinstance(item.get("name"), str) and bool(item["name"])
            and isinstance(item.get("arguments"), str)
            and item_ids[item["id"]] == 1
            and result.get("type") == "function_call_output"
            and set(result) <= {"type", "id", "call_id", "output"}
            and isinstance(result.get("output"), (str, list))
            and ("id" not in result or (
                isinstance(result["id"], str) and bool(result["id"])
                and item_ids[result["id"]] == 1
            ))
        )
        if not valid:
            raise UnsupportedProtocolTranslationError(
                "unsupported_protocol_semantics",
                "Cannot retain malformed or ambiguous completed flat function history.",
            )
        retained.update((positions[0], paired[0]))
    return frozenset(retained)
