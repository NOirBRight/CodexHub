"""Closed native calls/results remain caller history with no current tool grant."""
from __future__ import annotations

import copy
import json

import pytest

import gateway_compat
import protocol_translation
from gateway_errors import UpstreamProtocolTranslationError


def upstream():
    return {"name": "claude-subscription", "upstream_format": "chat_completions", "tool_protocol": "chat_tools", "tool_surface_strategy": "eager"}


def closed_history():
    first = json.loads(protocol_translation.chat_completion_to_response_body(json.dumps({
        "id": "actual-first-chat-response", "model": "selected-model", "choices": [{"index": 0, "finish_reason": "tool_calls", "message": {
            "role": "assistant", "content": "", "tool_calls": [{"id": "caller-actual-call", "type": "function", "function": {"name": "caller_tool", "arguments": '{"fixture":"retained"}'}}],
        }}],
    }).encode(), repair=False))
    second = json.loads(protocol_translation.chat_completion_to_response_body(json.dumps({
        "id": "actual-second-chat-response", "model": "selected-model", "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "Caller result retained."}}],
    }).encode(), repair=False))
    call = next(item for item in first["output"] if item["type"] == "function_call")
    result = {"type": "function_call_output", "id": "actual-result-item", "call_id": call["call_id"], "output": "actual caller execution result"}
    return [*first["output"], result, *second["output"]]


@pytest.mark.parametrize("declare", [False, True])
@pytest.mark.parametrize("result_has_item_id", [False, True])
def test_all_actual_output_and_result_history_survives_fresh_gateway_preparation(declare, result_has_item_id):
    history = closed_history()
    if not result_has_item_id:
        history[2].pop("id")
    tools = [{"type": "function", "name": "caller_tool", "parameters": {"type": "object", "properties": {"fixture": {"type": "string"}}, "additionalProperties": False}}] if declare else []
    payload = {"model": "selected-model", "tools": tools, "input": history, "tool_choice": "auto"}
    original = copy.deepcopy(payload)
    prepared = json.loads(gateway_compat.compatible_request_body(json.dumps(payload).encode(), upstream(), event_context={}, inject_codex_tools=False))
    assert prepared["input"] == history
    if not declare:
        assert not prepared.get("tools")
    exchange = protocol_translation.prepare_exchange(json.dumps(prepared).encode(), inbound_format="responses", outbound_format="chat_completions")
    assert not isinstance(exchange, protocol_translation.NonForwardable)
    messages = json.loads(exchange.upstream_body)["messages"]
    assert messages == [
        {"role": "assistant", "content": ""},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "caller-actual-call", "type": "function", "function": {"name": "caller_tool", "arguments": '{"fixture":"retained"}'}}]},
        {"role": "tool", "tool_call_id": "caller-actual-call", "content": "actual caller execution result"},
        {"role": "assistant", "content": "Caller result retained."},
    ]
    assert payload == original


@pytest.mark.parametrize("mutation", ["call_unknown_field", "result_unknown_field", "duplicate_call", "duplicate_result", "duplicate_item", "result_before_call", "wrong_family", "arguments_object", "result_identity_invalid", "encrypted_arguments"])
def test_self_contained_history_requires_exact_valid_native_pair(mutation):
    history = closed_history()
    call, result = history[1], history[2]
    if mutation == "call_unknown_field":
        call["private-field"] = "private-value"
    elif mutation == "result_unknown_field":
        result["private-field"] = "private-value"
    elif mutation == "duplicate_call":
        history.insert(2, {**call, "id": "another-call-item"})
    elif mutation == "duplicate_result":
        history.insert(3, {**result, "id": "another-result-item"})
    elif mutation == "duplicate_item":
        result["id"] = call["id"]
    elif mutation == "result_before_call":
        history[1], history[2] = result, call
    elif mutation == "wrong_family":
        result["type"] = "custom_tool_call_output"
    elif mutation == "arguments_object":
        call["arguments"] = {}
    elif mutation == "result_identity_invalid":
        result["id"] = 42
    elif mutation == "encrypted_arguments":
        call["encrypted_function_args"] = ["message"]
    payload = {"model": "selected-model", "tools": [], "input": history, "tool_choice": "auto"}
    original = copy.deepcopy(payload)
    with pytest.raises(UpstreamProtocolTranslationError, match="malformed or ambiguous") as error:
        gateway_compat.compatible_request_body(json.dumps(payload).encode(), upstream(), event_context={}, inject_codex_tools=False)
    assert "private-value" not in str(error.value)
    assert "private-field" not in str(error.value)
    assert payload == original


@pytest.mark.parametrize("mutation", ["uncompleted", "no_typed_id", "no_result", "wrong_call_id", "custom", "namespace"])
def test_unproven_or_noncanonical_history_cannot_acquire_native_function_ownership(mutation):
    history = closed_history()
    call, result = history[1], history[2]
    if mutation == "uncompleted":
        call["status"] = "in_progress"
    elif mutation == "no_typed_id":
        call.pop("id")
    elif mutation == "no_result":
        history.pop(2)
    elif mutation == "wrong_call_id":
        result["call_id"] = "unowned-result"
    elif mutation == "custom":
        call["type"] = "custom_tool_call"
        call["input"] = call.pop("arguments")
        result["type"] = "custom_tool_call_output"
    elif mutation == "namespace":
        call["namespace"] = "unknown_namespace"
    prepared = json.loads(gateway_compat.compatible_request_body(json.dumps({"model": "selected-model", "tools": [], "input": history, "tool_choice": "auto"}).encode(), upstream(), event_context={}, inject_codex_tools=False))
    assert not any(item.get("type") in {"function_call", "function_call_output", "custom_tool_call", "custom_tool_call_output"} for item in prepared["input"])
    assert not prepared.get("tools")
