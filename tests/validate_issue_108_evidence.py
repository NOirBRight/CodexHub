"""Fail-closed, sanitized evidence validators for Issue #108 replays."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import sys
from typing import Any


TOOL_SURFACE_SCHEMA = "codexhub.issue108.tool-surface-replay.v1"
QUALIFICATION_SCHEMA = "codexhub.issue108.qualification-evidence.v2"
FAILURE_SCHEMA = "codexhub.issue108.qualification-failure.v1"

_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_SENSITIVE_VALUE = re.compile(
    r"(?i)(?:api[_-]?key|authorization|bearer|token|password|secret|sk-[a-z0-9]|ghp_)"
)
_LOCAL_PATH = re.compile(r"(?i)(?:[a-z]:[\\/]|\\\\users\\|/users/|/home/)")
_RAW_CONTENT = re.compile(r"(?i)(?:automated qualification|\*\*\* begin patch|\*\*\* update file:)")
DIRECT_TOOL_SURFACE_BUDGET = 64
_ACCEPTED_DEFERRED_PAYLOAD_SHA256 = "sha256:5c697ad0f536d5419e557c5fe4b3208016ec69c2cbe006dba4192210cf1e0294"
_REQUIRED_CORE_TOOL_NAMES = frozenset({"shell_command", "apply_patch"})
_EXPECTED_DEFERRED_CANONICAL_TOOL_SHAPE = [
    {
        "type": "function",
        "name": "shell_command",
        "keys": ["description", "name", "parameters", "strict", "type"],
    },
    {
        "type": "function",
        "name": "update_plan",
        "keys": ["description", "name", "parameters", "strict", "type"],
    },
    {
        "type": "function",
        "name": "request_user_input",
        "keys": ["description", "name", "parameters", "strict", "type"],
    },
    {
        "type": "custom",
        "name": "apply_patch",
        "keys": ["description", "format", "name", "type"],
    },
    {
        "type": "function",
        "name": "view_image",
        "keys": ["description", "name", "parameters", "strict", "type"],
    },
    {
        "type": "function",
        "name": "tool_search",
        "keys": ["description", "name", "parameters", "type"],
    },
    {
        "type": "function",
        "name": "multi_agent_v1__spawn_agent",
        "keys": ["description", "name", "parameters", "type"],
    },
]


class EvidenceValidationError(ValueError):
    """A deliberately non-sensitive validation failure."""


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise EvidenceValidationError(code)


def _require_mapping(value: Any, keys: set[str], code: str) -> dict[str, Any]:
    _require(isinstance(value, dict) and set(value) == keys, code)
    return value


def _require_list(value: Any, code: str) -> list[Any]:
    _require(isinstance(value, list), code)
    return value


def _require_int(value: Any, code: str, *, minimum: int = 0) -> int:
    _require(isinstance(value, int) and not isinstance(value, bool) and value >= minimum, code)
    return value


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _iter_strings(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _iter_strings(key)
            yield from _iter_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_strings(item)


def _assert_sanitized(value: Any) -> None:
    for text in _iter_strings(value):
        lowered = text.lower()
        _require("proxy" not in lowered, "evidence_terminology_invalid")
        _require(_SENSITIVE_VALUE.search(text) is None, "evidence_sensitive_value")
        _require(_LOCAL_PATH.search(text) is None, "evidence_local_path")
        _require(_RAW_CONTENT.search(text) is None, "evidence_raw_content")


def _load_fixture(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise EvidenceValidationError("fixture_unavailable_or_invalid") from error
    _require(isinstance(payload, dict), "fixture_root_invalid")
    _assert_sanitized(payload)
    return payload


def _route_identity(value: Any) -> None:
    identity = _require_mapping(value, {"model", "upstream", "route_mode"}, "route_identity_invalid")
    _require(identity == {"model": "glm-5.2", "upstream": "ollama_cloud", "route_mode": "codexhub"}, "route_identity_mismatch")


def _build_source_payload(namespace_tool_count: int) -> dict[str, Any]:
    shell_command = {
        "type": "function",
        "name": "shell_command",
        "description": "Run a command in the deterministic evidence replay.",
        "parameters": {
            "type": "object",
            "properties": {"command": {"type": "string"}},
            "required": ["command"],
            "additionalProperties": False,
        },
    }
    apply_patch = {
        "type": "custom",
        "name": "apply_patch",
        "description": "Apply one structured file update in the deterministic evidence replay.",
        "format": {"type": "grammar", "syntax": "lark", "definition": "start: patch"},
    }
    tools: list[dict[str, Any]] = [shell_command, apply_patch]
    if namespace_tool_count:
        tools.append(
            {
                "type": "namespace",
                "name": "mcp__issue108_replay",
                "description": "Eligible namespace tools for the deterministic evidence replay.",
                "tools": [
                    {
                        "type": "function",
                        "name": f"eligible_{index:03d}",
                        "parameters": {"type": "object", "properties": {}},
                    }
                    for index in range(namespace_tool_count)
                ],
            }
        )
    return {"model": "glm-5.2", "input": [], "tools": tools}


def _prepared_surface(workspace: Path, namespace_tool_count: int, strategy: str) -> dict[str, Any]:
    source_payload = _build_source_payload(namespace_tool_count)
    source_bytes = json.dumps(source_payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    source_python = workspace / "src-python"
    _require(source_python.is_dir(), "workspace_source_unavailable")
    sys.path.insert(0, str(source_python))
    try:
        import codex_proxy

        prepared_bytes = codex_proxy.compatible_request_body(
            source_bytes,
            {
                "name": "ollama_cloud",
                "upstream_format": "responses",
                "tool_protocol": "responses_structured",
                "tool_surface_strategy": strategy,
            },
        )
    finally:
        sys.path.pop(0)
    prepared_payload = json.loads(prepared_bytes)
    tools = prepared_payload.get("tools")
    _require(isinstance(tools, list), "prepared_surface_invalid")
    flattened_count = sum(
        1
        for tool in tools
        if isinstance(tool, dict)
        and isinstance(tool.get("name"), str)
        and tool["name"].startswith("mcp__issue108_replay__eligible_")
    )
    tool_names = {
        tool.get("name")
        for tool in tools
        if isinstance(tool, dict) and isinstance(tool.get("name"), str)
    }
    return {
        "strategy": strategy,
        "source_payload_sha256": _canonical_digest(source_payload),
        "prepared_surface_sha256": _canonical_digest({"model": prepared_payload.get("model"), "tools": tools}),
        "direct_tool_count": len(tools),
        "namespace_flattened_count": flattened_count,
        "tool_search_visible": "tool_search" in tool_names,
        "tool_names": sorted(tool_names),
    }


def _derive_tool_surface_outcome(surface: dict[str, Any]) -> dict[str, str]:
    """Derive the A/B result from the prepared surface, never from fixture claims."""

    strategy = surface["strategy"]
    direct_tool_count = surface["direct_tool_count"]
    flattened_count = surface["namespace_flattened_count"]
    tool_search_visible = surface["tool_search_visible"]
    tool_names = set(surface["tool_names"])
    _require(strategy in {"eager", "deferred_core"}, "tool_surface_strategy_invalid")
    _require(isinstance(direct_tool_count, int), "tool_surface_direct_tool_count_invalid")
    _require(isinstance(flattened_count, int), "tool_surface_flattened_count_invalid")
    _require(isinstance(tool_search_visible, bool), "tool_surface_search_visibility_invalid")
    _require(_REQUIRED_CORE_TOOL_NAMES <= tool_names, "tool_surface_required_core_missing")

    if strategy == "deferred_core":
        _require(flattened_count == 0, "tool_surface_deferred_namespace_flattened")
        _require(tool_search_visible, "tool_surface_deferred_search_missing")
    else:
        _require(not tool_search_visible, "tool_surface_eager_search_unexpected")

    if direct_tool_count > DIRECT_TOOL_SURFACE_BUDGET:
        _require(strategy == "eager", "tool_surface_deferred_budget_exceeded")
        _require(flattened_count > 0, "tool_surface_budget_source_invalid")
        return {"status": "red", "decision": "direct_tool_surface_budget_exceeded"}

    _require(flattened_count == 0, "tool_surface_under_budget_namespace_flattened")
    return {"status": "green", "decision": "within_direct_tool_budget"}


def validate_tool_surface_fixture(payload: dict[str, Any], workspace: Path) -> dict[str, Any]:
    root = _require_mapping(
        payload,
        {"schema", "sanitized", "evidence_kind", "route_identity", "cases"},
        "tool_surface_fixture_schema_invalid",
    )
    _require(root["schema"] == TOOL_SURFACE_SCHEMA, "tool_surface_fixture_schema_invalid")
    _require(root["sanitized"] is True, "tool_surface_fixture_not_sanitized")
    _require(root["evidence_kind"] == "deterministic_semantic_replay", "tool_surface_evidence_kind_invalid")
    _route_identity(root["route_identity"])
    cases = _require_list(root["cases"], "tool_surface_cases_invalid")
    _require(len(cases) == 3, "tool_surface_cases_invalid")

    expectations = {
        "minimal_core": ("eager", 0, False, "green", "within_direct_tool_budget"),
        "namespace_200_eager": ("eager", 200, False, "red", "direct_tool_surface_budget_exceeded"),
        "namespace_200_deferred_core": ("deferred_core", 200, True, "green", "within_direct_tool_budget"),
    }
    seen: set[str] = set()
    case_statuses: dict[str, str] = {}
    case_decisions: dict[str, str] = {}
    source_digests: dict[str, str] = {}
    direct_tool_counts: dict[str, int] = {}
    deferred_payload_digest = ""
    for case_value in cases:
        case = _require_mapping(
            case_value,
            {
                "id",
                "tool_surface_strategy",
                "namespace_tool_count",
                "source_payload_sha256",
                "prepared_surface_sha256",
                "direct_tool_count",
                "namespace_flattened_count",
                "tool_search_visible",
            },
            "tool_surface_case_schema_invalid",
        )
        case_id = case["id"]
        _require(isinstance(case_id, str) and case_id in expectations and case_id not in seen, "tool_surface_case_invalid")
        seen.add(case_id)
        strategy, namespace_count, tool_search_visible, expected_status, expected_decision = expectations[case_id]
        _require(case["tool_surface_strategy"] == strategy, "tool_surface_strategy_invalid")
        _require(case["namespace_tool_count"] == namespace_count, "tool_surface_namespace_count_invalid")
        _require(isinstance(case["tool_search_visible"], bool) and case["tool_search_visible"] is tool_search_visible, "tool_surface_search_visibility_invalid")
        _require(isinstance(case["source_payload_sha256"], str) and _SHA256.fullmatch(case["source_payload_sha256"]), "tool_surface_digest_invalid")
        _require(isinstance(case["prepared_surface_sha256"], str) and _SHA256.fullmatch(case["prepared_surface_sha256"]), "tool_surface_digest_invalid")
        _require_int(case["direct_tool_count"], "tool_surface_direct_tool_count_invalid", minimum=1)
        _require_int(case["namespace_flattened_count"], "tool_surface_flattened_count_invalid")
        actual = _prepared_surface(workspace, namespace_count, strategy)
        _require(case["source_payload_sha256"] == actual["source_payload_sha256"], "tool_surface_source_digest_mismatch")
        _require(case["prepared_surface_sha256"] == actual["prepared_surface_sha256"], "tool_surface_prepared_digest_mismatch")
        _require(case["direct_tool_count"] == actual["direct_tool_count"], "tool_surface_direct_tool_count_mismatch")
        _require(case["namespace_flattened_count"] == actual["namespace_flattened_count"], "tool_surface_flattened_count_mismatch")
        _require(case["tool_search_visible"] is actual["tool_search_visible"], "tool_surface_search_visibility_mismatch")
        outcome = _derive_tool_surface_outcome(actual)
        _require(outcome["status"] == expected_status, "tool_surface_semantic_outcome_invalid")
        _require(outcome["decision"] == expected_decision, "tool_surface_semantic_decision_invalid")
        case_statuses[case_id] = outcome["status"]
        case_decisions[case_id] = outcome["decision"]
        source_digests[case_id] = case["source_payload_sha256"]
        direct_tool_counts[case_id] = case["direct_tool_count"]
        if case_id == "namespace_200_deferred_core":
            deferred_payload_digest = case["prepared_surface_sha256"]

    _require(seen == set(expectations), "tool_surface_cases_invalid")
    _require(source_digests["namespace_200_eager"] == source_digests["namespace_200_deferred_core"], "tool_surface_same_input_missing")
    _require(direct_tool_counts["namespace_200_eager"] > DIRECT_TOOL_SURFACE_BUDGET, "tool_surface_eager_budget_not_exceeded")
    _require(direct_tool_counts["namespace_200_deferred_core"] <= DIRECT_TOOL_SURFACE_BUDGET, "tool_surface_deferred_budget_not_reduced")
    _require(case_statuses == {"minimal_core": "green", "namespace_200_eager": "red", "namespace_200_deferred_core": "green"}, "tool_surface_ab_outcome_invalid")
    return {
        "mode": "tool_surface_evidence_replay",
        "passed": True,
        "failures": [],
        "case_outcomes": case_statuses,
        "case_decisions": case_decisions,
        "direct_tool_counts": direct_tool_counts,
        "direct_tool_budget": DIRECT_TOOL_SURFACE_BUDGET,
        "same_200_source_payload": True,
        "deferred_payload_digest": deferred_payload_digest,
    }


def _validate_mutation(value: Any, code: str) -> None:
    mutation = _require_mapping(value, {"file_count", "hunk_count", "line_replacement_count"}, code)
    _require(mutation == {"file_count": 1, "hunk_count": 1, "line_replacement_count": 1}, code)


def _validate_gateway_capture_events(value: Any) -> tuple[int, dict[str, int]]:
    events = _require_list(value, "qualification_gateway_events_invalid")
    request_start_count = 0
    request_complete_count = 0
    adapter_counts = {"apply_patch": 0, "history": 0}
    for value in events:
        _require(isinstance(value, dict), "qualification_gateway_events_invalid")
        event_name = value.get("event")
        if event_name == "request_start":
            event = _require_mapping(value, {"event", "route_identity"}, "qualification_gateway_events_invalid")
            _route_identity(event["route_identity"])
            request_start_count += 1
        elif event_name == "request_complete":
            event = _require_mapping(value, {"event", "status"}, "qualification_gateway_events_invalid")
            _require(event["status"] == 200, "qualification_request_completion_invalid")
            request_complete_count += 1
        elif event_name == "apply_patch_adapter":
            event = _require_mapping(value, {"event", "outcome"}, "qualification_gateway_events_invalid")
            _require(event["outcome"] == "adapted", "qualification_adapter_outcome_invalid")
            adapter_counts["apply_patch"] += 1
        elif event_name == "history_adapter":
            event = _require_mapping(value, {"event", "outcome"}, "qualification_gateway_events_invalid")
            _require(event["outcome"] == "adapted", "qualification_adapter_outcome_invalid")
            adapter_counts["history"] += 1
        else:
            raise EvidenceValidationError("qualification_gateway_event_unexpected")

    _require(request_start_count >= 1, "qualification_request_start_missing")
    _require(request_complete_count == request_start_count, "qualification_request_completion_count_invalid")
    _require(adapter_counts["apply_patch"] >= 1, "qualification_apply_patch_adapter_missing")
    _require(adapter_counts["history"] >= 1, "qualification_history_adapter_missing")
    return request_start_count, adapter_counts


def _validate_canonical_tool_shape(value: Any) -> str:
    shape = _require_list(value, "qualification_canonical_tool_shape_invalid")
    _require(shape == _EXPECTED_DEFERRED_CANONICAL_TOOL_SHAPE, "qualification_canonical_tool_shape_invalid")
    return _canonical_digest(shape)


def _validate_request_surfaces(value: Any, *, request_count: int, canonical_shape_sha256: str) -> str:
    surfaces = _require_list(value, "qualification_request_surfaces_invalid")
    _require(len(surfaces) == request_count, "qualification_payload_equivalence_invalid")
    raw_digests: set[str] = set()
    for value in surfaces:
        surface = _require_mapping(
            value,
            {
                "raw_tools_sha256",
                "canonical_shape_sha256",
                "namespace_flattened_count",
                "tool_search_visible",
            },
            "qualification_request_surface_invalid",
        )
        raw_digest = surface["raw_tools_sha256"]
        _require(isinstance(raw_digest, str) and _SHA256.fullmatch(raw_digest), "qualification_request_surface_invalid")
        _require(surface["canonical_shape_sha256"] == canonical_shape_sha256, "qualification_canonical_shape_digest_mismatch")
        _require(surface["namespace_flattened_count"] == 0, "qualification_namespace_flattened")
        _require(surface["tool_search_visible"] is True, "qualification_search_visibility_invalid")
        raw_digests.add(raw_digest)
    _require(len(raw_digests) == 1, "qualification_payload_equivalence_invalid")
    deferred_payload_digest = raw_digests.pop()
    _require(deferred_payload_digest == _ACCEPTED_DEFERRED_PAYLOAD_SHA256, "qualification_payload_digest_mismatch")
    return deferred_payload_digest


def validate_qualification_fixture(payload: dict[str, Any]) -> dict[str, Any]:
    root = _require_mapping(
        payload,
        {"schema", "sanitized", "provenance", "acceptance_capture"},
        "qualification_fixture_schema_invalid",
    )
    _require(root["schema"] == QUALIFICATION_SCHEMA, "qualification_fixture_schema_invalid")
    _require(root["sanitized"] is True, "qualification_fixture_not_sanitized")
    provenance = _require_mapping(
        root["provenance"],
        {"capture_kind", "capture_format"},
        "qualification_provenance_invalid",
    )
    _require(
        provenance == {
            "capture_kind": "isolated_responses_acceptance",
            "capture_format": "sanitized_gateway_capture.v1",
        },
        "qualification_provenance_invalid",
    )
    capture = _require_mapping(
        root["acceptance_capture"],
        {
            "tool_surface_strategy",
            "gateway_events",
            "request_surfaces",
            "canonical_tool_shape",
            "cli",
            "mutation",
            "tool_search_call_count",
        },
        "qualification_capture_schema_invalid",
    )
    _require(capture["tool_surface_strategy"] == "deferred_core", "qualification_strategy_invalid")
    request_count, adapter_counts = _validate_gateway_capture_events(capture["gateway_events"])
    canonical_shape_sha256 = _validate_canonical_tool_shape(capture["canonical_tool_shape"])
    deferred_payload_digest = _validate_request_surfaces(
        capture["request_surfaces"],
        request_count=request_count,
        canonical_shape_sha256=canonical_shape_sha256,
    )
    cli = _require_mapping(capture["cli"], {"completed_tool_sequence", "terminal_event"}, "qualification_cli_evidence_invalid")
    _require(cli["completed_tool_sequence"] == ["shell_command", "apply_patch", "shell_command"], "qualification_sequence_invalid")
    _require(cli["terminal_event"] == "turn.completed", "qualification_termination_invalid")
    _validate_mutation(capture["mutation"], "qualification_mutation_invalid")
    _require(capture["tool_search_call_count"] == 0, "qualification_tool_search_selected")
    return {
        "mode": "qualification_evidence_replay",
        "passed": True,
        "failures": [],
        "request_count": request_count,
        "adapter_counts": adapter_counts,
        "request_error_count": 0,
        "fallback_counts": {"luna": 0, "terra": 0},
        "deferred_payload_digest": deferred_payload_digest,
        "canonical_tool_shape_digest": canonical_shape_sha256,
    }


def validate_failure_fixture(payload: dict[str, Any]) -> dict[str, Any]:
    root = _require_mapping(
        payload,
        {
            "schema",
            "sanitized",
            "phase",
            "route_identity",
            "last_successful_tool",
            "response_termination",
            "failure_classification",
            "request_count",
            "adapter_counts",
            "timeout_classification",
            "error_class",
            "http_status",
            "failure_codes",
        },
        "qualification_failure_fixture_schema_invalid",
    )
    _require(root["schema"] == FAILURE_SCHEMA, "qualification_failure_fixture_schema_invalid")
    _require(root["sanitized"] is True, "qualification_failure_fixture_not_sanitized")
    _require(root["phase"] in {"readiness_preflight", "acceptance"}, "qualification_failure_phase_invalid")
    _route_identity(root["route_identity"])
    _require(root["last_successful_tool"] in {"none", "shell_command", "apply_patch"}, "qualification_failure_last_tool_invalid")
    _require(
        root["response_termination"]
        in {"completed", "harness_error", "harness_timeout", "sandbox_rejected", "transport_error", "process_tail_cleanup", "response_error", "readiness_failed"},
        "qualification_failure_termination_invalid",
    )
    _require_int(root["request_count"], "qualification_failure_request_count_invalid")
    adapter_counts = _require_mapping(root["adapter_counts"], {"apply_patch", "history"}, "qualification_failure_adapter_counts_invalid")
    _require_int(adapter_counts["apply_patch"], "qualification_failure_adapter_counts_invalid")
    _require_int(adapter_counts["history"], "qualification_failure_adapter_counts_invalid")
    _require(
        root["timeout_classification"] in {"harness_error", "model_idle", "transport", "sandbox", "process_tail_cleanup", "not_timeout", "readiness"},
        "qualification_failure_timeout_classification_invalid",
    )
    _require(
        root["failure_classification"] == root["timeout_classification"],
        "qualification_failure_classification_invalid",
    )
    _require(
        isinstance(root["error_class"], str)
        and (root["error_class"] == "none" or re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", root["error_class"])),
        "qualification_failure_error_class_invalid",
    )
    _require_int(root["http_status"], "qualification_failure_http_status_invalid")
    _require(root["http_status"] <= 599, "qualification_failure_http_status_invalid")
    if root["failure_classification"] == "harness_error":
        _require(root["response_termination"] == "harness_error", "qualification_failure_harness_termination_invalid")
        _require(root["error_class"] != "none", "qualification_failure_harness_error_class_missing")
        _require(root["http_status"] == 500, "qualification_failure_harness_http_status_invalid")
    else:
        _require(root["error_class"] == "none" and root["http_status"] == 0, "qualification_failure_unexpected_harness_detail")
    failure_codes = _require_list(root["failure_codes"], "qualification_failure_codes_invalid")
    _require(failure_codes and all(isinstance(code, str) and re.fullmatch(r"[a-z0-9_]+", code) for code in failure_codes), "qualification_failure_codes_invalid")
    return {
        "mode": "qualification_failure_evidence_replay",
        "passed": True,
        "failures": [],
        "request_count": root["request_count"],
        "timeout_classification": root["timeout_classification"],
        "failure_classification": root["failure_classification"],
    }


def _run(mode: str, fixture: Path, workspace: Path | None) -> dict[str, Any]:
    payload = _load_fixture(fixture)
    if mode == "tool-surface":
        _require(workspace is not None, "workspace_required")
        return validate_tool_surface_fixture(payload, workspace.resolve())
    if mode == "qualification":
        return validate_qualification_fixture(payload)
    return validate_failure_fixture(payload)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--mode", choices=("tool-surface", "qualification", "qualification-failure"), required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--workspace", type=Path)
    args = parser.parse_args(argv)
    try:
        report = _run(args.mode, args.fixture, args.workspace)
    except EvidenceValidationError:
        report = {
            "mode": {
                "tool-surface": "tool_surface_evidence_replay",
                "qualification": "qualification_evidence_replay",
                "qualification-failure": "qualification_failure_evidence_replay",
            }[args.mode],
            "passed": False,
            "failures": ["evidence_fixture_invalid"],
        }
        print(json.dumps(report, ensure_ascii=True, separators=(",", ":")))
        return 1
    except Exception:
        report = {
            "mode": {
                "tool-surface": "tool_surface_evidence_replay",
                "qualification": "qualification_evidence_replay",
                "qualification-failure": "qualification_failure_evidence_replay",
            }[args.mode],
            "passed": False,
            "failures": ["evidence_validator_execution_failed"],
        }
        print(json.dumps(report, ensure_ascii=True, separators=(",", ":")))
        return 1
    print(json.dumps(report, ensure_ascii=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
