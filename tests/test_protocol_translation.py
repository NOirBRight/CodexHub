import json
import unittest

import protocol_translation


class ProtocolTranslationTests(unittest.TestCase):
    def test_chat_history_with_missing_tool_ids_fails_closed(self):
        for message in (
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"type": "function", "function": {"name": "get_weather", "arguments": "{}"}}],
            },
            {"role": "tool", "content": "Sunny"},
        ):
            with self.subTest(message=message), self.assertRaises(
                protocol_translation.UnsupportedProtocolTranslationError
            ) as raised:
                protocol_translation.chat_completions_request_to_responses_body(
                    json.dumps({"model": "example-model", "messages": [message]}).encode("utf-8")
                )

            self.assertEqual(raised.exception.code, "unpaired_tool_call")

    def test_responses_history_with_missing_tool_ids_fails_closed(self):
        for item in (
            {"type": "function_call", "name": "get_weather", "arguments": "{}"},
            {"type": "function_call_output", "output": "Sunny"},
        ):
            with self.subTest(item=item), self.assertRaises(
                protocol_translation.UnsupportedProtocolTranslationError
            ) as raised:
                protocol_translation.responses_request_to_chat_completion_body(
                    json.dumps({"model": "example-model", "input": [item]}).encode("utf-8")
                )

            self.assertEqual(raised.exception.code, "unpaired_tool_call")

    def test_response_bodies_with_missing_tool_ids_fail_closed(self):
        responses_body = json.dumps(
            {
                "id": "resp_123",
                "model": "example-model",
                "output": [{"type": "function_call", "name": "get_weather", "arguments": "{}"}],
            }
        ).encode("utf-8")
        chat_body = json.dumps(
            {
                "id": "chatcmpl_123",
                "model": "example-model",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {"type": "function", "function": {"name": "get_weather", "arguments": "{}"}}
                            ],
                        }
                    }
                ],
            }
        ).encode("utf-8")

        for convert, body in (
            (protocol_translation.response_body_to_chat_completion_body, responses_body),
            (protocol_translation.chat_completion_to_response_body, chat_body),
        ):
            with self.subTest(convert=convert.__name__), self.assertRaises(
                protocol_translation.UnsupportedProtocolTranslationError
            ) as raised:
                convert(body)

            self.assertEqual(raised.exception.code, "unpaired_tool_call")

    def test_chat_completion_chunking_with_missing_tool_id_fails_closed(self):
        body = json.dumps(
            {
                "id": "chatcmpl_123",
                "model": "example-model",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {"type": "function", "function": {"name": "get_weather", "arguments": "{}"}}
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
            }
        ).encode("utf-8")

        with self.assertRaises(protocol_translation.UnsupportedProtocolTranslationError) as raised:
            protocol_translation.chat_completion_body_to_stream_chunks(body)

        self.assertEqual(raised.exception.code, "unpaired_tool_call")

    def test_responses_content_with_annotations_fails_closed(self):
        body = json.dumps(
            {
                "model": "example-model",
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": "See the citation.",
                                "annotations": [{"type": "url_citation", "url": "https://example.test"}],
                            }
                        ],
                    }
                ],
            }
        ).encode("utf-8")

        with self.assertRaises(protocol_translation.UnsupportedProtocolTranslationError) as raised:
            protocol_translation.responses_request_to_chat_completion_body(body)

        self.assertEqual(raised.exception.code, "unsupported_protocol_semantics")

    def test_unknown_content_fields_fail_closed_in_both_request_directions(self):
        responses_body = json.dumps(
            {
                "model": "example-model",
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "Hello", "future_metadata": {"id": "future"}}],
                    }
                ],
            }
        ).encode("utf-8")
        chat_body = json.dumps(
            {
                "model": "example-model",
                "messages": [
                    {
                        "role": "user",
                        "content": [{"type": "text", "text": "Hello", "future_metadata": {"id": "future"}}],
                    }
                ],
            }
        ).encode("utf-8")

        for convert, body in (
            (protocol_translation.responses_request_to_chat_completion_body, responses_body),
            (protocol_translation.chat_completions_request_to_responses_body, chat_body),
        ):
            with self.subTest(convert=convert.__name__), self.assertRaises(
                protocol_translation.UnsupportedProtocolTranslationError
            ) as raised:
                convert(body)

            self.assertEqual(raised.exception.code, "unsupported_protocol_semantics")

    def test_unmapped_request_message_and_tool_fields_fail_closed(self):
        responses_payload = {
            "model": "example-model",
            "input": [{"type": "message", "role": "user", "content": "Hello"}],
            "future_semantic_field": {"must": "not disappear"},
        }
        chat_payload = {
            "model": "example-model",
            "messages": [{"role": "user", "content": "Hello"}],
            "future_semantic_field": {"must": "not disappear"},
        }
        for convert, payload in (
            (protocol_translation.responses_request_to_chat_completion_body, responses_payload),
            (protocol_translation.chat_completions_request_to_responses_body, chat_payload),
        ):
            with self.subTest(convert=convert.__name__), self.assertRaises(
                protocol_translation.UnsupportedProtocolTranslationError
            ) as raised:
                convert(json.dumps(payload).encode("utf-8"))

            self.assertEqual(raised.exception.code, "unsupported_protocol_semantics")

        lossy_chat_messages = (
            {"role": "system", "content": [{"type": "file", "file_id": "file_123"}]},
            {"role": "tool", "tool_call_id": "call_123", "content": [{"type": "file", "file_id": "file_123"}]},
            {"role": "user", "name": "unmapped-name", "content": "Hello"},
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "Call it", "annotations": [{"type": "url_citation"}]}],
                "tool_calls": [
                    {"id": "call_123", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}
                ],
            },
        )
        for message in lossy_chat_messages:
            with self.subTest(message=message), self.assertRaises(
                protocol_translation.UnsupportedProtocolTranslationError
            ) as raised:
                protocol_translation.chat_completions_request_to_responses_body(
                    json.dumps({"model": "example-model", "messages": [message]}).encode("utf-8")
                )

            self.assertEqual(raised.exception.code, "unsupported_protocol_semantics")

    def test_non_reversible_chat_choice_semantics_fail_closed(self):
        with self.assertRaises(protocol_translation.UnsupportedProtocolTranslationError) as raised:
            protocol_translation.chat_completions_request_to_responses_body(
                json.dumps(
                    {"model": "example-model", "messages": [{"role": "user", "content": "Hello"}], "n": 2}
                ).encode("utf-8")
            )
        self.assertEqual(raised.exception.code, "unsupported_protocol_semantics")

        body = json.dumps(
            {
                "id": "chatcmpl_choices",
                "model": "example-model",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": "A"}, "finish_reason": "stop"},
                    {"index": 1, "message": {"role": "assistant", "content": "B"}, "finish_reason": "stop"},
                ],
            }
        ).encode("utf-8")
        with self.assertRaises(protocol_translation.UnsupportedProtocolTranslationError) as raised:
            protocol_translation.chat_completion_to_response_body(body)
        self.assertEqual(raised.exception.code, "unsupported_protocol_semantics")

    def test_chat_completion_chunking_rejects_lossy_message_semantics(self):
        for message in (
            {"role": "assistant", "content": None, "refusal": "I cannot help with that."},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {"id": "call_123", "type": "custom", "function": {"name": "apply_patch", "arguments": "{}"}}
                ],
            },
            {"role": "assistant", "content": "Hello", "future_semantic_field": {"id": "future"}},
        ):
            with self.subTest(message=message), self.assertRaises(
                protocol_translation.UnsupportedProtocolTranslationError
            ) as raised:
                protocol_translation.chat_completion_body_to_stream_chunks(
                    json.dumps(
                        {
                            "id": "chatcmpl_123",
                            "model": "example-model",
                            "choices": [{"index": 0, "message": message, "finish_reason": "stop"}],
                        }
                    ).encode("utf-8")
                )

            self.assertEqual(raised.exception.code, "unsupported_protocol_semantics")

    def test_responses_semantic_items_without_chat_equivalents_fail_closed(self):
        payloads = {
            "reasoning": {
                "input": [{"type": "reasoning", "summary": [{"type": "summary_text", "text": "private"}]}]
            },
            "refusal": {
                "input": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "refusal", "refusal": "I cannot help with that."}],
                    }
                ]
            },
            "search": {"input": [{"type": "web_search_call", "status": "completed", "action": {"query": "Codex"}}]},
            "custom_tool": {
                "input": [
                    {"type": "custom_tool_call", "call_id": "call_patch", "name": "apply_patch", "input": "*** Begin Patch"}
                ]
            },
            "file": {
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_file", "file_id": "file_123"}],
                    }
                ]
            },
            "audio": {
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_audio", "input_audio": {"data": "abc", "format": "wav"}}],
                    }
                ]
            },
            "unknown": {"input": [{"type": "future_semantic_item", "value": "must not disappear"}]},
        }

        for semantic, payload in payloads.items():
            with self.subTest(semantic=semantic), self.assertRaises(
                protocol_translation.UnsupportedProtocolTranslationError
            ) as raised:
                protocol_translation.responses_request_to_chat_completion_body(
                    json.dumps({"model": "example-model", **payload}).encode("utf-8")
                )

            self.assertEqual(raised.exception.code, "unsupported_protocol_semantics")

    def test_chat_semantic_fields_without_responses_equivalents_fail_closed(self):
        payloads = {
            "reasoning": {
                "reasoning_effort": "high",
                "messages": [{"role": "user", "content": "Hello"}],
            },
            "refusal": {
                "messages": [{"role": "assistant", "content": None, "refusal": "I cannot help with that."}],
            },
            "search": {
                "messages": [{"role": "user", "content": "Search the web."}],
                "tools": [{"type": "web_search"}],
            },
            "custom_tool": {
                "messages": [{"role": "user", "content": "Apply a patch."}],
                "tools": [{"type": "custom", "name": "apply_patch"}],
            },
            "file": {
                "messages": [{"role": "user", "content": [{"type": "file", "file_id": "file_123"}]}],
            },
            "audio": {
                "messages": [
                    {
                        "role": "user",
                        "content": [{"type": "input_audio", "input_audio": {"data": "abc", "format": "wav"}}],
                    }
                ],
            },
            "annotations": {
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": "See this.",
                                "annotations": [{"type": "url_citation", "url": "https://example.test"}],
                            }
                        ],
                    }
                ],
            },
            "unknown": {
                "messages": [{"role": "user", "content": [{"type": "future_content", "value": "must not disappear"}]}],
            },
        }

        for semantic, payload in payloads.items():
            with self.subTest(semantic=semantic), self.assertRaises(
                protocol_translation.UnsupportedProtocolTranslationError
            ) as raised:
                protocol_translation.chat_completions_request_to_responses_body(
                    json.dumps({"model": "example-model", **payload}).encode("utf-8")
                )

            self.assertEqual(raised.exception.code, "unsupported_protocol_semantics")

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

    def test_function_tool_strictness_is_preserved_between_request_formats(self):
        responses_body = json.dumps(
            {
                "model": "example-model",
                "input": "Hello",
                "tools": [
                    {
                        "type": "function",
                        "name": "get_weather",
                        "parameters": {"type": "object", "properties": {}},
                        "strict": True,
                    }
                ],
            }
        ).encode("utf-8")

        chat_payload = json.loads(protocol_translation.responses_request_to_chat_completion_body(responses_body))
        self.assertTrue(chat_payload["tools"][0]["function"]["strict"])

        round_tripped = json.loads(
            protocol_translation.chat_completions_request_to_responses_body(json.dumps(chat_payload).encode("utf-8"))
        )
        self.assertTrue(round_tripped["tools"][0]["strict"])

    def test_url_image_detail_round_trips_between_request_formats(self):
        responses_body = json.dumps(
            {
                "model": "example-model",
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "Describe this image."},
                            {"type": "input_image", "image_url": "https://example.test/image.png", "detail": "high"},
                        ],
                    }
                ],
            }
        ).encode("utf-8")

        chat_payload = json.loads(protocol_translation.responses_request_to_chat_completion_body(responses_body))
        image_part = chat_payload["messages"][0]["content"][1]
        self.assertEqual(image_part, {"type": "image_url", "image_url": {"url": "https://example.test/image.png", "detail": "high"}})

        round_tripped = json.loads(
            protocol_translation.chat_completions_request_to_responses_body(json.dumps(chat_payload).encode("utf-8"))
        )
        self.assertEqual(
            round_tripped["input"][0]["content"][1],
            {"type": "input_image", "image_url": "https://example.test/image.png", "detail": "high"},
        )

    def test_responses_non_function_tools_fail_closed(self):
        for tool_type in ("web_search_preview", "file_search", "custom"):
            with self.subTest(tool_type=tool_type), self.assertRaises(
                protocol_translation.UnsupportedProtocolTranslationError
            ) as raised:
                protocol_translation.responses_request_to_chat_completion_body(
                    json.dumps(
                        {
                            "model": "example-model",
                            "input": "Hello",
                            "tools": [{"type": tool_type}],
                        }
                    ).encode("utf-8")
                )

            self.assertEqual(raised.exception.code, "unsupported_protocol_semantics")

    def test_non_function_tool_choices_fail_closed(self):
        responses_body = json.dumps(
            {"model": "example-model", "input": "Hello", "tool_choice": {"type": "custom", "name": "apply_patch"}}
        ).encode("utf-8")
        chat_body = json.dumps(
            {
                "model": "example-model",
                "messages": [{"role": "user", "content": "Hello"}],
                "tool_choice": {"type": "custom", "name": "apply_patch"},
            }
        ).encode("utf-8")

        for convert, body in (
            (protocol_translation.responses_request_to_chat_completion_body, responses_body),
            (protocol_translation.chat_completions_request_to_responses_body, chat_body),
        ):
            with self.subTest(convert=convert.__name__), self.assertRaises(
                protocol_translation.UnsupportedProtocolTranslationError
            ) as raised:
                convert(body)

            self.assertEqual(raised.exception.code, "unsupported_protocol_semantics")

    def test_responses_reasoning_controls_fail_closed(self):
        with self.assertRaises(protocol_translation.UnsupportedProtocolTranslationError) as raised:
            protocol_translation.responses_request_to_chat_completion_body(
                json.dumps(
                    {
                        "model": "example-model",
                        "input": "Hello",
                        "reasoning": {"effort": "high"},
                    }
                ).encode("utf-8")
            )

        self.assertEqual(raised.exception.code, "unsupported_protocol_semantics")

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

    def test_failed_or_incomplete_responses_body_is_never_translated_as_success(self):
        for status in ("failed", "incomplete"):
            with self.subTest(status=status):
                translated = json.loads(
                    protocol_translation.response_body_to_chat_completion_body(
                        json.dumps(
                            {
                                "id": "resp_123",
                                "model": "example-model",
                                "status": status,
                                "output": [
                                    {
                                        "type": "message",
                                        "role": "assistant",
                                        "content": [{"type": "output_text", "text": "Partial", "annotations": []}],
                                    }
                                ],
                            }
                        ).encode("utf-8")
                    )
                )

                self.assertIn("error", translated)
                self.assertNotIn("choices", translated)

    def test_response_output_semantics_without_chat_equivalents_fail_closed(self):
        outputs = {
            "reasoning": {"type": "reasoning", "summary": [{"type": "summary_text", "text": "private"}]},
            "refusal": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "refusal", "refusal": "I cannot help with that."}],
            },
            "search": {"type": "web_search_call", "status": "completed", "action": {"query": "Codex"}},
            "custom_tool": {"type": "custom_tool_call", "call_id": "call_patch", "name": "apply_patch", "input": "*** Begin Patch"},
            "file": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "input_file", "file_id": "file_123"}],
            },
            "audio": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_audio", "audio": {"data": "abc", "format": "wav"}}],
            },
            "annotations": {
                "type": "message",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": "See this.",
                        "annotations": [{"type": "url_citation", "url": "https://example.test"}],
                    }
                ],
            },
            "unknown": {"type": "future_output", "value": "must not disappear"},
        }

        for semantic, output in outputs.items():
            with self.subTest(semantic=semantic), self.assertRaises(
                protocol_translation.UnsupportedProtocolTranslationError
            ) as raised:
                protocol_translation.response_body_to_chat_completion_body(
                    json.dumps({"id": "resp_123", "model": "example-model", "status": "completed", "output": [output]}).encode(
                        "utf-8"
                    )
                )

            self.assertEqual(raised.exception.code, "unsupported_protocol_semantics")

    def test_chat_response_preserves_text_and_function_call_together(self):
        translated = json.loads(
            protocol_translation.chat_completion_to_response_body(
                json.dumps(
                    {
                        "id": "chatcmpl_123",
                        "model": "example-model",
                        "choices": [
                            {
                                "message": {
                                    "role": "assistant",
                                    "content": "I will check that.",
                                    "tool_calls": [
                                        {
                                            "id": "call_weather",
                                            "type": "function",
                                            "function": {"name": "get_weather", "arguments": '{"city":"Shanghai"}'},
                                        }
                                    ],
                                },
                                "finish_reason": "tool_calls",
                            }
                        ],
                    }
                ).encode("utf-8")
            )
        )

        self.assertEqual([item["type"] for item in translated["output"]], ["message", "function_call"])
        self.assertEqual(translated["output"][0]["content"][0]["text"], "I will check that.")
        self.assertEqual(translated["output"][1]["call_id"], "call_weather")

    def test_chat_response_semantics_without_responses_equivalents_fail_closed(self):
        messages = {
            "reasoning": {"role": "assistant", "content": None, "reasoning_content": "private"},
            "refusal": {"role": "assistant", "content": None, "refusal": "I cannot help with that."},
            "search": {"role": "assistant", "content": None, "tool_calls": [{"type": "web_search"}]},
            "custom_tool": {"role": "assistant", "content": None, "tool_calls": [{"type": "custom", "name": "apply_patch"}]},
            "file": {"role": "assistant", "content": [{"type": "file", "file_id": "file_123"}]},
            "audio": {"role": "assistant", "content": None, "audio": {"data": "abc", "format": "wav"}},
            "annotations": {
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": "See this.",
                        "annotations": [{"type": "url_citation", "url": "https://example.test"}],
                    }
                ],
            },
            "unknown": {"role": "assistant", "content": [{"type": "future_content", "value": "must not disappear"}]},
        }

        for semantic, message in messages.items():
            with self.subTest(semantic=semantic), self.assertRaises(
                protocol_translation.UnsupportedProtocolTranslationError
            ) as raised:
                protocol_translation.chat_completion_to_response_body(
                    json.dumps({"id": "chatcmpl_123", "model": "example-model", "choices": [{"message": message}]}).encode(
                        "utf-8"
                    )
                )

            self.assertEqual(raised.exception.code, "unsupported_protocol_semantics")

    def test_chat_response_failure_and_length_states_are_preserved(self):
        failed = json.loads(
            protocol_translation.chat_completion_to_response_body(
                json.dumps(
                    {
                        "id": "chatcmpl_failed",
                        "model": "example-model",
                        "error": {"message": "upstream rejected the request", "type": "invalid_request_error", "code": "bad_input"},
                    }
                ).encode("utf-8")
            )
        )
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error"]["code"], "bad_input")

        incomplete = json.loads(
            protocol_translation.chat_completion_to_response_body(
                json.dumps(
                    {
                        "id": "chatcmpl_length",
                        "model": "example-model",
                        "choices": [
                            {
                                "message": {"role": "assistant", "content": "Partial"},
                                "finish_reason": "length",
                            }
                        ],
                    }
                ).encode("utf-8")
            )
        )
        self.assertEqual(incomplete["status"], "incomplete")
        self.assertEqual(incomplete["incomplete_details"], {"reason": "max_output_tokens"})

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

    def test_chat_stream_with_terminal_tool_call_missing_id_fails_closed(self):
        chunk = {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {"name": "get_weather", "arguments": "{}"},
                            }
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        }

        with self.assertRaises(protocol_translation.UnsupportedProtocolTranslationError) as raised:
            protocol_translation.chat_stream_chunks_to_response_events([chunk])
        self.assertEqual(raised.exception.code, "unpaired_tool_call")

        converter = protocol_translation.ChatToResponsesStreamConverter()
        with self.assertRaises(protocol_translation.UnsupportedProtocolTranslationError) as raised:
            converter.events_for_chunk(chunk)
        self.assertEqual(raised.exception.code, "unpaired_tool_call")

    def test_chat_length_stream_maps_to_responses_incomplete_event(self):
        chunk = {"choices": [{"delta": {"content": "Partial"}, "finish_reason": "length"}]}

        events = protocol_translation.chat_stream_chunks_to_response_events([chunk])
        self.assertEqual(events[-1]["type"], "response.incomplete")
        self.assertEqual(events[-1]["response"]["status"], "incomplete")
        self.assertEqual(events[-1]["response"]["incomplete_details"], {"reason": "max_output_tokens"})

        converter = protocol_translation.ChatToResponsesStreamConverter()
        stateful_events = converter.events_for_chunk(chunk)
        self.assertEqual(stateful_events[-1]["type"], "response.incomplete")
        self.assertEqual(stateful_events[-1]["response"]["status"], "incomplete")

    def test_chat_stream_custom_tool_delta_fails_closed(self):
        chunk = {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call_patch",
                                "type": "custom",
                                "function": {"name": "apply_patch", "arguments": "*** Begin Patch"},
                            }
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ]
        }

        with self.assertRaises(protocol_translation.UnsupportedProtocolTranslationError) as raised:
            protocol_translation.chat_stream_chunks_to_response_events([chunk])
        self.assertEqual(raised.exception.code, "unsupported_protocol_semantics")

        converter = protocol_translation.ChatToResponsesStreamConverter()
        with self.assertRaises(protocol_translation.UnsupportedProtocolTranslationError) as raised:
            converter.events_for_chunk(chunk)
        self.assertEqual(raised.exception.code, "unsupported_protocol_semantics")

    def test_chat_stream_non_text_content_fails_closed(self):
        chunk = {
            "choices": [
                {
                    "delta": {"content": [{"type": "file", "file_id": "file_123"}]},
                    "finish_reason": "stop",
                }
            ]
        }

        with self.assertRaises(protocol_translation.UnsupportedProtocolTranslationError) as raised:
            protocol_translation.chat_stream_chunks_to_response_events([chunk])
        self.assertEqual(raised.exception.code, "unsupported_protocol_semantics")

        converter = protocol_translation.ChatToResponsesStreamConverter()
        with self.assertRaises(protocol_translation.UnsupportedProtocolTranslationError) as raised:
            converter.events_for_chunk(chunk)
        self.assertEqual(raised.exception.code, "unsupported_protocol_semantics")

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

    def test_responses_stream_with_function_call_missing_id_fails_closed(self):
        events = [
            {"type": "response.created", "response": {"id": "resp_123", "model": "example-model"}},
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {
                    "id": "fc_weather",
                    "type": "function_call",
                    "status": "in_progress",
                    "name": "get_weather",
                    "arguments": "",
                },
            },
        ]

        with self.assertRaises(protocol_translation.UnsupportedProtocolTranslationError) as raised:
            protocol_translation.response_events_to_chat_stream_chunks(events)
        self.assertEqual(raised.exception.code, "unpaired_tool_call")

        converter = protocol_translation.ResponsesToChatStreamConverter()
        converter.chunks_for_event(events[0])
        with self.assertRaises(protocol_translation.UnsupportedProtocolTranslationError) as raised:
            converter.chunks_for_event(events[1])
        self.assertEqual(raised.exception.code, "unpaired_tool_call")

    def test_responses_function_argument_delta_without_paired_call_fails_closed(self):
        events = [
            {"type": "response.created", "response": {"id": "resp_123", "model": "example-model"}},
            {
                "type": "response.function_call_arguments.delta",
                "item_id": "fc_weather",
                "output_index": 0,
                "delta": "{}",
            },
        ]

        with self.assertRaises(protocol_translation.UnsupportedProtocolTranslationError) as raised:
            protocol_translation.response_events_to_chat_stream_chunks(events)
        self.assertEqual(raised.exception.code, "unpaired_tool_call")

        converter = protocol_translation.ResponsesToChatStreamConverter()
        converter.chunks_for_event(events[0])
        with self.assertRaises(protocol_translation.UnsupportedProtocolTranslationError) as raised:
            converter.chunks_for_event(events[1])
        self.assertEqual(raised.exception.code, "unpaired_tool_call")

    def test_responses_failed_or_incomplete_stream_fails_closed_for_chat(self):
        for terminal_type in ("response.failed", "response.incomplete"):
            events = [
                {"type": "response.created", "response": {"id": "resp_123", "model": "example-model"}},
                {"type": terminal_type, "response": {"id": "resp_123", "status": terminal_type.removeprefix("response.")}},
            ]

            with self.subTest(terminal_type=terminal_type), self.assertRaises(
                protocol_translation.UnsupportedProtocolTranslationError
            ) as raised:
                protocol_translation.response_events_to_chat_stream_chunks(events)
            self.assertEqual(raised.exception.code, "upstream_response_failed")

            converter = protocol_translation.ResponsesToChatStreamConverter()
            converter.chunks_for_event(events[0])
            with self.assertRaises(protocol_translation.UnsupportedProtocolTranslationError) as raised:
                converter.chunks_for_event(events[1])
            self.assertEqual(raised.exception.code, "upstream_response_failed")

    def test_stream_reasoning_semantics_fail_closed_in_both_directions(self):
        for event_type in ("response.output_item.added", "response.output_item.done"):
            response_events = [
                {"type": "response.created", "response": {"id": "resp_123", "model": "example-model"}},
                {
                    "type": event_type,
                    "output_index": 0,
                    "item": {"id": "rs_123", "type": "reasoning", "status": "in_progress", "summary": []},
                },
            ]
            with self.subTest(event_type=event_type), self.assertRaises(
                protocol_translation.UnsupportedProtocolTranslationError
            ) as raised:
                protocol_translation.response_events_to_chat_stream_chunks(response_events)
            self.assertEqual(raised.exception.code, "unsupported_protocol_semantics")

            responses_to_chat = protocol_translation.ResponsesToChatStreamConverter()
            responses_to_chat.chunks_for_event(response_events[0])
            with self.assertRaises(protocol_translation.UnsupportedProtocolTranslationError) as raised:
                responses_to_chat.chunks_for_event(response_events[1])
            self.assertEqual(raised.exception.code, "unsupported_protocol_semantics")

        chat_chunk = {"choices": [{"delta": {"reasoning_content": "private"}, "finish_reason": "stop"}]}
        with self.assertRaises(protocol_translation.UnsupportedProtocolTranslationError) as raised:
            protocol_translation.chat_stream_chunks_to_response_events([chat_chunk])
        self.assertEqual(raised.exception.code, "unsupported_protocol_semantics")

        chat_to_responses = protocol_translation.ChatToResponsesStreamConverter()
        with self.assertRaises(protocol_translation.UnsupportedProtocolTranslationError) as raised:
            chat_to_responses.events_for_chunk(chat_chunk)
        self.assertEqual(raised.exception.code, "unsupported_protocol_semantics")

    def test_stream_refusal_content_part_fails_closed_for_chat(self):
        events = [
            {"type": "response.created", "response": {"id": "resp_123", "model": "example-model"}},
            {
                "type": "response.content_part.added",
                "output_index": 0,
                "item_id": "msg_123",
                "content_index": 0,
                "part": {"type": "refusal", "refusal": "I cannot help with that."},
            },
        ]

        with self.assertRaises(protocol_translation.UnsupportedProtocolTranslationError) as raised:
            protocol_translation.response_events_to_chat_stream_chunks(events)
        self.assertEqual(raised.exception.code, "unsupported_protocol_semantics")

        converter = protocol_translation.ResponsesToChatStreamConverter()
        converter.chunks_for_event(events[0])
        with self.assertRaises(protocol_translation.UnsupportedProtocolTranslationError) as raised:
            converter.chunks_for_event(events[1])
        self.assertEqual(raised.exception.code, "unsupported_protocol_semantics")

    def test_responses_completed_stream_with_unknown_output_fails_closed_for_chat(self):
        events = [
            {"type": "response.created", "response": {"id": "resp_123", "model": "example-model"}},
            {
                "type": "response.completed",
                "response": {
                    "id": "resp_123",
                    "status": "completed",
                    "output": [{"type": "future_output", "value": "must not disappear"}],
                },
            },
        ]

        with self.assertRaises(protocol_translation.UnsupportedProtocolTranslationError) as raised:
            protocol_translation.response_events_to_chat_stream_chunks(events)
        self.assertEqual(raised.exception.code, "unsupported_protocol_semantics")

        converter = protocol_translation.ResponsesToChatStreamConverter()
        converter.chunks_for_event(events[0])
        with self.assertRaises(protocol_translation.UnsupportedProtocolTranslationError) as raised:
            converter.chunks_for_event(events[1])
        self.assertEqual(raised.exception.code, "unsupported_protocol_semantics")

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
