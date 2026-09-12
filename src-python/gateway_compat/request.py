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
import gateway_catalog_runtime as _gateway_catalog_runtime
import gateway_events as _gateway_events
import gateway_request as _gateway_request

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
    external_requires_reasoning_content_history as _external_requires_reasoning_content_history,
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

def _wrap_chat_function_tools(payload: dict[str, Any]) -> bool:
    tools = payload.get("tools")
    if not isinstance(tools, list):
        return False
    next_tools: list[Any] = []
    changed = False
    for tool in tools:
        if (
            isinstance(tool, dict)
            and tool.get("type") == "function"
            and "function" not in tool
            and isinstance(tool.get("name"), str)
            and tool["name"]
        ):
            function: dict[str, Any] = {"name": tool["name"]}
            if isinstance(tool.get("description"), str):
                function["description"] = tool["description"]
            if isinstance(tool.get("parameters"), dict):
                function["parameters"] = tool["parameters"]
            if isinstance(tool.get("strict"), bool):
                function["strict"] = tool["strict"]
            next_tools.append({"type": "function", "function": function})
            changed = True
        else:
            next_tools.append(tool)
    if changed:
        payload["tools"] = next_tools
    return changed


_WEB_SEARCH_TOOL_TYPES = frozenset({"web_search", "web_search_preview"})


def _drop_third_party_web_search_external_web_access(payload: dict[str, Any]) -> bool:
    """Drop hosted web_search.external_web_access when the provider cannot honor it.

    Codex Desktop 0.153+ puts this flag on hosted web_search. Official accepts
    it. xAI and other non-OpenAI Responses endpoints 400 with
    "Argument not supported: external_web_access". true is lossless to drop
    (those providers already search live). false is cache-only and must fail
    closed — dropping it would silently enable live fetches. Same for
    web_search_preview: third-parties still execute live search.
    """

    tools = payload.get("tools")
    if not isinstance(tools, list):
        return False
    changed = False
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") not in _WEB_SEARCH_TOOL_TYPES:
            continue
        if "external_web_access" not in tool:
            continue
        value = tool.get("external_web_access")
        if value is False:
            raise UpstreamProtocolTranslationError(
                UnsupportedProtocolTranslationError(
                    "unsupported_protocol_semantics",
                    "Cannot honor web_search external_web_access=false on a third-party route.",
                )
            )
        tool.pop("external_web_access", None)
        changed = True
    return changed


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
        decode = json.loads if (
            official_passthrough or upstream_name == "official"
            or _official_passthrough._is_raw_provider_probe_context(event_context)
        ) else _collaboration_adapter_module.decode_adapted_json
        payload = decode(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        if official_passthrough:
            return body
        upstream_model = upstream.get("upstream_model")
        if isinstance(model_id, str) and isinstance(upstream_model, str) and upstream_model and model_id != upstream_model:
            return _official_passthrough._replace_embedded_model(body, model_id, upstream_model)
        return body

    if not isinstance(payload, dict):
        return body

    if upstream_name == "official":
        inject_codex_tools = False

    upstream_model = upstream.get("upstream_model")
    requested_model = payload.get("model")
    requested_reasoning = _official_passthrough._requested_reasoning_effort(payload)
    changed = False
    if official_passthrough:
        return _official_passthrough.official_passthrough_request_body(body, payload, upstream, model_id=model_id, event_context=event_context)

    collaboration_protocol = _collaboration_adapter_module.resolve_boundary(
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
        try:
            if _collab_v2.expand_chat_v2_for_official(payload, event_context if isinstance(event_context, dict) else None):
                changed = True
                collaboration_protocol = _collaboration_adapter_module.resolve_boundary(
                    payload,
                    event_context,
                    surface="request",
                )
            if _chat_official_native.expand_chat_native_tools_for_official(
                payload, event_context if isinstance(event_context, dict) else None
            ):
                changed = True
        except RuntimeToolCompatibilityError as exc:
            _official_passthrough._raise_runtime_tool_compatibility_error(exc)

    if upstream_name != "official" and host._strip_reasoning_encrypted_content(payload):
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
    if isinstance(event_context, dict):
        event_context["tool_protocol"] = tool_protocol
    if upstream_name != "official" and not raw_provider_probe and not collaboration_v2:
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
            _gateway_events.write_proxy_event(
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
        if upstream_name != "official" and (tool_surface_strategy == "deferred_core" or collaboration_v2):
            # ``additional_tools`` is an internal carrier.  Only deferred
            # external routes, or the client-owned V2 adapter, need it
            # promoted so namespace pruning/runtime planning can inspect the
            # declarations.  Ordinary eager routes must preserve this legacy
            # carrier byte-for-byte (#425).
            if _official_passthrough._hoist_additional_tools_input_items(payload):
                changed = True
        if upstream_name != "official" and tool_surface_strategy == "deferred_core" and isinstance(payload.get("tools"), list):
            tools = payload["tools"]
            tool_surface_source_tools = list(tools)
            deferred_namespace_tools = [
                tool
                for tool in tools
                if _official_passthrough._is_raw_namespace_schema(tool)
                and not (
                    isinstance(tool, Mapping)
                    and tool.get("name") in {"multi_agent_v1", _COLLABORATION_V2_NAMESPACE}
                )
            ]
            retained_tools = [
                tool
                for tool in tools
                if not (
                    _official_passthrough._is_raw_namespace_schema(tool)
                    and not (
                        isinstance(tool, Mapping)
                        and tool.get("name") in {"multi_agent_v1", _COLLABORATION_V2_NAMESPACE}
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
        if isinstance(event_context, dict) or collaboration_protocol is not None:
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
    if upstream_name != "official" and not raw_provider_probe:
        import multimodal_tool_result as _multimodal_tool_result

        media_policy = _multimodal_tool_result.policy_from_request(
            upstream, payload, event_context
        )
        try:
            media_changed, media_stats = _multimodal_tool_result.adapt_request_payload(
                payload, media_policy, event_context
            )
        except UnsupportedProtocolTranslationError as exc:
            raise UpstreamProtocolTranslationError(exc) from exc
        if media_changed:
            changed = True
            _gateway_events.write_adapter_event(
                event_context,
                "tool_result_media_adapted",
                upstream=upstream_name,
                request_kind=media_policy.request_kind,
                **media_stats.as_event_fields(),
            )
    if raw_provider_probe:
        pass
    elif collaboration_v2 and upstream_name != "official":
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
            and not _external_requires_reasoning_content_history(upstream)
            and _response._drop_v2_chat_reasoning_history(
                payload,
                event_context=event_context,
                upstream_name=upstream_name,
            )
        ):
            changed = True
    elif upstream_name != "official":
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
    # deferred_core intentionally keeps Codex's bounded, explicit discovery
    # entry point. It does not flatten namespace declarations or introduce a
    # broader discovery service; eager remains the #105-compatible surface.
    include_tool_search = (
        tool_surface_strategy == "deferred_core"
        and not collaboration_v2
    )
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
        runtime_plain_tool_search = bool(
            runtime_tool_plan is not None
            and any(
                entry.family in {"plain_function", "tool_search"}
                and entry.original_name == TOOL_SEARCH_EXPLICIT_FUNCTION_TOOL["name"]
                and entry.disposition != "omit"
                for entry in runtime_tool_plan.entries
            )
        )
        effective_include_tool_search = include_tool_search and (
            runtime_tool_plan is None
            or runtime_plain_tool_search
            or tool_protocol in host.STRUCTURED_TOOL_PROTOCOLS
        )
        tool_names_before = _official_passthrough._function_tool_names(payload.get("tools"))
        tool_surface_counts: dict[str, int] = {}
        worker_caller_carrier_supported = _multi_agent._worker_caller_carrier_supported(event_context)
        if isinstance(event_context, dict):
            event_context.pop("_spawn_selector_required", None)
            if worker_caller_carrier_supported:
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
            # Real namespace declarations are encoded by the request-local
            # plan. This legacy surface must not grant undeclared agent tools.
            include_multi_agent_tools=False,
            include_spawn_agent=True,
            include_wait_agent=True,
            include_close_agent=True,
            include_resume_agent=True,
            include_send_input=True,
            include_node_repl_tools=True,
            include_local_tool_gateway_tools=True,
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
            open_agent_ids=[],
            wait_agent_ids=[],
            close_agent_ids=[],
            worker_selector_values=(
                ("worker", "default") if worker_caller_carrier_supported else ("default",)
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
            added_tool_names = sorted(
                _official_passthrough._function_tool_names(payload.get("tools")) - tool_names_before
            )
            _gateway_events.write_adapter_event(
                event_context,
                "explicit_codex_tools_injected",
                upstream=upstream_name,
                model=payload.get("model") if isinstance(payload.get("model"), str) else None,
                added_tool_count=len(added_tool_names),
                added_tool_names=added_tool_names,
            )
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
        _gateway_events.write_proxy_event(
            "external_tool_surface_prepared",
            **pending_tool_surface_event,
            final_tool_count=len(final_tools) if isinstance(final_tools, list) else 0,
        )
    model_id = payload.get("model")
    max_output_tokens, context_window_fallback = (
        _gateway_catalog_runtime.catalog_output_limit(model_id) if isinstance(model_id, str) else (None, False)
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
        _gateway_events.write_adapter_event(
            event_context,
            "unsupported_reasoning_removed",
            upstream=upstream_name,
            model=requested_model if isinstance(requested_model, str) else None,
            upstream_model=upstream_model if isinstance(upstream_model, str) else None,
        )
        changed = True

    if _gateway_request.apply_maintained_thinking_controls(
        payload, upstream_name, requested_model, upstream_model
    ):
        changed = True

    if upstream_name == "ollama_cloud":
        if _official_passthrough._apply_ollama_reasoning_effort_alias(payload):
            changed = True

    if _response._sanitize_unsupported_compaction_input_items(payload):
        changed = True
    if upstream_name != "official" and host._sanitize_third_party_reasoning_items(
        payload,
        preserve_collaboration_agent_message_encryption=(
            collaboration_protocol == _COLLABORATION_V2
        ),
    ):
        changed = True

    if upstream_name != "official":
        if payload.get("tool_choice") not in (None, "auto"):
            payload["tool_choice"] = "auto"
            changed = True
        native_v2_namespace = bool(
            collaboration_v2
            and runtime_tool_plan is not None
            and any(
                getattr(entry, "family", None) == "namespace"
                and getattr(entry, "version", None) == "v2"
                and getattr(entry, "disposition", None) == "native"
                for entry in runtime_tool_plan.entries
            )
        )
        if not native_v2_namespace:
            encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
            flattened, schema_rewrites = _official_passthrough._normalize_transparent_tool_schema_booleans(encoded)
            if schema_rewrites:
                payload = json.loads(flattened.decode("utf-8"))
                changed = True
        if isinstance(payload.get("messages"), list) and _wrap_chat_function_tools(payload):
            changed = True
        if _drop_third_party_web_search_external_web_access(payload):
            changed = True

    if not changed:
        return body
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
