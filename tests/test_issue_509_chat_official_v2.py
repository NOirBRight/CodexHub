from __future__ import annotations

import copy
import json

import pytest

from collaboration_runtime_contract import (
    COLLABORATION_V2,
    EXPECTED_PARAMETER_SCHEMAS,
    V2_TOOLS,
)
from protocol_translation import (
    chat_completion_to_response_body,
    chat_completions_request_to_responses_body,
    prepare_exchange,
)
from tool_compatibility.collab_v2 import AGENT_MESSAGE_ENVELOPE_PREFIX
import gateway_compat
import gateway_errors


ARGUMENTS = {
    "followup_task": {"target": "agent/root/worker", "message": "continue"},
    "interrupt_agent": {"target": "agent/root/worker"},
    "list_agents": {},
    "send_message": {"target": "agent/root/worker", "message": "status"},
    "spawn_agent": {"task_name": "worker", "message": "inspect"},
    "wait_agent": {},
}
RESULTS = {
    "followup_task": "",
    "interrupt_agent": json.dumps({"previous_status": "running"}),
    "list_agents": json.dumps({"agents": [{"agent_name": "worker", "agent_status": "running"}]}),
    "send_message": "",
    "spawn_agent": json.dumps({"task_name": "worker"}),
    "wait_agent": json.dumps({"message": "done", "timed_out": False}),
}


def _official() -> dict:
    return {"name": "official", "upstream_format": "responses"}


def _chat_v2_functions() -> list[dict]:
    tools = []
    for name, schema in EXPECTED_PARAMETER_SCHEMAS[COLLABORATION_V2].items():
        parameters = copy.deepcopy(schema)
        tools.append(
            {
                "type": "function",
                "name": name,
                "description": name,
                "strict": False,
                "parameters": parameters,
            }
        )
    return tools


def _chat_request(*, tools: list | None = None, input_items: object = "use collaboration") -> bytes:
    return json.dumps(
        {
            "model": "placeholder",
            "messages": [{"role": "user", "content": "delegate"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool["description"],
                        "parameters": tool["parameters"],
                        "strict": False,
                    },
                }
                for tool in (tools if tools is not None else _chat_v2_functions())
            ],
            "tool_choice": "auto",
        }
    ).encode()


def _prepared_official(*, tools: list | None = None, input_items: object | None = None) -> tuple[dict, dict]:
    converted = chat_completions_request_to_responses_body(_chat_request(tools=tools))
    payload = json.loads(converted)
    if input_items is not None:
        payload["input"] = input_items
    context: dict = {}
    prepared = gateway_compat.compatible_request_body(
        json.dumps(payload).encode(),
        _official(),
        event_context=context,
        inject_codex_tools=False,
    )
    return json.loads(prepared), context


def test_chat_v2_functions_expand_to_official_namespace() -> None:
    first, _ = _prepared_official()
    second, _ = _prepared_official()
    assert first["tools"][0]["type"] == "namespace"
    assert first["tools"][0]["name"] == "collaboration"
    assert {child["name"] for child in first["tools"][0]["tools"]} == set(V2_TOOLS)
    assert first["tools"] == second["tools"]
    assert first["tool_choice"] == "auto"


def test_responses_inbound_official_keeps_native_namespace() -> None:
    children = []
    for name, schema in EXPECTED_PARAMETER_SCHEMAS[COLLABORATION_V2].items():
        parameters = copy.deepcopy(schema)
        children.append(
            {
                "type": "function",
                "name": name,
                "description": name,
                "strict": False,
                "parameters": parameters,
            }
        )
    namespace = {
        "type": "namespace",
        "name": "collaboration",
        "description": "collaboration",
        "tools": children,
    }
    context: dict = {}
    prepared = json.loads(
        gateway_compat.compatible_request_body(
            json.dumps(
                {
                    "model": "placeholder",
                    "input": "delegate",
                    "tools": [namespace],
                    "tool_choice": "auto",
                }
            ).encode(),
            _official(),
            event_context=context,
            inject_codex_tools=False,
        )
    )
    assert prepared["tools"][0]["type"] == "namespace"
    assert prepared["tools"][0]["name"] == "collaboration"
    assert not any(
        isinstance(tool, dict) and str(tool.get("name", "")).startswith("__codexhub_ns_")
        for tool in prepared["tools"]
    )


@pytest.mark.parametrize("name", V2_TOOLS)
def test_official_namespace_calls_collapse_to_chat_function_names(name: str) -> None:
    official_request, context = _prepared_official()
    call = {
        "type": "function_call",
        "id": f"fc_{name}",
        "call_id": f"call_{name}",
        "namespace": "collaboration",
        "name": name,
        "arguments": json.dumps(ARGUMENTS[name]),
        "encrypted_function_args": [],
    }
    decoded = json.loads(
        gateway_compat.compatible_response_body(
            json.dumps({"output": [call]}).encode(),
            "official",
            context,
        )
    )
    item = decoded["output"][0]
    assert item["name"] == name
    chat = json.loads(chat_completion_to_response_body(json.dumps({
        "id": "chatcmpl-v2",
        "object": "chat.completion",
        "created": 1,
        "model": "placeholder",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": f"call_{name}",
                    "type": "function",
                    "function": {"name": item["name"], "arguments": json.dumps(ARGUMENTS[name])},
                }],
            },
            "finish_reason": "tool_calls",
        }],
    }).encode()))
    assert chat["output"][0]["name"] == name
    assert chat["output"][0]["call_id"] == f"call_{name}"


def test_chat_v2_history_gains_official_namespace() -> None:
    call_id = "call_spawn_agent"
    input_items = [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "continue"}]},
        {
            "type": "function_call",
            "id": "item_spawn_agent",
            "call_id": call_id,
            "name": "spawn_agent",
            "arguments": json.dumps(ARGUMENTS["spawn_agent"]),
        },
        {
            "type": "function_call_output",
            "id": "result_spawn_agent",
            "call_id": call_id,
            "output": RESULTS["spawn_agent"],
        },
    ]
    prepared, _ = _prepared_official(input_items=input_items)
    call = next(item for item in prepared["input"] if item.get("type") == "function_call")
    assert call["namespace"] == "collaboration"
    assert call["name"] == "spawn_agent"
    assert call["encrypted_function_args"] == []


def test_incomplete_v2_six_pack_fails_closed() -> None:
    tools = _chat_v2_functions()[:-1]
    with pytest.raises(gateway_errors.UpstreamProtocolTranslationError):
        _prepared_official(tools=tools)


def test_v1_only_chat_tools_fail_closed_on_official() -> None:
    tools = [
        {
            "type": "function",
            "name": "close_agent",
            "description": "close",
            "strict": False,
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        }
    ]
    with pytest.raises(gateway_errors.UpstreamProtocolTranslationError):
        _prepared_official(tools=tools)


def test_plaintext_agent_message_envelope_expands_on_official() -> None:
    original = {
        "type": "agent_message",
        "id": "agent_message_child",
        "author": "agent/root",
        "recipient": "agent/root/worker",
        "content": [{"type": "input_text", "text": "child task"}],
    }
    envelope = AGENT_MESSAGE_ENVELOPE_PREFIX + json.dumps(
        original,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    prepared, context = _prepared_official(
        input_items=[
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": envelope}],
            }
        ]
    )
    assert context["collaboration_protocol"] == COLLABORATION_V2
    assert prepared["input"][0]["type"] == "agent_message"
    assert prepared["input"][0]["id"] == "agent_message_child"


def test_encrypted_agent_message_envelope_fails_closed() -> None:
    original = {
        "type": "agent_message",
        "id": "agent_message_child",
        "author": "agent/root",
        "recipient": "agent/root/worker",
        "content": [{"type": "encrypted_content", "encrypted_content": "secret"}],
    }
    envelope = AGENT_MESSAGE_ENVELOPE_PREFIX + json.dumps(
        original,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    converted = chat_completions_request_to_responses_body(_chat_request())
    payload = json.loads(converted)
    payload["input"] = [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": envelope}],
        }
    ]
    with pytest.raises(gateway_errors.UpstreamProtocolTranslationError):
        gateway_compat.compatible_request_body(
            json.dumps(payload).encode(),
            _official(),
            event_context={},
            inject_codex_tools=False,
        )


def test_prepare_exchange_then_official_expand_round_trips_cache_key() -> None:
    body = json.dumps(
        {
            "model": "placeholder",
            "messages": [{"role": "user", "content": "delegate"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool["description"],
                        "parameters": tool["parameters"],
                        "strict": False,
                    },
                }
                for tool in _chat_v2_functions()
            ],
            "prompt_cache_key": "stable-session",
        }
    ).encode()
    exchange = prepare_exchange(
        body,
        inbound_format="chat_completions",
        outbound_format="responses",
    )
    context: dict = {}
    prepared = json.loads(
        gateway_compat.compatible_request_body(
            exchange.upstream_body,
            _official(),
            event_context=context,
            inject_codex_tools=False,
        )
    )
    assert prepared["tools"][0]["name"] == "collaboration"
