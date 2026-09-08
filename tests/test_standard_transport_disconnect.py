"""Loopback tests for third-party disconnect phase tagging and retry suppression.

These tests drive the production standard opener against a local HTTP server.
They capture the original xAI long-request symptoms at the Gateway seam:
BrokenPipe during body write, RemoteDisconnected after a full upload, inner
retry suppression, and a successful tool-bearing JSON response.
"""

from __future__ import annotations

import json
import socket
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.error import URLError
from urllib.request import Request

import gateway_events
import gateway_transport
from route_primitives import TransportPolicy


def _start_stub(handler: type[BaseHTTPRequestHandler]) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    server.thread = thread  # type: ignore[attr-defined]
    return server


def _stop_stub(server: ThreadingHTTPServer) -> None:
    server.shutdown()
    server.server_close()
    thread = getattr(server, "thread", None)
    if thread is not None:
        thread.join(timeout=2)


class _CloseAfterBodyHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        started = time.monotonic()
        body = self.rfile.read(length) if length else b""
        self.server.captures.append(  # type: ignore[attr-defined]
            {
                "bytes_read": len(body),
                "header_complete_ms": int((time.monotonic() - started) * 1000),
                "mode": "close_after_body",
            }
        )
        self.close_connection = True


class _SuccessHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length) if length else b""
        self.server.captures.append(  # type: ignore[attr-defined]
            {"bytes_read": len(body), "mode": "success"}
        )
        payload = json.dumps(
            {
                "id": "resp_loopback",
                "output": [
                    {
                        "type": "function_call",
                        "name": "exec_command",
                        "arguments": '{"cmd":"echo ok"}',
                    }
                ],
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


def _open_xai(url: str, body: bytes, **kwargs: Any) -> Any:
    request = Request(url, data=body, method="POST")
    request.add_header("Content-Type", "application/json")
    with (
        patch.object(gateway_transport, "STANDARD_HTTP_POOLS", {}),
        patch("gateway_transport.getproxies", return_value={"no": "localhost,127.0.0.1"}),
    ):
        return gateway_transport.open_upstream_response(
            request,
            upstream_name="xai",
            upstream_format="responses",
            timeout=kwargs.get("timeout", 2),
            max_attempts=kwargs.get("max_attempts", 5),
            transport_policy=TransportPolicy.STANDARD,
            event_context={
                "request_id": kwargs.get("request_id", "loopback-xai"),
                "model": "xai/grok-4.6",
            },
        )


def test_close_after_full_upload_is_response_headers_and_not_retried() -> None:
    server = _start_stub(_CloseAfterBodyHandler)
    server.captures = []  # type: ignore[attr-defined]
    body = b'{"input":"' + b"x" * 65536 + b'"}'
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict("os.environ", {"CODEX_HOME": str(Path(tmpdir) / "home")}):
                gateway_events.refresh_runtime_paths()
                with patch.dict(
                    "os.environ",
                    {
                        "CODEX_PROXY_AUTO_RETRY_ENABLED": "1",
                        "CODEX_PROXY_AUTO_RETRY_MAX_ATTEMPTS": "5",
                    },
                ):
                    try:
                        _open_xai(
                            f"http://127.0.0.1:{server.server_address[1]}/v1/responses",
                            body,
                            request_id="loopback-after-body",
                        )
                    except (OSError, URLError) as exc:
                        raised = exc
                    else:
                        raise AssertionError("expected disconnect after full upload")
                gateway_events.flush_proxy_event_writer()
                payloads = [
                    json.loads(line)
                    for line in gateway_events.PROXY_EVENT_LOG_PATH.read_text(
                        encoding="utf-8"
                    ).splitlines()
                    if line.strip()
                ]
    finally:
        _stop_stub(server)
        gateway_events.refresh_runtime_paths()

    assert gateway_transport.transport_failure_phase(raised) == "response_headers"
    assert gateway_transport.retry_safety_failure_phase(raised) == "response_headers"
    assert len(server.captures) == 1
    assert server.captures[0]["bytes_read"] == len(body)
    suppressed = [item for item in payloads if item.get("event") == "upstream_retry_suppressed"]
    retries = [item for item in payloads if item.get("event") == "upstream_retry"]
    assert len(suppressed) == 1
    assert suppressed[0]["retry_forbidden"] is True
    assert suppressed[0]["retry_safety_class"] == "suppressed_post_write"
    assert suppressed[0]["failure_phase"] == "response_headers"
    assert retries == []


def _start_close_after_headers() -> tuple[socket.socket, int, threading.Event, threading.Thread]:
    accepted = threading.Event()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    port = listener.getsockname()[1]

    def _serve() -> None:
        connection, _ = listener.accept()
        try:
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = connection.recv(1024)
                if not chunk:
                    break
                data += chunk
            accepted.set()
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        finally:
            connection.close()

    thread = threading.Thread(target=_serve, daemon=True)
    thread.start()
    return listener, port, accepted, thread


def test_close_mid_body_is_request_write_and_not_retried() -> None:
    listener, port, accepted, thread = _start_close_after_headers()
    body = b"x" * (8 * 1024 * 1024)
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.dict("os.environ", {"CODEX_HOME": str(Path(tmpdir) / "home")}):
                gateway_events.refresh_runtime_paths()
                with patch.dict(
                    "os.environ",
                    {
                        "CODEX_PROXY_AUTO_RETRY_ENABLED": "1",
                        "CODEX_PROXY_AUTO_RETRY_MAX_ATTEMPTS": "5",
                    },
                ):
                    try:
                        _open_xai(
                            f"http://127.0.0.1:{port}/v1/responses",
                            body,
                            request_id="loopback-mid-body",
                        )
                    except (OSError, URLError) as exc:
                        raised = exc
                    else:
                        raise AssertionError("expected disconnect during body write")
                gateway_events.flush_proxy_event_writer()
                payloads = [
                    json.loads(line)
                    for line in gateway_events.PROXY_EVENT_LOG_PATH.read_text(
                        encoding="utf-8"
                    ).splitlines()
                    if line.strip()
                ]
    finally:
        listener.close()
        thread.join(timeout=2)
        gateway_events.refresh_runtime_paths()

    assert accepted.is_set()
    assert gateway_transport.transport_failure_phase(raised) == "request_write"
    assert gateway_transport.retry_safety_failure_phase(raised) == "request_write"
    suppressed = [item for item in payloads if item.get("event") == "upstream_retry_suppressed"]
    retries = [item for item in payloads if item.get("event") == "upstream_retry"]
    assert len(suppressed) == 1
    assert suppressed[0]["retry_forbidden"] is True
    assert suppressed[0]["retry_safety_class"] == "suppressed_post_write"
    assert retries == []


def test_standard_success_returns_function_call() -> None:
    server = _start_stub(_SuccessHandler)
    server.captures = []  # type: ignore[attr-defined]
    body = b'{"model":"grok-4.6","input":[{"role":"user","content":"run"}]}'
    try:
        response = _open_xai(
            f"http://127.0.0.1:{server.server_address[1]}/v1/responses",
            body,
            max_attempts=1,
        )
        payload = json.loads(response.read().decode("utf-8"))
    finally:
        _stop_stub(server)

    assert server.captures[0]["bytes_read"] == len(body)
    assert payload["output"][0]["type"] == "function_call"
    assert payload["output"][0]["name"] == "exec_command"


def test_patched_urlopen_still_intercepts_standard_open() -> None:
    seen: list[str] = []

    def fake_urlopen(request: Request, timeout: float = 0) -> object:  # noqa: ARG001
        seen.append(request.full_url)
        raise AssertionError("patched urlopen should be used")

    request = Request("https://api.x.ai/v1/responses", data=b"{}", method="POST")
    with patch.object(gateway_transport, "urlopen", fake_urlopen):
        try:
            gateway_transport.open_once(
                request,
                upstream_name="xai",
                timeout=1,
                transport_policy=TransportPolicy.STANDARD,
            )
        except AssertionError as exc:
            assert "patched urlopen" in str(exc)
    assert seen == ["https://api.x.ai/v1/responses"]


def test_standard_http_connection_enables_tcp_keepalive() -> None:
    server = _start_stub(_SuccessHandler)
    server.captures = []  # type: ignore[attr-defined]
    try:
        connection = gateway_transport.OfficialHTTPConnection(
            "127.0.0.1",
            server.server_address[1],
            timeout=2,
        )
        connection.socket_options = gateway_transport.official_socket_options()
        connection.connect()
        try:
            keepalive = connection.sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE)
        finally:
            connection.close()
    finally:
        _stop_stub(server)
    assert keepalive == 1
