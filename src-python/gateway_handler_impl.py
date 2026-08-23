"""HTTP handler methods for the CodexHub Gateway.

`GatewayHandlerMixin` carries the request-handling method bodies; the entry
module (`codex_proxy`) mixes it into `CodexProxyHandler`. All collaborators are
imported from their owning modules; forwarder functions below read owning-module
attributes at call time so test patches on those modules stay live.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from collections.abc import Callable, Mapping
from http.client import IncompleteRead
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request

import apply_patch_adapter as _apply_patch_adapter_module
import gateway_admission
import gateway_catalog_runtime
import gateway_compat
import gateway_errors
import gateway_events
import gateway_settings
import gateway_sse
import gateway_stream_semantics
import gateway_transport
import proxy_telemetry
import route_plan as _route_plan_module
from apply_patch_adapter import APPLY_PATCH_ADAPTER_ERROR_CODE
from catalog import canonical_model_id
from gateway_admission import (
    GatewayUserRequestedShutdown,
    gateway_shutdown_controller_for_handler as _gateway_shutdown_controller_for_handler,
)
from gateway_compat.host import (
    EXCESSIVE_TOOL_LOOP_BOUND,
    EXCESSIVE_TOOL_LOOP_ERROR_CODE,
    _resolve_collaboration_boundary,
)
from gateway_errors import (
    CompactEmptyResponseError,
    DownstreamErrorSpec,
    GatewayPreResponseBudgetExhausted,
    ImageProxyError,
    LifecycleEmptyFinalResponseError,
    LifecycleFinalFormatResponseError,
    ModelIdentityResolutionError,
    UnqualifiedRouteProtocolError,
    UnsupportedRouteProtocolError,
    UpstreamProtocolTranslationError,
    UpstreamStreamIdleTimeoutError,
    _downstream_json_error_payload,
    _downstream_sse_error_payload_for_inbound_format,
    _local_gateway_auth_error_payload,
    _redact_identity_in_text,
    _responses_failed_event_for_stream_error,
    safe_upstream_error_detail,
    user_requested_shutdown_payload,
)
from gateway_exchange import (
    ExchangeFailureTypes,
    ExchangeHooks,
    ExchangeProgress,
    ExchangeRequest,
    InboundRequest,
    InboundRequestHooks,
    OpenExchangeRequest,
    ParsedInboundRequest as GatewayRequestInput,
    RelayExchangeRequest,
    _bind_downstream_stream_commit,
    _downstream_has_been_exposed,
    _handler_downstream_stream_commit,
    _relay_context_for_handler,
    _responses_synthetic_terminal_failure,
    execute_exchange,
    parse_inbound_request,
    terminal_result,
)
from gateway_relay import (
    RelayContext,
    SseLineRelayContext,
    iter_upstream_sse_lines,
    relay_official_passthrough_sse_response,
    relay_raw_response,
    relay_transparent_upstream_response,
    relay_upstream_response,
    send_sse_headers,
    write_non_streaming_body,
    write_sse_bytes,
    write_sse_data,
    write_sse_done,
    write_sse_event,
    write_sse_keepalive,
)
from gateway_request import (
    _UNSET_CONTENT_ENCODING,
    _filtered_response_headers,
    _is_event_stream,
    _json_response_bytes,
    _local_request_authorized,
    _reasoning_policy_for_request,
    _request_observability_with_prefix,
    _validate_reasoning_effort_for_upstream,
    _value_contains_image,
    _websocket_probe_frame_metadata,
    decoded_request_body,
    enforce_text_only_image_boundary,
    provider_scoped_route_model,
    raw_provider_probe_requested,
    request_context_from_headers,
    try_extract_model,
)
from gateway_sse import DownstreamStreamCommit
from gateway_stream_semantics import (
    DownstreamClosedBeforeRetryError,
    DownstreamClosedDuringImageProxyError,
    DownstreamKeepaliveFailedError,
    UpstreamEmptyCompletedResponseError,
    UpstreamStreamErrorEvent,
    UpstreamStreamInterruptedError,
    _request_kind_from_headers_and_payload,
    _sse_json_line,
)
from protocol_translation import (
    GatewayChatToResponsesStreamConverter as _ChatToResponsesStreamConverter,
    GatewayResponsesToChatStreamConverter as _ResponsesToChatStreamConverter,
    NonForwardable,
    PreparedExchange,
    UnsupportedProtocolTranslationError,
    UpstreamStreamIncompleteError,
)
from route_plan import (
    RelayExecutionPlan,
    RouteAttemptPlan,
    _route_attempt_event_fields,
    _route_plan_event_fields,
)
from route_primitives import (
    CapabilityState,
    MutationPolicy,
    RETRY_FAILURE_PERMANENT,
    RETRY_FAILURE_QUICK_TRANSIENT,
    RETRY_REQUEST_COMPACT,
    RETRY_REQUEST_MAIN_GENERATION,
    RETRY_REQUEST_OFFICIAL_CONTROL,
    RETRY_SAFETY_SUPPRESSED_POST_EXPOSURE,
    RETRY_SAFETY_SUPPRESSED_POST_WRITE,
    RouteProtocol,
    StreamingPolicy,
    TransportPolicy,
    UsagePolicy,
    VisionAction,
)
from runtime_tool_compatibility import (
    ToolCompatibilityError as RuntimeToolCompatibilityError,
)
from sse_events import (
    SseAssemblerClosedError,
    SseEvent,
    SseEventAssembler,
    SseFrameTooLargeError,
)
from gateway_events import RUNTIME_CODEX_DIR
from gateway_transport import (
    _SUPPRESSED_RETRY_SAFETY_CLASSES,
    UpstreamSseReaderLifecycle as _UpstreamSseReaderLifecycle,
    _get_header,
    _model_access_path_from_event_context,
    _retry_after_delay_seconds,
    _retry_identity_from_context,
    _upstream_failure_class,
    transport_failure_phase,
)
from websocket_transport import (
    WebSocketProtocolError,
    close_frame,
    read_frame,
    redacted_handshake_metadata,
    websocket_upgrade_response_headers,
    write_frame,
)

logger = logging.getLogger("codex_proxy")

_RUNTIME_TOOL_COMPATIBILITY_ATTEMPT_KEY = gateway_compat.official_passthrough._RUNTIME_TOOL_COMPATIBILITY_ATTEMPT_KEY


# ---------------------------------------------------------------------------
# Call-time forwarders. These keep owning-module patches live for handler code.
# ---------------------------------------------------------------------------


def write_proxy_event(event: str, **fields: Any) -> None:
    gateway_events.write_proxy_event(event, **fields)


def _write_adapter_event(event_context: Any, event: str, **fields: Any) -> None:
    gateway_events.write_adapter_event(event_context, event, **fields)


def _capture_usage(*args: Any, **kwargs: Any) -> Any:
    return gateway_events.capture_usage(*args, **kwargs)


def _observe_gateway_diagnostic(method: str, *args: Any, **kwargs: Any) -> None:
    gateway_events.observe_gateway_diagnostic(method, *args, **kwargs)


def _record_user_requested_shutdown() -> None:
    gateway_events.record_user_requested_shutdown()


def _activate_gateway_request(admission: Any) -> Any:
    return gateway_admission.activate_gateway_request(admission)


def _restore_gateway_request(previous: Any) -> None:
    gateway_admission.restore_gateway_request(previous)


def _active_gateway_request() -> Any:
    return gateway_admission.active_gateway_request()


def sleep_for_retry_with_gateway_cancellation(*args: Any, **kwargs: Any) -> Any:
    return gateway_admission.sleep_for_retry_with_gateway_cancellation(*args, **kwargs)


def _gateway_transport() -> Any:
    return gateway_transport.default_gateway_transport()


def _open_upstream_response(request: Request, **kwargs: Any) -> Any:
    return gateway_transport.open_upstream_response(request, **kwargs)


def upstream_headers(*args: Any, **kwargs: Any) -> dict[str, str]:
    return gateway_transport.upstream_headers(*args, **kwargs)


def materialize_operational_authentication(*args: Any, **kwargs: Any) -> Any:
    return gateway_transport.materialize_operational_authentication(*args, **kwargs)


def bind_route_plan_operational_authentication(*args: Any, **kwargs: Any) -> Any:
    return gateway_transport.bind_route_plan_operational_authentication(*args, **kwargs)


def _retry_safety_class(*args: Any, **kwargs: Any) -> Any:
    return gateway_transport._retry_safety_class(*args, **kwargs)


def _emit_upstream_retry_event(*args: Any, **kwargs: Any) -> Any:
    return gateway_transport._emit_upstream_retry_event(*args, **kwargs)


def _emit_upstream_retry_suppressed_event(*args: Any, **kwargs: Any) -> Any:
    return gateway_transport._emit_upstream_retry_suppressed_event(*args, **kwargs)


def _downstream_retry_payload(*args: Any, **kwargs: Any) -> Any:
    return gateway_transport._downstream_retry_payload(*args, **kwargs)


def choose_upstream(model_id: str) -> Any:
    return gateway_catalog_runtime.choose_upstream(model_id)


def official_upstream() -> Any:
    return gateway_catalog_runtime.official_upstream()


def model_supports_image(model_id: str | None, upstream: Any = None) -> bool:
    return gateway_catalog_runtime.model_supports_image(model_id, upstream)


def route_plan_for_request(*args: Any, **kwargs: Any) -> Any:
    return _route_plan_module.route_plan_for_request(*args, **kwargs)


def _route_runtime_facts(request_kind: str) -> Any:
    return _route_plan_module._route_runtime_facts(request_kind)


def strip_tools_for_compact_payload(*args: Any, **kwargs: Any) -> Any:
    return gateway_stream_semantics.strip_tools_for_compact_payload(*args, **kwargs)


def _downstream_stream_status_payload(*args: Any, **kwargs: Any) -> Any:
    return gateway_stream_semantics._downstream_stream_status_payload(*args, **kwargs)


def compatible_request_body(*args: Any, **kwargs: Any) -> Any:
    return gateway_compat.compatible_request_body(*args, **kwargs)


def official_passthrough_request_body(*args: Any, **kwargs: Any) -> Any:
    return gateway_compat.official_passthrough_request_body(*args, **kwargs)


def transparent_request_body(*args: Any, **kwargs: Any) -> Any:
    return gateway_compat.transparent_request_body(*args, **kwargs)


def _normalize_transparent_tool_schema_booleans(*args: Any, **kwargs: Any) -> Any:
    return gateway_compat.official_passthrough._normalize_transparent_tool_schema_booleans(*args, **kwargs)


def _excessive_transparent_chat_tool_loop_count(*args: Any, **kwargs: Any) -> Any:
    return gateway_compat.official_passthrough._excessive_transparent_chat_tool_loop_count(*args, **kwargs)


def _excessive_transparent_responses_tool_loop_count(*args: Any, **kwargs: Any) -> Any:
    return gateway_compat.official_passthrough._excessive_transparent_responses_tool_loop_count(
        *args, **kwargs
    )


def _rewrite_transparent_developer_role_messages(*args: Any, **kwargs: Any) -> Any:
    return gateway_compat.official_passthrough._rewrite_transparent_developer_role_messages(*args, **kwargs)


def _responses_body_with_lifecycle_final_retry_guidance(*args: Any, **kwargs: Any) -> Any:
    return gateway_compat.multi_agent._responses_body_with_lifecycle_final_retry_guidance(
        *args, **kwargs
    )


def _safe_json_mapping(*args: Any, **kwargs: Any) -> Any:
    return gateway_compat.official_passthrough._safe_json_mapping(*args, **kwargs)


def gateway_image_proxy_enabled() -> bool:
    return gateway_settings.gateway_image_proxy_enabled()


def gateway_official_http_passthrough_enabled() -> bool:
    return gateway_settings.gateway_official_http_passthrough_enabled()


def gateway_websocket_recorder_idle_timeout_seconds() -> float:
    return gateway_settings.gateway_websocket_recorder_idle_timeout_seconds()


def gateway_websocket_recorder_max_frames() -> int:
    return gateway_settings.gateway_websocket_recorder_max_frames()


def max_request_body_bytes() -> int:
    return gateway_settings.max_request_body_bytes()


def model_event_sse_idle_timeout_seconds() -> float:
    return gateway_settings.model_event_sse_idle_timeout_seconds()


def sse_keepalive_seconds() -> float:
    return gateway_settings.sse_keepalive_seconds()


def transport_sse_idle_timeout_seconds() -> float:
    return gateway_settings.transport_sse_idle_timeout_seconds()


def upstream_timeout_seconds() -> int:
    return gateway_settings.upstream_timeout_seconds()


# ---------------------------------------------------------------------------
# Inbound-request and event-context glue.
# ---------------------------------------------------------------------------


def _event_context_with_request_kind(context: Mapping[str, Any], request_kind: str) -> dict[str, Any]:
    payload = gateway_events.public_event_context(context)
    existing = payload.get("request_kind")
    if isinstance(existing, str) and existing and existing != request_kind:
        payload.setdefault("client_request_kind", existing)
    payload["request_kind"] = request_kind
    return payload


def _parse_gateway_request_input(
    handler: Any,
    *,
    inbound_format: str,
    provider_hint: str | None,
    request_id: str,
    started_at: float,
    request_context: Mapping[str, Any],
    proxy_request_context: Mapping[str, Any],
    raw_provider_probe: bool,
    content_length: int,
) -> GatewayRequestInput:
    """Compatibility adapter from HTTP input to the typed inbound seam."""
    return parse_inbound_request(
        InboundRequest(
            request_id=request_id,
            started_at=started_at,
            path=handler.path,
            protocol=RouteProtocol(inbound_format),
            provider_hint=provider_hint,
            headers=handler.headers,
            body=handler.rfile.read(content_length),
            request_context=request_context,
            proxy_request_context=proxy_request_context,
            raw_provider_probe=raw_provider_probe,
        ),
        InboundRequestHooks(
            decode_body=decoded_request_body,
            get_header=_get_header,
            request_kind=_request_kind_from_headers_and_payload,
            compact_request_kind=RETRY_REQUEST_COMPACT,
            event_context_for_kind=_event_context_with_request_kind,
            try_extract_model=try_extract_model,
            provider_scoped_model=provider_scoped_route_model,
        ),
    )


class GatewayHandlerMixin:
    """Request-handling methods mixed into CodexProxyHandler."""

    def _proxy_official_image_generation(self) -> None:
        request_id = uuid.uuid4().hex[:12]
        started_at = time.monotonic()
        request_context = request_context_from_headers(self.headers)

        def send_user_requested_shutdown() -> None:
            _record_user_requested_shutdown()
            self._send_json_and_close(503, user_requested_shutdown_payload("responses"))

        if not _local_request_authorized(self.headers, request_context):
            write_proxy_event(
                "request_error",
                request_id=request_id,
                path=self.path,
                method="POST",
                model=None,
                upstream="local",
                route_reason="official_image_generation",
                status=401,
                error="UnauthorizedLocalClient",
                detail="missing or invalid local Gateway client key",
                duration_ms=int((time.monotonic() - started_at) * 1000),
                **request_context,
            )
            self._send_json_and_close(401, _local_gateway_auth_error_payload())
            return

        shutdown_controller = _gateway_shutdown_controller_for_handler(self)
        admission = shutdown_controller.admit()
        if admission is None:
            send_user_requested_shutdown()
            return
        previous_admission = _activate_gateway_request(admission)
        upstream_name = "official"
        status = 500
        try:
            admission.raise_if_cancelled()
            upstream = official_upstream()
            upstream_name = str(upstream["name"])
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
            except (TypeError, ValueError):
                self._send_json_and_close(400, {"error": "invalid Content-Length"})
                return
            if content_length < 0:
                self._send_json_and_close(400, {"error": "invalid Content-Length"})
                return
            max_body_bytes = max_request_body_bytes()
            if content_length > max_body_bytes:
                self._send_json_and_close(
                    413,
                    {
                        "error": "request body too large",
                        "max_request_body_bytes": max_body_bytes,
                    },
                )
                return

            body = self.rfile.read(content_length)
            admission.raise_if_cancelled()
            operational_authentication = materialize_operational_authentication(
                self.headers,
                upstream,
            )
            headers = upstream_headers(
                self.headers,
                upstream,
                request_mutation_policy=MutationPolicy.OFFICIAL_PASSTHROUGH,
                operational_authentication=operational_authentication,
            )
            request = _gateway_transport().build_request(
                upstream,
                "/images/generations",
                data=body,
                headers=headers,
                method="POST",
            )
            event_context = {
                "request_id": request_id,
                "model": None,
                **_event_context_with_request_kind(
                    request_context,
                    RETRY_REQUEST_MAIN_GENERATION,
                ),
            }
            write_proxy_event(
                "request_start",
                request_id=request_id,
                path=self.path,
                method="POST",
                model=None,
                upstream=upstream_name,
                route_reason="official_image_generation",
                content_length=content_length,
                **request_context,
            )
            try:
                with _open_upstream_response(
                    request,
                    upstream_name=upstream_name,
                    upstream_format="images",
                    timeout=upstream_timeout_seconds(),
                    event_context=event_context,
                    request_kind=RETRY_REQUEST_MAIN_GENERATION,
                    max_attempts=1,
                    retry_http_errors=False,
                    transport_policy=TransportPolicy.OFFICIAL_KEEPALIVE,
                ) as response:
                    status = self._relay_raw_upstream_response(response, upstream_name)
            except HTTPError as exc:
                try:
                    status = self._relay_raw_upstream_response(exc, upstream_name)
                finally:
                    exc.close()
            write_proxy_event(
                "request_complete",
                request_id=request_id,
                method="POST",
                model=None,
                upstream=upstream_name,
                route_reason="official_image_generation",
                status=status,
                duration_ms=int((time.monotonic() - started_at) * 1000),
                **request_context,
            )
        except GatewayUserRequestedShutdown:
            send_user_requested_shutdown()
        except (IncompleteRead, OSError, URLError) as exc:
            if admission.cancelled:
                send_user_requested_shutdown()
                return
            detail = safe_upstream_error_detail(exc)
            write_proxy_event(
                "request_error",
                request_id=request_id,
                method="POST",
                model=None,
                upstream=upstream_name,
                route_reason="official_image_generation",
                status=502,
                error=type(exc).__name__,
                detail=detail,
                duration_ms=int((time.monotonic() - started_at) * 1000),
                **request_context,
            )
            self._send_json_and_close(
                502,
                {"error": type(exc).__name__, "detail": detail},
            )
        except Exception as exc:
            if admission.cancelled:
                send_user_requested_shutdown()
                return
            detail = safe_upstream_error_detail(exc)
            logger.error(
                "unexpected image generation proxy error request_id=%s detail=%s",
                request_id,
                detail,
            )
            self._send_json_and_close(
                500,
                {"error": type(exc).__name__, "detail": detail},
            )
        finally:
            _restore_gateway_request(previous_admission)
            shutdown_controller.complete(admission)

    def _proxy_post_request(self, *, inbound_format: str, provider_hint: str | None = None) -> None:
        """Shared POST handler for inbound Responses and Chat Completions requests.

        ``inbound_format`` is the wire format the *caller* used.  When it is
        ``chat_completions`` the request body is converted to Responses format
        before routing, and the upstream response is converted back to Chat
        Completions format before being returned to the caller.
        """
        request_id = uuid.uuid4().hex[:12]
        self._diagnostic_request_id = request_id
        self._pre_response_deadline = None
        started_at = time.monotonic()
        request_context = request_context_from_headers(self.headers)
        if not _local_request_authorized(self.headers, request_context):
            write_proxy_event(
                "request_error",
                request_id=request_id,
                path=self.path,
                method="POST",
                model=None,
                upstream="local",
                route_reason="local_client_auth",
                status=401,
                error="UnauthorizedLocalClient",
                detail="missing or invalid local Gateway client key",
                duration_ms=int((time.monotonic() - started_at) * 1000),
                **request_context,
            )
            self._send_json(401, _local_gateway_auth_error_payload())
            self.close_connection = True
            return
        request_kind = RETRY_REQUEST_MAIN_GENERATION
        proxy_request_context = _event_context_with_request_kind(request_context, request_kind)
        raw_provider_probe = raw_provider_probe_requested(self.headers, self.path)
        if raw_provider_probe:
            proxy_request_context["raw_provider_probe"] = True
        model = None
        model_requested = None
        upstream: Mapping[str, Any] | None = None
        upstream_name = None
        upstream_format = "responses"
        reports_cached_input_tokens = False
        behavior_profile = None
        route_reason: str | None = None
        route_plan: RoutePlan | None = None
        active_route_attempt: RouteAttemptPlan | None = None
        relay_execution_plan: RelayExecutionPlan | None = None
        route_policy_event_fields: dict[str, Any] = {}
        downstream_sse_started = False
        response_lifecycle_state: dict[str, str] = {}
        caller_body = b""
        caller_stream = True
        caller_request_observability: dict[str, Any] = {}
        request_observability: dict[str, Any] = {}
        usage_capture: dict[str, Any] = {}
        request_start_written = False
        write_request_start_once: Callable[[Mapping[str, Any]], None] | None = None

        def send_user_requested_shutdown() -> None:
            _record_user_requested_shutdown()
            if not self._send_user_requested_shutdown_outcome(
                inbound_format=inbound_format,
                downstream_sse_started=downstream_sse_started,
            ):
                finish_downstream_write_failure()

        def finish_downstream_write_failure(*, write_exc: OSError | None = None) -> None:
            exc = write_exc or OSError("downstream closed")
            failure_seam = _handler_downstream_stream_commit(self)
            if failure_seam is not None:
                failure_seam.close()
            self.close_connection = True
            write_proxy_event(
                "downstream_stream_closed",
                request_id=request_id,
                model=canonical_model_id(model) if model else None,
                model_requested=model_requested,
                upstream=upstream_name or "upstream_error",
                provider_hint=provider_hint,
                upstream_format=upstream_format,
                behavior_profile=behavior_profile,
                inbound_format=inbound_format,
                status=499,
                error=type(exc).__name__,
                detail=safe_upstream_error_detail(exc),
                **proxy_request_context,
            )
            write_proxy_event(
                "request_complete",
                request_id=request_id,
                method="POST",
                model=canonical_model_id(model) if model else None,
                model_requested=model_requested,
                model_canonical=canonical_model_id(model) if model else None,
                upstream=upstream_name or "upstream_error",
                provider_id=upstream_name,
                provider_hint=provider_hint,
                upstream_format=upstream_format,
                reports_cached_input_tokens=reports_cached_input_tokens,
                behavior_profile=behavior_profile,
                inbound_format=inbound_format,
                route_reason=route_reason,
                route_mode="official" if upstream_name == "official" else "codexhub",
                is_stream=caller_stream,
                status=499,
                duration_ms=int((time.monotonic() - started_at) * 1000),
                **request_observability,
                **usage_capture,
                **proxy_request_context,
            )

        shutdown_controller = _gateway_shutdown_controller_for_handler(self)
        admission = shutdown_controller.admit()
        if admission is None:
            send_user_requested_shutdown()
            return
        previous_admission = _activate_gateway_request(admission)
        adapter_event_context: dict[str, Any] | None = None

        try:
            admission.raise_if_cancelled()
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length < 0:
                raise ValueError("Content-Length must be non-negative")
            max_body_bytes = max_request_body_bytes()
            if content_length > max_body_bytes:
                write_proxy_event(
                    "request_error",
                    request_id=request_id,
                    path=self.path,
                    method="POST",
                    model=None,
                    upstream="local",
                    route_reason="request_body_limit",
                    content_length=content_length,
                    max_request_body_bytes=max_body_bytes,
                    status=413,
                    error="RequestBodyTooLarge",
                    detail="request body exceeds configured limit",
                    duration_ms=int((time.monotonic() - started_at) * 1000),
                    **proxy_request_context,
                )
                self._send_json(
                    413,
                    {
                        "error": "request body too large",
                        "max_request_body_bytes": max_body_bytes,
                    },
                )
                self.close_connection = True
                return
            request_input = _parse_gateway_request_input(
                self,
                inbound_format=inbound_format,
                provider_hint=provider_hint,
                request_id=request_id,
                started_at=started_at,
                request_context=request_context,
                proxy_request_context=proxy_request_context,
                raw_provider_probe=raw_provider_probe,
                content_length=content_length,
            )
            content_type = request_input.content_type
            content_encoding = request_input.content_encoding
            content_decoded = request_input.content_decoded
            decode_error = None
            body = request_input.body
            caller_body = request_input.body
            inbound_payload = request_input.inbound_payload
            request_kind = request_input.request_kind
            proxy_request_context = request_input.proxy_request_context
            model_requested = request_input.model_requested
            model = request_input.model
            route_reason = request_input.route_reason
            upstream = choose_upstream(model) if model else official_upstream()
            upstream_name = upstream["name"]
            upstream_format = str(upstream.get("upstream_format", "responses"))
            reports_cached_input_tokens = bool(upstream.get("reports_cached_input_tokens"))
            _validate_reasoning_effort_for_upstream(inbound_payload, upstream, model)
            inbound_has_image = (
                isinstance(inbound_payload, Mapping)
                and _value_contains_image(inbound_payload)
            )
            target_accepts_images = bool(
                model and model_supports_image(model, upstream)
            )
            image_proxy_enabled = gateway_image_proxy_enabled()
            caller_stream = (
                inbound_payload.get("stream") is True
                if isinstance(inbound_payload, Mapping)
                else True
            )
            route_runtime_facts: dict[str, RouteRuntimeFacts] = {
                request_kind: _route_runtime_facts(request_kind)
            }
            if request_kind != RETRY_REQUEST_MAIN_GENERATION:
                route_runtime_facts[RETRY_REQUEST_MAIN_GENERATION] = (
                    _route_runtime_facts(
                        RETRY_REQUEST_MAIN_GENERATION
                    )
                )
            collaboration_protocol = (
                _resolve_collaboration_boundary(
                    inbound_payload,
                    proxy_request_context,
                    surface="request",
                )
                if provider_hint is not None and upstream_name != "official"
                else None
            )
            route_plan = route_plan_for_request(
                upstream,
                request_context,
                inbound_format=inbound_format,
                provider_hint=provider_hint,
                collaboration_protocol=collaboration_protocol,
                model_requested=model_requested,
                canonical_route_model=model,
                request_kind=request_kind,
                raw_provider_probe=raw_provider_probe,
                input_has_image=inbound_has_image,
                target_accepts_images=target_accepts_images,
                image_proxy_enabled=image_proxy_enabled,
                official_http_passthrough_enabled=(
                    gateway_official_http_passthrough_enabled()
                ),
                caller_stream=caller_stream,
                runtime_facts=route_runtime_facts,
            )
            primary_route_attempt = route_plan.primary_attempt
            behavior_profile = route_plan.behavior_profile
            upstream_format = route_plan.selected_upstream_format
            if request_kind != route_plan.request_kind:
                request_kind = route_plan.request_kind
                proxy_request_context = _event_context_with_request_kind(request_context, request_kind)
            self._pre_response_deadline = (
                primary_route_attempt.retry.pre_response_deadline(started_at)
                if primary_route_attempt is not None
                else None
            )
            reasoning_policy = _reasoning_policy_for_request(inbound_payload, upstream, model)
            route_policy_event_fields = {
                **_route_plan_event_fields(route_plan),
                **(
                    _route_attempt_event_fields(
                        primary_route_attempt,
                        provider_id=route_plan.provider_id,
                        model_requested=route_plan.model_requested,
                        model_canonical=route_plan.canonical_model,
                        upstream_model=route_plan.upstream_model,
                    )
                    if primary_route_attempt is not None
                    else {}
                ),
                **({"reasoning_policy": reasoning_policy} if reasoning_policy else {}),
            }
            proxy_request_context = {
                **proxy_request_context,
                **route_policy_event_fields,
            }
            if primary_route_attempt is None:
                protocol_error = (
                    UnsupportedRouteProtocolError
                    if (
                        route_plan.protocol_capability_state
                        == CapabilityState.UNSUPPORTED
                    )
                    else UnqualifiedRouteProtocolError
                )
                raise protocol_error(
                    route_plan.protocol_failure_reason
                    or "configured upstream protocol is not executable"
                )
            model_canonical = canonical_model_id(model) if model else None
            # Create the request-scoped downstream stream-commit seam early so that
            # every production SSE header/body (status, retry, keepalive, converted
            # output, error, terminal) is authorized by the same lifecycle owner.
            # The upstream response is attached once it is opened.
            self._downstream_stream_commit = _bind_downstream_stream_commit(
                self,
                None,
                upstream_name or "unknown",
                model=model_canonical,
                request_id=request_id,
                inbound_format=inbound_format,
                upstream_format=upstream_format,
            )
            if (
                route_plan.tool_exposure.strip_caller_tools
                and isinstance(inbound_payload, dict)
                and strip_tools_for_compact_payload(
                    inbound_payload,
                    event_context={
                        "request_id": request_id,
                        "behavior_profile": behavior_profile,
                        **proxy_request_context,
                    },
                )
            ):
                body = json.dumps(inbound_payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
            # Capture the caller's desired stream mode and prompt cache key
            # before compatibility helpers can force stream=true or reshape the
            # body for the selected upstream.
            prompt_cache_key = None
            if isinstance(inbound_payload, Mapping) and isinstance(inbound_payload.get("prompt_cache_key"), str):
                prompt_cache_key = inbound_payload["prompt_cache_key"]
            caller_request_observability = proxy_telemetry.enrich_request_observability(
                body=caller_body,
                codex_home=RUNTIME_CODEX_DIR,
                upstream=upstream,
                include_body_hmac=(
                    route_plan.request_mutation_policy
                    != MutationPolicy.OFFICIAL_PASSTHROUGH
                ),
                prompt_cache_key=prompt_cache_key,
                extract_prompt_cache_key=(
                    route_plan.request_mutation_policy
                    != MutationPolicy.OFFICIAL_PASSTHROUGH
                ),
            )

            def emit_request_start_once(observability_fields: Mapping[str, Any]) -> None:
                nonlocal request_start_written
                if request_start_written:
                    return
                write_proxy_event(
                    "request_start",
                    request_id=request_id,
                    path=self.path,
                    method="POST",
                    model=model_canonical,
                    model_requested=model_requested,
                    model_canonical=model_canonical,
                    upstream=upstream_name,
                    provider_id=upstream_name,
                    provider_hint=provider_hint,
                    upstream_format=upstream_format,
                    reports_cached_input_tokens=reports_cached_input_tokens,
                    behavior_profile=behavior_profile,
                    route_reason=route_reason,
                    route_mode="official" if upstream_name == "official" else "codexhub",
                    inbound_format=inbound_format,
                    is_stream=caller_stream,
                    content_length=content_length,
                    decoded_content_length=len(caller_body) if content_decoded else None,
                    content_type=content_type[:120] if content_type else None,
                    content_encoding=content_encoding[:80] if content_encoding else None,
                    content_decoded=content_decoded,
                    decode_error=decode_error[:160] if decode_error else None,
                    **dict(observability_fields),
                    **proxy_request_context,
                )
                request_start_written = True

            write_request_start_once = emit_request_start_once
            adapter_event_context = {
                "request_id": request_id,
                "model": model_canonical,
                "behavior_profile": behavior_profile,
                **proxy_request_context,
                "_caller_wire_format": inbound_format,
            }

            def emit_downstream_status(status_payload: Mapping[str, Any]) -> bool:
                nonlocal downstream_sse_started
                if not caller_stream:
                    return True
                if not downstream_sse_started:
                    if not self._send_sse_headers(200, upstream_name):
                        return False
                    downstream_sse_started = True
                return self._write_sse_data(
                    _downstream_stream_status_payload(inbound_format, status_payload, model_canonical)
                )

            if route_plan.vision.action is not VisionAction.REJECT:
                operational_authentication = materialize_operational_authentication(
                    self.headers,
                    upstream,
                )
                route_plan = bind_route_plan_operational_authentication(
                    route_plan,
                    self.headers,
                    upstream,
                    operational_authentication,
                    drop_content_encoding=content_decoded,
                )
                primary_route_attempt = route_plan.attempts[0]
            usage_capture: dict[str, Any] = {}
            vision_proxy_payload_format = (
                route_plan.prepared_request_protocol.value
            )
            image_proxy_payload: dict[str, Any] | None = None
            if route_plan.vision.action in {VisionAction.PROXY, VisionAction.REJECT}:
                try:
                    parsed_image_proxy_payload = json.loads(body.decode("utf-8-sig"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    parsed_image_proxy_payload = None
                if isinstance(parsed_image_proxy_payload, dict):
                    image_proxy_payload = parsed_image_proxy_payload
            try:
                if route_plan.vision.action in {VisionAction.PROXY, VisionAction.REJECT}:
                    if image_proxy_payload is None:
                        raise ImageProxyError(
                            "Vision Proxy could not inspect the planned image payload."
                        )
                    image_proxy_changed = enforce_text_only_image_boundary(
                        image_proxy_payload,
                        inbound_format=vision_proxy_payload_format,
                        target_model=model,
                        target_upstream=upstream,
                        vision_plan=route_plan.vision,
                        event_context=adapter_event_context,
                        progress_callback=emit_downstream_status if caller_stream else None,
                    )
                    if image_proxy_changed:
                        body = json.dumps(image_proxy_payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
            except DownstreamClosedDuringImageProxyError:
                finish_downstream_write_failure()
                return
            seam = _handler_downstream_stream_commit(self)
            if seam is not None and seam.downstream_closed:
                finish_downstream_write_failure(
                    write_exc=seam.last_write_error() or OSError("downstream closed")
                )
                return
            prepared_caller_body = body

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
                self._active_prepared_exchange = exchange

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
                    if not self._send_sse_headers(200, upstream_name):
                        return False
                    downstream_sse_started = True
                if not self._write_sse_event("codexhub.retry", payload):
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
                seam = _handler_downstream_stream_commit(self)
                if seam is not None:
                    seam.set_upstream_format(selected_format)

            def attach_upstream(response: Any) -> None:
                seam = _handler_downstream_stream_commit(self)
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
                return self._relay_upstream_response(
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
                wrote = self._write_downstream_sse_error(
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

            exchange_progress = ExchangeProgress(
                upstream_format=upstream_format,
                downstream_sse_started=downstream_sse_started,
            )
            exchange_request = ExchangeRequest(
                inbound=request_input,
                route_plan=route_plan,
                upstream=upstream,
                upstream_name=upstream_name,
                prepared_body=prepared_caller_body,
                inbound_payload=inbound_payload,
                model_canonical=model_canonical,
                caller_stream=caller_stream,
                prompt_cache_key=prompt_cache_key,
                caller_request_observability=caller_request_observability,
                event_context=adapter_event_context,
                proxy_request_context=proxy_request_context,
                usage_capture=usage_capture,
                response_lifecycle_state=response_lifecycle_state,
                pre_response_deadline=self._pre_response_deadline,
                downstream_sse_started=downstream_sse_started,
            )
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
                    self
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
            try:
                exchange_result = execute_exchange(
                    exchange_request,
                    exchange_hooks,
                    progress=exchange_progress,
                )
            finally:
                active_route_attempt = exchange_progress.active_attempt
                relay_execution_plan = (
                    exchange_progress.relay_execution_plan
                )
                upstream_format = exchange_progress.upstream_format
                request_observability = (
                    exchange_progress.request_observability
                )
                downstream_sse_started = (
                    exchange_progress.downstream_sse_started
                    or downstream_sse_started
                )
                self._active_prepared_exchange = (
                    exchange_progress.active_prepared_exchange
                )
            terminal = terminal_result(exchange_result)
            if terminal.handled and not terminal.completed:
                return
            if not terminal.completed:
                raise RuntimeError(
                    terminal.error or "invalid exchange terminal result"
                )
            status = terminal.status
            write_proxy_event(
                "request_complete",
                request_id=request_id,
                method="POST",
                model=model_canonical,
                model_requested=model_requested,
                model_canonical=model_canonical,
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
        except GatewayUserRequestedShutdown:
            send_user_requested_shutdown()
        except CompactEmptyResponseError as exc:
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
                if not self._write_downstream_sse_error(
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
            self._safe_send_downstream_json_error(
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
        except (LifecycleEmptyFinalResponseError, LifecycleFinalFormatResponseError) as exc:
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
                if not self._write_downstream_sse_error(
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
            self._safe_send_downstream_json_error(
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
        except ModelIdentityResolutionError as exc:
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
                if not self._write_downstream_sse_error(
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
            self._safe_send_downstream_json_error(
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
        except ImageProxyError as exc:
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
                if not self._write_downstream_sse_error(
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
            self._safe_send_downstream_json_error(
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
        except UpstreamProtocolTranslationError as exc:
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
                    if not self._write_sse_event(
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
                    self.close_connection = True
                else:
                    if not self._write_downstream_sse_error(
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
            self._safe_send_downstream_json_error(
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
        except ValueError as exc:
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
            self._safe_send_downstream_json_error(
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
        except HTTPError as exc:
            if admission.cancelled:
                send_user_requested_shutdown()
                return
            identity = _retry_identity_from_context(adapter_event_context)
            if downstream_sse_started:
                if not self._write_downstream_sse_error(
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
                status = self._relay_upstream_response(
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
                self.close_connection = True
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
        except GatewayPreResponseBudgetExhausted as exc:
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
            self._safe_send_downstream_json_error(
                504,
                inbound_format=inbound_format,
                upstream_name=upstream_name or "gateway",
                request_id=request_id,
                error=error_code,
                detail=detail,
                error_type=error_code,
            )
            self.close_connection = True
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
        except IncompleteRead as exc:
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
                if not self._write_downstream_sse_error(
                    inbound_format=inbound_format,
                    upstream_name=upstream_name or "upstream_error",
                    exc=exc,
                    detail=detail,
                    redact_identity=identity,
                ):
                    finish_downstream_write_failure()
                return
            self._safe_send_downstream_json_error(
                502,
                inbound_format=inbound_format,
                upstream_name=upstream_name or "upstream_error",
                request_id=request_id,
                exc=exc,
                detail=detail,
                redact_identity=identity,
            )
        except (OSError, URLError) as exc:
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
                if not self._write_downstream_sse_error(
                    inbound_format=inbound_format,
                    upstream_name=upstream_name or "upstream_error",
                    exc=exc,
                    detail=detail,
                    redact_identity=identity,
                ):
                    finish_downstream_write_failure()
                return
            self._safe_send_downstream_json_error(
                502,
                inbound_format=inbound_format,
                upstream_name=upstream_name or "upstream_error",
                request_id=request_id,
                exc=exc,
                detail=detail,
                redact_identity=identity,
            )
        except Exception as exc:
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
                if not self._write_downstream_sse_error(
                    inbound_format=inbound_format,
                    upstream_name=upstream_name or "upstream_error",
                    status=500,
                    exc=exc,
                    redact_identity=identity,
                ):
                    finish_downstream_write_failure()
                return
            self._safe_send_downstream_json_error(
                500,
                inbound_format=inbound_format,
                upstream_name=upstream_name or "upstream_error",
                request_id=request_id,
                exc=exc,
                detail=detail,
                redact_identity=identity,
            )
        finally:
            self._pre_response_deadline = None
            _restore_gateway_request(previous_admission)
            shutdown_controller.complete(admission)

    def _send_local_responses_no_content(self) -> None:
        request_id = uuid.uuid4().hex[:12]
        started_at = time.monotonic()
        request_context = request_context_from_headers(self.headers)
        write_proxy_event(
            "request_start",
            request_id=request_id,
            path=self.path,
            method="GET",
            model=None,
            upstream="local",
            route_reason="local_responses_probe",
            content_length=0,
            **request_context,
        )
        self.send_response(204)
        self.send_header("Content-Length", "0")
        self.send_header("X-Codex-Proxy-Upstream", "local")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        write_proxy_event(
            "request_complete",
            request_id=request_id,
            method="GET",
            model=None,
            upstream="local",
            route_reason="local_responses_probe",
            status=204,
            duration_ms=int((time.monotonic() - started_at) * 1000),
            **request_context,
        )

    def _handle_websocket_recording_probe(self) -> None:
        request_id = uuid.uuid4().hex[:12]
        started_at = time.monotonic()
        request_context = request_context_from_headers(self.headers)
        handshake_metadata = redacted_handshake_metadata(self.path, self.headers)
        selected_subprotocol = handshake_metadata.get("selected_subprotocol")
        key = _get_header(self.headers, "Sec-WebSocket-Key")
        if not key:
            self._send_json(400, {"error": "missing Sec-WebSocket-Key"})
            self.close_connection = True
            write_proxy_event(
                "websocket_probe_error",
                request_id=request_id,
                error="MissingSecWebSocketKey",
                duration_ms=int((time.monotonic() - started_at) * 1000),
                **handshake_metadata,
                **request_context,
            )
            return

        write_proxy_event(
            "websocket_probe_start",
            request_id=request_id,
            **handshake_metadata,
            **request_context,
        )
        self.send_response(101, "Switching Protocols")
        for header, value in websocket_upgrade_response_headers(key, selected_subprotocol if isinstance(selected_subprotocol, str) else None):
            self.send_header(header, value)
        self.end_headers()

        frames_recorded = 0
        close_code = None
        error_name = None
        stop_reason = "max_frames"
        max_frames = gateway_websocket_recorder_max_frames()
        recorder_idle_timeout = gateway_websocket_recorder_idle_timeout_seconds()
        connection = getattr(self, "connection", None)
        if connection is not None and hasattr(connection, "settimeout"):
            try:
                connection.settimeout(recorder_idle_timeout)
            except OSError:
                pass
        try:
            while frames_recorded < max_frames:
                try:
                    frame = read_frame(self.rfile, expect_masked=True, max_payload_bytes=1024 * 1024)
                except EOFError:
                    stop_reason = "eof"
                    break
                except TimeoutError:
                    stop_reason = "idle_timeout"
                    break
                frames_recorded += 1
                frame_metadata = _websocket_probe_frame_metadata(frame)
                write_proxy_event(
                    "websocket_probe_frame",
                    request_id=request_id,
                    frame_index=frames_recorded,
                    **frame_metadata,
                    **request_context,
                )
                if frame.opcode == 0x8:
                    close_code = frame_metadata.get("close_code")
                    stop_reason = "client_close"
                    break
        except WebSocketProtocolError as exc:
            error_name = type(exc).__name__
            write_proxy_event(
                "websocket_probe_error",
                request_id=request_id,
                error=error_name,
                detail=str(exc)[:160],
                duration_ms=int((time.monotonic() - started_at) * 1000),
                **handshake_metadata,
                **request_context,
            )
        finally:
            try:
                write_frame(self.wfile, close_frame(1000, "recorded"), mask=False)
                self.wfile.flush()
            except OSError as exc:
                error_name = type(exc).__name__
                write_proxy_event(
                    "websocket_probe_error",
                    request_id=request_id,
                    error=error_name,
                    detail=safe_upstream_error_detail(exc),
                    duration_ms=int((time.monotonic() - started_at) * 1000),
                    **handshake_metadata,
                    **request_context,
                )
            self.close_connection = True

        write_proxy_event(
            "websocket_probe_complete",
            request_id=request_id,
            frames_recorded=frames_recorded,
            close_code=close_code,
            stop_reason=stop_reason,
            error=error_name,
            duration_ms=int((time.monotonic() - started_at) * 1000),
            **handshake_metadata,
            **request_context,
        )

    def _reject_local_responses_websocket_probe(self) -> None:
        request_id = uuid.uuid4().hex[:12]
        started_at = time.monotonic()
        request_context = request_context_from_headers(self.headers)
        write_proxy_event(
            "request_start",
            request_id=request_id,
            path=self.path,
            method="GET",
            model=None,
            upstream="local",
            route_reason="local_responses_websocket_fast_reject",
            content_length=0,
            **request_context,
        )

        payload = {"detail": "WebSocket transport is not supported by this local Codex proxy; use POST /v1/responses."}
        body = _json_response_bytes(payload)
        self.send_response(405, "Method Not Allowed")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Codex-Proxy-Upstream", "local")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()
        self.close_connection = True
        write_proxy_event(
            "request_complete",
            request_id=request_id,
            method="GET",
            model=None,
            upstream="local",
            route_reason="local_responses_websocket_fast_reject",
            status=405,
            duration_ms=int((time.monotonic() - started_at) * 1000),
            **request_context,
        )

    def _passthrough_official_control_request(self, method: str) -> None:
        request_id = uuid.uuid4().hex[:12]
        started_at = time.monotonic()
        request_context = request_context_from_headers(self.headers)
        proxy_request_context = _event_context_with_request_kind(request_context, RETRY_REQUEST_OFFICIAL_CONTROL)
        upstream = official_upstream()
        upstream_name = upstream["name"]
        relay_execution_plan = RelayExecutionPlan(
            selected_upstream_format=RouteProtocol.RESPONSES.value,
            request_kind=RETRY_REQUEST_OFFICIAL_CONTROL,
            streaming_policy=StreamingPolicy.GATEWAY_ADAPTED,
            usage_policy=UsagePolicy.SYNC_CAPTURE,
            response_mutation_policy=MutationPolicy.GATEWAY_COMPATIBILITY,
            sse_mutation_policy=MutationPolicy.GATEWAY_COMPATIBILITY,
            verify_cross_protocol_source=False,
            lifecycle_final_retry_enabled=False,
        )

        try:
            headers = upstream_headers(self.headers, upstream)
            write_proxy_event(
                "request_start",
                request_id=request_id,
                path=self.path,
                method=method,
                model=None,
                upstream=upstream_name,
                route_reason=RETRY_REQUEST_OFFICIAL_CONTROL,
                content_length=0,
                **proxy_request_context,
            )
            control_path = self.path[3:] if self.path.startswith("/v1/") else self.path
            request = _gateway_transport().build_request(
                upstream,
                control_path,
                headers=headers,
                method=method,
            )
            adapter_event_context = {
                "request_id": request_id,
                "model": None,
                **proxy_request_context,
            }
            with _open_upstream_response(
                request,
                upstream_name=upstream_name,
                upstream_format="responses",
                timeout=upstream_timeout_seconds(),
                event_context=adapter_event_context,
                request_kind=RETRY_REQUEST_OFFICIAL_CONTROL,
            ) as response:
                status = self._relay_upstream_response(
                    response,
                    upstream_name,
                    request_id=request_id,
                    model=None,
                    relay_execution_plan=relay_execution_plan,
                )
            write_proxy_event(
                "request_complete",
                request_id=request_id,
                method=method,
                model=None,
                upstream=upstream_name,
                route_reason=RETRY_REQUEST_OFFICIAL_CONTROL,
                status=status,
                duration_ms=int((time.monotonic() - started_at) * 1000),
                **proxy_request_context,
            )
        except HTTPError as exc:
            try:
                status = self._relay_upstream_response(
                    exc,
                    upstream_name,
                    request_id=request_id,
                    model=None,
                    relay_execution_plan=relay_execution_plan,
                )
            except OSError as relay_exc:
                self.close_connection = True
                write_proxy_event(
                    "client_write_failed",
                    request_id=request_id,
                    method=method,
                    model=None,
                    upstream=upstream_name,
                    route_reason=RETRY_REQUEST_OFFICIAL_CONTROL,
                    status=getattr(exc, "code", 502),
                    error=type(relay_exc).__name__,
                    detail=safe_upstream_error_detail(relay_exc),
                    duration_ms=int((time.monotonic() - started_at) * 1000),
                    **proxy_request_context,
                )
                return
            write_proxy_event(
                "request_error",
                request_id=request_id,
                method=method,
                model=None,
                upstream=upstream_name,
                route_reason=RETRY_REQUEST_OFFICIAL_CONTROL,
                status=status,
                error="HTTPError",
                detail=safe_upstream_error_detail(exc),
                duration_ms=int((time.monotonic() - started_at) * 1000),
                **proxy_request_context,
            )
        except (OSError, URLError) as exc:
            detail = safe_upstream_error_detail(exc)
            write_proxy_event(
                "request_error",
                request_id=request_id,
                method=method,
                model=None,
                upstream=upstream_name,
                route_reason=RETRY_REQUEST_OFFICIAL_CONTROL,
                status=502,
                error=type(exc).__name__,
                detail=detail,
                duration_ms=int((time.monotonic() - started_at) * 1000),
                **proxy_request_context,
            )
            self._safe_send_json(502, {"error": type(exc).__name__, "detail": detail}, request_id)

    def _send_user_requested_shutdown_outcome(
        self,
        *,
        inbound_format: str,
        downstream_sse_started: bool,
    ) -> bool:
        payload = user_requested_shutdown_payload(inbound_format)
        try:
            if downstream_sse_started:
                if inbound_format == "chat_completions":
                    written = self._write_sse_data(payload)
                else:
                    written = self._write_sse_event("error", payload)
                self.close_connection = True
                return written
            self._send_json(503, payload)
        except OSError:
            self.close_connection = True
            return False
        self.close_connection = True
        return True

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = _json_response_bytes(payload)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json_and_close(self, status: int, payload: dict[str, Any]) -> None:
        body = _json_response_bytes(payload)
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self._write_non_streaming_body_relay(body)

    def _relay_raw_upstream_response(self, response: Any, upstream_name: str) -> int:
        return relay_raw_response(
            response,
            upstream_name,
            writer=self,
            filtered_headers=_filtered_response_headers,
            active_request=_active_gateway_request,
            write_body=self._write_non_streaming_body_relay,
        )

    def _send_sse_headers(self, status: int, upstream_name: str) -> bool:
        seam = _handler_downstream_stream_commit(self)
        commit_headers = None if seam is None else seam.commit_headers
        return send_sse_headers(
            self,
            status,
            upstream_name,
            commit_headers=commit_headers,
        )

    def _write_sse_bytes(self, data: bytes, *, observe: bool = True) -> bool:
        seam = _handler_downstream_stream_commit(self)
        commit_sse_bytes = None if seam is None else seam.commit_sse_bytes
        return write_sse_bytes(
            self,
            data,
            commit_sse_bytes=commit_sse_bytes,
            observe=observe,
        )

    def _write_non_streaming_body_relay(self, body: bytes) -> bool:
        return write_non_streaming_body(self, body)

    def _write_sse_event(self, event: str, payload: Mapping[str, Any]) -> bool:
        return write_sse_event(
            self,
            event,
            payload,
            encode_json_line=_sse_json_line,
            commit_sse_bytes=self._write_sse_bytes,
        )

    def _write_sse_data(self, payload: Mapping[str, Any]) -> bool:
        return write_sse_data(
            self,
            payload,
            encode_json_line=_sse_json_line,
            commit_sse_bytes=self._write_sse_bytes,
        )

    def _write_sse_keepalive(self) -> bool:
        return write_sse_keepalive(
            self,
            commit_sse_bytes=self._write_sse_bytes,
        )

    def _write_sse_done(self) -> bool:
        seam = _handler_downstream_stream_commit(self)
        return write_sse_done(
            self,
            commit_sse_bytes=self._write_sse_bytes,
            terminal_committed=seam is not None and seam.terminal_committed,
        )

    def _iter_upstream_sse_lines(
        self,
        response: Any,
        *,
        downstream_output_started: Callable[[], bool] | None = None,
        line_resets_idle_timeout: Callable[[bytes], bool] | None = None,
        on_line: Callable[[bytes], None] | None = None,
    ) -> Any:
        admission = _active_gateway_request()
        seam = _handler_downstream_stream_commit(self)

        def attach_upstream(lifecycle: Any) -> None:
            if seam is not None:
                seam.attach_upstream(lifecycle)

        context = SseLineRelayContext(
            admission=admission,
            keepalive_interval=sse_keepalive_seconds(),
            transport_timeout_seconds=transport_sse_idle_timeout_seconds(),
            model_event_timeout_seconds=model_event_sse_idle_timeout_seconds(),
            lifecycle_factory=lambda upstream_response, current_admission: _UpstreamSseReaderLifecycle(
                upstream_response,
                admission=current_admission,
                logger_hook=logger,
            ),
            attach_upstream=attach_upstream,
            write_keepalive=self._write_sse_keepalive,
            idle_timeout_error=lambda timeout_seconds, phase: UpstreamStreamIdleTimeoutError(
                timeout_seconds,
                phase=phase,
            ),
            keepalive_failure_error=DownstreamKeepaliveFailedError,
            join_timeout_seconds=_UpstreamSseReaderLifecycle.JOIN_TIMEOUT_SECONDS,
        )
        return iter_upstream_sse_lines(
            response,
            context=context,
            downstream_output_started=downstream_output_started,
            line_resets_idle_timeout=line_resets_idle_timeout,
            on_line=on_line,
        )

    def _iter_upstream_sse_events(
        self,
        response: Any,
        *,
        event_resets_idle_timeout: Callable[[SseEvent], bool],
        on_chunk: Callable[[bytes], None] | None = None,
    ) -> Any:
        assembler = SseEventAssembler()
        pending_events: list[SseEvent] = []
        assembler_finished = False
        deferred_size_error: SseFrameTooLargeError | None = None

        def assemble_chunk(chunk: bytes) -> bool:
            nonlocal deferred_size_error
            events: list[SseEvent] = []
            try:
                assembler.feed(chunk, on_event=events.append)
            except SseFrameTooLargeError as exc:
                deferred_size_error = exc
            pending_events.extend(events)
            return any(event_resets_idle_timeout(event) for event in events)

        try:
            for chunk in self._iter_upstream_sse_lines(
                response,
                line_resets_idle_timeout=assemble_chunk,
                on_line=on_chunk,
            ):
                if not chunk:
                    break
                ready_events = tuple(pending_events)
                pending_events.clear()
                yield from ready_events
                if deferred_size_error is not None:
                    raise deferred_size_error

            termination = assembler.finish()
            assembler_finished = True
            yield from termination.events
            if termination.disposition == "incomplete":
                raise UpstreamStreamIncompleteError(
                    "Upstream SSE stream ended with an incomplete pending frame"
                )
        finally:
            if not assembler_finished:
                try:
                    assembler.cancel()
                except SseAssemblerClosedError:
                    pass

    def _write_sse_error_event(
        self,
        upstream_name: str,
        exc: BaseException,
        *,
        redact_identity: str | None = None,
    ) -> None:
        self._write_sse_event(
            "error",
            _downstream_sse_error_payload_for_inbound_format(
                DownstreamErrorSpec(
                    inbound_format="responses",
                    upstream_name=upstream_name,
                    exc=exc,
                    redact_identity=redact_identity,
                )
            ),
        )

    def _write_downstream_sse_error(
        self,
        *,
        inbound_format: str,
        upstream_name: str,
        status: int = 502,
        exc: BaseException | None = None,
        error: str | None = None,
        detail: str | None = None,
        error_type: str = "upstream_error",
        preserve_explicit_error: bool = False,
        redact_identity: str | None = None,
    ) -> bool:
        error_spec = DownstreamErrorSpec(
            inbound_format=inbound_format,
            upstream_name=upstream_name,
            status=status,
            exc=exc,
            error=error,
            detail=detail,
            error_type=error_type,
            preserve_explicit_error=preserve_explicit_error,
            redact_identity=redact_identity,
        )
        if inbound_format == "chat_completions":
            data = (
                b"data: "
                + json.dumps(
                    _downstream_sse_error_payload_for_inbound_format(error_spec),
                    ensure_ascii=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                + b"\n\n"
            )
            if not self._write_sse_bytes(data):
                return False
            self.close_connection = True
            return True
        if not self._write_sse_event("error", _downstream_sse_error_payload_for_inbound_format(error_spec)):
            return False
        self.close_connection = True
        return True

    def _write_sse_protocol_error_event(
        self,
        upstream_name: str,
        status: int,
        detail: str,
        *,
        error: str = "UpstreamProtocolError",
        redact_identity: str | None = None,
    ) -> None:
        self._write_sse_event(
            "error",
            _downstream_sse_error_payload_for_inbound_format(
                DownstreamErrorSpec(
                    inbound_format="responses",
                    upstream_name=upstream_name,
                    status=status,
                    error=error,
                    detail=detail,
                    redact_identity=redact_identity,
                )
            ),
        )

    def _safe_send_downstream_json_error(
        self,
        status: int,
        *,
        inbound_format: str,
        upstream_name: str,
        request_id: str,
        exc: BaseException | None = None,
        error: str | None = None,
        detail: str | None = None,
        error_type: str = "upstream_error",
        redact_identity: str | None = None,
    ) -> None:
        error_spec = DownstreamErrorSpec(
            inbound_format=inbound_format,
            upstream_name=upstream_name,
            status=status,
            exc=exc,
            error=error,
            detail=detail,
            error_type=error_type,
            redact_identity=redact_identity,
        )
        self._safe_send_json(
            status,
            _downstream_json_error_payload(error_spec),
            request_id,
        )

    def _safe_send_json(self, status: int, payload: dict[str, Any], request_id: str) -> None:
        try:
            self._send_json(status, payload)
        except OSError as exc:
            self.close_connection = True
            write_proxy_event(
                "client_write_failed",
                request_id=request_id,
                status=status,
                error=type(exc).__name__,
                detail=safe_upstream_error_detail(exc),
            )

    def _relay_official_passthrough_sse_response(
        self,
        response: Any,
        upstream_name: str,
        *,
        request_id: str | None = None,
        model: str | None = None,
        upstream_format: str = "responses",
        inbound_format: str = "responses",
        usage_capture: dict[str, Any] | None = None,
        headers_already_sent: bool = False,
        mark_downstream_sse_started: Callable[[], None] | None = None,
        event_context: Mapping[str, Any] | None = None,
        defer_stream_errors: bool = False,
    ) -> int:
        return relay_official_passthrough_sse_response(
            _relay_context_for_handler(self),
            response,
            upstream_name,
            request_id=request_id,
            model=model,
            upstream_format=upstream_format,
            inbound_format=inbound_format,
            usage_capture=usage_capture,
            headers_already_sent=headers_already_sent,
            mark_downstream_sse_started=mark_downstream_sse_started,
            event_context=event_context,
            defer_stream_errors=defer_stream_errors,
        )

    def _relay_transparent_upstream_response(
        self,
        response: Any,
        upstream_name: str,
        *,
        request_id: str | None = None,
        model: str | None = None,
        upstream_format: str = "responses",
        inbound_format: str = "responses",
        usage_capture: dict[str, Any] | None = None,
        headers_already_sent: bool = False,
        mark_downstream_sse_started: Callable[[], None] | None = None,
        event_context: Mapping[str, Any] | None = None,
        defer_stream_errors: bool = False,
    ) -> int:
        return relay_transparent_upstream_response(
            _relay_context_for_handler(self),
            response,
            upstream_name,
            request_id=request_id,
            model=model,
            upstream_format=upstream_format,
            inbound_format=inbound_format,
            usage_capture=usage_capture,
            headers_already_sent=headers_already_sent,
            mark_downstream_sse_started=mark_downstream_sse_started,
            event_context=event_context,
            defer_stream_errors=defer_stream_errors,
        )

    def _relay_upstream_response(
        self,
        response: Any,
        upstream_name: str,
        relay_execution_plan: RelayExecutionPlan,
        request_id: str | None = None,
        model: str | None = None,
        inbound_format: str = "responses",
        caller_stream: bool = True,
        event_context: Mapping[str, Any] | None = None,
        usage_capture: dict[str, Any] | None = None,
        headers_already_sent: bool = False,
        defer_stream_errors: bool = False,
        mark_downstream_sse_started: Callable[[], None] | None = None,
        response_lifecycle_state: dict[str, str] | None = None,
    ) -> int:
        """Adapt the handler to the extracted upstream relay state machine."""
        return relay_upstream_response(
            _relay_context_for_handler(self),
            response,
            upstream_name,
            relay_execution_plan,
            request_id=request_id,
            model=model,
            inbound_format=inbound_format,
            caller_stream=caller_stream,
            event_context=event_context,
            usage_capture=usage_capture,
            headers_already_sent=headers_already_sent,
            defer_stream_errors=defer_stream_errors,
            mark_downstream_sse_started=mark_downstream_sse_started,
            response_lifecycle_state=response_lifecycle_state,
        )
