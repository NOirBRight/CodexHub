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
