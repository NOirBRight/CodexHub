"""Focused contract tests for the isolated Claude Messages compatibility spike."""

from __future__ import annotations

import json
from pathlib import Path
import re
import unittest

from anthropic_messages_spike import (
    CompatibilityStatus,
    chat_chunks_to_messages_sse,
    classify_claude_headers,
    compatibility_matrix,
    exercise_chat_completions_upstream,
    exercise_responses_upstream,
    messages_to_chat_completions,
    messages_to_responses,
    responses_events_to_messages_sse,
    upstream_error_to_messages_sse,
)


def _decode_sse(records: tuple[bytes, ...]) -> list[tuple[str, dict[str, object]]]:
    decoded: list[tuple[str, dict[str, object]]] = []
    for record in records:
        lines = record.decode("utf-8").splitlines()
        event_name = lines[0].removeprefix("event: ")
        payload = json.loads(lines[1].removeprefix("data: "))
        decoded.append((event_name, payload))
    return decoded


class MessagesToResponsesTests(unittest.TestCase):
    def test_text_request_preserves_model_and_adapts_text_input(self):
        result = messages_to_responses(
            {
                "model": "gateway-text-model",
                "max_tokens": 64,
                "messages": [{"role": "user", "content": "SANITIZED_TEXT"}],
            }
        )

        self.assertEqual(
            json.loads(result.body),
            {
                "model": "gateway-text-model",
                "max_output_tokens": 64,
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "SANITIZED_TEXT"}],
                    }
                ],
            },
        )
        self.assertEqual(result.unsupported, ())

    def test_unmapped_messages_field_blocks_forwarding_instead_of_being_dropped(self):
        result = messages_to_responses(
            {
                "model": "gateway-text-model",
                "max_tokens": 64,
                "thinking": {"type": "adaptive"},
                "messages": [{"role": "user", "content": "SANITIZED_TEXT"}],
            }
        )

        self.assertIsNone(result.body)
        self.assertFalse(result.forwardable)
        self.assertEqual(result.unsupported, ("request.thinking",))

    def test_image_is_nonforwardable_until_the_upstream_capability_is_verified(self):
        result = messages_to_responses(
            {
                "model": "gateway-vision-model",
                "max_tokens": 64,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": "SANITIZED_IMAGE_BYTES",
                                },
                            }
                        ],
                    }
                ],
            }
        )

        self.assertIsNone(result.body)
        self.assertEqual(result.unsupported, ("messages[0].content[0]",))

    def test_tool_result_requires_a_prior_call_and_first_content_position(self):
        unmatched = messages_to_responses(
            {
                "model": "gateway-tool-model",
                "max_tokens": 64,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_missing",
                                "content": "SANITIZED_TOOL_RESULT",
                            }
                        ],
                    }
                ],
            }
        )
        out_of_order = messages_to_responses(
            {
                "model": "gateway-tool-model",
                "max_tokens": 64,
                "messages": [
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "toolu_read_001",
                                "name": "read_file",
                                "input": {"path": "fixture.txt"},
                            }
                        ],
                    },
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "SANITIZED_TEXT"},
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_read_001",
                                "content": "SANITIZED_TOOL_RESULT",
                            },
                        ],
                    },
                ],
            }
        )

        self.assertIsNone(unmatched.body)
        self.assertEqual(unmatched.unsupported, ("messages[0].content[0].tool_use_id",))
        self.assertIsNone(out_of_order.body)
        self.assertEqual(out_of_order.unsupported, ("messages[1].content[1].tool_result_order",))

    def test_unresolved_tool_call_history_is_nonforwardable(self):
        result = messages_to_responses(
            {
                "model": "gateway-tool-model",
                "max_tokens": 64,
                "messages": [
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "toolu_read_001",
                                "name": "read_file",
                                "input": {"path": "fixture.txt"},
                            }
                        ],
                    },
                    {"role": "user", "content": "SANITIZED_TEXT"},
                ],
            }
        )

        self.assertIsNone(result.body)
        self.assertEqual(result.unsupported, ("messages[0].content[0].unresolved",))


class ResponsesToMessagesSseTests(unittest.TestCase):
    def test_text_stream_has_anthropic_lifecycle_and_usage(self):
        records = responses_events_to_messages_sse(
            [
                {
                    "type": "response.created",
                    "response": {"id": "resp_text_001", "model": "gpt-test"},
                },
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {"id": "msg_001", "type": "message", "role": "assistant", "content": []},
                },
                {
                    "type": "response.output_text.delta",
                    "output_index": 0,
                    "delta": "SANITIZED_REPLY",
                },
                {
                    "type": "response.output_text.done",
                    "output_index": 0,
                    "text": "SANITIZED_REPLY",
                },
                {
                    "type": "response.output_item.done",
                    "output_index": 0,
                    "item": {"id": "msg_001", "type": "message", "role": "assistant", "content": []},
                },
                {
                    "type": "response.completed",
                    "response": {
                        "id": "resp_text_001",
                        "model": "gpt-test",
                        "usage": {"input_tokens": 7, "output_tokens": 3},
                    },
                },
            ]
        )

        events = _decode_sse(records)
        self.assertEqual([event_name for event_name, _payload in events], [
            "message_start",
            "content_block_start",
            "content_block_delta",
            "content_block_stop",
            "message_delta",
            "message_stop",
        ])
        self.assertEqual(events[0][1]["message"]["id"], "resp_text_001")
        self.assertEqual(events[2][1]["delta"], {"type": "text_delta", "text": "SANITIZED_REPLY"})
        self.assertEqual(events[4][1]["usage"], {"input_tokens": 7, "output_tokens": 3})

    def test_text_stream_rejects_a_final_snapshot_that_conflicts_with_deltas(self):
        with self.assertRaisesRegex(ValueError, "Responses text deltas do not match final text"):
            responses_events_to_messages_sse(
                [
                    {
                        "type": "response.created",
                        "response": {"id": "resp_text_mismatch_001", "model": "gpt-test"},
                    },
                    {
                        "type": "response.output_item.added",
                        "output_index": 0,
                        "item": {
                            "id": "msg_text_mismatch_001",
                            "type": "message",
                            "role": "assistant",
                            "content": [],
                        },
                    },
                    {
                        "type": "response.output_text.delta",
                        "output_index": 0,
                        "delta": "SANITIZED_REPLY",
                    },
                    {
                        "type": "response.output_text.done",
                        "output_index": 0,
                        "text": "SANITIZED_DIFFERENT_REPLY",
                    },
                ]
            )

    def test_tool_stream_uses_upstream_call_id_for_the_follow_up_turn(self):
        records = responses_events_to_messages_sse(
            [
                {
                    "type": "response.created",
                    "response": {"id": "resp_tool_001", "model": "gpt-test"},
                },
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {
                        "id": "fc_toolu_read_001",
                        "type": "function_call",
                        "call_id": "toolu_read_001",
                        "name": "read_file",
                    },
                },
                {
                    "type": "response.function_call_arguments.delta",
                    "output_index": 0,
                    "delta": '{"path":"fixture.txt"}',
                },
                {
                    "type": "response.output_item.done",
                    "output_index": 0,
                    "item": {"id": "fc_toolu_read_001", "type": "function_call"},
                },
                {
                    "type": "response.completed",
                    "response": {
                        "id": "resp_tool_001",
                        "model": "gpt-test",
                        "usage": {"input_tokens": 11, "output_tokens": 4},
                    },
                },
            ]
        )

        events = _decode_sse(records)
        self.assertEqual(events[1][1]["content_block"], {
            "type": "tool_use",
            "id": "toolu_read_001",
            "name": "read_file",
            "input": {},
        })
        self.assertEqual(events[2][1]["delta"], {
            "type": "input_json_delta",
            "partial_json": '{"path":"fixture.txt"}',
        })
        self.assertEqual(events[4][1]["delta"]["stop_reason"], "tool_use")

    def test_tool_stream_accepts_a_matching_final_argument_snapshot(self):
        records = responses_events_to_messages_sse(
            [
                {
                    "type": "response.created",
                    "response": {"id": "resp_tool_done_001", "model": "gpt-test"},
                },
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {
                        "id": "fc_done_001",
                        "type": "function_call",
                        "call_id": "toolu_done_001",
                        "name": "read_file",
                    },
                },
                {
                    "type": "response.function_call_arguments.delta",
                    "output_index": 0,
                    "delta": '{"path":"fixture.txt"}',
                },
                {
                    "type": "response.function_call_arguments.done",
                    "output_index": 0,
                    "arguments": '{"path":"fixture.txt"}',
                },
                {
                    "type": "response.output_item.done",
                    "output_index": 0,
                    "item": {"id": "fc_done_001", "type": "function_call"},
                },
                {
                    "type": "response.completed",
                    "response": {
                        "id": "resp_tool_done_001",
                        "model": "gpt-test",
                        "usage": {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10},
                    },
                },
            ]
        )

        events = _decode_sse(records)
        deltas = [payload["delta"] for event, payload in events if event == "content_block_delta"]
        self.assertEqual(deltas, [{"type": "input_json_delta", "partial_json": '{"path":"fixture.txt"}'}])

    def test_cache_usage_details_are_explicitly_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unsupported Responses usage field: input_tokens_details"):
            responses_events_to_messages_sse(
                [
                    {
                        "type": "response.created",
                        "response": {"id": "resp_usage_detail_001", "model": "gpt-test"},
                    },
                    {
                        "type": "response.completed",
                        "response": {
                            "id": "resp_usage_detail_001",
                            "model": "gpt-test",
                            "usage": {
                                "input_tokens": 7,
                                "output_tokens": 3,
                                "input_tokens_details": {"cached_tokens": 1},
                            },
                        },
                    },
                ]
            )

    def test_unknown_responses_output_item_fails_explicitly(self):
        with self.assertRaisesRegex(ValueError, "Unsupported Responses output item type: reasoning"):
            responses_events_to_messages_sse(
                [
                    {
                        "type": "response.created",
                        "response": {"id": "resp_reasoning_001", "model": "gpt-test"},
                    },
                    {
                        "type": "response.output_item.added",
                        "output_index": 0,
                        "item": {"id": "rs_001", "type": "reasoning"},
                    },
                ]
            )

    def test_tool_stream_rejects_arguments_that_are_not_a_json_object(self):
        with self.assertRaisesRegex(ValueError, "Responses tool arguments must be a JSON object"):
            responses_events_to_messages_sse(
                [
                    {
                        "type": "response.created",
                        "response": {"id": "resp_tool_bad_001", "model": "gpt-test"},
                    },
                    {
                        "type": "response.output_item.added",
                        "output_index": 0,
                        "item": {
                            "id": "fc_bad_001",
                            "type": "function_call",
                            "call_id": "toolu_bad_001",
                            "name": "read_file",
                            "arguments": "",
                        },
                    },
                    {
                        "type": "response.function_call_arguments.delta",
                        "output_index": 0,
                        "delta": "not-json",
                    },
                    {
                        "type": "response.output_item.done",
                        "output_index": 0,
                        "item": {"id": "fc_bad_001", "type": "function_call"},
                    },
                ]
            )

    def test_tool_stream_rejects_a_final_argument_snapshot_that_conflicts_with_deltas(self):
        with self.assertRaisesRegex(ValueError, "Responses tool argument deltas do not match final arguments"):
            responses_events_to_messages_sse(
                [
                    {
                        "type": "response.created",
                        "response": {"id": "resp_tool_mismatch_001", "model": "gpt-test"},
                    },
                    {
                        "type": "response.output_item.added",
                        "output_index": 0,
                        "item": {
                            "id": "fc_mismatch_001",
                            "type": "function_call",
                            "call_id": "toolu_mismatch_001",
                            "name": "read_file",
                        },
                    },
                    {
                        "type": "response.function_call_arguments.delta",
                        "output_index": 0,
                        "delta": '{"path":"fixture.txt"}',
                    },
                    {
                        "type": "response.function_call_arguments.done",
                        "output_index": 0,
                        "arguments": '{"path":"other.txt"}',
                    },
                ]
            )

    def test_missing_upstream_usage_stays_absent_instead_of_zero_filled(self):
        records = responses_events_to_messages_sse(
            [
                {"type": "response.created", "response": {"id": "resp_no_usage", "model": "gpt-test"}},
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {"id": "msg_no_usage", "type": "message", "role": "assistant", "content": []},
                },
                {"type": "response.output_text.delta", "output_index": 0, "delta": "SANITIZED_REPLY"},
                {
                    "type": "response.output_item.done",
                    "output_index": 0,
                    "item": {"id": "msg_no_usage", "type": "message", "role": "assistant", "content": []},
                },
                {"type": "response.completed", "response": {"id": "resp_no_usage", "model": "gpt-test"}},
            ]
        )

        events = _decode_sse(records)
        self.assertEqual(events[0][1]["message"]["usage"], {})
        self.assertEqual(events[-2][1]["usage"], {})


class ChatCompletionsPrototypeTests(unittest.TestCase):
    def test_messages_request_adapts_to_chat_completions_tool_history(self):
        result = messages_to_chat_completions(
            {
                "model": "gateway-chat-model",
                "max_tokens": 64,
                "tools": [
                    {
                        "name": "read_file",
                        "input_schema": {"type": "object", "properties": {}},
                    }
                ],
                "messages": [
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "toolu_read_001",
                                "name": "read_file",
                                "input": {"path": "fixture.txt"},
                            }
                        ],
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_read_001",
                                "content": "SANITIZED_TOOL_RESULT",
                            }
                        ],
                    },
                ],
            }
        )

        payload = json.loads(result.body)
        self.assertEqual(payload["max_tokens"], 64)
        self.assertEqual(payload["messages"][0]["tool_calls"][0]["id"], "toolu_read_001")
        self.assertEqual(payload["messages"][1], {
            "role": "tool",
            "tool_call_id": "toolu_read_001",
            "content": "SANITIZED_TOOL_RESULT",
        })
        self.assertEqual(payload["tools"][0]["function"]["name"], "read_file")

    def test_chat_tool_stream_returns_anthropic_tool_use_events(self):
        records = chat_chunks_to_messages_sse(
            [
                {
                    "id": "chatcmpl_tool_001",
                    "model": "chat-test",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "toolu_read_001",
                                        "type": "function",
                                        "function": {
                                            "name": "read_file",
                                            "arguments": '{"path":',
                                        },
                                    }
                                ]
                            },
                            "finish_reason": None,
                        }
                    ],
                },
                {
                    "id": "chatcmpl_tool_001",
                    "model": "chat-test",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {"index": 0, "function": {"arguments": '"fixture.txt"}'}}
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                    "usage": {"prompt_tokens": 9, "completion_tokens": 4},
                },
            ]
        )

        events = _decode_sse(records)
        self.assertEqual(events[1][1]["content_block"]["id"], "toolu_read_001")
        self.assertEqual(events[2][1]["delta"]["partial_json"], '{"path":')
        self.assertEqual(events[3][1]["delta"]["partial_json"], '"fixture.txt"}')
        self.assertEqual(events[-2][1]["usage"], {"input_tokens": 9, "output_tokens": 4})

    def test_unknown_chat_delta_fails_explicitly(self):
        with self.assertRaisesRegex(ValueError, "Unsupported Chat Completions delta field: reasoning_content"):
            chat_chunks_to_messages_sse(
                [
                    {
                        "id": "chatcmpl_reasoning_001",
                        "model": "chat-test",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"reasoning_content": "<redacted>"},
                                "finish_reason": "stop",
                            }
                        ],
                    }
                ]
            )

    def test_chat_cache_usage_details_are_explicitly_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unsupported Chat Completions usage field: prompt_tokens_details"):
            chat_chunks_to_messages_sse(
                [
                    {
                        "id": "chatcmpl_usage_detail_001",
                        "model": "chat-test",
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                        "usage": {
                            "prompt_tokens": 5,
                            "completion_tokens": 2,
                            "prompt_tokens_details": {"cached_tokens": 1},
                        },
                    }
                ]
            )

    def test_present_non_list_chat_choices_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "Chat Completions choices is not a list"):
            chat_chunks_to_messages_sse(
                [
                    {
                        "id": "chatcmpl_bad_choices_001",
                        "model": "chat-test",
                        "choices": {"index": 0},
                    }
                ]
            )

    def test_present_non_object_chat_choice_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "Chat Completions choice is not an object"):
            chat_chunks_to_messages_sse(
                [
                    {
                        "id": "chatcmpl_bad_choice_001",
                        "model": "chat-test",
                        "choices": ["not-an-object"],
                    }
                ]
            )

    def test_present_non_object_chat_delta_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "Chat Completions delta is not an object"):
            chat_chunks_to_messages_sse(
                [
                    {
                        "id": "chatcmpl_bad_delta_001",
                        "model": "chat-test",
                        "choices": [
                            {
                                "index": 0,
                                "delta": "not-an-object",
                                "finish_reason": "stop",
                            }
                        ],
                    }
                ]
            )

    def test_present_non_string_chat_content_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "Chat Completions delta content is not a string or null"):
            chat_chunks_to_messages_sse(
                [
                    {
                        "id": "chatcmpl_bad_content_001",
                        "model": "chat-test",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": ["not", "a", "string"]},
                                "finish_reason": "stop",
                            }
                        ],
                    }
                ]
            )

    def test_present_non_list_chat_tool_calls_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "Chat Completions delta tool_calls is not a list or null"):
            chat_chunks_to_messages_sse(
                [
                    {
                        "id": "chatcmpl_bad_tool_calls_001",
                        "model": "chat-test",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"tool_calls": {"index": 0}},
                                "finish_reason": "tool_calls",
                            }
                        ],
                    }
                ]
            )

    def test_present_invalid_chat_chunk_identity_fields_fail_closed(self):
        invalid_fields = (
            ("id", 7, "Chat Completions chunk id is not a non-empty string"),
            ("model", [], "Chat Completions chunk model is not a non-empty string"),
        )

        for field, value, error in invalid_fields:
            with self.subTest(field=field):
                chunk = {
                    "id": "chatcmpl_valid_identity_001",
                    "model": "chat-test",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                }
                chunk[field] = value
                with self.assertRaisesRegex(ValueError, error):
                    chat_chunks_to_messages_sse([chunk])

    def test_present_non_string_chat_finish_reason_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "Chat Completions finish_reason is not a string or null"):
            chat_chunks_to_messages_sse(
                [
                    {
                        "id": "chatcmpl_bad_finish_reason_001",
                        "model": "chat-test",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"content": "SANITIZED_REPLY"},
                                "finish_reason": 7,
                            }
                        ],
                    }
                ]
            )

    def test_present_invalid_chat_tool_call_index_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "Chat Completions tool call index is not a non-negative integer"):
            chat_chunks_to_messages_sse(
                [
                    {
                        "id": "chatcmpl_bad_tool_index_001",
                        "model": "chat-test",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": "0",
                                            "id": "toolu_bad_index_001",
                                            "type": "function",
                                            "function": {"name": "read_file", "arguments": "{}"},
                                        }
                                    ]
                                },
                                "finish_reason": "tool_calls",
                            }
                        ],
                    }
                ]
            )

    def test_present_non_string_chat_function_arguments_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "Chat Completions function arguments is not a string or null"):
            chat_chunks_to_messages_sse(
                [
                    {
                        "id": "chatcmpl_bad_arguments_001",
                        "model": "chat-test",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "id": "toolu_bad_arguments_001",
                                            "type": "function",
                                            "function": {"name": "read_file", "arguments": {}},
                                        }
                                    ]
                                },
                                "finish_reason": "tool_calls",
                            }
                        ],
                    }
                ]
            )

    def test_chat_tool_continuation_rejects_invalid_or_changed_identity(self):
        continuation_cases = (
            (
                "invalid_id",
                {"index": 0, "id": 7, "function": {"arguments": '"fixture.txt"}'}},
                "Chat Completions tool call id is not a non-empty string or null",
            ),
            (
                "changed_id",
                {
                    "index": 0,
                    "id": "toolu_other_001",
                    "function": {"arguments": '"fixture.txt"}'},
                },
                "Chat Completions tool call id changed for index 0",
            ),
            (
                "invalid_name",
                {"index": 0, "function": {"name": 7, "arguments": '"fixture.txt"}'}},
                "Chat Completions function name is not a non-empty string or null",
            ),
            (
                "changed_name",
                {
                    "index": 0,
                    "function": {"name": "write_file", "arguments": '"fixture.txt"}'},
                },
                "Chat Completions function name changed for index 0",
            ),
        )

        for case, continuation, error in continuation_cases:
            with self.subTest(case=case):
                with self.assertRaisesRegex(ValueError, error):
                    chat_chunks_to_messages_sse(
                        [
                            {
                                "id": "chatcmpl_identity_001",
                                "model": "chat-test",
                                "choices": [
                                    {
                                        "index": 0,
                                        "delta": {
                                            "tool_calls": [
                                                {
                                                    "index": 0,
                                                    "id": "toolu_identity_001",
                                                    "type": "function",
                                                    "function": {
                                                        "name": "read_file",
                                                        "arguments": '{"path":',
                                                    },
                                                }
                                            ]
                                        },
                                        "finish_reason": None,
                                    }
                                ],
                            },
                            {
                                "id": "chatcmpl_identity_001",
                                "model": "chat-test",
                                "choices": [
                                    {
                                        "index": 0,
                                        "delta": {"tool_calls": [continuation]},
                                        "finish_reason": "tool_calls",
                                    }
                                ],
                            },
                        ]
                    )

    def test_chat_tool_call_id_cannot_be_reused_by_another_index(self):
        with self.assertRaisesRegex(
            ValueError,
            "Chat Completions tool call id reused for indexes 0 and 1",
        ):
            chat_chunks_to_messages_sse(
                [
                    {
                        "id": "chatcmpl_duplicate_tool_id_001",
                        "model": "chat-test",
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": 0,
                                            "id": "toolu_duplicate_001",
                                            "type": "function",
                                            "function": {"name": "read_file", "arguments": "{}"},
                                        },
                                        {
                                            "index": 1,
                                            "id": "toolu_duplicate_001",
                                            "type": "function",
                                            "function": {"name": "write_file", "arguments": "{}"},
                                        },
                                    ]
                                },
                                "finish_reason": "tool_calls",
                            }
                        ],
                    }
                ]
            )

    def test_present_invalid_known_chat_discriminators_fail_closed(self):
        invalid_chunks = (
            (
                "choice_index",
                {
                    "id": "chatcmpl_bad_choice_index_001",
                    "model": "chat-test",
                    "choices": [{"index": "0", "delta": {}, "finish_reason": "stop"}],
                },
                "Chat Completions choice index is not a non-negative integer",
            ),
            (
                "delta_role",
                {
                    "id": "chatcmpl_bad_role_001",
                    "model": "chat-test",
                    "choices": [
                        {"index": 0, "delta": {"role": 7}, "finish_reason": "stop"}
                    ],
                },
                "Chat Completions delta role is not assistant or null",
            ),
            (
                "tool_call_type",
                {
                    "id": "chatcmpl_bad_tool_type_001",
                    "model": "chat-test",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "toolu_bad_type_001",
                                        "type": "custom",
                                        "function": {"name": "read_file", "arguments": "{}"},
                                    }
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                },
                "Chat Completions tool call type is not function or null",
            ),
        )

        for case, chunk, error in invalid_chunks:
            with self.subTest(case=case):
                with self.assertRaisesRegex(ValueError, error):
                    chat_chunks_to_messages_sse([chunk])

    def test_unsupported_chat_choice_cardinality_fails_closed(self):
        invalid_streams = (
            (
                "nonzero_index",
                [
                    {
                        "id": "chatcmpl_choice_index_001",
                        "model": "chat-test",
                        "choices": [{"index": 1, "delta": {}, "finish_reason": "stop"}],
                    }
                ],
                "Unsupported Chat Completions choice index: 1",
            ),
            (
                "multiple_choices",
                [
                    {
                        "id": "chatcmpl_multiple_choices_001",
                        "model": "chat-test",
                        "choices": [
                            {"index": 0, "delta": {"content": "FIRST"}, "finish_reason": "stop"},
                            {"index": 1, "delta": {"content": "SECOND"}, "finish_reason": "stop"},
                        ],
                    }
                ],
                "Multiple Chat Completions choices are unsupported",
            ),
        )

        for case, chunks, error in invalid_streams:
            with self.subTest(case=case):
                with self.assertRaisesRegex(ValueError, error):
                    chat_chunks_to_messages_sse(chunks)

    def test_chat_chunk_identity_cannot_change_during_stream(self):
        conflicts = (
            (
                "id",
                "chatcmpl_identity_other_001",
                "Chat Completions chunk id changed during stream",
            ),
            (
                "model",
                "chat-other",
                "Chat Completions chunk model changed during stream",
            ),
        )

        for field, conflicting_value, error in conflicts:
            with self.subTest(field=field):
                terminal_chunk = {
                    "id": "chatcmpl_stable_identity_001",
                    "model": "chat-test",
                    "choices": [
                        {"index": 0, "delta": {"content": "SECOND"}, "finish_reason": "stop"}
                    ],
                }
                terminal_chunk[field] = conflicting_value
                with self.assertRaisesRegex(ValueError, error):
                    chat_chunks_to_messages_sse(
                        [
                            {
                                "id": "chatcmpl_stable_identity_001",
                                "model": "chat-test",
                                "choices": [
                                    {
                                        "index": 0,
                                        "delta": {"content": "FIRST"},
                                        "finish_reason": None,
                                    }
                                ],
                            },
                            terminal_chunk,
                        ]
                    )

    def test_unsupported_chat_finish_reasons_fail_closed(self):
        for finish_reason in ("length", "content_filter", "function_call"):
            with self.subTest(finish_reason=finish_reason):
                with self.assertRaisesRegex(
                    ValueError,
                    f"Unsupported Chat Completions finish_reason: {finish_reason}",
                ):
                    chat_chunks_to_messages_sse(
                        [
                            {
                                "id": "chatcmpl_unsupported_finish_001",
                                "model": "chat-test",
                                "choices": [
                                    {
                                        "index": 0,
                                        "delta": {"content": "SANITIZED_REPLY"},
                                        "finish_reason": finish_reason,
                                    }
                                ],
                            }
                        ]
                    )

    def test_unsupported_chat_container_fields_fail_closed(self):
        invalid_chunks = (
            (
                "chunk",
                {
                    "id": "chatcmpl_unknown_chunk_field_001",
                    "model": "chat-test",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    "service_tier": "priority",
                },
                "Unsupported Chat Completions chunk field: service_tier",
            ),
            (
                "choice",
                {
                    "id": "chatcmpl_unknown_choice_field_001",
                    "model": "chat-test",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {},
                            "finish_reason": "stop",
                            "logprobs": {"content": []},
                        }
                    ],
                },
                "Unsupported Chat Completions choice field: logprobs",
            ),
            (
                "delta",
                {
                    "id": "chatcmpl_unsupported_refusal_001",
                    "model": "chat-test",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"refusal": "SANITIZED_REFUSAL"},
                            "finish_reason": "stop",
                        }
                    ],
                },
                "Unsupported Chat Completions delta field: refusal",
            ),
            (
                "tool_call",
                {
                    "id": "chatcmpl_unknown_tool_field_001",
                    "model": "chat-test",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "toolu_unknown_field_001",
                                        "type": "function",
                                        "function": {"name": "read_file", "arguments": "{}"},
                                        "provider_detail": {},
                                    }
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                },
                "Unsupported Chat Completions tool call field: provider_detail",
            ),
            (
                "function",
                {
                    "id": "chatcmpl_unknown_function_field_001",
                    "model": "chat-test",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "toolu_unknown_function_field_001",
                                        "type": "function",
                                        "function": {
                                            "name": "read_file",
                                            "arguments": "{}",
                                            "provider_detail": {},
                                        },
                                    }
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                },
                "Unsupported Chat Completions function field: provider_detail",
            ),
        )

        for scope, chunk, error in invalid_chunks:
            with self.subTest(scope=scope):
                with self.assertRaisesRegex(ValueError, error):
                    chat_chunks_to_messages_sse([chunk])

    def test_absent_and_nullable_chat_stream_fields_remain_permitted(self):
        records = chat_chunks_to_messages_sse(
            [
                {
                    "id": "chatcmpl_optional_fields_001",
                    "model": "chat-test",
                    "usage": None,
                    "service_tier": None,
                    "system_fingerprint": None,
                },
                {
                    "id": "chatcmpl_optional_fields_001",
                    "model": "chat-test",
                    "choices": [
                        {
                            "delta": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": None,
                                "function_call": None,
                                "refusal": None,
                            },
                            "finish_reason": None,
                            "logprobs": None,
                        }
                    ],
                },
                {
                    "id": "chatcmpl_optional_fields_001",
                    "model": "chat-test",
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "id": "toolu_optional_fields_001",
                                        "type": "function",
                                        "function": {
                                            "name": "read_file",
                                            "arguments": '{"path":',
                                        },
                                    }
                                ]
                            },
                            "finish_reason": None,
                        }
                    ],
                },
                {
                    "id": "chatcmpl_optional_fields_001",
                    "model": "chat-test",
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "id": None,
                                        "type": None,
                                        "function": {
                                            "name": None,
                                            "arguments": '"fixture.txt"}',
                                        },
                                    }
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ],
                },
            ]
        )

        events = _decode_sse(records)
        self.assertEqual(events[1][1]["content_block"]["id"], "toolu_optional_fields_001")
        self.assertEqual(
            [payload["delta"]["partial_json"] for event, payload in events if event == "content_block_delta"],
            ['{"path":', '"fixture.txt"}'],
        )


class InMemoryUpstreamExerciseTests(unittest.TestCase):
    def test_responses_upstream_exercise_keeps_the_gateway_seam_in_memory(self):
        exchange = exercise_responses_upstream(
            {
                "model": "gateway-responses-model",
                "max_tokens": 32,
                "messages": [{"role": "user", "content": "SANITIZED_TEXT"}],
            },
            [
                {"type": "response.created", "response": {"id": "resp_001", "model": "gpt-test"}},
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {"id": "msg_001", "type": "message", "role": "assistant", "content": []},
                },
                {"type": "response.output_text.delta", "output_index": 0, "delta": "SANITIZED_REPLY"},
                {
                    "type": "response.output_item.done",
                    "output_index": 0,
                    "item": {"id": "msg_001", "type": "message", "role": "assistant", "content": []},
                },
                {
                    "type": "response.completed",
                    "response": {"id": "resp_001", "model": "gpt-test", "usage": {}},
                },
            ],
        )

        self.assertEqual(exchange.upstream_kind, "responses")
        self.assertTrue(exchange.translation.forwardable)
        self.assertIn("input", json.loads(exchange.outbound_body))
        self.assertEqual(_decode_sse(exchange.downstream_sse)[2][1]["delta"]["text"], "SANITIZED_REPLY")

    def test_chat_provider_exercise_uses_chat_request_and_tool_sse(self):
        exchange = exercise_chat_completions_upstream(
            {
                "model": "gateway-chat-model",
                "max_tokens": 32,
                "messages": [{"role": "user", "content": "SANITIZED_TEXT"}],
            },
            [
                {
                    "id": "chatcmpl_001",
                    "model": "chat-test",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": "SANITIZED_REPLY"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 2},
                }
            ],
        )

        self.assertEqual(exchange.upstream_kind, "chat_completions")
        self.assertTrue(exchange.translation.forwardable)
        self.assertIn("messages", json.loads(exchange.outbound_body))
        self.assertEqual(_decode_sse(exchange.downstream_sse)[2][1]["delta"]["text"], "SANITIZED_REPLY")


class SafetyPolicyTests(unittest.TestCase):
    def test_open_headers_are_classified_and_credentials_are_redacted(self):
        policy = classify_claude_headers(
            {
                "Authorization": "Bearer SHOULD_NOT_APPEAR",
                "anthropic-version": "2023-06-01",
                "anthropic-beta": "future-capability",
                "x-claude-code-session-id": "session-private-value",
                "x-claude-code-future-field": "opaque-value",
                "x-custom-header": "custom-private-value",
            },
            upstream_format="responses",
        )

        self.assertEqual(policy.sanitized["authorization"], "<redacted>")
        self.assertEqual(policy.sanitized["x-claude-code-session-id"], "<pseudonymized>")
        self.assertEqual(policy.semantic_headers["anthropic-beta"], "future-capability")
        self.assertEqual(policy.semantic_headers["x-claude-code-future-field"], "opaque-value")
        self.assertIn("anthropic-version", policy.consumed)
        self.assertIn("x-claude-code-future-field", policy.consumed)
        self.assertEqual(policy.unsupported, ("header.anthropic-beta", "header.x-custom-header"))

    def test_upstream_error_becomes_an_anthropic_sse_error(self):
        records = upstream_error_to_messages_sse(
            529,
            {"error": {"type": "server_error", "message": "SANITIZED_OVERLOAD"}},
        )

        event_name, payload = _decode_sse(records)[0]
        self.assertEqual(event_name, "error")
        self.assertEqual(payload, {
            "type": "error",
            "error": {"type": "overloaded_error", "message": "SANITIZED_OVERLOAD"},
        })

    def test_compatibility_matrix_keeps_high_risk_features_explicit(self):
        matrix = compatibility_matrix()

        self.assertEqual(matrix["text"], CompatibilityStatus.ADAPTED)
        self.assertEqual(matrix["thinking"], CompatibilityStatus.UNSUPPORTED)
        self.assertEqual(matrix["beta_fields"], CompatibilityStatus.UNSUPPORTED)
        self.assertEqual(matrix["cancellation"], CompatibilityStatus.UNKNOWN)
        self.assertEqual(matrix["count_tokens"], CompatibilityStatus.UNSUPPORTED)

    def test_sanitized_trace_fixture_has_required_shapes_without_credentials(self):
        fixture_path = Path(__file__).parent / "fixtures" / "anthropic_messages_spike_trace.json"
        fixture_text = fixture_path.read_text(encoding="utf-8")
        fixture = json.loads(fixture_text)

        self.assertTrue(fixture["sanitized"])
        self.assertEqual(fixture["protocol"]["anthropic_version"], "2023-06-01")
        self.assertIn("response_json", fixture["text_stream"])
        self.assertIn("sse_event_order", fixture["text_stream"])
        self.assertTrue(fixture["tool_follow_up"]["next_request"]["history_contains_same_tool_id"])
        self.assertFalse(fixture["cancellation"]["synthesizes_message_stop"])
        self.assertIsNone(re.search(r"(?i)(sk-[a-z0-9]|gho_[a-z0-9]|bearer\s+[a-z0-9])", fixture_text))

    def test_real_cli_loopback_trace_records_partial_input_and_tool_id_evidence(self):
        fixture_path = Path(__file__).parent / "fixtures" / "claude_messages_real_cli_smoke.json"
        fixture_text = fixture_path.read_text(encoding="utf-8")
        fixture = json.loads(fixture_text)
        scenarios = {item["scenario"]: item for item in fixture["scenarios"]}

        self.assertTrue(fixture["sanitized"])
        self.assertEqual(fixture["capture_kind"], "real_cli_to_local_loopback_prototype")
        self.assertTrue(fixture["capture_succeeded"])
        self.assertFalse(fixture["full_compatibility"])
        self.assertEqual(fixture["compatibility_outcome"], "scoped PARTIAL")
        self.assertTrue(scenarios["text"]["client"]["expected_output_seen"])
        self.assertFalse(scenarios["text"]["request_summaries"][0]["messages_to_responses_forwardable"])
        self.assertTrue(scenarios["tool"]["tool_follow_up_verified"])
        self.assertTrue(scenarios["tool"]["request_summaries"][1]["tool_result_is_first_content_block"])
        self.assertTrue(scenarios["cancel"]["disconnect_observed"])
        self.assertIsNone(re.search(r"(?i)(sk-[a-z0-9]|gho_[a-z0-9]|bearer\s+[a-z0-9])", fixture_text))

    def test_tool_call_result_history_keeps_the_same_call_id(self):
        result = messages_to_responses(
            {
                "model": "gateway-tool-model",
                "max_tokens": 128,
                "system": [{"type": "text", "text": "SANITIZED_SYSTEM"}],
                "tools": [
                    {
                        "name": "read_file",
                        "description": "Read a fixture file",
                        "input_schema": {
                            "type": "object",
                            "properties": {"path": {"type": "string"}},
                            "required": ["path"],
                        },
                    }
                ],
                "messages": [
                    {"role": "user", "content": [{"type": "text", "text": "SANITIZED_FIRST"}]},
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "toolu_read_001",
                                "name": "read_file",
                                "input": {"path": "fixture.txt"},
                            }
                        ],
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_read_001",
                                "content": "SANITIZED_TOOL_RESULT",
                            }
                        ],
                    },
                    {"role": "user", "content": [{"type": "text", "text": "SANITIZED_FOLLOW_UP"}]},
                ],
            }
        )

        payload = json.loads(result.body)
        self.assertEqual(payload["instructions"], "SANITIZED_SYSTEM")
        self.assertEqual(payload["tools"][0]["name"], "read_file")
        self.assertEqual(payload["input"][1]["call_id"], "toolu_read_001")
        self.assertEqual(payload["input"][2], {
            "type": "function_call_output",
            "call_id": "toolu_read_001",
            "output": "SANITIZED_TOOL_RESULT",
        })
        self.assertEqual(payload["input"][3]["content"][0]["text"], "SANITIZED_FOLLOW_UP")
        self.assertEqual(result.unsupported, ())
