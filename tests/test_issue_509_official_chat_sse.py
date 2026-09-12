from __future__ import annotations

import json

import protocol_translation


def _official_created(*, usage: object = None) -> dict:
    return {
        "type": "response.created",
        "response": {
            "id": "resp_official",
            "model": "gpt-5.6-luna",
            "status": "in_progress",
            "output": None,
            "usage": usage,
        },
    }


def test_official_created_event_with_null_usage_converts_to_chat() -> None:
    converter = protocol_translation.ResponsesToChatStreamConverter()
    chunks = converter.chunks_for_event(_official_created(usage=None))
    assert chunks[0]["object"] == "chat.completion.chunk"
    assert chunks[0]["model"] == "gpt-5.6-luna"


def test_official_message_phase_and_text_logprobs_convert_to_chat_stream() -> None:
    events = [
        _official_created(),
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {
                "id": "msg_1",
                "type": "message",
                "status": "in_progress",
                "role": "assistant",
                "phase": "commentary",
                "content": [],
            },
        },
        {
            "type": "response.content_part.added",
            "output_index": 0,
            "item_id": "msg_1",
            "content_index": 0,
            "part": {
                "type": "output_text",
                "text": "hello",
                "logprobs": {"content": []},
            },
        },
        {
            "type": "response.output_text.delta",
            "item_id": "msg_1",
            "delta": " world",
        },
        {
            "type": "response.completed",
            "response": {
                "id": "resp_official",
                "model": "gpt-5.6-luna",
                "status": "completed",
                "output": [
                    {
                        "id": "msg_1",
                        "type": "message",
                        "status": "completed",
                        "role": "assistant",
                        "phase": "commentary",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "hello world",
                                "logprobs": {"content": []},
                            }
                        ],
                    }
                ],
            },
        },
    ]
    chunks = protocol_translation.response_events_to_chat_stream_chunks(events)
    content = "".join(
        choice.get("delta", {}).get("content") or ""
        for chunk in chunks
        for choice in chunk.get("choices", [])
        if isinstance(choice, dict)
    )
    assert "world" in content
    assert "encrypted_content" not in json.dumps(chunks)


def test_official_completed_body_drops_logprobs_and_keeps_text() -> None:
    body = protocol_translation.response_body_to_chat_completion_body(
        json.dumps(
            {
                "id": "resp_official",
                "model": "gpt-5.6-luna",
                "status": "completed",
                "output": [
                    {
                        "id": "msg_1",
                        "type": "message",
                        "role": "assistant",
                        "phase": "commentary",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "only this",
                                "logprobs": {"content": []},
                            }
                        ],
                    }
                ],
            }
        ).encode()
    )
    payload = json.loads(body)
    assert payload["choices"][0]["message"]["content"] == "only this"
    assert "logprobs" not in json.dumps(payload)
    assert "phase" not in json.dumps(payload)


def test_hosted_web_search_call_converts_to_chat_tool_call() -> None:
    events = [
        {
            "type": "response.created",
            "response": {"id": "resp_search", "model": "muse", "status": "in_progress"},
        },
        {
            "type": "response.output_item.added",
            "item": {
                "id": "ws_1",
                "type": "web_search_call",
                "call_id": "call_ws",
                "status": "in_progress",
                "action": {"query": "Codex CLI"},
            },
        },
        {
            "type": "response.web_search_call.searching",
            "item_id": "ws_1",
        },
        {
            "type": "response.output_item.done",
            "item": {
                "id": "ws_1",
                "type": "web_search_call",
                "call_id": "call_ws",
                "status": "completed",
                "action": {"query": "Codex CLI"},
            },
        },
        {
            "type": "response.output_text.delta",
            "delta": "Codex CLI is the terminal client.",
        },
        {
            "type": "response.completed",
            "response": {
                "id": "resp_search",
                "model": "muse",
                "status": "completed",
                "output": [
                    {
                        "id": "ws_1",
                        "type": "web_search_call",
                        "call_id": "call_ws",
                        "status": "completed",
                        "action": {"query": "Codex CLI"},
                    },
                    {
                        "id": "msg_1",
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "Codex CLI is the terminal client."}],
                    },
                ],
            },
        },
    ]
    chunks = protocol_translation.response_events_to_chat_stream_chunks(events)
    names = [
        call.get("function", {}).get("name")
        for chunk in chunks
        for choice in chunk.get("choices", [])
        for call in (choice.get("delta") or {}).get("tool_calls") or []
        if isinstance(call, dict)
    ]
    content = "".join(
        choice.get("delta", {}).get("content") or ""
        for chunk in chunks
        for choice in chunk.get("choices", [])
        if isinstance(choice, dict)
    )
    assert "web_search" in names
    assert "terminal client" in content
    finish_reasons = [
        choice.get("finish_reason")
        for chunk in chunks
        for choice in chunk.get("choices", [])
        if isinstance(choice, dict) and choice.get("finish_reason")
    ]
    assert finish_reasons[-1] == "tool_calls"

    converter = protocol_translation.ResponsesToChatStreamConverter()
    converted: list[dict] = []
    for event in events:
        converted.extend(converter.chunks_for_event(event))
    converter_reasons = [
        choice.get("finish_reason")
        for chunk in converted
        for choice in chunk.get("choices", [])
        if isinstance(choice, dict) and choice.get("finish_reason")
    ]
    assert converter_reasons[-1] == "tool_calls"

    body = protocol_translation.response_body_to_chat_completion_body(
        json.dumps(
            {
                "id": "resp_search",
                "model": "muse",
                "status": "completed",
                "output": [
                    {
                        "id": "ws_1",
                        "type": "web_search_call",
                        "call_id": "call_ws",
                        "status": "completed",
                        "action": {"query": "Codex CLI"},
                    }
                ],
            }
        ).encode()
    )
    payload = json.loads(body)
    assert payload["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "web_search"
