from __future__ import annotations

import json
from unittest.mock import patch

import pytest

import codex_proxy
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
        "call_id": "call_v2_spawn",
        "arguments": {
            "task_name": "worker-task",
            "message": "return the bounded result",
            "fork_turns": "all",
        },
    }


def _v1_spawn_call() -> dict[str, object]:
    return {
        "type": "function_call",
        "namespace": "multi_agent_v1",
        "name": "spawn_agent",
        "call_id": "call_v1_spawn",
        "arguments": {"agent_type": "general", "message": "return the bounded result"},
    }


def test_collaboration_boundary_classifies_explicit_protocols_and_rejects_mixed_inputs() -> None:
    assert classify_collaboration_payload({"input": [_v1_spawn_call()]}) == COLLABORATION_V1
    assert classify_collaboration_payload({"input": [_v2_spawn_call()]}) == COLLABORATION_V2
    assert classify_collaboration_payload(
        {
            "tools": [
                {
                    "type": "namespace",
                    "name": "collaboration",
                    "tools": [{"type": "function", "name": "spawn_agent"}],
                }
            ]
        }
    ) == COLLABORATION_V2

    with pytest.raises(CollaborationBoundaryError, match="mixed_v1_v2"):
        classify_collaboration_payload({"input": [_v1_spawn_call(), _v2_spawn_call()]})
    with pytest.raises(CollaborationBoundaryError, match="missing_namespace"):
        classify_collaboration_payload({"input": [{"type": "function_call", "name": "spawn_agent"}]})
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
    assert classify_collaboration_payload({"metadata": {"multi_agent_version": "v2"}}) == COLLABORATION_V2
    with pytest.raises(CollaborationBoundaryError, match="unknown_state"):
        classify_collaboration_payload({"metadata": {"multi_agent_version": "v3"}})
    with pytest.raises(CollaborationBoundaryError, match="missing_namespace"):
        classify_collaboration_payload({"namespace": "collaboration", "type": "function_call"})


def test_collaboration_protocols_collects_mixed_history_protocols() -> None:
    assert collaboration_protocols({"input": [_v1_spawn_call(), _v2_spawn_call()]}) == frozenset({
        COLLABORATION_V1,
        COLLABORATION_V2,
    })


def test_v2_disables_v1_guidance_and_semantic_repair() -> None:
    context = {"repair_policy": REPAIR_CODEX_SUBAGENT, "collaboration_protocol": COLLABORATION_V2}
    assert guidance_enabled(context) is False
    assert semantic_repair_enabled(context) is False


def test_v2_request_preserves_native_namespace_and_does_not_inject_v1_tools() -> None:
    context = {"repair_policy": REPAIR_CODEX_SUBAGENT, "request_id": "issue198-v2"}
    body = {
        "model": "glm-5.2",
        "input": [_v2_spawn_call()],
        "tools": [
            {
                "type": "namespace",
                "name": "collaboration",
                "tools": [{"type": "function", "name": "spawn_agent"}],
            }
        ],
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


def test_selected_v2_model_metadata_in_context_skips_v1_injection() -> None:
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

    assert context["collaboration_protocol"] == COLLABORATION_V2
    assert "multi_agent_v1__spawn_agent" not in {
        tool.get("name") for tool in payload.get("tools", []) if isinstance(tool, dict)
    }


@pytest.mark.parametrize(
    ("selection", "expected"),
    [
        ({"collaboration_protocol": "collaboration_v1"}, COLLABORATION_V1),
        ({"collaboration_protocol": "collaboration_v2"}, COLLABORATION_V2),
        ({"metadata": {"multi_agent_version": "v2"}}, COLLABORATION_V2),
        ({"features": {"multi_agent_version": "v2"}}, COLLABORATION_V2),
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


def test_boundary_rejection_diagnostic_is_bounded_and_does_not_include_payload() -> None:
    context = {"request_id": "safe-request", "repair_policy": REPAIR_CODEX_SUBAGENT}
    body = {
        "model": "glm-5.2",
        "input": [
            {
                **_v1_spawn_call(),
                "arguments": {"message": "SECRET_PROMPT", "task_path": "SECRET_TASK"},
            },
            _v2_spawn_call(),
        ],
    }

    with patch.object(codex_proxy, "write_proxy_event") as write_event:
        with pytest.raises(codex_proxy.UpstreamProtocolTranslationError):
            codex_proxy.compatible_request_body(
                json.dumps(body).encode(),
                _upstream(),
                event_context=context,
            )

    event = next(call for call in write_event.call_args_list if call.args[0] == "collaboration_boundary_rejected")
    fields = event.kwargs
    assert fields["classification"] == "mixed_v1_v2"
    assert "SECRET_PROMPT" not in repr(fields)
    assert "SECRET_TASK" not in repr(fields)
    assert "call_v1_spawn" not in repr(fields)


def test_payload_and_selected_v2_metadata_conflict_fails_closed() -> None:
    context = {
        "repair_policy": REPAIR_CODEX_SUBAGENT,
        "collaboration_protocol": COLLABORATION_V2,
        "request_id": "issue198-conflict",
    }

    with pytest.raises(codex_proxy.UpstreamProtocolTranslationError) as exc_info:
        codex_proxy.compatible_request_body(
            json.dumps({"model": "glm-5.2", "input": [_v1_spawn_call()]}).encode(),
            _upstream(),
            event_context=context,
        )

    assert exc_info.value.cause.code == codex_proxy.COLLABORATION_BOUNDARY_ERROR_CODE


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


def test_v1_request_keeps_existing_repair_path_and_boundary_is_fail_closed() -> None:
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

    with pytest.raises(codex_proxy.UpstreamProtocolTranslationError) as exc_info:
        codex_proxy.compatible_request_body(
            json.dumps({"model": "glm-5.2", "input": [_v1_spawn_call(), _v2_spawn_call()]}).encode(),
            _upstream(),
            event_context={"request_id": "issue198-mixed"},
        )
    assert exc_info.value.cause.code == codex_proxy.COLLABORATION_BOUNDARY_ERROR_CODE


def test_model_switch_v2_history_to_v1_current_surface_is_allowed() -> None:
    context = {"repair_policy": REPAIR_CODEX_SUBAGENT, "request_id": "switch-v2-v1"}
    body = {
        "model": "deepseek-v4-flash:0731",
        "input": [_v2_spawn_call(), {"type": "function_call_output", "call_id": "call_v2_spawn", "output": "done"}],
        "tools": [{
            "type": "namespace",
            "name": "multi_agent_v1",
            "tools": [{"type": "function", "name": "spawn_agent"}],
        }],
    }

    payload = json.loads(codex_proxy.compatible_request_body(
        json.dumps(body).encode(), _upstream(), event_context=context,
    ))

    assert context["collaboration_protocol"] == COLLABORATION_V1
    assert payload["model"] == body["model"]
    assert payload["input"][0]["namespace"] == "collaboration"
    assert payload["tools"][0]["name"] == "multi_agent_v1"


def test_model_switch_v1_history_to_v2_current_surface_is_allowed() -> None:
    context = {"repair_policy": REPAIR_CODEX_SUBAGENT, "request_id": "switch-v1-v2"}
    body = {
        "model": "gpt-5.6-luna",
        "input": [_v1_spawn_call(), {"type": "function_call_output", "call_id": "call_v1_spawn", "output": "done"}],
        "tools": [{
            "type": "namespace",
            "name": "collaboration",
            "tools": [{"type": "function", "name": "followup_task"}],
        }],
    }

    payload = json.loads(codex_proxy.compatible_request_body(
        json.dumps(body).encode(), _upstream(), event_context=context,
    ))

    assert context["collaboration_protocol"] == COLLABORATION_V2
    assert payload["model"] == body["model"]
    assert payload["input"][0]["namespace"] == "multi_agent_v1"
    assert payload["tools"][0]["name"] == "collaboration"


def test_current_tool_surface_overrides_previous_turn_protocol_context() -> None:
    context = {
        "repair_policy": REPAIR_CODEX_SUBAGENT,
        "collaboration_protocol": COLLABORATION_V2,
        "request_id": "switch-stale-context",
    }
    body = {
        "model": "deepseek-v4-flash:0731",
        "input": [{"type": "message", "role": "user", "content": "continue"}],
        "tools": [{
            "type": "namespace",
            "name": "multi_agent_v1",
            "tools": [{"type": "function", "name": "spawn_agent"}],
        }],
    }

    codex_proxy.compatible_request_body(
        json.dumps(body).encode(), _upstream(), event_context=context,
    )

    assert context["collaboration_protocol"] == COLLABORATION_V1


def test_current_tool_surface_containing_both_protocols_still_fails_closed() -> None:
    body = {
        "model": "deepseek-v4-flash:0731",
        "input": [{"type": "message", "role": "user", "content": "continue"}],
        "tools": [
            {"type": "namespace", "name": "multi_agent_v1", "tools": [{"type": "function", "name": "spawn_agent"}]},
            {"type": "namespace", "name": "collaboration", "tools": [{"type": "function", "name": "followup_task"}]},
        ],
    }

    with pytest.raises(codex_proxy.UpstreamProtocolTranslationError) as exc_info:
        codex_proxy.compatible_request_body(json.dumps(body).encode(), _upstream(), event_context={})

    assert exc_info.value.cause.code == codex_proxy.COLLABORATION_BOUNDARY_ERROR_CODE
