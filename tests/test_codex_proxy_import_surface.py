"""Freeze the codex_proxy import surface used by the test suite."""

from __future__ import annotations

import inspect
import logging

import pytest

import codex_proxy
import apply_patch_adapter
import collaboration_adapter
import tool_surface_adapter
import gateway_errors
import gateway_settings
import gateway_sse
import gateway_transport
import route_plan
import route_primitives

# One-time scan of tests/ imports, attributes, and patch targets.
# Stdlib/vendor module attributes (json, sys, urllib3, ...) are excluded.
FROZEN_CODEX_PROXY_NAMES = (
    'AttemptRequestBodyMode',
    'AuthenticationStrategy',
    'BEHAVIOR_CODEX_APP_EXTERNAL_ADAPTER',
    'BEHAVIOR_EXTERNAL_PROVIDER_GATEWAY',
    'BEHAVIOR_OFFICIAL_CODEX_APP_HTTP_PASSTHROUGH',
    'BEHAVIOR_OFFICIAL_GATEWAY_COMPAT',
    'BEHAVIOR_THIRD_PARTY_APP_TRANSPARENT_METERED',
    'CODEX_SEMANTIC_EXTERNAL_ADAPTER',
    'CODEX_SEMANTIC_NONE',
    'COLLABORATION_BOUNDARY_ERROR_CODE',
    'CallerRequestBodyMode',
    'CapabilityState',
    'CodexCompatibilityPolicy',
    'CodexProxyHandler',
    'CollaborationBackend',
    'CompactEmptyResponseError',
    'DOWNSTREAM_STREAM_COMMIT_ALLOWLIST',
    'DOWNSTREAM_STREAM_COMMIT_SEAM_METHODS',
    'DownstreamClosedDuringImageProxyError',
    'DownstreamErrorSpec',
    'ExecutionOwner',
    'GATEWAY_DIAGNOSTIC_RECORDER',
    'GATEWAY_EVENT_WRITER',
    'GATEWAY_EVENT_WRITER_SHUTDOWN_TIMEOUT_SECONDS',
    'GatewayPreResponseBudgetExhausted',
    'GatewayTransport',
    'GatewayRequestAdmission',
    'GatewayShutdownController',
    'GatewayUserRequestedShutdown',
    'IMAGE_PROXY_CACHE_PATH',
    'IMAGE_PROXY_PROMPT',
    'ImageProxyError',
    'MULTI_AGENT_DISCOVERY_TOOLS',
    'MULTI_AGENT_TOOL_NAMES',
    'ModelIdentityResolutionError',
    'MutationPolicy',
    'OFFICIAL_CONNECT_TIMEOUT_SECONDS',
    'OFFICIAL_HTTP_POOLS',
    'OFFICIAL_POOL_MAX_CONNECTIONS',
    'OFFICIAL_POOL_MAX_IDLE_SECONDS',
    'OFFICIAL_PROXY_POOL_MAX_IDLE_SECONDS',
    'OFFICIAL_TERMINAL_DRAIN_TIMEOUT_SECONDS',
    'POLICY_PATH',
    'PROXY_EVENT_LOG_PATH',
    'PROXY_TEXT_LOG_PATH',
    'PassthroughSseSemanticStats',
    'REPAIR_CODEX_SUBAGENT',
    'REPAIR_NONE',
    'REQUEST_KIND_GATEWAY',
    'REQUEST_KIND_TRANSPARENT',
    'RETRY_CONSERVATIVE_PRE_OUTPUT',
    'RETRY_FAILURE_PERMANENT',
    'RETRY_FAILURE_PROVIDER_OVERLOADED',
    'RETRY_FAILURE_PROVIDER_THROTTLE',
    'RETRY_FAILURE_QUICK_TRANSIENT',
    'RETRY_GATEWAY_FULL',
    'RETRY_REQUEST_COMPACT',
    'RETRY_REQUEST_IMAGE_PROXY_VISION',
    'RETRY_REQUEST_MAIN_GENERATION',
    'RETRY_SAFETY_SUPPRESSED_POST_EXPOSURE',
    'RETRY_SAFETY_SUPPRESSED_POST_WRITE',
    'ROUTE_PLAN_SCHEMA_VERSION',
    'RUNTIME_CODEX_DIR',
    'RUNTIME_PROXY_DIR',
    'RelayExecutionPlan',
    'RetryPolicy',
    'RouteMutation',
    'RoutePlan',
    'RouteProtocol',
    'RouteRuntimeFacts',
    'StreamingPolicy',
    'TOOL_SEARCH_EXPLICIT_FUNCTION_TOOL',
    'TRANSIENT_HTTP_RETRY_STATUSES',
    'ThreadingHTTPServer',
    'ToolExposureMode',
    'TransportFacts',
    'TransportPolicy',
    'USAGE_ASYNC_TAP',
    'USAGE_SYNC_CAPTURE',
    'USER_REQUESTED_SHUTDOWN_OUTCOME',
    'UnsupportedProtocolTranslationError',
    'UpstreamProtocolTranslationError',
    'UpstreamStreamErrorEvent',
    'UpstreamStreamIncompleteError',
    'UpstreamStreamInterruptedError',
    'UsagePolicy',
    'VISION_PROXY_TRANSPARENT_OVERLAY',
    'VisionAction',
    'VisionNetworkAction',
    'WIRE_CHAT_TO_RESPONSES',
    'WIRE_RESPONSES_TO_CHAT',
    'WIRE_TRANSPARENT',
    'WORKER_BINDING_SIGNING_ROOT',
    '_GatewayDownstreamStreamCommit',
    '_OfficialHTTPSConnection',
    '_OfficialHTTPSConnectionPool',
    '_OfficialPooledResponse',
    '_TRANSPORT_PHASE_ATTRIBUTE',
    'UpstreamSseReaderLifecycle',
    '_UpstreamSseReaderLifecycle',
    '_activate_gateway_request',
    '_active_gateway_request',
    '_adapt_apply_patch_custom_tool_history',
    '_adapt_third_party_apply_patch_response_body',
    '_adapt_third_party_apply_patch_stream_events',
    '_bounded_empty_tool_search_terminal_calls',
    '_call_vision_model_for_image_description',
    '_capacity_retry_elapsed_limit_allows',
    '_chat_completion_to_response_body',
    '_chat_completions_request_to_responses_body',
    '_chat_completions_url',
    '_chat_messages_to_responses_input',
    '_chat_stream_chunks_have_terminal',
    '_chat_stream_chunks_to_response_events',
    '_chat_tool_choice_to_responses_tool_choice',
    '_chat_tools_to_responses_tools',
    '_codexhub_error_payload',
    '_coerce_required_subagent_tool_calls',
    '_configure_official_windows_keepalive',
    '_connection_disposition',
    '_diagnostic_connection_disposition',
    '_diagnostic_error_connection_disposition',
    '_downgrade_invalid_third_party_tool_calls',
    '_downstream_json_error_payload',
    '_downstream_sse_error_payload_for_inbound_format',
    '_enqueue_gateway_event_payload',
    '_event_context_with_request_kind',
    '_events_to_responses_body',
    '_explicit_transport_phase',
    '_external_native_responses_tool_codec',
    '_external_tool_protocol',
    '_external_tool_surface_strategy',
    '_filtered_response_headers',
    '_gateway_shutdown_controller_for_handler',
    '_guard_duplicate_multi_agent_spawn_calls',
    '_image_proxy_cache_lookup',
    '_image_proxy_cache_store',
    '_image_proxy_description_for_part',
    '_image_proxy_response_body',
    '_image_proxy_vision_upstream',
    '_is_compact_summary_payload',
    '_is_raw_reasoning_stream_event',
    '_is_reasoning_summary_stream_event',
    '_is_websocket_upgrade',
    '_local_request_authorized',
    '_model_access_path_idempotency_guaranteed',
    '_multi_agent_explicit_function_tools',
    '_normalize_third_party_tool_call',
    '_normalize_transparent_tool_schema_booleans',
    '_normalize_usage_for_event',
    '_observe_gateway_diagnostic',
    '_offer_official_passthrough_usage_line',
    '_official_pool_manager',
    '_official_proxy_url',
    '_official_socket_options',
    '_official_urlopen',
    '_open_upstream_once',
    '_open_upstream_response',
    '_parse_sse_json_payload',
    '_prepare_runtime_tool_compatibility',
    '_public_event_context',
    '_published_catalog_model',
    '_reconcile_function_call_argument_events',
    '_repair_missing_required_subagent_call_events',
    '_request_kind_from_headers_and_payload',
    '_resolve_collaboration_boundary',
    '_response_body_to_chat_completion_body',
    '_response_events_to_chat_stream_chunks',
    '_responses_events_have_terminal',
    '_responses_request_to_chat_completion_body',
    '_responses_url',
    '_restore_gateway_request',
    '_retry_attempts_for_failure_class',
    '_route_plan_event_fields',
    '_route_runtime_facts',
    '_runtime_settings_value',
    '_safe_route_endpoint_url',
    '_set_official_attempt_connection_disposition',
    '_sleep_for_retry_with_gateway_cancellation',
    '_sse_payload_bytes',
    '_strip_tools_for_compact_payload',
    '_suppress_bounded_tool_search_calls',
    '_tool_search_query_digest',
    '_upstream_failure_class',
    '_upstream_retry_attempts',
    '_usage_from_response_event',
    '_validate_reasoning_effort_for_upstream',
    '_validate_worker_binding_history',
    '_value_contains_image',
    '_write_adapter_event',
    '_write_usage_observed_body_event',
    '_write_usage_observed_event',
    'apply_image_proxy_to_responses_payload',
    'behavior_profile_for_request',
    'bind_route_plan_operational_authentication',
    'catalog_with_openai_context_guard',
    'choose_upstream',
    'codex_access_token',
    'codex_account_id',
    'compatible_request_body',
    'compatible_response_body',
    'compatible_sse_line',
    'current_catalog_data',
    'decoded_request_body',
    'enforce_text_only_image_boundary',
    'existing_generated_catalog_path',
    'extract_model',
    'flush_proxy_event_writer',
    'gateway_auto_retry_enabled',
    'gateway_auto_retry_max_attempts',
    'gateway_capacity_retry_elapsed_limit_seconds',
    'gateway_client_key',
    'gateway_downstream_retry_notice_enabled',
    'gateway_image_proxy_enabled',
    'gateway_image_proxy_model',
    'gateway_official_http_passthrough_enabled',
    'gateway_retry_delay_seconds',
    'gateway_stream_retry_elapsed_limit_seconds',
    'generated_catalog_by_slug',
    'generated_catalog_slugs',
    'load_catalog_models',
    'load_policy',
    'logger',
    'materialize_operational_authentication',
    'max_request_body_bytes',
    'model_event_sse_idle_timeout_seconds',
    'model_supports_image',
    'official_base_url',
    'official_passthrough_request_body',
    'official_upstream',
    'official_upstream_open_attempts',
    'ollama_cloud_alias_upstream_model',
    'ollama_cloud_runtime_upstream',
    'provider_scoped_path',
    'provider_scoped_route_model',
    'proxy_telemetry',
    'raw_provider_probe_requested',
    'request_context_from_headers',
    'resolve_external_model_alias',
    'resolve_ollama_cloud_model',
    'route_plan_for_request',
    'run_server',
    'should_include_model',
    'transparent_request_body',
    'transport_failure_phase',
    'try_extract_model',
    'upstream_headers',
    'upstream_timeout_seconds',
    'user_requested_shutdown_payload',
    'worker_binding_signing',
    'write_proxy_event',
)

REEXPORT_IDENTITY = {
    "RouteProtocol": route_primitives.RouteProtocol,
    "SensitiveValue": route_primitives.SensitiveValue,
    "OperationalAuthentication": route_primitives.OperationalAuthentication,
    "FrozenRequestHeaders": route_primitives.FrozenRequestHeaders,
    "CapabilityState": route_primitives.CapabilityState,
    "RETRY_GATEWAY_FULL": route_primitives.RETRY_GATEWAY_FULL,
    "ImageProxyError": gateway_errors.ImageProxyError,
    "ModelIdentityResolutionError": gateway_errors.ModelIdentityResolutionError,
    "UnsupportedRouteProtocolError": gateway_errors.UnsupportedRouteProtocolError,
    "UnqualifiedRouteProtocolError": gateway_errors.UnqualifiedRouteProtocolError,
    "GatewayPreResponseBudgetExhausted": gateway_errors.GatewayPreResponseBudgetExhausted,
    "upstream_timeout_seconds": gateway_settings.upstream_timeout_seconds,
    "gateway_client_key": gateway_settings.gateway_client_key,
    "route_plan_for_request": route_plan.route_plan_for_request,
    "RoutePlan": route_plan.RoutePlan,
    "behavior_profile_for_request": route_plan.behavior_profile_for_request,
    "_sse_payload_bytes": gateway_sse._sse_payload_bytes,
    "_parse_sse_json_payload": gateway_sse._parse_sse_json_payload,
    "_GatewayDownstreamStreamCommit": gateway_sse.DownstreamStreamCommit,
    "PassthroughSseSemanticStats": gateway_sse.PassthroughSseSemanticStats,
    "_identity_failure": gateway_errors._identity_failure,
    "UpstreamProtocolTranslationError": gateway_errors.UpstreamProtocolTranslationError,
    "COLLABORATION_BOUNDARY_ERROR_CODE": collaboration_adapter.COLLABORATION_BOUNDARY_ERROR_CODE,
    "WORKER_SELECTOR_ERROR_CODE": collaboration_adapter.WORKER_SELECTOR_ERROR_CODE,
    "WORKER_BINDING_ERROR_CODE": collaboration_adapter.WORKER_BINDING_ERROR_CODE,
    "WORKER_REQUESTED_BINDING_FIELD": collaboration_adapter.WORKER_REQUESTED_BINDING_FIELD,
    "WORKER_REQUESTED_BINDING_VERSION": collaboration_adapter.WORKER_REQUESTED_BINDING_VERSION,
    "WORKER_REQUESTED_BINDING_FIELDS": collaboration_adapter.WORKER_REQUESTED_BINDING_FIELDS,
    "LEGACY_NATIVE_WORKER_SPAWN_FIELDS": collaboration_adapter.LEGACY_NATIVE_WORKER_SPAWN_FIELDS,
    "LEGACY_NATIVE_WORKER_SPAWN_METADATA_FIELD": collaboration_adapter.LEGACY_NATIVE_WORKER_SPAWN_METADATA_FIELD,
    "CollaborationAdapter": collaboration_adapter.CollaborationAdapter,
    "INTERNAL_INPUT_ITEM_TYPES": tool_surface_adapter.INTERNAL_INPUT_ITEM_TYPES,
    "MULTI_AGENT_DISCOVERY_TOOLS": tool_surface_adapter.MULTI_AGENT_DISCOVERY_TOOLS,
    "MULTI_AGENT_NAMESPACE_ALIASES": tool_surface_adapter.MULTI_AGENT_NAMESPACE_ALIASES,
    "NODE_REPL_NAMESPACE": tool_surface_adapter.NODE_REPL_NAMESPACE,
    "THIRD_PARTY_TOOL_NAME_ALIASES": tool_surface_adapter.THIRD_PARTY_TOOL_NAME_ALIASES,
    "TOOL_NAME_RE": tool_surface_adapter.TOOL_NAME_RE,
    "TOOL_SEARCH_EMPTY_MISS_BOUND": tool_surface_adapter.TOOL_SEARCH_EMPTY_MISS_BOUND,
    "TOOL_SEARCH_EXPLICIT_FUNCTION_TOOL": tool_surface_adapter.TOOL_SEARCH_EXPLICIT_FUNCTION_TOOL,
    "TOOL_SEARCH_UNAVAILABLE_QUERY_CLASSIFICATION": tool_surface_adapter.TOOL_SEARCH_UNAVAILABLE_QUERY_CLASSIFICATION,
    "TOOL_SEARCH_UNAVAILABLE_STATUS": tool_surface_adapter.TOOL_SEARCH_UNAVAILABLE_STATUS,
    "ToolSurfaceAdapter": tool_surface_adapter.ToolSurfaceAdapter,
    "ToolSurfaceFacts": tool_surface_adapter.ToolSurfaceFacts,
    "APPLY_PATCH_FUNCTION_NAME": tool_surface_adapter.APPLY_PATCH_FUNCTION_NAME,
    "APPLY_PATCH_ADAPTER_EVENT": apply_patch_adapter.APPLY_PATCH_ADAPTER_EVENT,
    "APPLY_PATCH_ADAPTER_ERROR_CODE": apply_patch_adapter.APPLY_PATCH_ADAPTER_ERROR_CODE,
    "APPLY_PATCH_FUNCTION_CALL_FIELDS": apply_patch_adapter.APPLY_PATCH_FUNCTION_CALL_FIELDS,
    "APPLY_PATCH_HISTORY_ADAPTER_EVENT": apply_patch_adapter.APPLY_PATCH_HISTORY_ADAPTER_EVENT,
    "APPLY_PATCH_CUSTOM_TOOL_HISTORY_CALL_FIELDS": apply_patch_adapter.APPLY_PATCH_CUSTOM_TOOL_HISTORY_CALL_FIELDS,
    "APPLY_PATCH_CUSTOM_TOOL_HISTORY_OUTPUT_FIELDS": apply_patch_adapter.APPLY_PATCH_CUSTOM_TOOL_HISTORY_OUTPUT_FIELDS,
    "APPLY_PATCH_CUSTOM_TOOL_HISTORY_NATIVE_FIELDS": apply_patch_adapter.APPLY_PATCH_CUSTOM_TOOL_HISTORY_NATIVE_FIELDS,
    "ApplyPatchAdapter": apply_patch_adapter.ApplyPatchAdapter,
    "ApplyPatchFacts": apply_patch_adapter.ApplyPatchFacts,
    "GatewayTransport": gateway_transport.GatewayTransport,
    "TransportFacts": gateway_transport.TransportFacts,
    "UpstreamSseReaderLifecycle": gateway_transport.UpstreamSseReaderLifecycle,
    "_OfficialHTTPSConnection": gateway_transport._OfficialHTTPSConnection,
    "_OfficialHTTPSConnectionPool": gateway_transport._OfficialHTTPSConnectionPool,
    "_OfficialPooledResponse": gateway_transport._OfficialPooledResponse,
    "_TRANSPORT_PHASE_ATTRIBUTE": gateway_transport._TRANSPORT_PHASE_ATTRIBUTE,
    "_UpstreamSseReaderLifecycle": gateway_transport._UpstreamSseReaderLifecycle,
    "_capacity_retry_elapsed_limit_allows": gateway_transport._capacity_retry_elapsed_limit_allows,
    "_clamp_timeout_to_pre_response_budget": gateway_transport._clamp_timeout_to_pre_response_budget,
    "_configure_official_windows_keepalive": gateway_transport._configure_official_windows_keepalive,
    "_connection_disposition": gateway_transport._connection_disposition,
    "_explicit_transport_phase": gateway_transport._explicit_transport_phase,
    "_get_header": gateway_transport._get_header,
    "_header_items": gateway_transport._header_items,
    "_http_error_body_bytes": gateway_transport._http_error_body_bytes,
    "_official_socket_options": gateway_transport._official_socket_options,
    "_remaining_pre_response_budget_seconds": gateway_transport._remaining_pre_response_budget_seconds,
    "_require_retry_delay_within_pre_response_budget": gateway_transport._require_retry_delay_within_pre_response_budget,
    "_retry_after_delay_seconds": gateway_transport._retry_after_delay_seconds,
    "_retry_attempts_for_failure_class": gateway_transport._retry_attempts_for_failure_class,
    "_upstream_error_retryable": gateway_transport._upstream_error_retryable,
    "_upstream_failure_class": gateway_transport._upstream_failure_class,
    "_upstream_retry_status": gateway_transport._upstream_retry_status,
    "transport_failure_phase": gateway_transport.transport_failure_phase,
    "OFFICIAL_CONNECT_TIMEOUT_SECONDS": gateway_transport.OFFICIAL_CONNECT_TIMEOUT_SECONDS,
    "OFFICIAL_HTTP_POOLS": gateway_transport.OFFICIAL_HTTP_POOLS,
    "OFFICIAL_POOL_MAX_CONNECTIONS": gateway_transport.OFFICIAL_POOL_MAX_CONNECTIONS,
    "OFFICIAL_POOL_MAX_IDLE_SECONDS": gateway_transport.OFFICIAL_POOL_MAX_IDLE_SECONDS,
    "OFFICIAL_PROXY_POOL_MAX_IDLE_SECONDS": gateway_transport.OFFICIAL_PROXY_POOL_MAX_IDLE_SECONDS,
    "OFFICIAL_TERMINAL_DRAIN_TIMEOUT_SECONDS": gateway_transport.OFFICIAL_TERMINAL_DRAIN_TIMEOUT_SECONDS,
    "_responses_url": route_plan._responses_url,
    "_chat_completions_url": route_plan._chat_completions_url,
    "_external_tool_protocol": route_plan._external_tool_protocol,
    "_external_tool_surface_strategy": route_plan._external_tool_surface_strategy,
    "_external_native_responses_tool_codec": route_plan._external_native_responses_tool_codec,
    "_upstream_retry_attempts": gateway_settings._upstream_retry_attempts,
    "_default_retry_attempts_for_request_kind": gateway_settings._default_retry_attempts_for_request_kind,
    "_request_kind_retry_attempts_configured": gateway_settings._request_kind_retry_attempts_configured,
}


def test_frozen_codex_proxy_names_remain_importable() -> None:
    missing = [name for name in FROZEN_CODEX_PROXY_NAMES if not hasattr(codex_proxy, name)]
    assert missing == []


def test_extracted_gateway_names_keep_object_identity() -> None:
    drifted = {
        name: getattr(codex_proxy, name)
        for name, expected in REEXPORT_IDENTITY.items()
        if getattr(codex_proxy, name) is not expected
    }
    assert drifted == {}


def test_route_plan_source_does_not_import_facade() -> None:
    with open(route_plan.__file__, encoding="utf-8") as handle:
        source = handle.read()
    assert "_proxy_attr" not in source
    assert "import codex_proxy" not in source
    assert "_request_header" not in source


def test_gateway_settings_source_does_not_import_facade() -> None:
    with open(gateway_settings.__file__, encoding="utf-8") as handle:
        source = handle.read()
    assert "import codex_proxy" not in source
    assert "from codex_proxy" not in source


def test_collaboration_adapter_does_not_import_facade_or_transport() -> None:
    with open(collaboration_adapter.__file__, encoding="utf-8") as handle:
        source = handle.read()
    assert "import codex_proxy" not in source
    assert "from codex_proxy" not in source
    assert "gateway_sse" not in source
    assert "transport" not in source


def test_tool_surface_adapter_does_not_import_facade_or_transport() -> None:
    with open(tool_surface_adapter.__file__, encoding="utf-8") as handle:
        source = handle.read()
    assert "import codex_proxy" not in source
    assert "from codex_proxy" not in source
    assert "gateway_sse" not in source
    assert "transport" not in source
    assert "BaseHTTPRequestHandler" not in source
    assert "urllib3" not in source
    assert "urlopen" not in source


def test_apply_patch_adapter_does_not_import_facade_or_transport() -> None:
    with open(apply_patch_adapter.__file__, encoding="utf-8") as handle:
        source = handle.read()
    assert "import codex_proxy" not in source
    assert "from codex_proxy" not in source
    assert "gateway_sse" not in source
    assert "transport" not in source
    assert "BaseHTTPRequestHandler" not in source
    assert "urllib3" not in source
    assert "urlopen" not in source


def test_gateway_transport_does_not_import_facade_handler_or_sse() -> None:
    with open(gateway_transport.__file__, encoding="utf-8") as handle:
        source = handle.read()
    assert "import codex_proxy" not in source
    assert "from codex_proxy" not in source
    assert "gateway_sse" not in source
    assert "BaseHTTPRequestHandler" not in source
    assert "CodexProxyHandler" not in source
    assert "class GatewayTransport" in source
    assert "TransportPolicy.OFFICIAL_KEEPALIVE" in source


def test_downstream_stream_commit_lives_in_gateway_sse() -> None:
    assert gateway_sse.DownstreamStreamCommit is codex_proxy._GatewayDownstreamStreamCommit
    with open(gateway_sse.__file__, encoding="utf-8") as handle:
        sse_source = handle.read()
    with open(codex_proxy.__file__, encoding="utf-8") as handle:
        facade_source = handle.read()
    assert "class DownstreamStreamCommit" in sse_source
    assert "class _GatewayDownstreamStreamCommit" not in facade_source
    assert "class DownstreamStreamCommit" not in facade_source
    assert "import codex_proxy" not in sse_source
    assert "_payload_exposes_downstream_output" not in sse_source
    assert "_DEFAULT_TERMINAL_EVENT_TYPES" not in sse_source
    assert "_default_terminal_observer" not in sse_source
    assert "RESPONSES_TERMINAL_EVENT_TYPES" not in sse_source
    assert "safe_upstream_error_detail" not in sse_source


def _sample_retry_execution_plan() -> route_plan.RetryExecutionPlan:
    return route_plan.RetryExecutionPlan(
        eligibility=route_primitives.CapabilityState.SUPPORTED,
        policy=route_primitives.RetryPolicy.GATEWAY_FULL,
        request_kind=route_primitives.RETRY_REQUEST_MAIN_GENERATION,
        request_timeout_seconds=30,
        base_open_attempts=2,
        base_relay_attempts=2,
        failure_expansion_attempts=2,
        request_kind_attempts_configured=False,
        retry_http_errors=True,
        open_attempt_budget=None,
        capacity_elapsed_limit_seconds=0.0,
        stream_elapsed_limit_seconds=0.0,
        emit_downstream_retry_notice=False,
        pre_response_budget_seconds=None,
        lifecycle_final_retry_eligible=False,
    )


def test_retry_delay_seconds_rejects_exc_parameter() -> None:
    plan = _sample_retry_execution_plan()
    assert "exc" not in inspect.signature(plan.retry_delay_seconds).parameters
    with pytest.raises(TypeError):
        plan.retry_delay_seconds(
            1,
            failure_class=route_primitives.RETRY_FAILURE_QUICK_TRANSIENT,
            exc=RuntimeError("leftover Retry-After caller"),
        )
    assert (
        plan.retry_delay_seconds(
            1,
            failure_class=route_primitives.RETRY_FAILURE_QUICK_TRANSIENT,
            retry_after_seconds=7,
        )
        == 7
    )


def test_invalid_codec_still_raises_when_planning_event_sink_is_none(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(route_plan, "_planning_event_sink", None)
    with caplog.at_level(logging.WARNING, logger="route_plan"):
        with pytest.raises(gateway_errors.UpstreamProtocolTranslationError) as raised:
            route_plan._external_native_responses_tool_codec(
                {"native_responses_tool_codec": "not-a-codec"}
            )
    assert raised.value.cause.code == route_plan.NATIVE_RESPONSES_TOOL_CODEC_ERROR_CODE
    assert "native_responses_tool_codec_rejected" in caplog.text


def test_handler_authorization_uses_facade_get_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CODEX_PROXY_GATEWAY_CLIENT_KEY", "local-key")
    seen: list[str] = []
    real_get_header = codex_proxy._get_header

    def wrapped_get_header(headers: object, name: str) -> str | None:
        seen.append(name)
        return real_get_header(headers, name)

    monkeypatch.setattr(codex_proxy, "_get_header", wrapped_get_header)
    assert codex_proxy._local_request_authorized(
        {"Authorization": "Bearer local-key"},
        {"client_id": "unknown"},
    )
    assert not codex_proxy._local_request_authorized(
        {"Authorization": "Bearer wrong"},
        {"client_id": "unknown"},
    )
    assert "Authorization" in seen
    assert not hasattr(route_plan, "_request_header")
    assert not hasattr(route_plan, "_local_request_authorized")
    assert getattr(codex_proxy, "_local_request_authorized") is not getattr(
        route_plan, "_local_request_authorized", None
    )
