"""Typed request-to-relay orchestration for the Python Gateway.

The facade reads HTTP bytes and supplies live-patchable typed hooks. The
implementation remains independent of the HTTP facade.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field, replace
from enum import Enum
import json
from typing import Any, Protocol

from gateway_exchange_ports import (
    DownstreamAction,
    DownstreamState,
    ExchangeEvent,
    ExchangeObserver,
    ExchangePorts,
    ExchangeTransport,
    ExecutionControl,
)
from gateway_interfaces import UpstreamResponseLike
from gateway_errors import UpstreamProtocolTranslationError
from protocol_translation import PreparedExchange, UnsupportedProtocolTranslationError
from route_primitives import (
    CAPACITY_RETRY_FAILURE_CLASSES,
    RETRY_FAILURE_PERMANENT,
    RETRY_FAILURE_QUICK_TRANSIENT,
    MutationPolicy,
    RouteProtocol,
)

from http.client import IncompleteRead
from urllib.error import HTTPError

import gateway_compat.official_passthrough as _passthrough
import gateway_errors as _gateway_errors
import gateway_stream_semantics as _gateway_stream_semantics
import gateway_transport as _gateway_transport
import protocol_translation as _protocol_translation
from gateway_errors import (
    CompactEmptyResponseError,
    LifecycleEmptyFinalResponseError,
    LifecycleFinalFormatResponseError,
    UpstreamStreamIdleTimeoutError,
)
from gateway_stream_semantics import (
    DownstreamClosedBeforeRetryError,
    UpstreamEmptyCompletedResponseError,
    UpstreamStreamErrorEvent,
    UpstreamStreamInterruptedError,
)
from protocol_translation import UpstreamStreamIncompleteError

_RUNTIME_TOOL_COMPATIBILITY_ATTEMPT_KEY = (
    _passthrough._RUNTIME_TOOL_COMPATIBILITY_ATTEMPT_KEY
)
_RELAY_RETRYABLE = (
    IncompleteRead,
    CompactEmptyResponseError,
    UpstreamStreamInterruptedError,
    UpstreamStreamIdleTimeoutError,
    UpstreamStreamIncompleteError,
    UpstreamStreamErrorEvent,
    LifecycleEmptyFinalResponseError,
    LifecycleFinalFormatResponseError,
)

from http.client import IncompleteRead
from urllib.error import HTTPError

import gateway_compat.official_passthrough as _passthrough
import gateway_errors as _gateway_errors
import gateway_stream_semantics as _gateway_stream_semantics
import gateway_transport as _gateway_transport
import protocol_translation as _protocol_translation
from gateway_errors import (
    CompactEmptyResponseError,
    LifecycleEmptyFinalResponseError,
    LifecycleFinalFormatResponseError,
    UpstreamStreamIdleTimeoutError,
)
from gateway_stream_semantics import (
    DownstreamClosedBeforeRetryError,
    UpstreamEmptyCompletedResponseError,
    UpstreamStreamErrorEvent,
    UpstreamStreamInterruptedError,
)
from protocol_translation import UpstreamStreamIncompleteError

_RUNTIME_TOOL_COMPATIBILITY_ATTEMPT_KEY = (
    _passthrough._RUNTIME_TOOL_COMPATIBILITY_ATTEMPT_KEY
)
_RELAY_RETRYABLE = (
    IncompleteRead,
    CompactEmptyResponseError,
    UpstreamStreamInterruptedError,
    UpstreamStreamIdleTimeoutError,
    UpstreamStreamIncompleteError,
    UpstreamStreamErrorEvent,
    LifecycleEmptyFinalResponseError,
    LifecycleFinalFormatResponseError,
)

class DecodeBodyHook(Protocol):
    def __call__(self, body: bytes, encoding: str | None) -> tuple[bytes, bool, str | None]: ...

class RequestKindHook(Protocol):
    def __call__(self, headers: Mapping[str, str], payload: Any, inbound_format: str) -> str: ...


class RetryPolicyLike(Protocol):
    base_relay_attempts: int
    lifecycle_final_extra_attempts: Callable[[Mapping[str, Any]], int]
    empty_completed_max_attempts: int
    emit_downstream_retry_notice: bool

    def relay_attempts_for_failure_class(self, *, failure_class: str, stream_failure: bool) -> int: ...
    def retry_delay_seconds(self, attempt: int, *, failure_class: str, retry_after_seconds: int | None = None) -> int: ...
    def capacity_elapsed_limit_allows(self, elapsed_seconds: float, delay_seconds: int | float) -> bool: ...
    def stream_elapsed_limit_allows(self, elapsed_seconds: float, delay_seconds: int | float) -> bool: ...
    def new_open_attempt_budget(self) -> dict[str, int] | None: ...


class ToolExposureLike(Protocol):
    gateway_schema_injection: bool


class RelayExecutionPlanLike(Protocol):
    selected_upstream_format: str
    request_kind: str
    streaming_policy: Any
    usage_policy: Any
    response_mutation_policy: Any
    sse_mutation_policy: Any
    verify_cross_protocol_source: bool
    lifecycle_final_retry_enabled: bool


class RouteAttemptLike(Protocol):
    index: int
    selected_upstream_format: str
    upstream_protocol: RouteProtocol
    tool_protocol: str
    tool_surface_strategy: str
    native_responses_tool_codec: str
    request_mutation_policy: MutationPolicy
    retry: RetryPolicyLike

    def prepare_body(self, body: bytes) -> PreparedExchange: ...
    def relay_execution_plan(self, *, lifecycle_final_retry_enabled: bool) -> RelayExecutionPlanLike: ...
    def allows_protocol_fallback_status(self, status: int) -> bool: ...


class RoutePlanLike(Protocol):
    attempts: tuple[RouteAttemptLike, ...]
    primary_attempt: RouteAttemptLike | None
    transparent_tool_loop_guard: bool
    tool_exposure: ToolExposureLike

@dataclass(frozen=True, slots=True)
class InboundRequest:
    """HTTP-independent inbound bytes and request-scoped routing facts."""
    request_id: str
    started_at: float
    path: str
    protocol: RouteProtocol
    provider_hint: str | None
    headers: Mapping[str, str]
    body: bytes
    request_context: Mapping[str, Any]
    proxy_request_context: Mapping[str, Any]
    raw_provider_probe: bool = False

    @property
    def inbound_format(self) -> str:
        return self.protocol.value

@dataclass(frozen=True, slots=True)
class InboundRequestHooks:
    decode_body: DecodeBodyHook
    get_header: Callable[[Mapping[str, str], str], str | None]
    request_kind: RequestKindHook
    compact_request_kind: str
    event_context_for_kind: Callable[[Mapping[str, Any], str], dict[str, Any]]
    try_extract_model: Callable[[bytes], str | None]
    provider_scoped_model: Callable[[str | None, str | None], str | None]

@dataclass(frozen=True)
class ParsedInboundRequest:
    request_id: str
    started_at: float
    path: str
    protocol: RouteProtocol
    provider_hint: str | None
    headers: Mapping[str, str]
    request_context: dict[str, Any]
    proxy_request_context: dict[str, Any]
    raw_provider_probe: bool
    content_length: int
    content_type: str | None
    content_encoding: str | None
    content_decoded: bool
    body: bytes
    inbound_payload: Any
    request_kind: str
    model_requested: str | None
    model: str | None
    route_reason: str

    @property
    def inbound_format(self) -> str:
        return self.protocol.value

def parse_inbound_request(request: InboundRequest, hooks: InboundRequestHooks) -> ParsedInboundRequest:
    """Parse caller bytes once without depending on an HTTP handler."""
    content_type = hooks.get_header(request.headers, "Content-Type")
    content_encoding = hooks.get_header(request.headers, "Content-Encoding")
    body, decoded, decode_error = hooks.decode_body(request.body, content_encoding)
    if decode_error:
        raise ValueError(f"request body content-encoding decode failed: {decode_error}")
    try:
        payload = json.loads(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        payload = None
    request_kind = hooks.request_kind(request.headers, payload, request.inbound_format)
    proxy_context = dict(request.proxy_request_context)
    if request_kind == hooks.compact_request_kind:
        proxy_context = hooks.event_context_for_kind(request.request_context, request_kind)
        if request.raw_provider_probe:
            proxy_context["raw_provider_probe"] = True
    if isinstance(payload, Mapping) and isinstance(payload.get("model"), str):
        requested = payload["model"]
    else:
        requested = hooks.try_extract_model(body)
    model = hooks.provider_scoped_model(requested, request.provider_hint)
    if request.provider_hint is not None and not model:
        raise ValueError(f"model is required for provider path: {request.provider_hint}")
    reason = "provider_path" if request.provider_hint and model else "model" if model else "official_control_fallback"
    return ParsedInboundRequest(
        request_id=request.request_id, started_at=request.started_at, path=request.path,
        protocol=request.protocol, provider_hint=request.provider_hint,
        headers=request.headers, request_context=dict(request.request_context),
        proxy_request_context=proxy_context, raw_provider_probe=request.raw_provider_probe,
        content_length=len(request.body), content_type=content_type,
        content_encoding=content_encoding, content_decoded=decoded, body=body,
        inbound_payload=payload, request_kind=request_kind, model_requested=requested,
        model=model, route_reason=reason,
    )

class OfficialMutationHook(Protocol):
    def __call__(self, body: bytes, payload: Mapping[str, Any] | None, upstream: Mapping[str, Any], *, model_id: str | None) -> bytes: ...

class TransparentMutationHook(Protocol):
    def __call__(self, body: bytes, payload: Mapping[str, Any] | None, upstream: Mapping[str, Any], *, model_id: str | None) -> bytes: ...

class CompatibilityMutationHook(Protocol):
    def __call__(self, body: bytes, upstream: Mapping[str, Any], *, model_id: str | None, event_context: Mapping[str, Any] | None, inject_codex_tools: bool, tool_protocol_override: str, tool_surface_strategy_override: str, native_responses_tool_codec_override: str) -> bytes: ...

class UpstreamRequestLike(Protocol):
    data: bytes | None
    headers: Mapping[str, str]


class RetrySafetyHook(Protocol):
    def __call__(
        self,
        exc: BaseException,
        *,
        request: UpstreamRequestLike,
        upstream_name: str,
        request_kind: str,
        downstream_exposed: bool,
        model_access_path: str,
        failure_phase: str | None,
    ) -> str: ...


class RetryEventHook(Protocol):
    def __call__(
        self,
        event_context: Mapping[str, Any] | None,
        *,
        upstream_name: str,
        upstream_format: str,
        request_kind: str,
        attempt: int,
        max_attempts: int,
        exc: BaseException,
        failure_class: str,
        failure_phase: str | None,
        retry_safety_class: str,
        delay_seconds: int | float = 0,
    ) -> None: ...


class DownstreamRetryPayloadHook(Protocol):
    def __call__(
        self,
        *,
        upstream_name: str,
        upstream_format: str,
        request_kind: str,
        attempt: int,
        max_attempts: int,
        exc: BaseException,
        delay_seconds: int | float,
        failure_class: str,
        failure_phase: str | None,
        redact_identity: str | None,
    ) -> Mapping[str, Any]: ...


class ProtocolFallbackHook(Protocol):
    def __call__(
        self,
        failed_attempt: RouteAttemptLike,
        next_attempt: RouteAttemptLike,
        exc: BaseException,
        request_observability: Mapping[str, Any],
    ) -> None: ...


@dataclass(frozen=True)
class OpenExchangeRequest:
    request: UpstreamRequestLike
    attempt: RouteAttemptLike
    upstream_name: str
    upstream_format: str
    event_context: Mapping[str, Any] | None
    downstream_retry_callback: Callable[[Mapping[str, Any]], bool] | None
    downstream_exposed: Callable[[], bool]
    pre_response_deadline: float | None
    open_attempt_budget: dict[str, int] | None

@dataclass(frozen=True)
class RelayExchangeRequest:
    attempt: RouteAttemptLike
    relay_plan: RelayExecutionPlanLike
    upstream_name: str
    request_id: str
    model: str | None
    inbound_format: str
    caller_stream: bool
    event_context: Mapping[str, Any] | None
    usage_capture: dict[str, Any]
    headers_already_sent: bool
    defer_stream_errors: bool
    mark_downstream_sse_started: Callable[[], None]
    response_lifecycle_state: dict[str, str]

@dataclass(frozen=True)
class ExchangeRequest:
    inbound: ParsedInboundRequest
    route_plan: RoutePlanLike
    upstream: Mapping[str, Any]
    upstream_name: str
    prepared_body: bytes
    inbound_payload: Any
    model_canonical: str | None
    caller_stream: bool
    prompt_cache_key: str | None
    caller_request_observability: Mapping[str, Any]
    event_context: dict[str, Any]
    proxy_request_context: dict[str, Any]
    usage_capture: dict[str, Any]
    response_lifecycle_state: dict[str, str]
    pre_response_deadline: float | None
    downstream_sse_started: bool = False

@dataclass
class ExchangeProgress:
    active_attempt: RouteAttemptLike | None = None
    relay_execution_plan: RelayExecutionPlanLike | None = None
    upstream_format: str = "responses"
    request_observability: dict[str, Any] = field(default_factory=dict)
    downstream_sse_started: bool = False
    active_prepared_exchange: PreparedExchange | None = None

class ExchangeDisposition(Enum):
    COMPLETED = "completed"
    STOPPED = "stopped"

@dataclass(frozen=True)
class ExchangeResult:
    disposition: ExchangeDisposition
    progress: ExchangeProgress
    status: int | None = None
    stop_reason: str | None = None

@dataclass(frozen=True)
class TerminalExchangeResult:
    completed: bool
    handled: bool
    status: int
    error: str | None = None

def terminal_result(result: object) -> TerminalExchangeResult:
    """Map the closed result union; unknown values fail closed."""
    if isinstance(result, ExchangeResult):
        if (
            result.disposition is ExchangeDisposition.COMPLETED
            and isinstance(result.status, int)
            and result.stop_reason is None
        ):
            return TerminalExchangeResult(True, True, result.status)
        if result.disposition is ExchangeDisposition.STOPPED:
            if result.status is not None or result.stop_reason not in {"downstream_closed", "empty_completed_response"}:
                return TerminalExchangeResult(False, False, 500, "invalid_exchange_result")
            status = 499 if result.stop_reason == "downstream_closed" else 502
            return TerminalExchangeResult(False, True, status, result.stop_reason)
    return TerminalExchangeResult(False, False, 500, "invalid_exchange_result")

def exchange_error_status(exc: BaseException) -> int | None:
    """Extract an HTTP status code from an exception (fixed policy)."""
    status_value = getattr(exc, "code", None)
    return status_value if isinstance(status_value, int) else None


def request_observability_for_attempt(
    request: "ExchangeRequest",
    attempt: RouteAttemptLike,
    attempt_body: bytes,
) -> dict[str, Any]:
    """Build observability fields for one executed attempt (fixed policy).

    Reads owning-module attributes at call time (ADR-0007) so patches stay
    live: proxy_telemetry enrichment, gateway_request prefixing.
    """
    import proxy_telemetry as _proxy_telemetry
    import gateway_request as _gateway_request
    import gateway_events as _gateway_events

    upstream_observability = _proxy_telemetry.enrich_request_observability(
        body=attempt_body,
        codex_home=_gateway_events.RUNTIME_CODEX_DIR,
        upstream=request.upstream,
        include_body_hmac=(
            attempt.request_mutation_policy
            != MutationPolicy.OFFICIAL_PASSTHROUGH
        ),
        prompt_cache_key=request.prompt_cache_key,
        extract_prompt_cache_key=(
            attempt.request_mutation_policy
            != MutationPolicy.OFFICIAL_PASSTHROUGH
        ),
    )
    return {
        **upstream_observability,
        **_gateway_request.request_observability_with_prefix(
            request.caller_request_observability, "caller"
        ),
        **_gateway_request.request_observability_with_prefix(
            upstream_observability, "upstream"
        ),
        "request_observability_scope": "executed_attempt",
        "request_observability_attempt_index": attempt.index,
        "request_observability_upstream_protocol": (
            attempt.upstream_protocol.value
        ),
    }


def protocol_fallback_fields(
    request: "ExchangeRequest",
    failed_attempt: RouteAttemptLike,
    next_attempt: RouteAttemptLike,
    exc: BaseException,
    attempt_observability: Mapping[str, Any],
) -> dict[str, Any]:
    """Build the upstream_protocol_fallback event fields (fixed policy)."""
    import gateway_errors as _errors
    import gateway_request as _greq

    failed = failed_attempt.telemetry_snapshot()
    following = next_attempt.telemetry_snapshot()
    return {
        "request_id": request.inbound.request_id,
        "model": request.inbound.model,
        "model_requested": request.inbound.model_requested,
        "model_canonical": request.model_canonical,
        "upstream": request.upstream_name,
        "provider_id": request.upstream_name,
        "provider_hint": request.inbound.provider_hint,
        "upstream_format": (
            request.route_plan.configured_upstream_protocol_name
        ),
        "behavior_profile": getattr(request, "behavior_profile", None),
        "failed_upstream_format": (
            failed_attempt.selected_upstream_format
        ),
        "next_upstream_format": (
            next_attempt.selected_upstream_format
        ),
        "failed_route_attempt_index": failed["index"],
        "failed_route_attempt_request_body_mode": (
            failed["request_body_mode"]
        ),
        "failed_route_attempt_request_conversion_steps": (
            failed["request_conversion_steps"]
        ),
        "failed_route_attempt_mutation_summary": (
            failed["mutation_summary"]
        ),
        "next_route_attempt_index": following["index"],
        "next_route_attempt_request_body_mode": (
            following["request_body_mode"]
        ),
        "next_route_attempt_request_conversion_steps": (
            following["request_conversion_steps"]
        ),
        "next_route_attempt_mutation_summary": (
            following["mutation_summary"]
        ),
        "status": getattr(exc, "code", 502),
        "error": "HTTPError",
        "detail": _errors.safe_upstream_error_detail(
            exc,
            redact_identity=_gateway_transport.retry_identity_from_context(
                request.event_context
            ),
        ),
        **dict(attempt_observability),
        **request.proxy_request_context,
    }


def _prepare_attempt_body(request: ExchangeRequest, attempt: RouteAttemptLike, observer: ExchangeObserver | None = None) -> tuple[PreparedExchange, bytes]:
    """Canonicalize, adapt, then perform exactly one selected wire conversion.

    Fixed request policy stays in this module. Mutation helpers are read from
    gateway_compat at call time (ADR-0007) so test patches stay live.
    """
    import gateway_compat as _gateway_compat
    import gateway_compat.official_passthrough as _passthrough

    policy = attempt.request_mutation_policy
    upstream = dict(request.upstream)
    upstream["upstream_format"] = attempt.selected_upstream_format
    conversion_body = request.prepared_body
    prepared_exchange: PreparedExchange | None = None
    pre_compatibility_applied = False
    caller_is_chat = request.inbound.inbound_format == "chat_completions"
    attempt_is_responses = attempt.selected_upstream_format == "responses"
    if policy in (MutationPolicy.TRANSPARENT, MutationPolicy.GATEWAY_COMPATIBILITY) and caller_is_chat and attempt_is_responses:
        try:
            prepared_exchange = attempt.prepare_body(conversion_body)
        except UnsupportedProtocolTranslationError as exc:
            raise UpstreamProtocolTranslationError(exc) from exc
        conversion_body = prepared_exchange.upstream_body
    if policy is MutationPolicy.TRANSPARENT:
        mutation_upstream = dict(upstream)
        mutation_upstream["upstream_format"] = attempt.selected_upstream_format if prepared_exchange is not None else request.inbound.inbound_format
        conversion_body = _gateway_compat.transparent_request_body(conversion_body, _passthrough._safe_json_mapping(conversion_body), mutation_upstream, model_id=request.inbound.model)
        conversion_body, developer_rewrites = _passthrough._rewrite_transparent_developer_role_messages(conversion_body, mutation_upstream)
        if developer_rewrites:
            request.proxy_request_context["developer_role_rewrites"] = developer_rewrites
            if observer is not None:
                observer.record(ExchangeEvent("developer_role_rewrite", {
                    "request_id": request.inbound.request_id,
                    "model": request.model_canonical,
                    "upstream": request.upstream_name,
                    "inbound_format": request.inbound.inbound_format,
                    "messages_rewritten": developer_rewrites,
                    **request.proxy_request_context,
                }))
        conversion_body, schema_rewrites = _passthrough._normalize_transparent_tool_schema_booleans(conversion_body)
        if schema_rewrites:
            request.proxy_request_context["tool_schema_rewrites"] = schema_rewrites
            if observer is not None:
                observer.record(ExchangeEvent("tool_schema_boolean_normalized", {
                    "request_id": request.inbound.request_id,
                    "model": request.model_canonical,
                    "upstream": request.upstream_name,
                    "inbound_format": request.inbound.inbound_format,
                    "schemas_rewritten": schema_rewrites,
                    **request.proxy_request_context,
                }))
        if request.route_plan.transparent_tool_loop_guard:
            transparent_payload = _passthrough._safe_json_mapping(conversion_body) or {}
            repeated_count = (
                _passthrough._excessive_transparent_responses_tool_loop_count(transparent_payload)
                if mutation_upstream["upstream_format"] == "responses"
                else _passthrough._excessive_transparent_chat_tool_loop_count(transparent_payload)
                if mutation_upstream["upstream_format"] == "chat_completions"
                else None
            )
            if repeated_count is not None:
                raise UpstreamProtocolTranslationError(
                    UnsupportedProtocolTranslationError(
                        _passthrough.EXCESSIVE_TOOL_LOOP_ERROR_CODE,
                        "Repeated successful function calls exceeded "
                        f"the bound of {_passthrough.EXCESSIVE_TOOL_LOOP_BOUND}.",
                    )
                )
    elif policy is MutationPolicy.GATEWAY_COMPATIBILITY and (prepared_exchange is not None or (not caller_is_chat and attempt.selected_upstream_format == "chat_completions")):
        conversion_body = _gateway_compat.compatible_request_body(conversion_body, upstream, model_id=request.inbound.model, event_context=request.event_context, inject_codex_tools=request.route_plan.tool_exposure.gateway_schema_injection, tool_protocol_override=attempt.tool_protocol, tool_surface_strategy_override=attempt.tool_surface_strategy, native_responses_tool_codec_override=attempt.native_responses_tool_codec)
        pre_compatibility_applied = True
    if prepared_exchange is None:
        try:
            prepared_exchange = attempt.prepare_body(conversion_body)
        except UnsupportedProtocolTranslationError as exc:
            raise UpstreamProtocolTranslationError(exc) from exc
    else:
        prepared_exchange = replace(prepared_exchange, upstream_body=conversion_body)
    body = prepared_exchange.upstream_body
    if policy is MutationPolicy.OFFICIAL_PASSTHROUGH:
        payload = request.inbound_payload if attempt.selected_upstream_format == request.inbound.inbound_format and isinstance(request.inbound_payload, Mapping) else _passthrough._safe_json_mapping(body)
        return prepared_exchange, _gateway_compat.official_passthrough_request_body(body, payload, upstream, model_id=request.inbound.model)
    if policy is MutationPolicy.GATEWAY_COMPATIBILITY and not pre_compatibility_applied:
        body = _gateway_compat.compatible_request_body(body, upstream, model_id=request.inbound.model, event_context=request.event_context, inject_codex_tools=request.route_plan.tool_exposure.gateway_schema_injection, tool_protocol_override=attempt.tool_protocol, tool_surface_strategy_override=attempt.tool_surface_strategy, native_responses_tool_codec_override=attempt.native_responses_tool_codec)
    if policy is MutationPolicy.GATEWAY_COMPATIBILITY:
        body, schema_rewrites = _passthrough._normalize_transparent_tool_schema_booleans(body)
        if schema_rewrites:
            request.proxy_request_context["tool_schema_rewrites"] = schema_rewrites
            if observer is not None:
                observer.record(ExchangeEvent("tool_schema_boolean_normalized", {
                    "request_id": request.inbound.request_id,
                    "model": request.model_canonical,
                    "upstream": request.upstream_name,
                    "inbound_format": request.inbound.inbound_format,
                    "schemas_rewritten": schema_rewrites,
                    **request.proxy_request_context,
                }))
    return prepared_exchange, body


def execute_exchange(request: ExchangeRequest, ports: ExchangePorts, *, progress: ExchangeProgress | None = None) -> ExchangeResult:
    """Prepare and execute RoutePlanLike attempts through the four ports.

    Fixed policy (retry arithmetic, fallback decisions, elapsed limits,
    failure taxonomy) lives here; ports carry transport, downstream relay,
    clock/cancellation, and typed event records.
    """
    import gateway_admission as _gateway_admission
    import gateway_transport as _gateway_transport
    import gateway_events as _gateway_events
    import gateway_compat.official_passthrough as _passthrough

    transport = ports.transport
    downstream = ports.downstream
    control = ports.control
    observer = ports.observer

    state = progress or ExchangeProgress()
    state.downstream_sse_started = request.downstream_sse_started
    bodies: dict[int, bytes] = {}
    exchanges: dict[int, PreparedExchange] = {}

    def body_for(attempt: RouteAttemptLike) -> bytes:
        if attempt.index not in bodies:
            exchange, body = _prepare_attempt_body(request, attempt, observer)
            exchanges[attempt.index], bodies[attempt.index] = exchange, body
        state.active_prepared_exchange = exchanges[attempt.index]
        return bodies[attempt.index]

    primary = request.route_plan.primary_attempt
    if primary is None:
        raise RuntimeError("cannot execute a route plan without attempts")
    state.request_observability = request_observability_for_attempt(request, primary, body_for(primary))
    observer.record(ExchangeEvent("request_start", dict(state.request_observability)))
    emit_notice = primary.retry.emit_downstream_retry_notice
    open_budget = primary.retry.new_open_attempt_budget()
    generation = 0

    for attempt in request.route_plan.attempts:
        attempt_request_kind = getattr(attempt.retry, "request_kind", request.inbound.request_kind)
        state.active_attempt = attempt
        state.upstream_format = attempt.selected_upstream_format
        request.event_context["tool_protocol"] = attempt.tool_protocol
        relay_attempts = attempt.retry.base_relay_attempts
        lifecycle_extra = attempt.retry.lifecycle_final_extra_attempts(request.event_context)
        state.relay_execution_plan = attempt.relay_execution_plan(lifecycle_final_retry_enabled=lifecycle_extra > 0)
        max_relay_attempts = relay_attempts + lifecycle_extra
        relay_attempt = 1
        lifecycle_reason: str | None = None
        try:
            while relay_attempt <= max_relay_attempts:
                generation += 1
                request.event_context[_RUNTIME_TOOL_COMPATIBILITY_ATTEMPT_KEY] = generation
                attempt_body = body_for(attempt)
                if lifecycle_reason and attempt.upstream_protocol is RouteProtocol.RESPONSES:
                    attempt_body = _passthrough._responses_body_with_lifecycle_final_retry_guidance(attempt_body, lifecycle_reason)
                    observer.record(ExchangeEvent("lifecycle_guidance", {
                        "upstream": request.upstream_name,
                        "upstream_format": "responses",
                        "reason": lifecycle_reason,
                    }))
                upstream_request = _gateway_transport.build_request_url(
                    attempt.endpoint_url,
                    data=attempt_body,
                    headers=attempt.request_headers.to_dict(),
                    method="POST",
                )
                state.request_observability = request_observability_for_attempt(request, attempt, attempt_body)
                observer.record(ExchangeEvent("request_start", dict(state.request_observability)))

                def mark_started() -> None:
                    state.downstream_sse_started = True

                def emit_open_retry(payload: Mapping[str, Any]) -> bool:
                    emitted = downstream.perform(DownstreamAction.EMIT_RETRY_NOTICE, payload=payload)
                    if emitted:
                        state.downstream_sse_started = True
                    return emitted

                try:
                    with transport.open(OpenExchangeRequest(
                        request=upstream_request, attempt=attempt,
                        upstream_name=request.upstream_name,
                        upstream_format=state.upstream_format,
                        event_context=request.event_context,
                        downstream_retry_callback=emit_open_retry if emit_notice else None,
                        downstream_exposed=lambda: downstream.state().exposed,
                        pre_response_deadline=None if state.downstream_sse_started else request.pre_response_deadline,
                        open_attempt_budget=open_budget,
                    )) as response:
                        downstream.perform(DownstreamAction.ATTACH_UPSTREAM, response=response)
                        status = downstream.relay(response, RelayExchangeRequest(
                            attempt=attempt, relay_plan=state.relay_execution_plan,
                            upstream_name=request.upstream_name,
                            request_id=request.inbound.request_id, model=request.model_canonical,
                            inbound_format=request.inbound.inbound_format,
                            caller_stream=request.caller_stream,
                            event_context=request.event_context,
                            usage_capture=request.usage_capture,
                            headers_already_sent=state.downstream_sse_started,
                            defer_stream_errors=relay_attempt < relay_attempts,
                            mark_downstream_sse_started=mark_started,
                            response_lifecycle_state=request.response_lifecycle_state,
                        ))
                    return ExchangeResult(ExchangeDisposition.COMPLETED, state, status=status)
                except _gateway_stream_semantics.DownstreamClosedBeforeRetryError:
                    downstream.perform(DownstreamAction.FINISH_FAILURE)
                    return ExchangeResult(ExchangeDisposition.STOPPED, state, stop_reason="downstream_closed")
                except _RELAY_RETRYABLE as exc:
                    control.checkpoint()
                    lifecycle_retry = isinstance(exc, (_gateway_errors.LifecycleEmptyFinalResponseError, _gateway_errors.LifecycleFinalFormatResponseError))
                    if lifecycle_retry:
                        stream_failure, retry_exc = True, exc
                        failure_class = RETRY_FAILURE_QUICK_TRANSIENT
                        lifecycle_reason = "empty" if isinstance(exc, _gateway_errors.LifecycleEmptyFinalResponseError) else "format"
                        retry_limit, delay = max_relay_attempts, 0
                    else:
                        stream_failure = isinstance(exc, (_gateway_stream_semantics.UpstreamStreamInterruptedError, _gateway_errors.UpstreamStreamIdleTimeoutError, _protocol_translation.UpstreamStreamIncompleteError, _gateway_stream_semantics.UpstreamStreamErrorEvent))
                        retry_exc = getattr(exc, "cause", exc) if isinstance(exc, _gateway_stream_semantics.UpstreamStreamInterruptedError) else exc
                        failure_class = _gateway_transport.upstream_failure_class(retry_exc)
                        relay_attempts = attempt.retry.relay_attempts_for_failure_class(failure_class=failure_class, stream_failure=stream_failure)
                        if isinstance(retry_exc, _gateway_stream_semantics.UpstreamEmptyCompletedResponseError):
                            relay_attempts = min(relay_attempts, attempt.retry.empty_completed_max_attempts)
                        max_relay_attempts = relay_attempts + lifecycle_extra
                        retry_limit = relay_attempts
                        delay = 0
                    phase = "stream_body" if stream_failure else None
                    safety = _gateway_transport._retry_safety_class(
                        retry_exc, request=upstream_request,
                        upstream_name=request.upstream_name,
                        request_kind=attempt_request_kind,
                        downstream_exposed=lambda: downstream.state().exposed,
                        model_access_path=_gateway_transport.model_access_path_from_event_context(request.event_context, request.upstream_name, state.upstream_format),
                        failure_phase=phase,
                    )
                    if safety in _gateway_transport.SUPPRESSED_RETRY_SAFETY_CLASSES:
                        observer.record(ExchangeEvent("retry_suppressed", {
                            "event_context": request.event_context,
                            "upstream_name": request.upstream_name, "upstream_format": state.upstream_format,
                            "request_kind": attempt_request_kind, "attempt": relay_attempt, "max_attempts": retry_limit,
                            "exc": retry_exc, "failure_class": failure_class, "failure_phase": phase,
                            "retry_safety_class": safety,
                        }))
                        if isinstance(retry_exc, _gateway_stream_semantics.UpstreamEmptyCompletedResponseError):
                            wrote = downstream.perform(DownstreamAction.HANDLE_EMPTY_COMPLETED, exc=retry_exc)
                            if not wrote:
                                downstream.perform(DownstreamAction.FINISH_FAILURE)
                                return ExchangeResult(ExchangeDisposition.STOPPED, state, stop_reason="downstream_closed")
                            return ExchangeResult(ExchangeDisposition.STOPPED, state, stop_reason="empty_completed_response")
                        raise retry_exc
                    if lifecycle_retry:
                        if relay_attempt >= retry_limit:
                            raise
                    else:
                        if (
                            relay_attempt >= retry_limit
                            or failure_class == RETRY_FAILURE_PERMANENT
                        ):
                            raise retry_exc
                        delay = attempt.retry.retry_delay_seconds(
                            relay_attempt,
                            failure_class=failure_class,
                            retry_after_seconds=_gateway_transport.retry_after_delay_seconds(
                                retry_exc
                            ),
                        )
                        elapsed = max(
                            0.0,
                            control.now() - request.inbound.started_at,
                        )
                        if (
                            failure_class in CAPACITY_RETRY_FAILURE_CLASSES
                            and not attempt.retry.capacity_elapsed_limit_allows(
                                elapsed, delay
                            )
                        ):
                            raise retry_exc
                        if (
                            stream_failure
                            and failure_class
                            == RETRY_FAILURE_QUICK_TRANSIENT
                            and not attempt.retry.stream_elapsed_limit_allows(
                                elapsed, delay
                            )
                        ):
                            raise retry_exc
                    observer.record(ExchangeEvent("retry", {
                        "event_context": request.event_context,
                        "upstream_name": request.upstream_name, "upstream_format": state.upstream_format,
                        "request_kind": attempt_request_kind, "attempt": relay_attempt, "max_attempts": retry_limit,
                        "exc": retry_exc, "delay_seconds": delay, "failure_class": failure_class,
                        "failure_phase": phase, "retry_safety_class": safety,
                    }))
                    if emit_notice:
                        payload = _gateway_transport._downstream_retry_payload(upstream_name=request.upstream_name, upstream_format=state.upstream_format, request_kind=attempt_request_kind, attempt=relay_attempt, max_attempts=retry_limit, exc=retry_exc, delay_seconds=delay, failure_class=failure_class, failure_phase=phase, redact_identity=_gateway_transport.retry_identity_from_context(request.event_context))
                        if not downstream.perform(DownstreamAction.EMIT_RETRY_NOTICE, payload=payload):
                            downstream.perform(DownstreamAction.FINISH_FAILURE)
                            return ExchangeResult(ExchangeDisposition.STOPPED, state, stop_reason="downstream_closed")
                        state.downstream_sse_started = True
                    control.wait(delay)
                    relay_attempt += 1
            raise RuntimeError("unreachable upstream relay retry state")
        except _gateway_errors.HTTPError as exc:
            next_index = attempt.index + 1
            next_attempt = request.route_plan.attempts[next_index] if next_index < len(request.route_plan.attempts) else None
            if next_attempt is not None and not state.downstream_sse_started and attempt.allows_protocol_fallback_status(exchange_error_status(exc)):
                safety = _gateway_transport._retry_safety_class(
                    exc, request=upstream_request,
                    upstream_name=request.upstream_name,
                    request_kind=attempt_request_kind,
                    downstream_exposed=lambda: downstream.state().exposed,
                    model_access_path=_gateway_transport.model_access_path_from_event_context(request.event_context, request.upstream_name, state.upstream_format),
                    failure_phase="response_headers",
                )
                if safety not in _gateway_transport.SUPPRESSED_RETRY_SAFETY_CLASSES:
                    observer.record(ExchangeEvent("protocol_fallback", protocol_fallback_fields(request, attempt, next_attempt, exc, state.request_observability)))
                    continue
                observer.record(ExchangeEvent("retry_suppressed", {
                    "event_context": request.event_context,
                    "upstream_name": request.upstream_name, "upstream_format": state.upstream_format,
                    "request_kind": attempt_request_kind, "attempt": relay_attempt, "max_attempts": relay_attempts,
                    "exc": exc, "failure_class": _gateway_transport.upstream_failure_class(exc),
                    "failure_phase": "response_headers", "retry_safety_class": safety,
                }))
            raise
    raise RuntimeError("unreachable upstream protocol selection state")
