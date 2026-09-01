"""Gateway compatibility application pipeline."""

from __future__ import annotations

from collections.abc import Iterable as IterableABC
from pathlib import Path
from typing import Any, Iterable, Mapping, NoReturn

import hashlib
import json
import re
import uuid

import gateway_events as _gateway_events
import tool_surface_adapter as _tool_surface_adapter_module

import gateway_stream_semantics as _stream_semantics

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
from protocol_translation import UnsupportedProtocolTranslationError
from runtime_tool_compatibility import (
    HostedCapabilityFacts as RuntimeHostedCapabilityFacts,
    ProtocolCapabilities as RuntimeProtocolCapabilities,
    ToolCompatibilityError as RuntimeToolCompatibilityError,
    ToolCompatibilityPlan as RuntimeToolCompatibilityPlan,
    build_tool_compatibility_plan,
)
from subagent_policy import deterministic_required_action
from gateway_settings import subagent_guidance_enabled, subagent_semantic_repair_enabled
from subagent_scheduler import bounded_workflow_from_exact_prompts, compute_allowed_actions
from subagent_state import build_subagent_state, is_worker_subagent_request, state_guidance_message
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

from . import multi_agent as _multi_agent
from . import response as _response
from . import host

def _compatible_compaction_message(item: Mapping[str, Any]) -> dict[str, str] | None:
    seen: set[str] = set()
    fragments: list[str] = []
    for fragment in _stream_semantics.collect_text_fragments(dict(item)):
        if fragment not in seen:
            seen.add(fragment)
            fragments.append(fragment)

    if not fragments:
        return _developer_text_message(
            "[Compacted conversation context — opaque, details unavailable]"
        )

    return _developer_text_message("[Compacted conversation context]\n" + "\n\n".join(fragments))


def _developer_text_message(content: str) -> dict[str, str]:
    return {"type": "message", "role": "developer", "content": content}


def _stringify_internal_field(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    try:
        return json.dumps(value, ensure_ascii=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(value).strip()


def _append_internal_field(lines: list[str], label: str, value: Any) -> None:
    text = _stringify_internal_field(value)
    if not text:
        return
    lines.append(f"{label}:")
    lines.append(text)


def _single_line_internal_field(value: Any) -> str:
    text = _stringify_internal_field(value)
    return " ".join(text.split()) if text else ""


def _valid_tool_name(value: Any) -> bool:
    return _tool_surface_adapter_module.valid_tool_name(value)


def _is_tool_call_item(item: Mapping[str, Any]) -> bool:
    return _tool_surface_adapter_module.is_tool_call_item(item)


def _has_invalid_tool_name(item: Mapping[str, Any]) -> bool:
    return _tool_surface_adapter_module.has_invalid_tool_name(item)


def _transcript_text(title: str, item: Mapping[str, Any]) -> str:
    lines = [title]
    for label, key in (
        ("type", "type"),
        ("namespace", "namespace"),
        ("name", "name"),
        ("call_id", "call_id"),
        ("status", "status"),
    ):
        value = _stringify_internal_field(item.get(key))
        if value:
            lines.append(f"{label}: {value}")
    _append_internal_field(lines, "input", item.get("input"))
    _append_internal_field(lines, "arguments", item.get("arguments"))
    _append_internal_field(lines, "output", item.get("output"))
    _append_internal_field(lines, "action", item.get("action"))
    _append_internal_field(lines, "execution", item.get("execution"))
    _append_internal_field(lines, "tools", item.get("tools"))
    return "\n".join(lines)


def _assistant_transcript_message(title: str, item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": _transcript_text(title, item)}],
    }


def _json_object_from_arguments(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return dict(value)
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        try:
            parsed, _end = json.JSONDecoder().raw_decode(text)
        except json.JSONDecodeError:
            return None
    return dict(parsed) if isinstance(parsed, dict) else None


def _json_argument_string_needs_repair(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        parsed = json.loads(value.strip())
    except json.JSONDecodeError:
        parsed_obj = _json_object_from_arguments(value)
        return parsed_obj is not None
    return not isinstance(parsed, dict)


def _dump_arguments_like(original: Any, arguments: Mapping[str, Any]) -> Any:
    if isinstance(original, str):
        return json.dumps(arguments, ensure_ascii=True, separators=(",", ":"))
    return dict(arguments)


def _tool_schema_name(value: Any) -> str | None:
    return _tool_surface_adapter_module.tool_schema_name(value)


def _tool_parameters_schema(value: Mapping[str, Any]) -> dict[str, Any]:
    return _tool_surface_adapter_module.tool_parameters_schema(value)


def _explicit_function_tool(name: str, description: str, parameters: Mapping[str, Any]) -> dict[str, Any]:
    return _tool_surface_adapter_module.explicit_function_tool(name, description, parameters)


def _supports_explicit_namespace_alias(namespace_name: str) -> bool:
    return _tool_surface_adapter_module.supports_explicit_namespace_alias(namespace_name)


def _looks_like_response_tool_name_fragment(value: Mapping[str, Any]) -> bool:
    return _tool_surface_adapter_module.looks_like_response_tool_name_fragment(value)


def _is_local_tool_gateway_tool_schema(value: Any) -> bool:
    return _tool_surface_adapter_module.is_local_tool_gateway_tool_schema(value)


def _is_mcp_or_codex_app_tool_schema(value: Any) -> bool:
    return _tool_surface_adapter_module.is_mcp_or_codex_app_tool_schema(value)


def _is_flattened_namespace_schema(value: Any) -> bool:
    return _tool_surface_adapter_module.is_flattened_namespace_schema(value)


def _is_raw_namespace_schema(value: Any) -> bool:
    return _tool_surface_adapter_module.is_raw_namespace_schema(value)


def _valid_namespace_function_names(value: Any) -> tuple[str, tuple[str, ...]] | None:
    return _tool_surface_adapter_module.valid_namespace_function_names(value)


def _deferred_namespace_surface_counts(
    source_tools: list[Any],
    final_tools: list[Any],
) -> tuple[int, int]:
    return _tool_surface_adapter_module.deferred_namespace_surface_counts(source_tools, final_tools)


def _flatten_namespace_function_tools(tools: list[Any]) -> list[dict[str, Any]]:
    return _tool_surface_adapter_module.flatten_namespace_function_tools(tools)


_RUNTIME_TOOL_COMPATIBILITY_PLAN_KEY = "_runtime_tool_compatibility_plan"


_RUNTIME_TOOL_COMPATIBILITY_STREAM_KEY = "_runtime_tool_compatibility_stream"


_RUNTIME_TOOL_COMPATIBILITY_ATTEMPT_KEY = "_runtime_tool_compatibility_attempt_generation"


_RUNTIME_TOOL_COMPATIBILITY_ATTEMPT_PLAN_KEY = "_runtime_tool_compatibility_attempt_plan"


_RUNTIME_TOOL_COMPATIBILITY_ATTEMPT_PLAN_GENERATION_KEY = (
    "_runtime_tool_compatibility_attempt_plan_generation"
)


_RUNTIME_TOOL_CAPABILITY_MANIFEST_ERROR_CODE = "tool_compatibility_capability_manifest"


def _raise_malformed_runtime_tool_capability_manifest() -> NoReturn:
    raise RuntimeToolCompatibilityError(
        _RUNTIME_TOOL_CAPABILITY_MANIFEST_ERROR_CODE,
        "malformed_capability_manifest",
    )


def _validate_runtime_tool_capability_facts(facts: Mapping[str, Any]) -> None:
    boolean_keys = {
        "function_lifecycle",
        "supports_functions",
        "namespace_lifecycle",
        "supports_namespace",
        "supports_namespaces",
        "custom_lifecycle",
        "supports_custom",
        "supports_custom_tools",
        "tool_search_lifecycle",
        "supports_tool_search",
        "accepts_namespace_adapter",
        "namespace_adapter",
        "accepts_custom_adapter",
        "custom_adapter",
        "accepts_tool_search_adapter",
        "tool_search_adapter",
    }
    for key in boolean_keys:
        if key in facts and type(facts[key]) is not bool:
            _raise_malformed_runtime_tool_capability_manifest()

    for key in ("hosted_lifecycles", "hosted_kinds", "unknown_lifecycles", "unknown_kinds"):
        if key not in facts:
            continue
        value = facts[key]
        if isinstance(value, str):
            continue
        if isinstance(value, Mapping):
            if any(not isinstance(name, str) or type(enabled) is not bool for name, enabled in value.items()):
                _raise_malformed_runtime_tool_capability_manifest()
            continue
        if isinstance(value, (bytes, bytearray)) or not isinstance(value, IterableABC):
            _raise_malformed_runtime_tool_capability_manifest()
        if any(not isinstance(name, str) for name in value):
            _raise_malformed_runtime_tool_capability_manifest()

    for key in ("max_tool_name_length", "max_alias_attempts"):
        if key in facts and (type(facts[key]) is not int or facts[key] <= 0):
            _raise_malformed_runtime_tool_capability_manifest()


def _runtime_tool_protocol_capabilities(
    tool_protocol: str,
    upstream: Mapping[str, Any],
) -> RuntimeProtocolCapabilities:
    try:
        supplied = upstream.get("tool_protocol_capabilities")
        if supplied is not None and not isinstance(supplied, Mapping):
            _raise_malformed_runtime_tool_capability_manifest()
        if isinstance(supplied, Mapping):
            _validate_runtime_tool_capability_facts(supplied)
        facts = supplied if isinstance(supplied, Mapping) else None
        # A capability manifest is authoritative only for predicates it
        # explicitly states.  Responses and chat-completions retain the
        # conservative plain-function + adapter baseline.  Text-compatible
        # endpoints have no native lifecycle by default; every capability
        # must be explicit.
        if tool_protocol == "text_compat":
            baseline_protocol = "none"
        elif tool_protocol == "responses_structured":
            baseline_protocol = "chat_tools"
        else:
            baseline_protocol = tool_protocol
        if facts is not None:
            return RuntimeProtocolCapabilities.for_protocol(baseline_protocol, facts)
        if baseline_protocol in {"chat_tools", "chat", "chat_completions"}:
            return RuntimeProtocolCapabilities.chat_tools()
        return RuntimeProtocolCapabilities()
    except RuntimeToolCompatibilityError:
        raise
    except (AttributeError, OverflowError, TypeError, ValueError):
        _raise_malformed_runtime_tool_capability_manifest()


def _raise_runtime_tool_compatibility_error(error: RuntimeToolCompatibilityError) -> NoReturn:
    raise UpstreamProtocolTranslationError(
        UnsupportedProtocolTranslationError(error.code, str(error)),
        classification=error.classification,
    ) from error


def _runtime_tool_alias_token(
    declarations: list[Any],
    *,
    selected_protocol: str,
    protocol_capabilities: RuntimeProtocolCapabilities,
    provider_hosted_capabilities: Any,
) -> str:
    capability_set = {
        "function_lifecycle": protocol_capabilities.function_lifecycle,
        "namespace_lifecycle": protocol_capabilities.namespace_lifecycle,
        "custom_lifecycle": protocol_capabilities.custom_lifecycle,
        "tool_search_lifecycle": protocol_capabilities.tool_search_lifecycle,
        "hosted_lifecycles": sorted(protocol_capabilities.hosted_lifecycles),
        "unknown_lifecycles": sorted(protocol_capabilities.unknown_lifecycles),
        "accepts_namespace_adapter": protocol_capabilities.accepts_namespace_adapter,
        "accepts_custom_adapter": protocol_capabilities.accepts_custom_adapter,
        "accepts_tool_search_adapter": protocol_capabilities.accepts_tool_search_adapter,
        "max_tool_name_length": protocol_capabilities.max_tool_name_length,
        "provider_hosted_kinds": sorted(
            RuntimeHostedCapabilityFacts.from_value(
                provider_hosted_capabilities
            ).supported_kinds
        ),
    }
    canonical = json.dumps(
        {
            "capability_set": capability_set,
            "declarations": declarations,
            "selected_protocol": selected_protocol,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _prepare_runtime_tool_compatibility(
    payload: dict[str, Any],
    upstream: Mapping[str, Any],
    tool_protocol: str,
    event_context: dict[str, Any],
    native_responses_tool_codec: str | None = None,
) -> bool:
    tools = payload.get("tools")
    declarations = tools if isinstance(tools, list) else []
    configured_codec = (
        native_responses_tool_codec
        if native_responses_tool_codec is not None
        else _external_native_responses_tool_codec(upstream)
    )
    # A native Responses codec is not a declaration filter for Chat.  When a
    # maintained model is reachable over both protocols, the Chat compatibility
    # plan must still see the custom declaration and apply its reversible
    # function adapter instead of leaking the Responses-only setting (#285).
    codec = configured_codec if tool_protocol == "responses_structured" else "none"
    if codec == "strict_apply_patch":
        apply_patch_tools = [
            tool
            for tool in declarations
            if isinstance(tool, Mapping) and tool.get("name") == APPLY_PATCH_FUNCTION_NAME
        ]
        if len(apply_patch_tools) > 1:
            _raise_native_responses_tool_contract_error(
                event_context,
                codec=codec,
                reason="duplicate_declaration",
                count=len(apply_patch_tools),
            )
        if apply_patch_tools:
            apply_patch_tool = apply_patch_tools[0]
            if apply_patch_tool.get("type") != "custom":
                _raise_native_responses_tool_contract_error(
                    event_context,
                    codec=codec,
                    reason="declaration_not_custom",
                )
            _validate_strict_apply_patch_custom_tool(
                apply_patch_tool,
                event_context,
                codec=codec,
            )
    planned_declarations = [
        declaration
        for declaration in declarations
        if not (
            isinstance(declaration, Mapping)
            and declaration.get("name") == APPLY_PATCH_FUNCTION_NAME
            and (
                codec == "strict_apply_patch"
                or not isinstance(declaration.get("format"), Mapping)
            )
        )
    ]
    try:
        provider_hosted_capabilities = upstream.get("hosted_tool_capabilities")
        protocol_capabilities = _runtime_tool_protocol_capabilities(tool_protocol, upstream)
        plan = build_tool_compatibility_plan(
            planned_declarations,
            selected_protocol=tool_protocol,
            provider_hosted_capabilities=provider_hosted_capabilities,
            tool_choice=payload.get("tool_choice"),
            protocol_capabilities=protocol_capabilities,
            request_token=_runtime_tool_alias_token(
                planned_declarations,
                selected_protocol=tool_protocol,
                protocol_capabilities=protocol_capabilities,
                provider_hosted_capabilities=provider_hosted_capabilities,
            ),
            collaboration_protocol=event_context.get("collaboration_protocol"),
        )
    except RuntimeToolCompatibilityError as exc:
        _gateway_events.write_proxy_event(
            "runtime_tool_compatibility_rejected",
            surface=exc.surface,
            outcome="rejected",
            count=1,
        )
        _raise_runtime_tool_compatibility_error(exc)
    event_context[_RUNTIME_TOOL_COMPATIBILITY_PLAN_KEY] = plan
    event_context.pop(_RUNTIME_TOOL_COMPATIBILITY_STREAM_KEY, None)
    event_context.pop(_RUNTIME_TOOL_COMPATIBILITY_ATTEMPT_PLAN_KEY, None)
    event_context.pop(_RUNTIME_TOOL_COMPATIBILITY_ATTEMPT_PLAN_GENERATION_KEY, None)
    _gateway_events.write_proxy_event(
        "runtime_tool_compatibility_planned",
        counts=plan.diagnostics.as_dict()["counts"],
    )
    return False

def _apply_runtime_tool_compatibility_plan(
    payload: dict[str, Any],
    plan: RuntimeToolCompatibilityPlan,
) -> bool:
    try:
        encoded = plan.encode_payload(payload)
    except RuntimeToolCompatibilityError as exc:
        _raise_runtime_tool_compatibility_error(exc)
    if encoded == payload:
        return False
    payload.clear()
    payload.update(encoded)
    return True


def _runtime_tool_compatibility_plan(
    event_context: Mapping[str, Any] | None,
) -> RuntimeToolCompatibilityPlan | None:
    value = (event_context or {}).get(_RUNTIME_TOOL_COMPATIBILITY_PLAN_KEY)
    return value if isinstance(value, RuntimeToolCompatibilityPlan) else None


def _runtime_tool_compatibility_plan_for_attempt(
    event_context: Mapping[str, Any] | None,
) -> RuntimeToolCompatibilityPlan | None:
    """Resolve the immutable request plan into the current relay attempt.

    The request plan owns stable aliases and declaration classification.  Each
    permitted upstream retry receives a shallow plan copy with a fresh call
    ownership ledger; no route or provider selection is performed here.
    """
    request_plan = _runtime_tool_compatibility_plan(event_context)
    if request_plan is None or not isinstance(event_context, dict):
        return request_plan
    generation = event_context.get(_RUNTIME_TOOL_COMPATIBILITY_ATTEMPT_KEY)
    if generation is None:
        return request_plan
    attempt_plan = event_context.get(_RUNTIME_TOOL_COMPATIBILITY_ATTEMPT_PLAN_KEY)
    planned_generation = event_context.get(
        _RUNTIME_TOOL_COMPATIBILITY_ATTEMPT_PLAN_GENERATION_KEY
    )
    if (
        not isinstance(attempt_plan, RuntimeToolCompatibilityPlan)
        or planned_generation != generation
        or attempt_plan is request_plan
    ):
        attempt_plan = request_plan.new_attempt()
        event_context[_RUNTIME_TOOL_COMPATIBILITY_ATTEMPT_PLAN_KEY] = attempt_plan
        event_context[_RUNTIME_TOOL_COMPATIBILITY_ATTEMPT_PLAN_GENERATION_KEY] = generation
        event_context.pop(_RUNTIME_TOOL_COMPATIBILITY_STREAM_KEY, None)
    return attempt_plan


def _runtime_tool_compatibility_stream_for_attempt(
    event_context: Mapping[str, Any] | None,
) -> tuple[RuntimeToolCompatibilityPlan | None, Any | None]:
    """Return the attempt-local stream ledger shared by both relay surfaces."""
    plan = _runtime_tool_compatibility_plan_for_attempt(event_context)
    if plan is None or not isinstance(event_context, dict):
        return plan, None
    stream = event_context.get(_RUNTIME_TOOL_COMPATIBILITY_STREAM_KEY)
    if stream is None or getattr(stream, "plan", None) is not plan:
        stream = plan.new_stream()
        event_context[_RUNTIME_TOOL_COMPATIBILITY_STREAM_KEY] = stream
    return plan, stream


def _runtime_tool_adapter_alias_hash(aliases: Iterable[str]) -> str:
    """Hash the ordered generated alias surface without logging tool names."""

    encoded = json.dumps(
        list(aliases),
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _runtime_tool_adapter_request_snapshot(
    plan: RuntimeToolCompatibilityPlan,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Return bounded evidence for the body sent to the selected upstream.

    This is intentionally computed after ``encode_payload``.  The capture
    proxy used by the private E2E runner sits before this boundary and can
    therefore only prove the CLI's native collaboration surface; these
    fields are the Gateway's own proof of the final provider wire shape.
    """

    tools = payload.get("tools")
    tool_values = tools if isinstance(tools, list) else []
    aliases = [
        tool.get("name")
        for tool in tool_values
        if isinstance(tool, Mapping)
        and tool.get("type") == "function"
        and isinstance(tool.get("name"), str)
        and plan.registry.record_for_alias(tool.get("name")) is not None
    ]
    namespace_count = sum(
        1
        for tool in tool_values
        if isinstance(tool, Mapping) and tool.get("type") == "namespace"
    )
    namespace_child_count = sum(
        len(tool.get("tools"))
        for tool in tool_values
        if isinstance(tool, Mapping)
        and tool.get("type") == "namespace"
        and isinstance(tool.get("tools"), list)
    )
    history_call_ids: set[str] = set()
    history_output_ids: set[str] = set()
    alias_call_ids: set[str] = set()
    history_call_count = 0
    history_output_count = 0
    input_items = payload.get("input")
    for item in input_items if isinstance(input_items, list) else ():
        if not isinstance(item, Mapping):
            continue
        item_type = item.get("type")
        call_id = item.get("call_id")
        if item_type == "function_call" and plan.registry.record_for_alias(item.get("name")) is not None:
            history_call_count += 1
            if isinstance(call_id, str) and call_id:
                history_call_ids.add(call_id)
                alias_call_ids.add(call_id)
        elif item_type == "function_call_output":
            record = plan.registry.record_for_call(call_id)
            if record is not None or (isinstance(call_id, str) and call_id in alias_call_ids):
                history_output_count += 1
                if isinstance(call_id, str) and call_id:
                    history_output_ids.add(call_id)
    return {
        "adapted_alias_count": len(aliases),
        "adapted_alias_unique_count": len(set(aliases)),
        "adapted_alias_hash": _runtime_tool_adapter_alias_hash(aliases),
        "upstream_function_tool_count": sum(
            1
            for tool in tool_values
            if isinstance(tool, Mapping) and tool.get("type") == "function"
        ),
        "upstream_namespace_count": namespace_count,
        "upstream_namespace_child_count": namespace_child_count,
        "adapted_history_call_count": history_call_count,
        "adapted_history_output_count": history_output_count,
        "adapted_history_pair_count": len(history_call_ids & history_output_ids),
    }


def _runtime_tool_adapter_item_snapshot(
    plan: RuntimeToolCompatibilityPlan,
    value: Any,
) -> dict[str, Any]:
    """Count alias-owned response items without retaining their contents."""

    items = value if isinstance(value, list) else [value]
    call_count = 0
    output_count = 0
    alias_call_ids: set[str] = set()
    aliases: list[str] = []
    for item in items:
        if not isinstance(item, Mapping):
            continue
        candidates = [item]
        nested = item.get("item")
        if isinstance(nested, Mapping):
            candidates.append(nested)
        for candidate in candidates:
            if candidate.get("type") == "function_call":
                name = candidate.get("name")
                if plan.registry.record_for_alias(name) is not None:
                    call_count += 1
                    call_id = candidate.get("call_id")
                    if isinstance(call_id, str) and call_id:
                        alias_call_ids.add(call_id)
                    if isinstance(name, str):
                        aliases.append(name)
            elif candidate.get("type") == "function_call_output":
                call_id = candidate.get("call_id")
                if (
                    plan.registry.record_for_call(call_id) is not None
                    or (isinstance(call_id, str) and call_id in alias_call_ids)
                ):
                    output_count += 1
    return {
        "wire_alias_call_count": call_count,
        "wire_alias_output_count": output_count,
        "wire_alias_hash": _runtime_tool_adapter_alias_hash(aliases),
    }


def _write_runtime_tool_adapter_request_evidence(
    plan: RuntimeToolCompatibilityPlan,
    payload: Mapping[str, Any],
    event_context: Mapping[str, Any] | None,
) -> None:
    snapshot = _runtime_tool_adapter_request_snapshot(plan, payload)
    if not plan.has_adaptations and not (
        snapshot["adapted_history_call_count"]
        or snapshot["adapted_history_output_count"]
    ):
        return
    _gateway_events.write_adapter_event(
        event_context,
        "runtime_tool_adapter_request",
        surface="request",
        outcome="adapted",
        **snapshot,
    )


def _write_runtime_tool_adapter_response_evidence(
    plan: RuntimeToolCompatibilityPlan,
    wire_value: Any,
    decoded_value: Any,
    event_context: Mapping[str, Any] | None,
    *,
    surface: str,
) -> None:
    snapshot = _runtime_tool_adapter_item_snapshot(plan, wire_value)
    wire_count = snapshot["wire_alias_call_count"] + snapshot["wire_alias_output_count"]
    if not wire_count or wire_value == decoded_value:
        return
    _gateway_events.write_adapter_event(
        event_context,
        "runtime_tool_adapter_response",
        surface=surface,
        outcome="inverse_mapped",
        adapted_alias_hash=_runtime_tool_adapter_alias_hash(plan.aliases),
        reverse_mapping_count=wire_count,
        reverse_mapped_call_count=snapshot["wire_alias_call_count"],
        reverse_mapped_output_count=snapshot["wire_alias_output_count"],
        **snapshot,
    )


def _runtime_required_tool_diagnostics(
    plan: RuntimeToolCompatibilityPlan | None,
    tool_choice_name: Any,
) -> tuple[str, str]:
    """Return bounded family/disposition fields for required-tool telemetry."""
    if plan is None or not isinstance(tool_choice_name, str):
        return "unknown", "unknown"

    record = plan.registry.record_for_alias(tool_choice_name)
    if record is not None:
        family = record.family
        disposition = next(
            (
                entry.disposition
                for entry in plan.entries
                if entry.declaration_index == record.declaration_index
            ),
            "unknown",
        )
    else:
        matches = [
            entry for entry in plan.entries if entry.original_name == tool_choice_name
        ]
        if len(matches) != 1:
            return "unknown", "unknown"
        family = matches[0].family
        disposition = matches[0].disposition

    bounded_families = {
        "plain_function",
        "namespace",
        "custom_freeform",
        "tool_search",
        "selected_provider_hosted",
        "unknown_future_kind",
    }
    bounded_dispositions = {
        "native",
        "adapt",
        "omit",
        "required-but-unavailable",
    }
    return (
        family if family in bounded_families else "unknown",
        disposition if disposition in bounded_dispositions else "unknown",
    )


def _runtime_alias_matches_namespace(
    plan: RuntimeToolCompatibilityPlan | None,
    tool: Any,
    namespace: str,
) -> bool:
    if plan is None or not isinstance(tool, Mapping):
        return False
    record = plan.registry.record_for_alias(_tool_schema_name(tool))
    return record is not None and record.namespace == namespace


def _runtime_alias_for_namespace_child(
    plan: RuntimeToolCompatibilityPlan | None,
    namespace: str,
    child_name: str,
) -> str | None:
    if plan is None:
        return None
    for alias in plan.aliases:
        record = plan.registry.record_for_alias(alias)
        if record is not None and record.namespace == namespace and record.child_name == child_name:
            return alias
    return None


def _runtime_plan_has_native_plain_function(
    plan: RuntimeToolCompatibilityPlan | None,
    item: Mapping[str, Any],
) -> bool:
    return _tool_surface_adapter_module.runtime_plan_has_native_plain_function(plan, item)


def _rewrite_generated_guidance_tool_name(value: Any, original: str, alias: str) -> Any:
    if isinstance(value, str):
        return value.replace(original, alias)
    if isinstance(value, list):
        return [_rewrite_generated_guidance_tool_name(item, original, alias) for item in value]
    if isinstance(value, Mapping):
        return {
            key: _rewrite_generated_guidance_tool_name(item, original, alias)
            for key, item in value.items()
        }
    return value


STRICT_APPLY_PATCH_EXAMPLE = """*** Begin Patch
*** Update File: example.txt
@@
-before
+after
*** End Patch"""


STRICT_APPLY_PATCH_CUSTOM_TOOL_FIELDS = frozenset(
    {"type", "name", "description", "format"}
)


STRICT_APPLY_PATCH_FORMAT_FIELDS = frozenset(
    {"type", "syntax", "definition"}
)


def _raise_native_responses_tool_contract_error(
    event_context: Mapping[str, Any] | None,
    *,
    codec: str,
    reason: str,
    count: int = 1,
) -> NoReturn:
    _gateway_events.write_adapter_event(
        event_context,
        "native_responses_tool_codec",
        codec=codec,
        outcome="rejected",
        count=count,
        reason=reason,
    )
    raise UpstreamProtocolTranslationError(
        UnsupportedProtocolTranslationError(
            host.NATIVE_RESPONSES_TOOL_CONTRACT_ERROR_CODE,
            "External native Responses apply_patch declaration is ambiguous or lossy.",
        )
    )


def _validate_strict_apply_patch_custom_tool(
    tool: Mapping[str, Any],
    event_context: Mapping[str, Any] | None,
    *,
    codec: str,
) -> None:
    if set(tool) != STRICT_APPLY_PATCH_CUSTOM_TOOL_FIELDS:
        _raise_native_responses_tool_contract_error(
            event_context,
            codec=codec,
            reason="custom_tool_fields_not_exact",
        )
    description = tool.get("description")
    tool_format = tool.get("format")
    if not isinstance(description, str) or not description.strip():
        _raise_native_responses_tool_contract_error(
            event_context,
            codec=codec,
            reason="missing_description",
        )
    if not isinstance(tool_format, Mapping) or set(tool_format) != STRICT_APPLY_PATCH_FORMAT_FIELDS:
        _raise_native_responses_tool_contract_error(
            event_context,
            codec=codec,
            reason="format_fields_not_exact",
        )
    if tool_format.get("type") != "grammar":
        _raise_native_responses_tool_contract_error(
            event_context,
            codec=codec,
            reason="format_not_grammar",
        )
    if not isinstance(tool_format.get("syntax"), str) or not tool_format["syntax"].strip():
        _raise_native_responses_tool_contract_error(
            event_context,
            codec=codec,
            reason="missing_grammar_syntax",
        )
    if not isinstance(tool_format.get("definition"), str) or not tool_format["definition"].strip():
        _raise_native_responses_tool_contract_error(
            event_context,
            codec=codec,
            reason="missing_grammar_definition",
        )


def _strict_apply_patch_function_tool(tool: Mapping[str, Any]) -> dict[str, Any]:
    description_parts: list[str] = []
    description = tool.get("description")
    if isinstance(description, str) and description.strip():
        description_parts.append(description.strip())
    tool_format = tool.get("format")
    if isinstance(tool_format, Mapping):
        grammar = tool_format.get("definition")
        if isinstance(grammar, str) and grammar.strip():
            syntax = tool_format.get("syntax")
            grammar_label = f"Original freeform grammar ({syntax}):" if isinstance(syntax, str) and syntax else "Original freeform grammar:"
            description_parts.append(f"{grammar_label}\n{grammar.strip()}")
    description_parts.append(
        "Provide the complete patch in the required `patch` string. "
        f"Example:\n{STRICT_APPLY_PATCH_EXAMPLE}"
    )
    return {
        "type": "function",
        "name": APPLY_PATCH_FUNCTION_NAME,
        "description": "\n\n".join(description_parts),
        "strict": True,
        "parameters": {
            "type": "object",
            "properties": {"patch": {"type": "string", "minLength": 1}},
            "required": ["patch"],
            "additionalProperties": False,
        },
    }


def _adapt_native_responses_tool_declarations(
    payload: dict[str, Any],
    upstream: Mapping[str, Any],
    event_context: Mapping[str, Any] | None,
    *,
    codec: str | None = None,
) -> bool:
    codec = codec or _external_native_responses_tool_codec(upstream)
    if codec == "none":
        return False
    tools = payload.get("tools")
    if not isinstance(tools, list):
        return False

    apply_patch_tools = [
        tool
        for tool in tools
        if isinstance(tool, Mapping) and tool.get("name") == APPLY_PATCH_FUNCTION_NAME
    ]
    if len(apply_patch_tools) > 1:
        _raise_native_responses_tool_contract_error(
            event_context,
            codec=codec,
            reason="duplicate_declaration",
            count=len(apply_patch_tools),
        )
    if not apply_patch_tools:
        _gateway_events.write_adapter_event(
            event_context,
            "native_responses_tool_codec",
            codec=codec,
            outcome="untouched",
            count=0,
        )
        return False
    apply_patch_tool = apply_patch_tools[0]
    if apply_patch_tool.get("type") != "custom":
        _raise_native_responses_tool_contract_error(
            event_context,
            codec=codec,
            reason="declaration_not_custom",
        )
    _validate_strict_apply_patch_custom_tool(
        apply_patch_tool,
        event_context,
        codec=codec,
    )

    rewritten_tools: list[Any] = []
    adapted = 0
    for tool in tools:
        if (
            isinstance(tool, Mapping)
            and tool.get("type") == "custom"
            and tool.get("name") == APPLY_PATCH_FUNCTION_NAME
        ):
            rewritten_tools.append(_strict_apply_patch_function_tool(tool))
            adapted += 1
        else:
            rewritten_tools.append(tool)
    if not adapted:
        return False
    payload["tools"] = rewritten_tools
    _gateway_events.write_adapter_event(
        event_context,
        "native_responses_tool_codec",
        codec=codec,
        outcome="adapted",
        count=adapted,
    )
    return True


def _structured_tool_function_call_item(item: Mapping[str, Any]) -> dict[str, Any] | None:
    return _tool_surface_adapter_module.structured_tool_function_call_item(item)


def _hoist_additional_tools_input_items(payload: dict[str, Any]) -> bool:
    return _tool_surface_adapter_module.hoist_additional_tools_input_items(payload)


def _rewrite_structured_tool_input_items(
    payload: dict[str, Any],
    event_context: Mapping[str, Any] | None = None,
    upstream_name: str | None = None,
    compatibility_plan: RuntimeToolCompatibilityPlan | None = None,
) -> bool:
    changed = _tool_surface_adapter_module.rewrite_structured_tool_input_items(
        payload,
        event_context=event_context,
        compatibility_plan=compatibility_plan,
    )
    if changed:
        _gateway_events.write_adapter_event(
            event_context,
            "structured_tool_input_items_rewritten",
            upstream=upstream_name,
        )
    return changed


def _inject_explicit_codex_tools(
    payload: dict[str, Any],
    include_tool_search: bool = True,
    include_multi_agent_tools: bool = True,
    include_spawn_agent: bool = True,
    include_wait_agent: bool = True,
    include_close_agent: bool = True,
    include_resume_agent: bool = True,
    include_send_input: bool = True,
    include_node_repl_tools: bool = True,
    include_local_tool_gateway_tools: bool = True,
    strip_namespace_tools: bool = True,
    strip_all_namespace_tools: bool = False,
    include_flattened_namespace_tools: bool = True,
    deferred_core_surface: bool = False,
    tool_surface_counts: dict[str, int] | None = None,
    tool_surface_source_tools: list[Any] | None = None,
    open_agent_ids: list[str] | None = None,
    wait_agent_ids: list[str] | None = None,
    close_agent_ids: list[str] | None = None,
    worker_selector_values: tuple[str, ...] = ("worker", "default"),
) -> bool:
    return _tool_surface_adapter_module.inject_explicit_codex_tools(
        payload,
        include_tool_search=include_tool_search,
        include_multi_agent_tools=include_multi_agent_tools,
        include_spawn_agent=include_spawn_agent,
        include_wait_agent=include_wait_agent,
        include_close_agent=include_close_agent,
        include_resume_agent=include_resume_agent,
        include_send_input=include_send_input,
        include_node_repl_tools=include_node_repl_tools,
        include_local_tool_gateway_tools=include_local_tool_gateway_tools,
        strip_namespace_tools=strip_namespace_tools,
        strip_all_namespace_tools=strip_all_namespace_tools,
        include_flattened_namespace_tools=include_flattened_namespace_tools,
        deferred_core_surface=deferred_core_surface,
        tool_surface_counts=tool_surface_counts,
        tool_surface_source_tools=tool_surface_source_tools,
        open_agent_ids=open_agent_ids,
        wait_agent_ids=wait_agent_ids,
        close_agent_ids=close_agent_ids,
        worker_selector_values=worker_selector_values,
    )


def _restrict_tools_to_required_tool(payload: dict[str, Any], tool_name: str | None) -> bool:
    if not tool_name:
        return False
    tools = payload.get("tools")
    if not isinstance(tools, list):
        return False
    filtered_tools = [tool for tool in tools if _tool_schema_name(tool) == tool_name]
    if not filtered_tools or len(filtered_tools) == len(tools):
        return False
    payload["tools"] = filtered_tools
    return True


def _function_tool_names(value: Any) -> set[str]:
    return _tool_surface_adapter_module.function_tool_names(value)


def _codex_apps_flat_alias_parts(name: Any) -> tuple[str, str] | None:
    return _tool_surface_adapter_module.codex_apps_flat_alias_parts(name)


def _codex_apps_flat_alias_name(name: Any) -> str | None:
    return _tool_surface_adapter_module.codex_apps_flat_alias_name(name)


def _split_namespace_tool_alias(name: Any) -> tuple[str, str] | None:
    return _tool_surface_adapter_module.split_namespace_tool_alias(name)


def _codex_apps_namespace_flat_alias(namespace: Any, name: Any) -> str | None:
    return _tool_surface_adapter_module.codex_apps_namespace_flat_alias(namespace, name)


def _requested_reasoning_effort(payload: Mapping[str, Any]) -> Any:
    reasoning = payload.get("reasoning")
    if isinstance(reasoning, Mapping):
        return reasoning.get("effort")
    if isinstance(reasoning, str):
        return reasoning
    return payload.get("reasoning_effort")


def _normalize_third_party_tool_call(
    value: Any,
    event_context: Mapping[str, Any] | None = None,
    compatibility_plan: Any = None,
) -> tuple[Any, bool]:
    return _tool_surface_adapter_module.normalize_third_party_tool_call(value, event_context, compatibility_plan)


def _status_completed_agent_ids(status: Any) -> list[str]:
    if not isinstance(status, Mapping):
        return []
    return [
        agent_id
        for agent_id, value in status.items()
        if isinstance(agent_id, str) and isinstance(value, Mapping) and "completed" in value
    ]


def _status_not_found_agent_ids(status: Any) -> list[str]:
    if not isinstance(status, Mapping):
        return []
    return [
        agent_id
        for agent_id, value in status.items()
        if isinstance(agent_id, str) and isinstance(value, str) and value == "not_found"
    ]


def _joined_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        return "\n".join(_joined_text(child) for child in value.values())
    if isinstance(value, list):
        return "\n".join(_joined_text(child) for child in value)
    return ""


def _active_user_request_text(value: Any) -> str:
    if not isinstance(value, list):
        return _joined_text(value)
    for item in reversed(value):
        if not isinstance(item, Mapping) or item.get("type") != "message":
            continue
        if item.get("role") != "user":
            continue
        text = _joined_text(item.get("content"))
        first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
        if first_line.startswith("Previous real Codex native ") or first_line.startswith("Codex native "):
            continue
        if text.strip():
            return text
    return ""


def _exact_child_prompts_from_request_text(text: str) -> list[str]:
    prompts: list[str] = []
    seen: set[str] = set()
    patterns = (
        r"child prompt must be exactly this complete string:\s*`([^`]+)`",
        r"Spawn child [A-Z]\s+with prompt exactly this complete string:\s*`([^`]+)`",
        r"Spawn child [A-Z]\s+with prompt exactly:\s*([^\r\n]+)",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, text, flags=re.IGNORECASE):
            prompt = match.group(1).strip()
            if prompt and prompt not in seen:
                prompts.append(prompt)
                seen.add(prompt)
    return prompts


def _workflow_baseline_status(text: str) -> str:
    marker = "Baseline git status before this E2E case started"
    marker_index = text.lower().find(marker.lower())
    if marker_index < 0:
        return ""
    candidate = text[marker_index:]
    match = re.search(r"```(?:text)?\s*\n(?P<body>.*?)```", candidate, re.DOTALL)
    if match:
        return match.group("body").strip()
    return ""


def _line_value(text: str, prefix: str) -> str | None:
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(prefix):
            value = line[len(prefix) :].strip()
            return value or None
    return None


def _split_agent_id_list(value: str | None) -> list[str]:
    if not value:
        return []
    return [item for item in re.split(r"[\s,]+", value.strip()) if item]


def _compatible_tool_message(item: Mapping[str, Any]) -> dict[str, str] | None:
    item_type = item.get("type")
    if item_type == "custom_tool_call":
        lines = ["Read-only Codex tool call transcript"]
        for label, key in (("tool", "name"), ("call_id", "call_id"), ("status", "status")):
            value = _stringify_internal_field(item.get(key))
            if value:
                lines.append(f"{label}: {value}")
        _append_internal_field(lines, "input", item.get("input"))
    elif item_type == "custom_tool_call_output":
        lines = ["Read-only Codex tool result transcript"]
        value = _stringify_internal_field(item.get("call_id"))
        if value:
            lines.append(f"call_id: {value}")
        _append_internal_field(lines, "output", item.get("output"))
    elif item_type == "function_call":
        lines = ["Read-only Codex function call transcript"]
        for label, key in (("namespace", "namespace"), ("function", "name"), ("call_id", "call_id"), ("status", "status")):
            value = _stringify_internal_field(item.get(key))
            if value:
                lines.append(f"{label}: {value}")
        _append_internal_field(lines, "arguments", item.get("arguments"))
    elif item_type == "function_call_output":
        lines = ["Read-only Codex function result transcript"]
        value = _stringify_internal_field(item.get("call_id"))
        if value:
            lines.append(f"call_id: {value}")
        _append_internal_field(lines, "output", item.get("output"))
    elif item_type == "web_search_call":
        lines = ["Read-only Codex web search call transcript"]
        value = _stringify_internal_field(item.get("status"))
        if value:
            lines.append(f"status: {value}")
        _append_internal_field(lines, "action", item.get("action"))
    elif item_type == "tool_search_call":
        lines = ["Read-only Codex tool search call transcript"]
        for label, key in (("call_id", "call_id"), ("status", "status")):
            value = _stringify_internal_field(item.get(key))
            if value:
                lines.append(f"{label}: {value}")
        _append_internal_field(lines, "arguments", item.get("arguments"))
        _append_internal_field(lines, "execution", item.get("execution"))
    elif item_type == "tool_search_output":
        lines = ["Read-only Codex tool search result transcript"]
        for label, key in (("call_id", "call_id"), ("status", "status")):
            value = _stringify_internal_field(item.get(key))
            if value:
                lines.append(f"{label}: {value}")
        _append_internal_field(lines, "execution", item.get("execution"))
        if _multi_agent._has_multi_agent_discovery_tools(item.get("tools")):
            lines.append("status: discovered_codex_native_multi_agent_tools")
            lines.append(
                "available_function_tools: multi_agent_v1__spawn_agent, multi_agent_v1__wait_agent, multi_agent_v1__close_agent, multi_agent_v1__resume_agent, multi_agent_v1__send_input"
            )
            lines.append(
                "next_action: call multi_agent_v1__spawn_agent to create the child agent; do not call tool_search again for the same multi-agent query."
            )
        if item.get("query_classification") == TOOL_SEARCH_UNAVAILABLE_QUERY_CLASSIFICATION:
            lines.append(f"query_classification: {TOOL_SEARCH_UNAVAILABLE_QUERY_CLASSIFICATION}")
            lines.append(f"empty_miss_count: {TOOL_SEARCH_EMPTY_MISS_BOUND}")
            lines.append("terminal: true")
            lines.append(
                "required_next_action: continue without the unavailable tool; do not call tool_search again for this exact query."
            )
        _append_internal_field(lines, "tools", item.get("tools"))
    else:
        return None

    if len(lines) == 1:
        return None
    return _developer_text_message("\n".join(lines))


def _compatible_internal_message(item: Mapping[str, Any]) -> dict[str, str] | None:
    if item.get("type") == "compaction":
        return _compatible_compaction_message(item)
    if item.get("type") == "reasoning":
        return None
    return _compatible_tool_message(item)


def _is_standard_responses_function_call(item: Mapping[str, Any]) -> bool:
    return (
        item.get("type") == "function_call"
        and isinstance(item.get("call_id"), str)
        and bool(item["call_id"])
        and isinstance(item.get("name"), str)
        and bool(item["name"])
        and "arguments" in item
        and not item.get("namespace")
        and WORKER_REQUESTED_BINDING_FIELD not in item
        and _multi_agent._multi_agent_function_call_name(item) is None
        and _multi_agent._node_repl_function_call_name(item) is None
        and not _is_mcp_or_codex_app_function_call(item)
    )


def _excessive_transparent_responses_tool_loop_count(payload: Mapping[str, Any]) -> int | None:
    input_items = payload.get("input")
    if not isinstance(input_items, list):
        return None

    pending_calls: dict[str, tuple[str, str]] = {}
    previous_pair: tuple[str, str, str] | None = None
    repeated_count = 0
    for item in input_items:
        if not isinstance(item, Mapping):
            previous_pair = None
            repeated_count = 0
            continue
        if _is_standard_responses_function_call(item):
            if item.get("status") == "completed":
                pending_calls[item["call_id"]] = (
                    item["name"],
                    json.dumps(item["arguments"], ensure_ascii=True, separators=(",", ":"), sort_keys=True),
                )
            else:
                previous_pair = None
                repeated_count = 0
            continue
        if item.get("type") == "function_call_output" and isinstance(item.get("call_id"), str):
            call = pending_calls.pop(item["call_id"], None)
            if call is not None and "output" in item:
                pair = call + (json.dumps(item["output"], ensure_ascii=True, separators=(",", ":"), sort_keys=True),)
                repeated_count = repeated_count + 1 if pair == previous_pair else 1
                previous_pair = pair
                if repeated_count >= host.EXCESSIVE_TOOL_LOOP_BOUND:
                    return repeated_count
                continue
        previous_pair = None
        repeated_count = 0
    return None


def _excessive_transparent_chat_tool_loop_count(payload: Mapping[str, Any]) -> int | None:
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return None

    previous_pair: tuple[str, str, str] | None = None
    repeated_count = 0
    index = 0
    while index < len(messages) - 1:
        message = messages[index]
        result = messages[index + 1]
        tool_calls = message.get("tool_calls") if isinstance(message, Mapping) else None
        if (
            not isinstance(tool_calls, list)
            or len(tool_calls) != 1
            or not isinstance(result, Mapping)
            or message.get("role") != "assistant"
            or result.get("role") != "tool"
        ):
            previous_pair = None
            repeated_count = 0
            index += 1
            continue
        call = tool_calls[0]
        function = call.get("function") if isinstance(call, Mapping) else None
        if (
            not isinstance(function, Mapping)
            or call.get("type") != "function"
            or not isinstance(call.get("id"), str)
            or call["id"] != result.get("tool_call_id")
            or not isinstance(function.get("name"), str)
            or not isinstance(function.get("arguments"), str)
            or not isinstance(result.get("content"), str)
        ):
            previous_pair = None
            repeated_count = 0
            index += 1
            continue
        pair = (function["name"], function["arguments"], result["content"])
        repeated_count = repeated_count + 1 if pair == previous_pair else 1
        previous_pair = pair
        if repeated_count >= host.EXCESSIVE_TOOL_LOOP_BOUND:
            return repeated_count
        index += 2
    return None


def _rewrite_internal_input_items(
    payload: dict[str, Any],
    event_context: Mapping[str, Any] | None = None,
    upstream_name: str | None = None,
    preserve_standard_function_history: bool = False,
) -> bool:
    input_items = payload.get("input")
    if not isinstance(input_items, list):
        return False

    changed = False
    rewritten_items: list[Any] = []
    single_step_node_repl_request = _multi_agent._has_single_step_node_repl_request(input_items)
    multi_agent_search_call_ids: set[str] = set()
    multi_agent_calls_by_call_id: dict[str, tuple[str, dict[str, Any] | None]] = {}
    node_repl_call_ids: set[str] = set()
    preserved_standard_call_ids: set[str] = set()
    for item in input_items:
        item_type = item.get("type") if isinstance(item, dict) else None
        call_id = item.get("call_id") if isinstance(item, dict) else None
        if preserve_standard_function_history and isinstance(item, dict):
            if _is_standard_responses_function_call(item):
                preserved_standard_call_ids.add(item["call_id"])
                rewritten_items.append(item)
                continue
            if item_type == "function_call_output" and call_id in preserved_standard_call_ids:
                rewritten_items.append(item)
                continue
        if isinstance(item_type, str) and item_type in INTERNAL_INPUT_ITEM_TYPES:
            if item_type == "function_call" and isinstance(call_id, str):
                if _multi_agent._node_repl_function_call_name(item) is not None:
                    node_repl_call_ids.add(call_id)
                    rewritten_items.append(_multi_agent._compatible_node_repl_call_message(item))
                    changed = True
                    continue
                tool_name = _multi_agent._multi_agent_function_call_name(item)
                if tool_name is not None:
                    arguments = _json_object_from_arguments(item.get("arguments"))
                    multi_agent_calls_by_call_id[call_id] = (tool_name, arguments)
                    rewritten_items.append(_multi_agent._compatible_multi_agent_call_message(item, tool_name))
                    changed = True
                    continue
            if (
                item_type == "function_call_output"
                and isinstance(call_id, str)
                and call_id in multi_agent_calls_by_call_id
            ):
                tool_name, arguments = multi_agent_calls_by_call_id[call_id]
                rewritten_items.append(_multi_agent._compatible_multi_agent_output_message(item, tool_name, arguments))
                changed = True
                continue
            if item_type == "function_call_output" and isinstance(call_id, str) and call_id in node_repl_call_ids:
                rewritten_items.append(
                    _multi_agent._compatible_node_repl_output_message(item, enforce_final=single_step_node_repl_request)
                )
                changed = True
                continue
            if (
                item_type == "tool_search_call"
                and isinstance(call_id, str)
                and _multi_agent._is_multi_agent_discovery_arguments(_json_object_from_arguments(item.get("arguments")))
            ):
                multi_agent_search_call_ids.add(call_id)
            elif (
                item_type == "tool_search_output"
                and isinstance(call_id, str)
                and call_id in multi_agent_search_call_ids
                and not item.get("tools")
            ):
                item = _multi_agent._multi_agent_discovery_output_item(item)
                _gateway_events.write_adapter_event(
                    event_context,
                    "tool_search_discovery_fallback_applied",
                    upstream=upstream_name,
                    call_id=call_id,
                )

            replacement = _compatible_internal_message(item)
            if replacement is not None:
                rewritten_items.append(replacement)
            changed = True
            continue
        rewritten_items.append(item)

    if changed:
        payload["input"] = rewritten_items
    return changed


def _downgrade_invalid_third_party_tool_calls(value: Any, compatibility_plan: Any = None) -> tuple[Any, bool]:
    return _tool_surface_adapter_module.downgrade_invalid_third_party_tool_calls(value, compatibility_plan)


def _function_call_namespace(item: Mapping[str, Any]) -> str | None:
    namespace = item.get("namespace")
    if isinstance(namespace, str) and namespace:
        return namespace
    alias = _split_namespace_tool_alias(item.get("name"))
    if alias is not None:
        return alias[0]
    return None


def _is_mcp_or_codex_app_function_call(item: Mapping[str, Any]) -> bool:
    if item.get("type") != "function_call":
        return False
    namespace = _function_call_namespace(item)
    if isinstance(namespace, str) and (namespace.startswith("mcp__") or namespace == "codex_app"):
        return True
    name = item.get("name")
    return isinstance(name, str) and (name.startswith("mcp__") or name.startswith("codex_app__"))


def _message_item_visible_text(item: Mapping[str, Any]) -> str:
    if item.get("type") != "message":
        return ""
    return host._chat_content_text(item.get("content")).strip()


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str) and item]


def _contains_response_function_call(value: Any) -> bool:
    if isinstance(value, list):
        return any(_contains_response_function_call(item) for item in value)
    if not isinstance(value, Mapping):
        return False
    if value.get("type") == "function_call":
        return True
    tool_calls = value.get("tool_calls")
    if isinstance(tool_calls, list) and tool_calls:
        return True
    return any(_contains_response_function_call(item) for item in value.values())


def _response_output_is_text_or_empty(output: Any) -> bool:
    if output is None:
        return True
    if not isinstance(output, list):
        return False
    for item in output:
        if not isinstance(item, Mapping):
            return False
        item_type = item.get("type")
        if item_type not in {"message", "reasoning"}:
            return False
    return True


def _response_events_are_text_or_empty(events: list[Mapping[str, Any]]) -> bool:
    for event in events:
        event_type = event.get("type")
        if event_type in {"_response.output_item.added", "_response.output_item.done"}:
            item = event.get("item")
            if not isinstance(item, Mapping):
                continue
            item_type = item.get("type")
            if item_type not in {"message", "reasoning"}:
                return False
        elif event_type == "_response.completed":
            response = event.get("response")
            if isinstance(response, Mapping) and not _response_output_is_text_or_empty(_response.get("output")):
                return False
        elif event_type in {"_response.failed", "_response.incomplete", "error"}:
            return False
    return True


def _replace_embedded_model(body: bytes, model_id: str, upstream_model: str) -> bytes:
    model_token = json.dumps(model_id).encode("utf-8")
    upstream_token = json.dumps(upstream_model).encode("utf-8")

    def replace_match(match: re.Match[bytes]) -> bytes:
        prefix, token = match.group(0).split(b":", 1)
        if token.strip() == model_token:
            return prefix + b":" + upstream_token
        return match.group(0)

    return host.EMBEDDED_MODEL_RE.sub(replace_match, body)


def official_passthrough_request_body(
    body: bytes,
    payload: Mapping[str, Any] | None,
    upstream: Mapping[str, Any],
    model_id: str | None = None,
) -> bytes:
    if not isinstance(payload, Mapping):
        # Strict official passthrough has no parsed shape to safely rewrite.
        return body

    next_payload = dict(payload)
    upstream_model = upstream.get("upstream_model")
    changed = False
    if isinstance(upstream_model, str) and upstream_model and next_payload.get("model") != upstream_model:
        next_payload["model"] = upstream_model
        changed = True
    service_tier = upstream.get("service_tier")
    if isinstance(service_tier, str) and service_tier and next_payload.get("service_tier") != service_tier:
        next_payload["service_tier"] = service_tier
        changed = True
    if _response._sanitize_unsupported_compaction_input_items(next_payload):
        changed = True
    if next_payload.get("store") is not False:
        next_payload["store"] = False
        changed = True
    reasoning_changed, reasoning_counts = host._sanitize_official_input_reasoning_items(next_payload)
    if reasoning_changed:
        _gateway_events.write_proxy_event(
            "official_reasoning_history_sanitized",
            upstream="official",
            reasoning_items_removed=reasoning_counts["removed_non_portable"],
            reasoning_items_kept_official_encrypted=reasoning_counts["kept_official_encrypted"],
        )
        changed = True
    if not changed:
        return body
    return json.dumps(next_payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


def _safe_json_mapping(body: bytes) -> Mapping[str, Any] | None:
    try:
        payload = json.loads(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, Mapping) else None


def transparent_request_body(
    body: bytes,
    payload: Mapping[str, Any] | None,
    upstream: Mapping[str, Any],
    model_id: str | None = None,
) -> bytes:
    upstream_name = upstream.get("name")
    upstream_model = upstream.get("upstream_model")
    official_responses_backend = upstream_name == "official"
    upstream_is_third_party = upstream_name != "official"
    if not isinstance(upstream_model, str) or not upstream_model:
        if isinstance(payload, Mapping):
            next_payload = dict(payload)
            changed = False
            if host._normalize_responses_message_input_items(next_payload):
                changed = True
            if official_responses_backend and _response._sanitize_unsupported_compaction_input_items(next_payload):
                changed = True
            if upstream_is_third_party and _rewrite_internal_input_items(
                next_payload,
                preserve_standard_function_history=True,
            ):
                changed = True
            if official_responses_backend and "max_output_tokens" in next_payload:
                del next_payload["max_output_tokens"]
                changed = True
            if official_responses_backend and next_payload.get("store") is not False:
                next_payload["store"] = False
                changed = True
            if official_responses_backend and next_payload.get("stream") is not True:
                next_payload["stream"] = True
                changed = True
            if official_responses_backend and host._normalize_responses_string_input(next_payload):
                changed = True
            if upstream_name == "ollama_cloud" and _apply_ollama_reasoning_effort_alias(next_payload):
                changed = True
            if changed:
                return json.dumps(next_payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        return body
    if not isinstance(payload, Mapping):
        if isinstance(model_id, str) and model_id != upstream_model:
            return _replace_embedded_model(body, model_id, upstream_model)
        return body

    next_payload = dict(payload)
    changed = False
    if next_payload.get("model") != upstream_model:
        next_payload["model"] = upstream_model
        changed = True
    if official_responses_backend and "max_output_tokens" in next_payload:
        del next_payload["max_output_tokens"]
        changed = True
    if official_responses_backend and next_payload.get("store") is not False:
        next_payload["store"] = False
        changed = True
    if official_responses_backend and next_payload.get("stream") is not True:
        next_payload["stream"] = True
        changed = True
    if official_responses_backend and host._normalize_responses_string_input(next_payload):
        changed = True
    if official_responses_backend and _response._sanitize_unsupported_compaction_input_items(next_payload):
        changed = True
    if host._normalize_responses_message_input_items(next_payload):
        changed = True
    if upstream_is_third_party and _rewrite_internal_input_items(
        next_payload,
        preserve_standard_function_history=True,
    ):
        changed = True
    if upstream_name == "ollama_cloud" and _apply_ollama_reasoning_effort_alias(next_payload):
        changed = True
    if not changed:
        return body
    return json.dumps(next_payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


def _rewrite_transparent_developer_role_messages(
    body: bytes,
    upstream: Mapping[str, Any],
) -> tuple[bytes, int]:
    if upstream.get("supports_developer_role", True) is not False:
        return body, 0
    payload = _safe_json_mapping(body)
    if payload is None:
        return body, 0
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return body, 0
    next_messages: list[Any] = []
    rewritten = 0
    for message in messages:
        if isinstance(message, dict) and message.get("role") == "developer":
            message = {**message, "role": "system"}
            rewritten += 1
        next_messages.append(message)
    if not rewritten:
        return body, 0
    next_payload = dict(payload)
    next_payload["messages"] = next_messages
    return json.dumps(next_payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8"), rewritten


_TOOL_SCHEMA_MAP_KEYS = ("properties", "patternProperties", "$defs", "defs", "definitions", "dependentSchemas")


_TOOL_SCHEMA_VALUE_KEYS = (
    "items",
    "additionalItems",
    "additionalProperties",
    "contains",
    "propertyNames",
    "if",
    "then",
    "else",
    "not",
    "unevaluatedItems",
    "unevaluatedProperties",
    "contentSchema",
)


_TOOL_SCHEMA_LIST_KEYS = ("allOf", "anyOf", "oneOf", "prefixItems")


def _json_pointer_get(root: Any, pointer: str) -> Any:
    if pointer in {"#", ""}:
        return root
    if not pointer.startswith("#/"):
        return None
    node: Any = root
    for raw_part in pointer[2:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict) and part in node:
            node = node[part]
        else:
            return None
    return node


def _normalize_tool_json_schema_items(
    value: list[Any],
    state: dict[str, int],
    *,
    root: Any,
    visiting: set[str],
) -> list[Any]:
    return [
        _normalize_tool_json_schema(item, state, root=root, visiting=visiting)
        if isinstance(item, (dict, bool))
        else item
        for item in value
    ]


def _normalize_tool_json_schema(
    node: Any,
    state: dict[str, int],
    *,
    root: Any | None = None,
    visiting: set[str] | None = None,
) -> Any:
    """Normalize tool JSON Schema for third-party upstreams.

    Boolean subschemas become objects (``true`` -> ``{}``, ``false`` ->
    ``{"not": {}}``). Local ``$ref`` values are inlined; recursive refs are
    replaced with an open object so providers such as OpenCode Go do not reject
    the request with "Recursive JSON schemas are not currently supported".
    """
    if visiting is None:
        visiting = set()
    if root is None:
        root = node
    if isinstance(node, bool):
        state["rewritten"] += 1
        return {} if node else {"not": {}}
    if not isinstance(node, dict):
        return node
    next_node = dict(node)
    ref = next_node.get("$ref")
    if not isinstance(ref, str):
        ref = next_node.get("$dynamicRef")
    if isinstance(ref, str) and ref.startswith("#"):
        if ref in visiting:
            state["rewritten"] += 1
            leftover = {
                key: value
                for key, value in next_node.items()
                if key not in {"$ref", "$dynamicRef"}
            }
            return leftover or {"type": "object"}
        target = _json_pointer_get(root, ref)
        if isinstance(target, (dict, bool)):
            visiting.add(ref)
            inlined = _normalize_tool_json_schema(target, state, root=root, visiting=visiting)
            visiting.discard(ref)
            state["rewritten"] += 1
            merged = dict(inlined) if isinstance(inlined, dict) else {"type": "object"}
            for key, value in next_node.items():
                if key in {"$ref", "$dynamicRef"} or key in merged:
                    continue
                merged[key] = (
                    _normalize_tool_json_schema(value, state, root=root, visiting=visiting)
                    if isinstance(value, (dict, bool))
                    else value
                )
            return merged
    for key in _TOOL_SCHEMA_MAP_KEYS:
        value = next_node.get(key)
        if isinstance(value, dict):
            next_node[key] = {
                name: _normalize_tool_json_schema(subschema, state, root=root, visiting=visiting)
                if isinstance(subschema, (dict, bool))
                else subschema
                for name, subschema in value.items()
            }
    for key in _TOOL_SCHEMA_VALUE_KEYS:
        value = next_node.get(key)
        if isinstance(value, (dict, bool)):
            next_node[key] = _normalize_tool_json_schema(value, state, root=root, visiting=visiting)
        elif isinstance(value, list):
            next_node[key] = _normalize_tool_json_schema_items(value, state, root=root, visiting=visiting)
    for key in _TOOL_SCHEMA_LIST_KEYS:
        value = next_node.get(key)
        if isinstance(value, list):
            next_node[key] = _normalize_tool_json_schema_items(value, state, root=root, visiting=visiting)
    leftover_ref = False
    for key in ("$ref", "$dynamicRef"):
        if isinstance(next_node.get(key), str):
            leftover_ref = True
            next_node.pop(key, None)
    if leftover_ref:
        state["rewritten"] += 1
        if not next_node:
            return {"type": "object"}
    return next_node


def _rewrite_tool_entry_schemas(tool: Any, state: dict[str, int]) -> Any:
    if not isinstance(tool, dict):
        return tool
    next_tool = dict(tool)
    function = next_tool.get("function")
    if isinstance(function, dict):
        next_function = dict(function)
        for key in ("parameters", "input_schema"):
            schema = next_function.get(key)
            if isinstance(schema, (dict, bool)):
                next_function[key] = _normalize_tool_json_schema(schema, state)
        next_tool["function"] = next_function
    for key in ("parameters", "input_schema"):
        schema = next_tool.get(key)
        if isinstance(schema, (dict, bool)):
            next_tool[key] = _normalize_tool_json_schema(schema, state)
    nested = next_tool.get("tools")
    if isinstance(nested, list):
        next_tool["tools"] = [_rewrite_tool_entry_schemas(item, state) for item in nested]
    return next_tool


def _normalize_transparent_tool_schema_booleans(body: bytes) -> tuple[bytes, int]:
    payload = _safe_json_mapping(body)
    if payload is None:
        return body, 0
    tools = payload.get("tools")
    if not isinstance(tools, list):
        return body, 0
    state = {"rewritten": 0}
    next_tools = [_rewrite_tool_entry_schemas(tool, state) for tool in tools]
    if not state["rewritten"]:
        return body, 0
    next_payload = dict(payload)
    next_payload["tools"] = next_tools
    return json.dumps(next_payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8"), state["rewritten"]


def _is_raw_provider_probe_context(event_context: Mapping[str, Any] | None) -> bool:
    return bool((event_context or {}).get("raw_provider_probe"))


def _apply_ollama_reasoning_effort_alias(payload: dict[str, Any]) -> bool:
    reasoning = payload.get("reasoning")
    if isinstance(reasoning, dict):
        effort = reasoning.get("effort")
        replacement = host.OLLAMA_REASONING_EFFORT_ALIASES.get(effort) if isinstance(effort, str) else None
        if replacement is not None:
            reasoning["effort"] = replacement
            return True
        return False
    replacement = host.OLLAMA_REASONING_EFFORT_ALIASES.get(reasoning) if isinstance(reasoning, str) else None
    if replacement is not None:
        payload["reasoning"] = replacement
        return True
    return False
