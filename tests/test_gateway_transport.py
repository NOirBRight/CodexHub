"""Direct seam tests for Gateway upstream transport."""

from __future__ import annotations

import inspect
import ssl
from email.utils import formatdate
from io import BytesIO
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest

import gateway_transport
from gateway_errors import GatewayPreResponseBudgetExhausted
from gateway_transport import (
    GatewayTransport,
    TransportFacts,
    UpstreamSseReaderLifecycle,
    _retry_after_delay_seconds,
    _upstream_failure_class,
    build_upstream_headers,
    materialize_operational_authentication,
)
from route_primitives import (
    RETRY_FAILURE_PERMANENT,
    RETRY_FAILURE_PROVIDER_OVERLOADED,
    RETRY_FAILURE_PROVIDER_THROTTLE,
    RETRY_FAILURE_QUICK_TRANSIENT,
    AuthenticationStrategy,
    TransportPolicy,
)


FORBIDDEN_SOURCE_MARKERS = (
    "import codex_proxy",
    "from codex_proxy",
    "gateway_sse",
    "BaseHTTPRequestHandler",
    "CodexProxyHandler",
)


class _FakeResponse:
    def __init__(self, body: bytes = b"ok", status: int = 200, headers: dict | None = None):
        self.status = status
        self.code = status
        self.reason = "OK"
        self.headers = headers or {"Content-Type": "application/json"}
        self._body = body
        self.closed = False
        self.connection_disposition = "new"

    def read(self, amount: int | None = None) -> bytes:
        return self._body if amount is None else self._body[:amount]

    def readline(self, limit: int = -1) -> bytes:
        return b""

    def close(self) -> None:
        self.closed = True


class _FakeHTTPError(HTTPError):
    def __init__(self, status: int, headers: dict | None = None, body: bytes = b""):
        super().__init__(
            "https://example.test/v1",
            status,
            "error",
            headers or {},
            BytesIO(body),
        )


def test_gateway_transport_source_does_not_import_facade_or_sse() -> None:
    source = Path(gateway_transport.__file__).read_text(encoding="utf-8")
    for marker in FORBIDDEN_SOURCE_MARKERS:
        assert marker not in source
    assert "class GatewayTransport" in source
    assert "class TransportFacts" in source


def test_transport_failure_phase_classifies_ssl_eof() -> None:
    error = ssl.SSLEOFError("EOF occurred in violation of protocol")
    assert gateway_transport.transport_failure_phase(error) == "tls_handshake"


def test_typed_transport_seam_is_gateway_transport() -> None:
    assert inspect.isclass(GatewayTransport)
    assert inspect.isclass(TransportFacts)
    annotations = GatewayTransport.__annotations__
    assert "facts" in annotations
    assert "official_open" in annotations
    assert "standard_open" in annotations
    assert "open_once_hook" in annotations


def test_open_once_uses_official_policy_not_string_dispatch() -> None:
    seen: list[str] = []

    def official_open(request: Request, *, timeout: float) -> _FakeResponse:
        seen.append("official")
        return _FakeResponse()

    def standard_open(request: Request, *, timeout: float) -> _FakeResponse:
        seen.append("standard")
        return _FakeResponse()

    transport = GatewayTransport(official_open=official_open, standard_open=standard_open)
    request = Request("https://example.test/v1/responses", data=b"{}", method="POST")
    transport.open_once(
        request,
        upstream_name="official",
        timeout=5,
        transport_policy=TransportPolicy.OFFICIAL_KEEPALIVE,
    )
    transport.open_once(
        request,
        upstream_name="volcengine",
        timeout=5,
        transport_policy=TransportPolicy.STANDARD,
    )
    assert seen == ["official", "standard"]


def test_open_response_retries_scripted_http_error_and_honors_retry_after() -> None:
    calls: list[int] = []
    sleeps: list[float] = []

    def official_open(request: Request, *, timeout: float) -> _FakeResponse:
        calls.append(1)
        if len(calls) == 1:
            raise _FakeHTTPError(429, {"Retry-After": "3"})
        return _FakeResponse(b'{"id":"ok"}')

    transport = GatewayTransport(
        official_open=official_open,
        standard_open=official_open,
        sleep=sleeps.append,
    )
    response = transport.open_response(
        Request("https://example.test/v1/responses", data=b"{}", method="POST"),
        upstream_name="official",
        upstream_format="responses",
        timeout=5,
        max_attempts=3,
        retry_http_errors=True,
        transport_policy=TransportPolicy.OFFICIAL_KEEPALIVE,
    )
    assert response.read() == b'{"id":"ok"}'
    assert len(calls) == 2
    assert sleeps == [3]


def test_open_response_classifies_503_as_overloaded_and_permanent_400() -> None:
    assert _upstream_failure_class(_FakeHTTPError(503)) == RETRY_FAILURE_PROVIDER_OVERLOADED
    assert _upstream_failure_class(_FakeHTTPError(400)) == RETRY_FAILURE_PERMANENT
    assert _upstream_failure_class(_FakeHTTPError(429)) == RETRY_FAILURE_PROVIDER_THROTTLE
    assert _upstream_failure_class(URLError(TimeoutError("timed out"))) == RETRY_FAILURE_QUICK_TRANSIENT


def test_retry_after_supports_delta_seconds_and_http_date() -> None:
    assert _retry_after_delay_seconds(_FakeHTTPError(429, {"Retry-After": "2.2"})) == 3
    http_date = formatdate(timeval=None, usegmt=True)
    delay = _retry_after_delay_seconds(_FakeHTTPError(429, {"Retry-After": http_date}))
    assert delay is not None
    assert delay >= 0
    assert _retry_after_delay_seconds(URLError("nope")) is None


def test_pre_response_budget_raises_before_open() -> None:
    transport = GatewayTransport(
        official_open=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("opened")),
        standard_open=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("opened")),
    )
    with pytest.raises(GatewayPreResponseBudgetExhausted):
        transport.open_response(
            Request("https://example.test/v1/responses", data=b"{}", method="POST"),
            upstream_name="official",
            upstream_format="responses",
            timeout=5,
            pre_response_deadline=0.0,
            transport_policy=TransportPolicy.OFFICIAL_KEEPALIVE,
        )


def test_header_and_auth_materialization_uses_injected_tokens() -> None:
    transport = GatewayTransport(
        access_token=lambda: "injected-token",
        account_id=lambda: "acct-injected",
        new_id=lambda: "fixed-id",
    )
    auth = transport.materialize_authentication(
        {"Content-Type": "application/json"},
        {"auth": "codex_auth", "name": "official"},
    )
    assert auth.strategy == AuthenticationStrategy.CODEX_AUTH
    assert auth.authorization == "Bearer injected-token"
    assert auth.account_id == "acct-injected"
    headers = transport.build_headers(
        {"Content-Type": "application/json"},
        {"auth": "codex_auth", "name": "official", "upstream_model": "gpt-5.4"},
        operational_authentication=auth,
    )
    assert headers["Authorization"] == "Bearer injected-token"
    assert headers["Chatgpt-account-id"] == "acct-injected"
    assert headers["Session-id"] == "fixed-id"
    assert "x-openai-internal-codex-responses-lite" not in {
        key.lower() for key in headers
    }


def test_module_level_header_helpers_match_injected_adapter() -> None:
    headers = build_upstream_headers(
        {"Authorization": "Bearer caller", "Content-Type": "application/json"},
        {"auth": "incoming", "name": "local"},
        access_token=lambda: "unused",
        account_id=lambda: None,
    )
    assert headers["Authorization"] == "Bearer caller"
    auth = materialize_operational_authentication(
        {"Authorization": "Bearer caller"},
        {"auth": "incoming"},
    )
    assert auth.authorization == "Bearer caller"


def test_subscription_registry_dispatches_without_transport_auth_branch() -> None:
    from subscription_credential import register, unregister

    class _Hypothetical:
        def access_token(self) -> str:
            return "hypothetical-token"

        def account_headers(self) -> dict[str, str]:
            return {"X-Account": "acct-h"}

        def refresh(self) -> str:
            return "hypothetical-token"

    unregister("hypothetical_oauth")
    register("hypothetical_oauth", _Hypothetical())
    try:
        headers = build_upstream_headers(
            {"Content-Type": "application/json"},
            {"auth": "hypothetical_oauth", "name": "hypo"},
        )
        assert headers["Authorization"] == "Bearer hypothetical-token"
        assert headers["X-Account"] == "acct-h"
        assert "Session-id" not in headers
        assert "Chatgpt-account-id" not in headers
    finally:
        unregister("hypothetical_oauth")


def test_upstream_sse_reader_lifecycle_with_scripted_response() -> None:
    class Scripted:
        def __init__(self) -> None:
            self.lines = [b"data: 1\n", b""]
            self.closed = False

        def readline(self, limit: int = -1) -> bytes:
            return self.lines.pop(0) if self.lines else b""

        def close(self) -> None:
            self.closed = True

    response = Scripted()
    lifecycle = UpstreamSseReaderLifecycle(response, thread_name="test-reader")
    assert list(lifecycle.iter_lines()) == [b"data: 1\n", b""]
    assert response.closed
    joined, outcome = lifecycle.join()
    assert joined is True
    assert outcome == "upstream_sse_reader_thread_terminated"


def test_transport_build_request_materializes_endpoint_url():
    transport = GatewayTransport(
        endpoint_url_hook=lambda upstream, path: "https://example.test" + path,
    )
    request = transport.build_request(
        {"name": "third_party"},
        "/v1/responses",
        data=b"{}",
        headers={"Content-Type": "application/json"},
    )
    assert request.full_url == "https://example.test/v1/responses"
    assert request.data == b"{}"
    assert request.get_header("Content-type") == "application/json"


def test_transport_build_request_merges_endpoint_query_parameters():
    transport = GatewayTransport(
        endpoint_url_hook=lambda upstream, path: "https://example.test" + path + "?api-version=1",
    )
    request = transport.build_request({}, "/responses?trace=1")
    assert request.full_url == "https://example.test/responses?api-version=1&trace=1"


def test_open_once_hook_is_used_by_open_response() -> None:
    seen: list[str] = []

    def fake_once(request: Request, *, upstream_name: str, timeout: float, transport_policy=None):
        seen.append(upstream_name)
        return _FakeResponse()

    transport = GatewayTransport(
        official_open=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("official")),
        open_once_hook=fake_once,
    )
    transport.open_response(
        Request("https://example.test/v1/responses", data=b"{}", method="POST"),
        upstream_name="official",
        upstream_format="responses",
        timeout=1,
        max_attempts=1,
    )
    assert seen == ["official"]
