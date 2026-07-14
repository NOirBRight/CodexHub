from __future__ import annotations

import json
from pathlib import Path
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


class _ExplodingRecorder:
    def observe_proxy_event(self, event: str, fields: object) -> None:
        raise RuntimeError("recorder unavailable")


class DiagnosticRecorderGatewayTests(TestCase):
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
