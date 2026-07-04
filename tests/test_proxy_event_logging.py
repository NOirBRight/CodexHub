from __future__ import annotations

import importlib
import json
import sqlite3
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

import codex_proxy


class ProxyEventLoggingTests(TestCase):
    def test_event_log_uses_runtime_codex_home(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_home = Path(tmpdir) / "codex-home"
            try:
                with patch.dict("os.environ", {"CODEX_HOME": str(codex_home)}, clear=False):
                    importlib.reload(codex_proxy)

                    self.assertEqual(
                        codex_proxy.PROXY_EVENT_LOG_PATH,
                        codex_home / "proxy" / "codex-proxy-events.jsonl",
                    )
                    codex_proxy.write_proxy_event("request_complete", request_id="req-test", status=200)

                    payload = json.loads(codex_proxy.PROXY_EVENT_LOG_PATH.read_text(encoding="utf-8").strip())
                    self.assertEqual(payload["event"], "request_complete")
                    self.assertEqual(payload["request_id"], "req-test")
            finally:
                importlib.reload(codex_proxy)

    def test_event_log_writes_jsonl_without_sqlite_request_path_write(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_home = Path(tmpdir) / "codex-home"
            try:
                with patch.dict("os.environ", {"CODEX_HOME": str(codex_home)}, clear=False):
                    import proxy_telemetry

                    importlib.reload(proxy_telemetry)
                    importlib.reload(codex_proxy)

                    with patch("proxy_telemetry.write_event_to_sqlite") as sqlite_write:
                        codex_proxy.write_proxy_event(
                            "request_start",
                            request_id="req-jsonl",
                            method="POST",
                            path="/v1/responses",
                            client_id="opencode",
                            thread_id="thread-1",
                            window_id="window-1",
                            upstream="official",
                            provider_id="official",
                            model="openai/gpt-5.5",
                            model_requested="openai/gpt-5.5",
                            model_canonical="openai/gpt-5.5",
                            request_body_hmac="body-hash",
                            Authorization="Bearer should-not-persist",
                        )
                        codex_proxy.write_proxy_event(
                            "request_complete",
                            request_id="req-jsonl",
                            method="POST",
                            status=200,
                            duration_ms=123,
                            usage_source="upstream",
                            usage_input_tokens=10,
                            usage_cached_input_tokens=4,
                            usage_output_tokens=2,
                            upstream="official",
                            model="openai/gpt-5.5",
                        )

                    jsonl = codex_proxy.PROXY_EVENT_LOG_PATH.read_text(encoding="utf-8")
                    self.assertNotIn("should-not-persist", jsonl)
                    sqlite_write.assert_not_called()
                    self.assertFalse(proxy_telemetry.telemetry_db_path(codex_home).exists())
                    payloads = [json.loads(line) for line in jsonl.splitlines() if line.strip()]
                    self.assertEqual([payload["event"] for payload in payloads], ["request_start", "request_complete"])
                    self.assertTrue(all(payload["request_id"] == "req-jsonl" for payload in payloads))
            finally:
                importlib.reload(codex_proxy)

    def test_official_bare_model_names_are_normalized_before_sqlite_write(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            import proxy_telemetry

            codex_home = Path(tmpdir) / "codex-home"
            payload = proxy_telemetry.prepare_event_payload(
                "request_start",
                {
                    "request_id": "req-official-model",
                    "upstream": "official",
                    "model": "gpt-5.5",
                },
                codex_home,
            )

            self.assertEqual(payload["model"], "openai/gpt-5.5")
            self.assertEqual(payload["model_canonical"], "openai/gpt-5.5")
            self.assertEqual(payload["model_requested"], "gpt-5.5")

            db_path = Path(tmpdir) / "codex-proxy-telemetry.sqlite"
            proxy_telemetry.write_event_to_sqlite(
                db_path,
                {
                    "ts": "2026-07-03T01:00:00Z",
                    "event": "request_complete",
                    "request_id": "req-official-model",
                    "upstream": "official",
                    "model": "gpt-5.5",
                    "model_canonical": "gpt-5.5",
                    "status": 200,
                },
            )

            connection = sqlite3.connect(db_path)
            try:
                self.assertEqual(
                    connection.execute(
                        "SELECT model, model_requested, model_canonical FROM gateway_requests WHERE request_id = ?",
                        ("req-official-model",),
                    ).fetchone(),
                    ("openai/gpt-5.5", "gpt-5.5", "openai/gpt-5.5"),
                )
            finally:
                connection.close()

    def test_request_context_ignores_client_route_mode_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_home = Path(tmpdir) / "codex-home"
            try:
                with patch.dict("os.environ", {"CODEX_HOME": str(codex_home)}, clear=False):
                    importlib.reload(codex_proxy)
                    context = codex_proxy.request_context_from_headers(
                        {
                            "x-codex-metadata": json.dumps(
                                {
                                    "client_id": "opencode",
                                    "thread_id": "thread-1",
                                    "route_mode": "official",
                                }
                            )
                        }
                    )

                    self.assertEqual(context["client_id"], "opencode")
                    self.assertEqual(context["thread_id"], "thread-1")
                    self.assertNotIn("route_mode", context)
                    codex_proxy.write_proxy_event(
                        "request_start",
                        request_id="req-route-context",
                        route_mode="codexhub",
                        upstream="external",
                        **context,
                    )
            finally:
                importlib.reload(codex_proxy)

    def test_backfill_jsonl_to_sqlite_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            import proxy_telemetry

            root = Path(tmpdir)
            log_path = root / "codex-proxy-events.jsonl"
            db_path = root / "codex-proxy-telemetry.sqlite"
            events = [
                {
                    "ts": "2026-07-03T01:00:00Z",
                    "event": "request_start",
                    "request_id": "req-backfill",
                    "method": "POST",
                    "path": "/v1/responses",
                    "client_id": "codex-app",
                    "upstream": "official",
                    "model": "openai/gpt-5.5",
                },
                {
                    "ts": "2026-07-03T01:00:02Z",
                    "event": "request_complete",
                    "request_id": "req-backfill",
                    "status": 200,
                    "duration_ms": 2000,
                    "usage_source": "upstream",
                    "usage_input_tokens": 7,
                    "usage_output_tokens": 3,
                },
            ]
            log_path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")

            proxy_telemetry.backfill_event_log_to_sqlite(log_path, db_path)
            proxy_telemetry.backfill_event_log_to_sqlite(log_path, db_path)

            connection = sqlite3.connect(db_path)
            try:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM gateway_events").fetchone()[0], 2)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM gateway_requests").fetchone()[0], 1)
                self.assertEqual(
                    connection.execute(
                        "SELECT value FROM telemetry_meta WHERE key = 'last_backfill_size'"
                    ).fetchone()[0],
                    str(log_path.stat().st_size),
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT usage_input_tokens, usage_output_tokens FROM gateway_requests WHERE request_id = ?",
                        ("req-backfill",),
                    ).fetchone(),
                    (7, 3),
                )
            finally:
                connection.close()

    def test_sqlite_preserves_distinct_events_for_same_request_id(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            import proxy_telemetry

            db_path = Path(tmpdir) / "codex-proxy-telemetry.sqlite"
            first = {
                "ts": "2026-07-03T01:00:00Z",
                "event": "request_error",
                "request_id": "req-retry",
                "status": 502,
                "duration_ms": 100,
                "usage_missing_reason": "upstream_error",
            }
            second = {
                "ts": "2026-07-03T01:00:01Z",
                "event": "request_error",
                "request_id": "req-retry",
                "status": 504,
                "duration_ms": 200,
                "usage_missing_reason": "upstream_timeout",
            }
            second_reordered = {
                "duration_ms": 200,
                "request_id": "req-retry",
                "event": "request_error",
                "usage_missing_reason": "upstream_timeout",
                "status": 504,
                "ts": "2026-07-03T01:00:01Z",
            }

            proxy_telemetry.write_event_to_sqlite(db_path, first)
            proxy_telemetry.write_event_to_sqlite(db_path, second)
            proxy_telemetry.write_event_to_sqlite(db_path, second_reordered)

            connection = sqlite3.connect(db_path)
            try:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM gateway_events").fetchone()[0], 2)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM gateway_requests").fetchone()[0], 1)
                self.assertEqual(
                    connection.execute(
                        "SELECT completed_ts, status, duration_ms, usage_missing_reason FROM gateway_requests WHERE request_id = ?",
                        ("req-retry",),
                    ).fetchone(),
                    ("2026-07-03T01:00:01Z", 504, 200, "upstream_timeout"),
                )
            finally:
                connection.close()

    def test_sqlite_writer_migrates_existing_request_table(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            import proxy_telemetry

            db_path = Path(tmpdir) / "codex-proxy-telemetry.sqlite"
            connection = sqlite3.connect(db_path)
            try:
                connection.execute(
                    """
                    CREATE TABLE gateway_requests (
                        request_id TEXT PRIMARY KEY,
                        created_at TEXT NOT NULL,
                        updated_at TEXT NOT NULL
                    )
                    """
                )
                connection.commit()
            finally:
                connection.close()

            proxy_telemetry.write_event_to_sqlite(
                db_path,
                {
                    "ts": "2026-07-03T01:00:00Z",
                    "event": "request_complete",
                    "request_id": "req-migrate",
                    "status": 200,
                    "usage_input_tokens": 12,
                },
            )

            connection = sqlite3.connect(db_path)
            try:
                columns = {row[1] for row in connection.execute("PRAGMA table_info(gateway_requests)")}
                self.assertIn("status", columns)
                self.assertIn("usage_input_tokens", columns)
                self.assertEqual(
                    connection.execute(
                        "SELECT status, usage_input_tokens FROM gateway_requests WHERE request_id = ?",
                        ("req-migrate",),
                    ).fetchone(),
                    (200, 12),
                )
            finally:
                connection.close()

    def test_proxy_event_logging_does_not_attempt_sqlite_write(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            codex_home = Path(tmpdir) / "codex-home"
            try:
                with patch.dict("os.environ", {"CODEX_HOME": str(codex_home)}, clear=False):
                    import proxy_telemetry

                    importlib.reload(proxy_telemetry)
                    importlib.reload(codex_proxy)
                    with patch("proxy_telemetry.write_event_to_sqlite", side_effect=sqlite3.DatabaseError("boom")) as sqlite_write:
                        codex_proxy.write_proxy_event(
                            "request_complete",
                            request_id="req-jsonl-survives",
                            status=200,
                        )
                    sqlite_write.assert_not_called()

                    payloads = [
                        json.loads(line)
                        for line in codex_proxy.PROXY_EVENT_LOG_PATH.read_text(encoding="utf-8").splitlines()
                        if line.strip()
                    ]
                    self.assertTrue(
                        any(payload.get("request_id") == "req-jsonl-survives" for payload in payloads)
                    )
                    self.assertFalse(any(payload.get("event") == "telemetry_sqlite_write_failed" for payload in payloads))
            finally:
                importlib.reload(codex_proxy)
