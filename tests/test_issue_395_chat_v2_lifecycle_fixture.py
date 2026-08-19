from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import run_issue_283_cli_v2_lifecycle as track_a
import run_issue_395_cli_chat_v2_lifecycle as fixture
from collaboration_runtime_contract import V2_TOOLS


def _aliases() -> dict[str, str]:
    return {
        name: f"__codexhub_ns_fixture_{index}"
        for index, name in enumerate(V2_TOOLS)
    }


def _tools(aliases: dict[str, str]) -> list[dict]:
    return [
        {
            "type": "function",
            "function": {
                "name": aliases[name],
                "description": name,
                "parameters": {"type": "object", "properties": {}},
            },
        }
        for name in V2_TOOLS
    ]


def test_chat_request_reconstructs_exact_v2_history_identities() -> None:
    aliases = _aliases()
    body = {
        "model": "fixture-v2",
        "tools": _tools(aliases),
        "messages": [
            {"role": "user", "content": "start"},
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_spawn",
                        "type": "function",
                        "function": {
                            "name": aliases["spawn_agent"],
                            "arguments": '{"task_name":"worker","message":"work"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_spawn",
                "content": '{"task_name":"/root/worker"}',
            },
        ],
    }

    reconstructed, observed_aliases, is_child = fixture._chat_to_fixture_request(body)

    assert observed_aliases == aliases
    assert is_child is False
    call, output = reconstructed["input"][-2:]
    assert call == {
        "type": "function_call",
        "id": "fc_call_spawn",
        "namespace": "collaboration",
        "name": "spawn_agent",
        "call_id": "call_spawn",
        "arguments": '{"task_name":"worker","message":"work"}',
    }
    assert output["call_id"] == "call_spawn"
    assert output["output"] == '{"task_name":"/root/worker"}'


def test_agent_message_envelope_selects_child_without_transport_metadata() -> None:
    item = {
        "type": "agent_message",
        "id": "agent_message_1",
        "author": "/root",
        "recipient": "/root/worker",
        "content": [
            {
                "type": "input_text",
                "text": "Message Type: NEW_TASK\nPayload:\nwork",
            }
        ],
    }
    body = {
        "model": "fixture-v2",
        "messages": [
            {
                "role": "user",
                "content": fixture.AGENT_MESSAGE_PREFIX
                + json.dumps(item, separators=(",", ":"), sort_keys=True),
            }
        ],
        "tools": [],
    }

    reconstructed, aliases, is_child = fixture._chat_to_fixture_request(body)

    assert aliases == {}
    assert is_child is True
    assert reconstructed["client_metadata"] == {"thread_id": "child:/root/worker"}
    assert reconstructed["input"] == [item]


def test_unknown_chat_alias_fails_closed() -> None:
    aliases = _aliases()
    body = {
        "model": "fixture-v2",
        "tools": _tools(aliases),
        "messages": [
            {
                "role": "assistant",
                "tool_calls": [
                    {
                        "id": "call_unknown",
                        "type": "function",
                        "function": {"name": "__codexhub_ns_unknown_99", "arguments": "{}"},
                    }
                ],
            }
        ],
    }

    with pytest.raises(track_a.CaptureFailure, match="chat_tool_alias_unknown"):
        fixture._chat_to_fixture_request(body)


def test_function_events_become_progressive_chat_tool_call_chunks() -> None:
    aliases = _aliases()
    events = track_a._function_events(
        "send_message",
        {"target": "/root/worker", "message": "status"},
        7,
        "fixture-v2",
    )

    chunks = fixture._events_to_chat_chunks(events, aliases, "fixture-v2")

    assert len(chunks) == 2
    first_call = chunks[0]["choices"][0]["delta"]["tool_calls"][0]
    second_call = chunks[1]["choices"][0]["delta"]["tool_calls"][0]
    assert first_call["id"] == "call_7"
    assert first_call["function"]["name"] == aliases["send_message"]
    arguments = first_call["function"]["arguments"] + second_call["function"]["arguments"]
    assert json.loads(arguments) == {"target": "/root/worker", "message": "status"}
    assert chunks[0]["choices"][0]["finish_reason"] is None
    assert chunks[1]["choices"][0]["finish_reason"] == "tool_calls"


def test_message_events_become_two_ordered_content_chunks() -> None:
    events = track_a._message_events(9, "parent completion", "fixture-v2")

    chunks = fixture._events_to_chat_chunks(events, {}, "fixture-v2")

    assert len(chunks) == 2
    assert "".join(chunk["choices"][0]["delta"].get("content", "") for chunk in chunks) == "parent completion"
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
