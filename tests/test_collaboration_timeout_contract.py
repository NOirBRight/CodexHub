import copy
import pytest
from collaboration_runtime_contract import (
    CollaborationContractError,
    EXPECTED_PARAMETER_SCHEMAS,
    normalize_collaboration_arguments,
)
from runtime_tool_compatibility import build_tool_compatibility_plan, ProtocolCapabilities


@pytest.mark.parametrize("version,namespace", [("v1", "multi_agent_v1"), ("v2", "collaboration")])
def test_provider_wait_schema_expresses_native_integer_requirement(version, namespace):
    tools = [{"type": "namespace", "name": namespace, "description": "runtime", "tools": [
        {"type": "function", "name": name, "description": "runtime", "strict": False, "parameters": copy.deepcopy(schema)}
        for name, schema in EXPECTED_PARAMETER_SCHEMAS["collaboration_" + version].items()]}]
    original = copy.deepcopy(tools)
    plan = build_tool_compatibility_plan(tools, selected_protocol="chat_tools", tool_choice="auto",
        protocol_capabilities=ProtocolCapabilities(function_lifecycle=True, accepts_namespace_adapter=True))
    encoded = plan.encode_payload({"tools": tools})
    wait = next(t for t in encoded["tools"] if "timeout_ms" in t.get("parameters", {}).get("properties", {}))
    assert wait["parameters"]["properties"]["timeout_ms"]["type"] == "integer"
    assert "300000.0" in wait["parameters"]["properties"]["timeout_ms"]["description"]
    assert tools == original


@pytest.mark.parametrize("wire,expected", [
    ('{"targets":["child"],"timeout_ms":300000.0}',
     '{"targets":["child"],"timeout_ms":300000}'),
    ('{"targets":["child"],"timeout_ms":3e5}',
     '{"targets":["child"],"timeout_ms":300000}'),
])
def test_timeout_normalization_is_exact_and_lossless(wire, expected):
    assert normalize_collaboration_arguments(
        "collaboration_v1", "wait_agent", wire
    ) == (expected, True)


@pytest.mark.parametrize("wire", [
    '{"targets":["child"],"timeout_ms":9999}',
    '{"targets":["child"],"timeout_ms":3600001}',
    '{"targets":["child"],"timeout_ms":0.5}',
    '{"targets":["child"],"timeout_ms":true}',
    '{"targets":["child"],"timeout_ms":1e400}',
    '{"targets":["child"],"timeout_ms":9223372036854775808}',
    '{"targets":["child"],"timeout_ms":1,"timeout_ms":2}',
])
def test_timeout_normalization_rejects_fractional_nonfinite_overflow_and_duplicates(wire):
    with pytest.raises(CollaborationContractError):
        normalize_collaboration_arguments("collaboration_v1", "wait_agent", wire)


def test_timeout_normalization_does_not_claim_an_unrelated_tool():
    wire = '{"value":300000.0}'
    with pytest.raises(CollaborationContractError):
        normalize_collaboration_arguments("collaboration_v1", "send_input", wire)


@pytest.mark.parametrize("version,namespace", [("v1", "multi_agent_v1"), ("v2", "collaboration")])
def test_native_namespace_body_keeps_provider_wire_bytes(version, namespace):
    tools = [{"type": "namespace", "name": namespace, "description": "runtime", "tools": [
        {"type": "function", "name": name, "description": "runtime", "strict": False,
         "parameters": copy.deepcopy(schema)}
        for name, schema in EXPECTED_PARAMETER_SCHEMAS["collaboration_" + version].items()
    ]}]
    plan = build_tool_compatibility_plan(
        tools,
        selected_protocol="responses_structured",
        tool_choice="auto",
        protocol_capabilities=ProtocolCapabilities(
            function_lifecycle=True,
            namespace_lifecycle=True,
            accepts_namespace_adapter=True,
        ),
    )
    arguments = (
        '{"targets":["child"],"timeout_ms":300000.0}'
        if version == "v1"
        else '{"timeout_ms":300000.0}'
    )
    payload = {
        "tools": tools,
        "input": [{
            "type": "function_call", "id": "item-native", "call_id": "call-native",
            "namespace": namespace, "name": "wait_agent", "arguments": arguments,
        }],
    }
    encoded = plan.encode_payload(payload)
    assert encoded["input"][0]["arguments"] == arguments


@pytest.mark.parametrize("version,namespace", [("v1", "multi_agent_v1"), ("v2", "collaboration")])
def test_adapted_sse_canonicalizes_the_same_timeout_value(version, namespace):
    tools = [{"type": "namespace", "name": namespace, "description": "runtime", "tools": [
        {"type": "function", "name": name, "description": "runtime", "strict": False,
         "parameters": copy.deepcopy(schema)}
        for name, schema in EXPECTED_PARAMETER_SCHEMAS["collaboration_" + version].items()
    ]}]
    plan = build_tool_compatibility_plan(
        tools,
        selected_protocol="responses_structured",
        tool_choice="auto",
        protocol_capabilities=ProtocolCapabilities(
            function_lifecycle=True,
            namespace_lifecycle=False,
            accepts_namespace_adapter=True,
        ),
    )
    entry = next(entry for entry in plan.entries if entry.family == "namespace")
    alias = entry.aliases[entry.child_names.index("wait_agent")]
    stream = plan.new_stream()
    stream.decode_events_for_event({
        "type": "response.output_item.added", "output_index": 0,
        "item": {"type": "function_call", "id": "item-stream", "call_id": "call-stream",
                 "name": alias, "arguments": "", "status": "in_progress"},
    })
    done = stream.decode_events_for_event({
        "type": "response.function_call_arguments.done", "item_id": "item-stream",
        "output_index": 0,
        "arguments": '{"targets":["child"],"timeout_ms":300000.0}'
        if version == "v1" else '{"timeout_ms":300000.0}',
    })
    assert done[0]["arguments"] == (
        '{"targets":["child"],"timeout_ms":300000}'
        if version == "v1" else '{"timeout_ms":300000}'
    )


@pytest.mark.parametrize("version,namespace", [("v1", "multi_agent_v1"), ("v2", "collaboration")])
def test_native_sse_validates_but_preserves_timeout_wire_value(version, namespace):
    tools = [{"type": "namespace", "name": namespace, "description": "runtime", "tools": [
        {"type": "function", "name": name, "description": "runtime", "strict": False,
         "parameters": copy.deepcopy(schema)}
        for name, schema in EXPECTED_PARAMETER_SCHEMAS["collaboration_" + version].items()
    ]}]
    plan = build_tool_compatibility_plan(
        tools,
        selected_protocol="responses_structured",
        tool_choice="auto",
        protocol_capabilities=ProtocolCapabilities(
            function_lifecycle=True,
            namespace_lifecycle=True,
            accepts_namespace_adapter=True,
        ),
    )
    arguments = (
        '{"targets":["child"],"timeout_ms":300000.0}'
        if version == "v1" else '{"timeout_ms":300000.0}'
    )
    stream = plan.new_stream()
    stream.decode_events_for_event({
        "type": "response.output_item.added", "output_index": 0,
        "item": {"type": "function_call", "id": "native-item", "call_id": "native-call",
                 "namespace": namespace, "name": "wait_agent", "arguments": "", "status": "in_progress"},
    })
    done = stream.decode_events_for_event({
        "type": "response.function_call_arguments.done", "item_id": "native-item",
        "output_index": 0, "arguments": arguments,
    })
    assert done[0]["arguments"] == arguments
