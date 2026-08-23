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

@dataclass(frozen=True)
class ExchangeFailureTypes:
    downstream_closed_before_retry: type[BaseException]
    incomplete_read: type[BaseException]
    protocol_fallback_error: type[BaseException]
    compact_empty: type[BaseException]
    stream_interrupted: type[BaseException]
    stream_idle_timeout: type[BaseException]
    stream_incomplete: type[BaseException]
    stream_error_event: type[BaseException]
    lifecycle_empty_final: type[BaseException]
    lifecycle_final_format: type[BaseException]
    upstream_empty_completed: type[BaseException]

    @property
    def relay_retryable(self) -> tuple[type[BaseException], ...]:
        return (self.incomplete_read, self.compact_empty, self.stream_interrupted, self.stream_idle_timeout,
                self.stream_incomplete, self.stream_error_event,
                self.lifecycle_empty_final, self.lifecycle_final_format)

@dataclass(frozen=True)
class ExchangeHooks:
    """Typed live dependencies used by exchange orchestration."""
    failure_types: ExchangeFailureTypes
    set_active_prepared_exchange: Callable[[PreparedExchange], None]
    activate_attempt: Callable[[RouteAttemptLike], None]
    safe_json_mapping: Callable[[bytes], Mapping[str, Any] | None]
    official_mutation: OfficialMutationHook
    transparent_mutation: TransparentMutationHook
    rewrite_developer_roles: Callable[[bytes, Mapping[str, Any]], tuple[bytes, int]]
    normalize_tool_schema_booleans: Callable[[bytes], tuple[bytes, int]]
    validate_transparent_tool_loop: Callable[[bytes, str], None]
    compatibility_mutation: CompatibilityMutationHook
    request_observability: Callable[[RouteAttemptLike, bytes], dict[str, Any]]
    emit_request_start: Callable[[Mapping[str, Any]], None]
    build_request: Callable[[RouteAttemptLike, bytes], UpstreamRequestLike]
    lifecycle_guidance: Callable[[bytes, str], bytes]
    open_response: Callable[[OpenExchangeRequest], AbstractContextManager[UpstreamResponseLike]]
    relay_response: Callable[[UpstreamResponseLike, RelayExchangeRequest], int]
    set_upstream_format: Callable[[str], None]
    attach_upstream: Callable[[UpstreamResponseLike], None]
    downstream_exposed: Callable[[], bool]
    raise_if_cancelled: Callable[[], None]
    emit_downstream_retry: Callable[[Mapping[str, Any]], bool]
    finish_downstream_failure: Callable[[], None]
    failure_class: Callable[[BaseException], str]
    retry_safety_class: RetrySafetyHook
    model_access_path: Callable[[Mapping[str, Any] | None, str, str], str]
    retry_after_seconds: Callable[[BaseException], int | float | None]
    emit_retry: RetryEventHook
    emit_retry_suppressed: RetryEventHook
    downstream_retry_payload: DownstreamRetryPayloadHook
    retry_identity: Callable[[Mapping[str, Any] | None], str | None]
    sleep: Callable[[int | float], None]
    protocol_fallback: ProtocolFallbackHook
    error_status: Callable[[BaseException], int | None]
    handle_empty_completed: Callable[[BaseException], bool]
    monotonic: Callable[[], float]
    suppressed_retry_safety_classes: frozenset[str] = field(default_factory=frozenset)
    permanent_failure_class: str = RETRY_FAILURE_PERMANENT
    quick_transient_failure_class: str = RETRY_FAILURE_QUICK_TRANSIENT
    runtime_attempt_key: str = "_runtime_tool_compatibility_attempt_generation"

def _prepare_attempt_body(request: ExchangeRequest, attempt: RouteAttemptLike, hooks: ExchangeHooks) -> tuple[PreparedExchange, bytes]:
    """Canonicalize, adapt, then perform exactly one selected wire conversion."""
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
        conversion_body = hooks.transparent_mutation(conversion_body, hooks.safe_json_mapping(conversion_body), mutation_upstream, model_id=request.inbound.model)
        conversion_body, developer_rewrites = hooks.rewrite_developer_roles(conversion_body, mutation_upstream)
        if developer_rewrites:
            request.proxy_request_context["developer_role_rewrites"] = developer_rewrites
        conversion_body, schema_rewrites = hooks.normalize_tool_schema_booleans(conversion_body)
        if schema_rewrites:
            request.proxy_request_context["tool_schema_rewrites"] = schema_rewrites
        if request.route_plan.transparent_tool_loop_guard:
            hooks.validate_transparent_tool_loop(conversion_body, mutation_upstream["upstream_format"])
    elif policy is MutationPolicy.GATEWAY_COMPATIBILITY and (prepared_exchange is not None or (not caller_is_chat and attempt.selected_upstream_format == "chat_completions")):
        conversion_body = hooks.compatibility_mutation(conversion_body, upstream, model_id=request.inbound.model, event_context=request.event_context, inject_codex_tools=request.route_plan.tool_exposure.gateway_schema_injection, tool_protocol_override=attempt.tool_protocol, tool_surface_strategy_override=attempt.tool_surface_strategy, native_responses_tool_codec_override=attempt.native_responses_tool_codec)
        pre_compatibility_applied = True
    if prepared_exchange is None:
        try:
            prepared_exchange = attempt.prepare_body(conversion_body)
        except UnsupportedProtocolTranslationError as exc:
            raise UpstreamProtocolTranslationError(exc) from exc
    else:
        prepared_exchange = replace(prepared_exchange, upstream_body=conversion_body)
    hooks.set_active_prepared_exchange(prepared_exchange)
    body = prepared_exchange.upstream_body
    if policy is MutationPolicy.OFFICIAL_PASSTHROUGH:
        payload = request.inbound_payload if attempt.selected_upstream_format == request.inbound.inbound_format and isinstance(request.inbound_payload, Mapping) else hooks.safe_json_mapping(body)
        return prepared_exchange, hooks.official_mutation(body, payload, upstream, model_id=request.inbound.model)
    if policy is MutationPolicy.GATEWAY_COMPATIBILITY and not pre_compatibility_applied:
        body = hooks.compatibility_mutation(body, upstream, model_id=request.inbound.model, event_context=request.event_context, inject_codex_tools=request.route_plan.tool_exposure.gateway_schema_injection, tool_protocol_override=attempt.tool_protocol, tool_surface_strategy_override=attempt.tool_surface_strategy, native_responses_tool_codec_override=attempt.native_responses_tool_codec)
    return prepared_exchange, body

def execute_exchange(request: ExchangeRequest, hooks: ExchangeHooks, *, progress: ExchangeProgress | None = None) -> ExchangeResult:
    """Prepare and execute RoutePlanLike attempts through transport and relay hooks."""
    state = progress or ExchangeProgress()
    state.downstream_sse_started = request.downstream_sse_started
    bodies: dict[int, bytes] = {}
    exchanges: dict[int, PreparedExchange] = {}
    def body_for(attempt: RouteAttemptLike) -> bytes:
        if attempt.index not in bodies:
            exchange, body = _prepare_attempt_body(request, attempt, hooks)
            exchanges[attempt.index], bodies[attempt.index] = exchange, body
        state.active_prepared_exchange = exchanges[attempt.index]
        hooks.set_active_prepared_exchange(exchanges[attempt.index])
        return bodies[attempt.index]

    primary = request.route_plan.primary_attempt
    if primary is None:
        raise RuntimeError("cannot execute a route plan without attempts")
    state.request_observability = hooks.request_observability(primary, body_for(primary))
    hooks.emit_request_start(state.request_observability)
    emit_notice = primary.retry.emit_downstream_retry_notice
    open_budget = primary.retry.new_open_attempt_budget()
    generation = 0

    for attempt in request.route_plan.attempts:
        attempt_request_kind = getattr(attempt.retry, "request_kind", request.inbound.request_kind)
        state.active_attempt = attempt
        state.upstream_format = attempt.selected_upstream_format
        hooks.activate_attempt(attempt)
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
                request.event_context[hooks.runtime_attempt_key] = generation
                hooks.set_upstream_format(state.upstream_format)
                attempt_body = body_for(attempt)
                if lifecycle_reason and attempt.upstream_protocol is RouteProtocol.RESPONSES:
                    attempt_body = hooks.lifecycle_guidance(attempt_body, lifecycle_reason)
                upstream_request = hooks.build_request(attempt, attempt_body)
                state.request_observability = hooks.request_observability(attempt, attempt_body)
                hooks.emit_request_start(state.request_observability)
                def mark_started() -> None:
                    state.downstream_sse_started = True

                def emit_open_retry(payload: Mapping[str, Any]) -> bool:
                    emitted = hooks.emit_downstream_retry(payload)
                    if emitted:
                        state.downstream_sse_started = True
                    return emitted

                try:
                    with hooks.open_response(OpenExchangeRequest(
                        request=upstream_request, attempt=attempt,
                        upstream_name=request.upstream_name,
                        upstream_format=state.upstream_format,
                        event_context=request.event_context,
                        downstream_retry_callback=emit_open_retry if emit_notice else None,
                        downstream_exposed=hooks.downstream_exposed,
                        pre_response_deadline=None if state.downstream_sse_started else request.pre_response_deadline,
                        open_attempt_budget=open_budget,
                    )) as response:
                        hooks.attach_upstream(response)
                        status = hooks.relay_response(response, RelayExchangeRequest(
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
                except hooks.failure_types.downstream_closed_before_retry:
                    hooks.finish_downstream_failure()
                    return ExchangeResult(ExchangeDisposition.STOPPED, state, stop_reason="downstream_closed")
                except hooks.failure_types.relay_retryable as exc:
                    hooks.raise_if_cancelled()
                    lifecycle_retry = isinstance(exc, (hooks.failure_types.lifecycle_empty_final, hooks.failure_types.lifecycle_final_format))
                    if lifecycle_retry:
                        stream_failure, retry_exc = True, exc
                        failure_class = hooks.quick_transient_failure_class
                        lifecycle_reason = "empty" if isinstance(exc, hooks.failure_types.lifecycle_empty_final) else "format"
                        retry_limit, delay = max_relay_attempts, 0
                    else:
                        stream_failure = isinstance(exc, (hooks.failure_types.stream_interrupted, hooks.failure_types.stream_idle_timeout, hooks.failure_types.stream_incomplete, hooks.failure_types.stream_error_event))
                        retry_exc = getattr(exc, "cause", exc) if isinstance(exc, hooks.failure_types.stream_interrupted) else exc
                        failure_class = hooks.failure_class(retry_exc)
                        relay_attempts = attempt.retry.relay_attempts_for_failure_class(failure_class=failure_class, stream_failure=stream_failure)
                        if isinstance(retry_exc, hooks.failure_types.upstream_empty_completed):
                            relay_attempts = min(relay_attempts, attempt.retry.empty_completed_max_attempts)
                        max_relay_attempts = relay_attempts + lifecycle_extra
                        retry_limit = relay_attempts
                        delay = 0
                    phase = "stream_body" if stream_failure else None
                    safety = hooks.retry_safety_class(
                        retry_exc, request=upstream_request,
                        upstream_name=request.upstream_name,
                        request_kind=attempt_request_kind,
                        downstream_exposed=hooks.downstream_exposed(),
                        model_access_path=hooks.model_access_path(request.event_context, request.upstream_name, state.upstream_format),
                        failure_phase=phase,
                    )
                    if safety in hooks.suppressed_retry_safety_classes:
                        hooks.emit_retry_suppressed(request.event_context, upstream_name=request.upstream_name, upstream_format=state.upstream_format, request_kind=attempt_request_kind, attempt=relay_attempt, max_attempts=retry_limit, exc=retry_exc, failure_class=failure_class, failure_phase=phase, retry_safety_class=safety)
                        if isinstance(retry_exc, hooks.failure_types.upstream_empty_completed):
                            if not hooks.handle_empty_completed(retry_exc):
                                hooks.finish_downstream_failure()
                                return ExchangeResult(ExchangeDisposition.STOPPED, state, stop_reason="downstream_closed")
                            return ExchangeResult(ExchangeDisposition.STOPPED, state, stop_reason="empty_completed_response")
                        raise retry_exc
                    if lifecycle_retry:
                        if relay_attempt >= retry_limit:
                            raise
                    else:
                        if (
                            relay_attempt >= retry_limit
                            or failure_class == hooks.permanent_failure_class
                        ):
                            raise retry_exc
                        delay = attempt.retry.retry_delay_seconds(
                            relay_attempt,
                            failure_class=failure_class,
                            retry_after_seconds=hooks.retry_after_seconds(
                                retry_exc
                            ),
                        )
                        elapsed = max(
                            0.0,
                            hooks.monotonic() - request.inbound.started_at,
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
                            == hooks.quick_transient_failure_class
                            and not attempt.retry.stream_elapsed_limit_allows(
                                elapsed, delay
                            )
                        ):
                            raise retry_exc
                    hooks.emit_retry(request.event_context, upstream_name=request.upstream_name, upstream_format=state.upstream_format, request_kind=attempt_request_kind, attempt=relay_attempt, max_attempts=retry_limit, exc=retry_exc, delay_seconds=delay, failure_class=failure_class, failure_phase=phase, retry_safety_class=safety)
                    if emit_notice:
                        payload = hooks.downstream_retry_payload(upstream_name=request.upstream_name, upstream_format=state.upstream_format, request_kind=attempt_request_kind, attempt=relay_attempt, max_attempts=retry_limit, exc=retry_exc, delay_seconds=delay, failure_class=failure_class, failure_phase=phase, redact_identity=hooks.retry_identity(request.event_context))
                        if not hooks.emit_downstream_retry(payload):
                            hooks.finish_downstream_failure()
                            return ExchangeResult(ExchangeDisposition.STOPPED, state, stop_reason="downstream_closed")
                        state.downstream_sse_started = True
                    hooks.sleep(delay)
                    relay_attempt += 1
            raise RuntimeError("unreachable upstream relay retry state")
        except hooks.failure_types.protocol_fallback_error as exc:
            next_index = attempt.index + 1
            next_attempt = request.route_plan.attempts[next_index] if next_index < len(request.route_plan.attempts) else None
            if next_attempt is not None and not state.downstream_sse_started and attempt.allows_protocol_fallback_status(hooks.error_status(exc)):
                safety = hooks.retry_safety_class(
                    exc, request=upstream_request,
                    upstream_name=request.upstream_name,
                    request_kind=attempt_request_kind,
                    downstream_exposed=hooks.downstream_exposed(),
                    model_access_path=hooks.model_access_path(request.event_context, request.upstream_name, state.upstream_format),
                    failure_phase="response_headers",
                )
                if safety not in hooks.suppressed_retry_safety_classes:
                    hooks.protocol_fallback(attempt, next_attempt, exc, state.request_observability)
                    continue
                hooks.emit_retry_suppressed(request.event_context, upstream_name=request.upstream_name, upstream_format=state.upstream_format, request_kind=attempt_request_kind, attempt=relay_attempt, max_attempts=relay_attempts, exc=exc, failure_class=hooks.failure_class(exc), failure_phase="response_headers", retry_safety_class=safety)
            raise
    raise RuntimeError("unreachable upstream protocol selection state")


# ---------------------------------------------------------------------------
# Handler-facing relay bindings. These build per-request RelayGlue /
# RelayContext objects and the downstream stream-commit seam, reading
# owning-module attributes at call time so test patches stay live.
# ---------------------------------------------------------------------------

import logging
from http.client import IncompleteRead
from urllib.error import URLError

import gateway_admission
import gateway_compat
import gateway_errors
import gateway_events
import gateway_sse
import gateway_stream_semantics
import gateway_transport
from gateway_errors import (
    CompactEmptyResponseError,
    UpstreamStreamIdleTimeoutError,
    responses_failed_event_for_stream_error as _responses_failed_event_for_stream_error,
    safe_upstream_error_detail,
)
from gateway_relay import RelayContext, RelayGlue
from gateway_request import (
    UNSET_CONTENT_ENCODING as _UNSET_CONTENT_ENCODING,
    filtered_response_headers as _filtered_response_headers,
    is_event_stream as _is_event_stream,
    decoded_request_body,
)
from gateway_sse import DownstreamStreamCommit
from gateway_stream_semantics import (
    DownstreamKeepaliveFailedError,
    UpstreamEmptyCompletedResponseError,
    UpstreamStreamErrorEvent,
    UpstreamStreamInterruptedError,
)
from gateway_transport import (
    UpstreamSseReaderLifecycle as _UpstreamSseReaderLifecycle,
    get_header as _get_header,
    retry_identity_from_context as _retry_identity_from_context,
)
from protocol_translation import (
    GatewayChatToResponsesStreamConverter as _ChatToResponsesStreamConverter,
    GatewayResponsesToChatStreamConverter as _ResponsesToChatStreamConverter,
    NonForwardable,
    UpstreamStreamIncompleteError,
)
from route_primitives import (
    RETRY_REQUEST_COMPACT,
    RETRY_SAFETY_SUPPRESSED_POST_EXPOSURE,
    RETRY_SAFETY_SUPPRESSED_POST_WRITE,
    StreamingPolicy,
    UsagePolicy,
)
from runtime_tool_compatibility import (
    ToolCompatibilityError as RuntimeToolCompatibilityError,
)
from sse_events import SseFrameTooLargeError

logger = logging.getLogger("codex_proxy")


def _observe_gateway_diagnostic(method: str, *args: Any, **kwargs: Any) -> None:
    gateway_events.observe_gateway_diagnostic(method, *args, **kwargs)


def _write_adapter_event(event_context: Any, event: str, **fields: Any) -> None:
    gateway_events.write_adapter_event(event_context, event, **fields)


def responses_synthetic_terminal_failure(
    handler: Any,
    exc: BaseException,
    *,
    status: int,
    response_id: str | None,
    upstream_name: str,
    model: str | None,
    redact_identity: str | None = None,
) -> tuple[bool, str | None, str | None]:
    """Write a Responses-format synthetic terminal failure event."""
    if not handler._write_sse_event(
        "response.failed",
        _responses_failed_event_for_stream_error(
            upstream_name=upstream_name,
            model=model,
            status=status,
            exc=exc,
            response_id=response_id,
            redact_identity=redact_identity,
        ),
    ):
        seam = _handler_downstream_stream_commit(handler)
        write_exc = seam.last_write_error() if seam is not None else None
        if write_exc is None:
            write_exc = OSError("downstream closed")
        return False, type(write_exc).__name__, safe_upstream_error_detail(write_exc, redact_identity=redact_identity)
    return True, None, None
_responses_synthetic_terminal_failure = responses_synthetic_terminal_failure


# Downstream writes that do not participate in the stream-commit seam.
# Non-streaming JSON responses, WebSocket handshakes/frames, and non-streaming
# body-relay writes are intentionally allowlisted: they either carry no SSE
# terminal semantics or are complete payloads whose lifecycle is bounded by the
# calling context. All production SSE headers/bodies must be authorized by the
# request-scoped stream-commit seam.
DOWNSTREAM_STREAM_COMMIT_ALLOWLIST: frozenset[str] = frozenset({
    "_send_json",
    "_safe_send_json",
    "_send_local_responses_no_content",
    "_send_method_not_allowed",
    "_reject_local_responses_websocket_probe",
    "_handle_websocket_recording_probe",
    "_write_non_streaming_body_relay",
    "_write_sse_bytes",
})

# Low-level seam methods that are the only legitimate direct ``self.wfile.write``
# callers inside the stream-commit seam. They are kept separate from the allowlist
# because they participate in (rather than bypass) the seam.
# ``write`` is _HandlerDownstreamIO.write, the handler-bound byte sink.
DOWNSTREAM_STREAM_COMMIT_SEAM_METHODS: frozenset[str] = frozenset({
    "commit_data",
    "commit_sse_bytes",
    "commit_terminal_failure",
    "write",
})


def handler_downstream_stream_commit(handler: Any) -> DownstreamStreamCommit | None:
    """Return the request-scoped stream-commit seam bound to ``handler`` if active."""
    seam = getattr(handler, "_downstream_stream_commit", None)
    return seam if isinstance(seam, DownstreamStreamCommit) else None
_handler_downstream_stream_commit = handler_downstream_stream_commit


def downstream_has_been_exposed(handler: Any) -> bool:
    """Return True if upstream response content has been exposed downstream.

    Exposure includes both bytes already written to the downstream socket and
    upstream content (visible/tool output) that the relay layer has accepted and
    would relay, even if it is still buffered.  Headers alone and metadata-only
    events such as ``response.created`` do not count as exposure.
    """
    seam = _handler_downstream_stream_commit(handler)
    if seam is None:
        return False
    return bool(
        getattr(seam, "_downstream_output_started", False)
        or getattr(seam, "_downstream_content_exposed", False)
    )
_downstream_has_been_exposed = downstream_has_been_exposed


class _HandlerDownstreamIO:
    """Adapter from CodexProxyHandler to DownstreamStreamCommit write/flush/close."""

    def __init__(self, handler: Any) -> None:
        self._handler = handler

    def write(self, data: bytes) -> None:
        self._handler.wfile.write(data)

    def flush(self) -> None:
        self._handler.wfile.flush()

    def close(self) -> None:
        self._handler.close_connection = True


def _bind_handler_synthetic_terminal_failure(
    handler: Any,
    callback: Callable[..., tuple[bool, str | None, str | None]] | None,
    *,
    redact_identity: str | None = None,
) -> Callable[..., tuple[bool, str | None, str | None]] | None:
    if callback is None:
        return None

    def bound(
        exc: BaseException,
        *,
        status: int,
        response_id: str | None,
        upstream_name: str,
        model: str | None,
    ) -> tuple[bool, str | None, str | None]:
        return callback(
            handler,
            exc,
            status=status,
            response_id=response_id,
            upstream_name=upstream_name,
            model=model,
            redact_identity=redact_identity,
        )

    return bound


def bind_downstream_stream_commit(
    handler: Any,
    upstream: Any | None,
    upstream_name: str,
    **kwargs: Any,
) -> DownstreamStreamCommit:
    redact_identity = kwargs.pop("redact_identity", None)
    kwargs.setdefault("usage_line_callback", gateway_events.offer_official_passthrough_usage_line)
    kwargs.setdefault("diagnostic_observer", _observe_gateway_diagnostic)
    kwargs.setdefault("terminal_observer", gateway_stream_semantics._responses_terminal_observer)
    kwargs.setdefault(
        "output_observer",
        lambda event: gateway_stream_semantics._responses_event_commits_downstream_output(event, ""),
    )
    kwargs.setdefault(
        "error_detail_callback",
        lambda exc: safe_upstream_error_detail(exc, redact_identity=redact_identity),
    )
    kwargs.setdefault(
        "terminal_drain_timeout_seconds",
        gateway_transport.OFFICIAL_TERMINAL_DRAIN_TIMEOUT_SECONDS,
    )
    if "synthetic_terminal_failure_callback" in kwargs:
        kwargs["synthetic_terminal_failure_callback"] = (
            _bind_handler_synthetic_terminal_failure(
                handler,
                kwargs["synthetic_terminal_failure_callback"],
                redact_identity=redact_identity,
            )
        )
    return DownstreamStreamCommit(
        _HandlerDownstreamIO(handler),
        upstream,
        upstream_name,
        **kwargs,
    )
_bind_downstream_stream_commit = bind_downstream_stream_commit


# Relay bindings are rebuilt per request so owning-module patches stay live.
def _relay_write_proxy_event(event: str, **fields: Any) -> None:
    gateway_events.write_proxy_event(event, **fields)


def _relay_compatible_sse_line(*args: Any, **kwargs: Any) -> Any:
    return gateway_compat.compatible_sse_line(*args, **kwargs)


def _relay_active_gateway_request() -> Any:
    return gateway_admission.active_gateway_request()


def _relay_glue() -> RelayGlue:
    return RelayGlue(
        _bind_downstream_stream_commit=_bind_downstream_stream_commit,
        _handler_downstream_stream_commit=_handler_downstream_stream_commit,
        _observe_gateway_diagnostic=_observe_gateway_diagnostic,
        _write_adapter_event=_write_adapter_event,
        _bind_handler_synthetic_terminal_failure=_bind_handler_synthetic_terminal_failure,
        _responses_synthetic_terminal_failure=_responses_synthetic_terminal_failure,
        compatible_sse_line=_relay_compatible_sse_line,
        write_proxy_event=_relay_write_proxy_event,
        _active_gateway_request=_relay_active_gateway_request,
        logger=logger,
    )


def relay_context_for_handler(handler: Any) -> RelayContext:
    return RelayContext(
        handler=handler,
        glue=_relay_glue(),
        transparent_relay=handler._relay_transparent_upstream_response,
        official_passthrough_relay=handler._relay_official_passthrough_sse_response,
        prepared_exchange=getattr(handler, "_active_prepared_exchange", None),
    )
_relay_context_for_handler = relay_context_for_handler
