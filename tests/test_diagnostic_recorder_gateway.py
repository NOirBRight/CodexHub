from __future__ import annotations

import json
from pathlib import Path
import tempfile
from unittest import TestCase
from unittest.mock import patch
from urllib.request import Request

import codex_proxy
import diagnostic_recorder


class _Response:
    status = 200
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


class _ExplodingRecorder:
    def observe_proxy_event(self, event: str, fields: object) -> None:
        raise RuntimeError("recorder unavailable")


class DiagnosticRecorderGatewayTests(TestCase):
    def test_upstream_open_records_only_sanitized_attempt_and_header_metadata(self) -> None:
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
        self.assertEqual([record["kind"] for record in records], ["upstream_attempt", "upstream_headers"])
        self.assertNotIn("raw-request-secret", rendered)
        self.assertNotIn("upstream-secret", rendered)
        self.assertEqual(records[0]["connection_disposition"], "unobserved")
        self.assertEqual(records[1]["content_type_class"], "event-stream")

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

    def test_recorder_failure_is_never_visible_to_proxy_event_callers(self) -> None:
        with patch.object(codex_proxy, "GATEWAY_DIAGNOSTIC_RECORDER", _ExplodingRecorder()):
            codex_proxy._observe_gateway_diagnostic(
                "observe_proxy_event",
                "request_start",
                {"request_id": "raw-request"},
            )
