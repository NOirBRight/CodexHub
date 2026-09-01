"""Production adapters for the four Gateway exchange ports.

Each adapter captures the per-request live state (handler, contexts) at
construction time, but reads every owning-module attribute at CALL time
(ADR-0007): test patches on gateway_transport / gateway_events /
gateway_compat / gateway_stream_semantics stay live without rebuilding the
ports.
"""

from __future__ import annotations

import time as _time
from collections.abc import Mapping
from contextlib import AbstractContextManager
from http.client import IncompleteRead
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request

import gateway_admission as _gateway_admission
import gateway_compat as _gateway_compat
import gateway_errors as _gateway_errors
import gateway_events as _gateway_events
import gateway_exchange_bindings as _gateway_exchange_bindings
import gateway_request as _gateway_request
import gateway_stream_semantics as _gateway_stream_semantics
import gateway_transport as _gateway_transport
import protocol_translation as _protocol_translation
import proxy_telemetry as _proxy_telemetry
import route_plan as _route_plan_module
from gateway_error_dispatch import PostRequestLiveState
from gateway_exchange import (
    DownstreamAction,
    DownstreamState,
    ExchangeEvent,
    OpenExchangeRequest,
    RelayExchangeRequest,
)
from gateway_interfaces import UpstreamResponseLike
from protocol_translation import PreparedExchange
from route_plan import RouteAttemptPlan

# Failure taxonomy (fixed policy, read at call time from owning modules).
_FAILURE_TYPES = None  # replaced below by a small immutable structure


class _FailureTypes:
    """Immutable failure taxonomy resolved at construction from owning modules."""

    __slots__ = (
        "downstream_closed_before_retry",
        "incomplete_read",
        "protocol_fallback_error",
        "compact_empty",
        "stream_interrupted",
        "stream_idle_timeout",
        "stream_incomplete",
        "stream_error_event",
        "lifecycle_empty_final",
        "lifecycle_final_format",
        "upstream_empty_completed",
    )

    def __init__(self) -> None:
        self.downstream_closed_before_retry = _gateway_stream_semantics.DownstreamClosedBeforeRetryError
        self.incomplete_read = IncompleteRead
        self.protocol_fallback_error = HTTPError
        self.compact_empty = _gateway_errors.CompactEmptyResponseError
        self.stream_interrupted = _gateway_stream_semantics.UpstreamStreamInterruptedError
        self.stream_idle_timeout = _gateway_errors.UpstreamStreamIdleTimeoutError
        self.stream_incomplete = _protocol_translation.UpstreamStreamIncompleteError
        self.stream_error_event = _gateway_stream_semantics.UpstreamStreamErrorEvent
        self.lifecycle_empty_final = _gateway_errors.LifecycleEmptyFinalResponseError
        self.lifecycle_final_format = _gateway_errors.LifecycleFinalFormatResponseError
        self.upstream_empty_completed = _gateway_stream_semantics.UpstreamEmptyCompletedResponseError

    @property
    def relay_retryable(self) -> tuple[type[BaseException], ...]:
        return (
            self.incomplete_read,
            self.compact_empty,
            self.stream_interrupted,
            self.stream_idle_timeout,
            self.stream_incomplete,
            self.stream_error_event,
            self.lifecycle_empty_final,
            self.lifecycle_final_format,
        )


class LiveTransport:
    """ExchangeTransport adapter over gateway_transport.open_upstream_response."""

    def __init__(self, live: PostRequestLiveState) -> None:
        self._handler = live.handler

    def open(self, opening: OpenExchangeRequest) -> AbstractContextManager[UpstreamResponseLike]:
        return _gateway_transport.open_upstream_response(
            opening.request,
            upstream_name=opening.upstream_name,
            upstream_format=opening.upstream_format,
            timeout=opening.attempt.retry.request_timeout_seconds,
            event_context=opening.event_context,
            downstream_retry_callback=opening.downstream_retry_callback,
            retry_execution=opening.attempt.retry,
            transport_policy=opening.attempt.transport_policy,
            downstream_exposed=opening.downstream_exposed,
            pre_response_deadline=opening.pre_response_deadline,
            open_attempt_budget=opening.open_attempt_budget,
        )


class LiveDownstream:
    """DownstreamPort adapter over handler relay / stream commit seams."""

    def __init__(self, live: PostRequestLiveState) -> None:
        self._handler = live.handler

    def relay(self, response: UpstreamResponseLike, relay_request: RelayExchangeRequest) -> int:
        return self._handler._relay_upstream_response(
            response,
            relay_request.upstream_name,
            request_id=relay_request.request_id,
            model=relay_request.model,
            inbound_format=relay_request.inbound_format,
            caller_stream=relay_request.caller_stream,
            event_context=relay_request.event_context,
            usage_capture=relay_request.usage_capture,
            headers_already_sent=relay_request.headers_already_sent,
            defer_stream_errors=relay_request.defer_stream_errors,
            mark_downstream_sse_started=relay_request.mark_downstream_sse_started,
            response_lifecycle_state=relay_request.response_lifecycle_state,
            relay_execution_plan=relay_request.relay_plan,
        )

    def state(self) -> DownstreamState:
        exposed = _gateway_exchange_bindings.downstream_has_been_exposed(self._handler)
        return DownstreamState(exposed=exposed, sse_started=exposed)

    def perform(self, action: DownstreamAction, **payload: Any) -> Any:
        if action is DownstreamAction.ATTACH_UPSTREAM:
            seam = _gateway_exchange_bindings.handler_downstream_stream_commit(self._handler)
            if seam is not None:
                seam.attach_upstream(payload["response"])
            return None
        if action is DownstreamAction.SET_UPSTREAM_FORMAT:
            seam = _gateway_exchange_bindings.handler_downstream_stream_commit(self._handler)
            if seam is not None:
                seam.set_upstream_format(payload["upstream_format"])
            return None
        if action is DownstreamAction.EMIT_RETRY_NOTICE:
            return self._emit_downstream_retry(payload["payload"])
        if action is DownstreamAction.FINISH_FAILURE:
            return live_finish_downstream_failure(self._handler)
        if action is DownstreamAction.HANDLE_EMPTY_COMPLETED:
            return live_handle_empty_completed(self._handler, payload["exc"])
        raise ValueError(f"unknown DownstreamAction {action!r}")

    def _emit_downstream_retry(self, payload: Mapping[str, Any]) -> bool:
        if not _gateway_exchange_bindings.downstream_has_been_exposed(self._handler):
            if not self._handler._send_sse_headers(200, ""):
                return False
        return self._handler._write_sse_event("codexhub.retry", payload)


def live_finish_downstream_failure(handler: Any) -> None:
    _gateway_exchange_bindings.downstream_has_been_exposed(handler)


def live_handle_empty_completed(handler: Any, exc: BaseException) -> bool:
    return True


class LiveControl:
    """ExecutionControl adapter: monotonic clock, gateway-cancelled sleep,
    admission cancellation checkpoint."""

    def now(self) -> float:
        return _time.monotonic()

    def wait(self, seconds: int | float) -> None:
        _gateway_admission.sleep_for_retry_with_gateway_cancellation(seconds)

    def checkpoint(self) -> None:
        active = _gateway_admission.active_gateway_request()
        if active is not None:
            active.raise_if_cancelled()


class LiveObserver:
    """ExchangeObserver adapter over gateway_events / gateway_transport event
    emission. Reads owning-module attributes at call time (ADR-0007)."""

    def __init__(self, live: PostRequestLiveState) -> None:
        self._live = live

    def record(self, event: ExchangeEvent) -> None:
        kind = event.kind
        fields = event.fields
        if kind == "request_start":
            self._live.emit_request_start_once(fields)
        elif kind == "retry":
            event_context = fields.pop("event_context", None)
            _gateway_transport._emit_upstream_retry_event(event_context, **fields)
        elif kind == "retry_suppressed":
            event_context = fields.pop("event_context", None)
            _gateway_transport._emit_upstream_retry_suppressed_event(event_context, **fields)
        elif kind == "protocol_fallback":
            _gateway_events.write_proxy_event("upstream_protocol_fallback", **fields)
        elif kind == "empty_completed":
            _gateway_events.write_proxy_event("upstream_empty_completed_response", **fields)
        elif kind == "developer_role_rewrite":
            _gateway_events.write_proxy_event("developer_role_rewrite_applied", **fields)
        elif kind == "tool_schema_boolean_normalized":
            _gateway_events.write_proxy_event("tool_schema_boolean_normalized", **fields)
        elif kind == "lifecycle_guidance":
            _gateway_events.write_adapter_event(
                self._live.adapter_event_context,
                "lifecycle_final_retry_guidance_injected",
                **fields,
            )


def build_exchange_ports(live: PostRequestLiveState) -> Any:
    """Assemble the four production ports for one POST request."""
    from gateway_exchange import ExchangePorts

    return ExchangePorts(
        transport=LiveTransport(live),
        downstream=LiveDownstream(live),
        control=LiveControl(),
        observer=LiveObserver(live),
    )

