"""Gateway compatibility application pipeline."""

from __future__ import annotations

from collections.abc import Iterable as IterableABC
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterable, Mapping, NoReturn

import hashlib
import json
import re
import uuid

import apply_patch_adapter as _apply_patch_adapter_module
import collaboration_adapter as _collaboration_adapter_module
import gateway_events as _gateway_events
import tool_history as _tool_history
import tool_surface_adapter as _tool_surface_adapter_module

from apply_patch_adapter import (
    ApplyPatchFacts,
    ThirdPartyApplyPatchStreamAdapter as _ApplyPatchStreamAdapterImpl,
)
from collaboration_adapter import (
    CollaborationFacts,
    PathBindingSigner,
    WORKER_REQUESTED_BINDING_FIELD,
)
from codex_semantic_adapter import (
    COLLABORATION_V1 as _COLLABORATION_V1,
    COLLABORATION_V2 as _COLLABORATION_V2,
    COLLABORATION_V2_NAMESPACE as _COLLABORATION_V2_NAMESPACE,
    multi_agent_discovery_arguments as _semantic_multi_agent_discovery_arguments,
    normalize_multi_agent_arguments as _semantic_normalize_multi_agent_arguments,
    normalize_tool_search_arguments as _semantic_normalize_tool_search_arguments,
)
from gateway_errors import UpstreamProtocolTranslationError
from gateway_sse import sse_line_ending as _sse_line_ending, sse_payload_bytes as _sse_payload_bytes
import protocol_translation as _protocol_translation
from protocol_translation import UnsupportedProtocolTranslationError
from runtime_tool_compatibility import (
    HostedCapabilityFacts as RuntimeHostedCapabilityFacts,
    ProtocolCapabilities as RuntimeProtocolCapabilities,
    ToolCompatibilityError as RuntimeToolCompatibilityError,
    ToolCompatibilityPlan as RuntimeToolCompatibilityPlan,
    build_tool_compatibility_plan,
)
import tool_compatibility.chat_official_native as _chat_official_native
import tool_compatibility.collab_v2 as _collab_v2
from tool_surface_adapter import (
    APPLY_PATCH_FUNCTION_NAME,
    INTERNAL_INPUT_ITEM_TYPES,
    MULTI_AGENT_DISCOVERY_TOOLS,
    MULTI_AGENT_NAMESPACE_ALIASES,
    NODE_REPL_NAMESPACE,
    TOOL_SEARCH_EMPTY_MISS_BOUND,
    TOOL_SEARCH_EXPLICIT_FUNCTION_TOOL,
    TOOL_SEARCH_UNAVAILABLE_QUERY_CLASSIFICATION,
    TOOL_SEARCH_UNAVAILABLE_STATUS,
    ToolSurfaceFacts,
)
from route_plan import (
    NATIVE_RESPONSES_TOOL_CODEC_ERROR_CODE,
    TOOL_SURFACE_STRATEGY_ERROR_CODE,
    external_native_responses_tool_codec as _external_native_responses_tool_codec,
    external_tool_protocol as _external_tool_protocol,
    external_tool_surface_strategy as _external_tool_surface_strategy,
)
from route_primitives import (
    BEHAVIOR_CODEX_APP_EXTERNAL_ADAPTER,
    BEHAVIOR_EXTERNAL_PROVIDER_GATEWAY,
    BEHAVIOR_OFFICIAL_CODEX_APP_HTTP_PASSTHROUGH,
)

from . import official_passthrough as _official_passthrough
from . import sse as _sse
from . import host

def _rewrite_v2_unsupported_tool_history(
    payload: dict[str, Any],
    *,
    upstream: Mapping[str, Any],
    tool_protocol: str,
    compatibility_plan: RuntimeToolCompatibilityPlan | None,
    event_context: Mapping[str, Any] | None,
    upstream_name: str | None,
) -> bool:
    """Keep V2 Collaboration calls native while adapting stale tool history.

    Collaboration V2 is a boundary for the ``collaboration`` namespace, not a
    blanket exemption from the third-party input-item adapter.  Codex Desktop
    can place unrelated ``custom_tool_call`` history (for example ``exec``)
    beside V2 calls.  Responses providers generally expose only the plain
    function lifecycle unless an explicit custom lifecycle capability is
    supplied, so those opaque items must become transcript messages before the
    request reaches the provider. A uniquely paired plain-function lifecycle
    from an older tool surface is likewise retained as a read-only transcript
    when the current immutable plan has no owner for its identity.
    """
    input_items = payload.get("input")
    if not isinstance(input_items, list):
        return False

    capabilities = (
        compatibility_plan.capabilities
        if compatibility_plan is not None
        else _official_passthrough._runtime_tool_protocol_capabilities(tool_protocol, upstream)
    )
    tools = payload.get("tools")
    if not isinstance(tools, list):
        tools = []
    declared_custom_names = {
        tool.get("name")
        for tool in tools
        if isinstance(tool, Mapping)
        and tool.get("type") == "custom"
        and isinstance(tool.get("name"), str)
    }

    def plan_owns_custom_call(item: Mapping[str, Any]) -> bool:
        if compatibility_plan is None:
            return False
        name = item.get("name")
        alias_record = compatibility_plan.registry.record_for_alias(name)
        if alias_record is not None and alias_record.family == "custom_freeform":
            return True
        call_id = item.get("call_id")
        call_record = compatibility_plan.registry.record_for_call(call_id)
        if call_record is not None and call_record.family == "custom_freeform":
            return True
        return any(
            entry.family == "custom_freeform"
            and entry.original_name == name
            for entry in compatibility_plan.entries
        )

    def preserve_custom_call(item: Mapping[str, Any]) -> bool:
        # Preserve only a custom lifecycle that belongs to this request's
        # immutable compatibility plan.  A provider capability fact alone does
        # not establish ownership of an undeclared historical item (and must
        # not let a plain-function name collision bypass sanitization).
        if compatibility_plan is not None:
            return plan_owns_custom_call(item)
        return (
            capabilities.custom_lifecycle
            and item.get("name") in declared_custom_names
        )

    preserved_call_ids = {
        item.get("call_id")
        for item in input_items
        if isinstance(item, Mapping)
        and item.get("type") == "custom_tool_call"
        and isinstance(item.get("call_id"), str)
        and preserve_custom_call(item)
    }

    def plan_owns_plain_function_call(item: Mapping[str, Any]) -> bool:
        if compatibility_plan is None:
            return True
        name = item.get("name")
        call_id = item.get("call_id")
        if compatibility_plan.registry.record_for_alias(name) is not None:
            return True
        if compatibility_plan.registry.record_for_call(call_id) is not None:
            return True
        return any(
            entry.family == "plain_function"
            and entry.original_name == name
            for entry in compatibility_plan.entries
        )

    def has_valid_optional_item_identity(item: Mapping[str, Any]) -> bool:
        identities = []
        for field in ("id", "item_id"):
            if field not in item:
                continue
            value = item.get(field)
            if not isinstance(value, str) or not value:
                return False
            identities.append(value)
        return len(set(identities)) <= 1

    def is_well_formed_stale_function_call(item: Mapping[str, Any]) -> bool:
        allowed_fields = {
            "type",
            "id",
            "item_id",
            "status",
            "call_id",
            "name",
            "arguments",
        }
        return (
            set(item).issubset(allowed_fields)
            and _official_passthrough._is_standard_responses_function_call(item)
            and item.get("status") == "completed"
            and isinstance(item.get("arguments"), str)
            and has_valid_optional_item_identity(item)
        )

    def is_well_formed_stale_function_output(item: Mapping[str, Any]) -> bool:
        allowed_fields = {
            "type",
            "id",
            "item_id",
            "status",
            "call_id",
            "output",
        }
        return (
            set(item).issubset(allowed_fields)
            and item.get("type") == "function_call_output"
            and item.get("status") in (None, "completed")
            and isinstance(item.get("output"), str)
            and has_valid_optional_item_identity(item)
        )

    positions_by_call_id: dict[str, list[int]] = {}
    for index, item in enumerate(input_items):
        if not isinstance(item, Mapping):
            continue
        call_id = item.get("call_id")
        if isinstance(call_id, str) and call_id:
            positions_by_call_id.setdefault(call_id, []).append(index)

    stale_function_pair_indexes: set[int] = set()
    stale_function_pair_count = 0
    for call_index, item in enumerate(input_items):
        if (
            not isinstance(item, Mapping)
            or not is_well_formed_stale_function_call(item)
            or plan_owns_plain_function_call(item)
        ):
            continue
        call_id = item["call_id"]
        positions = positions_by_call_id.get(call_id, [])
        if len(positions) != 2 or positions[0] != call_index:
            continue
        output_index = positions[1]
        output_item = input_items[output_index]
        if (
            not isinstance(output_item, Mapping)
            or not is_well_formed_stale_function_output(output_item)
        ):
            continue
        stale_function_pair_indexes.update((call_index, output_index))
        stale_function_pair_count += 1

    rewritten_items: list[Any] = []
    changed = False
    custom_rewritten_count = 0
    for index, item in enumerate(input_items):
        if not isinstance(item, Mapping):
            rewritten_items.append(item)
            continue
        if index in stale_function_pair_indexes:
            replacement = _tool_history.internal_message(item)
            if replacement is not None:
                rewritten_items.append(replacement)
                changed = True
            else:
                rewritten_items.append(item)
            continue
        item_type = item.get("type")
        if item_type == "custom_tool_call" and not preserve_custom_call(item):
            replacement = _tool_history.internal_message(item)
            if replacement is not None:
                rewritten_items.append(replacement)
                changed = True
                custom_rewritten_count += 1
            else:
                rewritten_items.append(item)
            continue
        if (
            item_type == "custom_tool_call_output"
            and item.get("call_id") not in preserved_call_ids
        ):
            replacement = _tool_history.internal_message(item)
            if replacement is not None:
                rewritten_items.append(replacement)
                changed = True
                custom_rewritten_count += 1
            else:
                rewritten_items.append(item)
            continue
        rewritten_items.append(item)

    if not changed:
        return False
    payload["input"] = rewritten_items
    if custom_rewritten_count:
        _gateway_events.write_adapter_event(
            event_context,
            "v2_custom_tool_history_rewritten",
            upstream=upstream_name,
            count=custom_rewritten_count,
        )
    if stale_function_pair_count:
        _gateway_events.write_adapter_event(
            event_context,
            "v2_stale_function_history_rewritten",
            upstream=upstream_name,
            pair_count=stale_function_pair_count,
        )
    return True


def _drop_v2_chat_reasoning_history(
    payload: dict[str, Any],
    *,
    event_context: Mapping[str, Any] | None,
    upstream_name: str | None,
) -> bool:
    input_items = payload.get("input")
    if not isinstance(input_items, list):
        return False

    rewritten_items = [
        item
        for item in input_items
        if not (isinstance(item, Mapping) and item.get("type") == "reasoning")
    ]
    removed_count = len(input_items) - len(rewritten_items)
    if not removed_count:
        return False

    payload["input"] = rewritten_items
    _gateway_events.write_adapter_event(
        event_context,
        "v2_chat_reasoning_history_removed",
        upstream=upstream_name,
        count=removed_count,
    )
    return True


def _drop_chat_message_phase(
    payload: dict[str, Any],
    *,
    event_context: Mapping[str, Any] | None,
    upstream_name: str | None,
) -> bool:
    input_items = payload.get("input")
    if not isinstance(input_items, list):
        return False

    removed_count = 0
    rewritten_items: list[Any] = []
    for item in input_items:
        if isinstance(item, Mapping) and item.get("type") == "message" and "phase" in item:
            rewritten = dict(item)
            rewritten.pop("phase")
            rewritten_items.append(rewritten)
            removed_count += 1
        else:
            rewritten_items.append(item)
    if not removed_count:
        return False

    payload["input"] = rewritten_items
    _gateway_events.write_adapter_event(
        event_context,
        "chat_message_phase_removed",
        upstream=upstream_name,
        count=removed_count,
    )
    return True


def _sanitize_unsupported_compaction_input_items(payload: dict[str, Any]) -> bool:
    input_items = payload.get("input")
    if not isinstance(input_items, list):
        return False

    changed = False
    rewritten_items: list[Any] = []
    for item in input_items:
        if not isinstance(item, dict):
            rewritten_items.append(item)
            continue

        item_type = item.get("type")
        if item_type == "compaction":
            replacement = _tool_history.internal_message(item)
            if replacement is not None:
                rewritten_items.append(replacement)
            changed = True
            continue
        if item_type == "compaction_trigger":
            changed = True
            continue

        rewritten_items.append(item)

    if changed:
        payload["input"] = rewritten_items
    return changed


def _sanitize_official_system_messages(payload: dict[str, Any]) -> bool:
    """Rewrite Official-bound system roles to developer. Official Codex 400s otherwise."""
    changed = False
    for key in ("input", "messages"):
        items = payload.get(key)
        if not isinstance(items, list):
            continue
        rewritten_items: list[Any] = []
        key_changed = False
        for item in items:
            if not isinstance(item, dict):
                rewritten_items.append(item)
                continue
            item_type = item.get("type")
            role = item.get("role")
            if item_type == "system" or role == "system":
                rewritten = dict(item)
                if key == "input":
                    rewritten["type"] = "message"
                rewritten["role"] = "developer"
                rewritten_items.append(rewritten)
                key_changed = True
            else:
                rewritten_items.append(item)
        if key_changed:
            payload[key] = rewritten_items
            changed = True
    return changed


def _fold_official_instructions_into_developer(payload: dict[str, Any]) -> bool:
    """Move Chat→Responses ``instructions`` into a developer input item.

    Chat Completions ``role=system`` becomes ``instructions`` during
    translation. Official still 400s leftover ``role=system``; folding
    keeps the same rewrite as inbound Responses/Chat developer.
    """
    instructions = payload.get("instructions")
    if not isinstance(instructions, str) or not instructions.strip():
        return False
    input_items = payload.get("input")
    if input_items is None:
        next_input: list[Any] = []
    elif isinstance(input_items, list):
        next_input = input_items
    else:
        return False
    payload["input"] = [
        {
            "type": "message",
            "role": "developer",
            "content": instructions,
        },
        *next_input,
    ]
    del payload["instructions"]
    return True


def _fill_official_strict_json_schema(node: Any) -> bool:
    """Fill Official Codex strict-function schema requirements in place."""
    if not isinstance(node, dict):
        return False
    changed = False
    looks_like_object = node.get("type") == "object" or isinstance(node.get("properties"), dict)
    if looks_like_object:
        if "additionalProperties" not in node:
            node["additionalProperties"] = False
            changed = True
        properties = node.get("properties")
        if isinstance(properties, dict) and properties:
            required = node.get("required")
            property_names = [name for name in properties if isinstance(name, str)]
            if not isinstance(required, list):
                node["required"] = list(property_names)
                changed = True
            else:
                present = {name for name in required if isinstance(name, str)}
                missing = [name for name in property_names if name not in present]
                if missing:
                    node["required"] = [
                        name for name in required if isinstance(name, str)
                    ] + missing
                    changed = True
            for child in properties.values():
                if _fill_official_strict_json_schema(child):
                    changed = True
    additional = node.get("additionalProperties")
    if isinstance(additional, dict) and _fill_official_strict_json_schema(additional):
        changed = True
    items = node.get("items")
    if isinstance(items, dict) and _fill_official_strict_json_schema(items):
        changed = True
    elif isinstance(items, list):
        for item in items:
            if _fill_official_strict_json_schema(item):
                changed = True
    for key in ("allOf", "anyOf", "oneOf", "prefixItems"):
        value = node.get(key)
        if isinstance(value, list):
            for item in value:
                if _fill_official_strict_json_schema(item):
                    changed = True
    for key in ("$defs", "defs", "definitions"):
        value = node.get(key)
        if isinstance(value, dict):
            for child in value.values():
                if _fill_official_strict_json_schema(child):
                    changed = True
    return changed


def _sanitize_official_strict_function_tool(tool: Any) -> dict[str, Any] | None:
    if not isinstance(tool, dict) or tool.get("type") != "function":
        return None
    nested = tool.get("function")
    envelope = nested if isinstance(nested, dict) else tool
    if envelope.get("strict") is not True:
        return None
    parameters = envelope.get("parameters")
    if not isinstance(parameters, dict):
        return None
    next_parameters = deepcopy(parameters)
    if not _fill_official_strict_json_schema(next_parameters):
        return None
    next_tool = dict(tool)
    if isinstance(nested, dict):
        next_function = dict(nested)
        next_function["parameters"] = next_parameters
        next_tool["function"] = next_function
    else:
        next_tool["parameters"] = next_parameters
    return next_tool


def _sanitize_official_strict_function_schemas(payload: dict[str, Any]) -> bool:
    """Official Codex 400s strict tools that omit additionalProperties=false."""
    tools = payload.get("tools")
    if not isinstance(tools, list):
        return False
    changed = False
    next_tools: list[Any] = []
    for tool in tools:
        rewritten = _sanitize_official_strict_function_tool(tool)
        if rewritten is None:
            next_tools.append(tool)
            continue
        next_tools.append(rewritten)
        changed = True
    if changed:
        payload["tools"] = next_tools
    return changed


def _sanitize_official_invalid_tool_calls(payload: dict[str, Any]) -> bool:
    input_items = payload.get("input")
    if not isinstance(input_items, list):
        return False

    changed = False
    bad_function_call_ids: set[str] = set()
    bad_custom_call_ids: set[str] = set()
    rewritten_items: list[Any] = []

    for item in input_items:
        if not isinstance(item, dict):
            rewritten_items.append(item)
            continue

        item_type = item.get("type")
        call_id = item.get("call_id")
        if _official_passthrough._has_invalid_tool_name(item):
            if isinstance(call_id, str):
                if item_type == "custom_tool_call":
                    bad_custom_call_ids.add(call_id)
                else:
                    bad_function_call_ids.add(call_id)
            title = (
                "Invalid Codex tool call transcript"
                if item_type == "custom_tool_call"
                else "Invalid Codex function call transcript"
            )
            rewritten_items.append(_tool_history.transcript_message(title, item))
            changed = True
            continue

        if item_type == "function_call_output" and isinstance(call_id, str) and call_id in bad_function_call_ids:
            rewritten_items.append(_tool_history.transcript_message("Invalid Codex function result transcript", item))
            changed = True
            continue

        if item_type == "custom_tool_call_output" and isinstance(call_id, str) and call_id in bad_custom_call_ids:
            rewritten_items.append(_tool_history.transcript_message("Invalid Codex tool result transcript", item))
            changed = True
            continue

        rewritten_items.append(item)

    if changed:
        payload["input"] = rewritten_items
    return changed


def _apply_patch_adapter_enabled(event_context: Mapping[str, Any] | None) -> bool:
    return _apply_patch_adapter_module.enabled(event_context)


def _adapt_apply_patch_custom_tool_history(
    input_items: list[Any],
    *,
    event_context: Mapping[str, Any] | None,
) -> tuple[list[Any], set[str], bool]:
    return _apply_patch_adapter_module.adapt_custom_tool_history(
        input_items,
        event_context=event_context,
    )


def _adapt_third_party_apply_patch_response_body(
    payload: Any,
    event_context: Mapping[str, Any] | None,
) -> tuple[Any, bool]:
    return _apply_patch_adapter_module.adapt_response_body(payload, event_context)


class _ThirdPartyApplyPatchStreamAdapter(_ApplyPatchStreamAdapterImpl):
    def __init__(self, event_context: Mapping[str, Any] | None, *, surface: str = "stream"):
        super().__init__(event_context, surface=surface)


def _adapt_third_party_apply_patch_stream_events(
    events: list[Mapping[str, Any]],
    *,
    event_context: Mapping[str, Any] | None = None,
) -> tuple[list[Mapping[str, Any]], bool]:
    return _apply_patch_adapter_module.adapt_stream_events(events, event_context=event_context)


def compatible_response_body(
    body: bytes,
    upstream_name: str,
    event_context: Mapping[str, Any] | None = None,
) -> bytes:
    if upstream_name == "official":
        from . import collaboration_delivery
        body = collaboration_delivery.decode_body(body, event_context)
        try:
            payload = json.loads(body.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return body
        if isinstance(payload, dict):
            try:
                changed = False
                if _collab_v2.collapse_official_v2_names_for_chat(payload, event_context):
                    changed = True
                if _chat_official_native.collapse_official_native_tools_for_chat(payload, event_context):
                    changed = True
            except RuntimeToolCompatibilityError as exc:
                _official_passthrough._raise_runtime_tool_compatibility_error(exc)
            if changed:
                return json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        return body
    if _official_passthrough._is_raw_provider_probe_context(event_context):
        return body

    try:
        payload = _collaboration_adapter_module.decode_adapted_json(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return body

    collaboration_protocol = _collaboration_adapter_module.resolve_boundary(
        payload,
        event_context,
        surface="response",
    )
    event_context = _collaboration_adapter_module.context_with_protocol(event_context, collaboration_protocol)
    changed = False
    payload, runtime_tool_plan, runtime_tool_changed = (
        _official_passthrough.decode_tool_response(event_context, payload)
    )
    changed = changed or runtime_tool_changed
    changed = host._hide_reasoning_text(payload) or changed
    payload, apply_patch_changed = _adapt_third_party_apply_patch_response_body(payload, event_context)
    changed = changed or apply_patch_changed
    payload, _ = _collaboration_adapter_module.apply_external_worker_response_contract(
        payload,
        event_context,
        surface="body",
        attach_sidecars=False,
    )
    payload, alias_changed = _official_passthrough._normalize_third_party_tool_call(payload, event_context, runtime_tool_plan)
    if alias_changed:
        _gateway_events.write_adapter_event(
            event_context,
            "third_party_tool_call_alias_normalized",
            upstream=upstream_name,
            surface="body",
        )
    changed = changed or alias_changed
    payload, bounded_tool_search_changed = (
        _tool_surface_adapter_module.suppress_bounded_tool_search_calls(payload, event_context)
    )
    changed = changed or bounded_tool_search_changed
    payload, invalid_tool_changed = _official_passthrough._downgrade_invalid_third_party_tool_calls(payload, runtime_tool_plan)
    changed = changed or invalid_tool_changed
    if isinstance(payload, dict) and (event_context or {}).get("_caller_wire_format") == "chat_completions":
        try:
            if _chat_official_native.collapse_official_native_tools_for_chat(payload, event_context):
                changed = True
        except RuntimeToolCompatibilityError as exc:
            _official_passthrough._raise_runtime_tool_compatibility_error(exc)
    payload, requested_binding_changed = (
        _collaboration_adapter_module.apply_external_worker_response_contract(
            payload,
            event_context,
            surface="body",
            validate_selectors=False,
        )
    )
    changed = changed or requested_binding_changed
    import multimodal_tool_result as _multimodal_tool_result

    if isinstance(payload, dict) and _multimodal_tool_result.annotate_compact_response_payload(
        payload, event_context
    ):
        changed = True
    if isinstance(payload, dict) and _protocol_translation.stamp_wire_timestamps(payload):
        changed = True
    if not changed:
        return body
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
