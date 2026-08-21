from __future__ import annotations

import copy
import json

import pytest

import codex_proxy
from collaboration_runtime_contract import (
    COLLABORATION_V2,
    EXPECTED_PARAMETER_SCHEMAS,
    V2_TOOLS,
)
from protocol_translation import (
    ChatToResponsesStreamConverter,
    chat_completion_to_response_body,
    chat_completions_request_to_responses_body,
    prepare_exchange,
)
from runtime_tool_compatibility import ToolCompatibilityError


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


def _namespace() -> dict:
    children = []
    for name, schema in EXPECTED_PARAMETER_SCHEMAS[COLLABORATION_V2].items():
        parameters = copy.deepcopy(schema)
        if not parameters["required"]:
            del parameters["required"]
        children.append(
            {
                "type": "function",
                "name": name,
                "description": "runtime",
                "strict": False,
                "parameters": parameters,
            }
        )
    return {
        "type": "namespace",
        "name": "collaboration",
        "description": "runtime",
        "tools": children,
    }


def _upstream() -> dict:
    return {
        "name": "generic_chat_endpoint",
        "upstream_format": "chat_completions",
        "tool_protocol": "chat_tools",
        "tool_surface_strategy": "eager",
    }


def _request(
    *,
    input_items: object = "use collaboration",
    model: str = "placeholder",
) -> bytes:
    return json.dumps(
        {
            "model": model,
            "input": input_items,
            "tools": [_namespace()],
            "tool_choice": "auto",
        }
    ).encode()


def _prepared_chat(
    *,
    input_items: object = "use collaboration",
    model: str = "placeholder",
    upstream: dict | None = None,
) -> tuple[dict, dict]:
    context: dict = {}
    selected_upstream = upstream or _upstream()
    prepared = codex_proxy.compatible_request_body(
        _request(input_items=input_items, model=model),
        selected_upstream,
        event_context=context,
        inject_codex_tools=False,
    )
    exchange = prepare_exchange(
        prepared,
        inbound_format="responses",
        outbound_format="chat_completions",
    )
    return json.loads(exchange.upstream_body), context


def _aliases(chat_request: dict) -> dict[str, str]:
    return {
        original: declaration["function"]["name"]
        for original, declaration in zip(V2_TOOLS, chat_request["tools"], strict=True)
    }




def test_native_responses_codec_is_inert_on_chat_custom_adapter() -> None:
    upstream = {
        **_upstream(),
        "native_responses_tool_codec": "strict_apply_patch",
    }
    body = json.dumps(
        {
            "model": "placeholder",
            "input": "edit",
            "tools": [
                {
                    "type": "custom",
                    "name": "apply_patch",
                    "description": "Apply a patch.",
                    "format": {"type": "text"},
                }
            ],
            "tool_choice": "auto",
        }
    ).encode()
    context: dict = {}

    prepared = codex_proxy.compatible_request_body(
        body,
        upstream,
        event_context=context,
        inject_codex_tools=False,
    )
    exchange = prepare_exchange(
        prepared,
        inbound_format="responses",
        outbound_format="chat_completions",
    )
    chat = json.loads(exchange.upstream_body)

    assert len(chat["tools"]) == 1
    function = chat["tools"][0]["function"]
    assert function["name"].startswith("__codexhub_custom_")
    assert function["parameters"]["required"] == ["__codexhub_custom_input"]
    assert function["parameters"]["properties"]["__codexhub_custom_input"] == {
        "type": "string"
    }


def test_v2_declarations_flatten_to_six_deterministic_chat_functions() -> None:
    first, _ = _prepared_chat()
    second, _ = _prepared_chat()
    assert len(first["tools"]) == len(V2_TOOLS) == 6
    assert first["tools"] == second["tools"]
    assert list(_aliases(first)) == list(V2_TOOLS)
    assert all(tool["type"] == "function" for tool in first["tools"])
    assert all(tool["function"]["name"].startswith("__codexhub_ns_") for tool in first["tools"])
    assert "encrypted" not in json.dumps(first["tools"])


def test_v2_adapter_is_not_keyed_by_provider_or_model_name() -> None:
    first, _ = _prepared_chat(
        model="arbitrary/model-one",
        upstream={**_upstream(), "name": "provider_one"},
    )
    second, _ = _prepared_chat(
        model="another/model-two",
        upstream={**_upstream(), "name": "provider_two"},
    )
    assert first["tools"] == second["tools"]


@pytest.mark.parametrize("name", V2_TOOLS)
def test_v2_chat_calls_reverse_to_exact_namespace_and_identity(name: str) -> None:
    chat_request, context = _prepared_chat()
    alias = _aliases(chat_request)[name]
    completion = {
        "id": "chatcmpl-v2",
        "object": "chat.completion",
        "created": 1,
        "model": "placeholder",
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": f"call_{name}",
                            "type": "function",
                            "function": {
                                "name": alias,
                                "arguments": json.dumps(ARGUMENTS[name]),
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
    }
    responses = chat_completion_to_response_body(json.dumps(completion).encode())
    decoded = json.loads(
        codex_proxy.compatible_response_body(
            responses,
            _upstream()["name"],
            event_context=context,
        )
    )
    item = decoded["output"][0]
    assert item["namespace"] == "collaboration"
    assert item["name"] == name
    assert item["call_id"] == f"call_{name}"
    assert item["id"] == f"fc_call_{name}"
    assert item["encrypted_function_args"] == []
    assert json.loads(item["arguments"]) == ARGUMENTS[name]


@pytest.mark.parametrize("name", V2_TOOLS)
def test_v2_call_and_result_history_become_chat_tool_messages(name: str) -> None:
    call_id = f"call_{name}"
    input_items = [
        {"type": "message", "role": "user", "content": "continue"},
        {
            "type": "function_call",
            "id": f"item_{name}",
            "call_id": call_id,
            "namespace": "collaboration",
            "name": name,
            "arguments": json.dumps(ARGUMENTS[name]),
            "encrypted_function_args": [],
        },
        {
            "type": "function_call_output",
            "id": f"result_{name}",
            "call_id": call_id,
            "output": RESULTS[name],
        },
    ]
    chat_request, _ = _prepared_chat(input_items=input_items)
    aliases = _aliases(chat_request)
    assert chat_request["messages"][1]["tool_calls"][0]["id"] == call_id
    assert chat_request["messages"][1]["tool_calls"][0]["function"]["name"] == aliases[name]
    assert chat_request["messages"][2] == {
        "role": "tool",
        "tool_call_id": call_id,
        "content": RESULTS[name],
    }


@pytest.mark.parametrize("name", V2_TOOLS)
def test_v2_chat_stream_calls_reverse_progressively(name: str) -> None:
    chat_request, context = _prepared_chat()
    alias = _aliases(chat_request)[name]
    arguments = json.dumps(ARGUMENTS[name])
    converter = ChatToResponsesStreamConverter()
    chunks = [
        {
            "id": "chatcmpl-v2-stream",
            "model": "placeholder",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "role": "assistant",
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": f"call_{name}",
                                "type": "function",
                                "function": {"name": alias, "arguments": arguments[:2]},
                            }
                        ],
                    },
                    "finish_reason": None,
                }
            ],
        },
        {
            "id": "chatcmpl-v2-stream",
            "model": "placeholder",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {"arguments": arguments[2:]},
                            }
                        ]
                    },
                    "finish_reason": "tool_calls",
                }
            ],
        },
    ]
    events = []
    for chunk in chunks:
        events.extend(converter.events_for_chunk(chunk))
    events.extend(converter.events_for_done())

    mapped = []
    for event in events:
        line = codex_proxy.compatible_sse_line(
            b"data: " + json.dumps(event).encode() + b"\n\n",
            _upstream()["name"],
            event_context=context,
        )
        if line:
            mapped.append(json.loads(line.split(b":", 1)[1]))

    added = next(event for event in mapped if event["type"] == "response.output_item.added")
    done = next(event for event in mapped if event["type"] == "response.output_item.done")
    assert added["item"]["namespace"] == "collaboration"
    assert added["item"]["name"] == name
    assert done["item"]["namespace"] == "collaboration"
    assert done["item"]["name"] == name
    assert done["item"]["call_id"] == f"call_{name}"
    assert done["item"]["encrypted_function_args"] == []
    terminals = [event for event in mapped if event["type"] in {"response.completed", "response.failed", "response.incomplete"}]
    assert [event["type"] for event in terminals] == ["response.completed"]


def test_v2_unknown_alias_fails_visibly() -> None:
    _, context = _prepared_chat()
    body = json.dumps(
        {
            "output": [
                {
                    "type": "function_call",
                    "id": "item_unknown",
                    "call_id": "call_unknown",
                    "name": "__codexhub_ns_unknown_1",
                    "arguments": "{}",
                }
            ]
        }
    ).encode()
    with pytest.raises(codex_proxy.UpstreamProtocolTranslationError) as caught:
        codex_proxy.compatible_response_body(body, _upstream()["name"], event_context=context)
    assert caught.value.cause.code == "tool_compatibility_boundary"


def test_v2_encrypted_argument_handoff_fails_before_chat_sampling() -> None:
    input_items = [
        {
            "type": "function_call",
            "id": "item_send",
            "call_id": "call_send",
            "namespace": "collaboration",
            "name": "send_message",
            "arguments": json.dumps(ARGUMENTS["send_message"]),
            "encrypted_function_args": ["message"],
        }
    ]
    with pytest.raises(codex_proxy.UpstreamProtocolTranslationError) as caught:
        _prepared_chat(input_items=input_items)
    assert caught.value.cause.code == "tool_compatibility_boundary"


def test_v2_plaintext_agent_message_round_trip_preserves_identity_and_roles() -> None:
    agent_message = {
        "type": "agent_message",
        "id": "agent_message_1",
        "author": "agent/root/worker",
        "recipient": "agent/root",
        "content": [{"type": "input_text", "text": "inspection complete"}],
    }
    chat_request, context = _prepared_chat(input_items=[agent_message])
    assert chat_request["messages"][0]["role"] == "user"
    assert chat_request["messages"][0]["content"].startswith(
        "__codexhub_agent_message_v2__:"
    )

    flattened_responses = json.loads(
        chat_completions_request_to_responses_body(
            json.dumps(chat_request).encode()
        )
    )
    decoded = context["_runtime_tool_compatibility_plan"].decode_payload(
        flattened_responses
    )
    assert decoded["input"] == [agent_message]


def test_v2_encrypted_agent_message_fails_before_chat_sampling() -> None:
    encrypted = {
        "type": "agent_message",
        "id": "agent_message_encrypted",
        "author": "agent/root/worker",
        "recipient": "agent/root",
        "content": [
            {"type": "encrypted_content", "encrypted_content": "opaque"}
        ],
    }
    with pytest.raises(codex_proxy.UpstreamProtocolTranslationError) as caught:
        _prepared_chat(input_items=[encrypted])
    assert caught.value.cause.code == "tool_compatibility_boundary"


def test_v2_child_agent_message_without_repeated_namespace_uses_chat_envelope() -> None:
    agent_message = {
        "type": "agent_message",
        "id": "agent_message_child",
        "author": "agent/root",
        "recipient": "agent/root/worker",
        "content": [{"type": "input_text", "text": "child task"}],
    }
    context: dict = {}
    prepared = codex_proxy.compatible_request_body(
        json.dumps(
            {
                "model": "placeholder",
                "input": [agent_message],
                "tools": [],
                "stream": True,
            }
        ).encode(),
        _upstream(),
        event_context=context,
        inject_codex_tools=False,
    )
    payload = json.loads(prepared)
    assert context["collaboration_protocol"] == COLLABORATION_V2
    assert context["_runtime_tool_compatibility_plan"].collaboration_protocol == COLLABORATION_V2
    assert payload["input"][0]["type"] == "message"
    assert payload["input"][0]["content"][0]["text"].startswith(
        "__codexhub_agent_message_v2__:"
    )
    chat = json.loads(
        prepare_exchange(
            prepared,
            inbound_format="responses",
            outbound_format="chat_completions",
        ).upstream_body
    )
    assert chat["messages"][0]["role"] == "user"
    assert chat["messages"][0]["content"].startswith(
        "__codexhub_agent_message_v2__:"
    )


def test_partial_agent_message_cannot_select_v2_child_context() -> None:
    with pytest.raises(
        codex_proxy.UpstreamProtocolTranslationError,
        match="malformed or ambiguous",
    ):
        codex_proxy.compatible_request_body(
            json.dumps(
                {
                    "model": "placeholder",
                    "input": [{"type": "agent_message", "id": "partial"}],
                    "tools": [],
                    "stream": True,
                }
            ).encode(),
            _upstream(),
            event_context={},
            inject_codex_tools=False,
        )


def test_v2_child_agent_message_without_adapter_capability_fails_closed() -> None:
    agent_message = {
        "type": "agent_message",
        "id": "agent_message_child",
        "author": "agent/root",
        "recipient": "agent/root/worker",
        "content": [{"type": "input_text", "text": "child task"}],
    }
    with pytest.raises(codex_proxy.UpstreamProtocolTranslationError, match="required_unavailable"):
        codex_proxy.compatible_request_body(
            json.dumps(
                {
                    "model": "placeholder",
                    "input": [agent_message],
                    "tools": [],
                    "stream": True,
                }
            ).encode(),
            {
                "name": "unsupported",
                "upstream_format": "chat_completions",
                "tool_protocol": "none",
            },
            event_context={},
            inject_codex_tools=False,
        )


def test_v2_agent_message_envelope_cannot_be_forged() -> None:
    _, context = _prepared_chat()
    forged = {
        "type": "message",
        "role": "user",
        "content": [
            {
                "type": "input_text",
                "text": "__codexhub_agent_message_v2__:{\"type\":\"agent_message\"}",
            }
        ],
    }
    with pytest.raises(ToolCompatibilityError) as caught:
        context["_runtime_tool_compatibility_plan"].decode_history([forged])
    assert caught.value.classification == "unknown_agent_message_envelope"
