from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
from types import SimpleNamespace

from gateway_exchange import (
    ExchangeDisposition,
    ExchangeEvent,
    ExchangePorts,
    ExchangeRequest,
    ExchangeProgress,
    ExchangeResult,
    ParsedInboundRequest,
    execute_exchange,
    terminal_result,
)
from gateway_exchange_ports import DownstreamAction, DownstreamState
from urllib.error import HTTPError

from protocol_translation import prepare_exchange
from route_primitives import MutationPolicy, RouteProtocol


class _NeverRaised(Exception):
    pass


def _http_error(status: int) -> HTTPError:
    return HTTPError("http://upstream", status, f"status {status}", {}, None)


def test_terminal_result_rejects_contradictory_states():
    progress = ExchangeProgress()
    assert terminal_result(ExchangeResult(ExchangeDisposition.COMPLETED, progress, status=200, stop_reason="bad")).error == "invalid_exchange_result"
    assert terminal_result(ExchangeResult(ExchangeDisposition.STOPPED, progress, status=500, stop_reason="downstream_closed")).error == "invalid_exchange_result"


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
        self.endpoint_url = "http://upstream/v1"
        self.request_headers = SimpleNamespace(to_dict=lambda: {})
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

    def telemetry_snapshot(self):
        return {
            "index": self.index,
            "request_body_mode": "passthrough",
            "request_conversion_steps": [],
            "mutation_summary": "",
        }


class _Plan:
    def __init__(self, *attempts: _Attempt):
        self.attempts = attempts
        self.primary_attempt = attempts[0]
        self.provider_id = "provider"
        self.configured_upstream_protocol_name = "responses"
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


def _make_ports(
    trace: list[str],
    seen: dict[str, object],
    *,
    open_outcomes: list[object] | None = None,
    failures: tuple[type[BaseException], ...] = (_NeverRaised,),
    exposed: bool = False,
    notify: bool = True,
) -> ExchangePorts:
    """Build scripted ports: transport with scripted open outcomes, downstream
    relay recording, fixed control (no wait), recording observer."""
    outcomes = iter(open_outcomes or [])

    @contextmanager
    def open_response(_opening):
        trace.append("open")
        outcome = next(outcomes, SimpleNamespace(status=200))
        if isinstance(outcome, BaseException):
            raise outcome
        yield outcome

    class _Transport:
        def open(self, opening):
            return open_response(opening)

    class _Downstream:
        def relay(self, response, relay_request):
            trace.append("relay")
            seen["relayed"] = response
            return 200

        def state(self):
            return DownstreamState(exposed=exposed, sse_started=exposed)

        def perform(self, action, **payload):
            if action is DownstreamAction.EMIT_RETRY_NOTICE:
                trace.append("notice")
                return notify
            if action is DownstreamAction.FINISH_FAILURE:
                trace.append("finish")
                return None
            if action is DownstreamAction.ATTACH_UPSTREAM:
                trace.append("attach")
                return None
            if action is DownstreamAction.SET_UPSTREAM_FORMAT:
                return None
            if action is DownstreamAction.HANDLE_EMPTY_COMPLETED:
                trace.append("empty")
                return True
            raise ValueError(action)

    class _Control:
        def now(self):
            return 0.0

        def wait(self, _seconds):
            trace.append("wait")

        def checkpoint(self):
            return None

    class _Observer:
        def __init__(self):
            self.events: list[ExchangeEvent] = []

        def record(self, event):
            self.events.append(event)

    observer = _Observer()
    seen["events"] = observer.events
    return ExchangePorts(
        transport=_Transport(),
        downstream=_Downstream(),
        control=_Control(),
        observer=observer,
    )


def _run(
    body: bytes,
    attempt: _Attempt,
    trace: list[str],
    *,
    attempts: tuple[_Attempt, ...] | None = None,
    open_outcomes: list[object] | None = None,
    failures: tuple[type[BaseException], ...] = (_NeverRaised,),
):
    parsed = _parsed(body, attempt._inbound)
    plan = _Plan(*(attempts or (attempt,)))
    seen: dict[str, object] = {}
    ports = _make_ports(
        trace, seen,
        open_outcomes=open_outcomes,
        failures=failures,
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
        ports,
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
    assert trace.count("open") == 1
    assert "relay" in trace


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
    events = seen["events"]
    assert any(e.kind == "request_start" for e in events)


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
    # prepare (inside body_for) happens before the first transport open,
    # then relay; ordering of side effects is preserved.
    assert trace[0] == "prepare"
    # open -> attach -> relay ordering inside the transport context
    assert "open" in trace
    assert trace[-2:] == ["attach", "relay"]


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
    result, seen = _run(
        body,
        first,
        trace,
        attempts=(first, second),
        open_outcomes=[_http_error(415), SimpleNamespace(status=200)],
    )
    assert result.status == 200
    assert first.prepared_bodies == [body]
    assert second.prepared_bodies == [body]
    assert trace.count("open") == 2
    events = seen["events"]
    assert any(e.kind == "protocol_fallback" for e in events)


def test_terminal_result_mapping_fails_closed_for_unknown_result() -> None:
    terminal = terminal_result(object())
    assert terminal.completed is False
    assert terminal.handled is False
    assert terminal.status == 500
    assert terminal.error == "invalid_exchange_result"
