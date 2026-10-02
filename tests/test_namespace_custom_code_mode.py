"""Namespaced current exec uses the existing reversible custom codec."""
from __future__ import annotations
import copy
import json

import pytest
from runtime_tool_compatibility import build_tool_compatibility_plan, ToolCompatibilityError, ProtocolCapabilities
from tool_compatibility.stream import CompatibilityStreamState
from tool_compatibility.contracts import CUSTOM_INPUT_KEY


def declaration():
    return {"type": "namespace", "name": "functions", "description": "Caller Code Mode tools", "tools": [
        {"type": "custom", "name": "exec", "description": "Run caller JavaScript; nested shell and collaboration are caller-owned.", "format": {"type": "grammar", "syntax": "lark", "definition": "start: SOURCE\nSOURCE: /[\\s\\S]+/"}},
        {"type": "function", "name": "wait", "description": "Wait for a caller cell", "parameters": {"type": "object", "properties": {"cell_id": {"type": "string"}}, "required": ["cell_id"], "additionalProperties": False}, "strict": False},
    ]}


def history():
    return [
        {"type": "custom_tool_call", "name": "exec", "namespace": "functions", "id": "ctc_typed", "call_id": "caller-exec", "input": 'text(await tools.exec_command({cmd:"cat fixture"}));'},
        {"type": "custom_tool_call_output", "id": "ctco_typed", "call_id": "caller-exec", "output": "actually executed fixture result"},
        {"type": "function_call", "name": "wait", "namespace": "functions", "id": "fc_wait", "call_id": "caller-wait", "arguments": '{"cell_id":"real-cell"}'},
        {"type": "function_call_output", "id": "fco_wait", "call_id": "caller-wait", "output": "caller cell completed"},
    ]


def test_namespaced_custom_completed_full_history_roundtrip_uses_actual_ids():
    tools = [declaration()]
    original = copy.deepcopy(tools)
    plan = build_tool_compatibility_plan(tools, selected_protocol="chat_completions")
    encoded = plan.encode_payload({"tools": tools, "input": history()})
    wire_tools = encoded["tools"]
    assert len(wire_tools) == 2
    assert "caller JavaScript" in wire_tools[0]["description"]
    assert "Original format" in wire_tools[0]["description"]
    assert wire_tools[0]["parameters"]["properties"] == {CUSTOM_INPUT_KEY: {"type": "string"}}
    wire = encoded["input"]
    assert wire[0]["type"] == "function_call"
    assert "namespace" not in wire[0]
    assert json.loads(wire[0]["arguments"]) == {CUSTOM_INPUT_KEY: history()[0]["input"]}
    assert [item["id"] for item in wire] == [item["id"] for item in history()]
    assert [item["call_id"] for item in wire] == [item["call_id"] for item in history()]
    assert plan.decode_history(wire) == history()
    # A new Gateway plan after restart resolves the same declaration and wire history.
    restarted = build_tool_compatibility_plan(tools, selected_protocol="chat_completions")
    assert restarted.decode_history(wire) == history()
    assert tools == original


def test_namespaced_custom_native_route_preserves_full_completed_history():
    plan = build_tool_compatibility_plan([declaration()], selected_protocol="responses_structured", protocol_capabilities=ProtocolCapabilities.responses_structured())
    assert plan.encode_history(history()) == history()


def test_namespaced_custom_stream_reconstructs_exact_input_and_identity():
    plan = build_tool_compatibility_plan([declaration()], selected_protocol="chat_completions")
    alias = plan.encode_payload({"tools": [declaration()]})["tools"][0]["name"]
    stream = CompatibilityStreamState(plan)
    item = {"type": "function_call", "id": "fc_vendor", "call_id": "call_vendor", "name": alias, "arguments": ""}
    events = []
    events.extend(stream.decode_events_for_event({"type": "response.output_item.added", "output_index": 0, "item": item}))
    arguments = json.dumps({CUSTOM_INPUT_KEY: 'text("exact \\u200b source");'})
    events.extend(stream.decode_events_for_event({"type": "response.function_call_arguments.delta", "item_id": "fc_vendor", "output_index": 0, "delta": arguments}))
    events.extend(stream.decode_events_for_event({"type": "response.function_call_arguments.done", "item_id": "fc_vendor", "output_index": 0, "arguments": arguments}))
    events.extend(stream.decode_events_for_event({"type": "response.output_item.done", "output_index": 0, "item": {**item, "arguments": arguments}}))
    done = next(event["item"] for event in events if event["type"] == "response.output_item.done")
    assert done == {"type": "custom_tool_call", "id": "fc_vendor", "call_id": "call_vendor", "name": "exec", "namespace": "functions", "input": 'text("exact \\u200b source");'}


@pytest.mark.parametrize("arguments", ['{"input":{}}', '{"input":"text(1)","extra":1}', '{"input":null}'])
def test_namespaced_custom_malformed_envelope_stays_failure(arguments):
    plan = build_tool_compatibility_plan([declaration()], selected_protocol="chat_completions")
    alias = plan.encode_payload({"tools": [declaration()]})["tools"][0]["name"]
    with pytest.raises(ToolCompatibilityError):
        plan.decode_payload({"output": [{"type": "function_call", "id": "fc_bad", "call_id": "call_bad", "name": alias, "arguments": arguments}]})


def test_observed_codex_0159_declaration_runs_the_shared_gateway_codec():
    from pathlib import Path
    import gateway_compat
    fixture = json.loads((Path(__file__).parent / "fixtures/collaboration/codex-cli-0.159.2-linux-code-mode.json").read_text())
    tools = fixture["tools"]
    assert fixture["client_version"] == "codex-cli 0.159.2"
    payload = {"model": "exact-selected-model", "tools": [], "input": [{"type": "additional_tools", "tools": tools}, *history()], "tool_choice": "auto"}
    context = {}
    prepared = json.loads(gateway_compat.compatible_request_body(json.dumps(payload).encode(), {"name": "cursor-subscription", "upstream_format": "responses", "tool_protocol": "chat_tools", "tool_surface_strategy": "eager"}, event_context=context, inject_codex_tools=False))
    assert prepared["model"] == payload["model"]
    assert context["collaboration_protocol"] == "collaboration_v2"
    assert [item["call_id"] for item in prepared["input"]] == [item["call_id"] for item in history()]
    assert all(item["type"] == "function_call" for item in prepared["input"] if "name" in item)
    assert "actually executed fixture result" in prepared["input"][1]["output"]
    assert len(prepared["tools"]) == sum(len(tool["tools"]) for tool in tools)


def test_namespaced_and_flat_custom_exec_keep_distinct_owners():
    flat = {**declaration()["tools"][0], "description": "A different flat caller exec"}
    tools = [declaration(), flat]
    items = [*history(), {"type": "custom_tool_call", "name": "exec", "id": "ctc_flat", "call_id": "flat-call", "input": "text(42)"}, {"type": "custom_tool_call_output", "id": "ctco_flat", "call_id": "flat-call", "output": "42"}]
    plan = build_tool_compatibility_plan(tools, selected_protocol="chat_completions")
    wire = plan.encode_payload({"tools": tools, "input": items})
    assert wire["tools"][0]["name"] != wire["tools"][2]["name"]
    assert wire["input"][0]["name"] != wire["input"][4]["name"]
    assert plan.decode_history(wire["input"]) == items


def test_namespaced_custom_stream_terminal_retains_completed_actual_call():
    plan = build_tool_compatibility_plan([declaration()], selected_protocol="chat_completions")
    alias = plan.encode_payload({"tools": [declaration()]})["tools"][0]["name"]
    arguments = json.dumps({CUSTOM_INPUT_KEY: "text(42)"})
    item = {"type": "function_call", "id": "fc_vendor", "call_id": "call_vendor", "name": alias, "arguments": arguments}
    events = [
        {"type": "response.output_item.added", "output_index": 0, "item": {**item, "arguments": ""}},
        {"type": "response.function_call_arguments.delta", "item_id": item["id"], "output_index": 0, "delta": arguments},
        {"type": "response.function_call_arguments.done", "item_id": item["id"], "output_index": 0, "arguments": arguments},
        {"type": "response.output_item.done", "output_index": 0, "item": item},
        {"type": "response.completed", "response": {"id": "resp_vendor", "output": [item]}},
    ]
    decoded = CompatibilityStreamState(plan).decode_events(events)
    expected = {"type": "custom_tool_call", "namespace": "functions", "name": "exec", "id": item["id"], "call_id": item["call_id"], "input": "text(42)"}
    assert decoded[-1]["response"]["output"] == [expected]
