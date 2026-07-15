from __future__ import annotations

import io
import json
from pathlib import Path
import tempfile
from unittest import TestCase
from unittest.mock import patch
from urllib.request import Request

import codex_proxy
import diagnostic_recorder


class _Connection:
    def __init__(self) -> None:
        self.sock = object()
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _ResettingProxyManager:
    def __init__(self, pool: object) -> None:
        self._pool = pool
        self.calls = 0

    def request(self, *_args: object, **_kwargs: object) -> object:
        self.calls += 1
        self._pool._get_conn()
        raise codex_proxy.urllib3.exceptions.ProtocolError(
            "fixture proxy reset",
            ConnectionResetError("fixture proxy reset"),
        )


class _RouteManager:
    def __init__(self) -> None:
        self.pool_classes_by_scheme: dict[str, object] = {}


class _StreamBodyResetResponse:
    status = 200
    headers = {"Content-Type": "text/event-stream"}

    def __init__(self) -> None:
        self._line_sent = False

    def readline(self) -> bytes:
        if not self._line_sent:
            self._line_sent = True
            return b"data: {}\n\n"
        raise ConnectionResetError("fixture proxy reset")


class OfficialProxyPathLocalizationTests(TestCase):
    def _rolling_records(self, root: Path) -> list[dict[str, object]]:
        rolling = root / "diagnostics" / "rolling"
        return [
            json.loads(line)
            for path in rolling.glob("*.jsonl")
            for line in path.read_text(encoding="utf-8").splitlines()
            if line
        ]

    def test_proxy_reset_before_headers_retains_connection_disposition(self) -> None:
        """A pre-header proxy reset retains whether its pool lease was fresh or reused."""

        for released_at, expected in ((None, "new"), (99.0, "reused")):
            with self.subTest(expected=expected):
                root = Path(self.enterContext(tempfile.TemporaryDirectory()))
                recorder = diagnostic_recorder.DiagnosticRecorder(root)
                self.addCleanup(recorder.shutdown, 1)
                request = Request("https://example.test/v1/responses", data=b"{}", method="POST")
                pool = object.__new__(codex_proxy._OfficialHTTPSConnectionPool)
                pool.proxy = object()
                connection = _Connection()
                if released_at is not None:
                    connection._codexhub_released_at = released_at
                manager = _ResettingProxyManager(pool)

                with (
                    patch.object(
                        codex_proxy.urllib3.connectionpool.HTTPSConnectionPool,
                        "_get_conn",
                        return_value=connection,
                    ),
                    patch("codex_proxy.time.monotonic", return_value=100.0),
                    patch("codex_proxy._official_pool_manager", return_value=manager),
                    patch.object(codex_proxy, "GATEWAY_DIAGNOSTIC_RECORDER", recorder),
                ):
                    with self.assertRaises(ConnectionResetError):
                        codex_proxy._open_upstream_response(
                            request,
                            upstream_name="official",
                            upstream_format="responses",
                            timeout=1,
                            event_context={"request_id": "private-request", "model": "openai/gpt-5.6-terra"},
                            max_attempts=1,
                        )

                self.assertTrue(recorder.flush(3))
                records = self._rolling_records(root)
                kinds = [record["kind"] for record in records]
                attempts = [record for record in records if record["kind"] == "upstream_attempt"]

                self.assertNotIn("upstream_headers", kinds)
                self.assertEqual(len(attempts), 1)
                self.assertEqual(attempts[0]["connection_disposition"], expected)
                self.assertNotIn("private-request", json.dumps(records))

    def test_pre_header_proxy_reset_respects_the_fixed_gateway_retry_budget(self) -> None:
        request = Request("https://example.test/v1/responses", data=b"{}", method="POST")
        pool = object.__new__(codex_proxy._OfficialHTTPSConnectionPool)
        pool.proxy = object()
        connection = _Connection()
        manager = _ResettingProxyManager(pool)

        with (
            patch.object(
                codex_proxy.urllib3.connectionpool.HTTPSConnectionPool,
                "_get_conn",
                return_value=connection,
            ),
            patch("codex_proxy._official_pool_manager", return_value=manager),
            patch("codex_proxy.time.sleep") as sleep,
            patch("codex_proxy.write_proxy_event") as write_event,
        ):
            with self.assertRaises(ConnectionResetError):
                codex_proxy._open_upstream_response(
                    request,
                    upstream_name="official",
                    upstream_format="responses",
                    timeout=1,
                    event_context={"request_id": "private-request"},
                    max_attempts=3,
                )

        retries = [call for call in write_event.call_args_list if call.args and call.args[0] == "upstream_retry"]
        self.assertEqual(manager.calls, 3)
        self.assertEqual(len(retries), 2)
        self.assertEqual(sleep.call_count, 2)

    def test_official_proxy_selection_distinguishes_bypass_explicit_and_registry_routes(self) -> None:
        target = "https://example.test/v1/responses"
        explicit_proxy = "http://explicit-proxy.invalid"
        registry_proxy = "http://registry-proxy.invalid"

        with (
            patch("codex_proxy.sys.platform", "win32"),
            patch("codex_proxy.getproxies", return_value={"https": explicit_proxy}),
            patch("codex_proxy.getproxies_registry") as registry,
            patch("codex_proxy.proxy_bypass", return_value=True),
        ):
            self.assertIsNone(codex_proxy._official_proxy_url(target))
        registry.assert_not_called()

        with (
            patch("codex_proxy.sys.platform", "win32"),
            patch("codex_proxy.getproxies", return_value={"https": explicit_proxy}),
            patch("codex_proxy.getproxies_registry") as registry,
            patch("codex_proxy.proxy_bypass", return_value=False),
        ):
            self.assertEqual(codex_proxy._official_proxy_url(target), explicit_proxy)
        registry.assert_not_called()

        with (
            patch("codex_proxy.sys.platform", "win32"),
            patch("codex_proxy.getproxies", return_value={"no": "localhost"}),
            patch("codex_proxy.getproxies_registry", return_value={"https": registry_proxy}),
            patch("codex_proxy.proxy_bypass", return_value=False),
        ):
            self.assertEqual(codex_proxy._official_proxy_url(target), registry_proxy)

    def test_direct_and_registry_derived_routes_use_separate_pool_managers(self) -> None:
        direct_manager = _RouteManager()
        proxy_manager = _RouteManager()
        with (
            patch.object(codex_proxy, "OFFICIAL_HTTP_POOLS", {}),
            patch(
                "codex_proxy._official_proxy_url",
                side_effect=[None, "http://registry-proxy.invalid"],
            ),
            patch("codex_proxy.urllib3.ProxyManager", return_value=proxy_manager) as make_proxy_manager,
            patch("codex_proxy.urllib3.PoolManager", return_value=direct_manager) as make_direct_manager,
        ):
            direct = codex_proxy._official_pool_manager("https://example.test/v1/responses")
            proxied = codex_proxy._official_pool_manager("https://example.test/v1/responses")

        self.assertIs(direct, direct_manager)
        self.assertIs(proxied, proxy_manager)
        self.assertIsNot(direct, proxied)
        make_proxy_manager.assert_called_once()
        make_direct_manager.assert_called_once()
        self.assertIs(proxy_manager.pool_classes_by_scheme["https"], codex_proxy._OfficialHTTPSConnectionPool)
        self.assertIs(direct_manager.pool_classes_by_scheme["https"], codex_proxy._OfficialHTTPSConnectionPool)

    def test_proxy_reset_during_stream_body_is_ordered_after_headers_and_never_retried(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        recorder = diagnostic_recorder.DiagnosticRecorder(root)
        self.addCleanup(recorder.shutdown, 1)
        response = _StreamBodyResetResponse()
        handler = object.__new__(codex_proxy.CodexProxyHandler)
        handler.close_connection = False
        handler.send_response = lambda _status: None
        handler.send_header = lambda _key, _value: None
        handler.end_headers = lambda: None
        handler.wfile = io.BytesIO()
        recorder.observe_upstream_headers("private-request", status=200, headers=response.headers)

        with patch.object(codex_proxy, "GATEWAY_DIAGNOSTIC_RECORDER", recorder):
            status = handler._relay_official_passthrough_sse_response(
                response,
                "official",
                request_id="private-request",
            )

        self.assertTrue(recorder.flush(3))
        records = self._rolling_records(root)
        kinds = [record["kind"] for record in records]

        self.assertEqual(status, 502)
        self.assertTrue(handler.close_connection)
        self.assertLess(kinds.index("upstream_headers"), kinds.index("sse_first"))
        self.assertLess(kinds.index("sse_first"), kinds.index("upstream_close"))
        self.assertNotIn("retry", kinds)
        rendered = json.dumps(records)
        self.assertNotIn("private-request", rendered)
        self.assertNotIn("fixture proxy reset", rendered)
