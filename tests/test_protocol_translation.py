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

    def test_chat_template_reasoning_effort_translates_to_responses_reasoning(self):
        body = json.dumps(
            {
                "model": "glm-5.2",
                "messages": [{"role": "user", "content": "hi"}],
                "chat_template_kwargs": {"reasoning_effort": "max"},
                "stream": True,
                "stream_options": {"include_usage": True},
            }
        ).encode("utf-8")
        translated = json.loads(protocol_translation.chat_completions_request_to_responses_body(body))
        self.assertEqual(translated["reasoning"], {"effort": "max"})
        self.assertNotIn("chat_template_kwargs", translated)
        self.assertNotIn("stream_options", translated)
        self.assertTrue(translated["stream"])

    def test_chat_reasoning_controls_translate_to_responses_effort(self):
        for payload, expected in (
            ({"reasoning_effort": "high"}, {"effort": "high"}),
            ({"reasoning": "xhigh"}, {"effort": "xhigh"}),
            ({"reasoning": {"effort": "low"}}, {"effort": "low"}),
        ):
            with self.subTest(payload=payload):
                body = json.dumps(
                    {
                        "model": "glm-5.2",
                        "messages": [{"role": "user", "content": "hi"}],
                        **payload,
                    }
                ).encode("utf-8")
                translated = json.loads(protocol_translation.chat_completions_request_to_responses_body(body))
                self.assertEqual(translated["reasoning"], expected)

    def test_conflicting_chat_reasoning_controls_fail_closed(self):
        body = json.dumps(
            {
                "model": "glm-5.2",
                "messages": [{"role": "user", "content": "hi"}],
                "reasoning_effort": "high",
                "chat_template_kwargs": {"reasoning_effort": "max"},
            }
        ).encode("utf-8")
        with self.assertRaises(protocol_translation.UnsupportedProtocolTranslationError) as raised:
            protocol_translation.chat_completions_request_to_responses_body(body)
        self.assertEqual(raised.exception.code, "unsupported_protocol_semantics")

    def test_unmappable_chat_template_kwargs_and_stream_options_fail_closed(self):
        for extra in (
            {"chat_template_kwargs": {"enable_thinking": True}},
            {"chat_template_kwargs": {"reasoning_effort": "max", "enable_thinking": False}},
            {"stream_options": {"include_usage": True, "chunk_delimiter": "\n"}},
            {"reasoning": {"effort": "high", "summary": "auto"}},
        ):
            with self.subTest(extra=extra):
                body = json.dumps(
                    {
                        "model": "glm-5.2",
                        "messages": [{"role": "user", "content": "hi"}],
                        **extra,
                    }
                ).encode("utf-8")
                with self.assertRaises(protocol_translation.UnsupportedProtocolTranslationError) as raised:
                    protocol_translation.chat_completions_request_to_responses_body(body)
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

    def test_structured_request_items_and_tools_reject_unknown_fields(self):
        responses_payloads = (
            {
                "input": [
                    {"type": "message", "role": "user", "content": "Hello", "future": {"id": "message"}}
                ]
            },
            {
                "input": [
                    {
                        "type": "function_call",
                        "call_id": "call_123",
                        "name": "lookup",
                        "arguments": "{}",
                        "future": {"id": "call"},
                    }
                ]
            },
            {
                "input": "Hello",
                "tools": [
                    {
                        "type": "function",
                        "name": "lookup",
                        "parameters": {"type": "object"},
                        "future": {"id": "tool"},
                    }
                ],
            },
        )
        for payload in responses_payloads:
            with self.subTest(direction="responses_to_chat", payload=payload), self.assertRaises(
                protocol_translation.UnsupportedProtocolTranslationError
            ) as raised:
                protocol_translation.responses_request_to_chat_completion_body(
                    json.dumps({"model": "example-model", **payload}).encode("utf-8")
                )
            self.assertEqual(raised.exception.code, "unsupported_protocol_semantics")

    def test_request_semantic_containers_reject_present_invalid_types(self):
        cases = (
            (
                protocol_translation.responses_request_to_chat_completion_body,
                {"model": "example-model", "input": {"role": "user", "content": "Hello"}},
            ),
            (
                protocol_translation.responses_request_to_chat_completion_body,
                {
                    "model": "example-model",
                    "input": [{"type": "message", "role": "user", "content": {"text": "Hello"}}],
                },
            ),
            (
                protocol_translation.responses_request_to_chat_completion_body,
                {"model": "example-model", "input": "Hello", "instructions": {"text": "Be concise."}},
            ),
            (
                protocol_translation.chat_completions_request_to_responses_body,
                {"model": "example-model", "messages": {"role": "user", "content": "Hello"}},
            ),
            (
                protocol_translation.chat_completions_request_to_responses_body,
                {
                    "model": "example-model",
                    "messages": [{"role": "user", "content": {"text": "Hello"}}],
                },
            ),
            (
                protocol_translation.responses_request_to_chat_completion_body,
                {"model": "example-model", "input": "Hello", "tools": {"type": "function"}},
            ),
            (
                protocol_translation.chat_completions_request_to_responses_body,
                {
                    "model": "example-model",
                    "messages": [{"role": "user", "content": "Hello"}],
                    "tools": {"type": "function"},
                },
            ),
        )

        for convert, payload in cases:
            with self.subTest(convert=convert.__name__, payload=payload), self.assertRaises(
                protocol_translation.UnsupportedProtocolTranslationError
            ) as raised:
                convert(json.dumps(payload).encode("utf-8"))

            self.assertEqual(raised.exception.code, "unsupported_protocol_semantics")

    def test_structured_chat_request_items_and_tools_reject_unknown_fields(self):
        chat_payloads = (
            {
                "messages": [{"role": "user", "content": "Hello"}],
                "tools": [
                    {
                        "type": "function",
                        "function": {"name": "lookup", "parameters": {"type": "object"}},
                        "future": {"id": "tool"},
                    }
                ],
            },
            {
                "messages": [{"role": "user", "content": "Hello"}],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "lookup",
                            "parameters": {"type": "object"},
                            "future": {"id": "function"},
                        },
                    }
                ],
            },
            {
                "messages": [
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_123",
                                "type": "function",
                                "function": {"name": "lookup", "arguments": {"query": "Codex"}},
                            }
                        ],
                    }
                ]
            },
        )
        for payload in chat_payloads:
            with self.subTest(direction="chat_to_responses", payload=payload), self.assertRaises(
                protocol_translation.UnsupportedProtocolTranslationError
            ) as raised:
                protocol_translation.chat_completions_request_to_responses_body(
                    json.dumps({"model": "example-model", **payload}).encode("utf-8")
                )
            self.assertEqual(raised.exception.code, "unsupported_protocol_semantics")

    def test_non_string_function_arguments_fail_closed_in_body_converters(self):
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
                                {
                                    "id": "call_123",
                                    "type": "function",
                                    "function": {"name": "lookup", "arguments": {"query": "Codex"}},
                                }
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
            }
        ).encode("utf-8")
        responses_body = json.dumps(
            {
                "id": "resp_123",
                "status": "completed",
                "model": "example-model",
                "output": [
                    {
                        "id": "fc_123",
                        "type": "function_call",
                        "status": "completed",
                        "call_id": "call_123",
                        "name": "lookup",
                        "arguments": {"query": "Codex"},
                    }
                ],
            }
        ).encode("utf-8")

        for convert, body in (
            (protocol_translation.chat_completion_to_response_body, chat_body),
            (protocol_translation.chat_completion_body_to_stream_chunks, chat_body),
            (protocol_translation.response_body_to_chat_completion_body, responses_body),
        ):
            with self.subTest(convert=convert.__name__), self.assertRaises(
                protocol_translation.UnsupportedProtocolTranslationError
            ) as raised:
                convert(body)
            self.assertEqual(raised.exception.code, "unsupported_protocol_semantics")

    def test_response_semantic_containers_reject_present_invalid_types(self):
        chat_body = json.dumps(
            {
                "id": "chatcmpl_123",
                "model": "example-model",
                "choices": {"index": 0, "message": {"role": "assistant", "content": "Hello"}},
            }
        ).encode("utf-8")
        responses_body = json.dumps(
            {
                "id": "resp_123",
                "status": "completed",
                "model": "example-model",
                "output": {"type": "message", "role": "assistant", "content": []},
            }
        ).encode("utf-8")

        for convert, body in (
            (protocol_translation.chat_completion_to_response_body, chat_body),
            (protocol_translation.chat_completion_body_to_stream_chunks, chat_body),
            (protocol_translation.response_body_to_chat_completion_body, responses_body),
            (protocol_translation.response_body_to_response_sse_events, responses_body),
        ):
            with self.subTest(convert=convert.__name__), self.assertRaises(
                protocol_translation.UnsupportedProtocolTranslationError
            ) as raised:
                convert(body)

            self.assertEqual(raised.exception.code, "unsupported_protocol_semantics")

    def test_response_message_content_rejects_present_invalid_container_types(self):
        chat_body = json.dumps(
            {
                "id": "chatcmpl_123",
                "model": "example-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": {"text": "Hello"}},
                        "finish_reason": "stop",
                    }
                ],
            }
        ).encode("utf-8")
        responses_body = json.dumps(
            {
                "id": "resp_123",
                "status": "completed",
                "model": "example-model",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": {"type": "output_text", "text": "Hello"},
                    }
                ],
            }
        ).encode("utf-8")

        for convert, body in (
            (protocol_translation.chat_completion_to_response_body, chat_body),
            (protocol_translation.chat_completion_body_to_stream_chunks, chat_body),
            (protocol_translation.response_body_to_chat_completion_body, responses_body),
            (protocol_translation.response_body_to_response_sse_events, responses_body),
        ):
            with self.subTest(convert=convert.__name__), self.assertRaises(
                protocol_translation.UnsupportedProtocolTranslationError
            ) as raised:
                convert(body)

            self.assertEqual(raised.exception.code, "unsupported_protocol_semantics")

    def test_chat_response_choice_and_message_reject_invalid_container_types(self):
        payloads = (
            {
                "id": "chatcmpl_123",
                "model": "example-model",
                "choices": [[]],
            },
            {
                "id": "chatcmpl_123",
                "model": "example-model",
                "choices": [{"index": 0, "message": [], "finish_reason": "stop"}],
            },
        )

        for payload in payloads:
            body = json.dumps(payload).encode("utf-8")
            for convert in (
                protocol_translation.chat_completion_to_response_body,
                protocol_translation.chat_completion_body_to_stream_chunks,
            ):
                with self.subTest(convert=convert.__name__, payload=payload), self.assertRaises(
                    protocol_translation.UnsupportedProtocolTranslationError
                ) as raised:
                    convert(body)

                self.assertEqual(raised.exception.code, "unsupported_protocol_semantics")

    def test_valid_empty_containers_and_assistant_null_content_remain_supported(self):
        responses_request = json.loads(
            protocol_translation.responses_request_to_chat_completion_body(
                json.dumps({"model": "example-model", "input": [], "tools": []}).encode("utf-8")
            )
        )
        self.assertEqual(responses_request["messages"], [{"role": "user", "content": ""}])
        self.assertNotIn("tools", responses_request)

        chat_request = json.loads(
            protocol_translation.chat_completions_request_to_responses_body(
                json.dumps({"model": "example-model", "messages": [], "tools": []}).encode("utf-8")
            )
        )
        self.assertEqual(
            chat_request["input"],
            [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": ""}]}],
        )
        self.assertNotIn("tools", chat_request)

        empty_chat_body = json.loads(
            protocol_translation.chat_completion_to_response_body(
                json.dumps({"id": "chatcmpl_empty", "model": "example-model", "choices": []}).encode("utf-8")
            )
        )
        self.assertEqual(empty_chat_body["output"], [])

        empty_responses_body = json.loads(
            protocol_translation.response_body_to_chat_completion_body(
                json.dumps(
                    {"id": "resp_empty", "status": "completed", "model": "example-model", "output": []}
                ).encode("utf-8")
            )
        )
        self.assertIsNone(empty_responses_body["choices"][0]["message"]["content"])

        assistant_null = json.loads(
            protocol_translation.chat_completions_request_to_responses_body(
                json.dumps(
                    {
                        "model": "example-model",
                        "messages": [{"role": "assistant", "content": None}],
                    }
                ).encode("utf-8")
            )
        )
        self.assertEqual(assistant_null["input"][0]["role"], "assistant")

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
                "reasoning": {"effort": "high", "summary": "auto"},
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

    def test_chat_stream_rejects_multiple_or_nonzero_choices_before_translation(self):
        chunks = (
            {
                "choices": [
                    {"index": 0, "delta": {"content": "A"}, "finish_reason": "stop"},
                    {"index": 1, "delta": {"content": "B"}, "finish_reason": "stop"},
                ]
            },
            {"choices": [{"index": 1, "delta": {"content": "B"}, "finish_reason": "stop"}]},
            {"choices": [{"index": None, "delta": {"content": "B"}, "finish_reason": "stop"}]},
            {"choices": {"index": 0, "delta": {"content": "A"}}},
        )
        for chunk in chunks:
            with self.subTest(mode="batch", chunk=chunk), self.assertRaises(
                protocol_translation.UnsupportedProtocolTranslationError
            ) as raised:
                protocol_translation.chat_stream_chunks_to_response_events([chunk])
            self.assertEqual(raised.exception.code, "unsupported_protocol_semantics")

            converter = protocol_translation.ChatToResponsesStreamConverter()
            with self.subTest(mode="incremental", chunk=chunk), self.assertRaises(
                protocol_translation.UnsupportedProtocolTranslationError
            ) as raised:
                converter.events_for_chunk(chunk)
            self.assertEqual(raised.exception.code, "unsupported_protocol_semantics")
            self.assertFalse(converter.created)
            self.assertFalse(converter.completed)
            self.assertEqual(converter.text_parts, [])

    def test_chat_stream_rejects_semantic_chunks_after_terminal(self):
        terminal_chunk = {
            "choices": [{"index": 0, "delta": {"content": "A"}, "finish_reason": "stop"}]
        }
        late_chunk = {
            "choices": [{"index": 0, "delta": {"content": "B"}, "finish_reason": None}]
        }

        with self.assertRaises(protocol_translation.UnsupportedProtocolTranslationError) as raised:
            protocol_translation.chat_stream_chunks_to_response_events([terminal_chunk, late_chunk])
        self.assertEqual(raised.exception.code, "unsupported_protocol_semantics")

        converter = protocol_translation.ChatToResponsesStreamConverter()
        terminal_events = converter.events_for_chunk(terminal_chunk)
        self.assertEqual(terminal_events[-1]["type"], "response.completed")
        with self.assertRaises(protocol_translation.UnsupportedProtocolTranslationError) as raised:
            converter.events_for_chunk(late_chunk)
        self.assertEqual(raised.exception.code, "unsupported_protocol_semantics")

    def test_chat_stream_allows_nonsemantic_framing_after_terminal(self):
        terminal_chunk = {
            "choices": [{"index": 0, "delta": {"content": "A"}, "finish_reason": "stop"}]
        }
        usage_chunk = {
            "choices": [],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

        events = protocol_translation.chat_stream_chunks_to_response_events(
            [terminal_chunk, usage_chunk, "[DONE]"]
        )
        self.assertEqual(events[-1]["type"], "response.completed")
        self.assertEqual(events[-1]["response"]["output"][0]["content"][0]["text"], "A")

        converter = protocol_translation.ChatToResponsesStreamConverter()
        converter.events_for_chunk(terminal_chunk)
        self.assertEqual(converter.events_for_chunk(usage_chunk), [])
        self.assertEqual(converter.events_for_done(), [])

    def test_chat_stream_rejects_unknown_tool_fields_and_non_string_arguments(self):
        tool_calls = (
            {
                "index": 0,
                "id": "call_123",
                "type": "function",
                "function": {"name": "lookup", "arguments": "{}"},
                "future": {"id": "tool"},
            },
            {
                "index": 0,
                "id": "call_123",
                "type": "function",
                "function": {"name": "lookup", "arguments": {"query": "Codex"}},
            },
        )
        for tool_call in tool_calls:
            chunk = {
                "choices": [
                    {
                        "index": 0,
                        "delta": {"tool_calls": [tool_call]},
                        "finish_reason": "tool_calls",
                    }
                ]
            }
            with self.subTest(mode="batch", tool_call=tool_call), self.assertRaises(
                protocol_translation.UnsupportedProtocolTranslationError
            ) as raised:
                protocol_translation.chat_stream_chunks_to_response_events([chunk])
            self.assertEqual(raised.exception.code, "unsupported_protocol_semantics")

            converter = protocol_translation.ChatToResponsesStreamConverter()
            with self.subTest(mode="incremental", tool_call=tool_call), self.assertRaises(
                protocol_translation.UnsupportedProtocolTranslationError
            ) as raised:
                converter.events_for_chunk(chunk)
            self.assertEqual(raised.exception.code, "unsupported_protocol_semantics")

    def test_chat_stream_preserves_text_and_structured_tool_call(self):
        chunks = [
            {"choices": [{"index": 0, "delta": {"content": "I will check."}, "finish_reason": None}]},
            {
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_123",
                                    "type": "function",
                                    "function": {"name": "lookup", "arguments": "{}"},
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ]
            },
        ]

        events = protocol_translation.chat_stream_chunks_to_response_events(chunks)

        completed = events[-1]["response"]
        self.assertEqual([item["type"] for item in completed["output"]], ["message", "function_call"])
        self.assertEqual(completed["output"][0]["content"][0]["text"], "I will check.")
        self.assertEqual(completed["output"][1]["call_id"], "call_123")

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

    def test_responses_stream_rejects_semantic_events_after_terminal(self):
        created = {"type": "response.created", "response": {"id": "resp_123", "model": "example-model"}}
        terminal = {
            "type": "response.completed",
            "response": {"id": "resp_123", "status": "completed", "output": []},
        }
        late_delta = {
            "type": "response.output_text.delta",
            "output_index": 0,
            "item_id": "msg_123",
            "content_index": 0,
            "delta": "late",
        }

        with self.assertRaises(protocol_translation.UnsupportedProtocolTranslationError) as raised:
            protocol_translation.response_events_to_chat_stream_chunks([created, terminal, late_delta])
        self.assertEqual(raised.exception.code, "unsupported_protocol_semantics")

        converter = protocol_translation.ResponsesToChatStreamConverter()
        converter.chunks_for_event(created)
        terminal_chunks = converter.chunks_for_event(terminal)
        self.assertEqual(terminal_chunks[-1]["choices"][0]["finish_reason"], "stop")
        with self.assertRaises(protocol_translation.UnsupportedProtocolTranslationError) as raised:
            converter.chunks_for_event(late_delta)
        self.assertEqual(raised.exception.code, "unsupported_protocol_semantics")

    def test_responses_stream_rejects_invalid_terminal_output_container(self):
        terminal = {
            "type": "response.completed",
            "response": {
                "id": "resp_123",
                "status": "completed",
                "output": {"type": "message", "role": "assistant", "content": []},
            },
        }

        with self.assertRaises(protocol_translation.UnsupportedProtocolTranslationError) as raised:
            protocol_translation.response_events_to_chat_stream_chunks([terminal])
        self.assertEqual(raised.exception.code, "unsupported_protocol_semantics")

        converter = protocol_translation.ResponsesToChatStreamConverter()
        with self.assertRaises(protocol_translation.UnsupportedProtocolTranslationError) as raised:
            converter.chunks_for_event(terminal)
        self.assertEqual(raised.exception.code, "unsupported_protocol_semantics")

    def test_responses_stream_allows_done_framing_after_terminal(self):
        created = {"type": "response.created", "response": {"id": "resp_123", "model": "example-model"}}
        terminal = {
            "type": "response.completed",
            "response": {"id": "resp_123", "status": "completed", "output": []},
        }

        chunks = protocol_translation.response_events_to_chat_stream_chunks(
            [created, terminal, "[DONE]"]
        )
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

    def test_responses_stream_preserves_final_function_arguments_without_deltas(self):
        item = {
            "id": "fc_123",
            "type": "function_call",
            "status": "completed",
            "call_id": "call_123",
            "name": "lookup",
            "arguments": '{"query":"Codex"}',
        }
        prefix = [
            {"type": "response.created", "response": {"id": "resp_123", "model": "example-model"}},
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {**item, "status": "in_progress", "arguments": ""},
            },
        ]
        endings = (
            [
                {
                    "type": "response.function_call_arguments.done",
                    "item_id": "fc_123",
                    "output_index": 0,
                    "arguments": item["arguments"],
                },
                {"type": "response.output_item.done", "output_index": 0, "item": item},
            ],
            [{"type": "response.output_item.done", "output_index": 0, "item": item}],
        )
        terminal = {
            "type": "response.completed",
            "response": {"id": "resp_123", "status": "completed", "output": [item]},
        }

        def streamed_arguments(chunks):
            return "".join(
                tool_call.get("function", {}).get("arguments", "")
                for chunk in chunks
                for choice in chunk.get("choices", [])
                for tool_call in choice.get("delta", {}).get("tool_calls", [])
            )

        for ending in endings:
            events = [*prefix, *ending, terminal]
            with self.subTest(mode="batch", ending=ending):
                chunks = protocol_translation.response_events_to_chat_stream_chunks(events)
                self.assertEqual(streamed_arguments(chunks), item["arguments"])

            converter = protocol_translation.ResponsesToChatStreamConverter()
            chunks = []
            for event in events:
                chunks.extend(converter.chunks_for_event(event))
            with self.subTest(mode="incremental", ending=ending):
                self.assertEqual(streamed_arguments(chunks), item["arguments"])

    def test_responses_stream_rejects_disagreeing_function_argument_final(self):
        prefix = [
            {"type": "response.created", "response": {"id": "resp_123", "model": "example-model"}},
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {
                    "id": "fc_123",
                    "type": "function_call",
                    "status": "in_progress",
                    "call_id": "call_123",
                    "name": "lookup",
                    "arguments": "",
                },
            },
            {
                "type": "response.function_call_arguments.delta",
                "item_id": "fc_123",
                "output_index": 0,
                "delta": '{"query":',
            },
        ]
        done = {
            "type": "response.function_call_arguments.done",
            "item_id": "fc_123",
            "output_index": 0,
            "arguments": '{"other":"value"}',
        }

        with self.assertRaises(protocol_translation.UnsupportedProtocolTranslationError) as raised:
            protocol_translation.response_events_to_chat_stream_chunks([*prefix, done])
        self.assertEqual(raised.exception.code, "unsupported_protocol_semantics")

        converter = protocol_translation.ResponsesToChatStreamConverter()
        for event in prefix:
            converter.chunks_for_event(event)
        with self.assertRaises(protocol_translation.UnsupportedProtocolTranslationError) as raised:
            converter.chunks_for_event(done)
        self.assertEqual(raised.exception.code, "unsupported_protocol_semantics")

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

    def test_response_body_to_sse_preserves_incomplete_and_failed_terminals(self):
        payloads = (
            (
                {
                    "id": "resp_incomplete",
                    "object": "response",
                    "status": "incomplete",
                    "output": [],
                    "incomplete_details": {"reason": "max_output_tokens"},
                },
                "response.incomplete",
            ),
            (
                {
                    "id": "resp_failed",
                    "object": "response",
                    "status": "failed",
                    "output": [],
                    "error": {"code": "provider_error", "message": "provider failed"},
                },
                "response.failed",
            ),
        )

        for payload, terminal_type in payloads:
            with self.subTest(status=payload["status"]):
                events = protocol_translation.response_body_to_response_sse_events(
                    json.dumps(payload).encode("utf-8")
                )
                self.assertEqual(events[-1]["type"], terminal_type)
                self.assertEqual(events[-1]["response"]["status"], payload["status"])
                self.assertNotIn("response.completed", [event["type"] for event in events])

    def test_response_body_to_sse_preserves_custom_tool_freeform_lifecycle(self):
        item = {
            "id": "ctc_patch_fixture",
            "type": "custom_tool_call",
            "status": "completed",
            "call_id": "call_patch_fixture",
            "name": "apply_patch",
            "input": "*** Begin Patch\n*** Update File: SANITIZED_TARGET\n*** End Patch\n",
        }
        body = json.dumps(
            {
                "id": "resp_patch_fixture",
                "object": "response",
                "status": "completed",
                "model": "<third-party-glm>",
                "output": [item],
            }
        ).encode("utf-8")

        events = protocol_translation.response_body_to_response_sse_events(body)
        reconstructed = json.loads(protocol_translation.events_to_responses_body(events))

        self.assertEqual(
            [event["type"] for event in events],
            [
                "response.created",
                "response.in_progress",
                "response.output_item.added",
                "response.custom_tool_call_input.delta",
                "response.custom_tool_call_input.done",
                "response.output_item.done",
                "response.completed",
            ],
        )
        input_done = next(event for event in events if event["type"] == "response.custom_tool_call_input.done")
        self.assertEqual(input_done["item_id"], item["id"])
        self.assertEqual(input_done["input"], item["input"])
        self.assertEqual(reconstructed["output"], [item])

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
