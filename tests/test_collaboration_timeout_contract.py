import copy
import json
import pytest
from collaboration_runtime_contract import (
    CollaborationContractError,
    EXPECTED_PARAMETER_SCHEMAS,
    normalize_collaboration_arguments,
)
from runtime_tool_compatibility import build_tool_compatibility_plan, ProtocolCapabilities
import gateway_compat
from gateway_compat.sse import reconcile_function_call_argument_events


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
    '{"targets":["child"],"timeout_ms":"1280"}',
    '{"targets":["child"],"timeout_ms":-9223372036854775809}',
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


@pytest.mark.parametrize(
    "version,namespace",
    [("v1", "multi_agent_v1"), ("v2", "collaboration")],
)
def test_public_request_response_and_sse_paths_share_integer_contract(version, namespace):
    tools = [{"type": "namespace", "name": namespace, "description": "runtime", "tools": [
        {"type": "function", "name": name, "description": "runtime", "strict": False,
         "parameters": copy.deepcopy(schema)}
        for name, schema in EXPECTED_PARAMETER_SCHEMAS["collaboration_" + version].items()
    ]}]
    arguments = json.dumps(
        {"targets": ["child"], "timeout_ms": 300000.0}
        if version == "v1" else {"timeout_ms": 300000.0},
        separators=(",", ":"),
    )
    upstream = {
        "name": "fixture-chat-provider",
        "upstream_format": "chat_completions",
        "tool_protocol": "chat_tools",
        "tool_surface_strategy": "eager",
    }
    request = {
        "model": "fixture",
        "tools": tools,
        "tool_choice": "auto",
        "input": [{
            "type": "function_call",
            "id": "item-timeout",
            "call_id": "call-timeout",
            "namespace": namespace,
            "name": "wait_agent",
            "arguments": arguments,
        }],
    }
    context = {}
    encoded = json.loads(
        gateway_compat.compatible_request_body(
            json.dumps(request).encode(),
            upstream,
            event_context=context,
            inject_codex_tools=False,
        )
    )
    alias = encoded["input"][0]["name"]
    assert json.loads(encoded["input"][0]["arguments"])["timeout_ms"] == 300000

    response = json.loads(
        gateway_compat.compatible_response_body(
            json.dumps({
                "output": [{
                    "type": "function_call",
                    "id": "item-timeout",
                    "call_id": "call-timeout",
                    "name": alias,
                    "arguments": arguments,
                }],
            }).encode(),
            upstream["name"],
            event_context=context,
        )
    )
    assert json.loads(response["output"][0]["arguments"])["timeout_ms"] == 300000

    # A Responses stream must establish the output item before its argument
    # completion event.  The public SSE adapter intentionally rejects a bare
    # ``arguments.done`` event because it cannot prove the call identity.
    added = gateway_compat.compatible_sse_line(
        (
            "data: "
            + json.dumps({
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {
                    "type": "function_call",
                    "id": "item-timeout",
                    "call_id": "call-timeout",
                    "name": alias,
                    "arguments": "",
                    "status": "in_progress",
                },
            }, separators=(",", ":"))
            + "\n\n"
        ).encode(),
        upstream["name"],
        event_context=context,
    )
    assert b"response.output_item.added" in added

    sse = gateway_compat.compatible_sse_line(
        (
            "data: "
            + json.dumps({
                "type": "response.function_call_arguments.done",
                "item_id": "item-timeout",
                "output_index": 0,
                "arguments": arguments,
            }, separators=(",", ":"))
            + "\n\n"
        ).encode(),
        upstream["name"],
        event_context=context,
    )
    assert json.loads(json.loads(sse.split(b":", 1)[1])["arguments"])["timeout_ms"] == 300000


def test_reconcile_adapted_argument_events_preserves_deltas_and_validates_snapshot():
    tools = [{"type": "namespace", "name": "collaboration", "description": "runtime", "tools": [
        {"type": "function", "name": name, "description": "runtime", "strict": False,
         "parameters": copy.deepcopy(schema)}
        for name, schema in EXPECTED_PARAMETER_SCHEMAS["collaboration_v2"].items()
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
    alias = next(entry for entry in plan.entries if entry.family == "namespace").aliases[
        next(entry for entry in plan.entries if entry.family == "namespace").child_names.index("wait_agent")
    ]
    arguments = '{"timeout_ms":300000}'
    events = [
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {
                "type": "function_call",
                "id": "item-adapted",
                "call_id": "call-adapted",
                "name": alias,
                "arguments": arguments,
            },
        },
        {
            "type": "response.function_call_arguments.delta",
            "item_id": "item-adapted",
            "output_index": 0,
            "delta": '{"timeout_ms":',
        },
        {
            "type": "response.function_call_arguments.delta",
            "item_id": "item-adapted",
            "output_index": 0,
            "delta": "300000}",
        },
        {
            "type": "response.function_call_arguments.done",
            "item_id": "item-adapted",
            "output_index": 0,
            "arguments": arguments,
        },
    ]
    rewritten, changed = reconcile_function_call_argument_events(
        events,
        runtime_tool_plan=plan,
    )
    assert not changed
    assert [event["type"] for event in rewritten] == [event["type"] for event in events]
    assert [event["delta"] for event in rewritten[1:3]] == ['{"timeout_ms":', "300000}"]


@pytest.mark.parametrize("version,namespace,value", [
    ("collaboration_v2", "collaboration", value)
    for value in (1280, 0, -1, -(2**63), None, 3600001, 2**63 - 1)
] + [
    ("collaboration_v1", "multi_agent_v1", value)
    for value in (-(2**63), -1, 0, 1, 1280, 9999, 3600001, 2**63 - 1, None)
])
@pytest.mark.parametrize("native", [True, False])
def test_client_wait_input_range_is_not_the_effective_wait_duration(version, namespace, value, native):
    tools = [{"type": "namespace", "name": namespace, "description": "runtime", "tools": [
        {"type": "function", "name": name, "description": "runtime", "strict": False,
         "parameters": copy.deepcopy(schema)}
        for name, schema in EXPECTED_PARAMETER_SCHEMAS[version].items()
    ]}]
    plan = build_tool_compatibility_plan(
        tools, selected_protocol="responses_structured", tool_choice="auto",
        protocol_capabilities=ProtocolCapabilities(
            function_lifecycle=True, namespace_lifecycle=native, accepts_namespace_adapter=True,
        ),
    )
    arguments = {"timeout_ms": value}
    if version == "collaboration_v1":
        arguments["targets"] = ["child"]
    wire = json.dumps(arguments)
    history = [{"type": "function_call", "id": "fc_wait", "call_id": "call_wait",
                "namespace": namespace, "name": "wait_agent", "arguments": wire}]
    if version == "collaboration_v2":
        message = "Wait timed out."
        if value is not None and value < 10000:
            message += f"\n\nRequested timeout of {value}ms was clamped to the minimum of 10000ms."
        output = json.dumps({"message": message, "timed_out": True})
        if value is not None and value > 3600000:
            output = "timeout_ms must be at most 3600000"
    else:
        output = json.dumps({"status": {}, "timed_out": True})
        if value is not None and value <= 0:
            output = "timeout_ms must be greater than zero"
    history.append({"type": "function_call_output", "id": "out_wait", "call_id": "call_wait", "output": output})
    encoded = plan.encode_payload({"tools": tools, "tool_choice": "auto", "input": history})
    restored = plan.decode_payload({"input": encoded["input"]})["input"]
    assert restored[0]["arguments"] == wire
    assert restored[1] == history[1]
    # Newly generated calls must use the same client integer domain. The client
    # owns clamping/range errors, so body and stream cannot impose a duration floor.
    call = encoded["input"][0]
    assert plan.decode_payload({"output": [call]})["output"][0]["arguments"] == wire
    stream = plan.new_stream()
    stream.decode_events_for_event({"type": "response.output_item.added", "output_index": 0,
        "item": {**call, "arguments": "", "status": "in_progress"}})
    events = stream.decode_events_for_event({"type": "response.function_call_arguments.done",
        "item_id": "fc_wait", "output_index": 0, "arguments": wire})
    assert events[0]["arguments"] == wire


@pytest.mark.parametrize("version,name,arguments", [
    ("collaboration_v2", "spawn_agent", {"task_name": "worker", "message": "review", field: None})
    for field in ("agent_type", "model", "reasoning_effort", "fork_turns")
] + [("collaboration_v2", "list_agents", {"path_prefix": None})] + [
    ("collaboration_v1", "spawn_agent", {field: None})
    for field in ("message", "items", "agent_type", "model", "reasoning_effort")
] + [
    ("collaboration_v1", "send_input", {"target": "child", field: None})
    for field in ("message", "items")
])
@pytest.mark.parametrize("native", [True, False])
def test_client_optional_null_arguments_preserve_wire_values(version, name, arguments, native):
    wire = json.dumps(arguments)
    assert normalize_collaboration_arguments(version, name, wire) == (wire, False)
    plan, namespace = _client_plan(version, native)
    call = {"type": "function_call", "id": "fc_nullable", "call_id": "call_nullable",
            "namespace": namespace, "name": name, "arguments": wire}
    encoded = plan.encode_payload({"input": [call]})
    assert plan.decode_payload({"input": encoded["input"]})["input"][0]["arguments"] == wire


@pytest.mark.parametrize("version,name,arguments", [
    ("collaboration_v1", "spawn_agent", {"fork_context": None}),
    ("collaboration_v1", "send_input", {"target": "child", "interrupt": None}),
    ("collaboration_v2", "send_message", {"target": "child", "message": None}),
    ("collaboration_v2", "spawn_agent", {"task_name": None, "message": "review"}),
])
def test_nullable_options_do_not_relax_required_values_or_plain_booleans(version, name, arguments):
    with pytest.raises(CollaborationContractError):
        normalize_collaboration_arguments(version, name, json.dumps(arguments))


def _client_plan(version, native):
    namespace = "multi_agent_v1" if version == "collaboration_v1" else "collaboration"
    tools = [{"type": "namespace", "name": namespace, "description": "runtime", "tools": [
        {"type": "function", "name": name, "description": "runtime", "strict": False,
         "parameters": copy.deepcopy(schema)}
        for name, schema in EXPECTED_PARAMETER_SCHEMAS[version].items()
    ]}]
    return build_tool_compatibility_plan(
        tools, selected_protocol="responses_structured", tool_choice="auto",
        protocol_capabilities=ProtocolCapabilities(
            function_lifecycle=True, namespace_lifecycle=native, accepts_namespace_adapter=True,
        ),
    ), namespace


@pytest.mark.parametrize("native", [True, False])
@pytest.mark.parametrize("version", ["collaboration_v1", "collaboration_v2"])
def test_client_parse_error_can_resume_without_reinterpreting_failed_arguments(version, native):
    plan, namespace = _client_plan(version, native)
    wire = '{"timeout_ms":true}'
    if version == "collaboration_v1":
        wire = '{"targets":["child"],"timeout_ms":true}'
    history = [
        {"type": "function_call", "id": "fc_error", "call_id": "call_error",
         "namespace": namespace, "name": "wait_agent", "arguments": wire},
        {"type": "function_call_output", "id": "out_error", "call_id": "call_error",
         "output": "failed to parse function arguments: invalid type: boolean `true`, expected i64"},
    ]
    encoded = plan.encode_payload({"input": history})
    restored = plan.decode_payload({"input": encoded["input"]})["input"]
    assert restored[0]["arguments"] == wire
    assert restored[1] == history[1]


@pytest.mark.parametrize("version,name,arguments,output", [
    ("collaboration_v1", "spawn_agent", {}, "Provide one of: message or items"),
    ("collaboration_v1", "send_input", {"target": "child", "message": "x", "items": []}, "Provide either message or items, but not both"),
    ("collaboration_v1", "spawn_agent", {"items": []}, "Items can't be empty"),
    ("collaboration_v1", "send_input", {"target": "child", "message": ""}, "Empty message can't be sent to an agent"),
    ("collaboration_v2", "send_message", {"target": "/root/missing", "message": "x"}, "live agent path `/root/missing` not found"),
    ("collaboration_v2", "followup_task", {"target": "/root/missing", "message": "x"}, "agent with id 00000000-0000-4000-8000-000000000001 not found"),
    ("collaboration_v1", "send_input", {"target": "child", "message": "x"}, "agent with id 00000000-0000-4000-8000-000000000001 is closed"),
    ("collaboration_v2", "send_message", {"target": "/root/child", "message": "x"}, "collab tool failed: synthetic runtime failure"),
    ("collaboration_v2", "spawn_agent", {"task_name": "worker", "message": "x"}, "collab spawn failed: synthetic runtime failure"),
    ("collaboration_v2", "list_agents", {}, "collab spawn failed: synthetic runtime failure"),
    ("collaboration_v2", "spawn_agent", {"task_name": "worker", "message": "x"}, "spawned agent is missing a canonical task name"),
])
@pytest.mark.parametrize("native", [True, False])
def test_client_runtime_failure_history_remains_an_unchanged_failure(version, name, arguments, output, native):
    plan, namespace = _client_plan(version, native)
    wire = json.dumps(arguments)
    history = [
        {"type": "function_call", "id": "fc_failure", "call_id": "call_failure",
         "namespace": namespace, "name": name, "arguments": wire},
        {"type": "function_call_output", "id": "out_failure", "call_id": "call_failure", "output": output},
    ]
    encoded = plan.encode_payload({"input": history})
    restored = plan.decode_payload({"input": encoded["input"]})["input"]
    assert restored[0]["arguments"] == wire
    assert restored[1] == history[1]


@pytest.mark.parametrize("name,output", [
    ("send_message", "collab tool failed: "),
    ("spawn_agent", "collab spawn failed: \n"),
    ("list_agents", "collab tool failed: fixture"),
    ("wait_agent", "live agent path `/root/missing` not found"),
    ("send_message", "agent with id not-a-uuid not found"),
    ("send_message", "live agent path `/root/missing` not found\nextra"),
    ("send_message", '"collab tool failed: fixture"'),
    ("spawn_agent", '{"error":"collab spawn failed: fixture"}'),
])
def test_runtime_failure_framing_does_not_waive_other_result_contracts(name, output):
    from collaboration_runtime_contract import validate_collaboration_result

    with pytest.raises(CollaborationContractError):
        validate_collaboration_result("collaboration_v2", name, output)
