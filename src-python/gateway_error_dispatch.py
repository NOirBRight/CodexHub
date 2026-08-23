"""Exception-to-response mapping for Gateway POST proxy requests."""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from http.client import IncompleteRead
from typing import Any
from urllib.error import HTTPError, URLError

import apply_patch_adapter
import catalog
import gateway_admission
import gateway_errors
import gateway_events
import gateway_exchange_bindings
import gateway_request
import gateway_transport
import route_primitives


@dataclass
class PostRequestLiveState:
    """Mutable POST-request context shared by exchange hooks and error dispatch."""

    handler: Any
    inbound_format: Any = None
    provider_hint: Any = None
    request_id: Any = None
    started_at: Any = None
    admission: Any = None
    downstream_sse_started: Any = None
    adapter_event_context: Any = None
    proxy_request_context: Any = None
    model: Any = None
    model_requested: Any = None
    model_canonical: Any = None
    upstream: Any = None
    upstream_name: Any = None
    upstream_format: Any = None
    reports_cached_input_tokens: Any = None
    behavior_profile: Any = None
    route_reason: Any = None
    route_plan: Any = None
    caller_stream: Any = None
    caller_request_observability: Any = None
    usage_capture: Any = None
    response_lifecycle_state: Any = None
    prompt_cache_key: Any = None
    write_request_start_once: Any = None
    emit_request_start_once: Any = None
    request_start_written: Any = None
    primary_route_attempt: Any = None
    prepared_caller_body: Any = None
    active_route_attempt: Any = None
    relay_execution_plan: Any = None
    request_observability: Any = None
    request_input: Any = None
    inbound_payload: Any = None
    send_user_requested_shutdown: Any = None
    finish_downstream_write_failure: Any = None



def finish_proxy_post_downstream_write_failure(
    live: PostRequestLiveState,
    *,
    write_exc: OSError | None = None,
) -> None:
    """Close the downstream commit seam and emit the 499 complete event."""

    handler = live.handler
    exc = write_exc or OSError("downstream closed")
    failure_seam = gateway_exchange_bindings.handler_downstream_stream_commit(handler)
    if failure_seam is not None:
        failure_seam.close()
    handler.close_connection = True
    gateway_events.write_proxy_event(
        "downstream_stream_closed",
        request_id=live.request_id,
        model=catalog.canonical_model_id(live.model) if live.model else None,
        model_requested=live.model_requested,
        upstream=live.upstream_name or "upstream_error",
        provider_hint=live.provider_hint,
        upstream_format=live.upstream_format,
        behavior_profile=live.behavior_profile,
        inbound_format=live.inbound_format,
        status=499,
        error=type(exc).__name__,
        detail=gateway_errors.safe_upstream_error_detail(exc),
        **(live.proxy_request_context or {}),
    )
    gateway_events.write_proxy_event(
        "request_complete",
        request_id=live.request_id,
        method="POST",
        model=catalog.canonical_model_id(live.model) if live.model else None,
        model_requested=live.model_requested,
        model_canonical=catalog.canonical_model_id(live.model) if live.model else None,
        upstream=live.upstream_name or "upstream_error",
        provider_id=live.upstream_name,
        provider_hint=live.provider_hint,
        upstream_format=live.upstream_format,
        reports_cached_input_tokens=live.reports_cached_input_tokens,
        behavior_profile=live.behavior_profile,
        inbound_format=live.inbound_format,
        route_reason=live.route_reason,
        route_mode="official" if live.upstream_name == "official" else "codexhub",
        is_stream=live.caller_stream,
        status=499,
        duration_ms=int((time.monotonic() - live.started_at) * 1000),
        **(live.request_observability or {}),
        **(live.usage_capture or {}),
        **(live.proxy_request_context or {}),
    )


def emit_proxy_post_success(live: PostRequestLiveState, status: int) -> None:
    """Emit the successful POST request_complete event."""

    gateway_events.write_proxy_event(
        "request_complete",
        request_id=live.request_id,
        method="POST",
        model=live.model_canonical,
        model_requested=live.model_requested,
        model_canonical=live.model_canonical,
        upstream=live.upstream_name,
        provider_id=live.upstream_name,
        provider_hint=live.provider_hint,
        upstream_format=live.upstream_format,
        reports_cached_input_tokens=live.reports_cached_input_tokens,
        behavior_profile=live.behavior_profile,
        inbound_format=live.inbound_format,
        route_reason=live.route_reason,
        route_mode="official" if live.upstream_name == "official" else "codexhub",
        is_stream=live.caller_stream,
        status=status,
        duration_ms=int((time.monotonic() - live.started_at) * 1000),
        **(live.request_observability or {}),
        **(live.usage_capture or {}),
        **(live.proxy_request_context or {}),
    )


def dispatch_proxy_post_exception(exc: BaseException, live: PostRequestLiveState) -> None:
    """Map a POST-request exception onto JSON or SSE downstream errors."""

    write_proxy_event = gateway_events.write_proxy_event
    _retry_identity_from_context = gateway_transport.retry_identity_from_context
    safe_upstream_error_detail = gateway_errors.safe_upstream_error_detail
    _redact_identity_in_text = gateway_errors.redact_identity_in_text
    _request_observability_with_prefix = gateway_request.request_observability_with_prefix
    _responses_failed_event_for_stream_error = gateway_errors.responses_failed_event_for_stream_error
    canonical_model_id = catalog.canonical_model_id
    APPLY_PATCH_ADAPTER_ERROR_CODE = apply_patch_adapter.APPLY_PATCH_ADAPTER_ERROR_CODE
    transport_failure_phase = gateway_transport.transport_failure_phase
    logger = logging.getLogger("codex_proxy")
    GatewayUserRequestedShutdown = gateway_admission.GatewayUserRequestedShutdown
    CompactEmptyResponseError = gateway_errors.CompactEmptyResponseError
    LifecycleEmptyFinalResponseError = gateway_errors.LifecycleEmptyFinalResponseError
    LifecycleFinalFormatResponseError = gateway_errors.LifecycleFinalFormatResponseError
    ModelIdentityResolutionError = gateway_errors.ModelIdentityResolutionError
    ImageProxyError = gateway_errors.ImageProxyError
    UpstreamProtocolTranslationError = gateway_errors.UpstreamProtocolTranslationError
    GatewayPreResponseBudgetExhausted = gateway_errors.GatewayPreResponseBudgetExhausted
    RETRY_FAILURE_PERMANENT = route_primitives.RETRY_FAILURE_PERMANENT
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

    if isinstance(exc, GatewayUserRequestedShutdown):
        send_user_requested_shutdown()
        return

    if isinstance(exc, CompactEmptyResponseError):
        if admission.cancelled:
            send_user_requested_shutdown()
            return
        identity = _retry_identity_from_context(adapter_event_context)
        detail = safe_upstream_error_detail(exc, redact_identity=identity)
        write_proxy_event(
            "request_error",
            request_id=request_id,
            model=canonical_model_id(model) if model else None,
            model_requested=model_requested,
            upstream=upstream_name,
            provider_hint=provider_hint,
            upstream_format=upstream_format,
            behavior_profile=behavior_profile,
            inbound_format=inbound_format,
            status=502,
            error="compact_empty_response",
            detail=detail,
            duration_ms=int((time.monotonic() - started_at) * 1000),
            **proxy_request_context,
        )
        if downstream_sse_started:
            if not handler._write_downstream_sse_error(
                inbound_format=inbound_format,
                upstream_name=upstream_name or "upstream_error",
                status=502,
                exc=exc,
                error="compact_empty_response",
                detail=detail,
                redact_identity=identity,
            ):
                finish_downstream_write_failure()
            return
        handler._safe_send_downstream_json_error(
            502,
            inbound_format=inbound_format,
            upstream_name=upstream_name or "upstream_error",
            request_id=request_id,
            exc=exc,
            error="compact_empty_response",
            detail=detail,
            redact_identity=identity,
            error_type="compact_empty_response",
        )
        return

    if isinstance(exc, (LifecycleEmptyFinalResponseError, LifecycleFinalFormatResponseError)):
        if admission.cancelled:
            send_user_requested_shutdown()
            return
        identity = _retry_identity_from_context(adapter_event_context)
        detail = safe_upstream_error_detail(exc, redact_identity=identity)
        error_code = (
            "lifecycle_empty_final_response"
            if isinstance(exc, LifecycleEmptyFinalResponseError)
            else "lifecycle_final_format_response"
        )
        write_proxy_event(
            "request_error",
            request_id=request_id,
            model=canonical_model_id(model) if model else None,
            model_requested=model_requested,
            upstream=upstream_name,
            provider_hint=provider_hint,
            upstream_format=upstream_format,
            inbound_format=inbound_format,
            status=502,
            error=error_code,
            detail=detail,
            duration_ms=int((time.monotonic() - started_at) * 1000),
            **proxy_request_context,
        )
        if downstream_sse_started:
            if not handler._write_downstream_sse_error(
                inbound_format=inbound_format,
                upstream_name=upstream_name or "upstream_error",
                status=502,
                exc=exc,
                error=error_code,
                detail=detail,
                redact_identity=identity,
            ):
                finish_downstream_write_failure()
            return
        handler._safe_send_downstream_json_error(
            502,
            inbound_format=inbound_format,
            upstream_name=upstream_name or "upstream_error",
            request_id=request_id,
            exc=exc,
            error=error_code,
            detail=detail,
            redact_identity=identity,
            error_type=error_code,
        )
        return

    if isinstance(exc, ModelIdentityResolutionError):
        if admission.cancelled:
            send_user_requested_shutdown()
            return
        error_code = "model_identity_error"
        identity = _retry_identity_from_context(adapter_event_context)
        detail = safe_upstream_error_detail(exc, redact_identity=identity)
        detail = _redact_identity_in_text(detail, exc.model_slug)
        write_proxy_event(
            "request_error",
            request_id=request_id,
            model=canonical_model_id(model) if model else None,
            model_requested=model_requested,
            upstream=upstream_name or "gateway",
            provider_hint=provider_hint,
            upstream_format=upstream_format,
            behavior_profile=behavior_profile,
            inbound_format=inbound_format,
            status=400,
            error=error_code,
            detail=detail,
            identity_classification=exc.classification,
            identity_reason=exc.reason,
            duration_ms=int((time.monotonic() - started_at) * 1000),
            **proxy_request_context,
        )
        if downstream_sse_started:
            if not handler._write_downstream_sse_error(
                inbound_format=inbound_format,
                upstream_name=upstream_name or "gateway",
                status=400,
                exc=exc,
                error=error_code,
                detail=detail,
                error_type=error_code,
                redact_identity=identity,
                preserve_explicit_error=True,
            ):
                finish_downstream_write_failure()
            return
        handler._safe_send_downstream_json_error(
            400,
            inbound_format=inbound_format,
            upstream_name=upstream_name or "gateway",
            request_id=request_id,
            exc=exc,
            error=error_code,
            detail=detail,
            error_type=error_code,
            redact_identity=identity,
        )
        return

    if isinstance(exc, ImageProxyError):
        if admission.cancelled:
            send_user_requested_shutdown()
            return
        identity = _retry_identity_from_context(adapter_event_context)
        detail = _redact_identity_in_text(str(exc)[:300], identity)
        if not request_start_written and callable(write_request_start_once):
            fallback_request_observability = {
                **caller_request_observability,
                **_request_observability_with_prefix(caller_request_observability, "caller"),
            }
            write_request_start_once(fallback_request_observability)
        write_proxy_event(
            "request_error",
            request_id=request_id,
            model=canonical_model_id(model) if model else None,
            model_requested=model_requested,
            upstream=upstream_name,
            provider_hint=provider_hint,
            upstream_format=upstream_format,
            behavior_profile=behavior_profile,
            inbound_format=inbound_format,
            status=502,
            error=type(exc).__name__,
            detail=detail,
            duration_ms=int((time.monotonic() - started_at) * 1000),
            **proxy_request_context,
        )
        if downstream_sse_started:
            if not handler._write_downstream_sse_error(
                inbound_format=inbound_format,
                upstream_name=upstream_name or "upstream_error",
                status=502,
                exc=exc,
                error="image_proxy_error",
                detail=detail,
                redact_identity=identity,
            ):
                finish_downstream_write_failure()
            return
        handler._safe_send_downstream_json_error(
            502,
            inbound_format=inbound_format,
            upstream_name=upstream_name or "upstream_error",
            request_id=request_id,
            exc=exc,
            error="image_proxy_error",
            detail=detail,
            redact_identity=identity,
            error_type="image_proxy_error",
        )
        return

    if isinstance(exc, UpstreamProtocolTranslationError):
        if admission.cancelled:
            send_user_requested_shutdown()
            return
        identity = _retry_identity_from_context(adapter_event_context)
        detail = _redact_identity_in_text(str(exc), identity)
        error_code = exc.cause.code
        is_apply_patch_adapter_error = error_code == APPLY_PATCH_ADAPTER_ERROR_CODE
        error_status = 400
        json_error_type = "invalid_request_error" if is_apply_patch_adapter_error else error_code
        sse_error_type = "invalid_request_error" if is_apply_patch_adapter_error else "upstream_error"
        selected_native_responses_tool_codec = (
            active_route_attempt.native_responses_tool_codec
            if active_route_attempt is not None
            else "none"
        )
        write_proxy_event(
            "request_error",
            request_id=request_id,
            model=canonical_model_id(model) if model else None,
            model_requested=model_requested,
            upstream=upstream_name,
            provider_hint=provider_hint,
            upstream_format=upstream_format,
            behavior_profile=behavior_profile,
            inbound_format=inbound_format,
            status=error_status,
            error=error_code,
            detail=detail[:300],
            duration_ms=int((time.monotonic() - started_at) * 1000),
            **proxy_request_context,
        )
        if downstream_sse_started:
            if inbound_format == "responses":
                if not handler._write_sse_event(
                    "response.failed",
                    _responses_failed_event_for_stream_error(
                        upstream_name=upstream_name or "upstream_error",
                        model=canonical_model_id(model) if model else None,
                        status=error_status,
                        exc=exc,
                        error=error_code,
                        detail=detail,
                        response_id=response_lifecycle_state.get("response_id"),
                        redact_identity=identity,
                    ),
                ):
                    finish_downstream_write_failure()
                    return
                if (
                    is_apply_patch_adapter_error
                    and selected_native_responses_tool_codec == "strict_apply_patch"
                ):
                    write_proxy_event(
                        "third_party_apply_patch_terminal",
                        request_id=request_id,
                        model=canonical_model_id(model) if model else None,
                        upstream=upstream_name,
                        codec=selected_native_responses_tool_codec,
                        disposition="response.failed",
                        failure_class=RETRY_FAILURE_PERMANENT,
                        retry_count=0,
                    )
                handler.close_connection = True
            else:
                if not handler._write_downstream_sse_error(
                    inbound_format=inbound_format,
                    upstream_name=upstream_name or "upstream_error",
                    status=error_status,
                    exc=exc,
                    error=error_code,
                    detail=detail,
                    error_type=sse_error_type,
                    preserve_explicit_error=True,
                    redact_identity=identity,
                ):
                    finish_downstream_write_failure()
                    return
            return
        handler._safe_send_downstream_json_error(
            error_status,
            inbound_format=inbound_format,
            upstream_name=upstream_name or "upstream_error",
            request_id=request_id,
            exc=exc,
            error=error_code,
            detail=detail,
            redact_identity=identity,
            error_type=json_error_type,
        )
        return

    if isinstance(exc, ValueError):
        if admission.cancelled:
            send_user_requested_shutdown()
            return
        identity = _retry_identity_from_context(adapter_event_context)
        detail = _redact_identity_in_text(str(exc)[:300], identity)
        write_proxy_event(
            "request_error",
            request_id=request_id,
            model=canonical_model_id(model) if model else None,
            model_requested=model_requested,
            upstream=upstream_name,
            provider_hint=provider_hint,
            upstream_format=upstream_format,
            behavior_profile=behavior_profile,
            inbound_format=inbound_format,
            status=400,
            error=type(exc).__name__,
            detail=detail,
            duration_ms=int((time.monotonic() - started_at) * 1000),
            **proxy_request_context,
        )
        handler._safe_send_downstream_json_error(
            400,
            inbound_format=inbound_format,
            upstream_name=upstream_name or provider_hint or "gateway",
            request_id=request_id,
            exc=exc,
            error=type(exc).__name__,
            detail=detail,
            redact_identity=identity,
            error_type="invalid_request_error",
        )
        return

    if isinstance(exc, HTTPError):
        if admission.cancelled:
            send_user_requested_shutdown()
            return
        identity = _retry_identity_from_context(adapter_event_context)
        if downstream_sse_started:
            if not handler._write_downstream_sse_error(
                inbound_format=inbound_format,
                upstream_name=upstream_name or "upstream_error",
                status=getattr(exc, "code", 502),
                exc=exc,
                redact_identity=identity,
            ):
                finish_downstream_write_failure()
                return
            write_proxy_event(
                "request_complete",
                request_id=request_id,
                method="POST",
                model=canonical_model_id(model) if model else None,
                model_requested=model_requested,
                model_canonical=canonical_model_id(model) if model else None,
                upstream=upstream_name,
                provider_id=upstream_name,
                provider_hint=provider_hint,
                upstream_format=upstream_format,
                reports_cached_input_tokens=reports_cached_input_tokens,
                behavior_profile=behavior_profile,
                inbound_format=inbound_format,
                route_reason=route_reason,
                route_mode="official" if upstream_name == "official" else "codexhub",
                is_stream=caller_stream,
                status=getattr(exc, "code", 502),
                duration_ms=int((time.monotonic() - started_at) * 1000),
                **request_observability,
                **usage_capture,
                **proxy_request_context,
            )
            return
        try:
            if relay_execution_plan is None:
                raise RuntimeError(
                    "upstream response arrived without a planned relay "
                    "execution contract"
                ) from exc
            previous_retry_identity = (
                adapter_event_context.get("_retry_attempt_identity")
                if isinstance(adapter_event_context, Mapping)
                else None
            )
            adapter_event_context = {
                "request_id": request_id,
                "model": canonical_model_id(model) if model else None,
                "behavior_profile": behavior_profile,
                **proxy_request_context,
            }
            if isinstance(previous_retry_identity, str) and previous_retry_identity:
                adapter_event_context["_retry_attempt_identity"] = previous_retry_identity
            status = handler._relay_upstream_response(
                exc,
                upstream_name or "upstream_error",
                request_id=request_id,
                model=canonical_model_id(model) if model else None,
                inbound_format=inbound_format,
                caller_stream=caller_stream,
                event_context=adapter_event_context,
                usage_capture=usage_capture,
                relay_execution_plan=relay_execution_plan,
            )
        except OSError as relay_exc:
            handler.close_connection = True
            write_proxy_event(
                "client_write_failed",
                request_id=request_id,
                model=canonical_model_id(model) if model else None,
                upstream=upstream_name,
                upstream_format=upstream_format,
                behavior_profile=behavior_profile,
                status=getattr(exc, "code", 502),
                error=type(relay_exc).__name__,
                detail=safe_upstream_error_detail(relay_exc),
                duration_ms=int((time.monotonic() - started_at) * 1000),
                **proxy_request_context,
            )
            return
        write_proxy_event(
            "request_complete",
            request_id=request_id,
            method="POST",
            model=canonical_model_id(model) if model else None,
            model_requested=model_requested,
            model_canonical=canonical_model_id(model) if model else None,
            upstream=upstream_name,
            provider_id=upstream_name,
            provider_hint=provider_hint,
            upstream_format=upstream_format,
            reports_cached_input_tokens=reports_cached_input_tokens,
            behavior_profile=behavior_profile,
            inbound_format=inbound_format,
            route_reason=route_reason,
            route_mode="official" if upstream_name == "official" else "codexhub",
            is_stream=caller_stream,
            status=status,
            duration_ms=int((time.monotonic() - started_at) * 1000),
            **request_observability,
            **usage_capture,
            **proxy_request_context,
        )
        return

    if isinstance(exc, GatewayPreResponseBudgetExhausted):
        if admission.cancelled:
            send_user_requested_shutdown()
            return
        error_code = "gateway_pre_response_budget_exhausted"
        detail = "Gateway pre-response budget exhausted before a usable upstream response."
        event_fields = {
            "failure_phase": exc.phase,
            "attempt": exc.attempt,
            "pre_response_budget_ms": int(exc.budget_seconds * 1000),
            "retryable": False,
        }
        write_proxy_event(
            "request_error",
            request_id=request_id,
            model=canonical_model_id(model) if model else None,
            model_requested=model_requested,
            upstream=upstream_name,
            upstream_format=upstream_format,
            behavior_profile=behavior_profile,
            inbound_format=inbound_format,
            status=504,
            error=error_code,
            detail=detail,
            duration_ms=int((time.monotonic() - started_at) * 1000),
            **event_fields,
            **proxy_request_context,
        )
        handler._safe_send_downstream_json_error(
            504,
            inbound_format=inbound_format,
            upstream_name=upstream_name or "gateway",
            request_id=request_id,
            error=error_code,
            detail=detail,
            error_type=error_code,
        )
        handler.close_connection = True
        write_proxy_event(
            "request_complete",
            request_id=request_id,
            method="POST",
            model=canonical_model_id(model) if model else None,
            model_requested=model_requested,
            model_canonical=canonical_model_id(model) if model else None,
            upstream=upstream_name,
            provider_id=upstream_name,
            provider_hint=provider_hint,
            upstream_format=upstream_format,
            reports_cached_input_tokens=reports_cached_input_tokens,
            behavior_profile=behavior_profile,
            inbound_format=inbound_format,
            route_reason=route_reason,
            route_mode="official" if upstream_name == "official" else "codexhub",
            is_stream=caller_stream,
            status=504,
            duration_ms=int((time.monotonic() - started_at) * 1000),
            **event_fields,
            **request_observability,
            **usage_capture,
            **proxy_request_context,
        )
        return

    if isinstance(exc, IncompleteRead):
        if admission.cancelled:
            send_user_requested_shutdown()
            return
        identity = _retry_identity_from_context(adapter_event_context)
        detail = safe_upstream_error_detail(exc, redact_identity=identity)
        write_proxy_event(
            "request_error",
            request_id=request_id,
            model=canonical_model_id(model) if model else None,
            upstream=upstream_name,
            upstream_format=upstream_format,
            behavior_profile=behavior_profile,
            status=502,
            error=type(exc).__name__,
            detail=detail,
            failure_phase=transport_failure_phase(exc),
            duration_ms=int((time.monotonic() - started_at) * 1000),
            **proxy_request_context,
        )
        if downstream_sse_started:
            if not handler._write_downstream_sse_error(
                inbound_format=inbound_format,
                upstream_name=upstream_name or "upstream_error",
                exc=exc,
                detail=detail,
                redact_identity=identity,
            ):
                finish_downstream_write_failure()
            return
        handler._safe_send_downstream_json_error(
            502,
            inbound_format=inbound_format,
            upstream_name=upstream_name or "upstream_error",
            request_id=request_id,
            exc=exc,
            detail=detail,
            redact_identity=identity,
        )
        return

    if isinstance(exc, (OSError, URLError)):
        if admission.cancelled:
            send_user_requested_shutdown()
            return
        identity = _retry_identity_from_context(adapter_event_context)
        detail = safe_upstream_error_detail(exc, redact_identity=identity)
        write_proxy_event(
            "request_error",
            request_id=request_id,
            model=canonical_model_id(model) if model else None,
            upstream=upstream_name,
            upstream_format=upstream_format,
            behavior_profile=behavior_profile,
            status=502,
            error=type(exc).__name__,
            detail=detail,
            failure_phase=transport_failure_phase(exc),
            duration_ms=int((time.monotonic() - started_at) * 1000),
            **proxy_request_context,
        )
        if downstream_sse_started:
            if not handler._write_downstream_sse_error(
                inbound_format=inbound_format,
                upstream_name=upstream_name or "upstream_error",
                exc=exc,
                detail=detail,
                redact_identity=identity,
            ):
                finish_downstream_write_failure()
            return
        handler._safe_send_downstream_json_error(
            502,
            inbound_format=inbound_format,
            upstream_name=upstream_name or "upstream_error",
            request_id=request_id,
            exc=exc,
            detail=detail,
            redact_identity=identity,
        )
        return

    if isinstance(exc, Exception):
        if admission.cancelled:
            send_user_requested_shutdown()
            return
        identity = _retry_identity_from_context(adapter_event_context)
        detail = safe_upstream_error_detail(exc, redact_identity=identity)
        logger.error("unexpected proxy error request_id=%s detail=%s", request_id, detail)
        write_proxy_event(
            "request_error",
            request_id=request_id,
            model=canonical_model_id(model) if model else None,
            upstream=upstream_name,
            upstream_format=upstream_format,
            behavior_profile=behavior_profile,
            status=500,
            error=type(exc).__name__,
            detail=detail,
            duration_ms=int((time.monotonic() - started_at) * 1000),
            **proxy_request_context,
        )
        if downstream_sse_started:
            if not handler._write_downstream_sse_error(
                inbound_format=inbound_format,
                upstream_name=upstream_name or "upstream_error",
                status=500,
                exc=exc,
                redact_identity=identity,
            ):
                finish_downstream_write_failure()
            return
        handler._safe_send_downstream_json_error(
            500,
            inbound_format=inbound_format,
            upstream_name=upstream_name or "upstream_error",
            request_id=request_id,
            exc=exc,
            detail=detail,
            redact_identity=identity,
        )
        return
