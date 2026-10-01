"""Actual completed Responses output remains valid caller-owned next-turn input."""
from __future__ import annotations

import copy
import json

import pytest

import gateway_compat
import protocol_translation
from tool_compatibility.contracts import CUSTOM_INPUT_KEY


def response_for_call(name: str, arguments: str):
    return json.loads(protocol_translation.chat_completion_to_response_body(json.dumps({
        "id": "resp_observed", "model": "exact-selected-model",
        "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": None,
            "tool_calls": [{"id": "actual-call", "type": "function", "function": {"name": name, "arguments": arguments}}],
        }}],
    }).encode(), repair=False))


def test_actual_completed_function_output_is_accepted_unchanged_as_next_turn_history():
    response = response_for_call("caller_tool", '{"fixture":"actual"}')
    call = response["output"][0]
    assert call["status"] == "completed"
    assert call["id"] != call["call_id"]
    result = {"type": "function_call_output", "id": "result-typed-item", "call_id": call["call_id"], "output": "actual caller execution result"}
    payload = {"model": "exact-selected-model", "input": [call, result]}
    original = copy.deepcopy(payload)
    exchange = protocol_translation.prepare_exchange(json.dumps(payload).encode(), inbound_format="responses", outbound_format="chat_completions")
    assert not isinstance(exchange, protocol_translation.NonForwardable)
    chat = json.loads(exchange.upstream_body)
    assert chat["messages"] == [
        {"role": "assistant", "content": None, "tool_calls": [{"id": call["call_id"], "type": "function", "function": {"name": call["name"], "arguments": call["arguments"]}}]},
        {"role": "tool", "tool_call_id": call["call_id"], "content": result["output"]},
    ]
    assert payload == original


@pytest.mark.parametrize("text", ["", "The fixture check is complete."])
@pytest.mark.parametrize("with_call", [False, True])
def test_all_actual_completed_response_output_items_are_accepted_unchanged(text, with_call):
    message = {"role": "assistant", "content": text}
    if with_call:
        message["tool_calls"] = [{"id": "actual-call", "type": "function", "function": {"name": "caller_tool", "arguments": "{}"}}]
    response = json.loads(protocol_translation.chat_completion_to_response_body(json.dumps({
        "id": "resp_actual_complete", "model": "exact-selected-model",
        "choices": [{"index": 0, "finish_reason": "tool_calls" if with_call else "stop", "message": message}],
    }).encode(), repair=False))
    assert response["output"]
    assistant = next(item for item in response["output"] if item["type"] == "message")
    assert assistant["status"] == "completed"
    assert assistant["content"] == [{"type": "output_text", "text": text, "annotations": []}]
    history = copy.deepcopy(response["output"])
    if with_call:
        call = next(item for item in history if item["type"] == "function_call")
        history.append({"type": "function_call_output", "id": "fco_actual_result", "call_id": call["call_id"], "output": "actual caller result"})
    payload = {"model": "exact-selected-model", "input": history}
    original = copy.deepcopy(payload)
    exchange = protocol_translation.prepare_exchange(json.dumps(payload).encode(), inbound_format="responses", outbound_format="chat_completions")
    assert not isinstance(exchange, protocol_translation.NonForwardable)
    messages = json.loads(exchange.upstream_body)["messages"]
    assert messages[0] == {"role": "assistant", "content": text}
    if with_call:
        assert messages[1]["tool_calls"][0]["id"] == call["call_id"]
        assert messages[2] == {"role": "tool", "tool_call_id": call["call_id"], "content": "actual caller result"}
    assert payload == original


@pytest.mark.parametrize("status", [None, "in_progress", "incomplete", "unexpected-private-status", {}, 1])
def test_completed_message_history_does_not_admit_unknown_or_unfinished_status(status):
    item = {"type": "message", "id": "actual-message-item", "status": status, "role": "assistant", "content": [{"type": "output_text", "text": "actual response", "annotations": []}]}
    with pytest.raises(protocol_translation.UnsupportedProtocolTranslationError) as error:
        protocol_translation.responses_request_to_chat_completion_body(json.dumps({"model": "exact-selected-model", "input": [item]}).encode())
    assert error.value.code == "unsupported_protocol_semantics"
    assert "unexpected-private-status" not in str(error.value)


def test_completed_message_history_still_rejects_unknown_fields():
    item = {"type": "message", "id": "actual-message-item", "status": "completed", "role": "assistant", "content": "actual response", "unknown-private-field": "private-value"}
    with pytest.raises(protocol_translation.UnsupportedProtocolTranslationError) as error:
        protocol_translation.responses_request_to_chat_completion_body(json.dumps({"model": "exact-selected-model", "input": [item]}).encode())
    assert error.value.code == "unsupported_protocol_semantics"
    assert "private-value" not in str(error.value)


@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("namespace", [None, "functions"])
def test_completed_custom_output_roundtrips_through_production_compatibility_and_exchange(namespace, restart):
    custom = {"type": "custom", "name": "exec", "description": "Run caller Code Mode source exactly.", "format": {"type": "grammar", "syntax": "lark", "definition": "start: SOURCE\nSOURCE: /[\\s\\S]+/"}}
    tools = [{"type": "namespace", "name": namespace, "description": "Caller tools", "tools": [custom]}] if namespace else [custom]
    upstream = {"name": "cursor-subscription", "upstream_format": "chat_completions", "tool_protocol": "chat_tools", "tool_surface_strategy": "eager"}
    context = {}
    first = json.loads(gateway_compat.compatible_request_body(json.dumps({"model": "exact-selected-model", "tools": tools, "input": [], "tool_choice": "auto"}).encode(), upstream, event_context=context, inject_codex_tools=False))
    alias = first["tools"][0]["name"]
    source = 'text(await tools.exec_command({cmd:"cat actual_fixture"}));'
    wire = response_for_call(alias, json.dumps({CUSTOM_INPUT_KEY: source}))
    response = json.loads(gateway_compat.compatible_response_body(json.dumps(wire).encode(), upstream["name"], context))
    call = response["output"][0]
    assert call["type"] == "custom_tool_call"
    assert call["status"] == "completed"
    assert call["call_id"] == "actual-call"
    assert call["input"] == source
    if namespace:
        assert call["namespace"] == namespace
    result = {"type": "custom_tool_call_output", "id": "result-typed-item", "call_id": call["call_id"], "output": "actual caller fixture execution result"}
    payload = {"model": "exact-selected-model", "tools": tools, "input": [call, result], "tool_choice": "auto"}
    original = copy.deepcopy(payload)
    continuation = json.loads(gateway_compat.compatible_request_body(json.dumps(payload).encode(), upstream, event_context={} if restart else context, inject_codex_tools=False))
    assert continuation["input"][0]["id"] == call["id"]
    assert continuation["input"][0]["call_id"] == call["call_id"]
    assert continuation["input"][1]["id"] == result["id"]
    assert continuation["input"][0]["type"] == "function_call"
    exchange = protocol_translation.prepare_exchange(json.dumps(continuation).encode(), inbound_format="responses", outbound_format="chat_completions")
    assert not isinstance(exchange, protocol_translation.NonForwardable)
    chat = json.loads(exchange.upstream_body)
    tool_call = chat["messages"][0]["tool_calls"][0]
    assert tool_call["id"] == call["call_id"]
    assert tool_call["function"]["name"] == alias
    assert json.loads(tool_call["function"]["arguments"]) == {CUSTOM_INPUT_KEY: source}
    assert call["call_id"] == chat["messages"][1]["tool_call_id"]
    assert "actual caller fixture execution result" in chat["messages"][1]["content"]
    assert payload == original


@pytest.mark.parametrize("status", [None, "in_progress", "incomplete", "unexpected-private-status", {}, 1])
def test_completed_call_history_does_not_admit_unknown_or_unfinished_status(status):
    call = {"type": "function_call", "id": "typed-item", "call_id": "actual-call", "name": "caller_tool", "arguments": "{}", "status": status}
    payload = {"model": "exact-selected-model", "input": [call, {"type": "function_call_output", "call_id": call["call_id"], "output": "actual result"}]}
    with pytest.raises(protocol_translation.UnsupportedProtocolTranslationError) as error:
        protocol_translation.responses_request_to_chat_completion_body(json.dumps(payload).encode())
    assert error.value.code == "unsupported_protocol_semantics"
    assert "unexpected-private-status" not in str(error.value)


def test_completed_call_history_still_rejects_unknown_fields():
    call = {"type": "function_call", "id": "typed-item", "call_id": "actual-call", "name": "caller_tool", "arguments": "{}", "status": "completed", "unknown-private-field": "private-value"}
    with pytest.raises(protocol_translation.UnsupportedProtocolTranslationError) as error:
        protocol_translation.responses_request_to_chat_completion_body(json.dumps({"model": "exact-selected-model", "input": [call]}).encode())
    assert error.value.code == "unsupported_protocol_semantics"
    assert "private-value" not in str(error.value)
    assert "unknown-private-field" not in str(error.value)


@pytest.mark.parametrize("invalid_result", ["unknown_field", "wrong_family"])
def test_custom_continuation_requires_exact_declared_call_and_strict_result_shape(invalid_result):
    from gateway_errors import UpstreamProtocolTranslationError

    tools = [{"type": "custom", "name": "exec", "description": "Caller exec", "format": {"type": "text"}}]
    call = {"type": "custom_tool_call", "name": "exec", "id": "ctc_actual", "call_id": "actual-call", "status": "completed", "input": "text(42)"}
    result = {"type": "custom_tool_call_output", "id": "ctco_actual", "call_id": "actual-call", "output": "actual result"}
    if invalid_result == "wrong_call":
        result["call_id"] = "unowned-call"
    elif invalid_result == "unknown_field":
        result["unknown-private-field"] = "private-value"
    else:
        result["type"] = "function_call_output"
    upstream = {"name": "cursor-subscription", "upstream_format": "chat_completions", "tool_protocol": "chat_tools", "tool_surface_strategy": "eager"}
    payload = {"model": "exact-selected-model", "tools": tools, "input": [call, result], "tool_choice": "auto"}
    with pytest.raises((UpstreamProtocolTranslationError, protocol_translation.NonForwardable)) as error:
        prepared = gateway_compat.compatible_request_body(json.dumps(payload).encode(), upstream, event_context={}, inject_codex_tools=False)
        protocol_translation.prepare_exchange(prepared, inbound_format="responses", outbound_format="chat_completions")
    assert "private-value" not in str(error.value)
    assert "unknown-private-field" not in str(error.value)


def test_unowned_custom_result_does_not_become_an_owned_tool_result():
    tools = [{"type": "custom", "name": "exec", "description": "Caller exec", "format": {"type": "text"}}]
    call = {"type": "custom_tool_call", "name": "exec", "id": "ctc_actual", "call_id": "actual-call", "status": "completed", "input": "text(42)"}
    result = {"type": "custom_tool_call_output", "id": "ctco_unowned", "call_id": "unowned-call", "output": "unowned actual result"}
    upstream = {"name": "cursor-subscription", "upstream_format": "chat_completions", "tool_protocol": "chat_tools", "tool_surface_strategy": "eager"}
    prepared = json.loads(gateway_compat.compatible_request_body(json.dumps({"model": "exact-selected-model", "tools": tools, "input": [call, result], "tool_choice": "auto"}).encode(), upstream, event_context={}, inject_codex_tools=False))
    assert prepared["input"][0]["call_id"] == call["call_id"]
    # Keep the existing read-only treatment of unowned history; it has no
    # authority to become a result for this request's declared custom call.
    assert prepared["input"][1]["type"] == "message"
    assert not any(item.get("type") == "function_call_output" for item in prepared["input"])
