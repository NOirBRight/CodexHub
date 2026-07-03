from __future__ import annotations

import unittest

from probe_upstream_format import (
    UPSTREAM_FORMAT_AUTO,
    UPSTREAM_FORMAT_CHAT,
    UPSTREAM_FORMAT_RESPONSES,
    chat_stream_tool_ok,
    endpoint_url,
    model_ids_from_payload,
    recommended_format,
    responses_stream_tool_ok,
)


class ProbeUpstreamFormatTests(unittest.TestCase):
    def test_endpoint_url_does_not_duplicate_v1_suffix(self) -> None:
        self.assertEqual(endpoint_url("https://example.test/v1", "/models"), "https://example.test/v1/models")
        self.assertEqual(endpoint_url("https://example.test", "/models"), "https://example.test/v1/models")

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
        }

        self.assertEqual(recommended_format(result), UPSTREAM_FORMAT_RESPONSES)
        result["responses_tool_stream_ok"] = False
        self.assertEqual(recommended_format(result), UPSTREAM_FORMAT_CHAT)
        result["chat_tool_stream_ok"] = False
        self.assertEqual(recommended_format(result), UPSTREAM_FORMAT_AUTO)


if __name__ == "__main__":
    unittest.main()
