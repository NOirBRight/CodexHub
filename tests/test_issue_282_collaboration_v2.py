from __future__ import annotations

import copy
import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

import pytest

import codex_proxy
from codex_semantic_adapter import (
    COLLABORATION_V1,
    COLLABORATION_V2,
    CollaborationBoundaryError,
    classify_collaboration_payload,
)
from runtime_tool_compatibility import (
    ProtocolCapabilities,
    ToolCompatibilityError,
    build_tool_compatibility_plan,
)


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_GENERATOR = ROOT / "scripts" / "build_issue_392_collaboration_contract.py"


def _load_contract_generator():
    spec = importlib.util.spec_from_file_location("issue392_runtime_contract", CONTRACT_GENERATOR)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _declaration(version: str) -> dict[str, object]:
    module = _load_contract_generator()
    namespace = module.V1_NAMESPACE if version == COLLABORATION_V1 else module.V2_NAMESPACE
    children = []
    for name, parameter_schema in module.EXPECTED_PARAMETER_SCHEMAS[version].items():
        parameters = copy.deepcopy(parameter_schema)
        if not parameters["required"]:
            del parameters["required"]
        children.append(
            {
                "type": "function",
                "name": name,
                "description": f"dynamic child description for {name}",
                "strict": False,
                "parameters": parameters,
            }
        )
    return {
        "type": "namespace",
        "name": namespace,
        "description": "dynamic namespace description",
        "tools": children,
    }


def _request(version: str) -> dict[str, object]:
    return {
        "model": "model-a",
        "tool_choice": "auto",
        "tools": [_declaration(version)],
        "input": [],
    }


def _responses_upstream(*, native_namespace: bool) -> dict[str, object]:
    return {
        "name": "responses_endpoint",
        "upstream_model": "model-a",
        "upstream_format": "responses",
        "tool_protocol": "responses_structured",
        "tool_surface_strategy": "eager",
        "tool_protocol_capabilities": {
            "function_lifecycle": True,
            "namespace_lifecycle": native_namespace,
            "accepts_namespace_adapter": True,
        },
    }


V2_ARGUMENTS = {
    "followup_task": {"target": "/root/worker", "message": "continue"},
    "interrupt_agent": {"target": "/root/worker"},
    "list_agents": {},
    "send_message": {"target": "/root/worker", "message": "status"},
    "spawn_agent": {"task_name": "worker", "message": "do work", "fork_turns": "all"},
    "wait_agent": {"timeout_ms": 1000},
}
V2_RESULTS = {
    "followup_task": None,
    "interrupt_agent": {"previous_status": "running"},
    "list_agents": {
        "agents": [{"agent_name": "/root/worker", "agent_status": "running"}]
    },
    "send_message": None,
    "spawn_agent": {"task_name": "/root/worker"},
    "wait_agent": {"message": "done", "timed_out": False},
}


def _v2_history() -> list[dict[str, object]]:
    history: list[dict[str, object]] = []
    for index, name in enumerate(V2_ARGUMENTS):
        call_id = f"call-{index}"
        history.extend(
            [
                {
                    "type": "function_call",
                    "id": f"item-call-{index}",
                    "call_id": call_id,
                    "namespace": "collaboration",
                    "name": name,
                    "arguments": json.dumps(V2_ARGUMENTS[name], separators=(",", ":")),
                },
                {
                    "type": "function_call_output",
                    "id": f"item-result-{index}",
                    "call_id": call_id,
                    "output": json.dumps(V2_RESULTS[name], separators=(",", ":")),
                },
            ]
        )
    history.append(
        {
            "type": "agent_message",
            "id": "agent-message-1",
            "author": "/root/worker",
            "recipient": "/root",
            "content": [
                {"type": "input_text", "text": "done"},
                {"type": "encrypted_content", "encrypted_content": "opaque"},
            ],
        }
    )
    return history


def _v2_plan(*, native: bool = False):
    capabilities = ProtocolCapabilities.responses_structured(
        namespace_lifecycle=native,
        function_lifecycle=True,
        accepts_namespace_adapter=True,
    )
    return build_tool_compatibility_plan(
        [_declaration(COLLABORATION_V2)],
        selected_protocol="responses_structured",
        tool_choice="auto",
        protocol_capabilities=capabilities,
        request_token="issue-282",
    )


def _ordinary_function_declaration() -> dict[str, object]:
    return {
        "type": "function",
        "name": "ordinary_lookup",
        "description": "An unrelated ordinary function.",
        "strict": False,
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
    }


def _mixed_v2_plan(*, native: bool = False):
    capabilities = ProtocolCapabilities.responses_structured(
        namespace_lifecycle=native,
        function_lifecycle=True,
        accepts_namespace_adapter=True,
    )
    return build_tool_compatibility_plan(
        [_declaration(COLLABORATION_V2), _ordinary_function_declaration()],
        selected_protocol="responses_structured",
        tool_choice="auto",
        protocol_capabilities=capabilities,
        request_token="issue-282-mixed",
    )


def test_exact_runtime_namespaces_classify_and_descriptions_are_dynamic() -> None:
    for version in (COLLABORATION_V1, COLLABORATION_V2):
        body = _request(version)
        assert classify_collaboration_payload(body) == version
        body["tools"][0]["description"] = "changed at runtime"
        for child in body["tools"][0]["tools"]:
            child["description"] = "also dynamic"
        assert classify_collaboration_payload(body) == version


def test_v1_default_role_spawn_schema_without_agent_type_classifies_exactly() -> None:
    body = _request(COLLABORATION_V1)
    spawn = next(
        child
        for child in body["tools"][0]["tools"]
        if child["name"] == "spawn_agent"
    )
    del spawn["parameters"]["properties"]["agent_type"]

    assert classify_collaboration_payload(body) == COLLABORATION_V1

    del spawn["parameters"]["properties"]["model"]
    with pytest.raises(CollaborationBoundaryError) as caught:
        classify_collaboration_payload(body)
    assert caught.value.classification == "namespace_child_parameter_schema_mismatch"


@pytest.mark.parametrize(
    ("mutate", "classification"),
    [
        (lambda body: body["tools"][0]["tools"].pop(), "namespace_child_set_invalid"),
        (
            lambda body: body["tools"][0]["tools"].append(
                copy.deepcopy(body["tools"][0]["tools"][0])
            ),
            "namespace_child_duplicate",
        ),
        (
            lambda body: body["tools"][0]["tools"][0].__setitem__("strict", True),
            "namespace_child_strict_invalid",
        ),
        (
            lambda body: body["tools"][0]["tools"][0]["parameters"].__setitem__(
                "additionalProperties", True
            ),
            "namespace_child_parameter_schema_mismatch",
        ),
        (
            lambda body: body["tools"][0]["tools"][0].__setitem__(
                "output_schema", {"type": "object"}
            ),
            "namespace_child_fields_invalid",
        ),
        (lambda body: body.__setitem__("tool_choice", "required"), "tool_choice_invalid"),
        (
            lambda body: body.__setitem__("metadata", {"multi_agent_version": "v2"}),
            "collaboration_version_signal_unexpected",
        ),
        (
            lambda body: body.__setitem__("features", {"multi_agent_version": "v2"}),
            "collaboration_version_signal_unexpected",
        ),
        (
            lambda body: body.__setitem__("client_metadata", {"multi_agent_version": "v2"}),
            "collaboration_version_signal_unexpected",
        ),
    ],
)
def test_exact_runtime_namespace_request_fails_closed(
    mutate,
    classification: str,
) -> None:
    body = _request(COLLABORATION_V2)
    mutate(body)
    with pytest.raises(CollaborationBoundaryError) as caught:
        classify_collaboration_payload(body)
    assert caught.value.classification == classification


def test_direct_duplicate_and_mixed_runtime_markers_fail_closed() -> None:
    direct = copy.deepcopy(_declaration(COLLABORATION_V2)["tools"])
    with pytest.raises(CollaborationBoundaryError) as caught:
        classify_collaboration_payload({"tool_choice": "auto", "tools": direct})
    assert caught.value.classification == "collaboration_marker_missing"

    duplicate = _request(COLLABORATION_V2)
    duplicate["tools"].append(copy.deepcopy(duplicate["tools"][0]))
    with pytest.raises(CollaborationBoundaryError) as caught:
        classify_collaboration_payload(duplicate)
    assert caught.value.classification == "collaboration_marker_duplicate_or_mixed"

    mixed = _request(COLLABORATION_V2)
    mixed["tools"].append(_declaration(COLLABORATION_V1))
    with pytest.raises(CollaborationBoundaryError) as caught:
        classify_collaboration_payload(mixed)
    assert caught.value.classification == "collaboration_marker_duplicate_or_mixed"


def test_invalid_request_is_rejected_before_runtime_planning() -> None:
    body = _request(COLLABORATION_V2)
    body["tools"][0]["tools"].pop()

    with patch.object(codex_proxy, "_prepare_runtime_tool_compatibility") as prepare:
        with pytest.raises(codex_proxy.UpstreamProtocolTranslationError) as caught:
            codex_proxy.compatible_request_body(
                json.dumps(body).encode(),
                _responses_upstream(native_namespace=False),
                event_context={"request_id": "bounded-request"},
                inject_codex_tools=False,
            )

    assert caught.value.cause.code == codex_proxy.COLLABORATION_BOUNDARY_ERROR_CODE
    prepare.assert_not_called()


def test_collaboration_rejection_diagnostic_is_count_only_and_content_safe() -> None:
    body = _request(COLLABORATION_V2)
    body["tools"][0]["tools"].pop()
    body["input"] = [
        {
            "type": "function_call",
            "id": "SECRET_ITEM",
            "call_id": "SECRET_CALL",
            "namespace": "collaboration",
            "name": "spawn_agent",
            "arguments": '{"task_name":"SECRET_TASK","message":"SECRET_MESSAGE"}',
        }
    ]

    with patch.object(codex_proxy, "write_proxy_event") as write_event:
        with pytest.raises(codex_proxy.UpstreamProtocolTranslationError):
            codex_proxy.compatible_request_body(
                json.dumps(body).encode(),
                _responses_upstream(native_namespace=False),
                event_context={"request_id": "SECRET_REQUEST"},
                inject_codex_tools=False,
            )

    rejected = next(
        call for call in write_event.call_args_list
        if call.args[0] == "collaboration_boundary_rejected"
    )
    assert rejected.kwargs == {"surface": "request", "outcome": "rejected", "count": 1}
    assert "SECRET" not in repr(rejected.kwargs)


def test_native_responses_namespace_is_validated_and_preserved_unchanged() -> None:
    body = _request(COLLABORATION_V2)
    raw = json.dumps(body, separators=(",", ":")).encode()
    context: dict[str, object] = {}

    transformed = codex_proxy.compatible_request_body(
        raw,
        _responses_upstream(native_namespace=True),
        event_context=context,
        inject_codex_tools=False,
    )

    assert transformed == raw
    assert context["collaboration_protocol"] == COLLABORATION_V2
    plan = context["_runtime_tool_compatibility_plan"]
    assert plan.entries[0].disposition == "native"
    assert plan.entries[0].version == "v2"


def test_conservative_responses_adapts_all_six_v2_children_without_v1_behavior() -> None:
    body = _request(COLLABORATION_V2)
    context: dict[str, object] = {
        "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT,
        "request_id": "bounded-request",
    }

    transformed = json.loads(
        codex_proxy.compatible_request_body(
            json.dumps(body).encode(),
            _responses_upstream(native_namespace=False),
            event_context=context,
            inject_codex_tools=True,
        )
    )

    aliases = [tool["name"] for tool in transformed["tools"]]
    assert len(aliases) == 6
    assert len(set(aliases)) == 6
    assert all(alias.startswith("__codexhub_ns_") for alias in aliases)
    assert not any("multi_agent_v1" in alias for alias in aliases)
    assert context["collaboration_protocol"] == COLLABORATION_V2
    assert context["subagent_spawn_allowed"] is False
    plan = context["_runtime_tool_compatibility_plan"]
    assert plan.entries[0].disposition == "adapt"
    assert plan.entries[0].child_names == (
        "followup_task",
        "interrupt_agent",
        "list_agents",
        "send_message",
        "spawn_agent",
        "wait_agent",
    )


def test_v2_chat_surface_fails_before_sampling_instead_of_downgrading() -> None:
    body = _request(COLLABORATION_V2)
    upstream = {
        "name": "chat_endpoint",
        "upstream_model": "model-a",
        "upstream_format": "chat_completions",
        "tool_protocol": "chat_tools",
        "tool_surface_strategy": "eager",
    }

    with pytest.raises(codex_proxy.UpstreamProtocolTranslationError) as caught:
        codex_proxy.compatible_request_body(
            json.dumps(body).encode(),
            upstream,
            event_context={},
            inject_codex_tools=False,
        )

    assert caught.value.cause.code == "tool_compatibility_required_unavailable"


def test_v2_unrepresentable_responses_surface_fails_instead_of_omitting_lifecycle() -> None:
    with pytest.raises(ToolCompatibilityError) as caught:
        build_tool_compatibility_plan(
            [_declaration(COLLABORATION_V2)],
            selected_protocol="responses_structured",
            tool_choice="auto",
            protocol_capabilities=ProtocolCapabilities(),
            request_token="unrepresentable",
        )

    assert caught.value.code == "tool_compatibility_required_unavailable"


@pytest.mark.parametrize("native", [False, True], ids=["adapted", "native"])
def test_v2_all_six_calls_results_and_agent_message_round_trip(native: bool) -> None:
    plan = _v2_plan(native=native)
    original = {
        "tool_choice": "auto",
        "tools": [_declaration(COLLABORATION_V2)],
        "input": _v2_history(),
    }

    encoded = plan.encode_payload(original)
    decoded = plan.decode_payload({"input": encoded["input"]})

    assert decoded["input"] == original["input"]
    assert [item["id"] for item in decoded["input"]] == [
        item["id"] for item in original["input"]
    ]
    if native:
        assert encoded == original
    else:
        calls = [item for item in encoded["input"] if item["type"] == "function_call"]
        assert len(calls) == 6
        assert all("namespace" not in item for item in calls)
        assert len({item["name"] for item in calls}) == 6


def test_v2_adapted_history_and_restart_replay_use_the_same_inverse() -> None:
    plan = _v2_plan()
    payload = {
        "tool_choice": "auto",
        "tools": [_declaration(COLLABORATION_V2)],
        "input": _v2_history(),
    }

    first = plan.encode_payload(payload)
    restart = plan.new_attempt().encode_payload(payload)

    assert restart == first
    assert plan.decode_payload({"input": first["input"]})["input"] == payload["input"]


@pytest.mark.parametrize(
    ("mutate", "classification"),
    [
        (lambda history: history[0].pop("id"), "missing_item_identity"),
        (
            lambda history: history[2].__setitem__("id", history[0]["id"]),
            "duplicate_item_identity",
        ),
        (
            lambda history: history[0].__setitem__("arguments", '{"task_name":'),
            "malformed_collaboration_arguments",
        ),
        (
            lambda history: history[0].__setitem__(
                "arguments", json.dumps({"target": 7, "message": "continue"})
            ),
            "collaboration_arguments_schema_mismatch",
        ),
        (
            lambda history: history[1].__setitem__("output", json.dumps({"unexpected": True})),
            "collaboration_result_schema_mismatch",
        ),
        (
            lambda history: history[-1].__setitem__("extra", "not-on-wire"),
            "agent_message_fields_invalid",
        ),
        (
            lambda history: history[-1]["content"].append(
                {"type": "output_text", "text": "wrong variant"}
            ),
            "agent_message_content_invalid",
        ),
    ],
)
def test_v2_lifecycle_ambiguity_and_schema_drift_fail_closed(
    mutate,
    classification: str,
) -> None:
    history = _v2_history()
    mutate(history)

    with pytest.raises(ToolCompatibilityError) as caught:
        _v2_plan().encode_payload(
            {
                "tool_choice": "auto",
                "tools": [_declaration(COLLABORATION_V2)],
                "input": history,
            }
        )

    assert caught.value.classification == classification
    assert caught.value.surface == "history"


def test_v2_void_result_empty_string_is_normalized_to_null() -> None:
    """Real Codex CLI 0.146.1 emits '' for void-result V2 handlers.

    The contract expects JSON null, so the boundary should accept the empty
    string as a reversible normalization rather than rejecting it as
    malformed_collaboration_result.
    """
    from collaboration_runtime_contract import validate_collaboration_result

    validate_collaboration_result(COLLABORATION_V2, "send_message", "")
    validate_collaboration_result(COLLABORATION_V2, "followup_task", "")


@pytest.mark.parametrize("native", [False, True], ids=["adapted", "native"])
def test_v2_void_result_empty_string_round_trips(native: bool) -> None:
    history = _v2_history()
    # followup_task result is at index 1, send_message result at index 7.
    history[1]["output"] = ""
    history[7]["output"] = ""

    plan = _v2_plan(native=native)
    payload = {
        "tool_choice": "auto",
        "tools": [_declaration(COLLABORATION_V2)],
        "input": history,
    }

    encoded = plan.encode_payload(payload)
    decoded = plan.decode_payload({"input": encoded["input"]})

    assert decoded["input"] == history


def test_v2_send_message_non_empty_result_still_fails_closed() -> None:
    history = _v2_history()
    # send_message result is at index 7.
    history[7]["output"] = json.dumps({"unexpected": True})

    with pytest.raises(ToolCompatibilityError) as caught:
        _v2_plan().encode_payload(
            {
                "tool_choice": "auto",
                "tools": [_declaration(COLLABORATION_V2)],
                "input": history,
            }
        )

    assert caught.value.classification == "collaboration_result_schema_mismatch"
    assert caught.value.surface == "history"


def test_v2_adapted_stream_restores_all_six_calls_and_preserves_boundaries() -> None:
    plan = _v2_plan()
    stream = plan.new_stream()
    decoded_types: list[str] = []
    decoded_names: list[str] = []
    wire_output: list[dict[str, object]] = []

    for index, name in enumerate(V2_ARGUMENTS):
        alias = plan.entries[0].aliases[index]
        item_id = f"stream-item-{index}"
        call_id = f"stream-call-{index}"
        arguments = json.dumps(V2_ARGUMENTS[name], separators=(",", ":"))
        added_item = {
            "type": "function_call",
            "id": item_id,
            "call_id": call_id,
            "name": alias,
            "arguments": "",
            "status": "in_progress",
        }
        done_item = {**added_item, "arguments": arguments, "status": "completed"}
        events = [
            {"type": "response.output_item.added", "output_index": index, "item": added_item},
            {
                "type": "response.function_call_arguments.delta",
                "output_index": index,
                "item_id": item_id,
                "delta": arguments,
            },
            {
                "type": "response.function_call_arguments.done",
                "output_index": index,
                "item_id": item_id,
                "arguments": arguments,
            },
            {"type": "response.output_item.done", "output_index": index, "item": done_item},
        ]
        for event in events:
            decoded = stream.decode_events_for_event(event)
            assert len(decoded) == 1
            decoded_types.append(decoded[0]["type"])
            if decoded[0]["type"] == "response.output_item.done":
                decoded_names.append(decoded[0]["item"]["name"])
                assert decoded[0]["item"]["namespace"] == "collaboration"
                assert decoded[0]["item"]["id"] == item_id
                assert decoded[0]["item"]["call_id"] == call_id
        wire_output.append(done_item)

    terminal = stream.decode_events_for_event(
        {
            "type": "response.completed",
            "response": {
                "id": "response-1",
                "model": "model-a",
                "object": "response",
                "output": wire_output,
                "status": "completed",
                "usage": {},
            },
        }
    )

    assert decoded_names == list(V2_ARGUMENTS)
    assert decoded_types == [
        event_type
        for _ in V2_ARGUMENTS
        for event_type in (
            "response.output_item.added",
            "response.function_call_arguments.delta",
            "response.function_call_arguments.done",
            "response.output_item.done",
        )
    ]
    assert terminal[0]["type"] == "response.completed"
    assert [item["name"] for item in terminal[0]["response"]["output"]] == list(V2_ARGUMENTS)


def test_v2_stream_rejects_malformed_agent_message_before_forwarding() -> None:
    stream = _v2_plan().new_stream()
    item = _v2_history()[-1]
    item["content"] = [{"type": "output_text", "text": "wrong variant"}]

    with pytest.raises(ToolCompatibilityError) as caught:
        stream.decode_events_for_event(
            {"type": "response.output_item.added", "output_index": 0, "item": item}
        )

    assert caught.value.classification == "agent_message_content_invalid"
    assert caught.value.surface == "stream"


def test_mixed_collaboration_history_fails_before_runtime_planning() -> None:
    body = _request(COLLABORATION_V2)
    body["input"] = [
        {
            "type": "function_call",
            "id": "item-v1",
            "call_id": "call-v1",
            "namespace": "multi_agent_v1",
            "name": "spawn_agent",
            "arguments": '{"agent_type":"general","message":"work"}',
        },
        _v2_history()[0],
    ]

    with patch.object(codex_proxy, "_prepare_runtime_tool_compatibility") as prepare:
        with pytest.raises(codex_proxy.UpstreamProtocolTranslationError) as caught:
            codex_proxy.compatible_request_body(
                json.dumps(body).encode(),
                _responses_upstream(native_namespace=False),
                event_context={},
                inject_codex_tools=False,
            )

    assert caught.value.cause.code == codex_proxy.COLLABORATION_BOUNDARY_ERROR_CODE
    prepare.assert_not_called()


@pytest.mark.parametrize(
    ("item_index", "field", "value", "classification"),
    [
        (0, "arguments", V2_ARGUMENTS["followup_task"], "collaboration_arguments_wire_type_invalid"),
        (1, "output", None, "collaboration_result_wire_type_invalid"),
        (
            8,
            "arguments",
            '{"task_name":"worker","task_name":"other","message":"work"}',
            "malformed_collaboration_arguments",
        ),
        (
            9,
            "output",
            '{"task_name":"/root/worker","task_name":"/root/other"}',
            "malformed_collaboration_result",
        ),
    ],
    ids=[
        "raw-arguments-object",
        "raw-output-null",
        "duplicate-argument-key",
        "duplicate-result-key",
    ],
)
def test_v2_request_history_requires_string_call_and_result_payloads(
    item_index: int,
    field: str,
    value,
    classification: str,
) -> None:
    history = _v2_history()
    history[item_index][field] = value

    with pytest.raises(ToolCompatibilityError) as caught:
        _v2_plan().encode_payload(
            {
                "tool_choice": "auto",
                "tools": [_declaration(COLLABORATION_V2)],
                "input": history,
            }
        )

    assert caught.value.classification == classification
    assert caught.value.surface == "history"


@pytest.mark.parametrize(
    ("mutate", "classification"),
    [
        (
            lambda history: history.insert(0, history.pop(1)),
            "unknown_call_identity",
        ),
        (
            lambda history: history[1].__setitem__("call_id", "orphan-call"),
            "unknown_call_identity",
        ),
        (
            lambda history: history.append(copy.deepcopy(history[1])),
            "duplicate_call_identity",
        ),
        (
            lambda history: history[1].__setitem__("output", "{}"),
            "collaboration_result_schema_mismatch",
        ),
        (
            lambda history: history[1].__setitem__("output", {}),
            "collaboration_result_wire_type_invalid",
        ),
    ],
    ids=[
        "result-before-call",
        "orphan-result",
        "duplicate-result",
        "invalid-result-schema",
        "invalid-result-wire-type",
    ],
)
def test_v2_history_results_are_owned_and_validated_fail_closed(
    mutate,
    classification: str,
) -> None:
    history = _v2_history()[:2]
    mutate(history)

    with pytest.raises(ToolCompatibilityError) as caught:
        _v2_plan().encode_payload(
            {
                "tool_choice": "auto",
                "tools": [_declaration(COLLABORATION_V2)],
                "input": history,
            }
        )

    assert caught.value.classification == classification
    assert caught.value.surface == "history"


@pytest.mark.parametrize("native", [False, True], ids=["adapted", "native"])
def test_v2_history_preserves_unrelated_function_call_and_result(native: bool) -> None:
    plan = _mixed_v2_plan(native=native)
    collaboration_call, collaboration_result = _v2_history()[:2]
    ordinary_call = {
        "type": "function_call",
        "id": "ordinary-call-item",
        "call_id": "ordinary-call",
        "name": "ordinary_lookup",
        "arguments": '{"query":"opaque"}',
    }
    ordinary_result = {
        "type": "function_call_output",
        "id": "ordinary-result-item",
        "call_id": "ordinary-call",
        "output": '{"value":"opaque"}',
    }
    history = [
        collaboration_call,
        ordinary_call,
        ordinary_result,
        collaboration_result,
    ]
    payload = {
        "tool_choice": "auto",
        "tools": [_declaration(COLLABORATION_V2), _ordinary_function_declaration()],
        "input": history,
    }
    ordinary_wire = [
        json.dumps(item, separators=(",", ":")).encode()
        for item in (ordinary_call, ordinary_result)
    ]

    encoded = plan.encode_payload(payload)

    assert [
        json.dumps(item, separators=(",", ":")).encode()
        for item in encoded["input"][1:3]
    ] == ordinary_wire
    if native:
        assert encoded == payload
    else:
        assert encoded["input"][0]["name"] == plan.entries[0].aliases[0]
        assert "namespace" not in encoded["input"][0]

    decoded = plan.decode_payload({"input": encoded["input"]})
    assert decoded["input"] == history


@pytest.mark.parametrize("native", [False, True], ids=["adapted-alias", "native-namespace"])
def test_v2_unknown_result_claiming_collaboration_identity_fails_closed(native: bool) -> None:
    plan = _mixed_v2_plan(native=native)
    ordinary_call = {
        "type": "function_call",
        "id": "ordinary-call-item",
        "call_id": "ordinary-call",
        "name": "ordinary_lookup",
        "arguments": '{"query":"opaque"}',
    }
    identity = (
        {"namespace": "collaboration", "name": "followup_task"}
        if native
        else {"name": plan.entries[0].aliases[0]}
    )
    unknown_result = {
        "type": "function_call_output",
        "id": "SECRET_RESULT_ITEM",
        "call_id": "ordinary-call",
        "output": '{"secret":"SECRET_RESULT_CONTENT"}',
        **identity,
    }

    with pytest.raises(ToolCompatibilityError) as caught:
        plan.encode_payload(
            {
                "tool_choice": "auto",
                "tools": [_declaration(COLLABORATION_V2), _ordinary_function_declaration()],
                "input": [ordinary_call, unknown_result],
            }
        )

    assert caught.value.classification == "unknown_call_identity"
    assert caught.value.surface == "history"
    assert "SECRET" not in str(caught.value)


def _agent_message_stream_event(
    event_type: str,
    item: dict[str, object],
    *,
    output_index: int = 0,
) -> dict[str, object]:
    return {"type": event_type, "output_index": output_index, "item": item}


def test_v2_agent_message_stream_preserves_complete_lifecycle() -> None:
    stream = _v2_plan().new_stream()
    item = _v2_history()[-1]

    added = stream.decode_events_for_event(
        _agent_message_stream_event("response.output_item.added", item)
    )
    done = stream.decode_events_for_event(
        _agent_message_stream_event("response.output_item.done", item)
    )
    terminal = stream.decode_events_for_event(
        {
            "type": "response.completed",
            "response": {
                "id": "response-agent-message",
                "object": "response",
                "status": "completed",
                "output": [item],
            },
        }
    )

    assert added[0]["item"] == item
    assert done[0]["item"] == item
    assert terminal[0]["response"]["output"] == [item]


def _agent_message_terminal_event(output: list[dict[str, object]] | None) -> dict[str, object]:
    response: dict[str, object] = {
        "id": "response-agent-message",
        "object": "response",
        "status": "completed",
    }
    if output is not None:
        response["output"] = output
    return {"type": "response.completed", "response": response}


def _complete_agent_message_lifecycle(
    stream,
    item: dict[str, object],
    *,
    output_index: int = 0,
) -> None:
    stream.decode_events_for_event(
        _agent_message_stream_event(
            "response.output_item.added",
            item,
            output_index=output_index,
        )
    )
    stream.decode_events_for_event(
        _agent_message_stream_event(
            "response.output_item.done",
            item,
            output_index=output_index,
        )
    )


def test_v2_agent_message_stream_rejects_terminal_only_item() -> None:
    stream = _v2_plan().new_stream()

    with pytest.raises(ToolCompatibilityError) as caught:
        stream.decode_events_for_event(
            _agent_message_terminal_event([_v2_history()[-1]])
        )

    assert caught.value.classification == "missing_stream_identity"
    assert caught.value.surface == "stream"


@pytest.mark.parametrize(
    ("terminal_output", "classification"),
    [
        (
            lambda item: [item, item],
            "duplicate_item_identity",
        ),
        (
            lambda item: [
                {
                    **item,
                    "id": "unknown-agent-message",
                }
            ],
            "missing_stream_identity",
        ),
        (
            lambda item: [],
            "incomplete_stream",
        ),
        (
            lambda item: [
                {
                    **item,
                    "content": [{"type": "input_text", "text": "changed terminal"}],
                }
            ],
            "ambiguous_native_identity",
        ),
    ],
    ids=["duplicate-terminal-id", "unknown-terminal-id", "missing-terminal-item", "changed-terminal-content"],
)
def test_v2_agent_message_stream_reconciles_terminal_ownership(
    terminal_output,
    classification: str,
) -> None:
    stream = _v2_plan().new_stream()
    item = _v2_history()[-1]
    _complete_agent_message_lifecycle(stream, item)

    with pytest.raises(ToolCompatibilityError) as caught:
        stream.decode_events_for_event(
            _agent_message_terminal_event(terminal_output(item))
        )

    assert caught.value.classification == classification
    assert caught.value.surface == "stream"


@pytest.mark.parametrize(
    ("events", "classification"),
    [
        (
            lambda item: [
                _agent_message_stream_event(
                    "response.output_item.added",
                    item,
                    output_index=0,
                ),
                _agent_message_stream_event(
                    "response.output_item.done",
                    item,
                    output_index=1,
                ),
            ],
            "ambiguous_native_identity",
        ),
        (
            lambda item: [
                _agent_message_stream_event(
                    "response.output_item.added",
                    item,
                ),
                _agent_message_stream_event(
                    "response.output_item.done",
                    item,
                    output_index=0,
                ),
                _agent_message_terminal_event(None),
            ],
            "incomplete_stream",
        ),
    ],
    ids=["output-index-drift", "terminal-output-index-required"],
)
def test_v2_agent_message_stream_rejects_index_boundary_drift(
    events,
    classification: str,
) -> None:
    stream = _v2_plan().new_stream()
    item = _v2_history()[-1]

    with pytest.raises(ToolCompatibilityError) as caught:
        for event in events(item):
            stream.decode_events_for_event(event)

    assert caught.value.classification == classification
    assert caught.value.surface == "stream"


def test_v2_agent_message_stream_rejects_terminal_order_drift() -> None:
    stream = _v2_plan().new_stream()
    first = _v2_history()[-1]
    second = {**first, "id": "agent-message-2"}
    _complete_agent_message_lifecycle(stream, first, output_index=0)
    _complete_agent_message_lifecycle(stream, second, output_index=1)

    with pytest.raises(ToolCompatibilityError) as caught:
        stream.decode_events_for_event(
            _agent_message_terminal_event([second, first])
        )

    assert caught.value.classification == "ambiguous_native_identity"
    assert caught.value.surface == "stream"


@pytest.mark.parametrize(
    ("events", "classification"),
    [
        (
            lambda item: [
                _agent_message_stream_event("response.output_item.added", item),
                _agent_message_stream_event("response.output_item.added", item),
            ],
            "duplicate_item_identity",
        ),
        (
            lambda item: [
                _agent_message_stream_event("response.output_item.done", item),
            ],
            "missing_stream_identity",
        ),
        (
            lambda item: [
                _agent_message_stream_event("response.output_item.added", item),
                {
                    "type": "response.completed",
                    "response": {"id": "response", "object": "response", "status": "completed"},
                },
            ],
            "incomplete_stream",
        ),
        (
            lambda item: [
                _agent_message_stream_event("response.output_item.added", item),
                _agent_message_stream_event(
                    "response.output_item.done",
                    {
                        **item,
                        "content": [{"type": "input_text", "text": "changed"}],
                    },
                ),
            ],
            "ambiguous_native_identity",
        ),
    ],
    ids=["duplicate-id", "done-without-added", "missing-done", "changed-content"],
)
def test_v2_agent_message_stream_rejects_broken_lifecycle(events, classification: str) -> None:
    stream = _v2_plan().new_stream()
    item = _v2_history()[-1]

    with pytest.raises(ToolCompatibilityError) as caught:
        for event in events(item):
            stream.decode_events_for_event(event)

    assert caught.value.classification == classification
    assert caught.value.surface == "stream"


@pytest.mark.parametrize("native", [False, True], ids=["adapted", "native"])
def test_v2_stream_call_lifecycle_reconciles_terminal_identity_and_order(native: bool) -> None:
    plan = _v2_plan(native=native)
    stream = plan.new_stream()
    arguments = json.dumps(V2_ARGUMENTS["followup_task"], separators=(",", ":"))
    name = "followup_task" if native else plan.entries[0].aliases[0]
    item = {
        "type": "function_call",
        "id": "v2-stream-item",
        "call_id": "v2-stream-call",
        "name": name,
        "arguments": "",
        "status": "in_progress",
    }
    if native:
        item["namespace"] = "collaboration"
    stream.decode_events_for_event(
        {"type": "response.output_item.added", "output_index": 0, "item": item}
    )
    stream.decode_events_for_event(
        {
            "type": "response.function_call_arguments.done",
            "item_id": item["id"],
            "output_index": 0,
            "arguments": arguments,
        }
    )
    done_item = {**item, "arguments": arguments, "status": "completed"}
    stream.decode_events_for_event(
        {"type": "response.output_item.done", "output_index": 0, "item": done_item}
    )

    terminal = stream.decode_events_for_event(
        {
            "type": "response.completed",
            "response": {"output": [done_item]},
        }
    )
    assert terminal[0]["response"]["output"][0]["id"] == item["id"]


@pytest.mark.parametrize("native", [False, True], ids=["adapted", "native"])
def test_v2_stream_rejects_output_index_drift_and_terminal_shape_changes(native: bool) -> None:
    def make_stream() -> tuple[object, dict[str, object], str]:
        plan = _v2_plan(native=native)
        stream = plan.new_stream()
        arguments = json.dumps(V2_ARGUMENTS["followup_task"], separators=(",", ":"))
        name = "followup_task" if native else plan.entries[0].aliases[0]
        item = {
            "type": "function_call",
            "id": "v2-stream-item",
            "call_id": "v2-stream-call",
            "name": name,
            "arguments": "",
            "status": "in_progress",
        }
        if native:
            item["namespace"] = "collaboration"
        stream.decode_events_for_event(
            {"type": "response.output_item.added", "output_index": 0, "item": item}
        )
        stream.decode_events_for_event(
            {
                "type": "response.function_call_arguments.done",
                "item_id": item["id"],
                "output_index": 0,
                "arguments": arguments,
            }
        )
        return stream, {**item, "arguments": arguments, "status": "completed"}, arguments

    stream, done_item, _arguments = make_stream()
    with pytest.raises(ToolCompatibilityError) as caught:
        stream.decode_events_for_event(
            {"type": "response.output_item.done", "output_index": 1, "item": done_item}
        )
    assert caught.value.classification == "ambiguous_native_identity"

    stream, done_item, _arguments = make_stream()
    stream.decode_events_for_event(
        {"type": "response.output_item.done", "output_index": 0, "item": done_item}
    )
    with pytest.raises(ToolCompatibilityError) as caught:
        stream.decode_events_for_event(
            {
                "type": "response.completed",
                "response": {"output": [{**done_item, "extra": "invalid"}]},
            }
        )
    assert caught.value.classification == "collaboration_call_fields_invalid"


def test_v2_stream_does_not_create_v1_worker_binding_state() -> None:
    context: dict[str, object] = {}
    body = _request(COLLABORATION_V2)
    codex_proxy.compatible_request_body(
        json.dumps(body).encode(),
        _responses_upstream(native_namespace=False),
        event_context=context,
        inject_codex_tools=False,
    )
    alias = context["_runtime_tool_compatibility_plan"].entries[0].aliases[0]
    added = {
        "type": "response.output_item.added",
        "output_index": 0,
        "item": {
            "type": "function_call",
            "id": "v2-delta-item",
            "call_id": "v2-delta-call",
            "name": alias,
            "arguments": "",
            "status": "in_progress",
        },
    }
    codex_proxy.compatible_sse_line(
        ("data: " + json.dumps(added) + "\n").encode(),
        "custom_endpoint",
        event_context=context,
    )
    event = {
        "type": "response.function_call_arguments.delta",
        "output_index": 0,
        "item_id": "v2-delta-item",
        "delta": "{",
    }
    # The argument delta is intentionally incomplete; it still must not
    # initialize the V1 worker stream scheduler before V2 is resolved.
    codex_proxy.compatible_sse_line(
        ("data: " + json.dumps(event) + "\n").encode(),
        "custom_endpoint",
        event_context=context,
    )
    assert "_worker_stream_binding_state" not in context
    assert "_subagent_state" not in context


def test_agent_message_requires_an_exact_selected_v2_contract() -> None:
    plain_plan = build_tool_compatibility_plan(
        [{"type": "function", "name": "plain", "parameters": {}}],
        selected_protocol="responses_structured",
        tool_choice="auto",
        protocol_capabilities=ProtocolCapabilities.responses_structured(),
        request_token="agent-message-without-v2",
    )
    item = _v2_history()[-1]

    with pytest.raises(ToolCompatibilityError) as caught:
        plain_plan.decode_payload({"input": [item]})
    assert caught.value.classification == "unknown_native_identity"
    assert caught.value.surface == "history"

    with pytest.raises(ToolCompatibilityError) as caught:
        plain_plan.new_stream().decode_events_for_event(
            {"type": "response.output_item.added", "output_index": 0, "item": item}
        )
    assert caught.value.classification == "unknown_native_identity"
    assert caught.value.surface == "stream"
