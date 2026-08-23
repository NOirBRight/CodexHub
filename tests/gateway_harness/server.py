"""In-process Gateway + stub-upstream servers for characterization tests."""

from __future__ import annotations

import json
import threading
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterator
from unittest.mock import patch

import codex_proxy
import gateway_transport

GATEWAY_CLIENT_KEY = "characterization-client-key"


@dataclass
class StubCapture:
    method: str
    path: str
    headers: dict[str, str]
    body: bytes


@dataclass
class StubUpstream:
    server: ThreadingHTTPServer
    captures: list[StubCapture] = field(default_factory=list)
    response_status: int = 200
    response_content_type: str = "application/json"
    response_body: bytes = b"{}"
    stream_chunks: tuple[bytes, ...] | None = None
    hold_after_headers: threading.Event | None = None
    headers_sent: threading.Event = field(default_factory=threading.Event)


class _StubHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, format: str, *args: object) -> None:
        return

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0"))
        return self.rfile.read(length) if length else b""

    def _record(self, method: str, body: bytes) -> StubUpstream:
        stub: StubUpstream = self.server.stub  # type: ignore[attr-defined]
        stub.captures.append(
            StubCapture(
                method=method,
                path=self.path,
                headers={key.lower(): value for key, value in self.headers.items()},
                body=body,
            )
        )
        return stub

    def do_GET(self) -> None:  # noqa: N802
        stub = self._record("GET", b"")
        self._write_response(stub)

    def do_POST(self) -> None:  # noqa: N802
        stub = self._record("POST", self._read_body())
        self._write_response(stub)

    def _write_response(self, stub: StubUpstream) -> None:
        chunks = stub.stream_chunks
        if chunks is not None:
            self.send_response(stub.response_status)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            stub.headers_sent.set()
            if stub.hold_after_headers is not None:
                stub.hold_after_headers.wait(timeout=5)
            for chunk in chunks:
                self.wfile.write(chunk)
                self.wfile.flush()
            return
        self.send_response(stub.response_status)
        self.send_header("Content-Type", stub.response_content_type)
        self.send_header("Content-Length", str(len(stub.response_body)))
        self.send_header("Connection", "close")
        self.end_headers()
        stub.headers_sent.set()
        if stub.hold_after_headers is not None:
            stub.hold_after_headers.wait(timeout=5)
        self.wfile.write(stub.response_body)


@contextmanager
def _serve(handler: type[BaseHTTPRequestHandler]) -> Iterator[ThreadingHTTPServer]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


class GatewayHarness:
    """Start a stub upstream and a production Gateway bound to loopback."""

    def __init__(self) -> None:
        self.host = "127.0.0.1"
        self._stack = ExitStack()
        self.stub: StubUpstream | None = None
        self.gateway: ThreadingHTTPServer | None = None

    @property
    def port(self) -> int:
        assert self.gateway is not None
        return self.gateway.server_port

    @property
    def stub_base_url(self) -> str:
        assert self.stub is not None
        return f"http://127.0.0.1:{self.stub.server.server_port}/v1"

    def __enter__(self) -> GatewayHarness:
        stub_server = self._stack.enter_context(_serve(_StubHandler))
        stub = StubUpstream(server=stub_server)
        stub_server.stub = stub  # type: ignore[attr-defined]
        self.stub = stub

        self._stack.enter_context(patch("codex_proxy.getproxies", return_value={"no": "localhost,127.0.0.1"}))
        self._stack.enter_context(patch.object(codex_proxy, "OFFICIAL_HTTP_POOLS", {}))
        self._stack.enter_context(patch.object(gateway_transport, "OFFICIAL_HTTP_POOLS", {}))
        self._stack.enter_context(patch.object(codex_proxy, "gateway_client_key", return_value=GATEWAY_CLIENT_KEY))
        self._stack.enter_context(patch.object(codex_proxy, "codex_access_token", return_value="synthetic-official-token"))
        self._stack.enter_context(patch.object(codex_proxy, "codex_account_id", return_value="synthetic-account-id"))
        self._stack.enter_context(patch.object(codex_proxy, "gateway_auto_retry_enabled", return_value=False))
        self._stack.enter_context(patch("gateway_settings.gateway_auto_retry_enabled", return_value=False))
        self._stack.enter_context(patch.object(codex_proxy, "transport_sse_idle_timeout_seconds", return_value=2.0))
        self._stack.enter_context(
            patch.object(
                codex_proxy,
                "choose_upstream",
                side_effect=self._choose_upstream,
            )
        )
        self._stack.enter_context(
            patch.object(
                codex_proxy,
                "official_upstream",
                side_effect=self._official_upstream,
            )
        )
        self._stack.enter_context(
            patch.object(
                codex_proxy,
                "current_catalog_data",
                return_value={
                    "models": [
                        {"slug": "gpt-5.5", "codex_proxy_metadata": {"provider": "openai"}},
                        {
                            "slug": "volc/glm-5.2",
                            "codex_proxy_metadata": {
                                "provider": "volc",
                                "upstream_name": "volc",
                                "upstream_model": "glm-5.2",
                            },
                        },
                    ]
                },
            )
        )

        gateway = self._stack.enter_context(_serve(codex_proxy.CodexProxyHandler))
        gateway.gateway_shutdown_controller = codex_proxy.GatewayShutdownController()  # type: ignore[attr-defined]
        self.gateway = gateway
        return self

    def __exit__(self, *exc: object) -> None:
        self._stack.close()
        self.stub = None
        self.gateway = None

    def _official_upstream(self) -> dict[str, Any]:
        return {
            "name": "official",
            "provider_id": "openai",
            "base_url": self.stub_base_url,
            "auth": "codex_auth",
            "reports_cached_input_tokens": True,
        }

    def _choose_upstream(self, model_id: str) -> dict[str, Any]:
        slug = str(model_id)
        if slug in {"gpt-5.5", "openai/gpt-5.5"}:
            return {
                "name": "official",
                "provider_id": "openai",
                "model_id": "gpt-5.5",
                "base_url": self.stub_base_url,
                "auth": "codex_auth",
                "upstream_model": "gpt-5.5",
                "upstream_format": "responses",
                "reports_cached_input_tokens": True,
            }
        if slug in {"volc/glm-5.2", "glm-5.2"}:
            return {
                "name": "volcengine",
                "provider_id": "volc",
                "model_id": "volc/glm-5.2",
                "base_url": self.stub_base_url,
                "auth": "api_key",
                "api_key": "volc-test-token",
                "upstream_model": "glm-5.2",
                "upstream_format": "chat_completions",
                "tool_protocol": "auto",
                "tool_surface_strategy": "eager",
                "native_responses_tool_codec": "none",
                "reports_cached_input_tokens": False,
                "supports_developer_role": True,
                "supported_reasoning_levels": (),
                "input_modalities": ("text",),
            }
        raise codex_proxy.ModelIdentityResolutionError(
            f"model is not in the characterization catalog: {slug}",
            classification="local_resolution_failure",
            reason="unsupported_model",
            provider_id=slug.partition("/")[0] or None,
            model_slug=slug,
        )

    def set_json_response(
        self,
        payload: dict[str, Any] | bytes,
        *,
        status: int = 200,
        content_type: str = "application/json",
    ) -> None:
        assert self.stub is not None
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
        self.stub.response_status = status
        self.stub.response_content_type = content_type
        self.stub.response_body = body
        self.stub.stream_chunks = None

    def set_sse_response(self, chunks: tuple[bytes, ...], *, status: int = 200) -> None:
        assert self.stub is not None
        self.stub.response_status = status
        self.stub.stream_chunks = chunks

    def close_admission(self) -> None:
        assert self.gateway is not None
        controller = getattr(self.gateway, "gateway_shutdown_controller", None)
        assert controller is not None
        controller.close_admission()
