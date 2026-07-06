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
from codex_proxy import (
    CodexProxyHandler,
    RETRY_REQUEST_COMPACT,
    _chat_stream_chunks_to_response_events,
    _filtered_response_headers,
    _is_websocket_upgrade,
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

    def send_response(self, status, message=None):
        self.status = status

    def send_header(self, key, value):
        self.headers.append((key, value))

    def end_headers(self):
        self.headers_ended = True

    def _write_downstream_sse_error(self, **kwargs):
        return CodexProxyHandler._write_downstream_sse_error(self, **kwargs)

    def _write_sse_event(self, event, payload):
        return CodexProxyHandler._write_sse_event(self, event, payload)

    def _send_sse_headers(self, status, upstream_name):
        return CodexProxyHandler._send_sse_headers(self, status, upstream_name)

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
        line = self.lines.pop(0)
        if isinstance(line, BaseException):
            raise line
        return line

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return None


class FakeDelayedSseResponse(FakeSseResponse):
    def __init__(self, lines, first_delay_seconds):
        super().__init__(lines)
        self.first_delay_seconds = first_delay_seconds
        self.readline_calls = 0

    def readline(self):
        self.readline_calls += 1
        if self.readline_calls == 1:
            time.sleep(self.first_delay_seconds)
        return super().readline()


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


class TimeoutAfterBytes:
    def __init__(self, data: bytes):
        self.stream = io.BytesIO(data)

    def read(self, size=-1):
        chunk = self.stream.read(size)
        if chunk:
            return chunk
        raise TimeoutError("idle websocket probe")


def masked_client_ws_frame(payload: bytes, *, opcode: int = 0x1, mask: bytes = b"\x05\x06\x07\x08") -> bytes:
    if len(payload) > 65535:
        raise ValueError("test helper only supports 16-bit websocket frames")
    if len(payload) <= 125:
        length_bytes = bytes([0x80 | len(payload)])
    else:
        length_bytes = b"\xfe" + len(payload).to_bytes(2, "big")
    masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
    return bytes([0x80 | opcode]) + length_bytes + mask + masked


def websocket_get_handler(path: str, frame_bytes: bytes = b""):
    handler = CodexProxyHandler.__new__(CodexProxyHandler)
    handler.path = path
    handler.headers = {
        "Connection": "keep-alive, Upgrade",
        "Upgrade": "websocket",
        "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==",
        "Sec-WebSocket-Version": "13",
        "Sec-WebSocket-Protocol": "codex, realtime",
        "Authorization": "Bearer secret-token",
        "Cookie": "sid=secret",
        "X-Codex-Client-Id": "codex-app",
    }
    handler.rfile = io.BytesIO(frame_bytes)
    handler.close_connection = False
    fake = FakeHandler()
    handler.send_response = fake.send_response
    handler.send_header = fake.send_header
    handler.end_headers = fake.end_headers
    handler.wfile = fake.wfile
    return handler, fake


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
        self.ollama_runtime_patch = patch("codex_proxy.resolve_ollama_cloud_model", return_value=(False, None))
        self.ollama_runtime_patch.start()
        self.addCleanup(self.ollama_runtime_patch.stop)

    def test_gateway_auto_retry_settings_default_to_enabled_thirty_attempts(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(codex_proxy.gateway_auto_retry_enabled())
            self.assertEqual(codex_proxy.gateway_auto_retry_max_attempts(), 30)

    def test_official_http_passthrough_setting_defaults_enabled_and_env_can_disable(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(codex_proxy.gateway_official_http_passthrough_enabled())
        with patch.dict(os.environ, {"CODEX_PROXY_OFFICIAL_HTTP_PASSTHROUGH_ENABLED": "0"}, clear=True):
            self.assertFalse(codex_proxy.gateway_official_http_passthrough_enabled())

    def test_official_codex_app_responses_uses_http_passthrough_profile(self):
        upstream = {"name": "official"}
        context = {"client_id": "codex-app"}

        self.assertEqual(
            codex_proxy.behavior_profile_for_request(upstream, context, inbound_format="responses"),
            codex_proxy.BEHAVIOR_OFFICIAL_CODEX_APP_HTTP_PASSTHROUGH,
        )

    def test_official_chat_completions_uses_gateway_compat_profile(self):
        upstream = {"name": "official"}
        context = {"client_id": "codex-app"}

        self.assertEqual(
            codex_proxy.behavior_profile_for_request(upstream, context, inbound_format="chat_completions"),
            codex_proxy.BEHAVIOR_OFFICIAL_GATEWAY_COMPAT,
        )

    def test_official_unknown_client_uses_gateway_compat_profile(self):
        upstream = {"name": "official"}
        context = {"client_id": "unknown"}

        self.assertEqual(
            codex_proxy.behavior_profile_for_request(upstream, context, inbound_format="responses"),
            codex_proxy.BEHAVIOR_OFFICIAL_GATEWAY_COMPAT,
        )

    def test_third_party_always_uses_external_gateway_profile(self):
        upstream = {"name": "ollama"}
        context = {"client_id": "codex-app"}

        self.assertEqual(
            codex_proxy.behavior_profile_for_request(upstream, context, inbound_format="responses"),
            codex_proxy.BEHAVIOR_EXTERNAL_PROVIDER_GATEWAY,
        )

    def test_official_codex_app_compact_request_does_not_strip_tools_before_passthrough_profile(self):
        body = json.dumps(
            {
                "model": "openai/gpt-5.5",
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": (
                            "Create a detailed summary of the conversation so far. "
                            "This is a compact summary. Do not call any tools. "
                            "The summary should include <summary>."
                        ),
                    }
                ],
                "tools": [{"type": "function", "name": "multi_agent_v1__spawn_agent"}],
                "stream": False,
            }
        ).encode("utf-8")
        handler = CodexProxyHandler.__new__(CodexProxyHandler)
        handler.path = "/v1/responses"
        handler.headers = {
            "Content-Length": str(len(body)),
            "Content-Type": "application/json",
            "X-Codex-Client-Id": "codex-app",
        }
        handler.rfile = io.BytesIO(body)
        handler.close_connection = False
        fake = FakeHandler()
        handler.send_response = fake.send_response
        handler.send_header = fake.send_header
        handler.end_headers = fake.end_headers
        handler.wfile = fake.wfile

        with (
            patch("codex_proxy.codex_access_token", return_value="sub-token"),
            patch("codex_proxy.codex_account_id", return_value="acct-1"),
            patch("codex_proxy._open_upstream_response", return_value=FakeContextResponse(b'{"id":"resp_official","output":[]}')),
            patch("codex_proxy._strip_tools_for_compact_payload", wraps=codex_proxy._strip_tools_for_compact_payload) as strip_tools,
        ):
            CodexProxyHandler.do_POST(handler)

        strip_tools.assert_not_called()

    def test_third_party_compact_request_still_strips_tools(self):
        body = json.dumps(
            {
                "model": "volc/glm-5.2",
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": (
                            "Create a detailed summary of the conversation so far. "
                            "This is a compact summary. Respond with text only. "
                            "The summary should include <summary>."
                        ),
                    }
                ],
                "tools": [{"type": "function", "name": "multi_agent_v1__spawn_agent"}],
                "stream": False,
            }
        ).encode("utf-8")
        handler = CodexProxyHandler.__new__(CodexProxyHandler)
        handler.path = "/v1/responses"
        handler.headers = {
            "Content-Length": str(len(body)),
            "Content-Type": "application/json",
            "X-Codex-Client-Id": "codex-app",
        }
        handler.rfile = io.BytesIO(body)
        handler.close_connection = False
        fake = FakeHandler()
        handler.send_response = fake.send_response
        handler.send_header = fake.send_header
        handler.end_headers = fake.end_headers
        handler.wfile = fake.wfile

        with (
            patch("codex_proxy._open_upstream_response", return_value=FakeContextResponse(b'{"id":"resp_external","output":[]}')),
            patch("codex_proxy._strip_tools_for_compact_payload", wraps=codex_proxy._strip_tools_for_compact_payload) as strip_tools,
        ):
            CodexProxyHandler.do_POST(handler)

        strip_tools.assert_called_once()

    def test_post_request_events_include_behavior_profile_on_success(self):
        body = json.dumps(
            {
                "model": "openai/gpt-5.5",
                "input": [{"type": "message", "role": "user", "content": "hi"}],
                "stream": False,
            }
        ).encode("utf-8")
        handler = CodexProxyHandler.__new__(CodexProxyHandler)
        handler.path = "/v1/responses"
        handler.headers = {
            "Content-Length": str(len(body)),
            "Content-Type": "application/json",
            "X-Codex-Client-Id": "codex-app",
        }
        handler.rfile = io.BytesIO(body)
        handler.close_connection = False
        fake = FakeHandler()
        handler.send_response = fake.send_response
        handler.send_header = fake.send_header
        handler.end_headers = fake.end_headers
        handler.wfile = fake.wfile

        with (
            patch("codex_proxy.codex_access_token", return_value="sub-token"),
            patch("codex_proxy.codex_account_id", return_value="acct-1"),
            patch("codex_proxy._open_upstream_response", return_value=FakeContextResponse(b'{"id":"resp_official","output":[]}')),
        ):
            CodexProxyHandler.do_POST(handler)

        events = [(call.args[0], call.kwargs) for call in self.write_proxy_event.call_args_list]
        request_start = next(fields for event, fields in events if event == "request_start")
        request_complete = next(fields for event, fields in events if event == "request_complete")
        self.assertEqual(
            request_start["behavior_profile"],
            codex_proxy.BEHAVIOR_OFFICIAL_CODEX_APP_HTTP_PASSTHROUGH,
        )
        self.assertEqual(
            request_complete["behavior_profile"],
            codex_proxy.BEHAVIOR_OFFICIAL_CODEX_APP_HTTP_PASSTHROUGH,
        )

    def test_post_request_error_includes_behavior_profile(self):
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
            "X-Codex-Client-Id": "codex-app",
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

        with patch("codex_proxy._open_upstream_response", side_effect=error):
            CodexProxyHandler.do_POST(handler)

        events = [(call.args[0], call.kwargs) for call in self.write_proxy_event.call_args_list]
        request_error = next(fields for event, fields in events if event == "request_error")
        self.assertEqual(
            request_error["behavior_profile"],
            codex_proxy.BEHAVIOR_EXTERNAL_PROVIDER_GATEWAY,
        )

    def test_official_http_passthrough_uses_single_open_attempt_and_no_stream_retry_deferral(self):
        body = json.dumps(
            {
                "model": "openai/gpt-5.5",
                "input": [{"type": "message", "role": "user", "content": "hi"}],
                "stream": True,
            }
        ).encode("utf-8")
        handler = CodexProxyHandler.__new__(CodexProxyHandler)
        handler.path = "/v1/responses"
        handler.headers = {
            "Content-Length": str(len(body)),
            "Content-Type": "application/json",
            "X-Codex-Client-Id": "codex-app",
        }
        handler.rfile = io.BytesIO(body)
        handler.close_connection = False
        fake = FakeHandler()
        handler.send_response = fake.send_response
        handler.send_header = fake.send_header
        handler.end_headers = fake.end_headers
        handler.wfile = fake.wfile
        relayed = []
        handler._relay_upstream_response = lambda response, upstream_name, **kwargs: relayed.append(kwargs) or 200

        with (
            patch("codex_proxy.codex_access_token", return_value="sub-token"),
            patch("codex_proxy.codex_account_id", return_value="acct-1"),
            patch("codex_proxy._open_upstream_response", return_value=FakeContextResponse(b'{"id":"resp_official","output":[]}')) as open_response,
        ):
            CodexProxyHandler.do_POST(handler)

        self.assertEqual(open_response.call_args.kwargs.get("max_attempts"), 1)
        self.assertFalse(relayed[0]["defer_stream_errors"])

    def test_official_http_passthrough_does_not_call_image_proxy(self):
        body = json.dumps(
            {
                "model": "openai/gpt-5.5",
                "input": [{"type": "message", "role": "user", "content": "describe this"}],
                "stream": False,
            }
        ).encode("utf-8")
        handler = CodexProxyHandler.__new__(CodexProxyHandler)
        handler.path = "/v1/responses"
        handler.headers = {
            "Content-Length": str(len(body)),
            "Content-Type": "application/json",
            "X-Codex-Client-Id": "codex-app",
        }
        handler.rfile = io.BytesIO(body)
        handler.close_connection = False
        fake = FakeHandler()
        handler.send_response = fake.send_response
        handler.send_header = fake.send_header
        handler.end_headers = fake.end_headers
        handler.wfile = fake.wfile

        with (
            patch.dict(os.environ, {"CODEX_PROXY_IMAGE_PROXY_ENABLED": "1"}, clear=False),
            patch("codex_proxy.codex_access_token", return_value="sub-token"),
            patch("codex_proxy.codex_account_id", return_value="acct-1"),
            patch("codex_proxy._open_upstream_response", return_value=FakeContextResponse(b'{"id":"resp_official","output":[]}')),
            patch("codex_proxy.apply_image_proxy_to_responses_payload", return_value=False) as image_proxy,
        ):
            CodexProxyHandler.do_POST(handler)

        image_proxy.assert_not_called()

    def test_official_http_passthrough_open_failure_does_not_emit_gateway_retry_or_notice(self):
        body = json.dumps(
            {
                "model": "openai/gpt-5.5",
                "input": [{"type": "message", "role": "user", "content": "hi"}],
                "stream": True,
            }
        ).encode("utf-8")
        handler = CodexProxyHandler.__new__(CodexProxyHandler)
        handler.path = "/v1/responses"
        handler.headers = {
            "Content-Length": str(len(body)),
            "Content-Type": "application/json",
            "X-Codex-Client-Id": "codex-app",
        }
        handler.rfile = io.BytesIO(body)
        handler.close_connection = False
        fake = FakeHandler()
        handler.send_response = fake.send_response
        handler.send_header = fake.send_header
        handler.end_headers = fake.end_headers
        handler.wfile = fake.wfile
        error = HTTPError(
            "https://chatgpt.com/backend-api/codex/responses",
            503,
            "Service Unavailable",
            {"Content-Type": "application/json"},
            io.BytesIO(b'{"error":{"type":"server_error","message":"try later"}}'),
        )
        success = FakeContextResponse(b'{"id":"resp_official","output":[]}')

        with (
            patch.dict(
                os.environ,
                {
                    "CODEX_PROXY_AUTO_RETRY_ENABLED": "1",
                    "CODEX_PROXY_AUTO_RETRY_MAX_ATTEMPTS": "2",
                    "CODEX_PROXY_DOWNSTREAM_RETRY_NOTICE_ENABLED": "1",
                },
                clear=False,
            ),
            patch("codex_proxy.codex_access_token", return_value="sub-token"),
            patch("codex_proxy.codex_account_id", return_value="acct-1"),
            patch("codex_proxy.urlopen", side_effect=[error, success]) as mock_urlopen,
            patch("codex_proxy.time.sleep") as mock_sleep,
        ):
            CodexProxyHandler.do_POST(handler)

        self.assertEqual(mock_urlopen.call_count, 1)
        mock_sleep.assert_not_called()
        event_names = [call.args[0] for call in self.write_proxy_event.call_args_list if call.args]
        self.assertNotIn("upstream_retry", event_names)
        self.assertNotIn("sse_retry_notice", event_names)

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

    def test_model_event_idle_timeout_uses_new_env_before_legacy_setting_alias(self):
        settings_path = Path(self.runtime_proxy_dir.name) / "settings.json"
        with open(settings_path, "w", encoding="utf-8") as handle:
            json.dump({"gateway_post_content_sse_idle_timeout_seconds": 60}, handle)

        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(codex_proxy.model_event_sse_idle_timeout_seconds(), 60)
        with patch.dict(os.environ, {"CODEX_PROXY_MODEL_EVENT_SSE_IDLE_TIMEOUT_SECONDS": "300"}, clear=True):
            self.assertEqual(codex_proxy.model_event_sse_idle_timeout_seconds(), 300)

    def test_gateway_retry_delay_caps_after_third_retry(self):
        self.assertEqual([codex_proxy.gateway_retry_delay_seconds(attempt) for attempt in range(1, 6)], [2, 4, 6, 8, 8])

    def test_gateway_capacity_retry_delay_uses_slower_cadence(self):
        delays = [
            codex_proxy.gateway_retry_delay_seconds(
                attempt,
                failure_class=codex_proxy.RETRY_FAILURE_PROVIDER_THROTTLE,
            )
            for attempt in range(1, 6)
        ]
        self.assertEqual(delays, [10, 20, 30, 60, 60])

    def test_gateway_retry_delay_respects_retry_after_header(self):
        error = HTTPError(
            "https://ark.example.test/v1/responses",
            429,
            "Too Many Requests",
            {"Retry-After": "7"},
            io.BytesIO(b'{"error":{"code":"11210","message":"tpm exceeded"}}'),
        )
        self.assertEqual(
            codex_proxy.gateway_retry_delay_seconds(
                1,
                failure_class=codex_proxy.RETRY_FAILURE_PROVIDER_THROTTLE,
                exc=error,
            ),
            7,
        )

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
        mock_sleep.assert_called_once_with(10)
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
        self.assertEqual(fields["failure_class"], codex_proxy.RETRY_FAILURE_PROVIDER_THROTTLE)
        self.assertEqual(fields["attempt"], 1)
        self.assertEqual(fields["max_attempts"], 3)
        self.assertEqual(fields["delay_ms"], 10000)

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
                    io.BytesIO(b'{"error":{"type":"server_error","message":"try later"}}'),
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
                expected_delay = 10 if status in {429, 503} else 2
                mock_sleep.assert_called_once_with(expected_delay)

    def test_open_upstream_response_classifies_provider_capacity_codes(self):
        request = codex_proxy.Request("https://ark.example.test/v1/responses", data=b"{}", method="POST")
        cases = [
            (
                429,
                b'{"error":{"code":"11210","message":"tpm exceeded; wait and retry"}}',
                codex_proxy.RETRY_FAILURE_PROVIDER_THROTTLE,
            ),
            (
                503,
                b'{"error":{"code":"10012","message":"engine internal error or queued"}}',
                codex_proxy.RETRY_FAILURE_PROVIDER_OVERLOADED,
            ),
        ]
        for status, body, expected_class in cases:
            with self.subTest(status=status, expected_class=expected_class):
                self.write_proxy_event.reset_mock()
                error = HTTPError(
                    "https://ark.example.test/v1/responses",
                    status,
                    "Capacity Error",
                    {},
                    io.BytesIO(body),
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
                    patch("codex_proxy.urlopen", side_effect=[error, success]),
                    patch("codex_proxy.time.sleep") as mock_sleep,
                ):
                    response = codex_proxy._open_upstream_response(
                        request,
                        upstream_name="volcengine",
                        upstream_format="responses",
                        timeout=1,
                        event_context={"request_id": "req_capacity", "model": "volc/glm-5.2"},
                        request_kind="main_generation",
                    )

                self.assertIs(response, success)
                mock_sleep.assert_called_once_with(10)
                retry_events = [
                    call.kwargs for call in self.write_proxy_event.call_args_list
                    if call.args and call.args[0] == "upstream_retry"
                ]
                self.assertEqual(len(retry_events), 1)
                self.assertEqual(retry_events[0]["failure_class"], expected_class)
                self.assertEqual(retry_events[0]["delay_ms"], 10000)

    def test_open_upstream_response_stops_capacity_retry_when_retry_after_exceeds_elapsed_limit(self):
        request = codex_proxy.Request("https://ark.example.test/v1/responses", data=b"{}", method="POST")
        error = HTTPError(
            "https://ark.example.test/v1/responses",
            429,
            "Too Many Requests",
            {"Retry-After": "10"},
            io.BytesIO(b'{"error":{"code":"11210","message":"tpm exceeded"}}'),
        )

        with (
            patch.dict(
                os.environ,
                {
                    "CODEX_PROXY_AUTO_RETRY_ENABLED": "1",
                    "CODEX_PROXY_AUTO_RETRY_MAX_ATTEMPTS": "2",
                    "CODEX_PROXY_CAPACITY_RETRY_ELAPSED_LIMIT_SECONDS": "5",
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
                    event_context={"request_id": "req_capacity_limit", "model": "volc/glm-5.2"},
                    request_kind="main_generation",
                )

        self.assertEqual(mock_urlopen.call_count, 1)
        mock_sleep.assert_not_called()
        retry_events = [
            call for call in self.write_proxy_event.call_args_list
            if call.args and call.args[0] == "upstream_retry"
        ]
        self.assertEqual(retry_events, [])

    def test_open_upstream_response_capacity_retry_uses_global_attempts_without_request_kind_override(self):
        request = codex_proxy.Request("https://ark.example.test/v1/responses", data=b"{}", method="POST")
        errors = [
            HTTPError(
                "https://ark.example.test/v1/responses",
                429,
                "Too Many Requests",
                {},
                io.BytesIO(b'{"error":{"code":"11210","message":"tpm exceeded"}}'),
            )
            for _ in range(4)
        ]
        success = FakeResponse(b'{"id":"resp_retry"}')

        with (
            patch.dict(
                os.environ,
                {
                    "CODEX_PROXY_AUTO_RETRY_ENABLED": "1",
                    "CODEX_PROXY_AUTO_RETRY_MAX_ATTEMPTS": "5",
                },
                clear=False,
            ),
            patch("codex_proxy.urlopen", side_effect=[*errors, success]) as mock_urlopen,
            patch("codex_proxy.time.sleep") as mock_sleep,
        ):
            response = codex_proxy._open_upstream_response(
                request,
                upstream_name="volcengine",
                upstream_format="responses",
                timeout=1,
                event_context={"request_id": "req_capacity_global", "model": "volc/glm-5.2"},
                request_kind="main_generation",
            )

        self.assertIs(response, success)
        self.assertEqual(mock_urlopen.call_count, 5)
        self.assertEqual([call.args[0] for call in mock_sleep.call_args_list], [10, 20, 30, 60])
        retry_events = [
            call.kwargs for call in self.write_proxy_event.call_args_list
            if call.args and call.args[0] == "upstream_retry"
        ]
        self.assertEqual([event["max_attempts"] for event in retry_events], [5, 5, 5, 5])

    def test_stream_quick_transient_retry_uses_global_attempts_without_request_kind_override(self):
        with patch.dict(
            os.environ,
            {
                "CODEX_PROXY_AUTO_RETRY_ENABLED": "1",
                "CODEX_PROXY_AUTO_RETRY_MAX_ATTEMPTS": "5",
            },
            clear=False,
        ):
            base_attempts = codex_proxy._upstream_retry_attempts(codex_proxy.RETRY_REQUEST_MAIN_GENERATION)
            self.assertEqual(base_attempts, 3)
            self.assertEqual(
                codex_proxy._retry_attempts_for_failure_class(
                    request_kind=codex_proxy.RETRY_REQUEST_MAIN_GENERATION,
                    base_attempts=base_attempts,
                    failure_class=codex_proxy.RETRY_FAILURE_QUICK_TRANSIENT,
                    explicit_max_attempts=False,
                    stream_failure=True,
                ),
                5,
            )
            self.assertEqual(
                codex_proxy._retry_attempts_for_failure_class(
                    request_kind=codex_proxy.RETRY_REQUEST_MAIN_GENERATION,
                    base_attempts=base_attempts,
                    failure_class=codex_proxy.RETRY_FAILURE_QUICK_TRANSIENT,
                    explicit_max_attempts=False,
                    stream_failure=False,
                ),
                3,
            )

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

    def test_post_responses_streaming_keeps_retry_notice_out_of_downstream_by_default(self):
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
        mock_sleep.assert_called_once_with(10)
        self.assertEqual(fake.status, 200)
        written = b"".join(fake.wfile.writes)
        self.assertNotIn(b"event: codexhub.retry\n", written)
        self.assertNotIn(b'"type":"codexhub.retry"', written)
        self.assertIn(b"response.output_text.delta", written)
        notice_events = [
            call.kwargs for call in self.write_proxy_event.call_args_list
            if call.args and call.args[0] == "sse_retry_notice"
        ]
        self.assertEqual(notice_events, [])

    def test_post_responses_streaming_retries_read_error_before_first_upstream_event(self):
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
        failed_stream = FakeSseResponse([TimeoutError("read timed out")])
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
                    "CODEX_PROXY_SSE_KEEPALIVE_SECONDS": "0",
                },
                clear=False,
            ),
            patch("codex_proxy.urlopen", side_effect=[failed_stream, success]) as mock_urlopen,
            patch("codex_proxy.time.sleep") as mock_sleep,
        ):
            CodexProxyHandler.do_POST(handler)

        self.assertEqual(mock_urlopen.call_count, 2)
        mock_sleep.assert_called_once_with(2)
        self.assertEqual(fake.status, 200)
        written = b"".join(fake.wfile.writes)
        self.assertNotIn(b"event: codexhub.retry\n", written)
        self.assertNotIn(b'"type":"codexhub.retry"', written)
        self.assertIn(b"response.output_text.delta", written)
        self.assertNotIn(b"upstream_stream_error", written)
        retry_events = [
            call.kwargs for call in self.write_proxy_event.call_args_list
            if call.args and call.args[0] == "upstream_retry"
        ]
        self.assertEqual(len(retry_events), 1)
        self.assertEqual(retry_events[0]["error"], "TimeoutError")

    def test_post_responses_streaming_retries_reset_after_buffered_reasoning_start(self):
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
        failed_stream = FakeSseResponse(
            [
                b'data: {"type":"response.created","response":{"id":"resp_reset","status":"in_progress"}}\n\n',
                (
                    b'data: {"type":"response.output_item.added","output_index":0,'
                    b'"item":{"id":"rs_1","type":"reasoning","status":"in_progress","summary":[]}}\n\n'
                ),
                ConnectionResetError("socket reset"),
            ]
        )
        success = FakeSseResponse(
            [
                b'data: {"type":"response.created","response":{"id":"resp_retry","status":"in_progress"}}\n\n',
                b'data: {"type":"response.output_text.delta","delta":"ok"}\n\n',
                b'data: {"type":"response.completed","response":{"id":"resp_retry","status":"completed","usage":{"input_tokens":1,"output_tokens":1}}}\n\n',
                b"",
            ]
        )

        with (
            patch.dict(
                os.environ,
                {
                    "CODEX_PROXY_AUTO_RETRY_ENABLED": "1",
                    "CODEX_PROXY_AUTO_RETRY_MAX_ATTEMPTS": "2",
                    "CODEX_PROXY_SSE_KEEPALIVE_SECONDS": "0",
                },
                clear=False,
            ),
            patch("codex_proxy.urlopen", side_effect=[failed_stream, success]) as mock_urlopen,
            patch("codex_proxy.time.sleep") as mock_sleep,
        ):
            CodexProxyHandler.do_POST(handler)

        self.assertEqual(mock_urlopen.call_count, 2)
        mock_sleep.assert_called_once_with(2)
        self.assertEqual(fake.status, 200)
        written = b"".join(fake.wfile.writes)
        self.assertIn(b"resp_retry", written)
        self.assertIn(b"response.output_text.delta", written)
        self.assertNotIn(b"resp_reset", written)
        self.assertNotIn(b"rs_1", written)
        self.assertNotIn(b"upstream_stream_error", written)
        retry_events = [
            call.kwargs for call in self.write_proxy_event.call_args_list
            if call.args and call.args[0] == "upstream_retry"
        ]
        self.assertEqual(len(retry_events), 1)
        self.assertEqual(retry_events[0]["error"], "ConnectionResetError")

    def test_post_responses_streaming_retries_incomplete_after_buffered_text_delta(self):
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
        failed_stream = FakeSseResponse(
            [
                b'data: {"type":"response.created","response":{"id":"resp_text_reset","status":"in_progress"}}\n\n',
                b'data: {"type":"response.output_text.delta","delta":"partial"}\n\n',
                b"",
            ]
        )
        success = FakeSseResponse(
            [
                b'data: {"type":"response.created","response":{"id":"resp_text_retry","status":"in_progress"}}\n\n',
                b'data: {"type":"response.output_text.delta","delta":"ok"}\n\n',
                b'data: {"type":"response.completed","response":{"id":"resp_text_retry","status":"completed","usage":{"input_tokens":1,"output_tokens":1}}}\n\n',
                b"",
            ]
        )

        with (
            patch.dict(
                os.environ,
                {
                    "CODEX_PROXY_AUTO_RETRY_ENABLED": "1",
                    "CODEX_PROXY_AUTO_RETRY_MAX_ATTEMPTS": "2",
                    "CODEX_PROXY_SSE_KEEPALIVE_SECONDS": "0",
                },
                clear=False,
            ),
            patch("codex_proxy.urlopen", side_effect=[failed_stream, success]) as mock_urlopen,
            patch("codex_proxy.time.sleep") as mock_sleep,
        ):
            CodexProxyHandler.do_POST(handler)

        self.assertEqual(mock_urlopen.call_count, 2)
        mock_sleep.assert_called_once_with(2)
        self.assertEqual(fake.status, 200)
        written = b"".join(fake.wfile.writes)
        self.assertIn(b"resp_text_retry", written)
        self.assertIn(b"response.output_text.delta", written)
        self.assertIn(b'"delta":"ok"', written)
        self.assertNotIn(b"resp_text_reset", written)
        self.assertNotIn(b"partial", written)
        retry_events = [
            call.kwargs for call in self.write_proxy_event.call_args_list
            if call.args and call.args[0] == "upstream_retry"
        ]
        self.assertEqual(len(retry_events), 1)
        self.assertEqual(retry_events[0]["error"], "UpstreamStreamIncompleteError")

    def test_post_responses_streaming_uses_global_attempts_for_buffered_stream_incomplete(self):
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
        failed_streams = [
            FakeSseResponse(
                [
                    f'data: {{"type":"response.created","response":{{"id":"resp_text_reset_{index}","status":"in_progress"}}}}\n\n'.encode("utf-8"),
                    f'data: {{"type":"response.output_text.delta","delta":"partial {index}"}}\n\n'.encode("utf-8"),
                    b"",
                ]
            )
            for index in range(4)
        ]
        success = FakeSseResponse(
            [
                b'data: {"type":"response.created","response":{"id":"resp_text_retry","status":"in_progress"}}\n\n',
                b'data: {"type":"response.output_text.delta","delta":"ok"}\n\n',
                b'data: {"type":"response.completed","response":{"id":"resp_text_retry","status":"completed","usage":{"input_tokens":1,"output_tokens":1}}}\n\n',
                b"",
            ]
        )

        with (
            patch.dict(
                os.environ,
                {
                    "CODEX_PROXY_AUTO_RETRY_ENABLED": "1",
                    "CODEX_PROXY_AUTO_RETRY_MAX_ATTEMPTS": "5",
                    "CODEX_PROXY_STREAM_RETRY_ELAPSED_LIMIT_SECONDS": "0",
                    "CODEX_PROXY_SSE_KEEPALIVE_SECONDS": "0",
                },
                clear=False,
            ),
            patch("codex_proxy.urlopen", side_effect=[*failed_streams, success]) as mock_urlopen,
            patch("codex_proxy.time.sleep") as mock_sleep,
        ):
            CodexProxyHandler.do_POST(handler)

        self.assertEqual(mock_urlopen.call_count, 5)
        self.assertEqual([call.args[0] for call in mock_sleep.call_args_list], [2, 4, 6, 8])
        self.assertEqual(fake.status, 200)
        written = b"".join(fake.wfile.writes)
        self.assertIn(b"resp_text_retry", written)
        self.assertIn(b'"delta":"ok"', written)
        self.assertNotIn(b"partial", written)
        retry_events = [
            call.kwargs for call in self.write_proxy_event.call_args_list
            if call.args and call.args[0] == "upstream_retry"
        ]
        self.assertEqual(len(retry_events), 4)
        self.assertEqual([event["max_attempts"] for event in retry_events], [5, 5, 5, 5])

    def test_post_responses_streaming_synthesizes_terminal_after_buffered_tool_call_done(self):
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
        failed_stream = FakeSseResponse(
            [
                b'data: {"type":"response.created","response":{"id":"resp_tool_reset","status":"in_progress"}}\n\n',
                (
                    b'data: {"type":"response.output_item.added","output_index":0,'
                    b'"item":{"id":"ctc_patch","type":"custom_tool_call","status":"in_progress",'
                    b'"call_id":"call_patch","name":"apply_patch","input":""}}\n\n'
                ),
                (
                    b'data: {"type":"response.custom_tool_call_input.delta","item_id":"ctc_patch",'
                    b'"output_index":0,"delta":"*** Begin Patch\\n"}\n\n'
                ),
                (
                    b'data: {"type":"response.custom_tool_call_input.done","item_id":"ctc_patch",'
                    b'"output_index":0,"input":"*** Begin Patch\\n*** End Patch\\n"}\n\n'
                ),
                (
                    b'data: {"type":"response.output_item.done","output_index":0,'
                    b'"item":{"id":"ctc_patch","type":"custom_tool_call","status":"completed",'
                    b'"call_id":"call_patch","name":"apply_patch","input":"*** Begin Patch\\n*** End Patch\\n"}}\n\n'
                ),
                b"",
            ]
        )
        with (
            patch.dict(
                os.environ,
                {
                    "CODEX_PROXY_AUTO_RETRY_ENABLED": "1",
                    "CODEX_PROXY_AUTO_RETRY_MAX_ATTEMPTS": "2",
                    "CODEX_PROXY_SSE_KEEPALIVE_SECONDS": "0",
                },
                clear=False,
            ),
            patch("codex_proxy.urlopen", side_effect=[failed_stream]) as mock_urlopen,
            patch("codex_proxy.time.sleep") as mock_sleep,
        ):
            CodexProxyHandler.do_POST(handler)

        self.assertEqual(mock_urlopen.call_count, 1)
        mock_sleep.assert_not_called()
        self.assertEqual(fake.status, 200)
        written = b"".join(fake.wfile.writes)
        self.assertIn(b"resp_tool_reset", written)
        self.assertIn(b"call_patch", written)
        self.assertIn(b"response.custom_tool_call_input.done", written)
        self.assertIn(b"event: response.completed", written)
        self.assertIn(b'"status":"completed"', written)
        self.assertNotIn(b"upstream_stream_error", written)
        retry_events = [
            call.kwargs for call in self.write_proxy_event.call_args_list
            if call.args and call.args[0] == "upstream_retry"
        ]
        self.assertEqual(retry_events, [])
        synthesized_events = [
            call.kwargs for call in self.write_proxy_event.call_args_list
            if call.args and call.args[0] == "upstream_stream_incomplete_synthesized_terminal"
        ]
        self.assertEqual(len(synthesized_events), 1)
        self.assertEqual(synthesized_events[0]["completed_tool_calls"], 1)

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

    def test_openai_fast_alias_routes_to_priority_service_tier(self):
        upstream = choose_upstream("openai/gpt-5.5-fast")

        self.assertEqual(upstream["name"], "official")
        self.assertEqual(upstream["auth"], "codex_auth")
        self.assertEqual(upstream["upstream_model"], "gpt-5.5")
        self.assertEqual(upstream["service_tier"], "priority")

        body = compatible_request_body(
            b'{"model":"openai/gpt-5.5-fast","input":"hi"}',
            upstream,
            "openai/gpt-5.5-fast",
        )
        payload = json.loads(body)

        self.assertEqual(payload["model"], "gpt-5.5")
        self.assertEqual(payload["service_tier"], "priority")

    def test_provider_scoped_openai_fast_routes_to_priority_service_tier(self):
        route_model = codex_proxy.provider_scoped_route_model("gpt-5.5-fast", "openai")
        upstream = choose_upstream(route_model)

        self.assertEqual(route_model, "openai/gpt-5.5-fast")
        self.assertEqual(upstream["name"], "official")
        self.assertEqual(upstream["upstream_model"], "gpt-5.5")
        self.assertEqual(upstream["service_tier"], "priority")

    def test_bare_fast_model_routes_to_priority_service_tier(self):
        upstream = choose_upstream("gpt-5.5-fast")

        self.assertEqual(upstream["name"], "official")
        self.assertEqual(upstream["auth"], "codex_auth")
        self.assertEqual(upstream["upstream_model"], "gpt-5.5")
        self.assertEqual(upstream["service_tier"], "priority")

        body = compatible_request_body(
            b'{"model":"gpt-5.5-fast","input":"hi","service_tier":"default"}',
            upstream,
            "gpt-5.5-fast",
        )
        payload = json.loads(body)

        self.assertEqual(payload["model"], "gpt-5.5")
        self.assertEqual(payload["service_tier"], "priority")

    def test_current_catalog_data_exposes_official_fast_pseudo_models(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            catalog_path = Path(tmpdir) / "catalog.json"
            catalog_path.write_text(
                json.dumps(
                    {
                        "models": [
                            {
                                "slug": "openai/gpt-5.5",
                                "display_name": "OpenAI GPT-5.5",
                                "context_window": 258400,
                            },
                            {
                                "slug": "openai/gpt-5.4",
                                "display_name": "OpenAI GPT-5.4",
                                "context_window": 272000,
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )

            with patch("codex_proxy.existing_generated_catalog_path", return_value=catalog_path):
                catalog = codex_proxy.current_catalog_data()

        by_slug = {model["slug"]: model for model in catalog["models"]}
        self.assertEqual(by_slug["openai/gpt-5.5-fast"]["display_name"], "OpenAI GPT-5.5 Fast")
        self.assertEqual(by_slug["openai/gpt-5.5-fast"]["context_window"], 258400)
        self.assertEqual(by_slug["openai/gpt-5.5-fast"]["codex_proxy_metadata"]["upstream_model"], "gpt-5.5")
        self.assertEqual(by_slug["openai/gpt-5.5-fast"]["codex_proxy_metadata"]["service_tier"], "priority")
        self.assertEqual(by_slug["openai/gpt-5.4-fast"]["display_name"], "OpenAI GPT-5.4 Fast")

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

    def test_runtime_enabled_external_provider_model_routes_without_static_policy_allowlist(self):
        volc_minimax = {
            "alias": "volc/minimax-m3",
            "provider_alias": "volc",
            "upstream_name": "volcengine",
            "display_prefix": "Volc",
            "base_url": "https://ark.example.test/v1",
            "api_key": "volc-test-token",
            "upstream_model": "minimax-m3",
            "priority_base": 200,
            "context_window": 200000,
            "max_output_tokens": 8192,
            "input_modalities": ("text",),
            "context_source": "providers_toml",
            "max_output_source": "providers_toml",
        }

        with patch("codex_proxy.resolve_external_model_alias", return_value=volc_minimax):
            upstream = choose_upstream("volc/minimax-m3")

        self.assertEqual(upstream["name"], "volcengine")
        self.assertEqual(upstream["upstream_model"], "minimax-m3")

    def test_runtime_ollama_provider_model_routes_without_static_policy_allowlist_or_generated_catalog(self):
        runtime_model = {
            "alias": "ollama-cloud/new-runtime-model",
            "provider_alias": "ollama-cloud",
            "upstream_name": "ollama_cloud",
            "display_prefix": "Ollama",
            "base_url": "https://ollama.example.test/v1",
            "api_key": "ollama-runtime-token",
            "upstream_model": "new-runtime-model",
            "upstream_format": "responses",
            "tool_protocol": "auto",
            "input_modalities": ("text",),
        }

        with (
            patch("codex_proxy.resolve_ollama_cloud_model", return_value=(True, runtime_model)),
            patch("codex_proxy.generated_catalog_slugs", return_value=set()),
        ):
            upstream = choose_upstream("ollama-cloud/new-runtime-model")

        self.assertEqual(upstream["name"], "ollama_cloud")
        self.assertEqual(upstream["auth"], "api_key")
        self.assertEqual(upstream["base_url"], "https://ollama.example.test/v1")
        self.assertEqual(upstream["upstream_model"], "new-runtime-model")

    def test_runtime_ollama_provider_rejects_disabled_model_despite_static_policy_allowlist(self):
        with patch("codex_proxy.resolve_ollama_cloud_model", return_value=(True, None)):
            with self.assertRaises(ValueError) as context:
                choose_upstream("ollama-cloud/glm-5.2")

        self.assertIn("model is not allowed", str(context.exception))

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

    def test_official_http_passthrough_only_maps_model_service_tier_and_store(self):
        body = json.dumps(
            {
                "model": "openai/gpt-5.5-fast",
                "input": [{"role": "user", "content": "Current URL: https://example.test/page"}],
                "tools": [{"type": "function", "name": "multi_agent_v1__spawn_agent"}],
                "stream": False,
                "max_output_tokens": 123,
            }
        ).encode("utf-8")
        upstream = {"name": "official", "upstream_model": "gpt-5.5", "service_tier": "priority"}

        transformed = compatible_request_body(
            body,
            upstream,
            model_id="openai/gpt-5.5-fast",
            behavior_profile=codex_proxy.BEHAVIOR_OFFICIAL_CODEX_APP_HTTP_PASSTHROUGH,
        )
        payload = json.loads(transformed)

        self.assertEqual(payload["model"], "gpt-5.5")
        self.assertEqual(payload["service_tier"], "priority")
        self.assertIs(payload["store"], False)
        self.assertIs(payload["stream"], False)
        self.assertEqual(payload["max_output_tokens"], 123)
        self.assertNotIn("Codex browser context detected.", json.dumps(payload))
        self.assertEqual(payload["tools"], [{"type": "function", "name": "multi_agent_v1__spawn_agent"}])

    def test_official_gateway_compat_keeps_existing_official_mutations(self):
        upstream = choose_upstream("gpt-5.5")
        body = json.dumps(
            {
                "model": "gpt-5.5",
                "input": [{"role": "user", "content": "Current URL: https://example.test/page"}],
                "stream": False,
                "max_output_tokens": 100,
            }
        ).encode("utf-8")

        transformed = compatible_request_body(
            body,
            upstream,
            behavior_profile=codex_proxy.BEHAVIOR_OFFICIAL_GATEWAY_COMPAT,
        )
        payload = json.loads(transformed)

        self.assertIs(payload["store"], False)
        self.assertIs(payload["stream"], True)
        self.assertNotIn("max_output_tokens", payload)
        self.assertIn("Codex browser context detected.", json.dumps(payload))

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
        lines = [
            b'data: {"type":"response.output_text.delta","delta":"one"}\n',
            b"\n",
            b'data: {"type":"response.completed","response":{"status":"completed"}}\n',
            b"\n",
            b"",
        ]
        response = FakeSseResponse(lines)

        CodexProxyHandler._relay_upstream_response(handler, response, "official")

        self.assertEqual(handler.status, 200)
        self.assertTrue(handler.headers_ended)
        self.assertEqual(handler.wfile.writes, lines[:-1])
        self.assertGreaterEqual(handler.wfile.flush_count, 3)
        self.assertTrue(handler.close_connection)

    def test_sse_relay_keeps_downstream_alive_while_waiting_for_upstream_line(self):
        handler = FakeHandler()
        response = FakeDelayedSseResponse(
            [
                b'data: {"type":"response.created","response":{"id":"resp_keepalive","status":"in_progress"}}\n\n',
                b'data: {"type":"response.completed","response":{"status":"completed"}}\n\n',
                b"",
            ],
            first_delay_seconds=0.05,
        )

        with patch.dict(os.environ, {"CODEX_PROXY_SSE_KEEPALIVE_SECONDS": "0.01"}, clear=False):
            CodexProxyHandler._relay_upstream_response(handler, response, "official")

        written = b"".join(handler.wfile.writes)
        keepalive_index = written.index(b": codexhub.keepalive\n\n")
        first_event_index = written.index(b"response.created")
        self.assertLess(keepalive_index, first_event_index)
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

    def test_external_sse_relay_drops_reasoning_text_delta_for_codex_app(self):
        handler = FakeHandler()
        event = {
            "type": "response.reasoning_text.delta",
            "item_id": "rs_123",
            "output_index": 0,
            "content_index": 0,
            "delta": "streamed raw thinking",
        }
        response = FakeSseResponse(
            [
                f"data: {json.dumps(event)}\n".encode("utf-8"),
                b"\n",
                b'data: {"type":"response.completed","response":{"status":"completed"}}\n',
                b"\n",
                b"",
            ]
        )

        status = CodexProxyHandler._relay_upstream_response(handler, response, "ollama_cloud")

        data = b"".join(handler.wfile.writes)
        self.assertEqual(status, 200)
        self.assertNotIn(b"streamed raw thinking", data)
        self.assertIn(b"response.completed", data)

    def test_external_responses_sse_relay_drops_named_reasoning_summary_event_frame(self):
        handler = FakeHandler()
        response = FakeSseResponse(
            [
                b"event: response.reasoning_summary_text.delta\n",
                b'data: {"type":"response.reasoning_summary_text.delta","delta":"hidden"}\n',
                b"\n",
                b"event: response.output_text.delta\n",
                b'data: {"type":"response.output_text.delta","delta":"ok"}\n',
                b"\n",
                b"event: response.completed\n",
                b'data: {"type":"response.completed","response":{"status":"completed"}}\n',
                b"\n",
                b"",
            ]
        )

        status = CodexProxyHandler._relay_upstream_response(
            handler,
            response,
            "ollama_cloud",
            request_id="req_named_reasoning_summary",
            model="ollama-cloud/glm-5.2",
            upstream_format="responses",
            inbound_format="responses",
            caller_stream=True,
        )

        data = b"".join(handler.wfile.writes)
        self.assertEqual(status, 200)
        self.assertNotIn(b"event: response.reasoning_summary_text.delta", data)
        self.assertNotIn(b"hidden", data)
        self.assertIn(b"event: response.output_text.delta", data)
        self.assertIn(b'"delta":"ok"', data)
        self.assertIn(b"event: response.completed", data)

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
        completed = {
            "type": "response.completed",
            "response": {"id": "resp_bad_tool", "status": "completed", "output": []},
        }
        response = FakeSseResponse([
            f"data: {json.dumps(event)}\n".encode("utf-8"),
            b"\n",
            f"data: {json.dumps(completed)}\n".encode("utf-8"),
            b"\n",
            b"",
        ])

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
        self.assertEqual(done["item"]["arguments"], "{\"message\":\"hi\"}")
        self.assertEqual(completed["response"]["output"][0]["call_id"], "call_spawn")

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

    def test_responses_sse_passthrough_aborts_on_model_event_idle_timeout(self):
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
                "CODEX_PROXY_TRANSPORT_SSE_IDLE_TIMEOUT_SECONDS": "10",
                "CODEX_PROXY_MODEL_EVENT_SSE_IDLE_TIMEOUT_SECONDS": "0.02",
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
        self.assertEqual(event_kwargs["stream_idle_phase"], "model_event")

    def test_responses_sse_passthrough_aborts_on_transport_idle_timeout(self):
        handler = FakeHandler()
        response = FakeSequencedDelayedSseResponse(
            [
                (0.1, b'data: {"type":"response.created","response":{"id":"resp_pre","model":"gpt-5.5"}}\n\n'),
                (0, b'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'),
                (0, b""),
            ]
        )

        with patch.dict(
            os.environ,
            {
                "CODEX_PROXY_TRANSPORT_SSE_IDLE_TIMEOUT_SECONDS": "0.01",
                "CODEX_PROXY_MODEL_EVENT_SSE_IDLE_TIMEOUT_SECONDS": "10",
            },
            clear=False,
        ):
            status = CodexProxyHandler._relay_upstream_response(
                handler,
                response,
                "official",
                request_id="req_transport_idle",
                model="openai/gpt-5.5",
                upstream_format="responses",
                inbound_format="responses",
                caller_stream=True,
            )

        data = b"".join(handler.wfile.writes)
        self.assertEqual(status, 502)
        self.assertIn(b"upstream_stream_idle_timeout", data)
        self.assertNotIn(b"response.created", data)
        matching_events = [
            call
            for call in self.write_proxy_event.call_args_list
            if call.args and call.args[0] == "upstream_stream_idle_timeout"
        ]
        self.assertTrue(matching_events)
        event_kwargs = matching_events[-1].kwargs
        self.assertEqual(event_kwargs["stream_idle_timeout_seconds"], 0.01)
        self.assertEqual(event_kwargs["stream_idle_phase"], "transport")

    def test_responses_sse_passthrough_aborts_after_output_model_event_idle_timeout(self):
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

        with patch.dict(
            os.environ,
            {
                "CODEX_PROXY_TRANSPORT_SSE_IDLE_TIMEOUT_SECONDS": "10",
                "CODEX_PROXY_MODEL_EVENT_SSE_IDLE_TIMEOUT_SECONDS": "0.02",
            },
            clear=False,
        ):
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
        self.assertEqual(event_kwargs["stream_idle_phase"], "model_event")
        self.assertTrue(event_kwargs["downstream_output_started"])

    def test_responses_sse_function_call_argument_deltas_keep_idle_timer_alive(self):
        handler = FakeHandler()
        response = FakeSequencedDelayedSseResponse(
            [
                (0, b'data: {"type":"response.created","response":{"id":"resp_tool","model":"gpt-5.5"}}\n\n'),
                (
                    0,
                    b'data: {"type":"response.output_item.added","output_index":0,"item":{"id":"fc_tool","type":"function_call","status":"in_progress","call_id":"call_tool","name":"shell_command","arguments":""}}\n\n',
                ),
                (
                    0.03,
                    b'data: {"type":"response.function_call_arguments.delta","item_id":"fc_tool","output_index":0,"delta":"{\\"command\\":"}\n\n',
                ),
                (
                    0.03,
                    b'data: {"type":"response.function_call_arguments.delta","item_id":"fc_tool","output_index":0,"delta":"\\"rg idle\\""}\n\n',
                ),
                (
                    0.03,
                    b'data: {"type":"response.function_call_arguments.done","item_id":"fc_tool","output_index":0,"arguments":"{\\"command\\":\\"rg idle\\"}"}\n\n',
                ),
                (
                    0,
                    b'data: {"type":"response.output_item.done","output_index":0,"item":{"id":"fc_tool","type":"function_call","status":"completed","call_id":"call_tool","name":"shell_command","arguments":"{\\"command\\":\\"rg idle\\"}"}}\n\n',
                ),
                (0, b'data: {"type":"response.completed","response":{"id":"resp_tool","status":"completed","output":[]}}\n\n'),
                (0, b""),
            ]
        )

        with patch.dict(
            os.environ,
            {
                "CODEX_PROXY_TRANSPORT_SSE_IDLE_TIMEOUT_SECONDS": "10",
                "CODEX_PROXY_MODEL_EVENT_SSE_IDLE_TIMEOUT_SECONDS": "0.05",
            },
            clear=False,
        ):
            status = CodexProxyHandler._relay_upstream_response(
                handler,
                response,
                "official",
                request_id="req_tool_arg_idle",
                model="openai/gpt-5.5",
                upstream_format="responses",
                inbound_format="responses",
                caller_stream=True,
            )

        data = b"".join(handler.wfile.writes)
        self.assertEqual(status, 200)
        self.assertIn(b"response.function_call_arguments.done", data)
        self.assertIn(b"response.completed", data)
        self.assertNotIn(b"upstream_stream_idle_timeout", data)

    def test_responses_sse_custom_tool_input_deltas_keep_idle_timer_alive(self):
        handler = FakeHandler()
        response = FakeSequencedDelayedSseResponse(
            [
                (0, b'data: {"type":"response.created","response":{"id":"resp_patch","model":"gpt-5.5"}}\n\n'),
                (
                    0,
                    b'data: {"type":"response.output_item.added","output_index":0,"item":{"id":"ctc_patch","type":"custom_tool_call","status":"in_progress","call_id":"call_patch","name":"apply_patch","input":""}}\n\n',
                ),
                (
                    0.03,
                    b'data: {"type":"response.custom_tool_call_input.delta","item_id":"ctc_patch","output_index":0,"delta":"*** Begin Patch\\n"}\n\n',
                ),
                (
                    0.03,
                    b'data: {"type":"response.custom_tool_call_input.delta","item_id":"ctc_patch","output_index":0,"delta":"*** Add File: e2e.py\\n"}\n\n',
                ),
                (
                    0.03,
                    b'data: {"type":"response.custom_tool_call_input.done","item_id":"ctc_patch","output_index":0,"input":"*** Begin Patch\\n*** Add File: e2e.py\\n*** End Patch\\n"}\n\n',
                ),
                (
                    0,
                    b'data: {"type":"response.output_item.done","output_index":0,"item":{"id":"ctc_patch","type":"custom_tool_call","status":"completed","call_id":"call_patch","name":"apply_patch","input":"*** Begin Patch\\n*** Add File: e2e.py\\n*** End Patch\\n"}}\n\n',
                ),
                (0, b'data: {"type":"response.completed","response":{"id":"resp_patch","status":"completed","output":[]}}\n\n'),
                (0, b""),
            ]
        )

        with patch.dict(
            os.environ,
            {
                "CODEX_PROXY_TRANSPORT_SSE_IDLE_TIMEOUT_SECONDS": "10",
                "CODEX_PROXY_MODEL_EVENT_SSE_IDLE_TIMEOUT_SECONDS": "0.05",
            },
            clear=False,
        ):
            status = CodexProxyHandler._relay_upstream_response(
                handler,
                response,
                "official",
                request_id="req_custom_tool_idle",
                model="openai/gpt-5.5",
                upstream_format="responses",
                inbound_format="responses",
                caller_stream=True,
            )

        data = b"".join(handler.wfile.writes)
        self.assertEqual(status, 200)
        self.assertIn(b"response.custom_tool_call_input.done", data)
        self.assertIn(b"response.completed", data)
        self.assertNotIn(b"upstream_stream_idle_timeout", data)

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

    def test_buffered_responses_sse_without_completed_returns_502_error(self):
        handler = FakeHandler()
        response = FakeSseResponse([
            b'data: {"type":"response.created","response":{"id":"resp_1","model":"gpt-5.5"}}\n\n',
            b'data: {"type":"response.output_text.delta","delta":"partial"}\n\n',
            b"",
        ])

        status = CodexProxyHandler._relay_upstream_response(
            handler,
            response,
            "official",
            request_id="req_incomplete_buffer",
            model="openai/gpt-5.5",
            upstream_format="responses",
            inbound_format="responses",
            caller_stream=False,
        )

        self.assertEqual(status, 502)
        self.assertEqual(handler.status, 502)
        payload = json.loads(handler.wfile.writes[0])
        self.assertEqual(payload["error"]["type"], "upstream_stream_incomplete")
        self.assertEqual(payload["error"]["code"], "upstream_stream_incomplete")
        headers = dict(handler.headers)
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(headers["Content-Length"], str(len(handler.wfile.writes[0])))
        matching_events = [
            call
            for call in self.write_proxy_event.call_args_list
            if call.args and call.args[0] == "upstream_stream_incomplete"
        ]
        self.assertTrue(matching_events)
        event_kwargs = matching_events[-1].kwargs
        self.assertEqual(event_kwargs["request_id"], "req_incomplete_buffer")
        self.assertEqual(event_kwargs["model"], "openai/gpt-5.5")
        self.assertEqual(event_kwargs["upstream"], "official")
        self.assertEqual(event_kwargs["status"], 502)
        self.assertEqual(event_kwargs["upstream_format"], "responses")
        self.assertEqual(event_kwargs["inbound_format"], "responses")

    def test_responses_sse_to_chat_stream_without_completed_writes_sse_error(self):
        handler = FakeHandler()
        response = FakeSseResponse([
            b'data: {"type":"response.created","response":{"id":"resp_1","model":"gpt-5.5"}}\n\n',
            b'data: {"type":"response.output_text.delta","delta":"partial"}\n\n',
            b"",
        ])

        status = CodexProxyHandler._relay_upstream_response(
            handler,
            response,
            "official",
            request_id="req_incomplete_chat_convert",
            model="openai/gpt-5.5",
            upstream_format="responses",
            inbound_format="chat_completions",
            caller_stream=True,
        )

        self.assertEqual(status, 502)
        data = b"".join(handler.wfile.writes)
        self.assertIn(b"upstream_stream_incomplete", data)
        self.assertNotIn(b"finish_reason", data)
        self.assertNotIn(b"data: [DONE]", data)

    def test_chat_sse_without_finish_or_done_writes_sse_error(self):
        handler = FakeHandler()
        response = FakeSseResponse([
            b'data: {"id":"chatcmpl_1","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"content":"partial"},"finish_reason":null}]}\n\n',
            b"",
        ])

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

        self.assertEqual(status, 502)
        data = b"".join(handler.wfile.writes)
        self.assertIn(b"upstream_stream_incomplete", data)
        self.assertNotIn(b"data: [DONE]", data)

    def test_responses_sse_passthrough_without_terminal_writes_sse_error(self):
        handler = FakeHandler()
        response = FakeSseResponse([
            b'data: {"type":"response.created","response":{"id":"resp_1","model":"gpt-5.5"}}\n\n',
            b'data: {"type":"response.output_text.delta","delta":"partial"}\n\n',
            b"",
        ])

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

        self.assertEqual(status, 502)
        data = b"".join(handler.wfile.writes)
        self.assertIn(b"upstream_stream_incomplete", data)
        self.assertIn(b'"retry_owner":"client"', data)
        self.assertIn(b'"failure_class":"quick_transient"', data)
        self.assertIn(b'"retryable":true', data)

    def test_responses_sse_passthrough_empty_stream_writes_sse_error(self):
        handler = FakeHandler()
        response = FakeSseResponse([b""])

        status = CodexProxyHandler._relay_upstream_response(
            handler,
            response,
            "official",
            request_id="req_empty_responses_passthrough",
            model="openai/gpt-5.5",
            upstream_format="responses",
            inbound_format="responses",
            caller_stream=True,
        )

        self.assertEqual(status, 502)
        data = b"".join(handler.wfile.writes)
        self.assertIn(b"upstream_stream_incomplete", data)

    def test_responses_sse_passthrough_empty_stream_defers_before_output(self):
        handler = FakeHandler()
        response = FakeSseResponse([b""])

        with self.assertRaises(codex_proxy.UpstreamStreamIncompleteError):
            CodexProxyHandler._relay_upstream_response(
                handler,
                response,
                "official",
                request_id="req_empty_responses_passthrough_retry",
                model="openai/gpt-5.5",
                upstream_format="responses",
                inbound_format="responses",
                caller_stream=True,
                defer_stream_errors=True,
            )

        self.assertEqual(handler.status, 200)
        self.assertEqual(handler.wfile.writes, [])

    def test_responses_sse_error_event_before_output_defers_without_body(self):
        handler = FakeHandler()
        response = FakeSseResponse(
            [
                b'event: response.created\n',
                b'data: {"type":"response.created","response":{"id":"resp_busy","status":"in_progress","output":[]}}\n',
                b"\n",
                b'event: error\n',
                b'data: {"type":"error","error":{"code":10012,"message":"The system is busy, please try again later."}}\n',
                b"\n",
                b"",
            ]
        )

        with self.assertRaises(codex_proxy.UpstreamStreamErrorEvent) as raised:
            CodexProxyHandler._relay_upstream_response(
                handler,
                response,
                "xunfei",
                request_id="req_sse_error_defer",
                model="xunfei/xopglm52",
                upstream_format="responses",
                inbound_format="responses",
                caller_stream=True,
                defer_stream_errors=True,
            )

        self.assertEqual(handler.wfile.writes, [])
        self.assertEqual(
            codex_proxy._upstream_failure_class(raised.exception),
            codex_proxy.RETRY_FAILURE_PROVIDER_OVERLOADED,
        )

    def test_responses_sse_incomplete_custom_tool_input_defers_without_body(self):
        handler = FakeHandler()
        response = FakeSseResponse(
            [
                b'data: {"type":"response.created","response":{"id":"resp_patch","status":"in_progress","output":[]}}\n\n',
                (
                    b'data: {"type":"response.output_item.added","output_index":0,'
                    b'"item":{"id":"ctc_patch","type":"custom_tool_call","status":"in_progress",'
                    b'"call_id":"call_patch","name":"apply_patch","input":""}}\n\n'
                ),
                (
                    b'data: {"type":"response.custom_tool_call_input.delta","item_id":"ctc_patch",'
                    b'"output_index":0,"delta":"*** Begin Patch\\n"}\n\n'
                ),
                b"",
            ]
        )

        with self.assertRaises(codex_proxy.UpstreamStreamIncompleteError):
            CodexProxyHandler._relay_upstream_response(
                handler,
                response,
                "ollama_cloud",
                request_id="req_incomplete_tool_input",
                model="ollama-cloud/glm-5.2",
                upstream_format="responses",
                inbound_format="responses",
                caller_stream=True,
                defer_stream_errors=True,
            )

        self.assertEqual(handler.wfile.writes, [])

    def test_responses_sse_reset_after_created_defers_without_body(self):
        handler = FakeHandler()
        response = FakeSseResponse(
            [
                b'data: {"type":"response.created","response":{"id":"resp_reset","status":"in_progress","output":[]}}\n\n',
                ConnectionResetError("socket reset"),
            ]
        )

        with self.assertRaises(codex_proxy.UpstreamStreamInterruptedError):
            CodexProxyHandler._relay_upstream_response(
                handler,
                response,
                "ollama_cloud",
                request_id="req_reset_after_created",
                model="ollama-cloud/glm-5.2",
                upstream_format="responses",
                inbound_format="responses",
                caller_stream=True,
                defer_stream_errors=True,
            )

        self.assertEqual(handler.wfile.writes, [])

    def test_responses_sse_reset_after_reasoning_start_defers_without_body(self):
        handler = FakeHandler()
        response = FakeSseResponse(
            [
                b'data: {"type":"response.created","response":{"id":"resp_reasoning","status":"in_progress","output":[]}}\n\n',
                (
                    b'data: {"type":"response.output_item.added","output_index":0,'
                    b'"item":{"id":"rs_1","type":"reasoning","status":"in_progress","summary":[]}}\n\n'
                ),
                ConnectionResetError("socket reset"),
            ]
        )

        with self.assertRaises(codex_proxy.UpstreamStreamInterruptedError):
            CodexProxyHandler._relay_upstream_response(
                handler,
                response,
                "ollama_cloud",
                request_id="req_reset_after_reasoning_start",
                model="ollama-cloud/glm-5.2",
                upstream_format="responses",
                inbound_format="responses",
                caller_stream=True,
                defer_stream_errors=True,
            )

        self.assertEqual(handler.wfile.writes, [])

    def test_responses_sse_error_event_final_writes_response_failed(self):
        handler = FakeHandler()
        response = FakeSseResponse(
            [
                b'event: response.created\n',
                b'data: {"type":"response.created","response":{"id":"resp_busy","status":"in_progress","output":[]}}\n',
                b"\n",
                b'event: error\n',
                b'data: {"type":"error","error":{"code":10012,"message":"The system is busy, please try again later."}}\n',
                b"\n",
                b"",
            ]
        )

        status = CodexProxyHandler._relay_upstream_response(
            handler,
            response,
            "xunfei",
            request_id="req_sse_error_final",
            model="xunfei/xopglm52",
            upstream_format="responses",
            inbound_format="responses",
            caller_stream=True,
        )

        data = b"".join(handler.wfile.writes)
        self.assertEqual(status, 502)
        self.assertIn(b"event: response.failed\n", data)
        self.assertIn(b'"type":"response.failed"', data)
        self.assertNotIn(b"event: error\n", data)
        self.assertNotIn(b"response.created", data)

    def test_responses_streaming_converts_non_sse_json_response_to_sse(self):
        handler = FakeHandler()
        body = json.dumps(
            {
                "id": "resp_json_stream",
                "object": "response",
                "status": "completed",
                "model": "xopglm52",
                "output": [
                    {
                        "id": "msg_json_stream",
                        "type": "message",
                        "status": "completed",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "json stream fallback"}],
                    }
                ],
                "usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
            }
        ).encode("utf-8")
        response = FakeResponse(body)

        status = CodexProxyHandler._relay_upstream_response(
            handler,
            response,
            "xunfei",
            request_id="req_json_stream_fallback",
            model="xunfei/xopglm52",
            upstream_format="responses",
            inbound_format="responses",
            caller_stream=True,
            mark_downstream_sse_started=lambda: None,
        )

        self.assertEqual(status, 200)
        headers = dict(handler.headers)
        self.assertIn("text/event-stream", headers["Content-Type"])
        data = b"".join(handler.wfile.writes)
        self.assertIn(b"event: response.output_text.delta\n", data)
        self.assertIn(b"json stream fallback", data)
        self.assertIn(b"event: response.completed\n", data)
        self.assertNotEqual(data[:1], b"{")

    def test_compact_non_sse_empty_chat_response_becomes_retryable_error(self):
        handler = FakeHandler()
        body = json.dumps(
            {
                "id": "resp_empty",
                "object": "response",
                "status": "completed",
                "model": "glm-5.2",
                "output": [],
            }
        ).encode("utf-8")
        response = FakeResponse(body)

        with self.assertRaises(codex_proxy.CompactEmptyResponseError):
            CodexProxyHandler._relay_upstream_response(
                handler,
                response,
                "ollama_cloud",
                upstream_format="responses",
                inbound_format="chat_completions",
                caller_stream=False,
                request_kind=RETRY_REQUEST_COMPACT,
            )

        self.assertIsNone(handler.status)
        self.assertEqual(handler.wfile.writes, [])

    def test_non_compact_empty_assistant_response_logs_telemetry_but_stays_successful(self):
        handler = FakeHandler()
        body = json.dumps(
            {
                "id": "resp_empty",
                "object": "response",
                "status": "completed",
                "model": "gpt-5.5",
                "output": [],
            }
        ).encode("utf-8")
        response = FakeResponse(body)

        status = CodexProxyHandler._relay_upstream_response(
            handler,
            response,
            "official",
            request_id="req_empty_non_compact",
            model="openai/gpt-5.5",
            upstream_format="responses",
            inbound_format="responses",
            caller_stream=False,
            request_kind="main_generation",
        )

        self.assertEqual(status, 200)
        self.assertEqual(handler.status, 200)
        event_names = [call.args[0] for call in self.write_proxy_event.call_args_list]
        self.assertIn("empty_assistant_response", event_names)

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

    def test_official_request_normalizes_string_input_to_message_list(self):
        transformed = compatible_request_body(
            b'{"model":"gpt-5.5","input":"Say hi","stream":true}',
            {"name": "official", "auth": "codex_auth"},
        )
        payload = json.loads(transformed)

        self.assertEqual(
            payload["input"],
            [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Say hi"}],
                }
            ],
        )

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

    def test_responses_structured_provider_normalizes_openai_message_shorthand(self):
        body = json.dumps(
            {
                "model": "xopglm52",
                "input": [
                    {"role": "developer", "content": "System guidance."},
                    {"role": "user", "content": [{"type": "input_text", "text": "test"}]},
                    {"type": "function_call", "call_id": "call_tool", "name": "known_tool", "arguments": "{}"},
                    {"type": "function_call_output", "call_id": "call_tool", "output": "ok"},
                ],
            }
        ).encode("utf-8")

        transformed = compatible_request_body(
            body,
            {
                "name": "xunfei",
                "upstream_format": "responses",
                "tool_protocol": "responses_structured",
            },
            event_context={"request_id": "req"},
            inject_codex_tools=False,
        )
        payload = json.loads(transformed)

        self.assertEqual(payload["input"][0]["type"], "message")
        self.assertEqual(payload["input"][0]["role"], "developer")
        self.assertEqual(payload["input"][1]["type"], "message")
        self.assertEqual(payload["input"][1]["role"], "user")
        self.assertEqual(payload["input"][1]["content"], [{"type": "input_text", "text": "test"}])
        self.assertEqual(payload["input"][2]["type"], "function_call")
        self.assertEqual(payload["input"][3]["type"], "function_call_output")

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

    def test_chat_tools_uses_event_led_state_without_text_guidance(self):
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
        self.assertNotIn("Codex native multi_agent_v1 current state", transcript)
        self.assertEqual(event_context["subagent_open_agent_ids"], ["019f-child"])
        self.assertFalse(event_context["subagent_spawn_allowed"])

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

    def test_text_compat_routes_reviewer_issue_to_closed_implementer_for_resume(self):
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

        self.assertNotIn("multi_agent_v1__spawn_agent", tools_by_name)
        self.assertNotIn("multi_agent_v1__close_agent", tools_by_name)
        self.assertIn("multi_agent_v1__resume_agent", tools_by_name)
        self.assertIn("multi_agent_v1__send_input", tools_by_name)
        self.assertIn("status: reviewer_issue_fix_required", transcript)
        self.assertIn("send_input_target: impl-1", transcript)

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

        self.assertEqual(call["name"], "js")
        self.assertEqual(call["namespace"], "mcp__node_repl")
        self.assertEqual(json.loads(call["arguments"])["code"], "1+1")

    def test_external_response_flattens_codex_apps_namespace_call(self):
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

        self.assertEqual(call["name"], "mcp__codex_apps__local_tool_gateway___read_file")
        self.assertNotIn("namespace", call)
        self.assertEqual(json.loads(call["arguments"])["path"], "docs/plan.md")

    def test_external_response_keeps_codex_apps_flat_alias(self):
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

        self.assertEqual(call["name"], "mcp__codex_apps__local_tool_gateway___read_file")
        self.assertNotIn("namespace", call)
        self.assertEqual(json.loads(call["arguments"])["path"], "docs/plan.md")

    def test_external_sse_flattens_codex_apps_namespace_call(self):
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

        self.assertEqual(call["name"], "mcp__codex_apps__local_tool_gateway___read_file")
        self.assertNotIn("namespace", call)
        self.assertEqual(json.loads(call["arguments"])["path"], "docs/plan.md")

    def test_external_sse_keeps_codex_apps_flat_alias(self):
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

        self.assertEqual(call["name"], "mcp__codex_apps__local_tool_gateway___read_file")
        self.assertNotIn("namespace", call)
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

    def test_websocket_recorder_disabled_uses_existing_fast_reject(self):
        handler, fake = websocket_get_handler("/v1/responses")

        with patch.dict(os.environ, {"CODEX_PROXY_WEBSOCKET_RECORDER_ENABLED": "0"}, clear=False):
            CodexProxyHandler.do_GET(handler)

        self.assertEqual(fake.status, 405)
        self.assertTrue(handler.close_connection)
        event_names = [call.args[0] for call in self.write_proxy_event.call_args_list]
        self.assertEqual(event_names, ["request_start", "request_complete"])
        request_start = self.write_proxy_event.call_args_list[0].kwargs
        self.assertEqual(request_start["route_reason"], "local_responses_websocket_fast_reject")

    def test_websocket_recorder_accepts_upgrade_and_logs_only_metadata(self):
        payload = json.dumps(
            {
                "model": "openai/gpt-5.5",
                "input": [{"role": "user", "content": "SECRET_PROMPT"}],
                "tools": [{"name": "shell", "arguments": "SECRET_TOOL_ARGUMENTS"}],
            },
            separators=(",", ":"),
        ).encode("utf-8")
        handler, fake = websocket_get_handler(
            "/v1/responses?model=openai/gpt-5.5&thread_id=thread-1",
            masked_client_ws_frame(payload),
        )

        with patch.dict(
            os.environ,
            {
                "CODEX_PROXY_WEBSOCKET_RECORDER_ENABLED": "1",
                "CODEX_PROXY_WEBSOCKET_RECORDER_MAX_FRAMES": "1",
            },
            clear=False,
        ):
            CodexProxyHandler.do_GET(handler)

        self.assertEqual(fake.status, 101)
        response_headers = dict(fake.headers)
        self.assertEqual(response_headers["Upgrade"], "websocket")
        self.assertEqual(response_headers["Connection"], "Upgrade")
        self.assertEqual(response_headers["Sec-WebSocket-Accept"], "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")
        self.assertEqual(response_headers["Sec-WebSocket-Protocol"], "codex")
        self.assertTrue(any(write.startswith(b"\x88") for write in fake.wfile.writes))
        self.assertTrue(handler.close_connection)

        events = [(call.args[0], call.kwargs) for call in self.write_proxy_event.call_args_list]
        event_names = [event for event, _fields in events]
        self.assertEqual(event_names, ["websocket_probe_start", "websocket_probe_frame", "websocket_probe_complete"])
        start = events[0][1]
        frame = events[1][1]
        complete = events[2][1]
        self.assertEqual(start["path"], "/v1/responses")
        self.assertEqual(start["query_keys"], ["model", "thread_id"])
        self.assertEqual(start["selected_subprotocol"], "codex")
        self.assertIn("authorization", start["header_names"])
        self.assertEqual(frame["direction"], "client_to_proxy")
        self.assertEqual(frame["opcode"], 1)
        self.assertEqual(frame["payload_length"], len(payload))
        self.assertTrue(frame["appears_json"])
        self.assertEqual(frame["json_top_level_keys"], ["input", "model", "tools"])
        self.assertEqual(complete["frames_recorded"], 1)

        serialized_events = json.dumps([fields for _event, fields in events], sort_keys=True)
        self.assertNotIn("SECRET_PROMPT", serialized_events)
        self.assertNotIn("SECRET_TOOL_ARGUMENTS", serialized_events)
        self.assertNotIn("secret-token", serialized_events)
        self.assertNotIn("sid=secret", serialized_events)
        self.assertNotIn("dGhlIHNhbXBsZSBub25jZQ==", serialized_events)

    def test_websocket_recorder_completes_after_idle_timeout(self):
        payload = json.dumps({"model": "openai/gpt-5.5"}, separators=(",", ":")).encode("utf-8")
        handler, fake = websocket_get_handler("/v1/responses", b"")
        handler.rfile = TimeoutAfterBytes(masked_client_ws_frame(payload))

        with patch.dict(
            os.environ,
            {
                "CODEX_PROXY_WEBSOCKET_RECORDER_ENABLED": "1",
                "CODEX_PROXY_WEBSOCKET_RECORDER_MAX_FRAMES": "4",
            },
            clear=False,
        ):
            CodexProxyHandler.do_GET(handler)

        self.assertEqual(fake.status, 101)
        self.assertTrue(handler.close_connection)
        events = [(call.args[0], call.kwargs) for call in self.write_proxy_event.call_args_list]
        self.assertEqual([event for event, _fields in events], [
            "websocket_probe_start",
            "websocket_probe_frame",
            "websocket_probe_complete",
        ])
        self.assertEqual(events[-1][1]["frames_recorded"], 1)
        self.assertEqual(events[-1][1]["stop_reason"], "idle_timeout")


if __name__ == "__main__":
    unittest.main()
