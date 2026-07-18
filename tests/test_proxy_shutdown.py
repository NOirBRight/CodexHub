from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.request import Request

import codex_proxy
import pytest
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


def test_transport_attached_after_admission_closes_is_cancelled_without_upstream_work() -> None:
    controller = codex_proxy.GatewayShutdownController()
    admission = controller.admit()
    assert admission is not None

    controller.close_admission()
    late_transport = Mock()
    admission.attach_upstream_transport(late_transport)

    late_transport.close.assert_called_once_with()


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


def test_requests_arriving_after_admission_closure_never_open_an_upstream_transport() -> None:
    controller = codex_proxy.GatewayShutdownController()
    controller.close_admission()
    server = ThreadingHTTPServer(("127.0.0.1", 0), CodexProxyHandler)
    server.gateway_shutdown_controller = controller
    thread = threading.Thread(target=server.serve_forever)
    thread.start()

    try:
        host, port = server.server_address
        connection = HTTPConnection(host, port, timeout=2)
        with patch.object(
            codex_proxy,
            "_open_upstream_response",
            side_effect=AssertionError("closed admission must not open upstream work"),
        ) as open_upstream:
            connection.request(
                "POST",
                "/v1/responses",
                body=b'{"model":"gpt-5.6-terra","input":"must not reach upstream"}',
                headers={"Content-Type": "application/json"},
            )
            response = connection.getresponse()
            payload = json.loads(response.read().decode("utf-8"))
        connection.close()

        assert response.status == 503
        assert payload == codex_proxy.user_requested_shutdown_payload("responses")
        open_upstream.assert_not_called()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_request_racing_admission_closure_never_opens_or_retries_upstream_work() -> None:
    controller = codex_proxy.GatewayShutdownController()
    admission = controller.admit()
    assert admission is not None
    previous = codex_proxy._activate_gateway_request(admission)

    try:
        controller.close_admission()
        with patch.object(codex_proxy, "_open_upstream_once") as open_upstream:
            with pytest.raises(codex_proxy.GatewayUserRequestedShutdown):
                codex_proxy._open_upstream_response(
                    Request("https://example.invalid/v1/responses", data=b"{}", method="POST"),
                    upstream_name="official",
                    upstream_format="responses",
                    timeout=30,
                )
        open_upstream.assert_not_called()
    finally:
        codex_proxy._restore_gateway_request(previous)
        controller.complete(admission)


def test_cancelled_admission_interrupts_retry_wait_without_sleep() -> None:
    controller = codex_proxy.GatewayShutdownController()
    admission = controller.admit()
    assert admission is not None
    previous = codex_proxy._activate_gateway_request(admission)

    try:
        controller.close_admission()
        with patch.object(codex_proxy.time, "sleep") as sleep:
            with pytest.raises(codex_proxy.GatewayUserRequestedShutdown):
                codex_proxy._sleep_for_retry_with_gateway_cancellation(30.0)
        sleep.assert_not_called()
    finally:
        codex_proxy._restore_gateway_request(previous)
        controller.complete(admission)


def test_active_request_receives_a_sanitized_user_requested_shutdown_outcome() -> None:
    controller = codex_proxy.GatewayShutdownController()
    server = ThreadingHTTPServer(("127.0.0.1", 0), CodexProxyHandler)
    server.gateway_shutdown_controller = controller
    server_thread = threading.Thread(target=server.serve_forever)
    server_thread.start()
    upstream_entered = threading.Event()
    result: dict[str, object] = {}

    def wait_for_shutdown(*_args, **_kwargs):
        upstream_entered.set()
        admission = codex_proxy._active_gateway_request()
        assert admission is not None
        assert admission.wait_for_cancellation(2.0)
        admission.raise_if_cancelled()

    def post_active_request() -> None:
        host, port = server.server_address
        connection = HTTPConnection(host, port, timeout=4)
        connection.request(
            "POST",
            "/v1/responses",
            body=b'{"model":"gpt-5.6-terra","input":"active request"}',
            headers={"Content-Type": "application/json"},
        )
        response = connection.getresponse()
        result["status"] = response.status
        result["payload"] = json.loads(response.read().decode("utf-8"))
        connection.close()

    client_thread = threading.Thread(target=post_active_request)
    try:
        with (
            patch.object(
                codex_proxy,
                "upstream_headers",
                return_value={"Authorization": "Bearer test-token"},
            ) as build_upstream_headers,
            patch.object(
                codex_proxy,
                "codex_access_token",
                side_effect=AssertionError("shutdown cancellation must not read Codex auth"),
            ) as access_token,
            patch.object(
                codex_proxy,
                "codex_account_id",
                side_effect=AssertionError("shutdown cancellation must not read Codex auth"),
            ) as account_id,
            patch.object(codex_proxy, "_open_upstream_response", side_effect=wait_for_shutdown) as open_upstream,
            patch.object(codex_proxy, "write_proxy_event") as write_event,
        ):
            client_thread.start()
            assert upstream_entered.wait(timeout=2)

            host, port = server.server_address
            shutdown_connection = HTTPConnection(host, port, timeout=2)
            shutdown_connection.request("POST", "/shutdown")
            shutdown_response = shutdown_connection.getresponse()
            assert shutdown_response.status == 200
            shutdown_response.read()
            shutdown_connection.close()

            client_thread.join(timeout=3)
            assert not client_thread.is_alive()
            assert result == {
                "status": 503,
                "payload": codex_proxy.user_requested_shutdown_payload("responses"),
            }
            assert open_upstream.call_count == 1
            build_upstream_headers.assert_called_once()
            access_token.assert_not_called()
            account_id.assert_not_called()
            shutdown_event = next(
                call.kwargs
                for call in write_event.call_args_list
                if call.args and call.args[0] == "request_cancelled"
            )
            assert shutdown_event["shutdown_outcome"] == "user_requested_shutdown"
            assert "active request" not in repr(shutdown_event)
    finally:
        if client_thread.is_alive():
            client_thread.join(timeout=1)
        if server_thread.is_alive():
            server.shutdown()
        server.server_close()
        server_thread.join(timeout=2)


def test_downstream_already_closed_does_not_hide_user_requested_shutdown_cleanup() -> None:
    class BrokenStream:
        def write(self, _payload: bytes) -> None:
            raise BrokenPipeError("downstream already closed")

        def flush(self) -> None:
            raise BrokenPipeError("downstream already closed")

    handler = object.__new__(CodexProxyHandler)
    handler.wfile = BrokenStream()
    handler.close_connection = False

    handler._send_user_requested_shutdown_outcome(
        inbound_format="responses",
        downstream_sse_started=True,
    )

    assert handler.close_connection is True


def test_run_server_uses_the_remaining_shared_shutdown_budget_for_flush() -> None:
    now = [0.0]
    controller = codex_proxy.GatewayShutdownController(
        clock=lambda: now[0],
        shutdown_budget_seconds=2.0,
    )
    controller.close_admission()
    server = Mock()
    writer = Mock()
    writer.shutdown.return_value = SimpleNamespace(completed=True, outcome="drained")

    with tempfile.TemporaryDirectory() as tmpdir:
        with (
            patch.object(codex_proxy, "PROXY_TEXT_LOG_PATH", Path(tmpdir) / "proxy.log"),
            patch.object(codex_proxy.logging, "basicConfig"),
            patch.object(codex_proxy.logging, "FileHandler"),
            patch.object(codex_proxy.logging, "StreamHandler"),
            patch.object(codex_proxy, "ThreadingHTTPServer", return_value=server),
            patch.object(codex_proxy, "GatewayShutdownController", return_value=controller),
            patch.object(codex_proxy, "GATEWAY_EVENT_WRITER", writer),
        ):
            codex_proxy.run_server("127.0.0.1", 8080)

    writer.shutdown.assert_called_once_with(timeout=2.0)


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
