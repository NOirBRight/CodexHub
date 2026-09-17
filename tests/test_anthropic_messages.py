from __future__ import annotations

import json

import pytest

from anthropic_messages import (
    AnthropicToChatStreamConverter,
    anthropic_message_to_chat_completion_body,
    bind_request_headers,
    chat_request_to_anthropic_body,
)
from protocol_translation import UnsupportedProtocolTranslationError


def test_bind_request_headers_adds_x_api_key_on_messages() -> None:
    headers = bind_request_headers(
        {"Authorization": "Bearer sk-test", "User-Agent": "CodexHub/test"},
        "https://opencode.ai/zen/go/v1/messages",
    )
    assert headers["x-api-key"] == "sk-test"
    assert headers["anthropic-version"] == "2023-06-01"
    assert headers["Authorization"] == "Bearer sk-test"


def test_bind_request_headers_ignores_chat_completions() -> None:
    headers = bind_request_headers(
        {"Authorization": "Bearer sk-test"},
        "https://opencode.ai/zen/go/v1/chat/completions",
    )
    assert "x-api-key" not in headers


def test_chat_request_to_anthropic_body_moves_system_and_tools() -> None:
    body = chat_request_to_anthropic_body(
        json.dumps(
            {
                "model": "union-alpha",
                "messages": [
                    {"role": "system", "content": "Be concise."},
                    {"role": "user", "content": "hi"},
                ],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "description": "Read a file",
                            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
                        },
                    }
                ],
                "stream": True,
            }
        ).encode()
    )
    payload = json.loads(body)
    assert payload["system"] == "Be concise."
    assert payload["messages"] == [{"role": "user", "content": "hi"}]
    assert payload["tools"][0]["name"] == "read_file"
    assert payload["stream"] is True


def test_anthropic_message_to_chat_completion_body_round_trips_text() -> None:
    body = anthropic_message_to_chat_completion_body(
        json.dumps(
            {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "union-alpha",
                "content": [{"type": "text", "text": "pong"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 5},
            }
        ).encode()
    )
    payload = json.loads(body)
    assert payload["choices"][0]["message"]["content"] == "pong"
    assert payload["choices"][0]["finish_reason"] == "stop"


def test_anthropic_stream_converter_emits_text_deltas() -> None:
    converter = AnthropicToChatStreamConverter()
    start = converter.chat_payloads_for_sse(
        "message_start",
        {"type": "message_start", "message": {"id": "msg_1", "model": "union-alpha"}},
    )
    deltas = converter.chat_payloads_for_sse(
        "content_block_delta",
        {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "pong"}},
    )
    done = converter.chat_payloads_for_sse("message_stop", {"type": "message_stop"})
    assert start == []
    assert deltas[0]["choices"][0]["delta"]["content"] == "pong"
    assert done[-1] == "[DONE]"


def _chat(payload: dict) -> dict:
    return json.loads(chat_request_to_anthropic_body(json.dumps(payload).encode()))


def test_tool_choice_auto_passes_through() -> None:
    payload = _chat(
        {
            "model": "union-alpha",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [
                {
                    "type": "function",
                    "function": {"name": "read_file", "parameters": {"type": "object"}},
                }
            ],
            "tool_choice": "auto",
        }
    )
    assert payload["tool_choice"] == {"type": "auto"}


def test_tool_choice_none_omits_anthropic_field() -> None:
    payload = _chat(
        {
            "model": "union-alpha",
            "messages": [{"role": "user", "content": "hi"}],
            "tool_choice": "none",
        }
    )
    assert "tool_choice" not in payload


def test_tool_choice_without_tools_fails_closed() -> None:
    with pytest.raises(UnsupportedProtocolTranslationError, match="without tools"):
        chat_request_to_anthropic_body(
            json.dumps(
                {
                    "model": "union-alpha",
                    "messages": [{"role": "user", "content": "hi"}],
                    "tool_choice": "auto",
                }
            ).encode()
        )


def test_named_tool_choice_maps_to_anthropic_tool() -> None:
    payload = _chat(
        {
            "model": "union-alpha",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [
                {
                    "type": "function",
                    "function": {"name": "read_file", "parameters": {"type": "object"}},
                }
            ],
            "tool_choice": {"type": "function", "function": {"name": "read_file"}},
        }
    )
    assert payload["tool_choice"] == {"type": "tool", "name": "read_file"}


def test_unknown_tool_choice_fails_closed() -> None:
    with pytest.raises(UnsupportedProtocolTranslationError, match="tool_choice"):
        chat_request_to_anthropic_body(
            json.dumps(
                {
                    "model": "union-alpha",
                    "messages": [{"role": "user", "content": "hi"}],
                    "tool_choice": {"type": "computer_use", "name": "x"},
                }
            ).encode()
        )


def test_unknown_chat_field_fails_closed() -> None:
    with pytest.raises(UnsupportedProtocolTranslationError, match="without losing them"):
        chat_request_to_anthropic_body(
            json.dumps(
                {
                    "model": "union-alpha",
                    "messages": [{"role": "user", "content": "hi"}],
                    "prompt_cache_options": {"ttl": "5m"},
                }
            ).encode()
        )


def test_omitted_max_tokens_uses_union_alpha_catalog_cap() -> None:
    payload = _chat(
        {
            "model": "union-alpha",
            "messages": [{"role": "user", "content": "hi"}],
        }
    )
    assert payload["max_tokens"] == 131072


def test_caller_max_tokens_wins_over_catalog() -> None:
    payload = _chat(
        {
            "model": "union-alpha",
            "max_tokens": 128,
            "messages": [{"role": "user", "content": "hi"}],
        }
    )
    assert payload["max_tokens"] == 128


def test_https_image_url_becomes_anthropic_image_source() -> None:
    payload = _chat(
        {
            "model": "union-alpha",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "what is this"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "https://example.test/shot.png"},
                        },
                    ],
                }
            ],
        }
    )
    blocks = payload["messages"][0]["content"]
    assert blocks[0] == {"type": "text", "text": "what is this"}
    assert blocks[1] == {
        "type": "image",
        "source": {"type": "url", "url": "https://example.test/shot.png"},
    }


def test_data_uri_image_becomes_base64_source() -> None:
    payload = _chat(
        {
            "model": "union-alpha",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "data:image/png;base64,aGVsbG8=",
                            },
                        }
                    ],
                }
            ],
        }
    )
    assert payload["messages"][0]["content"][0] == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "aGVsbG8="},
    }


def test_image_detail_is_dropped_as_declared_adaptation() -> None:
    payload = _chat(
        {
            "model": "union-alpha",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "https://example.test/shot.png",
                                "detail": "high",
                            },
                        }
                    ],
                }
            ],
        }
    )
    source = payload["messages"][0]["content"][0]["source"]
    assert source == {"type": "url", "url": "https://example.test/shot.png"}


def test_reasoning_effort_enables_anthropic_thinking_budget() -> None:
    payload = _chat(
        {
            "model": "union-alpha",
            "reasoning_effort": "xhigh",
            "messages": [{"role": "user", "content": "hi"}],
        }
    )
    assert payload["thinking"] == {"type": "enabled", "budget_tokens": 16384}


def test_thinking_disabled_omits_anthropic_thinking() -> None:
    payload = _chat(
        {
            "model": "union-alpha",
            "thinking": {"type": "disabled"},
            "messages": [{"role": "user", "content": "hi"}],
        }
    )
    assert "thinking" not in payload


def test_reasoning_details_rebuild_signed_thinking_blocks() -> None:
    payload = _chat(
        {
            "model": "union-alpha",
            "messages": [
                {
                    "role": "assistant",
                    "content": "ok",
                    "reasoning_content": "plan",
                    "reasoning_details": [
                        {
                            "type": "anthropic_thinking",
                            "thinking": "plan",
                            "signature": "sig-1",
                        }
                    ],
                },
                {"role": "user", "content": "go"},
            ],
        }
    )
    blocks = payload["messages"][0]["content"]
    assert blocks[0] == {"type": "thinking", "thinking": "plan", "signature": "sig-1"}
    assert blocks[1] == {"type": "text", "text": "ok"}


def test_anthropic_thinking_block_without_signature_fails_closed() -> None:
    with pytest.raises(UnsupportedProtocolTranslationError, match="signature"):
        chat_request_to_anthropic_body(
            json.dumps(
                {
                    "model": "union-alpha",
                    "messages": [
                        {
                            "role": "assistant",
                            "content": "ok",
                            "reasoning_details": [
                                {"type": "anthropic_thinking", "thinking": "plan"}
                            ],
                        }
                    ],
                }
            ).encode()
        )


def test_anthropic_message_maps_thinking_to_reasoning_details() -> None:
    payload = json.loads(
        anthropic_message_to_chat_completion_body(
            json.dumps(
                {
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "model": "union-alpha",
                    "content": [
                        {
                            "type": "thinking",
                            "thinking": "plan",
                            "signature": "sig-1",
                        },
                        {"type": "text", "text": "pong"},
                    ],
                    "stop_reason": "end_turn",
                }
            ).encode()
        )
    )
    message = payload["choices"][0]["message"]
    assert message["content"] == "pong"
    assert message["reasoning_content"] == "plan"
    assert message["reasoning_details"] == [
        {"type": "anthropic_thinking", "thinking": "plan", "signature": "sig-1"}
    ]


def test_chat_chunks_helper_passes_plain_chat_through() -> None:
    from anthropic_messages import chat_chunks_for_sse_event

    assert chat_chunks_for_sse_event(None, None, None) == []
    done = chat_chunks_for_sse_event(None, "[DONE]", None)
    assert done == ["[DONE]"]
    payload = {"id": "chatcmpl_1"}
    assert chat_chunks_for_sse_event(None, payload, None) == [payload]


def test_anthropic_stream_converter_emits_thinking_deltas() -> None:
    converter = AnthropicToChatStreamConverter()
    converter.chat_payloads_for_sse(
        "content_block_start",
        {"type": "content_block_start", "content_block": {"type": "thinking"}},
    )
    deltas = converter.chat_payloads_for_sse(
        "content_block_delta",
        {
            "type": "content_block_delta",
            "delta": {"type": "thinking_delta", "thinking": "plan"},
        },
    )
    assert deltas[0]["choices"][0]["delta"]["reasoning_content"] == "plan"
