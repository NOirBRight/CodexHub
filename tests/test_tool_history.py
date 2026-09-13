"""Direct behavior tests for the transcript-rendering owner."""

from __future__ import annotations

import json

import pytest

import tool_history
from protocol_translation import UnsupportedProtocolTranslationError


def _content(message: dict) -> str:
    return message["content"]


def test_internal_message_renders_custom_tool_call_transcript() -> None:
    message = tool_history.internal_message(
        {
            "type": "custom_tool_call",
            "name": "apply_patch",
            "call_id": "call_patch",
            "status": "completed",
            "input": "*** Begin Patch",
        }
    )
    assert message is not None
    assert message["type"] == "message"
    assert message["role"] == "developer"
    assert _content(message) == (
        "Read-only Codex tool call transcript\n"
        "tool: apply_patch\n"
        "call_id: call_patch\n"
        "status: completed\n"
        "input:\n"
        "*** Begin Patch"
    )


def test_internal_message_renders_function_call_and_result_transcripts() -> None:
    call = tool_history.internal_message(
        {
            "type": "function_call",
            "namespace": "multi_agent_v1",
            "name": "spawn_agent",
            "call_id": "call_1",
            "arguments": {"agent_type": "default"},
        }
    )
    assert call is not None
    assert _content(call).startswith("Read-only Codex function call transcript")
    assert "namespace: multi_agent_v1" in _content(call)
    assert "function: spawn_agent" in _content(call)
    assert json.dumps({"agent_type": "default"}, separators=(",", ":")) in _content(call)

    result = tool_history.internal_message(
        {"type": "function_call_output", "call_id": "call_1", "output": "done"}
    )
    assert result is not None
    assert _content(result).startswith("Read-only Codex function result transcript")
    assert "output:\ndone" in _content(result)


def test_internal_message_renders_tool_search_discovery_transcript() -> None:
    message = tool_history.internal_message(
        {
            "type": "tool_search_output",
            "call_id": "call_search",
            "status": "completed",
            "execution": "client",
            "tools": [
                {
                    "type": "namespace",
                    "name": "multi_agent_v1",
                    "tools": [{"type": "function", "name": "spawn_agent"}],
                }
            ],
        }
    )
    assert message is not None
    assert "status: discovered_codex_native_multi_agent_tools" in _content(message)


def test_internal_message_rejects_structured_media_results() -> None:
    with pytest.raises(UnsupportedProtocolTranslationError):
        tool_history.internal_message(
            {
                "type": "custom_tool_call_output",
                "call_id": "call_patch",
                "output": [{"type": "input_image", "image_url": "https://example.invalid/a.png"}],
            }
        )
    with pytest.raises(UnsupportedProtocolTranslationError):
        tool_history.internal_message(
            {
                "type": "function_call_output",
                "call_id": "call_1",
                "output": {"type": "input_image", "image_url": "https://example.invalid/b.png"},
            }
        )


def test_internal_message_skips_reasoning_and_unknown_items() -> None:
    assert tool_history.internal_message({"type": "reasoning"}) is None
    assert tool_history.internal_message({"type": "message"}) is None
    assert tool_history.internal_message({"type": "custom_tool_call"}) is None


def test_internal_message_renders_compaction_context() -> None:
    message = tool_history.internal_message(
        {"type": "compaction", "summary": [{"type": "summary_text", "text": "kept"}]}
    )
    assert message is not None
    assert _content(message) == "[Compacted conversation context]\nkept"

    opaque = tool_history.internal_message({"type": "compaction", "encrypted_content": "x"})
    assert opaque is not None
    assert _content(opaque).startswith("[Compacted conversation context — opaque")


def test_transcript_message_renders_assistant_output_text() -> None:
    message = tool_history.transcript_message(
        "Invalid Codex function result transcript",
        {"type": "function_call", "name": "mystery", "arguments": "{}"},
    )
    assert message["type"] == "message"
    assert message["role"] == "assistant"
    text = message["content"][0]["text"]
    assert text.startswith("Invalid Codex function result transcript\n")
    assert "name: mystery" in text
