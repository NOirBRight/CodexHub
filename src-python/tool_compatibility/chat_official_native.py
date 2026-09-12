"""Chat → official native-tool expand/collapse for hosted, custom, and tool_search.

Chat clients declare either a function alias (``__codexhub_hosted_*`` /
``__codexhub_custom_*`` / ``__codexhub_search_*``) or a known non-function
Responses shape.  A user function that merely shares a hosted kind name stays a
plain function.  Encrypted fields and unknown aliases fail closed.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from route_primitives import APPLY_PATCH_FUNCTION_NAME

from .contracts import (
    CUSTOM_INPUT_KEY,
    CUSTOM_OUTPUT_KEY,
    TOOL_SEARCH_INPUT_KEY,
    TOOL_SEARCH_OUTPUT_KEY,
    ToolCompatibilityError,
    copy_mapping as _copy_mapping,
    dump_envelope as _dump_envelope,
    json_object_with_key as _json_object_with_key,
)
from .dispositions import CHAT_OFFICIAL_HOSTED_KINDS, hosted_event_spec_for_declaration_kind, name_of
from .registry import RequestScopedToolAliasRegistry

CHAT_OFFICIAL_NATIVE_NAME_MAP_KEY = "_chat_official_native_name_map"
KNOWN_CUSTOM_NAMES = (APPLY_PATCH_FUNCTION_NAME,)
_HOSTED_CALL_META = frozenset({"id", "type", "status", "call_id", "name"})
_INCOMPLETE_HOSTED_CALLS = frozenset({"computer_use_preview_call", "local_shell_call"})


def _hosted_alias_to_kind() -> dict[str, str]:
    registry = RequestScopedToolAliasRegistry(request_token="request")
    mapping: dict[str, str] = {}
    for kind in sorted(CHAT_OFFICIAL_HOSTED_KINDS):
        alias = registry.allocate_hosted(declaration_index=0, kind=kind)
        mapping[alias] = kind
    return mapping


def _custom_alias_to_name() -> dict[str, str]:
    registry = RequestScopedToolAliasRegistry(request_token="request")
    mapping: dict[str, str] = {}
    for name in KNOWN_CUSTOM_NAMES:
        alias = registry.allocate_custom(
            declaration_index=0,
            original_name=name,
            version=None,
        )
        mapping[alias] = name
    return mapping


def _tool_search_alias() -> str:
    registry = RequestScopedToolAliasRegistry(request_token="request")
    return registry.allocate_tool_search(declaration_index=0)


_ENCRYPTED_PAYLOAD_KEYS = frozenset(
    {
        "encrypted_content",
        "encrypted_function_args",
        "encrypted_input",
        "encrypted_output",
    }
)


def _reject_encrypted_fields(value: Mapping[str, Any], *, surface: str) -> None:
    for key in _ENCRYPTED_PAYLOAD_KEYS:
        child = value.get(key)
        if child not in (None, "", [], False):
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "encrypted_native_tool_unavailable",
                surface=surface,
            )


def _parse_object(value: Any, *, surface: str) -> dict[str, Any]:
    if value in (None, ""):
        return {}
    if isinstance(value, Mapping):
        parsed = dict(value)
    elif isinstance(value, str):
        try:
            loaded = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "malformed_envelope",
                surface=surface,
            ) from exc
        if loaded in (None, ""):
            return {}
        if not isinstance(loaded, dict):
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "malformed_envelope",
                surface=surface,
            )
        parsed = loaded
    else:
        raise ToolCompatibilityError(
            "tool_compatibility_boundary",
            "malformed_envelope",
            surface=surface,
        )
    _reject_encrypted_fields(parsed, surface=surface)
    return parsed


def _native_custom_declaration(name: str, source: Mapping[str, Any] | None = None) -> dict[str, Any]:
    tool: dict[str, Any] = {
        "type": "custom",
        "name": name,
        "format": {"type": "text"},
    }
    if isinstance(source, Mapping):
        description = source.get("description")
        nested = source.get("function")
        if not isinstance(description, str) and isinstance(nested, Mapping):
            description = nested.get("description")
        if isinstance(description, str) and description:
            tool["description"] = description
        fmt = source.get("format")
        if isinstance(fmt, Mapping):
            tool["format"] = _copy_mapping(fmt)
    return tool


def _native_tool_search_declaration(source: Mapping[str, Any] | None = None) -> dict[str, Any]:
    tool: dict[str, Any] = {"type": "tool_search", "execution": "client"}
    if isinstance(source, Mapping) and source.get("type") == "tool_search":
        return _copy_mapping(source)
    if isinstance(source, Mapping):
        description = source.get("description")
        nested = source.get("function")
        if not isinstance(description, str) and isinstance(nested, Mapping):
            description = nested.get("description")
        if isinstance(description, str) and description:
            tool["description"] = description
        parameters = source.get("parameters")
        if not isinstance(parameters, Mapping) and isinstance(nested, Mapping):
            parameters = nested.get("parameters")
        if isinstance(parameters, Mapping):
            tool["parameters"] = _copy_mapping(parameters)
    return tool


def _native_hosted_declaration(kind: str, source: Mapping[str, Any] | None = None) -> dict[str, Any]:
    if isinstance(source, Mapping) and source.get("type") == kind:
        return _copy_mapping(source)
    return {"type": kind}


def _expand_native_tool_choice(
    payload: dict[str, Any],
    *,
    hosted_chat_names: Mapping[str, str],
    custom_chat_names: Mapping[str, str],
    search_chat_name: str,
    hosted_aliases: Mapping[str, str],
    custom_aliases: Mapping[str, str],
    search_alias: str,
) -> None:
    choice = payload.get("tool_choice")
    if choice in (None, "auto", "none", "required"):
        return
    name: str | None = None
    if isinstance(choice, Mapping) and choice.get("type") == "function":
        nested = choice.get("name")
        name = nested if isinstance(nested, str) else None
        function = choice.get("function")
        if name is None and isinstance(function, Mapping):
            nested = function.get("name")
            name = nested if isinstance(nested, str) else None
    if not isinstance(name, str) or not name:
        return
    if name in hosted_aliases or name in hosted_chat_names.values() or name in hosted_chat_names:
        kind = hosted_aliases.get(name) or next(
            (kind for kind, chat_name in hosted_chat_names.items() if chat_name == name or kind == name),
            name if name in hosted_chat_names else None,
        )
        if kind is None:
            return
        payload["tool_choice"] = {"type": kind}
        return
    if name in custom_aliases or name in custom_chat_names.values() or name in custom_chat_names:
        native = custom_aliases.get(name) or next(
            (native for native, chat_name in custom_chat_names.items() if chat_name == name or native == name),
            name if name in custom_chat_names else None,
        )
        if native is None:
            return
        payload["tool_choice"] = {"type": "custom", "name": native}
        return
    if name in {search_alias, search_chat_name, "tool_search"} and search_chat_name:
        payload["tool_choice"] = {"type": "tool_search"}



def expand_chat_native_tools_for_official(
    payload: dict[str, Any],
    event_context: Mapping[str, Any] | None = None,
) -> bool:
    """Restore Chat aliases / known native types to official Responses declarations."""

    tools = payload.get("tools")
    if not isinstance(tools, list):
        tools = []
    hosted_aliases = _hosted_alias_to_kind()
    custom_aliases = _custom_alias_to_name()
    search_alias = _tool_search_alias()
    hosted_sources: dict[str, Mapping[str, Any]] = {}
    custom_sources: dict[str, Mapping[str, Any]] = {}
    search_source: Mapping[str, Any] | None = None
    remaining: list[Any] = []
    chat_names: dict[str, dict[str, str] | str] = {
        "hosted": {},
        "custom": {},
        "tool_search": "",
        "hosted_event": {},
    }
    saw_native = False

    for tool in tools:
        if not isinstance(tool, Mapping):
            remaining.append(tool)
            continue
        tool_type = tool.get("type")
        if tool_type == "namespace":
            remaining.append(tool)
            continue
        _reject_encrypted_fields(tool, surface="request")
        if tool_type in CHAT_OFFICIAL_HOSTED_KINDS:
            kind = str(tool_type)
            if kind in hosted_sources:
                raise ToolCompatibilityError(
                    "tool_compatibility_boundary",
                    "hosted_kind_duplicate",
                    surface="request",
                )
            hosted_sources[kind] = tool
            chat_names["hosted"][kind] = kind
            saw_native = True
            continue
        if tool_type == "custom":
            name = name_of(tool)
            if not isinstance(name, str) or not name:
                raise ToolCompatibilityError(
                    "tool_compatibility_boundary",
                    "malformed_declaration",
                    surface="request",
                )
            if name in custom_sources:
                raise ToolCompatibilityError(
                    "tool_compatibility_boundary",
                    "custom_name_duplicate",
                    surface="request",
                )
            custom_sources[name] = tool
            chat_names["custom"][name] = name
            saw_native = True
            continue
        if tool_type == "tool_search":
            if tool.get("execution") != "client":
                raise ToolCompatibilityError(
                    "tool_compatibility_boundary",
                    "invalid_tool_search_execution",
                    surface="request",
                )
            if search_source is not None:
                raise ToolCompatibilityError(
                    "tool_compatibility_boundary",
                    "tool_search_duplicate",
                    surface="request",
                )
            search_source = tool
            chat_names["tool_search"] = "tool_search"
            saw_native = True
            continue
        name = name_of(tool)
        if isinstance(name, str) and name in hosted_aliases:
            kind = hosted_aliases[name]
            if kind in hosted_sources:
                raise ToolCompatibilityError(
                    "tool_compatibility_boundary",
                    "hosted_kind_duplicate",
                    surface="request",
                )
            hosted_sources[kind] = tool
            chat_names["hosted"][kind] = name
            saw_native = True
            continue
        if isinstance(name, str) and name in custom_aliases:
            native_name = custom_aliases[name]
            if native_name in custom_sources:
                raise ToolCompatibilityError(
                    "tool_compatibility_boundary",
                    "custom_name_duplicate",
                    surface="request",
                )
            custom_sources[native_name] = tool
            chat_names["custom"][native_name] = name
            saw_native = True
            continue
        if name == search_alias:
            if search_source is not None:
                raise ToolCompatibilityError(
                    "tool_compatibility_boundary",
                    "tool_search_duplicate",
                    surface="request",
                )
            search_source = tool
            chat_names["tool_search"] = name
            saw_native = True
            continue
        if RequestScopedToolAliasRegistry.looks_like_alias(name):
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "unknown_alias",
                surface="request",
            )
        remaining.append(tool)

    if not saw_native:
        _reject_undeclared_native_history(
            payload,
            hosted_aliases=hosted_aliases,
            custom_aliases=custom_aliases,
            search_alias=search_alias,
        )
        return False

    event_owners: dict[str, str] = {}
    for kind in hosted_sources:
        spec = hosted_event_spec_for_declaration_kind(kind)
        if spec is None:
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "incomplete_hosted_lifecycle",
                surface="request",
            )
        event_kind, _stages = spec
        if event_kind in event_owners:
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "hosted_kind_ambiguous",
                surface="request",
            )
        event_owners[event_kind] = kind
        chat_names["hosted_event"][event_kind] = chat_names["hosted"][kind]

    native_tools: list[dict[str, Any]] = []
    for kind, source in hosted_sources.items():
        native_tools.append(_native_hosted_declaration(kind, source))
    for name, source in custom_sources.items():
        native_tools.append(_native_custom_declaration(name, source))
    if search_source is not None:
        native_tools.append(_native_tool_search_declaration(search_source))
    payload["tools"] = [*native_tools, *remaining]
    _expand_native_tool_choice(
        payload,
        hosted_chat_names=chat_names["hosted"],
        custom_chat_names=chat_names["custom"],
        search_chat_name=chat_names["tool_search"] if isinstance(chat_names["tool_search"], str) else "",
        hosted_aliases=hosted_aliases,
        custom_aliases=custom_aliases,
        search_alias=search_alias,
    )
    if isinstance(event_context, dict):
        event_context[CHAT_OFFICIAL_NATIVE_NAME_MAP_KEY] = chat_names

    owners = _expand_native_history(
        payload,
        hosted_aliases=hosted_aliases,
        custom_aliases=custom_aliases,
        search_alias=search_alias,
        hosted_chat_names=chat_names["hosted"],
        custom_chat_names=chat_names["custom"],
        search_chat_name=chat_names["tool_search"] if isinstance(chat_names["tool_search"], str) else "",
    )
    if isinstance(event_context, dict) and owners:
        event_context["_chat_official_native_call_owners"] = owners
    return True


def _reject_undeclared_native_history(
    payload: Mapping[str, Any],
    *,
    hosted_aliases: Mapping[str, str],
    custom_aliases: Mapping[str, str],
    search_alias: str,
) -> None:
    input_items = payload.get("input")
    if not isinstance(input_items, list):
        return
    for item in input_items:
        if not isinstance(item, Mapping) or item.get("type") != "function_call":
            continue
        name = item.get("name")
        if name in hosted_aliases or name in custom_aliases or name == search_alias:
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "unknown_native_identity",
                surface="history",
            )
        if RequestScopedToolAliasRegistry.looks_like_alias(name):
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "unknown_alias",
                surface="history",
            )


def _expand_native_history(
    payload: dict[str, Any],
    *,
    hosted_aliases: Mapping[str, str],
    custom_aliases: Mapping[str, str],
    search_alias: str,
    hosted_chat_names: Mapping[str, str],
    custom_chat_names: Mapping[str, str],
    search_chat_name: str,
) -> dict[str, tuple[str, str]]:
    input_items = payload.get("input")
    if not isinstance(input_items, list):
        return {}
    hosted_name_to_kind = {kind: kind for kind in hosted_chat_names}
    hosted_name_to_kind.update({chat_name: kind for kind, chat_name in hosted_chat_names.items()})
    hosted_name_to_kind.update(hosted_aliases)
    custom_name_to_native = {native: native for native in custom_chat_names}
    custom_name_to_native.update({chat_name: native for native, chat_name in custom_chat_names.items()})
    custom_name_to_native.update(custom_aliases)
    owners: dict[str, tuple[str, str]] = {}
    rewritten: list[Any] = []
    for item in input_items:
        if not isinstance(item, Mapping):
            rewritten.append(item)
            continue
        _reject_encrypted_fields(item, surface="history")
        item_type = item.get("type")
        if item_type == "function_call":
            next_item, owner = _expand_native_call(
                item,
                hosted_name_to_kind=hosted_name_to_kind,
                custom_name_to_native=custom_name_to_native,
                search_chat_name=search_chat_name,
                search_alias=search_alias,
            )
            rewritten.append(next_item)
            call_id = next_item.get("call_id")
            if owner is not None and isinstance(call_id, str) and call_id:
                owners[call_id] = owner
            continue
        if item_type == "function_call_output":
            rewritten.append(_expand_native_result(item, owners))
            continue
        rewritten.append(_copy_mapping(item))
    payload["input"] = rewritten
    return owners


def _expand_native_call(
    item: Mapping[str, Any],
    *,
    hosted_name_to_kind: Mapping[str, str],
    custom_name_to_native: Mapping[str, str],
    search_chat_name: str,
    search_alias: str,
) -> tuple[dict[str, Any], tuple[str, str] | None]:
    next_item = _copy_mapping(item)
    name = next_item.get("name")
    if isinstance(name, str) and name in hosted_name_to_kind:
        kind = hosted_name_to_kind[name]
        spec = hosted_event_spec_for_declaration_kind(kind)
        if spec is None:
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "incomplete_hosted_lifecycle",
                surface="history",
            )
        event_kind, _stages = spec
        fields = _parse_object(next_item.get("arguments"), surface="history")
        if "type" in fields:
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "malformed_envelope",
                surface="history",
            )
        expanded = {
            "type": event_kind,
            "call_id": next_item.get("call_id"),
        }
        if isinstance(next_item.get("id"), str) and next_item["id"]:
            expanded["id"] = next_item["id"]
        if isinstance(next_item.get("status"), str):
            expanded["status"] = next_item["status"]
        expanded.update(fields)
        return expanded, ("hosted", kind)
    if isinstance(name, str) and name in custom_name_to_native:
        native_name = custom_name_to_native[name]
        envelope = _json_object_with_key(next_item.get("arguments"), CUSTOM_INPUT_KEY)
        expanded = {
            "type": "custom_tool_call",
            "call_id": next_item.get("call_id"),
            "name": native_name,
            "input": envelope[CUSTOM_INPUT_KEY],
        }
        if isinstance(next_item.get("id"), str) and next_item["id"]:
            expanded["id"] = next_item["id"]
        if isinstance(next_item.get("status"), str):
            expanded["status"] = next_item["status"]
        return expanded, ("custom", native_name)
    if name in {search_alias, search_chat_name} and search_chat_name:
        envelope = _json_object_with_key(next_item.get("arguments"), TOOL_SEARCH_INPUT_KEY)
        expanded = {
            "type": "tool_search_call",
            "call_id": next_item.get("call_id"),
            "execution": "client",
            "arguments": envelope[TOOL_SEARCH_INPUT_KEY],
        }
        if isinstance(next_item.get("id"), str) and next_item["id"]:
            expanded["id"] = next_item["id"]
        if isinstance(next_item.get("status"), str):
            expanded["status"] = next_item["status"]
        return expanded, ("tool_search", "tool_search")
    if RequestScopedToolAliasRegistry.looks_like_alias(name):
        raise ToolCompatibilityError(
            "tool_compatibility_boundary",
            "unknown_alias",
            surface="history",
        )
    return next_item, None


def _expand_native_result(item: Mapping[str, Any], owners: Mapping[str, tuple[str, str]]) -> dict[str, Any]:
    next_item = _copy_mapping(item)
    call_id = next_item.get("call_id")
    owner = owners.get(call_id) if isinstance(call_id, str) else None
    if owner is None:
        return next_item
    family, _native = owner
    if family == "custom":
        envelope = _json_object_with_key(next_item.get("output"), CUSTOM_OUTPUT_KEY)
        next_item["type"] = "custom_tool_call_output"
        next_item["output"] = envelope[CUSTOM_OUTPUT_KEY]
        return next_item
    if family == "tool_search":
        envelope = _json_object_with_key(next_item.get("output"), TOOL_SEARCH_OUTPUT_KEY)
        payload = envelope[TOOL_SEARCH_OUTPUT_KEY]
        if not isinstance(payload, Mapping):
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "invalid_envelope",
                surface="history",
            )
        expanded = {
            "type": "tool_search_output",
            "call_id": call_id,
            "execution": "client",
        }
        if isinstance(next_item.get("id"), str) and next_item["id"]:
            expanded["id"] = next_item["id"]
        for key, value in payload.items():
            if key in {"type", "execution", "id", "item_id", "call_id"}:
                continue
            expanded[key] = value
        return expanded
    return next_item


def collapse_official_native_tools_for_chat(
    payload: dict[str, Any],
    event_context: Mapping[str, Any] | None,
) -> bool:
    """Rewrite official hosted/custom/tool_search items into Chat function calls."""

    name_map = (event_context or {}).get(CHAT_OFFICIAL_NATIVE_NAME_MAP_KEY)
    if not isinstance(name_map, Mapping) or not name_map:
        return False
    output = payload.get("output")
    if not isinstance(output, list):
        return False
    hosted_event = name_map.get("hosted_event")
    hosted_event = hosted_event if isinstance(hosted_event, Mapping) else {}
    custom_names = name_map.get("custom")
    custom_names = custom_names if isinstance(custom_names, Mapping) else {}
    search_name = name_map.get("tool_search")
    changed = False
    for index, item in enumerate(list(output)):
        if not isinstance(item, dict):
            continue
        _reject_encrypted_fields(item, surface="response")
        item_type = item.get("type")
        if item_type in _INCOMPLETE_HOSTED_CALLS:
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "incomplete_hosted_lifecycle",
                surface="response",
            )
        if item_type in hosted_event:
            chat_name = hosted_event.get(item_type)
            if not isinstance(chat_name, str) or not chat_name:
                raise ToolCompatibilityError(
                    "tool_compatibility_boundary",
                    "unknown_native_identity",
                    surface="response",
                )
            output[index] = _collapse_hosted_call(item, chat_name)
            changed = True
            continue
        if item_type.endswith("_call") and item_type not in {
            "function_call",
            "custom_tool_call",
            "tool_search_call",
        }:
            raise ToolCompatibilityError(
                "tool_compatibility_boundary",
                "unknown_hosted_kind",
                surface="response",
            )
        if item_type == "custom_tool_call":
            native_name = item.get("name")
            chat_name = custom_names.get(native_name, native_name)
            if not isinstance(chat_name, str) or not chat_name:
                raise ToolCompatibilityError(
                    "tool_compatibility_boundary",
                    "unknown_native_identity",
                    surface="response",
                )
            output[index] = _collapse_custom_call(item, chat_name)
            changed = True
            continue
        if item_type == "tool_search_call":
            chat_name = search_name if isinstance(search_name, str) and search_name else "tool_search"
            output[index] = _collapse_tool_search_call(item, chat_name)
            changed = True
            continue
        if item_type == "custom_tool_call_output":
            output[index] = _collapse_custom_result(item)
            changed = True
            continue
        if item_type == "tool_search_output":
            output[index] = _collapse_tool_search_result(item)
            changed = True
            continue
    return changed


def _call_id_of(item: Mapping[str, Any]) -> str:
    call_id = item.get("call_id")
    if isinstance(call_id, str) and call_id:
        return call_id
    item_id = item.get("id")
    if isinstance(item_id, str) and item_id:
        return item_id
    raise ToolCompatibilityError(
        "tool_compatibility_boundary",
        "missing_call_identity",
        surface="response",
    )


def _collapse_hosted_call(item: Mapping[str, Any], chat_name: str) -> dict[str, Any]:
    arguments = {
        key: value
        for key, value in item.items()
        if key not in _HOSTED_CALL_META
    }
    collapsed = {
        "type": "function_call",
        "call_id": _call_id_of(item),
        "name": chat_name,
        "arguments": json.dumps(arguments, ensure_ascii=True, separators=(",", ":")),
    }
    item_id = item.get("id")
    if isinstance(item_id, str) and item_id:
        collapsed["id"] = item_id
    status = item.get("status")
    if isinstance(status, str):
        collapsed["status"] = status
    return collapsed


def _collapse_custom_call(item: Mapping[str, Any], chat_name: str) -> dict[str, Any]:
    if "input" not in item:
        raise ToolCompatibilityError(
            "tool_compatibility_boundary",
            "malformed_envelope",
            surface="response",
        )
    collapsed = {
        "type": "function_call",
        "call_id": _call_id_of(item),
        "name": chat_name,
        "arguments": _dump_envelope(CUSTOM_INPUT_KEY, item.get("input")),
    }
    item_id = item.get("id")
    if isinstance(item_id, str) and item_id:
        collapsed["id"] = item_id
    status = item.get("status")
    if isinstance(status, str):
        collapsed["status"] = status
    return collapsed


def _collapse_tool_search_call(item: Mapping[str, Any], chat_name: str) -> dict[str, Any]:
    if item.get("execution") not in (None, "client"):
        raise ToolCompatibilityError(
            "tool_compatibility_boundary",
            "invalid_tool_search_execution",
            surface="response",
        )
    collapsed = {
        "type": "function_call",
        "call_id": _call_id_of(item),
        "name": chat_name,
        "arguments": _dump_envelope(TOOL_SEARCH_INPUT_KEY, item.get("arguments", {})),
    }
    item_id = item.get("id")
    if isinstance(item_id, str) and item_id:
        collapsed["id"] = item_id
    status = item.get("status")
    if isinstance(status, str):
        collapsed["status"] = status
    return collapsed


def _collapse_custom_result(item: Mapping[str, Any]) -> dict[str, Any]:
    if "output" not in item:
        raise ToolCompatibilityError(
            "tool_compatibility_boundary",
            "malformed_envelope",
            surface="response",
        )
    next_item = _copy_mapping(item)
    next_item["type"] = "function_call_output"
    next_item["output"] = _dump_envelope(CUSTOM_OUTPUT_KEY, item.get("output"))
    return next_item


def _collapse_tool_search_result(item: Mapping[str, Any]) -> dict[str, Any]:
    payload = {
        key: value
        for key, value in item.items()
        if key not in {"id", "type", "status", "call_id", "execution"}
    }
    next_item = {
        "type": "function_call_output",
        "call_id": _call_id_of(item),
        "output": _dump_envelope(TOOL_SEARCH_OUTPUT_KEY, payload),
    }
    item_id = item.get("id")
    if isinstance(item_id, str) and item_id:
        next_item["id"] = item_id
    return next_item
