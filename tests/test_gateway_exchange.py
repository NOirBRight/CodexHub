from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
from types import SimpleNamespace

from gateway_exchange import (
    ExchangeFailureTypes,
    ExchangeHooks,
    ExchangeRequest,
    ParsedInboundRequest,
    execute_exchange,
    terminal_result,
)
from protocol_translation import prepare_exchange
from route_primitives import MutationPolicy, RouteProtocol


class _NeverRaised(Exception):
    pass


class _FallbackError(Exception):
    def __init__(self, status: int):
        self.status = status
        super().__init__(f"fallback status {status}")


@dataclass
class _Retry:
    base_relay_attempts: int = 1
    emit_downstream_retry_notice: bool = False
    request_timeout_seconds: int = 10
    empty_completed_max_attempts: int = 2

    def new_open_attempt_budget(self):
        return None

    def lifecycle_final_extra_attempts(self, _context):
        return 0

    def relay_attempts_for_failure_class(self, **_kwargs):
        return 1


class _Attempt:
    def __init__(self, *, inbound: RouteProtocol, outbound: RouteProtocol, policy: MutationPolicy, trace: list[str], index: int = 0, fallback_statuses: frozenset[int] = frozenset()):
        self.index = index
        self.upstream_protocol = outbound
        self.selected_upstream_format = outbound.value
        self.request_mutation_policy = policy
        self.retry = _Retry()
        self.tool_protocol = "none"
        self.tool_surface_strategy = "eager"
        self.native_responses_tool_codec = "none"
        self.transport_policy = SimpleNamespace()
        self._inbound = inbound
        self._trace = trace
        self._fallback_statuses = fallback_statuses
        self.prepared_bodies: list[bytes] = []

    def prepare_body(self, body: bytes):
        self._trace.append("prepare")
        self.prepared_bodies.append(body)
        return prepare_exchange(
            body,
            inbound_format=self._inbound.value,
            outbound_format=self.upstream_protocol.value,
        )

    def relay_execution_plan(self, *, lifecycle_final_retry_enabled: bool):
        return SimpleNamespace(lifecycle_final_retry_enabled=lifecycle_final_retry_enabled)

    def allows_protocol_fallback_status(self, status):
        return status in self._fallback_statuses


class _Plan:
    def __init__(self, *attempts: _Attempt):
        self.attempts = attempts
        self.primary_attempt = attempts[0]
        self.provider_id = "provider"
        self.transparent_tool_loop_guard = False
        self.tool_exposure = SimpleNamespace(gateway_schema_injection=False)


def _parsed(body: bytes, protocol: RouteProtocol) -> ParsedInboundRequest:
    payload = json.loads(body)
    return ParsedInboundRequest(
        request_id="req-1",
        started_at=0.0,
        path="/v1/responses",
        protocol=protocol,
        provider_hint=None,
        headers={},
        request_context={},
        proxy_request_context={},
        raw_provider_probe=False,
        content_length=len(body),
        content_type="application/json",
        content_encoding=None,
        content_decoded=False,
        body=body,
        inbound_payload=payload,
        request_kind="gateway",
        model_requested=payload.get("model"),
        model=payload.get("model"),
        route_reason="model",
    )


def _run(
    body: bytes,
    attempt: _Attempt,
    trace: list[str],
    *,
    attempts: tuple[_Attempt, ...] | None = None,
    open_outcomes: list[object] | None = None,
):
    parsed = _parsed(body, attempt._inbound)
    plan = _Plan(*(attempts or (attempt,)))
    seen: dict[str, object] = {}
    outcomes = iter(open_outcomes or [])

    @contextmanager
    def open_response(_opening):
        trace.append("open")
        outcome = next(outcomes, SimpleNamespace(status=200))
        if isinstance(outcome, BaseException):
            raise outcome
        yield outcome

    def official_mutation(candidate, _payload, _upstream, *, model_id):
        trace.append("mutate")
        seen["mutated"] = candidate
        return candidate

    def transparent_mutation(candidate, _payload, _upstream, *, model_id):
        trace.append("mutate")
        seen["mutated"] = candidate
        return candidate

    def build_request(_attempt, candidate):
        trace.append("build")
        seen["built"] = candidate
        return SimpleNamespace(headers={})

    def relay_response(_response, _relay):
        trace.append("relay")
        return 200

    failures = ExchangeFailureTypes(
        downstream_closed_before_retry=_NeverRaised,
        incomplete_read=_NeverRaised,
        protocol_fallback_error=_FallbackError,
        compact_empty=_NeverRaised,
        stream_interrupted=_NeverRaised,
        stream_idle_timeout=_NeverRaised,
        stream_incomplete=_NeverRaised,
        stream_error_event=_NeverRaised,
        lifecycle_empty_final=_NeverRaised,
        lifecycle_final_format=_NeverRaised,
        upstream_empty_completed=_NeverRaised,
    )
    hooks = ExchangeHooks(
        failure_types=failures,
        set_active_prepared_exchange=lambda exchange: seen.update(exchange=exchange),
        activate_attempt=lambda _attempt: trace.append("activate"),
        safe_json_mapping=lambda candidate: json.loads(candidate),
        official_mutation=official_mutation,
        transparent_mutation=transparent_mutation,
        rewrite_developer_roles=lambda candidate, _upstream: (candidate, 0),
        normalize_tool_schema_booleans=lambda candidate: (candidate, 0),
        validate_transparent_tool_loop=lambda _candidate, _format: None,
        compatibility_mutation=lambda candidate, _upstream, **_kwargs: candidate,
        request_observability=lambda _attempt, _candidate: {},
        emit_request_start=lambda _fields: None,
        build_request=build_request,
        lifecycle_guidance=lambda candidate, _reason: candidate,
        open_response=open_response,
        relay_response=relay_response,
        set_upstream_format=lambda _format: None,
        attach_upstream=lambda _response: None,
        downstream_exposed=lambda: False,
        raise_if_cancelled=lambda: None,
        emit_downstream_retry=lambda _payload: True,
        finish_downstream_failure=lambda: None,
        failure_class=lambda _exc: "permanent",
        retry_safety_class=lambda _exc, **_kwargs: "safe_prewrite",
        model_access_path=lambda _context, _name, _format: "test",
        retry_after_seconds=lambda _exc: None,
        emit_retry=lambda *_args, **_kwargs: None,
        emit_retry_suppressed=lambda *_args, **_kwargs: None,
        downstream_retry_payload=lambda **_kwargs: {},
        retry_identity=lambda _context: None,
        sleep=lambda _delay: None,
        protocol_fallback=lambda *_args: trace.append("fallback"),
        error_status=lambda exc: exc.status if isinstance(exc, _FallbackError) else None,
        handle_empty_completed=lambda _exc: True,
        monotonic=lambda: 0.0,
    )
    result = execute_exchange(
        ExchangeRequest(
            inbound=parsed,
            route_plan=plan,
            upstream={"name": "provider"},
            upstream_name="provider",
            prepared_body=body,
            inbound_payload=parsed.inbound_payload,
            model_canonical="model",
            caller_stream=False,
            prompt_cache_key=None,
            caller_request_observability={},
            event_context={},
            proxy_request_context={},
            usage_capture={},
            response_lifecycle_state={},
            pre_response_deadline=None,
        ),
        hooks,
    )
    return result, seen


def test_execute_exchange_same_protocol_preserves_body_identity() -> None:
    body = json.dumps({"model": "model", "input": "hello"}).encode()
    trace: list[str] = []
    attempt = _Attempt(
        inbound=RouteProtocol.RESPONSES,
        outbound=RouteProtocol.RESPONSES,
        policy=MutationPolicy.OFFICIAL_PASSTHROUGH,
        trace=trace,
    )
    result, seen = _run(body, attempt, trace)
    assert result.status == 200
    assert seen["mutated"] is body
    assert seen["built"] is body


def test_execute_exchange_prepares_one_hop_attempt() -> None:
    body = json.dumps({
        "model": "model",
        "messages": [{"role": "user", "content": "hello"}],
    }).encode()
    trace: list[str] = []
    attempt = _Attempt(
        inbound=RouteProtocol.CHAT_COMPLETIONS,
        outbound=RouteProtocol.RESPONSES,
        policy=MutationPolicy.TRANSPARENT,
        trace=trace,
    )
    _result, seen = _run(body, attempt, trace)
    assert attempt.prepared_bodies == [body]
    converted = json.loads(seen["built"])
    assert converted["input"][0]["role"] == "user"
    assert "messages" not in converted


def test_execute_exchange_orders_prepare_before_mutation_and_transport() -> None:
    body = json.dumps({"model": "model", "input": "hello"}).encode()
    trace: list[str] = []
    attempt = _Attempt(
        inbound=RouteProtocol.RESPONSES,
        outbound=RouteProtocol.RESPONSES,
        policy=MutationPolicy.OFFICIAL_PASSTHROUGH,
        trace=trace,
    )
    _run(body, attempt, trace)
    assert trace == ["prepare", "mutate", "activate", "build", "open", "relay"]


def test_execute_exchange_falls_back_with_typed_transport_error() -> None:
    body = json.dumps({"model": "model", "input": "hello"}).encode()
    trace: list[str] = []
    first = _Attempt(
        inbound=RouteProtocol.RESPONSES,
        outbound=RouteProtocol.RESPONSES,
        policy=MutationPolicy.OFFICIAL_PASSTHROUGH,
        trace=trace,
        index=0,
        fallback_statuses=frozenset({415}),
    )
    second = _Attempt(
        inbound=RouteProtocol.RESPONSES,
        outbound=RouteProtocol.RESPONSES,
        policy=MutationPolicy.OFFICIAL_PASSTHROUGH,
        trace=trace,
        index=1,
    )
    result, _seen = _run(
        body,
        first,
        trace,
        attempts=(first, second),
        open_outcomes=[_FallbackError(415), SimpleNamespace(status=200)],
    )
    assert result.status == 200
    assert first.prepared_bodies == [body]
    assert second.prepared_bodies == [body]
    assert "fallback" in trace
    assert trace.count("open") == 2


def test_terminal_result_mapping_fails_closed_for_unknown_result() -> None:
    terminal = terminal_result(object())
    assert terminal.completed is False
    assert terminal.handled is False
    assert terminal.status == 500
    assert terminal.error == "invalid_exchange_result"
