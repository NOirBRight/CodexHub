from __future__ import annotations

import io
import json
from pathlib import Path
import ssl
import tempfile
from unittest import TestCase
from unittest.mock import patch
from urllib.error import URLError
from urllib.request import Request

import codex_proxy
import diagnostic_recorder


class _Response:
    status = 200
    connection_disposition = "reused"
    headers = {
        "Content-Type": "text/event-stream; charset=utf-8",
        "Content-Length": "42",
        "Authorization": "Bearer upstream-secret",
    }


class _LineResponse:
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = iter(lines)

    def readline(self) -> bytes:
        return next(self._lines)


class _BrokenContext(dict[str, object]):
    def get(self, key: str, default: object = None) -> object:
        raise RuntimeError("context accessor failed")


class _MetadataFaultResponse:
    @property
    def status(self) -> int:
        raise RuntimeError("status accessor failed")

    @property
    def headers(self) -> dict[str, str]:
        raise RuntimeError("headers accessor failed")


class _TerminalResponse:
    status = 200
    headers: dict[str, str] = {}

    def __init__(self) -> None:
        self._sent = False

    def readline(self) -> bytes:
        if self._sent:
            return b""
        self._sent = True
        return b'data: {"type":"response.completed","response":{"id":"resp_private","status":"completed"}}\n\n'


class _FailingWriteStream:
    def write(self, _data: bytes) -> int:
        raise OSError("downstream closed")

    def flush(self) -> None:
        raise OSError("downstream closed")


class _PoolConnection:
    def __init__(self) -> None:
        self.sock = object()
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _SlowWriteSocket:
    def __init__(self, *, minimum_write_timeout: float = 0.2, fail_large_write: bool = False) -> None:
        self.timeout: float | None = None
        self.timeouts: list[float | None] = []
        self.sent_bytes = 0
        self.minimum_write_timeout = minimum_write_timeout
        self.fail_large_write = fail_large_write

    def settimeout(self, timeout: float | None) -> None:
        self.timeout = timeout
        self.timeouts.append(timeout)

    def sendall(self, data: bytes) -> None:
        self.sent_bytes += len(data)
        if len(data) > 1024 and (
            self.fail_large_write
            or (self.timeout is not None and self.timeout < self.minimum_write_timeout)
        ):
            raise TimeoutError("simulated slow request write")

    def makefile(self, _mode: str) -> io.BytesIO:
        return io.BytesIO(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n")

    def close(self) -> None:
        return None


class _ReadTimeoutFile:
    def readline(self, _limit: int = -1) -> bytes:
        raise TimeoutError("simulated response read timeout")

    def close(self) -> None:
        return None

    def flush(self) -> None:
        return None


class _ReadTimeoutSocket(_SlowWriteSocket):
    def makefile(self, _mode: str) -> _ReadTimeoutFile:
        return _ReadTimeoutFile()


class _ExplodingRecorder:
    def observe_proxy_event(self, event: str, fields: object) -> None:
        raise RuntimeError("recorder unavailable")


class DiagnosticRecorderGatewayTests(TestCase):
    def _official_request_pool_fixture(self, sock: _SlowWriteSocket) -> tuple[object, object]:
        connection = codex_proxy._OfficialHTTPSConnection("example.test", timeout=0.05)
        connection.sock = sock
        connection.is_verified = True
        connection.proxy_is_verified = True
        pool = codex_proxy._OfficialHTTPSConnectionPool("example.test")
        return pool, connection

    def test_official_pool_uses_request_budget_for_new_and_reused_connections(self) -> None:
        class _ConnectableConnection(codex_proxy._OfficialHTTPSConnection):
            def __init__(self, sock: _SlowWriteSocket) -> None:
                super().__init__("example.test", timeout=0.05)
                self._fixture_sock = sock

            def connect(self) -> None:
                self.sock = self._fixture_sock
                self.is_verified = True
                self.proxy_is_verified = True

        for reused in (False, True):
            with self.subTest(connection="reused" if reused else "new"):
                sock = _SlowWriteSocket(minimum_write_timeout=0.2)
                connection = _ConnectableConnection(sock)
                connection.is_verified = True
                connection.proxy_is_verified = True
                if reused:
                    connection.sock = sock
                pool = codex_proxy._OfficialHTTPSConnectionPool("example.test")

                response = pool._make_request(
                    connection,
                    "POST",
                    "/v1/responses",
                    body=b"x" * (2 * 1024 * 1024),
                    headers={"Content-Length": str(2 * 1024 * 1024)},
                    retries=None,
                    timeout=codex_proxy.urllib3.Timeout(connect=0.05, read=1.0),
                    chunked=False,
                    response_conn=None,
                    preload_content=False,
                    decode_content=False,
                )

                self.assertEqual(response.status, 200)
                self.assertIn(0.05, sock.timeouts)
                self.assertIn(1.0, sock.timeouts)

    def test_official_pool_uses_request_budget_for_slow_large_body_writes(self) -> None:
        sock = _SlowWriteSocket(minimum_write_timeout=0.2)
        pool, connection = self._official_request_pool_fixture(sock)
        body = b"x" * (2 * 1024 * 1024)

        response = pool._make_request(
            connection,
            "POST",
            "/v1/responses",
            body=body,
            headers={"Content-Length": str(len(body))},
            retries=None,
            timeout=codex_proxy.urllib3.Timeout(connect=0.05, read=1.0),
            chunked=False,
            response_conn=None,
            preload_content=False,
            decode_content=False,
        )

        self.assertEqual(response.status, 200)
        self.assertGreaterEqual(sock.sent_bytes, len(body))
        self.assertIn(0.05, sock.timeouts)
        self.assertIn(1.0, sock.timeouts)

    def test_official_request_write_timeout_is_classified_as_request_write(self) -> None:
        sock = _SlowWriteSocket(fail_large_write=True)
        pool, connection = self._official_request_pool_fixture(sock)

        with self.assertRaises(TimeoutError) as raised:
            pool._make_request(
                connection,
                "POST",
                "/v1/responses",
                body=b"x" * (2 * 1024 * 1024),
                headers={"Content-Length": str(2 * 1024 * 1024)},
                retries=None,
                timeout=codex_proxy.urllib3.Timeout(connect=0.05, read=1.0),
                chunked=False,
                response_conn=None,
                preload_content=False,
                decode_content=False,
            )

        self.assertEqual(codex_proxy.transport_failure_phase(raised.exception), "request_write")
        self.assertEqual(codex_proxy._retry_safety_failure_phase(raised.exception), "request_write")

    def test_upstream_open_telemetry_uses_request_write_for_tagged_timeout(self) -> None:
        tmpdir = self.enterContext(tempfile.TemporaryDirectory())
        recorder = diagnostic_recorder.DiagnosticRecorder(Path(tmpdir))
        self.addCleanup(recorder.shutdown, 1)
        request = Request("https://example.test/v1/responses", data=b"{}", method="POST")
        failure = TimeoutError("simulated request write timeout")
        setattr(failure, codex_proxy._TRANSPORT_PHASE_ATTRIBUTE, "request_write")

        with (
            patch.object(codex_proxy, "GATEWAY_DIAGNOSTIC_RECORDER", recorder),
            patch("codex_proxy._open_upstream_once", side_effect=failure),
        ):
            with self.assertRaises(TimeoutError):
                codex_proxy._open_upstream_response(
                    request,
                    upstream_name="official",
                    upstream_format="responses",
                    timeout=1,
                    event_context={"request_id": "request-write-fixture", "model": "openai/gpt-5.6"},
                    max_attempts=1,
                )

        self.assertTrue(recorder.flush(3))
        records = [
            json.loads(line)
            for path in (Path(tmpdir) / "diagnostics" / "rolling").glob("*.jsonl")
            for line in path.read_text(encoding="utf-8").splitlines()
            if line
        ]
        phase_records = [record for record in records if record["kind"] == "upstream_request_write"]
        self.assertEqual(len(phase_records), 1)
        self.assertEqual(phase_records[0]["failure_phase"], "request_write")

    def test_official_urlopen_preserves_request_write_phase_through_translation(self) -> None:
        request = Request("https://example.test/v1/responses", data=b"{}", method="POST")
        failure = TimeoutError("simulated request write timeout")
        setattr(failure, codex_proxy._TRANSPORT_PHASE_ATTRIBUTE, "request_write")
        wrapped = codex_proxy.urllib3.exceptions.MaxRetryError(None, request.full_url, failure)
        manager = codex_proxy.urllib3.PoolManager()

        with (
            patch("codex_proxy._official_pool_manager", return_value=manager),
            patch.object(manager, "request", side_effect=wrapped),
            self.assertRaises(TimeoutError) as raised,
        ):
            codex_proxy._official_urlopen(request, timeout=1.0)

        self.assertIs(raised.exception, failure)
        self.assertEqual(codex_proxy.transport_failure_phase(raised.exception), "request_write")

    def test_connect_and_tls_failures_are_not_marked_as_request_write(self) -> None:
        sock = _SlowWriteSocket(fail_large_write=True)
        connection = codex_proxy._OfficialHTTPSConnection("example.test", timeout=0.05)
        connection.sock = sock
        connection.is_verified = True
        connection.proxy_is_verified = True
        setattr(connection, codex_proxy._OFFICIAL_REQUEST_WRITE_TIMEOUT_ATTRIBUTE, 1.0)

        with self.assertRaises(TimeoutError) as raised:
            connection.send(b"x" * 2048)

        self.assertIsNone(getattr(raised.exception, codex_proxy._TRANSPORT_PHASE_ATTRIBUTE, None))
        self.assertEqual(codex_proxy.transport_failure_phase(raised.exception), "tcp_connect")
        self.assertEqual(codex_proxy.transport_failure_phase(ssl.SSLError("TLS handshake failed")), "tls_handshake")

    def test_official_read_timeout_keeps_read_socket_budget_and_no_write_phase(self) -> None:
        sock = _ReadTimeoutSocket()
        pool, connection = self._official_request_pool_fixture(sock)

        with self.assertRaises(codex_proxy.urllib3.exceptions.ReadTimeoutError) as raised:
            pool._make_request(
                connection,
                "POST",
                "/v1/responses",
                body=b"x",
                headers={"Content-Length": "1"},
                retries=None,
                timeout=codex_proxy.urllib3.Timeout(connect=0.05, read=0.75),
                chunked=False,
                response_conn=None,
                preload_content=False,
                decode_content=False,
            )

        self.assertIn(0.75, sock.timeouts)
        self.assertIsNone(codex_proxy._explicit_transport_phase(raised.exception))

    def test_official_timeout_keeps_connect_cap_and_read_budget(self) -> None:
        class _CapturingManager:
            def __init__(self) -> None:
                self.timeout = None

            def request(self, *_args: object, **kwargs: object) -> object:
                self.timeout = kwargs["timeout"]
                raise codex_proxy.urllib3.exceptions.ConnectTimeoutError(None, "simulated connect timeout")

        manager = _CapturingManager()
        request = Request("https://example.test/v1/responses", data=b"{}", method="POST")
        with patch("codex_proxy._official_pool_manager", return_value=manager):
            with self.assertRaises(TimeoutError):
                codex_proxy._official_urlopen(request, timeout=60.0)

        assert manager.timeout is not None
        self.assertEqual(manager.timeout.connect_timeout, codex_proxy.OFFICIAL_CONNECT_TIMEOUT_SECONDS)
        self.assertEqual(manager.timeout.read_timeout, 60.0)

    def test_official_pool_exposes_new_and_reused_connection_dispositions(self) -> None:
        pool = object.__new__(codex_proxy._OfficialHTTPSConnectionPool)
        pool.proxy = None
        connection = _PoolConnection()

        with (
            patch.object(codex_proxy.urllib3.connectionpool.HTTPSConnectionPool, "_get_conn", return_value=connection),
            patch("codex_proxy.time.monotonic", return_value=100.0),
        ):
            pool._get_conn()
            self.assertEqual(codex_proxy._connection_disposition(connection), "new")
            connection._codexhub_released_at = 99.0
            pool._get_conn()
            self.assertEqual(codex_proxy._connection_disposition(connection), "reused")
        self.assertEqual(codex_proxy._diagnostic_connection_disposition(object()), "unobserved")

    def test_reused_upstream_open_omits_unobservable_transport_success_phases(self) -> None:
        tmpdir = self.enterContext(tempfile.TemporaryDirectory())
        recorder = diagnostic_recorder.DiagnosticRecorder(Path(tmpdir))
        self.addCleanup(recorder.shutdown, 1)
        request = Request("https://example.test/v1/responses", data=b"{}", method="POST")

        with (
            patch.object(codex_proxy, "GATEWAY_DIAGNOSTIC_RECORDER", recorder),
            patch("codex_proxy._open_upstream_once", return_value=_Response()),
        ):
            response = codex_proxy._open_upstream_response(
                request,
                upstream_name="official",
                upstream_format="responses",
                timeout=1,
                event_context={"request_id": "raw-request-secret", "model": "openai/gpt-5.6"},
            )

        self.assertIsInstance(response, _Response)
        self.assertTrue(recorder.flush(3))
        rolling = Path(tmpdir) / "diagnostics" / "rolling"
        rendered = "\n".join(path.read_text(encoding="utf-8") for path in rolling.glob("*.jsonl"))
        records = [json.loads(line) for line in rendered.splitlines() if line]
        self.assertEqual(
            [record["kind"] for record in records],
            [
                "upstream_request_write",
                "upstream_attempt",
                "upstream_headers",
            ],
        )
        self.assertNotIn("raw-request-secret", rendered)
        self.assertNotIn("upstream-secret", rendered)
        self.assertTrue(
            all(record["kind"] not in {"upstream_dns", "upstream_tcp", "upstream_tls"} for record in records)
        )
        self.assertEqual(records[1]["connection_disposition"], "reused")
        self.assertEqual(records[2]["content_type_class"], "event-stream")

    def test_upstream_failure_records_the_supported_transport_phase(self) -> None:
        tmpdir = self.enterContext(tempfile.TemporaryDirectory())
        recorder = diagnostic_recorder.DiagnosticRecorder(Path(tmpdir))
        self.addCleanup(recorder.shutdown, 1)
        request = Request("https://example.test/v1/responses", data=b"{}", method="POST")

        with (
            patch.object(codex_proxy, "GATEWAY_DIAGNOSTIC_RECORDER", recorder),
            patch("codex_proxy._open_upstream_once", side_effect=URLError("private failure")),
            patch("codex_proxy.transport_failure_phase", return_value="tls"),
        ):
            with self.assertRaises(URLError):
                codex_proxy._open_upstream_response(
                    request,
                    upstream_name="official",
                    upstream_format="responses",
                    timeout=1,
                    event_context={"request_id": "private-request", "model": "openai/gpt-5.6"},
                    max_attempts=1,
                )

        self.assertTrue(recorder.flush(3))
        rendered = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (Path(tmpdir) / "diagnostics" / "rolling").glob("*.jsonl")
        )
        records = [json.loads(line) for line in rendered.splitlines() if line]
        self.assertEqual(
            [record["kind"] for record in records],
            ["upstream_tls", "upstream_attempt", "incident_marker"],
        )
        self.assertNotIn("private-request", rendered)
        self.assertNotIn("private failure", rendered)

    def test_terminal_observed_before_failed_downstream_write_replays_as_not_forwarded(self) -> None:
        tmpdir = self.enterContext(tempfile.TemporaryDirectory())
        handler = object.__new__(codex_proxy.CodexProxyHandler)
        handler.send_response = lambda *_args: None
        handler.send_header = lambda *_args: None
        handler.end_headers = lambda: None
        handler.wfile = _FailingWriteStream()

        # Keep the zero-tail automatic incident deterministic: the control
        # thread is deliberately disabled while the fixture drives it.
        with patch.object(diagnostic_recorder.DiagnosticRecorder, "_ensure_control_thread_locked"):
            recorder = diagnostic_recorder.DiagnosticRecorder(Path(tmpdir), incident_tail_seconds=0)
            self.addCleanup(recorder.shutdown, 1)
            with patch.object(codex_proxy, "GATEWAY_DIAGNOSTIC_RECORDER", recorder):
                status = handler._relay_official_passthrough_sse_response(
                    _TerminalResponse(),
                    "official",
                    request_id="private-terminal-request",
                )

        self.assertEqual(status, 499)
        self.assertEqual(recorder.process_due_incidents(), 1)
        artifact = recorder.read_incident("i000001")
        self.assertIsNotNone(artifact)
        assert artifact is not None
        kinds = [record["kind"] for record in artifact["records"]]
        self.assertIn("upstream_terminal", kinds)
        self.assertNotIn("downstream_terminal", kinds)
        self.assertIn("downstream_write", kinds)
        self.assertEqual(artifact["manifest"]["classification"], "terminal-not-forwarded")

    def test_upstream_open_ignores_diagnostic_context_and_metadata_accessor_failures(self) -> None:
        request = Request("https://example.test/v1/responses", data=b"{}", method="POST")
        response = _MetadataFaultResponse()

        with patch("codex_proxy._open_upstream_once", return_value=response):
            actual = codex_proxy._open_upstream_response(
                request,
                upstream_name="official",
                upstream_format="responses",
                timeout=1,
                event_context=_BrokenContext(),
            )

        self.assertIs(actual, response)

    def test_sse_iterator_reports_lines_without_observing_line_contents(self) -> None:
        handler = object.__new__(codex_proxy.CodexProxyHandler)
        seen: list[bytes] = []

        lines = list(
            handler._iter_upstream_sse_lines(
                _LineResponse([b"data: private-token\n\n", b""]),
                on_line=seen.append,
            )
        )

        self.assertEqual(lines, [b"data: private-token\n\n", b""])
        self.assertEqual(seen, [b"data: private-token\n\n"])

    def test_downstream_gateway_seam_records_response_open_and_headers(self) -> None:
        tmpdir = self.enterContext(tempfile.TemporaryDirectory())
        recorder = diagnostic_recorder.DiagnosticRecorder(Path(tmpdir))
        self.addCleanup(recorder.shutdown, 1)
        handler = object.__new__(codex_proxy.CodexProxyHandler)
        handler._diagnostic_request_id = "private-downstream-request"

        with patch.object(codex_proxy, "GATEWAY_DIAGNOSTIC_RECORDER", recorder):
            handler._observe_downstream_phase("downstream_response_open", status=200)
            handler._observe_downstream_phase("downstream_headers")

        self.assertTrue(recorder.flush(3))
        rendered = "\n".join(
            path.read_text(encoding="utf-8")
            for path in (Path(tmpdir) / "diagnostics" / "rolling").glob("*.jsonl")
        )
        records = [json.loads(line) for line in rendered.splitlines() if line]
        self.assertEqual([record["kind"] for record in records], ["downstream_response_open", "downstream_headers"])
        self.assertNotIn("private-downstream-request", rendered)

    def test_recorder_failure_is_never_visible_to_proxy_event_callers(self) -> None:
        with patch.object(codex_proxy, "GATEWAY_DIAGNOSTIC_RECORDER", _ExplodingRecorder()):
            codex_proxy._observe_gateway_diagnostic(
                "observe_proxy_event",
                "request_start",
                {"request_id": "raw-request"},
            )
