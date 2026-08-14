from __future__ import annotations

import copy
import hashlib
import json
from unittest.mock import patch

import pytest

import codex_proxy
from collaboration_runtime_contract import EXPECTED_PARAMETER_SCHEMAS
from codex_semantic_adapter import (
    COLLABORATION_V1,
    COLLABORATION_V2,
    CollaborationBoundaryError,
    classify_collaboration_payload,
    collaboration_protocols,
)
from subagent_policy import REPAIR_CODEX_SUBAGENT, guidance_enabled, semantic_repair_enabled


def _upstream() -> dict[str, object]:
    return {
        "name": "ollama_cloud",
        "upstream_model": "glm-5.2",
        "upstream_format": "responses",
        "tool_protocol": "responses_structured",
        "tool_surface_strategy": "eager",
        "tool_protocol_capabilities": {"namespace_lifecycle": True},
    }


def _v2_spawn_call() -> dict[str, object]:
    return {
        "type": "function_call",
        "namespace": "collaboration",
        "name": "spawn_agent",
        "id": "item_v2_spawn",
        "call_id": "call_v2_spawn",
        "arguments": json.dumps(
            {
                "task_name": "worker-task",
                "message": "return the bounded result",
                "fork_turns": "all",
            },
            separators=(",", ":"),
        ),
    }


def _v1_spawn_call() -> dict[str, object]:
    return {
        "type": "function_call",
        "namespace": "multi_agent_v1",
        "name": "spawn_agent",
        "id": "item_v1_spawn",
        "call_id": "call_v1_spawn",
        "arguments": {"agent_type": "general", "message": "return the bounded result"},
    }


def _collaboration_namespace(version: str) -> dict[str, object]:
    namespace = "multi_agent_v1" if version == COLLABORATION_V1 else "collaboration"
    children = []
    for name, schema in EXPECTED_PARAMETER_SCHEMAS[version].items():
        parameters = copy.deepcopy(schema)
        if not parameters["required"]:
            del parameters["required"]
        children.append(
            {
                "type": "function",
                "name": name,
                "description": "dynamic",
                "strict": False,
                "parameters": parameters,
            }
        )
    return {
        "type": "namespace",
        "name": namespace,
        "description": "dynamic",
        "tools": children,
    }


def _v2_spawn_output() -> dict[str, object]:
    return {
        "type": "function_call_output",
        "id": "item_v2_spawn_output",
        "call_id": "call_v2_spawn",
        "output": '{"task_name":"/root/worker-task"}',
    }


def test_collaboration_boundary_classifies_explicit_protocols_and_rejects_mixed_inputs() -> None:
    assert classify_collaboration_payload({"input": [_v1_spawn_call()]}) == COLLABORATION_V1
    assert classify_collaboration_payload({"input": [_v2_spawn_call()]}) == COLLABORATION_V2
    assert classify_collaboration_payload(
        {
            "tool_choice": "auto",
            "tools": [_collaboration_namespace(COLLABORATION_V2)],
        }
    ) == COLLABORATION_V2

    with pytest.raises(CollaborationBoundaryError, match="mixed_v1_v2"):
        classify_collaboration_payload({"input": [_v1_spawn_call(), _v2_spawn_call()]})
    # A top-level provider function/call may reuse a Collaboration child name;
    # only an exact Collaboration namespace is a protocol marker.
    assert classify_collaboration_payload(
        {"input": [{"type": "function_call", "name": "spawn_agent"}]}
    ) is None
    with pytest.raises(CollaborationBoundaryError, match="mixed_v1_v2"):
        classify_collaboration_payload(
            {
                "input": [
                    {
                        **_v2_spawn_call(),
                        "arguments": {"task_name": "worker-task", "fork_context": False},
                    }
                ]
            }
        )
    # Version metadata alone is not a Collaboration marker; ordinary requests
    # must reach the selected provider without a client-specific version gate.
    assert classify_collaboration_payload({"metadata": {"multi_agent_version": "v2"}}) is None
    assert classify_collaboration_payload({"metadata": {"multi_agent_version": "v3"}}) is None
    with pytest.raises(CollaborationBoundaryError, match="missing_namespace"):
        classify_collaboration_payload({"namespace": "collaboration", "type": "function_call"})


def test_v2_namespace_function_declaration_accepts_parameters_schema() -> None:
    body = {
        "tool_choice": "auto",
        "tools": [_collaboration_namespace(COLLABORATION_V2)],
    }

    assert classify_collaboration_payload(body) == COLLABORATION_V2

    context: dict[str, object] = {}
    transformed = codex_proxy.compatible_request_body(
        json.dumps(body).encode(),
        _upstream(),
        event_context=context,
    )

    assert context["collaboration_protocol"] == COLLABORATION_V2
    assert json.loads(transformed)["tools"] == body["tools"]


def test_v2_function_call_with_top_level_parameters_fails_closed() -> None:
    body = {
        "input": [
            {
                "type": "function_call",
                "namespace": "collaboration",
                "name": "followup_task",
                "call_id": "call-v2-parameters",
                "parameters": {"type": "object", "properties": {}},
            }
        ]
    }

    with pytest.raises(CollaborationBoundaryError, match="mixed_v1_v2"):
        classify_collaboration_payload(body)


@pytest.mark.parametrize(
    "history_item",
    [
        pytest.param(
            {
                "type": "function_call",
                "namespace": "collaboration",
                "name": "followup_task",
                "call_id": "call-v2-parameters",
                "parameters": {"type": "object", "properties": {}},
            },
            id="top-level-parameters",
        ),
        pytest.param(
            {
                "type": "function_call",
                "namespace": "collaboration",
                "name": "followup_task",
                "call_id": "call-v2-v1-field",
                "arguments": {"fork_context": False, "message": "continue"},
            },
            id="v1-only-field",
        ),
        pytest.param(
            {
                "type": "function_call",
                "namespace": "collaboration",
                "name": "followup_task",
                "tool_name": "spawn_agent",
                "call_id": "call-v2-conflicting-discriminator",
                "arguments": {"task_name": "continue"},
            },
            id="discriminator-conflict",
        ),
    ],
)
def test_current_v2_tools_reject_malformed_collaboration_history(
    history_item: dict[str, object],
) -> None:
    body = {
        "model": "gpt-5.6-luna",
        "input": [history_item],
        "tool_choice": "auto",
        "tools": [_collaboration_namespace(COLLABORATION_V2)],
    }

    with pytest.raises(codex_proxy.UpstreamProtocolTranslationError) as exc_info:
        codex_proxy.compatible_request_body(
            json.dumps(body).encode(),
            _upstream(),
            event_context={},
        )

    assert exc_info.value.cause.code == codex_proxy.COLLABORATION_BOUNDARY_ERROR_CODE


def test_current_v2_tools_reject_completed_mixed_collaboration_history() -> None:
    body = {
        "model": "gpt-5.6-luna",
        "input": [
            _v1_spawn_call(),
            {"type": "function_call_output", "call_id": "call_v1_spawn", "output": "done"},
            _v2_spawn_call(),
            _v2_spawn_output(),
        ],
        "tool_choice": "auto",
        "tools": [_collaboration_namespace(COLLABORATION_V2)],
    }
    context = {"request_id": "mixed-history-v2-current"}

    with patch.object(codex_proxy, "_prepare_runtime_tool_compatibility") as prepare:
        with pytest.raises(codex_proxy.UpstreamProtocolTranslationError) as caught:
            codex_proxy.compatible_request_body(
                json.dumps(body).encode(),
                _upstream(),
                event_context=context,
            )

    assert caught.value.cause.code == codex_proxy.COLLABORATION_BOUNDARY_ERROR_CODE
    prepare.assert_not_called()


def test_collaboration_protocols_collects_mixed_history_protocols() -> None:
    assert collaboration_protocols({"input": [_v1_spawn_call(), _v2_spawn_call()]}) == frozenset({
        COLLABORATION_V1,
        COLLABORATION_V2,
    })


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda declaration: declaration.update({"tools": []}), id="empty-tools"),
        pytest.param(
            lambda declaration: declaration["tools"].pop(),
            id="missing-child",
        ),
        pytest.param(
            lambda declaration: declaration["tools"][0].update({"strict": True}),
            id="child-schema-mismatch",
        ),
    ],
)
def test_history_collaboration_namespace_requires_exact_contract(mutate) -> None:
    declaration = _collaboration_namespace(COLLABORATION_V2)
    mutate(declaration)

    with pytest.raises(CollaborationBoundaryError):
        collaboration_protocols({"input": [declaration]})


def test_v2_disables_v1_guidance_and_semantic_repair() -> None:
    context = {"repair_policy": REPAIR_CODEX_SUBAGENT, "collaboration_protocol": COLLABORATION_V2}
    assert guidance_enabled(context) is False
    assert semantic_repair_enabled(context) is False


def test_v2_request_preserves_native_namespace_and_does_not_inject_v1_tools() -> None:
    context = {"repair_policy": REPAIR_CODEX_SUBAGENT, "request_id": "issue198-v2"}
    body = {
        "model": "glm-5.2",
        "input": [_v2_spawn_call()],
        "tool_choice": "auto",
        "tools": [_collaboration_namespace(COLLABORATION_V2)],
    }

    transformed = codex_proxy.compatible_request_body(
        json.dumps(body).encode(),
        _upstream(),
        event_context=context,
    )
    payload = json.loads(transformed)

    assert context["collaboration_protocol"] == COLLABORATION_V2
    assert context["subagent_spawn_allowed"] is False
    assert payload["input"] == body["input"]
    assert payload["tools"] == body["tools"]
    assert "multi_agent_v1__spawn_agent" not in {
        tool.get("name") for tool in payload["tools"] if isinstance(tool, dict)
    }


def test_v2_preserves_collaboration_calls_but_rewrites_unsupported_custom_tool_history() -> None:
    context: dict[str, object] = {"request_id": "issue198-v2-custom-history"}
    custom_call = {
        "type": "custom_tool_call",
        "status": "completed",
        "call_id": "call_exec_history",
        "name": "exec",
        "input": "echo sanitized",
    }
    custom_output = {
        "type": "custom_tool_call_output",
        "call_id": "call_exec_history",
        "output": "completed",
    }
    body = {
        "model": "glm-5.2",
        "input": [
            _v2_spawn_call(),
            _v2_spawn_output(),
            custom_call,
            custom_output,
            {"type": "message", "role": "user", "content": "continue"},
        ],
        "tool_choice": "auto",
        "tools": [_collaboration_namespace(COLLABORATION_V2)],
    }

    transformed = codex_proxy.compatible_request_body(
        json.dumps(body).encode(),
        _upstream(),
        event_context=context,
    )
    payload = json.loads(transformed)

    assert context["collaboration_protocol"] == COLLABORATION_V2
    assert payload["input"][0:2] == body["input"][0:2]
    assert all(
        item.get("type") not in {"custom_tool_call", "custom_tool_call_output"}
        for item in payload["input"]
        if isinstance(item, dict)
    )
    assert "Read-only Codex tool call transcript" in payload["input"][2]["content"]
    assert "Read-only Codex tool result transcript" in payload["input"][3]["content"]


def test_v2_does_not_preserve_custom_history_that_only_matches_a_plain_function() -> None:
    context: dict[str, object] = {"request_id": "issue198-v2-custom-name-collision"}
    body = {
        "model": "deepseek-v4-flash:0731",
        "input": [
            _v2_spawn_call(),
            {
                "type": "custom_tool_call",
                "status": "completed",
                "call_id": "call_exec_history",
                "name": "exec",
                "input": "echo sanitized",
            },
            {
                "type": "custom_tool_call_output",
                "call_id": "call_exec_history",
                "output": "completed",
            },
        ],
        "tool_choice": "auto",
        "tools": [
            _collaboration_namespace(COLLABORATION_V2),
            {"type": "function", "name": "exec"},
        ],
    }

    transformed = codex_proxy.compatible_request_body(
        json.dumps(body).encode(),
        _upstream(),
        event_context=context,
    )
    payload = json.loads(transformed)

    assert all(
        item.get("type") not in {"custom_tool_call", "custom_tool_call_output"}
        for item in payload["input"]
        if isinstance(item, dict)
    )


def test_v2_does_not_preserve_undeclared_custom_history_when_capability_is_explicit() -> None:
    context: dict[str, object] = {"request_id": "issue198-v2-custom-capability-collision"}
    upstream = {
        **_upstream(),
        "tool_protocol_capabilities": {
            "namespace_lifecycle": True,
            "custom_lifecycle": True,
        },
    }
    body = {
        "model": "deepseek-v4-flash:0731",
        "input": [
            _v2_spawn_call(),
            {
                "type": "custom_tool_call",
                "status": "completed",
                "call_id": "call_exec_history",
                "name": "exec",
                "input": "echo sanitized",
            },
            {
                "type": "custom_tool_call_output",
                "call_id": "call_exec_history",
                "output": "completed",
            },
        ],
        "tool_choice": "auto",
        "tools": [
            _collaboration_namespace(COLLABORATION_V2),
            {"type": "function", "name": "exec"},
        ],
    }

    transformed = codex_proxy.compatible_request_body(
        json.dumps(body).encode(),
        upstream,
        event_context=context,
    )
    payload = json.loads(transformed)

    assert all(
        item.get("type") not in {"custom_tool_call", "custom_tool_call_output"}
        for item in payload["input"]
        if isinstance(item, dict)
    )
    assert "Read-only Codex tool call transcript" in payload["input"][1]["content"]
    assert "Read-only Codex tool result transcript" in payload["input"][2]["content"]


def test_v2_does_not_preserve_unknown_custom_alias_history() -> None:
    context: dict[str, object] = {"request_id": "issue198-v2-unknown-custom-alias"}
    body = {
        "model": "deepseek-v4-flash:0731",
        "input": [
            _v2_spawn_call(),
            {
                "type": "custom_tool_call",
                "status": "completed",
                "call_id": "call_unknown_custom_alias",
                "name": "__codexhub_custom_fake",
                "input": "echo sanitized",
            },
            {
                "type": "custom_tool_call_output",
                "call_id": "call_unknown_custom_alias",
                "output": "completed",
            },
        ],
        "tool_choice": "auto",
        "tools": [
            _collaboration_namespace(COLLABORATION_V2),
        ],
    }

    transformed = codex_proxy.compatible_request_body(
        json.dumps(body).encode(),
        _upstream(),
        event_context=context,
    )
    payload = json.loads(transformed)

    assert all(
        item.get("type") not in {"custom_tool_call", "custom_tool_call_output"}
        for item in payload["input"]
        if isinstance(item, dict)
    )
    assert "Read-only Codex tool call transcript" in payload["input"][1]["content"]
    assert "Read-only Codex tool result transcript" in payload["input"][2]["content"]


def test_v2_does_not_treat_namespace_alias_as_custom_history_owner() -> None:
    context: dict[str, object] = {"request_id": "issue198-v2-namespace-alias-collision"}
    token = "namespace-alias-test"
    token_digest = hashlib.sha256(token.encode()).hexdigest()[:10]
    namespace_alias = f"__codexhub_ns_{token_digest}_2"
    body = {
        "model": "deepseek-v4-flash:0731",
        "input": [
            _v2_spawn_call(),
            {
                "type": "custom_tool_call",
                "status": "completed",
                "call_id": "call_namespace_alias",
                "name": namespace_alias,
                "input": "echo sanitized",
            },
            {
                "type": "custom_tool_call_output",
                "call_id": "call_namespace_alias",
                "output": "completed",
            },
        ],
        "tool_choice": "auto",
        "tools": [
            _collaboration_namespace(COLLABORATION_V2),
            {
                "type": "namespace",
                "name": "other",
                "tools": [{"type": "function", "name": "exec"}],
            },
        ],
    }
    upstream = {
        **_upstream(),
        "tool_protocol_capabilities": {
            "function_lifecycle": True,
            "namespace_lifecycle": False,
            "accepts_namespace_adapter": True,
        },
    }

    with patch("codex_proxy.uuid.uuid4") as uuid4:
        uuid4.return_value.hex = token
        transformed = codex_proxy.compatible_request_body(
            json.dumps(body).encode(),
            upstream,
            event_context=context,
        )
    payload = json.loads(transformed)

    assert all(
        item.get("type") not in {"custom_tool_call", "custom_tool_call_output"}
        for item in payload["input"]
        if isinstance(item, dict)
    )
    assert "Read-only Codex tool call transcript" in payload["input"][1]["content"]
    assert "Read-only Codex tool result transcript" in payload["input"][2]["content"]


def test_v2_sanitizes_custom_history_when_tools_are_null() -> None:
    context: dict[str, object] = {"request_id": "issue198-v2-null-tools"}
    body = {
        "model": "deepseek-v4-flash:0731",
        "input": [
            _v2_spawn_call(),
            {
                "type": "custom_tool_call",
                "status": "completed",
                "call_id": "call_null_tools",
                "name": "exec",
                "input": "echo sanitized",
            },
            {
                "type": "custom_tool_call_output",
                "call_id": "call_null_tools",
                "output": "completed",
            },
        ],
        "tools": None,
    }

    transformed = codex_proxy.compatible_request_body(
        json.dumps(body).encode(),
        _upstream(),
        event_context=context,
    )
    payload = json.loads(transformed)

    assert all(
        item.get("type") not in {"custom_tool_call", "custom_tool_call_output"}
        for item in payload["input"]
        if isinstance(item, dict)
    )
    assert "Read-only Codex tool call transcript" in payload["input"][1]["content"]
    assert "Read-only Codex tool result transcript" in payload["input"][2]["content"]


def test_v2_preserves_custom_history_when_upstream_explicitly_supports_custom_lifecycle() -> None:
    context: dict[str, object] = {"request_id": "issue198-v2-native-custom"}
    upstream = {
        **_upstream(),
        "tool_protocol_capabilities": {
            "namespace_lifecycle": True,
            "custom_lifecycle": True,
        },
    }
    body = {
        "model": "glm-5.2",
        "input": [
            _v2_spawn_call(),
            {
                "type": "custom_tool_call",
                "status": "completed",
                "call_id": "call_exec_history",
                "name": "exec",
                "input": "echo native",
            },
            {
                "type": "custom_tool_call_output",
                "call_id": "call_exec_history",
                "output": "completed",
            },
        ],
        "tool_choice": "auto",
        "tools": [
            _collaboration_namespace(COLLABORATION_V2),
            {
                "type": "custom",
                "name": "exec",
                "format": {"type": "text"},
            },
        ],
    }

    transformed = codex_proxy.compatible_request_body(
        json.dumps(body).encode(),
        upstream,
        event_context=context,
    )
    payload = json.loads(transformed)

    assert payload["input"][1:] == body["input"][1:]


def test_v2_preserves_declared_custom_history_without_event_context() -> None:
    body = {
        "model": "glm-5.2",
        "input": [
            _v2_spawn_call(),
            {
                "type": "custom_tool_call",
                "status": "completed",
                "call_id": "call_exec_history",
                "name": "exec",
                "input": "echo native",
            },
            {
                "type": "custom_tool_call_output",
                "call_id": "call_exec_history",
                "output": "completed",
            },
        ],
        "tool_choice": "auto",
        "tools": [
            _collaboration_namespace(COLLABORATION_V2),
            {
                "type": "custom",
                "name": "exec",
                "format": {"type": "text"},
            },
        ],
    }
    upstream = {
        **_upstream(),
        "tool_protocol_capabilities": {
            "namespace_lifecycle": True,
            "custom_lifecycle": True,
        },
    }

    transformed = codex_proxy.compatible_request_body(
        json.dumps(body).encode(),
        upstream,
    )
    payload = json.loads(transformed)

    assert payload["input"] == body["input"]


def test_v2_response_does_not_apply_v1_alias_repair() -> None:
    context = {"repair_policy": REPAIR_CODEX_SUBAGENT, "collaboration_protocol": COLLABORATION_V2}
    body = {
        "id": "resp_v2",
        "output": [
            {
                "type": "function_call",
                "namespace": "collaboration",
                "name": "spawn_agent",
                "call_id": "call_v2_alias",
                "arguments": "{\"task_name\":\"worker-task\"}",
            }
        ],
    }

    transformed = codex_proxy.compatible_response_body(
        json.dumps(body).encode(),
        "ollama_cloud",
        event_context=context,
    )

    assert json.loads(transformed) == body


def test_v2_response_with_a_v1_alias_fails_closed() -> None:
    body = {
        "id": "resp_v2_mixed",
        "output": [
            {
                "type": "function_call",
                "namespace": "collaboration",
                "name": "multi_agent_v1__spawn_agent",
                "call_id": "call_v2_mixed",
                "arguments": "{\"task_name\":\"worker-task\"}",
            }
        ],
    }

    with pytest.raises(codex_proxy.UpstreamProtocolTranslationError) as exc_info:
        codex_proxy.compatible_response_body(
            json.dumps(body).encode(),
            "ollama_cloud",
            event_context={"collaboration_protocol": COLLABORATION_V2},
        )

    assert exc_info.value.cause.code == codex_proxy.COLLABORATION_BOUNDARY_ERROR_CODE


def test_direct_v2_response_and_sse_resolve_boundary_before_repairs() -> None:
    body = {
        "id": "resp_v2_direct",
        "output": [_v2_spawn_call()],
    }
    transformed = codex_proxy.compatible_response_body(
        json.dumps(body).encode(),
        "ollama_cloud",
    )
    assert json.loads(transformed) == body

    line = b"data: " + json.dumps(_v2_spawn_call(), separators=(",", ":")).encode() + b"\n\n"
    assert codex_proxy.compatible_sse_line(line, "ollama_cloud") == line


def test_selected_v2_metadata_skips_v1_injection_without_a_call_marker() -> None:
    context = {
        "repair_policy": REPAIR_CODEX_SUBAGENT,
        "collaboration_protocol": COLLABORATION_V2,
        "request_id": "issue198-selected-v2",
    }
    body = {
        "model": "glm-5.2",
        "input": [{"type": "message", "role": "user", "content": "continue"}],
    }

    transformed = codex_proxy.compatible_request_body(
        json.dumps(body).encode(),
        _upstream(),
        event_context=context,
    )
    payload = json.loads(transformed)

    assert context["collaboration_protocol"] == COLLABORATION_V2
    assert payload["input"] == body["input"]
    assert "multi_agent_v1__spawn_agent" not in {
        tool.get("name") for tool in payload.get("tools", []) if isinstance(tool, dict)
    }


def test_context_multi_agent_version_does_not_select_a_runtime_protocol() -> None:
    context = {
        "repair_policy": REPAIR_CODEX_SUBAGENT,
        "multi_agent_version": "v2",
        "request_id": "issue198-context-v2",
    }
    transformed = codex_proxy.compatible_request_body(
        json.dumps({"model": "glm-5.2", "input": "continue"}).encode(),
        _upstream(),
        event_context=context,
    )
    payload = json.loads(transformed)

    assert "collaboration_protocol" not in context
    assert payload["input"]


def test_request_multi_agent_version_is_rejected_when_collaboration_namespace_is_present() -> None:
    context = {
        "repair_policy": REPAIR_CODEX_SUBAGENT,
        "request_id": "issue198-request-v2",
    }
    body = {
        "model": "glm-5.2",
        "input": [{"type": "message", "role": "user", "content": "continue"}],
        "tool_choice": "auto",
        "tools": [_collaboration_namespace(COLLABORATION_V2)],
        "metadata": {"multi_agent_version": "v2"},
    }

    with pytest.raises(codex_proxy.UpstreamProtocolTranslationError) as caught:
        codex_proxy.compatible_request_body(
            json.dumps(body).encode(),
            _upstream(),
            event_context=context,
        )
    assert caught.value.cause.code == codex_proxy.COLLABORATION_BOUNDARY_ERROR_CODE


@pytest.mark.parametrize(
    ("selection", "expected"),
    [
        ({"collaboration_protocol": "collaboration_v1"}, COLLABORATION_V1),
        ({"collaboration_protocol": "collaboration_v2"}, COLLABORATION_V2),
    ],
)
def test_context_feature_selection_is_table_driven(
    selection: dict[str, object],
    expected: str,
) -> None:
    context = {"repair_policy": REPAIR_CODEX_SUBAGENT, **selection}
    body = {"model": "glm-5.2", "input": "continue"}

    transformed = codex_proxy.compatible_request_body(
        json.dumps(body).encode(),
        _upstream(),
        event_context=context,
    )

    assert context["collaboration_protocol"] == expected
    if expected == COLLABORATION_V2:
        payload = json.loads(transformed)
        assert "multi_agent_v1__spawn_agent" not in {
            tool.get("name") for tool in payload.get("tools", []) if isinstance(tool, dict)
        }


def test_unknown_context_state_fails_closed() -> None:
    with pytest.raises(codex_proxy.UpstreamProtocolTranslationError) as exc_info:
        codex_proxy.compatible_request_body(
            json.dumps({"model": "glm-5.2", "input": "continue"}).encode(),
            _upstream(),
            event_context={"collaboration_protocol": "unclassified"},
        )

    assert exc_info.value.cause.code == codex_proxy.COLLABORATION_BOUNDARY_ERROR_CODE


def test_mixed_history_rejection_is_bounded_and_does_not_include_payload() -> None:
    context = {"request_id": "safe-request", "repair_policy": REPAIR_CODEX_SUBAGENT}
    body = {
        "model": "glm-5.2",
        "input": [
            {
                **_v1_spawn_call(),
                "arguments": {"message": "SECRET_PROMPT SECRET_TASK"},
            },
            _v2_spawn_call(),
        ],
    }

    with patch.object(codex_proxy, "write_proxy_event") as write_event:
        with pytest.raises(codex_proxy.UpstreamProtocolTranslationError):
            codex_proxy._resolve_collaboration_boundary(body, context)

    event = next(
        call
        for call in write_event.call_args_list
        if call.args[0] == "collaboration_boundary_rejected"
    )
    fields = event.kwargs
    assert fields == {"surface": "request", "outcome": "rejected", "count": 1}
    assert "SECRET_PROMPT" not in repr(fields)
    assert "SECRET_TASK" not in repr(fields)
    assert "call_v1_spawn" not in repr(fields)


def test_selected_v2_context_conflicting_with_v1_history_fails_closed() -> None:
    context = {
        "repair_policy": REPAIR_CODEX_SUBAGENT,
        "collaboration_protocol": COLLABORATION_V2,
        "request_id": "issue198-conflict",
    }

    with pytest.raises(codex_proxy.UpstreamProtocolTranslationError) as caught:
        codex_proxy.compatible_request_body(
            json.dumps({"model": "glm-5.2", "input": [_v1_spawn_call()]}).encode(),
            _upstream(),
            event_context=context,
        )

    assert caught.value.cause.code == codex_proxy.COLLABORATION_BOUNDARY_ERROR_CODE


def test_conflicting_current_target_metadata_without_namespace_is_ordinary_request() -> None:
    body = json.dumps(
        {
            "model": "glm-5.2",
            "input": "continue",
            "metadata": {"multi_agent_version": "v1"},
            "features": {"multi_agent_version": "v2"},
        }
    ).encode()

    transformed = codex_proxy.compatible_request_body(
        body,
        _upstream(),
        event_context={},
    )

    assert json.loads(transformed)["input"] == "continue"


def test_official_passthrough_does_not_interpret_collaboration_metadata() -> None:
    body_payload = {
        "model": "gpt-5.5",
        "input": [{"type": "function_call", "namespace": "unknown", "name": "spawn_agent"}],
        "stream": True,
    }
    body = json.dumps(body_payload).encode()

    transformed = codex_proxy.compatible_request_body(
        body,
        {"name": "official", "auth": "codex_auth"},
        event_context={"request_id": "issue198-official"},
        behavior_profile=codex_proxy.BEHAVIOR_OFFICIAL_CODEX_APP_HTTP_PASSTHROUGH,
    )

    payload = json.loads(transformed)
    assert payload["input"] == body_payload["input"]


def test_v1_request_keeps_existing_repair_path_and_rejects_mixed_history() -> None:
    context = {"repair_policy": REPAIR_CODEX_SUBAGENT, "request_id": "issue198-v1"}
    transformed = codex_proxy.compatible_request_body(
        json.dumps({"model": "glm-5.2", "input": [_v1_spawn_call()]}).encode(),
        _upstream(),
        event_context=context,
    )
    payload = json.loads(transformed)
    assert context["collaboration_protocol"] == COLLABORATION_V1
    assert any(
        isinstance(item, dict)
        and item.get("name") in {"multi_agent_v1__spawn_agent", "spawn_agent"}
        for item in payload.get("tools", [])
    )

    mixed_context = {"request_id": "issue198-mixed"}
    with pytest.raises(codex_proxy.UpstreamProtocolTranslationError) as caught:
        codex_proxy.compatible_request_body(
            json.dumps({"model": "glm-5.2", "input": [_v1_spawn_call(), _v2_spawn_call()]}).encode(),
            _upstream(),
            event_context=mixed_context,
        )
    assert caught.value.cause.code == codex_proxy.COLLABORATION_BOUNDARY_ERROR_CODE


def test_model_switch_v2_history_to_v1_current_surface_fails_closed() -> None:
    context = {"repair_policy": REPAIR_CODEX_SUBAGENT, "request_id": "switch-v2-v1"}
    body = {
        "model": "deepseek-v4-flash:0731",
        "input": [_v2_spawn_call(), _v2_spawn_output()],
        "tool_choice": "auto",
        "tools": [_collaboration_namespace(COLLABORATION_V1)],
    }
    upstream = _upstream()
    upstream["upstream_model"] = body["model"]

    with pytest.raises(codex_proxy.UpstreamProtocolTranslationError) as caught:
        codex_proxy.compatible_request_body(
            json.dumps(body).encode(), upstream, event_context=context,
        )

    assert caught.value.cause.code == codex_proxy.COLLABORATION_BOUNDARY_ERROR_CODE


def test_model_switch_v1_history_to_v2_current_surface_fails_closed() -> None:
    context = {"repair_policy": REPAIR_CODEX_SUBAGENT, "request_id": "switch-v1-v2"}
    body = {
        "model": "gpt-5.6-luna",
        "input": [_v1_spawn_call(), {"type": "function_call_output", "call_id": "call_v1_spawn", "output": "done"}],
        "tool_choice": "auto",
        "tools": [_collaboration_namespace(COLLABORATION_V2)],
    }
    upstream = _upstream()
    upstream["upstream_model"] = body["model"]

    with pytest.raises(codex_proxy.UpstreamProtocolTranslationError) as caught:
        codex_proxy.compatible_request_body(
            json.dumps(body).encode(), upstream, event_context=context,
        )

    assert caught.value.cause.code == codex_proxy.COLLABORATION_BOUNDARY_ERROR_CODE


def test_current_tool_surface_conflicting_with_context_fails_closed() -> None:
    context = {
        "repair_policy": REPAIR_CODEX_SUBAGENT,
        "collaboration_protocol": COLLABORATION_V2,
        "request_id": "switch-stale-context",
    }
    body = {
        "model": "deepseek-v4-flash:0731",
        "input": [{"type": "message", "role": "user", "content": "continue"}],
        "tool_choice": "auto",
        "tools": [_collaboration_namespace(COLLABORATION_V1)],
    }

    with pytest.raises(codex_proxy.UpstreamProtocolTranslationError) as caught:
        codex_proxy.compatible_request_body(
            json.dumps(body).encode(), _upstream(), event_context=context,
        )
    assert caught.value.cause.code == codex_proxy.COLLABORATION_BOUNDARY_ERROR_CODE


def test_current_tool_surface_containing_both_protocols_still_fails_closed() -> None:
    body = {
        "model": "deepseek-v4-flash:0731",
        "input": [{"type": "message", "role": "user", "content": "continue"}],
        "tool_choice": "auto",
        "tools": [
            _collaboration_namespace(COLLABORATION_V1),
            _collaboration_namespace(COLLABORATION_V2),
        ],
    }

    with pytest.raises(codex_proxy.UpstreamProtocolTranslationError) as exc_info:
        codex_proxy.compatible_request_body(json.dumps(body).encode(), _upstream(), event_context={})

    assert exc_info.value.cause.code == codex_proxy.COLLABORATION_BOUNDARY_ERROR_CODE


def test_current_target_boundary_rejects_mixed_history() -> None:
    current_v1_tools_body = {
        "input": [_v1_spawn_call(), _v2_spawn_call()],
        "tool_choice": "auto",
        "tools": [_collaboration_namespace(COLLABORATION_V1)],
    }
    for payload, context in (
        (current_v1_tools_body, {}),
        (
            {"input": [_v1_spawn_call(), _v2_spawn_call()]},
            {"multi_agent_version": "v2"},
        ),
        ({"input": [_v1_spawn_call(), _v2_spawn_call()]}, {}),
    ):
        with pytest.raises(codex_proxy.UpstreamProtocolTranslationError):
            codex_proxy._resolve_collaboration_boundary(payload, context)

    with pytest.raises(codex_proxy.UpstreamProtocolTranslationError):
        codex_proxy.compatible_request_body(
            json.dumps(
                {
                    "input": [{"type": "message", "role": "user", "content": "continue"}],
                    "tool_choice": "auto",
                    "tools": [
                        _collaboration_namespace(COLLABORATION_V1),
                        _collaboration_namespace(COLLABORATION_V2),
                    ],
                }
            ).encode(),
            _upstream(),
            event_context={},
        )
