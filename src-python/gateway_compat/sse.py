"""Gateway compatibility application pipeline."""

from __future__ import annotations

from collections.abc import Iterable as IterableABC
from pathlib import Path
from typing import Any, Iterable, Mapping, NoReturn

import hashlib
import json
import re
import uuid

from apply_patch_adapter import (
    ApplyPatchAdapter,
    ApplyPatchFacts,
    _ThirdPartyApplyPatchStreamAdapter as _ApplyPatchStreamAdapterImpl,
)
from collaboration_adapter import (
    CollaborationAdapter,
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
from gateway_sse import _sse_line_ending, _sse_payload_bytes
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
    ToolSurfaceAdapter,
    ToolSurfaceFacts,
)
from route_plan import (
    NATIVE_RESPONSES_TOOL_CODEC_ERROR_CODE,
    TOOL_SURFACE_STRATEGY_ERROR_CODE,
    _external_native_responses_tool_codec,
    _external_tool_protocol,
    _external_tool_surface_strategy,
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
    host._collaboration_adapter().remember_stream_item(state, item, terminal=terminal)


def _remember_worker_stream_event(
    value: Mapping[str, Any],
    event_context: Mapping[str, Any] | None,
) -> None:
    host._collaboration_adapter().remember_stream_event(value, event_context)


def _raise_on_invalid_worker_stream_event(
    value: Mapping[str, Any],
    event_context: Mapping[str, Any] | None,
    *,
    surface: str,
) -> None:
    host._collaboration_adapter().raise_on_invalid_stream_event(
        value,
        event_context,
        surface=surface,
    )


def _reconcile_function_call_argument_events(events: list[Mapping[str, Any]]) -> tuple[list[Mapping[str, Any]], bool]:
    arguments_by_item_id: dict[str, str] = {}

    def remember_item(item: Any) -> None:
        if not isinstance(item, Mapping) or item.get("type") != "function_call":
            return
        item_id = item.get("id")
        if not isinstance(item_id, str) or not item_id:
            return
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
        if event.get("type") == "response.completed":
            response = event.get("response")
            output = response.get("output") if isinstance(response, Mapping) else None
            if isinstance(output, list):
                for item in output:
                    remember_item(item)

    changed = False
    rewritten: list[Mapping[str, Any]] = []
    for event in events:
        if isinstance(event, Mapping) and event.get("type") == "response.function_call_arguments.delta":
            changed = True
            continue
        if not isinstance(event, Mapping) or event.get("type") != "response.function_call_arguments.done":
            rewritten.append(event)
            continue
        item_id = event.get("item_id")
        if not isinstance(item_id, str) or item_id not in arguments_by_item_id:
            changed = True
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


def _required_subagent_call_spec(event_context: Mapping[str, Any] | None) -> dict[str, Any] | None:
    context = event_context or {}
    if _official_passthrough._is_raw_provider_probe_context(context):
        return None
    tool_protocol = str(context.get("tool_protocol") or "")
    if tool_protocol not in {"text_compat", "chat_tools", "responses_structured"}:
        return None
    if bool(context.get("subagent_lifecycle_complete")):
        return None

    subagent_state = context.get("_subagent_state")
    state_next_action = getattr(subagent_state, "next_action", None)
    if state_next_action is not None and state_next_action not in {"spawn", "wait", "close", "send_input"}:
        return None
    if bool(context.get("subagent_spawn_allowed")) and state_next_action not in {"spawn", "wait", "close", "send_input"}:
        return None

    legal_actions = context.get("subagent_legal_actions")
    if isinstance(legal_actions, list):
        action = deterministic_required_action([item for item in legal_actions if isinstance(item, Mapping)])
        if action is None:
            return None
        tool_name = action.get("tool_name")
        arguments = action.get("arguments")
        if isinstance(tool_name, str) and isinstance(arguments, Mapping):
            agent_ids = action.get("agent_ids")
            return {
                "tool_name": tool_name,
                "agent_ids": _official_passthrough._string_list(agent_ids) if isinstance(agent_ids, list) else [],
                "arguments": dict(arguments),
            }

    close_agent_ids = _official_passthrough._string_list(context.get("subagent_close_agent_ids"))
    wait_agent_ids = _official_passthrough._string_list(context.get("subagent_wait_agent_ids"))
    if state_next_action == "spawn":
        arguments = context.get("subagent_required_spawn_arguments")
        if isinstance(arguments, Mapping) and isinstance(arguments.get("message"), str) and arguments.get("message"):
            return {
                "tool_name": "spawn_agent",
                "agent_ids": [],
                "arguments": dict(arguments),
            }
    if state_next_action == "send_input":
        target = getattr(subagent_state, "send_input_target", None)
        if isinstance(target, str) and target:
            return {
                "tool_name": "send_input",
                "agent_ids": [target],
                "arguments": {
                    "target": target,
                    "message": _required_subagent_send_input_message(subagent_state, target),
                },
            }
    if state_next_action == "close" and close_agent_ids:
        return {"tool_name": "close_agent", "agent_ids": close_agent_ids, "arguments": {"target": close_agent_ids[0]}}
    if state_next_action == "wait" and wait_agent_ids:
        return {
            "tool_name": "wait_agent",
            "agent_ids": wait_agent_ids,
            "arguments": {"targets": wait_agent_ids, "timeout_ms": 60000},
        }
    if close_agent_ids:
        return {"tool_name": "close_agent", "agent_ids": close_agent_ids, "arguments": {"target": close_agent_ids[0]}}
    if wait_agent_ids:
        return {
            "tool_name": "wait_agent",
            "agent_ids": wait_agent_ids,
            "arguments": {"targets": wait_agent_ids, "timeout_ms": 60000},
        }
    return None


def _required_subagent_send_input_message(subagent_state: Any, target: str) -> str:
    agent = getattr(subagent_state, "agents", {}).get(target) if subagent_state is not None else None
    prompt = getattr(agent, "prompt", "") if agent is not None else ""
    if isinstance(prompt, str) and prompt.strip():
        return (
            "Your previous completed result had empty visible output. "
            "Return exactly the output requested in your original prompt, with no prose or markdown.\n"
            f"Original prompt:\n{prompt.strip()}"
        )
    return (
        "Your previous completed result had empty visible output. "
        "Return the exact output requested in your original prompt, with no prose or markdown."
    )


def _required_subagent_call_item(spec: Mapping[str, Any], call_id: str | None = None) -> dict[str, Any]:
    tool_name = spec.get("tool_name")
    if not isinstance(tool_name, str) or tool_name not in host.MULTI_AGENT_TOOL_NAMES:
        tool_name = "wait_agent"
    arguments = spec.get("arguments")
    if not isinstance(arguments, Mapping):
        arguments = {}
    call_id = call_id or f"call_codexhub_required_{tool_name}_{uuid.uuid4().hex[:12]}"
    return {
        "id": f"fc_{call_id}",
        "type": "function_call",
        "status": "completed",
        "call_id": call_id,
        "namespace": "multi_agent_v1",
        "name": tool_name,
        "arguments": json.dumps(dict(arguments), ensure_ascii=True, separators=(",", ":")),
    }


def _with_preserved_spawn_agent_type(
    arguments: Mapping[str, Any],
    original_arguments: Mapping[str, Any] | None,
) -> dict[str, Any]:
    rewritten = dict(arguments)
    agent_type = original_arguments.get("agent_type") if original_arguments is not None else None
    if agent_type in {"worker", "general"}:
        rewritten["agent_type"] = agent_type
    return rewritten


def _required_subagent_call_item_like(spec: Mapping[str, Any], value: Mapping[str, Any]) -> dict[str, Any]:
    if spec.get("tool_name") == "spawn_agent":
        original_arguments = _official_passthrough._json_object_from_arguments(value.get("arguments"))
        spec = dict(spec)
        required_arguments = spec.get("arguments")
        spec["arguments"] = _with_preserved_spawn_agent_type(
            dict(required_arguments) if isinstance(required_arguments, Mapping) else {},
            original_arguments,
        )
    call_id = value.get("call_id")
    item = _required_subagent_call_item(spec, call_id=call_id if isinstance(call_id, str) and call_id else None)
    item_id = value.get("id")
    if isinstance(item_id, str) and item_id:
        item["id"] = item_id
    status = value.get("status")
    if isinstance(status, str) and status:
        item["status"] = status
    if item.get("status") == "in_progress":
        item["arguments"] = ""
    return item


def _validate_generated_required_spawn_call(
    value: Mapping[str, Any],
    event_context: Mapping[str, Any] | None,
    *,
    surface: str,
    validated_call_ids: set[str],
) -> None:
    raw_arguments = value.get("arguments")
    if (
        _multi_agent._multi_agent_function_call_name(value) != "spawn_agent"
        or raw_arguments in (None, "")
        or _official_passthrough._json_object_from_arguments(raw_arguments) is None
    ):
        return
    identities = [identity for identity in (value.get("call_id"), value.get("id")) if isinstance(identity, str)]
    if any(identity in validated_call_ids for identity in identities):
        return
    _multi_agent._validate_external_worker_selectors(value, event_context, surface=surface)
    validated_call_ids.update(identity for identity in identities if identity)


def _coerce_required_subagent_tool_calls(
    value: Any,
    event_context: Mapping[str, Any] | None,
    *,
    surface: str = "coerce",
) -> tuple[Any, bool]:
    if not subagent_semantic_repair_enabled(event_context):
        return value, False

    spec = _required_subagent_call_spec(event_context)
    if spec is None:
        return value, False
    if spec.get("tool_name") == "spawn_agent":
        prompts = (event_context or {}).get("subagent_exact_spawn_prompts")
        if isinstance(prompts, list) and len([prompt for prompt in prompts if isinstance(prompt, str) and prompt]) > 1:
            return value, False
    coerced_item_ids: set[str]
    if isinstance(event_context, dict):
        stored = event_context.setdefault("_required_subagent_coerced_item_ids", set())
        coerced_item_ids = stored if isinstance(stored, set) else set()
        event_context["_required_subagent_coerced_item_ids"] = coerced_item_ids
        stored_generated = event_context.setdefault("_required_subagent_generated_spawn_item_ids", set())
        generated_spawn_item_ids = stored_generated if isinstance(stored_generated, set) else set()
        event_context["_required_subagent_generated_spawn_item_ids"] = generated_spawn_item_ids
        stored_validated = event_context.setdefault("_required_subagent_validated_generated_spawn_ids", set())
        validated_generated_spawn_ids = stored_validated if isinstance(stored_validated, set) else set()
        event_context["_required_subagent_validated_generated_spawn_ids"] = validated_generated_spawn_ids
    else:
        coerced_item_ids = set()
        generated_spawn_item_ids = set()
        validated_generated_spawn_ids = set()
    rewritten, changed = _coerce_required_subagent_tool_calls_inner(
        value,
        spec,
        coerced_item_ids,
        generated_spawn_item_ids,
        event_context,
        surface,
        validated_generated_spawn_ids,
    )
    if changed:
        _write_required_subagent_repair_event(event_context, spec, surface="coerce")
    return rewritten, changed


def _coerce_exact_spawn_prompt_tool_calls(
    value: Any,
    event_context: Mapping[str, Any] | None,
) -> tuple[Any, bool]:
    if not subagent_semantic_repair_enabled(event_context):
        return value, False
    context = event_context or {}
    prompts = context.get("subagent_exact_spawn_prompts")
    if not isinstance(prompts, list):
        return value, False
    exact_prompts = [prompt for prompt in prompts if isinstance(prompt, str) and prompt]
    if not exact_prompts:
        return value, False
    try:
        offset = int(context.get("subagent_exact_spawn_offset") or 0)
    except (TypeError, ValueError):
        offset = 0
    specs = [{"message": prompt, "fork_context": False} for prompt in exact_prompts[max(0, offset) :]]
    if not specs:
        return value, False
    if isinstance(event_context, dict):
        state_key = "_exact_spawn_prompt_coerce_state"
        stored_state = event_context.get(state_key)
        if not isinstance(stored_state, dict):
            stored_state = {}
            event_context[state_key] = stored_state
        signature = {"prompts": exact_prompts, "offset": max(0, offset)}
        if stored_state.get("signature") != signature:
            stored_state.clear()
            stored_state["signature"] = signature
            stored_state["next_index"] = 0
            stored_state["arguments_by_item_id"] = {}
        state = stored_state
    else:
        state = {"next_index": 0, "arguments_by_item_id": {}}
    rewritten, changed = _coerce_exact_spawn_prompt_tool_calls_inner(value, specs, state)
    if changed:
        _write_required_subagent_repair_event(
            event_context,
            {"tool_name": "spawn_agent", "agent_ids": []},
            surface="exact_prompt_coerce",
        )
    return rewritten, changed


def _coerce_exact_spawn_prompt_tool_calls_inner(
    value: Any,
    specs: list[Mapping[str, Any]],
    state: dict[str, Any],
) -> tuple[Any, bool]:
    if isinstance(value, list):
        changed = False
        rewritten = []
        for item in value:
            replacement, item_changed = _coerce_exact_spawn_prompt_tool_calls_inner(item, specs, state)
            rewritten.append(replacement)
            changed = changed or item_changed
        return (rewritten if changed else value), changed

    if not isinstance(value, dict):
        return value, False

    if value.get("type") == "response.function_call_arguments.done":
        item_id = value.get("item_id")
        arguments_by_item_id = state.get("arguments_by_item_id")
        if not isinstance(item_id, str) or not isinstance(arguments_by_item_id, dict):
            return value, False
        expected = arguments_by_item_id.get(item_id)
        if not isinstance(expected, str):
            return value, False
        original_arguments = _official_passthrough._json_object_from_arguments(value.get("arguments"))
        expected_arguments = _official_passthrough._json_object_from_arguments(expected)
        if expected_arguments is not None:
            expected_arguments = _with_preserved_spawn_agent_type(expected_arguments, original_arguments)
            expected = json.dumps(expected_arguments, ensure_ascii=True, separators=(",", ":"))
            arguments_by_item_id[item_id] = expected
        if value.get("arguments") == expected:
            return value, False
        rewritten = dict(value)
        rewritten["arguments"] = expected
        return rewritten, True

    if _multi_agent._is_multi_agent_spawn_function_call(value):
        item_id = value.get("id")
        arguments_by_item_id = state.setdefault("arguments_by_item_id", {})
        expected_arguments: Mapping[str, Any] | None = None
        if isinstance(item_id, str) and isinstance(arguments_by_item_id, dict):
            stored = arguments_by_item_id.get(item_id)
            if isinstance(stored, str):
                parsed = _official_passthrough._json_object_from_arguments(stored)
                if parsed is not None:
                    expected_arguments = parsed
        if expected_arguments is None:
            next_index = int(state.get("next_index") or 0)
            if next_index >= len(specs):
                return value, False
            expected_arguments = specs[next_index]
            state["next_index"] = next_index + 1
        original_arguments = _official_passthrough._json_object_from_arguments(value.get("arguments"))
        expected_arguments = _with_preserved_spawn_agent_type(expected_arguments, original_arguments)
        expected_json = json.dumps(dict(expected_arguments), ensure_ascii=True, separators=(",", ":"))
        if isinstance(item_id, str) and isinstance(arguments_by_item_id, dict):
            arguments_by_item_id[item_id] = expected_json
        rewritten = dict(value)
        rewritten["namespace"] = "multi_agent_v1"
        rewritten["name"] = "spawn_agent"
        if rewritten.get("status") == "in_progress":
            rewritten["arguments"] = ""
        else:
            rewritten["arguments"] = _official_passthrough._dump_arguments_like(value.get("arguments"), expected_arguments)
        return (rewritten, True) if rewritten != value else (value, False)

    changed = False
    rewritten = dict(value)
    for key, item in value.items():
        replacement, item_changed = _coerce_exact_spawn_prompt_tool_calls_inner(item, specs, state)
        if item_changed:
            rewritten[key] = replacement
            changed = True
    return (rewritten if changed else value), changed


def _coerce_required_subagent_tool_calls_inner(
    value: Any,
    spec: Mapping[str, Any],
    coerced_item_ids: set[str],
    generated_spawn_item_ids: set[str],
    event_context: Mapping[str, Any] | None,
    surface: str,
    validated_generated_spawn_ids: set[str],
) -> tuple[Any, bool]:
    if isinstance(value, list):
        changed = False
        rewritten = []
        for item in value:
            replacement, item_changed = _coerce_required_subagent_tool_calls_inner(
                item,
                spec,
                coerced_item_ids,
                generated_spawn_item_ids,
                event_context,
                surface,
                validated_generated_spawn_ids,
            )
            rewritten.append(replacement)
            changed = changed or item_changed
        return (rewritten if changed else value), changed

    if not isinstance(value, dict):
        return value, False

    if value.get("type") == "response.function_call_arguments.done":
        item_id = value.get("item_id")
        if not isinstance(item_id, str) or item_id not in coerced_item_ids:
            return value, False
        arguments = dict(spec.get("arguments")) if isinstance(spec.get("arguments"), Mapping) else {}
        original_arguments = _official_passthrough._json_object_from_arguments(value.get("arguments"))
        if spec.get("tool_name") == "spawn_agent" and original_arguments is not None:
            arguments = _with_preserved_spawn_agent_type(arguments, original_arguments)
        expected = json.dumps(dict(arguments), ensure_ascii=True, separators=(",", ":"))
        if spec.get("tool_name") == "spawn_agent" and item_id in generated_spawn_item_ids:
            _validate_generated_required_spawn_call(
                {
                    "type": "function_call",
                    "id": item_id,
                    "namespace": "multi_agent_v1",
                    "name": "spawn_agent",
                    "arguments": expected,
                },
                event_context,
                surface=surface,
                validated_call_ids=validated_generated_spawn_ids,
            )
        if value.get("arguments") != expected:
            rewritten = dict(value)
            rewritten["arguments"] = expected
            return rewritten, True
        return value, False

    original_tool_name = _multi_agent._multi_agent_function_call_name(value)
    if original_tool_name is not None:
        replacement = _required_subagent_call_item_like(spec, value)
        item_id = replacement.get("id")
        if isinstance(item_id, str) and item_id:
            coerced_item_ids.add(item_id)
        if original_tool_name != "spawn_agent" and _multi_agent._multi_agent_function_call_name(replacement) == "spawn_agent":
            if isinstance(item_id, str) and item_id:
                generated_spawn_item_ids.add(item_id)
            _validate_generated_required_spawn_call(
                replacement,
                event_context,
                surface=surface,
                validated_call_ids=validated_generated_spawn_ids,
            )
        return (replacement, True) if replacement != value else (value, False)

    changed = False
    rewritten = dict(value)
    for key, item in value.items():
        replacement, item_changed = _coerce_required_subagent_tool_calls_inner(
            item,
            spec,
            coerced_item_ids,
            generated_spawn_item_ids,
            event_context,
            surface,
            validated_generated_spawn_ids,
        )
        if item_changed:
            rewritten[key] = replacement
            changed = True
    return (rewritten if changed else value), changed


def _required_subagent_call_events(
    spec: Mapping[str, Any],
    response: Mapping[str, Any] | None = None,
    *,
    output_index: int = 0,
) -> list[dict[str, Any]]:
    response_obj = dict(response) if isinstance(response, Mapping) else {}
    call_id = f"call_codexhub_required_{spec.get('tool_name')}_{uuid.uuid4().hex[:12]}"
    item = _required_subagent_call_item(spec, call_id=call_id)
    in_progress_item = dict(item)
    in_progress_item["status"] = "in_progress"
    in_progress_item["arguments"] = ""
    completed_response = {
        "id": response_obj.get("id") if isinstance(response_obj.get("id"), str) else f"resp_{uuid.uuid4().hex[:12]}",
        "object": "response",
        "status": "completed",
        "model": response_obj.get("model"),
        "output": [item],
    }
    usage = response_obj.get("usage")
    if isinstance(usage, Mapping):
        completed_response["usage"] = dict(usage)
    return [
        {
            "type": "response.output_item.added",
            "output_index": output_index,
            "item": in_progress_item,
        },
        {
            "type": "response.function_call_arguments.done",
            "item_id": item["id"],
            "output_index": output_index,
            "arguments": item["arguments"],
        },
        {
            "type": "response.output_item.done",
            "output_index": output_index,
            "item": item,
        },
        {
            "type": "response.completed",
            "response": completed_response,
        },
    ]


def _write_required_subagent_repair_event(
    event_context: Mapping[str, Any] | None,
    spec: Mapping[str, Any],
    *,
    surface: str,
) -> None:
    host._write_adapter_event(
        event_context,
        "required_subagent_call_repaired",
        surface=surface,
        tool=spec.get("tool_name") if isinstance(spec.get("tool_name"), str) else None,
        agent_ids=spec.get("agent_ids") if isinstance(spec.get("agent_ids"), list) else None,
    )


def _reject_missing_worker_selector_for_generated_call(
    spec: Mapping[str, Any],
    event_context: Mapping[str, Any] | None,
    *,
    surface: str,
) -> None:
    host._collaboration_adapter().reject_missing_worker_selector_for_generated_call(
        spec,
        event_context,
        surface=surface,
    )


def _repair_missing_required_subagent_call_payload(
    payload: Any,
    event_context: Mapping[str, Any] | None,
) -> tuple[Any, bool]:
    if not subagent_semantic_repair_enabled(event_context):
        return payload, False

    if not isinstance(payload, dict):
        return payload, False
    spec = _required_subagent_call_spec(event_context)
    if spec is None:
        return payload, False
    if _official_passthrough._contains_response_function_call(payload):
        return payload, False
    if "error" in payload or not _official_passthrough._response_output_is_text_or_empty(payload.get("output")):
        return payload, False

    _reject_missing_worker_selector_for_generated_call(spec, event_context, surface="body")
    rewritten = dict(payload)
    rewritten["status"] = "completed"
    rewritten["output"] = [_required_subagent_call_item(spec)]
    _write_required_subagent_repair_event(event_context, spec, surface="body")
    return rewritten, True


def _repair_missing_required_subagent_call_events(
    events: list[Mapping[str, Any]],
    event_context: Mapping[str, Any] | None,
) -> tuple[list[Mapping[str, Any]], bool]:
    if not subagent_semantic_repair_enabled(event_context):
        return events, False

    spec = _required_subagent_call_spec(event_context)
    if spec is None:
        return events, False
    if _official_passthrough._contains_response_function_call(events) or not _official_passthrough._response_events_are_text_or_empty(events):
        return events, False

    completed_response: Mapping[str, Any] | None = None
    for event in events:
        if event.get("type") == "response.completed":
            response = event.get("response")
            completed_response = response if isinstance(response, Mapping) else {}
    if completed_response is None:
        return events, False

    _reject_missing_worker_selector_for_generated_call(spec, event_context, surface="events")
    prefix = [
        dict(event)
        for event in events
        if event.get("type") in {"response.created", "response.in_progress", "response.queued"}
    ]
    repaired = prefix + _required_subagent_call_events(spec, completed_response, output_index=0)
    _write_required_subagent_repair_event(event_context, spec, surface="events")
    return repaired, True


def _repair_missing_required_subagent_call_sse_line(
    payload: Mapping[str, Any],
    event_context: Mapping[str, Any] | None,
    line_ending: bytes,
) -> bytes | None:
    if not subagent_semantic_repair_enabled(event_context):
        return None

    if payload.get("type") != "response.completed":
        return None
    spec = _required_subagent_call_spec(event_context)
    if spec is None:
        return None
    if _official_passthrough._contains_response_function_call(payload):
        return None
    response = payload.get("response")
    response_obj = response if isinstance(response, Mapping) else {}
    if not _official_passthrough._response_output_is_text_or_empty(response_obj.get("output")):
        return None
    _reject_missing_worker_selector_for_generated_call(spec, event_context, surface="sse")
    output = response_obj.get("output")
    output_index = len(output) if isinstance(output, list) else 0
    events = _required_subagent_call_events(spec, response_obj, output_index=output_index)
    _write_required_subagent_repair_event(event_context, spec, surface="sse")
    return b"".join(host._sse_json_line(event, line_ending) + line_ending for event in events)


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
        payload = json.loads(payload_bytes.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return line

    collaboration_protocol = host._resolve_collaboration_boundary(
        payload,
        event_context,
        surface="stream",
    )
    if collaboration_protocol is None and isinstance(event_context, Mapping):
        selected_protocol = event_context.get("collaboration_protocol")
        if selected_protocol in {_COLLABORATION_V1, _COLLABORATION_V2}:
            collaboration_protocol = selected_protocol
    event_context = host._collaboration_context_with_protocol(event_context, collaboration_protocol)
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
        host._write_adapter_event(
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
    payload, post_final_multi_agent_changed = _multi_agent._suppress_multi_agent_calls_after_lifecycle_final(
        payload,
        event_context,
    )
    if payload is None:
        return b""
    changed = changed or post_final_multi_agent_changed
    payload, worker_multi_agent_changed = _multi_agent._suppress_worker_multi_agent_tool_calls(payload, event_context)
    if payload is None:
        return b""
    changed = changed or worker_multi_agent_changed
    payload, coordinator_forbidden_changed = _multi_agent._suppress_coordinator_forbidden_tool_calls(payload, event_context)
    if payload is None:
        return b""
    changed = changed or coordinator_forbidden_changed
    payload, invalid_tool_changed = _official_passthrough._downgrade_invalid_third_party_tool_calls(payload, runtime_tool_plan)
    changed = changed or invalid_tool_changed
    payload, duplicate_spawn_changed = _multi_agent._guard_duplicate_multi_agent_spawn_calls(payload, event_context)
    changed = changed or duplicate_spawn_changed
    payload, exact_spawn_changed = _coerce_exact_spawn_prompt_tool_calls(payload, event_context)
    changed = changed or exact_spawn_changed
    payload, required_tool_changed = _coerce_required_subagent_tool_calls(
        payload,
        event_context,
        surface="sse",
    )
    changed = changed or required_tool_changed
    payload, requested_binding_changed = _multi_agent._apply_external_worker_response_contract(
        payload,
        event_context,
        surface="sse",
        validate_selectors=False,
        capture_stream_event=False,
    )
    changed = changed or requested_binding_changed
    repaired_line = _repair_missing_required_subagent_call_sse_line(payload, event_context, line_ending)
    if repaired_line is not None:
        return repaired_line
    if not changed:
        return line
    return host._sse_json_line(payload, line_ending)
