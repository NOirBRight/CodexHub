import io
import json
import unittest
from unittest.mock import patch

from codex_proxy import (
    CodexProxyHandler,
    _chat_completions_request_to_responses_body,
    _chat_messages_to_responses_input,
    _chat_tool_choice_to_responses_tool_choice,
    _chat_tools_to_responses_tools,
    _response_body_to_chat_completion_body,
    _response_events_to_chat_stream_chunks,
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


class ChatToolChoiceTests(unittest.TestCase):
    def test_string_tool_choice(self):
        self.assertEqual(_chat_tool_choice_to_responses_tool_choice("auto"), "auto")
        self.assertEqual(_chat_tool_choice_to_responses_tool_choice("none"), "none")
        self.assertEqual(_chat_tool_choice_to_responses_tool_choice("required"), "required")

    def test_dict_tool_choice(self):
        result = _chat_tool_choice_to_responses_tool_choice({"type": "function", "function": {"name": "foo"}})
        self.assertEqual(result, {"type": "function", "name": "foo"})


class ChatToolsToResponsesTests(unittest.TestCase):
    def test_filters_non_function_tools(self):
        result = _chat_tools_to_responses_tools([
            {"type": "function", "function": {"name": "foo"}},
            {"type": "other"},
        ])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "foo")


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
    def test_text_delta_events(self):
        events = [
            {"type": "response.created", "response": {"id": "resp_1", "model": "gpt-5.5"}},
            {"type": "response.output_text.delta", "delta": "Hello"},
            {"type": "response.output_text.delta", "delta": " world"},
            {"type": "response.completed", "response": {"id": "resp_1", "model": "gpt-5.5", "output": []}},
        ]

        chunks = _response_events_to_chat_stream_chunks(events)

        # 2 text chunks + 1 finish chunk
        self.assertEqual(len(chunks), 3)
        self.assertEqual(chunks[0]["choices"][0]["delta"]["content"], "Hello")
        self.assertEqual(chunks[1]["choices"][0]["delta"]["content"], " world")
        self.assertEqual(chunks[2]["choices"][0]["finish_reason"], "stop")
        self.assertIsNone(chunks[0]["choices"][0]["finish_reason"])

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
                    "output": [{"type": "function_call", "id": "fc_call_1", "call_id": "call_1", "name": "get_weather", "arguments": '{}'}],
                },
            },
        ]

        chunks = _response_events_to_chat_stream_chunks(events)

        # header chunk + 2 argument delta chunks + finish chunk
        self.assertEqual(len(chunks), 4)
        header = chunks[0]["choices"][0]["delta"]["tool_calls"][0]
        self.assertEqual(header["id"], "call_1")
        self.assertEqual(header["function"]["name"], "get_weather")
        self.assertEqual(chunks[1]["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"], '{"city":')
        self.assertEqual(chunks[2]["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"], '"NYC"}')
        self.assertEqual(chunks[3]["choices"][0]["finish_reason"], "tool_calls")

    def test_no_events_produces_finish_chunk(self):
        chunks = _response_events_to_chat_stream_chunks([])
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0]["choices"][0]["finish_reason"], "stop")


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
    status = 200

    def __init__(self, body):
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
        return self._lines.pop(0)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class ChatCompletionsEndpointTests(unittest.TestCase):
    """End-to-end tests for POST /v1/chat/completions through the proxy."""

    def setUp(self):
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
        self.event_patch.start()
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

        with patch("codex_proxy.urlopen", return_value=_FakeJsonResponse(upstream_body)) as mock_urlopen:
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

        with patch("codex_proxy.urlopen", return_value=_FakeSseResponse(sse_lines)):
            CodexProxyHandler.do_POST(handler)

        written = b"".join(handler.wfile.writes)
        lines = [l for l in written.split(b"\n") if l.startswith(b"data: ")]
        # 2 text chunks + 1 finish chunk + [DONE]
        self.assertGreaterEqual(len(lines), 3)
        first_chunk = json.loads(lines[0].removeprefix(b"data: "))
        self.assertEqual(first_chunk["object"], "chat.completion.chunk")
        self.assertEqual(first_chunk["choices"][0]["delta"]["content"], "Hello")
        # Last line should be [DONE]
        self.assertTrue(written.rstrip().endswith(b"data: [DONE]"))

    def test_post_chat_completions_404_for_unknown_path(self):
        handler = self._make_handler(b'{}', path="/v1/unknown")
        sent = []
        handler._send_json = lambda status, payload: sent.append((status, payload))
        CodexProxyHandler.do_POST(handler)
        self.assertEqual(sent[0][0], 404)


if __name__ == "__main__":
    unittest.main()