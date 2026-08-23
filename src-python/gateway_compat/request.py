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
    ThirdPartyApplyPatchStreamAdapter as _ApplyPatchStreamAdapterImpl,
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
    ToolSurfaceAdapter,
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
from . import response as _response
from . import host

def compatible_request_body(
    body: bytes,
    upstream: Mapping[str, Any],
    model_id: str | None = None,
    event_context: Mapping[str, Any] | None = None,
    inject_codex_tools: bool = True,
    behavior_profile: str = BEHAVIOR_EXTERNAL_PROVIDER_GATEWAY,
    tool_protocol_override: str | None = None,
    tool_surface_strategy_override: str | None = None,
    native_responses_tool_codec_override: str | None = None,
) -> bytes:
    upstream_name = upstream.get("name")
    official_passthrough = behavior_profile == BEHAVIOR_OFFICIAL_CODEX_APP_HTTP_PASSTHROUGH
    validated_tool_surface_strategy: str | None = None
    if (
        not official_passthrough
        and upstream_name != "official"
    ):
        # Reject malformed configuration before an unparsable external body can
        # bypass the third-party compatibility boundary. Official passthrough
        # never consults the external capability.
        validated_tool_surface_strategy = (
            tool_surface_strategy_override
            if tool_surface_strategy_override is not None
            else _external_tool_surface_strategy(upstream)
        )
    try:
        payload = json.loads(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        if official_passthrough:
            return body
        upstream_model = upstream.get("upstream_model")
        if isinstance(model_id, str) and isinstance(upstream_model, str) and upstream_model and model_id != upstream_model:
            return _official_passthrough._replace_embedded_model(body, model_id, upstream_model)
        return body

    if not isinstance(payload, dict):
        return body

    upstream_model = upstream.get("upstream_model")
    requested_model = payload.get("model")
    requested_reasoning = _official_passthrough._requested_reasoning_effort(payload)
    changed = False
    if official_passthrough:
        return _official_passthrough.official_passthrough_request_body(body, payload, upstream, model_id=model_id)

    collaboration_protocol = host._resolve_collaboration_boundary(
        payload,
        event_context,
        surface="request",
    )

    changed = host._normalize_responses_message_input_items(payload)
    if upstream_name == "official":
        if host._sanitize_official_reasoning_items(payload):
            changed = True
        if _response._sanitize_unsupported_compaction_input_items(payload):
            changed = True
        if host._normalize_responses_string_input(payload):
            changed = True
        if _response._sanitize_official_system_messages(payload):
            changed = True
        if _response._sanitize_official_invalid_tool_calls(payload):
            changed = True
        if isinstance(upstream_model, str) and upstream_model and payload.get("model") != upstream_model:
            payload["model"] = upstream_model
            changed = True
        service_tier = upstream.get("service_tier")
        if isinstance(service_tier, str) and service_tier and payload.get("service_tier") != service_tier:
            payload["service_tier"] = service_tier
            changed = True
        # The chatgpt.com/backend-api/codex endpoint requires store=false,
        # forces streaming, and rejects max_output_tokens. Inject/fix these
        # so callers that don't know about Codex's quirks (e.g. ZCode via
        # the Chat Completions gateway) still work.
        if payload.get("store") is not False:
            payload["store"] = False
            changed = True
        if payload.get("stream") is not True:
            payload["stream"] = True
            changed = True
        if "max_output_tokens" in payload:
            del payload["max_output_tokens"]
            changed = True
        if _response._sanitize_official_system_messages(payload):
            changed = True
        if not changed:
            return body
        return json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")

    if host._strip_reasoning_encrypted_content(payload):
        changed = True

    raw_provider_probe = _official_passthrough._is_raw_provider_probe_context(event_context)
    tool_protocol = (
        tool_protocol_override
        if tool_protocol_override is not None
        else _external_tool_protocol(upstream)
    )
    tool_surface_strategy = (
        validated_tool_surface_strategy
        if validated_tool_surface_strategy is not None
        else _external_tool_surface_strategy(upstream)
    )
    collaboration_v2 = collaboration_protocol == _COLLABORATION_V2
    codex_app_external = (
        behavior_profile == BEHAVIOR_CODEX_APP_EXTERNAL_ADAPTER
        or (event_context or {}).get("behavior_profile")
        == BEHAVIOR_CODEX_APP_EXTERNAL_ADAPTER
    )
    guidance_enabled = subagent_guidance_enabled(event_context)
    semantic_repair_enabled = subagent_semantic_repair_enabled(event_context)
    if isinstance(event_context, dict):
        event_context["tool_protocol"] = tool_protocol
    if not raw_provider_probe and not collaboration_v2:
        if _multi_agent._validate_worker_binding_history(payload):
            changed = True
    bounded_tool_search_terminal_calls = (
        {}
        if raw_provider_probe
        else _multi_agent._bounded_empty_tool_search_terminal_calls(payload.get("input"))
    )
    bounded_tool_search_queries = {
        query for query, _count in bounded_tool_search_terminal_calls.values()
    }
    if isinstance(event_context, dict):
        # A flattened ``function_call`` named ``tool_search`` is ambiguous
        # unless this request actually exposed Codex's client-owned search
        # declaration.  Remember that bounded history or an explicit
        # declaration established that ownership; ordinary provider
        # functions with the same name must remain untouched.
        declared_client_tool_search = any(
            isinstance(tool, Mapping)
            and (
                (tool.get("type") == "tool_search" and tool.get("execution") == "client")
                or (
                    tool.get("type") == TOOL_SEARCH_EXPLICIT_FUNCTION_TOOL["type"]
                    and tool == TOOL_SEARCH_EXPLICIT_FUNCTION_TOOL
                )
            )
            for tool in (payload.get("tools") if isinstance(payload.get("tools"), list) else ())
        )
        if bounded_tool_search_queries or declared_client_tool_search:
            event_context["_tool_search_client_owned"] = True
        if bounded_tool_search_queries:
            event_context["_bounded_tool_search_query_digests"] = frozenset(
                _multi_agent._tool_search_query_digest(query) for query in bounded_tool_search_queries
            )
        else:
            event_context.pop("_bounded_tool_search_query_digests", None)
    if _multi_agent._terminalize_bounded_empty_tool_search_misses(payload, bounded_tool_search_terminal_calls):
        for _query, count in bounded_tool_search_terminal_calls.values():
            host.write_proxy_event(
                "tool_search_empty_miss_bound",
                query_classification=TOOL_SEARCH_UNAVAILABLE_QUERY_CLASSIFICATION,
                count=count,
                status=TOOL_SEARCH_UNAVAILABLE_STATUS,
            )
        changed = True
    runtime_tool_plan: RuntimeToolCompatibilityPlan | None = None
    pending_tool_surface_event: dict[str, Any] | None = None
    tool_surface_source_tools: list[Any] | None = None
    if not raw_provider_probe:
        # The selected tool-surface policy is a wire-shaping concern, not a
        # telemetry concern.  Apply it before runtime planning even when a
        # direct helper caller does not provide a mutable event context.  If
        # this remains behind the ``dict`` check, a runtime plan can see the
        # original namespace and re-expand every child into aliases (#425).
        if tool_surface_strategy == "deferred_core" or collaboration_v2:
            # ``additional_tools`` is an internal carrier.  Only deferred
            # external routes, or the client-owned V2 adapter, need it
            # promoted so namespace pruning/runtime planning can inspect the
            # declarations.  Ordinary eager routes must preserve this legacy
            # carrier byte-for-byte (#425).
            if _official_passthrough._hoist_additional_tools_input_items(payload):
                changed = True
        if tool_surface_strategy == "deferred_core" and isinstance(payload.get("tools"), list):
            tools = payload["tools"]
            tool_surface_source_tools = list(tools)
            deferred_namespace_tools = [
                tool
                for tool in tools
                if _official_passthrough._is_raw_namespace_schema(tool)
                and not (
                    collaboration_v2
                    and isinstance(tool, Mapping)
                    and tool.get("name") == _COLLABORATION_V2_NAMESPACE
                )
            ]
            retained_tools = [
                tool
                for tool in tools
                if not (
                    _official_passthrough._is_raw_namespace_schema(tool)
                    and not (
                        collaboration_v2
                        and isinstance(tool, Mapping)
                        and tool.get("name") == _COLLABORATION_V2_NAMESPACE
                    )
                )
            ]
            if len(retained_tools) != len(tools):
                tools[:] = retained_tools
                changed = True
            if collaboration_v2:
                namespace_declaration_count, deferred_tool_count = (
                    _official_passthrough._deferred_namespace_surface_counts(deferred_namespace_tools, retained_tools)
                )
                retained_tool_ids = {id(tool) for tool in retained_tools}
                pending_tool_surface_event = {
                    "tool_surface_strategy": tool_surface_strategy,
                    "namespace_declaration_count": namespace_declaration_count,
                    "eager_tool_count": 0,
                    "retained_core_count": sum(
                        1
                        for tool in tool_surface_source_tools
                        if not _official_passthrough._is_raw_namespace_schema(tool)
                        and id(tool) in retained_tool_ids
                    ),
                    "deferred_tool_count": deferred_tool_count,
                }
        # Runtime planning is required for the wire transformation even when
        # this helper is used without a mutable telemetry context.  The
        # production handler supplies a dict so the plan/stream ledger can be
        # reused for response decoding, but direct callers and a few request
        # boundaries legitimately pass None or an immutable Mapping.  Using a
        # private context here prevents those calls from silently forwarding a
        # raw Collaboration namespace (or re-expanding deferred children) just
        # because telemetry storage was unavailable.
        # A real relay always supplies a mutable context.  The one context-free
        # path that still needs planning is the client-owned Collaboration V2
        # adapter: direct callers may omit telemetry, but the namespace must
        # still be converted before it can reach a third-party provider.  Keep
        # ordinary context-free compatibility calls on their legacy shaping
        # path; they have no response ledger to decode and changing them would
        # turn a helper call into a different protocol boundary.
        if isinstance(event_context, dict) or collaboration_v2:
            runtime_plan_context = (
                event_context if isinstance(event_context, dict) else {}
            )
            if _official_passthrough._prepare_runtime_tool_compatibility(
                payload,
                upstream,
                tool_protocol,
                runtime_plan_context,
                native_responses_tool_codec=native_responses_tool_codec_override,
            ):
                changed = True
            runtime_tool_plan = _official_passthrough._runtime_tool_compatibility_plan(runtime_plan_context)
    if raw_provider_probe:
        pass
    elif collaboration_v2:
        # V2 must not run V1 semantic repair, but a third-party structured
        # Responses endpoint still cannot consume Codex's freeform
        # ``apply_patch`` history items. Keep this wire-only inverse adapter
        # active so the next request does not leak ``custom_tool_call`` into
        # an endpoint that only accepts function-call history.
        input_items = payload.get("input")
        if isinstance(input_items, list):
            adapted_items, _adapted_call_ids, history_changed = _response._adapt_apply_patch_custom_tool_history(
                input_items,
                event_context=event_context,
            )
            if history_changed:
                payload["input"] = adapted_items
                changed = True
        if _response._rewrite_v2_unsupported_tool_history(
            payload,
            upstream=upstream,
            tool_protocol=tool_protocol,
            compatibility_plan=runtime_tool_plan,
            event_context=event_context,
            upstream_name=upstream_name,
        ):
            changed = True
        if (
            upstream.get("upstream_format") == "chat_completions"
            and _response._drop_v2_chat_reasoning_history(
                payload,
                event_context=event_context,
                upstream_name=upstream_name,
            )
        ):
            changed = True
    else:
        # ``additional_tools`` is a legacy Codex input carrier. Preserve it
        # byte-for-byte for eager providers; deferred_core alone promotes it
        # so the selected external surface policy can inspect namespaces.
        if tool_surface_strategy == "deferred_core" and _official_passthrough._hoist_additional_tools_input_items(payload):
            changed = True
        if tool_protocol in host.STRUCTURED_TOOL_PROTOCOLS:
            if _official_passthrough._rewrite_structured_tool_input_items(
                payload,
                event_context=event_context,
                upstream_name=upstream_name,
                compatibility_plan=runtime_tool_plan,
            ):
                changed = True
        elif tool_protocol == "none":
            tools = payload.get("tools")
            if isinstance(tools, list):
                filtered_tools = [tool for tool in tools if not _multi_agent._is_multi_agent_tool_schema(tool)]
                if len(filtered_tools) != len(tools):
                    payload["tools"] = filtered_tools
                    changed = True
            if _official_passthrough._rewrite_internal_input_items(payload, event_context=event_context, upstream_name=upstream_name):
                changed = True
        else:
            if _official_passthrough._rewrite_internal_input_items(payload, event_context=event_context, upstream_name=upstream_name):
                changed = True
    if (
        not raw_provider_probe
        and upstream.get("upstream_format") == "chat_completions"
        and (collaboration_v2 or codex_app_external)
        and _response._drop_chat_message_phase(
            payload,
            event_context=event_context,
            upstream_name=upstream_name,
        )
    ):
        changed = True
    input_items = payload.get("input")
    subagent_worker_context = (
        not raw_provider_probe
        and not collaboration_v2
        and tool_protocol in {"text_compat", "chat_tools", "responses_structured"}
        and is_worker_subagent_request(input_items)
    )
    # deferred_core intentionally keeps Codex's bounded, explicit discovery
    # entry point. It does not flatten namespace declarations or introduce a
    # broader discovery service; eager remains the #105-compatible surface.
    # Worker subagents retain their established restricted surface.
    include_tool_search = (
        tool_surface_strategy == "deferred_core"
        and not collaboration_v2
        and not subagent_worker_context
    )
    subagent_state = (
        build_subagent_state(input_items)
        if (
            not raw_provider_probe
            and not collaboration_v2
            and not subagent_worker_context
            and tool_protocol in {"text_compat", "chat_tools", "responses_structured"}
        )
        else None
    )
    subagent_state_active = subagent_state is not None and (
        bool(subagent_state.agents) or subagent_state.requested_count is not None
        or bool(getattr(subagent_state, "workflow_intent", False))
        or subagent_state.next_action == "send_input"
    )
    node_repl_single_step_complete = (
        not raw_provider_probe
        and not collaboration_v2
        and _multi_agent._has_completed_single_step_node_repl_context(input_items)
    )
    subagent_workflow_plan_read_complete = (
        not raw_provider_probe
        and subagent_state_active
        and subagent_state is not None
        and bool(getattr(subagent_state, "workflow_intent", False))
        and _multi_agent._has_node_repl_subagent_plan_read_context(input_items)
    )
    subagent_workflow_plan_read_required = (
        not raw_provider_probe
        and subagent_state_active
        and subagent_state is not None
        and bool(getattr(subagent_state, "workflow_intent", False))
        and not subagent_workflow_plan_read_complete
        and not bool(getattr(subagent_state, "agents", {}))
    )

    if raw_provider_probe:
        open_agent_ids = []
        wait_agent_ids = []
        close_agent_ids = []
        closed_agent_ids = []
        lifecycle_complete = False
        include_spawn_agent = False
        include_wait_agent = False
        include_close_agent = False
        include_resume_agent = False
        include_send_input = False
        state_hint = None
    elif subagent_worker_context:
        open_agent_ids = []
        wait_agent_ids = []
        close_agent_ids = []
        closed_agent_ids = []
        lifecycle_complete = False
        include_spawn_agent = False
        include_wait_agent = False
        include_close_agent = False
        include_resume_agent = False
        include_send_input = False
        state_hint = None
    elif collaboration_v2:
        open_agent_ids = []
        wait_agent_ids = []
        close_agent_ids = []
        closed_agent_ids = []
        lifecycle_complete = False
        include_spawn_agent = False
        include_wait_agent = False
        include_close_agent = False
        include_resume_agent = False
        include_send_input = False
        state_hint = None
    elif subagent_state_active and subagent_state is not None and guidance_enabled:
        spawned_agent_ids = subagent_state.spawned_agent_ids
        open_agent_ids = subagent_state.open_agent_ids
        wait_agent_ids = subagent_state.wait_agent_ids
        close_agent_ids = subagent_state.close_agent_ids
        closed_agent_ids = subagent_state.closed_agent_ids
        lifecycle_complete = subagent_state.lifecycle_complete
        include_spawn_agent = subagent_state.next_action == "spawn" and not lifecycle_complete
        include_wait_agent = subagent_state.next_action == "wait" and bool(wait_agent_ids)
        include_close_agent = subagent_state.next_action == "close" and bool(close_agent_ids)
        include_resume_agent = subagent_state.next_action == "send_input"
        include_send_input = subagent_state.next_action == "send_input"
        if subagent_workflow_plan_read_required:
            include_spawn_agent = False
            include_wait_agent = False
            include_close_agent = False
            include_resume_agent = False
            include_send_input = False
        state_hint = (
            state_guidance_message(subagent_state)
            if tool_protocol in {"text_compat", "chat_tools", "responses_structured"} or lifecycle_complete
            else None
        )
    elif subagent_state_active and subagent_state is not None:
        spawned_agent_ids = subagent_state.spawned_agent_ids
        open_agent_ids = subagent_state.open_agent_ids
        wait_agent_ids = subagent_state.wait_agent_ids
        close_agent_ids = subagent_state.close_agent_ids
        closed_agent_ids = subagent_state.closed_agent_ids
        lifecycle_complete = False
        include_spawn_agent = True
        include_wait_agent = True
        include_close_agent = True
        include_resume_agent = True
        include_send_input = True
        state_hint = None
    else:
        spawned_agent_ids = _multi_agent._spawned_multi_agent_ids(input_items)
        open_agent_ids = _multi_agent._open_multi_agent_ids(input_items)
        completed_wait_agent_ids = set(_multi_agent._completed_multi_agent_wait_ids(input_items))
        closed_agent_ids = _multi_agent._closed_multi_agent_ids(input_items)
        wait_agent_ids = [agent_id for agent_id in open_agent_ids if agent_id not in completed_wait_agent_ids]
        close_agent_ids = [agent_id for agent_id in open_agent_ids if agent_id in completed_wait_agent_ids]
        has_open_agent = _multi_agent._has_open_multi_agent_context(input_items)
        requested_spawn_count = _multi_agent._requested_multi_agent_spawn_count(input_items)
        single_loop_multi_agent_request = _multi_agent._has_single_loop_multi_agent_request(input_items)
        bounded_multi_agent_request = single_loop_multi_agent_request or requested_spawn_count is not None
        spawn_more_required = (
            requested_spawn_count is not None and len(spawned_agent_ids) < requested_spawn_count
        )
        lifecycle_complete = (
            bounded_multi_agent_request
            and bool(closed_agent_ids)
            and not has_open_agent
            and (requested_spawn_count is None or len(closed_agent_ids) >= requested_spawn_count)
        )
        include_spawn_agent = not has_open_agent
        include_wait_agent = (not has_open_agent) or not open_agent_ids or bool(wait_agent_ids)
        include_close_agent = (not has_open_agent) or not open_agent_ids or bool(close_agent_ids)
        include_resume_agent = True
        include_send_input = True
        if bounded_multi_agent_request:
            include_resume_agent = False
            include_send_input = False
            if spawn_more_required:
                include_spawn_agent = True
                include_wait_agent = False
                include_close_agent = False
            elif not has_open_agent and not closed_agent_ids:
                include_wait_agent = False
                include_close_agent = False
        if lifecycle_complete:
            include_spawn_agent = False
            include_wait_agent = False
            include_close_agent = False
            include_resume_agent = False
            include_send_input = False
            state_hint = _multi_agent._multi_agent_lifecycle_complete_message(closed_agent_ids)
        elif spawn_more_required and spawned_agent_ids:
            state_hint = _multi_agent._multi_agent_spawn_more_message(spawned_agent_ids, requested_spawn_count)
        else:
            state_hint = _multi_agent._multi_agent_current_state_message(wait_agent_ids, close_agent_ids)
    if isinstance(event_context, dict) and not raw_provider_probe:
        if subagent_state is not None:
            event_context["_subagent_state"] = subagent_state
            exact_prompts = _official_passthrough._exact_child_prompts_from_request_text(_official_passthrough._active_user_request_text(input_items))
            protocol_state = getattr(subagent_state, "protocol_state", None)
            if exact_prompts:
                event_context["subagent_exact_spawn_prompts"] = list(exact_prompts)
                event_context["subagent_exact_spawn_offset"] = (
                    len(getattr(protocol_state, "agents", {}) or {}) if protocol_state is not None else 0
                )
            if exact_prompts and protocol_state is not None:
                workflow = bounded_workflow_from_exact_prompts(
                    exact_prompts,
                    assigned_agent_ids=list(protocol_state.agents.keys()),
                )
                legal_actions = compute_allowed_actions(workflow, protocol_state)
                if len(legal_actions) == 1:
                    event_context["subagent_legal_actions"] = [
                        {
                            "kind": legal_actions[0].kind,
                            "tool_name": legal_actions[0].tool_name,
                            "arguments": dict(legal_actions[0].arguments),
                            "agent_ids": list(legal_actions[0].agent_ids),
                            "node_id": legal_actions[0].node_id,
                        }
                    ]
            required_spawn_arguments = _multi_agent._required_spawn_arguments_for_state(input_items, subagent_state)
            if required_spawn_arguments is not None:
                event_context["subagent_required_spawn_arguments"] = required_spawn_arguments
        event_context["subagent_worker_context"] = bool(subagent_worker_context)
        event_context["subagent_open_agent_ids"] = list(open_agent_ids)
        event_context["subagent_wait_agent_ids"] = list(wait_agent_ids)
        event_context["subagent_close_agent_ids"] = list(close_agent_ids)
        event_context["subagent_closed_agent_ids"] = list(closed_agent_ids)
        event_context["subagent_spawn_allowed"] = bool(include_spawn_agent)
        event_context["subagent_lifecycle_complete"] = bool(lifecycle_complete)
        event_context["subagent_workflow_active"] = bool(
            subagent_state_active
            and subagent_state is not None
            and bool(getattr(subagent_state, "workflow_intent", False))
        )
        event_context["subagent_workflow_plan_read_complete"] = bool(subagent_workflow_plan_read_complete)
        event_context["subagent_workflow_plan_read_required"] = bool(subagent_workflow_plan_read_required)
    if guidance_enabled and state_hint is not None and isinstance(input_items, list):
        node_repl_alias = _official_passthrough._runtime_alias_for_namespace_child(
            runtime_tool_plan,
            NODE_REPL_NAMESPACE,
            "js",
        )
        if node_repl_alias is not None:
            state_hint = _official_passthrough._rewrite_generated_guidance_tool_name(
                state_hint,
                "mcp__node_repl__js",
                node_repl_alias,
            )
        input_items.append(state_hint)
        host._write_adapter_event(
            event_context,
            "multi_agent_current_state_guidance_injected",
            upstream=upstream_name,
            model=payload.get("model") if isinstance(payload.get("model"), str) else None,
            wait_agent_ids=wait_agent_ids,
            close_agent_ids=close_agent_ids,
            closed_agent_ids=closed_agent_ids,
            lifecycle_complete=lifecycle_complete,
        )
        changed = True
    if (
        subagent_worker_context
        and guidance_enabled
        and isinstance(input_items, list)
        and not _multi_agent._has_worker_subagent_finalization_guidance(input_items)
    ):
        input_items.append(_multi_agent._worker_subagent_finalization_message())
        host._write_adapter_event(
            event_context,
            "worker_subagent_finalization_guidance_injected",
            upstream=upstream_name,
            model=payload.get("model") if isinstance(payload.get("model"), str) else None,
        )
        changed = True
    if node_repl_single_step_complete and isinstance(input_items, list):
        input_items.append(_multi_agent._node_repl_single_step_complete_message())
        host._write_adapter_event(
            event_context,
            "node_repl_single_step_complete_guidance_injected",
            upstream=upstream_name,
            model=payload.get("model") if isinstance(payload.get("model"), str) else None,
        )
        changed = True
    if raw_provider_probe:
        if isinstance(upstream_model, str) and upstream_model and payload.get("model") != upstream_model:
            payload["model"] = upstream_model
            changed = True
        if not changed:
            return body
        return json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    if (
        (
            runtime_tool_plan is None
            or (
                native_responses_tool_codec_override
                if native_responses_tool_codec_override is not None
                else _external_native_responses_tool_codec(upstream)
            )
            == "strict_apply_patch"
        )
        and
        tool_protocol == "responses_structured"
        and _official_passthrough._adapt_native_responses_tool_declarations(
            payload,
            upstream,
            event_context,
            codec=native_responses_tool_codec_override,
        )
    ):
        changed = True
    allow_codex_tools = tool_protocol != "none"
    if inject_codex_tools and allow_codex_tools and not raw_provider_probe and not collaboration_v2:
        if lifecycle_complete:
            if _multi_agent._hide_tools_for_completed_subagent_lifecycle(payload):
                host._write_adapter_event(
                    event_context,
                    "subagent_lifecycle_complete_tools_hidden",
                    upstream=upstream_name,
                    model=payload.get("model") if isinstance(payload.get("model"), str) else None,
                )
                changed = True
        else:
            restrict_to_subagent_coordinator_tools = bool(
                guidance_enabled
                and
                subagent_state_active
                and subagent_state is not None
                and bool(getattr(subagent_state, "workflow_intent", False))
            )
            # Coordinator and worker restrictions deliberately remain narrower
            # than the normal deferred-core surface.
            runtime_plain_tool_search = bool(
                runtime_tool_plan is not None
                and any(
                    entry.family in {"plain_function", "tool_search"}
                    and entry.original_name == TOOL_SEARCH_EXPLICIT_FUNCTION_TOOL["name"]
                    and entry.disposition != "omit"
                    for entry in runtime_tool_plan.entries
                )
            )
            effective_include_tool_search = (
                include_tool_search
                and (
                    runtime_tool_plan is None
                    or runtime_plain_tool_search
                    # The Gateway owns the deferred-core declaration for
                    # structured Responses/Chat routes.  It is not present
                    # in the caller's initial tool list, so requiring a
                    # pre-existing plan entry would suppress the very
                    # declaration that must be adapted for Chat providers.
                    or tool_protocol in host.STRUCTURED_TOOL_PROTOCOLS
                )
                and not subagent_worker_context
                and not restrict_to_subagent_coordinator_tools
            )
            include_node_repl_for_subagent_workflow = (
                restrict_to_subagent_coordinator_tools
                and not node_repl_single_step_complete
                and not subagent_workflow_plan_read_complete
                and not bool(subagent_state.agents if subagent_state is not None else {})
            )
            if (
                tool_surface_strategy == "deferred_core"
                and include_node_repl_for_subagent_workflow
                and _multi_agent._restore_deferred_core_node_repl_namespace(
                    payload,
                    tool_surface_source_tools,
                )
            ):
                changed = True
                if runtime_tool_plan is not None:
                    runtime_tool_plan = runtime_tool_plan.with_final_declarations(
                        payload["tools"],
                        tool_choice=payload.get("tool_choice"),
                    )
                    if isinstance(event_context, dict):
                        event_context[_official_passthrough._RUNTIME_TOOL_COMPATIBILITY_PLAN_KEY] = runtime_tool_plan
            if subagent_worker_context and _multi_agent._filter_tools_for_subagent_worker(
                payload,
                compatibility_plan=runtime_tool_plan,
            ):
                host._write_adapter_event(
                    event_context,
                    "subagent_worker_tools_restricted",
                    upstream=upstream_name,
                    model=payload.get("model") if isinstance(payload.get("model"), str) else None,
                )
                changed = True
            if restrict_to_subagent_coordinator_tools and _multi_agent._filter_tools_for_subagent_coordinator(
                payload,
                include_node_repl_tools=include_node_repl_for_subagent_workflow,
                compatibility_plan=runtime_tool_plan,
            ):
                host._write_adapter_event(
                    event_context,
                    "subagent_coordinator_tools_restricted",
                    upstream=upstream_name,
                    model=payload.get("model") if isinstance(payload.get("model"), str) else None,
                    include_node_repl_tools=include_node_repl_for_subagent_workflow,
                )
                changed = True
            tool_names_before = _official_passthrough._function_tool_names(payload.get("tools"))
            tool_surface_counts: dict[str, int] = {}
            worker_caller_carrier_supported = _multi_agent._worker_caller_carrier_supported(event_context)
            if isinstance(event_context, dict):
                if include_spawn_agent:
                    event_context["_spawn_selector_required"] = True
                else:
                    event_context.pop("_spawn_selector_required", None)
                if include_spawn_agent and worker_caller_carrier_supported:
                    event_context["_worker_binding_required"] = True
                    event_context["_worker_requested_binding"] = {
                        "agent_type": "worker",
                        "model": requested_model,
                        "reasoning": requested_reasoning,
                    }
                else:
                    event_context.pop("_worker_binding_required", None)
                    event_context.pop("_worker_requested_binding", None)
            explicit_tools_injected = _official_passthrough._inject_explicit_codex_tools(
                payload,
                include_tool_search=effective_include_tool_search,
                include_multi_agent_tools=not subagent_worker_context,
                include_spawn_agent=include_spawn_agent,
                include_wait_agent=include_wait_agent,
                include_close_agent=include_close_agent,
                include_resume_agent=include_resume_agent,
                include_send_input=include_send_input,
                include_node_repl_tools=(
                    include_node_repl_for_subagent_workflow
                    if restrict_to_subagent_coordinator_tools
                    else not node_repl_single_step_complete
                ),
                include_local_tool_gateway_tools=not subagent_worker_context,
                strip_namespace_tools=runtime_tool_plan is None,
                strip_all_namespace_tools=(
                    runtime_tool_plan is None and tool_surface_strategy == "deferred_core"
                ),
                include_flattened_namespace_tools=(
                    runtime_tool_plan is None and tool_surface_strategy == "eager"
                ),
                deferred_core_surface=tool_surface_strategy == "deferred_core",
                tool_surface_counts=tool_surface_counts,
                tool_surface_source_tools=tool_surface_source_tools,
                open_agent_ids=open_agent_ids,
                wait_agent_ids=wait_agent_ids,
                close_agent_ids=close_agent_ids,
                worker_selector_values=(
                    ("worker", "default")
                    if worker_caller_carrier_supported
                    else ("default",)
                ),
            )
            if _multi_agent._restrict_bounded_tool_search_queries(payload, bounded_tool_search_queries):
                changed = True
            if tool_surface_counts:
                if runtime_tool_plan is not None and tool_surface_strategy == "eager":
                    tool_surface_counts["eager_tool_count"] = sum(
                        len(entry.aliases)
                        for entry in runtime_tool_plan.entries
                        if entry.family == "namespace"
                        and entry.disposition == "adapt"
                        and _official_passthrough._is_flattened_namespace_schema(entry.declaration)
                    )
                    tool_surface_counts["deferred_tool_count"] = 0
                pending_tool_surface_event = {
                    "tool_surface_strategy": tool_surface_strategy,
                    **tool_surface_counts,
                }
            if explicit_tools_injected:
                added_tool_names = sorted(_official_passthrough._function_tool_names(payload.get("tools")) - tool_names_before)
                host._write_adapter_event(
                    event_context,
                    "explicit_codex_tools_injected",
                    upstream=upstream_name,
                    model=payload.get("model") if isinstance(payload.get("model"), str) else None,
                    added_tool_count=len(added_tool_names),
                    added_tool_names=added_tool_names,
                )
                changed = True
            required_tool_choice_name = None
            if subagent_state_active:
                runtime_node_repl_alias = _official_passthrough._runtime_alias_for_namespace_child(
                    runtime_tool_plan,
                    NODE_REPL_NAMESPACE,
                    "js",
                )
                required_node_repl_name = runtime_node_repl_alias or "mcp__node_repl__js"
                if (
                    subagent_workflow_plan_read_required
                    and include_node_repl_for_subagent_workflow
                    and (
                        runtime_node_repl_alias is not None
                        or required_node_repl_name in _official_passthrough._function_tool_names(payload.get("tools"))
                    )
                ):
                    required_tool_choice_name = required_node_repl_name
                else:
                    required_tool_choice_name = _multi_agent._required_subagent_tool_choice(
                        tool_protocol=tool_protocol,
                        lifecycle_complete=lifecycle_complete,
                        include_spawn_agent=include_spawn_agent,
                        include_wait_agent=include_wait_agent,
                        include_close_agent=include_close_agent,
                        include_resume_agent=include_resume_agent,
                        include_send_input=include_send_input,
                        include_node_repl_for_subagent_workflow=include_node_repl_for_subagent_workflow,
                    )
            if semantic_repair_enabled and _official_passthrough._restrict_tools_to_required_tool(payload, required_tool_choice_name):
                required_tool_family, required_tool_disposition = _official_passthrough._runtime_required_tool_diagnostics(
                    runtime_tool_plan,
                    required_tool_choice_name,
                )
                host.write_proxy_event(
                    "required_tool_tools_restricted",
                    tool_choice_required=True,
                    required_tool_family=required_tool_family,
                    required_tool_disposition=required_tool_disposition,
                )
                changed = True
            if semantic_repair_enabled and _multi_agent._set_required_subagent_tool_choice(
                payload,
                required_tool_choice_name,
                event_context=event_context,
                upstream=upstream_name,
            ):
                changed = True
    if runtime_tool_plan is not None and isinstance(payload.get("tools"), list):
        final_declarations = [
            tool
            for tool in payload["tools"]
            if not (
                isinstance(tool, Mapping)
                and tool.get("name") == APPLY_PATCH_FUNCTION_NAME
            )
        ]
        finalized_plan = runtime_tool_plan.with_final_declarations(
            final_declarations,
            tool_choice=payload.get("tool_choice"),
        )
        if finalized_plan is not runtime_tool_plan and isinstance(event_context, dict):
            runtime_tool_plan = finalized_plan
            event_context[_official_passthrough._RUNTIME_TOOL_COMPATIBILITY_PLAN_KEY] = finalized_plan
    if runtime_tool_plan is not None and _official_passthrough._apply_runtime_tool_compatibility_plan(
        payload,
        runtime_tool_plan,
    ):
        changed = True
    if runtime_tool_plan is not None:
        _official_passthrough._write_runtime_tool_adapter_request_evidence(
            runtime_tool_plan,
            payload,
            event_context,
        )
    if pending_tool_surface_event is not None:
        final_tools = payload.get("tools")
        host.write_proxy_event(
            "external_tool_surface_prepared",
            **pending_tool_surface_event,
            final_tool_count=len(final_tools) if isinstance(final_tools, list) else 0,
        )
    model_id = payload.get("model")
    max_output_tokens, context_window_fallback = (
        host._catalog_output_limit(model_id) if isinstance(model_id, str) else (None, False)
    )
    if max_output_tokens is not None:
        requested_max_output_tokens = payload.get("max_output_tokens")
        if context_window_fallback and (
            not isinstance(requested_max_output_tokens, int)
            or requested_max_output_tokens >= max_output_tokens
        ):
            if "max_output_tokens" in payload:
                del payload["max_output_tokens"]
                changed = True
        elif not isinstance(requested_max_output_tokens, int) or requested_max_output_tokens > max_output_tokens:
            payload["max_output_tokens"] = max_output_tokens
            changed = True

    if isinstance(upstream_model, str) and upstream_model and payload.get("model") != upstream_model:
        payload["model"] = upstream_model
        changed = True

    upstream_format = upstream.get("upstream_format")
    if (
        "reasoning" in payload
        and upstream_format != "chat_completions"
        and host._reasoning_param_is_unsupported(upstream_name, requested_model, upstream_model)
    ):
        del payload["reasoning"]
        host._write_adapter_event(
            event_context,
            "unsupported_reasoning_removed",
            upstream=upstream_name,
            model=requested_model if isinstance(requested_model, str) else None,
            upstream_model=upstream_model if isinstance(upstream_model, str) else None,
        )
        changed = True

    if upstream_name == "ollama_cloud":
        if _official_passthrough._apply_ollama_reasoning_effort_alias(payload):
            changed = True

    if not changed:
        return body
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
