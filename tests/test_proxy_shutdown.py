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
