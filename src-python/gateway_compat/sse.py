"""Gateway compatibility application pipeline."""

from __future__ import annotations

from collections.abc import Iterable as IterableABC
from pathlib import Path
from typing import Any, Iterable, Mapping, NoReturn

import hashlib
import json
import re
import uuid

import collaboration_adapter as _collaboration_adapter_module
import gateway_events as _gateway_events

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
from . import official_passthrough as _official_passthrough
from . import host

def _remember_worker_stream_item(
    state: dict[str, Any],
    item: Any,
    *,
    terminal: bool = False,
) -> None:
    _collaboration_adapter_module.remember_stream_item(state, item, terminal=terminal)


def _remember_worker_stream_event(
    value: Mapping[str, Any],
    event_context: Mapping[str, Any] | None,
) -> None:
    _collaboration_adapter_module.remember_stream_event(value, event_context)


def _raise_on_invalid_worker_stream_event(
    value: Mapping[str, Any],
    event_context: Mapping[str, Any] | None,
    *,
    surface: str,
) -> None:
    _collaboration_adapter_module.raise_on_invalid_stream_event(
        value,
        event_context,
        surface=surface,
    )


def reconcile_function_call_argument_events(
    events: list[Mapping[str, Any]],
    *,
    runtime_tool_plan: RuntimeToolCompatibilityPlan | None = None,
) -> tuple[list[Mapping[str, Any]], bool]:
    """Validate adapted Collaboration argument events without dropping deltas.

    Chat-to-Responses conversion already emits progressive argument deltas.
    The old reconciliation pass discarded those deltas and kept only ``done``;
    that made the SSE path observably different from the body/native path.  A
    request-scoped plan lets this check touch only registered Collaboration
    aliases, leaving provider-owned functions untouched.
    """
    arguments_by_item_id: dict[str, str] = {}
    collaboration_item_ids: set[str] = set()
    delta_arguments_by_item_id: dict[str, list[str]] = {}

    def collaboration_record(item: Any) -> Any | None:
        if runtime_tool_plan is None or not isinstance(item, Mapping):
            return None
        record = runtime_tool_plan.registry.record_for_alias(item.get("name"))
        if record is None:
            return None
        if record.family != "namespace" or record.version not in {"v1", "v2"}:
            return None
        return record

    def remember_item(item: Any) -> None:
        if not isinstance(item, Mapping) or item.get("type") != "function_call":
            return
        if collaboration_record(item) is None:
            return
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id:
            return
        collaboration_item_ids.add(item_id)
        arguments = item.get("arguments")
        if isinstance(arguments, str):
            arguments_text = arguments
        elif isinstance(arguments, Mapping):
            arguments_text = json.dumps(arguments, ensure_ascii=True, separators=(",", ":"))
        else:
            arguments_text = ""
        if arguments_text or item_id not in arguments_by_item_id:
            arguments_by_item_id[item_id] = arguments_text

    for event in events:
        if not isinstance(event, Mapping):
            continue
        if event.get("type") in {"response.output_item.added", "response.output_item.done"}:
            remember_item(event.get("item"))
            continue
        if event.get("type") == "response.function_call_arguments.delta":
            item_id = event.get("item_id")
            if isinstance(item_id, str) and item_id in collaboration_item_ids:
                delta = event.get("delta")
                if isinstance(delta, str):
                    delta_arguments_by_item_id.setdefault(item_id, []).append(delta)
            continue
        if event.get("type") == "response.completed":
            response = event.get("response")
            output = response.get("output") if isinstance(response, Mapping) else None
            if isinstance(output, list):
                for item in output:
                    remember_item(item)

    # A completed item is the authoritative snapshot.  When deltas are
    # present, their concatenation must agree with that snapshot before the
    # executable completion event is allowed through.
    for item_id, fragments in delta_arguments_by_item_id.items():
        expected = arguments_by_item_id.get(item_id)
        if expected is not None and "".join(fragments) != expected:
            raise UpstreamProtocolTranslationError(
                RuntimeToolCompatibilityError(
                    "tool_compatibility_boundary",
                    "arguments_event_mismatch",
                    surface="stream",
                )
            )

    changed = False
    rewritten: list[Mapping[str, Any]] = []
    for event in events:
        if not isinstance(event, Mapping) or event.get("type") != "response.function_call_arguments.done":
            rewritten.append(event)
            continue
        item_id = event.get("item_id")
        if not isinstance(item_id, str) or item_id not in collaboration_item_ids:
            rewritten.append(event)
            continue
        expected_arguments = arguments_by_item_id[item_id]
        if event.get("arguments") != expected_arguments:
            replacement = dict(event)
            replacement["arguments"] = expected_arguments
            rewritten.append(replacement)
            changed = True
            continue
        rewritten.append(event)
    return (rewritten if changed else events), changed


def compatible_sse_line(
    line: bytes,
    upstream_name: str,
    event_context: Mapping[str, Any] | None = None,
    *,
    runtime_tool_inverse_only: bool = False,
) -> bytes:
    if upstream_name == "official" or _official_passthrough._is_raw_provider_probe_context(event_context) or not line.startswith(b"data:"):
        return line

    line_ending = _sse_line_ending(line)
    payload_bytes = _sse_payload_bytes(line)
    if payload_bytes is None:
        return line

    try:
        payload = _collaboration_adapter_module.decode_adapted_json(payload_bytes.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return line

    collaboration_protocol = _collaboration_adapter_module.resolve_boundary(
        payload,
        event_context,
        surface="stream",
    )
    if collaboration_protocol is None and isinstance(event_context, Mapping):
        selected_protocol = event_context.get("collaboration_protocol")
        if selected_protocol in {_COLLABORATION_V1, _COLLABORATION_V2}:
            collaboration_protocol = selected_protocol
    event_context = _collaboration_adapter_module.context_with_protocol(event_context, collaboration_protocol)
    if not runtime_tool_inverse_only:
        if collaboration_protocol != _COLLABORATION_V2:
            _remember_worker_stream_event(payload, event_context)
        _raise_on_invalid_worker_stream_event(
            payload,
            event_context,
            surface="sse",
        )

    runtime_tool_plan, stream_state = _official_passthrough._runtime_tool_compatibility_stream_for_attempt(
        event_context
    )
    if runtime_tool_plan is not None and stream_state is not None:
        wire_event = payload
        try:
            decoded_events = stream_state.decode_events_for_event(payload)
        except RuntimeToolCompatibilityError as exc:
            _official_passthrough._raise_runtime_tool_compatibility_error(exc)
        _official_passthrough._write_runtime_tool_adapter_response_evidence(
            runtime_tool_plan,
            wire_event,
            decoded_events,
            event_context,
            surface="sse",
        )
        if not decoded_events:
            return b""
        if len(decoded_events) > 1:
            return b"".join(
                host._sse_json_line(event, line_ending) + line_ending
                for event in decoded_events
            )
        decoded_payload = decoded_events[0]
        runtime_tool_changed = decoded_payload != payload
        payload = decoded_payload
    else:
        runtime_tool_changed = False

    if runtime_tool_inverse_only:
        if not runtime_tool_changed:
            return line
        return host._sse_json_line(payload, line_ending) + line_ending

    if host._is_raw_reasoning_stream_event(payload):
        return b""

    changed = host._hide_reasoning_text(payload) or runtime_tool_changed
    payload, _ = _multi_agent._apply_external_worker_response_contract(
        payload,
        event_context,
        surface="sse",
        attach_sidecars=False,
    )
    payload, alias_changed = _official_passthrough._normalize_third_party_tool_call(payload, event_context, runtime_tool_plan)
    if alias_changed:
        _gateway_events.write_adapter_event(
            event_context,
            "third_party_tool_call_alias_normalized",
            upstream=upstream_name,
            surface="sse",
        )
    changed = changed or alias_changed
    payload, bounded_tool_search_changed = _multi_agent._suppress_bounded_tool_search_calls(payload, event_context)
    if payload is None:
        return b""
    changed = changed or bounded_tool_search_changed
    payload, invalid_tool_changed = _official_passthrough._downgrade_invalid_third_party_tool_calls(payload, runtime_tool_plan)
    changed = changed or invalid_tool_changed
    payload, requested_binding_changed = _multi_agent._apply_external_worker_response_contract(
        payload,
        event_context,
        surface="sse",
        validate_selectors=False,
        capture_stream_event=False,
    )
    changed = changed or requested_binding_changed
    if not changed:
        return line
    return host._sse_json_line(payload, line_ending)
