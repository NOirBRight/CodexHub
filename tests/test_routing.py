import os
import gzip
import io
import json
import ssl
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, call, patch
from urllib.error import HTTPError, URLError

import codex_proxy
from subagent_state import build_subagent_state
from codex_proxy import (
    CodexProxyHandler,
    RETRY_REQUEST_COMPACT,
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
    def __init__(self, fail_on_write=None):
        self.writes = []
        self.flush_count = 0
        self.fail_on_write = fail_on_write

    def write(self, data):
        if self.fail_on_write is not None and self.fail_on_write(data, len(self.writes)):
            raise ConnectionResetError("socket reset")
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

    def _relay_official_passthrough_sse_response(self, *args, **kwargs):
        return CodexProxyHandler._relay_official_passthrough_sse_response(self, *args, **kwargs)

    def _relay_transparent_upstream_response(self, *args, **kwargs):
        return CodexProxyHandler._relay_transparent_upstream_response(self, *args, **kwargs)


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


def post_handler(path: str, body: bytes, headers: dict[str, str] | None = None):
    handler = CodexProxyHandler.__new__(CodexProxyHandler)
    handler.path = path
    merged_headers = {
        "Content-Length": str(len(body)),
        "Content-Type": "application/json",
        "X-Codex-Client-Id": "codex-app",
    }
    if headers:
        merged_headers.update(headers)
    handler.headers = merged_headers
    handler.rfile = io.BytesIO(body)
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

    def assert_no_official_passthrough_gateway_events(self):
        blocked = {
            "upstream_retry",
            "sse_retry_notice",
            "image_proxy_applied",
            "image_proxy_failed",
            "browser_context_guidance_injected",
            "compact_text_only_tools_stripped",
            "upstream_stream_incomplete_synthesized_terminal",
            "upstream_stream_incomplete",
            "upstream_stream_interrupted",
            "upstream_stream_error_event",
        }
        event_names = {call.args[0] for call in self.write_proxy_event.call_args_list if call.args}
        self.assertFalse(blocked & event_names, blocked & event_names)

    def test_gateway_auto_retry_settings_default_to_enabled_thirty_attempts(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(codex_proxy.gateway_auto_retry_enabled())
            self.assertEqual(codex_proxy.gateway_auto_retry_max_attempts(), 30)

    def test_official_http_passthrough_setting_defaults_enabled_and_env_can_disable(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(codex_proxy.gateway_official_http_passthrough_enabled())
        with patch.dict(os.environ, {"CODEX_PROXY_OFFICIAL_HTTP_PASSTHROUGH_ENABLED": "0"}, clear=True):
            self.assertFalse(codex_proxy.gateway_official_http_passthrough_enabled())

    def test_local_request_auth_defaults_open_without_gateway_key(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("codex_proxy._runtime_settings_value", return_value=None),
        ):
            self.assertTrue(codex_proxy._local_request_authorized({}, {"client_id": "unknown"}))

    def test_local_request_auth_requires_matching_gateway_key(self):
        with patch.dict(os.environ, {"CODEX_PROXY_GATEWAY_CLIENT_KEY": "local-key"}, clear=True):
            self.assertTrue(
                codex_proxy._local_request_authorized(
                    {"Authorization": "Bearer local-key"},
                    {"client_id": "unknown"},
                )
            )
            self.assertFalse(
                codex_proxy._local_request_authorized(
                    {},
                    {"client_id": "unknown"},
                )
            )
            self.assertFalse(
                codex_proxy._local_request_authorized(
                    {"Authorization": "Bearer wrong"},
                    {"client_id": "unknown"},
                )
            )

    def test_local_request_auth_rejects_spoofed_codex_app_context(self):
        with patch.dict(os.environ, {"CODEX_PROXY_GATEWAY_CLIENT_KEY": "local-key"}, clear=True):
            self.assertFalse(
                codex_proxy._local_request_authorized(
                    {"Authorization": "Bearer codex-app-token"},
                    {"client_id": "codex-app"},
                )
            )
            self.assertFalse(
                codex_proxy._local_request_authorized(
                    {},
                    {"client_id": "codex-app"},
                )
            )
            self.assertFalse(
                codex_proxy._local_request_authorized(
                    {
                        "Authorization": "Bearer wrong",
                        "User-Agent": "codex-app/0.1.3",
                    },
                    {"client_id": "codex-app"},
                )
            )
            self.assertFalse(
                codex_proxy._local_request_authorized(
                    {
                        "Authorization": "Bearer wrong",
                        "X-Codex-Client-Id": "codex-app",
                    },
                    {"client_id": "codex-app"},
                )
            )

    def test_max_request_body_bytes_defaults_and_env_override(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(codex_proxy.max_request_body_bytes(), 64 * 1024 * 1024)
        with patch.dict(os.environ, {"CODEX_PROXY_MAX_REQUEST_BODY_BYTES": "1024"}, clear=True):
            self.assertEqual(codex_proxy.max_request_body_bytes(), 1024)
        with patch.dict(os.environ, {"CODEX_PROXY_MAX_REQUEST_BODY_BYTES": "bad"}, clear=True):
            self.assertEqual(codex_proxy.max_request_body_bytes(), 64 * 1024 * 1024)

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

    def test_route_decision_codex_app_third_party_chat_upstream_uses_codex_adapter_and_wire_conversion(self):
        upstream = {"name": "volcengine", "upstream_format": "chat_completions"}
        decision = codex_proxy.route_decision_for_request(
            upstream,
            {"client_id": "codex-app"},
            inbound_format="responses",
        )

        self.assertEqual(decision.behavior_profile, codex_proxy.BEHAVIOR_CODEX_APP_EXTERNAL_ADAPTER)
        self.assertEqual(decision.codex_semantic_adapter, codex_proxy.CODEX_SEMANTIC_EXTERNAL_ADAPTER)
        self.assertEqual(decision.wire_format_adapter, codex_proxy.WIRE_RESPONSES_TO_CHAT)
        self.assertEqual(decision.retry_policy, codex_proxy.RETRY_GATEWAY_FULL)
        self.assertEqual(decision.usage_policy, codex_proxy.USAGE_SYNC_CAPTURE)
        self.assertEqual(decision.repair_policy, codex_proxy.REPAIR_CODEX_SUBAGENT)

    def test_route_decision_third_party_app_provider_same_format_is_transparent_metered(self):
        upstream = {"name": "volcengine", "upstream_format": "chat_completions"}
        decision = codex_proxy.route_decision_for_request(
            upstream,
            {"client_id": "zcode"},
            inbound_format="chat_completions",
            provider_hint="volc",
        )

        self.assertEqual(decision.behavior_profile, codex_proxy.BEHAVIOR_THIRD_PARTY_APP_TRANSPARENT_METERED)
        self.assertEqual(decision.codex_semantic_adapter, codex_proxy.CODEX_SEMANTIC_NONE)
        self.assertEqual(decision.wire_format_adapter, codex_proxy.WIRE_TRANSPARENT)
        self.assertEqual(decision.retry_policy, codex_proxy.RETRY_CONSERVATIVE_PRE_OUTPUT)
        self.assertEqual(decision.usage_policy, codex_proxy.USAGE_ASYNC_TAP)
        self.assertEqual(decision.repair_policy, codex_proxy.REPAIR_NONE)

    def test_route_decision_third_party_app_official_responses_is_transparent_metered(self):
        upstream = {"name": "official", "upstream_format": "responses"}
        decision = codex_proxy.route_decision_for_request(
            upstream,
            {"client_id": "opencode"},
            inbound_format="responses",
        )

        self.assertEqual(decision.behavior_profile, codex_proxy.BEHAVIOR_THIRD_PARTY_APP_TRANSPARENT_METERED)
        self.assertEqual(decision.wire_format_adapter, codex_proxy.WIRE_TRANSPARENT)
        self.assertEqual(decision.usage_policy, codex_proxy.USAGE_ASYNC_TAP)

    def test_route_decision_official_unknown_client_is_gateway_compat(self):
        upstream = {"name": "official", "upstream_format": "responses"}
        decision = codex_proxy.route_decision_for_request(
            upstream,
            {"client_id": "unknown"},
            inbound_format="responses",
        )

        self.assertEqual(decision.behavior_profile, codex_proxy.BEHAVIOR_OFFICIAL_GATEWAY_COMPAT)
        self.assertEqual(decision.codex_semantic_adapter, codex_proxy.CODEX_SEMANTIC_NONE)
        self.assertEqual(decision.request_kind_policy, codex_proxy.REQUEST_KIND_GATEWAY)
        self.assertEqual(decision.retry_policy, codex_proxy.RETRY_GATEWAY_FULL)
        self.assertEqual(decision.usage_policy, codex_proxy.USAGE_SYNC_CAPTURE)
        self.assertEqual(decision.repair_policy, codex_proxy.REPAIR_NONE)

    def test_route_decision_third_party_standard_unknown_client_uses_gateway_profile(self):
        upstream = {"name": "volcengine", "upstream_format": "chat_completions"}
        decision = codex_proxy.route_decision_for_request(
            upstream,
            {"client_id": "unknown"},
            inbound_format="chat_completions",
        )

        self.assertEqual(decision.behavior_profile, codex_proxy.BEHAVIOR_EXTERNAL_PROVIDER_GATEWAY)
        self.assertEqual(decision.codex_semantic_adapter, codex_proxy.CODEX_SEMANTIC_EXTERNAL_ADAPTER)
        self.assertEqual(decision.request_kind_policy, codex_proxy.REQUEST_KIND_GATEWAY)
        self.assertEqual(decision.retry_policy, codex_proxy.RETRY_GATEWAY_FULL)
        self.assertEqual(decision.usage_policy, codex_proxy.USAGE_SYNC_CAPTURE)

    def test_third_party_app_official_responses_uses_transparent_metered_runtime_path(self):
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
            "X-Codex-Client-Id": "opencode",
        }
        handler.rfile = io.BytesIO(body)
        handler.close_connection = False
        fake = FakeHandler()
        handler.send_response = fake.send_response
        handler.send_header = fake.send_header
        handler.end_headers = fake.end_headers
        handler.wfile = fake.wfile
        response_body = b'{"id":"resp_transparent","object":"response","usage":{"input_tokens":1,"output_tokens":2,"total_tokens":3}}'

        with (
            patch("codex_proxy.codex_access_token", return_value="sub-token"),
            patch("codex_proxy.codex_account_id", return_value="acct-1"),
            patch("codex_proxy.compatible_request_body", side_effect=AssertionError("codex adapter ran")),
            patch("codex_proxy._official_urlopen", return_value=FakeContextResponse(response_body)) as mock_urlopen,
        ):
            CodexProxyHandler.do_POST(handler)

        request = mock_urlopen.call_args.args[0]
        sent_payload = json.loads(request.data)
        self.assertEqual(sent_payload["model"], "gpt-5.5")
        self.assertIs(sent_payload["store"], False)
        self.assertIs(sent_payload["stream"], True)
        self.assertEqual(b"".join(handler.wfile.writes), response_body)
        request_start = next(
            call.kwargs for call in self.write_proxy_event.call_args_list if call.args and call.args[0] == "request_start"
        )
        request_complete = next(
            call.kwargs for call in self.write_proxy_event.call_args_list if call.args and call.args[0] == "request_complete"
        )
        self.assertEqual(request_start["behavior_profile"], codex_proxy.BEHAVIOR_THIRD_PARTY_APP_TRANSPARENT_METERED)
        for fields in (request_start, request_complete):
            self.assertEqual(fields["wire_format_adapter"], codex_proxy.WIRE_TRANSPARENT)
            self.assertEqual(fields["codex_semantic_adapter"], codex_proxy.CODEX_SEMANTIC_NONE)
            self.assertEqual(fields["request_kind_policy"], codex_proxy.REQUEST_KIND_TRANSPARENT)
            self.assertEqual(fields["retry_policy"], codex_proxy.RETRY_CONSERVATIVE_PRE_OUTPUT)
            self.assertEqual(fields["usage_policy"], codex_proxy.USAGE_ASYNC_TAP)
            self.assertEqual(fields["repair_policy"], codex_proxy.REPAIR_NONE)

    def test_third_party_app_official_responses_nonstream_buffers_forced_sse(self):
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
            "X-Codex-Client-Id": "opencode",
        }
        handler.rfile = io.BytesIO(body)
        handler.close_connection = False
        fake = FakeHandler()
        handler.send_response = fake.send_response
        handler.send_header = fake.send_header
        handler.end_headers = fake.end_headers
        handler.wfile = fake.wfile
        completed = {
            "type": "response.completed",
            "response": {
                "id": "resp_buffered",
                "object": "response",
                "created_at": 1783430000,
                "model": "gpt-5.5",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "hello", "annotations": []}],
                    }
                ],
                "usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
            },
        }

        with (
            patch("codex_proxy.codex_access_token", return_value="sub-token"),
            patch("codex_proxy.codex_account_id", return_value="acct-1"),
            patch("codex_proxy.compatible_request_body", side_effect=AssertionError("codex adapter ran")),
            patch(
                "codex_proxy._official_urlopen",
                return_value=FakeSseResponse([f"data: {json.dumps(completed)}\n\n".encode("utf-8"), b""]),
            ) as mock_urlopen,
        ):
            CodexProxyHandler.do_POST(handler)

        sent_payload = json.loads(mock_urlopen.call_args.args[0].data)
        self.assertIs(sent_payload["stream"], True)
        written = b"".join(handler.wfile.writes)
        self.assertNotIn(b"data:", written)
        result = json.loads(written)
        self.assertEqual(result["id"], "resp_buffered")
        self.assertEqual(result["object"], "response")
        self.assertEqual(result["created_at"], 1783430000)
        self.assertEqual(result["output"][0]["content"][0]["text"], "hello")
        self.assertEqual(dict(fake.headers).get("Content-Type"), "application/json")

    def test_third_party_app_official_chat_completions_uses_lightweight_responses_fallback(self):
        body = json.dumps(
            {
                "model": "openai/gpt-5.5",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
            }
        ).encode("utf-8")
        handler = CodexProxyHandler.__new__(CodexProxyHandler)
        handler.path = "/v1/chat/completions"
        handler.headers = {
            "Content-Length": str(len(body)),
            "Content-Type": "application/json",
            "X-Codex-Client-Id": "opencode",
        }
        handler.rfile = io.BytesIO(body)
        handler.close_connection = False
        fake = FakeHandler()
        handler.send_response = fake.send_response
        handler.send_header = fake.send_header
        handler.end_headers = fake.end_headers
        handler.wfile = fake.wfile
        response_body = json.dumps(
            {
                "id": "resp_official_chat_fallback",
                "object": "response",
                "status": "completed",
                "model": "openai/gpt-5.5",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "hello", "annotations": []}],
                    }
                ],
                "usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
            }
        ).encode("utf-8")

        with (
            patch("codex_proxy.codex_access_token", return_value="sub-token"),
            patch("codex_proxy.codex_account_id", return_value="acct-1"),
            patch("codex_proxy.compatible_request_body", side_effect=AssertionError("codex adapter ran")),
            patch("codex_proxy._official_urlopen", return_value=FakeContextResponse(response_body)) as mock_urlopen,
        ):
            CodexProxyHandler.do_POST(handler)

        request = mock_urlopen.call_args.args[0]
        self.assertTrue(request.full_url.endswith("/responses"))
        sent_payload = json.loads(request.data)
        self.assertEqual(sent_payload["model"], "gpt-5.5")
        self.assertIn("input", sent_payload)
        self.assertNotIn("messages", sent_payload)
        self.assertIs(sent_payload["store"], False)
        self.assertIs(sent_payload["stream"], True)
        result = json.loads(b"".join(handler.wfile.writes))
        self.assertEqual(result["object"], "chat.completion")
        self.assertEqual(result["choices"][0]["message"]["content"], "hello")
        request_start = next(
            call.kwargs for call in self.write_proxy_event.call_args_list if call.args and call.args[0] == "request_start"
        )
        self.assertEqual(request_start["behavior_profile"], codex_proxy.BEHAVIOR_THIRD_PARTY_APP_TRANSPARENT_METERED)
        self.assertEqual(request_start["request_kind"], codex_proxy.RETRY_REQUEST_MAIN_GENERATION)

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
            codex_proxy.BEHAVIOR_CODEX_APP_EXTERNAL_ADAPTER,
        )

    def test_unauthorized_local_client_returns_401_and_logs_event(self):
        body = json.dumps({"model": "openai/gpt-5.5", "input": []}).encode("utf-8")
        handler, fake = post_handler(
            "/v1/responses",
            body,
            headers={"X-Codex-Client-Id": "unknown"},
        )

        with patch.dict(os.environ, {"CODEX_PROXY_GATEWAY_CLIENT_KEY": "local-key"}, clear=False):
            CodexProxyHandler._proxy_post_request(handler, inbound_format="responses")

        self.assertEqual(fake.status, 401)
        payload = json.loads(fake.wfile.writes[-1].decode("utf-8"))
        self.assertEqual(payload["codexhub_error"]["code"], "gateway.auth")
        self.assertEqual(payload["codexhub_error"]["source"], "gateway")
        self.assertFalse(payload["codexhub_error"]["retryable"])
        self.assertTrue(handler.close_connection)
        event = self.write_proxy_event.call_args_list[-1]
        self.assertEqual(event.args[0], "request_error")
        self.assertEqual(event.kwargs["route_reason"], "local_client_auth")
        self.assertEqual(event.kwargs["error"], "UnauthorizedLocalClient")

    def test_provider_scoped_responses_without_model_returns_current_error(self):
        body = json.dumps({"input": [{"role": "user", "content": "hi"}]}).encode("utf-8")
        handler, fake = post_handler("/v1/providers/volc/responses", body)

        CodexProxyHandler._proxy_post_request(handler, inbound_format="responses", provider_hint="volc")

        self.assertEqual(fake.status, 400)
        payload = json.loads(fake.wfile.writes[-1].decode("utf-8"))
        self.assertIn("model is required for provider path: volc", payload.get("error", ""))

    def test_third_party_ultra_reasoning_effort_returns_openai_compatible_400(self):
        body = json.dumps(
            {
                "model": "volc/glm-5.2",
                "input": [{"role": "user", "content": "hi"}],
                "reasoning": {"effort": "ultra"},
                "stream": False,
            }
        ).encode("utf-8")
        handler, fake = post_handler("/v1/responses", body)

        with patch(
            "codex_proxy._open_upstream_response",
            return_value=FakeContextResponse(b'{"id":"resp-third-party","output":[]}'),
        ) as open_upstream:
            CodexProxyHandler._proxy_post_request(handler, inbound_format="responses")

        self.assertEqual(fake.status, 400)
        open_upstream.assert_not_called()
        payload = json.loads(fake.wfile.writes[-1].decode("utf-8"))
        self.assertIn("reasoning effort 'ultra'", payload["error"])
        self.assertEqual(payload["detail"], payload["error"])
        self.assertEqual(payload["codexhub_error"]["code"], "provider.request")
        self.assertEqual(payload["codexhub_error"]["details"]["type"], "invalid_request_error")
        self.assertEqual(payload["codexhub_error"]["source"], "volcengine")
        self.assertFalse(payload["codexhub_error"]["retryable"])

    def test_third_party_top_level_ultra_reasoning_effort_returns_400(self):
        body = json.dumps(
            {
                "model": "volc/glm-5.2",
                "messages": [{"role": "user", "content": "hi"}],
                "reasoning_effort": "ultra",
                "stream": False,
            }
        ).encode("utf-8")
        handler, fake = post_handler("/v1/chat/completions", body)

        with patch(
            "codex_proxy._open_upstream_response",
            return_value=FakeContextResponse(
                b'{"id":"chatcmpl-third-party","choices":[{"index":0,"message":{"role":"assistant","content":"ok"}}]}'
            ),
        ) as open_upstream:
            CodexProxyHandler._proxy_post_request(handler, inbound_format="chat_completions")

        self.assertEqual(fake.status, 400)
        open_upstream.assert_not_called()
        payload = json.loads(fake.wfile.writes[-1].decode("utf-8"))
        self.assertEqual(payload["error"]["type"], "invalid_request_error")
        self.assertEqual(payload["codexhub_error"]["code"], "provider.request")

    def test_third_party_string_ultra_reasoning_effort_returns_400(self):
        body = json.dumps(
            {
                "model": "volc/glm-5.2",
                "input": [{"role": "user", "content": "hi"}],
                "reasoning": "ultra",
                "stream": False,
            }
        ).encode("utf-8")
        handler, fake = post_handler("/v1/responses", body)

        with patch(
            "codex_proxy._open_upstream_response",
            return_value=FakeContextResponse(b'{"id":"resp-third-party","output":[]}'),
        ) as open_upstream:
            CodexProxyHandler._proxy_post_request(handler, inbound_format="responses")

        self.assertEqual(fake.status, 400)
        open_upstream.assert_not_called()
        payload = json.loads(fake.wfile.writes[-1].decode("utf-8"))
        self.assertEqual(payload["codexhub_error"]["code"], "provider.request")

    def test_supported_string_reasoning_effort_reaches_third_party_upstream(self):
        body = json.dumps(
            {
                "model": "volc/glm-5.2",
                "input": [{"role": "user", "content": "hi"}],
                "reasoning": "high",
                "stream": False,
            }
        ).encode("utf-8")
        handler, fake = post_handler("/v1/responses", body)

        with patch(
            "codex_proxy._open_upstream_response",
            return_value=FakeContextResponse(b'{"id":"resp-third-party","output":[]}'),
        ) as open_upstream:
            CodexProxyHandler._proxy_post_request(handler, inbound_format="responses")

        self.assertEqual(fake.status, 200)
        open_upstream.assert_called_once()

    def test_sol_ultra_reasoning_effort_reaches_official_upstream(self):
        body = json.dumps(
            {
                "model": "openai/gpt-5.6-sol",
                "input": [{"role": "user", "content": "hi"}],
                "reasoning": {"effort": "ultra"},
                "stream": False,
            }
        ).encode("utf-8")
        handler, fake = post_handler("/v1/responses", body)

        with (
            patch("codex_proxy.choose_upstream", return_value=official_upstream()),
            patch("codex_proxy.codex_access_token", return_value="sub-token"),
            patch("codex_proxy.codex_account_id", return_value="acct-1"),
            patch(
                "codex_proxy._open_upstream_response",
                return_value=FakeContextResponse(b'{"id":"resp-official","output":[]}'),
            ) as open_upstream,
        ):
            CodexProxyHandler._proxy_post_request(handler, inbound_format="responses")

        self.assertEqual(fake.status, 200)
        open_upstream.assert_called_once()
        forwarded = json.loads(open_upstream.call_args.args[0].data.decode("utf-8"))
        self.assertEqual(forwarded["reasoning"]["effort"], "ultra")

    def test_terra_string_ultra_reasoning_effort_reaches_official_upstream(self):
        body = json.dumps(
            {
                "model": "openai/gpt-5.6-terra",
                "input": [{"role": "user", "content": "hi"}],
                "reasoning": "ultra",
                "stream": False,
            }
        ).encode("utf-8")
        handler, fake = post_handler("/v1/responses", body)

        with (
            patch("codex_proxy.choose_upstream", return_value=official_upstream()),
            patch("codex_proxy.codex_access_token", return_value="sub-token"),
            patch("codex_proxy.codex_account_id", return_value="acct-1"),
            patch(
                "codex_proxy._open_upstream_response",
                return_value=FakeContextResponse(b'{"id":"resp-official","output":[]}'),
            ) as open_upstream,
        ):
            CodexProxyHandler._proxy_post_request(handler, inbound_format="responses")

        self.assertEqual(fake.status, 200)
        forwarded = json.loads(open_upstream.call_args.args[0].data.decode("utf-8"))
        self.assertEqual(forwarded["reasoning"], "ultra")

    def test_luna_ultra_reasoning_effort_is_rejected_before_official_upstream(self):
        body = json.dumps(
            {
                "model": "openai/gpt-5.6-luna",
                "input": [{"role": "user", "content": "hi"}],
                "reasoning": {"effort": "ultra"},
                "stream": False,
            }
        ).encode("utf-8")
        handler, fake = post_handler("/v1/responses", body)

        with (
            patch("codex_proxy.choose_upstream", return_value=official_upstream()),
            patch("codex_proxy.codex_access_token", return_value="sub-token"),
            patch("codex_proxy.codex_account_id", return_value="acct-1"),
            patch("codex_proxy._open_upstream_response") as open_upstream,
        ):
            CodexProxyHandler._proxy_post_request(handler, inbound_format="responses")

        self.assertEqual(fake.status, 400)
        open_upstream.assert_not_called()
        payload = json.loads(fake.wfile.writes[-1].decode("utf-8"))
        self.assertIn("supported only for gpt-5.6-sol and gpt-5.6-terra", payload["error"])

    def test_compressed_responses_request_extracts_model_after_decode(self):
        original = json.dumps(
            {
                "model": "openai/gpt-5.5",
                "input": [{"type": "message", "role": "user", "content": "hi"}],
                "stream": False,
            }
        ).encode("utf-8")
        compressed = gzip.compress(original)
        handler, _fake = post_handler(
            "/v1/responses",
            compressed,
            headers={"Content-Encoding": "gzip"},
        )

        with (
            patch("codex_proxy.codex_access_token", return_value="sub-token"),
            patch("codex_proxy.codex_account_id", return_value="acct-1"),
            patch("codex_proxy._official_urlopen", return_value=FakeContextResponse(b'{"id":"resp","output":[]}')) as mock_urlopen,
        ):
            CodexProxyHandler._proxy_post_request(handler, inbound_format="responses")

        request = mock_urlopen.call_args.args[0]
        self.assertEqual(json.loads(request.data.decode("utf-8"))["model"], "gpt-5.5")

    def test_raw_provider_probe_sets_context_on_request_start(self):
        body = json.dumps(
            {
                "model": "volc/glm-5.2",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
            }
        ).encode("utf-8")
        handler, _fake = post_handler(
            "/v1/providers/volc/chat/completions?raw_provider_probe=1",
            body,
        )

        with patch("codex_proxy.urlopen", return_value=FakeContextResponse(b'{"id":"chatcmpl","choices":[]}')):
            CodexProxyHandler._proxy_post_request(handler, inbound_format="chat_completions", provider_hint="volc")

        request_start = [
            call.kwargs
            for call in self.write_proxy_event.call_args_list
            if call.args and call.args[0] == "request_start"
        ][0]
        self.assertTrue(request_start["raw_provider_probe"])

    def test_request_body_over_limit_returns_413_and_logs_limit(self):
        body = json.dumps({"model": "openai/gpt-5.5", "input": "abcdef"}).encode("utf-8")
        handler, fake = post_handler("/v1/responses", body)

        with patch.dict(os.environ, {"CODEX_PROXY_MAX_REQUEST_BODY_BYTES": "8"}, clear=False):
            CodexProxyHandler._proxy_post_request(handler, inbound_format="responses")

        self.assertEqual(fake.status, 413)
        self.assertTrue(handler.close_connection)
        event = self.write_proxy_event.call_args_list[-1]
        self.assertEqual(event.args[0], "request_error")
        self.assertEqual(event.kwargs["route_reason"], "request_body_limit")
        self.assertEqual(event.kwargs["status"], 413)

    def test_official_http_passthrough_uses_bounded_open_attempts_and_defers_empty_stream_errors(self):
        body = json.dumps(
            {
                "model": "gpt-5.5-fast",
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

        self.assertEqual(open_response.call_args.kwargs.get("max_attempts"), codex_proxy.official_upstream_open_attempts())
        self.assertFalse(open_response.call_args.kwargs.get("retry_http_errors"))
        self.assertTrue(relayed[0]["defer_stream_errors"])
        self.assert_no_official_passthrough_gateway_events()

    def test_official_http_passthrough_retries_incomplete_read_before_first_sse_byte(self):
        body = json.dumps(
            {
                "model": "gpt-5.5-fast",
                "input": [{"type": "message", "role": "user", "content": "hi"}],
                "stream": True,
            }
        ).encode("utf-8")
        handler, fake = post_handler("/v1/responses", body)
        interrupted = FakeSseResponse([codex_proxy.IncompleteRead(b"")])
        success = FakeSseResponse(
            [
                b'data: {"type":"response.created","response":{"id":"resp_retry"}}\n\n',
                b'data: {"type":"response.output_text.delta","delta":"OK"}\n\n',
                b'data: {"type":"response.completed","response":{"id":"resp_retry","status":"completed"}}\n\n',
                b"",
            ]
        )

        with (
            patch("codex_proxy.codex_access_token", return_value="sub-token"),
            patch("codex_proxy.codex_account_id", return_value="acct-1"),
            patch("codex_proxy._open_upstream_response", side_effect=[interrupted, success]) as open_response,
            patch("codex_proxy.time.sleep") as mock_sleep,
        ):
            CodexProxyHandler.do_POST(handler)

        self.assertEqual(open_response.call_count, 2)
        mock_sleep.assert_called_once()
        self.assertEqual(fake.status, 200)
        downstream = b"".join(fake.wfile.writes)
        self.assertIn(b"response.completed", downstream)
        self.assertNotIn(b"response.failed", downstream)
        retry_events = [
            call.kwargs
            for call in self.write_proxy_event.call_args_list
            if call.args and call.args[0] == "upstream_retry"
        ]
        self.assertEqual(len(retry_events), 1)
        self.assertEqual(retry_events[0]["failure_phase"], "stream_body")

    def test_official_http_passthrough_never_retries_after_first_sse_byte(self):
        body = json.dumps(
            {
                "model": "gpt-5.5-fast",
                "input": [{"type": "message", "role": "user", "content": "hi"}],
                "stream": True,
            }
        ).encode("utf-8")
        handler, fake = post_handler("/v1/responses", body)
        interrupted = FakeSseResponse(
            [
                b'data: {"type":"response.created","response":{"id":"resp_partial"}}\n\n',
                codex_proxy.IncompleteRead(b""),
            ]
        )
        unused_success = FakeSseResponse(
            [
                b'data: {"type":"response.completed","response":{"id":"resp_unused","status":"completed"}}\n\n',
                b"",
            ]
        )

        with (
            patch("codex_proxy.codex_access_token", return_value="sub-token"),
            patch("codex_proxy.codex_account_id", return_value="acct-1"),
            patch("codex_proxy._open_upstream_response", side_effect=[interrupted, unused_success]) as open_response,
            patch("codex_proxy.time.sleep") as mock_sleep,
        ):
            CodexProxyHandler.do_POST(handler)

        self.assertEqual(open_response.call_count, 1)
        mock_sleep.assert_not_called()
        downstream = b"".join(fake.wfile.writes)
        self.assertIn(b"response.created", downstream)
        self.assertIn(b"response.failed", downstream)
        self.assertFalse(
            any(
                call.args and call.args[0] == "upstream_retry"
                for call in self.write_proxy_event.call_args_list
            )
        )

    def test_official_http_passthrough_converts_compaction_input_before_upstream(self):
        input_items = [
            {"type": "message", "role": "user", "content": f"message {index}"}
            for index in range(68)
        ]
        input_items.append({"type": "compaction", "summary": "release thread summary"})
        body = json.dumps(
            {
                "model": "openai/gpt-5.5",
                "input": input_items,
                "stream": True,
            }
        ).encode("utf-8")
        handler, _fake = post_handler("/v1/responses", body)
        handler._relay_upstream_response = lambda response, upstream_name, **kwargs: 200

        with (
            patch("codex_proxy.codex_access_token", return_value="sub-token"),
            patch("codex_proxy.codex_account_id", return_value="acct-1"),
            patch("codex_proxy._open_upstream_response", return_value=FakeContextResponse(b'{"id":"resp_official","output":[]}')) as open_response,
        ):
            CodexProxyHandler.do_POST(handler)

        request = open_response.call_args.args[0]
        sent_payload = json.loads(request.data)
        sent_raw = request.data.decode("utf-8")

        self.assertEqual(sent_payload["input"][68]["type"], "message")
        self.assertEqual(sent_payload["input"][68]["role"], "developer")
        self.assertIn("release thread summary", sent_payload["input"][68]["content"])
        self.assertNotIn('"type":"compaction"', sent_raw)

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
        self.assert_no_official_passthrough_gateway_events()

    def test_official_http_passthrough_skips_repeated_body_parsing(self):
        body = json.dumps(
            {
                "model": "gpt-5.5-fast",
                "input": [{"type": "message", "role": "user", "content": "hi"}],
                "stream": True,
                "store": False,
                "prompt_cache_key": "cache-key-1",
            }
        ).encode("utf-8")
        handler = CodexProxyHandler.__new__(CodexProxyHandler)
        handler.path = "/v1/responses"
        handler.headers = {
            "Content-Length": str(len(body)),
            "Content-Type": "application/json",
            "X-Codex-Client-Id": "codex-app",
            "Accept": "text/event-stream",
            "Session-id": "session-1",
            "Thread-id": "thread-1",
            "X-codex-window-id": "session-1:1",
            "X-client-request-id": "request-1",
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

        real_json_loads = codex_proxy.json.loads
        parse_count = 0

        def counting_json_loads(value, *args, **kwargs):
            nonlocal parse_count
            if isinstance(value, str) and '"input"' in value:
                parse_count += 1
            return real_json_loads(value, *args, **kwargs)

        with (
            patch("codex_proxy.codex_access_token", return_value="sub-token"),
            patch("codex_proxy.codex_account_id", return_value="acct-1"),
            patch("codex_proxy.json.loads", side_effect=counting_json_loads),
            patch(
                "codex_proxy._open_upstream_response",
                return_value=FakeContextResponse(b'{"id":"resp_official","output":[]}'),
            ),
        ):
            CodexProxyHandler.do_POST(handler)

        self.assertLessEqual(parse_count, 1)
        self.assertEqual(len(relayed), 1)
        self.assert_no_official_passthrough_gateway_events()

    def test_official_http_passthrough_keeps_cache_key_but_skips_body_hmac(self):
        body = json.dumps(
            {
                "model": "gpt-5.5-fast",
                "input": [{"type": "message", "role": "user", "content": "hi"}],
                "stream": True,
                "store": False,
                "prompt_cache_key": "cache-key-1",
            }
        ).encode("utf-8")
        handler = CodexProxyHandler.__new__(CodexProxyHandler)
        handler.path = "/v1/responses"
        handler.headers = {
            "Content-Length": str(len(body)),
            "Content-Type": "application/json",
            "X-Codex-Client-Id": "codex-app",
            "Accept": "text/event-stream",
            "Session-id": "session-1",
            "Thread-id": "thread-1",
            "X-codex-window-id": "session-1:1",
            "X-client-request-id": "request-1",
        }
        handler.rfile = io.BytesIO(body)
        handler.close_connection = False
        fake = FakeHandler()
        handler.send_response = fake.send_response
        handler.send_header = fake.send_header
        handler.end_headers = fake.end_headers
        handler.wfile = fake.wfile
        handler._relay_upstream_response = lambda response, upstream_name, **kwargs: 200

        with (
            patch("codex_proxy.codex_access_token", return_value="sub-token"),
            patch("codex_proxy.codex_account_id", return_value="acct-1"),
            patch(
                "codex_proxy._open_upstream_response",
                return_value=FakeContextResponse(b'{"id":"resp_official","output":[]}'),
            ),
        ):
            CodexProxyHandler.do_POST(handler)

        request_start = next(
            call.kwargs for call in self.write_proxy_event.call_args_list if call.args and call.args[0] == "request_start"
        )
        self.assertIn("prompt_cache_key_hash", request_start)
        self.assertTrue(request_start["request_body_hmac_skipped"])
        self.assertNotIn("request_body_hmac", request_start)
        self.assert_no_official_passthrough_gateway_events()

    def test_official_http_passthrough_preserves_codex_app_headers_without_synthetic_identity_defaults(self):
        incoming = {
            "Authorization": "Bearer old-token",
            "Chatgpt-account-id": "acct-from-app",
            "Accept": "text/event-stream",
            "Originator": "codex_app",
            "User-Agent": "Codex Desktop/0.142.4",
            "Session-id": "session-from-app",
            "Thread-id": "thread-from-app",
            "X-codex-window-id": "window-from-app",
            "X-client-request-id": "request-from-app",
            "X-OpenAI-Internal-Codex-Responses-Lite": "true",
            "Connection": "keep-alive",
        }
        upstream = {"name": "official", "auth": "codex_auth", "upstream_model": "gpt-5.6-sol"}

        with patch("codex_proxy.codex_access_token", return_value="new-token"):
            headers = upstream_headers(
                incoming,
                upstream,
                behavior_profile=codex_proxy.BEHAVIOR_OFFICIAL_CODEX_APP_HTTP_PASSTHROUGH,
                model_id="openai/gpt-5.6-sol",
            )

        self.assertEqual(headers["Authorization"], "Bearer new-token")
        self.assertEqual(headers["Chatgpt-account-id"], "acct-from-app")
        self.assertEqual(headers["Accept"], "text/event-stream")
        self.assertEqual(headers["Originator"], "codex_app")
        self.assertEqual(headers["User-Agent"], "Codex Desktop/0.142.4")
        self.assertEqual(headers["Session-id"], "session-from-app")
        self.assertEqual(headers["Thread-id"], "thread-from-app")
        self.assertEqual(headers["X-codex-window-id"], "window-from-app")
        self.assertEqual(headers["X-client-request-id"], "request-from-app")
        self.assertEqual(headers["X-OpenAI-Internal-Codex-Responses-Lite"], "true")
        self.assertNotIn("Connection", headers)

    def test_official_unsupported_model_requests_drop_responses_lite_header(self):
        for model_id, prompt in (
            ("gpt-5.4-mini", "generate a short task title"),
            ("gpt-5.4", "generate personalized task suggestions"),
        ):
            with self.subTest(model_id=model_id):
                body = json.dumps(
                    {
                        "model": model_id,
                        "input": [{"type": "message", "role": "user", "content": prompt}],
                        "stream": True,
                        "store": False,
                    }
                ).encode("utf-8")
                handler = CodexProxyHandler.__new__(CodexProxyHandler)
                handler.path = "/v1/responses"
                handler.headers = {
                    "Content-Length": str(len(body)),
                    "Content-Type": "application/json",
                    "Accept": "text/event-stream",
                    "User-Agent": "Codex Desktop/0.142.4",
                    "Session-id": "auxiliary-session",
                    "Thread-id": "auxiliary-thread",
                    "X-codex-window-id": "auxiliary-window:0",
                    "X-client-request-id": "auxiliary-request",
                    "X-OpenAI-Internal-Codex-Responses-Lite": "true",
                }
                handler.rfile = io.BytesIO(body)
                handler.close_connection = False
                fake = FakeHandler()
                handler.send_response = fake.send_response
                handler.send_header = fake.send_header
                handler.end_headers = fake.end_headers
                handler.wfile = fake.wfile
                handler._relay_upstream_response = lambda response, upstream_name, **kwargs: 200
                captured_requests = []

                def open_upstream(request, **_kwargs):
                    captured_requests.append(request)
                    return FakeContextResponse(b'{"id":"resp_auxiliary","output":[]}')

                with (
                    patch("codex_proxy.codex_access_token", return_value="sub-token"),
                    patch("codex_proxy.codex_account_id", return_value="acct-1"),
                    patch("codex_proxy._open_upstream_response", side_effect=open_upstream),
                ):
                    CodexProxyHandler.do_POST(handler)

                self.assertEqual(len(captured_requests), 1)
                forwarded_header_names = {key.lower() for key, _value in captured_requests[0].header_items()}
                self.assertNotIn("x-openai-internal-codex-responses-lite", forwarded_header_names)

    def test_official_http_passthrough_does_not_generate_missing_identity_headers(self):
        incoming = {
            "Content-Type": "application/json",
            "X-Codex-Client-Id": "codex-app",
        }
        upstream = {"name": "official", "auth": "codex_auth"}

        with (
            patch("codex_proxy.codex_access_token", return_value="new-token"),
            patch("codex_proxy.codex_account_id", return_value="acct-from-auth"),
        ):
            headers = upstream_headers(
                incoming,
                upstream,
                behavior_profile=codex_proxy.BEHAVIOR_OFFICIAL_CODEX_APP_HTTP_PASSTHROUGH,
            )

        self.assertEqual(headers["Authorization"], "Bearer new-token")
        self.assertEqual(headers["Chatgpt-account-id"], "acct-from-auth")
        self.assertNotIn("Originator", headers)
        self.assertNotIn("User-Agent", headers)
        self.assertNotIn("Session-id", headers)
        self.assertNotIn("Thread-id", headers)
        self.assertNotIn("X-codex-window-id", headers)
        self.assertNotIn("X-client-request-id", headers)

    def test_official_http_passthrough_sse_relay_does_not_parse_or_rewrite_events(self):
        fake = FakeHandler()
        response = FakeSseResponse(
            [
                b'data: {"type":"response.created","response":{"id":"resp_1"}}\n\n',
                b'data: {"type":"response.output_text.delta","delta":"hi"}\n\n',
                b'data: {"type":"response.completed","response":{"id":"resp_1","status":"completed"}}\n\n',
                b"",
            ]
        )
        usage_offers = []

        def record_usage_offer(context, line):
            self.assertIn(line, fake.wfile.writes)
            usage_offers.append((context, line))

        with (
            patch("codex_proxy._parse_sse_json_payload", side_effect=AssertionError("official relay parsed SSE")),
            patch("codex_proxy.compatible_sse_line", side_effect=AssertionError("official relay rewrote SSE")),
            patch("codex_proxy._offer_official_passthrough_usage_line", side_effect=record_usage_offer, create=True),
        ):
            status = CodexProxyHandler._relay_upstream_response(
                fake,
                response,
                "official",
                request_id="req-1",
                model="gpt-5.5",
                upstream_format="responses",
                inbound_format="responses",
                caller_stream=True,
                behavior_profile=codex_proxy.BEHAVIOR_OFFICIAL_CODEX_APP_HTTP_PASSTHROUGH,
                usage_capture={},
            )

        self.assertEqual(status, 200)
        body = b"".join(fake.wfile.writes)
        self.assertIn(b"response.created", body)
        self.assertIn(b"response.output_text.delta", body)
        self.assertIn(b"response.completed", body)
        self.assertNotIn(b"response.failed", body)
        self.assertNotIn(b"codexhub.keepalive", body)
        self.assertEqual(len(usage_offers), 3)
        self.assertEqual(usage_offers[0][0]["request_id"], "req-1")

    def test_official_passthrough_records_sse_semantic_summary(self):
        fake = FakeHandler()
        usage_capture = {}

        status = CodexProxyHandler._relay_upstream_response(
            fake,
            FakeSseResponse(
                [
                    b"event: response.created\n",
                    b'data: {"type":"response.created","response":{"id":"resp_1"}}\n',
                    b"\n",
                    b"event: response.output_text.delta\n",
                    b'data: {"type":"response.output_text.delta","delta":"hi"}\n\n',
                    b"event: response.completed\n",
                    b'data: {"type":"response.completed",\n',
                    b'data: "response":{"id":"resp_1","status":"completed"}}\n',
                    b"\n",
                    b"",
                ]
            ),
            "official",
            request_id="req-official-semantics",
            model="gpt-5.5",
            upstream_format="responses",
            inbound_format="responses",
            caller_stream=True,
            behavior_profile=codex_proxy.BEHAVIOR_OFFICIAL_CODEX_APP_HTTP_PASSTHROUGH,
            usage_capture=usage_capture,
        )

        self.assertEqual(status, 200)
        self.assertEqual(usage_capture["sse_events_streamed"], 3)
        self.assertEqual(usage_capture["sse_json_events_streamed"], 3)
        self.assertTrue(usage_capture["sse_terminal_event_seen"])
        self.assertTrue(usage_capture["sse_completed_event_seen"])
        self.assertTrue(usage_capture["sse_downstream_output_seen"])
        self.assertFalse(usage_capture["sse_done_sentinel_seen"])
        self.assertEqual(usage_capture["sse_last_event_type"], "response.completed")
        self.assertEqual(
            usage_capture["sse_event_types"],
            [
                "response.completed",
                "response.created",
                "response.output_text.delta",
            ],
        )

    def test_official_http_passthrough_raw_relay_even_when_caller_stream_false(self):
        fake = FakeHandler()
        response = FakeSseResponse(
            [
                b'data: {"type":"response.created","response":{"id":"resp_1"}}\n\n',
                b'data: {"type":"response.output_text.delta","delta":"hi"}\n\n',
                b'data: {"type":"response.completed","response":{"id":"resp_1","status":"completed"}}\n\n',
                b"",
            ]
        )

        status = CodexProxyHandler._relay_upstream_response(
            fake,
            response,
            "official",
            request_id="req-1",
            model="gpt-5.5",
            upstream_format="responses",
            inbound_format="responses",
            caller_stream=False,
            behavior_profile=codex_proxy.BEHAVIOR_OFFICIAL_CODEX_APP_HTTP_PASSTHROUGH,
            usage_capture={},
        )

        body = b"".join(fake.wfile.writes)
        self.assertEqual(status, 200)
        self.assertIn(b'data: {"type":"response.created"', body)
        self.assertIn(b'data: {"type":"response.output_text.delta"', body)
        self.assertNotIn(b'"output"', body)

    def test_official_http_passthrough_http_error_event_stream_relay_is_raw(self):
        body = json.dumps(
            {
                "model": "gpt-5.5-fast",
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
        error_line = b'data: {"type":"response.failed","response":{"id":"resp_error","status":"failed"}}\n\n'
        error = HTTPError(
            "https://chatgpt.com/backend-api/codex/responses",
            503,
            "Service Unavailable",
            {"Content-Type": "text/event-stream; charset=utf-8"},
            io.BytesIO(error_line),
        )

        with (
            patch("codex_proxy.codex_access_token", return_value="sub-token"),
            patch("codex_proxy.codex_account_id", return_value="acct-1"),
            patch(
                "codex_proxy._normalize_third_party_tool_call",
                side_effect=AssertionError("official passthrough HTTPError parsed SSE"),
            ),
            patch("codex_proxy._official_urlopen", side_effect=error),
        ):
            CodexProxyHandler.do_POST(handler)

        written = b"".join(handler.wfile.writes)
        self.assertEqual(fake.status, 503)
        self.assertEqual(written, error_line)
        self.assertNotIn(b"event: response.failed", written)
        self.assert_no_official_passthrough_gateway_events()

    def test_official_passthrough_usage_worker_emits_usage_observed(self):
        context = {
            "request_id": "req-async-usage",
            "model": "openai/gpt-5.5",
            "upstream": "official",
            "upstream_format": "responses",
            "inbound_format": "responses",
            "client_id": "zcode",
            "client_inference_source": "header",
        }
        line = b'data: {"type":"response.completed","response":{"usage":{"input_tokens":2,"output_tokens":3,"total_tokens":5}}}\n\n'

        with patch("codex_proxy.write_proxy_event") as write_event:
            payload_bytes = codex_proxy._sse_payload_bytes(line)
            payload = json.loads(payload_bytes.decode("utf-8"))
            usage = codex_proxy._usage_from_response_event(payload)
            codex_proxy._write_usage_observed_event(context, usage)

        write_event.assert_called_once()
        self.assertEqual(write_event.call_args.args[0], "usage_observed")
        fields = write_event.call_args.kwargs
        self.assertEqual(fields["request_id"], "req-async-usage")
        self.assertEqual(fields["client_id"], "zcode")
        self.assertEqual(fields["client_inference_source"], "header")
        self.assertEqual(fields["usage_source"], "upstream")
        self.assertEqual(fields["usage_input_tokens"], 2)
        self.assertEqual(fields["usage_output_tokens"], 3)

    def test_usage_observed_body_without_usage_emits_terminal_missing_event(self):
        context = {
            "request_id": "req-body-missing-usage",
            "model": "volc/glm-5.2",
            "upstream": "volcengine",
            "upstream_format": "responses",
            "inbound_format": "responses",
            "client_id": "zcode",
            "client_inference_source": "user_agent",
        }

        with patch("codex_proxy.write_proxy_event") as write_event:
            codex_proxy._write_usage_observed_body_event(
                context,
                b'{"id":"resp_1","object":"response","output":[]}',
            )

        write_event.assert_called_once()
        self.assertEqual(write_event.call_args.args[0], "usage_observed")
        fields = write_event.call_args.kwargs
        self.assertEqual(fields["request_id"], "req-body-missing-usage")
        self.assertEqual(fields["client_id"], "zcode")
        self.assertEqual(fields["usage_source"], "missing")
        self.assertEqual(fields["usage_missing_reason"], "upstream_missing_usage")

    def test_official_http_passthrough_sse_interruption_writes_terminal_failure(self):
        fake = FakeHandler()
        status = CodexProxyHandler._relay_upstream_response(
            fake,
            FakeSseResponse(
                [
                    b'data: {"type":"response.created","response":{"id":"resp_1"}}\n\n',
                    URLError("connection reset"),
                ]
            ),
            "official",
            request_id="req-1",
            model="gpt-5.5",
            upstream_format="responses",
            inbound_format="responses",
            caller_stream=True,
            behavior_profile=codex_proxy.BEHAVIOR_OFFICIAL_CODEX_APP_HTTP_PASSTHROUGH,
            usage_capture={},
        )

        body = b"".join(fake.wfile.writes)
        self.assertEqual(status, 502)
        self.assertIn(b"response.created", body)
        self.assertNotIn(b"event: error", body)
        self.assertIn(b"event: response.failed", body)
        self.assertIn(b'"type":"response.failed"', body)
        self.assertIn(b'"code":"URLError"', body)
        self.assertIn(b'"upstream":"official"', body)
        self.assertTrue(fake.close_connection)

    def test_official_passthrough_ignores_stream_error_after_completed_event(self):
        fake = FakeHandler()
        usage_capture = {}

        with patch("codex_proxy.write_proxy_event") as write_event:
            status = CodexProxyHandler._relay_upstream_response(
                fake,
                FakeSseResponse(
                    [
                        b'data: {"type":"response.created","response":{"id":"resp_1"}}\n\n',
                        b'data: {"type":"response.completed","response":{"id":"resp_1","status":"completed"}}\n\n',
                        codex_proxy.IncompleteRead(b""),
                    ]
                ),
                "official",
                request_id="req-terminal-before-eof",
                model="gpt-5.6-sol",
                upstream_format="responses",
                inbound_format="responses",
                caller_stream=True,
                behavior_profile=codex_proxy.BEHAVIOR_OFFICIAL_CODEX_APP_HTTP_PASSTHROUGH,
                usage_capture=usage_capture,
            )

        self.assertEqual(status, 200)
        self.assertTrue(usage_capture["sse_completed_event_seen"])
        self.assertNotIn(b"response.failed", b"".join(fake.wfile.writes))
        self.assertFalse(
            any(
                call.args and call.args[0] == "official_passthrough_stream_closed"
                for call in write_event.call_args_list
            )
        )

    def test_official_passthrough_shortens_post_terminal_drain_timeout(self):
        fake = FakeHandler()
        response = FakeSseResponse(
            [
                b'data: {"type":"response.completed","response":{"id":"resp_1","status":"completed"}}\n\n',
                b"",
            ]
        )
        response.shorten_terminal_drain_timeout = Mock()

        status = CodexProxyHandler._relay_upstream_response(
            fake,
            response,
            "official",
            request_id="req-terminal-drain-timeout",
            model="gpt-5.6-sol",
            upstream_format="responses",
            inbound_format="responses",
            caller_stream=True,
            behavior_profile=codex_proxy.BEHAVIOR_OFFICIAL_CODEX_APP_HTTP_PASSTHROUGH,
            usage_capture={},
        )

        self.assertEqual(status, 200)
        response.shorten_terminal_drain_timeout.assert_called_once_with(
            codex_proxy.OFFICIAL_TERMINAL_DRAIN_TIMEOUT_SECONDS
        )

    def test_official_passthrough_stream_close_records_upstream_read_counters(self):
        fake = FakeHandler()

        with patch("codex_proxy.write_proxy_event") as write_event:
            status = CodexProxyHandler._relay_upstream_response(
                fake,
                FakeSseResponse(
                    [
                        b'data: {"type":"response.created","response":{"id":"resp_1"}}\n\n',
                        URLError("connection reset"),
                    ]
                ),
                "official",
                request_id="req-official-stream-read",
                model="openai/gpt-5.5",
                upstream_format="responses",
                inbound_format="responses",
                caller_stream=True,
                behavior_profile=codex_proxy.BEHAVIOR_OFFICIAL_CODEX_APP_HTTP_PASSTHROUGH,
                usage_capture={},
            )

        self.assertEqual(status, 502)
        event = next(
            call.kwargs
            for call in write_event.call_args_list
            if call.args and call.args[0] == "official_passthrough_stream_closed"
        )
        self.assertEqual(event["failure_phase"], "stream_body")
        self.assertEqual(event["failure_side"], "upstream_read")
        self.assertEqual(event["failure_class"], "upstream_stream_interrupted")
        self.assertFalse(event["client_disconnected"])
        self.assertTrue(event["synthetic_terminal_event_sent"])
        self.assertEqual(event["synthetic_terminal_event_type"], "response.failed")
        self.assertEqual(event["lines_streamed"], 1)
        self.assertGreater(event["bytes_streamed"], 0)
        self.assertIsInstance(event["last_upstream_byte_age_ms"], int)
        self.assertGreaterEqual(event["last_upstream_byte_age_ms"], 0)
        self.assertTrue(event["headers_sent_downstream"])
        self.assertTrue(event["downstream_sse_started"])

    def test_official_passthrough_stream_close_records_downstream_write_failure(self):
        fake = FakeHandler()
        fake.wfile = FakeWFile(fail_on_write=lambda _data, index: index == 0)

        with patch("codex_proxy.write_proxy_event") as write_event:
            status = CodexProxyHandler._relay_upstream_response(
                fake,
                FakeSseResponse(
                    [
                        b'data: {"type":"response.created","response":{"id":"resp_1"}}\n\n',
                        b"",
                    ]
                ),
                "official",
                request_id="req-official-downstream-write",
                model="openai/gpt-5.5",
                upstream_format="responses",
                inbound_format="responses",
                caller_stream=True,
                behavior_profile=codex_proxy.BEHAVIOR_OFFICIAL_CODEX_APP_HTTP_PASSTHROUGH,
                usage_capture={},
            )

        self.assertEqual(status, 499)
        event = next(
            call.kwargs
            for call in write_event.call_args_list
            if call.args and call.args[0] == "official_passthrough_stream_closed"
        )
        self.assertEqual(event["status"], 499)
        self.assertEqual(event["failure_class"], "downstream_client_closed")
        self.assertTrue(event["client_disconnected"])
        self.assertEqual(event["failure_phase"], "downstream_write")
        self.assertEqual(event["failure_side"], "downstream_write")
        self.assertFalse(event["synthetic_terminal_event_sent"])
        self.assertEqual(event["lines_streamed"], 0)
        self.assertEqual(event["bytes_streamed"], 0)
        self.assertTrue(event["headers_sent_downstream"])
        self.assertTrue(event["downstream_sse_started"])

    def test_official_http_passthrough_open_failure_does_not_emit_gateway_retry_or_notice(self):
        body = json.dumps(
            {
                "model": "gpt-5.5-fast",
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
            patch("codex_proxy._official_urlopen", side_effect=[error, success]) as mock_urlopen,
            patch("codex_proxy.time.sleep") as mock_sleep,
        ):
            CodexProxyHandler.do_POST(handler)

        self.assertEqual(mock_urlopen.call_count, 1)
        mock_sleep.assert_not_called()
        event_names = [call.args[0] for call in self.write_proxy_event.call_args_list if call.args]
        self.assertNotIn("upstream_retry", event_names)
        self.assertNotIn("sse_retry_notice", event_names)
        self.assert_no_official_passthrough_gateway_events()

    def test_official_passthrough_retries_timeout_before_response_headers(self):
        body = json.dumps(
            {
                "model": "gpt-5.5-fast",
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
        handler._relay_upstream_response = Mock(return_value=200)
        success = FakeContextResponse(b'{"id":"resp_official","output":[]}')

        with (
            patch.dict(os.environ, {"CODEX_PROXY_OFFICIAL_UPSTREAM_OPEN_ATTEMPTS": "3"}, clear=False),
            patch("codex_proxy.codex_access_token", return_value="sub-token"),
            patch("codex_proxy.codex_account_id", return_value="acct-1"),
            patch("codex_proxy._official_urlopen", side_effect=[TimeoutError("connect timed out"), success]) as mock_urlopen,
            patch("codex_proxy.time.sleep") as mock_sleep,
        ):
            CodexProxyHandler.do_POST(handler)

        self.assertEqual(mock_urlopen.call_count, 2)
        mock_sleep.assert_called_once()
        retry_events = [
            call.kwargs
            for call in self.write_proxy_event.call_args_list
            if call.args and call.args[0] == "upstream_retry"
        ]
        self.assertEqual(len(retry_events), 1)
        self.assertEqual(retry_events[0]["failure_phase"], "tcp_connect")
        handler._relay_upstream_response.assert_called_once()

    def test_transport_failure_phase_classifies_ssl_eof(self):
        error = ssl.SSLEOFError("EOF occurred in violation of protocol")
        self.assertEqual(codex_proxy.transport_failure_phase(error), "tls_handshake")
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
        self.assertEqual([codex_proxy.gateway_retry_delay_seconds(attempt) for attempt in range(1, 6)], [2, 2, 4, 6, 8])

    def test_gateway_throttle_retry_delay_uses_slower_cadence(self):
        delays = [
            codex_proxy.gateway_retry_delay_seconds(
                attempt,
                failure_class=codex_proxy.RETRY_FAILURE_PROVIDER_THROTTLE,
            )
            for attempt in range(1, 6)
        ]
        self.assertEqual(delays, [10, 20, 30, 60, 60])

    def test_gateway_overloaded_retry_delay_uses_quick_cadence(self):
        delays = [
            codex_proxy.gateway_retry_delay_seconds(
                attempt,
                failure_class=codex_proxy.RETRY_FAILURE_PROVIDER_OVERLOADED,
            )
            for attempt in range(1, 7)
        ]
        self.assertEqual(delays, [2, 2, 4, 6, 8, 8])

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
            self.assertEqual(codex_proxy._upstream_retry_attempts(codex_proxy.RETRY_REQUEST_IMAGE_PROXY_VISION), 3)

    def test_default_retry_attempts_by_request_kind(self):
        with patch.dict(os.environ, {"CODEX_PROXY_AUTO_RETRY_ENABLED": "1"}, clear=False):
            self.assertEqual(codex_proxy._upstream_retry_attempts(codex_proxy.RETRY_REQUEST_MAIN_GENERATION), 5)
            self.assertEqual(codex_proxy._upstream_retry_attempts(codex_proxy.RETRY_REQUEST_COMPACT), 3)
            self.assertEqual(codex_proxy._upstream_retry_attempts(codex_proxy.RETRY_REQUEST_IMAGE_PROXY_VISION), 3)

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

    def test_official_open_uses_gateway_owned_pooled_transport(self):
        request = codex_proxy.Request("https://chatgpt.com/backend-api/codex/responses", data=b"{}", method="POST")
        success = FakeResponse(b'{"id":"resp_keepalive"}')

        with (
            patch("codex_proxy._official_urlopen", return_value=success) as official_urlopen,
            patch("codex_proxy.urlopen") as mock_urlopen,
        ):
            response = codex_proxy._open_upstream_response(
                request,
                upstream_name="official",
                upstream_format="responses",
                timeout=1,
                event_context={"request_id": "req-official-keepalive"},
                request_kind=codex_proxy.RETRY_REQUEST_MAIN_GENERATION,
            )

        self.assertIs(response, success)
        official_urlopen.assert_called_once_with(request, timeout=1)
        mock_urlopen.assert_not_called()

    def test_official_retry_closes_failed_http_response_before_reusing_pool(self):
        request = codex_proxy.Request("https://chatgpt.com/backend-api/codex/responses", data=b"{}", method="POST")
        failed_body = Mock()
        failed_body.read.return_value = b'{"error":"temporarily unavailable"}'
        error = HTTPError(request.full_url, 503, "Unavailable", {}, failed_body)
        success = FakeResponse(b'{"id":"resp_recovered"}')

        with (
            patch("codex_proxy._official_urlopen", side_effect=[error, success]),
            patch("codex_proxy.time.sleep"),
        ):
            response = codex_proxy._open_upstream_response(
                request,
                upstream_name="official",
                upstream_format="responses",
                timeout=1,
                event_context={"request_id": "req-official-http-retry"},
                max_attempts=2,
            )

        self.assertIs(response, success)
        failed_body.close.assert_called_once()

    def test_official_transport_reuses_connection_across_sequential_requests(self):
        client_ports: set[int] = set()

        class KeepaliveHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self):
                client_ports.add(self.client_address[1])
                content_length = int(self.headers.get("Content-Length", "0"))
                if content_length:
                    self.rfile.read(content_length)
                body = b'{"id":"resp_keepalive"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format, *args):
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), KeepaliveHandler)
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/responses"
            for _ in range(2):
                request = codex_proxy.Request(url, data=b"{}", method="POST")
                with codex_proxy._official_urlopen(request, timeout=2) as response:
                    self.assertEqual(response.read(), b'{"id":"resp_keepalive"}')
        finally:
            server.shutdown()
            server.server_close()
            server_thread.join(timeout=2)

        self.assertEqual(len(client_ports), 1)

    def test_open_upstream_once_keeps_official_transport_isolated_from_third_party(self):
        official_request = codex_proxy.Request(
            "https://chatgpt.com/backend-api/codex/responses", data=b"{}", method="POST"
        )
        third_party_request = codex_proxy.Request(
            "https://ark.example.test/v1/responses", data=b"{}", method="POST"
        )
        official_success = FakeResponse(b'{"id":"resp_official_transport"}')
        third_party_success = FakeResponse(b'{"id":"resp_default_transport"}')

        with (
            patch("codex_proxy._official_urlopen", return_value=official_success) as official_urlopen,
            patch("codex_proxy.urlopen", return_value=third_party_success) as mock_urlopen,
        ):
            official_response = codex_proxy._open_upstream_once(
                official_request,
                upstream_name="official",
                timeout=1,
            )
            third_party_response = codex_proxy._open_upstream_once(
                third_party_request,
                upstream_name="volcengine",
                timeout=1,
            )

        self.assertIs(official_response, official_success)
        self.assertIs(third_party_response, third_party_success)
        official_urlopen.assert_called_once_with(official_request, timeout=1)
        mock_urlopen.assert_called_once_with(third_party_request, timeout=1)

    def test_official_connection_pool_is_cached_and_bounded(self):
        private_pool = Mock()
        private_pool.pool_classes_by_scheme = {}
        with (
            patch.object(codex_proxy, "OFFICIAL_HTTP_POOLS", {}),
            patch("codex_proxy._official_proxy_url", return_value=None),
            patch("codex_proxy.urllib3.PoolManager", return_value=private_pool) as pool_manager,
        ):
            first = codex_proxy._official_pool_manager("https://chatgpt.com/backend-api/codex/responses")
            second = codex_proxy._official_pool_manager("https://chatgpt.com/backend-api/codex/responses")

        self.assertIs(first, private_pool)
        self.assertIs(second, private_pool)
        pool_manager.assert_called_once()
        options = pool_manager.call_args.kwargs
        self.assertTrue(options["block"])
        self.assertEqual(options["maxsize"], codex_proxy.OFFICIAL_POOL_MAX_CONNECTIONS)
        self.assertIn(
            (codex_proxy.socket.SOL_SOCKET, codex_proxy.socket.SO_KEEPALIVE, 1),
            options["socket_options"],
        )
        self.assertIs(
            private_pool.pool_classes_by_scheme["https"],
            codex_proxy._OfficialHTTPSConnectionPool,
        )

    def test_official_proxy_uses_windows_registry_when_environment_only_has_no_proxy(self):
        with (
            patch("codex_proxy.sys.platform", "win32"),
            patch("codex_proxy.getproxies", return_value={"no": "localhost,127.0.0.1"}),
            patch(
                "codex_proxy.getproxies_registry",
                return_value={"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"},
            ),
            patch("codex_proxy.proxy_bypass", return_value=False),
        ):
            proxy_url = codex_proxy._official_proxy_url(
                "https://chatgpt.com/backend-api/codex/responses"
            )

        self.assertEqual(proxy_url, "http://127.0.0.1:7890")

    def test_official_proxy_prefers_explicit_environment_proxy_over_windows_registry(self):
        with (
            patch("codex_proxy.sys.platform", "win32"),
            patch(
                "codex_proxy.getproxies",
                return_value={"https": "http://127.0.0.1:7891", "no": "localhost"},
            ),
            patch("codex_proxy.getproxies_registry") as registry_proxies,
            patch("codex_proxy.proxy_bypass", return_value=False),
        ):
            proxy_url = codex_proxy._official_proxy_url(
                "https://chatgpt.com/backend-api/codex/responses"
            )

        self.assertEqual(proxy_url, "http://127.0.0.1:7891")
        registry_proxies.assert_not_called()

    def test_official_pool_discards_connection_after_idle_reuse_limit(self):
        connection = Mock()
        connection._codexhub_released_at = 100.0
        pool = codex_proxy._OfficialHTTPSConnectionPool("chatgpt.com")

        with (
            patch.object(
                codex_proxy.urllib3.connectionpool.HTTPSConnectionPool,
                "_get_conn",
                return_value=connection,
            ),
            patch("codex_proxy.time.monotonic", return_value=100.0 + codex_proxy.OFFICIAL_POOL_MAX_IDLE_SECONDS + 1),
        ):
            returned = pool._get_conn()

        self.assertIs(returned, connection)
        connection.close.assert_called_once()

    def test_official_proxy_pool_keeps_connection_past_direct_idle_limit(self):
        connection = Mock()
        connection._codexhub_released_at = 100.0
        pool = codex_proxy._OfficialHTTPSConnectionPool(
            "chatgpt.com",
            _proxy=codex_proxy.urllib3.util.parse_url("http://127.0.0.1:7890"),
        )

        with (
            patch.object(
                codex_proxy.urllib3.connectionpool.HTTPSConnectionPool,
                "_get_conn",
                return_value=connection,
            ),
            patch("codex_proxy.time.monotonic", return_value=100.0 + codex_proxy.OFFICIAL_POOL_MAX_IDLE_SECONDS + 1),
        ):
            returned = pool._get_conn()

        self.assertIs(returned, connection)
        connection.close.assert_not_called()

    def test_official_proxy_pool_discards_connection_after_proxy_idle_limit(self):
        connection = Mock()
        connection._codexhub_released_at = 100.0
        pool = codex_proxy._OfficialHTTPSConnectionPool(
            "chatgpt.com",
            _proxy=codex_proxy.urllib3.util.parse_url("http://127.0.0.1:7890"),
        )

        with (
            patch.object(
                codex_proxy.urllib3.connectionpool.HTTPSConnectionPool,
                "_get_conn",
                return_value=connection,
            ),
            patch(
                "codex_proxy.time.monotonic",
                return_value=100.0 + codex_proxy.OFFICIAL_PROXY_POOL_MAX_IDLE_SECONDS + 1,
            ),
        ):
            returned = pool._get_conn()

        self.assertIs(returned, connection)
        connection.close.assert_called_once()

    def test_official_transport_caps_connect_timeout_but_preserves_read_timeout(self):
        manager = Mock()
        response = Mock(status=200, reason="OK", headers={})
        manager.request.return_value = response
        request = codex_proxy.Request(
            "https://chatgpt.com/backend-api/codex/responses", data=b"{}", method="POST"
        )

        with patch("codex_proxy._official_pool_manager", return_value=manager):
            codex_proxy._official_urlopen(request, timeout=60)

        timeout = manager.request.call_args.kwargs["timeout"]
        self.assertEqual(timeout.connect_timeout, codex_proxy.OFFICIAL_CONNECT_TIMEOUT_SECONDS)
        self.assertEqual(timeout.read_timeout, 60)

    def test_official_pool_records_release_time_for_idle_reuse_limit(self):
        connection = Mock()
        pool = codex_proxy._OfficialHTTPSConnectionPool("chatgpt.com")

        with (
            patch.object(
                codex_proxy.urllib3.connectionpool.HTTPSConnectionPool,
                "_put_conn",
            ) as put_conn,
            patch("codex_proxy.time.monotonic", return_value=123.0),
        ):
            pool._put_conn(connection)

        self.assertEqual(connection._codexhub_released_at, 123.0)
        put_conn.assert_called_once_with(connection)

    def test_official_pool_retains_five_second_tcp_keepalive_tuning(self):
        with patch("codex_proxy.sys.platform", "linux"):
            options = codex_proxy._official_socket_options()

        self.assertIn((codex_proxy.socket.SOL_SOCKET, codex_proxy.socket.SO_KEEPALIVE, 1), options)
        if hasattr(codex_proxy.socket, "TCP_KEEPIDLE"):
            self.assertIn((codex_proxy.socket.IPPROTO_TCP, codex_proxy.socket.TCP_KEEPIDLE, 5), options)
        if hasattr(codex_proxy.socket, "TCP_KEEPINTVL"):
            self.assertIn((codex_proxy.socket.IPPROTO_TCP, codex_proxy.socket.TCP_KEEPINTVL, 5), options)
        if hasattr(codex_proxy.socket, "TCP_KEEPCNT"):
            self.assertIn((codex_proxy.socket.IPPROTO_TCP, codex_proxy.socket.TCP_KEEPCNT, 3), options)

    def test_official_windows_pool_applies_keepalive_intervals(self):
        fake_socket = Mock()
        with (
            patch("codex_proxy.sys.platform", "win32"),
            patch.object(codex_proxy.socket, "SIO_KEEPALIVE_VALS", 0x98000004, create=True),
        ):
            codex_proxy._configure_official_windows_keepalive(fake_socket)

        fake_socket.ioctl.assert_called_once_with(0x98000004, (1, 5000, 5000))

    def test_official_pooled_stream_translates_reset_and_discards_connection(self):
        raw_response = Mock()
        raw_response.status = 200
        raw_response.reason = "OK"
        raw_response.headers = {"Content-Type": "text/event-stream"}
        raw_response.read.side_effect = codex_proxy.urllib3.exceptions.ProtocolError(
            "stream reset",
            ConnectionResetError("connection reset"),
        )

        with self.assertRaises(ConnectionResetError):
            with codex_proxy._OfficialPooledResponse(raw_response) as response:
                response.read(65536)

        raw_response.close.assert_called_once()
        raw_response.release_conn.assert_called_once()

    def test_official_pooled_stream_cancellation_returns_pool_capacity(self):
        pool = codex_proxy._OfficialHTTPSConnectionPool(
            "chatgpt.com",
            maxsize=1,
            block=True,
        )
        connection = pool._get_conn(timeout=0.01)
        raw_response = codex_proxy.urllib3.response.HTTPResponse(
            body=io.BytesIO(b"unfinished stream"),
            status=200,
            preload_content=False,
            pool=pool,
            connection=connection,
        )

        with codex_proxy._OfficialPooledResponse(raw_response):
            pass

        returned = pool._get_conn(timeout=0.01)
        self.assertIs(returned, connection)
        pool._put_conn(returned)

    def test_official_pooled_stream_restores_timeout_before_reuse(self):
        raw_response = Mock()
        raw_response.status = 200
        raw_response.reason = "OK"
        raw_response.headers = {"Content-Type": "text/event-stream"}
        raw_response.readline.return_value = b""
        raw_response.connection.sock.gettimeout.return_value = 300.0

        with codex_proxy._OfficialPooledResponse(raw_response) as response:
            response.shorten_terminal_drain_timeout(1.0)
            self.assertEqual(response.readline(), b"")

        self.assertEqual(
            raw_response.connection.sock.settimeout.call_args_list,
            [call(1.0), call(300.0)],
        )
        raw_response.release_conn.assert_called_once()
        raw_response.close.assert_not_called()

    def test_transparent_retry_retries_open_failure_without_downstream_notice(self):
        request = codex_proxy.Request("https://example.test/v1/chat/completions", data=b"{}", method="POST")
        error = URLError(TimeoutError("connect timed out"))
        success = FakeResponse(b'{"id":"ok","choices":[{"message":{"content":"done"}}]}')
        downstream_retry = Mock()

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
            patch("codex_proxy.time.sleep"),
        ):
            response = codex_proxy._open_upstream_response(
                request,
                upstream_name="volcengine",
                upstream_format="chat_completions",
                timeout=1,
                event_context={"request_id": "req-transparent-retry"},
                downstream_retry_callback=downstream_retry,
                request_kind=codex_proxy.RETRY_REQUEST_MAIN_GENERATION,
                retry_policy=codex_proxy.RETRY_CONSERVATIVE_PRE_OUTPUT,
            )

        self.assertIs(response, success)
        self.assertEqual(mock_urlopen.call_count, 2)
        downstream_retry.assert_not_called()

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
                expected_delay = 10 if status == 429 else 2
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
                expected_delay = 10 if expected_class == codex_proxy.RETRY_FAILURE_PROVIDER_THROTTLE else 2
                mock_sleep.assert_called_once_with(expected_delay)
                retry_events = [
                    call.kwargs for call in self.write_proxy_event.call_args_list
                    if call.args and call.args[0] == "upstream_retry"
                ]
                self.assertEqual(len(retry_events), 1)
                self.assertEqual(retry_events[0]["failure_class"], expected_class)
                self.assertEqual(retry_events[0]["delay_ms"], expected_delay * 1000)

    def test_stream_error_event_classifies_common_provider_error_values(self):
        cases = [
            (
                "openai_rate_limit",
                {"type": "error", "error": {"type": "rate_limit_error", "code": "rate_limit_exceeded"}},
                codex_proxy.RETRY_FAILURE_PROVIDER_THROTTLE,
            ),
            (
                "openai_insufficient_quota",
                {"type": "error", "error": {"type": "insufficient_quota", "code": "insufficient_quota"}},
                codex_proxy.RETRY_FAILURE_PERMANENT,
            ),
            (
                "openai_engine_overloaded",
                {"type": "error", "error": {"type": "server_error", "message": "The engine is currently overloaded"}},
                codex_proxy.RETRY_FAILURE_PROVIDER_OVERLOADED,
            ),
            (
                "azure_openai_rate_limit",
                {"type": "error", "error": {"code": "429", "message": "Rate limit is exceeded. Try again in 10 seconds."}},
                codex_proxy.RETRY_FAILURE_PROVIDER_THROTTLE,
            ),
            (
                "azure_openai_content_filter",
                {"type": "error", "error": {"code": "content_filter", "message": "The response was filtered"}},
                codex_proxy.RETRY_FAILURE_PERMANENT,
            ),
            (
                "anthropic_rate_limit",
                {"type": "error", "error": {"type": "rate_limit_error"}},
                codex_proxy.RETRY_FAILURE_PROVIDER_THROTTLE,
            ),
            (
                "anthropic_overloaded",
                {"type": "error", "error": {"type": "overloaded_error"}},
                codex_proxy.RETRY_FAILURE_PROVIDER_OVERLOADED,
            ),
            (
                "google_resource_exhausted",
                {"type": "error", "error": {"status": "RESOURCE_EXHAUSTED", "code": 429}},
                codex_proxy.RETRY_FAILURE_PROVIDER_THROTTLE,
            ),
            (
                "google_unavailable",
                {"type": "error", "error": {"status": "UNAVAILABLE", "code": 503}},
                codex_proxy.RETRY_FAILURE_PROVIDER_OVERLOADED,
            ),
            (
                "google_invalid_argument",
                {"type": "error", "error": {"status": "INVALID_ARGUMENT", "message": "Request contains an invalid argument."}},
                codex_proxy.RETRY_FAILURE_PERMANENT,
            ),
            (
                "bedrock_throttling_exception",
                {"type": "error", "error": {"type": "ThrottlingException"}},
                codex_proxy.RETRY_FAILURE_PROVIDER_THROTTLE,
            ),
            (
                "bedrock_service_unavailable",
                {"type": "error", "error": {"type": "ServiceUnavailableException"}},
                codex_proxy.RETRY_FAILURE_PROVIDER_OVERLOADED,
            ),
            (
                "bedrock_validation_exception",
                {"type": "error", "error": {"__type": "ValidationException", "message": "Input is invalid."}},
                codex_proxy.RETRY_FAILURE_PERMANENT,
            ),
            (
                "mistral_validation",
                {"type": "error", "error": {"type": "validation_error", "message": "Validation error"}},
                codex_proxy.RETRY_FAILURE_PERMANENT,
            ),
            (
                "deepseek_insufficient_balance",
                {"type": "error", "error": {"code": "insufficient_balance", "message": "Insufficient Balance"}},
                codex_proxy.RETRY_FAILURE_PERMANENT,
            ),
            (
                "deepseek_server_overloaded",
                {"type": "error", "error": {"code": 503, "message": "Server Overloaded"}},
                codex_proxy.RETRY_FAILURE_PROVIDER_OVERLOADED,
            ),
            (
                "openrouter_model_down",
                {"type": "error", "error": {"code": 502, "message": "Your chosen model is down"}},
                codex_proxy.RETRY_FAILURE_PROVIDER_OVERLOADED,
            ),
            (
                "openrouter_no_available_provider",
                {"type": "error", "error": {"code": 503, "message": "There is no available model provider"}},
                codex_proxy.RETRY_FAILURE_PROVIDER_OVERLOADED,
            ),
            (
                "openrouter_insufficient_credits",
                {"type": "error", "error": {"code": 402, "message": "Insufficient credits"}},
                codex_proxy.RETRY_FAILURE_PERMANENT,
            ),
            (
                "cohere_rate_limit",
                {"type": "error", "error": {"message": "trial token rate limit exceeded, limit is 100000 tokens per minute"}},
                codex_proxy.RETRY_FAILURE_PROVIDER_THROTTLE,
            ),
            (
                "dashscope_throttling",
                {"type": "error", "code": "Throttling", "message": "Requests rate exceeded"},
                codex_proxy.RETRY_FAILURE_PROVIDER_THROTTLE,
            ),
            (
                "dashscope_allocation_quota",
                {"type": "error", "code": "Throttling.AllocationQuota", "message": "Allocated quota exceeded, please try again later."},
                codex_proxy.RETRY_FAILURE_PROVIDER_THROTTLE,
            ),
            (
                "xunfei_concurrency_throttle",
                {"type": "error", "error": {"code": "10007", "message": "service is processing current request"}},
                codex_proxy.RETRY_FAILURE_PROVIDER_THROTTLE,
            ),
            (
                "xunfei_engine_busy",
                {"type": "error", "error": {"code": "10012", "message": "engine internal error or queued"}},
                codex_proxy.RETRY_FAILURE_PROVIDER_OVERLOADED,
            ),
            (
                "xunfei_schema_error",
                {"type": "error", "error": {"code": "10004", "message": "$.payload.message.text min length is 1"}},
                codex_proxy.RETRY_FAILURE_PERMANENT,
            ),
            (
                "xunfei_context_too_long",
                {"type": "error", "error": {"code": "10012", "message": "context length exceeded"}},
                codex_proxy.RETRY_FAILURE_PERMANENT,
            ),
        ]
        for label, payload, expected_class in cases:
            with self.subTest(label=label, expected_class=expected_class):
                exc = codex_proxy.UpstreamStreamErrorEvent(payload)

                self.assertEqual(codex_proxy._upstream_failure_class(exc), expected_class)

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
            self.assertEqual(base_attempts, 5)
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
                5,
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
        mock_sleep.assert_called_once_with(2)
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

    def test_transparent_provider_responses_retries_sse_error_event_before_downstream_headers(self):
        body = json.dumps(
            {
                "model": "glm-5.2",
                "input": [{"type": "message", "role": "user", "content": "hi"}],
                "stream": True,
            }
        ).encode("utf-8")
        handler = CodexProxyHandler.__new__(CodexProxyHandler)
        handler.path = "/v1/providers/volc/responses"
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
                b"event: response.created\n",
                b'data: {"type":"response.created","response":{"id":"resp_busy","status":"in_progress","output":[]}}\n',
                b"\n",
                b"event: response.failed\n",
                b'data: {"type":"response.failed","response":{"id":"resp_busy","status":"failed","error":{"code":10012,"message":"The system is busy, please try again later."}}}\n',
                b"\n",
                b"",
            ]
        )
        success = FakeSseResponse(
            [
                b'data: {"type":"response.created","response":{"id":"resp_transparent_retry","status":"in_progress"}}\n\n',
                b'data: {"type":"response.output_text.delta","delta":"ok"}\n\n',
                b'data: {"type":"response.completed","response":{"id":"resp_transparent_retry","status":"completed","usage":{"input_tokens":1,"output_tokens":1}}}\n\n',
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
        self.assertIn(b"resp_transparent_retry", written)
        self.assertIn(b"response.output_text.delta", written)
        self.assertNotIn(b"The system is busy", written)
        self.assertNotIn(b"event: codexhub.retry\n", written)
        retry_events = [
            call.kwargs for call in self.write_proxy_event.call_args_list
            if call.args and call.args[0] == "upstream_retry"
        ]
        self.assertEqual(len(retry_events), 1)
        self.assertEqual(retry_events[0]["failure_class"], codex_proxy.RETRY_FAILURE_PROVIDER_OVERLOADED)
        self.assertEqual(retry_events[0]["upstream"], "volcengine")

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
        self.assertEqual([call.args[0] for call in mock_sleep.call_args_list], [2, 2, 4, 6])
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

    def test_post_responses_streaming_retries_empty_completed_once_then_relays_visible_success(self):
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
        empty_stream = FakeSseResponse(
            [
                b'data: {"type":"response.created","response":{"id":"resp_empty","status":"in_progress"}}\n\n',
                b'data: {"type":"response.completed","response":{"id":"resp_empty","status":"completed","output":[],"usage":{"input_tokens":7,"output_tokens":0,"total_tokens":7}}}\n\n',
                b"",
            ]
        )
        success = FakeSseResponse(
            [
                b'data: {"type":"response.created","response":{"id":"resp_visible","status":"in_progress"}}\n\n',
                b'data: {"type":"response.output_text.delta","delta":"ok"}\n\n',
                b'data: {"type":"response.completed","response":{"id":"resp_visible","status":"completed","output":[{"type":"message","content":[{"type":"output_text","text":"ok"}]}],"usage":{"input_tokens":7,"output_tokens":1,"total_tokens":8}}}\n\n',
                b"",
            ]
        )

        with (
            patch.dict(
                os.environ,
                {
                    "CODEX_PROXY_AUTO_RETRY_ENABLED": "1",
                    "CODEX_PROXY_AUTO_RETRY_MAX_ATTEMPTS": "5",
                    "CODEX_PROXY_SSE_KEEPALIVE_SECONDS": "0",
                },
                clear=False,
            ),
            patch("codex_proxy.urlopen", side_effect=[empty_stream, success]) as mock_urlopen,
            patch("codex_proxy.time.sleep") as mock_sleep,
        ):
            CodexProxyHandler.do_POST(handler)

        self.assertEqual(mock_urlopen.call_count, 2)
        mock_sleep.assert_called_once_with(2)
        self.assertEqual(fake.status, 200)
        written = b"".join(fake.wfile.writes)
        self.assertIn(b"resp_visible", written)
        self.assertIn(b'"delta":"ok"', written)
        self.assertNotIn(b"resp_empty", written)
        self.assertNotIn(b"upstream_empty_completed_response", written)
        retry_events = [
            call.kwargs for call in self.write_proxy_event.call_args_list
            if call.args and call.args[0] == "upstream_retry"
        ]
        self.assertEqual(len(retry_events), 1)
        self.assertEqual(retry_events[0]["error"], "UpstreamEmptyCompletedResponseError")
        self.assertEqual(retry_events[0]["max_attempts"], 2)

    def test_post_responses_streaming_empty_completed_retry_is_bounded_to_one_extra_attempt(self):
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
        empty_streams = [
            FakeSseResponse(
                [
                    f'data: {{"type":"response.created","response":{{"id":"resp_empty_{index}","status":"in_progress"}}}}\n\n'.encode("utf-8"),
                    f'data: {{"type":"response.completed","response":{{"id":"resp_empty_{index}","status":"completed","output":[],"usage":{{"input_tokens":7,"output_tokens":0,"total_tokens":7}}}}}}\n\n'.encode("utf-8"),
                    b"",
                ]
            )
            for index in range(2)
        ]
        should_not_be_used = FakeSseResponse(
            [
                b'data: {"type":"response.created","response":{"id":"resp_unwanted_success","status":"in_progress"}}\n\n',
                b'data: {"type":"response.output_text.delta","delta":"late ok"}\n\n',
                b'data: {"type":"response.completed","response":{"id":"resp_unwanted_success","status":"completed","output":[{"type":"message","content":[{"type":"output_text","text":"late ok"}]}],"usage":{"input_tokens":7,"output_tokens":2,"total_tokens":9}}}\n\n',
                b"",
            ]
        )

        with (
            patch.dict(
                os.environ,
                {
                    "CODEX_PROXY_AUTO_RETRY_ENABLED": "1",
                    "CODEX_PROXY_AUTO_RETRY_MAX_ATTEMPTS": "5",
                    "CODEX_PROXY_SSE_KEEPALIVE_SECONDS": "0",
                },
                clear=False,
            ),
            patch("codex_proxy.urlopen", side_effect=[*empty_streams, should_not_be_used]) as mock_urlopen,
            patch("codex_proxy.time.sleep") as mock_sleep,
        ):
            CodexProxyHandler.do_POST(handler)

        self.assertEqual(mock_urlopen.call_count, 2)
        mock_sleep.assert_called_once_with(2)
        self.assertEqual(fake.status, 200)
        written = b"".join(fake.wfile.writes)
        self.assertIn(b"upstream_empty_completed_response", written)
        self.assertNotIn(b'"type":"response.completed"', written)
        self.assertNotIn(b"resp_empty_0", written)
        self.assertNotIn(b"resp_empty_1", written)
        self.assertNotIn(b"resp_unwanted_success", written)
        retry_events = [
            call.kwargs for call in self.write_proxy_event.call_args_list
            if call.args and call.args[0] == "upstream_retry"
        ]
        self.assertEqual(len(retry_events), 1)
        self.assertEqual(retry_events[0]["error"], "UpstreamEmptyCompletedResponseError")
        self.assertEqual(retry_events[0]["max_attempts"], 2)

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
            patch("codex_proxy._official_urlopen", return_value=FakeContextResponse(b'{"id":"resp_control"}')),
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
            patch("codex_proxy._official_urlopen", side_effect=error),
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
        self.assertIn('<image path="codexhub://image/', content[1]["text"])
        self.assertIn("</image>", content[1]["text"])
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

    def test_text_only_image_boundary_guard_rejects_raw_images_when_proxy_disabled(self):
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

        with patch.dict(os.environ, {"CODEX_PROXY_IMAGE_PROXY_ENABLED": "0"}, clear=False):
            with self.assertRaises(codex_proxy.ImageProxyError) as context:
                codex_proxy.enforce_text_only_image_boundary(
                    payload,
                    inbound_format="responses",
                    target_model="volc/glm-5.2",
                    target_upstream=upstream,
                    event_context={"request_id": "req_img"},
                )

        self.assertIn("does not support image input", str(context.exception))
        self.assertIn("Image Proxy is disabled", str(context.exception))

    def test_image_proxy_prompt_requests_ocr_ui_and_chart_detail(self):
        prompt = codex_proxy.IMAGE_PROXY_PROMPT

        self.assertIn("visible text", prompt)
        self.assertIn("OCR", prompt)
        self.assertIn("UI", prompt)
        self.assertIn("buttons", prompt)
        self.assertIn("errors", prompt)
        self.assertIn("charts", prompt)
        self.assertIn("tables", prompt)
        self.assertIn("ambiguous", prompt)
        self.assertIn("unreadable", prompt)

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

    def test_runtime_discovered_official_models_route_for_bare_and_openai_aliases(self):
        catalog = {
            model: {
                "slug": model,
                "supported_in_api": True,
                "codex_proxy_metadata": {
                    "provider": "openai",
                    "upstream_name": "official",
                    "upstream_model": model,
                },
            }
            for model in ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna")
        }

        with patch("codex_proxy.generated_catalog_by_slug", return_value=catalog):
            for model in ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"):
                with self.subTest(model=model, form="bare"):
                    upstream = choose_upstream(model)
                    self.assertEqual(upstream["name"], "official")
                    self.assertEqual(upstream["auth"], "codex_auth")
                    self.assertEqual(upstream["upstream_model"], model)
                with self.subTest(model=model, form="alias"):
                    upstream = choose_upstream(f"openai/{model}")
                    self.assertEqual(upstream["name"], "official")
                    self.assertEqual(upstream["upstream_model"], model)

    def test_runtime_official_route_rejects_untrusted_catalog_metadata(self):
        catalog = {
            "openai/gpt-untrusted": {
                "slug": "openai/gpt-untrusted",
                "supported_in_api": True,
                "codex_proxy_metadata": {
                    "provider": "external",
                    "upstream_name": "official",
                    "upstream_model": "gpt-untrusted",
                },
            }
        }

        with patch("codex_proxy.generated_catalog_by_slug", return_value=catalog):
            with self.assertRaisesRegex(ValueError, "model is not allowed"):
                choose_upstream("gpt-untrusted")

    def test_runtime_official_route_respects_policy_denylist(self):
        policy = codex_proxy.load_policy(codex_proxy.POLICY_PATH)
        policy = replace(policy, denied_models=set(policy.denied_models) | {"gpt-5.6-sol"})
        catalog = {
            "gpt-5.6-sol": {
                "slug": "gpt-5.6-sol",
                "supported_in_api": True,
                "codex_proxy_metadata": {
                    "provider": "openai",
                    "upstream_name": "official",
                    "upstream_model": "gpt-5.6-sol",
                },
            }
        }

        with (
            patch("codex_proxy.load_policy", return_value=policy),
            patch("codex_proxy.generated_catalog_by_slug", return_value=catalog),
        ):
            with self.assertRaisesRegex(ValueError, "model is not allowed"):
                choose_upstream("gpt-5.6-sol")

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
                                "slug": "gpt-5.5",
                                "display_name": "5.5",
                                "context_window": 258400,
                            },
                            {
                                "slug": "gpt-5.4",
                                "display_name": "5.4",
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
        self.assertNotIn("openai/gpt-5.5-fast", by_slug)
        self.assertEqual(by_slug["gpt-5.5-fast"]["display_name"], "5.5 Fast")
        self.assertEqual(by_slug["gpt-5.5-fast"]["context_window"], 258400)
        self.assertEqual(by_slug["gpt-5.5-fast"]["codex_proxy_metadata"]["upstream_model"], "gpt-5.5")
        self.assertEqual(by_slug["gpt-5.5-fast"]["codex_proxy_metadata"]["service_tier"], "priority")
        self.assertEqual(by_slug["gpt-5.4-fast"]["display_name"], "5.4 Fast")

    def test_current_catalog_data_exposes_vision_for_every_model_only_while_image_proxy_is_enabled(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_proxy_dir = Path(tmpdir) / "proxy"
            runtime_proxy_dir.mkdir()
            catalog_path = Path(tmpdir) / "catalog.json"
            catalog_path.write_text(
                json.dumps(
                    {
                        "models": [
                            {
                                "slug": "glm-5.2",
                                "input_modalities": ["text"],
                            },
                            {
                                "slug": "text-model-without-modalities",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )

            with (
                patch("codex_proxy.existing_generated_catalog_path", return_value=catalog_path),
                patch("codex_proxy.RUNTIME_PROXY_DIR", runtime_proxy_dir),
                patch.dict(os.environ, {"CODEX_PROXY_IMAGE_PROXY_ENABLED": "1"}, clear=False),
            ):
                enabled_catalog = codex_proxy.current_catalog_data()

            with (
                patch("codex_proxy.existing_generated_catalog_path", return_value=catalog_path),
                patch("codex_proxy.RUNTIME_PROXY_DIR", runtime_proxy_dir),
                patch.dict(os.environ, {"CODEX_PROXY_IMAGE_PROXY_ENABLED": "0"}, clear=False),
            ):
                disabled_catalog = codex_proxy.current_catalog_data()

        enabled_by_slug = {model["slug"]: model for model in enabled_catalog["models"]}
        disabled_by_slug = {model["slug"]: model for model in disabled_catalog["models"]}
        self.assertEqual(enabled_by_slug["glm-5.2"]["input_modalities"], ["text", "image"])
        self.assertEqual(
            enabled_by_slug["text-model-without-modalities"]["input_modalities"],
            ["text", "image"],
        )
        self.assertEqual(disabled_by_slug["glm-5.2"]["input_modalities"], ["text"])
        self.assertNotIn("input_modalities", disabled_by_slug["text-model-without-modalities"])

    def test_current_catalog_data_applies_context_guard_only_to_openai_models(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            runtime_proxy_dir = Path(tmpdir) / "proxy"
            runtime_proxy_dir.mkdir()
            (runtime_proxy_dir / "settings.json").write_text(
                json.dumps({"openai_context_guard_enabled": True}),
                encoding="utf-8",
            )
            catalog_path = Path(tmpdir) / "catalog.json"
            catalog_path.write_text(
                json.dumps(
                    {
                        "models": [
                            {
                                "slug": "gpt-5.6-sol",
                                "context_window": 353_000,
                                "max_context_window": 353_000,
                            },
                            {
                                "slug": "gpt-5.3-codex-spark",
                                "context_window": 128_000,
                                "max_context_window": 128_000,
                            },
                            {
                                "slug": "glm-5.2",
                                "context_window": 1_000_000,
                                "max_context_window": 1_000_000,
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )

            with (
                patch("codex_proxy.existing_generated_catalog_path", return_value=catalog_path),
                patch("codex_proxy.RUNTIME_PROXY_DIR", runtime_proxy_dir),
            ):
                guarded_catalog = codex_proxy.current_catalog_data()

            (runtime_proxy_dir / "settings.json").write_text(
                json.dumps({"openai_context_guard_enabled": False}),
                encoding="utf-8",
            )
            with (
                patch("codex_proxy.existing_generated_catalog_path", return_value=catalog_path),
                patch("codex_proxy.RUNTIME_PROXY_DIR", runtime_proxy_dir),
            ):
                unguarded_catalog = codex_proxy.current_catalog_data()

        guarded_by_slug = {model["slug"]: model for model in guarded_catalog["models"]}
        unguarded_by_slug = {model["slug"]: model for model in unguarded_catalog["models"]}
        self.assertEqual(guarded_by_slug["gpt-5.6-sol"]["context_window"], 272_000)
        self.assertEqual(guarded_by_slug["gpt-5.6-sol"]["max_context_window"], 272_000)
        self.assertEqual(guarded_by_slug["gpt-5.3-codex-spark"]["context_window"], 128_000)
        self.assertEqual(guarded_by_slug["glm-5.2"]["context_window"], 1_000_000)
        self.assertEqual(unguarded_by_slug["gpt-5.6-sol"]["context_window"], 353_000)

    def test_current_catalog_data_dedupes_official_aliases_without_touching_third_party_ids(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            catalog_path = Path(tmpdir) / "catalog.json"
            catalog_path.write_text(
                json.dumps(
                    {
                        "models": [
                            {
                                "slug": "openai/gpt-5.5",
                                "display_name": "Legacy GPT-5.5",
                                "context_window": 1,
                                "enabled": True,
                            },
                            {
                                "slug": "gpt-5.5",
                                "display_name": "GPT-5.5",
                                "context_window": 258400,
                                "enabled": False,
                            },
                            {
                                "slug": "acme/gpt-5.6-sol",
                                "display_name": "Acme Sol",
                            },
                            {
                                "slug": "ollama-cloud/glm-5.2",
                                "display_name": "GLM-5.2",
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )

            with patch("codex_proxy.existing_generated_catalog_path", return_value=catalog_path):
                catalog = codex_proxy.current_catalog_data()

        models = catalog["models"]
        by_slug = {model["slug"]: model for model in models}
        self.assertEqual(
            [model["slug"] for model in models if not model["slug"].endswith("-fast")],
            ["gpt-5.5", "acme/gpt-5.6-sol", "ollama-cloud/glm-5.2"],
        )
        self.assertNotIn("openai/gpt-5.5", by_slug)
        self.assertEqual(by_slug["gpt-5.5"]["display_name"], "5.5")
        self.assertEqual(by_slug["gpt-5.5"]["context_window"], 258400)
        self.assertTrue(by_slug["gpt-5.5"]["enabled"])
        self.assertIn("acme/gpt-5.6-sol", by_slug)
        self.assertIn("ollama-cloud/glm-5.2", by_slug)

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

    def test_responses_to_chat_completion_body_accepts_role_content_items_without_type(self):
        body = json.dumps(
            {
                "model": "glm-5.2",
                "input": [
                    {
                        "role": "user",
                        "content": [{"type": "input_text", "text": "Reply with E2EOK03 only."}],
                    }
                ],
                "stream": False,
            }
        ).encode("utf-8")

        payload = json.loads(_responses_request_to_chat_completion_body(body))

        self.assertEqual(
            payload["messages"],
            [{"role": "user", "content": "Reply with E2EOK03 only."}],
        )

    def test_responses_error_detail_converts_to_chat_error_instead_of_empty_message(self):
        body = json.dumps({"detail": "Store must be set to false"}).encode("utf-8")

        payload = json.loads(codex_proxy._response_body_to_chat_completion_body(body))

        self.assertIn("error", payload)
        self.assertEqual(payload["error"]["message"], "Store must be set to false")
        self.assertNotIn("choices", payload)

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

    def test_official_body_converts_compaction_input_to_developer_message(self):
        upstream = choose_upstream("gpt-5.5")
        body = b'{"model":"gpt-5.5","input":[{"type":"compaction","summary":"keep official shape"}]}'

        transformed = compatible_request_body(body, upstream)
        payload = json.loads(transformed)

        self.assertEqual(payload["input"][0]["type"], "message")
        self.assertEqual(payload["input"][0]["role"], "developer")
        self.assertIn("Compacted conversation context", payload["input"][0]["content"])
        self.assertIn("keep official shape", payload["input"][0]["content"])
        self.assertNotIn('"type":"compaction"', transformed.decode("utf-8"))

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

    def test_official_transparent_request_body_removes_unsupported_max_output_tokens(self):
        body = json.dumps(
            {
                "model": "openai/gpt-5.5",
                "input": [{"role": "user", "content": "hi"}],
                "stream": True,
                "max_output_tokens": 16,
            }
        ).encode("utf-8")
        upstream = {"name": "official", "upstream_model": "gpt-5.5"}

        transformed = codex_proxy.transparent_request_body(
            body,
            json.loads(body),
            upstream,
            model_id="openai/gpt-5.5",
        )
        payload = json.loads(transformed)

        self.assertEqual(payload["model"], "gpt-5.5")
        self.assertNotIn("max_output_tokens", payload)

    def test_official_transparent_request_body_normalizes_string_input_for_codex_backend(self):
        body = json.dumps(
            {
                "model": "openai/gpt-5.5",
                "input": "hi",
                "stream": True,
            }
        ).encode("utf-8")
        upstream = {"name": "official", "upstream_model": "gpt-5.5"}

        transformed = codex_proxy.transparent_request_body(
            body,
            json.loads(body),
            upstream,
            model_id="openai/gpt-5.5",
        )
        payload = json.loads(transformed)

        self.assertEqual(
            payload["input"],
            [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
        )

    def test_provider_transparent_request_body_normalizes_message_shorthand(self):
        body = json.dumps(
            {
                "model": "xopdeepseekv4flash",
                "input": [
                    {"role": "developer", "content": "System guidance."},
                    {"role": "user", "content": [{"type": "input_text", "text": "test"}]},
                ],
                "stream": True,
            }
        ).encode("utf-8")
        upstream = {"name": "xunfei", "upstream_format": "responses"}

        transformed = codex_proxy.transparent_request_body(
            body,
            json.loads(body),
            upstream,
            model_id="xunfei/xopdeepseekv4flash",
        )
        payload = json.loads(transformed)

        self.assertEqual(payload["input"][0]["type"], "message")
        self.assertEqual(payload["input"][0]["role"], "developer")
        self.assertEqual(payload["input"][0]["content"], "System guidance.")
        self.assertEqual(payload["input"][1]["type"], "message")
        self.assertEqual(payload["input"][1]["role"], "user")

    def test_official_transparent_request_body_sets_store_false_for_codex_backend(self):
        body = json.dumps(
            {
                "model": "gpt-5.5-fast",
                "input": [{"type": "message", "role": "user", "content": "hi"}],
                "stream": True,
            }
        ).encode("utf-8")
        upstream = {"name": "official", "upstream_model": "gpt-5.5"}

        transformed = codex_proxy.transparent_request_body(
            body,
            json.loads(body),
            upstream,
            model_id="openai/gpt-5.5",
        )
        payload = json.loads(transformed)

        self.assertIs(payload["store"], False)

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
        self.assertNotIn("Codex browser context detected.", json.dumps(payload))
        self.assertEqual(len(payload["input"]), 1)

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

    def test_external_text_compat_body_replaces_opaque_compaction_input(self):
        upstream = dict(choose_upstream("volc/glm-5.2"))
        upstream["tool_protocol"] = "text_compat"
        body = json.dumps(
            {
                "model": "volc/glm-5.2",
                "input": [
                    {"type": "message", "role": "user", "content": "test"},
                    {
                        "type": "compaction",
                        "encrypted_content": "gAAAA-opaque-context",
                    },
                ],
            }
        ).encode("utf-8")

        transformed = compatible_request_body(body, upstream)
        payload = json.loads(transformed)
        raw = transformed.decode("utf-8")

        self.assertEqual(payload["input"][1]["type"], "message")
        self.assertEqual(payload["input"][1]["role"], "developer")
        self.assertIn("opaque", payload["input"][1]["content"])
        self.assertNotIn('"type":"compaction"', raw)
        self.assertNotIn("gAAAA", raw)

    def test_external_responses_structured_body_sanitizes_internal_input_items(self):
        upstream = {
            "name": "xunfei",
            "upstream_model": "xopglm52",
            "upstream_format": "responses",
        }
        body = json.dumps(
            {
                "model": "xunfei/xopglm52",
                "input": [
                    {"type": "message", "role": "user", "content": "test"},
                    {
                        "type": "compaction",
                        "encrypted_content": "gAAAA-opaque-context",
                    },
                    {"type": "compaction_trigger", "threshold": 200000},
                    {
                        "type": "reasoning",
                        "encrypted_content": "gAAAA-reasoning",
                    },
                ],
            }
        ).encode("utf-8")

        transformed = compatible_request_body(body, upstream, model_id="xunfei/xopglm52")
        payload = json.loads(transformed)
        raw = transformed.decode("utf-8")

        self.assertEqual(payload["model"], "xopglm52")
        self.assertEqual([item["type"] for item in payload["input"]], ["message", "message"])
        self.assertEqual(payload["input"][1]["role"], "developer")
        self.assertIn("opaque", payload["input"][1]["content"])
        for forbidden in ('"type":"compaction"', '"type":"compaction_trigger"', '"type":"reasoning"', "gAAAA"):
            self.assertNotIn(forbidden, raw)

    def test_external_responses_structured_body_preserves_available_tool_history(self):
        upstream = {
            "name": "ollama_cloud",
            "upstream_model": "glm-5.2",
            "upstream_format": "responses",
            "tool_protocol": "responses_structured",
        }
        body = json.dumps(
            {
                "model": "glm-5.2",
                "input": [
                    {"type": "message", "role": "user", "content": "Run echo first."},
                    {
                        "type": "function_call",
                        "call_id": "call_shell",
                        "name": "shell_command",
                        "arguments": "{\"command\":\"echo hi\"}",
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_shell",
                        "output": "Exit code: 0\nOutput:\nhi",
                    },
                    {"type": "message", "role": "user", "content": "Now answer normally."},
                ],
                "tools": [
                    {
                        "type": "function",
                        "name": "shell_command",
                        "parameters": {"type": "object"},
                    }
                ],
                "stream": True,
            }
        ).encode("utf-8")

        transformed = compatible_request_body(body, upstream, model_id="glm-5.2")
        payload = json.loads(transformed)
        raw = transformed.decode("utf-8")

        self.assertEqual(payload["model"], "glm-5.2")
        self.assertEqual(
            [item["type"] for item in payload["input"][:4]],
            ["message", "function_call", "function_call_output", "message"],
        )
        self.assertEqual(payload["input"][1]["name"], "shell_command")
        self.assertEqual(payload["input"][1]["call_id"], "call_shell")
        self.assertEqual(payload["input"][2]["call_id"], "call_shell")
        self.assertIn("Output", payload["input"][2]["output"])
        self.assertIn('"type":"function_call"', raw)
        self.assertIn('"type":"function_call_output"', raw)

    def test_external_no_tool_protocol_body_sanitizes_internal_input_items(self):
        upstream = {
            "name": "no_tools",
            "upstream_model": "glm-5.2",
            "tool_protocol": "none",
        }
        body = json.dumps(
            {
                "model": "volc/glm-5.2",
                "tools": [
                    {"type": "function", "name": "normal_tool", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__spawn_agent", "parameters": {"type": "object"}},
                ],
                "input": [
                    {"type": "message", "role": "user", "content": "test"},
                    {"type": "compaction_trigger", "threshold": 200000},
                    {
                        "type": "reasoning",
                        "encrypted_content": "gAAAA-reasoning",
                    },
                ],
            }
        ).encode("utf-8")

        transformed = compatible_request_body(body, upstream, model_id="volc/glm-5.2")
        payload = json.loads(transformed)
        raw = transformed.decode("utf-8")

        self.assertEqual(payload["model"], "glm-5.2")
        self.assertEqual(payload["tools"], [{"type": "function", "name": "normal_tool", "parameters": {"type": "object"}}])
        self.assertEqual(payload["input"], [{"type": "message", "role": "user", "content": "test"}])
        self.assertNotIn('"type":"compaction_trigger"', raw)
        self.assertNotIn('"type":"reasoning"', raw)
        self.assertNotIn("gAAAA", raw)

    def test_third_party_transparent_body_sanitizes_internal_input_items(self):
        upstream = {"name": "ollama_cloud", "upstream_model": "glm-5.2"}
        body = json.dumps(
            {
                "model": "volc/glm-5.2",
                "input": [
                    {"type": "message", "role": "user", "content": "test"},
                    {
                        "type": "compaction",
                        "encrypted_content": "gAAAA-opaque-context",
                    },
                    {"type": "compaction_trigger", "threshold": 200000},
                ],
            }
        ).encode("utf-8")

        transformed = codex_proxy.transparent_request_body(
            body,
            json.loads(body),
            upstream,
            model_id="volc/glm-5.2",
        )
        payload = json.loads(transformed)
        raw = transformed.decode("utf-8")

        self.assertEqual(payload["model"], "glm-5.2")
        self.assertEqual([item["type"] for item in payload["input"]], ["message", "message"])
        self.assertEqual(payload["input"][1]["role"], "developer")
        self.assertIn("opaque", payload["input"][1]["content"])
        self.assertNotIn('"type":"compaction"', raw)
        self.assertNotIn('"type":"compaction_trigger"', raw)
        self.assertNotIn("gAAAA", raw)

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
        self.assertEqual(status, 502)
        self.assertNotIn(b"streamed raw thinking", data)
        self.assertNotIn(b"response.completed", data)
        self.assertIn(b"upstream_empty_completed_response", data)

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

    def test_chat_tool_call_chunks_fail_closed_when_upstream_omits_id(self):
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

        with self.assertRaises(codex_proxy.UpstreamProtocolTranslationError) as raised:
            _chat_stream_chunks_to_response_events(chunks)

        self.assertEqual(raised.exception.cause.code, "unpaired_tool_call")

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
            event_context={"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT},
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
                "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT,
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
            "request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT,
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
            "request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT,
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
            event_context={"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT},
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
            event_context={"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT},
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
                "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT,
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
            event_context={"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT},
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

    def test_transparent_chat_to_responses_stream_treats_done_write_reset_as_downstream_close(self):
        handler = FakeHandler()
        handler.wfile = FakeWFile(
            fail_on_write=lambda data, _index: data == b"data: [DONE]\n\n"
        )
        chunks = [
            {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]},
            {"usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}, "choices": []},        ]
        response = FakeSseResponse(
            [f"data: {json.dumps(chunk)}\n".encode("utf-8") for chunk in chunks] + [b"data: [DONE]\n", b""]
        )

        status = CodexProxyHandler._relay_upstream_response(
            handler,
            response,
            "ollama_cloud",
            request_id="req_done_reset",
            model="ollama-cloud/glm-5.2",
            upstream_format="chat_completions",
            inbound_format="responses",
            caller_stream=True,
            event_context={"client_id": "omp", "client_inference_source": "header"},
            behavior_profile=codex_proxy.BEHAVIOR_THIRD_PARTY_APP_TRANSPARENT_METERED,
        )

        self.assertEqual(status, 200)
        self.assertTrue(handler.close_connection)
        event_names = [call.args[0] for call in self.write_proxy_event.call_args_list if call.args]
        self.assertIn("downstream_stream_closed", event_names)
        self.assertNotIn("upstream_stream_interrupted", event_names)
        downstream_event = next(
            call.kwargs for call in self.write_proxy_event.call_args_list
            if call.args and call.args[0] == "downstream_stream_closed"
        )
        self.assertEqual(downstream_event["client_id"], "omp")

    def test_transparent_chat_passthrough_stream_treats_done_write_reset_as_downstream_close(self):
        handler = FakeHandler()
        handler.wfile = FakeWFile(
            fail_on_write=lambda data, _index: data.startswith(b"data: [DONE]")
        )
        chunks = [
            {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]},
            {"usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}, "choices": []},        ]
        response = FakeSseResponse(
            [f"data: {json.dumps(chunk)}\n".encode("utf-8") for chunk in chunks] + [b"data: [DONE]\n", b""]
        )

        status = CodexProxyHandler._relay_transparent_upstream_response(
            handler,
            response,
            "ollama_cloud",
            request_id="req_chat_passthrough_done_reset",
            model="ollama-cloud/glm-5.2",
            upstream_format="chat_completions",
            inbound_format="chat_completions",
            event_context={"client_id": "omp", "client_inference_source": "header"},
        )

        self.assertEqual(status, 200)
        self.assertTrue(handler.close_connection)
        event_names = [call.args[0] for call in self.write_proxy_event.call_args_list if call.args]
        self.assertIn("downstream_stream_closed", event_names)
        self.assertNotIn("transparent_stream_closed", event_names)
        downstream_event = next(
            call.kwargs for call in self.write_proxy_event.call_args_list
            if call.args and call.args[0] == "downstream_stream_closed"
        )
        self.assertEqual(downstream_event["client_id"], "omp")
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

    def test_transparent_responses_sse_error_event_before_output_defers_without_headers(self):
        handler = FakeHandler()
        response = FakeSseResponse(
            [
                b"event: error\n",
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
                request_id="req_transparent_busy_retry",
                model="xunfei/xopglm52",
                upstream_format="responses",
                inbound_format="responses",
                caller_stream=True,
                behavior_profile=codex_proxy.BEHAVIOR_THIRD_PARTY_APP_TRANSPARENT_METERED,
                defer_stream_errors=True,
            )

        self.assertIsNone(handler.status)
        self.assertFalse(handler.headers_ended)
        self.assertEqual(handler.wfile.writes, [])
        self.assertEqual(
            codex_proxy._upstream_failure_class(raised.exception),
            codex_proxy.RETRY_FAILURE_PROVIDER_OVERLOADED,
        )

    def test_transparent_responses_response_failed_after_metadata_defers_without_headers(self):
        handler = FakeHandler()
        response = FakeSseResponse(
            [
                b"event: response.created\n",
                b'data: {"type":"response.created","response":{"id":"resp_busy","status":"in_progress","output":[]}}\n',
                b"\n",
                b"event: response.in_progress\n",
                b'data: {"type":"response.in_progress","response":{"id":"resp_busy","status":"in_progress"}}\n',
                b"\n",
                b"event: response.failed\n",
                b'data: {"type":"response.failed","response":{"id":"resp_busy","status":"failed","error":{"code":10012,"message":"EngineInternalError:1105|{\\"Code\\":1105,\\"Message\\":\\"The system is busy, please try again later.\\"}"}}}\n',
                b"\n",
                b"",
            ]
        )

        with self.assertRaises(codex_proxy.UpstreamStreamErrorEvent) as raised:
            CodexProxyHandler._relay_upstream_response(
                handler,
                response,
                "xunfei",
                request_id="req_transparent_failed_after_metadata",
                model="xunfei/xopglm52",
                upstream_format="responses",
                inbound_format="responses",
                caller_stream=True,
                behavior_profile=codex_proxy.BEHAVIOR_THIRD_PARTY_APP_TRANSPARENT_METERED,
                defer_stream_errors=True,
            )

        self.assertIsNone(handler.status)
        self.assertFalse(handler.headers_ended)
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

    def test_official_browser_context_does_not_inject_skill_guidance_or_tools(self):
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
        self.assertNotIn("browser:control-in-app-browser", transcript)
        self.assertNotIn("node_repl js", transcript)
        self.assertNotIn("browser session unavailable", transcript)
        self.assertEqual(len(payload["input"]), 1)
        self.assertNotIn('"role":"system"', transformed.decode("utf-8"))

    def test_external_request_injects_explicit_codex_native_tools(self):
        body = json.dumps({"model": "glm-5.2", "input": "spawn a child"}).encode("utf-8")

        transformed = compatible_request_body(body, {"name": "ollama_cloud"}, event_context={"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT})
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
            event_context={"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT},
            inject_codex_tools=False,
        )
        payload = json.loads(transformed)

        self.assertEqual(payload["input"][0]["type"], "message")
        self.assertEqual(payload["input"][0]["role"], "developer")
        self.assertEqual(payload["input"][1]["type"], "message")
        self.assertEqual(payload["input"][1]["role"], "user")
        self.assertEqual(payload["input"][1]["content"], [{"type": "input_text", "text": "test"}])
        self.assertEqual(payload["input"][2]["type"], "message")
        self.assertEqual(payload["input"][2]["role"], "developer")
        self.assertIn("Read-only Codex function call transcript", payload["input"][2]["content"])
        self.assertIn("known_tool", payload["input"][2]["content"])
        self.assertEqual(payload["input"][3]["type"], "message")
        self.assertEqual(payload["input"][3]["role"], "developer")
        self.assertIn("Read-only Codex function result transcript", payload["input"][3]["content"])
        self.assertIn("ok", payload["input"][3]["content"])

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
            event_context={"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT},
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
        event_context = {"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT}

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
        event_context = {"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT}

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
                event_context={"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT},
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
        event_context = {"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT}

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

    def test_responses_structured_exact_line_child_prompt_is_worker_context(self):
        worker_prompt = "Return exactly this line: SENTINEL:level1-single-glm52-responses"
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
        event_context = {"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT}

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

        self.assertTrue(event_context["subagent_worker_context"])
        self.assertFalse(event_context["subagent_spawn_allowed"])
        self.assertNotIn("mcp__codex_apps__local_tool_gateway___run_shell", tools_by_name)
        self.assertNotIn("multi_agent_v1__spawn_agent", tools_by_name)
        self.assertNotIn("tool_choice", payload)

    def test_responses_structured_dynamic_dag_node_prompt_is_worker_context(self):
        worker_prompt = (
            "You are a Level 3 Dynamic DAG worker.\n"
            "Node: task-a-implementer\n"
            "Return exactly one line:\n"
            "A_DONE\n"
            "Do not call multi_agent tools. Do not create or modify files."
        )
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
        event_context = {"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT}

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

        self.assertTrue(event_context["subagent_worker_context"])
        self.assertFalse(event_context["subagent_spawn_allowed"])
        self.assertNotIn("mcp__codex_apps__local_tool_gateway___run_shell", tools_by_name)
        self.assertNotIn("multi_agent_v1__spawn_agent", tools_by_name)
        self.assertNotIn("tool_choice", payload)

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
        event_context = {"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT}

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
        event_context = {"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT}

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
        event_context = {"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT}

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
        event_context = {"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT}

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
        event_context = {"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT}

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
        event_context = {"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT}

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
        event_context = {"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT}

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

    def test_responses_structured_coerces_parallel_exact_child_spawn_prompts_in_order(self):
        level_one_prompt = """
Execute a bounded concurrent two-agent Codex native subagent lifecycle.

Required sequence:
1. Spawn child A with prompt exactly this complete string: `Return exactly this line: SENTINEL:A`
2. Spawn child B with prompt exactly this complete string: `Return exactly this line: SENTINEL:B`
3. Do not wait before both children have been spawned.
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
        event_context = {"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT}

        compatible_request_body(
            body,
            {"name": "ollama_cloud", "upstream_format": "responses", "tool_protocol": "responses_structured"},
            event_context=event_context,
        )
        response_body = json.dumps(
            {
                "id": "resp_parallel",
                "object": "response",
                "status": "completed",
                "output": [
                    {
                        "id": "fc_a",
                        "type": "function_call",
                        "status": "completed",
                        "call_id": "call_a",
                        "namespace": "multi_agent_v1",
                        "name": "spawn_agent",
                        "arguments": json.dumps(
                            {"message": "Return exactly this line: SENTINEL:A", "fork_context": False}
                        ),
                    },
                    {
                        "id": "fc_b",
                        "type": "function_call",
                        "status": "completed",
                        "call_id": "call_b",
                        "namespace": "multi_agent_v1",
                        "name": "spawn_agent",
                        "arguments": json.dumps(
                            {"message": "Return exactly this line: SENTINEL:A", "fork_context": False}
                        ),
                    },
                ],
            }
        ).encode("utf-8")

        transformed_response = compatible_response_body(response_body, "ollama_cloud", event_context=event_context)
        output = json.loads(transformed_response)["output"]
        call_args = [json.loads(item["arguments"]) for item in output]

        self.assertEqual(
            [args["message"] for args in call_args],
            ["Return exactly this line: SENTINEL:A", "Return exactly this line: SENTINEL:B"],
        )
        self.assertTrue(all(args["fork_context"] is False for args in call_args))

    def test_responses_sse_coerces_exact_child_spawn_prompts_across_lines(self):
        level_one_prompt = """
Execute a bounded concurrent two-agent Codex native subagent lifecycle.

Required sequence:
1. Spawn child A with prompt exactly this complete string: `Return exactly this line: SENTINEL:A`
2. Spawn child B with prompt exactly this complete string: `Return exactly this line: SENTINEL:B`
3. Do not wait before both children have been spawned.
"""
        event_context = {"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT}
        compatible_request_body(
            json.dumps(
                {
                    "model": "minimax-m3",
                    "input": [{"type": "message", "role": "user", "content": level_one_prompt}],
                    "tools": [
                        {"type": "function", "name": "multi_agent_v1__spawn_agent", "parameters": {"type": "object"}},
                        {"type": "function", "name": "multi_agent_v1__wait_agent", "parameters": {"type": "object"}},
                        {"type": "function", "name": "multi_agent_v1__close_agent", "parameters": {"type": "object"}},
                    ],
                }
            ).encode("utf-8"),
            {"name": "ollama_cloud", "upstream_format": "responses", "tool_protocol": "responses_structured"},
            event_context=event_context,
        )

        def convert(event):
            line = b"data: " + json.dumps(event, ensure_ascii=True, separators=(",", ":")).encode("utf-8") + b"\n\n"
            payload = compatible_sse_line(line, "ollama_cloud", event_context=event_context)
            return json.loads(payload.removeprefix(b"data: ").strip())

        convert(
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {
                    "id": "fc_a",
                    "type": "function_call",
                    "status": "in_progress",
                    "call_id": "call_a",
                    "namespace": "multi_agent_v1",
                    "name": "spawn_agent",
                    "arguments": "",
                },
            }
        )
        first_done = convert(
            {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": {
                    "id": "fc_a",
                    "type": "function_call",
                    "status": "completed",
                    "call_id": "call_a",
                    "namespace": "multi_agent_v1",
                    "name": "spawn_agent",
                    "arguments": json.dumps({"message": "Return exactly this line: SENTINEL:A", "fork_context": False}),
                },
            }
        )
        convert(
            {
                "type": "response.output_item.added",
                "output_index": 1,
                "item": {
                    "id": "fc_b",
                    "type": "function_call",
                    "status": "in_progress",
                    "call_id": "call_b",
                    "namespace": "multi_agent_v1",
                    "name": "spawn_agent",
                    "arguments": "",
                },
            }
        )
        second_done = convert(
            {
                "type": "response.output_item.done",
                "output_index": 1,
                "item": {
                    "id": "fc_b",
                    "type": "function_call",
                    "status": "completed",
                    "call_id": "call_b",
                    "namespace": "multi_agent_v1",
                    "name": "spawn_agent",
                    "arguments": json.dumps({"message": "Return exactly this line: SENTINEL:A", "fork_context": False}),
                },
            }
        )

        first_args = json.loads(first_done["item"]["arguments"])
        second_args = json.loads(second_done["item"]["arguments"])
        self.assertEqual(first_args["message"], "Return exactly this line: SENTINEL:A")
        self.assertEqual(second_args["message"], "Return exactly this line: SENTINEL:B")

    def test_responses_structured_workflow_quality_reviewer_prompt_carries_baseline_status(self):
        workflow_prompt = r"""
Use subagent-driven-development with implementer, spec reviewer, and code quality reviewer.

Coordinator inputs:
OUTPUT_PATH=C:\repo\diagnostics\subagent-e2e\level12-e2e-20260707-000000\level2-k2_7-responses.artifact-r01.txt
SENTINEL=SENTINEL:level2-k2_7-responses-20260706
MODEL_UNDER_TEST=kimi-k2.7-code
ENDPOINT_UNDER_TEST=responses
CASE=level2-k2_7-responses

Baseline git status before this E2E case started. These entries are pre-existing and must not be blamed on this diagnostic run:
```text
 M src-python/subagent_state.py
 M tests/test_subagent_state.py
```

Execution constraints:
1. The coordinator may read the plan once with node_repl.
2. Start with this ordered lifecycle: spawn one implementer, wait, close; then spawn one spec reviewer, wait, close; then spawn one code-quality reviewer, wait, close.
"""
        plan_text = r"""
# Short Subagent Development E2E Plan
OUTPUT_PATH=C:\repo\diagnostics\subagent-e2e\level12-e2e-20260707-000000\level2-k2_7-responses.artifact-r01.txt
SENTINEL=SENTINEL:level2-k2_7-responses-20260706
Task 1: create the diagnostic artifact.
"""
        body = json.dumps(
            {
                "model": "ollama-e2e-responses/kimi-k2.7-code",
                "input": [
                    {"type": "message", "role": "user", "content": workflow_prompt},
                    {
                        "type": "function_call",
                        "call_id": "call_plan",
                        "name": "mcp__node_repl__js",
                        "arguments": "{}",
                    },
                    {"type": "function_call_output", "call_id": "call_plan", "output": plan_text},
                    {
                        "type": "function_call",
                        "call_id": "call_impl_spawn",
                        "namespace": "multi_agent_v1",
                        "name": "spawn_agent",
                        "arguments": json.dumps(
                            {"message": "Implement Task 1 exactly.", "nickname": "implementer-task-1"}
                        ),
                    },
                    {"type": "function_call_output", "call_id": "call_impl_spawn", "output": json.dumps({"agent_id": "impl-1"})},
                    {
                        "type": "function_call",
                        "call_id": "call_impl_wait",
                        "namespace": "multi_agent_v1",
                        "name": "wait_agent",
                        "arguments": json.dumps({"targets": ["impl-1"], "timeout_ms": 60000}),
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_impl_wait",
                        "output": json.dumps({"status": {"impl-1": {"completed": "Status: DONE"}}}),
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_impl_close",
                        "namespace": "multi_agent_v1",
                        "name": "close_agent",
                        "arguments": json.dumps({"target": "impl-1"}),
                    },
                    {"type": "function_call_output", "call_id": "call_impl_close", "output": json.dumps({"status": "closed"})},
                    {
                        "type": "function_call",
                        "call_id": "call_spec_spawn",
                        "namespace": "multi_agent_v1",
                        "name": "spawn_agent",
                        "arguments": json.dumps(
                            {"message": "Spec compliance review for Task 1.", "nickname": "spec-reviewer-task-1"}
                        ),
                    },
                    {"type": "function_call_output", "call_id": "call_spec_spawn", "output": json.dumps({"agent_id": "spec-1"})},
                    {
                        "type": "function_call",
                        "call_id": "call_spec_wait",
                        "namespace": "multi_agent_v1",
                        "name": "wait_agent",
                        "arguments": json.dumps({"targets": ["spec-1"], "timeout_ms": 60000}),
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_spec_wait",
                        "output": json.dumps({"status": {"spec-1": {"completed": "Verdict: PASS\nChecks: ok"}}}),
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_spec_close",
                        "namespace": "multi_agent_v1",
                        "name": "close_agent",
                        "arguments": json.dumps({"target": "spec-1"}),
                    },
                    {"type": "function_call_output", "call_id": "call_spec_close", "output": json.dumps({"status": "closed"})},
                ],
                "tools": [
                    {"type": "function", "name": "multi_agent_v1__spawn_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__wait_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__close_agent", "parameters": {"type": "object"}},
                ],
            }
        ).encode("utf-8")
        event_context = {"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT}

        with patch.dict(os.environ, {"CODEXHUB_SUBAGENT_ASSIST_MODE": "assisted"}, clear=False):
            compatible_request_body(
                body,
                {"name": "ollama_cloud", "upstream_format": "responses", "tool_protocol": "responses_structured"},
                event_context=event_context,
            )

        spawn_args = event_context["subagent_required_spawn_arguments"]
        message = spawn_args["message"]
        self.assertEqual(spawn_args["nickname"], "quality-reviewer")
        self.assertIn("Baseline git status entries allowed for this case:", message)
        self.assertIn("M src-python/subagent_state.py", message)
        self.assertIn("M tests/test_subagent_state.py", message)
        self.assertIn("Do not report baseline-listed paths as product-source modifications introduced", message)

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
        event_context = {"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT}

        with patch.dict(os.environ, {"CODEXHUB_SUBAGENT_ASSIST_MODE": "assisted"}, clear=False):
            transformed = compatible_request_body(
                body,
                {"name": "ollama_cloud", "upstream_format": "responses", "tool_protocol": "responses_structured"},
                event_context=event_context,
            )

        self.assertIn("subagent_legal_actions", event_context)
        self.assertEqual(event_context["subagent_legal_actions"][0]["arguments"]["message"], "Return B")
        self.assertIn("Return B", transformed.decode("utf-8"))

    def test_responses_structured_dynamic_dag_exposes_multiple_legal_spawns_without_required_repair(self):
        body = json.dumps(
            {
                "model": "ollama-e2e-responses/minimax-m3",
                "input": [
                    {"type": "message", "role": "user", "content": "Run LEVEL3_DYNAMIC_DAG with native subagents."},
                    {
                        "type": "function_call",
                        "call_id": "call_a",
                        "namespace": "multi_agent_v1",
                        "name": "spawn_agent",
                        "arguments": json.dumps(
                            {"message": "Node: task-a-implementer", "nickname": "task-a-implementer"}
                        ),
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_a",
                        "output": json.dumps({"agent_id": "agent-a", "nickname": "task-a-implementer"}),
                    },
                    {
                        "type": "function_call",
                        "call_id": "wait_a",
                        "namespace": "multi_agent_v1",
                        "name": "wait_agent",
                        "arguments": json.dumps({"targets": ["agent-a"], "timeout_ms": 60000}),
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "wait_a",
                        "output": json.dumps({"timed_out": False, "status": {"agent-a": {"completed": "A_DONE"}}}),
                    },
                    {
                        "type": "function_call",
                        "call_id": "close_a",
                        "namespace": "multi_agent_v1",
                        "name": "close_agent",
                        "arguments": json.dumps({"target": "agent-a"}),
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "close_a",
                        "output": json.dumps({"status": "closed"}),
                    },
                ],
                "tools": [
                    {"type": "function", "name": "multi_agent_v1__spawn_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__wait_agent", "parameters": {"type": "object"}},
                    {"type": "function", "name": "multi_agent_v1__close_agent", "parameters": {"type": "object"}},
                ],
            }
        ).encode("utf-8")
        event_context = {"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT}

        with patch.dict(os.environ, {"CODEXHUB_SUBAGENT_ASSIST_MODE": "assisted"}, clear=False):
            transformed = compatible_request_body(
                body,
                {"name": "ollama_cloud", "upstream_format": "responses", "tool_protocol": "responses_structured"},
                event_context=event_context,
            )

        payload = json.loads(transformed)
        tools_by_name = {tool["name"]: tool for tool in payload["tools"] if tool.get("type") == "function"}
        transcript = json.dumps(payload, ensure_ascii=False)

        self.assertTrue(event_context["subagent_dynamic_dag_active"])
        self.assertEqual(event_context["subagent_dynamic_dag_ready_nodes"], ["task-a-reviewer", "task-b-implementer"])
        self.assertEqual(len(event_context["subagent_legal_actions"]), 2)
        self.assertNotIn("subagent_required_spawn_arguments", event_context)
        self.assertIn("multi_agent_v1__spawn_agent", tools_by_name)
        self.assertNotIn("multi_agent_v1__wait_agent", tools_by_name)
        self.assertNotIn("multi_agent_v1__close_agent", tools_by_name)
        self.assertIn("Dynamic DAG workflow state", transcript)
        self.assertIn("ready_nodes: task-a-reviewer, task-b-implementer", transcript)

    def test_dynamic_dag_duplicate_spawn_for_assigned_node_is_suppressed(self):
        event_context = {
            "request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT,
            "tool_protocol": "responses_structured",
            "subagent_spawn_allowed": True,
            "subagent_dynamic_dag_active": True,
            "subagent_legal_actions": [
                {
                    "kind": "workflow",
                    "tool_name": "spawn_agent",
                    "node_id": "task-a-reviewer",
                    "arguments": {
                        "message": "Node: task-a-reviewer",
                        "nickname": "task-a-reviewer",
                        "fork_context": False,
                    },
                }
            ],
            "subagent_assigned_dynamic_nodes": ["task-a-reviewer"],
        }
        events = [
            {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": {
                    "id": "fc_dup",
                    "type": "function_call",
                    "status": "completed",
                    "call_id": "call_dup",
                    "name": "multi_agent_v1__spawn_agent",
                    "arguments": json.dumps(
                        {"message": "Node: task-a-reviewer", "nickname": "task-a-reviewer"}
                    ),
                },
            }
        ]

        guarded, changed = codex_proxy._guard_duplicate_multi_agent_spawn_calls(events, event_context)

        self.assertTrue(changed)
        self.assertEqual(guarded[0]["item"]["type"], "message")
        self.assertIn("already assigned", guarded[0]["item"]["content"])

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
        event_context = {"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT}

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

    def test_stale_subagent_request_does_not_force_spawn_tool_for_latest_plain_user_turn(self):
        body = json.dumps(
            {
                "model": "glm-5.2",
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": "请 spawn 1 个 subagent 来检查代码",
                    },
                    {"type": "message", "role": "assistant", "content": "好的。"},
                    {"type": "message", "role": "user", "content": "test\n"},
                ],
                "tools": [],
                "stream": True,
            }
        ).encode("utf-8")
        event_context = {"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT}

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
        tool_names = [tool.get("name") for tool in payload.get("tools", []) if isinstance(tool, dict)]
        self.assertNotEqual(
            payload.get("tool_choice"),
            {"type": "function", "name": "multi_agent_v1__spawn_agent"},
        )
        self.assertIn("multi_agent_v1__spawn_agent", tool_names)
        self.assertFalse(event_context["subagent_workflow_active"])

    def test_latest_explicit_subagent_request_still_forces_spawn_tool(self):
        body = json.dumps(
            {
                "model": "glm-5.2",
                "input": [
                    {"type": "message", "role": "user", "content": "上一轮只是普通聊天。"},
                    {"type": "message", "role": "assistant", "content": "好的。"},
                    {
                        "type": "message",
                        "role": "user",
                        "content": "请 spawn 1 个 subagent 来检查代码",
                    },
                ],
                "tools": [],
                "stream": True,
            }
        ).encode("utf-8")
        event_context = {"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT}

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
        tool_names = [tool.get("name") for tool in payload.get("tools", []) if isinstance(tool, dict)]
        self.assertIn("multi_agent_v1__spawn_agent", tool_names)
        self.assertEqual(
            payload.get("tool_choice"),
            {"type": "function", "name": "multi_agent_v1__spawn_agent"},
        )
        self.assertTrue(event_context["subagent_spawn_allowed"])

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
        event_context = {"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT}

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
        event_context = {"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT}

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
        event_context = {"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT}

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
        event_context = {"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT}

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
        event_context = {"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT}

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
        event_context = {"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT}

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
        event_context = {"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT}

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
        event_context = {"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT}

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
        event_context = {"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT}

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
        event_context = {"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT}

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
        event_context = {"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT}

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
        event_context = {"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT}

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

        transformed = compatible_request_body(body, {"name": "ollama_cloud"}, event_context={"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT})
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

        transformed = compatible_request_body(body, {"name": "ollama_cloud"}, event_context={"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT})
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

        transformed = compatible_request_body(body, {"name": "ollama_cloud"}, event_context={"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT})
        payload = json.loads(transformed)
        tools_by_name = {tool["name"]: tool for tool in payload["tools"] if tool.get("type") == "function"}
        transcript = json.dumps(payload, ensure_ascii=True)

        self.assertFalse(any(tool.get("type") == "namespace" and tool.get("name") == "mcp__node_repl" for tool in payload["tools"]))
        self.assertNotIn("mcp__node_repl__js", tools_by_name)
        self.assertNotIn("mcp__node_repl__js_reset", tools_by_name)
        self.assertIn("multi_agent_v1__spawn_agent", tools_by_name)
        self.assertIn("status: single_step_complete", transcript)
        self.assertIn("required_next_action: write the final answer now", transcript)

    def test_external_browser_comments_keeps_node_repl_alias_without_browser_guidance(self):
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

        transformed = compatible_request_body(body, {"name": "ollama_cloud"}, event_context={"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT})
        payload = json.loads(transformed)
        transcript = json.dumps(payload, ensure_ascii=True)
        tools_by_name = {tool["name"]: tool for tool in payload["tools"] if tool.get("type") == "function"}

        self.assertNotIn("tool_search", tools_by_name)
        self.assertIn("mcp__node_repl__js", tools_by_name)
        self.assertIn("mcp__node_repl__js", transcript)
        self.assertNotIn("browser:control-in-app-browser", transcript)

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

        transformed = compatible_request_body(body, {"name": "ollama_cloud"}, event_context={"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT})
        payload = json.loads(transformed)
        tools_by_name = {tool["name"]: tool for tool in payload["tools"] if tool.get("type") == "function"}
        transcript = json.dumps(payload, ensure_ascii=True)

        self.assertIn("mcp__node_repl__js", tools_by_name)
        self.assertIn("mcp__node_repl__js_reset", tools_by_name)
        self.assertNotIn("browser:control-in-app-browser", transcript)
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

        transformed = compatible_request_body(body, {"name": "ollama_cloud"}, event_context={"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT})
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

        transformed = compatible_request_body(body, {"name": "ollama_cloud"}, event_context={"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT})
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

        transformed = compatible_request_body(body, {"name": "ollama_cloud"}, event_context={"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT})
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

        transformed = compatible_request_body(body, {"name": "ollama_cloud"}, event_context={"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT})
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

        transformed = compatible_request_body(body, {"name": "ollama_cloud"}, event_context={"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT})
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

        transformed = compatible_request_body(body, {"name": "ollama_cloud"}, event_context={"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT})
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

        transformed = compatible_request_body(body, {"name": "ollama_cloud"}, event_context={"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT})
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

        transformed = compatible_request_body(body, {"name": "ollama_cloud"}, event_context={"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT})
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

        transformed = compatible_request_body(body, {"name": "ollama_cloud"}, event_context={"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT})
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

        transformed = compatible_request_body(body, {"name": "ollama_cloud"}, event_context={"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT})
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

        transformed = compatible_request_body(body, {"name": "ollama_cloud"}, event_context={"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT})
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
        event_context = {"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT}

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

        transformed = compatible_request_body(body, {"name": "ollama_cloud"}, event_context={"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT})
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

        transformed = compatible_request_body(body, {"name": "ollama_cloud"}, event_context={"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT})
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

        transformed = compatible_request_body(body, {"name": "ollama_cloud"}, event_context={"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT})
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

        transformed = compatible_request_body(body, {"name": "ollama_cloud"}, event_context={"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT})
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

        transformed = compatible_response_body(body, "ollama_cloud", event_context={"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT})
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

        transformed = compatible_response_body(body, "ollama_cloud", event_context={"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT})
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

        transformed = compatible_response_body(body, "ollama_cloud", event_context={"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT})
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

        transformed = compatible_response_body(body, "ollama_cloud", event_context={"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT})
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
            transformed = compatible_response_body(body, "ollama_cloud", event_context={"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT})

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

        transformed = compatible_response_body(body, "ollama_cloud", event_context={"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT})
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
            "request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT,
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
            "request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT,
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
            "request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT,
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
        event_context = {"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT}

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
            "request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT,
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

    def test_coordinator_workflow_response_suppresses_local_tool_after_plan_read(self):
        body = json.dumps(
            {
                "output": [
                    {
                        "type": "function_call",
                        "id": "fc_plan",
                        "call_id": "call_plan",
                        "name": "update_plan",
                        "arguments": json.dumps(
                            {
                                "steps": json.dumps(
                                    [
                                        {"label": "Spawn implementer subagent", "status": "in_progress"},
                                        {"label": "Spawn spec reviewer subagent", "status": "pending"},
                                    ]
                                )
                            }
                        ),
                    }
                ]
            }
        ).encode("utf-8")
        event_context = {
            "request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT,
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
        self.assertNotIn("update_plan", transcript)

    def test_coordinator_workflow_sse_suppresses_local_tool_after_plan_read(self):
        event_context = {
            "request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT,
            "tool_protocol": "responses_structured",
            "subagent_worker_context": False,
            "subagent_wait_agent_ids": ["agent_1"],
            "subagent_close_agent_ids": [],
            "subagent_spawn_allowed": False,
            "subagent_lifecycle_complete": False,
            "subagent_workflow_active": True,
            "subagent_workflow_plan_read_complete": True,
        }
        item_in_progress = {
            "id": "fc_plan",
            "type": "function_call",
            "status": "in_progress",
            "call_id": "call_plan",
            "name": "update_plan",
            "arguments": "",
        }
        item_done = {
            **item_in_progress,
            "status": "completed",
            "arguments": json.dumps({"steps": "[]"}),
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
                    "item_id": "fc_plan",
                    "output_index": 0,
                    "arguments": json.dumps({"steps": "[]"}),
                }
            ).encode("utf-8") + b"\n",
            "ollama_cloud",
            event_context=event_context,
        )
        done = compatible_sse_line(
            b"data: " + json.dumps(
                {"type": "response.output_item.done", "output_index": 0, "item": item_done}
            ).encode("utf-8") + b"\n",
            "ollama_cloud",
            event_context=event_context,
        )

        added_payload = json.loads(added.removeprefix(b"data: "))
        done_payload = json.loads(done.removeprefix(b"data: "))

        self.assertEqual(added_payload["item"]["type"], "message")
        self.assertEqual(args_done, b"")
        self.assertEqual(done_payload["item"]["type"], "message")
        self.assertNotIn('"type": "function_call"', added.decode("utf-8"))
        self.assertNotIn('"type": "function_call"', done.decode("utf-8"))

    def test_worker_sse_suppresses_nested_multi_agent_alias_and_arguments(self):
        event_context = {
            "request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT,
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
            transformed = compatible_sse_line(line, "ollama_cloud", event_context={"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT})
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
            transformed = compatible_sse_line(line, "ollama_cloud", event_context={"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT})
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

        transformed = compatible_response_body(body, "ollama_cloud", event_context={"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT})
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

        transformed = compatible_response_body(body, "ollama_cloud", event_context={"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT})
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
        request_context = {"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT, "raw_provider_probe": True}

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
            event_context={"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT, "raw_provider_probe": True},
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
                "request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT,
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
                "request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT,
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
        event_context = {"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT}
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
        event_context = {"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT}
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
        event_context = {"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT}
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
                "request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT,
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
            "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT,
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
            "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT,
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
                "request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT,
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
            "request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT,
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
                "request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT,
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
                "request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT,
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
                "request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT,
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
            "request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT,
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
                "request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT,
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
                "request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT,
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

    def test_responses_structured_suppresses_subagent_call_after_visible_lifecycle_final(self):
        final_text = (
            "RESULT: PASS\n"
            "SENTINEL: SENTINEL:level2-m3-responses-20260706\n"
            "SUBAGENT_CHAIN: implementer,spec-reviewer,quality-reviewer"
        )
        body = json.dumps(
            {
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": final_text, "annotations": []}],
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_repeated_spawn",
                        "name": "multi_agent_v1__spawn_agent",
                        "arguments": json.dumps({"message": "repeat implementer after final"}),
                    },
                ]
            }
        ).encode("utf-8")

        transformed = compatible_response_body(
            body,
            "ollama_cloud",
            event_context={
                "request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT,
                "tool_protocol": "responses_structured",
                "subagent_open_agent_ids": [],
                "subagent_spawn_allowed": False,
                "subagent_lifecycle_complete": True,
            },
        )
        payload = json.loads(transformed)

        self.assertEqual(len(payload["output"]), 1)
        self.assertEqual(payload["output"][0]["type"], "message")
        self.assertEqual(payload["output"][0]["content"][0]["text"], final_text)
        self.assertFalse(any(item.get("type") == "function_call" for item in payload["output"]))

    def test_responses_structured_sse_suppresses_subagent_call_after_visible_lifecycle_final(self):
        final_text = (
            "RESULT: PASS\n"
            "SENTINEL: SENTINEL:level2-m3-responses-20260706\n"
            "SUBAGENT_CHAIN: implementer,spec-reviewer,quality-reviewer"
        )
        event_context = {
            "request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT,
            "tool_protocol": "responses_structured",
            "subagent_open_agent_ids": [],
            "subagent_spawn_allowed": False,
            "subagent_lifecycle_complete": True,
        }
        final_event = {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": {
                "id": "msg_final",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": final_text, "annotations": []}],
            },
        }
        spawn_added = {
            "type": "response.output_item.added",
            "output_index": 1,
            "item": {
                "id": "fc_repeat",
                "type": "function_call",
                "status": "in_progress",
                "call_id": "call_repeat",
                "name": "multi_agent_v1__spawn_agent",
                "arguments": "",
            },
        }
        spawn_arguments_done = {
            "type": "response.function_call_arguments.done",
            "item_id": "fc_repeat",
            "output_index": 1,
            "arguments": json.dumps({"message": "repeat implementer after final"}),
        }
        spawn_done = {
            "type": "response.output_item.done",
            "output_index": 1,
            "item": {
                "id": "fc_repeat",
                "type": "function_call",
                "status": "completed",
                "call_id": "call_repeat",
                "name": "multi_agent_v1__spawn_agent",
                "arguments": json.dumps({"message": "repeat implementer after final"}),
            },
        }

        transformed = [
            compatible_sse_line(
                b"data: " + json.dumps(event).encode("utf-8") + b"\n",
                "ollama_cloud",
                event_context=event_context,
            )
            for event in [final_event, spawn_added, spawn_arguments_done, spawn_done]
        ]

        self.assertNotEqual(transformed[0], b"")
        self.assertEqual(transformed[1], b"")
        self.assertEqual(transformed[2], b"")
        self.assertEqual(transformed[3], b"")

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

        transformed = compatible_response_body(body, "ollama_cloud", event_context={"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT})
        call = json.loads(transformed)["output"][0]

        self.assertEqual(call["namespace"], "mcp__node_repl")
        self.assertEqual(call["name"], "js")
        self.assertEqual(json.loads(call["arguments"])["code"], "1+1")

    def test_supported_third_party_effort_preserves_explicit_subagent_lifecycle_calls(self):
        codex_proxy._validate_reasoning_effort_for_upstream(
            {"reasoning": {"effort": "high"}},
            {"name": "ollama_cloud", "auth": "api_key"},
            "ollama-cloud/glm-5.2",
        )
        body = json.dumps(
            {
                "output": [
                    {
                        "type": "function_call",
                        "call_id": "call_spawn",
                        "name": "multi_agent_v1__spawn_agent",
                        "arguments": json.dumps({"message": "return sentinel"}),
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_wait",
                        "name": "multi_agent_v1__wait_agent",
                        "arguments": json.dumps({"targets": ["child-1"]}),
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_close",
                        "name": "multi_agent_v1__close_agent",
                        "arguments": json.dumps({"target": "child-1"}),
                    },
                ]
            }
        ).encode("utf-8")

        transformed = compatible_response_body(
            body,
            "ollama_cloud",
            event_context={"request_id": "req", "repair_policy": codex_proxy.REPAIR_NONE},
        )
        calls = json.loads(transformed)["output"]

        self.assertEqual([call["namespace"] for call in calls], ["multi_agent_v1"] * 3)
        self.assertEqual([call["name"] for call in calls], ["spawn_agent", "wait_agent", "close_agent"])

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

        transformed = compatible_response_body(body, "ollama_cloud", event_context={"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT})
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

        transformed = compatible_response_body(body, "ollama_cloud", event_context={"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT})
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

        transformed = compatible_response_body(body, "ollama_cloud", event_context={"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT})
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

        transformed = compatible_response_body(body, "ollama_cloud", event_context={"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT})
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

        transformed = compatible_sse_line(line, "ollama_cloud", event_context={"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT})
        call = json.loads(transformed.removeprefix(b"data: "))["item"]

        self.assertEqual(call["namespace"], "mcp__codex_apps__local_tool_gateway_")
        self.assertEqual(call["name"], "read_file")
        self.assertEqual(json.loads(call["arguments"])["path"], "docs/plan.md")

    def test_external_provider_empty_completed_sse_is_retryable_before_downstream_output(self):
        fake = FakeHandler()
        response = FakeSseResponse(
            [
                b'data: {"type":"response.created","response":{"id":"resp_1"}}\n\n',
                b'data: {"type":"response.completed","response":{"id":"resp_1","status":"completed","output":[],"usage":{"input_tokens":7,"output_tokens":0,"total_tokens":7}}}\n\n',
                b"",
            ]
        )

        with self.assertRaisesRegex(codex_proxy.UpstreamStreamIncompleteError, "empty completed"):
            CodexProxyHandler._relay_upstream_response(
                fake,
                response,
                "ollama_cloud",
                request_id="req-empty-retryable",
                model="glm-5.2",
                upstream_format="responses",
                inbound_format="responses",
                caller_stream=True,
                usage_capture={},
                defer_stream_errors=True,
            )

        self.assertEqual(fake.wfile.writes, [])

    def test_external_provider_reasoning_only_completed_sse_is_retryable_as_empty_output(self):
        fake = FakeHandler()
        response = FakeSseResponse(
            [
                b'data: {"type":"response.created","response":{"id":"resp_1"}}\n\n',
                b'data: {"type":"response.reasoning_text.delta","delta":"hidden reasoning"}\n\n',
                b'data: {"type":"response.completed","response":{"id":"resp_1","status":"completed","output":[],"usage":{"input_tokens":7,"output_tokens":0,"total_tokens":7}}}\n\n',
                b"",
            ]
        )

        with self.assertRaisesRegex(codex_proxy.UpstreamStreamIncompleteError, "empty completed"):
            CodexProxyHandler._relay_upstream_response(
                fake,
                response,
                "ollama_cloud",
                request_id="req-hidden-reasoning-empty",
                model="glm-5.2",
                upstream_format="responses",
                inbound_format="responses",
                caller_stream=True,
                usage_capture={},
                defer_stream_errors=True,
            )

        self.assertEqual(fake.wfile.writes, [])

    def test_external_provider_empty_completed_sse_writes_explicit_error_when_retry_exhausted(self):
        fake = FakeHandler()
        response = FakeSseResponse(
            [
                b'data: {"type":"response.created","response":{"id":"resp_1"}}\n\n',
                b'data: {"type":"response.completed","response":{"id":"resp_1","status":"completed","output":[],"usage":{"input_tokens":7,"output_tokens":0,"total_tokens":7}}}\n\n',
                b"",
            ]
        )

        with patch("codex_proxy.compatible_sse_line", wraps=codex_proxy.compatible_sse_line) as rewrite:
            status = CodexProxyHandler._relay_upstream_response(
                fake,
                response,
                "ollama_cloud",
                request_id="req-empty-final",
                model="glm-5.2",
                upstream_format="responses",
                inbound_format="responses",
                caller_stream=True,
                usage_capture={},
            )

        self.assertEqual(status, 502)
        body = b"".join(fake.wfile.writes)
        self.assertIn(b"upstream_empty_completed_response", body)
        self.assertNotIn(b'"type":"response.completed"', body)
        self.assertGreaterEqual(rewrite.call_count, 1)

    def test_external_provider_tool_call_only_sse_is_not_treated_as_empty_completed(self):
        fake = FakeHandler()
        call_item = {
            "type": "function_call",
            "call_id": "call_tool",
            "name": "multi_agent_v1__spawn_agent",
            "arguments": json.dumps({"message": "return ok"}),
        }
        response = FakeSseResponse(
            [
                b'data: {"type":"response.created","response":{"id":"resp_1"}}\n\n',
                b"data: "
                + json.dumps({"type": "response.output_item.done", "output_index": 0, "item": call_item}).encode("utf-8")
                + b"\n\n",
                b"data: "
                + json.dumps(
                    {
                        "type": "response.completed",
                        "response": {"id": "resp_1", "status": "completed", "output": [call_item]},
                    }
                ).encode("utf-8")
                + b"\n\n",
                b"",
            ]
        )

        status = CodexProxyHandler._relay_upstream_response(
            fake,
            response,
            "ollama_cloud",
            request_id="req-tool-only",
            model="glm-5.2",
            upstream_format="responses",
            inbound_format="responses",
            caller_stream=True,
            usage_capture={},
            defer_stream_errors=True,
        )

        body = b"".join(fake.wfile.writes)
        self.assertEqual(status, 200)
        self.assertIn(b"response.completed", body)
        self.assertNotIn(b"upstream_empty_completed_response", body)

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

        transformed = compatible_sse_line(line, "ollama_cloud", event_context={"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT})
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

        transformed = compatible_sse_line(line, "ollama_cloud", event_context={"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT})
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
            event_context={"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT},
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
            event_context={"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT},
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
            event_context={"request_id": "req", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT},
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
