"""Native parent/child continuations must not enter the Chat alias adapter."""
import copy
import json

import pytest

import gateway_compat
import gateway_errors
import gateway_events
from collaboration_runtime_contract import (
    COLLABORATION_V1, COLLABORATION_V2, EXPECTED_PARAMETER_SCHEMAS,
)

ARGUMENTS = {
    "followup_task": {"target": "/root/worker", "message": "continue"},
    "interrupt_agent": {"target": "/root/worker"},
    "list_agents": {},
    "send_message": {"target": "/root", "message": "opaque-message"},
    "spawn_agent": {"task_name": "worker", "message": "inspect"},
    "wait_agent": {},
}
RESULTS = {
    "followup_task": "",
    "interrupt_agent": '{"previous_status":"running"}',
    "list_agents": '{"agents":[]}',
    "send_message": "",
    "spawn_agent": '{"task_name":"/root/worker"}',
    "wait_agent": '{"message":"done","timed_out":false}',
}


def namespace(version=COLLABORATION_V2):
    return {
        "type": "namespace",
        "name": "collaboration" if version == COLLABORATION_V2 else "multi_agent_v1",
        "description": "native collaboration",
        "tools": [
            {"type": "function", "name": name, "description": name,
             "strict": False, "parameters": copy.deepcopy(schema)}
            for name, schema in EXPECTED_PARAMETER_SCHEMAS[version].items()
        ],
    }


def prepare(tools, history):
    context = {}
    body = gateway_compat.compatible_request_body(
        json.dumps({"model": "placeholder", "tools": tools, "input": history,
                    "tool_choice": "auto"}).encode(),
        {"name": "official", "upstream_format": "responses"},
        event_context=context, inject_codex_tools=False,
    )
    return json.loads(body), context


@pytest.mark.parametrize("version,name", [
    (version, name) for version, schemas in EXPECTED_PARAMETER_SCHEMAS.items()
    for name in schemas
])
def test_native_collaboration_continuation_preserves_call_and_result(version, name):
    declaration = namespace(version)
    history = [
        {"type": "function_call", "namespace": declaration["name"], "id": "item-native",
         "name": name, "call_id": "call-native", "arguments": json.dumps(ARGUMENTS.get(name, {}))},
        {"type": "function_call_output", "id": "result-native", "call_id": "call-native",
         "output": RESULTS.get(name, "")},
    ]
    prepared, context = prepare([declaration], history)
    assert prepared["tools"] == [declaration]
    assert prepared["input"] == history
    assert context["collaboration_protocol"] == version
    assert "_chat_official_v2_name_map" not in context


@pytest.mark.parametrize("with_declaration", [False, True])
def test_child_continuation_with_opaque_call_and_other_native_tool(with_declaration):
    history = [
        {"type": "agent_message", "id": "handoff", "author": "/root",
         "recipient": "/root/worker", "content": [
             {"type": "encrypted_content", "encrypted_content": "opaque-handoff"}]},
        {"type": "function_call", "namespace": "collaboration", "name": "send_message",
         "id": "item-child", "call_id": "call-child", "arguments": json.dumps(ARGUMENTS["send_message"]),
         "encrypted_function_args": ["message"]},
        {"type": "function_call_output", "id": "result-child", "call_id": "call-child", "output": ""},
    ]
    tools = [namespace()] if with_declaration else []
    tools.append({"type": "custom", "name": "exec", "description": "execute"})
    prepared, _ = prepare(tools, history)
    assert prepared["input"] == history


def test_native_collaboration_response_retains_encryption_marker_with_custom_tool():
    _, context = prepare([namespace(), {"type": "custom", "name": "exec"}], [])
    call = {"type": "function_call", "namespace": "collaboration", "name": "send_message",
            "id": "item-response", "call_id": "call-response",
            "arguments": json.dumps(ARGUMENTS["send_message"]),
            "encrypted_function_args": ["message"]}
    body = json.dumps({"output": [call]}).encode()
    assert json.loads(gateway_compat.compatible_response_body(body, "official", context)) == {
        "output": [call]
    }


@pytest.mark.parametrize("name", ["spawn_agent", "send_message", "close_agent"])
def test_unrelated_namespace_can_reuse_collaboration_names(name):
    declaration = {"type": "namespace", "name": "user_tools", "tools": [
        {"type": "function", "name": name, "parameters": {"type": "object"}}]}
    history = [{"type": "function_call", "namespace": "user_tools", "name": name,
                "call_id": "call-user", "arguments": "{}"}]
    prepared, _ = prepare([declaration], history)
    assert prepared["input"] == history


@pytest.mark.parametrize("mutation", ["missing_child", "duplicate_child", "missing_namespace"])
def test_malformed_native_collaboration_still_fails_closed(mutation):
    declaration = namespace()
    call = {"type": "function_call", "namespace": "collaboration", "name": "spawn_agent",
            "call_id": "call-invalid", "arguments": "{}"}
    if mutation == "missing_child":
        declaration["tools"].pop()
    elif mutation == "duplicate_child":
        declaration["tools"].append(copy.deepcopy(declaration["tools"][0]))
    else:
        call.pop("namespace")
    with pytest.raises(gateway_errors.UpstreamProtocolTranslationError):
        prepare([declaration], [call])


def test_rejected_collaboration_diagnostics_do_not_expose_payload(monkeypatch):
    sentinel = "private-task-content-and-credential-sentinel"
    events = []
    monkeypatch.setattr(gateway_events, "write_proxy_event",
                        lambda event, **fields: events.append((event, fields)))
    declaration = namespace()
    declaration["description"] = sentinel
    declaration["tools"].pop()
    with pytest.raises(gateway_errors.UpstreamProtocolTranslationError) as error:
        prepare([declaration], [{"type": "message", "role": "user", "content": sentinel}])
    downstream = gateway_errors.downstream_json_error_payload(gateway_errors.DownstreamErrorSpec(
        inbound_format="responses", upstream_name="official", status=400,
        exc=error.value, error="tool_compatibility_boundary",
    ))
    assert sentinel not in json.dumps(downstream)
    assert sentinel not in json.dumps(events)
