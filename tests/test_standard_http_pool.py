"""Loopback tests for STANDARD urllib3 pooling across all third-party origins."""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from unittest.mock import patch
from urllib.request import Request

import gateway_http_pool
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


class _KeepAliveHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        body = b'{"ok":true}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = False


class _StreamingHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        return

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(b"data: {\"delta\":1}\n\n")
        self.wfile.flush()
        self.close_connection = False


def _post(url: str, body: bytes = b"{}", **kwargs: Any) -> Any:
    request = Request(url, data=body, method="POST")
    request.add_header("Content-Type", "application/json")
    return gateway_transport.open_once(
        request,
        upstream_name=kwargs.get("upstream_name", "xai"),
        timeout=kwargs.get("timeout", 2),
        transport_policy=TransportPolicy.STANDARD,
    )


def _isolated_pools() -> tuple[Any, Any]:
    return (
        patch.object(gateway_transport, "STANDARD_HTTP_POOLS", {}),
        patch("gateway_transport.getproxies", return_value={"no": "localhost,127.0.0.1"}),
    )


def test_same_origin_second_post_reuses_connection() -> None:
    server = _start_stub(_KeepAliveHandler)
    url = f"http://127.0.0.1:{server.server_address[1]}/v1/responses"
    pools_patch, proxy_patch = _isolated_pools()
    try:
        with pools_patch, proxy_patch:
            first = _post(url, upstream_name="xai")
            first_disposition = first.connection_disposition
            assert json.loads(first.read().decode("utf-8")) == {"ok": True}
            first.close()
            second = _post(url, upstream_name="xai")
            second_disposition = second.connection_disposition
            second.read()
            second.close()
    finally:
        _stop_stub(server)

    assert first_disposition == "new"
    assert second_disposition == "reused"


def test_idle_expiry_forces_new_connection() -> None:
    server = _start_stub(_KeepAliveHandler)
    url = f"http://127.0.0.1:{server.server_address[1]}/v1/responses"
    pools_patch, proxy_patch = _isolated_pools()
    try:
        with (
            pools_patch,
            proxy_patch,
            patch.object(gateway_http_pool, "POOL_MAX_IDLE_SECONDS", 0.05),
        ):
            first = _post(url)
            first.read()
            first.close()
            time.sleep(0.08)
            second = _post(url)
            second_disposition = second.connection_disposition
            second.read()
            second.close()
    finally:
        _stop_stub(server)

    assert second_disposition == "new"


def test_distinct_origins_use_isolated_managers() -> None:
    first_server = _start_stub(_KeepAliveHandler)
    second_server = _start_stub(_KeepAliveHandler)
    pools: dict[str, Any] = {}
    lock = threading.Lock()
    try:
        xai = gateway_transport.standard_pool_manager(
            f"http://127.0.0.1:{first_server.server_address[1]}/v1/responses",
            pools=pools,
            pools_lock=lock,
            proxy_url=None,
        )
        opencode = gateway_transport.standard_pool_manager(
            f"http://127.0.0.1:{second_server.server_address[1]}/v1/responses",
            pools=pools,
            pools_lock=lock,
            proxy_url=None,
        )
        same_xai = gateway_transport.standard_pool_manager(
            f"http://127.0.0.1:{first_server.server_address[1]}/v1/chat",
            pools=pools,
            pools_lock=lock,
            proxy_url=None,
        )
    finally:
        _stop_stub(first_server)
        _stop_stub(second_server)

    assert xai is not opencode
    assert xai is same_xai
    assert len(pools) == 2
    assert gateway_http_pool.pool_origin(
        "https://api.x.ai/v1/responses"
    ) != gateway_http_pool.pool_origin("https://opencode.ai/v1/responses")
    assert gateway_http_pool.standard_pool_key(
        "https://api.x.ai/v1/responses", None
    ) != gateway_http_pool.standard_pool_key(
        "https://opencode.ai/v1/responses", None
    )


def test_proxy_and_direct_use_separate_standard_managers() -> None:
    pools: dict[str, Any] = {}
    lock = threading.Lock()
    url = "https://api.x.ai/v1/responses"
    direct = gateway_transport.standard_pool_manager(
        url, pools=pools, pools_lock=lock, proxy_url=None
    )
    proxied = gateway_transport.standard_pool_manager(
        url,
        pools=pools,
        pools_lock=lock,
        proxy_url="http://registry-proxy.invalid",
    )
    other_origin = gateway_transport.standard_pool_manager(
        "https://opencode.ai/v1/responses",
        pools=pools,
        pools_lock=lock,
        proxy_url=None,
    )

    assert direct is not proxied
    assert direct is not other_origin
    assert len(pools) == 3
    assert proxied.pool_classes_by_scheme["http"] is gateway_transport.OfficialHTTPConnectionPool
    assert proxied.pool_classes_by_scheme["https"] is gateway_transport.OfficialHTTPSConnectionPool
    assert "direct|https://api.x.ai:443" in pools
    assert "http://registry-proxy.invalid|https://api.x.ai:443" in pools


def test_incomplete_pooled_response_close_does_not_return_live_connection() -> None:
    class _Inner:
        status = 200
        reason = "OK"
        headers: dict[str, str] = {}

        def __init__(self) -> None:
            self.connection = object()
            self.closed = False
            self.released = False

        def read(self, amount: int | None = None) -> bytes:
            return b"partial"

        def close(self) -> None:
            self.closed = True

        def release_conn(self) -> None:
            self.released = True

    inner = _Inner()
    pooled = gateway_transport.OfficialPooledResponse(inner)
    pooled.close()
    assert inner.closed is True
    assert inner.released is True


def test_exhausted_pooled_response_releases_without_closing_socket_first() -> None:
    class _Inner:
        status = 200
        reason = "OK"
        headers: dict[str, str] = {}

        def __init__(self) -> None:
            self.connection = object()
            self.closed = False
            self.released = False

        def read(self, amount: int | None = None) -> bytes:
            return b""

        def close(self) -> None:
            self.closed = True

        def release_conn(self) -> None:
            self.released = True

    inner = _Inner()
    pooled = gateway_transport.OfficialPooledResponse(inner)
    assert pooled.read() == b""
    pooled.close()
    assert inner.closed is False
    assert inner.released is True


def test_closing_unread_stream_does_not_reuse_the_connection() -> None:
    server = _start_stub(_StreamingHandler)
    url = f"http://127.0.0.1:{server.server_address[1]}/v1/responses"
    pools_patch, proxy_patch = _isolated_pools()
    try:
        with pools_patch, proxy_patch:
            first = _post(url)
            first.readline()
            first.close()
            second = _post(url)
            second_disposition = second.connection_disposition
            second.readline()
            second.close()
    finally:
        _stop_stub(server)

    assert second_disposition == "new"


def test_standard_pool_does_not_share_official_registry() -> None:
    official = gateway_transport.OFFICIAL_HTTP_POOLS
    standard = gateway_transport.STANDARD_HTTP_POOLS
    assert official is not standard
    with patch.object(gateway_transport, "STANDARD_HTTP_POOLS", {}):
        gateway_transport.standard_pool_manager(
            "https://api.x.ai/v1/responses",
            proxy_url=None,
        )
        assert gateway_transport.OFFICIAL_HTTP_POOLS is official
        assert "direct|https://api.x.ai:443" not in official
