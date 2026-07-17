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
    UpstreamStreamIncompleteError,
    _chat_completions_request_to_responses_body,
    _chat_stream_chunks_have_terminal,
    _chat_messages_to_responses_input,
    _chat_tool_choice_to_responses_tool_choice,
    _chat_tools_to_responses_tools,
    _events_to_responses_body,
    _is_compact_summary_payload,
    _normalize_usage_for_event,
    _response_body_to_chat_completion_body,
    _response_events_to_chat_stream_chunks,
    _request_kind_from_headers_and_payload,
    _responses_request_to_chat_completion_body,
    _responses_events_have_terminal,
    _strip_tools_for_compact_payload,
)


class UpstreamUrlTests(unittest.TestCase):
    def test_upstream_urls_accept_complete_responses_endpoint(self):
        upstream = {"base_url": "https://example.test/v1/responses"}

        self.assertEqual(
            codex_proxy._responses_url(upstream, "/v1/responses"),
            "https://example.test/v1/responses",
        )
        self.assertEqual(
            codex_proxy._chat_completions_url(upstream),
            "https://example.test/v1/chat/completions",
        )

    def test_upstream_urls_accept_complete_singular_response_endpoint(self):
        upstream = {"base_url": "https://example.test/v1/response"}

        self.assertEqual(
            codex_proxy._responses_url(upstream, "/v1/responses"),
            "https://example.test/v1/response",
        )
        self.assertEqual(
            codex_proxy._responses_url(upstream, "/v1/responses?cursor=abc"),
            "https://example.test/v1/response?cursor=abc",
        )
        self.assertEqual(
            codex_proxy._chat_completions_url(upstream),
            "https://example.test/v1/chat/completions",
        )

    def test_upstream_urls_do_not_duplicate_existing_version_base(self):
        upstream = {"base_url": "https://example.test/v1"}

        self.assertEqual(
            codex_proxy._responses_url(upstream, "/v1/responses"),
            "https://example.test/v1/responses",
        )
        self.assertEqual(
            codex_proxy._chat_completions_url({"base_url": "https://example.test/v2"}),
            "https://example.test/v2/chat/completions",
        )

    def test_upstream_urls_accept_complete_chat_completions_endpoint(self):
        upstream = {"base_url": "https://example.test/v2/chat/completions"}

        self.assertEqual(
            codex_proxy._chat_completions_url(upstream),
            "https://example.test/v2/chat/completions",
        )
        self.assertEqual(
            codex_proxy._responses_url(upstream, "/v1/responses"),
            "https://example.test/v2/responses",
        )

    def test_upstream_urls_default_bare_hosts_to_v1(self):
        upstream = {"base_url": "https://example.test"}

        self.assertEqual(
            codex_proxy._responses_url(upstream, "/v1/responses"),
            "https://example.test/v1/responses",
        )
        self.assertEqual(
            codex_proxy._chat_completions_url({"base_url": "https://example.test/api/coding/v3"}),
            "https://example.test/api/coding/v3/chat/completions",
        )


class ChatRequestToResponsesTests(unittest.TestCase):
    def test_basic_messages_become_input(self):
        body = json.dumps({
            "model": "gpt-5.5",
            "messages": [
                {"role": "system", "content": "You are helpful."},
                {"role": "user", "content": "Hello"},
            ],
        }).encode("utf-8")

        result = _chat_completions_request_to_responses_body(body)
        payload = json.loads(result)

        self.assertEqual(payload["model"], "gpt-5.5")
        self.assertEqual(payload["instructions"], "You are helpful.")
        self.assertEqual(len(payload["input"]), 1)
        item = payload["input"][0]
        self.assertEqual(item["type"], "message")
        self.assertEqual(item["role"], "user")
        self.assertEqual(item["content"], [{"type": "input_text", "text": "Hello"}])

    def test_max_tokens_maps_to_max_output_tokens(self):
        body = json.dumps({
            "model": "gpt-5.5",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 4096,
            "temperature": 0.5,
            "stream": True,
        }).encode("utf-8")

        payload = json.loads(_chat_completions_request_to_responses_body(body))

        self.assertEqual(payload["max_output_tokens"], 4096)
        self.assertEqual(payload["temperature"], 0.5)
        self.assertTrue(payload["stream"])

    def test_responses_to_chat_stream_requests_include_usage(self):
        body = json.dumps({
            "model": "kimi-k2.7-code",
            "input": "hello",
            "stream": True,
        }).encode("utf-8")

        payload = json.loads(_responses_request_to_chat_completion_body(body))

        self.assertEqual(payload["stream_options"], {"include_usage": True})

    def test_responses_to_chat_preserves_function_call_history(self):
        body = json.dumps(
            {
                "model": "glm-5.2",
                "input": [
                    {"type": "message", "role": "user", "content": "Use a child agent."},
                    {
                        "type": "function_call",
                        "call_id": "call_spawn",
                        "name": "multi_agent_v1__spawn_agent",
                        "arguments": json.dumps({"message": "return child-ok"}),
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_spawn",
                        "output": json.dumps({"agent_id": "019f-child"}),
                    },
                ],
                "tools": [
                    {
                        "type": "function",
                        "name": "multi_agent_v1__spawn_agent",
                        "parameters": {"type": "object", "properties": {}},
                    }
                ],
            }
        ).encode("utf-8")

        payload = json.loads(_responses_request_to_chat_completion_body(body))

        self.assertEqual(payload["messages"][0]["role"], "user")
        self.assertEqual(payload["messages"][1]["role"], "assistant")
        self.assertEqual(payload["messages"][1]["tool_calls"][0]["id"], "call_spawn")
        self.assertEqual(payload["messages"][1]["tool_calls"][0]["function"]["name"], "multi_agent_v1__spawn_agent")
        self.assertEqual(payload["messages"][2]["role"], "tool")
        self.assertEqual(payload["messages"][2]["tool_call_id"], "call_spawn")
        self.assertIn("019f-child", payload["messages"][2]["content"])

    def test_tools_convert_to_responses_format(self):
        body = json.dumps({
            "model": "gpt-5.5",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Get weather",
                    "parameters": {"type": "object", "properties": {}},
                },
            }],
            "tool_choice": {"type": "function", "function": {"name": "get_weather"}},
        }).encode("utf-8")

        payload = json.loads(_chat_completions_request_to_responses_body(body))

        self.assertEqual(payload["tools"], [{
            "type": "function",
            "name": "get_weather",
            "description": "Get weather",
            "parameters": {"type": "object", "properties": {}},
        }])
        self.assertEqual(payload["tool_choice"], {"type": "function", "name": "get_weather"})

    def test_assistant_tool_calls_become_function_call_items(self):
        body = json.dumps({
            "model": "gpt-5.5",
            "messages": [
                {"role": "user", "content": "What's the weather?"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "get_weather", "arguments": '{"city":"NYC"}'},
                    }],
                },
                {"role": "tool", "tool_call_id": "call_1", "content": "Sunny"},
            ],
        }).encode("utf-8")

        payload = json.loads(_chat_completions_request_to_responses_body(body))

        # user message + function_call + function_call_output
        self.assertEqual(len(payload["input"]), 3)
        self.assertEqual(payload["input"][1]["type"], "function_call")
        self.assertEqual(payload["input"][1]["call_id"], "call_1")
        self.assertEqual(payload["input"][1]["name"], "get_weather")
        self.assertEqual(payload["input"][2]["type"], "function_call_output")
        self.assertEqual(payload["input"][2]["call_id"], "call_1")
        self.assertEqual(payload["input"][2]["output"], "Sunny")

    def test_empty_messages_get_default_input(self):
        body = json.dumps({"model": "gpt-5.5", "messages": []}).encode("utf-8")
        payload = json.loads(_chat_completions_request_to_responses_body(body))
        self.assertEqual(len(payload["input"]), 1)
        self.assertEqual(payload["input"][0]["role"], "user")

    def test_compact_prompt_detection_strips_tools_before_conversion(self):
        payload = {
            "model": "glm-5.2",
            "messages": [
                {"role": "assistant", "content": "previous work"},
                {
                    "role": "user",
                    "content": (
                        "CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.\n"
                        "Your task is to create a detailed summary of the conversation so far.\n"
                        "Your response must include an <analysis> block followed by a <summary> block."
                    ),
                },
            ],
            "tools": [{"type": "function", "function": {"name": "Bash", "parameters": {"type": "object"}}}],
            "tool_choice": "auto",
        }

        self.assertTrue(_is_compact_summary_payload(payload, "chat_completions"))
        self.assertTrue(_strip_tools_for_compact_payload(payload))

        self.assertNotIn("tools", payload)
        self.assertNotIn("tool_choice", payload)


class CodexAppExternalResponsesToolHistoryTests(unittest.TestCase):
    def test_native_responses_preserves_completed_shell_call_as_structured_history(self):
        body = json.dumps(
            {
                "model": "glm-5.2",
                "input": [
                    {"type": "message", "role": "user", "content": "test"},
                    {
                        "type": "function_call",
                        "call_id": "call_list_skills",
                        "name": "shell_command",
                        "arguments": json.dumps(
                            {
                                "command": (
                                    'Get-ChildItem "$env:USERPROFILE\\.codex\\skills" '
                                    "-Directory | Select-Object Name"
                                )
                            }
                        ),
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_list_skills",
                        "output": "Exit code: 0\nOutput:\nask-matt\ncode-review\n",
                    },
                ],
                "tools": [
                    {
                        "type": "function",
                        "name": "shell_command",
                        "parameters": {
                            "type": "object",
                            "properties": {"command": {"type": "string"}},
                            "required": ["command"],
                        },
                    }
                ],
            }
        ).encode("utf-8")
        upstream = {
            "name": "ollama_cloud",
            "upstream_model": "glm-5.2",
            "upstream_format": "responses",
            "tool_protocol": "responses_structured",
        }

        payload = json.loads(
            codex_proxy.compatible_request_body(
                body,
                upstream,
                model_id="glm-5.2",
                behavior_profile=codex_proxy.BEHAVIOR_CODEX_APP_EXTERNAL_ADAPTER,
            )
        )

        self.assertEqual(payload["input"][1]["type"], "function_call")
        self.assertEqual(payload["input"][1]["name"], "shell_command")
        self.assertEqual(payload["input"][2]["type"], "function_call_output")
        self.assertEqual(payload["input"][2]["call_id"], "call_list_skills")
        self.assertIn("ask-matt", payload["input"][2]["output"])

    def test_unresolved_auto_tool_protocol_stays_on_text_compatibility_route(self):
        upstream = {
            "name": "ollama_cloud",
            "upstream_model": "glm-5.2",
            "upstream_format": "auto",
            "tool_protocol": "auto",
        }

        self.assertEqual(codex_proxy._external_tool_protocol(upstream), "text_compat")


class RequestKindDetectionTests(unittest.TestCase):
    def test_compact_header_marks_request_kind_without_prompt_heuristic(self):
        payload = {"model": "gpt-5.5", "input": "summarize"}

        request_kind = _request_kind_from_headers_and_payload(
            {"x-query-source": "compact"},
            payload,
            "responses",
        )

        self.assertEqual(request_kind, "compact")

    def test_compact_prompt_heuristic_marks_request_kind(self):
        payload = {
            "model": "glm-5.2",
            "messages": [{
                "role": "user",
                "content": (
                    "CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.\n"
                    "Your task is to create a detailed summary of the conversation so far.\n"
                    "Return an <analysis> block followed by a <summary> block."
                ),
            }],
        }

        request_kind = _request_kind_from_headers_and_payload({}, payload, "chat_completions")

        self.assertEqual(request_kind, "compact")


class CodexHubErrorPayloadTests(unittest.TestCase):
    def test_typed_error_payload_maps_core_categories(self):
        cases = [
            (
                "provider.request",
                {
                    "source": "external-provider",
                    "message": "model is required",
                    "status": 400,
                    "error": "ValidationError",
                    "error_type": "invalid_request_error",
                },
            ),
            (
                "provider.auth",
                {
                    "source": "external-provider",
                    "message": "invalid provider key",
                    "status": 401,
                    "error": "HTTPError",
                },
            ),
            (
                "provider.rate_limit",
                {
                    "source": "external-provider",
                    "message": "rate limited",
                    "status": 429,
                    "error": "HTTPError",
                },
            ),
            (
                "upstream.http",
                {
                    "source": "external-provider",
                    "message": "bad gateway",
                    "status": 502,
                    "exc": HTTPError("https://example.test", 502, "Bad Gateway", {}, io.BytesIO(b"")),
                },
            ),
            (
                "upstream.transport",
                {
                    "source": "external-provider",
                    "message": "connection reset",
                    "status": 502,
                    "exc": URLError(ConnectionResetError("connection reset")),
                },
            ),
            (
                "upstream.protocol",
                {
                    "source": "external-provider",
                    "message": "stream incomplete",
                    "status": 502,
                    "error": "upstream_stream_incomplete",
                    "error_type": "upstream_stream_error",
                },
            ),
            (
                "gateway.auth",
                {
                    "source": "gateway",
                    "message": "missing or invalid local Gateway client key",
                    "status": 401,
                    "error": "UnauthorizedLocalClient",
                    "error_type": "gateway_auth_error",
                },
            ),
        ]

        for expected_code, kwargs in cases:
            with self.subTest(expected_code=expected_code):
                payload = codex_proxy._codexhub_error_payload(**kwargs)
                self.assertEqual(payload["code"], expected_code)
                self.assertEqual(set(payload), {"code", "message", "source", "retryable", "details"})
                self.assertIsInstance(payload["retryable"], bool)
                self.assertEqual(payload["details"]["status"], kwargs["status"])


class DownstreamErrorMapperTests(unittest.TestCase):
    def test_chat_json_error_mapper_preserves_chat_error_object_shape(self):
        error = codex_proxy.DownstreamErrorSpec(
            inbound_format="chat_completions",
            upstream_name="official",
            status=502,
            exc=URLError(ConnectionResetError("connection reset")),
        )

        payload = codex_proxy._downstream_json_error_payload(error)

        self.assertEqual(payload["error"]["type"], "upstream_error")
        self.assertEqual(payload["error"]["code"], "URLError")
        self.assertEqual(payload["error"]["status"], 502)
        self.assertEqual(payload["error"]["upstream"], "official")

    def test_responses_sse_error_mapper_preserves_stream_error_payload_shape(self):
        error = codex_proxy.DownstreamErrorSpec(
            inbound_format="responses",
            upstream_name="official",
            status=429,
            exc=URLError(TimeoutError("upstream timed out")),
        )

        payload = codex_proxy._downstream_sse_error_payload_for_inbound_format(error)

        self.assertEqual(payload["type"], "upstream_stream_error")
        self.assertEqual(payload["status"], 502)
        self.assertEqual(payload["upstream"], "official")
        self.assertEqual(payload["error"], "URLError")
        self.assertEqual(payload["retry_owner"], "client")


class ChatToolChoiceTests(unittest.TestCase):
    def test_string_tool_choice(self):
        self.assertEqual(_chat_tool_choice_to_responses_tool_choice("auto"), "auto")
        self.assertEqual(_chat_tool_choice_to_responses_tool_choice("none"), "none")
        self.assertEqual(_chat_tool_choice_to_responses_tool_choice("required"), "required")

    def test_dict_tool_choice(self):
        result = _chat_tool_choice_to_responses_tool_choice({"type": "function", "function": {"name": "foo"}})
        self.assertEqual(result, {"type": "function", "name": "foo"})


class ChatToolsToResponsesTests(unittest.TestCase):
    def test_rejects_non_function_tools_that_cannot_cross_the_protocol_seam(self):
        with self.assertRaises(ValueError):
            _chat_tools_to_responses_tools([
                {"type": "function", "function": {"name": "foo"}},
                {"type": "other"},
            ])


class ResponseBodyToChatTests(unittest.TestCase):
    def test_text_response(self):
        body = json.dumps({
            "id": "resp_123",
            "object": "response",
            "status": "completed",
            "model": "gpt-5.5",
            "output": [{
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Hello!", "annotations": []}],
            }],
            "usage": {"input_tokens": 10, "output_tokens": 2},
        }).encode("utf-8")

        result = json.loads(_response_body_to_chat_completion_body(body))

        self.assertEqual(result["object"], "chat.completion")
        self.assertEqual(result["id"], "resp_123")
        self.assertEqual(result["model"], "gpt-5.5")
        self.assertEqual(len(result["choices"]), 1)
        choice = result["choices"][0]
        self.assertEqual(choice["message"]["role"], "assistant")
        self.assertEqual(choice["message"]["content"], "Hello!")
        self.assertEqual(choice["finish_reason"], "stop")
        self.assertEqual(result["usage"]["input_tokens"], 10)

    def test_function_call_response(self):
        body = json.dumps({
            "id": "resp_456",
            "object": "response",
            "status": "completed",
            "model": "gpt-5.5",
            "output": [{
                "type": "function_call",
                "id": "fc_call_1",
                "call_id": "call_1",
                "name": "get_weather",
                "arguments": '{"city":"NYC"}',
                "status": "completed",
            }],
        }).encode("utf-8")

        result = json.loads(_response_body_to_chat_completion_body(body))

        choice = result["choices"][0]
        self.assertEqual(choice["finish_reason"], "tool_calls")
        self.assertEqual(len(choice["message"]["tool_calls"]), 1)
        tc = choice["message"]["tool_calls"][0]
        self.assertEqual(tc["id"], "call_1")
        self.assertEqual(tc["function"]["name"], "get_weather")
        self.assertEqual(tc["function"]["arguments"], '{"city":"NYC"}')


class ResponseEventsToChatStreamTests(unittest.TestCase):
    def test_responses_events_terminal_detection_requires_completed_or_failure(self):
        self.assertFalse(_responses_events_have_terminal([]))
        self.assertFalse(_responses_events_have_terminal([
            {"type": "response.created", "response": {"id": "resp_1", "model": "gpt-5.5"}},
            {"type": "response.output_text.delta", "delta": "partial"},
        ]))
        self.assertTrue(_responses_events_have_terminal([
            {"type": "response.completed", "response": {"id": "resp_1", "model": "gpt-5.5", "output": []}},
        ]))
        self.assertTrue(_responses_events_have_terminal([
            {"type": "response.failed", "response": {"id": "resp_1", "model": "gpt-5.5"}},
        ]))

    def test_events_to_responses_body_can_require_completed_event(self):
        with self.assertRaises(UpstreamStreamIncompleteError):
            _events_to_responses_body([
                {"type": "response.created", "response": {"id": "resp_1", "model": "gpt-5.5"}},
                {"type": "response.output_text.delta", "delta": "partial"},
            ], require_completed=True)

    def test_response_events_to_chat_stream_chunks_can_require_completed_event(self):
        with self.assertRaises(UpstreamStreamIncompleteError):
            _response_events_to_chat_stream_chunks([
                {"type": "response.created", "response": {"id": "resp_1", "model": "gpt-5.5"}},
                {"type": "response.output_text.delta", "delta": "partial"},
            ], require_completed=True)

    def test_chat_stream_chunks_terminal_detection_accepts_done_or_finish_reason(self):
        self.assertFalse(_chat_stream_chunks_have_terminal([]))
        self.assertFalse(_chat_stream_chunks_have_terminal([
            {"choices": [{"index": 0, "delta": {"content": "partial"}, "finish_reason": None}]},
        ]))
        self.assertTrue(_chat_stream_chunks_have_terminal([
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        ]))
        self.assertTrue(_chat_stream_chunks_have_terminal(["[DONE]"]))

    def test_text_delta_events(self):
        events = [
            {"type": "response.created", "response": {"id": "resp_1", "model": "gpt-5.5"}},
            {"type": "response.output_text.delta", "delta": "Hello"},
            {"type": "response.output_text.delta", "delta": " world"},
            {"type": "response.completed", "response": {"id": "resp_1", "model": "gpt-5.5", "output": []}},
        ]

        chunks = _response_events_to_chat_stream_chunks(events)

        # 1 role chunk + 2 text chunks + 1 finish chunk
        self.assertEqual(len(chunks), 4)
        self.assertEqual(chunks[0]["choices"][0]["delta"], {"role": "assistant"})
        self.assertEqual(chunks[1]["choices"][0]["delta"]["content"], "Hello")
        self.assertEqual(chunks[2]["choices"][0]["delta"]["content"], " world")
        self.assertEqual(chunks[3]["choices"][0]["finish_reason"], "stop")
        self.assertIsNone(chunks[1]["choices"][0]["finish_reason"])

    def test_function_call_delta_events(self):
        events = [
            {"type": "response.created", "response": {"id": "resp_2", "model": "gpt-5.5"}},
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {
                    "id": "fc_call_1",
                    "type": "function_call",
                    "status": "in_progress",
                    "call_id": "call_1",
                    "name": "get_weather",
                    "arguments": "",
                },
            },
            {
                "type": "response.function_call_arguments.delta",
                "item_id": "fc_call_1",
                "output_index": 0,
                "delta": '{"city":',
            },
            {
                "type": "response.function_call_arguments.delta",
                "item_id": "fc_call_1",
                "output_index": 0,
                "delta": '"NYC"}',
            },
            {
                "type": "response.completed",
                "response": {
                    "id": "resp_2",
                    "model": "gpt-5.5",
                    "output": [
                        {
                            "type": "function_call",
                            "id": "fc_call_1",
                            "call_id": "call_1",
                            "name": "get_weather",
                            "arguments": '{"city":"NYC"}',
                        }
                    ],
                },
            },
        ]

        chunks = _response_events_to_chat_stream_chunks(events)

        # 1 role chunk + header chunk + 2 argument delta chunks + finish chunk
        self.assertEqual(len(chunks), 5)
        self.assertEqual(chunks[0]["choices"][0]["delta"], {"role": "assistant"})
        header = chunks[1]["choices"][0]["delta"]["tool_calls"][0]
        self.assertEqual(header["id"], "call_1")
        self.assertEqual(header["function"]["name"], "get_weather")
        self.assertEqual(chunks[2]["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"], '{"city":')
        self.assertEqual(chunks[3]["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"], '"NYC"}')
        self.assertEqual(chunks[4]["choices"][0]["finish_reason"], "tool_calls")

    def test_no_events_produces_finish_chunk(self):
        chunks = _response_events_to_chat_stream_chunks([])
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0]["choices"][0]["finish_reason"], "stop")

    def test_events_to_responses_body_preserves_completed_usage(self):
        body = _events_to_responses_body([
            {"type": "response.created", "response": {"id": "resp_usage", "model": "gpt-5.5"}},
            {"type": "response.output_text.delta", "delta": "hello"},
            {
                "type": "response.completed",
                "response": {
                    "id": "resp_usage",
                    "model": "gpt-5.5",
                    "usage": {
                        "input_tokens": 20,
                        "input_tokens_details": {"cached_tokens": 8},
                        "output_tokens": 4,
                        "output_tokens_details": {"reasoning_tokens": 1},
                        "total_tokens": 24,
                    },
                },
            },
        ])

        payload = json.loads(body)

        self.assertEqual(payload["usage"]["input_tokens"], 20)
        self.assertEqual(payload["usage"]["input_tokens_details"]["cached_tokens"], 8)


class UsageNormalizationTests(unittest.TestCase):
    def test_normalizes_responses_usage_shape_for_events(self):
        fields = _normalize_usage_for_event({
            "input_tokens": 20,
            "input_tokens_details": {"cached_tokens": 8},
            "output_tokens": 4,
            "output_tokens_details": {"reasoning_tokens": 1},
            "total_tokens": 24,
        })

        self.assertEqual(fields["usage_source"], "upstream")
        self.assertEqual(fields["usage_input_tokens"], 20)
        self.assertEqual(fields["usage_cached_input_tokens"], 8)
        self.assertEqual(fields["usage_output_tokens"], 4)
        self.assertEqual(fields["usage_reasoning_tokens"], 1)
        self.assertEqual(fields["usage_total_tokens"], 24)

    def test_normalizes_chat_usage_shape_for_events(self):
        fields = _normalize_usage_for_event({
            "prompt_tokens": 11,
            "prompt_tokens_details": {"cached_tokens": 5},
            "completion_tokens": 7,
            "completion_tokens_details": {"reasoning_tokens": 2},
            "total_tokens": 18,
        })

        self.assertEqual(fields["usage_input_tokens"], 11)
        self.assertEqual(fields["usage_cached_input_tokens"], 5)
        self.assertEqual(fields["usage_output_tokens"], 7)
        self.assertEqual(fields["usage_reasoning_tokens"], 2)

    def test_missing_usage_records_reason_without_estimate(self):
        fields = _normalize_usage_for_event(None)

        self.assertEqual(fields["usage_source"], "missing")
        self.assertEqual(fields["usage_missing_reason"], "upstream_missing_usage")
        self.assertNotIn("usage_input_tokens", fields)


class _FakeWFile:
    def __init__(self):
        self.writes = []
        self.flush_count = 0

    def write(self, data):
        self.writes.append(data)

    def flush(self):
        self.flush_count += 1


class _FakeHandler:
    def __init__(self):
        self.status = None
        self.headers = []
        self.headers_ended = False
        self.close_connection = False
        self.wfile = _FakeWFile()

    def send_response(self, status):
        self.status = status

    def send_header(self, key, value):
        self.headers.append((key, value))

    def end_headers(self):
        self.headers_ended = True


class _FakeJsonResponse:
    def __init__(self, body, status=200):
        self.status = status
        self.headers = {"Content-Type": "application/json", "Content-Length": str(len(body))}
        self._body = body
        self._read = False

    def read(self, size=-1):
        if self._read:
            return b""
        self._read = True
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _FakeSseResponse:
    status = 200

    def __init__(self, lines):
        self.headers = {
            "Content-Type": "text/event-stream; charset=utf-8",
            "Transfer-Encoding": "chunked",
            "Content-Length": "999",
        }
        self._lines = list(lines)

    def readline(self):
        line = self._lines.pop(0)
        if isinstance(line, BaseException):
            raise line
        return line

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _ObservingSseResponse(_FakeSseResponse):
    def __init__(self, lines, observations):
        super().__init__(lines)
        self._observations = dict(observations)
        self._read_count = 0

    def readline(self):
        if self._read_count in self._observations:
            self._observations[self._read_count]()
        self._read_count += 1
        return super().readline()


def _http_error(status, body=None):
    body = body or json.dumps({"error": "upstream protocol unsupported"}).encode("utf-8")
    return HTTPError(
        "https://example.test/v1/responses",
        status,
        "upstream error",
        {"Content-Type": "application/json", "Content-Length": str(len(body))},
        io.BytesIO(body),
    )


class ChatCompletionsEndpointTests(unittest.TestCase):
    """End-to-end tests for POST /v1/chat/completions through the proxy."""

    def setUp(self):
        self.runtime_proxy_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.runtime_proxy_dir.cleanup)
        self.runtime_proxy_patch = patch("codex_proxy.RUNTIME_PROXY_DIR", Path(self.runtime_proxy_dir.name))
        self.runtime_proxy_patch.start()
        self.addCleanup(self.runtime_proxy_patch.stop)
        self.catalog_patch = patch("codex_proxy.generated_catalog_slugs", return_value={"gpt-5.5"})
        self.catalog_patch.start()
        self.addCleanup(self.catalog_patch.stop)
        self.catalog_by_slug_patch = patch(
            "codex_proxy.generated_catalog_by_slug",
            return_value={"gpt-5.5": {"slug": "gpt-5.5"}},
        )
        self.catalog_by_slug_patch.start()
        self.addCleanup(self.catalog_by_slug_patch.stop)
        self.event_patch = patch("codex_proxy.write_proxy_event")
        self.write_proxy_event = self.event_patch.start()
        self.addCleanup(self.event_patch.stop)
        self.auth_patch = patch("codex_proxy.codex_access_token", return_value="fake-sub-token")
        self.auth_patch.start()
        self.addCleanup(self.auth_patch.stop)
        self.account_patch = patch("codex_proxy.codex_account_id", return_value="fake-acct-id")
        self.account_patch.start()
        self.addCleanup(self.account_patch.stop)

    def _make_handler(self, body, path="/v1/chat/completions"):
        handler = CodexProxyHandler.__new__(CodexProxyHandler)
        handler.path = path
        handler.headers = {
            "Content-Length": str(len(body)),
            "Content-Type": "application/json",
        }
        handler.rfile = io.BytesIO(body)
        handler.close_connection = False
        fake = _FakeHandler()
        handler.send_response = fake.send_response
        handler.send_header = fake.send_header
        handler.end_headers = fake.end_headers
        handler.wfile = fake.wfile
        handler._fake = fake
        return handler

    def _chat_sse_error(self, written):
        self.assertNotIn(b"event: error\n", written)
        self.assertFalse(written.rstrip().endswith(b"data: [DONE]"))
        lines = [line for line in written.split(b"\n") if line.startswith(b"data: {")]
        self.assertTrue(lines)
        payload = json.loads(lines[-1].removeprefix(b"data: "))
        self.assertIsInstance(payload.get("error"), dict)
        return payload["error"]

    def test_post_chat_completions_routes_to_official_and_injects_subscription_token(self):
        body = json.dumps({
            "model": "gpt-5.5",
            "messages": [{"role": "user", "content": "Hello"}],
            "stream": False,
        }).encode("utf-8")
        handler = self._make_handler(body)

        # Upstream returns a Responses-format body.
        upstream_body = json.dumps({
            "id": "resp_test",
            "object": "response",
            "status": "completed",
            "model": "gpt-5.5",
            "output": [{
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Hi there!", "annotations": []}],
            }],
        }).encode("utf-8")

        with patch("codex_proxy._official_urlopen", return_value=_FakeJsonResponse(upstream_body)) as mock_urlopen:
            CodexProxyHandler.do_POST(handler)

        # Verify the upstream request was sent to the official Responses endpoint.
        request = mock_urlopen.call_args.args[0]
        self.assertIn("chatgpt.com/backend-api/codex", request.full_url)
        self.assertTrue(request.full_url.endswith("/responses"))
        # Authorization should be the subscription token, not the caller's apiKey.
        self.assertEqual(request.headers.get("Authorization"), "Bearer fake-sub-token")
        # The body should be in Responses format (has "input", not "messages").
        sent_payload = json.loads(request.data)
        self.assertIn("input", sent_payload)
        self.assertNotIn("messages", sent_payload)

        # The response written to the client should be Chat Completions format.
        written = b"".join(handler.wfile.writes)
        result = json.loads(written)
        self.assertEqual(result["object"], "chat.completion")
        self.assertEqual(result["choices"][0]["message"]["content"], "Hi there!")
        self.assertEqual(result["choices"][0]["finish_reason"], "stop")
        self.assertEqual(handler._fake.status, 200)

    def test_post_chat_completions_rejects_unsupported_upstream_response_semantics(self):
        body = json.dumps({
            "model": "gpt-5.5",
            "messages": [{"role": "user", "content": "Hello"}],
            "stream": False,
        }).encode("utf-8")
        handler = self._make_handler(body)
        upstream_body = json.dumps({
            "id": "resp_reasoning",
            "object": "response",
            "status": "completed",
            "model": "gpt-5.5",
            "output": [{"type": "reasoning", "summary": [{"type": "summary_text", "text": "private"}]}],
        }).encode("utf-8")

        with patch("codex_proxy._official_urlopen", return_value=_FakeJsonResponse(upstream_body)):
            CodexProxyHandler.do_POST(handler)

        result = json.loads(b"".join(handler.wfile.writes))
        self.assertEqual(handler._fake.status, 400)
        self.assertEqual(result["error"]["type"], "unsupported_protocol_semantics")
        self.assertEqual(
            result["codexhub_error"]["details"]["failure_class"],
            codex_proxy.RETRY_FAILURE_PERMANENT,
        )
        self.assertFalse(result["codexhub_error"]["retryable"])

    def test_post_chat_completions_events_use_proxy_request_kind(self):
        body = json.dumps({
            "model": "gpt-5.5",
            "messages": [{"role": "user", "content": "Hello"}],
            "stream": False,
        }).encode("utf-8")
        handler = self._make_handler(body)
        handler.headers["x-codex-client-metadata"] = json.dumps({
            "request_kind": "turn",
            "turn_id": "turn-meta",
        })
        upstream_body = json.dumps({
            "id": "resp_test",
            "object": "response",
            "status": "completed",
            "model": "gpt-5.5",
            "output": [{
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Hi there!", "annotations": []}],
            }],
        }).encode("utf-8")

        with patch("codex_proxy._official_urlopen", return_value=_FakeJsonResponse(upstream_body)):
            CodexProxyHandler.do_POST(handler)

        events = [(call.args[0], call.kwargs) for call in self.write_proxy_event.call_args_list]
        request_start = next(fields for event, fields in events if event == "request_start")
        request_complete = next(fields for event, fields in events if event == "request_complete")
        for fields in (request_start, request_complete):
            self.assertEqual(fields["request_kind"], "main_generation")
            self.assertEqual(fields["client_request_kind"], "turn")
            self.assertEqual(fields["turn_id"], "turn-meta")
            self.assertEqual(fields["behavior_profile"], codex_proxy.BEHAVIOR_OFFICIAL_GATEWAY_COMPAT)

    def test_post_chat_completions_retries_official_connect_error_before_relaying(self):
        body = json.dumps({
            "model": "gpt-5.5",
            "messages": [{"role": "user", "content": "Hello"}],
            "stream": False,
        }).encode("utf-8")
        handler = self._make_handler(body)
        upstream_body = json.dumps({
            "id": "resp_retry",
            "object": "response",
            "status": "completed",
            "model": "gpt-5.5",
            "output": [{
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Recovered", "annotations": []}],
            }],
        }).encode("utf-8")

        with patch(
            "codex_proxy._official_urlopen",
            side_effect=[URLError(TimeoutError("connect timed out")), _FakeJsonResponse(upstream_body)],
        ) as mock_urlopen, patch("codex_proxy.time.sleep"):
            CodexProxyHandler.do_POST(handler)

        self.assertEqual(mock_urlopen.call_count, 2)
        written = b"".join(handler.wfile.writes)
        result = json.loads(written)
        self.assertEqual(result["choices"][0]["message"]["content"], "Recovered")
        self.assertEqual(handler._fake.status, 200)

    def test_provider_scoped_chat_completions_routes_short_model(self):
        policy = codex_proxy.load_policy(codex_proxy.POLICY_PATH)
        external_model = {
            "alias": "volc/glm-5.2",
            "provider_alias": "volc",
            "upstream_name": "volcengine",
            "display_prefix": "Volc",
            "base_url": "https://ark.example.test/v1",
            "api_key": "volc-test-token",
            "upstream_model": "glm-5.2",
            "upstream_format": "chat_completions",
            "priority_base": 200,
            "context_window": 1024000,
            "max_output_tokens": 4096,
            "input_modalities": ("text",),
            "context_source": "providers_toml",
            "max_output_source": "providers_toml",
        }
        body = json.dumps({
            "model": "glm-5.2",
            "messages": [{"role": "user", "content": "Hello"}],
            "stream": False,
        }).encode("utf-8")
        handler = self._make_handler(body, path="/v1/providers/volc/chat/completions")
        upstream_body = json.dumps({
            "id": "chatcmpl_test",
            "object": "chat.completion",
            "model": "glm-5.2",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "Hi"},
                "finish_reason": "stop",
            }],
        }).encode("utf-8")

        with (
            patch(
                "codex_proxy.generated_catalog_slugs",
                return_value={"gpt-5.5", "volc/glm-5.2"},
            ),
            patch(
                "codex_proxy.generated_catalog_by_slug",
                return_value={
                    "gpt-5.5": {"slug": "gpt-5.5"},
                    "volc/glm-5.2": {"slug": "volc/glm-5.2"},
                },
            ),
            patch(
                "codex_proxy.load_policy",
                return_value=replace(
                    policy,
                    allowed_provider_models=policy.allowed_provider_models + ("volc/glm-5.2",),
                ),
            ),
            patch("codex_proxy.resolve_external_model_alias", return_value=external_model),
            patch("codex_proxy.urlopen", return_value=_FakeJsonResponse(upstream_body)) as mock_urlopen,
        ):
            CodexProxyHandler.do_POST(handler)

        request = mock_urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://ark.example.test/v1/chat/completions")
        sent_payload = json.loads(request.data)
        self.assertEqual(sent_payload["model"], "glm-5.2")
        self.assertIn("messages", sent_payload)
        self.assertNotIn("input", sent_payload)

        written = b"".join(handler.wfile.writes)
        result = json.loads(written)
        self.assertEqual(result["object"], "chat.completion")
        self.assertEqual(result["choices"][0]["message"]["content"], "Hi")
        self.assertEqual(handler._fake.status, 200)

    def test_provider_scoped_chat_to_chat_transparent_path_does_not_convert_through_responses(self):
        policy = codex_proxy.load_policy(codex_proxy.POLICY_PATH)
        external_model = {
            "alias": "volc/glm-5.2",
            "provider_alias": "volc",
            "upstream_name": "volcengine",
            "display_prefix": "Volc",
            "base_url": "https://ark.example.test/v1",
            "api_key": "volc-test-token",
            "upstream_model": "glm-5.2",
            "upstream_format": "chat_completions",
            "priority_base": 200,
            "context_window": 1024000,
            "max_output_tokens": 4096,
            "input_modalities": ("text",),
            "context_source": "providers_toml",
            "max_output_source": "providers_toml",
        }
        body = json.dumps({
            "model": "glm-5.2",
            "messages": [{"role": "user", "content": "Hello"}],
            "stream": False,
        }).encode("utf-8")
        handler = self._make_handler(body, path="/v1/providers/volc/chat/completions")
        upstream_body = json.dumps({
            "id": "chatcmpl_transparent",
            "object": "chat.completion",
            "model": "glm-5.2",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "Hi"},
                "finish_reason": "stop",
            }],
        }).encode("utf-8")

        with (
            patch(
                "codex_proxy.generated_catalog_slugs",
                return_value={"gpt-5.5", "volc/glm-5.2"},
            ),
            patch(
                "codex_proxy.generated_catalog_by_slug",
                return_value={
                    "gpt-5.5": {"slug": "gpt-5.5"},
                    "volc/glm-5.2": {"slug": "volc/glm-5.2"},
                },
            ),
            patch(
                "codex_proxy.load_policy",
                return_value=replace(
                    policy,
                    allowed_provider_models=policy.allowed_provider_models + ("volc/glm-5.2",),
                ),
            ),
            patch("codex_proxy.resolve_external_model_alias", return_value=external_model),
            patch(
                "codex_proxy._chat_completions_request_to_responses_body",
                side_effect=AssertionError("chat request converted to responses"),
            ),
            patch(
                "codex_proxy._responses_request_to_chat_completion_body",
                side_effect=AssertionError("responses request converted back to chat"),
            ),
            patch("codex_proxy.compatible_request_body", side_effect=AssertionError("codex adapter ran")),
            patch("codex_proxy.urlopen", return_value=_FakeJsonResponse(upstream_body)) as mock_urlopen,
        ):
            CodexProxyHandler.do_POST(handler)

        request = mock_urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://ark.example.test/v1/chat/completions")
        sent_payload = json.loads(request.data)
        self.assertEqual(sent_payload["model"], "glm-5.2")
        self.assertIn("messages", sent_payload)
        self.assertNotIn("input", sent_payload)
        self.assertEqual(handler._fake.status, 200)
        self.assertIn(b"chatcmpl_transparent", b"".join(handler.wfile.writes))

    def test_provider_scoped_transparent_chat_rewrites_developer_role_when_upstream_intolerant(self):
        policy = codex_proxy.load_policy(codex_proxy.POLICY_PATH)
        external_model = {
            "alias": "kimi/k3",
            "provider_alias": "kimi",
            "upstream_name": "kimi",
            "display_prefix": "Kimi",
            "base_url": "https://api.kimi.example.test/coding",
            "api_key": "kimi-test-token",
            "upstream_model": "k3",
            "upstream_format": "chat_completions",
            "supports_developer_role": False,
            "priority_base": 200,
            "context_window": 1048576,
            "max_output_tokens": 32768,
            "input_modalities": ("text", "image"),
            "context_source": "providers_toml",
            "max_output_source": "providers_toml",
        }
        body = json.dumps({
            "model": "k3",
            "messages": [
                {"role": "developer", "content": "You are a coding agent."},
                {"role": "user", "content": "Hello"},
            ],
            "stream": False,
        }).encode("utf-8")
        handler = self._make_handler(body, path="/v1/providers/kimi/chat/completions")
        upstream_body = json.dumps({
            "id": "chatcmpl_developer_rewrite",
            "object": "chat.completion",
            "model": "k3",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "Hi"},
                "finish_reason": "stop",
            }],
        }).encode("utf-8")

        with (
            patch(
                "codex_proxy.generated_catalog_slugs",
                return_value={"gpt-5.5", "kimi/k3"},
            ),
            patch(
                "codex_proxy.generated_catalog_by_slug",
                return_value={
                    "gpt-5.5": {"slug": "gpt-5.5"},
                    "kimi/k3": {"slug": "kimi/k3"},
                },
            ),
            patch(
                "codex_proxy.load_policy",
                return_value=replace(
                    policy,
                    allowed_provider_models=policy.allowed_provider_models + ("kimi/k3",),
                ),
            ),
            patch("codex_proxy.resolve_external_model_alias", return_value=external_model),
            patch("codex_proxy.urlopen", return_value=_FakeJsonResponse(upstream_body)) as mock_urlopen,
        ):
            CodexProxyHandler.do_POST(handler)

        request = mock_urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://api.kimi.example.test/coding/v1/chat/completions")
        sent_payload = json.loads(request.data)
        self.assertEqual(
            sent_payload["messages"],
            [
                {"role": "system", "content": "You are a coding agent."},
                {"role": "user", "content": "Hello"},
            ],
        )
        self.assertEqual(handler._fake.status, 200)
        marker_events = [
            call.kwargs
            for call in self.write_proxy_event.call_args_list
            if call.args and call.args[0] == "developer_role_rewrite_applied"
        ]
        self.assertEqual(len(marker_events), 1)
        self.assertEqual(marker_events[0]["upstream"], "kimi")
        self.assertEqual(marker_events[0]["messages_rewritten"], 1)

    def test_provider_scoped_transparent_chat_preserves_developer_role_when_upstream_tolerant(self):
        policy = codex_proxy.load_policy(codex_proxy.POLICY_PATH)
        external_model = {
            "alias": "kimi/k3",
            "provider_alias": "kimi",
            "upstream_name": "kimi",
            "display_prefix": "Kimi",
            "base_url": "https://api.kimi.example.test/coding",
            "api_key": "kimi-test-token",
            "upstream_model": "k3",
            "upstream_format": "chat_completions",
            "priority_base": 200,
            "context_window": 1048576,
            "max_output_tokens": 32768,
            "input_modalities": ("text", "image"),
            "context_source": "providers_toml",
            "max_output_source": "providers_toml",
        }
        body = json.dumps({
            "model": "k3",
            "messages": [
                {"role": "developer", "content": "You are a coding agent."},
                {"role": "user", "content": "Hello"},
            ],
            "stream": False,
        }).encode("utf-8")
        handler = self._make_handler(body, path="/v1/providers/kimi/chat/completions")
        upstream_body = json.dumps({
            "id": "chatcmpl_developer_preserved",
            "object": "chat.completion",
            "model": "k3",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "Hi"},
                "finish_reason": "stop",
            }],
        }).encode("utf-8")

        with (
            patch(
                "codex_proxy.generated_catalog_slugs",
                return_value={"gpt-5.5", "kimi/k3"},
            ),
            patch(
                "codex_proxy.generated_catalog_by_slug",
                return_value={
                    "gpt-5.5": {"slug": "gpt-5.5"},
                    "kimi/k3": {"slug": "kimi/k3"},
                },
            ),
            patch(
                "codex_proxy.load_policy",
                return_value=replace(
                    policy,
                    allowed_provider_models=policy.allowed_provider_models + ("kimi/k3",),
                ),
            ),
            patch("codex_proxy.resolve_external_model_alias", return_value=external_model),
            patch("codex_proxy.urlopen", return_value=_FakeJsonResponse(upstream_body)) as mock_urlopen,
        ):
            CodexProxyHandler.do_POST(handler)

        sent_payload = json.loads(mock_urlopen.call_args.args[0].data)
        self.assertEqual(sent_payload["messages"][0]["role"], "developer")
        self.assertEqual(handler._fake.status, 200)
        marker_events = [
            call.kwargs
            for call in self.write_proxy_event.call_args_list
            if call.args and call.args[0] == "developer_role_rewrite_applied"
        ]
        self.assertEqual(marker_events, [])

    def _ollama_glm_external_model(self):
        return {
            "alias": "ollama-cloud/glm-5.2",
            "provider_alias": "ollama-cloud",
            "upstream_name": "ollama_cloud",
            "display_prefix": "Ollama",
            "base_url": "https://ollama.example.test/v1",
            "api_key": "ollama-test-token",
            "upstream_model": "glm-5.2",
            "upstream_format": "responses",
            "supported_reasoning_levels": ("low", "high", "xhigh", "max"),
            "default_reasoning_level": "max",
            "priority_base": 200,
            "context_window": 131072,
            "max_output_tokens": 8192,
            "input_modalities": ("text",),
            "context_source": "providers_toml",
            "max_output_source": "providers_toml",
        }

    def _run_ollama_chat_request(self, payload_fields):
        policy = codex_proxy.load_policy(codex_proxy.POLICY_PATH)
        external_model = self._ollama_glm_external_model()
        body = json.dumps(
            {
                "model": "glm-5.2",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
                **payload_fields,
            }
        ).encode("utf-8")
        handler = self._make_handler(body, path="/v1/providers/ollama-cloud/chat/completions")
        upstream_body = json.dumps({
            "id": "resp_reasoning",
            "object": "response",
            "status": "completed",
            "model": "glm-5.2",
            "output": [{
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Hi"}],
            }],
        }).encode("utf-8")
        with (
            patch(
                "codex_proxy.generated_catalog_slugs",
                return_value={"gpt-5.5", "ollama-cloud/glm-5.2"},
            ),
            patch(
                "codex_proxy.generated_catalog_by_slug",
                return_value={
                    "gpt-5.5": {"slug": "gpt-5.5"},
                    "ollama-cloud/glm-5.2": {
                        "slug": "ollama-cloud/glm-5.2",
                        "input_modalities": ["text"],
                        "supported_reasoning_levels": ["low", "high", "xhigh", "max"],
                    },
                },
            ),
            patch(
                "codex_proxy.load_policy",
                return_value=replace(
                    policy,
                    allowed_provider_models=policy.allowed_provider_models + ("ollama-cloud/glm-5.2",),
                ),
            ),
            patch("codex_proxy.resolve_external_model_alias", return_value=external_model),
            patch("codex_proxy.urlopen", return_value=_FakeJsonResponse(upstream_body)) as mock_urlopen,
        ):
            CodexProxyHandler.do_POST(handler)
        return handler, mock_urlopen

    def test_chat_template_reasoning_effort_reaches_ollama_upstream_as_responses_reasoning(self):
        handler, mock_urlopen = self._run_ollama_chat_request({
            "chat_template_kwargs": {"reasoning_effort": "max"},
            "stream_options": {"include_usage": True},
        })
        self.assertEqual(handler._fake.status, 200)
        sent_payload = json.loads(mock_urlopen.call_args.args[0].data)
        self.assertEqual(sent_payload["reasoning"], {"effort": "max"})
        self.assertNotIn("chat_template_kwargs", sent_payload)
        self.assertNotIn("stream_options", sent_payload)

    def test_chat_template_reasoning_effort_high_stays_distinct_for_ollama(self):
        handler, mock_urlopen = self._run_ollama_chat_request({
            "chat_template_kwargs": {"reasoning_effort": "high"},
        })
        self.assertEqual(handler._fake.status, 200)
        sent_payload = json.loads(mock_urlopen.call_args.args[0].data)
        self.assertEqual(sent_payload["reasoning"], {"effort": "high"})

    def test_chat_template_reasoning_effort_xhigh_aliases_to_max_for_ollama(self):
        handler, mock_urlopen = self._run_ollama_chat_request({
            "chat_template_kwargs": {"reasoning_effort": "xhigh"},
        })
        self.assertEqual(handler._fake.status, 200)
        sent_payload = json.loads(mock_urlopen.call_args.args[0].data)
        self.assertEqual(sent_payload["reasoning"], {"effort": "max"})

    def test_unmappable_chat_template_kwargs_fails_closed_400(self):
        handler, mock_urlopen = self._run_ollama_chat_request({
            "chat_template_kwargs": {"enable_thinking": True},
        })
        self.assertEqual(handler._fake.status, 400)
        mock_urlopen.assert_not_called()
        self.assertIn(b"chat_template_kwargs", b"".join(handler.wfile.writes))

    def test_stream_options_with_extra_keys_fails_closed_400(self):
        handler, mock_urlopen = self._run_ollama_chat_request({
            "chat_template_kwargs": {"reasoning_effort": "max"},
            "stream_options": {"include_usage": True, "chunk_delimiter": "\n"},
        })
        self.assertEqual(handler._fake.status, 400)
        mock_urlopen.assert_not_called()

    def test_ultra_reasoning_effort_via_chat_template_kwargs_rejected_400(self):
        handler, mock_urlopen = self._run_ollama_chat_request({
            "chat_template_kwargs": {"reasoning_effort": "ultra"},
        })
        self.assertEqual(handler._fake.status, 400)
        mock_urlopen.assert_not_called()
        self.assertIn(b"ultra", b"".join(handler.wfile.writes))

    def test_reasoning_policy_marks_provider_default_when_control_omitted(self):
        handler, mock_urlopen = self._run_ollama_chat_request({})
        self.assertEqual(handler._fake.status, 200)
        request_start = next(
            call.kwargs
            for call in self.write_proxy_event.call_args_list
            if call.args and call.args[0] == "request_start"
        )
        self.assertEqual(request_start["reasoning_policy"], "provider-default")
        sent_payload = json.loads(mock_urlopen.call_args.args[0].data)
        self.assertNotIn("reasoning", sent_payload)

    def test_reasoning_policy_marks_explicit_when_control_present(self):
        handler, _mock_urlopen = self._run_ollama_chat_request({
            "chat_template_kwargs": {"reasoning_effort": "max"},
        })
        self.assertEqual(handler._fake.status, 200)
        request_start = next(
            call.kwargs
            for call in self.write_proxy_event.call_args_list
            if call.args and call.args[0] == "request_start"
        )
        self.assertEqual(request_start["reasoning_policy"], "explicit")

    def test_explicit_third_party_standard_chat_route_is_transparent_metered(self):
        policy = codex_proxy.load_policy(codex_proxy.POLICY_PATH)
        external_model = {
            "alias": "volc/glm-5.2",
            "provider_alias": "volc",
            "upstream_name": "volcengine",
            "display_prefix": "Volc",
            "base_url": "https://ark.example.test/v1",
            "api_key": "volc-test-token",
            "upstream_model": "glm-5.2",
            "upstream_format": "chat_completions",
            "priority_base": 200,
            "context_window": 1024000,
            "max_output_tokens": 4096,
            "input_modalities": ("text",),
            "context_source": "providers_toml",
            "max_output_source": "providers_toml",
        }
        body = json.dumps({
            "model": "volc/glm-5.2",
            "messages": [{"role": "user", "content": "Hello"}],
            "stream": False,
        }).encode("utf-8")
        handler = self._make_handler(body)
        handler.headers["X-Codex-Client-Id"] = "opencode"
        upstream_body = json.dumps({
            "id": "chatcmpl_standard_transparent",
            "object": "chat.completion",
            "model": "glm-5.2",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "Hi"},
                "finish_reason": "stop",
            }],
        }).encode("utf-8")

        with (
            patch(
                "codex_proxy.generated_catalog_slugs",
                return_value={"gpt-5.5", "volc/glm-5.2"},
            ),
            patch(
                "codex_proxy.generated_catalog_by_slug",
                return_value={
                    "gpt-5.5": {"slug": "gpt-5.5"},
                    "volc/glm-5.2": {"slug": "volc/glm-5.2"},
                },
            ),
            patch(
                "codex_proxy.load_policy",
                return_value=replace(
                    policy,
                    allowed_provider_models=policy.allowed_provider_models + ("volc/glm-5.2",),
                ),
            ),
            patch("codex_proxy.resolve_external_model_alias", return_value=external_model),
            patch(
                "codex_proxy._chat_completions_request_to_responses_body",
                side_effect=AssertionError("standard transparent chat route converted to responses"),
            ),
            patch("codex_proxy.compatible_request_body", side_effect=AssertionError("codex adapter ran")),
            patch("codex_proxy.urlopen", return_value=_FakeJsonResponse(upstream_body)) as mock_urlopen,
        ):
            CodexProxyHandler.do_POST(handler)

        request = mock_urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://ark.example.test/v1/chat/completions")
        sent_payload = json.loads(request.data)
        self.assertEqual(sent_payload["model"], "glm-5.2")
        self.assertIn("messages", sent_payload)
        self.assertNotIn("input", sent_payload)
        request_start = next(
            call.kwargs for call in self.write_proxy_event.call_args_list if call.args and call.args[0] == "request_start"
        )
        self.assertEqual(request_start["behavior_profile"], codex_proxy.BEHAVIOR_THIRD_PARTY_APP_TRANSPARENT_METERED)
        self.assertEqual(request_start["wire_format_adapter"], codex_proxy.WIRE_TRANSPARENT)
        self.assertEqual(request_start["codex_semantic_adapter"], codex_proxy.CODEX_SEMANTIC_NONE)

    def test_provider_scoped_transparent_http_error_keeps_real_upstream_header(self):
        policy = codex_proxy.load_policy(codex_proxy.POLICY_PATH)
        external_model = {
            "alias": "volc/glm-5.2",
            "provider_alias": "volc",
            "upstream_name": "volcengine",
            "display_prefix": "Volc",
            "base_url": "https://ark.example.test/v1",
            "api_key": "volc-test-token",
            "upstream_model": "glm-5.2",
            "upstream_format": "chat_completions",
            "priority_base": 200,
            "context_window": 1024000,
            "max_output_tokens": 4096,
            "input_modalities": ("text",),
            "context_source": "providers_toml",
            "max_output_source": "providers_toml",
        }
        body = json.dumps({
            "model": "glm-5.2",
            "messages": [{"role": "user", "content": "Hello"}],
            "stream": False,
        }).encode("utf-8")
        handler = self._make_handler(body, path="/v1/providers/volc/chat/completions")
        error_body = json.dumps({"error": {"message": "bad request", "type": "invalid_request_error"}}).encode("utf-8")

        with (
            patch(
                "codex_proxy.generated_catalog_slugs",
                return_value={"gpt-5.5", "volc/glm-5.2"},
            ),
            patch(
                "codex_proxy.generated_catalog_by_slug",
                return_value={
                    "gpt-5.5": {"slug": "gpt-5.5"},
                    "volc/glm-5.2": {"slug": "volc/glm-5.2"},
                },
            ),
            patch(
                "codex_proxy.load_policy",
                return_value=replace(
                    policy,
                    allowed_provider_models=policy.allowed_provider_models + ("volc/glm-5.2",),
                ),
            ),
            patch("codex_proxy.resolve_external_model_alias", return_value=external_model),
            patch("codex_proxy.urlopen", side_effect=_http_error(400, error_body)),
        ):
            CodexProxyHandler.do_POST(handler)

        self.assertEqual(handler._fake.status, 400)
        self.assertEqual(dict(handler._fake.headers).get("X-Codex-Proxy-Upstream"), "volcengine")

    def test_provider_scoped_chat_transparent_path_does_not_apply_compact_tool_stripping(self):
        policy = codex_proxy.load_policy(codex_proxy.POLICY_PATH)
        external_model = {
            "alias": "volc/glm-5.2",
            "provider_alias": "volc",
            "upstream_name": "volcengine",
            "display_prefix": "Volc",
            "base_url": "https://ark.example.test/v1",
            "api_key": "volc-test-token",
            "upstream_model": "glm-5.2",
            "upstream_format": "chat_completions",
            "priority_base": 200,
            "context_window": 1024000,
            "max_output_tokens": 4096,
            "input_modalities": ("text",),
            "context_source": "providers_toml",
            "max_output_source": "providers_toml",
        }
        body = json.dumps({
            "model": "glm-5.2",
            "messages": [{
                "role": "user",
                "content": (
                    "Create a detailed summary of the conversation so far. "
                    "Do not call any tools. The summary should include <summary>."
                ),
            }],
            "tools": [{"type": "function", "function": {"name": "Bash", "parameters": {"type": "object"}}}],
            "tool_choice": "auto",
            "stream": False,
        }).encode("utf-8")
        handler = self._make_handler(body, path="/v1/providers/volc/chat/completions")
        upstream_body = json.dumps({
            "id": "chatcmpl_summary",
            "object": "chat.completion",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "summary"},
                "finish_reason": "stop",
            }],
        }).encode("utf-8")

        with (
            patch(
                "codex_proxy.generated_catalog_slugs",
                return_value={"gpt-5.5", "volc/glm-5.2"},
            ),
            patch(
                "codex_proxy.generated_catalog_by_slug",
                return_value={
                    "gpt-5.5": {"slug": "gpt-5.5"},
                    "volc/glm-5.2": {"slug": "volc/glm-5.2"},
                },
            ),
            patch(
                "codex_proxy.load_policy",
                return_value=replace(
                    policy,
                    allowed_provider_models=policy.allowed_provider_models + ("volc/glm-5.2",),
                ),
            ),
            patch("codex_proxy.resolve_external_model_alias", return_value=external_model),
            patch("codex_proxy._strip_tools_for_compact_payload", side_effect=AssertionError("compact stripping ran")),
            patch("codex_proxy.urlopen", return_value=_FakeJsonResponse(upstream_body)),
        ):
            CodexProxyHandler.do_POST(handler)

        request_start = next(
            call.kwargs for call in self.write_proxy_event.call_args_list if call.args and call.args[0] == "request_start"
        )
        self.assertEqual(request_start["request_kind"], codex_proxy.RETRY_REQUEST_MAIN_GENERATION)
        self.assertEqual(handler._fake.status, 200)

    def test_provider_scoped_chat_transparent_vision_proxy_overlay_replaces_images_when_enabled(self):
        policy = codex_proxy.load_policy(codex_proxy.POLICY_PATH)
        external_model = {
            "alias": "volc/glm-5.2",
            "provider_alias": "volc",
            "upstream_name": "volcengine",
            "display_prefix": "Volc",
            "base_url": "https://ark.example.test/v1",
            "api_key": "volc-test-token",
            "upstream_model": "glm-5.2",
            "upstream_format": "chat_completions",
            "priority_base": 200,
            "context_window": 1024000,
            "max_output_tokens": 4096,
            "input_modalities": ("text",),
            "context_source": "providers_toml",
            "max_output_source": "providers_toml",
        }
        image_url = "data:image/png;base64,e2NoYXJ0fQ=="
        body = json.dumps({
            "model": "glm-5.2",
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": "Read this chart."},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            }],
            "stream": False,
        }).encode("utf-8")
        handler = self._make_handler(body, path="/v1/providers/volc/chat/completions")
        upstream_body = json.dumps({
            "id": "chatcmpl_image_overlay",
            "object": "chat.completion",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "Chart read."},
                "finish_reason": "stop",
            }],
        }).encode("utf-8")

        with (
            patch.dict(
                "os.environ",
                {
                    "CODEX_PROXY_IMAGE_PROXY_ENABLED": "1",
                    "CODEX_PROXY_IMAGE_PROXY_MODEL": "vision-chat/m3",
                    "CODEX_PROXY_TRANSPARENT_VISION_PROXY_ENABLED": "1",
                },
                clear=False,
            ),
            patch(
                "codex_proxy.generated_catalog_slugs",
                return_value={"gpt-5.5", "volc/glm-5.2", "vision-chat/m3"},
            ),
            patch(
                "codex_proxy.generated_catalog_by_slug",
                return_value={
                    "gpt-5.5": {"slug": "gpt-5.5"},
                    "volc/glm-5.2": {"slug": "volc/glm-5.2", "input_modalities": ["text"]},
                    "vision-chat/m3": {"slug": "vision-chat/m3", "input_modalities": ["text", "image"]},
                },
            ),
            patch(
                "codex_proxy.load_policy",
                return_value=replace(
                    policy,
                    allowed_provider_models=policy.allowed_provider_models + ("volc/glm-5.2", "vision-chat/m3"),
                ),
            ),
            patch("codex_proxy.resolve_external_model_alias", return_value=external_model),
            patch("codex_proxy._image_proxy_description_for_part", return_value="A chart with rising revenue."),
            patch("codex_proxy.urlopen", return_value=_FakeJsonResponse(upstream_body)) as mock_urlopen,
        ):
            CodexProxyHandler.do_POST(handler)

        request = mock_urlopen.call_args.args[0]
        sent_payload = json.loads(request.data)
        encoded = json.dumps(sent_payload)
        self.assertNotIn(image_url, encoded)
        self.assertIn("A chart with rising revenue.", encoded)
        self.assertIn("messages", sent_payload)
        request_start = next(
            call.kwargs for call in self.write_proxy_event.call_args_list if call.args and call.args[0] == "request_start"
        )
        self.assertEqual(request_start["vision_proxy_policy"], codex_proxy.VISION_PROXY_TRANSPARENT_OVERLAY)

    def test_provider_scoped_chat_text_only_image_request_fails_closed_502(self):
        policy = codex_proxy.load_policy(codex_proxy.POLICY_PATH)
        external_model = {
            "alias": "volc/glm-5.2",
            "provider_alias": "volc",
            "upstream_name": "volcengine",
            "display_prefix": "Volc",
            "base_url": "https://ark.example.test/v1",
            "api_key": "volc-test-token",
            "upstream_model": "glm-5.2",
            "upstream_format": "chat_completions",
            "priority_base": 200,
            "context_window": 1024000,
            "max_output_tokens": 4096,
            "input_modalities": ("text",),
            "context_source": "providers_toml",
            "max_output_source": "providers_toml",
        }
        body = json.dumps({
            "model": "glm-5.2",
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": "Read this chart."},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,e2NoYXJ0fQ=="}},
                ],
            }],
            "stream": False,
        }).encode("utf-8")
        handler = self._make_handler(body, path="/v1/providers/volc/chat/completions")

        with (
            patch.dict(
                "os.environ",
                {"CODEX_PROXY_IMAGE_PROXY_ENABLED": "0"},
                clear=False,
            ),
            patch(
                "codex_proxy.generated_catalog_slugs",
                return_value={"gpt-5.5", "volc/glm-5.2"},
            ),
            patch(
                "codex_proxy.generated_catalog_by_slug",
                return_value={
                    "gpt-5.5": {"slug": "gpt-5.5"},
                    "volc/glm-5.2": {"slug": "volc/glm-5.2", "input_modalities": ["text"]},
                },
            ),
            patch(
                "codex_proxy.load_policy",
                return_value=replace(
                    policy,
                    allowed_provider_models=policy.allowed_provider_models + ("volc/glm-5.2",),
                ),
            ),
            patch("codex_proxy.resolve_external_model_alias", return_value=external_model),
            patch("codex_proxy.urlopen") as mock_urlopen,
        ):
            CodexProxyHandler.do_POST(handler)

        mock_urlopen.assert_not_called()
        self.assertEqual(handler._fake.status, 502)
        written = b"".join(handler.wfile.writes)
        self.assertIn(b"does not support image input", written)

    def test_provider_scoped_chat_text_only_image_guard_uses_global_image_proxy_switch(self):
        policy = codex_proxy.load_policy(codex_proxy.POLICY_PATH)
        external_model = {
            "alias": "volc/glm-5.2",
            "provider_alias": "volc",
            "upstream_name": "volcengine",
            "display_prefix": "Volc",
            "base_url": "https://ark.example.test/v1",
            "api_key": "volc-test-token",
            "upstream_model": "glm-5.2",
            "upstream_format": "chat_completions",
            "priority_base": 200,
            "context_window": 1024000,
            "max_output_tokens": 4096,
            "input_modalities": ("text",),
            "context_source": "providers_toml",
            "max_output_source": "providers_toml",
        }
        image_url = "data:image/png;base64,e2NoYXJ0fQ=="
        body = json.dumps({
            "model": "glm-5.2",
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": "Read this chart."},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            }],
            "stream": False,
        }).encode("utf-8")
        handler = self._make_handler(body, path="/v1/providers/volc/chat/completions")
        upstream_body = json.dumps({
            "id": "chatcmpl_image_overlay_disabled",
            "object": "chat.completion",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "No overlay."},
                "finish_reason": "stop",
            }],
        }).encode("utf-8")

        with (
            patch.dict(
                "os.environ",
                {
                    "CODEX_PROXY_IMAGE_PROXY_ENABLED": "1",
                    "CODEX_PROXY_IMAGE_PROXY_MODEL": "vision-chat/m3",
                    "CODEX_PROXY_TRANSPARENT_VISION_PROXY_ENABLED": "0",
                },
                clear=False,
            ),
            patch(
                "codex_proxy.generated_catalog_slugs",
                return_value={"gpt-5.5", "volc/glm-5.2", "vision-chat/m3"},
            ),
            patch(
                "codex_proxy.generated_catalog_by_slug",
                return_value={
                    "gpt-5.5": {"slug": "gpt-5.5"},
                    "volc/glm-5.2": {"slug": "volc/glm-5.2", "input_modalities": ["text"]},
                    "vision-chat/m3": {"slug": "vision-chat/m3", "input_modalities": ["text", "image"]},
                },
            ),
            patch(
                "codex_proxy.load_policy",
                return_value=replace(
                    policy,
                    allowed_provider_models=policy.allowed_provider_models + ("volc/glm-5.2", "vision-chat/m3"),
                ),
            ),
            patch("codex_proxy.resolve_external_model_alias", return_value=external_model),
            patch("codex_proxy._image_proxy_description_for_part", return_value="A boundary-guard chart description."),
            patch("codex_proxy.urlopen", return_value=_FakeJsonResponse(upstream_body)) as mock_urlopen,
        ):
            CodexProxyHandler.do_POST(handler)

        request = mock_urlopen.call_args.args[0]
        sent_payload = json.loads(request.data)
        encoded = json.dumps(sent_payload)
        self.assertNotIn(image_url, encoded)
        self.assertIn("A boundary-guard chart description.", encoded)
        image_text = sent_payload["messages"][0]["content"][1]["text"]
        self.assertIn('<image path="codexhub://image/', image_text)
        self.assertIn("</image>", image_text)
        request_start = next(
            call.kwargs for call in self.write_proxy_event.call_args_list if call.args and call.args[0] == "request_start"
        )
        self.assertEqual(request_start["vision_proxy_policy"], codex_proxy.VISION_PROXY_TRANSPARENT_OVERLAY)

    def test_transparent_vision_proxy_failure_still_records_request_start(self):
        policy = codex_proxy.load_policy(codex_proxy.POLICY_PATH)
        external_model = {
            "alias": "volc/glm-5.2",
            "provider_alias": "volc",
            "upstream_name": "volcengine",
            "display_prefix": "Volc",
            "base_url": "https://ark.example.test/v1",
            "api_key": "volc-test-token",
            "upstream_model": "glm-5.2",
            "upstream_format": "chat_completions",
            "priority_base": 200,
            "context_window": 1024000,
            "max_output_tokens": 4096,
            "input_modalities": ("text",),
            "context_source": "providers_toml",
            "max_output_source": "providers_toml",
        }
        body = json.dumps({
            "model": "glm-5.2",
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": "Read this chart."},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,e2NoYXJ0fQ=="}},
                ],
            }],
            "stream": False,
        }).encode("utf-8")
        handler = self._make_handler(body, path="/v1/providers/volc/chat/completions")

        with (
            patch.dict(
                "os.environ",
                {
                    "CODEX_PROXY_IMAGE_PROXY_ENABLED": "1",
                    "CODEX_PROXY_IMAGE_PROXY_MODEL": "vision-chat/m3",
                    "CODEX_PROXY_TRANSPARENT_VISION_PROXY_ENABLED": "1",
                },
                clear=False,
            ),
            patch(
                "codex_proxy.generated_catalog_slugs",
                return_value={"gpt-5.5", "volc/glm-5.2", "vision-chat/m3"},
            ),
            patch(
                "codex_proxy.generated_catalog_by_slug",
                return_value={
                    "gpt-5.5": {"slug": "gpt-5.5"},
                    "volc/glm-5.2": {"slug": "volc/glm-5.2", "input_modalities": ["text"]},
                    "vision-chat/m3": {"slug": "vision-chat/m3", "input_modalities": ["text", "image"]},
                },
            ),
            patch(
                "codex_proxy.load_policy",
                return_value=replace(
                    policy,
                    allowed_provider_models=policy.allowed_provider_models + ("volc/glm-5.2", "vision-chat/m3"),
                ),
            ),
            patch("codex_proxy.resolve_external_model_alias", return_value=external_model),
            patch("codex_proxy._image_proxy_description_for_part", side_effect=codex_proxy.ImageProxyError("vision down")),
            patch("codex_proxy.urlopen") as mock_urlopen,
        ):
            CodexProxyHandler.do_POST(handler)

        mock_urlopen.assert_not_called()
        event_names = [call.args[0] for call in self.write_proxy_event.call_args_list if call.args]
        self.assertIn("request_start", event_names)
        self.assertIn("request_error", event_names)
        self.assertLess(event_names.index("request_start"), event_names.index("request_error"))
        request_start = next(
            call.kwargs for call in self.write_proxy_event.call_args_list if call.args and call.args[0] == "request_start"
        )
        self.assertEqual(request_start["vision_proxy_policy"], codex_proxy.VISION_PROXY_TRANSPARENT_OVERLAY)
        self.assertIn("caller_request_body_hmac", request_start)
        self.assertEqual(handler._fake.status, 502)

    def test_transparent_streaming_vision_proxy_failure_writes_sse_error_after_progress(self):
        policy = codex_proxy.load_policy(codex_proxy.POLICY_PATH)
        external_model = {
            "alias": "volc/glm-5.2",
            "provider_alias": "volc",
            "upstream_name": "volcengine",
            "display_prefix": "Volc",
            "base_url": "https://ark.example.test/v1",
            "api_key": "volc-test-token",
            "upstream_model": "glm-5.2",
            "upstream_format": "chat_completions",
            "priority_base": 200,
            "context_window": 1024000,
            "max_output_tokens": 4096,
            "input_modalities": ("text",),
            "context_source": "providers_toml",
            "max_output_source": "providers_toml",
        }
        body = json.dumps({
            "model": "glm-5.2",
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": "Read this chart."},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,e3N0cmVhbS1mYWlsfQ=="}},
                ],
            }],
            "stream": True,
        }).encode("utf-8")
        handler = self._make_handler(body, path="/v1/providers/volc/chat/completions")

        with (
            patch.dict(
                "os.environ",
                {
                    "CODEX_PROXY_IMAGE_PROXY_ENABLED": "1",
                    "CODEX_PROXY_IMAGE_PROXY_MODEL": "vision-chat/m3",
                    "CODEX_PROXY_TRANSPARENT_VISION_PROXY_ENABLED": "1",
                },
                clear=False,
            ),
            patch(
                "codex_proxy.generated_catalog_slugs",
                return_value={"gpt-5.5", "volc/glm-5.2", "vision-chat/m3"},
            ),
            patch(
                "codex_proxy.generated_catalog_by_slug",
                return_value={
                    "gpt-5.5": {"slug": "gpt-5.5"},
                    "volc/glm-5.2": {"slug": "volc/glm-5.2", "input_modalities": ["text"]},
                    "vision-chat/m3": {"slug": "vision-chat/m3", "input_modalities": ["text", "image"]},
                },
            ),
            patch(
                "codex_proxy.load_policy",
                return_value=replace(
                    policy,
                    allowed_provider_models=policy.allowed_provider_models + ("volc/glm-5.2", "vision-chat/m3"),
                ),
            ),
            patch("codex_proxy.resolve_external_model_alias", return_value=external_model),
            patch("codex_proxy._image_proxy_cache_lookup", return_value=None),
            patch("codex_proxy._image_proxy_description_for_part", side_effect=codex_proxy.ImageProxyError("vision down")),
            patch("codex_proxy.urlopen") as mock_urlopen,
        ):
            CodexProxyHandler.do_POST(handler)

        mock_urlopen.assert_not_called()
        written = b"".join(handler.wfile.writes)
        self.assertIn(b'"codexhub_status":{"type":"image_proxy"', written)
        self.assertIn(b'data: {"error"', written)
        self.assertIn(b"image_proxy_error", written)
        self.assertNotIn(b'\n{"error"', written)

    def test_transparent_fallback_vision_proxy_telemetry_distinguishes_caller_and_upstream_body(self):
        policy = codex_proxy.load_policy(codex_proxy.POLICY_PATH)
        external_model = {
            "alias": "responses-only/glm-5.2",
            "provider_alias": "responses-only",
            "upstream_name": "responses_only_provider",
            "display_prefix": "ResponsesOnly",
            "base_url": "https://responses-only.example.test/v1",
            "api_key": "responses-only-token",
            "upstream_model": "glm-5.2-responses",
            "upstream_format": "responses",
            "priority_base": 200,
            "context_window": 1024000,
            "max_output_tokens": 4096,
            "input_modalities": ("text",),
            "context_source": "providers_toml",
            "max_output_source": "providers_toml",
        }
        image_url = "data:image/png;base64,e2NoYXJ0fQ=="
        body = json.dumps({
            "model": "glm-5.2",
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": "Read this chart."},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            }],
            "stream": False,
        }).encode("utf-8")
        handler = self._make_handler(body, path="/v1/providers/responses-only/chat/completions")
        upstream_body = json.dumps({
            "id": "resp_image_fallback",
            "object": "response",
            "status": "completed",
            "model": "glm-5.2-responses",
            "output": [{
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Chart read."}],
            }],
        }).encode("utf-8")

        with (
            patch.dict(
                "os.environ",
                {
                    "CODEX_PROXY_IMAGE_PROXY_ENABLED": "1",
                    "CODEX_PROXY_IMAGE_PROXY_MODEL": "vision-chat/m3",
                    "CODEX_PROXY_TRANSPARENT_VISION_PROXY_ENABLED": "1",
                },
                clear=False,
            ),
            patch(
                "codex_proxy.generated_catalog_slugs",
                return_value={"gpt-5.5", "responses-only/glm-5.2", "vision-chat/m3"},
            ),
            patch(
                "codex_proxy.generated_catalog_by_slug",
                return_value={
                    "gpt-5.5": {"slug": "gpt-5.5"},
                    "responses-only/glm-5.2": {"slug": "responses-only/glm-5.2", "input_modalities": ["text"]},
                    "vision-chat/m3": {"slug": "vision-chat/m3", "input_modalities": ["text", "image"]},
                },
            ),
            patch(
                "codex_proxy.load_policy",
                return_value=replace(
                    policy,
                    allowed_provider_models=policy.allowed_provider_models + ("responses-only/glm-5.2", "vision-chat/m3"),
                ),
            ),
            patch("codex_proxy.resolve_external_model_alias", return_value=external_model),
            patch("codex_proxy._image_proxy_description_for_part", return_value="A chart with rising revenue."),
            patch("codex_proxy.urlopen", return_value=_FakeJsonResponse(upstream_body)),
        ):
            CodexProxyHandler.do_POST(handler)

        request_start = next(
            call.kwargs for call in self.write_proxy_event.call_args_list if call.args and call.args[0] == "request_start"
        )
        request_complete = next(
            call.kwargs for call in self.write_proxy_event.call_args_list if call.args and call.args[0] == "request_complete"
        )
        for fields in (request_start, request_complete):
            self.assertIn("caller_request_body_hmac", fields)
            self.assertIn("upstream_request_body_hmac", fields)
            self.assertNotEqual(fields["caller_request_body_hmac"], fields["upstream_request_body_hmac"])
            self.assertEqual(fields["request_body_hmac"], fields["upstream_request_body_hmac"])

    def test_provider_scoped_responses_to_responses_transparent_path_does_not_run_codex_adapter(self):
        policy = codex_proxy.load_policy(codex_proxy.POLICY_PATH)
        external_model = {
            "alias": "responses-only/glm-5.2",
            "provider_alias": "responses-only",
            "upstream_name": "responses_only_provider",
            "display_prefix": "ResponsesOnly",
            "base_url": "https://responses-only.example.test/v1",
            "api_key": "responses-only-token",
            "upstream_model": "glm-5.2-responses",
            "upstream_format": "responses",
            "priority_base": 200,
            "context_window": 1024000,
            "max_output_tokens": 4096,
            "input_modalities": ("text",),
            "context_source": "providers_toml",
            "max_output_source": "providers_toml",
        }
        body = json.dumps({
            "model": "glm-5.2",
            "input": "Hello",
            "stream": False,
        }).encode("utf-8")
        handler = self._make_handler(body, path="/v1/providers/responses-only/responses")
        upstream_body = json.dumps({
            "id": "resp_transparent_provider",
            "object": "response",
            "status": "completed",
            "model": "glm-5.2-responses",
            "output": [{
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Hi from responses", "annotations": []}],
            }],
            "usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
        }).encode("utf-8")

        with (
            patch(
                "codex_proxy.generated_catalog_slugs",
                return_value={"gpt-5.5", "responses-only/glm-5.2"},
            ),
            patch(
                "codex_proxy.generated_catalog_by_slug",
                return_value={
                    "gpt-5.5": {"slug": "gpt-5.5"},
                    "responses-only/glm-5.2": {"slug": "responses-only/glm-5.2"},
                },
            ),
            patch(
                "codex_proxy.load_policy",
                return_value=replace(
                    policy,
                    allowed_provider_models=policy.allowed_provider_models + ("responses-only/glm-5.2",),
                ),
            ),
            patch("codex_proxy.resolve_external_model_alias", return_value=external_model),
            patch("codex_proxy.compatible_request_body", side_effect=AssertionError("codex adapter ran")),
            patch("codex_proxy.compatible_response_body", side_effect=AssertionError("codex response adapter ran")),
            patch("codex_proxy.urlopen", return_value=_FakeJsonResponse(upstream_body)) as mock_urlopen,
        ):
            CodexProxyHandler.do_POST(handler)

        request = mock_urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://responses-only.example.test/v1/responses")
        sent_payload = json.loads(request.data)
        self.assertEqual(sent_payload["model"], "glm-5.2-responses")
        self.assertIn("input", sent_payload)
        self.assertNotIn("messages", sent_payload)
        self.assertEqual(b"".join(handler.wfile.writes), upstream_body)
        request_start = next(
            call.kwargs for call in self.write_proxy_event.call_args_list if call.args and call.args[0] == "request_start"
        )
        self.assertEqual(request_start["behavior_profile"], codex_proxy.BEHAVIOR_THIRD_PARTY_APP_TRANSPARENT_METERED)

    def test_provider_scoped_chat_to_responses_upstream_uses_lightweight_fallback_without_codex_adapter(self):
        policy = codex_proxy.load_policy(codex_proxy.POLICY_PATH)
        external_model = {
            "alias": "responses-only/glm-5.2",
            "provider_alias": "responses-only",
            "upstream_name": "responses_only_provider",
            "display_prefix": "ResponsesOnly",
            "base_url": "https://responses-only.example.test/v1",
            "api_key": "responses-only-token",
            "upstream_model": "glm-5.2-responses",
            "upstream_format": "responses",
            "priority_base": 200,
            "context_window": 1024000,
            "max_output_tokens": 4096,
            "input_modalities": ("text",),
            "context_source": "providers_toml",
            "max_output_source": "providers_toml",
        }
        body = json.dumps({
            "model": "glm-5.2",
            "messages": [{"role": "user", "content": "Hello"}],
            "stream": False,
        }).encode("utf-8")
        handler = self._make_handler(body, path="/v1/providers/responses-only/chat/completions")
        upstream_body = json.dumps({
            "id": "resp_lightweight",
            "object": "response",
            "status": "completed",
            "model": "glm-5.2-responses",
            "output": [{
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Hi from responses"}],
            }],
            "usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
        }).encode("utf-8")

        with (
            patch(
                "codex_proxy.generated_catalog_slugs",
                return_value={"gpt-5.5", "responses-only/glm-5.2"},
            ),
            patch(
                "codex_proxy.generated_catalog_by_slug",
                return_value={
                    "gpt-5.5": {"slug": "gpt-5.5"},
                    "responses-only/glm-5.2": {"slug": "responses-only/glm-5.2"},
                },
            ),
            patch(
                "codex_proxy.load_policy",
                return_value=replace(
                    policy,
                    allowed_provider_models=policy.allowed_provider_models + ("responses-only/glm-5.2",),
                ),
            ),
            patch("codex_proxy.resolve_external_model_alias", return_value=external_model),
            patch("codex_proxy.compatible_request_body", side_effect=AssertionError("codex adapter ran")),
            patch("codex_proxy.compatible_response_body", side_effect=AssertionError("codex response adapter ran")),
            patch("codex_proxy.urlopen", return_value=_FakeJsonResponse(upstream_body)) as mock_urlopen,
        ):
            CodexProxyHandler.do_POST(handler)

        request = mock_urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://responses-only.example.test/v1/responses")
        sent_payload = json.loads(request.data)
        self.assertEqual(sent_payload["model"], "glm-5.2-responses")
        self.assertIn("input", sent_payload)
        self.assertNotIn("messages", sent_payload)
        result = json.loads(b"".join(handler.wfile.writes))
        self.assertEqual(result["object"], "chat.completion")
        self.assertEqual(result["choices"][0]["message"]["content"], "Hi from responses")

    def test_provider_scoped_responses_to_chat_non_streaming_fallback_skips_codex_response_repairs(self):
        policy = codex_proxy.load_policy(codex_proxy.POLICY_PATH)
        external_model = {
            "alias": "chat-only/glm-5.2",
            "provider_alias": "chat-only",
            "upstream_name": "chat_only_provider",
            "display_prefix": "ChatOnly",
            "base_url": "https://chat-only.example.test/v1",
            "api_key": "chat-only-token",
            "upstream_model": "glm-5.2-chat",
            "upstream_format": "chat_completions",
            "priority_base": 200,
            "context_window": 1024000,
            "max_output_tokens": 4096,
            "input_modalities": ("text",),
            "context_source": "providers_toml",
            "max_output_source": "providers_toml",
        }
        body = json.dumps({
            "model": "glm-5.2",
            "input": "Call the tool",
            "stream": False,
        }).encode("utf-8")
        handler = self._make_handler(body, path="/v1/providers/chat-only/responses")
        upstream_body = json.dumps({
            "id": "chatcmpl_tool",
            "object": "chat.completion",
            "model": "glm-5.2-chat",
            "choices": [{
                "index": 0,
                "message": {
                    "role": "assistant",
                    "tool_calls": [{
                        "id": "call_raw",
                        "type": "function",
                        "function": {"name": "raw_tool", "arguments": "{\"x\":1}"},
                    }],
                },
                "finish_reason": "tool_calls",
            }],
        }).encode("utf-8")

        with (
            patch(
                "codex_proxy.generated_catalog_slugs",
                return_value={"gpt-5.5", "chat-only/glm-5.2"},
            ),
            patch(
                "codex_proxy.generated_catalog_by_slug",
                return_value={
                    "gpt-5.5": {"slug": "gpt-5.5"},
                    "chat-only/glm-5.2": {"slug": "chat-only/glm-5.2"},
                },
            ),
            patch(
                "codex_proxy.load_policy",
                return_value=replace(
                    policy,
                    allowed_provider_models=policy.allowed_provider_models + ("chat-only/glm-5.2",),
                ),
            ),
            patch("codex_proxy.resolve_external_model_alias", return_value=external_model),
            patch("codex_proxy.compatible_response_body", side_effect=AssertionError("codex response adapter ran")),
            patch("codex_proxy._normalize_third_party_tool_call", side_effect=AssertionError("tool alias repair ran")),
            patch("codex_proxy._downgrade_invalid_third_party_tool_calls", side_effect=AssertionError("tool downgrade repair ran")),
            patch("codex_proxy._guard_duplicate_multi_agent_spawn_calls", side_effect=AssertionError("subagent repair ran")),
            patch("codex_proxy.urlopen", return_value=_FakeJsonResponse(upstream_body)),
        ):
            CodexProxyHandler.do_POST(handler)

        result = json.loads(b"".join(handler.wfile.writes))
        self.assertEqual(result["object"], "response")
        self.assertEqual(result["output"][0]["type"], "function_call")
        self.assertEqual(result["output"][0]["call_id"], "call_raw")
        self.assertEqual(result["output"][0]["name"], "raw_tool")

    def test_provider_scoped_chat_to_responses_fallback_records_usage_as_async_pending(self):
        policy = codex_proxy.load_policy(codex_proxy.POLICY_PATH)
        external_model = {
            "alias": "responses-only/glm-5.2",
            "provider_alias": "responses-only",
            "upstream_name": "responses_only_provider",
            "display_prefix": "ResponsesOnly",
            "base_url": "https://responses-only.example.test/v1",
            "api_key": "responses-only-token",
            "upstream_model": "glm-5.2-responses",
            "upstream_format": "responses",
            "priority_base": 200,
            "context_window": 1024000,
            "max_output_tokens": 4096,
            "input_modalities": ("text",),
            "context_source": "providers_toml",
            "max_output_source": "providers_toml",
        }
        body = json.dumps({
            "model": "glm-5.2",
            "messages": [{"role": "user", "content": "Hello"}],
            "stream": False,
        }).encode("utf-8")
        handler = self._make_handler(body, path="/v1/providers/responses-only/chat/completions")
        upstream_body = json.dumps({
            "id": "resp_lightweight_usage",
            "object": "response",
            "status": "completed",
            "model": "glm-5.2-responses",
            "output": [{
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Hi from responses"}],
            }],
            "usage": {"input_tokens": 5, "output_tokens": 7, "total_tokens": 12},
        }).encode("utf-8")

        with (
            patch(
                "codex_proxy.generated_catalog_slugs",
                return_value={"gpt-5.5", "responses-only/glm-5.2"},
            ),
            patch(
                "codex_proxy.generated_catalog_by_slug",
                return_value={
                    "gpt-5.5": {"slug": "gpt-5.5"},
                    "responses-only/glm-5.2": {"slug": "responses-only/glm-5.2"},
                },
            ),
            patch(
                "codex_proxy.load_policy",
                return_value=replace(
                    policy,
                    allowed_provider_models=policy.allowed_provider_models + ("responses-only/glm-5.2",),
                ),
            ),
            patch("codex_proxy.resolve_external_model_alias", return_value=external_model),
            patch("codex_proxy.urlopen", return_value=_FakeJsonResponse(upstream_body)),
        ):
            CodexProxyHandler.do_POST(handler)

        request_complete = next(
            call.kwargs for call in self.write_proxy_event.call_args_list if call.args and call.args[0] == "request_complete"
        )
        self.assertEqual(request_complete["usage_policy"], codex_proxy.USAGE_ASYNC_TAP)
        self.assertEqual(request_complete["usage_source"], "missing")
        self.assertEqual(request_complete["usage_missing_reason"], "async_usage_pending")
        self.assertNotIn("usage_input_tokens", request_complete)

    def test_provider_scoped_responses_to_chat_upstream_uses_lightweight_fallback_without_codex_adapter(self):
        policy = codex_proxy.load_policy(codex_proxy.POLICY_PATH)
        external_model = {
            "alias": "chat-only/glm-5.2",
            "provider_alias": "chat-only",
            "upstream_name": "chat_only_provider",
            "display_prefix": "ChatOnly",
            "base_url": "https://chat-only.example.test/v1",
            "api_key": "chat-only-token",
            "upstream_model": "glm-5.2-chat",
            "upstream_format": "chat_completions",
            "priority_base": 200,
            "context_window": 1024000,
            "max_output_tokens": 4096,
            "input_modalities": ("text",),
            "context_source": "providers_toml",
            "max_output_source": "providers_toml",
        }
        body = json.dumps({
            "model": "glm-5.2",
            "input": "Hello",
            "stream": False,
        }).encode("utf-8")
        handler = self._make_handler(body, path="/v1/providers/chat-only/responses")
        upstream_body = json.dumps({
            "id": "chatcmpl_lightweight",
            "object": "chat.completion",
            "model": "glm-5.2-chat",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "Hi from chat"},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
        }).encode("utf-8")

        with (
            patch(
                "codex_proxy.generated_catalog_slugs",
                return_value={"gpt-5.5", "chat-only/glm-5.2"},
            ),
            patch(
                "codex_proxy.generated_catalog_by_slug",
                return_value={
                    "gpt-5.5": {"slug": "gpt-5.5"},
                    "chat-only/glm-5.2": {"slug": "chat-only/glm-5.2"},
                },
            ),
            patch(
                "codex_proxy.load_policy",
                return_value=replace(
                    policy,
                    allowed_provider_models=policy.allowed_provider_models + ("chat-only/glm-5.2",),
                ),
            ),
            patch("codex_proxy.resolve_external_model_alias", return_value=external_model),
            patch("codex_proxy.compatible_request_body", side_effect=AssertionError("codex adapter ran")),
            patch("codex_proxy.compatible_response_body", side_effect=AssertionError("codex response adapter ran")),
            patch("codex_proxy.urlopen", return_value=_FakeJsonResponse(upstream_body)) as mock_urlopen,
        ):
            CodexProxyHandler.do_POST(handler)

        request = mock_urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://chat-only.example.test/v1/chat/completions")
        sent_payload = json.loads(request.data)
        self.assertEqual(sent_payload["model"], "glm-5.2-chat")
        self.assertIn("messages", sent_payload)
        self.assertNotIn("input", sent_payload)
        request_start = next(
            call.kwargs for call in self.write_proxy_event.call_args_list if call.args and call.args[0] == "request_start"
        )
        expected_upstream_hmac = codex_proxy.proxy_telemetry.telemetry_hmac(
            codex_proxy.RUNTIME_CODEX_DIR,
            b"body",
            request.data,
        )
        self.assertEqual(request_start["upstream_request_body_hmac"], expected_upstream_hmac)
        self.assertEqual(request_start["request_body_hmac"], expected_upstream_hmac)
        result = json.loads(b"".join(handler.wfile.writes))
        self.assertEqual(result["object"], "response")
        self.assertEqual(result["output"][0]["content"][0]["text"], "Hi from chat")

    def test_provider_scoped_responses_to_chat_streaming_fallback_skips_codex_response_repairs(self):
        policy = codex_proxy.load_policy(codex_proxy.POLICY_PATH)
        external_model = {
            "alias": "chat-only/glm-5.2",
            "provider_alias": "chat-only",
            "upstream_name": "chat_only_provider",
            "display_prefix": "ChatOnly",
            "base_url": "https://chat-only.example.test/v1",
            "api_key": "chat-only-token",
            "upstream_model": "glm-5.2-chat",
            "upstream_format": "chat_completions",
            "priority_base": 200,
            "context_window": 1024000,
            "max_output_tokens": 4096,
            "input_modalities": ("text",),
            "context_source": "providers_toml",
            "max_output_source": "providers_toml",
        }
        body = json.dumps({
            "model": "glm-5.2",
            "input": "Hello",
            "stream": True,
        }).encode("utf-8")
        handler = self._make_handler(body, path="/v1/providers/chat-only/responses")
        chat_stream = [
            b'data: {"id":"chatcmpl_stream","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"role":"assistant","content":"Hi"},"finish_reason":null}]}\n',
            b'\n',
            b'data: {"id":"chatcmpl_stream","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n',
            b'\n',
            b'data: [DONE]\n',
            b'\n',
            b'',
        ]

        with (
            patch(
                "codex_proxy.generated_catalog_slugs",
                return_value={"gpt-5.5", "chat-only/glm-5.2"},
            ),
            patch(
                "codex_proxy.generated_catalog_by_slug",
                return_value={
                    "gpt-5.5": {"slug": "gpt-5.5"},
                    "chat-only/glm-5.2": {"slug": "chat-only/glm-5.2"},
                },
            ),
            patch(
                "codex_proxy.load_policy",
                return_value=replace(
                    policy,
                    allowed_provider_models=policy.allowed_provider_models + ("chat-only/glm-5.2",),
                ),
            ),
            patch("codex_proxy.resolve_external_model_alias", return_value=external_model),
            patch("codex_proxy._normalize_third_party_tool_call", side_effect=AssertionError("tool repair ran")),
            patch("codex_proxy._downgrade_invalid_third_party_tool_calls", side_effect=AssertionError("tool repair ran")),
            patch("codex_proxy._guard_duplicate_multi_agent_spawn_calls", side_effect=AssertionError("subagent guard ran")),
            patch("codex_proxy.urlopen", return_value=_FakeSseResponse(chat_stream)),
        ):
            CodexProxyHandler.do_POST(handler)

        written = b"".join(handler.wfile.writes)
        self.assertIn(b"response.output_text.delta", written)
        self.assertIn(b"Hi", written)
        self.assertIn(b"data: [DONE]", written)

    def test_provider_scoped_responses_to_chat_streaming_fallback_does_not_retry_after_headers(self):
        policy = codex_proxy.load_policy(codex_proxy.POLICY_PATH)
        external_model = {
            "alias": "chat-only/glm-5.2",
            "provider_alias": "chat-only",
            "upstream_name": "chat_only_provider",
            "display_prefix": "ChatOnly",
            "base_url": "https://chat-only.example.test/v1",
            "api_key": "chat-only-token",
            "upstream_model": "glm-5.2-chat",
            "upstream_format": "chat_completions",
            "priority_base": 200,
            "context_window": 1024000,
            "max_output_tokens": 4096,
            "input_modalities": ("text",),
            "context_source": "providers_toml",
            "max_output_source": "providers_toml",
        }
        body = json.dumps({
            "model": "glm-5.2",
            "input": "Hello",
            "stream": True,
        }).encode("utf-8")
        handler = self._make_handler(body, path="/v1/providers/chat-only/responses")
        failed_stream = _FakeSseResponse([
            b'data: {"id":"chatcmpl_stream","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"role":"assistant","content":"partial"},"finish_reason":null}]}\n',
            b'\n',
            OSError("chat stream reset after headers"),
        ])
        successful_stream = _FakeSseResponse([
            b'data: {"id":"chatcmpl_stream_retry","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"role":"assistant","content":"retry-success"},"finish_reason":null}]}\n',
            b'\n',
            b'data: {"id":"chatcmpl_stream_retry","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n',
            b'\n',
            b'data: [DONE]\n',
            b'\n',
            b'',
        ])

        with (
            patch.dict(
                "os.environ",
                {
                    "CODEX_PROXY_AUTO_RETRY_ENABLED": "1",
                    "CODEX_PROXY_AUTO_RETRY_MAX_ATTEMPTS": "2",
                    "CODEX_PROXY_DOWNSTREAM_RETRY_NOTICE_ENABLED": "1",
                },
                clear=False,
            ),
            patch(
                "codex_proxy.generated_catalog_slugs",
                return_value={"gpt-5.5", "chat-only/glm-5.2"},
            ),
            patch(
                "codex_proxy.generated_catalog_by_slug",
                return_value={
                    "gpt-5.5": {"slug": "gpt-5.5"},
                    "chat-only/glm-5.2": {"slug": "chat-only/glm-5.2"},
                },
            ),
            patch(
                "codex_proxy.load_policy",
                return_value=replace(
                    policy,
                    allowed_provider_models=policy.allowed_provider_models + ("chat-only/glm-5.2",),
                ),
            ),
            patch("codex_proxy.resolve_external_model_alias", return_value=external_model),
            patch("codex_proxy.urlopen", side_effect=[failed_stream, successful_stream]) as mock_urlopen,
            patch("codex_proxy.time.sleep") as mock_sleep,
        ):
            CodexProxyHandler.do_POST(handler)

        self.assertEqual(mock_urlopen.call_count, 1)
        mock_sleep.assert_not_called()
        written = b"".join(handler.wfile.writes)
        self.assertNotIn(b"retry-success", written)
        event_names = [call.args[0] for call in self.write_proxy_event.call_args_list if call.args]
        self.assertNotIn("upstream_retry", event_names)
        self.assertNotIn("sse_retry_notice", event_names)

    def test_provider_scoped_chat_to_responses_streaming_fallback_emits_chat_delta_incrementally(self):
        policy = codex_proxy.load_policy(codex_proxy.POLICY_PATH)
        external_model = {
            "alias": "responses-only/glm-5.2",
            "provider_alias": "responses-only",
            "upstream_name": "responses_only_provider",
            "display_prefix": "ResponsesOnly",
            "base_url": "https://responses-only.example.test/v1",
            "api_key": "responses-only-token",
            "upstream_model": "glm-5.2-responses",
            "upstream_format": "responses",
            "priority_base": 200,
            "context_window": 1024000,
            "max_output_tokens": 4096,
            "input_modalities": ("text",),
            "context_source": "providers_toml",
            "max_output_source": "providers_toml",
        }
        body = json.dumps({
            "model": "glm-5.2",
            "messages": [{"role": "user", "content": "Hello"}],
            "stream": True,
        }).encode("utf-8")
        handler = self._make_handler(body, path="/v1/providers/responses-only/chat/completions")
        sse_lines = [
            b'data: {"type":"response.created","response":{"id":"resp_stream","model":"glm-5.2-responses"}}\n',
            b'\n',
            b'data: {"type":"response.output_text.delta","delta":"Hello"}\n',
            b'\n',
            b'data: {"type":"response.completed","response":{"id":"resp_stream","model":"glm-5.2-responses","output":[]}}\n',
            b'\n',
            b'',
        ]

        def assert_delta_written_before_completion():
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                written = b"".join(handler.wfile.writes)
                if b'"content":"Hello"' in written:
                    self.assertNotIn(b"data: [DONE]", written)
                    return
                time.sleep(0.005)
            self.fail(f"chat delta was not written before completion; wrote {b''.join(handler.wfile.writes)!r}")

        with (
            patch(
                "codex_proxy.generated_catalog_slugs",
                return_value={"gpt-5.5", "responses-only/glm-5.2"},
            ),
            patch(
                "codex_proxy.generated_catalog_by_slug",
                return_value={
                    "gpt-5.5": {"slug": "gpt-5.5"},
                    "responses-only/glm-5.2": {"slug": "responses-only/glm-5.2"},
                },
            ),
            patch(
                "codex_proxy.load_policy",
                return_value=replace(
                    policy,
                    allowed_provider_models=policy.allowed_provider_models + ("responses-only/glm-5.2",),
                ),
            ),
            patch("codex_proxy.resolve_external_model_alias", return_value=external_model),
            patch(
                "codex_proxy.urlopen",
                return_value=_ObservingSseResponse(sse_lines, {4: assert_delta_written_before_completion}),
            ),
        ):
            CodexProxyHandler.do_POST(handler)

        written = b"".join(handler.wfile.writes)
        self.assertIn(b"data: [DONE]", written)

    def test_provider_scoped_responses_to_chat_streaming_fallback_emits_response_delta_incrementally(self):
        policy = codex_proxy.load_policy(codex_proxy.POLICY_PATH)
        external_model = {
            "alias": "chat-only/glm-5.2",
            "provider_alias": "chat-only",
            "upstream_name": "chat_only_provider",
            "display_prefix": "ChatOnly",
            "base_url": "https://chat-only.example.test/v1",
            "api_key": "chat-only-token",
            "upstream_model": "glm-5.2-chat",
            "upstream_format": "chat_completions",
            "priority_base": 200,
            "context_window": 1024000,
            "max_output_tokens": 4096,
            "input_modalities": ("text",),
            "context_source": "providers_toml",
            "max_output_source": "providers_toml",
        }
        body = json.dumps({
            "model": "glm-5.2",
            "input": "Hello",
            "stream": True,
        }).encode("utf-8")
        handler = self._make_handler(body, path="/v1/providers/chat-only/responses")
        chat_stream = [
            b'data: {"id":"chatcmpl_stream","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"role":"assistant","content":"Hi"},"finish_reason":null}]}\n',
            b'\n',
            b'data: {"id":"chatcmpl_stream","object":"chat.completion.chunk","choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n',
            b'\n',
            b'data: [DONE]\n',
            b'\n',
            b'',
        ]

        def assert_delta_written_before_completion():
            deadline = time.monotonic() + 1.0
            while time.monotonic() < deadline:
                written = b"".join(handler.wfile.writes)
                if b"response.output_text.delta" in written and b'"delta":"Hi"' in written:
                    self.assertNotIn(b"response.completed", written)
                    return
                time.sleep(0.005)
            self.fail(f"response delta was not written before completion; wrote {b''.join(handler.wfile.writes)!r}")

        with (
            patch(
                "codex_proxy.generated_catalog_slugs",
                return_value={"gpt-5.5", "chat-only/glm-5.2"},
            ),
            patch(
                "codex_proxy.generated_catalog_by_slug",
                return_value={
                    "gpt-5.5": {"slug": "gpt-5.5"},
                    "chat-only/glm-5.2": {"slug": "chat-only/glm-5.2"},
                },
            ),
            patch(
                "codex_proxy.load_policy",
                return_value=replace(
                    policy,
                    allowed_provider_models=policy.allowed_provider_models + ("chat-only/glm-5.2",),
                ),
            ),
            patch("codex_proxy.resolve_external_model_alias", return_value=external_model),
            patch(
                "codex_proxy.urlopen",
                return_value=_ObservingSseResponse(chat_stream, {2: assert_delta_written_before_completion}),
            ),
        ):
            CodexProxyHandler.do_POST(handler)

        written = b"".join(handler.wfile.writes)
        self.assertIn(b"response.completed", written)

    def test_provider_scoped_responses_to_chat_streaming_fallback_preserves_tool_calls(self):
        policy = codex_proxy.load_policy(codex_proxy.POLICY_PATH)
        external_model = {
            "alias": "chat-only/glm-5.2",
            "provider_alias": "chat-only",
            "upstream_name": "chat_only_provider",
            "display_prefix": "ChatOnly",
            "base_url": "https://chat-only.example.test/v1",
            "api_key": "chat-only-token",
            "upstream_model": "glm-5.2-chat",
            "upstream_format": "chat_completions",
            "priority_base": 200,
            "context_window": 1024000,
            "max_output_tokens": 4096,
            "input_modalities": ("text",),
            "context_source": "providers_toml",
            "max_output_source": "providers_toml",
        }
        body = json.dumps({
            "model": "glm-5.2",
            "input": "Call the tool",
            "stream": True,
        }).encode("utf-8")
        handler = self._make_handler(body, path="/v1/providers/chat-only/responses")
        chat_stream = [
            b'data: {"id":"chatcmpl_tool","object":"chat.completion.chunk","model":"glm-5.2-chat","choices":[{"index":0,"delta":{"role":"assistant","tool_calls":[{"index":0,"id":"call_tool","type":"function","function":{"name":"lookup","arguments":""}}]},"finish_reason":null}]}\n',
            b'\n',
            b'data: {"id":"chatcmpl_tool","object":"chat.completion.chunk","model":"glm-5.2-chat","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"q\\":"}}]},"finish_reason":null}]}\n',
            b'\n',
            b'data: {"id":"chatcmpl_tool","object":"chat.completion.chunk","model":"glm-5.2-chat","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\\"codex\\"}"}}]},"finish_reason":"tool_calls"}]}\n',
            b'\n',
            b'data: [DONE]\n',
            b'\n',
            b'',
        ]

        with (
            patch(
                "codex_proxy.generated_catalog_slugs",
                return_value={"gpt-5.5", "chat-only/glm-5.2"},
            ),
            patch(
                "codex_proxy.generated_catalog_by_slug",
                return_value={
                    "gpt-5.5": {"slug": "gpt-5.5"},
                    "chat-only/glm-5.2": {"slug": "chat-only/glm-5.2"},
                },
            ),
            patch(
                "codex_proxy.load_policy",
                return_value=replace(
                    policy,
                    allowed_provider_models=policy.allowed_provider_models + ("chat-only/glm-5.2",),
                ),
            ),
            patch("codex_proxy.resolve_external_model_alias", return_value=external_model),
            patch("codex_proxy.urlopen", return_value=_FakeSseResponse(chat_stream)),
        ):
            CodexProxyHandler.do_POST(handler)

        written = b"".join(handler.wfile.writes)
        self.assertIn(b"response.output_item.added", written)
        self.assertIn(b"response.function_call_arguments.delta", written)
        self.assertIn(b"response.function_call_arguments.done", written)
        self.assertIn(b"response.output_item.done", written)
        self.assertIn(b'"call_id":"call_tool"', written)
        self.assertIn(b'"name":"lookup"', written)
        self.assertIn(b'"arguments":"{\\"q\\":\\"codex\\"}"', written)
        self.assertIn(b"data: [DONE]", written)

    def test_provider_scoped_responses_to_chat_streaming_fallback_converts_chat_error_payload(self):
        policy = codex_proxy.load_policy(codex_proxy.POLICY_PATH)
        external_model = {
            "alias": "chat-only/glm-5.2",
            "provider_alias": "chat-only",
            "upstream_name": "chat_only_provider",
            "display_prefix": "ChatOnly",
            "base_url": "https://chat-only.example.test/v1",
            "api_key": "chat-only-token",
            "upstream_model": "glm-5.2-chat",
            "upstream_format": "chat_completions",
            "priority_base": 200,
            "context_window": 1024000,
            "max_output_tokens": 4096,
            "input_modalities": ("text",),
            "context_source": "providers_toml",
            "max_output_source": "providers_toml",
        }
        body = json.dumps({
            "model": "glm-5.2",
            "input": "Hello",
            "stream": True,
        }).encode("utf-8")
        handler = self._make_handler(body, path="/v1/providers/chat-only/responses")
        chat_stream = [
            b'data: {"error":{"message":"provider busy","code":"busy"}}\n',
            b'\n',
            b'',
        ]

        with (
            patch(
                "codex_proxy.generated_catalog_slugs",
                return_value={"gpt-5.5", "chat-only/glm-5.2"},
            ),
            patch(
                "codex_proxy.generated_catalog_by_slug",
                return_value={
                    "gpt-5.5": {"slug": "gpt-5.5"},
                    "chat-only/glm-5.2": {"slug": "chat-only/glm-5.2"},
                },
            ),
            patch(
                "codex_proxy.load_policy",
                return_value=replace(
                    policy,
                    allowed_provider_models=policy.allowed_provider_models + ("chat-only/glm-5.2",),
                ),
            ),
            patch("codex_proxy.resolve_external_model_alias", return_value=external_model),
            patch("codex_proxy.urlopen", return_value=_FakeSseResponse(chat_stream)),
        ):
            CodexProxyHandler.do_POST(handler)

        written = b"".join(handler.wfile.writes)
        self.assertIn(b"event: error", written)
        self.assertIn(b"provider busy", written)
        self.assertNotIn(b"stream_incomplete", written)

    def test_provider_scoped_chat_to_responses_streaming_fallback_converts_responses_failure(self):
        policy = codex_proxy.load_policy(codex_proxy.POLICY_PATH)
        external_model = {
            "alias": "responses-only/glm-5.2",
            "provider_alias": "responses-only",
            "upstream_name": "responses_only_provider",
            "display_prefix": "ResponsesOnly",
            "base_url": "https://responses-only.example.test/v1",
            "api_key": "responses-only-token",
            "upstream_model": "glm-5.2-responses",
            "upstream_format": "responses",
            "priority_base": 200,
            "context_window": 1024000,
            "max_output_tokens": 4096,
            "input_modalities": ("text",),
            "context_source": "providers_toml",
            "max_output_source": "providers_toml",
        }
        body = json.dumps({
            "model": "glm-5.2",
            "messages": [{"role": "user", "content": "Hello"}],
            "stream": True,
        }).encode("utf-8")
        handler = self._make_handler(body, path="/v1/providers/responses-only/chat/completions")
        responses_stream = [
            b'data: {"type":"response.failed","response":{"id":"resp_failed","status":"failed","error":{"code":"busy","message":"provider busy"}}}\n',
            b'\n',
            b'',
        ]

        with (
            patch(
                "codex_proxy.generated_catalog_slugs",
                return_value={"gpt-5.5", "responses-only/glm-5.2"},
            ),
            patch(
                "codex_proxy.generated_catalog_by_slug",
                return_value={
                    "gpt-5.5": {"slug": "gpt-5.5"},
                    "responses-only/glm-5.2": {"slug": "responses-only/glm-5.2"},
                },
            ),
            patch(
                "codex_proxy.load_policy",
                return_value=replace(
                    policy,
                    allowed_provider_models=policy.allowed_provider_models + ("responses-only/glm-5.2",),
                ),
            ),
            patch("codex_proxy.resolve_external_model_alias", return_value=external_model),
            patch("codex_proxy.urlopen", return_value=_FakeSseResponse(responses_stream)),
        ):
            CodexProxyHandler.do_POST(handler)

        written = b"".join(handler.wfile.writes)
        self.assertIn(b'"error"', written)
        self.assertIn(b"provider busy", written)
        self.assertNotIn(b"stream_incomplete", written)
        self.assertNotIn(b"data: [DONE]", written)

    def test_provider_scoped_chat_completions_requires_model(self):
        body = json.dumps({
            "messages": [{"role": "user", "content": "Hello"}],
            "stream": False,
        }).encode("utf-8")
        handler = self._make_handler(body, path="/v1/providers/volc/chat/completions")

        with patch("codex_proxy.urlopen") as mock_urlopen:
            CodexProxyHandler.do_POST(handler)

        mock_urlopen.assert_not_called()
        written = b"".join(handler.wfile.writes)
        result = json.loads(written)
        self.assertIn("model is required", result["error"]["message"])
        self.assertEqual(result["error"]["type"], "invalid_request_error")
        self.assertEqual(result["codexhub_error"]["code"], "provider.request")
        self.assertEqual(result["codexhub_error"]["message"], "model is required for provider path: volc")
        self.assertEqual(result["codexhub_error"]["source"], "volc")
        self.assertFalse(result["codexhub_error"]["retryable"])
        self.assertEqual(handler._fake.status, 400)

    def test_provider_scoped_chat_completions_routes_slash_model_as_provider_relative(self):
        policy = codex_proxy.load_policy(codex_proxy.POLICY_PATH)
        external_model = {
            "alias": "openrouter/anthropic/claude-sonnet-4",
            "provider_alias": "openrouter",
            "upstream_name": "openrouter",
            "display_prefix": "OpenRouter",
            "base_url": "https://openrouter.example.test/v1",
            "api_key": "openrouter-test-token",
            "upstream_model": "anthropic/claude-sonnet-4",
            "upstream_format": "chat_completions",
            "priority_base": 250,
            "context_window": 200000,
            "max_output_tokens": 32768,
            "input_modalities": ("text",),
            "context_source": "providers_toml",
            "max_output_source": "providers_toml",
        }
        body = json.dumps({
            "model": "anthropic/claude-sonnet-4",
            "messages": [{"role": "user", "content": "Hello"}],
            "stream": False,
        }).encode("utf-8")
        handler = self._make_handler(body, path="/v1/providers/openrouter/chat/completions")
        upstream_body = json.dumps({
            "id": "chatcmpl_test",
            "object": "chat.completion",
            "model": "anthropic/claude-sonnet-4",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "Hi"},
                "finish_reason": "stop",
            }],
        }).encode("utf-8")

        with (
            patch(
                "codex_proxy.load_policy",
                return_value=replace(
                    policy,
                    allowed_provider_models=policy.allowed_provider_models
                    + ("openrouter/anthropic/claude-sonnet-4",),
                ),
            ),
            patch("codex_proxy.resolve_external_model_alias", return_value=external_model),
            patch("codex_proxy.urlopen", return_value=_FakeJsonResponse(upstream_body)) as mock_urlopen,
        ):
            CodexProxyHandler.do_POST(handler)

        request = mock_urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://openrouter.example.test/v1/chat/completions")
        sent_payload = json.loads(request.data)
        self.assertEqual(sent_payload["model"], "anthropic/claude-sonnet-4")
        self.assertEqual(handler._fake.status, 200)

    def test_compact_empty_response_uses_compact_retry_budget(self):
        body = json.dumps(
            {
                "model": "gpt-5.5",
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            "CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.\n"
                            "Your task is to create a detailed summary of the conversation so far.\n"
                            "Return an <analysis> block followed by a <summary> block."
                        ),
                    }
                ],
                "stream": False,
            }
        ).encode("utf-8")
        handler = self._make_handler(body)
        handler.headers["x-query-source"] = "compact"
        empty_body = json.dumps(
            {
                "id": "resp_empty",
                "object": "response",
                "status": "completed",
                "model": "gpt-5.5",
                "output": [],
            }
        ).encode("utf-8")

        with (
            patch.dict(
                "os.environ",
                {
                    "CODEX_PROXY_AUTO_RETRY_ENABLED": "1",
                    "CODEX_PROXY_AUTO_RETRY_MAX_ATTEMPTS": "30",
                    "CODEX_PROXY_COMPACT_RETRY_MAX_ATTEMPTS": "3",
                },
                clear=False,
            ),
            patch(
                "codex_proxy._official_urlopen",
                side_effect=[
                    _FakeJsonResponse(empty_body),
                    _FakeJsonResponse(empty_body),
                    _FakeJsonResponse(empty_body),
                    _FakeJsonResponse(empty_body),
                ],
            ) as mock_urlopen,
            patch("codex_proxy.time.sleep"),
        ):
            CodexProxyHandler.do_POST(handler)

        self.assertEqual(mock_urlopen.call_count, 3)
        self.assertEqual(handler._fake.status, 502)
        payload = json.loads(handler.wfile.writes[0])
        self.assertEqual(payload["error"]["type"], "compact_empty_response")

    def test_provider_scoped_chat_completions_image_proxy_uses_streaming_responses_vision(self):
        policy = codex_proxy.load_policy(codex_proxy.POLICY_PATH)
        image_url = "data:image/png;base64,e2UydC12aXNpb24tcHJveHktZmFsbGJhY2t9"
        body = json.dumps({
            "model": "glm-5.2",
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": "Please inspect this attachment."},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            }],
            "stream": False,
        }).encode("utf-8")
        handler = self._make_handler(body, path="/v1/providers/ollama-cloud/chat/completions")
        handler.headers["X-Codex-Client-Id"] = "codex-app"

        vision_responses_events = [
            b'data: {"type":"response.created","response":{"id":"resp_vision","model":"minimax-m3","output":[]}}\n',
            b'\n',
            b'data: {"type":"response.output_item.added","output_index":0,"item":{"id":"msg_vision","type":"message","role":"assistant","content":[]}}\n',
            b'\n',
            b'data: {"type":"response.output_text.delta","output_index":0,"content_index":0,"delta":"Streaming image description."}\n',
            b'\n',
            b'data: {"type":"response.output_text.done","output_index":0,"content_index":0,"text":"Streaming image description."}\n',
            b'\n',
            b'data: {"type":"response.output_item.done","output_index":0,"item":{"id":"msg_vision","type":"message","role":"assistant","content":[{"type":"output_text","text":"Streaming image description.","annotations":[]}]}}\n',
            b'\n',
            b'data: {"type":"response.completed","response":{"id":"resp_vision","model":"minimax-m3","status":"completed","output":[{"id":"msg_vision","type":"message","role":"assistant","content":[{"type":"output_text","text":"Streaming image description.","annotations":[]}]}]}}\n',
            b'\n',
            b'',
        ]
        main_upstream_body = json.dumps({
            "id": "resp_main",
            "object": "response",
            "status": "completed",
            "model": "glm-5.2",
            "output": [{
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Main response", "annotations": []}],
            }],
        }).encode("utf-8")

        catalog = {
            "gpt-5.5": {"slug": "gpt-5.5"},
            "glm-5.2": {"slug": "glm-5.2", "input_modalities": ["text"]},
            "ollama-cloud/glm-5.2": {"slug": "ollama-cloud/glm-5.2", "input_modalities": ["text"]},
            "minimax-m3": {"slug": "minimax-m3", "input_modalities": ["text", "image"]},
            "ollama-cloud/minimax-m3": {"slug": "ollama-cloud/minimax-m3", "input_modalities": ["text", "image"]},
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.dict(
                    "os.environ",
                    {
                        "OLLAMA_API_KEY": "ollama-test-token",
                        "CODEX_PROXY_IMAGE_PROXY_ENABLED": "1",
                        "CODEX_PROXY_IMAGE_PROXY_MODEL": "minimax-m3",
                        "CODEX_PROXY_AUTO_RETRY_ENABLED": "0",
                    },
                    clear=False,
                ),
                patch("codex_proxy.IMAGE_PROXY_CACHE_PATH", f"{temp_dir}/image-proxy-cache.sqlite"),
                patch("codex_proxy.generated_catalog_slugs", return_value=set(catalog)),
                patch("codex_proxy.generated_catalog_by_slug", return_value=catalog),
                patch(
                    "codex_proxy.load_policy",
                    return_value=replace(
                        policy,
                        allowed_provider_models=policy.allowed_provider_models
                        + ("glm-5.2", "ollama-cloud/glm-5.2", "minimax-m3", "ollama-cloud/minimax-m3"),
                    ),
                ),
                patch(
                    "codex_proxy.urlopen",
                    side_effect=[
                        _FakeSseResponse(vision_responses_events),
                        _FakeJsonResponse(main_upstream_body),
                    ],
                ) as mock_urlopen,
            ):
                CodexProxyHandler.do_POST(handler)

        self.assertEqual(handler._fake.status, 200)
        self.assertEqual(mock_urlopen.call_count, 2)
        vision_responses_request = mock_urlopen.call_args_list[0].args[0]
        vision_responses_payload = json.loads(vision_responses_request.data)
        self.assertTrue(vision_responses_request.full_url.endswith("/responses"))
        self.assertEqual(vision_responses_payload["model"], "minimax-m3")
        self.assertTrue(vision_responses_payload["stream"])
        self.assertNotIn("tools", vision_responses_payload)
        self.assertNotIn("tool_choice", vision_responses_payload)

        main_request = mock_urlopen.call_args_list[1].args[0]
        main_payload = json.loads(main_request.data)
        self.assertTrue(main_request.full_url.endswith("/responses"))
        encoded_main = json.dumps(main_payload)
        self.assertIn("Streaming image description.", encoded_main)
        self.assertNotIn(image_url, encoded_main)

        written = b"".join(handler.wfile.writes)
        result = json.loads(written)
        self.assertEqual(result["choices"][0]["message"]["content"], "Main response")

    def test_provider_scoped_chat_completions_streaming_image_proxy_emits_compatible_progress_chunk(self):
        policy = codex_proxy.load_policy(codex_proxy.POLICY_PATH)
        image_url = "data:image/png;base64,e2UydC12aXNpb24tcHJvZ3Jlc3N9"
        body = json.dumps({
            "model": "glm-5.2",
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": "Please inspect this attachment."},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            }],
            "stream": True,
        }).encode("utf-8")
        handler = self._make_handler(body, path="/v1/providers/ollama-cloud/chat/completions")
        handler.headers["X-Codex-Client-Id"] = "codex-app"

        vision_responses_events = [
            b'data: {"type":"response.output_text.delta","delta":"Streaming image description."}\n',
            b'\n',
            b'data: {"type":"response.completed","response":{"id":"resp_vision","model":"minimax-m3","status":"completed","output":[]}}\n',
            b'\n',
            b'',
        ]
        main_responses_events = [
            b'data: {"type":"response.created","response":{"id":"resp_main","model":"glm-5.2"}}\n',
            b'\n',
            b'data: {"type":"response.output_text.delta","delta":"Main response"}\n',
            b'\n',
            b'data: {"type":"response.completed","response":{"id":"resp_main","model":"glm-5.2","output":[]}}\n',
            b'\n',
            b'',
        ]
        catalog = {
            "gpt-5.5": {"slug": "gpt-5.5"},
            "glm-5.2": {"slug": "glm-5.2", "input_modalities": ["text"]},
            "ollama-cloud/glm-5.2": {"slug": "ollama-cloud/glm-5.2", "input_modalities": ["text"]},
            "minimax-m3": {"slug": "minimax-m3", "input_modalities": ["text", "image"]},
            "ollama-cloud/minimax-m3": {"slug": "ollama-cloud/minimax-m3", "input_modalities": ["text", "image"]},
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.dict(
                    "os.environ",
                    {
                        "OLLAMA_API_KEY": "ollama-test-token",
                        "CODEX_PROXY_IMAGE_PROXY_ENABLED": "1",
                        "CODEX_PROXY_IMAGE_PROXY_MODEL": "minimax-m3",
                        "CODEX_PROXY_AUTO_RETRY_ENABLED": "0",
                    },
                    clear=False,
                ),
                patch("codex_proxy.IMAGE_PROXY_CACHE_PATH", f"{temp_dir}/image-proxy-cache.sqlite"),
                patch("codex_proxy.generated_catalog_slugs", return_value=set(catalog)),
                patch("codex_proxy.generated_catalog_by_slug", return_value=catalog),
                patch(
                    "codex_proxy.load_policy",
                    return_value=replace(
                        policy,
                        allowed_provider_models=policy.allowed_provider_models
                        + ("glm-5.2", "ollama-cloud/glm-5.2", "minimax-m3", "ollama-cloud/minimax-m3"),
                    ),
                ),
                patch(
                    "codex_proxy.urlopen",
                    side_effect=[
                        _FakeSseResponse(vision_responses_events),
                        _FakeSseResponse(main_responses_events),
                    ],
                ),
            ):
                CodexProxyHandler.do_POST(handler)

        written = b"".join(handler.wfile.writes)
        self.assertNotIn(b"event: codexhub.", written)
        data_lines = [line for line in written.split(b"\n") if line.startswith(b"data: {")]
        self.assertTrue(data_lines)
        first_chunk = json.loads(data_lines[0].removeprefix(b"data: "))
        self.assertEqual(first_chunk["object"], "chat.completion.chunk")
        self.assertEqual(
            first_chunk["choices"],
            [{"index": 0, "delta": {"role": "assistant", "content": "Analyzing image...\n\n"}, "finish_reason": None}],
        )
        self.assertEqual(first_chunk["codexhub_status"]["type"], "image_proxy")
        self.assertEqual(first_chunk["codexhub_status"]["status"], "reading")
        self.assertEqual(first_chunk["codexhub_status"]["image_count"], 1)
        self.assertIn(b"Main response", written)
        self.assertTrue(written.rstrip().endswith(b"data: [DONE]"))

    def test_provider_scoped_responses_streaming_image_proxy_emits_compatible_progress_event(self):
        policy = codex_proxy.load_policy(codex_proxy.POLICY_PATH)
        image_url = "data:image/png;base64,e2UydC12aXNpb24tcmVzcG9uc2VzLXByb2dyZXNzfQ=="
        body = json.dumps({
            "model": "glm-5.2",
            "input": [{
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "Please inspect this attachment."},
                    {"type": "input_image", "image_url": image_url},
                ],
            }],
            "stream": True,
        }).encode("utf-8")
        handler = self._make_handler(body, path="/v1/providers/ollama-cloud/responses")
        handler.headers["X-Codex-Client-Id"] = "codex-app"

        vision_responses_events = [
            b'data: {"type":"response.output_text.delta","delta":"Streaming image description."}\n',
            b'\n',
            b'data: {"type":"response.completed","response":{"id":"resp_vision","model":"minimax-m3","status":"completed","output":[]}}\n',
            b'\n',
            b'',
        ]
        main_responses_events = [
            b'data: {"type":"response.created","response":{"id":"resp_main","model":"glm-5.2"}}\n',
            b'\n',
            b'data: {"type":"response.output_text.delta","delta":"Main response"}\n',
            b'\n',
            b'data: {"type":"response.completed","response":{"id":"resp_main","model":"glm-5.2","output":[]}}\n',
            b'\n',
            b'',
        ]
        catalog = {
            "gpt-5.5": {"slug": "gpt-5.5"},
            "glm-5.2": {"slug": "glm-5.2", "input_modalities": ["text"]},
            "ollama-cloud/glm-5.2": {"slug": "ollama-cloud/glm-5.2", "input_modalities": ["text"]},
            "minimax-m3": {"slug": "minimax-m3", "input_modalities": ["text", "image"]},
            "ollama-cloud/minimax-m3": {"slug": "ollama-cloud/minimax-m3", "input_modalities": ["text", "image"]},
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.dict(
                    "os.environ",
                    {
                        "OLLAMA_API_KEY": "ollama-test-token",
                        "CODEX_PROXY_IMAGE_PROXY_ENABLED": "1",
                        "CODEX_PROXY_IMAGE_PROXY_MODEL": "minimax-m3",
                        "CODEX_PROXY_AUTO_RETRY_ENABLED": "0",
                    },
                    clear=False,
                ),
                patch("codex_proxy.IMAGE_PROXY_CACHE_PATH", f"{temp_dir}/image-proxy-cache.sqlite"),
                patch("codex_proxy.generated_catalog_slugs", return_value=set(catalog)),
                patch("codex_proxy.generated_catalog_by_slug", return_value=catalog),
                patch(
                    "codex_proxy.load_policy",
                    return_value=replace(
                        policy,
                        allowed_provider_models=policy.allowed_provider_models
                        + ("glm-5.2", "ollama-cloud/glm-5.2", "minimax-m3", "ollama-cloud/minimax-m3"),
                    ),
                ),
                patch(
                    "codex_proxy.urlopen",
                    side_effect=[
                        _FakeSseResponse(vision_responses_events),
                        _FakeSseResponse(main_responses_events),
                    ],
                ),
            ):
                CodexProxyHandler.do_POST(handler)

        written = b"".join(handler.wfile.writes)
        self.assertNotIn(b"event: codexhub.", written)
        data_lines = [line for line in written.split(b"\n") if line.startswith(b"data: {")]
        self.assertTrue(data_lines)
        first_event = json.loads(data_lines[0].removeprefix(b"data: "))
        self.assertEqual(first_event["type"], "response.output_text.delta")
        self.assertEqual(first_event["delta"], "Analyzing image...\n\n")
        self.assertEqual(first_event["codexhub_status"]["type"], "image_proxy")
        self.assertEqual(first_event["codexhub_status"]["status"], "reading")
        self.assertEqual(first_event["codexhub_status"]["image_count"], 1)
        self.assertIn(b"Main response", written)

    def test_zcode_provider_scoped_responses_image_proxy_follows_image_proxy_setting(self):
        policy = codex_proxy.load_policy(codex_proxy.POLICY_PATH)
        image_url = "data:image/png;base64,e3pjb2RlLXZpc2lvbi1wcm94eX0="
        body = json.dumps({
            "model": "glm-5.2",
            "input": [{
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "Please inspect this attachment."},
                    {"type": "input_image", "image_url": image_url},
                ],
            }],
            "stream": False,
        }).encode("utf-8")
        handler = self._make_handler(body, path="/v1/providers/ollama-cloud/responses")
        handler.headers["User-Agent"] = "zcode"

        vision_responses_events = [
            b'data: {"type":"response.output_text.delta","delta":"ZCode image description."}\n',
            b'\n',
            b'data: {"type":"response.completed","response":{"id":"resp_vision","model":"minimax-m3","status":"completed","output":[]}}\n',
            b'\n',
            b'',
        ]
        main_upstream_body = json.dumps({
            "id": "resp_main",
            "object": "response",
            "status": "completed",
            "model": "glm-5.2",
            "output": [{
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Main response", "annotations": []}],
            }],
        }).encode("utf-8")
        catalog = {
            "gpt-5.5": {"slug": "gpt-5.5"},
            "glm-5.2": {"slug": "glm-5.2", "input_modalities": ["text"]},
            "ollama-cloud/glm-5.2": {"slug": "ollama-cloud/glm-5.2", "input_modalities": ["text"]},
            "minimax-m3": {"slug": "minimax-m3", "input_modalities": ["text", "image"]},
            "ollama-cloud/minimax-m3": {"slug": "ollama-cloud/minimax-m3", "input_modalities": ["text", "image"]},
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.dict(
                    "os.environ",
                    {
                        "OLLAMA_API_KEY": "ollama-test-token",
                        "CODEX_PROXY_IMAGE_PROXY_ENABLED": "1",
                        "CODEX_PROXY_IMAGE_PROXY_MODEL": "minimax-m3",
                        "CODEX_PROXY_AUTO_RETRY_ENABLED": "0",
                    },
                    clear=False,
                ),
                patch("codex_proxy.IMAGE_PROXY_CACHE_PATH", f"{temp_dir}/image-proxy-cache.sqlite"),
                patch("codex_proxy.generated_catalog_slugs", return_value=set(catalog)),
                patch("codex_proxy.generated_catalog_by_slug", return_value=catalog),
                patch(
                    "codex_proxy.load_policy",
                    return_value=replace(
                        policy,
                        allowed_provider_models=policy.allowed_provider_models
                        + ("glm-5.2", "ollama-cloud/glm-5.2", "minimax-m3", "ollama-cloud/minimax-m3"),
                    ),
                ),
                patch(
                    "codex_proxy.urlopen",
                    side_effect=[
                        _FakeSseResponse(vision_responses_events),
                        _FakeJsonResponse(main_upstream_body),
                    ],
                ) as mock_urlopen,
            ):
                CodexProxyHandler.do_POST(handler)

        self.assertEqual(handler._fake.status, 200)
        self.assertEqual(mock_urlopen.call_count, 2)
        main_request = mock_urlopen.call_args_list[1].args[0]
        main_payload = json.loads(main_request.data)
        encoded_main = json.dumps(main_payload)
        self.assertIn("ZCode image description.", encoded_main)
        self.assertNotIn(image_url, encoded_main)
        request_start = next(
            call.kwargs for call in self.write_proxy_event.call_args_list if call.args and call.args[0] == "request_start"
        )
        self.assertEqual(request_start["client_id"], "zcode")
        self.assertEqual(request_start["vision_proxy_policy"], codex_proxy.VISION_PROXY_TRANSPARENT_OVERLAY)

    def test_provider_scoped_chat_completions_image_proxy_supports_chat_completions_vision(self):
        policy = codex_proxy.load_policy(codex_proxy.POLICY_PATH)
        image_url = "data:image/png;base64,e2UydC12aXNpb24tY2hhdC1mb3JtYXR9"
        body = json.dumps({
            "model": "glm-5.2",
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": "Please inspect this attachment."},
                    {"type": "image_url", "image_url": {"url": image_url}},
                ],
            }],
            "stream": False,
        }).encode("utf-8")
        handler = self._make_handler(body, path="/v1/providers/volc/chat/completions")
        handler.headers["X-Codex-Client-Id"] = "codex-app"

        target_model = {
            "alias": "volc/glm-5.2",
            "provider_alias": "volc",
            "upstream_name": "volcengine",
            "display_prefix": "Volc",
            "base_url": "https://ark.example.test/v1",
            "api_key": "volc-test-token",
            "upstream_model": "glm-5.2",
            "upstream_format": "responses",
            "priority_base": 200,
            "context_window": 1024000,
            "max_output_tokens": 4096,
            "input_modalities": ("text",),
            "context_source": "providers_toml",
            "max_output_source": "providers_toml",
        }
        vision_model = {
            "alias": "vision-chat/m3",
            "provider_alias": "vision-chat",
            "upstream_name": "vision_chat",
            "display_prefix": "VisionChat",
            "base_url": "https://vision.example.test/v1",
            "api_key": "vision-test-token",
            "upstream_model": "m3",
            "upstream_format": "chat_completions",
            "priority_base": 300,
            "context_window": 1000000,
            "max_output_tokens": 8192,
            "input_modalities": ("text", "image"),
            "context_source": "providers_toml",
            "max_output_source": "providers_toml",
        }
        vision_chat_body = json.dumps({
            "id": "chatcmpl_vision",
            "object": "chat.completion",
            "model": "m3",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "Chat-format image description."},
                "finish_reason": "stop",
            }],
        }).encode("utf-8")
        main_upstream_body = json.dumps({
            "id": "resp_main",
            "object": "response",
            "status": "completed",
            "model": "glm-5.2",
            "output": [{
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Main response", "annotations": []}],
            }],
        }).encode("utf-8")
        catalog = {
            "gpt-5.5": {"slug": "gpt-5.5"},
            "volc/glm-5.2": {"slug": "volc/glm-5.2", "input_modalities": ["text"]},
            "vision-chat/m3": {"slug": "vision-chat/m3", "input_modalities": ["text", "image"]},
        }

        def resolve_external_model(slug):
            return {
                "volc/glm-5.2": target_model,
                "vision-chat/m3": vision_model,
            }.get(slug)

        with tempfile.TemporaryDirectory() as temp_dir:
            with (
                patch.dict(
                    "os.environ",
                    {
                        "CODEX_PROXY_IMAGE_PROXY_ENABLED": "1",
                        "CODEX_PROXY_IMAGE_PROXY_MODEL": "vision-chat/m3",
                        "CODEX_PROXY_AUTO_RETRY_ENABLED": "0",
                    },
                    clear=False,
                ),
                patch("codex_proxy.IMAGE_PROXY_CACHE_PATH", f"{temp_dir}/image-proxy-cache.sqlite"),
                patch("codex_proxy.generated_catalog_slugs", return_value=set(catalog)),
                patch("codex_proxy.generated_catalog_by_slug", return_value=catalog),
                patch(
                    "codex_proxy.load_policy",
                    return_value=replace(
                        policy,
                        allowed_provider_models=policy.allowed_provider_models + ("volc/glm-5.2", "vision-chat/m3"),
                    ),
                ),
                patch("codex_proxy.resolve_external_model_alias", side_effect=resolve_external_model),
                patch(
                    "codex_proxy.urlopen",
                    side_effect=[
                        _FakeJsonResponse(vision_chat_body),
                        _FakeJsonResponse(main_upstream_body),
                    ],
                ) as mock_urlopen,
            ):
                CodexProxyHandler.do_POST(handler)

        self.assertEqual(handler._fake.status, 200)
        self.assertEqual(mock_urlopen.call_count, 2)
        vision_request = mock_urlopen.call_args_list[0].args[0]
        vision_payload = json.loads(vision_request.data)
        self.assertEqual(vision_request.full_url, "https://vision.example.test/v1/chat/completions")
        self.assertEqual(vision_payload["model"], "m3")
        self.assertFalse(vision_payload["stream"])
        self.assertIn("messages", vision_payload)
        self.assertNotIn("tools", vision_payload)
        self.assertNotIn("tool_choice", vision_payload)
        self.assertIn(image_url, json.dumps(vision_payload))

        main_request = mock_urlopen.call_args_list[1].args[0]
        main_payload = json.loads(main_request.data)
        self.assertEqual(main_request.full_url, "https://ark.example.test/v1/responses")
        encoded_main = json.dumps(main_payload)
        self.assertIn("Chat-format image description.", encoded_main)
        self.assertNotIn(image_url, encoded_main)

        written = b"".join(handler.wfile.writes)
        result = json.loads(written)
        self.assertEqual(result["choices"][0]["message"]["content"], "Main response")

    def test_post_chat_completions_streaming_converts_responses_sse_to_chat_sse(self):
        body = json.dumps({
            "model": "gpt-5.5",
            "messages": [{"role": "user", "content": "Hello"}],
            "stream": True,
        }).encode("utf-8")
        handler = self._make_handler(body)

        sse_lines = [
            b'data: {"type":"response.created","response":{"id":"resp_s","model":"gpt-5.5"}}\n',
            b'\n',
            b'data: {"type":"response.output_text.delta","delta":"Hello"}\n',
            b'\n',
            b'data: {"type":"response.output_text.delta","delta":"!"}\n',
            b'\n',
            b'data: {"type":"response.completed","response":{"id":"resp_s","model":"gpt-5.5","output":[]}}\n',
            b'\n',
            b'',
        ]

        with patch("codex_proxy._official_urlopen", return_value=_FakeSseResponse(sse_lines)):
            CodexProxyHandler.do_POST(handler)

        written = b"".join(handler.wfile.writes)
        lines = [l for l in written.split(b"\n") if l.startswith(b"data: ")]
        # 1 role chunk + 2 text chunks + 1 finish chunk + [DONE]
        self.assertGreaterEqual(len(lines), 4)
        first_chunk = json.loads(lines[0].removeprefix(b"data: "))
        self.assertEqual(first_chunk["object"], "chat.completion.chunk")
        self.assertEqual(first_chunk["choices"][0]["delta"], {"role": "assistant"})
        # Last line should be [DONE]
        self.assertTrue(written.rstrip().endswith(b"data: [DONE]"))

    def test_post_chat_completions_streaming_keeps_retry_events_out_of_downstream_stream(self):
        body = json.dumps({
            "model": "gpt-5.5",
            "messages": [{"role": "user", "content": "Hello"}],
            "stream": True,
        }).encode("utf-8")
        handler = self._make_handler(body)
        sse_lines = [
            b'data: {"type":"response.created","response":{"id":"resp_s","model":"gpt-5.5"}}\n',
            b'\n',
            b'data: {"type":"response.output_text.delta","delta":"Recovered"}\n',
            b'\n',
            b'data: {"type":"response.completed","response":{"id":"resp_s","model":"gpt-5.5","output":[]}}\n',
            b'\n',
            b'',
        ]

        with (
            patch.dict(
                "os.environ",
                {
                    "CODEX_PROXY_AUTO_RETRY_ENABLED": "1",
                    "CODEX_PROXY_AUTO_RETRY_MAX_ATTEMPTS": "2",
                },
                clear=False,
            ),
            patch(
                "codex_proxy._official_urlopen",
                side_effect=[URLError(TimeoutError("connect timed out")), _FakeSseResponse(sse_lines)],
            ) as mock_urlopen,
            patch("codex_proxy.time.sleep"),
        ):
            CodexProxyHandler.do_POST(handler)

        self.assertEqual(mock_urlopen.call_count, 2)
        written = b"".join(handler.wfile.writes)
        self.assertNotIn(b"event: codexhub.retry\n", written)
        self.assertNotIn(b'"type":"codexhub.retry"', written)
        self.assertIn(b"Recovered", written)
        self.assertTrue(written.rstrip().endswith(b"data: [DONE]"))
        chunks = [
            json.loads(line.removeprefix(b"data: "))
            for line in written.split(b"\n")
            if line.startswith(b"data: {")
        ]
        self.assertTrue(chunks)
        self.assertTrue(all(isinstance(chunk.get("choices"), list) for chunk in chunks))

    def test_post_chat_completions_streaming_returns_final_json_error_without_retry_event(self):
        body = json.dumps({
            "model": "gpt-5.5",
            "messages": [{"role": "user", "content": "Hello"}],
            "stream": True,
        }).encode("utf-8")
        handler = self._make_handler(body)
        upstream_body = json.dumps({"error": "upstream returned json"}).encode("utf-8")

        with (
            patch.dict(
                "os.environ",
                {
                    "CODEX_PROXY_AUTO_RETRY_ENABLED": "1",
                    "CODEX_PROXY_AUTO_RETRY_MAX_ATTEMPTS": "2",
                },
                clear=False,
            ),
            patch(
                "codex_proxy._official_urlopen",
                side_effect=[URLError(TimeoutError("connect timed out")), _FakeJsonResponse(upstream_body, status=502)],
            ),
            patch("codex_proxy.time.sleep"),
        ):
            CodexProxyHandler.do_POST(handler)

        written = b"".join(handler.wfile.writes)
        self.assertNotIn(b"event: codexhub.retry\n", written)
        self.assertNotIn(b'"type":"codexhub.retry"', written)
        self.assertNotIn(b"event: error\n", written)
        payload = json.loads(written)
        self.assertEqual(payload["error"]["message"], "upstream returned json")
        self.assertEqual(payload["error"]["type"], "upstream_error")
        self.assertIsInstance(payload["error"], dict)
        self.assertNotIn(upstream_body, written)
        self.assertEqual(handler._fake.status, 502)

    def test_post_chat_completions_streaming_reports_read_errors_as_chat_sse_error(self):
        cases = [
            TimeoutError("The read operation timed out"),
            OSError("socket reset"),
            URLError(TimeoutError("upstream timed out")),
        ]
        for exc in cases:
            with self.subTest(error=type(exc).__name__):
                body = json.dumps({
                    "model": "gpt-5.5",
                    "messages": [{"role": "user", "content": "Hello"}],
                    "stream": True,
                }).encode("utf-8")
                handler = self._make_handler(body)
                sse_lines = [
                    b'data: {"type":"response.created","response":{"id":"resp_s","model":"gpt-5.5"}}\n',
                    b'\n',
                    exc,
                ]

                with (
                    patch.dict("os.environ", {"CODEX_PROXY_AUTO_RETRY_ENABLED": "0"}, clear=False),
                    patch("codex_proxy._official_urlopen", return_value=_FakeSseResponse(sse_lines)),
                ):
                    CodexProxyHandler.do_POST(handler)

                written = b"".join(handler.wfile.writes)
                error = self._chat_sse_error(written)
                self.assertEqual(error["type"], "upstream_stream_error")
                self.assertEqual(error["code"], type(exc).__name__)
                self.assertEqual(error["status"], 502)
                self.assertEqual(error["upstream"], "official")
                self.assertNotIn(b"finish_reason", written)

    def test_post_chat_completions_streaming_retries_read_error_before_downstream_output(self):
        body = json.dumps({
            "model": "gpt-5.5",
            "messages": [{"role": "user", "content": "Hello"}],
            "stream": True,
        }).encode("utf-8")
        handler = self._make_handler(body)
        failed_stream = _FakeSseResponse([
            b'data: {"type":"response.created","response":{"id":"resp_fail","model":"gpt-5.5"}}\n',
            b'\n',
            ConnectionResetError("socket reset"),
        ])
        successful_stream = _FakeSseResponse([
            b'data: {"type":"response.created","response":{"id":"resp_ok","model":"gpt-5.5"}}\n',
            b'\n',
            b'data: {"type":"response.output_text.delta","delta":"ok"}\n',
            b'\n',
            b'data: {"type":"response.completed","response":{"id":"resp_ok","model":"gpt-5.5","output":[]}}\n',
            b'\n',
            b"",
        ])

        with (
            patch.dict(
                "os.environ",
                {
                    "CODEX_PROXY_AUTO_RETRY_ENABLED": "1",
                    "CODEX_PROXY_AUTO_RETRY_MAX_ATTEMPTS": "2",
                },
                clear=False,
            ),
            patch("codex_proxy._official_urlopen", side_effect=[failed_stream, successful_stream]),
            patch("codex_proxy.time.sleep"),
        ):
            CodexProxyHandler.do_POST(handler)

        written = b"".join(handler.wfile.writes)
        self.assertIn(b'"content":"ok"', written)
        self.assertIn(b"data: [DONE]", written)
        self.assertNotIn(b'"error"', written)
        event_names = [call.args[0] for call in self.write_proxy_event.call_args_list]
        self.assertIn("upstream_retry", event_names)

    def test_provider_chat_streaming_transparent_read_error_closes_without_synthetic_error(self):
        policy = codex_proxy.load_policy(codex_proxy.POLICY_PATH)
        external_model = {
            "alias": "volc/glm-5.2",
            "provider_alias": "volc",
            "upstream_name": "volcengine",
            "display_prefix": "Volc",
            "base_url": "https://ark.example.test/v1",
            "api_key": "volc-test-token",
            "upstream_model": "glm-5.2",
            "upstream_format": "chat_completions",
            "priority_base": 200,
            "context_window": 1024000,
            "max_output_tokens": 4096,
            "input_modalities": ("text",),
            "context_source": "providers_toml",
            "max_output_source": "providers_toml",
        }
        body = json.dumps({
            "model": "glm-5.2",
            "messages": [{"role": "user", "content": "Hello"}],
            "stream": True,
        }).encode("utf-8")
        handler = self._make_handler(body, path="/v1/providers/volc/chat/completions")
        chat_sse_lines = [
            b'data: {"id":"chatcmpl_s","object":"chat.completion.chunk","choices":[{"index":0,"delta":{"role":"assistant"},"finish_reason":null}]}\n',
            b'\n',
            OSError("chat stream reset"),
        ]

        with (
            patch.dict("os.environ", {"CODEX_PROXY_AUTO_RETRY_ENABLED": "0"}, clear=False),
            patch("codex_proxy.load_policy", return_value=policy),
            patch("codex_proxy.resolve_external_model_alias", return_value=external_model),
            patch("codex_proxy.urlopen", return_value=_FakeSseResponse(chat_sse_lines)) as mock_urlopen,
        ):
            CodexProxyHandler.do_POST(handler)

        written = b"".join(handler.wfile.writes)
        self.assertIn(b"chatcmpl_s", written)
        self.assertNotIn(b'"error"', written)
        self.assertNotIn(b"data: [DONE]", written)
        self.assertTrue(handler.close_connection)
        self.assertEqual(mock_urlopen.call_count, 1)
        event_names = [call.args[0] for call in self.write_proxy_event.call_args_list if call.args]
        self.assertIn("transparent_stream_closed", event_names)

    def test_post_chat_completions_non_streaming_open_failure_returns_chat_error_object(self):
        body = json.dumps({
            "model": "gpt-5.5",
            "messages": [{"role": "user", "content": "Hello"}],
            "stream": False,
        }).encode("utf-8")
        handler = self._make_handler(body)

        with (
            patch.dict("os.environ", {"CODEX_PROXY_AUTO_RETRY_ENABLED": "0"}, clear=False),
            patch("codex_proxy._official_urlopen", side_effect=URLError(ConnectionResetError("connection reset"))),
        ):
            CodexProxyHandler.do_POST(handler)

        result = json.loads(b"".join(handler.wfile.writes))
        self.assertIsInstance(result["error"], dict)
        self.assertEqual(result["error"]["type"], "upstream_error")
        self.assertEqual(result["error"]["code"], "URLError")
        self.assertEqual(result["error"]["status"], 502)
        self.assertEqual(result["codexhub_error"]["code"], "upstream.transport")
        self.assertEqual(result["codexhub_error"]["source"], "official")
        self.assertTrue(result["codexhub_error"]["retryable"])
        self.assertEqual(result["codexhub_error"]["details"]["error"], "URLError")
        self.assertEqual(handler._fake.status, 502)

    def test_post_chat_completions_http_error_keeps_openai_error_and_adds_typed_error(self):
        body = json.dumps({
            "model": "gpt-5.5",
            "messages": [{"role": "user", "content": "Hello"}],
            "stream": False,
        }).encode("utf-8")
        handler = self._make_handler(body)
        upstream_error = {
            "error": {
                "message": "rate limit exceeded",
                "type": "rate_limit_error",
                "code": "rate_limit_exceeded",
            }
        }

        with (
            patch.dict("os.environ", {"CODEX_PROXY_AUTO_RETRY_ENABLED": "0"}, clear=False),
            patch("codex_proxy._official_urlopen", side_effect=_http_error(429, json.dumps(upstream_error).encode("utf-8"))),
        ):
            CodexProxyHandler.do_POST(handler)

        result = json.loads(b"".join(handler.wfile.writes))
        self.assertEqual(result["error"], upstream_error["error"])
        self.assertEqual(result["codexhub_error"]["code"], "provider.rate_limit")
        self.assertEqual(result["codexhub_error"]["source"], "official")
        self.assertTrue(result["codexhub_error"]["retryable"])
        self.assertEqual(result["codexhub_error"]["details"]["status"], 429)
        self.assertEqual(handler._fake.status, 429)

    def test_post_responses_streaming_keeps_responses_sse_error_shape(self):
        body = json.dumps({
            "model": "gpt-5.5",
            "input": "Hello",
            "stream": True,
        }).encode("utf-8")
        handler = self._make_handler(body, path="/v1/responses")
        sse_lines = [
            b'data: {"type":"response.created","response":{"id":"resp_s","model":"gpt-5.5"}}\n',
            b'\n',
            b'data: {"type":"response.output_text.delta","delta":"partial"}\n',
            b'\n',
            OSError("responses stream reset"),
        ]

        with patch("codex_proxy._official_urlopen", return_value=_FakeSseResponse(sse_lines)):
            CodexProxyHandler.do_POST(handler)

        written = b"".join(handler.wfile.writes)
        self.assertIn(b"event: error\n", written)
        self.assertIn(b'"type":"upstream_stream_error"', written)
        self.assertIn(b'"error":"OSError"', written)
        self.assertIn(b'"codexhub_error"', written)
        self.assertIn(b'"code":"upstream.transport"', written)

    def test_post_responses_streaming_preserves_buffered_incomplete_terminal(self):
        body = json.dumps({
            "model": "gpt-5.5",
            "input": "Hello",
            "stream": True,
        }).encode("utf-8")
        handler = self._make_handler(body, path="/v1/responses")
        upstream_body = json.dumps(
            {
                "id": "resp_incomplete",
                "object": "response",
                "status": "incomplete",
                "model": "gpt-5.5",
                "output": [],
                "incomplete_details": {"reason": "max_output_tokens"},
            }
        ).encode("utf-8")

        with patch("codex_proxy._official_urlopen", return_value=_FakeJsonResponse(upstream_body)):
            CodexProxyHandler.do_POST(handler)

        written = b"".join(handler.wfile.writes)
        self.assertIn(b"event: response.incomplete", written)
        self.assertIn(b'"status":"incomplete"', written)
        self.assertNotIn(b"event: response.completed", written)

    def test_auto_upstream_format_uses_responses_when_responses_succeeds(self):
        policy = codex_proxy.load_policy(codex_proxy.POLICY_PATH)
        external_model = {
            "alias": "auto/glm-5.2",
            "provider_alias": "auto",
            "upstream_name": "auto_provider",
            "display_prefix": "Auto",
            "base_url": "https://auto.example.test/v1",
            "api_key": "auto-test-token",
            "upstream_model": "glm-5.2",
            "upstream_format": "auto",
            "priority_base": 200,
            "context_window": 1024000,
            "max_output_tokens": 4096,
            "input_modalities": ("text",),
            "context_source": "providers_toml",
            "max_output_source": "providers_toml",
        }
        body = json.dumps({
            "model": "glm-5.2",
            "messages": [{"role": "user", "content": "Hello"}],
            "stream": False,
        }).encode("utf-8")
        handler = self._make_handler(body, path="/v1/providers/auto/chat/completions")
        upstream_body = json.dumps({
            "id": "resp_auto",
            "object": "response",
            "status": "completed",
            "model": "glm-5.2",
            "output": [{
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Responses OK", "annotations": []}],
            }],
        }).encode("utf-8")

        with (
            patch.dict("os.environ", {"CODEX_PROXY_AUTO_RETRY_ENABLED": "0"}, clear=False),
            patch(
                "codex_proxy.load_policy",
                return_value=replace(policy, allowed_provider_models=policy.allowed_provider_models + ("auto/glm-5.2",)),
            ),
            patch("codex_proxy.resolve_external_model_alias", return_value=external_model),
            patch("codex_proxy.urlopen", return_value=_FakeJsonResponse(upstream_body)) as mock_urlopen,
        ):
            CodexProxyHandler.do_POST(handler)

        self.assertEqual(mock_urlopen.call_count, 1)
        self.assertTrue(mock_urlopen.call_args.args[0].full_url.endswith("/responses"))
        result = json.loads(b"".join(handler.wfile.writes))
        self.assertEqual(result["choices"][0]["message"]["content"], "Responses OK")

    def test_auto_upstream_format_falls_back_to_chat_for_protocol_http_error(self):
        policy = codex_proxy.load_policy(codex_proxy.POLICY_PATH)
        external_model = {
            "alias": "auto/glm-5.2",
            "provider_alias": "auto",
            "upstream_name": "auto_provider",
            "display_prefix": "Auto",
            "base_url": "https://auto.example.test/v1",
            "api_key": "auto-test-token",
            "upstream_model": "glm-5.2",
            "upstream_format": "auto",
            "priority_base": 200,
            "context_window": 1024000,
            "max_output_tokens": 4096,
            "input_modalities": ("text",),
            "context_source": "providers_toml",
            "max_output_source": "providers_toml",
        }
        body = json.dumps({
            "model": "glm-5.2",
            "messages": [{"role": "user", "content": "Hello"}],
            "stream": False,
        }).encode("utf-8")
        handler = self._make_handler(body, path="/v1/providers/auto/chat/completions")
        chat_body = json.dumps({
            "id": "chatcmpl_auto",
            "object": "chat.completion",
            "model": "glm-5.2",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "Chat fallback OK"},
                "finish_reason": "stop",
            }],
        }).encode("utf-8")

        with (
            patch.dict("os.environ", {"CODEX_PROXY_AUTO_RETRY_ENABLED": "0"}, clear=False),
            patch(
                "codex_proxy.load_policy",
                return_value=replace(policy, allowed_provider_models=policy.allowed_provider_models + ("auto/glm-5.2",)),
            ),
            patch("codex_proxy.resolve_external_model_alias", return_value=external_model),
            patch("codex_proxy.urlopen", side_effect=[_http_error(404), _FakeJsonResponse(chat_body)]) as mock_urlopen,
        ):
            CodexProxyHandler.do_POST(handler)

        self.assertEqual(mock_urlopen.call_count, 2)
        self.assertTrue(mock_urlopen.call_args_list[0].args[0].full_url.endswith("/responses"))
        self.assertTrue(mock_urlopen.call_args_list[1].args[0].full_url.endswith("/chat/completions"))
        chat_payload = json.loads(mock_urlopen.call_args_list[1].args[0].data)
        self.assertIn("messages", chat_payload)
        result = json.loads(b"".join(handler.wfile.writes))
        self.assertEqual(result["choices"][0]["message"]["content"], "Chat fallback OK")

    def test_auto_handler_preserves_completed_tool_lifecycle_through_chat_fallback(self):
        policy = codex_proxy.load_policy(codex_proxy.POLICY_PATH)
        external_model = {
            "alias": "auto/glm-5.2",
            "provider_alias": "auto",
            "upstream_name": "auto_provider",
            "display_prefix": "Auto",
            "base_url": "https://auto.example.test/v1",
            "api_key": "auto-test-token",
            "upstream_model": "glm-5.2",
            "upstream_format": "auto",
            "tool_protocol": "auto",
            "priority_base": 200,
            "context_window": 1024000,
            "max_output_tokens": 4096,
            "input_modalities": ("text",),
            "context_source": "providers_toml",
            "max_output_source": "providers_toml",
        }
        body = json.dumps({
            "model": "glm-5.2",
            "messages": [
                {"role": "user", "content": "test"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call_list_skills",
                        "type": "function",
                        "function": {
                            "name": "shell_command",
                            "arguments": '{"command":"Get-ChildItem skills"}',
                        },
                    }],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_list_skills",
                    "content": "Exit code: 0\nOutput:\nask-matt\ncode-review\n",
                },
            ],
            "tools": [{"type": "function", "function": {
                "name": "shell_command",
                "parameters": {"type": "object"},
            }}],
            "stream": False,
        }).encode("utf-8")
        handler = self._make_handler(body, path="/v1/providers/auto/chat/completions")
        chat_body = json.dumps({
            "id": "chatcmpl_auto_tools",
            "object": "chat.completion",
            "model": "glm-5.2",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": "Done after one tool result."},
                "finish_reason": "stop",
            }],
        }).encode("utf-8")

        with (
            patch.dict("os.environ", {"CODEX_PROXY_AUTO_RETRY_ENABLED": "0"}, clear=False),
            patch(
                "codex_proxy.load_policy",
                return_value=replace(policy, allowed_provider_models=policy.allowed_provider_models + ("auto/glm-5.2",)),
            ),
            patch("codex_proxy.resolve_external_model_alias", return_value=external_model),
            patch("codex_proxy.urlopen", side_effect=[_http_error(404), _FakeJsonResponse(chat_body)]) as mock_urlopen,
        ):
            CodexProxyHandler.do_POST(handler)

        self.assertEqual(mock_urlopen.call_count, 2)
        responses_payload = json.loads(mock_urlopen.call_args_list[0].args[0].data)
        self.assertEqual(responses_payload["input"][1]["type"], "function_call")
        self.assertEqual(responses_payload["input"][2]["type"], "function_call_output")
        chat_payload = json.loads(mock_urlopen.call_args_list[1].args[0].data)
        self.assertEqual(chat_payload["messages"][1]["tool_calls"][0]["id"], "call_list_skills")
        self.assertEqual(chat_payload["messages"][2]["role"], "tool")
        self.assertEqual(chat_payload["messages"][2]["tool_call_id"], "call_list_skills")
        result = json.loads(b"".join(handler.wfile.writes))
        self.assertEqual(result["choices"][0]["message"]["content"], "Done after one tool result.")

    def test_auto_upstream_format_does_not_fallback_after_responses_stream_starts(self):
        policy = codex_proxy.load_policy(codex_proxy.POLICY_PATH)
        external_model = {
            "alias": "auto/glm-5.2",
            "provider_alias": "auto",
            "upstream_name": "auto_provider",
            "display_prefix": "Auto",
            "base_url": "https://auto.example.test/v1",
            "api_key": "auto-test-token",
            "upstream_model": "glm-5.2",
            "upstream_format": "auto",
            "priority_base": 200,
            "context_window": 1024000,
            "max_output_tokens": 4096,
            "input_modalities": ("text",),
            "context_source": "providers_toml",
            "max_output_source": "providers_toml",
        }
        body = json.dumps({
            "model": "glm-5.2",
            "messages": [{"role": "user", "content": "Hello"}],
            "stream": True,
        }).encode("utf-8")
        handler = self._make_handler(body, path="/v1/providers/auto/chat/completions")
        sse_lines = [
            b'data: {"type":"response.created","response":{"id":"resp_auto","model":"glm-5.2"}}\n',
            b'\n',
            OSError("stream reset after start"),
        ]

        with (
            patch.dict("os.environ", {"CODEX_PROXY_AUTO_RETRY_ENABLED": "0"}, clear=False),
            patch(
                "codex_proxy.load_policy",
                return_value=replace(policy, allowed_provider_models=policy.allowed_provider_models + ("auto/glm-5.2",)),
            ),
            patch("codex_proxy.resolve_external_model_alias", return_value=external_model),
            patch("codex_proxy.urlopen", return_value=_FakeSseResponse(sse_lines)) as mock_urlopen,
        ):
            CodexProxyHandler.do_POST(handler)

        self.assertEqual(mock_urlopen.call_count, 1)
        error = self._chat_sse_error(b"".join(handler.wfile.writes))
        self.assertEqual(error["type"], "upstream_stream_error")
        self.assertEqual(error["upstream"], "auto_provider")

    def test_post_chat_completions_404_for_unknown_path(self):
        handler = self._make_handler(b'{}', path="/v1/unknown")
        sent = []
        handler._send_json = lambda status, payload: sent.append((status, payload))
        CodexProxyHandler.do_POST(handler)
        self.assertEqual(sent[0][0], 404)


if __name__ == "__main__":
    unittest.main()
