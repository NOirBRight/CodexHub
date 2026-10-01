"""Custom/freeform codec helpers shared by flat and namespace declarations."""
from __future__ import annotations

from dataclasses import replace
import json
from typing import Any, Mapping

from .argument_contract import child_name_for_entry
from .contracts import (
    CUSTOM_INPUT_KEY, ToolCompatibilityEntry, copy_mapping as _copy_mapping,
    freeze as _freeze, thaw as _thaw,
)
from .dispositions import CUSTOM_FREEFORM, NAMESPACE, namespace_details as _namespace_details

def custom_child_entry(entry: ToolCompatibilityEntry, name: str) -> ToolCompatibilityEntry:
    if entry.family != NAMESPACE:
        return entry
    child_name = child_name_for_entry(entry, name)
    children = _namespace_details(_thaw(entry.declaration))[1]
    for index, child in enumerate(children):
        if child.get("name") == child_name and child.get("type") == "custom":
            return replace(
                entry, family=CUSTOM_FREEFORM, declaration=_freeze(_copy_mapping(child)),
                aliases=(entry.aliases[index],) if entry.aliases else (), child_names=(),
            )
    return entry

def custom_function_declaration(tool: Mapping[str, Any], alias: str) -> dict[str, Any]:
    description = tool.get("description", "")
    return {
        "type": "function", "name": alias,
        "description": (description if isinstance(description, str) else "")
        + f"\nTransport adaptation: put the exact raw custom input in the JSON string property {CUSTOM_INPUT_KEY}. "
        + "Do not execute or alter the input in Gateway. Original format: "
        + json.dumps(_thaw(tool.get("format")), ensure_ascii=True, separators=(",", ":")),
        "parameters": {
            "type": "object", "properties": {CUSTOM_INPUT_KEY: {"type": "string"}},
            "required": [CUSTOM_INPUT_KEY], "additionalProperties": False,
        },
    }
