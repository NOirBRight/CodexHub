from __future__ import annotations

import unittest
from unittest.mock import patch

from probe_upstream_format import (
    UPSTREAM_FORMAT_ANTHROPIC,
    UPSTREAM_FORMAT_AUTO,
    UPSTREAM_FORMAT_CHAT,
    UPSTREAM_FORMAT_RESPONSES,
    anthropic_text_ok,
    chat_stream_tool_ok,
    endpoint_url,
    model_ids_from_payload,
    probe,
    recommended_format,
    recommended_tool_protocol,
    responses_stream_tool_ok,
)


class ProbeUpstreamFormatTests(unittest.TestCase):
    def test_endpoint_url_does_not_duplicate_version_suffix(self) -> None:
        self.assertEqual(endpoint_url("https://example.test/v1", "/models"), "https://example.test/v1/models")
        self.assertEqual(endpoint_url("https://example.test/v2", "/models"), "https://example.test/v2/models")
        self.assertEqual(endpoint_url("https://example.test", "/models"), "https://example.test/v1/models")
        self.assertEqual(
            endpoint_url("https://example.test/v1", "/responses"),
            "https://example.test/v1/responses",
        )
        self.assertEqual(
            endpoint_url("https://example.test/v2", "/chat/completions"),
            "https://example.test/v2/chat/completions",
        )

    def test_endpoint_url_accepts_complete_endpoint_urls(self) -> None:
        self.assertEqual(
            endpoint_url("https://example.test/v1/responses", "/responses"),
            "https://example.test/v1/responses",
        )
        self.assertEqual(
            endpoint_url("https://example.test/v1/response", "/responses"),
            "https://example.test/v1/response",
        )
        self.assertEqual(
            endpoint_url("https://example.test/v1/response", "/models"),
            "https://example.test/v1/models",
        )
        self.assertEqual(
            endpoint_url("https://example.test/v1/responses", "/models"),
            "https://example.test/v1/models",
        )
        self.assertEqual(
            endpoint_url("https://example.test/v2/chat/completions", "/chat/completions"),
            "https://example.test/v2/chat/completions",
        )
        self.assertEqual(
            endpoint_url("https://example.test/v2/chat/completions", "/responses"),
            "https://example.test/v2/responses",
        )

    def test_endpoint_url_appends_standard_suffix_to_bare_base(self) -> None:
        self.assertEqual(
            endpoint_url("https://example.test", "/responses"),
            "https://example.test/v1/responses",
        )
        self.assertEqual(
            endpoint_url("https://example.test/api/coding/v3", "/chat/completions"),
            "https://example.test/api/coding/v3/chat/completions",
        )

    def test_model_ids_accept_common_models_payload_shapes(self) -> None:
        self.assertEqual(model_ids_from_payload({"data": [{"id": "alpha"}, {"model": "beta"}]}), ["alpha", "beta"])
        self.assertEqual(model_ids_from_payload({"models": ["gamma", {"slug": "delta"}]}), ["gamma", "delta"])

    def test_responses_stream_requires_matching_done_and_completed_call_ids(self) -> None:
        events = [
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "name": "get_weather",
                    "call_id": "call_weather",
                },
            },
            {
                "type": "response.completed",
                "response": {
                    "output": [
                        {
                            "type": "function_call",
                            "name": "get_weather",
                            "call_id": "call_weather",
                        }
                    ]
                },
            },
        ]

        self.assertTrue(responses_stream_tool_ok(events))
        events[1]["response"]["output"][0]["call_id"] = ""
        self.assertFalse(responses_stream_tool_ok(events))

    def test_chat_stream_preserves_first_non_empty_tool_call_id(self) -> None:
        chunks = [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "id": "call_weather",
                                    "function": {"name": "get_weather", "arguments": ""},
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
                                    "id": "",
                                    "function": {"arguments": "{\"location\":\"Paris\"}"},
                                }
                            ]
                        }
                    }
                ]
            },
        ]

        self.assertTrue(chat_stream_tool_ok(chunks))
        chunks[1]["choices"][0]["delta"]["tool_calls"][0]["id"] = "call_other"
        self.assertFalse(chat_stream_tool_ok(chunks))

    def test_recommendation_prefers_responses_when_both_formats_pass(self) -> None:
        result = {
            "responses_text_ok": True,
            "responses_tool_ok": True,
            "responses_tool_stream_ok": True,
            "chat_text_ok": True,
            "chat_tool_ok": True,
            "chat_tool_stream_ok": True,
            "anthropic_text_ok": True,
        }

        self.assertEqual(recommended_format(result), UPSTREAM_FORMAT_RESPONSES)
        result["responses_text_ok"] = False
        self.assertEqual(recommended_format(result), UPSTREAM_FORMAT_CHAT)
        result["chat_text_ok"] = False
        self.assertEqual(recommended_format(result), UPSTREAM_FORMAT_ANTHROPIC)
        result["anthropic_text_ok"] = False
        self.assertEqual(recommended_format(result), UPSTREAM_FORMAT_AUTO)

    def test_recommends_responses_structured_when_responses_tools_work(self) -> None:
        result = {
            "responses_tool_ok": True,
            "responses_tool_stream_ok": False,
            "chat_tool_ok": True,
            "chat_tool_stream_ok": True,
        }

        self.assertEqual(recommended_tool_protocol(result), "responses_structured")

    def test_recommends_chat_tools_when_only_chat_tools_work(self) -> None:
        result = {
            "responses_tool_ok": False,
            "responses_tool_stream_ok": False,
            "chat_tool_ok": True,
            "chat_tool_stream_ok": False,
            "chat_tool_history_ok": True,
        }

        self.assertEqual(recommended_tool_protocol(result), "chat_tools")

    def test_recommends_text_compat_when_chat_tools_work_without_history(self) -> None:
        result = {
            "responses_tool_ok": False,
            "responses_tool_stream_ok": False,
            "chat_tool_ok": True,
            "chat_tool_stream_ok": False,
            "chat_tool_history_ok": False,
        }

        self.assertEqual(recommended_tool_protocol(result), "text_compat")

    def test_recommends_none_without_tool_support(self) -> None:
        result = {
            "responses_tool_ok": False,
            "responses_tool_stream_ok": False,
            "chat_tool_ok": False,
            "chat_tool_stream_ok": False,
            "chat_tool_history_ok": False,
        }

        self.assertEqual(recommended_tool_protocol(result), "none")

    def test_probe_collects_all_lightweight_endpoint_capabilities(self) -> None:
        def fake_request_json(
            base_url: str,
            api_key: str,
            path: str,
            *,
            method: str = "GET",
            payload: dict | None = None,
            timeout: int,
        ):
            self.assertEqual(base_url, "https://example.test/v1")
            self.assertEqual(api_key, "test-key")
            if path == "/models":
                return True, 200, {"data": [{"id": "model-a"}]}, None
            if path == "/responses":
                if payload and payload.get("tools"):
                    return True, 200, {"output": [{"type": "function_call", "name": "get_weather", "call_id": "call_weather"}]}, None
                return True, 200, {"id": "resp_1"}, None
            if path == "/chat/completions":
                if payload and payload.get("tools"):
                    return True, 200, {"choices": [{"message": {"tool_calls": [{"id": "call_weather", "function": {"name": "get_weather"}}]}}]}, None
                return True, 200, {"choices": [{"message": {"content": "OK"}}]}, None
            if path == "/messages":
                return False, 404, None, "not found"
            raise AssertionError(f"unexpected probe path: {path}")

        with patch("probe_upstream_format.request_json", side_effect=fake_request_json) as request_json:
            result = probe("https://example.test/v1", "test-key", None, 2)

        self.assertTrue(result["responses_text_ok"])
        self.assertTrue(result["responses_tool_ok"])
        self.assertTrue(result["chat_text_ok"])
        self.assertTrue(result["chat_tool_ok"])
        self.assertTrue(result["chat_tool_history_ok"])
        self.assertFalse(result["anthropic_text_ok"])
        self.assertEqual(result["recommended_format"], UPSTREAM_FORMAT_RESPONSES)
        self.assertEqual(result["recommended_tool_protocol"], "responses_structured")
        self.assertEqual(
            [call.args[2] for call in request_json.call_args_list],
            [
                "/models",
                "/responses",
                "/chat/completions",
                "/messages",
                "/responses",
                "/chat/completions",
                "/chat/completions",
            ],
        )

    def test_anthropic_message_shape_detection_is_lightweight(self) -> None:
        self.assertTrue(anthropic_text_ok({"id": "msg_1", "type": "message", "content": []}))
        self.assertTrue(anthropic_text_ok({"content": [{"type": "text", "text": "OK"}]}))
        self.assertFalse(anthropic_text_ok({"choices": []}))


if __name__ == "__main__":
    unittest.main()
