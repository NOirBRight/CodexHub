"""Regression coverage for parent-owned Collaboration execution.

These tests intentionally use the public Gateway body/SSE seams.  The
Gateway may adapt a wire protocol, but it must never turn a completed child
operation into a decision that the parent task is finished.
"""

from __future__ import annotations

import copy
import json

import pytest

import collaboration_adapter
import gateway_compat
import gateway_events
import gateway_errors
from codex_semantic_adapter import COLLABORATION_V1, COLLABORATION_V2
from collaboration_runtime_contract import EXPECTED_PARAMETER_SCHEMAS
from runtime_tool_compatibility import ProtocolCapabilities, ToolCompatibilityError, build_tool_compatibility_plan


def _namespace(version: str) -> dict[str, object]:
    children = []
    for name, schema in EXPECTED_PARAMETER_SCHEMAS[version].items():
        parameters = copy.deepcopy(schema)
        if not parameters["required"]:
            parameters.pop("required")
        children.append(
            {
                "type": "function",
                "name": name,
                "description": "fixture collaboration tool",
                "strict": False,
                "parameters": parameters,
            }
        )
    return {
        "type": "namespace",
        "name": "multi_agent_v1" if version == COLLABORATION_V1 else "collaboration",
        "description": "fixture collaboration namespace",
        "tools": children,
    }


def _parent_tool() -> dict[str, object]:
    return {
        "type": "function",
        "name": "run_parent_step",
        "description": "parent implementation and test step",
        "strict": False,
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    }


def _call(call_id: str, name: str, arguments: dict[str, object]) -> dict[str, object]:
    return {
        "id": f"item_{call_id}",
        "type": "function_call",
        "namespace": "multi_agent_v1",
        "name": name,
        "call_id": call_id,
        "arguments": json.dumps(arguments, separators=(",", ":")),
    }


def _result(call_id: str, output: object) -> dict[str, object]:
    return {
        "id": f"output_{call_id}",
        "type": "function_call_output",
        "call_id": call_id,
        "output": json.dumps(output, separators=(",", ":")),
    }


def _completed_child_history(*, close_output: object | None = None) -> list[dict[str, object]]:
    close_output = close_output if close_output is not None else {"previous_status": {"completed": "inspect done"}}
    return [
        {"type": "message", "role": "user", "content": "Use one subagent to inspect only; then implement and test yourself."},
        _call("spawn", "spawn_agent", {"agent_type": "default", "message": "Inspect only."}),
        _result("spawn", {"agent_id": "child", "nickname": "inspector"}),
        _call("wait", "wait_agent", {"targets": ["child"], "timeout_ms": 60000}),
        _result("wait", {"timed_out": False, "status": {"child": {"completed": "inspection complete"}}}),
        _call("close", "close_agent", {"target": "child"}),
        _result("close", close_output),
    ]


def _upstream(protocol: str = "responses_structured") -> dict[str, object]:
    return {
        "name": "fixture-provider",
        "upstream_model": "fixture-model",
        "upstream_format": "chat_completions" if protocol == "chat_tools" else "responses",
        "tool_protocol": protocol,
        "tool_surface_strategy": "eager",
        "tool_protocol_capabilities": {"namespace_lifecycle": True},
    }


def _request(history: list[dict[str, object]], *, tools: list[dict[str, object]] | None = None) -> bytes:
    return json.dumps(
        {
            "model": "fixture-model",
            "input": history,
            "tools": tools if tools is not None else [_namespace(COLLABORATION_V1), _parent_tool()],
            "tool_choice": "auto",
        }
    ).encode()


def _parent_call() -> dict[str, object]:
    return {
        "type": "function_call",
        "id": "parent-item",
        "call_id": "parent-call",
        "name": "run_parent_step",
        "arguments": "{}",
        "status": "completed",
    }


def _has_parent_call(value: object) -> bool:
    if isinstance(value, list):
        return any(_has_parent_call(item) for item in value)
    if not isinstance(value, dict):
        return False
    return (
        value.get("type") == "function_call" and value.get("name") == "run_parent_step"
    ) or any(_has_parent_call(item) for item in value.values())


@pytest.mark.parametrize("protocol", ["responses_structured", "chat_tools"])
@pytest.mark.parametrize(
    ("assist_mode", "repair_policy"),
    [("assisted", "codex_subagent_repair"), ("strict", "codex_subagent_repair"), ("assisted", "none")],
)
def test_completed_child_never_removes_parent_continuation_tools(
    monkeypatch, protocol: str, assist_mode: str, repair_policy: str
) -> None:
    """A child closing is not a parent terminal event on body or SSE paths."""
    monkeypatch.setattr(gateway_events, "write_proxy_event", lambda *args, **kwargs: None)
    monkeypatch.setenv("CODEXHUB_SUBAGENT_ASSIST_MODE", assist_mode)
    context: dict[str, object] = {"repair_policy": repair_policy}

    prepared = json.loads(gateway_compat.compatible_request_body(_request(_completed_child_history()), _upstream(protocol), event_context=context))
    assert any(tool.get("name") == "run_parent_step" for tool in prepared["tools"])
    assert not any(
        item.get("type") == "message" and "required_next_action: write the final" in str(item.get("content"))
        for item in prepared["input"]
        if isinstance(item, dict)
    )

    response = json.loads(
        gateway_compat.compatible_response_body(
            json.dumps({"object": "response", "id": "response", "status": "completed", "output": [_parent_call()]}).encode(),
            "fixture-provider",
            event_context=context,
        )
    )
    assert _has_parent_call(response)

    stream_item = {**_parent_call(), "status": "in_progress", "arguments": ""}
    gateway_compat.compatible_sse_line(
        ("data: " + json.dumps({"type": "response.output_item.added", "output_index": 0, "item": stream_item}) + "\n\n").encode(),
        "fixture-provider",
        event_context=context,
    )
    gateway_compat.compatible_sse_line(
        ("data: " + json.dumps({"type": "response.function_call_arguments.done", "output_index": 0, "item_id": "parent-item", "arguments": "{}"}) + "\n\n").encode(),
        "fixture-provider",
        event_context=context,
    )
    sse = gateway_compat.compatible_sse_line(
        ("data: " + json.dumps({"type": "response.output_item.done", "output_index": 0, "item": _parent_call()}) + "\n\n").encode(),
        "fixture-provider",
        event_context=context,
    )
    assert _has_parent_call(json.loads(sse.split(b":", 1)[1].strip()))


def test_new_user_turn_and_failed_close_do_not_take_over_parent_tools(monkeypatch) -> None:
    monkeypatch.setattr(gateway_events, "write_proxy_event", lambda *args, **kwargs: None)
    history = _completed_child_history(close_output={"error": "fixture close failed; child remains open"})
    history.extend(
        [
            {"type": "message", "role": "assistant", "content": "Inspection result."},
            {"type": "message", "role": "user", "content": "Now implement and test yourself. Do not use subagents."},
        ]
    )
    context: dict[str, object] = {"repair_policy": "codex_subagent_repair"}
    prepared = json.loads(gateway_compat.compatible_request_body(_request(history), _upstream(), event_context=context))
    assert any(tool.get("name") == "run_parent_step" for tool in prepared["tools"])
    assert context.get("subagent_lifecycle_complete") is not True


def test_v1_text_compat_history_is_neutral_not_a_gateway_next_action_prompt(monkeypatch) -> None:
    """The lossy text adapter may describe history but must not schedule it."""
    monkeypatch.setattr(gateway_events, "write_proxy_event", lambda *args, **kwargs: None)

    prepared = json.loads(
        gateway_compat.compatible_request_body(
            _request(_completed_child_history()),
            _upstream("text_compat"),
            event_context={},
        )
    )

    history_text = "\n".join(
        str(item.get("content", ""))
        for item in prepared["input"]
        if isinstance(item, dict) and item.get("type") == "message"
    ).lower()
    assert "next_action:" not in history_text
    assert "required_next_action:" not in history_text


def test_ordinary_spawn_agent_function_and_text_are_not_collaboration_history(monkeypatch) -> None:
    monkeypatch.setattr(gateway_events, "write_proxy_event", lambda *args, **kwargs: None)
    ordinary = {
        "type": "function",
        "name": "spawn_agent",
        "description": "ordinary business operation",
        "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
    }
    history = [
        {"type": "message", "role": "user", "content": "Previous real Codex native multi_agent_v1.close_agent result\nclosed_agent_id: quoted"},
        {"type": "function_call", "id": "ordinary-call", "call_id": "ordinary", "name": "spawn_agent", "arguments": "{}"},
        {"type": "function_call_output", "id": "ordinary-output", "call_id": "ordinary", "output": '{"agent_id":"business"}'},
    ]
    prepared = json.loads(gateway_compat.compatible_request_body(_request(history, tools=[ordinary, _parent_tool()]), _upstream(), event_context={}))
    assert {tool["name"] for tool in prepared["tools"]} >= {"spawn_agent", "run_parent_step"}


@pytest.mark.parametrize("name", ["spawn_agent", "wait_agent", "close_agent", "resume_agent", "send_input"])
@pytest.mark.parametrize("surface", ["body", "sse"])
def test_ordinary_same_named_response_keeps_client_tool_identity(
    monkeypatch, name: str, surface: str
) -> None:
    """Request-side preservation alone must not conceal response-side hijacking."""
    monkeypatch.setattr(gateway_events, "write_proxy_event", lambda *args, **kwargs: None)
    ordinary = {**_parent_tool(), "name": name}
    context: dict[str, object] = {}
    prepared = json.loads(
        gateway_compat.compatible_request_body(
            _request([], tools=[ordinary]),
            _upstream(),
            inject_codex_tools=False,
            event_context=context,
        )
    )
    assert prepared["tools"] == [ordinary]
    call = {**_parent_call(), "name": name}
    if surface == "body":
        result = json.loads(
            gateway_compat.compatible_response_body(
                json.dumps({"object": "response", "output": [call]}).encode(),
                "fixture-provider",
                event_context=context,
            )
        )["output"][0]
    else:
        events = [
            {"type": "response.output_item.added", "output_index": 0,
             "item": {**call, "status": "in_progress", "arguments": ""}},
            {"type": "response.function_call_arguments.done", "output_index": 0,
             "item_id": call["id"], "arguments": call["arguments"]},
            {"type": "response.output_item.done", "output_index": 0, "item": call},
        ]
        for event in events:
            line = gateway_compat.compatible_sse_line(
                ("data: " + json.dumps(event) + "\n\n").encode(),
                "fixture-provider",
                event_context=context,
            )
        result = json.loads(line.split(b":", 1)[1].strip())["item"]
    assert result["name"] == name
    assert result.get("namespace") is None
    assert result["arguments"] == call["arguments"]


@pytest.mark.parametrize("inject_codex_tools", [False, True])
def test_unregistered_alias_shaped_function_remains_client_owned(
    monkeypatch, inject_codex_tools: bool
) -> None:
    monkeypatch.setattr(gateway_events, "write_proxy_event", lambda *args, **kwargs: None)
    ordinary = {**_parent_tool(), "name": "multi_agent_v1__spawn_agent"}
    context: dict[str, object] = {}
    prepared = json.loads(
        gateway_compat.compatible_request_body(
            _request([], tools=[ordinary]),
            _upstream(),
            inject_codex_tools=inject_codex_tools,
            event_context=context,
        )
    )
    assert next(tool for tool in prepared["tools"] if tool.get("name") == ordinary["name"]) == ordinary
    assert context.get("collaboration_protocol") is None


def test_no_collaboration_declaration_does_not_grant_subagent_tools(monkeypatch) -> None:
    monkeypatch.setattr(gateway_events, "write_proxy_event", lambda *args, **kwargs: None)
    monkeypatch.setenv("CODEXHUB_SUBAGENT_ASSIST_MODE", "strict")
    prepared = json.loads(
        gateway_compat.compatible_request_body(
            _request([], tools=[_parent_tool()]),
            _upstream(),
            event_context={"repair_policy": "none"},
        )
    )
    assert not any(
        str(tool.get("name", "")).startswith("multi_agent_v1")
        for tool in prepared["tools"]
    )


@pytest.mark.parametrize("ordinary", [True, False])
def test_client_role_is_not_reinterpreted_as_gateway_worker_authority(ordinary, monkeypatch):
    monkeypatch.setattr(gateway_events, "write_proxy_event", lambda *args, **kwargs: None)
    name = "multi_agent_v1__spawn_agent" if ordinary else "spawn_agent"
    tools = [{**_parent_tool(), "name": name}] if ordinary else [_namespace(COLLABORATION_V1)]
    context = {}
    gateway_compat.compatible_request_body(_request([], tools=tools), _upstream(), event_context=context)
    call = {"id": "role-item", "call_id": "role-call", "type": "function_call", "name": name,
            "arguments": json.dumps({"agent_type": "worker" if ordinary else "reviewer", "message": "inspect"})}
    if not ordinary:
        call["namespace"] = "multi_agent_v1"
    response = json.loads(gateway_compat.compatible_response_body(json.dumps({"object": "response", "output": [call]}).encode(), "fixture-provider", event_context=context))
    assert response["output"][0] == call


@pytest.mark.parametrize("version", [COLLABORATION_V1, COLLABORATION_V2])
def test_declared_plain_spawn_history_can_coexist_with_real_namespace(version, monkeypatch):
    monkeypatch.setattr(gateway_events, "write_proxy_event", lambda *args, **kwargs: None)
    ordinary = {**_parent_tool(), "name": "spawn_agent"}
    call = {"type": "function_call", "id": "ordinary-item", "call_id": "ordinary-call", "name": "spawn_agent", "arguments": "{}"}
    context = {}
    prepared = json.loads(gateway_compat.compatible_request_body(
        _request([call, _result("ordinary-call", {})], tools=[ordinary, _namespace(version)]),
        _upstream(), inject_codex_tools=False, event_context=context,
    ))
    # Provider history shaping may omit its optional item id, but not the
    # client routing identity, arguments, or call/result association.
    assert {key: prepared["input"][0][key] for key in ("type", "call_id", "name", "arguments")} == {
        key: call[key] for key in ("type", "call_id", "name", "arguments")
    }
    assert "namespace" not in prepared["input"][0]


def test_v1_registered_alias_is_restored_to_a_client_executable_namespace_call(monkeypatch) -> None:
    """A Gateway-only alias must never reach Codex's native tool router."""
    monkeypatch.setattr(gateway_events, "write_proxy_event", lambda *args, **kwargs: None)
    context: dict[str, object] = {}
    prepared = json.loads(
        gateway_compat.compatible_request_body(
            _request([], tools=[_namespace(COLLABORATION_V1)]),
            {**_upstream(), "tool_protocol_capabilities": {"namespace_lifecycle": False}},
            event_context=context,
        )
    )
    plan = context["_runtime_tool_compatibility_plan"]
    entry = next(entry for entry in plan.entries if entry.namespace == "multi_agent_v1")
    alias = entry.aliases[entry.child_names.index("spawn_agent")]
    response = json.loads(
        gateway_compat.compatible_response_body(
            json.dumps(
                {
                    "object": "response",
                    "output": [
                        {
                            "type": "function_call",
                            "id": "v1-spawn-item",
                            "call_id": "v1-spawn-call",
                            "name": alias,
                            "arguments": '{"agent_type":"default","message":"inspect"}',
                        }
                    ],
                }
            ).encode(),
            "fixture-provider",
            event_context=context,
        )
    )
    restored = response["output"][0]
    assert restored["namespace"] == "multi_agent_v1"
    assert restored["name"] == "spawn_agent"


def _v2_plan():
    return build_tool_compatibility_plan(
        [_namespace(COLLABORATION_V2)],
        selected_protocol="responses_structured",
        tool_choice="auto",
        protocol_capabilities=ProtocolCapabilities.responses_structured(
            namespace_lifecycle=False,
            function_lifecycle=True,
            accepts_namespace_adapter=True,
        ),
        request_token="subagent-regression",
    )


@pytest.mark.parametrize(
    "arguments",
    [
        {"agent_type": "worker", "message": "Inspect only.", "fork_context": False},
        {"task_name": "inspector", "message": "Inspect only.", "custom_constraint": "preserve this"},
    ],
)
def test_v2_body_and_stream_reject_unrepresentable_arguments_identically(arguments: dict[str, object]) -> None:
    plan = _v2_plan()
    item = {
        "type": "function_call",
        "id": "v2-item",
        "call_id": "v2-call",
        "name": plan.entries[0].aliases[4],
        "arguments": json.dumps(arguments, separators=(",", ":")),
    }
    with pytest.raises(ToolCompatibilityError):
        plan.decode_payload({"output": [item]})

    stream = _v2_plan().new_stream()
    with pytest.raises(ToolCompatibilityError):
        stream.decode_events_for_event(
            {"type": "response.output_item.done", "output_index": 0, "item": {**item, "status": "completed"}}
        )


def test_v2_invalid_stream_call_fails_before_a_completed_event_can_be_emitted(monkeypatch) -> None:
    """A malformed adapted call is an error terminal, not ``response.completed``."""
    monkeypatch.setattr(gateway_events, "write_proxy_event", lambda *args, **kwargs: None)
    context: dict[str, object] = {}
    prepared = json.loads(
        gateway_compat.compatible_request_body(
            _request([], tools=[_namespace(COLLABORATION_V2)]),
            {
                **_upstream(),
                "tool_protocol_capabilities": {
                    "namespace_lifecycle": False,
                    "function_lifecycle": True,
                    "accepts_namespace_adapter": True,
                },
            },
            event_context=context,
        )
    )
    plan = context["_runtime_tool_compatibility_plan"]
    alias = next(
        entry.aliases[4]
        for entry in plan.entries
        if entry.version == "v2" and entry.namespace == "collaboration"
    )
    item = {
        "type": "function_call",
        "id": "invalid-v2-item",
        "call_id": "invalid-v2-call",
        "name": alias,
        "arguments": "",
        "status": "in_progress",
    }
    added = {
        "type": "response.output_item.added",
        "output_index": 0,
        "item": item,
    }
    assert gateway_compat.compatible_sse_line(
        ("data: " + json.dumps(added) + "\n\n").encode(),
        "fixture-provider",
        event_context=context,
    )

    invalid_done = {
        "type": "response.function_call_arguments.done",
        "output_index": 0,
        "item_id": "invalid-v2-item",
        "arguments": '{"message":"missing task_name"}',
    }
    with pytest.raises(gateway_errors.UpstreamProtocolTranslationError) as error:
        gateway_compat.compatible_sse_line(
            ("data: " + json.dumps(invalid_done) + "\n\n").encode(),
            "fixture-provider",
            event_context=context,
        )
    assert error.value.classification == "collaboration_arguments_schema_mismatch"


@pytest.mark.parametrize(
    ("name", "arguments"),
    [
        ("spawn_agent", '{"message":"missing task name"}'),
        ("wait_agent", '{"timeout_ms":"not a number"}'),
        ("followup_task", '{"target":"child"}'),
    ],
)
def test_v2_parse_error_history_is_replayed_without_revalidating_failed_arguments(
    name: str, arguments: str
) -> None:
    plan = _v2_plan()
    history = [
        {
            "type": "function_call",
            "id": "failed-call-item",
            "call_id": "failed-call",
            "namespace": "collaboration",
            "name": name,
            "arguments": arguments,
        },
        {
            "type": "function_call_output",
            "id": "failed-output-item",
            "call_id": "failed-call",
            "output": "failed to parse function arguments: missing field `task_name`",
        },
    ]
    assert plan.encode_payload({"tool_choice": "auto", "tools": [_namespace(COLLABORATION_V2)], "input": history})["input"]


@pytest.mark.parametrize("number", ["NaN", "Infinity", "-Infinity", "1e999"])
def test_v2_rejects_non_finite_numbers(number: str) -> None:
    plan = _v2_plan()
    item = {
        "type": "function_call",
        "id": "number-item",
        "call_id": "number-call",
        "name": plan.entries[0].aliases[5],
        "arguments": '{"timeout_ms":' + number + "}",
    }
    with pytest.raises(ToolCompatibilityError):
        plan.decode_payload({"output": [item]})


def test_v2_rejects_duplicate_json_argument_keys() -> None:
    plan = _v2_plan()
    item = {
        "type": "function_call",
        "id": "duplicate-key-item",
        "call_id": "duplicate-key-call",
        "name": plan.entries[0].aliases[4],
        "arguments": '{"task_name":"first","task_name":"second","message":"inspect"}',
    }
    with pytest.raises(ToolCompatibilityError):
        plan.decode_payload({"output": [item]})


def test_worker_error_cannot_become_an_effective_binding_match(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(collaboration_adapter, "WORKER_BINDING_SIGNING_ROOT", tmp_path)
    requested = {"agent_type": "worker", "model": "fixture-external", "reasoning": "high"}
    sidecar = collaboration_adapter.requested_binding_sidecar(requested, "worker-call")
    history = {
        "input": [
            _call(
                "worker-call",
                "spawn_agent",
                {"agent_type": "worker", "model": None, "message": "Inspect.", "_codexhub_worker_requested_binding": sidecar},
            ),
            _result("worker-call", {"agent_id": "", "nickname": None, "error": "fixture spawn failed"}),
        ]
    }
    with pytest.raises(Exception):
        collaboration_adapter.validate_worker_binding_history(history)


def test_worker_signature_with_non_ascii_text_is_a_controlled_rejection(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(collaboration_adapter, "WORKER_BINDING_SIGNING_ROOT", tmp_path)
    requested = {"agent_type": "worker", "model": "fixture-external", "reasoning": "high"}
    sidecar = collaboration_adapter.requested_binding_sidecar(requested, "worker-call")
    verified, reason = collaboration_adapter.verified_requested_binding({**sidecar, "signature": "中" * 64}, "worker-call")
    assert verified is None
    assert reason == "unknown_requested_binding_sidecar"
