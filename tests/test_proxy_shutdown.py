from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import Mock, patch

import codex_proxy
from codex_proxy import CodexProxyHandler


def _run_server_with_event_writer_result(writer_result):
    server = Mock()
    writer = Mock()
    writer.shutdown.return_value = writer_result

    with tempfile.TemporaryDirectory() as tmpdir:
        with (
            patch.object(codex_proxy, "PROXY_TEXT_LOG_PATH", Path(tmpdir) / "proxy.log"),
            patch.object(codex_proxy.logging, "basicConfig"),
            patch.object(codex_proxy.logging, "FileHandler"),
            patch.object(codex_proxy.logging, "StreamHandler"),
            patch.object(codex_proxy, "ThreadingHTTPServer", return_value=server),
            patch.object(codex_proxy, "GATEWAY_EVENT_WRITER", writer),
            patch.object(codex_proxy.logger, "warning") as warning,
        ):
            codex_proxy.run_server("127.0.0.1", 8080)

    return server, writer, warning


def test_shutdown_endpoint_stops_server() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", 0), CodexProxyHandler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()

    try:
        host, port = server.server_address
        connection = HTTPConnection(host, port, timeout=2)
        connection.request("POST", "/shutdown")
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        connection.close()

        assert response.status == 200
        assert payload["ok"] is True

        thread.join(timeout=2)
        assert not thread.is_alive()
    finally:
        if thread.is_alive():
            server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_shutdown_endpoint_closes_admission_and_reports_user_requested_shutdown() -> None:
    controller = codex_proxy.GatewayShutdownController()
    server = ThreadingHTTPServer(("127.0.0.1", 0), CodexProxyHandler)
    server.gateway_shutdown_controller = controller
    thread = threading.Thread(target=server.serve_forever)
    thread.start()

    try:
        host, port = server.server_address
        connection = HTTPConnection(host, port, timeout=2)
        connection.request("POST", "/shutdown")
        response = connection.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
        connection.close()

        assert response.status == 200
        assert payload == {
            "ok": True,
            "outcome": "user_requested_shutdown",
        }
        assert controller.admit() is None

        thread.join(timeout=2)
        assert not thread.is_alive()
    finally:
        if thread.is_alive():
            server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_closing_admission_cancels_every_active_upstream_transport() -> None:
    controller = codex_proxy.GatewayShutdownController()
    official_request = controller.admit()
    third_party_request = controller.admit()
    assert official_request is not None
    assert third_party_request is not None

    official_transport = Mock()
    third_party_transport = Mock()
    official_request.attach_upstream_transport(official_transport)
    third_party_request.attach_upstream_transport(third_party_transport)

    assert controller.close_admission() == 2
    official_transport.close.assert_called_once_with()
    third_party_transport.close.assert_called_once_with()
    assert controller.admit() is None


def test_shutdown_budget_is_shared_by_many_active_requests() -> None:
    now = [10.0]
    controller = codex_proxy.GatewayShutdownController(
        clock=lambda: now[0],
        shutdown_budget_seconds=2.0,
    )
    active = [controller.admit() for _ in range(3)]
    assert all(active)

    assert controller.close_admission() == 3
    now[0] = 11.25
    assert controller.remaining_shutdown_budget_seconds() == 0.75

    now[0] = 12.0
    assert controller.remaining_shutdown_budget_seconds() == 0.0


def test_user_requested_shutdown_outcome_is_sanitized_for_every_downstream_format() -> None:
    for inbound_format in ("responses", "chat_completions"):
        payload = codex_proxy.user_requested_shutdown_payload(inbound_format)
        serialized = json.dumps(payload).lower()

        assert "user_requested_shutdown" in serialized
        assert "prompt" not in serialized
        assert "task" not in serialized
        assert "bearer" not in serialized


def test_run_server_drains_the_event_writer_when_the_server_exits() -> None:
    server, writer, warning = _run_server_with_event_writer_result(
        SimpleNamespace(completed=True, outcome="drained"),
    )

    server.serve_forever.assert_called_once_with()
    writer.shutdown.assert_called_once_with(
        timeout=codex_proxy.GATEWAY_EVENT_WRITER_SHUTDOWN_TIMEOUT_SECONDS,
    )
    warning.assert_not_called()


def test_run_server_reports_a_bounded_event_writer_shutdown_timeout() -> None:
    _server, writer, warning = _run_server_with_event_writer_result(
        SimpleNamespace(completed=False, outcome="timeout"),
    )

    writer.shutdown.assert_called_once_with(
        timeout=codex_proxy.GATEWAY_EVENT_WRITER_SHUTDOWN_TIMEOUT_SECONDS,
    )
    warning.assert_called_once()
    assert "timeout" in warning.call_args.args[1:]
