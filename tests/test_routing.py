import os
import io
import json
import unittest
from unittest.mock import patch

import codex_proxy
from codex_proxy import (
    CodexProxyHandler,
    _filtered_response_headers,
    _is_websocket_upgrade,
    _responses_url,
    choose_upstream,
    compatible_request_body,
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


class RoutingTests(unittest.TestCase):
    def setUp(self):
        self.catalog_patch = patch(
            "codex_proxy.generated_catalog_slugs",
            return_value={
                "openai/gpt-5.5",
                "minimax-m3",
                "glm-5.2",
                "kimi-k2.7-code",
                "gemini-3-flash-preview",
                "deepseek-v4-pro",
                "deepseek-v4-flash",
                "volc/glm-5.2",
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
                "kimi-k2.7-code": {"slug": "kimi-k2.7-code", "max_output_tokens": 32768},
                "gemini-3-flash-preview": {"slug": "gemini-3-flash-preview", "max_output_tokens": 65536},
                "deepseek-v4-pro": {"slug": "deepseek-v4-pro", "max_output_tokens": 393216},
                "deepseek-v4-flash": {"slug": "deepseek-v4-flash", "max_output_tokens": 393216},
                "volc/glm-5.2": {"slug": "volc/glm-5.2", "max_output_tokens": 4096},
                "minimax-cn/minimax-m3": {"slug": "minimax-cn/minimax-m3", "max_output_tokens": 524288},
            },
        )
        self.catalog_by_slug_patch.start()
        self.addCleanup(self.catalog_by_slug_patch.stop)
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
            "alias": "minimax-cn/minimax-m3",
            "provider_alias": "minimax-cn",
            "upstream_name": "minimax_cn",
            "display_prefix": "MiniMax.cn",
            "base_url": "https://api.minimaxi.com/v1",
            "api_key": "minimax-test-token",
            "upstream_model": "minimax-m3",
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
                "minimax-cn/minimax-m3": self.minimax_external_model,
            }.get(slug),
        )
        self.external_patch.start()
        self.addCleanup(self.external_patch.stop)

    def test_gpt_routes_to_official(self):
        upstream = choose_upstream("gpt-5.5")
        self.assertEqual(upstream["name"], "official")
        self.assertEqual(upstream["auth"], "incoming")

    def test_openai_alias_routes_to_official_and_rewrites_upstream_model(self):
        upstream = choose_upstream("openai/gpt-5.5")
        self.assertEqual(upstream["name"], "official")
        self.assertEqual(upstream["auth"], "incoming")
        self.assertEqual(upstream["upstream_model"], "gpt-5.5")

        body = compatible_request_body(
            b'{"model":"openai/gpt-5.5","input":"hi"}',
            upstream,
            "openai/gpt-5.5",
        )

        self.assertEqual(json.loads(body)["model"], "gpt-5.5")

    def test_ollama_routes_to_cloud(self):
        upstream = choose_upstream("glm-5.2")
        self.assertEqual(upstream["name"], "ollama_cloud")
        self.assertEqual(upstream["auth"], "ollama_api_key")
        self.assertEqual(upstream["base_url"], "https://ollama.com/v1")

    def test_provider_prefixed_model_routes_to_external_provider(self):
        upstream = choose_upstream("volc/glm-5.2")
        self.assertEqual(upstream["name"], "volcengine")
        self.assertEqual(upstream["auth"], "api_key")
        self.assertEqual(upstream["base_url"], "https://ark.example.test/v1")
        self.assertEqual(upstream["upstream_model"], "glm-5.2")

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

    def test_ollama_body_converts_compaction_input_to_system_message(self):
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
        self.assertEqual(payload["input"][1]["role"], "system")
        self.assertIn("Earlier useful context.", payload["input"][1]["content"])
        self.assertNotIn('"type":"compaction"', transformed.decode("utf-8"))

    def test_ollama_body_converts_custom_tool_items_to_system_messages(self):
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
        self.assertEqual(payload["input"][1]["role"], "system")
        self.assertIn("[Codex tool call]", payload["input"][1]["content"])
        self.assertIn("apply_patch", payload["input"][1]["content"])
        self.assertIn("*** Begin Patch", payload["input"][1]["content"])
        self.assertEqual(payload["input"][2]["type"], "message")
        self.assertEqual(payload["input"][2]["role"], "system")
        self.assertIn("[Codex tool result]", payload["input"][2]["content"])
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
        self.assertIn("[Codex function call]", payload["input"][1]["content"])
        self.assertIn("shell_command", payload["input"][1]["content"])
        self.assertIn("[Codex function result]", payload["input"][2]["content"])
        self.assertIn("[Codex web search call]", payload["input"][3]["content"])
        self.assertIn("[Codex tool search call]", payload["input"][4]["content"])
        self.assertIn("[Codex tool search result]", payload["input"][5]["content"])
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

        self.assertEqual(transformed, body)
        self.assertEqual(json.loads(transformed)["reasoning"]["effort"], "xhigh")

    def test_official_body_keeps_compaction_input_unchanged(self):
        upstream = choose_upstream("gpt-5.5")
        body = b'{"model":"gpt-5.5","input":[{"type":"compaction","summary":"keep official shape"}]}'

        transformed = compatible_request_body(body, upstream)

        self.assertEqual(transformed, body)
        self.assertEqual(json.loads(transformed)["input"][0]["type"], "compaction")

    def test_official_body_keeps_custom_tool_items_unchanged(self):
        upstream = choose_upstream("gpt-5.5")
        body = b'{"model":"gpt-5.5","input":[{"type":"custom_tool_call","call_id":"call_1","name":"apply_patch","input":"patch"}]}'

        transformed = compatible_request_body(body, upstream)

        self.assertEqual(transformed, body)
        self.assertEqual(json.loads(transformed)["input"][0]["type"], "custom_tool_call")

    def test_external_body_rewrites_model_and_clamps_output_tokens(self):
        upstream = choose_upstream("volc/glm-5.2")
        body = b'{"model":"volc/glm-5.2","max_output_tokens":999999,"input":"hi"}'

        transformed = compatible_request_body(body, upstream)
        payload = json.loads(transformed)

        self.assertEqual(payload["model"], "glm-5.2")
        self.assertEqual(payload["max_output_tokens"], 4096)

    def test_external_body_converts_compaction_input_to_system_message(self):
        for model_id, upstream_model in (
            ("volc/glm-5.2", "glm-5.2"),
            ("minimax-cn/minimax-m3", "minimax-m3"),
        ):
            with self.subTest(model_id=model_id):
                upstream = choose_upstream(model_id)
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
                self.assertEqual(payload["input"][1]["role"], "system")
                self.assertIn("Earlier external-provider context.", payload["input"][1]["content"])
                self.assertNotIn('"type":"compaction"', transformed.decode("utf-8"))

    def test_external_body_converts_custom_tool_items_to_system_messages(self):
        for model_id, upstream_model in (
            ("volc/glm-5.2", "glm-5.2"),
            ("minimax-cn/minimax-m3", "minimax-m3"),
        ):
            with self.subTest(model_id=model_id):
                upstream = choose_upstream(model_id)
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
                self.assertEqual(payload["input"][0]["role"], "system")
                self.assertIn("[Codex tool call]", payload["input"][0]["content"])
                self.assertIn("shell_command", payload["input"][0]["content"])
                self.assertIn("rg custom_tool_call", payload["input"][0]["content"])
                self.assertEqual(payload["input"][1]["type"], "message")
                self.assertEqual(payload["input"][1]["role"], "system")
                self.assertIn("[Codex tool result]", payload["input"][1]["content"])
                self.assertIn("match", payload["input"][1]["content"])
                self.assertNotIn('"type":"custom_tool_call"', raw)
                self.assertNotIn('"type":"custom_tool_call_output"', raw)

    def test_external_body_normalizes_real_history_artifact_items(self):
        for model_id, upstream_model in (
            ("volc/glm-5.2", "glm-5.2"),
            ("minimax-cn/minimax-m3", "minimax-m3"),
        ):
            with self.subTest(model_id=model_id):
                upstream = choose_upstream(model_id)
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
                self.assertIn("[Codex function call]", payload["input"][0]["content"])
                self.assertIn("[Codex function result]", payload["input"][1]["content"])
                self.assertIn("[Codex web search call]", payload["input"][2]["content"])
                self.assertIn("[Codex tool search call]", payload["input"][3]["content"])
                self.assertIn("[Codex tool search result]", payload["input"][4]["content"])
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

    def test_official_auth_preserves_incoming_auth(self):
        upstream = choose_upstream("gpt-5.5")
        headers = upstream_headers({"Authorization": "Bearer openai-token", "Content-Type": "application/json"}, upstream)
        self.assertEqual(headers["Authorization"], "Bearer openai-token")

    def test_official_auth_ignores_ollama_api_key(self):
        upstream = choose_upstream("gpt-5.5")
        with patch.dict(os.environ, {"OLLAMA_API_KEY": "ollama-token"}, clear=False):
            headers = upstream_headers(
                {"Authorization": "Bearer openai-token", "Content-Type": "application/json"},
                upstream,
            )
        self.assertEqual(headers["Authorization"], "Bearer openai-token")
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


    def test_non_sse_relay_bulk_writes_body(self):
        handler = FakeHandler()
        body = b'{"ok":true}'
        response = FakeResponse(body)

        CodexProxyHandler._relay_upstream_response(handler, response, "official")

        self.assertEqual(handler.status, 200)
        self.assertEqual(handler.wfile.writes, [body])
        self.assertEqual(handler.wfile.flush_count, 1)
        self.assertTrue(handler.close_connection)

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
