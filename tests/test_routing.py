import os
import io
import json
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import codex_proxy
from subagent_state import build_subagent_state
from codex_proxy import (
    CodexProxyHandler,
    _chat_stream_chunks_to_response_events,
    _filtered_response_headers,
    _is_websocket_upgrade,
    raw_provider_probe_requested,
    _responses_request_to_chat_completion_body,
    _responses_url,
    choose_upstream,
    compatible_request_body,
    compatible_response_body,
    compatible_sse_line,
    decoded_request_body,
    extract_model,
    official_upstream,
    request_context_from_headers,
    try_extract_model,
    upstream_timeout_seconds,
    upstream_headers,
)


class FakeWFile:
    def __init__(self):
        self.writes = []
        self.flush_count = 0

    def write(self, data):
        self.writes.append(data)

    def flush(self):
        self.flush_count += 1


class FakeHandler:
    def __init__(self):
        self.status = None
        self.headers = []
        self.headers_ended = False
        self.close_connection = False
        self.wfile = FakeWFile()

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.headers.append((key, value))

    def end_headers(self):
        self.headers_ended = True

    def _write_downstream_sse_error(self, **kwargs):
        return CodexProxyHandler._write_downstream_sse_error(self, **kwargs)

    def _write_sse_event(self, event, payload):
        return CodexProxyHandler._write_sse_event(self, event, payload)

    def _write_sse_error_event(self, upstream_name, exc):
        return CodexProxyHandler._write_sse_error_event(self, upstream_name, exc)

    def _write_sse_keepalive(self):
        return CodexProxyHandler._write_sse_keepalive(self)

    def _iter_upstream_sse_lines(self, *args, **kwargs):
        return CodexProxyHandler._iter_upstream_sse_lines(self, *args, **kwargs)

    def _write_sse_protocol_error_event(self, upstream_name, status, detail, *, error="UpstreamProtocolError"):
        return CodexProxyHandler._write_sse_protocol_error_event(
            self,
            upstream_name,
            status,
            detail,
            error=error,
        )


class FakeSseResponse:
    status = 200

    def __init__(self, lines):
        self.headers = {
            "Content-Type": "text/event-stream; charset=utf-8",
            "Transfer-Encoding": "chunked",
            "Content-Length": "999",
        }
        self.lines = list(lines)

    def readline(self):
        return self.lines.pop(0)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return None


class FakeSequencedDelayedSseResponse(FakeSseResponse):
    def __init__(self, items):
        super().__init__([])
        self.items = list(items)
        self.closed = False

    def readline(self):
        if not self.items:
            return b""
        delay, line = self.items.pop(0)
        if delay:
            time.sleep(delay)
        return line

    def close(self):
        self.closed = True


class FakeResponse:
    status = 200

    def __init__(self, body):
        self.headers = {"Content-Type": "application/json", "Content-Length": str(len(body))}
        self.body = body
        self.did_read = False

    def read(self, size=-1):
        if self.did_read:
            return b""
        self.did_read = True
        return self.body


class FakeContextResponse(FakeResponse):
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return None


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.runtime_proxy_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.runtime_proxy_dir.cleanup)
        self.runtime_proxy_patch = patch("codex_proxy.RUNTIME_PROXY_DIR", Path(self.runtime_proxy_dir.name))
        self.runtime_proxy_patch.start()
        self.addCleanup(self.runtime_proxy_patch.stop)
        self.catalog_patch = patch(
            "codex_proxy.generated_catalog_slugs",
            return_value={
                "openai/gpt-5.5",
                "minimax-m3",
                "glm-5.2",
                "ollama-cloud/glm-5.2",
                "ollama-cloud/minimax-m3",
                "kimi-k2.7-code",
                "gemini-3-flash-preview",
                "deepseek-v4-pro",
                "deepseek-v4-flash",
                "volc/glm-5.2",
                "minimax-cn/MiniMax-M3",
                "minimax-cn/minimax-m3",
            },
        )
        self.catalog_patch.start()
        self.addCleanup(self.catalog_patch.stop)
        self.event_log_patch = patch("codex_proxy.write_proxy_event")
        self.write_proxy_event = self.event_log_patch.start()
        self.addCleanup(self.event_log_patch.stop)
        self.catalog_by_slug_patch = patch(
            "codex_proxy.generated_catalog_by_slug",
            return_value={
                "openai/gpt-5.5": {"slug": "openai/gpt-5.5"},
                "minimax-m3": {"slug": "minimax-m3", "max_output_tokens": 524288},
                "glm-5.2": {"slug": "glm-5.2", "max_output_tokens": 131072},
                "ollama-cloud/glm-5.2": {"slug": "ollama-cloud/glm-5.2", "max_output_tokens": 131072},
                "ollama-cloud/minimax-m3": {"slug": "ollama-cloud/minimax-m3", "max_output_tokens": 524288},
                "kimi-k2.7-code": {"slug": "kimi-k2.7-code", "max_output_tokens": 32768},
                "gemini-3-flash-preview": {"slug": "gemini-3-flash-preview", "max_output_tokens": 65536},
                "deepseek-v4-pro": {"slug": "deepseek-v4-pro", "max_output_tokens": 393216},
                "deepseek-v4-flash": {"slug": "deepseek-v4-flash", "max_output_tokens": 393216},
                "volc/glm-5.2": {"slug": "volc/glm-5.2", "max_output_tokens": 4096},
                "minimax-cn/MiniMax-M3": {"slug": "minimax-cn/MiniMax-M3", "max_output_tokens": 524288},
                "minimax-cn/minimax-m3": {"slug": "minimax-cn/minimax-m3", "max_output_tokens": 524288},
            },
        )
        self.catalog_by_slug_patch.start()
        self.addCleanup(self.catalog_by_slug_patch.stop)
        policy = codex_proxy.load_policy(codex_proxy.POLICY_PATH)
        self.policy_patch = patch(
            "codex_proxy.load_policy",
            return_value=replace(
                policy,
                allowed_provider_models=policy.allowed_provider_models + ("minimax-cn/MiniMax-M3",),
            ),
        )
        self.policy_patch.start()
        self.addCleanup(self.policy_patch.stop)
        self.external_model = {
            "alias": "volc/glm-5.2",
            "provider_alias": "volc",
            "upstream_name": "volcengine",
            "display_prefix": "Volc",
            "base_url": "https://ark.example.test/v1",
            "api_key": "volc-test-token",
            "upstream_model": "glm-5.2",
            "priority_base": 200,
            "context_window": 1024000,
            "max_output_tokens": 4096,
            "input_modalities": ("text",),
            "context_source": "providers_toml",
            "max_output_source": "providers_toml",
        }
        self.minimax_external_model = {
            "alias": "minimax-cn/MiniMax-M3",
            "provider_alias": "minimax-cn",
            "upstream_name": "minimax_cn",
            "display_prefix": "MiniMax.cn",
            "base_url": "https://api.minimaxi.com/v1",
            "api_key": "minimax-test-token",
            "upstream_model": "MiniMax-M3",
            "priority_base": 300,
            "context_window": 1000000,
            "max_output_tokens": 524288,
            "input_modalities": ("text", "image"),
            "context_source": "providers_toml",
            "max_output_source": "providers_toml",
        }
        self.external_patch = patch(
            "codex_proxy.resolve_external_model_alias",
            side_effect=lambda slug: {
                "volc/glm-5.2": self.external_model,
                "minimax-cn/MiniMax-M3": self.minimax_external_model,
                "minimax-cn/minimax-m3": self.minimax_external_model,
            }.get(slug),
        )
        self.external_patch.start()
        self.addCleanup(self.external_patch.stop)

    def test_gateway_auto_retry_settings_default_to_enabled_thirty_attempts(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(codex_proxy.gateway_auto_retry_enabled())
            self.assertEqual(codex_proxy.gateway_auto_retry_max_attempts(), 30)

    def test_subagent_assist_mode_defaults_to_assisted(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(codex_proxy.subagent_assist_mode(), "assisted")

    def test_subagent_assist_mode_accepts_strict_guided_assisted(self):
        for value in ("strict", "guided", "assisted"):
            with self.subTest(value=value), patch.dict(
                os.environ,
                {"CODEXHUB_SUBAGENT_ASSIST_MODE": value},
                clear=False,
            ):
                self.assertEqual(codex_proxy.subagent_assist_mode(), value)

    def test_subagent_assist_mode_invalid_value_falls_back_to_assisted(self):
        with patch.dict(os.environ, {"CODEXHUB_SUBAGENT_ASSIST_MODE": "maybe"}, clear=False):
            self.assertEqual(codex_proxy.subagent_assist_mode(), "assisted")

    def test_subagent_semantic_repair_enabled_only_in_assisted(self):
        with patch.dict(os.environ, {"CODEXHUB_SUBAGENT_ASSIST_MODE": "strict"}, clear=False):
            self.assertFalse(codex_proxy.subagent_semantic_repair_enabled({}))
        with patch.dict(os.environ, {"CODEXHUB_SUBAGENT_ASSIST_MODE": "guided"}, clear=False):
            self.assertFalse(codex_proxy.subagent_semantic_repair_enabled({}))
        with patch.dict(os.environ, {"CODEXHUB_SUBAGENT_ASSIST_MODE": "assisted"}, clear=False):
            self.assertTrue(codex_proxy.subagent_semantic_repair_enabled({}))

    def test_raw_probe_disables_subagent_semantic_repair_even_in_assisted(self):
        with patch.dict(os.environ, {"CODEXHUB_SUBAGENT_ASSIST_MODE": "assisted"}, clear=False):
            self.assertFalse(codex_proxy.subagent_semantic_repair_enabled({"raw_provider_probe": True}))

    def test_gateway_auto_retry_settings_fall_back_to_runtime_settings_when_env_missing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            settings_path = os.path.join(temp_dir, "settings.json")
            with open(settings_path, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "gateway_auto_retry_enabled": False,
                        "gateway_auto_retry_max_attempts": 4,
                    },
                    handle,
                )

            with (
                patch.dict(os.environ, {}, clear=True),
                patch("codex_proxy.RUNTIME_PROXY_DIR", Path(temp_dir)),
            ):
                self.assertFalse(codex_proxy.gateway_auto_retry_enabled())
                self.assertEqual(codex_proxy.gateway_auto_retry_max_attempts(), 4)

    def test_gateway_timeout_settings_fall_back_to_runtime_settings_when_env_missing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            settings_path = os.path.join(temp_dir, "settings.json")
            with open(settings_path, "w", encoding="utf-8") as handle:
                json.dump({"gateway_request_timeout_seconds": 45}, handle)

            with (
                patch.dict(os.environ, {}, clear=True),
                patch("codex_proxy.RUNTIME_PROXY_DIR", Path(temp_dir)),
            ):
                self.assertEqual(upstream_timeout_seconds(), 45)

    def test_gateway_auto_retry_runtime_settings_override_stale_env(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            settings_path = os.path.join(temp_dir, "settings.json")
            with open(settings_path, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "gateway_auto_retry_enabled": False,
                        "gateway_auto_retry_max_attempts": 2,
                    },
                    handle,
                )

            with (
                patch.dict(
                    os.environ,
                    {
                        "CODEX_PROXY_AUTO_RETRY_ENABLED": "1",
                        "CODEX_PROXY_AUTO_RETRY_MAX_ATTEMPTS": "30",
                    },
                    clear=False,
                ),
                patch("codex_proxy.RUNTIME_PROXY_DIR", Path(temp_dir)),
            ):
                self.assertFalse(codex_proxy.gateway_auto_retry_enabled())
                self.assertEqual(codex_proxy.gateway_auto_retry_max_attempts(), 2)

    def test_gateway_retry_delay_caps_after_third_retry(self):
        self.assertEqual([codex_proxy.gateway_retry_delay_seconds(attempt) for attempt in range(1, 6)], [2, 4, 6, 8, 8])

    def test_retry_attempts_are_bounded_by_request_kind(self):
        with patch.dict(
            os.environ,
            {
                "CODEX_PROXY_AUTO_RETRY_ENABLED": "1",
                "CODEX_PROXY_AUTO_RETRY_MAX_ATTEMPTS": "30",
                "CODEX_PROXY_COMPACT_RETRY_MAX_ATTEMPTS": "3",
                "CODEX_PROXY_MAIN_GENERATION_RETRY_MAX_ATTEMPTS": "2",
            },
            clear=False,
        ):
            self.assertEqual(codex_proxy._upstream_retry_attempts(codex_proxy.RETRY_REQUEST_COMPACT), 3)
            self.assertEqual(codex_proxy._upstream_retry_attempts(codex_proxy.RETRY_REQUEST_MAIN_GENERATION), 2)
            self.assertEqual(codex_proxy._upstream_retry_attempts(codex_proxy.RETRY_REQUEST_IMAGE_PROXY_VISION), 1)

    def test_open_upstream_response_retries_http_errors_for_any_provider(self):
        request = codex_proxy.Request("https://ark.example.test/v1/responses", data=b"{}", method="POST")
        error = HTTPError(
            "https://ark.example.test/v1/responses",
            429,
            "Too Many Requests",
            {},
            io.BytesIO(b'{"error":"rate limited"}'),
        )
        success = FakeResponse(b'{"id":"resp_retry"}')

        with (
            patch.dict(
                os.environ,
                {
                    "CODEX_PROXY_AUTO_RETRY_ENABLED": "1",
                    "CODEX_PROXY_AUTO_RETRY_MAX_ATTEMPTS": "3",
                },
                clear=False,
            ),
            patch("codex_proxy.urlopen", side_effect=[error, success]) as mock_urlopen,
            patch("codex_proxy.time.sleep") as mock_sleep,
        ):
            response = codex_proxy._open_upstream_response(
                request,
                upstream_name="volcengine",
                upstream_format="responses",
                timeout=1,
                event_context={"request_id": "req_retry", "model": "volc/glm-5.2"},
            )

        self.assertIs(response, success)
        self.assertEqual(mock_urlopen.call_count, 2)
        mock_sleep.assert_called_once_with(2)
        retry_events = [
            call for call in self.write_proxy_event.call_args_list
            if call.args and call.args[0] == "upstream_retry"
        ]
        self.assertEqual(len(retry_events), 1)
        fields = retry_events[0].kwargs
        self.assertEqual(fields["request_id"], "req_retry")
        self.assertEqual(fields["model"], "volc/glm-5.2")
        self.assertEqual(fields["upstream"], "volcengine")
        self.assertEqual(fields["provider_id"], "volcengine")
        self.assertEqual(fields["status"], 429)
        self.assertEqual(fields["error"], "HTTPError")
        self.assertEqual(fields["attempt"], 1)
        self.assertEqual(fields["max_attempts"], 3)
        self.assertEqual(fields["delay_ms"], 2000)

    def test_open_upstream_response_does_not_retry_permanent_http_errors(self):
        request = codex_proxy.Request("https://ark.example.test/v1/responses", data=b"{}", method="POST")
        for status in (400, 401, 403, 404, 413, 415, 422, 501, 505):
            with self.subTest(status=status):
                self.write_proxy_event.reset_mock()
                error = HTTPError(
                    "https://ark.example.test/v1/responses",
                    status,
                    "Permanent Error",
                    {},
                    io.BytesIO(b'{"error":{"type":"invalid_request_error","message":"bad request"}}'),
                )

                with (
                    patch.dict(
                        os.environ,
                        {
                            "CODEX_PROXY_AUTO_RETRY_ENABLED": "1",
                            "CODEX_PROXY_AUTO_RETRY_MAX_ATTEMPTS": "3",
                        },
                        clear=False,
                    ),
                    patch("codex_proxy.urlopen", side_effect=error) as mock_urlopen,
                    patch("codex_proxy.time.sleep") as mock_sleep,
                ):
                    with self.assertRaises(HTTPError):
                        codex_proxy._open_upstream_response(
                            request,
                            upstream_name="volcengine",
                            upstream_format="responses",
                            timeout=1,
                            event_context={"request_id": "req_bad_request", "model": "volc/glm-5.2"},
                            request_kind="main_generation",
                        )

                self.assertEqual(mock_urlopen.call_count, 1)
                mock_sleep.assert_not_called()
                retry_events = [
                    call for call in self.write_proxy_event.call_args_list
                    if call.args and call.args[0] == "upstream_retry"
                ]
                self.assertEqual(retry_events, [])

    def test_open_upstream_response_retries_transient_http_errors(self):
        request = codex_proxy.Request("https://ark.example.test/v1/responses", data=b"{}", method="POST")
        for status in (408, 409, 421, 425, 429, 500, 502, 503, 504, 520):
            with self.subTest(status=status):
                self.write_proxy_event.reset_mock()
                error = HTTPError(
                    "https://ark.example.test/v1/responses",
                    status,
                    "Transient Error",
                    {},
                    io.BytesIO(b'{"error":{"type":"rate_limit_exceeded","message":"try later"}}'),
                )
                success = FakeResponse(b'{"id":"resp_retry"}')

                with (
                    patch.dict(
                        os.environ,
                        {
                            "CODEX_PROXY_AUTO_RETRY_ENABLED": "1",
                            "CODEX_PROXY_AUTO_RETRY_MAX_ATTEMPTS": "2",
                        },
                        clear=False,
                    ),
                    patch("codex_proxy.urlopen", side_effect=[error, success]) as mock_urlopen,
                    patch("codex_proxy.time.sleep") as mock_sleep,
                ):
                    response = codex_proxy._open_upstream_response(
                        request,
                        upstream_name="volcengine",
                        upstream_format="responses",
                        timeout=1,
                        event_context={"request_id": "req_transient", "model": "volc/glm-5.2"},
                        request_kind="main_generation",
                    )

                self.assertIs(response, success)
                self.assertEqual(mock_urlopen.call_count, 2)
                mock_sleep.assert_called_once_with(2)

    def test_open_upstream_response_respects_x_should_retry_headers(self):
        request = codex_proxy.Request("https://ark.example.test/v1/responses", data=b"{}", method="POST")
        forced_retry = HTTPError(
            "https://ark.example.test/v1/responses",
            400,
            "Bad Request",
            {"x-should-retry": "true"},
            io.BytesIO(b'{"error":{"type":"invalid_request_error"}}'),
        )
        forced_no_retry = HTTPError(
            "https://ark.example.test/v1/responses",
            503,
            "Unavailable",
            {"x-should-retry": "false"},
            io.BytesIO(b'{"error":{"type":"server_error"}}'),
        )
        success = FakeResponse(b'{"id":"resp_retry"}')

        with (
            patch.dict(
                os.environ,
                {
                    "CODEX_PROXY_AUTO_RETRY_ENABLED": "1",
                    "CODEX_PROXY_AUTO_RETRY_MAX_ATTEMPTS": "2",
                },
                clear=False,
            ),
            patch("codex_proxy.urlopen", side_effect=[forced_retry, success]) as mock_urlopen,
            patch("codex_proxy.time.sleep"),
        ):
            self.assertIs(
                codex_proxy._open_upstream_response(
                    request,
                    upstream_name="volcengine",
                    upstream_format="responses",
                    timeout=1,
                    request_kind="main_generation",
                ),
                success,
            )
        self.assertEqual(mock_urlopen.call_count, 2)

        with (
            patch.dict(
                os.environ,
                {
                    "CODEX_PROXY_AUTO_RETRY_ENABLED": "1",
                    "CODEX_PROXY_AUTO_RETRY_MAX_ATTEMPTS": "2",
                },
                clear=False,
            ),
            patch("codex_proxy.urlopen", side_effect=forced_no_retry) as mock_urlopen,
            patch("codex_proxy.time.sleep") as mock_sleep,
        ):
            with self.assertRaises(HTTPError):
                codex_proxy._open_upstream_response(
                    request,
                    upstream_name="volcengine",
                    upstream_format="responses",
                    timeout=1,
                    request_kind="main_generation",
                )
        self.assertEqual(mock_urlopen.call_count, 1)
        mock_sleep.assert_not_called()

    def test_open_upstream_response_does_not_retry_quota_or_context_errors(self):
        request = codex_proxy.Request("https://ark.example.test/v1/responses", data=b"{}", method="POST")
        cases = [
            (429, b'{"error":{"type":"insufficient_quota","code":"insufficient_quota"}}'),
            (400, b'{"error":{"type":"context_length_exceeded","code":"context_length_exceeded"}}'),
            (422, b'{"error":{"type":"invalid_request_error","code":"invalid_image"}}'),
        ]
        for status, body in cases:
            with self.subTest(status=status, body=body):
                error = HTTPError(
                    "https://ark.example.test/v1/responses",
                    status,
                    "Permanent Error",
                    {},
                    io.BytesIO(body),
                )
                with (
                    patch.dict(
                        os.environ,
                        {
                            "CODEX_PROXY_AUTO_RETRY_ENABLED": "1",
                            "CODEX_PROXY_AUTO_RETRY_MAX_ATTEMPTS": "3",
                        },
                        clear=False,
                    ),
                    patch("codex_proxy.urlopen", side_effect=error) as mock_urlopen,
                    patch("codex_proxy.time.sleep") as mock_sleep,
                ):
                    with self.assertRaises(HTTPError):
                        codex_proxy._open_upstream_response(
                            request,
                            upstream_name="volcengine",
                            upstream_format="responses",
                            timeout=1,
                            request_kind="image_proxy_vision",
                        )
                self.assertEqual(mock_urlopen.call_count, 1)
                mock_sleep.assert_not_called()

    def test_open_upstream_response_does_not_retry_when_auto_retry_disabled(self):
        request = codex_proxy.Request("https://ark.example.test/v1/responses", data=b"{}", method="POST")
        error = HTTPError(
            "https://ark.example.test/v1/responses",
            503,
            "Unavailable",
            {},
            io.BytesIO(b'{"error":"down"}'),
        )

        with (
            patch.dict(
                os.environ,
                {
                    "CODEX_PROXY_AUTO_RETRY_ENABLED": "0",
                    "CODEX_PROXY_AUTO_RETRY_MAX_ATTEMPTS": "3",
                },
                clear=False,
            ),
            patch("codex_proxy.urlopen", side_effect=error) as mock_urlopen,
            patch("codex_proxy.time.sleep") as mock_sleep,
        ):
            with self.assertRaises(HTTPError):
                codex_proxy._open_upstream_response(
                    request,
                    upstream_name="volcengine",
                    upstream_format="responses",
                    timeout=1,
                    event_context={"request_id": "req_retry", "model": "volc/glm-5.2"},
                )

        self.assertEqual(mock_urlopen.call_count, 1)
        mock_sleep.assert_not_called()

    def test_post_responses_streaming_emits_downstream_retry_notice_before_success(self):
        body = json.dumps(
            {
                "model": "volc/glm-5.2",
                "input": [{"type": "message", "role": "user", "content": "hi"}],
                "stream": True,
            }
        ).encode("utf-8")
        handler = CodexProxyHandler.__new__(CodexProxyHandler)
        handler.path = "/v1/responses"
        handler.headers = {
            "Content-Length": str(len(body)),
            "Content-Type": "application/json",
        }
        handler.rfile = io.BytesIO(body)
        handler.close_connection = False
        fake = FakeHandler()
        handler.send_response = fake.send_response
        handler.send_header = fake.send_header
        handler.end_headers = fake.end_headers
        handler.wfile = fake.wfile
        error = HTTPError(
            "https://ark.example.test/v1/responses",
            503,
            "Service Unavailable",
            {},
            io.BytesIO(b'{"error":{"type":"server_error","message":"try later"}}'),
        )
        success = FakeSseResponse(
            [
                b'data: {"type":"response.created","response":{"id":"resp_retry","status":"in_progress"}}\n\n',
                b'data: {"type":"response.output_text.delta","delta":"ok"}\n\n',
                b'data: {"type":"response.completed","response":{"status":"completed","usage":{"input_tokens":1,"output_tokens":1}}}\n\n',
                b"",
            ]
        )

        with (
            patch.dict(
                os.environ,
                {
                    "CODEX_PROXY_AUTO_RETRY_ENABLED": "1",
                    "CODEX_PROXY_AUTO_RETRY_MAX_ATTEMPTS": "2",
                },
                clear=False,
            ),
            patch("codex_proxy.urlopen", side_effect=[error, success]) as mock_urlopen,
            patch("codex_proxy.time.sleep") as mock_sleep,
        ):
            CodexProxyHandler.do_POST(handler)

        self.assertEqual(mock_urlopen.call_count, 2)
        mock_sleep.assert_called_once_with(2)
        self.assertEqual(fake.status, 200)
        written = b"".join(fake.wfile.writes)
        retry_index = written.index(b"event: codexhub.retry\n")
        model_index = written.index(b"response.output_text.delta")
        self.assertLess(retry_index, model_index)
        self.assertIn(b'"request_kind":"main_generation"', written)
        notice_events = [
            call.kwargs for call in self.write_proxy_event.call_args_list
            if call.args and call.args[0] == "sse_retry_notice"
        ]
        self.assertEqual(len(notice_events), 1)
        self.assertEqual(notice_events[0]["request_kind"], "main_generation")
        self.assertEqual(notice_events[0]["status"], 503)
        self.assertEqual(notice_events[0]["attempt"], 1)
        self.assertEqual(notice_events[0]["max_attempts"], 2)

    def test_post_responses_error_event_uses_proxy_request_kind(self):
        body = json.dumps(
            {
                "model": "volc/glm-5.2",
                "input": [{"type": "message", "role": "user", "content": "hi"}],
                "stream": False,
            }
        ).encode("utf-8")
        handler = CodexProxyHandler.__new__(CodexProxyHandler)
        handler.path = "/v1/responses"
        handler.headers = {
            "Content-Length": str(len(body)),
            "Content-Type": "application/json",
            "x-codex-client-metadata": json.dumps({
                "request_kind": "turn",
                "turn_id": "turn-error",
            }),
        }
        handler.rfile = io.BytesIO(body)
        handler.close_connection = False
        fake = FakeHandler()
        handler.send_response = fake.send_response
        handler.send_header = fake.send_header
        handler.end_headers = fake.end_headers
        handler.wfile = fake.wfile
        error = HTTPError(
            "https://ark.example.test/v1/responses",
            400,
            "Bad Request",
            {"Content-Type": "application/json"},
            io.BytesIO(b'{"error":{"type":"invalid_request_error","message":"bad request"}}'),
        )

        with (
            patch.dict(os.environ, {"CODEX_PROXY_AUTO_RETRY_ENABLED": "1"}, clear=False),
            patch("codex_proxy.urlopen", side_effect=error) as mock_urlopen,
        ):
            CodexProxyHandler.do_POST(handler)

        self.assertEqual(mock_urlopen.call_count, 1)
        error_events = [
            call.kwargs for call in self.write_proxy_event.call_args_list
            if call.args and call.args[0] == "request_error"
        ]
        self.assertEqual(len(error_events), 1)
        self.assertEqual(error_events[0]["request_kind"], "main_generation")
        self.assertEqual(error_events[0]["client_request_kind"], "turn")
        self.assertEqual(error_events[0]["turn_id"], "turn-error")
        self.assertEqual(error_events[0]["status"], 400)
        retry_events = [
            call for call in self.write_proxy_event.call_args_list
            if call.args and call.args[0] == "upstream_retry"
        ]
        self.assertEqual(retry_events, [])

    def test_official_control_events_use_official_request_kind(self):
        handler = CodexProxyHandler.__new__(CodexProxyHandler)
        handler.path = "/v1/responses/resp_control"
        handler.headers = {
            "x-codex-client-metadata": json.dumps({
                "request_kind": "poll",
                "thread_id": "thread-control",
            }),
        }
        handler.close_connection = False
        fake = FakeHandler()
        handler.send_response = fake.send_response
        handler.send_header = fake.send_header
        handler.end_headers = fake.end_headers
        handler.wfile = fake.wfile

        with (
            patch("codex_proxy.codex_access_token", return_value="sub-token"),
            patch("codex_proxy.codex_account_id", return_value="acct-1"),
            patch("codex_proxy.urlopen", return_value=FakeContextResponse(b'{"id":"resp_control"}')),
        ):
            CodexProxyHandler.do_GET(handler)

        events = [(call.args[0], call.kwargs) for call in self.write_proxy_event.call_args_list]
        request_start = next(fields for event, fields in events if event == "request_start")
        request_complete = next(fields for event, fields in events if event == "request_complete")
        for fields in (request_start, request_complete):
            self.assertEqual(fields["request_kind"], "official_control")
            self.assertEqual(fields["client_request_kind"], "poll")
            self.assertEqual(fields["thread_id"], "thread-control")
            self.assertEqual(fields["route_reason"], "official_control")

    def test_official_control_error_event_uses_official_request_kind(self):
        handler = CodexProxyHandler.__new__(CodexProxyHandler)
        handler.path = "/v1/responses/resp_control"
        handler.headers = {
            "x-codex-client-metadata": json.dumps({
                "request_kind": "poll",
                "thread_id": "thread-control",
            }),
        }
        handler.close_connection = False
        fake = FakeHandler()
        handler.send_response = fake.send_response
        handler.send_header = fake.send_header
        handler.end_headers = fake.end_headers
        handler.wfile = fake.wfile
        error = HTTPError(
            "https://chatgpt.com/backend-api/codex/responses/resp_control",
            502,
            "Bad Gateway",
            {"Content-Type": "application/json"},
            io.BytesIO(b'{"error":{"type":"server_error","message":"bad gateway"}}'),
        )

        with (
            patch.dict(os.environ, {"CODEX_PROXY_AUTO_RETRY_ENABLED": "0"}, clear=False),
            patch("codex_proxy.codex_access_token", return_value="sub-token"),
            patch("codex_proxy.codex_account_id", return_value="acct-1"),
            patch("codex_proxy.urlopen", side_effect=error),
        ):
            CodexProxyHandler.do_GET(handler)

        error_events = [
            call.kwargs for call in self.write_proxy_event.call_args_list
            if call.args and call.args[0] == "request_error"
        ]
        self.assertEqual(len(error_events), 1)
        self.assertEqual(error_events[0]["request_kind"], "official_control")
        self.assertEqual(error_events[0]["client_request_kind"], "poll")
        self.assertEqual(error_events[0]["thread_id"], "thread-control")
        self.assertEqual(error_events[0]["route_reason"], "official_control")
        self.assertEqual(error_events[0]["status"], 502)

    def test_image_proxy_replaces_images_for_text_only_target_model(self):
        payload = {
            "model": "volc/glm-5.2",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "What is this?"},
                        {"type": "input_image", "image_url": "data:image/png;base64,abc"},
                    ],
                }
            ],
            "stream": False,
        }
        upstream = choose_upstream("volc/glm-5.2")

        with (
            patch.dict(
                os.environ,
                {
                    "CODEX_PROXY_IMAGE_PROXY_ENABLED": "1",
                    "CODEX_PROXY_IMAGE_PROXY_MODEL": "minimax-cn/MiniMax-M3",
                },
                clear=False,
            ),
            patch("codex_proxy._image_proxy_description_for_part", return_value="A blue chart.") as describe,
        ):
            changed = codex_proxy.apply_image_proxy_to_responses_payload(
                payload,
                "volc/glm-5.2",
                upstream,
                event_context={"request_id": "req_img"},
            )

        self.assertTrue(changed)
        content = payload["input"][0]["content"]
        self.assertEqual(content[1]["type"], "input_text")
        self.assertIn("A blue chart.", content[1]["text"])
        self.assertIn("The Gateway has already read the user's attached image", content[1]["text"])
        self.assertIn("Use the visual context below as the image content", content[1]["text"])
        self.assertIn("Do not mention the Gateway, preprocessing, replacement", content[1]["text"])
        self.assertNotIn("replaced the original image", content[1]["text"])
        self.assertNotIn("I only received", content[1]["text"])
        self.assertNotIn("image_url", content[1])
        describe.assert_called_once()

    def test_image_proxy_settings_fall_back_to_runtime_settings_when_env_missing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            settings_path = os.path.join(temp_dir, "settings.json")
            with open(settings_path, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "gateway_image_proxy_enabled": True,
                        "gateway_image_proxy_model": "  kimi-k2.6  ",
                    },
                    handle,
                )

            with (
                patch.dict(
                    os.environ,
                    {
                        "CODEX_PROXY_IMAGE_PROXY_ENABLED": "",
                        "CODEX_PROXY_IMAGE_PROXY_MODEL": "",
                    },
                    clear=False,
                ),
                patch("codex_proxy.RUNTIME_PROXY_DIR", Path(temp_dir)),
            ):
                os.environ.pop("CODEX_PROXY_IMAGE_PROXY_ENABLED", None)
                os.environ.pop("CODEX_PROXY_IMAGE_PROXY_MODEL", None)
                self.assertTrue(codex_proxy.gateway_image_proxy_enabled())
                self.assertEqual(codex_proxy.gateway_image_proxy_model(), "kimi-k2.6")

    def test_image_proxy_runtime_settings_override_stale_env(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            settings_path = os.path.join(temp_dir, "settings.json")
            with open(settings_path, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "gateway_image_proxy_enabled": True,
                        "gateway_image_proxy_model": "kimi-k2.6",
                    },
                    handle,
                )

            with (
                patch.dict(
                    os.environ,
                    {
                        "CODEX_PROXY_IMAGE_PROXY_ENABLED": "0",
                        "CODEX_PROXY_IMAGE_PROXY_MODEL": "minimax-cn/MiniMax-M3",
                    },
                    clear=False,
                ),
                patch("codex_proxy.RUNTIME_PROXY_DIR", Path(temp_dir)),
            ):
                self.assertTrue(codex_proxy.gateway_image_proxy_enabled())
                self.assertEqual(codex_proxy.gateway_image_proxy_model(), "kimi-k2.6")

    def test_image_proxy_skips_target_model_that_supports_images(self):
        payload = {
            "model": "minimax-cn/MiniMax-M3",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_image", "image_url": "data:image/png;base64,abc"}],
                }
            ],
        }
        upstream = choose_upstream("minimax-cn/MiniMax-M3")

        with (
            patch.dict(
                os.environ,
                {
                    "CODEX_PROXY_IMAGE_PROXY_ENABLED": "1",
                    "CODEX_PROXY_IMAGE_PROXY_MODEL": "minimax-cn/MiniMax-M3",
                },
                clear=False,
            ),
            patch("codex_proxy._image_proxy_description_for_part") as describe,
        ):
            changed = codex_proxy.apply_image_proxy_to_responses_payload(
                payload,
                "minimax-cn/MiniMax-M3",
                upstream,
                event_context={"request_id": "req_img"},
            )

        self.assertFalse(changed)
        self.assertEqual(payload["input"][0]["content"][0]["type"], "input_image")
        describe.assert_not_called()

    def test_image_proxy_requires_configured_vision_model_for_image_requests(self):
        payload = {
            "model": "volc/glm-5.2",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_image", "image_url": "data:image/png;base64,abc"}],
                }
            ],
        }
        upstream = choose_upstream("volc/glm-5.2")

        with patch.dict(os.environ, {"CODEX_PROXY_IMAGE_PROXY_ENABLED": "1", "CODEX_PROXY_IMAGE_PROXY_MODEL": ""}, clear=False):
            with self.assertRaises(codex_proxy.ImageProxyError) as context:
                codex_proxy.apply_image_proxy_to_responses_payload(
                    payload,
                    "volc/glm-5.2",
                    upstream,
                    event_context={"request_id": "req_img"},
                )

        self.assertIn("Vision model is not configured", str(context.exception))

    def test_image_proxy_vision_request_does_not_inject_codex_tools(self):
        response_body = json.dumps(
            {
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "A small attachment thumbnail."}],
                    }
                ]
            }
        ).encode("utf-8")
        upstream = choose_upstream("minimax-cn/MiniMax-M3")

        with (
            patch.dict(os.environ, {"CODEX_PROXY_AUTO_RETRY_ENABLED": "0"}, clear=False),
            patch("codex_proxy.urlopen", return_value=FakeContextResponse(response_body)) as mock_urlopen,
        ):
            description = codex_proxy._call_vision_model_for_image_description(
                {"type": "input_image", "image_url": "data:image/png;base64,abc"},
                "minimax-cn/MiniMax-M3",
                upstream,
                event_context={"request_id": "req_img", "image_proxy": True},
            )

        self.assertEqual(description, "A small attachment thumbnail.")
        request = mock_urlopen.call_args.args[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["model"], "MiniMax-M3")
        self.assertNotIn("tools", payload)
        self.assertNotIn("tool_choice", payload)

    def test_image_proxy_vision_request_strips_tools_even_if_adapter_adds_them(self):
        part = {"type": "input_image", "image_url": "data:image/png;base64,AAAA"}
        vision_upstream = {
            "name": "vision",
            "base_url": "https://vision.example.test/v1",
            "api_key": "vision-token",
            "auth": "api_key",
            "upstream_format": "responses",
            "upstream_model": "vision-model",
        }
        response_body = json.dumps(
            {
                "id": "resp_vision",
                "object": "response",
                "status": "completed",
                "model": "vision-model",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "A chart."}],
                    }
                ],
            }
        ).encode("utf-8")

        def add_tools(body, upstream, **kwargs):
            payload = json.loads(body.decode("utf-8"))
            payload["tools"] = [{"type": "function", "name": "unexpected_tool", "parameters": {"type": "object"}}]
            payload["tool_choice"] = "auto"
            return json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")

        with (
            patch("codex_proxy.compatible_request_body", side_effect=add_tools),
            patch("codex_proxy.urlopen", return_value=FakeContextResponse(response_body)) as mock_urlopen,
        ):
            description = codex_proxy._call_vision_model_for_image_description(
                part,
                "vision-model",
                vision_upstream,
                event_context={"request_id": "req_img", "image_proxy": True},
            )

        self.assertEqual(description, "A chart.")
        payload = json.loads(mock_urlopen.call_args.args[0].data)
        self.assertNotIn("tools", payload)
        self.assertNotIn("tool_choice", payload)

    def test_image_proxy_vision_request_logs_duration_and_usage(self):
        response_body = json.dumps(
            {
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "A small attachment thumbnail."}],
                    }
                ],
                "usage": {
                    "input_tokens": 11,
                    "output_tokens": 7,
                    "total_tokens": 18,
                    "output_tokens_details": {"reasoning_tokens": 3},
                },
            }
        ).encode("utf-8")
        upstream = choose_upstream("minimax-cn/MiniMax-M3")
        events: list[tuple[str, dict[str, object]]] = []

        def capture_event(event: str, **fields: object) -> None:
            events.append((event, fields))

        with (
            patch.dict(os.environ, {"CODEX_PROXY_AUTO_RETRY_ENABLED": "0"}, clear=False),
            patch("codex_proxy.urlopen", return_value=FakeContextResponse(response_body)),
            patch("codex_proxy.write_proxy_event", side_effect=capture_event),
        ):
            description = codex_proxy._call_vision_model_for_image_description(
                {"type": "input_image", "image_url": "data:image/png;base64,abc"},
                "minimax-cn/MiniMax-M3",
                upstream,
                event_context={"request_id": "req_img", "image_proxy": True},
            )

        self.assertEqual(description, "A small attachment thumbnail.")
        event_names = [name for name, _ in events]
        self.assertIn("image_proxy_vision_request_start", event_names)
        self.assertIn("image_proxy_vision_request_complete", event_names)
        complete_fields = dict(events[event_names.index("image_proxy_vision_request_complete")][1])
        self.assertEqual(complete_fields["request_id"], "req_img")
        self.assertEqual(complete_fields["vision_model"], "minimax-cn/MiniMax-M3")
        self.assertEqual(complete_fields["upstream"], upstream["name"])
        self.assertEqual(complete_fields["description_length"], len("A small attachment thumbnail."))
        self.assertGreaterEqual(complete_fields["duration_ms"], 0)
        self.assertEqual(complete_fields["usage_input_tokens"], 11)
        self.assertEqual(complete_fields["usage_output_tokens"], 7)
        self.assertEqual(complete_fields["usage_reasoning_tokens"], 3)

    def test_image_proxy_vision_request_does_not_inherit_gateway_auto_retry(self):
        upstream = choose_upstream("minimax-cn/MiniMax-M3")
        events: list[tuple[str, dict[str, object]]] = []
        upstream_error = HTTPError(
            "https://vision.example.test/v1/responses",
            500,
            "Internal Server Error",
            {},
            io.BytesIO(b"server error"),
        )

        def capture_event(event: str, **fields: object) -> None:
            events.append((event, fields))

        with (
            patch.dict(
                os.environ,
                {
                    "CODEX_PROXY_AUTO_RETRY_ENABLED": "1",
                    "CODEX_PROXY_AUTO_RETRY_MAX_ATTEMPTS": "30",
                },
                clear=False,
            ),
            patch("codex_proxy.urlopen", side_effect=upstream_error) as mock_urlopen,
            patch("codex_proxy.write_proxy_event", side_effect=capture_event),
            patch("codex_proxy.time.sleep"),
        ):
            with self.assertRaises(HTTPError):
                codex_proxy._call_vision_model_for_image_description(
                    {"type": "input_image", "image_url": "data:image/png;base64,abc"},
                    "minimax-cn/MiniMax-M3",
                    upstream,
                    event_context={"request_id": "req_img", "image_proxy": True},
                )

        self.assertEqual(mock_urlopen.call_count, 1)
        event_names = [name for name, _ in events]
        self.assertNotIn("upstream_retry", event_names)
        self.assertIn("image_proxy_vision_request_error", event_names)

    def test_image_proxy_cache_hit_avoids_repeated_vision_call_and_raw_image_storage(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_path = os.path.join(temp_dir, "image-proxy-cache.sqlite")
            part = {"type": "input_image", "image_url": "data:image/png;base64,abc"}
            upstream = choose_upstream("minimax-cn/MiniMax-M3")
            with (
                patch("codex_proxy.IMAGE_PROXY_CACHE_PATH", cache_path),
                patch("codex_proxy._call_vision_model_for_image_description", return_value="Cached description") as vision_call,
            ):
                first = codex_proxy._image_proxy_description_for_part(
                    part,
                    "minimax-cn/MiniMax-M3",
                    upstream,
                    event_context={"request_id": "req_img"},
                )
                second = codex_proxy._image_proxy_description_for_part(
                    part,
                    "minimax-cn/MiniMax-M3",
                    upstream,
                    event_context={"request_id": "req_img"},
                )

            self.assertEqual(first, "Cached description")
            self.assertEqual(second, "Cached description")
            vision_call.assert_called_once()
            with open(cache_path, "rb") as handle:
                self.assertNotIn(b"data:image/png;base64,abc", handle.read())

    def test_gpt_routes_to_official(self):
        upstream = choose_upstream("gpt-5.5")
        self.assertEqual(upstream["name"], "official")
        self.assertEqual(upstream["auth"], "codex_auth")

    def test_openai_alias_routes_to_official_and_rewrites_upstream_model(self):
        upstream = choose_upstream("openai/gpt-5.5")
        self.assertEqual(upstream["name"], "official")
        self.assertEqual(upstream["auth"], "codex_auth")
        self.assertEqual(upstream["upstream_model"], "gpt-5.5")

        body = compatible_request_body(
            b'{"model":"openai/gpt-5.5","input":"hi"}',
            upstream,
            "openai/gpt-5.5",
        )

        self.assertEqual(json.loads(body)["model"], "gpt-5.5")

    def test_responses_to_chat_completion_body_preserves_input_images(self):
        body = json.dumps(
            {
                "model": "minimax-cn/MiniMax-M3",
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "Describe this"},
                            {"type": "input_image", "image_url": "data:image/png;base64,abc"},
                        ],
                    }
                ],
                "stream": False,
            }
        ).encode("utf-8")

        payload = json.loads(_responses_request_to_chat_completion_body(body))

        content = payload["messages"][0]["content"]
        self.assertEqual(
            content,
            [
                {"type": "text", "text": "Describe this"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
            ],
        )

    def test_denied_openai_alias_is_rejected_even_when_bare_model_allowed(self):
        policy = codex_proxy.load_policy(codex_proxy.POLICY_PATH)
        policy = replace(
            policy,
            denied_models=set(policy.denied_models) | {"openai/gpt-5.5"},
        )

        with patch("codex_proxy.load_policy", return_value=policy):
            with self.assertRaises(ValueError) as context:
                choose_upstream("openai/gpt-5.5")

        self.assertIn("model is not allowed", str(context.exception))

    def test_ollama_routes_to_cloud(self):
        upstream = choose_upstream("glm-5.2")
        self.assertEqual(upstream["name"], "ollama_cloud")
        self.assertEqual(upstream["auth"], "ollama_api_key")
        self.assertEqual(upstream["base_url"], "https://ollama.com/v1")

    def test_ollama_provider_prefixed_model_routes_to_cloud_and_rewrites_body(self):
        upstream = choose_upstream("ollama-cloud/glm-5.2")

        self.assertEqual(upstream["name"], "ollama_cloud")
        self.assertEqual(upstream["auth"], "ollama_api_key")
        self.assertEqual(upstream["upstream_model"], "glm-5.2")

        transformed = compatible_request_body(
            b'{"model":"ollama-cloud/glm-5.2","input":"hi"}',
            upstream,
            "ollama-cloud/glm-5.2",
        )

        self.assertEqual(json.loads(transformed)["model"], "glm-5.2")

    def test_provider_prefixed_model_routes_to_external_provider(self):
        self.external_model["upstream_format"] = "chat_completions"
        self.external_model["tool_protocol"] = "text_compat"
        upstream = choose_upstream("volc/glm-5.2")
        self.assertEqual(upstream["name"], "volcengine")
        self.assertEqual(upstream["auth"], "api_key")
        self.assertEqual(upstream["base_url"], "https://ark.example.test/v1")
        self.assertEqual(upstream["upstream_model"], "glm-5.2")
        self.assertEqual(upstream["upstream_format"], "chat_completions")
        self.assertEqual(upstream["tool_protocol"], "text_compat")

    def test_provider_scoped_short_model_routes_to_external_provider(self):
        route_model = codex_proxy.provider_scoped_route_model("glm-5.2", "volc")

        self.assertEqual(route_model, "volc/glm-5.2")
        upstream = choose_upstream(route_model)
        self.assertEqual(upstream["name"], "volcengine")
        self.assertEqual(upstream["upstream_model"], "glm-5.2")

    def test_provider_scoped_short_model_preserves_exact_case(self):
        route_model = codex_proxy.provider_scoped_route_model("MiniMax-M3", "minimax-cn")

        self.assertEqual(route_model, "minimax-cn/MiniMax-M3")
        upstream = choose_upstream(route_model)
        self.assertEqual(upstream["name"], "minimax_cn")
        self.assertEqual(upstream["upstream_model"], "MiniMax-M3")

    def test_provider_scoped_slash_model_is_provider_relative(self):
        route_model = codex_proxy.provider_scoped_route_model("anthropic/claude-sonnet-4", "openrouter")

        self.assertEqual(route_model, "openrouter/anthropic/claude-sonnet-4")

    def test_provider_scoped_canonical_model_for_same_provider_is_preserved(self):
        route_model = codex_proxy.provider_scoped_route_model("minimax-cn/MiniMax-M3", "minimax-cn")

        self.assertEqual(route_model, "minimax-cn/MiniMax-M3")

    def test_provider_scoped_path_extracts_provider(self):
        self.assertEqual(
            codex_proxy.provider_scoped_path("/v1/providers/minimax-cn/chat/completions", "chat/completions"),
            "minimax-cn",
        )
        self.assertEqual(
            codex_proxy.provider_scoped_path("/v1/providers/volc/responses", "responses"),
            "volc",
        )
        self.assertEqual(
            codex_proxy.provider_scoped_path(
                "/v1/providers/odd%2Fprovider%3Fx%23frag%20%25/chat/completions",
                "chat/completions",
            ),
            "odd/provider?x#frag %",
        )
        self.assertIsNone(codex_proxy.provider_scoped_path("/v1/chat/completions", "chat/completions"))

    def test_external_provider_model_routes_with_exact_case(self):
        upstream = choose_upstream("minimax-cn/MiniMax-M3")

        self.assertEqual(upstream["name"], "minimax_cn")
        self.assertEqual(upstream["auth"], "api_key")
        self.assertEqual(upstream["upstream_model"], "MiniMax-M3")

    def test_external_provider_explicit_alias_routes_without_lowercasing(self):
        upstream = choose_upstream("minimax-cn/minimax-m3")

        self.assertEqual(upstream["name"], "minimax_cn")
        self.assertEqual(upstream["upstream_model"], "MiniMax-M3")

    def test_denied_provider_qualified_ollama_alias_is_rejected(self):
        policy = codex_proxy.load_policy(codex_proxy.POLICY_PATH)
        policy = replace(
            policy,
            denied_models=set(policy.denied_models) | {"ollama-cloud/glm-5.2"},
        )

        with patch("codex_proxy.load_policy", return_value=policy):
            with self.assertRaises(ValueError) as context:
                choose_upstream("ollama-cloud/glm-5.2")

        self.assertIn("model is not allowed", str(context.exception))

    def test_denied_bare_ollama_target_rejects_provider_qualified_alias(self):
        policy = codex_proxy.load_policy(codex_proxy.POLICY_PATH)
        policy = replace(
            policy,
            denied_models=set(policy.denied_models) | {"glm-5.2"},
            allowed_ollama_cloud_models=policy.allowed_ollama_cloud_models + ("ollama-cloud/glm-5.2",),
        )

        with patch("codex_proxy.load_policy", return_value=policy):
            with self.assertRaises(ValueError) as context:
                choose_upstream("ollama-cloud/glm-5.2")

        self.assertIn("model is not allowed", str(context.exception))

    def test_denied_external_compatibility_alias_is_rejected(self):
        self.minimax_external_model["matched_alias"] = "minimax-cn/minimax-m3"
        policy = codex_proxy.load_policy(codex_proxy.POLICY_PATH)
        policy = replace(
            policy,
            denied_models=set(policy.denied_models) | {"minimax-cn/minimax-m3"},
        )

        with patch("codex_proxy.load_policy", return_value=policy):
            with self.assertRaises(ValueError) as context:
                choose_upstream("minimax-cn/minimax-m3")

        self.assertIn("model is not allowed", str(context.exception))

    def test_provider_qualified_ollama_alias_requires_generated_catalog_entry(self):
        with patch("codex_proxy.generated_catalog_slugs", return_value=set()):
            with self.assertRaises(ValueError) as context:
                choose_upstream("ollama-cloud/glm-5.2")

        self.assertIn("generated cloud catalog", str(context.exception))

    def test_external_provider_unknown_case_is_rejected(self):
        with self.assertRaises(ValueError):
            choose_upstream("minimax-cn/MINIMAX-M3")

    def test_provider_prefixed_model_does_not_fall_back_to_ollama(self):
        with patch("codex_proxy.resolve_external_model_alias", return_value=None):
            with self.assertRaises(ValueError) as context:
                choose_upstream("volc/glm-5.2")

        self.assertIn("external provider model is not configured", str(context.exception))

    def test_denied_model_is_rejected(self):
        with self.assertRaises(ValueError):
            choose_upstream("glm-5.1")

    def test_unknown_non_gpt_model_is_rejected(self):
        with self.assertRaises(ValueError):
            choose_upstream("not-a-real-cloud-model")

    def test_non_allowed_cloud_model_is_rejected(self):
        with self.assertRaises(ValueError):
            choose_upstream("gemma3:12b")

    def test_extract_model(self):
        self.assertEqual(extract_model(b'{"model":"glm-5.2","input":"hi"}'), "glm-5.2")

    def test_try_extract_model_from_multipart_json_part(self):
        body = (
            b"--boundary\r\n"
            b'Content-Disposition: form-data; name="payload"\r\n'
            b"Content-Type: application/json\r\n\r\n"
            b'{"model":"glm-5.2","input":"hi"}\r\n'
            b"--boundary--\r\n"
        )

        self.assertEqual(try_extract_model(body), "glm-5.2")
        self.assertEqual(extract_model(body), "glm-5.2")

    def test_try_extract_model_from_form_model_field(self):
        body = (
            b"--boundary\r\n"
            b'Content-Disposition: form-data; name="model"\r\n\r\n'
            b"volc/glm-5.2\r\n"
            b"--boundary--\r\n"
        )

        self.assertEqual(try_extract_model(body), "volc/glm-5.2")

    def test_try_extract_model_from_zstd_encoded_json(self):
        if codex_proxy.zstandard is None:
            self.skipTest("zstandard module is not installed")
        body = codex_proxy.zstandard.ZstdCompressor().compress(b'{"model":"glm-5.2","input":"hi"}')

        self.assertEqual(try_extract_model(body, "zstd"), "glm-5.2")
        decoded, content_decoded, decode_error = decoded_request_body(body, "zstd")
        self.assertTrue(content_decoded)
        self.assertIsNone(decode_error)
        self.assertEqual(json.loads(decoded)["model"], "glm-5.2")

    def test_try_extract_model_from_zstd_without_content_size(self):
        if codex_proxy.zstandard is None:
            self.skipTest("zstandard module is not installed")
        body = codex_proxy.zstandard.ZstdCompressor(write_content_size=False).compress(
            b'{"model":"glm-5.2","input":"hi"}'
        )

        self.assertEqual(try_extract_model(body, "zstd"), "glm-5.2")
        decoded, content_decoded, decode_error = decoded_request_body(body, "zstd")
        self.assertTrue(content_decoded)
        self.assertIsNone(decode_error)
        self.assertEqual(json.loads(decoded)["model"], "glm-5.2")

    def test_request_context_from_direct_headers(self):
        context = request_context_from_headers(
            {
                "X-Codex-Turn-Id": "turn-123",
                "X-Codex-Thread-Id": "thread-123",
                "X-Codex-Window-Id": "window-123",
            }
        )

        self.assertEqual(context["turn_id"], "turn-123")
        self.assertEqual(context["thread_id"], "thread-123")
        self.assertEqual(context["window_id"], "window-123")

    def test_request_context_from_metadata_header(self):
        context = request_context_from_headers(
            {
                "x-codex-client-metadata": json.dumps(
                    {
                        "turn_id": "turn-meta",
                        "thread_id": "thread-meta",
                        "request_kind": "turn",
                    }
                )
            }
        )

        self.assertEqual(context["turn_id"], "turn-meta")
        self.assertEqual(context["thread_id"], "thread-meta")
        self.assertEqual(context["request_kind"], "turn")

    def test_event_context_with_request_kind_preserves_client_request_kind(self):
        context = {
            "request_kind": "turn",
            "turn_id": "turn-meta",
        }

        event_context = codex_proxy._event_context_with_request_kind(
            context,
            codex_proxy.RETRY_REQUEST_MAIN_GENERATION,
        )

        self.assertEqual(event_context["request_kind"], "main_generation")
        self.assertEqual(event_context["client_request_kind"], "turn")
        self.assertEqual(event_context["turn_id"], "turn-meta")
        self.assertEqual(context["request_kind"], "turn")

    def test_upstream_timeout_defaults_and_env_override(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(upstream_timeout_seconds(), 300)
        with patch.dict(os.environ, {"CODEX_PROXY_UPSTREAM_TIMEOUT_SECONDS": "30"}, clear=True):
            self.assertEqual(upstream_timeout_seconds(), 30)
        with patch.dict(os.environ, {"CODEX_PROXY_UPSTREAM_TIMEOUT_SECONDS": "bad"}, clear=True):
            self.assertEqual(upstream_timeout_seconds(), 300)

    def test_ollama_body_maps_xhigh_reasoning_effort_to_max(self):
        upstream = choose_upstream("glm-5.2")
        body = b'{"model":"glm-5.2","reasoning":{"effort":"xhigh"},"input":"hi"}'

        transformed = compatible_request_body(body, upstream)

        self.assertEqual(json.loads(transformed)["reasoning"]["effort"], "max")

    def test_ollama_body_maps_xhigh_string_reasoning_to_max(self):
        upstream = choose_upstream("glm-5.2")
        body = b'{"model":"glm-5.2","reasoning":"xhigh","input":"hi"}'

        transformed = compatible_request_body(body, upstream)

        self.assertEqual(json.loads(transformed)["reasoning"], "max")

    def test_ollama_body_adds_catalog_max_output_tokens(self):
        upstream = choose_upstream("glm-5.2")
        body = b'{"model":"glm-5.2","input":"hi"}'

        transformed = compatible_request_body(body, upstream)

        self.assertEqual(json.loads(transformed)["max_output_tokens"], 131072)

    def test_ollama_body_clamps_catalog_max_output_tokens(self):
        upstream = choose_upstream("gemini-3-flash-preview")
        body = b'{"model":"gemini-3-flash-preview","max_output_tokens":999999,"input":"hi"}'

        transformed = compatible_request_body(body, upstream)

        self.assertEqual(json.loads(transformed)["max_output_tokens"], 65536)

    def test_ollama_body_applies_upstream_output_token_cap(self):
        upstream = choose_upstream("deepseek-v4-pro")
        body = b'{"model":"deepseek-v4-pro","input":"hi"}'

        transformed = compatible_request_body(body, upstream)

        self.assertEqual(json.loads(transformed)["max_output_tokens"], 65536)

    def test_ollama_body_applies_minimax_output_token_cap(self):
        upstream = choose_upstream("minimax-m3")
        body = b'{"model":"minimax-m3","input":"hi"}'

        transformed = compatible_request_body(body, upstream)

        self.assertEqual(json.loads(transformed)["max_output_tokens"], 131072)

    def test_ollama_body_converts_compaction_input_to_developer_message(self):
        upstream = choose_upstream("glm-5.2")
        body = json.dumps(
            {
                "model": "glm-5.2",
                "input": [
                    {"type": "message", "role": "user", "content": "test"},
                    {
                        "type": "compaction",
                        "summary": [{"type": "summary_text", "text": "Earlier useful context."}],
                    },
                ],
            }
        ).encode("utf-8")

        transformed = compatible_request_body(body, upstream)
        payload = json.loads(transformed)

        self.assertEqual(payload["input"][1]["type"], "message")
        self.assertEqual(payload["input"][1]["role"], "developer")
        self.assertIn("Earlier useful context.", payload["input"][1]["content"])
        self.assertNotIn('"type":"compaction"', transformed.decode("utf-8"))

    def test_ollama_body_converts_custom_tool_items_to_developer_messages(self):
        upstream = choose_upstream("glm-5.2")
        body = json.dumps(
            {
                "model": "glm-5.2",
                "input": [
                    {"type": "message", "role": "user", "content": "test"},
                    {
                        "type": "custom_tool_call",
                        "status": "completed",
                        "call_id": "call_apply_patch",
                        "name": "apply_patch",
                        "input": "*** Begin Patch\n*** Update File: demo.txt\n@@\n+ok\n*** End Patch",
                    },
                    {
                        "type": "custom_tool_call_output",
                        "call_id": "call_apply_patch",
                        "output": "Exit code: 0\nOutput:\nSuccess. Updated demo.txt",
                    },
                ],
            }
        ).encode("utf-8")

        transformed = compatible_request_body(body, upstream)
        payload = json.loads(transformed)
        raw = transformed.decode("utf-8")

        self.assertEqual(payload["input"][1]["type"], "message")
        self.assertEqual(payload["input"][1]["role"], "developer")
        self.assertIn("Read-only Codex tool call transcript", payload["input"][1]["content"])
        self.assertIn("apply_patch", payload["input"][1]["content"])
        self.assertIn("*** Begin Patch", payload["input"][1]["content"])
        self.assertEqual(payload["input"][2]["type"], "message")
        self.assertEqual(payload["input"][2]["role"], "developer")
        self.assertIn("Read-only Codex tool result transcript", payload["input"][2]["content"])
        self.assertIn("Success. Updated demo.txt", payload["input"][2]["content"])
        self.assertNotIn('"type":"custom_tool_call"', raw)
        self.assertNotIn('"type":"custom_tool_call_output"', raw)

    def test_ollama_body_normalizes_real_history_artifact_items(self):
        upstream = choose_upstream("glm-5.2")
        body = json.dumps(
            {
                "model": "glm-5.2",
                "input": [
                    {"type": "message", "role": "user", "content": "test"},
                    {
                        "type": "reasoning",
                        "summary": [{"type": "summary_text", "text": "Reasoning summary to preserve."}],
                        "encrypted_content": "gAAAA-do-not-send-to-third-party",
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_fn",
                        "name": "shell_command",
                        "arguments": "{\"command\":\"echo hi\"}",
                    },
                    {"type": "function_call_output", "call_id": "call_fn", "output": "hi"},
                    {"type": "web_search_call", "status": "completed", "action": {"query": "codex proxy"}},
                    {
                        "type": "tool_search_call",
                        "status": "completed",
                        "call_id": "call_search",
                        "arguments": {"query": "render_chart"},
                        "execution": {"result": "ok"},
                    },
                    {
                        "type": "tool_search_output",
                        "status": "completed",
                        "call_id": "call_search",
                        "tools": [{"name": "render_chart"}],
                    },
                    {"type": "reasoning", "encrypted_content": "gAAAA-empty-summary-can-drop"},
                ],
            }
        ).encode("utf-8")

        transformed = compatible_request_body(body, upstream)
        payload = json.loads(transformed)
        raw = transformed.decode("utf-8")

        self.assertEqual(len(payload["input"]), 6)
        self.assertTrue(all(item["type"] == "message" for item in payload["input"]))
        self.assertIn("Read-only Codex function call transcript", payload["input"][1]["content"])
        self.assertIn("shell_command", payload["input"][1]["content"])
        self.assertIn("Read-only Codex function result transcript", payload["input"][2]["content"])
        self.assertIn("Read-only Codex web search call transcript", payload["input"][3]["content"])
        self.assertIn("Read-only Codex tool search call transcript", payload["input"][4]["content"])
        self.assertIn("Read-only Codex tool search result transcript", payload["input"][5]["content"])
        for forbidden in (
            '"type":"reasoning"',
            '"type":"function_call"',
            '"type":"function_call_output"',
            '"type":"web_search_call"',
            '"type":"tool_search_call"',
            '"type":"tool_search_output"',
            "gAAAA",
        ):
            self.assertNotIn(forbidden, raw)

    def test_official_body_leaves_xhigh_unchanged(self):
        upstream = choose_upstream("gpt-5.5")
        body = b'{"model":"gpt-5.5","reasoning":{"effort":"xhigh"},"input":"hi"}'

        transformed = compatible_request_body(body, upstream)

        self.assertEqual(json.loads(transformed)["reasoning"]["effort"], "xhigh")

    def test_official_body_keeps_compaction_input_unchanged(self):
        upstream = choose_upstream("gpt-5.5")
        body = b'{"model":"gpt-5.5","input":[{"type":"compaction","summary":"keep official shape"}]}'

        transformed = compatible_request_body(body, upstream)

        self.assertEqual(json.loads(transformed)["input"][0]["type"], "compaction")

    def test_official_body_keeps_custom_tool_items_unchanged(self):
        upstream = choose_upstream("gpt-5.5")
        body = b'{"model":"gpt-5.5","input":[{"type":"custom_tool_call","call_id":"call_1","name":"apply_patch","input":"patch"}]}'

        transformed = compatible_request_body(body, upstream)

        self.assertEqual(json.loads(transformed)["input"][0]["type"], "custom_tool_call")

    def test_official_body_injects_codex_endpoint_requirements(self):
        upstream = choose_upstream("gpt-5.5")
        body = b'{"model":"gpt-5.5","input":"hi","stream":false,"max_output_tokens":100}'

        transformed = compatible_request_body(body, upstream)
        payload = json.loads(transformed)

        self.assertFalse(payload["store"])
        self.assertTrue(payload["stream"])
        self.assertNotIn("max_output_tokens", payload)

    def test_official_body_downgrades_invalid_function_call_names(self):
        upstream = choose_upstream("gpt-5.5")
        body = json.dumps(
            {
                "model": "gpt-5.5",
                "input": [
                    {"type": "message", "role": "user", "content": "test"},
                    {
                        "type": "function_call",
                        "call_id": "call_bad",
                        "name": "[Codex",
                        "arguments": "{\"tool search call]\":\"\"}",
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_bad",
                        "output": "unsupported call: [Codex",
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_good",
                        "name": "shell_command",
                        "arguments": "{\"command\":\"echo ok\"}",
                    },
                ],
            }
        ).encode("utf-8")

        transformed = compatible_request_body(body, upstream)
        payload = json.loads(transformed)

        self.assertEqual(payload["input"][1]["type"], "message")
        self.assertEqual(payload["input"][1]["role"], "assistant")
        self.assertIn("Invalid Codex function call transcript", payload["input"][1]["content"][0]["text"])
        self.assertIn("[Codex", payload["input"][1]["content"][0]["text"])
        self.assertEqual(payload["input"][2]["type"], "message")
        self.assertEqual(payload["input"][2]["role"], "assistant")
        self.assertIn("Invalid Codex function result transcript", payload["input"][2]["content"][0]["text"])
        self.assertEqual(payload["input"][3]["type"], "function_call")
        self.assertEqual(payload["input"][3]["name"], "shell_command")

    def test_external_body_rewrites_model_and_clamps_output_tokens(self):
        upstream = choose_upstream("volc/glm-5.2")
        body = b'{"model":"volc/glm-5.2","max_output_tokens":999999,"input":"hi"}'

        transformed = compatible_request_body(body, upstream)
        payload = json.loads(transformed)

        self.assertEqual(payload["model"], "glm-5.2")
        self.assertEqual(payload["max_output_tokens"], 4096)

    def test_responses_request_converts_to_chat_completions_shape(self):
        body = json.dumps(
            {
                "model": "glm-5.2",
                "input": [{"type": "message", "role": "user", "content": "Use get_weather."}],
                "tools": [
                    {
                        "type": "function",
                        "name": "get_weather",
                        "description": "Get weather",
                        "parameters": {
                            "type": "object",
                            "properties": {"city": {"type": "string"}},
                            "required": ["city"],
                        },
                    }
                ],
                "tool_choice": {"type": "function", "name": "get_weather"},
                "max_output_tokens": 64,
                "stream": True,
            }
        ).encode("utf-8")

        transformed = _responses_request_to_chat_completion_body(body)
        payload = json.loads(transformed)

        self.assertEqual(payload["model"], "glm-5.2")
        self.assertEqual(payload["messages"], [{"role": "user", "content": "Use get_weather."}])
        self.assertEqual(payload["max_tokens"], 64)
        self.assertTrue(payload["stream"])
        self.assertEqual(payload["tools"][0]["type"], "function")
        self.assertEqual(payload["tools"][0]["function"]["name"], "get_weather")
        self.assertEqual(payload["tool_choice"]["function"]["name"], "get_weather")

    def test_external_body_converts_compaction_input_to_developer_message(self):
        for model_id, upstream_model in (
            ("volc/glm-5.2", "glm-5.2"),
            ("minimax-cn/minimax-m3", "MiniMax-M3"),
        ):
            with self.subTest(model_id=model_id):
                upstream = dict(choose_upstream(model_id))
                upstream["tool_protocol"] = "text_compat"
                body = json.dumps(
                    {
                        "model": model_id,
                        "input": [
                            {"type": "message", "role": "user", "content": "test"},
                            {
                                "type": "compaction",
                                "summary": [{"type": "summary_text", "text": "Earlier external-provider context."}],
                            },
                        ],
                    }
                ).encode("utf-8")

                transformed = compatible_request_body(body, upstream)
                payload = json.loads(transformed)

                self.assertEqual(payload["model"], upstream_model)
                self.assertEqual(payload["input"][1]["type"], "message")
                self.assertEqual(payload["input"][1]["role"], "developer")
                self.assertIn("Earlier external-provider context.", payload["input"][1]["content"])
                self.assertNotIn('"type":"compaction"', transformed.decode("utf-8"))

    def test_external_body_converts_custom_tool_items_to_developer_messages(self):
        for model_id, upstream_model in (
            ("volc/glm-5.2", "glm-5.2"),
            ("minimax-cn/minimax-m3", "MiniMax-M3"),
        ):
            with self.subTest(model_id=model_id):
                upstream = dict(choose_upstream(model_id))
                upstream["tool_protocol"] = "text_compat"
                body = json.dumps(
                    {
                        "model": model_id,
                        "input": [
                            {
                                "type": "custom_tool_call",
                                "status": "completed",
                                "call_id": "call_shell",
                                "name": "shell_command",
                                "input": {"command": "rg custom_tool_call"},
                            },
                            {
                                "type": "custom_tool_call_output",
                                "call_id": "call_shell",
                                "output": "Exit code: 0\nOutput:\nmatch",
                            },
                        ],
                    }
                ).encode("utf-8")

                transformed = compatible_request_body(body, upstream)
                payload = json.loads(transformed)
                raw = transformed.decode("utf-8")

                self.assertEqual(payload["model"], upstream_model)
                self.assertEqual(payload["input"][0]["type"], "message")
                self.assertEqual(payload["input"][0]["role"], "developer")
                self.assertIn("Read-only Codex tool call transcript", payload["input"][0]["content"])
                self.assertIn("shell_command", payload["input"][0]["content"])
                self.assertIn("rg custom_tool_call", payload["input"][0]["content"])
                self.assertEqual(payload["input"][1]["type"], "message")
                self.assertEqual(payload["input"][1]["role"], "developer")
                self.assertIn("Read-only Codex tool result transcript", payload["input"][1]["content"])
                self.assertIn("match", payload["input"][1]["content"])
                self.assertNotIn('"type":"custom_tool_call"', raw)
                self.assertNotIn('"type":"custom_tool_call_output"', raw)

    def test_external_body_normalizes_real_history_artifact_items(self):
        for model_id, upstream_model in (
            ("volc/glm-5.2", "glm-5.2"),
            ("minimax-cn/minimax-m3", "MiniMax-M3"),
        ):
            with self.subTest(model_id=model_id):
                upstream = dict(choose_upstream(model_id))
                upstream["tool_protocol"] = "text_compat"
                body = json.dumps(
                    {
                        "model": model_id,
                        "input": [
                            {
                                "type": "reasoning",
                                "summary": [{"type": "summary_text", "text": "External reasoning summary."}],
                                "encrypted_content": "gAAAA-do-not-send",
                            },
                            {
                                "type": "function_call",
                                "call_id": "call_fn",
                                "name": "shell_command",
                                "arguments": "{\"command\":\"echo hi\"}",
                            },
                            {"type": "function_call_output", "call_id": "call_fn", "output": "hi"},
                            {"type": "web_search_call", "status": "completed", "action": {"query": "glm"}},
                            {
                                "type": "tool_search_call",
                                "call_id": "call_tool_search",
                                "status": "completed",
                                "arguments": {"query": "openai"},
                            },
                            {
                                "type": "tool_search_output",
                                "call_id": "call_tool_search",
                                "status": "completed",
                                "tools": [{"name": "openai-platform-api-key"}],
                            },
                        ],
                    }
                ).encode("utf-8")

                transformed = compatible_request_body(body, upstream)
                payload = json.loads(transformed)
                raw = transformed.decode("utf-8")

                self.assertEqual(payload["model"], upstream_model)
                self.assertTrue(all(item["type"] == "message" for item in payload["input"]))
                self.assertIn("Read-only Codex function call transcript", payload["input"][0]["content"])
                self.assertIn("Read-only Codex function result transcript", payload["input"][1]["content"])
                self.assertIn("Read-only Codex web search call transcript", payload["input"][2]["content"])
                self.assertIn("Read-only Codex tool search call transcript", payload["input"][3]["content"])
                self.assertIn("Read-only Codex tool search result transcript", payload["input"][4]["content"])
                for forbidden in (
                    '"type":"reasoning"',
                    '"type":"function_call"',
                    '"type":"function_call_output"',
                    '"type":"web_search_call"',
                    '"type":"tool_search_call"',
                    '"type":"tool_search_output"',
                    "gAAAA",
                ):
                    self.assertNotIn(forbidden, raw)

    def test_external_non_json_body_rewrites_embedded_model_alias(self):
        upstream = choose_upstream("volc/glm-5.2")
        body = (
            b"--boundary\r\n"
            b'Content-Disposition: form-data; name="payload"\r\n'
            b"Content-Type: application/json\r\n\r\n"
            b'{"model":"volc/glm-5.2","input":"hi"}\r\n'
            b"--boundary--\r\n"
        )

        transformed = compatible_request_body(body, upstream, model_id="volc/glm-5.2")

        self.assertIn(b'"model":"glm-5.2"', transformed)
        self.assertNotIn(b'"model":"volc/glm-5.2"', transformed)

    def test_default_minimax_provider_rewrites_to_live_upstream_model_case(self):
        from providers_config import DEFAULT_PROVIDERS_PATH
        from providers_config import resolve_external_model_alias as real_resolve_external_model_alias

        def bundled_resolve_external_model_alias(model_id):
            return real_resolve_external_model_alias(model_id, providers_path=DEFAULT_PROVIDERS_PATH)

        with (
            patch("codex_proxy.resolve_external_model_alias", side_effect=bundled_resolve_external_model_alias),
            patch.dict(os.environ, {"MINIMAX_API_KEY": "minimax-live-case-token"}, clear=False),
        ):
            upstream = choose_upstream("minimax-cn/minimax-m3")

            body = b'{"model":"minimax-cn/minimax-m3","input":"hi"}'
            transformed = compatible_request_body(body, upstream, "minimax-cn/minimax-m3")

        self.assertEqual(upstream["name"], "minimax_cn")
        self.assertEqual(upstream["upstream_model"], "MiniMax-M3")
        self.assertEqual(json.loads(transformed)["model"], "MiniMax-M3")

    def test_official_responses_url_preserves_backend_subpath_and_query(self):
        upstream = official_upstream()

        self.assertEqual(
            _responses_url(upstream, "/v1/responses?cursor=abc"),
            "https://chatgpt.com/backend-api/codex/responses?cursor=abc",
        )
        self.assertEqual(
            _responses_url(upstream, "/v1/responses/resp_123/input_items"),
            "https://chatgpt.com/backend-api/codex/responses/resp_123/input_items",
        )

    def test_official_body_removes_plaintext_reasoning_encrypted_content(self):
        upstream = choose_upstream("gpt-5.5")
        body = json.dumps(
            {
                "model": "gpt-5.5",
                "input": [
                    {
                        "type": "reasoning",
                        "id": "rs_bad",
                        "summary": [{"type": "summary_text", "text": "third party reasoning summary"}],
                        "encrypted_content": "The user just typed test and the current goal is unknown.",
                    },
                    {"type": "message", "role": "user", "content": "test"},
                ],
            }
        ).encode("utf-8")

        transformed = compatible_request_body(body, upstream)
        payload = json.loads(transformed)

        self.assertNotIn("encrypted_content", payload["input"][0])
        self.assertEqual(payload["input"][0]["summary"][0]["text"], "third party reasoning summary")

    def test_official_body_keeps_official_reasoning_encrypted_content(self):
        upstream = choose_upstream("gpt-5.5")
        encrypted_content = "gAAAAABqQFxWldgz0tjB8nSg51Eg5_bsIdx_8n85wX2RQLunO8HVW1mm"
        body = json.dumps(
            {
                "model": "gpt-5.5",
                "input": [
                    {
                        "type": "reasoning",
                        "id": "rs_good",
                        "summary": [],
                        "encrypted_content": encrypted_content,
                    }
                ],
            }
        ).encode("utf-8")

        transformed = compatible_request_body(body, upstream)

        self.assertEqual(json.loads(transformed)["input"][0]["encrypted_content"], encrypted_content)

    def test_ollama_body_leaves_non_xhigh_reasoning_values_unchanged(self):
        upstream = choose_upstream("glm-5.2")
        for effort in ("low", "medium", "high", "max", "none"):
            with self.subTest(effort=effort):
                body = f'{{"model":"glm-5.2","reasoning":{{"effort":"{effort}"}},"input":"hi"}}'.encode("utf-8")

                transformed = compatible_request_body(body, upstream)
                payload = json.loads(transformed)

                self.assertEqual(payload["reasoning"]["effort"], effort)
                self.assertEqual(payload["max_output_tokens"], 131072)

    def test_ollama_auth_replaces_incoming_auth(self):
        upstream = choose_upstream("glm-5.2")
        with patch.dict(os.environ, {"OLLAMA_API_KEY": "ollama-token"}, clear=False):
            headers = upstream_headers(
                {"Authorization": "Bearer openai-token", "Content-Type": "application/json"},
                upstream,
            )
        self.assertEqual(headers["Authorization"], "Bearer ollama-token")
        self.assertNotIn("openai-token", str(headers))

    def test_decoded_body_headers_drop_content_encoding(self):
        upstream = choose_upstream("glm-5.2")
        with patch.dict(os.environ, {"OLLAMA_API_KEY": "ollama-token"}, clear=False):
            headers = upstream_headers(
                {
                    "Authorization": "Bearer openai-token",
                    "Content-Type": "application/json",
                    "Content-Encoding": "zstd",
                },
                upstream,
                drop_content_encoding=True,
            )

        self.assertNotIn("Content-Encoding", headers)
        self.assertEqual(headers["Content-Type"], "application/json")

    def test_encoded_body_decode_failure_does_not_fallback_to_official(self):
        body = b"not a gzip body"
        handler = CodexProxyHandler.__new__(CodexProxyHandler)
        handler.path = "/v1/responses"
        handler.headers = {
            "Content-Length": str(len(body)),
            "Content-Type": "application/json",
            "Content-Encoding": "gzip",
            "X-Codex-Window-Id": "thread:turn",
        }
        handler.rfile = io.BytesIO(body)
        sent = []
        handler._safe_send_json = lambda status, payload, request_id: sent.append((status, payload))

        with patch("codex_proxy.urlopen") as upstream_request:
            CodexProxyHandler.do_POST(handler)

        upstream_request.assert_not_called()
        self.assertEqual(sent[0][0], 400)
        self.assertIn("decode failed", sent[0][1]["error"])

    def test_chat_completions_upstream_posts_to_chat_endpoint_and_body(self):
        self.external_model["upstream_format"] = "chat_completions"
        body = json.dumps(
            {
                "model": "volc/glm-5.2",
                "input": [{"type": "message", "role": "user", "content": "hi"}],
                "stream": False,
            }
        ).encode("utf-8")
        handler = CodexProxyHandler.__new__(CodexProxyHandler)
        handler.path = "/v1/responses"
        handler.headers = {
            "Content-Length": str(len(body)),
            "Content-Type": "application/json",
            "X-Codex-Window-Id": "thread:turn",
        }
        handler.rfile = io.BytesIO(body)
        handler.close_connection = False
        handler._safe_send_json = lambda status, payload, request_id: None
        relayed = []
        handler._relay_upstream_response = lambda response, upstream_name, **kwargs: relayed.append(kwargs) or 200

        with patch("codex_proxy.urlopen", return_value=FakeContextResponse(b'{"id":"chatcmpl_1","choices":[]}')) as mock_urlopen:
            CodexProxyHandler.do_POST(handler)

        request = mock_urlopen.call_args.args[0]
        payload = json.loads(request.data)
        self.assertEqual(request.full_url, "https://ark.example.test/v1/chat/completions")
        self.assertEqual(payload["model"], "glm-5.2")
        self.assertEqual(payload["messages"], [{"role": "user", "content": "hi"}])
        self.assertFalse(payload["stream"])
        self.assertEqual(relayed[0]["upstream_format"], "chat_completions")

    def test_official_auth_injects_codex_subscription_token(self):
        upstream = choose_upstream("gpt-5.5")
        with patch("codex_proxy.codex_access_token", return_value="sub-token-from-auth-json"), \
             patch("codex_proxy.codex_account_id", return_value="acct-123"):
            headers = upstream_headers({"Authorization": "Bearer caller-token", "Content-Type": "application/json"}, upstream)
        self.assertEqual(headers["Authorization"], "Bearer sub-token-from-auth-json")
        self.assertEqual(headers["Chatgpt-account-id"], "acct-123")
        self.assertNotIn("caller-token", str(headers))

    def test_official_auth_ignores_ollama_api_key(self):
        upstream = choose_upstream("gpt-5.5")
        with patch("codex_proxy.codex_access_token", return_value="sub-token-from-auth-json"), \
             patch("codex_proxy.codex_account_id", return_value="acct-123"), \
             patch.dict(os.environ, {"OLLAMA_API_KEY": "ollama-token"}, clear=False):
            headers = upstream_headers(
                {"Authorization": "Bearer caller-token", "Content-Type": "application/json"},
                upstream,
            )
        self.assertEqual(headers["Authorization"], "Bearer sub-token-from-auth-json")
        self.assertNotIn("ollama-token", str(headers))

    def test_external_auth_replaces_incoming_auth_with_provider_key(self):
        upstream = choose_upstream("volc/glm-5.2")
        headers = upstream_headers(
            {"Authorization": "Bearer openai-token", "Content-Type": "application/json"},
            upstream,
        )

        self.assertEqual(headers["Authorization"], "Bearer volc-test-token")
        self.assertNotIn("openai-token", str(headers))

    def test_sse_relay_flushes_each_line(self):
        handler = FakeHandler()
        lines = [b"data: one\n", b"\n", b"data: two\n", b"\n", b""]
        response = FakeSseResponse(lines)

        CodexProxyHandler._relay_upstream_response(handler, response, "official")

        self.assertEqual(handler.status, 200)
        self.assertTrue(handler.headers_ended)
        self.assertEqual(handler.wfile.writes, [b"data: one\n", b"\n", b"data: two\n", b"\n"])
        self.assertGreaterEqual(handler.wfile.flush_count, 4)
        self.assertTrue(handler.close_connection)

    def test_ollama_sse_relay_removes_plaintext_reasoning_encrypted_content(self):
        handler = FakeHandler()
        event = {
            "type": "response.output_item.done",
            "item": {
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": "third party summary"}],
                "encrypted_content": "The user just typed test.",
            },
        }
        response = FakeSseResponse([f"data: {json.dumps(event)}\n".encode("utf-8"), b"\n", b""])

        CodexProxyHandler._relay_upstream_response(handler, response, "ollama_cloud")

        data_line = handler.wfile.writes[0].decode("utf-8")
        payload = json.loads(data_line.removeprefix("data: "))
        self.assertNotIn("encrypted_content", payload["item"])
        self.assertEqual(payload["item"]["summary"], [])

    def test_external_sse_relay_removes_plaintext_reasoning_encrypted_content(self):
        handler = FakeHandler()
        event = {
            "type": "response.output_item.done",
            "item": {
                "type": "reasoning",
                "summary": [{"type": "summary_text", "text": "third party summary"}],
                "encrypted_content": "The user just typed test.",
            },
        }
        response = FakeSseResponse([f"data: {json.dumps(event)}\n".encode("utf-8"), b"\n", b""])

        CodexProxyHandler._relay_upstream_response(handler, response, "volcengine")

        data_line = handler.wfile.writes[0].decode("utf-8")
        payload = json.loads(data_line.removeprefix("data: "))
        self.assertNotIn("encrypted_content", payload["item"])
        self.assertEqual(payload["item"]["summary"], [])

    def test_external_sse_relay_copies_reasoning_content_to_summary_for_codex_app(self):
        handler = FakeHandler()
        event = {
            "type": "response.output_item.done",
            "item": {
                "type": "reasoning",
                "summary": [],
                "content": [{"type": "reasoning_text", "text": "raw third party thinking"}],
            },
        }
        response = FakeSseResponse([f"data: {json.dumps(event)}\n".encode("utf-8"), b"\n", b""])

        CodexProxyHandler._relay_upstream_response(handler, response, "volcengine")

        data_line = handler.wfile.writes[0].decode("utf-8")
        payload = json.loads(data_line.removeprefix("data: "))
        self.assertEqual(payload["item"]["summary"], [])
        self.assertNotIn("content", payload["item"])

    def test_external_sse_relay_maps_reasoning_text_delta_to_summary_delta_for_codex_app(self):
        handler = FakeHandler()
        event = {
            "type": "response.reasoning_text.delta",
            "item_id": "rs_123",
            "output_index": 0,
            "content_index": 0,
            "delta": "streamed raw thinking",
        }
        response = FakeSseResponse([f"data: {json.dumps(event)}\n".encode("utf-8"), b"\n", b""])

        CodexProxyHandler._relay_upstream_response(handler, response, "ollama_cloud")

        self.assertEqual(handler.wfile.writes[0], b"")

    def test_external_sse_relay_downgrades_invalid_function_call_name(self):
        handler = FakeHandler()
        event = {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "call_id": "call_bad",
                "name": "Codex function call]<tool_call>shell_command",
                "arguments": "{\"command\":\"echo bad\"}",
            },
        }
        response = FakeSseResponse([f"data: {json.dumps(event)}\n".encode("utf-8"), b"\n", b""])

        CodexProxyHandler._relay_upstream_response(handler, response, "ollama_cloud")

        data_line = handler.wfile.writes[0].decode("utf-8")
        payload = json.loads(data_line.removeprefix("data: "))
        self.assertEqual(payload["item"]["type"], "message")
        self.assertEqual(payload["item"]["role"], "assistant")
        self.assertIn("Invalid third-party function call transcript", payload["item"]["content"][0]["text"])
        self.assertIn("Codex function call]", payload["item"]["content"][0]["text"])

    def test_chat_tool_call_chunks_preserve_first_call_id_in_responses_events(self):
        chunks = [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_spawn",
                                    "type": "function",
                                    "function": {"name": "spawn_agent", "arguments": ""},
                                }
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "",
                                    "function": {"arguments": "{\"message\":\"hi\""},
                                }
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": "}"},
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
        ]

        events = _chat_stream_chunks_to_response_events(chunks)

        event_types = [event["type"] for event in events]
        self.assertIn("response.output_item.added", event_types)
        self.assertIn("response.function_call_arguments.done", event_types)
        self.assertIn("response.output_item.done", event_types)
        self.assertIn("response.completed", event_types)

        done = next(event for event in events if event["type"] == "response.output_item.done")
        completed = next(event for event in events if event["type"] == "response.completed")
        self.assertEqual(done["item"]["call_id"], "call_spawn")
        self.assertEqual(done["item"]["name"], "spawn_agent")
        self.assertEqual(done["item"]["arguments"], "{\"message\":\"hi\"}")
        self.assertEqual(completed["response"]["output"][0]["call_id"], "call_spawn")

    def test_chat_tool_call_chunks_generate_call_id_when_upstream_omits_it(self):
        chunks = [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "type": "function",
                                    "function": {"name": "multi_agent_v1__spawn_agent", "arguments": ""},
                                }
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": "{\"message\":\"hi\"}"},
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
            "[DONE]",
        ]

        events = _chat_stream_chunks_to_response_events(chunks)

        done = next(event for event in events if event["type"] == "response.output_item.done")
        completed = next(event for event in events if event["type"] == "response.completed")
        self.assertTrue(done["item"]["call_id"].startswith("call_"))
        self.assertEqual(done["item"]["name"], "multi_agent_v1__spawn_agent")
        self.assertEqual(json.loads(done["item"]["arguments"])["message"], "hi")
        self.assertEqual(completed["response"]["output"][0]["call_id"], done["item"]["call_id"])

    def test_chat_tool_call_chunks_drop_text_message_when_tool_call_present(self):
        chunks = [
            {
                "choices": [
                    {
                        "delta": {"role": "assistant"},
                        "finish_reason": None,
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {"content": "I'll read the plan first."},
                        "finish_reason": None,
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_node",
                                    "type": "function",
                                    "function": {"name": "mcp__node_repl__js", "arguments": ""},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": json.dumps({"code": "readPlan()"})},
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
        ]

        events = _chat_stream_chunks_to_response_events(chunks)
        completed = next(event for event in events if event["type"] == "response.completed")

        self.assertTrue(any(event["type"] == "response.output_item.done" for event in events))
        self.assertFalse(any(event["type"] == "response.output_text.delta" for event in events))
        self.assertEqual(len(completed["response"]["output"]), 1)
        self.assertEqual(completed["response"]["output"][0]["type"], "function_call")
        self.assertEqual(completed["response"]["output"][0]["name"], "mcp__node_repl__js")

    def test_chat_stream_pipeline_preserves_node_repl_dot_alias_tool_call(self):
        chunks = [
            {
                "id": "chatcmpl_1",
                "model": "glm-5.2",
                "choices": [{"delta": {"role": "assistant"}, "finish_reason": None}],
            },
            {
                "id": "chatcmpl_1",
                "model": "glm-5.2",
                "choices": [{"delta": {"content": "I'll read the plan first."}, "finish_reason": None}],
            },
            {
                "id": "chatcmpl_1",
                "model": "glm-5.2",
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_node",
                                    "type": "function",
                                    "function": {"name": "mcp__node_repl.js", "arguments": ""},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": "chatcmpl_1",
                "model": "glm-5.2",
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": json.dumps({"code": "readPlan()"})},
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
            },
            "[DONE]",
        ]

        events = codex_proxy._chat_stream_chunks_to_response_events(chunks)
        events, _ = codex_proxy._normalize_third_party_tool_call(events)
        events, _ = codex_proxy._downgrade_invalid_third_party_tool_calls(events)
        events, _ = codex_proxy._reconcile_function_call_argument_events(events)

        done = [event for event in events if event.get("type") == "response.output_item.done"][0]
        completed = [event for event in events if event.get("type") == "response.completed"][0]
        self.assertEqual(done["item"]["type"], "function_call")
        self.assertEqual(done["item"]["namespace"], "mcp__node_repl")
        self.assertEqual(done["item"]["name"], "js")
        self.assertEqual(json.loads(done["item"]["arguments"]), {"code": "readPlan()"})
        self.assertEqual(completed["response"]["output"][0]["type"], "function_call")
        self.assertEqual(completed["response"]["output"][0]["namespace"], "mcp__node_repl")
        self.assertEqual(completed["response"]["output"][0]["name"], "js")
        self.assertFalse(any(event.get("item", {}).get("type") == "message" for event in events if isinstance(event.get("item"), dict)))

    def test_chat_stream_pipeline_preserves_multi_agent_dot_alias_tool_call(self):
        chunks = [
            {
                "id": "chatcmpl_1",
                "model": "glm-5.2",
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_spawn",
                                    "type": "function",
                                    "function": {"name": "multi_agent_v1.spawn_agent", "arguments": ""},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": "chatcmpl_1",
                "model": "glm-5.2",
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {
                                        "arguments": json.dumps({"message": "return ok", "nickname": "impl"})
                                    },
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
            },
            "[DONE]",
        ]

        events = codex_proxy._chat_stream_chunks_to_response_events(chunks)
        events, _ = codex_proxy._normalize_third_party_tool_call(events)
        events, _ = codex_proxy._downgrade_invalid_third_party_tool_calls(events)
        events, _ = codex_proxy._reconcile_function_call_argument_events(events)

        done = [event for event in events if event.get("type") == "response.output_item.done"][0]
        self.assertEqual(done["item"]["type"], "function_call")
        self.assertEqual(done["item"]["namespace"], "multi_agent_v1")
        self.assertEqual(done["item"]["name"], "spawn_agent")
        self.assertEqual(json.loads(done["item"]["arguments"])["nickname"], "impl")

    def test_chat_stream_reconcile_drops_raw_argument_deltas_after_normalization(self):
        chunks = [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_spawn",
                                    "type": "function",
                                    "function": {"name": "multi_agent_v1__spawn_agent", "arguments": ""},
                                }
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {
                                        "arguments": json.dumps(
                                            {
                                                "prompt": "Implement Task 1 exactly.",
                                                "name": "implementer-task-1",
                                                "agent_type": "general",
                                            }
                                        )
                                    },
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
            "[DONE]",
        ]

        events = _chat_stream_chunks_to_response_events(chunks)
        events, _ = codex_proxy._normalize_third_party_tool_call(events)
        events, changed = codex_proxy._reconcile_function_call_argument_events(events)
        done = next(event for event in events if event["type"] == "response.output_item.done")
        arguments_done = next(event for event in events if event["type"] == "response.function_call_arguments.done")

        self.assertTrue(changed)
        self.assertFalse(any(event["type"] == "response.function_call_arguments.delta" for event in events))
        self.assertEqual(json.loads(done["item"]["arguments"])["message"], "Implement Task 1 exactly.")
        self.assertEqual(json.loads(done["item"]["arguments"])["nickname"], "implementer-task-1")
        self.assertEqual(done["item"]["arguments"], arguments_done["arguments"])
        self.assertNotIn("agent_type", done["item"]["arguments"])


    def test_non_sse_relay_bulk_writes_body(self):
        handler = FakeHandler()
        body = b'{"ok":true}'
        response = FakeResponse(body)

        CodexProxyHandler._relay_upstream_response(handler, response, "official")

        self.assertEqual(handler.status, 200)
        self.assertEqual(handler.wfile.writes, [body])
        self.assertEqual(handler.wfile.flush_count, 1)
        self.assertTrue(handler.close_connection)

    def test_chat_completions_non_sse_relay_converts_tool_calls_to_responses_body(self):
        handler = FakeHandler()
        body = json.dumps(
            {
                "id": "chatcmpl_1",
                "model": "glm-5.2",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": "call_spawn",
                                    "type": "function",
                                    "function": {"name": "spawn_agent", "arguments": "{\"message\":\"hi\"}"},
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
            }
        ).encode("utf-8")
        response = FakeResponse(body)

        CodexProxyHandler._relay_upstream_response(
            handler,
            response,
            "volcengine",
            upstream_format="chat_completions",
        )

        payload = json.loads(handler.wfile.writes[0])
        self.assertEqual(payload["object"], "response")
        self.assertEqual(payload["output"][0]["type"], "function_call")
        self.assertEqual(payload["output"][0]["call_id"], "call_spawn")
        self.assertEqual(payload["output"][0]["name"], "spawn_agent")

    def test_chat_completions_non_sse_relay_converts_xmlish_tool_call_text(self):
        body = json.dumps(
            {
                "id": "chatcmpl_xmlish",
                "model": "minimax-m3",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": (
                                "]<]minimax[>[<tool_call>\n"
                                "]<]minimax[>[<invoke name=\"multi_agent_v1__send_input\">"
                                "]<]minimax[>[<message>Fix the artifact</message>"
                                "]<]minimax[>[<target>impl-1</target>"
                                "]<]minimax[>[</invoke>\n"
                                "]<]minimax[>[</tool_call>"
                            ),
                        },
                        "finish_reason": "stop",
                    }
                ],
            }
        ).encode("utf-8")

        transformed = compatible_response_body(
            codex_proxy._chat_completion_to_response_body(body),
            "ollama_cloud",
            event_context={"request_id": "req"},
        )
        call = json.loads(transformed)["output"][0]
        arguments = json.loads(call["arguments"])

        self.assertEqual(call["type"], "function_call")
        self.assertEqual(call["namespace"], "multi_agent_v1")
        self.assertEqual(call["name"], "send_input")
        self.assertEqual(arguments, {"message": "Fix the artifact", "target": "impl-1"})

    def test_chat_completions_non_sse_relay_guards_duplicate_spawn(self):
        handler = FakeHandler()
        body = json.dumps(
            {
                "id": "chatcmpl_1",
                "model": "glm-5.2",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": "call_spawn_again",
                                    "type": "function",
                                    "function": {
                                        "name": "multi_agent_v1__spawn_agent",
                                        "arguments": "{\"message\":\"repeat\"}",
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
            }
        ).encode("utf-8")
        response = FakeResponse(body)

        CodexProxyHandler._relay_upstream_response(
            handler,
            response,
            "volcengine",
            upstream_format="chat_completions",
            event_context={
                "tool_protocol": "chat_tools",
                "subagent_open_agent_ids": ["019f-child"],
                "subagent_spawn_allowed": False,
            },
        )

        call = json.loads(handler.wfile.writes[0])["output"][0]
        self.assertEqual(call["namespace"], "multi_agent_v1")
        self.assertEqual(call["name"], "wait_agent")
        self.assertEqual(json.loads(call["arguments"])["targets"], ["019f-child"])

    def test_external_response_suppresses_extra_workflow_spawn_in_same_response(self):
        body = json.dumps(
            {
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call_impl",
                        "name": "multi_agent_v1__spawn_agent",
                        "arguments": json.dumps(
                            {
                                "message": "You are the IMPLEMENTER subagent for task 1.",
                                "nickname": "implementer",
                            }
                        ),
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_spec",
                        "name": "multi_agent_v1__spawn_agent",
                        "arguments": json.dumps(
                            {
                                "message": "You are the SPEC COMPLIANCE REVIEWER subagent for task 1.",
                                "nickname": "spec-reviewer",
                            }
                        ),
                    },
                ]
            }
        ).encode("utf-8")
        event_context = {
            "request_id": "req",
            "tool_protocol": "chat_tools",
            "subagent_spawn_allowed": True,
            "_subagent_state": build_subagent_state(
                [
                    {
                        "type": "message",
                        "role": "user",
                        "content": "Use subagent-driven-development: spawn an implementer, then a spec reviewer, then a code quality reviewer.",
                    }
                ]
            ),
        }

        transformed = compatible_response_body(body, "ollama_cloud", event_context=event_context)
        output = json.loads(transformed)["output"]

        self.assertEqual(output[0]["namespace"], "multi_agent_v1")
        self.assertEqual(output[0]["name"], "spawn_agent")
        self.assertEqual(json.loads(output[0]["arguments"])["nickname"], "implementer")
        self.assertEqual(output[1]["type"], "message")
        self.assertIn("next_expected_role: implementer", output[1]["content"])

    def test_external_response_allows_first_workflow_implementer_with_all_tasks_wording(self):
        body = json.dumps(
            {
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call_impl",
                        "name": "multi_agent_v1__spawn_agent",
                        "arguments": json.dumps(
                            {
                                "message": "You are the IMPLEMENTER subagent. Implement all tasks in this short diagnostic plan.",
                                "nickname": "implementer",
                            }
                        ),
                    }
                ]
            }
        ).encode("utf-8")
        event_context = {
            "request_id": "req",
            "tool_protocol": "chat_tools",
            "subagent_spawn_allowed": True,
            "_subagent_state": build_subagent_state(
                [
                    {
                        "type": "message",
                        "role": "user",
                        "content": "Use subagent-driven-development: spawn an implementer, then a spec reviewer, then a code quality reviewer.",
                    }
                ]
            ),
        }

        transformed = compatible_response_body(body, "ollama_cloud", event_context=event_context)
        output = json.loads(transformed)["output"]

        self.assertEqual(output[0]["namespace"], "multi_agent_v1")
        self.assertEqual(output[0]["name"], "spawn_agent")
        self.assertEqual(json.loads(output[0]["arguments"])["nickname"], "implementer")

    def test_chat_completions_non_sse_chat_output_repairs_multi_agent_arguments(self):
        handler = FakeHandler()
        body = json.dumps(
            {
                "id": "chatcmpl_1",
                "model": "glm-5.2",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": "call_spawn",
                                    "type": "function",
                                    "function": {
                                        "name": "multi_agent_v1__spawn_agent",
                                        "arguments": json.dumps(
                                            {
                                                "agent_type": "general",
                                                "prompt": "return sentinel A",
                                                "name": "child-a",
                                            }
                                        ),
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
            }
        ).encode("utf-8")
        response = FakeResponse(body)

        CodexProxyHandler._relay_upstream_response(
            handler,
            response,
            "ollama_cloud",
            upstream_format="chat_completions",
            inbound_format="chat_completions",
            event_context={"request_id": "req"},
        )

        payload = json.loads(handler.wfile.writes[0])
        tool_call = payload["choices"][0]["message"]["tool_calls"][0]
        arguments = json.loads(tool_call["function"]["arguments"])

        self.assertEqual(tool_call["function"]["name"], "multi_agent_v1__spawn_agent")
        self.assertEqual(arguments["message"], "return sentinel A")
        self.assertEqual(arguments["nickname"], "child-a")
        self.assertIs(arguments["fork_context"], False)
        self.assertNotIn("agent_type", arguments)
        self.assertNotIn("prompt", arguments)
        self.assertNotIn("name", arguments)

    def test_chat_completions_sse_relay_converts_tool_call_stream_to_responses_events(self):
        handler = FakeHandler()
        chunks = [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_spawn",
                                    "type": "function",
                                    "function": {"name": "spawn_agent", "arguments": ""},
                                }
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": "{\"message\":\"hi\"}"},
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
        ]
        response = FakeSseResponse(
            [f"data: {json.dumps(chunk)}\n".encode("utf-8") for chunk in chunks] + [b"data: [DONE]\n", b""]
        )

        CodexProxyHandler._relay_upstream_response(
            handler,
            response,
            "volcengine",
            upstream_format="chat_completions",
        )

        payloads = [
            json.loads(write.decode("utf-8").removeprefix("data: "))
            for write in handler.wfile.writes
            if write.startswith(b"data: {")
        ]
        done = next(payload for payload in payloads if payload["type"] == "response.output_item.done")
        completed = next(payload for payload in payloads if payload["type"] == "response.completed")
        self.assertEqual(done["item"]["call_id"], "call_spawn")
        done_arguments = json.loads(done["item"]["arguments"])
        self.assertEqual(done_arguments["message"], "hi")
        self.assertIs(done_arguments["fork_context"], False)
        self.assertEqual(completed["response"]["output"][0]["call_id"], "call_spawn")

    def test_chat_completions_sse_relay_converts_message_tool_call_stream_to_responses_events(self):
        handler = FakeHandler()
        chunks = [
            {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": "call_spawn",
                                    "type": "function",
                                    "function": {
                                        "name": "multi_agent_v1__spawn_agent",
                                        "arguments": "{\"message\":\"hi\"}",
                                    },
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
        ]
        response = FakeSseResponse(
            [f"data: {json.dumps(chunk)}\n".encode("utf-8") for chunk in chunks] + [b"data: [DONE]\n", b""]
        )

        CodexProxyHandler._relay_upstream_response(
            handler,
            response,
            "volcengine",
            upstream_format="chat_completions",
        )

        payloads = [
            json.loads(write.decode("utf-8").removeprefix("data: "))
            for write in handler.wfile.writes
            if write.startswith(b"data: {")
        ]
        done = next(payload for payload in payloads if payload["type"] == "response.output_item.done")
        self.assertEqual(done["item"]["call_id"], "call_spawn")
        self.assertEqual(json.loads(done["item"]["arguments"])["message"], "hi")

    def test_chat_completions_sse_relay_converts_xmlish_tool_call_text(self):
        chunks = [
            {
                "choices": [
                    {
                        "delta": {
                            "content": (
                                "<tool_call><invoke name=\"multi_agent_v1__spawn_agent\">"
                                "<message>Spec compliance review for Task 1.</message>"
                                "<nickname>spec-reviewer-task-1</nickname>"
                                "</invoke></tool_call>"
                            )
                        }
                    }
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        ]

        response_body = codex_proxy._events_to_responses_body(_chat_stream_chunks_to_response_events(chunks))
        transformed = compatible_response_body(
            response_body,
            "ollama_cloud",
            event_context={"request_id": "req"},
        )
        call = json.loads(transformed)["output"][0]
        arguments = json.loads(call["arguments"])

        self.assertEqual(call["type"], "function_call")
        self.assertEqual(call["namespace"], "multi_agent_v1")
        self.assertEqual(call["name"], "spawn_agent")
        self.assertEqual(arguments["message"], "Spec compliance review for Task 1.")
        self.assertEqual(arguments["nickname"], "spec-reviewer-task-1")

    def test_chat_completions_sse_relay_guards_duplicate_spawn(self):
        handler = FakeHandler()
        chunks = [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_spawn_again",
                                    "type": "function",
                                    "function": {"name": "multi_agent_v1__spawn_agent", "arguments": ""},
                                }
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": "{\"message\":\"repeat\"}"},
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
        ]
        response = FakeSseResponse(
            [f"data: {json.dumps(chunk)}\n".encode("utf-8") for chunk in chunks] + [b"data: [DONE]\n", b""]
        )

        CodexProxyHandler._relay_upstream_response(
            handler,
            response,
            "volcengine",
            upstream_format="chat_completions",
            event_context={
                "tool_protocol": "chat_tools",
                "subagent_open_agent_ids": ["019f-child"],
                "subagent_spawn_allowed": False,
            },
        )

        payloads = [
            json.loads(write.decode("utf-8").removeprefix("data: "))
            for write in handler.wfile.writes
            if write.startswith(b"data: {")
        ]
        done = next(payload for payload in payloads if payload["type"] == "response.output_item.done")
        completed = next(payload for payload in payloads if payload["type"] == "response.completed")
        self.assertEqual(done["item"]["namespace"], "multi_agent_v1")
        self.assertEqual(done["item"]["name"], "wait_agent")
        self.assertEqual(json.loads(done["item"]["arguments"])["targets"], ["019f-child"])
        self.assertEqual(completed["response"]["output"][0]["name"], "wait_agent")

    def test_chat_completions_sse_chat_output_repairs_multi_agent_arguments(self):
        handler = FakeHandler()
        chunks = [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_spawn",
                                    "type": "function",
                                    "function": {"name": "multi_agent_v1__spawn_agent", "arguments": ""},
                                }
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {
                                        "arguments": json.dumps(
                                            {
                                                "agent_type": "general",
                                                "input": "return sentinel B",
                                                "name": "child-b",
                                            }
                                        )
                                    },
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
        ]
        response = FakeSseResponse(
            [f"data: {json.dumps(chunk)}\n".encode("utf-8") for chunk in chunks] + [b"data: [DONE]\n", b""]
        )

        CodexProxyHandler._relay_upstream_response(
            handler,
            response,
            "ollama_cloud",
            upstream_format="chat_completions",
            inbound_format="chat_completions",
            event_context={"request_id": "req"},
        )

        payloads = [
            json.loads(write.decode("utf-8").removeprefix("data: "))
            for write in handler.wfile.writes
            if write.startswith(b"data: {")
        ]
        tool_chunks = [
            payload
            for payload in payloads
            if payload["choices"][0]["delta"].get("tool_calls")
        ]
        self.assertTrue(tool_chunks)
        self.assertEqual(
            tool_chunks[0]["choices"][0]["delta"]["tool_calls"][0]["function"]["name"],
            "multi_agent_v1__spawn_agent",
        )
        argument_text = "".join(
            chunk["choices"][0]["delta"]["tool_calls"][0]["function"].get("arguments", "")
            for chunk in tool_chunks
        )
        arguments = json.loads(argument_text)
        self.assertEqual(arguments["message"], "return sentinel B")
        self.assertEqual(arguments["nickname"], "child-b")
        self.assertNotIn("agent_type", arguments)
        self.assertNotIn("input", arguments)
        self.assertNotIn("name", arguments)

    def test_chat_completions_sse_relay_converts_text_stream_to_responses_message(self):
        handler = FakeHandler()
        chunks = [
            {"choices": [{"delta": {"content": "hel"}}]},
            {"choices": [{"delta": {"content": "lo"}, "finish_reason": "stop"}]},
        ]
        response = FakeSseResponse(
            [f"data: {json.dumps(chunk)}\n".encode("utf-8") for chunk in chunks] + [b"data: [DONE]\n", b""]
        )

        CodexProxyHandler._relay_upstream_response(
            handler,
            response,
            "volcengine",
            upstream_format="chat_completions",
        )

        payloads = [
            json.loads(write.decode("utf-8").removeprefix("data: "))
            for write in handler.wfile.writes
            if write.startswith(b"data: {")
        ]
        completed = next(payload for payload in payloads if payload["type"] == "response.completed")
        output = completed["response"]["output"][0]
        self.assertEqual(output["type"], "message")
        self.assertEqual(output["content"][0]["text"], "hello")

    def test_chat_sse_lifecycle_empty_final_raises_retryable_without_downstream_write(self):
        handler = FakeHandler()
        chunks = [
            {
                "id": "chatcmpl_empty_final",
                "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
            },
            {
                "id": "chatcmpl_empty_final",
                "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            },
        ]
        response = FakeSseResponse(
            [f"data: {json.dumps(chunk)}\n".encode("utf-8") for chunk in chunks] + [b"data: [DONE]\n", b""]
        )
        event_context = {
            "request_id": "req_empty_final",
            "subagent_lifecycle_complete": True,
        }

        with (
            patch.dict(os.environ, {"CODEXHUB_SUBAGENT_ASSIST_MODE": "assisted"}, clear=False),
            self.assertRaises(codex_proxy.LifecycleEmptyFinalResponseError),
        ):
            CodexProxyHandler._relay_upstream_response(
                handler,
                response,
                "ollama_cloud",
                request_id="req_empty_final",
                model="ollama-cloud/kimi-k2.7-code",
                upstream_format="chat_completions",
                inbound_format="responses",
                caller_stream=True,
                event_context=event_context,
            )

        self.assertIsNone(handler.status)
        self.assertEqual(handler.wfile.writes, [])
        matching_events = [
            call
            for call in self.write_proxy_event.call_args_list
            if call.args and call.args[0] == "lifecycle_empty_final_resample"
        ]
        self.assertEqual(len(matching_events), 1)
        self.assertEqual(matching_events[0].kwargs["text_chars"], 0)
        self.assertEqual(matching_events[0].kwargs["tool_call_count"], 0)

    def test_lifecycle_final_format_violation_detects_preface_report(self):
        self.assertTrue(
            codex_proxy._lifecycle_final_format_violation(
                "All stages passed.\n\n"
                "RESULT: PASS\n"
                "SENTINEL: SENTINEL:level2\n"
                "SUBAGENT_CHAIN: implementer,spec-reviewer,quality-reviewer"
            )
        )
        self.assertFalse(
            codex_proxy._lifecycle_final_format_violation(
                "RESULT: PASS\n"
                "SENTINEL: SENTINEL:level2\n"
                "SUBAGENT_CHAIN: implementer,spec-reviewer,quality-reviewer"
            )
        )

    def test_chat_sse_lifecycle_final_format_raises_retryable_without_downstream_write(self):
        handler = FakeHandler()
        chunks = [
            {
                "id": "chatcmpl_format_final",
                "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
            },
            {
                "id": "chatcmpl_format_final",
                "object": "chat.completion.chunk",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "content": (
                                "All stages passed.\n\n"
                                "RESULT: PASS\n"
                                "SENTINEL: SENTINEL:level2\n"
                                "SUBAGENT_CHAIN: implementer,spec-reviewer,quality-reviewer"
                            )
                        },
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": "chatcmpl_format_final",
                "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            },
        ]
        response = FakeSseResponse(
            [f"data: {json.dumps(chunk)}\n".encode("utf-8") for chunk in chunks] + [b"data: [DONE]\n", b""]
        )
        event_context = {
            "request_id": "req_format_final",
            "subagent_lifecycle_complete": True,
        }

        with (
            patch.dict(os.environ, {"CODEXHUB_SUBAGENT_ASSIST_MODE": "assisted"}, clear=False),
            self.assertRaises(codex_proxy.LifecycleFinalFormatResponseError),
        ):
            CodexProxyHandler._relay_upstream_response(
                handler,
                response,
                "ollama_cloud",
                request_id="req_format_final",
                model="ollama-cloud/glm-5.2",
                upstream_format="chat_completions",
                inbound_format="responses",
                caller_stream=True,
                event_context=event_context,
            )

        self.assertIsNone(handler.status)
        self.assertEqual(handler.wfile.writes, [])
        matching_events = [
            call
            for call in self.write_proxy_event.call_args_list
            if call.args and call.args[0] == "lifecycle_final_format_resample"
        ]
        self.assertEqual(len(matching_events), 1)
        self.assertGreater(matching_events[0].kwargs["text_chars"], 0)
        self.assertEqual(matching_events[0].kwargs["tool_call_count"], 0)

    def test_responses_sse_lifecycle_final_format_raises_retryable_without_downstream_write(self):
        handler = FakeHandler()
        bad_text = (
            "All stages passed.\n\n"
            "RESULT: PASS\n"
            "SENTINEL: SENTINEL:level2\n"
            "SUBAGENT_CHAIN: implementer,spec-reviewer,quality-reviewer"
        )
        events = [
            {"type": "response.created", "response": {"id": "resp_format", "status": "in_progress"}},
            {"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "delta": bad_text},
            {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": {
                    "id": "msg_format",
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": bad_text, "annotations": []}],
                },
            },
            {
                "type": "response.completed",
                "response": {
                    "id": "resp_format",
                    "status": "completed",
                    "output": [
                        {
                            "id": "msg_format",
                            "type": "message",
                            "role": "assistant",
                            "content": [{"type": "output_text", "text": bad_text, "annotations": []}],
                        }
                    ],
                },
            },
        ]
        response = FakeSseResponse([f"data: {json.dumps(event)}\n".encode("utf-8") for event in events] + [b""])
        event_context = {
            "request_id": "req_responses_format_final",
            "subagent_lifecycle_complete": True,
        }

        with (
            patch.dict(os.environ, {"CODEXHUB_SUBAGENT_ASSIST_MODE": "assisted"}, clear=False),
            self.assertRaises(codex_proxy.LifecycleFinalFormatResponseError),
        ):
            CodexProxyHandler._relay_upstream_response(
                handler,
                response,
                "ollama_cloud",
                request_id="req_responses_format_final",
                model="ollama-cloud/glm-5.2",
                upstream_format="responses",
                inbound_format="responses",
                caller_stream=True,
                event_context=event_context,
            )

        self.assertIsNone(handler.status)
        self.assertEqual(handler.wfile.writes, [])
        matching_events = [
            call
            for call in self.write_proxy_event.call_args_list
            if call.args and call.args[0] == "lifecycle_final_format_resample"
        ]
        self.assertEqual(len(matching_events), 1)
        self.assertEqual(matching_events[0].kwargs["tool_item_count"], 0)

    def test_post_retries_once_for_chat_lifecycle_empty_final_even_when_main_retry_is_one(self):
        self.external_model["upstream_format"] = "chat_completions"
        body = json.dumps(
            {
                "model": "volc/glm-5.2",
                "stream": True,
                "input": [
                    {"type": "message", "role": "user", "content": "Spawn one child, wait, close, then final."},
                    {
                        "type": "function_call",
                        "call_id": "call_spawn",
                        "name": "multi_agent_v1__spawn_agent",
                        "arguments": json.dumps({"message": "return child-ok", "nickname": "child"}),
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_spawn",
                        "output": json.dumps({"agent_id": "agent_1", "nickname": "child"}),
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_wait",
                        "name": "multi_agent_v1__wait_agent",
                        "arguments": json.dumps({"targets": ["agent_1"], "timeout_ms": 60000}),
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_wait",
                        "output": json.dumps({"timed_out": False, "status": {"agent_1": {"completed": "child-ok"}}}),
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_close",
                        "name": "multi_agent_v1__close_agent",
                        "arguments": json.dumps({"target": "agent_1"}),
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_close",
                        "output": json.dumps({"previous_status": {"completed": "child-ok"}}),
                    },
                ],
                "tools": [
                    {"type": "function", "name": "multi_agent_v1__spawn_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__wait_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__close_agent", "parameters": {"type": "object"}},
                ],
            }
        ).encode("utf-8")
        handler = CodexProxyHandler.__new__(CodexProxyHandler)
        handler.path = "/v1/responses"
        handler.headers = {
            "Content-Length": str(len(body)),
            "Content-Type": "application/json",
            "X-Codex-Window-Id": "thread-empty-final:turn",
        }
        handler.rfile = io.BytesIO(body)
        handler.close_connection = False
        fake = FakeHandler()
        handler.send_response = fake.send_response
        handler.send_header = fake.send_header
        handler.end_headers = fake.end_headers
        handler.wfile = fake.wfile
        empty_chunks = [
            {
                "id": "chatcmpl_empty_final",
                "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
            },
            {
                "id": "chatcmpl_empty_final",
                "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            },
        ]
        final_chunks = [
            {
                "id": "chatcmpl_final",
                "object": "chat.completion.chunk",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "content": "RESULT: PASS\nSENTINEL: child-ok\nCLOSED: yes"},
                        "finish_reason": "stop",
                    }
                ],
            },
        ]
        empty_response = FakeSseResponse(
            [f"data: {json.dumps(chunk)}\n".encode("utf-8") for chunk in empty_chunks] + [b"data: [DONE]\n", b""]
        )
        final_response = FakeSseResponse(
            [f"data: {json.dumps(chunk)}\n".encode("utf-8") for chunk in final_chunks] + [b"data: [DONE]\n", b""]
        )

        with (
            patch.dict(
                os.environ,
                {
                    "CODEXHUB_SUBAGENT_ASSIST_MODE": "assisted",
                    "CODEX_PROXY_MAIN_GENERATION_RETRY_MAX_ATTEMPTS": "1",
                },
                clear=False,
            ),
            patch("codex_proxy.urlopen", side_effect=[empty_response, final_response]) as mock_urlopen,
            patch("codex_proxy.time.sleep") as mock_sleep,
        ):
            CodexProxyHandler.do_POST(handler)

        self.assertEqual(mock_urlopen.call_count, 2)
        mock_sleep.assert_called_once_with(0)
        data = b"".join(fake.wfile.writes)
        self.assertIn(b"codexhub.retry", data)
        self.assertIn(b"RESULT: PASS", data)
        resample_events = [
            call.kwargs
            for call in self.write_proxy_event.call_args_list
            if call.args and call.args[0] == "lifecycle_empty_final_resample"
        ]
        retry_events = [
            call.kwargs
            for call in self.write_proxy_event.call_args_list
            if call.args and call.args[0] == "upstream_retry"
        ]
        self.assertEqual(len(resample_events), 1)
        self.assertEqual(len(retry_events), 1)
        self.assertEqual(retry_events[0]["error"], "LifecycleEmptyFinalResponseError")
        self.assertEqual(retry_events[0]["max_attempts"], 2)
        self.assertEqual(retry_events[0]["delay_ms"], 0)

    def test_post_retries_once_for_chat_lifecycle_final_format_and_injects_guidance(self):
        self.external_model["upstream_format"] = "chat_completions"
        body = json.dumps(
            {
                "model": "volc/glm-5.2",
                "stream": True,
                "input": [
                    {"type": "message", "role": "user", "content": "Spawn one child, wait, close, then final."},
                    {
                        "type": "function_call",
                        "call_id": "call_spawn",
                        "name": "multi_agent_v1__spawn_agent",
                        "arguments": json.dumps({"message": "return child-ok", "nickname": "child"}),
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_spawn",
                        "output": json.dumps({"agent_id": "agent_1", "nickname": "child"}),
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_wait",
                        "name": "multi_agent_v1__wait_agent",
                        "arguments": json.dumps({"targets": ["agent_1"], "timeout_ms": 60000}),
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_wait",
                        "output": json.dumps({"timed_out": False, "status": {"agent_1": {"completed": "child-ok"}}}),
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_close",
                        "name": "multi_agent_v1__close_agent",
                        "arguments": json.dumps({"target": "agent_1"}),
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_close",
                        "output": json.dumps({"previous_status": {"completed": "child-ok"}}),
                    },
                ],
                "tools": [
                    {"type": "function", "name": "multi_agent_v1__spawn_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__wait_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__close_agent", "parameters": {"type": "object"}},
                ],
            }
        ).encode("utf-8")
        handler = CodexProxyHandler.__new__(CodexProxyHandler)
        handler.path = "/v1/responses"
        handler.headers = {
            "Content-Length": str(len(body)),
            "Content-Type": "application/json",
            "X-Codex-Window-Id": "thread-format-final:turn",
        }
        handler.rfile = io.BytesIO(body)
        handler.close_connection = False
        fake = FakeHandler()
        handler.send_response = fake.send_response
        handler.send_header = fake.send_header
        handler.end_headers = fake.end_headers
        handler.wfile = fake.wfile
        bad_text = (
            "All stages passed.\n\n"
            "RESULT: PASS\n"
            "SENTINEL: SENTINEL:level2\n"
            "SUBAGENT_CHAIN: implementer,spec-reviewer,quality-reviewer"
        )
        good_text = (
            "RESULT: PASS\n"
            "SENTINEL: SENTINEL:level2\n"
            "SUBAGENT_CHAIN: implementer,spec-reviewer,quality-reviewer"
        )
        bad_chunks = [
            {
                "id": "chatcmpl_format_final",
                "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": bad_text}, "finish_reason": "stop"}],
            },
        ]
        good_chunks = [
            {
                "id": "chatcmpl_final",
                "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": good_text}, "finish_reason": "stop"}],
            },
        ]
        bad_response = FakeSseResponse(
            [f"data: {json.dumps(chunk)}\n".encode("utf-8") for chunk in bad_chunks] + [b"data: [DONE]\n", b""]
        )
        good_response = FakeSseResponse(
            [f"data: {json.dumps(chunk)}\n".encode("utf-8") for chunk in good_chunks] + [b"data: [DONE]\n", b""]
        )

        with (
            patch.dict(
                os.environ,
                {
                    "CODEXHUB_SUBAGENT_ASSIST_MODE": "assisted",
                    "CODEX_PROXY_MAIN_GENERATION_RETRY_MAX_ATTEMPTS": "1",
                },
                clear=False,
            ),
            patch("codex_proxy.urlopen", side_effect=[bad_response, good_response]) as mock_urlopen,
            patch("codex_proxy.time.sleep") as mock_sleep,
        ):
            CodexProxyHandler.do_POST(handler)

        self.assertEqual(mock_urlopen.call_count, 2)
        mock_sleep.assert_called_once_with(0)
        second_request = mock_urlopen.call_args_list[1].args[0]
        self.assertIn(b"lifecycle_complete_final_retry", second_request.data)
        self.assertIn(b"retry_reason: format", second_request.data)
        data = b"".join(fake.wfile.writes)
        self.assertIn(b"codexhub.retry", data)
        self.assertIn(b"RESULT: PASS", data)
        resample_events = [
            call.kwargs
            for call in self.write_proxy_event.call_args_list
            if call.args and call.args[0] == "lifecycle_final_format_resample"
        ]
        retry_events = [
            call.kwargs
            for call in self.write_proxy_event.call_args_list
            if call.args and call.args[0] == "upstream_retry"
        ]
        self.assertEqual(len(resample_events), 1)
        self.assertEqual(len(retry_events), 1)
        self.assertEqual(retry_events[0]["error"], "LifecycleFinalFormatResponseError")
        self.assertEqual(retry_events[0]["max_attempts"], 2)
        self.assertEqual(retry_events[0]["delay_ms"], 0)

    def test_responses_sse_passthrough_without_terminal_writes_sse_error(self):
        handler = FakeHandler()
        response = FakeSseResponse(
            [
                b'data: {"type":"response.created","response":{"id":"resp_1","model":"gpt-5.5"}}\n\n',
                b'data: {"type":"response.output_text.delta","delta":"partial"}\n\n',
                b"",
            ]
        )

        status = CodexProxyHandler._relay_upstream_response(
            handler,
            response,
            "official",
            request_id="req_incomplete_responses_passthrough",
            model="openai/gpt-5.5",
            upstream_format="responses",
            inbound_format="responses",
            caller_stream=True,
        )

        data = b"".join(handler.wfile.writes)
        self.assertEqual(status, 502)
        self.assertIn(b"partial", data)
        self.assertIn(b"upstream_stream_incomplete", data)
        self.assertNotIn(b"response.completed", data)

    def test_chat_sse_without_terminal_writes_sse_error(self):
        handler = FakeHandler()
        response = FakeSseResponse(
            [
                b'data: {"id":"chatcmpl_1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"partial"},"finish_reason":null}]}\n\n',
                b"",
            ]
        )

        status = CodexProxyHandler._relay_upstream_response(
            handler,
            response,
            "ollama_cloud",
            request_id="req_incomplete_chat_sse",
            model="ollama-cloud/glm-5.2",
            upstream_format="chat_completions",
            inbound_format="chat_completions",
            caller_stream=True,
        )

        data = b"".join(handler.wfile.writes)
        self.assertEqual(status, 502)
        self.assertIn(b"upstream_stream_incomplete", data)
        self.assertNotIn(b"data: [DONE]", data)

    def test_responses_sse_passthrough_aborts_before_output_idle_timeout(self):
        handler = FakeHandler()
        response = FakeSequencedDelayedSseResponse(
            [
                (0, b'data: {"type":"response.created","response":{"id":"resp_pre","model":"gpt-5.5"}}\n\n'),
                (0.005, b": upstream keepalive\n\n"),
                (0.005, b": upstream keepalive\n\n"),
                (0.005, b": upstream keepalive\n\n"),
                (0.005, b": upstream keepalive\n\n"),
                (0.005, b'data: {"type":"response.output_text.delta","delta":"late"}\n\n'),
                (0, b'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'),
                (0, b""),
            ]
        )

        with patch.dict(
            os.environ,
            {
                "CODEX_PROXY_PRE_OUTPUT_SSE_IDLE_TIMEOUT_SECONDS": "0.02",
                "CODEX_PROXY_POST_CONTENT_SSE_IDLE_TIMEOUT_SECONDS": "10",
            },
            clear=False,
        ):
            status = CodexProxyHandler._relay_upstream_response(
                handler,
                response,
                "official",
                request_id="req_pre_output_idle",
                model="openai/gpt-5.5",
                upstream_format="responses",
                inbound_format="responses",
                caller_stream=True,
            )

        data = b"".join(handler.wfile.writes)
        self.assertEqual(status, 502)
        self.assertIn(b"upstream_stream_idle_timeout", data)
        self.assertNotIn(b"late", data)
        matching_events = [
            call
            for call in self.write_proxy_event.call_args_list
            if call.args and call.args[0] == "upstream_stream_idle_timeout"
        ]
        self.assertTrue(matching_events)
        event_kwargs = matching_events[-1].kwargs
        self.assertEqual(event_kwargs["stream_idle_timeout_seconds"], 0.02)
        self.assertEqual(event_kwargs["stream_idle_phase"], "pre_output")

    def test_responses_sse_passthrough_aborts_after_output_idle_timeout(self):
        handler = FakeHandler()
        response = FakeSequencedDelayedSseResponse(
            [
                (0, b'data: {"type":"response.created","response":{"id":"resp_1","model":"gpt-5.5"}}\n\n'),
                (0, b'data: {"type":"response.output_text.delta","delta":"partial"}\n\n'),
                (0.005, b": upstream keepalive\n\n"),
                (0.005, b": upstream keepalive\n\n"),
                (0.005, b": upstream keepalive\n\n"),
                (0.005, b": upstream keepalive\n\n"),
                (0.005, b'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'),
                (0, b""),
            ]
        )

        with patch.dict(os.environ, {"CODEX_PROXY_POST_CONTENT_SSE_IDLE_TIMEOUT_SECONDS": "0.02"}, clear=False):
            status = CodexProxyHandler._relay_upstream_response(
                handler,
                response,
                "official",
                request_id="req_output_idle",
                model="openai/gpt-5.5",
                upstream_format="responses",
                inbound_format="responses",
                caller_stream=True,
            )

        data = b"".join(handler.wfile.writes)
        self.assertEqual(status, 502)
        self.assertIn(b"partial", data)
        self.assertIn(b"upstream_stream_idle_timeout", data)
        self.assertNotIn(b"response.completed", data)
        matching_events = [
            call
            for call in self.write_proxy_event.call_args_list
            if call.args and call.args[0] == "upstream_stream_idle_timeout"
        ]
        self.assertTrue(matching_events)
        event_kwargs = matching_events[-1].kwargs
        self.assertEqual(event_kwargs["stream_idle_timeout_seconds"], 0.02)
        self.assertEqual(event_kwargs["stream_idle_phase"], "post_output")
        self.assertTrue(event_kwargs["downstream_output_started"])

    def test_ollama_non_sse_relay_removes_plaintext_reasoning_encrypted_content(self):
        handler = FakeHandler()
        body = json.dumps(
            {
                "output": [
                    {
                        "type": "reasoning",
                        "summary": [{"type": "summary_text", "text": "third party summary"}],
                        "encrypted_content": "The user just typed test.",
                    }
                ]
            }
        ).encode("utf-8")
        response = FakeResponse(body)

        CodexProxyHandler._relay_upstream_response(handler, response, "ollama_cloud")

        payload = json.loads(handler.wfile.writes[0])
        self.assertNotIn("encrypted_content", payload["output"][0])
        self.assertEqual(payload["output"][0]["summary"], [])

    def test_external_non_sse_relay_copies_reasoning_content_to_summary_for_codex_app(self):
        handler = FakeHandler()
        body = json.dumps(
            {
                "output": [
                    {
                        "type": "reasoning",
                        "summary": [],
                        "content": [{"type": "reasoning_text", "text": "raw third party thinking"}],
                    }
                ]
            }
        ).encode("utf-8")
        response = FakeResponse(body)

        CodexProxyHandler._relay_upstream_response(handler, response, "minimax")

        payload = json.loads(handler.wfile.writes[0])
        self.assertEqual(payload["output"][0]["summary"], [])
        self.assertNotIn("content", payload["output"][0])

    def test_external_non_sse_relay_downgrades_invalid_function_call_name(self):
        handler = FakeHandler()
        body = json.dumps(
            {
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call_bad",
                        "name": "shell_command` <- malformed name",
                        "arguments": "{\"command\":\"echo bad\"}",
                    }
                ]
            }
        ).encode("utf-8")
        response = FakeResponse(body)

        CodexProxyHandler._relay_upstream_response(handler, response, "volcengine")

        payload = json.loads(handler.wfile.writes[0])
        self.assertEqual(payload["output"][0]["type"], "message")
        self.assertEqual(payload["output"][0]["role"], "assistant")
        self.assertIn("Invalid third-party function call transcript", payload["output"][0]["content"][0]["text"])
        self.assertIn("shell_command`", payload["output"][0]["content"][0]["text"])

    def test_official_request_downgrades_invalid_tool_history_without_system_role(self):
        body = json.dumps(
            {
                "model": "gpt-5.5",
                "input": [
                    {"type": "function_call", "call_id": "call_bad", "name": "[Codex", "arguments": "{}"},
                    {"type": "function_call_output", "call_id": "call_bad", "output": "unsupported call"},
                ],
            }
        ).encode("utf-8")

        transformed = compatible_request_body(body, {"name": "official", "auth": "codex_auth"})
        payload = json.loads(transformed)

        self.assertEqual(payload["input"][0]["role"], "assistant")
        self.assertEqual(payload["input"][1]["role"], "assistant")
        self.assertNotIn('"role":"system"', transformed.decode("utf-8"))

    def test_official_request_downgrades_system_message_history(self):
        body = json.dumps(
            {
                "model": "gpt-5.5",
                "input": [
                    {"type": "message", "role": "user", "content": "Continue."},
                    {
                        "type": "message",
                        "role": "system",
                        "content": "Codex native mcp__node_repl.js result\nstatus: completed",
                    },
                ],
            }
        ).encode("utf-8")

        transformed = compatible_request_body(body, {"name": "official", "auth": "codex_auth"})
        payload = json.loads(transformed)

        self.assertEqual(payload["input"][1]["role"], "developer")
        self.assertIn("Codex native mcp__node_repl.js result", payload["input"][1]["content"])
        self.assertNotIn('"role":"system"', transformed.decode("utf-8"))

    def test_official_request_does_not_inject_explicit_codex_native_tools(self):
        body = json.dumps({"model": "gpt-5.5", "input": "hi"}).encode("utf-8")

        transformed = compatible_request_body(body, {"name": "official", "auth": "codex_auth"})
        payload = json.loads(transformed)
        tools_by_name = {tool["name"]: tool for tool in payload.get("tools", []) if tool.get("type") == "function"}

        self.assertNotIn("tool_search", tools_by_name)
        self.assertNotIn("multi_agent_v1__spawn_agent", tools_by_name)
        self.assertNotIn("mcp__node_repl__js", tools_by_name)

    def test_official_browser_context_injects_skill_guidance_without_tools(self):
        body = json.dumps(
            {
                "model": "gpt-5.5",
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": "# In app browser\nCurrent URL: https://example.test/page",
                    }
                ],
            }
        ).encode("utf-8")

        transformed = compatible_request_body(body, {"name": "official", "auth": "codex_auth"})
        payload = json.loads(transformed)
        transcript = json.dumps(payload, ensure_ascii=True)
        tools_by_name = {tool["name"]: tool for tool in payload.get("tools", []) if tool.get("type") == "function"}

        self.assertNotIn("tool_search", tools_by_name)
        self.assertNotIn("mcp__node_repl__js", tools_by_name)
        self.assertIn("browser:control-in-app-browser", transcript)
        self.assertIn("node_repl js", transcript)
        self.assertIn("browser session unavailable", transcript)
        self.assertEqual(payload["input"][1]["role"], "developer")
        self.assertNotIn('"role":"system"', transformed.decode("utf-8"))

    def test_external_request_injects_explicit_codex_native_tools(self):
        body = json.dumps({"model": "glm-5.2", "input": "spawn a child"}).encode("utf-8")

        transformed = compatible_request_body(body, {"name": "ollama_cloud"}, event_context={"request_id": "req"})
        payload = json.loads(transformed)
        tools_by_name = {tool["name"]: tool for tool in payload["tools"]}

        self.assertNotIn("tool_search", tools_by_name)
        self.assertIn("multi_agent_v1__spawn_agent", tools_by_name)
        self.assertIn("multi_agent_v1__wait_agent", tools_by_name)
        self.assertIn("multi_agent_v1__close_agent", tools_by_name)
        self.assertIn("multi_agent_v1__resume_agent", tools_by_name)
        self.assertIn("multi_agent_v1__send_input", tools_by_name)

    def test_responses_structured_provider_preserves_multi_agent_tool_history(self):
        body = json.dumps(
            {
                "model": "glm-5.2",
                "input": [
                    {"type": "message", "role": "user", "content": "Use one subagent."},
                    {
                        "type": "function_call",
                        "call_id": "call_spawn",
                        "namespace": "multi_agent_v1",
                        "name": "spawn_agent",
                        "arguments": {"message": "return child-ok"},
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_spawn",
                        "output": json.dumps({"agent_id": "019f-child", "nickname": "child"}),
                    },
                ],
            }
        ).encode("utf-8")

        transformed = compatible_request_body(
            body,
            {
                "name": "ollama_cloud",
                "upstream_format": "responses",
                "tool_protocol": "responses_structured",
            },
            event_context={"request_id": "req"},
        )
        payload = json.loads(transformed)
        transcript = json.dumps(payload, ensure_ascii=True)

        self.assertEqual(payload["input"][1]["type"], "function_call")
        self.assertEqual(payload["input"][1]["name"], "multi_agent_v1__spawn_agent")
        self.assertNotIn("namespace", payload["input"][1])
        self.assertEqual(payload["input"][2]["type"], "function_call_output")
        self.assertEqual(payload["input"][2]["call_id"], "call_spawn")
        self.assertIn('"agent_id": "019f-child"', payload["input"][2]["output"])
        self.assertNotIn("Codex native multi_agent_v1.spawn_agent result", transcript)

    def test_responses_structured_flat_history_uses_event_led_subagent_state(self):
        body = json.dumps(
            {
                "model": "glm-5.2",
                "input": [
                    {"type": "message", "role": "user", "content": "Spawn exactly one child agent, wait, close, final."},
                    {
                        "type": "function_call",
                        "call_id": "call_spawn",
                        "name": "multi_agent_v1__spawn_agent",
                        "arguments": json.dumps({"message": "return child-ok"}),
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_spawn",
                        "output": json.dumps({"agent_id": "019f-child", "nickname": "child"}),
                    },
                ],
                "tools": [
                    {"type": "function", "name": "multi_agent_v1__spawn_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__wait_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__close_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__resume_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__send_input", "parameters": {"type": "object"}},
                ],
            }
        ).encode("utf-8")
        event_context = {"request_id": "req"}

        transformed = compatible_request_body(
            body,
            {
                "name": "ollama_cloud",
                "upstream_format": "responses",
                "tool_protocol": "responses_structured",
            },
            event_context=event_context,
        )
        payload = json.loads(transformed)
        tools_by_name = {tool["name"]: tool for tool in payload["tools"] if tool.get("type") == "function"}

        self.assertNotIn("multi_agent_v1__spawn_agent", tools_by_name)
        self.assertIn("multi_agent_v1__wait_agent", tools_by_name)
        self.assertNotIn("multi_agent_v1__close_agent", tools_by_name)
        self.assertEqual(event_context["subagent_open_agent_ids"], ["019f-child"])
        self.assertEqual(event_context["subagent_wait_agent_ids"], ["019f-child"])
        self.assertFalse(event_context["subagent_spawn_allowed"])

    def test_chat_tools_workflow_spawned_implementer_exposes_wait_not_spawn(self):
        workflow_prompt = """
Use the real subagent-driven-development skill.

Execution constraints:
1. Use real Codex native subagents for implementer, spec reviewer, and code-quality reviewer.
2. The implementer creates exactly one diagnostic artifact.
3. The spec reviewer verifies exact file content.
4. The code-quality reviewer verifies minimal implementation.
5. If a reviewer finds issues, route fixes back to the existing implementer path.
6. Spawn exactly one implementer, wait for it, close it, then spawn exactly one spec reviewer, wait for it, close it, then spawn exactly one code-quality reviewer, wait for it, and close it.
"""
        body = json.dumps(
            {
                "model": "minimax-m3",
                "input": [
                    {"type": "message", "role": "user", "content": workflow_prompt},
                    {
                        "type": "function_call",
                        "call_id": "call_impl",
                        "name": "multi_agent_v1__spawn_agent",
                        "arguments": json.dumps(
                            {
                                "message": "You are the IMPLEMENTER subagent in a diagnostic chain.",
                                "nickname": "implementer",
                            }
                        ),
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_impl",
                        "output": json.dumps({"agent_id": "impl-1", "nickname": "implementer"}),
                    },
                ],
                "tools": [
                    {"type": "function", "name": "multi_agent_v1__spawn_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__wait_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__close_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__resume_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__send_input", "parameters": {"type": "object"}},
                ],
            }
        ).encode("utf-8")
        event_context = {"request_id": "req"}

        transformed = compatible_request_body(
            body,
            {
                "name": "ollama_cloud",
                "upstream_format": "chat_completions",
                "tool_protocol": "chat_tools",
            },
            event_context=event_context,
        )
        payload = json.loads(transformed)
        tools_by_name = {tool["name"]: tool for tool in payload["tools"] if tool.get("type") == "function"}
        transcript = json.dumps(payload, ensure_ascii=False)

        self.assertNotIn("multi_agent_v1__spawn_agent", tools_by_name)
        self.assertIn("multi_agent_v1__wait_agent", tools_by_name)
        self.assertEqual(event_context["subagent_wait_agent_ids"], ["impl-1"])
        self.assertFalse(event_context["subagent_spawn_allowed"])
        self.assertIn("status: spawned_child_wait_required", transcript)
        self.assertEqual(
            payload["tool_choice"],
            {"type": "function", "name": "multi_agent_v1__wait_agent"},
        )

    def test_guided_mode_injects_subagent_state_but_does_not_repair_response(self):
        request_body = json.dumps(
            {
                "model": "ollama-e2e-responses/glm-5.2",
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": "Use subagent-driven-development with implementer, spec reviewer, and code quality reviewer.",
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_spawn",
                        "namespace": "multi_agent_v1",
                        "name": "spawn_agent",
                        "arguments": json.dumps({"message": "implement", "nickname": "implementer"}),
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_spawn",
                        "output": json.dumps({"agent_id": "agent_1"}),
                    },
                ],
                "tools": [{"type": "function", "name": "multi_agent_v1__wait_agent", "parameters": {"type": "object"}}],
            }
        ).encode("utf-8")

        with patch.dict(os.environ, {"CODEXHUB_SUBAGENT_ASSIST_MODE": "guided"}, clear=False):
            transformed_request = compatible_request_body(
                request_body,
                {
                    "name": "ollama_cloud",
                    "upstream_format": "responses",
                    "tool_protocol": "responses_structured",
                },
                event_context={"request_id": "req"},
            )

        request_payload = json.loads(transformed_request)
        request_text = json.dumps(request_payload, ensure_ascii=False)
        self.assertIn("required_next_action", request_text)

        response_body = json.dumps(
            {
                "id": "resp_text",
                "object": "response",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "I will continue."}],
                    }
                ],
            }
        ).encode("utf-8")

        with patch.dict(os.environ, {"CODEXHUB_SUBAGENT_ASSIST_MODE": "guided"}, clear=False):
            transformed_response = compatible_response_body(
                response_body,
                "ollama_cloud",
                event_context={
                    "tool_protocol": "responses_structured",
                    "subagent_wait_agent_ids": ["agent_1"],
                    "subagent_close_agent_ids": [],
                    "subagent_spawn_allowed": False,
                    "subagent_lifecycle_complete": False,
                },
            )

        response_payload = json.loads(transformed_response)
        self.assertEqual(response_payload["output"][0]["type"], "message")
        self.assertNotIn("wait_agent", json.dumps(response_payload))

    def test_responses_structured_worker_subagent_prompt_does_not_force_spawn(self):
        worker_prompt = r"""
You are an implementer subagent. Your task is to create exactly one diagnostic artifact file.

Create the directory structure if it doesn't already exist, then create the file at this exact path:
C:\repo\diagnostics\artifact.txt

Do not modify any other files. Do not create any other files. Use a shell command to create the file.
"""
        body = json.dumps(
            {
                "model": "glm-5.2",
                "input": [{"type": "message", "role": "user", "content": worker_prompt}],
                "tools": [
                    {
                        "type": "namespace",
                        "name": "mcp__node_repl",
                        "tools": [{"type": "function", "name": "js", "parameters": {"type": "object"}}],
                    },
                    {
                        "type": "function",
                        "name": "mcp__codex_apps__local_tool_gateway___run_shell",
                        "parameters": {"type": "object"},
                    },
                    {"type": "function", "name": "mcp__codex_apps__github___fetch", "parameters": {"type": "object"}},
                    {"type": "function", "name": "tool_search", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__spawn_agent", "parameters": {"type": "object"}},
                ],
            }
        ).encode("utf-8")
        event_context = {"request_id": "req"}

        transformed = compatible_request_body(
            body,
            {
                "name": "ollama_cloud",
                "upstream_format": "responses",
                "tool_protocol": "responses_structured",
            },
            event_context=event_context,
        )
        payload = json.loads(transformed)
        tools_by_name = {tool["name"]: tool for tool in payload["tools"] if tool.get("type") == "function"}
        transcript = json.dumps(payload, ensure_ascii=False)

        self.assertTrue(event_context["subagent_worker_context"])
        self.assertFalse(event_context["subagent_spawn_allowed"])
        self.assertNotIn("mcp__node_repl__js", tools_by_name)
        self.assertNotIn("mcp__codex_apps__local_tool_gateway___run_shell", tools_by_name)
        self.assertNotIn("mcp__codex_apps__github___fetch", tools_by_name)
        self.assertNotIn("tool_search", tools_by_name)
        self.assertNotIn("multi_agent_v1__spawn_agent", tools_by_name)
        self.assertFalse(any(tool.get("type") == "namespace" and str(tool.get("name", "")).startswith("mcp__") for tool in payload["tools"]))
        self.assertNotIn("tool_choice", payload)
        self.assertIn("worker_subagent_finalization_required", transcript)
        self.assertIn("ordinary assistant message content", transcript)

    def test_responses_structured_task_worker_prompt_does_not_force_spawn(self):
        worker_prompt = r"""
You are implementing Task 1: Write The Diagnostic Artifact

## Task Description

Create exactly one text file at the following path:
C:\repo\diagnostics\artifact.txt

## Your Job

1. Create the directory structure if it does not already exist.
2. Do NOT modify any other files.
3. Do NOT commit anything.

Work from: C:\repo

## Report Format

When done, report:
- **Status:** DONE | BLOCKED
"""
        body = json.dumps(
            {
                "model": "glm-5.2",
                "input": [{"type": "message", "role": "user", "content": worker_prompt}],
                "tools": [
                    {
                        "type": "function",
                        "name": "mcp__codex_apps__local_tool_gateway___run_shell",
                        "parameters": {"type": "object"},
                    },
                    {"type": "function", "name": "multi_agent_v1__spawn_agent", "parameters": {"type": "object"}},
                ],
            }
        ).encode("utf-8")
        event_context = {"request_id": "req"}

        transformed = compatible_request_body(
            body,
            {
                "name": "ollama_cloud",
                "upstream_format": "responses",
                "tool_protocol": "responses_structured",
            },
            event_context=event_context,
        )
        payload = json.loads(transformed)
        tools_by_name = {tool["name"]: tool for tool in payload["tools"] if tool.get("type") == "function"}
        transcript = json.dumps(payload, ensure_ascii=False)

        self.assertTrue(event_context["subagent_worker_context"])
        self.assertFalse(event_context["subagent_spawn_allowed"])
        self.assertNotIn("mcp__codex_apps__local_tool_gateway___run_shell", tools_by_name)
        self.assertNotIn("multi_agent_v1__spawn_agent", tools_by_name)
        self.assertNotIn("tool_choice", payload)
        self.assertIn("worker_subagent_finalization_required", transcript)

    def test_responses_structured_role_header_worker_prompt_does_not_force_spawn(self):
        worker_prompt = r"""
Role: implementer
Task: Write the diagnostic artifact for the level2 E2E case.

You must create exactly one text file at this path:
C:\repo\diagnostics\artifact.txt
"""
        body = json.dumps(
            {
                "model": "kimi-k2.7-code",
                "input": [{"type": "message", "role": "user", "content": worker_prompt}],
                "tools": [
                    {
                        "type": "function",
                        "name": "mcp__codex_apps__local_tool_gateway___run_shell",
                        "parameters": {"type": "object"},
                    },
                    {"type": "function", "name": "multi_agent_v1__spawn_agent", "parameters": {"type": "object"}},
                ],
            }
        ).encode("utf-8")
        event_context = {"request_id": "req"}

        transformed = compatible_request_body(
            body,
            {
                "name": "ollama_cloud",
                "upstream_format": "responses",
                "tool_protocol": "responses_structured",
            },
            event_context=event_context,
        )
        payload = json.loads(transformed)
        tools_by_name = {tool["name"]: tool for tool in payload["tools"] if tool.get("type") == "function"}
        transcript = json.dumps(payload, ensure_ascii=False)

        self.assertTrue(event_context["subagent_worker_context"])
        self.assertNotIn("mcp__codex_apps__local_tool_gateway___run_shell", tools_by_name)
        self.assertNotIn("multi_agent_v1__spawn_agent", tools_by_name)
        self.assertNotIn("tool_choice", payload)
        self.assertIn("worker_subagent_finalization_required", transcript)

    def test_responses_structured_diagnostic_reviewer_without_subagent_word_does_not_force_spawn(self):
        worker_prompt = r"""
You are a spec compliance reviewer for a diagnostic test. Your job is to verify that a file was created with exact content matching the specification. Do not modify any files.

Check the file at this exact path:
C:\repo\diagnostics\artifact.txt

The required exact content is these four lines, each separated by a newline character (LF), with no BOM.

Report exactly one of:
- PASS - if the file matches the specification exactly
- FAIL - if any discrepancy is found
"""
        body = json.dumps(
            {
                "model": "glm-5.2",
                "input": [{"type": "message", "role": "user", "content": worker_prompt}],
                "tools": [
                    {
                        "type": "function",
                        "name": "mcp__codex_apps__local_tool_gateway___run_shell",
                        "parameters": {"type": "object"},
                    },
                    {"type": "function", "name": "multi_agent_v1__spawn_agent", "parameters": {"type": "object"}},
                ],
            }
        ).encode("utf-8")
        event_context = {"request_id": "req"}

        transformed = compatible_request_body(
            body,
            {
                "name": "ollama_cloud",
                "upstream_format": "responses",
                "tool_protocol": "responses_structured",
            },
            event_context=event_context,
        )
        payload = json.loads(transformed)
        tools_by_name = {tool["name"]: tool for tool in payload["tools"] if tool.get("type") == "function"}
        transcript = json.dumps(payload, ensure_ascii=False)

        self.assertTrue(event_context["subagent_worker_context"])
        self.assertFalse(event_context["subagent_spawn_allowed"])
        self.assertNotIn("mcp__codex_apps__local_tool_gateway___run_shell", tools_by_name)
        self.assertNotIn("multi_agent_v1__spawn_agent", tools_by_name)
        self.assertNotIn("tool_choice", payload)
        self.assertIn("worker_subagent_finalization_required", transcript)

    def test_responses_structured_code_quality_reviewer_without_subagent_word_does_not_force_spawn(self):
        worker_prompt = r"""
You are the code quality reviewer for a small diagnostic E2E case. The implementer was supposed to create exactly one diagnostic artifact and not modify any product source files. Verify both.

## Artifact path
C:\repo\diagnostics\artifact.txt

## Repo root
C:\repo

## What to check
1. Minimal implementation: the artifact exists and contains only the required diagnostic lines.
2. No product source modifications introduced after baseline.
"""
        body = json.dumps(
            {
                "model": "minimax-m3",
                "input": [{"type": "message", "role": "user", "content": worker_prompt}],
                "tools": [
                    {
                        "type": "function",
                        "name": "mcp__codex_apps__local_tool_gateway___run_shell",
                        "parameters": {"type": "object"},
                    },
                    {"type": "function", "name": "multi_agent_v1__spawn_agent", "parameters": {"type": "object"}},
                ],
            }
        ).encode("utf-8")
        event_context = {"request_id": "req"}

        transformed = compatible_request_body(
            body,
            {
                "name": "ollama_cloud",
                "upstream_format": "responses",
                "tool_protocol": "responses_structured",
            },
            event_context=event_context,
        )
        payload = json.loads(transformed)
        tools_by_name = {tool["name"]: tool for tool in payload["tools"] if tool.get("type") == "function"}
        transcript = json.dumps(payload, ensure_ascii=False)

        self.assertTrue(event_context["subagent_worker_context"])
        self.assertFalse(event_context["subagent_spawn_allowed"])
        self.assertNotIn("mcp__codex_apps__local_tool_gateway___run_shell", tools_by_name)
        self.assertNotIn("multi_agent_v1__spawn_agent", tools_by_name)
        self.assertNotIn("tool_choice", payload)
        self.assertIn("worker_subagent_finalization_required", transcript)

    def test_chat_tools_workflow_initial_request_requires_node_repl_plan_read_before_spawn(self):
        workflow_prompt = """
Use the real subagent-driven-development skill.

Execution constraints:
1. The coordinator may read the plan once with node_repl.
2. Spawn exactly one implementer, wait for it, close it, then spawn exactly one spec reviewer, wait for it, close it, then spawn exactly one code-quality reviewer, wait for it, and close it.
"""
        body = json.dumps(
            {
                "model": "minimax-m3",
                "input": [{"type": "message", "role": "user", "content": workflow_prompt}],
                "tools": [
                    {
                        "type": "namespace",
                        "name": "mcp__node_repl",
                        "tools": [{"type": "function", "name": "js", "parameters": {"type": "object"}}],
                    },
                    {
                        "type": "function",
                        "name": "mcp__codex_apps__local_tool_gateway___read_file",
                        "parameters": {"type": "object"},
                    },
                    {"type": "function", "name": "mcp__codex_apps__github___fetch", "parameters": {"type": "object"}},
                ],
            }
        ).encode("utf-8")
        event_context = {"request_id": "req"}

        transformed = compatible_request_body(
            body,
            {
                "name": "ollama_cloud",
                "upstream_format": "chat_completions",
                "tool_protocol": "chat_tools",
            },
            event_context=event_context,
        )
        payload = json.loads(transformed)
        tools_by_name = {tool["name"]: tool for tool in payload["tools"] if tool.get("type") == "function"}
        transcript = json.dumps(payload, ensure_ascii=False)

        self.assertNotIn("multi_agent_v1__spawn_agent", tools_by_name)
        self.assertIn("mcp__node_repl__js", tools_by_name)
        self.assertNotIn("multi_agent_v1__wait_agent", tools_by_name)
        self.assertNotIn("multi_agent_v1__close_agent", tools_by_name)
        self.assertNotIn("mcp__codex_apps__local_tool_gateway___read_file", tools_by_name)
        self.assertNotIn("mcp__codex_apps__github___fetch", tools_by_name)
        self.assertFalse(event_context["subagent_spawn_allowed"])
        self.assertIn("status: workflow_plan_read_required", transcript)
        self.assertIn("await import", transcript)
        self.assertEqual(
            payload["tool_choice"],
            {"type": "function", "name": "mcp__node_repl__js"},
        )

    def test_chat_tools_level_one_lifecycle_prompt_ignores_system_workflow_skill_examples(self):
        system_skill_text = """
Use subagent-driven-development with implementer, spec reviewer, and code quality reviewer.
Example Workflow:
Task 1: Hook installation script
Task 2: Recovery modes
"""
        level_one_prompt = """
Execute one real Codex native subagent lifecycle.

You are the coordinator. You must use the visible native subagent tools.

Required sequence:
1. Spawn exactly one child agent.
2. Wait for that child.
3. Close that child.
"""
        body = json.dumps(
            {
                "model": "minimax-m3",
                "input": [
                    {"type": "message", "role": "system", "content": system_skill_text},
                    {"type": "message", "role": "user", "content": level_one_prompt},
                ],
                "tools": [
                    {
                        "type": "namespace",
                        "name": "mcp__node_repl",
                        "tools": [{"type": "function", "name": "js", "parameters": {"type": "object"}}],
                    },
                ],
            }
        ).encode("utf-8")
        event_context = {"request_id": "req"}

        transformed = compatible_request_body(
            body,
            {
                "name": "ollama_cloud",
                "upstream_format": "chat_completions",
                "tool_protocol": "chat_tools",
            },
            event_context=event_context,
        )
        payload = json.loads(transformed)
        tools_by_name = {tool["name"]: tool for tool in payload["tools"] if tool.get("type") == "function"}
        transcript = json.dumps(payload, ensure_ascii=False)

        self.assertIn("multi_agent_v1__spawn_agent", tools_by_name)
        self.assertTrue(event_context["subagent_spawn_allowed"])
        self.assertNotIn("status: workflow_plan_read_required", transcript)
        self.assertEqual(
            payload["tool_choice"],
            {"type": "function", "name": "multi_agent_v1__spawn_agent"},
        )

    def test_responses_structured_repairs_missing_required_spawn_from_exact_child_prompt(self):
        level_one_prompt = """
Execute one real Codex native subagent lifecycle.

Required sequence:
1. Spawn exactly one child agent.
2. The child prompt must be exactly this complete string: `Return exactly this line: SENTINEL:A`
3. Wait for that child.
4. Close that child.
"""
        body = json.dumps(
            {
                "model": "minimax-m3",
                "input": [{"type": "message", "role": "user", "content": level_one_prompt}],
                "tools": [
                    {"type": "function", "name": "multi_agent_v1__spawn_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__wait_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__close_agent", "parameters": {"type": "object"}},
                ],
            }
        ).encode("utf-8")
        event_context = {"request_id": "req"}

        transformed = compatible_request_body(
            body,
            {"name": "ollama_cloud", "upstream_format": "responses", "tool_protocol": "responses_structured"},
            event_context=event_context,
        )
        payload = json.loads(transformed)

        self.assertEqual(payload["tool_choice"], {"type": "function", "name": "multi_agent_v1__spawn_agent"})
        self.assertEqual(
            event_context["subagent_required_spawn_arguments"],
            {"message": "Return exactly this line: SENTINEL:A", "fork_context": False},
        )

        response_body = json.dumps(
            {
                "id": "resp_text",
                "object": "response",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "I only see spawn_agent."}],
                    }
                ],
            }
        ).encode("utf-8")
        transformed_response = compatible_response_body(response_body, "ollama_cloud", event_context=event_context)
        call_item = json.loads(transformed_response)["output"][0]
        call_args = json.loads(call_item["arguments"])

        self.assertEqual(call_item["type"], "function_call")
        self.assertEqual(call_item["namespace"], "multi_agent_v1")
        self.assertEqual(call_item["name"], "spawn_agent")
        self.assertEqual(call_args["message"], "Return exactly this line: SENTINEL:A")
        self.assertFalse(call_args["fork_context"])

    def test_bounded_two_prompt_scheduler_uses_second_prompt_after_first_spawn(self):
        prompt = (
            "Spawn child A with prompt exactly this complete string: `Return A`\n"
            "Spawn child B with prompt exactly this complete string: `Return B`\n"
        )
        body = json.dumps(
            {
                "model": "ollama-e2e-responses/minimax-m3",
                "input": [
                    {"type": "message", "role": "user", "content": prompt},
                    {
                        "type": "function_call",
                        "call_id": "call_spawn_a",
                        "namespace": "multi_agent_v1",
                        "name": "spawn_agent",
                        "arguments": json.dumps({"message": "Return A", "fork_context": False}),
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_spawn_a",
                        "output": json.dumps({"agent_id": "agent-a"}),
                    },
                ],
                "tools": [
                    {"type": "function", "name": "multi_agent_v1__spawn_agent", "parameters": {"type": "object"}}
                ],
            }
        ).encode("utf-8")
        event_context = {"request_id": "req"}

        with patch.dict(os.environ, {"CODEXHUB_SUBAGENT_ASSIST_MODE": "assisted"}, clear=False):
            transformed = compatible_request_body(
                body,
                {"name": "ollama_cloud", "upstream_format": "responses", "tool_protocol": "responses_structured"},
                event_context=event_context,
            )

        self.assertIn("subagent_legal_actions", event_context)
        self.assertEqual(event_context["subagent_legal_actions"][0]["arguments"]["message"], "Return B")
        self.assertIn("Return B", transformed.decode("utf-8"))

    def test_chat_tools_level_one_lifecycle_prompt_ignores_developer_workflow_guidance(self):
        developer_skill_text = """
Use the real subagent-driven-development skill.
The coordinator must read the diagnostic plan before spawning.
Roles in this workflow are implementer, spec reviewer, and code quality reviewer.
Use mcp__node_repl__js to read the diagnostic plan before spawning any child agent.
"""
        level_one_prompt = """
Execute a bounded concurrent two-agent Codex native subagent lifecycle.

You are the coordinator. You must use the visible native subagent tools.

Required sequence:
1. Spawn child A with prompt exactly: Return exactly this line: SENTINEL:A
2. Spawn child B with prompt exactly: Return exactly this line: SENTINEL:B
3. Do not wait before both children have been spawned.
4. Wait for both exact child agent ids.
5. Close both exact child agent ids.
"""
        body = json.dumps(
            {
                "model": "minimax-m3",
                "input": [
                    {"type": "message", "role": "developer", "content": developer_skill_text},
                    {"type": "message", "role": "user", "content": level_one_prompt},
                ],
                "tools": [
                    {
                        "type": "namespace",
                        "name": "mcp__node_repl",
                        "tools": [{"type": "function", "name": "js", "parameters": {"type": "object"}}],
                    },
                ],
            }
        ).encode("utf-8")
        event_context = {"request_id": "req"}

        transformed = compatible_request_body(
            body,
            {
                "name": "ollama_cloud",
                "upstream_format": "chat_completions",
                "tool_protocol": "chat_tools",
            },
            event_context=event_context,
        )
        payload = json.loads(transformed)
        tools_by_name = {tool["name"]: tool for tool in payload["tools"] if tool.get("type") == "function"}
        transcript = json.dumps(payload, ensure_ascii=False)

        self.assertIn("multi_agent_v1__spawn_agent", tools_by_name)
        self.assertTrue(event_context["subagent_spawn_allowed"])
        self.assertNotIn("status: workflow_plan_read_required", transcript)
        self.assertNotIn("PLAN_PATH", transcript)
        self.assertEqual(
            payload["tool_choice"],
            {"type": "function", "name": "multi_agent_v1__spawn_agent"},
        )

    def test_chat_tools_workflow_failed_node_repl_plan_read_still_blocks_spawn(self):
        workflow_prompt = """
Use the real subagent-driven-development skill.

Execution constraints:
1. The coordinator may read the plan once with node_repl.
2. Spawn exactly one implementer, wait for it, close it, then spawn exactly one spec reviewer, wait for it, close it, then spawn exactly one code-quality reviewer, wait for it, and close it.
"""
        body = json.dumps(
            {
                "model": "minimax-m3",
                "input": [
                    {"type": "message", "role": "user", "content": workflow_prompt},
                    {
                        "type": "function_call",
                        "call_id": "call_node_plan",
                        "name": "mcp__node_repl__js",
                        "arguments": json.dumps({"code": "const fs = require('fs');"}),
                    },
                    {"type": "function_call_output", "call_id": "call_node_plan", "output": "require is not defined"},
                ],
                "tools": [
                    {
                        "type": "namespace",
                        "name": "mcp__node_repl",
                        "tools": [{"type": "function", "name": "js", "parameters": {"type": "object"}}],
                    },
                    {"type": "function", "name": "multi_agent_v1__spawn_agent", "parameters": {"type": "object"}},
                ],
            }
        ).encode("utf-8")
        event_context = {"request_id": "req"}

        transformed = compatible_request_body(
            body,
            {
                "name": "ollama_cloud",
                "upstream_format": "chat_completions",
                "tool_protocol": "chat_tools",
            },
            event_context=event_context,
        )
        payload = json.loads(transformed)
        tools_by_name = {tool["name"]: tool for tool in payload["tools"] if tool.get("type") == "function"}
        transcript = json.dumps(payload, ensure_ascii=False)

        self.assertNotIn("multi_agent_v1__spawn_agent", tools_by_name)
        self.assertIn("mcp__node_repl__js", tools_by_name)
        self.assertFalse(event_context["subagent_spawn_allowed"])
        self.assertIn("status: workflow_plan_read_required", transcript)
        self.assertEqual(
            payload["tool_choice"],
            {"type": "function", "name": "mcp__node_repl__js"},
        )

    def test_chat_tools_workflow_after_plan_read_hides_node_repl_and_other_mcp_tools(self):
        workflow_prompt = """
Use the real subagent-driven-development skill.

Execution constraints:
1. The coordinator may read the plan once with node_repl.
2. Spawn exactly one implementer, wait for it, close it, then spawn exactly one spec reviewer, wait for it, close it, then spawn exactly one code-quality reviewer, wait for it, and close it.
"""
        plan_text = """
# Short Subagent Development E2E Plan

The coordinator prompt supplies OUTPUT_PATH and SENTINEL.
Use an implementer subagent, then a spec reviewer, then a code quality reviewer.
"""
        body = json.dumps(
            {
                "model": "minimax-m3",
                "input": [
                    {"type": "message", "role": "user", "content": workflow_prompt},
                    {
                        "type": "function_call",
                        "call_id": "call_node_plan",
                        "name": "mcp__node_repl__js",
                        "arguments": json.dumps({"code": "read plan"}),
                    },
                    {"type": "function_call_output", "call_id": "call_node_plan", "output": plan_text},
                ],
                "tools": [
                    {
                        "type": "namespace",
                        "name": "mcp__node_repl",
                        "tools": [{"type": "function", "name": "js", "parameters": {"type": "object"}}],
                    },
                    {
                        "type": "function",
                        "name": "mcp__codex_apps__local_tool_gateway___read_file",
                        "parameters": {"type": "object"},
                    },
                    {"type": "function", "name": "mcp__codex_apps__github___fetch", "parameters": {"type": "object"}},
                ],
            }
        ).encode("utf-8")
        event_context = {"request_id": "req"}

        transformed = compatible_request_body(
            body,
            {
                "name": "ollama_cloud",
                "upstream_format": "chat_completions",
                "tool_protocol": "chat_tools",
            },
            event_context=event_context,
        )
        payload = json.loads(transformed)
        tools_by_name = {tool["name"]: tool for tool in payload["tools"] if tool.get("type") == "function"}
        transcript = json.dumps(payload, ensure_ascii=False)

        self.assertIn("multi_agent_v1__spawn_agent", tools_by_name)
        self.assertNotIn("mcp__node_repl__js", tools_by_name)
        self.assertNotIn("multi_agent_v1__wait_agent", tools_by_name)
        self.assertNotIn("multi_agent_v1__close_agent", tools_by_name)
        self.assertNotIn("mcp__codex_apps__local_tool_gateway___read_file", tools_by_name)
        self.assertNotIn("mcp__codex_apps__github___fetch", tools_by_name)
        self.assertTrue(event_context["subagent_spawn_allowed"])
        self.assertIn("status: next_subagent_spawn_required", transcript)
        self.assertIn("workflow_plan_read_status: completed_via_real_node_repl_current_turn", transcript)
        self.assertEqual(
            payload["tool_choice"],
            {"type": "function", "name": "multi_agent_v1__spawn_agent"},
        )

    def test_chat_tools_workflow_closed_implementer_exposes_spec_reviewer_spawn(self):
        workflow_prompt = """
Use the real subagent-driven-development skill.

Execution constraints:
1. Use real Codex native subagents for implementer, spec reviewer, and code-quality reviewer.
2. The implementer creates exactly one diagnostic artifact.
3. The spec reviewer verifies exact file content.
4. The code-quality reviewer verifies minimal implementation.
5. If a reviewer finds issues, route fixes back to the existing implementer path.
6. Spawn exactly one implementer, wait for it, close it, then spawn exactly one spec reviewer, wait for it, close it, then spawn exactly one code-quality reviewer, wait for it, and close it.
"""
        body = json.dumps(
            {
                "model": "minimax-m3",
                "input": [
                    {"type": "message", "role": "user", "content": workflow_prompt},
                    {
                        "type": "function_call",
                        "call_id": "call_impl",
                        "name": "multi_agent_v1__spawn_agent",
                        "arguments": json.dumps(
                            {
                                "message": "You are the IMPLEMENTER subagent in a diagnostic chain.",
                                "nickname": "implementer",
                            }
                        ),
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_impl",
                        "output": json.dumps({"agent_id": "impl-1", "nickname": "implementer"}),
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_impl_wait",
                        "name": "multi_agent_v1__wait_agent",
                        "arguments": json.dumps({"targets": ["impl-1"], "timeout_ms": 60000}),
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_impl_wait",
                        "output": json.dumps({"timed_out": False, "status": {"impl-1": {"completed": "DONE"}}}),
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_impl_close",
                        "name": "multi_agent_v1__close_agent",
                        "arguments": json.dumps({"target": "impl-1"}),
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_impl_close",
                        "output": json.dumps({"previous_status": {"completed": "DONE"}}),
                    },
                ],
                "tools": [
                    {"type": "function", "name": "multi_agent_v1__spawn_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__wait_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__close_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__resume_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__send_input", "parameters": {"type": "object"}},
                ],
            }
        ).encode("utf-8")
        event_context = {"request_id": "req"}

        transformed = compatible_request_body(
            body,
            {
                "name": "ollama_cloud",
                "upstream_format": "chat_completions",
                "tool_protocol": "chat_tools",
            },
            event_context=event_context,
        )
        payload = json.loads(transformed)
        tools_by_name = {tool["name"]: tool for tool in payload["tools"] if tool.get("type") == "function"}
        transcript = json.dumps(payload, ensure_ascii=False)

        self.assertIn("multi_agent_v1__spawn_agent", tools_by_name)
        self.assertNotIn("multi_agent_v1__wait_agent", tools_by_name)
        self.assertFalse(event_context["subagent_lifecycle_complete"])
        self.assertTrue(event_context["subagent_spawn_allowed"])
        self.assertIn("next_expected_role: spec_reviewer", transcript)

    def test_chat_tools_workflow_incomplete_implementer_exposes_close_before_retry_spawn(self):
        workflow_prompt = """
Use the real subagent-driven-development skill.

Execution constraints:
1. Use real Codex native subagents for implementer, spec reviewer, and code-quality reviewer.
2. The implementer creates exactly one diagnostic artifact.
3. The spec reviewer verifies exact file content.
4. The code-quality reviewer verifies minimal implementation.
5. If a reviewer finds issues, route fixes back to the existing implementer path.
6. Spawn exactly one implementer, wait for it, close it, then spawn exactly one spec reviewer, wait for it, close it, then spawn exactly one code-quality reviewer, wait for it, and close it.
"""
        body = json.dumps(
            {
                "model": "minimax-m3",
                "input": [
                    {"type": "message", "role": "user", "content": workflow_prompt},
                    {
                        "type": "function_call",
                        "call_id": "call_impl",
                        "name": "multi_agent_v1__spawn_agent",
                        "arguments": json.dumps(
                            {
                                "message": "You are the IMPLEMENTER subagent in a diagnostic chain.",
                                "nickname": "implementer",
                            }
                        ),
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_impl",
                        "output": json.dumps({"agent_id": "impl-1", "nickname": "implementer"}),
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_impl_wait",
                        "name": "multi_agent_v1__wait_agent",
                        "arguments": json.dumps({"targets": ["impl-1"], "timeout_ms": 60000}),
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_impl_wait",
                        "output": json.dumps(
                            {
                                "timed_out": False,
                                "status": {
                                    "impl-1": {
                                        "completed": "The file path didn't resolve. Let me check the actual path more carefully."
                                    }
                                },
                            }
                        ),
                    },
                ],
                "tools": [
                    {"type": "function", "name": "multi_agent_v1__spawn_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__wait_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__close_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__resume_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__send_input", "parameters": {"type": "object"}},
                ],
            }
        ).encode("utf-8")
        event_context = {"request_id": "req"}

        transformed = compatible_request_body(
            body,
            {
                "name": "ollama_cloud",
                "upstream_format": "chat_completions",
                "tool_protocol": "chat_tools",
            },
            event_context=event_context,
        )
        payload = json.loads(transformed)
        tools_by_name = {tool["name"]: tool for tool in payload["tools"] if tool.get("type") == "function"}
        transcript = json.dumps(payload, ensure_ascii=False)

        self.assertNotIn("multi_agent_v1__spawn_agent", tools_by_name)
        self.assertIn("multi_agent_v1__close_agent", tools_by_name)
        self.assertNotIn("multi_agent_v1__send_input", tools_by_name)
        self.assertFalse(event_context["subagent_spawn_allowed"])
        self.assertEqual(event_context["subagent_close_agent_ids"], ["impl-1"])
        self.assertIn("status: wait_completed_close_required", transcript)
        self.assertEqual(
            payload["tool_choice"],
            {"type": "function", "name": "multi_agent_v1__close_agent"},
        )

    def test_chat_tools_workflow_closed_incomplete_implementer_exposes_implementer_retry(self):
        workflow_prompt = """
Use the real subagent-driven-development skill.

Execution constraints:
1. Use real Codex native subagents for implementer, spec reviewer, and code-quality reviewer.
2. The implementer creates exactly one diagnostic artifact.
3. The spec reviewer verifies exact file content.
4. The code-quality reviewer verifies minimal implementation.
5. If a reviewer finds issues, route fixes back to the existing implementer path.
6. Spawn exactly one implementer, wait for it, close it, then spawn exactly one spec reviewer, wait for it, close it, then spawn exactly one code-quality reviewer, wait for it, and close it.
"""
        body = json.dumps(
            {
                "model": "minimax-m3",
                "input": [
                    {"type": "message", "role": "user", "content": workflow_prompt},
                    {
                        "type": "function_call",
                        "call_id": "call_impl",
                        "name": "multi_agent_v1__spawn_agent",
                        "arguments": json.dumps(
                            {
                                "message": "You are the IMPLEMENTER subagent in a diagnostic chain.",
                                "nickname": "implementer",
                            }
                        ),
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_impl",
                        "output": json.dumps({"agent_id": "impl-1", "nickname": "implementer"}),
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_impl_wait",
                        "name": "multi_agent_v1__wait_agent",
                        "arguments": json.dumps({"targets": ["impl-1"], "timeout_ms": 60000}),
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_impl_wait",
                        "output": json.dumps(
                            {
                                "timed_out": False,
                                "status": {"impl-1": {"completed": "STATUS: BLOCKED\nCould not find OUTPUT_PATH."}},
                            }
                        ),
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_impl_close",
                        "name": "multi_agent_v1__close_agent",
                        "arguments": json.dumps({"target": "impl-1"}),
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_impl_close",
                        "output": json.dumps({"previous_status": {"completed": "STATUS: BLOCKED"}}),
                    },
                ],
                "tools": [
                    {"type": "function", "name": "multi_agent_v1__spawn_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__wait_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__close_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__resume_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__send_input", "parameters": {"type": "object"}},
                ],
            }
        ).encode("utf-8")
        event_context = {"request_id": "req"}

        transformed = compatible_request_body(
            body,
            {
                "name": "ollama_cloud",
                "upstream_format": "chat_completions",
                "tool_protocol": "chat_tools",
            },
            event_context=event_context,
        )
        payload = json.loads(transformed)
        tools_by_name = {tool["name"]: tool for tool in payload["tools"] if tool.get("type") == "function"}
        transcript = json.dumps(payload, ensure_ascii=False)

        self.assertIn("multi_agent_v1__spawn_agent", tools_by_name)
        self.assertNotIn("multi_agent_v1__wait_agent", tools_by_name)
        self.assertNotIn("multi_agent_v1__close_agent", tools_by_name)
        self.assertTrue(event_context["subagent_spawn_allowed"])
        self.assertIn("next_expected_role: implementer", transcript)
        self.assertNotIn("next_expected_role: spec_reviewer", transcript)

    def test_chat_tools_workflow_waited_implementer_exposes_close_not_spawn(self):
        workflow_prompt = """
Use the real subagent-driven-development skill.

Execution constraints:
1. Use real Codex native subagents for implementer, spec reviewer, and code-quality reviewer.
2. Spawn exactly one implementer, wait for it, close it, then spawn exactly one spec reviewer, wait for it, close it, then spawn exactly one code-quality reviewer, wait for it, and close it.
"""
        body = json.dumps(
            {
                "model": "minimax-m3",
                "input": [
                    {"type": "message", "role": "user", "content": workflow_prompt},
                    {
                        "type": "function_call",
                        "call_id": "call_impl",
                        "name": "multi_agent_v1__spawn_agent",
                        "arguments": json.dumps(
                            {
                                "message": "You are the IMPLEMENTER subagent in a diagnostic chain.",
                                "nickname": "implementer",
                            }
                        ),
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_impl",
                        "output": json.dumps({"agent_id": "impl-1", "nickname": "implementer"}),
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_impl_wait",
                        "name": "multi_agent_v1__wait_agent",
                        "arguments": json.dumps({"targets": ["impl-1"], "timeout_ms": 60000}),
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_impl_wait",
                        "output": json.dumps({"timed_out": False, "status": {"impl-1": {"completed": "DONE"}}}),
                    },
                ],
                "tools": [
                    {"type": "function", "name": "multi_agent_v1__spawn_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__wait_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__close_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__resume_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__send_input", "parameters": {"type": "object"}},
                ],
            }
        ).encode("utf-8")
        event_context = {"request_id": "req"}

        transformed = compatible_request_body(
            body,
            {
                "name": "ollama_cloud",
                "upstream_format": "chat_completions",
                "tool_protocol": "chat_tools",
            },
            event_context=event_context,
        )
        payload = json.loads(transformed)
        tools_by_name = {tool["name"]: tool for tool in payload["tools"] if tool.get("type") == "function"}
        transcript = json.dumps(payload, ensure_ascii=False)

        self.assertNotIn("multi_agent_v1__spawn_agent", tools_by_name)
        self.assertIn("multi_agent_v1__close_agent", tools_by_name)
        self.assertEqual(event_context["subagent_close_agent_ids"], ["impl-1"])
        self.assertFalse(event_context["subagent_spawn_allowed"])
        self.assertIn("status: wait_completed_close_required", transcript)

    def test_chat_tools_single_task_workflow_waited_quality_reviewer_exposes_close(self):
        workflow_prompt = """
Use the real subagent-driven-development skill.

## Task 1: Write The Diagnostic Artifact

Execution constraints:
1. Use real Codex native subagents for implementer, spec reviewer, and code-quality reviewer.
2. Spawn exactly one implementer, wait for it, close it, then spawn exactly one spec reviewer, wait for it, close it, then spawn exactly one code-quality reviewer, wait for it, and close it.
3. Final coordinator response must be exactly three lines.
"""
        body = json.dumps(
            {
                "model": "minimax-m3",
                "input": [
                    {"type": "message", "role": "user", "content": workflow_prompt},
                    {
                        "type": "function_call",
                        "call_id": "call_impl",
                        "name": "multi_agent_v1__spawn_agent",
                        "arguments": json.dumps({"message": "You are the IMPLEMENTER subagent.", "nickname": "implementer"}),
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_impl",
                        "output": json.dumps({"agent_id": "impl-1", "nickname": "implementer"}),
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_impl_wait",
                        "name": "multi_agent_v1__wait_agent",
                        "arguments": json.dumps({"targets": ["impl-1"], "timeout_ms": 60000}),
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_impl_wait",
                        "output": json.dumps({"timed_out": False, "status": {"impl-1": {"completed": "DONE"}}}),
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_impl_close",
                        "name": "multi_agent_v1__close_agent",
                        "arguments": json.dumps({"target": "impl-1"}),
                    },
                    {"type": "function_call_output", "call_id": "call_impl_close", "output": json.dumps({"previous_status": {}})},
                    {
                        "type": "function_call",
                        "call_id": "call_spec",
                        "name": "multi_agent_v1__spawn_agent",
                        "arguments": json.dumps({"message": "You are the SPEC REVIEWER subagent.", "nickname": "spec-reviewer"}),
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_spec",
                        "output": json.dumps({"agent_id": "spec-1", "nickname": "spec-reviewer"}),
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_spec_wait",
                        "name": "multi_agent_v1__wait_agent",
                        "arguments": json.dumps({"targets": ["spec-1"], "timeout_ms": 60000}),
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_spec_wait",
                        "output": json.dumps({"timed_out": False, "status": {"spec-1": {"completed": "PASS"}}}),
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_spec_close",
                        "name": "multi_agent_v1__close_agent",
                        "arguments": json.dumps({"target": "spec-1"}),
                    },
                    {"type": "function_call_output", "call_id": "call_spec_close", "output": json.dumps({"previous_status": {}})},
                    {
                        "type": "function_call",
                        "call_id": "call_quality",
                        "name": "multi_agent_v1__spawn_agent",
                        "arguments": json.dumps({"message": "You are the CODE QUALITY REVIEWER subagent.", "nickname": "quality-reviewer"}),
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_quality",
                        "output": json.dumps({"agent_id": "quality-1", "nickname": "quality-reviewer"}),
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_quality_wait",
                        "name": "multi_agent_v1__wait_agent",
                        "arguments": json.dumps({"targets": ["quality-1"], "timeout_ms": 60000}),
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_quality_wait",
                        "output": json.dumps({"timed_out": False, "status": {"quality-1": {"completed": "PASS"}}}),
                    },
                ],
                "tools": [
                    {"type": "function", "name": "multi_agent_v1__spawn_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__wait_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__close_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__resume_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__send_input", "parameters": {"type": "object"}},
                ],
            }
        ).encode("utf-8")
        event_context = {"request_id": "req"}

        transformed = compatible_request_body(
            body,
            {
                "name": "ollama_cloud",
                "upstream_format": "chat_completions",
                "tool_protocol": "chat_tools",
            },
            event_context=event_context,
        )
        payload = json.loads(transformed)
        tools_by_name = {tool["name"]: tool for tool in payload["tools"] if tool.get("type") == "function"}
        transcript = json.dumps(payload, ensure_ascii=False)

        self.assertNotIn("multi_agent_v1__spawn_agent", tools_by_name)
        self.assertNotIn("multi_agent_v1__wait_agent", tools_by_name)
        self.assertIn("multi_agent_v1__close_agent", tools_by_name)
        self.assertFalse(event_context["subagent_lifecycle_complete"])
        self.assertEqual(event_context["subagent_close_agent_ids"], ["quality-1"])
        self.assertIn("status: wait_completed_close_required", transcript)

    def test_chat_tools_single_task_workflow_finalizes_after_all_reviewers_closed(self):
        workflow_prompt = """
Use the real subagent-driven-development skill.

## Task 1: Write The Diagnostic Artifact

Execution constraints:
1. Use real Codex native subagents for implementer, spec reviewer, and code-quality reviewer.
2. Spawn exactly one implementer, wait for it, close it, then spawn exactly one spec reviewer, wait for it, close it, then spawn exactly one code-quality reviewer, wait for it, and close it.
3. Final coordinator response must be exactly three lines.
"""
        body = json.dumps(
            {
                "model": "minimax-m3",
                "input": [
                    {"type": "message", "role": "user", "content": workflow_prompt},
                    {
                        "type": "function_call",
                        "call_id": "call_impl",
                        "name": "multi_agent_v1__spawn_agent",
                        "arguments": json.dumps({"message": "You are the IMPLEMENTER subagent.", "nickname": "implementer"}),
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_impl",
                        "output": json.dumps({"agent_id": "impl-1", "nickname": "implementer"}),
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_impl_wait",
                        "name": "multi_agent_v1__wait_agent",
                        "arguments": json.dumps({"targets": ["impl-1"], "timeout_ms": 60000}),
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_impl_wait",
                        "output": json.dumps({"timed_out": False, "status": {"impl-1": {"completed": "DONE"}}}),
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_impl_close",
                        "name": "multi_agent_v1__close_agent",
                        "arguments": json.dumps({"target": "impl-1"}),
                    },
                    {"type": "function_call_output", "call_id": "call_impl_close", "output": json.dumps({"previous_status": {}})},
                    {
                        "type": "function_call",
                        "call_id": "call_spec",
                        "name": "multi_agent_v1__spawn_agent",
                        "arguments": json.dumps({"message": "You are the SPEC REVIEWER subagent.", "nickname": "spec-reviewer"}),
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_spec",
                        "output": json.dumps({"agent_id": "spec-1", "nickname": "spec-reviewer"}),
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_spec_wait",
                        "name": "multi_agent_v1__wait_agent",
                        "arguments": json.dumps({"targets": ["spec-1"], "timeout_ms": 60000}),
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_spec_wait",
                        "output": json.dumps({"timed_out": False, "status": {"spec-1": {"completed": "PASS"}}}),
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_spec_close",
                        "name": "multi_agent_v1__close_agent",
                        "arguments": json.dumps({"target": "spec-1"}),
                    },
                    {"type": "function_call_output", "call_id": "call_spec_close", "output": json.dumps({"previous_status": {}})},
                    {
                        "type": "function_call",
                        "call_id": "call_quality",
                        "name": "multi_agent_v1__spawn_agent",
                        "arguments": json.dumps({"message": "You are the CODE QUALITY REVIEWER subagent.", "nickname": "quality-reviewer"}),
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_quality",
                        "output": json.dumps({"agent_id": "quality-1", "nickname": "quality-reviewer"}),
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_quality_wait",
                        "name": "multi_agent_v1__wait_agent",
                        "arguments": json.dumps({"targets": ["quality-1"], "timeout_ms": 60000}),
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_quality_wait",
                        "output": json.dumps({"timed_out": False, "status": {"quality-1": {"completed": "PASS"}}}),
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_quality_close",
                        "name": "multi_agent_v1__close_agent",
                        "arguments": json.dumps({"target": "quality-1"}),
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_quality_close",
                        "output": json.dumps({"previous_status": {}}),
                    },
                ],
                "tools": [
                    {"type": "function", "name": "multi_agent_v1__spawn_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__wait_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__close_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__resume_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__send_input", "parameters": {"type": "object"}},
                ],
            }
        ).encode("utf-8")
        event_context = {"request_id": "req"}

        transformed = compatible_request_body(
            body,
            {
                "name": "ollama_cloud",
                "upstream_format": "chat_completions",
                "tool_protocol": "chat_tools",
            },
            event_context=event_context,
        )
        payload = json.loads(transformed)
        tools_by_name = {tool["name"]: tool for tool in payload["tools"] if tool.get("type") == "function"}
        transcript = json.dumps(payload, ensure_ascii=False)

        self.assertNotIn("multi_agent_v1__spawn_agent", tools_by_name)
        self.assertNotIn("multi_agent_v1__wait_agent", tools_by_name)
        self.assertNotIn("multi_agent_v1__close_agent", tools_by_name)
        self.assertTrue(event_context["subagent_lifecycle_complete"])
        self.assertFalse(event_context["subagent_spawn_allowed"])
        self.assertIn("status: lifecycle_complete", transcript)

    def test_responses_structured_injects_close_required_guidance_after_wait(self):
        body = json.dumps(
            {
                "model": "glm-5.2",
                "input": [
                    {"type": "message", "role": "user", "content": "Spawn exactly one child agent, wait, close, final."},
                    {
                        "type": "function_call",
                        "call_id": "call_spawn",
                        "name": "multi_agent_v1__spawn_agent",
                        "arguments": json.dumps({"message": "return child-ok"}),
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_spawn",
                        "output": json.dumps({"agent_id": "019f-child", "nickname": "child"}),
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_wait",
                        "name": "multi_agent_v1__wait_agent",
                        "arguments": json.dumps({"targets": ["019f-child"], "timeout_ms": 60000}),
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_wait",
                        "output": json.dumps({"timed_out": False, "status": {"019f-child": {"completed": "child-ok"}}}),
                    },
                ],
                "tools": [
                    {"type": "function", "name": "mcp__codex_apps__github___fetch", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__spawn_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__wait_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__close_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "mcp__node_repl__js", "parameters": {"type": "object"}},
                ],
            }
        ).encode("utf-8")
        event_context = {"request_id": "req"}

        transformed = compatible_request_body(
            body,
            {
                "name": "ollama_cloud",
                "upstream_format": "responses",
                "tool_protocol": "responses_structured",
            },
            event_context=event_context,
        )
        payload = json.loads(transformed)
        tools_by_name = {tool["name"]: tool for tool in payload["tools"] if tool.get("type") == "function"}
        transcript = json.dumps(payload, ensure_ascii=False)

        self.assertNotIn("multi_agent_v1__spawn_agent", tools_by_name)
        self.assertNotIn("multi_agent_v1__wait_agent", tools_by_name)
        self.assertIn("multi_agent_v1__close_agent", tools_by_name)
        self.assertEqual(set(tools_by_name), {"multi_agent_v1__close_agent"})
        self.assertIn("status: wait_completed_close_required", transcript)
        self.assertIn("required_next_action: call multi_agent_v1__close_agent", transcript)
        self.assertEqual(event_context["subagent_close_agent_ids"], ["019f-child"])

    def test_chat_tools_uses_event_led_state_with_guidance(self):
        body = json.dumps(
            {
                "model": "glm-5.2",
                "input": [
                    {"type": "message", "role": "user", "content": "Use one subagent."},
                    {
                        "type": "function_call",
                        "call_id": "call_spawn",
                        "namespace": "multi_agent_v1",
                        "name": "spawn_agent",
                        "arguments": {"message": "return child-ok"},
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_spawn",
                        "output": json.dumps({"agent_id": "019f-child", "nickname": "child"}),
                    },
                ],
                "tools": [
                    {"type": "function", "name": "multi_agent_v1__spawn_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__wait_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__close_agent", "parameters": {"type": "object"}},
                ],
            }
        ).encode("utf-8")
        event_context = {"request_id": "req"}

        transformed = compatible_request_body(
            body,
            {
                "name": "ollama_cloud",
                "upstream_format": "chat_completions",
                "tool_protocol": "chat_tools",
            },
            event_context=event_context,
        )
        payload = json.loads(transformed)
        tools_by_name = {tool["name"]: tool for tool in payload["tools"] if tool.get("type") == "function"}
        transcript = json.dumps(payload, ensure_ascii=False)

        self.assertNotIn("multi_agent_v1__spawn_agent", tools_by_name)
        self.assertIn("multi_agent_v1__wait_agent", tools_by_name)
        self.assertIn("Codex native multi_agent_v1 current state", transcript)
        self.assertIn("status: spawned_child_wait_required", transcript)
        self.assertEqual(event_context["subagent_open_agent_ids"], ["019f-child"])
        self.assertFalse(event_context["subagent_spawn_allowed"])

    def test_chat_tools_injects_close_required_guidance_after_partial_bounded_close(self):
        body = json.dumps(
            {
                "model": "minimax-m3",
                "input": [
                    {"type": "message", "role": "user", "content": "Spawn two subagents, then wait for both and close both."},
                    {
                        "type": "function_call",
                        "call_id": "call_spawn_a",
                        "name": "multi_agent_v1__spawn_agent",
                        "arguments": json.dumps({"message": "return A", "nickname": "child-a"}),
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_spawn_a",
                        "output": json.dumps({"agent_id": "019f-child-a", "nickname": "child-a"}),
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_spawn_b",
                        "name": "multi_agent_v1__spawn_agent",
                        "arguments": json.dumps({"message": "return B", "nickname": "child-b"}),
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_spawn_b",
                        "output": json.dumps({"agent_id": "019f-child-b", "nickname": "child-b"}),
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_wait",
                        "name": "multi_agent_v1__wait_agent",
                        "arguments": json.dumps({"targets": ["019f-child-a", "019f-child-b"], "timeout_ms": 60000}),
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_wait",
                        "output": json.dumps(
                            {
                                "timed_out": False,
                                "status": {
                                    "019f-child-a": {"completed": "SENTINEL:A"},
                                    "019f-child-b": {"completed": "SENTINEL:B"},
                                },
                            }
                        ),
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_close_a",
                        "name": "multi_agent_v1__close_agent",
                        "arguments": json.dumps({"target": "019f-child-a"}),
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_close_a",
                        "output": json.dumps({"previous_status": {"completed": "SENTINEL:A"}}),
                    },
                ],
                "tools": [
                    {"type": "function", "name": "multi_agent_v1__spawn_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__wait_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__close_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__resume_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__send_input", "parameters": {"type": "object"}},
                ],
            }
        ).encode("utf-8")
        event_context = {"request_id": "req"}

        transformed = compatible_request_body(
            body,
            {
                "name": "ollama_cloud",
                "upstream_format": "chat_completions",
                "tool_protocol": "chat_tools",
            },
            event_context=event_context,
        )
        payload = json.loads(transformed)
        tools_by_name = {tool["name"]: tool for tool in payload["tools"] if tool.get("type") == "function"}
        transcript = json.dumps(payload, ensure_ascii=False)

        self.assertNotIn("multi_agent_v1__spawn_agent", tools_by_name)
        self.assertNotIn("multi_agent_v1__wait_agent", tools_by_name)
        self.assertIn("multi_agent_v1__close_agent", tools_by_name)
        self.assertEqual(
            tools_by_name["multi_agent_v1__close_agent"]["parameters"]["properties"]["target"]["enum"],
            ["019f-child-b"],
        )
        self.assertIn("status: wait_completed_close_required", transcript)
        self.assertIn("open_agent_ids_requiring_close: 019f-child-b", transcript)
        self.assertIn("Do not write the final report until every listed agent_id has been closed", transcript)
        self.assertEqual(event_context["subagent_close_agent_ids"], ["019f-child-b"])
        self.assertFalse(event_context["subagent_lifecycle_complete"])

    def test_chat_tools_injects_finalization_guidance_after_lifecycle_complete(self):
        body = json.dumps(
            {
                "model": "glm-5.2",
                "input": [
                    {"type": "message", "role": "user", "content": "Spawn exactly one child agent, wait, close, final."},
                    {
                        "type": "function_call",
                        "call_id": "call_spawn",
                        "name": "multi_agent_v1__spawn_agent",
                        "arguments": json.dumps({"message": "return child-ok"}),
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_spawn",
                        "output": json.dumps({"agent_id": "019f-child", "nickname": "child"}),
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_wait",
                        "name": "multi_agent_v1__wait_agent",
                        "arguments": json.dumps({"targets": ["019f-child"], "timeout_ms": 60000}),
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_wait",
                        "output": json.dumps({"timed_out": False, "status": {"019f-child": {"completed": "child-ok"}}}),
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_close",
                        "name": "multi_agent_v1__close_agent",
                        "arguments": json.dumps({"target": "019f-child"}),
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_close",
                        "output": json.dumps({"previous_status": {"completed": "child-ok"}}),
                    },
                ],
                "tools": [
                    {"type": "function", "name": "multi_agent_v1__spawn_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__wait_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__close_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__resume_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__send_input", "parameters": {"type": "object"}},
                ],
            }
        ).encode("utf-8")
        event_context = {"request_id": "req"}

        transformed = compatible_request_body(
            body,
            {
                "name": "ollama_cloud",
                "upstream_format": "chat_completions",
                "tool_protocol": "chat_tools",
            },
            event_context=event_context,
        )
        payload = json.loads(transformed)
        tools_by_name = {tool["name"]: tool for tool in payload["tools"] if tool.get("type") == "function"}
        transcript = json.dumps(payload, ensure_ascii=False)

        self.assertEqual(payload["tools"], [])
        for tool_name in codex_proxy.MULTI_AGENT_TOOL_NAMES:
            self.assertNotIn(f"multi_agent_v1__{tool_name}", tools_by_name)
        self.assertIn("status: lifecycle_complete", transcript)
        self.assertIn("write the final concise report now", transcript)
        self.assertIn("visible_response_required", transcript)
        self.assertIn("empty_final_forbidden", transcript)
        self.assertIn("ordinary assistant message content", transcript)
        self.assertIn("first visible output token", transcript)
        self.assertTrue(event_context["subagent_lifecycle_complete"])

    def test_external_request_flattens_mcp_node_repl_namespace_without_tool_search(self):
        body = json.dumps(
            {
                "model": "glm-5.2",
                "input": "run node repl sentinel",
                "tools": [
                    {
                        "type": "namespace",
                        "name": "mcp__node_repl",
                        "tools": [
                            {
                                "type": "function",
                                "name": "js",
                                "description": "Run JavaScript.",
                                "parameters": {
                                    "type": "object",
                                    "properties": {"code": {"type": "string"}},
                                    "required": ["code"],
                                    "additionalProperties": False,
                                },
                            },
                            {
                                "type": "function",
                                "name": "js_reset",
                                "parameters": {"type": "object", "additionalProperties": False},
                            },
                        ],
                    }
                ],
            }
        ).encode("utf-8")

        transformed = compatible_request_body(body, {"name": "ollama_cloud"}, event_context={"request_id": "req"})
        payload = json.loads(transformed)
        tools_by_name = {tool["name"]: tool for tool in payload["tools"] if tool.get("type") == "function"}

        self.assertFalse(any(tool.get("type") == "namespace" and tool.get("name") == "mcp__node_repl" for tool in payload["tools"]))
        self.assertNotIn("tool_search", tools_by_name)
        self.assertIn("mcp__node_repl__js", tools_by_name)
        self.assertIn("mcp__node_repl__js_reset", tools_by_name)
        self.assertIn("multi_agent_v1__spawn_agent", tools_by_name)

    def test_external_request_adds_node_repl_single_step_completion_guidance(self):
        body = json.dumps(
            {
                "model": "glm-5.2",
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": "Call mcp__node_repl__js exactly once, then stop tool use.",
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_node",
                        "namespace": "mcp__node_repl",
                        "name": "js",
                        "arguments": json.dumps({"code": "nodeRepl.write(\"ok\")"}),
                    },
                    {"type": "function_call_output", "call_id": "call_node", "output": "ok"},
                ],
            }
        ).encode("utf-8")

        transformed = compatible_request_body(body, {"name": "ollama_cloud"}, event_context={"request_id": "req"})
        transcript = json.dumps(json.loads(transformed), ensure_ascii=True)

        self.assertIn("Codex native mcp__node_repl.js result", transcript)
        self.assertIn("required_next_action: write the final answer now", transcript)
        self.assertIn("completed_tool_alias: mcp__node_repl__js", transcript)
        self.assertIn("do not call mcp__node_repl__js or tool_search again", transcript)

    def test_external_request_hides_node_repl_tools_after_single_step_result(self):
        body = json.dumps(
            {
                "model": "glm-5.2",
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": "Call mcp__node_repl__js exactly once, then stop tool use.",
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_node",
                        "namespace": "mcp__node_repl",
                        "name": "js",
                        "arguments": json.dumps({"code": "nodeRepl.write(\"ok\")"}),
                    },
                    {"type": "function_call_output", "call_id": "call_node", "output": "ok"},
                ],
                "tools": [
                    {
                        "type": "namespace",
                        "name": "mcp__node_repl",
                        "tools": [
                            {"type": "function", "name": "js", "parameters": {"type": "object"}},
                            {"type": "function", "name": "js_reset", "parameters": {"type": "object"}},
                        ],
                    },
                    {"type": "function", "name": "mcp__node_repl__js", "parameters": {"type": "object"}},
                    {"type": "function", "name": "mcp__node_repl__js_reset", "parameters": {"type": "object"}},
                ],
            }
        ).encode("utf-8")

        transformed = compatible_request_body(body, {"name": "ollama_cloud"}, event_context={"request_id": "req"})
        payload = json.loads(transformed)
        tools_by_name = {tool["name"]: tool for tool in payload["tools"] if tool.get("type") == "function"}
        transcript = json.dumps(payload, ensure_ascii=True)

        self.assertFalse(any(tool.get("type") == "namespace" and tool.get("name") == "mcp__node_repl" for tool in payload["tools"]))
        self.assertNotIn("mcp__node_repl__js", tools_by_name)
        self.assertNotIn("mcp__node_repl__js_reset", tools_by_name)
        self.assertIn("multi_agent_v1__spawn_agent", tools_by_name)
        self.assertIn("status: single_step_complete", transcript)
        self.assertIn("required_next_action: write the final answer now", transcript)

    def test_external_browser_comments_injects_browser_guidance_and_node_repl_alias(self):
        body = json.dumps(
            {
                "model": "glm-5.2",
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "# Browser comments\nbutton is misaligned"}],
                    }
                ],
                "tools": [
                    {
                        "type": "namespace",
                        "name": "mcp__node_repl",
                        "tools": [
                            {
                                "type": "function",
                                "name": "js",
                                "description": "Run JavaScript.",
                                "parameters": {"type": "object", "additionalProperties": True},
                            }
                        ],
                    }
                ],
            }
        ).encode("utf-8")

        transformed = compatible_request_body(body, {"name": "ollama_cloud"}, event_context={"request_id": "req"})
        payload = json.loads(transformed)
        transcript = json.dumps(payload, ensure_ascii=True)
        tools_by_name = {tool["name"]: tool for tool in payload["tools"] if tool.get("type") == "function"}

        self.assertNotIn("tool_search", tools_by_name)
        self.assertIn("mcp__node_repl__js", tools_by_name)
        self.assertIn("mcp__node_repl__js", transcript)
        self.assertIn("browser:control-in-app-browser", transcript)

    def test_external_browser_context_keeps_node_repl_tools_after_result(self):
        body = json.dumps(
            {
                "model": "glm-5.2",
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": "# In app browser\nCurrent URL: https://example.test/page\nRead the current page title.",
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_node",
                        "namespace": "mcp__node_repl",
                        "name": "js",
                        "arguments": json.dumps({"code": "nodeRepl.write(\"Example\")"}),
                    },
                    {"type": "function_call_output", "call_id": "call_node", "output": "Example"},
                ],
                "tools": [
                    {
                        "type": "namespace",
                        "name": "mcp__node_repl",
                        "tools": [
                            {"type": "function", "name": "js", "parameters": {"type": "object"}},
                            {"type": "function", "name": "js_reset", "parameters": {"type": "object"}},
                        ],
                    }
                ],
            }
        ).encode("utf-8")

        transformed = compatible_request_body(body, {"name": "ollama_cloud"}, event_context={"request_id": "req"})
        payload = json.loads(transformed)
        tools_by_name = {tool["name"]: tool for tool in payload["tools"] if tool.get("type") == "function"}
        transcript = json.dumps(payload, ensure_ascii=True)

        self.assertIn("mcp__node_repl__js", tools_by_name)
        self.assertIn("mcp__node_repl__js_reset", tools_by_name)
        self.assertIn("browser:control-in-app-browser", transcript)
        self.assertNotIn("status: single_step_complete", transcript)

    def test_external_request_hides_tool_search_after_multi_agent_discovery(self):
        body = json.dumps(
            {
                "model": "glm-5.2",
                "input": [
                    {"type": "tool_search_call", "call_id": "call_search", "arguments": {"query": "spawn_agent multi_agent subagent"}},
                    {"type": "tool_search_output", "call_id": "call_search", "tools": codex_proxy.MULTI_AGENT_DISCOVERY_TOOLS},
                ],
                "tools": [{"type": "function", "name": "tool_search", "parameters": {"type": "object"}}],
            }
        ).encode("utf-8")

        transformed = compatible_request_body(body, {"name": "ollama_cloud"}, event_context={"request_id": "req"})
        tools_by_name = {tool["name"]: tool for tool in json.loads(transformed)["tools"]}

        self.assertNotIn("tool_search", tools_by_name)
        self.assertIn("multi_agent_v1__spawn_agent", tools_by_name)
        self.assertIn("multi_agent_v1__wait_agent", tools_by_name)
        self.assertIn("multi_agent_v1__close_agent", tools_by_name)

    def test_external_request_hides_spawn_agent_while_child_is_open(self):
        body = json.dumps(
            {
                "model": "glm-5.2",
                "input": [
                    {
                        "type": "function_call",
                        "call_id": "call_spawn",
                        "namespace": "multi_agent_v1",
                        "name": "spawn_agent",
                        "arguments": {"message": "return child-ok"},
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_spawn",
                        "output": json.dumps({"agent_id": "019f-child", "nickname": "child"}),
                    },
                ],
                "tools": [{"type": "function", "name": "multi_agent_v1__spawn_agent", "parameters": {"type": "object"}}],
            }
        ).encode("utf-8")

        transformed = compatible_request_body(body, {"name": "ollama_cloud"}, event_context={"request_id": "req"})
        payload = json.loads(transformed)
        tools_by_name = {tool["name"]: tool for tool in payload["tools"]}
        transcript = json.dumps(payload, ensure_ascii=True)

        self.assertNotIn("multi_agent_v1__spawn_agent", tools_by_name)
        self.assertIn("multi_agent_v1__wait_agent", tools_by_name)
        self.assertNotIn("multi_agent_v1__close_agent", tools_by_name)
        self.assertIn("Codex native multi_agent_v1.spawn_agent result", transcript)
        self.assertIn("agent_id: 019f-child", transcript)
        self.assertIn("status: spawned_child_wait_required", transcript)
        self.assertFalse(any(tool.get("type") == "namespace" and tool.get("name") == "multi_agent_v1" for tool in payload["tools"]))
        wait_items = tools_by_name["multi_agent_v1__wait_agent"]["parameters"]["properties"]["targets"]["items"]
        self.assertEqual(wait_items["enum"], ["019f-child"])

    def test_external_request_hides_wait_agent_after_wait_completed(self):
        body = json.dumps(
            {
                "model": "glm-5.2",
                "input": [
                    {
                        "type": "function_call",
                        "call_id": "call_spawn",
                        "namespace": "multi_agent_v1",
                        "name": "spawn_agent",
                        "arguments": {"message": "return child-ok"},
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_spawn",
                        "output": json.dumps({"agent_id": "019f-child", "nickname": "child"}),
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_wait",
                        "namespace": "multi_agent_v1",
                        "name": "wait_agent",
                        "arguments": {"targets": ["019f-child"], "timeout_ms": 60000},
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_wait",
                        "output": json.dumps({"timed_out": False, "status": {"019f-child": {"completed": "child-ok"}}}),
                    },
                ],
            }
        ).encode("utf-8")

        transformed = compatible_request_body(body, {"name": "ollama_cloud"}, event_context={"request_id": "req"})
        payload = json.loads(transformed)
        tools_by_name = {tool["name"]: tool for tool in payload["tools"]}
        transcript = json.dumps(payload, ensure_ascii=True)

        self.assertNotIn("multi_agent_v1__spawn_agent", tools_by_name)
        self.assertNotIn("multi_agent_v1__wait_agent", tools_by_name)
        self.assertIn("multi_agent_v1__close_agent", tools_by_name)
        self.assertIn("status: wait_completed_close_required", transcript)
        close_target = tools_by_name["multi_agent_v1__close_agent"]["parameters"]["properties"]["target"]
        self.assertEqual(close_target["enum"], ["019f-child"])

    def test_external_request_allows_spawn_agent_after_close_result(self):
        body = json.dumps(
            {
                "model": "glm-5.2",
                "input": [
                    {
                        "type": "function_call",
                        "call_id": "call_spawn",
                        "namespace": "multi_agent_v1",
                        "name": "spawn_agent",
                        "arguments": {"message": "return child-ok"},
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_spawn",
                        "output": json.dumps({"agent_id": "019f-child"}),
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_close",
                        "namespace": "multi_agent_v1",
                        "name": "close_agent",
                        "arguments": {"target": "019f-child"},
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_close",
                        "output": json.dumps({"previous_status": {"completed": {}}}),
                    },
                ],
            }
        ).encode("utf-8")

        transformed = compatible_request_body(body, {"name": "ollama_cloud"}, event_context={"request_id": "req"})
        tools_by_name = {tool["name"]: tool for tool in json.loads(transformed)["tools"]}

        self.assertIn("multi_agent_v1__spawn_agent", tools_by_name)

    def test_external_request_keeps_spawn_agent_when_source_text_mentions_closed_lifecycle(self):
        body = json.dumps(
            {
                "model": "glm-5.2",
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": "Plan complete. Subagent-Driven recommended. Ensure exactly one terminal marker.",
                    },
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": (
                            'source snippet: if "Codex native multi_agent_v1.close_agent result" '
                            'in text and "status: closed" in text: pass'
                        ),
                    },
                ],
            }
        ).encode("utf-8")

        transformed = compatible_request_body(body, {"name": "ollama_cloud"}, event_context={"request_id": "req"})
        payload = json.loads(transformed)
        tools_by_name = {tool["name"]: tool for tool in payload["tools"]}
        transcript = json.dumps(payload, ensure_ascii=True)

        self.assertIn("multi_agent_v1__spawn_agent", tools_by_name)
        self.assertNotIn("status: lifecycle_complete", transcript)

    def test_external_request_hides_multi_agent_tools_after_single_loop_close(self):
        body = json.dumps(
            {
                "model": "kimi-k2.6",
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": "只执行一次 spawn -> wait -> close 闭环。close_agent 返回后不要再 spawn 第二个子代理，不要重复验证。",
                            }
                        ],
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_spawn",
                        "namespace": "multi_agent_v1",
                        "name": "spawn_agent",
                        "arguments": {"message": "return child-ok"},
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_spawn",
                        "output": json.dumps({"agent_id": "019f-child"}),
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_wait",
                        "namespace": "multi_agent_v1",
                        "name": "wait_agent",
                        "arguments": {"targets": ["019f-child"], "timeout_ms": 60000},
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_wait",
                        "output": json.dumps({"timed_out": False, "status": {"019f-child": {"completed": "child-ok"}}}),
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_close",
                        "namespace": "multi_agent_v1",
                        "name": "close_agent",
                        "arguments": {"target": "019f-child"},
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_close",
                        "output": json.dumps({"previous_status": {"completed": "child-ok"}}),
                    },
                ],
                "tools": [
                    {"type": "function", "name": "tool_search", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__spawn_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__wait_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__close_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__resume_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__send_input", "parameters": {"type": "object"}},
                ],
            }
        ).encode("utf-8")

        transformed = compatible_request_body(body, {"name": "ollama_cloud"}, event_context={"request_id": "req"})
        payload = json.loads(transformed)
        tools_by_name = {tool["name"]: tool for tool in payload["tools"]}
        transcript = json.dumps(payload, ensure_ascii=False)

        self.assertNotIn("tool_search", tools_by_name)
        for tool_name in codex_proxy.MULTI_AGENT_TOOL_NAMES:
            self.assertNotIn(f"multi_agent_v1__{tool_name}", tools_by_name)
        self.assertIn("status: lifecycle_complete", transcript)
        self.assertIn("required_next_action: write the final concise report now", transcript)

    def test_external_request_hides_multi_agent_tools_after_exactly_one_lifecycle_close(self):
        body = json.dumps(
            {
                "model": "glm-5.2",
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": "Run exactly one subagent lifecycle: spawn_agent, wait_agent, close_agent.",
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_spawn",
                        "namespace": "multi_agent_v1",
                        "name": "spawn_agent",
                        "arguments": {"message": "return child-ok"},
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_spawn",
                        "output": json.dumps({"agent_id": "019f-child"}),
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_wait",
                        "namespace": "multi_agent_v1",
                        "name": "wait_agent",
                        "arguments": {"targets": ["019f-child"], "timeout_ms": 60000},
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_wait",
                        "output": json.dumps({"timed_out": False, "status": {"019f-child": {"completed": "child-ok"}}}),
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_close",
                        "namespace": "multi_agent_v1",
                        "name": "close_agent",
                        "arguments": {"target": "019f-child"},
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_close",
                        "output": json.dumps({"previous_status": {"completed": "child-ok"}}),
                    },
                ],
                "tools": [
                    {
                        "type": "namespace",
                        "name": "multi_agent_v1",
                        "tools": [
                            {"type": "function", "name": "spawn_agent", "parameters": {"type": "object"}},
                            {"type": "function", "name": "wait_agent", "parameters": {"type": "object"}},
                            {"type": "function", "name": "close_agent", "parameters": {"type": "object"}},
                        ],
                    },
                    {"type": "function", "name": "multi_agent_v1__spawn_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__wait_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__close_agent", "parameters": {"type": "object"}},
                ],
            }
        ).encode("utf-8")

        transformed = compatible_request_body(body, {"name": "ollama_cloud"}, event_context={"request_id": "req"})
        payload = json.loads(transformed)
        tools_by_name = {tool["name"]: tool for tool in payload["tools"] if tool.get("type") == "function"}
        transcript = json.dumps(payload, ensure_ascii=True)

        self.assertFalse(any(tool.get("type") == "namespace" and tool.get("name") == "multi_agent_v1" for tool in payload["tools"]))
        for tool_name in codex_proxy.MULTI_AGENT_TOOL_NAMES:
            self.assertNotIn(f"multi_agent_v1__{tool_name}", tools_by_name)
        self.assertIn("status: lifecycle_complete", transcript)
        self.assertIn("completed_tool_aliases: multi_agent_v1__spawn_agent", transcript)

    def test_external_request_treats_real_subagent_collaboration_prompt_as_single_loop(self):
        body = json.dumps(
            {
                "model": "glm-5.2",
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": "请执行一次真实 Codex native subagent 协作测试。先调用可见的 subagent spawn 工具创建一个子代理。最终回复只报告结果。",
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_spawn",
                        "namespace": "multi_agent_v1",
                        "name": "spawn_agent",
                        "arguments": {"message": "return child-ok"},
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_spawn",
                        "output": json.dumps({"agent_id": "019f-child"}),
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_wait",
                        "namespace": "multi_agent_v1",
                        "name": "wait_agent",
                        "arguments": {"targets": ["019f-child"], "timeout_ms": 60000},
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_wait",
                        "output": json.dumps({"timed_out": False, "status": {"019f-child": {"completed": "child-ok"}}}),
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_close",
                        "namespace": "multi_agent_v1",
                        "name": "close_agent",
                        "arguments": {"target": "019f-child"},
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_close",
                        "output": json.dumps({"previous_status": {"completed": "child-ok"}}),
                    },
                ],
                "tools": [
                    {"type": "function", "name": "multi_agent_v1__spawn_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__wait_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__close_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__resume_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__send_input", "parameters": {"type": "object"}},
                ],
            }
        ).encode("utf-8")

        transformed = compatible_request_body(body, {"name": "ollama_cloud"}, event_context={"request_id": "req"})
        payload = json.loads(transformed)
        tools_by_name = {tool["name"]: tool for tool in payload["tools"] if tool.get("type") == "function"}
        transcript = json.dumps(payload, ensure_ascii=False)

        for tool_name in codex_proxy.MULTI_AGENT_TOOL_NAMES:
            self.assertNotIn(f"multi_agent_v1__{tool_name}", tools_by_name)
        self.assertIn("status: lifecycle_complete", transcript)

    def test_external_request_single_loop_hides_send_input_after_spawn_result(self):
        body = json.dumps(
            {
                "model": "glm-5.2",
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": "请执行一次真实 Codex native subagent 协作测试。先调用可见的 subagent spawn 工具创建一个子代理。等待子代理完成，然后关闭该子代理。",
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_spawn",
                        "namespace": "multi_agent_v1",
                        "name": "spawn_agent",
                        "arguments": {"message": "return child-ok"},
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_spawn",
                        "output": json.dumps({"agent_id": "019f-child", "nickname": "child"}),
                    },
                ],
                "tools": [
                    {"type": "function", "name": "multi_agent_v1__spawn_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__wait_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__close_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__resume_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__send_input", "parameters": {"type": "object"}},
                ],
            }
        ).encode("utf-8")

        transformed = compatible_request_body(body, {"name": "ollama_cloud"}, event_context={"request_id": "req"})
        payload = json.loads(transformed)
        tools_by_name = {tool["name"]: tool for tool in payload["tools"] if tool.get("type") == "function"}
        transcript = json.dumps(payload, ensure_ascii=False)

        self.assertNotIn("multi_agent_v1__spawn_agent", tools_by_name)
        self.assertIn("multi_agent_v1__wait_agent", tools_by_name)
        self.assertNotIn("multi_agent_v1__close_agent", tools_by_name)
        self.assertNotIn("multi_agent_v1__resume_agent", tools_by_name)
        self.assertNotIn("multi_agent_v1__send_input", tools_by_name)
        self.assertIn("status: spawned_child_wait_required", transcript)

    def test_external_request_bounded_multi_spawn_allows_next_spawn_before_wait(self):
        body = json.dumps(
            {
                "model": "glm-5.2",
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": "请执行一次真实 Codex native subagent 协作测试。同步 spawn 两个子代理，等待两个子代理完成，关闭两个子代理，最终回复结果。",
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_spawn_a",
                        "namespace": "multi_agent_v1",
                        "name": "spawn_agent",
                        "arguments": {"message": "return child-a"},
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_spawn_a",
                        "output": json.dumps({"agent_id": "019f-child-a", "nickname": "child-a"}),
                    },
                ],
                "tools": [
                    {"type": "function", "name": "multi_agent_v1__spawn_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__wait_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__close_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__resume_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__send_input", "parameters": {"type": "object"}},
                ],
            }
        ).encode("utf-8")

        transformed = compatible_request_body(body, {"name": "ollama_cloud"}, event_context={"request_id": "req"})
        payload = json.loads(transformed)
        tools_by_name = {tool["name"]: tool for tool in payload["tools"] if tool.get("type") == "function"}
        transcript = json.dumps(payload, ensure_ascii=False)

        self.assertIn("multi_agent_v1__spawn_agent", tools_by_name)
        self.assertNotIn("multi_agent_v1__wait_agent", tools_by_name)
        self.assertNotIn("multi_agent_v1__close_agent", tools_by_name)
        self.assertNotIn("multi_agent_v1__resume_agent", tools_by_name)
        self.assertNotIn("multi_agent_v1__send_input", tools_by_name)
        self.assertIn("status: spawn_more_required", transcript)
        self.assertIn("remaining_spawn_count: 1", transcript)

    def test_external_request_bounded_multi_spawn_waits_after_requested_spawns(self):
        body = json.dumps(
            {
                "model": "glm-5.2",
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": "请执行一次真实 Codex native subagent 协作测试。同步 spawn 两个子代理，等待两个子代理完成，关闭两个子代理，最终回复结果。",
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_spawn_a",
                        "namespace": "multi_agent_v1",
                        "name": "spawn_agent",
                        "arguments": {"message": "return child-a"},
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_spawn_a",
                        "output": json.dumps({"agent_id": "019f-child-a"}),
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_spawn_b",
                        "namespace": "multi_agent_v1",
                        "name": "spawn_agent",
                        "arguments": {"message": "return child-b"},
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_spawn_b",
                        "output": json.dumps({"agent_id": "019f-child-b"}),
                    },
                ],
                "tools": [
                    {"type": "function", "name": "multi_agent_v1__spawn_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__wait_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__close_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__resume_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__send_input", "parameters": {"type": "object"}},
                ],
            }
        ).encode("utf-8")

        transformed = compatible_request_body(body, {"name": "ollama_cloud"}, event_context={"request_id": "req"})
        payload = json.loads(transformed)
        tools_by_name = {tool["name"]: tool for tool in payload["tools"] if tool.get("type") == "function"}
        transcript = json.dumps(payload, ensure_ascii=False)

        self.assertNotIn("multi_agent_v1__spawn_agent", tools_by_name)
        self.assertIn("multi_agent_v1__wait_agent", tools_by_name)
        self.assertNotIn("multi_agent_v1__close_agent", tools_by_name)
        self.assertNotIn("multi_agent_v1__resume_agent", tools_by_name)
        self.assertNotIn("multi_agent_v1__send_input", tools_by_name)
        self.assertIn("status: spawned_child_wait_required", transcript)
        self.assertIn("open_agent_ids_requiring_wait: 019f-child-a, 019f-child-b", transcript)

    def test_external_request_bounded_empty_child_output_requires_send_input(self):
        body = json.dumps(
            {
                "model": "glm-5.2",
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": "请同步 spawn 两个子代理，等待两个子代理完成，关闭两个子代理，最终回复结果。",
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_spawn_a",
                        "namespace": "multi_agent_v1",
                        "name": "spawn_agent",
                        "arguments": {"message": "Return exactly this line: SENTINEL:A"},
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_spawn_a",
                        "output": json.dumps({"agent_id": "019f-child-a"}),
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_spawn_b",
                        "namespace": "multi_agent_v1",
                        "name": "spawn_agent",
                        "arguments": {"message": "Return exactly this line: SENTINEL:B"},
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_spawn_b",
                        "output": json.dumps({"agent_id": "019f-child-b"}),
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_wait",
                        "namespace": "multi_agent_v1",
                        "name": "wait_agent",
                        "arguments": {"targets": ["019f-child-a", "019f-child-b"], "timeout_ms": 60000},
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_wait",
                        "output": json.dumps(
                            {
                                "timed_out": False,
                                "status": {
                                    "019f-child-a": {"status": "completed", "message": "SENTINEL:A"},
                                    "019f-child-b": {"status": "completed", "message": None},
                                },
                            }
                        ),
                    },
                ],
                "tools": [
                    {"type": "function", "name": "multi_agent_v1__spawn_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__wait_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__close_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__resume_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__send_input", "parameters": {"type": "object"}},
                ],
            }
        ).encode("utf-8")
        event_context = {"request_id": "req"}

        transformed = compatible_request_body(
            body,
            {"name": "ollama_cloud", "tool_protocol": "responses_structured"},
            event_context=event_context,
        )
        payload = json.loads(transformed)
        tools_by_name = {tool["name"]: tool for tool in payload["tools"] if tool.get("type") == "function"}
        transcript = json.dumps(payload, ensure_ascii=False)

        self.assertNotIn("multi_agent_v1__spawn_agent", tools_by_name)
        self.assertNotIn("multi_agent_v1__wait_agent", tools_by_name)
        self.assertNotIn("multi_agent_v1__close_agent", tools_by_name)
        self.assertIn("multi_agent_v1__send_input", tools_by_name)
        self.assertEqual(payload["tool_choice"], {"type": "function", "name": "multi_agent_v1__send_input"})
        self.assertEqual(event_context["subagent_wait_agent_ids"], ["019f-child-b"])
        self.assertEqual(event_context["subagent_close_agent_ids"], ["019f-child-a"])
        self.assertIn("status: child_empty_output_fix_required", transcript)
        self.assertIn("send_input_target: 019f-child-b", transcript)

        response_body = json.dumps(
            {
                "id": "resp_text",
                "object": "response",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "I should close next."}],
                    }
                ],
            }
        ).encode("utf-8")
        transformed_response = compatible_response_body(response_body, "ollama_cloud", event_context=event_context)
        response_payload = json.loads(transformed_response)
        call_item = response_payload["output"][0]
        call_args = json.loads(call_item["arguments"])

        self.assertEqual(call_item["type"], "function_call")
        self.assertEqual(call_item["namespace"], "multi_agent_v1")
        self.assertEqual(call_item["name"], "send_input")
        self.assertEqual(call_args["target"], "019f-child-b")
        self.assertIn("Return exactly this line: SENTINEL:B", call_args["message"])

    def test_external_request_bounded_multi_spawn_completes_after_all_closed(self):
        body = json.dumps(
            {
                "model": "glm-5.2",
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": "请执行一次真实 Codex native subagent 协作测试。同步 spawn 两个子代理，等待两个子代理完成，关闭两个子代理，最终回复结果。",
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_spawn_a",
                        "namespace": "multi_agent_v1",
                        "name": "spawn_agent",
                        "arguments": {"message": "return child-a"},
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_spawn_a",
                        "output": json.dumps({"agent_id": "019f-child-a"}),
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_spawn_b",
                        "namespace": "multi_agent_v1",
                        "name": "spawn_agent",
                        "arguments": {"message": "return child-b"},
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_spawn_b",
                        "output": json.dumps({"agent_id": "019f-child-b"}),
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_wait",
                        "namespace": "multi_agent_v1",
                        "name": "wait_agent",
                        "arguments": {"targets": ["019f-child-a", "019f-child-b"], "timeout_ms": 60000},
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_wait",
                        "output": json.dumps(
                            {
                                "timed_out": False,
                                "status": {
                                    "019f-child-a": {"completed": "child-a"},
                                    "019f-child-b": {"completed": "child-b"},
                                },
                            }
                        ),
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_close_a",
                        "namespace": "multi_agent_v1",
                        "name": "close_agent",
                        "arguments": {"target": "019f-child-a"},
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_close_a",
                        "output": json.dumps({"previous_status": {"completed": "child-a"}}),
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_close_b",
                        "namespace": "multi_agent_v1",
                        "name": "close_agent",
                        "arguments": {"target": "019f-child-b"},
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_close_b",
                        "output": json.dumps({"previous_status": {"completed": "child-b"}}),
                    },
                ],
                "tools": [
                    {"type": "function", "name": "multi_agent_v1__spawn_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__wait_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__close_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__resume_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__send_input", "parameters": {"type": "object"}},
                ],
            }
        ).encode("utf-8")

        transformed = compatible_request_body(body, {"name": "ollama_cloud"}, event_context={"request_id": "req"})
        payload = json.loads(transformed)
        tools_by_name = {tool["name"]: tool for tool in payload["tools"] if tool.get("type") == "function"}
        transcript = json.dumps(payload, ensure_ascii=False)

        for tool_name in codex_proxy.MULTI_AGENT_TOOL_NAMES:
            self.assertNotIn(f"multi_agent_v1__{tool_name}", tools_by_name)
        self.assertIn("status: lifecycle_complete", transcript)
        self.assertIn("019f-child-a", transcript)
        self.assertIn("019f-child-b", transcript)

    def test_text_compat_allows_reviewer_spawn_after_implementer_done(self):
        body = json.dumps(
            {
                "model": "glm-5.2",
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": "Use subagent-driven development for Task 1.",
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_impl",
                        "namespace": "multi_agent_v1",
                        "name": "spawn_agent",
                        "arguments": {
                            "message": "Implement Task 1 exactly.",
                            "nickname": "implementer-task-1",
                        },
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_impl",
                        "output": json.dumps({"agent_id": "impl-1", "nickname": "implementer-task-1"}),
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_impl_wait",
                        "namespace": "multi_agent_v1",
                        "name": "wait_agent",
                        "arguments": {"targets": ["impl-1"], "timeout_ms": 60000},
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_impl_wait",
                        "output": json.dumps({"timed_out": False, "status": {"impl-1": {"completed": "DONE"}}}),
                    },
                ],
                "tools": [
                    {"type": "function", "name": "multi_agent_v1__spawn_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__wait_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__close_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__resume_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__send_input", "parameters": {"type": "object"}},
                ],
            }
        ).encode("utf-8")

        transformed = compatible_request_body(body, {"name": "ollama_cloud"}, event_context={"request_id": "req"})
        payload = json.loads(transformed)
        tools_by_name = {tool["name"]: tool for tool in payload["tools"] if tool.get("type") == "function"}
        transcript = json.dumps(payload, ensure_ascii=False)

        self.assertIn("multi_agent_v1__spawn_agent", tools_by_name)
        self.assertNotIn("multi_agent_v1__close_agent", tools_by_name)
        self.assertIn("status: next_subagent_spawn_required", transcript)
        self.assertIn("next_expected_role: spec_reviewer", transcript)

    def test_text_compat_routes_reviewer_issue_to_existing_implementer(self):
        body = json.dumps(
            {
                "model": "glm-5.2",
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": "Use subagent-driven development for Task 1.",
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_impl",
                        "namespace": "multi_agent_v1",
                        "name": "spawn_agent",
                        "arguments": {
                            "message": "Implement Task 1 exactly.",
                            "nickname": "implementer-task-1",
                        },
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_impl",
                        "output": json.dumps({"agent_id": "impl-1", "nickname": "implementer-task-1"}),
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_impl_wait",
                        "namespace": "multi_agent_v1",
                        "name": "wait_agent",
                        "arguments": {"targets": ["impl-1"], "timeout_ms": 60000},
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_impl_wait",
                        "output": json.dumps({"timed_out": False, "status": {"impl-1": {"completed": "DONE"}}}),
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_spec",
                        "namespace": "multi_agent_v1",
                        "name": "spawn_agent",
                        "arguments": {
                            "message": "Spec compliance review for Task 1.",
                            "nickname": "spec-reviewer-task-1",
                        },
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_spec",
                        "output": json.dumps({"agent_id": "spec-1", "nickname": "spec-reviewer-task-1"}),
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_spec_wait",
                        "namespace": "multi_agent_v1",
                        "name": "wait_agent",
                        "arguments": {"targets": ["spec-1"], "timeout_ms": 60000},
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_spec_wait",
                        "output": json.dumps(
                            {"timed_out": False, "status": {"spec-1": {"completed": "ISSUE: missing required test"}}}
                        ),
                    },
                ],
                "tools": [
                    {"type": "function", "name": "multi_agent_v1__spawn_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__wait_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__close_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__resume_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__send_input", "parameters": {"type": "object"}},
                ],
            }
        ).encode("utf-8")

        transformed = compatible_request_body(body, {"name": "ollama_cloud"}, event_context={"request_id": "req"})
        payload = json.loads(transformed)
        tools_by_name = {tool["name"]: tool for tool in payload["tools"] if tool.get("type") == "function"}
        transcript = json.dumps(payload, ensure_ascii=False)

        self.assertNotIn("multi_agent_v1__spawn_agent", tools_by_name)
        self.assertNotIn("multi_agent_v1__close_agent", tools_by_name)
        self.assertIn("multi_agent_v1__send_input", tools_by_name)
        self.assertIn("status: reviewer_issue_fix_required", transcript)
        self.assertIn("send_input_target: impl-1", transcript)

    def test_text_compat_routes_reviewer_issue_after_closed_implementer_to_new_spawn(self):
        body = json.dumps(
            {
                "model": "glm-5.2",
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": "Use subagent-driven development for Task 1.",
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_impl",
                        "namespace": "multi_agent_v1",
                        "name": "spawn_agent",
                        "arguments": {
                            "message": "Implement Task 1 exactly.",
                            "nickname": "implementer-task-1",
                        },
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_impl",
                        "output": json.dumps({"agent_id": "impl-1", "nickname": "implementer-task-1"}),
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_impl_wait",
                        "namespace": "multi_agent_v1",
                        "name": "wait_agent",
                        "arguments": {"targets": ["impl-1"], "timeout_ms": 60000},
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_impl_wait",
                        "output": json.dumps({"timed_out": False, "status": {"impl-1": {"completed": "DONE"}}}),
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_impl_close",
                        "namespace": "multi_agent_v1",
                        "name": "close_agent",
                        "arguments": {"target": "impl-1"},
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_impl_close",
                        "output": json.dumps({"previous_status": {"completed": "DONE"}}),
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_spec",
                        "namespace": "multi_agent_v1",
                        "name": "spawn_agent",
                        "arguments": {
                            "message": "Spec compliance review for Task 1.",
                            "nickname": "spec-reviewer-task-1",
                        },
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_spec",
                        "output": json.dumps({"agent_id": "spec-1", "nickname": "spec-reviewer-task-1"}),
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_spec_wait",
                        "namespace": "multi_agent_v1",
                        "name": "wait_agent",
                        "arguments": {"targets": ["spec-1"], "timeout_ms": 60000},
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_spec_wait",
                        "output": json.dumps(
                            {"timed_out": False, "status": {"spec-1": {"completed": "ISSUE: missing required test"}}}
                        ),
                    },
                ],
                "tools": [
                    {"type": "function", "name": "multi_agent_v1__spawn_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__wait_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__close_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__resume_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__send_input", "parameters": {"type": "object"}},
                ],
            }
        ).encode("utf-8")

        transformed = compatible_request_body(body, {"name": "ollama_cloud"}, event_context={"request_id": "req"})
        payload = json.loads(transformed)
        tools_by_name = {tool["name"]: tool for tool in payload["tools"] if tool.get("type") == "function"}
        transcript = json.dumps(payload, ensure_ascii=False)

        self.assertIn("multi_agent_v1__spawn_agent", tools_by_name)
        self.assertNotIn("multi_agent_v1__close_agent", tools_by_name)
        self.assertNotIn("multi_agent_v1__resume_agent", tools_by_name)
        self.assertNotIn("multi_agent_v1__send_input", tools_by_name)
        self.assertIn("status: next_subagent_spawn_required", transcript)
        self.assertIn("next_expected_role: implementer", transcript)
        self.assertIn("next_expected_task: task-1", transcript)

    def test_external_response_normalizes_multi_agent_wait_alias_and_arguments(self):
        body = json.dumps(
            {
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call_wait",
                        "name": "multi_agent_v1__wait_agent",
                        "arguments": json.dumps({"targets": "019f-child", "timeout_ms": "1000"}),
                    }
                ]
            }
        ).encode("utf-8")

        transformed = compatible_response_body(body, "ollama_cloud", event_context={"request_id": "req"})
        call = json.loads(transformed)["output"][0]

        self.assertEqual(call["name"], "wait_agent")
        self.assertEqual(call["namespace"], "multi_agent_v1")
        self.assertEqual(json.loads(call["arguments"])["targets"], ["019f-child"])
        self.assertEqual(json.loads(call["arguments"])["timeout_ms"], 1000)

    def test_external_response_normalizes_multi_agent_wait_target_alias(self):
        body = json.dumps(
            {
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call_wait",
                        "name": "multi_agent_v1__wait_agent",
                        "arguments": json.dumps({"target": "019f-child", "timeout_ms": "1000"}),
                    }
                ]
            }
        ).encode("utf-8")

        transformed = compatible_response_body(body, "ollama_cloud", event_context={"request_id": "req"})
        call = json.loads(transformed)["output"][0]
        arguments = json.loads(call["arguments"])

        self.assertEqual(call["name"], "wait_agent")
        self.assertEqual(call["namespace"], "multi_agent_v1")
        self.assertEqual(arguments["targets"], ["019f-child"])
        self.assertNotIn("target", arguments)

    def test_external_response_normalizes_multi_agent_close_targets_alias(self):
        body = json.dumps(
            {
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call_close",
                        "name": "multi_agent_v1__close_agent",
                        "arguments": json.dumps({"targets": ["019f-child-a", "019f-child-b"]}),
                    }
                ]
            }
        ).encode("utf-8")

        transformed = compatible_response_body(body, "ollama_cloud", event_context={"request_id": "req"})
        call = json.loads(transformed)["output"][0]
        arguments = json.loads(call["arguments"])

        self.assertEqual(call["name"], "close_agent")
        self.assertEqual(call["namespace"], "multi_agent_v1")
        self.assertEqual(arguments["target"], "019f-child-a")
        self.assertNotIn("targets", arguments)

    def test_external_response_normalizes_multi_agent_spawn_alias_arguments(self):
        body = json.dumps(
            {
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call_spawn",
                        "name": "multi_agent_v1__spawn_agent",
                        "arguments": json.dumps(
                            {
                                "agent_type": "general",
                                "prompt": "return sentinel",
                                "name": "child-a",
                                "fork_context": "false",
                            }
                        ),
                    }
                ]
            }
        ).encode("utf-8")

        transformed = compatible_response_body(body, "ollama_cloud", event_context={"request_id": "req"})
        call = json.loads(transformed)["output"][0]
        arguments = json.loads(call["arguments"])

        self.assertEqual(call["name"], "spawn_agent")
        self.assertEqual(call["namespace"], "multi_agent_v1")
        self.assertEqual(arguments["message"], "return sentinel")
        self.assertEqual(arguments["nickname"], "child-a")
        self.assertIs(arguments["fork_context"], False)
        self.assertNotIn("agent_type", arguments)
        self.assertNotIn("prompt", arguments)
        self.assertNotIn("name", arguments)

    def test_strict_mode_still_repairs_multi_agent_argument_shape(self):
        body = json.dumps(
            {
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call_spawn",
                        "name": "multi_agent_v1__spawn_agent",
                        "arguments": json.dumps(
                            {
                                "prompt": "return ok",
                                "name": "worker",
                                "agent_type": "general",
                            }
                        ),
                    }
                ]
            }
        ).encode("utf-8")

        with patch.dict(os.environ, {"CODEXHUB_SUBAGENT_ASSIST_MODE": "strict"}, clear=False):
            transformed = compatible_response_body(body, "ollama_cloud", event_context={"request_id": "req"})

        call = json.loads(transformed)["output"][0]
        args = json.loads(call["arguments"])
        self.assertEqual(call["namespace"], "multi_agent_v1")
        self.assertEqual(call["name"], "spawn_agent")
        self.assertEqual(args["message"], "return ok")
        self.assertEqual(args["nickname"], "worker")
        self.assertNotIn("agent_type", args)

    def test_external_response_normalizes_concatenated_multi_agent_alias(self):
        body = json.dumps(
            {
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call_spawn",
                        "name": "multi_agent_v1spawn_agent",
                        "arguments": json.dumps({"message": "return sentinel"}),
                    }
                ]
            }
        ).encode("utf-8")

        transformed = compatible_response_body(body, "ollama_cloud", event_context={"request_id": "req"})
        call = json.loads(transformed)["output"][0]

        self.assertEqual(call["type"], "function_call")
        self.assertEqual(call["namespace"], "multi_agent_v1")
        self.assertEqual(call["name"], "spawn_agent")
        self.assertEqual(json.loads(call["arguments"])["message"], "return sentinel")

    def test_worker_response_suppresses_nested_multi_agent_alias(self):
        body = json.dumps(
            {
                "output": [
                    {
                        "type": "function_call",
                        "id": "fc_spawn",
                        "call_id": "call_spawn",
                        "name": "multi_agent_v1spawn_agent",
                        "arguments": json.dumps({"message": "delegate file write"}),
                    }
                ]
            }
        ).encode("utf-8")
        event_context = {
            "request_id": "req",
            "tool_protocol": "responses_structured",
            "subagent_worker_context": True,
        }

        transformed = compatible_response_body(body, "ollama_cloud", event_context=event_context)
        payload = json.loads(transformed)
        transcript = json.dumps(payload, ensure_ascii=False)

        self.assertEqual(payload["output"][0]["type"], "message")
        self.assertIn("worker_subagent_multi_agent_call_suppressed", transcript)
        self.assertNotIn('"type": "function_call"', transcript)
        self.assertNotIn('"namespace": "multi_agent_v1"', transcript)

    def test_coordinator_workflow_response_suppresses_node_repl_after_spawn_and_repairs_wait(self):
        body = json.dumps(
            {
                "output": [
                    {
                        "type": "function_call",
                        "id": "fc_node",
                        "call_id": "call_node",
                        "name": "mcp__node_repl__js",
                        "arguments": json.dumps({"code": "read artifact directly"}),
                    }
                ]
            }
        ).encode("utf-8")
        event_context = {
            "request_id": "req",
            "tool_protocol": "responses_structured",
            "subagent_worker_context": False,
            "subagent_wait_agent_ids": ["agent_1"],
            "subagent_close_agent_ids": [],
            "subagent_spawn_allowed": False,
            "subagent_lifecycle_complete": False,
            "subagent_workflow_active": True,
            "subagent_workflow_plan_read_complete": True,
        }

        transformed = compatible_response_body(body, "ollama_cloud", event_context=event_context)
        payload = json.loads(transformed)
        transcript = json.dumps(payload, ensure_ascii=False)

        self.assertEqual(payload["output"][0]["type"], "function_call")
        self.assertEqual(payload["output"][0]["namespace"], "multi_agent_v1")
        self.assertEqual(payload["output"][0]["name"], "wait_agent")
        self.assertEqual(json.loads(payload["output"][0]["arguments"])["targets"], ["agent_1"])
        self.assertNotIn("mcp__node_repl", transcript)

    def test_coordinator_workflow_response_allows_node_repl_before_first_spawn(self):
        body = json.dumps(
            {
                "output": [
                    {
                        "type": "function_call",
                        "id": "fc_node",
                        "call_id": "call_node",
                        "name": "mcp__node_repl__js",
                        "arguments": json.dumps({"code": "read plan again"}),
                    }
                ]
            }
        ).encode("utf-8")
        event_context = {
            "request_id": "req",
            "tool_protocol": "responses_structured",
            "subagent_worker_context": False,
            "subagent_wait_agent_ids": [],
            "subagent_close_agent_ids": [],
            "subagent_open_agent_ids": [],
            "subagent_closed_agent_ids": [],
            "subagent_spawn_allowed": True,
            "subagent_lifecycle_complete": False,
            "subagent_workflow_active": True,
            "subagent_workflow_plan_read_complete": False,
            "subagent_workflow_plan_read_required": True,
        }

        transformed = compatible_response_body(body, "ollama_cloud", event_context=event_context)
        payload = json.loads(transformed)
        call = payload["output"][0]

        self.assertEqual(call["type"], "function_call")
        self.assertEqual(call["namespace"], "mcp__node_repl")
        self.assertEqual(call["name"], "js")

    def test_coordinator_workflow_response_suppresses_node_repl_after_plan_read_and_repairs_spawn(self):
        workflow_prompt = """
Use the real subagent-driven-development skill and this short diagnostic plan:
C:\\repo\\diagnostics\\subagent-e2e-cli\\short-subagent-development-plan.md

Coordinator inputs:
OUTPUT_PATH=C:\\repo\\diagnostics\\subagent-e2e\\level12-e2e-test\\level2-m3-responses.artifact-r01.txt
SENTINEL=SENTINEL:level2-m3-responses-20260706
MODEL_UNDER_TEST=minimax-m3
ENDPOINT_UNDER_TEST=responses
CASE=level2-m3-responses

Execution constraints:
1. Use real Codex native subagents for implementer, spec reviewer, and code-quality reviewer.
2. The coordinator may read the plan once, but must not create, edit, inspect, or verify OUTPUT_PATH directly.
6. Start with this ordered lifecycle: spawn one implementer, wait, close; then spawn one spec reviewer, wait, close; then spawn one code-quality reviewer, wait, close.
"""
        plan_text = """
# Short Subagent Development E2E Plan

## Task 1: Write The Diagnostic Artifact

The coordinator prompt supplies OUTPUT_PATH and SENTINEL.
Use an implementer subagent, then a spec reviewer, then a code quality reviewer.
"""
        request_body = json.dumps(
            {
                "model": "minimax-m3",
                "input": [
                    {"type": "message", "role": "user", "content": workflow_prompt},
                    {
                        "type": "function_call",
                        "call_id": "call_node_plan",
                        "name": "mcp__node_repl__js",
                        "arguments": json.dumps({"code": "read plan"}),
                    },
                    {"type": "function_call_output", "call_id": "call_node_plan", "output": plan_text},
                ],
                "tools": [{"type": "function", "name": "mcp__node_repl__js", "parameters": {"type": "object"}}],
            }
        ).encode("utf-8")
        event_context = {"request_id": "req"}

        with patch.dict(os.environ, {"CODEXHUB_SUBAGENT_ASSIST_MODE": "assisted"}, clear=False):
            compatible_request_body(
                request_body,
                {"name": "ollama_cloud", "upstream_format": "responses", "tool_protocol": "responses_structured"},
                event_context=event_context,
            )
            response_body = json.dumps(
                {
                    "output": [
                        {
                            "type": "function_call",
                            "id": "fc_node",
                            "call_id": "call_node",
                            "name": "mcp__node_repl__js",
                            "arguments": json.dumps({"code": "check artifact exists"}),
                        }
                    ]
                }
            ).encode("utf-8")
            transformed = compatible_response_body(response_body, "ollama_cloud", event_context=event_context)

        payload = json.loads(transformed)
        transcript = json.dumps(payload, ensure_ascii=False)
        call = payload["output"][0]
        call_args = json.loads(call["arguments"])

        self.assertTrue(event_context["subagent_workflow_plan_read_complete"])
        self.assertEqual(call["type"], "function_call")
        self.assertEqual(call["namespace"], "multi_agent_v1")
        self.assertEqual(call["name"], "spawn_agent")
        self.assertNotIn("mcp__node_repl", transcript)
        self.assertEqual(call_args["nickname"], "implementer")
        self.assertIn("You are the IMPLEMENTER subagent", call_args["message"])
        self.assertIn("case: level2-m3-responses", call_args["message"])
        self.assertIn("SENTINEL:level2-m3-responses-20260706", call_args["message"])

    def test_coordinator_workflow_response_suppresses_unknown_multi_agent_tool(self):
        body = json.dumps(
            {
                "output": [
                    {
                        "type": "function_call",
                        "id": "fc_state",
                        "call_id": "call_state",
                        "name": "multi_agent_v1__get_agent_state__get_agent_state",
                        "arguments": json.dumps({"agent_id": "agent_1"}),
                    }
                ]
            }
        ).encode("utf-8")
        event_context = {
            "request_id": "req",
            "tool_protocol": "responses_structured",
            "subagent_worker_context": False,
            "subagent_wait_agent_ids": ["agent_1"],
            "subagent_close_agent_ids": [],
            "subagent_spawn_allowed": False,
            "subagent_lifecycle_complete": False,
            "subagent_workflow_active": True,
            "subagent_workflow_plan_read_complete": True,
        }

        transformed = compatible_response_body(body, "ollama_cloud", event_context=event_context)
        payload = json.loads(transformed)
        transcript = json.dumps(payload, ensure_ascii=False)

        self.assertEqual(payload["output"][0]["type"], "function_call")
        self.assertEqual(payload["output"][0]["namespace"], "multi_agent_v1")
        self.assertEqual(payload["output"][0]["name"], "wait_agent")
        self.assertNotIn("get_agent_state", json.dumps(payload))

    def test_worker_sse_suppresses_nested_multi_agent_alias_and_arguments(self):
        event_context = {
            "request_id": "req",
            "tool_protocol": "responses_structured",
            "subagent_worker_context": True,
        }
        item_in_progress = {
            "id": "fc_spawn",
            "type": "function_call",
            "status": "in_progress",
            "call_id": "call_spawn",
            "name": "multi_agent_v1spawn_agent",
            "arguments": "",
        }
        item_done = {
            "id": "fc_spawn",
            "type": "function_call",
            "status": "completed",
            "call_id": "call_spawn",
            "name": "multi_agent_v1spawn_agent",
            "arguments": json.dumps({"message": "delegate file write"}),
        }

        added = compatible_sse_line(
            b"data: " + json.dumps(
                {"type": "response.output_item.added", "output_index": 0, "item": item_in_progress}
            ).encode("utf-8") + b"\n",
            "ollama_cloud",
            event_context=event_context,
        )
        args_done = compatible_sse_line(
            b"data: " + json.dumps(
                {
                    "type": "response.function_call_arguments.done",
                    "item_id": "fc_spawn",
                    "output_index": 0,
                    "arguments": item_done["arguments"],
                }
            ).encode("utf-8") + b"\n",
            "ollama_cloud",
            event_context=event_context,
        )
        completed = compatible_sse_line(
            b"data: " + json.dumps(
                {
                    "type": "response.completed",
                    "response": {
                        "id": "resp_spawn",
                        "object": "response",
                        "status": "completed",
                        "output": [item_done],
                    },
                }
            ).encode("utf-8") + b"\n",
            "ollama_cloud",
            event_context=event_context,
        )

        added_payload = json.loads(added.removeprefix(b"data: "))
        completed_payload = json.loads(completed.removeprefix(b"data: "))
        transcript = json.dumps([added_payload, completed_payload], ensure_ascii=False)

        self.assertEqual(args_done, b"")
        self.assertEqual(added_payload["item"]["type"], "message")
        self.assertEqual(completed_payload["response"]["output"][0]["type"], "message")
        self.assertIn("worker_subagent_multi_agent_call_suppressed", transcript)
        self.assertNotIn('"type": "function_call"', transcript)
        self.assertNotIn('"namespace": "multi_agent_v1"', transcript)

    def test_external_sse_normalizes_concatenated_multi_agent_alias_everywhere(self):
        item_in_progress = {
            "id": "fc_spawn",
            "type": "function_call",
            "status": "in_progress",
            "call_id": "call_spawn",
            "name": "multi_agent_v1spawn_agent",
            "arguments": "",
        }
        item_done = {
            "id": "fc_spawn",
            "type": "function_call",
            "status": "completed",
            "call_id": "call_spawn",
            "name": "multi_agent_v1spawn_agent",
            "arguments": json.dumps({"prompt": "return sentinel", "name": "child-a", "agent_type": "general"}),
        }
        events = [
            {"type": "response.output_item.added", "output_index": 0, "item": item_in_progress},
            {
                "type": "response.function_call_arguments.done",
                "item_id": "fc_spawn",
                "output_index": 0,
                "arguments": item_done["arguments"],
            },
            {"type": "response.output_item.done", "output_index": 0, "item": item_done},
            {
                "type": "response.completed",
                "response": {"id": "resp_spawn", "object": "response", "status": "completed", "output": [item_done]},
            },
        ]

        normalized_events = []
        for event in events:
            line = b"data: " + json.dumps(event).encode("utf-8") + b"\n"
            transformed = compatible_sse_line(line, "ollama_cloud", event_context={"request_id": "req"})
            normalized_events.append(json.loads(transformed.removeprefix(b"data: ")))
        transcript = json.dumps(normalized_events, ensure_ascii=False)

        self.assertNotIn("multi_agent_v1spawn_agent", transcript)
        for event in normalized_events:
            if event["type"] in {"response.output_item.added", "response.output_item.done"}:
                call = event["item"]
                self.assertEqual(call["namespace"], "multi_agent_v1")
                self.assertEqual(call["name"], "spawn_agent")
            if event["type"] == "response.completed":
                call = event["response"]["output"][0]
                self.assertEqual(call["namespace"], "multi_agent_v1")
                self.assertEqual(call["name"], "spawn_agent")
                arguments = json.loads(call["arguments"])
                self.assertEqual(arguments["message"], "return sentinel")
                self.assertEqual(arguments["nickname"], "child-a")
                self.assertNotIn("agent_type", arguments)

    def test_external_sse_normalizes_concatenated_multi_agent_alias_fragments(self):
        events = [
            {
                "type": "response.output_item.delta",
                "output_index": 0,
                "item_id": "fc_spawn",
                "name": "multi_agent_v1spawn_agent",
            },
            {
                "type": "response.output_item.delta",
                "output_index": 0,
                "delta": {"name": "multi_agent_v1spawn_agent"},
            },
        ]

        normalized_events = []
        for event in events:
            line = b"data: " + json.dumps(event).encode("utf-8") + b"\n"
            transformed = compatible_sse_line(line, "ollama_cloud", event_context={"request_id": "req"})
            normalized_events.append(json.loads(transformed.removeprefix(b"data: ")))
        transcript = json.dumps(normalized_events, ensure_ascii=False)

        self.assertNotIn("multi_agent_v1spawn_agent", transcript)
        self.assertEqual(normalized_events[0]["namespace"], "multi_agent_v1")
        self.assertEqual(normalized_events[0]["name"], "spawn_agent")
        self.assertEqual(normalized_events[1]["delta"]["namespace"], "multi_agent_v1")
        self.assertEqual(normalized_events[1]["delta"]["name"], "spawn_agent")

    def test_external_response_repairs_multi_agent_trailing_argument_json(self):
        body = json.dumps(
            {
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call_spawn",
                        "name": "multi_agent_v1__spawn_agent",
                        "arguments": '{"message":"return sentinel"}{"message":"duplicate"}',
                    }
                ]
            }
        ).encode("utf-8")

        transformed = compatible_response_body(body, "ollama_cloud", event_context={"request_id": "req"})
        call = json.loads(transformed)["output"][0]
        arguments = json.loads(call["arguments"])

        self.assertEqual(call["name"], "spawn_agent")
        self.assertEqual(call["namespace"], "multi_agent_v1")
        self.assertEqual(arguments["message"], "return sentinel")
        self.assertIs(arguments["fork_context"], False)

    def test_external_response_preserves_argument_object_shape_when_repairing_spawn(self):
        body = json.dumps(
            {
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call_spawn",
                        "name": "multi_agent_v1",
                        "arguments": {
                            "agent_type": "general",
                            "input": "return sentinel",
                            "name": "child-a",
                        },
                    }
                ]
            }
        ).encode("utf-8")

        transformed = compatible_response_body(body, "ollama_cloud", event_context={"request_id": "req"})
        call = json.loads(transformed)["output"][0]
        arguments = call["arguments"]

        self.assertEqual(call["name"], "spawn_agent")
        self.assertEqual(call["namespace"], "multi_agent_v1")
        self.assertIsInstance(arguments, dict)
        self.assertEqual(arguments["message"], "return sentinel")
        self.assertEqual(arguments["nickname"], "child-a")
        self.assertIs(arguments["fork_context"], False)
        self.assertNotIn("agent_type", arguments)
        self.assertNotIn("input", arguments)
        self.assertNotIn("name", arguments)

    def test_raw_provider_probe_skips_request_injection_and_response_repair(self):
        request_body = json.dumps({"model": "glm-5.2", "input": "spawn one child"}).encode("utf-8")
        request_context = {"request_id": "req", "raw_provider_probe": True}

        transformed_request = compatible_request_body(
            request_body,
            {"name": "ollama_cloud"},
            event_context=request_context,
        )

        self.assertEqual(json.loads(transformed_request), {"model": "glm-5.2", "input": "spawn one child"})
        self.assertTrue(raw_provider_probe_requested({"X-CodexHub-Raw-Provider-Probe": "1"}, "/v1/responses"))
        self.assertTrue(raw_provider_probe_requested({}, "/v1/providers/xunfei/chat/completions?raw_provider_probe=1"))

        response_body = json.dumps(
            {
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call_spawn",
                        "name": "multi_agent_v1__spawn_agent",
                        "arguments": json.dumps({"agent_type": "general", "prompt": "return sentinel"}),
                    }
                ]
            }
        ).encode("utf-8")

        transformed_response = compatible_response_body(
            response_body,
            "ollama_cloud",
            event_context={"request_id": "req", "raw_provider_probe": True},
        )

        call = json.loads(transformed_response)["output"][0]
        self.assertEqual(call["name"], "multi_agent_v1__spawn_agent")
        self.assertNotIn("namespace", call)
        self.assertIn("agent_type", json.loads(call["arguments"]))

    def test_text_compat_rewrites_duplicate_spawn_call_to_wait_existing_agent(self):
        body = json.dumps(
            {
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call_repeated_spawn",
                        "name": "multi_agent_v1__spawn_agent",
                        "arguments": json.dumps({"message": "return duplicate child"}),
                    }
                ]
            }
        ).encode("utf-8")

        transformed = compatible_response_body(
            body,
            "ollama_cloud",
            event_context={
                "request_id": "req",
                "tool_protocol": "text_compat",
                "subagent_open_agent_ids": ["019f-child"],
                "subagent_spawn_allowed": False,
                "subagent_lifecycle_complete": False,
            },
        )
        call = json.loads(transformed)["output"][0]

        self.assertEqual(call["type"], "function_call")
        self.assertEqual(call["namespace"], "multi_agent_v1")
        self.assertEqual(call["name"], "wait_agent")
        self.assertEqual(json.loads(call["arguments"])["targets"], ["019f-child"])

    def test_text_compat_allows_append_spawn_when_state_allows_spawn(self):
        body = json.dumps(
            {
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call_append_spawn",
                        "name": "multi_agent_v1__spawn_agent",
                        "arguments": json.dumps({"message": "return child-b"}),
                    }
                ]
            }
        ).encode("utf-8")

        transformed = compatible_response_body(
            body,
            "ollama_cloud",
            event_context={
                "request_id": "req",
                "tool_protocol": "text_compat",
                "subagent_open_agent_ids": ["019f-child-a"],
                "subagent_spawn_allowed": True,
                "subagent_lifecycle_complete": False,
            },
        )
        call = json.loads(transformed)["output"][0]

        self.assertEqual(call["name"], "spawn_agent")
        self.assertEqual(call["namespace"], "multi_agent_v1")

    def test_chat_tools_response_guard_suppresses_duplicate_spawn_arguments(self):
        request_body = json.dumps(
            {
                "model": "glm-5.2",
                "input": [
                    {"type": "message", "role": "user", "content": "Use subagent-driven development for Task 1."},
                    {
                        "type": "function_call",
                        "call_id": "call_impl",
                        "namespace": "multi_agent_v1",
                        "name": "spawn_agent",
                        "arguments": {
                            "message": "Implement Task 1 exactly.",
                            "nickname": "implementer-task-1",
                        },
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_impl",
                        "output": json.dumps({"agent_id": "impl-1", "nickname": "implementer-task-1"}),
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_impl_wait",
                        "namespace": "multi_agent_v1",
                        "name": "wait_agent",
                        "arguments": {"targets": ["impl-1"], "timeout_ms": 60000},
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_impl_wait",
                        "output": json.dumps({"timed_out": False, "status": {"impl-1": {"completed": "DONE"}}}),
                    },
                ],
                "tools": [
                    {"type": "function", "name": "multi_agent_v1__spawn_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__wait_agent", "parameters": {"type": "object"}},
                ],
            }
        ).encode("utf-8")
        event_context = {"request_id": "req"}
        compatible_request_body(
            request_body,
            {"name": "ollama_cloud", "upstream_format": "chat_completions", "tool_protocol": "chat_tools"},
            event_context=event_context,
        )
        response_body = json.dumps(
            {
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call_duplicate_impl",
                        "name": "multi_agent_v1__spawn_agent",
                        "arguments": json.dumps(
                            {"message": "Implement Task 1 exactly.", "nickname": "implementer-task-1"}
                        ),
                    }
                ]
            }
        ).encode("utf-8")

        transformed = compatible_response_body(response_body, "ollama_cloud", event_context=event_context)
        payload = json.loads(transformed)
        transcript = json.dumps(payload, ensure_ascii=False)

        self.assertEqual(payload["output"][0]["type"], "message")
        self.assertIn("required_next_action", transcript)
        self.assertIn("distinct role/task", transcript)

    def test_chat_stream_guard_reconciles_arguments_when_duplicate_spawn_becomes_wait(self):
        request_body = json.dumps(
            {
                "model": "glm-5.2",
                "input": [
                    {"type": "message", "role": "user", "content": "Use subagent-driven development for Task 1."},
                    {
                        "type": "function_call",
                        "call_id": "call_impl",
                        "namespace": "multi_agent_v1",
                        "name": "spawn_agent",
                        "arguments": {
                            "message": "Implement Task 1 exactly.",
                            "nickname": "implementer-task-1",
                        },
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_impl",
                        "output": json.dumps({"agent_id": "impl-1", "nickname": "implementer-task-1"}),
                    },
                ],
                "tools": [
                    {"type": "function", "name": "multi_agent_v1__spawn_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__wait_agent", "parameters": {"type": "object"}},
                ],
            }
        ).encode("utf-8")
        event_context = {"request_id": "req"}
        compatible_request_body(
            request_body,
            {"name": "ollama_cloud", "upstream_format": "chat_completions", "tool_protocol": "chat_tools"},
            event_context=event_context,
        )
        events = [
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {
                    "id": "fc_dup",
                    "type": "function_call",
                    "status": "in_progress",
                    "call_id": "call_duplicate_impl",
                    "name": "multi_agent_v1__spawn_agent",
                    "arguments": "",
                },
            },
            {
                "type": "response.function_call_arguments.done",
                "item_id": "fc_dup",
                "output_index": 0,
                "arguments": json.dumps({"message": "Implement Task 1 exactly.", "nickname": "implementer-task-1"}),
            },
            {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": {
                    "id": "fc_dup",
                    "type": "function_call",
                    "status": "completed",
                    "call_id": "call_duplicate_impl",
                    "name": "multi_agent_v1__spawn_agent",
                    "arguments": json.dumps({"message": "Implement Task 1 exactly.", "nickname": "implementer-task-1"}),
                },
            },
        ]

        guarded, changed = codex_proxy._guard_duplicate_multi_agent_spawn_calls(events, event_context)
        reconciled, reconciled_changed = codex_proxy._reconcile_function_call_argument_events(guarded)
        done_item = next(event["item"] for event in reconciled if event["type"] == "response.output_item.done")
        arguments_done = next(event for event in reconciled if event["type"] == "response.function_call_arguments.done")

        self.assertTrue(changed)
        self.assertTrue(reconciled_changed)
        self.assertEqual(done_item["namespace"], "multi_agent_v1")
        self.assertEqual(done_item["name"], "wait_agent")
        self.assertEqual(json.loads(arguments_done["arguments"]), {"targets": ["impl-1"], "timeout_ms": 60000})

    def test_chat_stream_guard_drops_arguments_when_duplicate_spawn_becomes_message(self):
        request_body = json.dumps(
            {
                "model": "glm-5.2",
                "input": [
                    {"type": "message", "role": "user", "content": "Use subagent-driven development for Task 1."},
                    {
                        "type": "function_call",
                        "call_id": "call_impl",
                        "namespace": "multi_agent_v1",
                        "name": "spawn_agent",
                        "arguments": {
                            "message": "Implement Task 1 exactly.",
                            "nickname": "implementer-task-1",
                        },
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_impl",
                        "output": json.dumps({"agent_id": "impl-1", "nickname": "implementer-task-1"}),
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_impl_wait",
                        "namespace": "multi_agent_v1",
                        "name": "wait_agent",
                        "arguments": {"targets": ["impl-1"], "timeout_ms": 60000},
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_impl_wait",
                        "output": json.dumps({"timed_out": False, "status": {"impl-1": {"completed": "DONE"}}}),
                    },
                ],
                "tools": [
                    {"type": "function", "name": "multi_agent_v1__spawn_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__wait_agent", "parameters": {"type": "object"}},
                ],
            }
        ).encode("utf-8")
        event_context = {"request_id": "req"}
        compatible_request_body(
            request_body,
            {"name": "ollama_cloud", "upstream_format": "chat_completions", "tool_protocol": "chat_tools"},
            event_context=event_context,
        )
        events = [
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {
                    "id": "fc_dup",
                    "type": "function_call",
                    "status": "in_progress",
                    "call_id": "call_duplicate_impl",
                    "name": "multi_agent_v1__spawn_agent",
                    "arguments": "",
                },
            },
            {
                "type": "response.function_call_arguments.done",
                "item_id": "fc_dup",
                "output_index": 0,
                "arguments": json.dumps({"message": "Implement Task 1 exactly.", "nickname": "implementer-task-1"}),
            },
            {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": {
                    "id": "fc_dup",
                    "type": "function_call",
                    "status": "completed",
                    "call_id": "call_duplicate_impl",
                    "name": "multi_agent_v1__spawn_agent",
                    "arguments": json.dumps({"message": "Implement Task 1 exactly.", "nickname": "implementer-task-1"}),
                },
            },
        ]

        guarded, changed = codex_proxy._guard_duplicate_multi_agent_spawn_calls(events, event_context)
        reconciled, reconciled_changed = codex_proxy._reconcile_function_call_argument_events(guarded)

        self.assertTrue(changed)
        self.assertTrue(reconciled_changed)
        self.assertFalse(any(event["type"] == "response.function_call_arguments.done" for event in reconciled))
        self.assertTrue(all(event.get("item", {}).get("type") == "message" for event in reconciled))

    def test_chat_tools_response_repairs_missing_wait_call_when_required(self):
        body = json.dumps(
            {
                "id": "resp_missing_wait",
                "object": "response",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "Waiting now.", "annotations": []}],
                    }
                ],
            }
        ).encode("utf-8")

        transformed = compatible_response_body(
            body,
            "ollama_cloud",
            event_context={
                "request_id": "req",
                "tool_protocol": "chat_tools",
                "subagent_wait_agent_ids": ["impl-1"],
                "subagent_spawn_allowed": False,
                "subagent_lifecycle_complete": False,
            },
        )
        call = json.loads(transformed)["output"][0]
        arguments = json.loads(call["arguments"])

        self.assertEqual(call["type"], "function_call")
        self.assertEqual(call["namespace"], "multi_agent_v1")
        self.assertEqual(call["name"], "wait_agent")
        self.assertEqual(arguments["targets"], ["impl-1"])
        self.assertEqual(arguments["timeout_ms"], 60000)

    def test_strict_mode_does_not_repair_missing_required_close_body(self):
        body = json.dumps(
            {
                "id": "resp_text",
                "object": "response",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "I am done."}],
                    }
                ],
            }
        ).encode("utf-8")
        context = {
            "tool_protocol": "responses_structured",
            "subagent_close_agent_ids": ["agent_1"],
            "subagent_wait_agent_ids": [],
            "subagent_spawn_allowed": False,
            "subagent_lifecycle_complete": False,
        }

        with patch.dict(os.environ, {"CODEXHUB_SUBAGENT_ASSIST_MODE": "strict"}, clear=False):
            transformed = compatible_response_body(body, "ollama_cloud", event_context=context)

        payload = json.loads(transformed)
        self.assertEqual(payload["output"][0]["type"], "message")
        self.assertNotIn("close_agent", json.dumps(payload))

    def test_assisted_mode_repairs_missing_required_close_body(self):
        body = json.dumps(
            {
                "id": "resp_text",
                "object": "response",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "I am done."}],
                    }
                ],
            }
        ).encode("utf-8")
        context = {
            "tool_protocol": "responses_structured",
            "subagent_close_agent_ids": ["agent_1"],
            "subagent_wait_agent_ids": [],
            "subagent_spawn_allowed": False,
            "subagent_lifecycle_complete": False,
        }

        with patch.dict(os.environ, {"CODEXHUB_SUBAGENT_ASSIST_MODE": "assisted"}, clear=False):
            transformed = compatible_response_body(body, "ollama_cloud", event_context=context)

        payload = json.loads(transformed)
        self.assertEqual(payload["output"][0]["type"], "function_call")
        self.assertEqual(payload["output"][0]["namespace"], "multi_agent_v1")
        self.assertEqual(payload["output"][0]["name"], "close_agent")

    def test_assisted_mode_does_not_repair_when_multiple_legal_actions_exist(self):
        body = json.dumps(
            {
                "id": "resp_text",
                "object": "response",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "I will choose the next branch."}],
                    }
                ],
            }
        ).encode("utf-8")
        context = {
            "tool_protocol": "responses_structured",
            "subagent_lifecycle_complete": False,
            "subagent_legal_actions": [
                {"tool_name": "spawn_agent", "arguments": {"message": "task B"}},
                {"tool_name": "spawn_agent", "arguments": {"message": "review task A"}},
            ],
        }

        with patch.dict(os.environ, {"CODEXHUB_SUBAGENT_ASSIST_MODE": "assisted"}, clear=False):
            transformed = compatible_response_body(body, "ollama_cloud", event_context=context)

        payload = json.loads(transformed)
        self.assertEqual(payload["output"][0]["type"], "message")
        self.assertNotIn("function_call", json.dumps(payload))

    def test_responses_structured_response_coerces_wrong_subagent_tool_to_required_wait(self):
        body = json.dumps(
            {
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call_wrong_close",
                        "name": "multi_agent_v1__close_agent",
                        "arguments": json.dumps({"target": "impl-1"}),
                    }
                ]
            }
        ).encode("utf-8")

        transformed = compatible_response_body(
            body,
            "ollama_cloud",
            event_context={
                "request_id": "req",
                "tool_protocol": "responses_structured",
                "subagent_wait_agent_ids": ["impl-1"],
                "subagent_spawn_allowed": False,
                "subagent_lifecycle_complete": False,
            },
        )
        call = json.loads(transformed)["output"][0]

        self.assertEqual(call["namespace"], "multi_agent_v1")
        self.assertEqual(call["name"], "wait_agent")
        self.assertEqual(json.loads(call["arguments"]), {"targets": ["impl-1"], "timeout_ms": 60000})

    def test_responses_structured_sse_coerces_wrong_subagent_tool_arguments_to_required_wait(self):
        event_context = {
            "request_id": "req",
            "tool_protocol": "responses_structured",
            "subagent_wait_agent_ids": ["impl-1"],
            "subagent_spawn_allowed": False,
            "subagent_lifecycle_complete": False,
        }
        wrong_args = json.dumps({"target": "impl-1"})
        events = [
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {
                    "id": "fc_wrong",
                    "type": "function_call",
                    "status": "in_progress",
                    "call_id": "call_wrong_close",
                    "name": "multi_agent_v1__close_agent",
                    "arguments": "",
                },
            },
            {
                "type": "response.function_call_arguments.done",
                "item_id": "fc_wrong",
                "output_index": 0,
                "arguments": wrong_args,
            },
            {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": {
                    "id": "fc_wrong",
                    "type": "function_call",
                    "status": "completed",
                    "call_id": "call_wrong_close",
                    "name": "multi_agent_v1__close_agent",
                    "arguments": wrong_args,
                },
            },
        ]

        normalized = []
        for event in events:
            line = b"data: " + json.dumps(event).encode("utf-8") + b"\n"
            transformed = compatible_sse_line(line, "ollama_cloud", event_context=event_context)
            normalized.append(json.loads(transformed.removeprefix(b"data: ")))
        expected_args = {"targets": ["impl-1"], "timeout_ms": 60000}

        added = normalized[0]["item"]
        args_done = normalized[1]
        done = normalized[2]["item"]
        self.assertEqual(added["namespace"], "multi_agent_v1")
        self.assertEqual(added["name"], "wait_agent")
        self.assertEqual(json.loads(args_done["arguments"]), expected_args)
        self.assertEqual(done["namespace"], "multi_agent_v1")
        self.assertEqual(done["name"], "wait_agent")
        self.assertEqual(json.loads(done["arguments"]), expected_args)

    def test_chat_tools_response_repairs_missing_close_call_when_required(self):
        body = json.dumps(
            {
                "id": "resp_missing_close",
                "object": "response",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "The implementer is done.", "annotations": []}],
                    }
                ],
            }
        ).encode("utf-8")

        transformed = compatible_response_body(
            body,
            "ollama_cloud",
            event_context={
                "request_id": "req",
                "tool_protocol": "chat_tools",
                "subagent_close_agent_ids": ["impl-1"],
                "subagent_spawn_allowed": False,
                "subagent_lifecycle_complete": False,
            },
        )
        call = json.loads(transformed)["output"][0]
        arguments = json.loads(call["arguments"])

        self.assertEqual(call["type"], "function_call")
        self.assertEqual(call["namespace"], "multi_agent_v1")
        self.assertEqual(call["name"], "close_agent")
        self.assertEqual(arguments["target"], "impl-1")

    def test_chat_tools_response_does_not_force_wait_while_spawn_still_allowed(self):
        body = json.dumps(
            {
                "id": "resp_spawn_more",
                "object": "response",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "I will continue.", "annotations": []}],
                    }
                ],
            }
        ).encode("utf-8")

        transformed = compatible_response_body(
            body,
            "ollama_cloud",
            event_context={
                "request_id": "req",
                "tool_protocol": "chat_tools",
                "subagent_wait_agent_ids": ["child-a"],
                "subagent_spawn_allowed": True,
                "subagent_lifecycle_complete": False,
            },
        )
        payload = json.loads(transformed)

        self.assertEqual(payload["output"][0]["type"], "message")

    def test_chat_tools_response_events_repair_missing_close_call(self):
        chunks = [
            {
                "choices": [
                    {
                        "delta": {"role": "assistant"},
                        "finish_reason": None,
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {"content": "The implementer finished."},
                        "finish_reason": None,
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {},
                        "finish_reason": "stop",
                    }
                ]
            },
            "[DONE]",
        ]
        events = _chat_stream_chunks_to_response_events(chunks)

        repaired, changed = codex_proxy._repair_missing_required_subagent_call_events(
            events,
            {
                "request_id": "req",
                "tool_protocol": "chat_tools",
                "subagent_close_agent_ids": ["impl-1"],
                "subagent_spawn_allowed": False,
                "subagent_lifecycle_complete": False,
            },
        )

        self.assertTrue(changed)
        item_done = [event for event in repaired if event.get("type") == "response.output_item.done"][0]
        call = item_done["item"]
        self.assertEqual(call["type"], "function_call")
        self.assertEqual(call["namespace"], "multi_agent_v1")
        self.assertEqual(call["name"], "close_agent")
        self.assertEqual(json.loads(call["arguments"])["target"], "impl-1")

    def test_chat_tools_response_events_repair_close_after_duplicate_spawn_suppressed(self):
        class CloseRequiredState:
            next_action = "close"
            bounded_request = False
            requested_append = False

            def allows_spawn_request(self, arguments):
                return False

        event_context = {
            "request_id": "req",
            "tool_protocol": "chat_tools",
            "subagent_open_agent_ids": ["child-a", "child-b"],
            "subagent_wait_agent_ids": [],
            "subagent_close_agent_ids": ["child-a", "child-b"],
            "subagent_spawn_allowed": False,
            "subagent_lifecycle_complete": False,
            "_subagent_state": CloseRequiredState(),
        }
        duplicate_spawn = {
            "id": "fc_dup",
            "type": "function_call",
            "status": "completed",
            "call_id": "call_dup",
            "name": "multi_agent_v1__spawn_agent",
            "arguments": json.dumps({"message": "Return duplicate child"}),
        }
        events = [
            {"type": "response.created", "response": {"id": "resp_dup_spawn"}},
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {**duplicate_spawn, "status": "in_progress", "arguments": ""},
            },
            {
                "type": "response.function_call_arguments.done",
                "item_id": "fc_dup",
                "output_index": 0,
                "arguments": duplicate_spawn["arguments"],
            },
            {"type": "response.output_item.done", "output_index": 0, "item": duplicate_spawn},
            {
                "type": "response.completed",
                "response": {
                    "id": "resp_dup_spawn",
                    "object": "response",
                    "status": "completed",
                    "output": [duplicate_spawn],
                },
            },
        ]

        events, guard_changed = codex_proxy._guard_duplicate_multi_agent_spawn_calls(events, event_context)
        events, _ = codex_proxy._coerce_required_subagent_tool_calls(events, event_context)
        events, _ = codex_proxy._reconcile_function_call_argument_events(events)
        events, repair_changed = codex_proxy._repair_missing_required_subagent_call_events(events, event_context)
        done_items = [
            event["item"]
            for event in events
            if event.get("type") == "response.output_item.done"
            and isinstance(event.get("item"), dict)
            and event["item"].get("type") == "function_call"
        ]

        self.assertTrue(guard_changed)
        self.assertTrue(repair_changed)
        self.assertEqual(len(done_items), 1)
        self.assertEqual(done_items[0]["namespace"], "multi_agent_v1")
        self.assertEqual(done_items[0]["name"], "close_agent")
        self.assertEqual(json.loads(done_items[0]["arguments"])["target"], "child-a")

    def test_chat_tools_sse_repairs_missing_close_call_on_completed_event(self):
        payload = {
            "type": "response.completed",
            "response": {
                "id": "resp_missing_close",
                "object": "response",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "Closing now.", "annotations": []}],
                    }
                ],
            },
        }
        line = b"data: " + json.dumps(payload).encode("utf-8") + b"\n"

        transformed = compatible_sse_line(
            line,
            "ollama_cloud",
            event_context={
                "request_id": "req",
                "tool_protocol": "chat_tools",
                "subagent_close_agent_ids": ["impl-1"],
                "subagent_spawn_allowed": False,
                "subagent_lifecycle_complete": False,
            },
        )
        events = []
        for raw_line in transformed.splitlines():
            if raw_line.startswith(b"data:"):
                events.append(json.loads(raw_line.removeprefix(b"data:").strip()))

        event_types = [event["type"] for event in events]
        self.assertIn("response.output_item.done", event_types)
        self.assertEqual(events[-1]["type"], "response.completed")
        call = events[-1]["response"]["output"][0]
        self.assertEqual(call["namespace"], "multi_agent_v1")
        self.assertEqual(call["name"], "close_agent")

    def test_text_compat_suppresses_spawn_after_lifecycle_complete(self):
        body = json.dumps(
            {
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call_repeated_spawn",
                        "name": "multi_agent_v1__spawn_agent",
                        "arguments": json.dumps({"message": "return child-again"}),
                    }
                ]
            }
        ).encode("utf-8")

        transformed = compatible_response_body(
            body,
            "ollama_cloud",
            event_context={
                "request_id": "req",
                "tool_protocol": "text_compat",
                "subagent_open_agent_ids": [],
                "subagent_spawn_allowed": False,
                "subagent_lifecycle_complete": True,
            },
        )
        payload = json.loads(transformed)
        transcript = json.dumps(payload, ensure_ascii=False)

        self.assertNotIn("spawn_agent", transcript)
        self.assertIn("required_next_action", transcript)
        self.assertIn("final", transcript.lower())
        self.assertIn("current-turn", transcript)
        self.assertIn("real Codex native tool executions", transcript)
        self.assertIn("hidden tools after close", transcript)

    def test_external_response_normalizes_mcp_node_repl_alias(self):
        body = json.dumps(
            {
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call_node",
                        "name": "mcp__node_repl__js",
                        "arguments": json.dumps({"code": "1+1"}),
                    }
                ]
            }
        ).encode("utf-8")

        transformed = compatible_response_body(body, "ollama_cloud", event_context={"request_id": "req"})
        call = json.loads(transformed)["output"][0]

        self.assertEqual(call["namespace"], "mcp__node_repl")
        self.assertEqual(call["name"], "js")
        self.assertEqual(json.loads(call["arguments"])["code"], "1+1")

    def test_external_response_repairs_generic_trailing_argument_json(self):
        body = json.dumps(
            {
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call_js",
                        "name": "mcp__node_repl__js",
                        "arguments": '{"code":"1+1"}{"code":"duplicate"}',
                    }
                ]
            }
        ).encode("utf-8")

        transformed = compatible_response_body(body, "ollama_cloud", event_context={"request_id": "req"})
        call = json.loads(transformed)["output"][0]

        self.assertEqual(call["namespace"], "mcp__node_repl")
        self.assertEqual(call["name"], "js")
        self.assertEqual(json.loads(call["arguments"]), {"code": "1+1"})

    def test_external_response_preserves_mcp_node_repl_namespace_call(self):
        body = json.dumps(
            {
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call_js",
                        "namespace": "mcp__node_repl",
                        "name": "js",
                        "arguments": json.dumps({"code": "1+1"}),
                    }
                ]
            }
        ).encode("utf-8")

        transformed = compatible_response_body(body, "ollama_cloud", event_context={"request_id": "req"})
        call = json.loads(transformed)["output"][0]

        self.assertEqual(call["namespace"], "mcp__node_repl")
        self.assertEqual(call["name"], "js")
        self.assertEqual(json.loads(call["arguments"]), {"code": "1+1"})

    def test_external_response_preserves_codex_apps_namespace_call(self):
        body = json.dumps(
            {
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call_read",
                        "namespace": "mcp__codex_apps__local_tool_gateway_",
                        "name": "read_file",
                        "arguments": json.dumps({"path": "docs/plan.md"}),
                    }
                ]
            }
        ).encode("utf-8")

        transformed = compatible_response_body(body, "ollama_cloud", event_context={"request_id": "req"})
        call = json.loads(transformed)["output"][0]

        self.assertEqual(call["namespace"], "mcp__codex_apps__local_tool_gateway_")
        self.assertEqual(call["name"], "read_file")
        self.assertEqual(json.loads(call["arguments"])["path"], "docs/plan.md")

    def test_external_response_restores_codex_apps_flat_alias(self):
        body = json.dumps(
            {
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call_read",
                        "name": "mcp__codex_apps__local_tool_gateway___read_file",
                        "arguments": json.dumps({"path": "docs/plan.md"}),
                    }
                ]
            }
        ).encode("utf-8")

        transformed = compatible_response_body(body, "ollama_cloud", event_context={"request_id": "req"})
        call = json.loads(transformed)["output"][0]

        self.assertEqual(call["namespace"], "mcp__codex_apps__local_tool_gateway_")
        self.assertEqual(call["name"], "read_file")
        self.assertEqual(json.loads(call["arguments"])["path"], "docs/plan.md")

    def test_external_sse_preserves_codex_apps_namespace_call(self):
        payload = {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "call_id": "call_read",
                "namespace": "mcp__codex_apps__local_tool_gateway_",
                "name": "read_file",
                "arguments": json.dumps({"path": "docs/plan.md"}),
            },
        }
        line = b"data: " + json.dumps(payload).encode("utf-8") + b"\n"

        transformed = compatible_sse_line(line, "ollama_cloud", event_context={"request_id": "req"})
        call = json.loads(transformed.removeprefix(b"data: "))["item"]

        self.assertEqual(call["namespace"], "mcp__codex_apps__local_tool_gateway_")
        self.assertEqual(call["name"], "read_file")
        self.assertEqual(json.loads(call["arguments"])["path"], "docs/plan.md")

    def test_external_sse_restores_codex_apps_flat_alias(self):
        payload = {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "call_id": "call_read",
                "name": "mcp__codex_apps__local_tool_gateway___read_file",
                "arguments": json.dumps({"path": "docs/plan.md"}),
            },
        }
        line = b"data: " + json.dumps(payload).encode("utf-8") + b"\n"

        transformed = compatible_sse_line(line, "ollama_cloud", event_context={"request_id": "req"})
        call = json.loads(transformed.removeprefix(b"data: "))["item"]

        self.assertEqual(call["namespace"], "mcp__codex_apps__local_tool_gateway_")
        self.assertEqual(call["name"], "read_file")
        self.assertEqual(json.loads(call["arguments"])["path"], "docs/plan.md")

    def test_external_sse_normalizes_tool_search_function_call(self):
        payload = {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "call_id": "call_search",
                "name": "tool_search",
                "arguments": json.dumps({"query": "spawn_agent", "limit": "8"}),
            },
        }
        line = b"data: " + json.dumps(payload).encode("utf-8") + b"\n"

        transformed = compatible_sse_line(line, "ollama_cloud", event_context={"request_id": "req"})
        call = json.loads(transformed.removeprefix(b"data: "))["item"]

        self.assertEqual(call["type"], "tool_search_call")
        self.assertEqual(call["arguments"], {"query": "spawn_agent", "limit": 8})

    def test_kimi_k2_6_external_request_removes_unsupported_reasoning(self):
        body = json.dumps(
            {
                "model": "volc/kimi-k2.6",
                "input": "hi",
                "reasoning": {"effort": "medium"},
            }
        ).encode("utf-8")

        transformed = compatible_request_body(
            body,
            {"name": "volcengine", "upstream_model": "kimi-k2.6-code"},
            event_context={"request_id": "req"},
        )

        self.assertNotIn("reasoning", json.loads(transformed))

    def test_kimi_k2_7_external_request_removes_unsupported_reasoning(self):
        body = json.dumps(
            {
                "model": "ollama-e2e-responses/kimi-k2.7-code",
                "input": "hi",
                "reasoning": {"effort": "medium"},
            }
        ).encode("utf-8")

        transformed = compatible_request_body(
            body,
            {
                "name": "ollama-e2e-responses",
                "upstream_model": "kimi-k2.7-code",
                "upstream_format": "responses",
                "tool_protocol": "responses_structured",
            },
            event_context={"request_id": "req"},
        )

        self.assertNotIn("reasoning", json.loads(transformed))

    def test_kimi_k2_7_chat_tools_keeps_reasoning(self):
        body = json.dumps(
            {
                "model": "ollama-e2e-chat/kimi-k2.7-code",
                "input": "hi",
                "reasoning": {"effort": "medium"},
            }
        ).encode("utf-8")

        transformed = compatible_request_body(
            body,
            {
                "name": "ollama-e2e-chat",
                "upstream_model": "kimi-k2.7-code",
                "upstream_format": "chat_completions",
                "tool_protocol": "chat_tools",
            },
            event_context={"request_id": "req"},
        )

        self.assertEqual(json.loads(transformed)["reasoning"], {"effort": "medium"})

    def test_response_header_filter_omits_hop_by_hop(self):
        headers = {
            "Content-Type": "text/event-stream",
            "Transfer-Encoding": "chunked",
            "Connection": "keep-alive",
            "Keep-Alive": "timeout=5",
            "Proxy-Authenticate": "Basic",
            "Proxy-Authorization": "Basic abc",
            "TE": "trailers",
            "Trailers": "Expires",
            "Upgrade": "websocket",
            "Content-Length": "999",
            "X-Trace": "abc",
        }

        filtered = dict(_filtered_response_headers(headers, is_event_stream=True))

        self.assertEqual(filtered, {"Content-Type": "text/event-stream", "X-Trace": "abc"})

    def test_websocket_upgrade_detection_accepts_tokenized_connection_header(self):
        self.assertTrue(
            _is_websocket_upgrade(
                {
                    "Connection": "keep-alive, Upgrade",
                    "Upgrade": "websocket",
                }
            )
        )
        self.assertFalse(_is_websocket_upgrade({"Connection": "keep-alive", "Upgrade": "websocket"}))


if __name__ == "__main__":
    unittest.main()
