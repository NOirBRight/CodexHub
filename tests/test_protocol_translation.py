import json
import unittest

import protocol_translation


class ProtocolTranslationTests(unittest.TestCase):
    def test_chat_request_translates_to_responses(self):
        body = json.dumps(
            {
                "model": "example-model",
                "messages": [
                    {"role": "system", "content": "Be concise."},
                    {"role": "user", "content": "Find the weather."},
                ],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "get_weather",
                            "parameters": {"type": "object", "properties": {}},
                        },
                    }
                ],
                "tool_choice": {"type": "function", "function": {"name": "get_weather"}},
            }
        ).encode("utf-8")

        translated = json.loads(protocol_translation.chat_completions_request_to_responses_body(body))

        self.assertEqual(translated["model"], "example-model")
        self.assertEqual(translated["instructions"], "Be concise.")
        self.assertEqual(translated["input"][0]["content"][0]["text"], "Find the weather.")
        self.assertEqual(translated["tools"][0]["name"], "get_weather")
        self.assertEqual(translated["tool_choice"], {"type": "function", "name": "get_weather"})

    def test_responses_body_translates_to_chat_completion(self):
        body = json.dumps(
            {
                "id": "resp_123",
                "model": "example-model",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "Calling a tool.", "annotations": []}],
                    },
                    {
                        "type": "function_call",
                        "call_id": "call_weather",
                        "name": "get_weather",
                        "arguments": '{"city":"Shanghai"}',
                    },
                ],
            }
        ).encode("utf-8")

        translated = json.loads(protocol_translation.response_body_to_chat_completion_body(body))
        choice = translated["choices"][0]

        self.assertEqual(translated["id"], "resp_123")
        self.assertEqual(choice["message"]["content"], "Calling a tool.")
        self.assertEqual(choice["message"]["tool_calls"][0]["id"], "call_weather")
        self.assertEqual(choice["message"]["tool_calls"][0]["function"]["name"], "get_weather")
        self.assertEqual(choice["finish_reason"], "tool_calls")

    def test_chat_stream_translates_to_responses_events(self):
        chunks = [
            {
                "model": "example-model",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_weather",
                                    "function": {"name": "get_weather", "arguments": '{"city":'},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            },
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": '"Shanghai"}'},
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
            },
        ]

        events = protocol_translation.chat_stream_chunks_to_response_events(chunks)

        self.assertEqual(events[0]["type"], "response.created")
        self.assertEqual(events[1]["type"], "response.output_item.added")
        self.assertEqual(events[1]["item"]["call_id"], "call_weather")
        self.assertEqual(
            [event["delta"] for event in events if event["type"] == "response.function_call_arguments.delta"],
            ['{"city":', '"Shanghai"}'],
        )
        self.assertEqual(events[-1]["type"], "response.completed")
        self.assertEqual(events[-1]["response"]["output"][0]["arguments"], '{"city":"Shanghai"}')

    def test_responses_stream_translates_to_chat_chunks(self):
        events = [
            {"type": "response.created", "response": {"id": "resp_123", "model": "example-model"}},
            {"type": "response.output_text.delta", "delta": "Hello"},
            {"type": "response.output_text.delta", "delta": " world"},
            {"type": "response.completed", "response": {"id": "resp_123", "output": []}},
        ]

        chunks = protocol_translation.response_events_to_chat_stream_chunks(events, require_completed=True)

        self.assertEqual(chunks[0]["choices"][0]["delta"], {"role": "assistant"})
        self.assertEqual(chunks[1]["choices"][0]["delta"]["content"], "Hello")
        self.assertEqual(chunks[2]["choices"][0]["delta"]["content"], " world")
        self.assertEqual(chunks[-1]["choices"][0]["finish_reason"], "stop")

    def test_stateful_stream_converters_emit_terminal_protocol_events(self):
        chat_to_responses = protocol_translation.ChatToResponsesStreamConverter()
        events = chat_to_responses.events_for_chunk(
            {
                "model": "example-model",
                "choices": [{"delta": {"content": "Hello"}, "finish_reason": "stop"}],
            }
        )
        self.assertEqual(events[-1]["type"], "response.completed")

        responses_to_chat = protocol_translation.ResponsesToChatStreamConverter()
        chunks = responses_to_chat.chunks_for_event(
            {
                "type": "response.completed",
                "response": {"id": "resp_123", "output": []},
            }
        )
        self.assertEqual(chunks[0]["choices"][0]["finish_reason"], "stop")


if __name__ == "__main__":
    unittest.main()
