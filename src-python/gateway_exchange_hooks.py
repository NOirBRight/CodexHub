"""Handler-facing ExchangeHooks factory for Gateway POST requests."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.request import Request

from gateway_error_dispatch import PostRequestLiveState
from gateway_exchange import (
    ExchangeFailureTypes,
    ExchangeHooks,
    OpenExchangeRequest,
    RelayExchangeRequest,
)
from http.client import IncompleteRead
from protocol_translation import PreparedExchange
from route_plan import RouteAttemptPlan
from urllib.error import HTTPError


def _handler_impl() -> Any:
    import gateway_handler_impl as module

    return module


def build_post_exchange_hooks(live: PostRequestLiveState) -> ExchangeHooks:
    """Assemble live-patchable ExchangeHooks for one POST request."""

    gi = _handler_impl()
    write_proxy_event = gi.write_proxy_event
    _request_observability_with_prefix = gi._request_observability_with_prefix
    _route_attempt_event_fields = gi._route_attempt_event_fields
    _rewrite_transparent_developer_role_messages = gi._rewrite_transparent_developer_role_messages
    _normalize_transparent_tool_schema_booleans = gi._normalize_transparent_tool_schema_booleans
    _safe_json_mapping = gi._safe_json_mapping
    _excessive_transparent_responses_tool_loop_count = gi._excessive_transparent_responses_tool_loop_count
    _excessive_transparent_chat_tool_loop_count = gi._excessive_transparent_chat_tool_loop_count
    _gateway_transport = gi._gateway_transport
    _responses_body_with_lifecycle_final_retry_guidance = gi._responses_body_with_lifecycle_final_retry_guidance
    _write_adapter_event = gi._write_adapter_event
    _handler_downstream_stream_commit = gi._handler_downstream_stream_commit
    _open_upstream_response = gi._open_upstream_response
    _active_gateway_request = gi._active_gateway_request
    _retry_identity_from_context = gi._retry_identity_from_context
    safe_upstream_error_detail = gi.safe_upstream_error_detail
    _capture_usage = gi._capture_usage
    official_passthrough_request_body = gi.official_passthrough_request_body
    transparent_request_body = gi.transparent_request_body
    compatible_request_body = gi.compatible_request_body
    _downstream_has_been_exposed = gi._downstream_has_been_exposed
    _upstream_failure_class = gi._upstream_failure_class
    _retry_safety_class = gi._retry_safety_class
    _model_access_path_from_event_context = gi._model_access_path_from_event_context
    _retry_after_delay_seconds = gi._retry_after_delay_seconds
    _emit_upstream_retry_event = gi._emit_upstream_retry_event
    _emit_upstream_retry_suppressed_event = gi._emit_upstream_retry_suppressed_event
    _downstream_retry_payload = gi._downstream_retry_payload
    sleep_for_retry_with_gateway_cancellation = gi.sleep_for_retry_with_gateway_cancellation
    _SUPPRESSED_RETRY_SAFETY_CLASSES = gi._SUPPRESSED_RETRY_SAFETY_CLASSES
    _RUNTIME_TOOL_COMPATIBILITY_ATTEMPT_KEY = gi._RUNTIME_TOOL_COMPATIBILITY_ATTEMPT_KEY
    RUNTIME_CODEX_DIR = gi.RUNTIME_CODEX_DIR
    EXCESSIVE_TOOL_LOOP_ERROR_CODE = gi.EXCESSIVE_TOOL_LOOP_ERROR_CODE
    EXCESSIVE_TOOL_LOOP_BOUND = gi.EXCESSIVE_TOOL_LOOP_BOUND
    UpstreamProtocolTranslationError = gi.UpstreamProtocolTranslationError
    UnsupportedProtocolTranslationError = gi.UnsupportedProtocolTranslationError
    DownstreamClosedBeforeRetryError = gi.DownstreamClosedBeforeRetryError
    CompactEmptyResponseError = gi.CompactEmptyResponseError
    UpstreamStreamInterruptedError = gi.UpstreamStreamInterruptedError
    UpstreamStreamIdleTimeoutError = gi.UpstreamStreamIdleTimeoutError
    UpstreamStreamIncompleteError = gi.UpstreamStreamIncompleteError
    UpstreamStreamErrorEvent = gi.UpstreamStreamErrorEvent
    LifecycleEmptyFinalResponseError = gi.LifecycleEmptyFinalResponseError
    LifecycleFinalFormatResponseError = gi.LifecycleFinalFormatResponseError
    UpstreamEmptyCompletedResponseError = gi.UpstreamEmptyCompletedResponseError
    proxy_telemetry = gi.proxy_telemetry
    MutationPolicy = gi.MutationPolicy
    time = gi.time
    handler = live.handler
    inbound_format = live.inbound_format
    provider_hint = live.provider_hint
    request_id = live.request_id
    started_at = live.started_at
    admission = live.admission
    downstream_sse_started = live.downstream_sse_started
    adapter_event_context = live.adapter_event_context
    proxy_request_context = live.proxy_request_context
    model = live.model
    model_requested = live.model_requested
    model_canonical = live.model_canonical
    upstream = live.upstream
    upstream_name = live.upstream_name
    upstream_format = live.upstream_format
    reports_cached_input_tokens = live.reports_cached_input_tokens
    behavior_profile = live.behavior_profile
    route_reason = live.route_reason
    route_plan = live.route_plan
    caller_stream = live.caller_stream
    caller_request_observability = live.caller_request_observability
    usage_capture = live.usage_capture
    response_lifecycle_state = live.response_lifecycle_state
    prompt_cache_key = live.prompt_cache_key
    write_request_start_once = live.write_request_start_once
    emit_request_start_once = live.emit_request_start_once
    request_start_written = live.request_start_written
    primary_route_attempt = live.primary_route_attempt
    prepared_caller_body = live.prepared_caller_body
    active_route_attempt = live.active_route_attempt
    relay_execution_plan = live.relay_execution_plan
    request_observability = live.request_observability
    request_input = live.request_input
    inbound_payload = live.inbound_payload
    send_user_requested_shutdown = live.send_user_requested_shutdown
    finish_downstream_write_failure = live.finish_downstream_write_failure

    def request_observability_for_attempt(
        attempt: RouteAttemptPlan,
        attempt_body: bytes,
    ) -> dict[str, Any]:
        upstream_observability = proxy_telemetry.enrich_request_observability(
            body=attempt_body,
            codex_home=RUNTIME_CODEX_DIR,
            upstream=upstream,
            include_body_hmac=(
                attempt.request_mutation_policy
                != MutationPolicy.OFFICIAL_PASSTHROUGH
            ),
            prompt_cache_key=prompt_cache_key,
            extract_prompt_cache_key=(
                attempt.request_mutation_policy
                != MutationPolicy.OFFICIAL_PASSTHROUGH
            ),
        )
        return {
            **upstream_observability,
            **_request_observability_with_prefix(
                caller_request_observability, "caller"
            ),
            **_request_observability_with_prefix(
                upstream_observability, "upstream"
            ),
            "request_observability_scope": "executed_attempt",
            "request_observability_attempt_index": attempt.index,
            "request_observability_upstream_protocol": (
                attempt.upstream_protocol.value
            ),
        }

    def set_active_exchange(exchange: PreparedExchange) -> None:
        handler._active_prepared_exchange = exchange

    def activate_route_attempt(attempt: RouteAttemptPlan) -> None:
        attempt_fields = _route_attempt_event_fields(
            attempt,
            provider_id=route_plan.provider_id,
            model_requested=route_plan.model_requested,
            model_canonical=route_plan.canonical_model,
            upstream_model=route_plan.upstream_model,
        )
        proxy_request_context.update(attempt_fields)
        adapter_event_context.update(attempt_fields)
        adapter_event_context["tool_protocol"] = attempt.tool_protocol

    def rewrite_developer_roles(
        attempt_body: bytes,
        compatibility_upstream: Mapping[str, Any],
    ) -> tuple[bytes, int]:
        mutated, rewrites = _rewrite_transparent_developer_role_messages(
            attempt_body, compatibility_upstream
        )
        if rewrites:
            write_proxy_event(
                "developer_role_rewrite_applied",
                request_id=request_id,
                model=model_canonical,
                upstream=upstream_name,
                inbound_format=inbound_format,
                messages_rewritten=rewrites,
                **proxy_request_context,
            )
        return mutated, rewrites

    def normalize_tool_schema_booleans(
        attempt_body: bytes,
    ) -> tuple[bytes, int]:
        mutated, rewrites = _normalize_transparent_tool_schema_booleans(
            attempt_body
        )
        if rewrites:
            write_proxy_event(
                "tool_schema_boolean_normalized",
                request_id=request_id,
                model=model_canonical,
                upstream=upstream_name,
                inbound_format=inbound_format,
                schemas_rewritten=rewrites,
                **proxy_request_context,
            )
        return mutated, rewrites

    def validate_transparent_tool_loop(
        attempt_body: bytes,
        attempt_format: str,
    ) -> None:
        transparent_payload = _safe_json_mapping(attempt_body) or {}
        repeated_count = (
            _excessive_transparent_responses_tool_loop_count(
                transparent_payload
            )
            if attempt_format == "responses"
            else _excessive_transparent_chat_tool_loop_count(
                transparent_payload
            )
            if attempt_format == "chat_completions"
            else None
        )
        if repeated_count is not None:
            raise UpstreamProtocolTranslationError(
                UnsupportedProtocolTranslationError(
                    EXCESSIVE_TOOL_LOOP_ERROR_CODE,
                    "Repeated successful function calls exceeded "
                    f"the bound of {EXCESSIVE_TOOL_LOOP_BOUND}.",
                )
            )

    def build_attempt_request(
        attempt: RouteAttemptPlan,
        attempt_body: bytes,
    ) -> Request:
        return _gateway_transport().build_request_url(
            attempt.endpoint_url,
            data=attempt_body,
            headers=attempt.request_headers.to_dict(),
            method="POST",
        )

    def lifecycle_guidance(
        attempt_body: bytes,
        reason: str,
    ) -> bytes:
        guided = _responses_body_with_lifecycle_final_retry_guidance(
            attempt_body, reason
        )
        _write_adapter_event(
            adapter_event_context,
            "lifecycle_final_retry_guidance_injected",
            upstream=upstream_name,
            upstream_format="responses",
            reason=reason,
        )
        return guided

    emit_retry_to_downstream = (
        primary_route_attempt.retry.emit_downstream_retry_notice
    )

    def emit_downstream_retry(payload: Mapping[str, Any]) -> bool:
        nonlocal downstream_sse_started
        if not emit_retry_to_downstream:
            return True
        if not downstream_sse_started:
            if not handler._send_sse_headers(200, upstream_name):
                return False
            downstream_sse_started = True
        if not handler._write_sse_event("codexhub.retry", payload):
            return False
        notice_fields = dict(proxy_request_context)
        notice_fields.update(
            {
                "request_id": request_id,
                "model": model_canonical,
                "model_requested": model_requested,
                "model_canonical": model_canonical,
                "upstream": upstream_name,
                "provider_id": upstream_name,
                "upstream_format": upstream_format,
                "behavior_profile": behavior_profile,
                "route_reason": route_reason,
                "route_mode": (
                    "official"
                    if upstream_name == "official"
                    else "codexhub"
                ),
                "inbound_format": inbound_format,
                "is_stream": caller_stream,
            }
        )
        retry_payload = dict(payload)
        retry_payload.pop("type", None)
        notice_fields.update(retry_payload)
        write_proxy_event("sse_retry_notice", **notice_fields)
        return True

    def set_upstream_format(selected_format: str) -> None:
        seam = _handler_downstream_stream_commit(handler)
        if seam is not None:
            seam.set_upstream_format(selected_format)

    def attach_upstream(response: Any) -> None:
        seam = _handler_downstream_stream_commit(handler)
        if seam is not None:
            seam.attach_upstream(response)

    def open_exchange(
        opening: OpenExchangeRequest,
    ) -> Any:
        return _open_upstream_response(
            opening.request,
            upstream_name=opening.upstream_name,
            upstream_format=opening.upstream_format,
            timeout=opening.attempt.retry.request_timeout_seconds,
            event_context=opening.event_context,
            downstream_retry_callback=(
                opening.downstream_retry_callback
            ),
            retry_execution=opening.attempt.retry,
            transport_policy=opening.attempt.transport_policy,
            downstream_exposed=opening.downstream_exposed,
            pre_response_deadline=opening.pre_response_deadline,
            open_attempt_budget=opening.open_attempt_budget,
        )

    def relay_exchange(
        response: Any,
        relay: RelayExchangeRequest,
    ) -> int:
        return handler._relay_upstream_response(
            response,
            relay.upstream_name,
            request_id=relay.request_id,
            model=relay.model,
            inbound_format=relay.inbound_format,
            caller_stream=relay.caller_stream,
            event_context=relay.event_context,
            usage_capture=relay.usage_capture,
            headers_already_sent=relay.headers_already_sent,
            defer_stream_errors=relay.defer_stream_errors,
            mark_downstream_sse_started=(
                relay.mark_downstream_sse_started
            ),
            response_lifecycle_state=relay.response_lifecycle_state,
            relay_execution_plan=relay.relay_plan,
        )

    def raise_if_cancelled() -> None:
        active_request = _active_gateway_request()
        if active_request is not None:
            active_request.raise_if_cancelled()

    def protocol_fallback(
        failed_attempt: RouteAttemptPlan,
        next_attempt: RouteAttemptPlan,
        exc: BaseException,
        attempt_observability: Mapping[str, Any],
    ) -> None:
        failed = failed_attempt.telemetry_snapshot()
        following = next_attempt.telemetry_snapshot()
        write_proxy_event(
            "upstream_protocol_fallback",
            request_id=request_id,
            model=model_canonical,
            model_requested=model_requested,
            model_canonical=model_canonical,
            upstream=upstream_name,
            provider_id=upstream_name,
            provider_hint=provider_hint,
            upstream_format=(
                route_plan.configured_upstream_protocol_name
            ),
            behavior_profile=behavior_profile,
            failed_upstream_format=(
                failed_attempt.selected_upstream_format
            ),
            next_upstream_format=(
                next_attempt.selected_upstream_format
            ),
            failed_route_attempt_index=failed["index"],
            failed_route_attempt_request_body_mode=(
                failed["request_body_mode"]
            ),
            failed_route_attempt_request_conversion_steps=(
                failed["request_conversion_steps"]
            ),
            failed_route_attempt_mutation_summary=(
                failed["mutation_summary"]
            ),
            next_route_attempt_index=following["index"],
            next_route_attempt_request_body_mode=(
                following["request_body_mode"]
            ),
            next_route_attempt_request_conversion_steps=(
                following["request_conversion_steps"]
            ),
            next_route_attempt_mutation_summary=(
                following["mutation_summary"]
            ),
            status=getattr(exc, "code", 502),
            error="HTTPError",
            detail=safe_upstream_error_detail(
                exc,
                redact_identity=_retry_identity_from_context(
                    adapter_event_context
                ),
            ),
            **dict(attempt_observability),
            **proxy_request_context,
        )

    def exchange_error_status(exc: BaseException) -> int | None:
        status_value = getattr(exc, "code", None)
        return status_value if isinstance(status_value, int) else None

    def handle_empty_completed(exc: BaseException) -> bool:
        detail = (
            "Upstream Responses stream completed without visible "
            "output or tool calls."
        )
        write_proxy_event(
            "upstream_empty_completed_response",
            request_id=request_id,
            model=model,
            upstream=upstream_name,
            status=502,
            upstream_format=upstream_format,
            inbound_format=inbound_format,
            terminal_seen=False,
            completed_seen=True,
            visible_or_tool_output_seen=False,
            completed_tool_calls=0,
            pending_downstream_lines=0,
            pending_downstream_bytes=0,
            last_event_type="response.completed",
        )
        wrote = handler._write_downstream_sse_error(
            inbound_format=inbound_format,
            upstream_name=upstream_name,
            status=502,
            error="upstream_empty_completed_response",
            detail=detail,
            redact_identity=_retry_identity_from_context(
                adapter_event_context
            ),
        )
        _capture_usage(
            usage_capture,
            None,
            missing_reason="empty_completed_response",
        )
        return wrote

    exchange_hooks = ExchangeHooks(
        failure_types=ExchangeFailureTypes(
            downstream_closed_before_retry=(
                DownstreamClosedBeforeRetryError
            ),
            incomplete_read=IncompleteRead,
            protocol_fallback_error=HTTPError,
            compact_empty=CompactEmptyResponseError,
            stream_interrupted=UpstreamStreamInterruptedError,
            stream_idle_timeout=UpstreamStreamIdleTimeoutError,
            stream_incomplete=UpstreamStreamIncompleteError,
            stream_error_event=UpstreamStreamErrorEvent,
            lifecycle_empty_final=LifecycleEmptyFinalResponseError,
            lifecycle_final_format=LifecycleFinalFormatResponseError,
            upstream_empty_completed=(
                UpstreamEmptyCompletedResponseError
            ),
        ),
        set_active_prepared_exchange=set_active_exchange,
        activate_attempt=activate_route_attempt,
        safe_json_mapping=_safe_json_mapping,
        official_mutation=official_passthrough_request_body,
        transparent_mutation=transparent_request_body,
        rewrite_developer_roles=rewrite_developer_roles,
        normalize_tool_schema_booleans=(
            normalize_tool_schema_booleans
        ),
        validate_transparent_tool_loop=(
            validate_transparent_tool_loop
        ),
        compatibility_mutation=compatible_request_body,
        request_observability=request_observability_for_attempt,
        emit_request_start=emit_request_start_once,
        build_request=build_attempt_request,
        lifecycle_guidance=lifecycle_guidance,
        open_response=open_exchange,
        relay_response=relay_exchange,
        set_upstream_format=set_upstream_format,
        attach_upstream=attach_upstream,
        downstream_exposed=lambda: _downstream_has_been_exposed(
            handler
        ),
        raise_if_cancelled=raise_if_cancelled,
        emit_downstream_retry=emit_downstream_retry,
        finish_downstream_failure=finish_downstream_write_failure,
        failure_class=_upstream_failure_class,
        retry_safety_class=_retry_safety_class,
        model_access_path=_model_access_path_from_event_context,
        retry_after_seconds=_retry_after_delay_seconds,
        emit_retry=_emit_upstream_retry_event,
        emit_retry_suppressed=(
            _emit_upstream_retry_suppressed_event
        ),
        downstream_retry_payload=_downstream_retry_payload,
        retry_identity=_retry_identity_from_context,
        sleep=sleep_for_retry_with_gateway_cancellation,
        protocol_fallback=protocol_fallback,
        error_status=exchange_error_status,
        handle_empty_completed=handle_empty_completed,
        monotonic=time.monotonic,
        suppressed_retry_safety_classes=(
            _SUPPRESSED_RETRY_SAFETY_CLASSES
        ),
        runtime_attempt_key=(
            _RUNTIME_TOOL_COMPATIBILITY_ATTEMPT_KEY
        ),
    )
    return exchange_hooks
