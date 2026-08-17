"""Optional live evidence checks for the third-party V2 subagent gate.

The private Windows runner keeps request bodies and credentials outside the
repository.  When ``CODEXHUB_BETA42_EXTERNAL_V2_PRIVATE_ROOT`` points at its
private output, these tests inspect only bounded capture metadata.  They are
skipped in ordinary unit-test runs and never print request bodies, IDs, or
provider credentials.  The live qualification uses GLM5.2 as both the
coordinator and the configured default subagent; it is not a provider-native
Collaboration capability claim.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest


EXPECTED_LIFECYCLE = [
    "spawn_agent",
    "list_agents",
    "send_message",
    "followup_task",
    "wait_agent",
    "interrupt_agent",
    "wait_agent",
    "list_agents",
]
EXPECTED_READBACK = [*EXPECTED_LIFECYCLE, "list_agents"]


def _private_root() -> Path:
    value = os.environ.get("CODEXHUB_BETA42_EXTERNAL_V2_PRIVATE_ROOT")
    if not value:
        pytest.skip("live Beta4.2 external V2 evidence is not configured")
    root = Path(value)
    if not root.is_dir():
        pytest.fail("configured live Beta4.2 external V2 evidence root is unavailable")
    return root


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        pytest.fail(f"live Beta4.2 evidence is unreadable: {type(exc).__name__}")
    if not isinstance(value, dict):
        pytest.fail("live Beta4.2 evidence root is not an object")
    return value


def _request_projection(path: Path) -> dict[str, Any]:
    """Drop the private request body before any assertion can expose it."""

    value = _read_json(path)
    lifecycle_calls = value.get("history_lifecycle_calls")
    safe_lifecycle_calls: list[dict[str, Any]] = []
    if isinstance(lifecycle_calls, list):
        for call in lifecycle_calls:
            if not isinstance(call, dict):
                continue
            safe_call: dict[str, Any] = {"tool": call.get("tool")}
            for key in (
                "agent_states_present",
                "agent_states_terminal",
                "error_terminal_evidence",
                "error_terminal_replay_evidence",
                "completed",
                "malformed",
            ):
                if key in call:
                    safe_call[key] = call.get(key)
            for key in (
                "agent_state_targets",
                "agent_state_terminal_targets",
                "result_targets",
                "targets",
            ):
                if isinstance(call.get(key), list):
                    safe_call[f"{key}_count"] = len(call[key])
            safe_lifecycle_calls.append(safe_call)
    return {
        key: value.get(key)
        for key in (
            "response_http_status",
            "response_sse_parse_status",
            "response_sse_event_count",
            "response_sse_terminal_type_count",
            "response_sse_error_type_count",
            "request_stream_enabled",
            "collaboration_protocol",
            "history_protocol",
            "endpoint_match",
            "model_match",
            "namespace_count",
            "namespace_child_count",
            "history_lifecycle_valid",
            "history_error_terminal_observed",
            "history_error_terminal_replayed",
        )
    } | {"history_lifecycle_calls": safe_lifecycle_calls}


def _lifecycle_tools(value: dict[str, Any]) -> list[str]:
    calls = value.get("history_lifecycle_calls")
    if not isinstance(calls, list):
        return []
    return [
        call.get("tool")
        for call in calls
        if isinstance(call, dict) and isinstance(call.get("tool"), str)
    ]


def _find_request_with_lifecycle(directory: Path, expected: list[str]) -> dict[str, Any]:
    for path in sorted(directory.glob("request-*.json")):
        projection = _request_projection(path)
        if _lifecycle_tools(projection) == expected:
            return projection
    pytest.fail("required live external V2 lifecycle was not captured")


def _adapter_summary(root: Path, phase: str) -> dict[str, Any]:
    return _read_json(root / f"external-v2-{phase}.gateway-summary.private.json")


def _assert_glm52_subagent_configuration(root: Path) -> None:
    """Prove the child model came from the real CLI config, not a prompt claim."""

    case_root = Path(
        os.environ.get(
            "CODEXHUB_BETA42_EXTERNAL_V2_CASE_ROOT",
            str(root.parent / "cases" / "external-v2-lifecycle"),
        )
    )
    config_path = case_root / ".codex" / "config.toml"
    try:
        lines = config_path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        pytest.fail(f"live GLM5.2 subagent configuration is unreadable: {type(exc).__name__}")
    assert 'default_subagent_model = "glm-5.2"' in lines
    assert 'model_provider = "codexhub"' in lines
    assert 'multi_agent_v2 = true' in lines


def _assert_real_external_route(summary: dict[str, Any]) -> None:
    """Reject synthetic/legacy evidence while keeping route metadata bounded."""

    assert summary.get("protocols") == ["v2"]
    routes = summary.get("route_records")
    assert isinstance(routes, list) and routes
    assert all(
        isinstance(route, dict)
        and route.get("provider_id") == "ollama_cloud"
        and route.get("model_canonical") == "ollama-cloud/glm-5.2"
        and route.get("route_upstream_model") == "glm-5.2"
        and route.get("upstream") == "ollama_cloud"
        and route.get("upstream_format") == "responses"
        and route.get("status") == 200
        for route in routes
    )


def test_real_external_v2_subagent_gateway_adapter_capture() -> None:
    """The real third-party run must prove the adapter, not a catalog claim."""

    root = _private_root()
    _assert_glm52_subagent_configuration(root)
    setup_request = _find_request_with_lifecycle(
        root / "captures" / "external-v2-setup", EXPECTED_LIFECYCLE
    )
    assert setup_request["response_http_status"] == 200
    assert setup_request["response_sse_parse_status"] == "complete"
    assert setup_request["response_sse_event_count"] > 0
    assert setup_request["response_sse_terminal_type_count"] == 1
    assert setup_request["response_sse_error_type_count"] == 0
    assert setup_request["request_stream_enabled"] is True
    assert setup_request["collaboration_protocol"] == "v2"
    assert setup_request["history_protocol"] == "v2"
    assert setup_request["namespace_count"] == 1
    assert setup_request["namespace_child_count"] == 6
    assert setup_request["history_lifecycle_valid"] is True

    summary = _adapter_summary(root, "setup")
    _assert_real_external_route(summary)
    records = summary.get("adapter_request_records")
    assert isinstance(records, list) and records
    assert all(
        record.get("upstream_namespace_count") == 0
        and record.get("upstream_namespace_child_count") == 0
        and record.get("upstream_function_tool_count") == 14
        and record.get("adapted_alias_count") == 6
        and record.get("adapted_alias_unique_count") == 6
        for record in records
        if isinstance(record, dict)
    )
    assert all(isinstance(record, dict) for record in records)
    assert records[-1]["adapted_history_call_count"] == 8
    assert records[-1]["adapted_history_output_count"] == 8
    assert len({record["adapted_alias_hash"] for record in records}) == 1

    responses = summary.get("adapter_response_records")
    assert isinstance(responses, list) and responses
    assert all(
        isinstance(record, dict) and record.get("reverse_mapping_count", 0) >= 1
        for record in responses
    )


def test_real_external_v2_subagent_restart_and_error_replay_boundary() -> None:
    """The same GLM5.2 V2 Session survives readback and error replay."""

    root = _private_root()
    _assert_glm52_subagent_configuration(root)
    setup = _find_request_with_lifecycle(
        root / "captures" / "external-v2-setup", EXPECTED_LIFECYCLE
    )
    setup_calls = setup["history_lifecycle_calls"]
    assert isinstance(setup_calls, list)
    assert setup_calls[-1]["agent_state_terminal_targets_count"] >= 1
    assert setup["history_error_terminal_replayed"] is True

    readback = _find_request_with_lifecycle(
        root / "captures" / "external-v2-readback",
        EXPECTED_READBACK,
    )
    calls = readback["history_lifecycle_calls"]
    assert isinstance(calls, list) and calls
    final_call = calls[-1]
    assert isinstance(final_call, dict)
    if final_call.get("tool") != "list_agents":
        pytest.fail("restart readback did not finish with list_agents")
    # The target is terminal in the setup history, but is absent from the
    # post-restart state.  The root remains live, so the aggregate
    # ``agent_states_terminal`` flag is intentionally not the assertion here.
    assert final_call.get("agent_states_present") is True
    assert final_call.get("agent_state_terminal_targets_count") == 0
    assert any(
        isinstance(call, dict) and call.get("error_terminal_replay_evidence") is True
        for call in calls[:-1]
    )

    readback_summary = _adapter_summary(root, "readback")
    _assert_real_external_route(readback_summary)
    assert readback_summary.get("request_error_count") == 0
    assert readback_summary.get("gateway_error_count") == 0

    error_replay = _find_request_with_lifecycle(
        root / "captures" / "external-v2-error-replay", ["spawn_agent", "interrupt_agent"]
    )
    assert error_replay["response_http_status"] == 200
    assert error_replay["response_sse_parse_status"] == "complete"
    assert error_replay["response_sse_error_type_count"] == 0
    assert error_replay["request_stream_enabled"] is True
    assert error_replay["collaboration_protocol"] == "v2"
    assert error_replay["history_protocol"] == "v2"
    assert error_replay["history_lifecycle_valid"] is True
    assert error_replay["history_error_terminal_observed"] is True
