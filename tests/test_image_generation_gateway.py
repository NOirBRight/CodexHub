from __future__ import annotations

import http.client
import json
import socket
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Iterator
from unittest.mock import patch

import codex_proxy
import gateway_catalog_runtime
import pytest
import gateway_transport
import codex_auth
import gateway_admission
import gateway_settings


class _ImageUpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
        content_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(content_length)
        self.server.captures.append(  # type: ignore[attr-defined]
            {
                "path": self.path,
                "headers": {key.lower(): value for key, value in self.headers.items()},
                "body": body,
            }
        )
        response_body = self.server.response_body  # type: ignore[attr-defined]
        self.send_response(self.server.response_status)  # type: ignore[attr-defined]
        self.send_header(
            "Content-Type",
            self.server.response_content_type,  # type: ignore[attr-defined]
        )
        self.send_header("Content-Length", str(len(response_body)))
        self.send_header("X-Image-Fixture", "preserved")
        self.end_headers()
        self.wfile.write(response_body)

    def log_message(self, format: str, *args: object) -> None:
        return


class _CancellationResponse:
    status = 200
    headers = {
        "Content-Type": "application/json",
        "Content-Length": "30",
    }

    def __init__(self, read_outcome: str) -> None:
        self.read_outcome = read_outcome
        self.read_started = threading.Event()
        self.closed = threading.Event()

    def __enter__(self) -> "_CancellationResponse":
        admission = gateway_admission.active_gateway_request()
        assert admission is not None
        admission.attach_upstream_transport(self)
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        self.close()
        return False

    def read(self) -> bytes:
        self.read_started.set()
        assert self.closed.wait(timeout=2)
        if self.read_outcome == "incomplete":
            raise http.client.IncompleteRead(b'{"partial":', 20)
        if self.read_outcome == "oserror":
            raise OSError("fixture cancellation closed upstream")
        if self.read_outcome == "generic":
            raise RuntimeError("fixture cancellation closed upstream")
        return b'{"partial":"must-not-relay"}'

    def close(self) -> None:
        self.closed.set()


@contextmanager
def _http_server(
    handler: type[BaseHTTPRequestHandler],
) -> Iterator[ThreadingHTTPServer]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    server.gateway_shutdown_controller = gateway_admission.GatewayShutdownController()  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _request_image_generation(
    gateway: ThreadingHTTPServer,
    body: bytes,
) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection(
        "127.0.0.1",
        gateway.server_port,
        timeout=3,
    )
    connection.request(
        "POST",
        "/v1/images/generations",
        body=body,
        headers={
            "Authorization": "Bearer local-client-key",
            "Content-Type": "application/json; charset=utf-8",
            "Originator": "codex-cli",
            "X-Codex-Image-Turn-Id": "fixture-turn-id",
            "X-Image-Fixture": "request-preserved",
            "Connection": "close",
        },
    )
    response = connection.getresponse()
    status = response.status
    headers = {key.lower(): value for key, value in response.getheaders()}
    response_body = response.read()
    connection.close()
    return status, headers, response_body


@pytest.mark.parametrize(
    ("upstream_status", "upstream_content_type", "upstream_body"),
    [
        (
            200,
            "application/json; charset=utf-8",
            b'{"created":123,"data":[{"b64_json":"AAECAw=="}],"size":"1024x1024"}',
        ),
        (
            429,
            "application/problem+json",
            b'{ "opaque_error" : { "code" : "fixture_limit" } }',
        ),
    ],
)
def test_image_generation_relays_official_raw_contract(
    upstream_status: int,
    upstream_content_type: str,
    upstream_body: bytes,
) -> None:
    request_body = (
        b'{ "prompt" : "non-secret-image-fixture", "model" : "gpt-image-2", '
        b'"background" : "opaque", "quality" : "high", "size" : "1024x1024" }'
    )
    with _http_server(_ImageUpstreamHandler) as upstream:
        upstream.captures = []  # type: ignore[attr-defined]
        upstream.response_status = upstream_status  # type: ignore[attr-defined]
        upstream.response_content_type = upstream_content_type  # type: ignore[attr-defined]
        upstream.response_body = upstream_body  # type: ignore[attr-defined]
        controlled_base = f"http://127.0.0.1:{upstream.server_port}/custom/v1"
        with (
            patch.object(gateway_catalog_runtime, "official_base_url", return_value=controlled_base),
            patch.object(gateway_settings, "gateway_client_key", return_value="local-client-key"),
            patch.object(codex_auth, "access_token", return_value="synthetic-official-token"),
            patch.object(codex_auth, "account_id", return_value="synthetic-account-id"),
            patch.object(gateway_transport, "OFFICIAL_HTTP_POOLS", {}),
            _http_server(codex_proxy.CodexProxyHandler) as gateway,
        ):
            status, headers, response_body = _request_image_generation(gateway, request_body)

    assert status == upstream_status
    assert headers["content-type"] == upstream_content_type
    assert headers["x-image-fixture"] == "preserved"
    assert response_body == upstream_body
    assert len(upstream.captures) == 1  # type: ignore[attr-defined]
    captured = upstream.captures[0]  # type: ignore[attr-defined]
    assert captured["path"] == "/custom/v1/images/generations"
    assert captured["body"] == request_body
    assert captured["headers"]["authorization"] == "Bearer synthetic-official-token"
    assert captured["headers"]["chatgpt-account-id"] == "synthetic-account-id"
    assert captured["headers"]["content-type"] == "application/json; charset=utf-8"
    assert captured["headers"]["originator"] == "codex-cli"
    assert captured["headers"]["x-codex-image-turn-id"] == "fixture-turn-id"
    assert captured["headers"]["x-image-fixture"] == "request-preserved"
    assert "local-client-key" not in str(captured)
    assert "session-id" not in captured["headers"]
    assert "x-client-request-id" not in captured["headers"]


@pytest.mark.parametrize("read_outcome", ["partial", "incomplete", "oserror", "generic"])
def test_image_generation_cancellation_during_upstream_body_read_uses_shutdown_outcome(
    read_outcome: str,
) -> None:
    upstream_response = _CancellationResponse(read_outcome)
    result: dict[str, object] = {}

    with (
        patch.object(
            gateway_catalog_runtime,
            "official_upstream",
            return_value={
                "name": "official",
                "base_url": "https://controlled.invalid/v1",
                "auth": "codex_auth",
            },
        ),
        patch.object(gateway_settings, "gateway_client_key", return_value="local-client-key"),
        patch.object(codex_auth, "access_token", return_value="synthetic-official-token"),
        patch.object(codex_auth, "account_id", return_value="synthetic-account-id"),
        patch.object(gateway_transport, "open_upstream_response", return_value=upstream_response),
        _http_server(codex_proxy.CodexProxyHandler) as gateway,
    ):
        request_thread = threading.Thread(
            target=lambda: result.update(
                zip(
                    ("status", "headers", "body"),
                    _request_image_generation(gateway, b'{"fixture":"cancel"}'),
                )
            ),
            daemon=True,
        )
        request_thread.start()
        assert upstream_response.read_started.wait(timeout=2)
        gateway.gateway_shutdown_controller.close_admission()  # type: ignore[attr-defined]
        request_thread.join(timeout=3)

    assert request_thread.is_alive() is False
    assert result["status"] == 503
    assert result["headers"]["connection"] == "close"  # type: ignore[index]
    payload = json.loads(result["body"])  # type: ignore[arg-type]
    assert payload["type"] == gateway_admission.USER_REQUESTED_SHUTDOWN_OUTCOME
    assert payload["error"] == gateway_admission.USER_REQUESTED_SHUTDOWN_OUTCOME
    assert b"must-not-relay" not in result["body"]  # type: ignore[operator]


def test_image_generation_official_lookup_failure_completes_admission_and_returns_error() -> None:
    with (
        patch.object(gateway_settings, "gateway_client_key", return_value="local-client-key"),
        patch.object(
            gateway_catalog_runtime,
            "official_upstream",
            side_effect=RuntimeError("controlled routing config failure"),
        ),
        _http_server(codex_proxy.CodexProxyHandler) as gateway,
    ):
        status, headers, body = _request_image_generation(
            gateway,
            b'{"fixture":"lookup-failure"}',
        )
        admission_drained = gateway.gateway_shutdown_controller.wait_for_active_requests()  # type: ignore[attr-defined]

    assert status == 500
    assert headers["connection"] == "close"
    payload = json.loads(body)
    assert payload["error"] == "RuntimeError"
    assert "controlled routing config failure" in payload["detail"]
    assert admission_drained is True


def test_unsupported_keepalive_post_returns_one_404_closes_and_does_not_log_body() -> None:
    sentinel = b"NON_SECRET_REJECTED_BODY_SENTINEL_401_402"
    body = b'{"fixture":"' + sentinel + b'"}\r\n'
    request = (
        b"POST /v1/unsupported-fixture HTTP/1.1\r\n"
        b"Host: 127.0.0.1\r\n"
        b"Content-Type: application/json\r\n"
        + f"Content-Length: {len(body)}\r\n".encode("ascii")
        + b"Connection: keep-alive\r\n\r\n"
        + body
    )

    with (
        patch.object(codex_proxy.logger, "info") as info_log,
        patch.object(codex_proxy.logger, "error") as error_log,
        _http_server(codex_proxy.CodexProxyHandler) as gateway,
    ):
        client = socket.create_connection(("127.0.0.1", gateway.server_port), timeout=2)
        client.settimeout(2)
        client.sendall(request)
        chunks: list[bytes] = []
        socket_closed = False
        try:
            while True:
                chunk = client.recv(65536)
                if not chunk:
                    socket_closed = True
                    break
                chunks.append(chunk)
        finally:
            client.close()

    response = b"".join(chunks)
    logged = repr(info_log.call_args_list) + repr(error_log.call_args_list)
    assert response.count(b"HTTP/1.1 ") == 1
    assert response.startswith(b"HTTP/1.1 404 ")
    assert b"\r\nConnection: close\r\n" in response
    assert sentinel not in response
    assert sentinel.decode("ascii") not in logged
    assert socket_closed is True
