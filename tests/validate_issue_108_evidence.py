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
QUALIFICATION_SCHEMA = "codexhub.issue108.qualification-evidence.v1"
FAILURE_SCHEMA = "codexhub.issue108.qualification-failure.v1"

_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_SENSITIVE_VALUE = re.compile(
    r"(?i)(?:api[_-]?key|authorization|bearer|token|password|secret|sk-[a-z0-9]|ghp_)"
)
_LOCAL_PATH = re.compile(r"(?i)(?:[a-z]:[\\/]|\\\\users\\|/users/|/home/)")
_RAW_CONTENT = re.compile(r"(?i)(?:automated qualification|\*\*\* begin patch|\*\*\* update file:)")


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


def _prepared_surface(workspace: Path, namespace_tool_count: int, strategy: str) -> tuple[str, str, int, int, bool]:
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
    tool_names = {tool.get("name") for tool in tools if isinstance(tool, dict)}
    return (
        _canonical_digest(source_payload),
        _canonical_digest({"model": prepared_payload.get("model"), "tools": tools}),
        len(tools),
        flattened_count,
        "tool_search" in tool_names,
    )


def _validate_tool_outcome(case_id: str, outcome_value: Any) -> str:
    outcome = _require_mapping(
        outcome_value,
        {
            "status",
            "termination",
            "tool_sequence",
            "mutation",
            "adapter_counts",
            "request_error_count",
            "fallback_count",
        },
        "tool_surface_outcome_invalid",
    )
    status = outcome["status"]
    _require(status in {"green", "red"}, "tool_surface_status_invalid")
    sequence = _require_list(outcome["tool_sequence"], "tool_surface_sequence_invalid")
    _require(all(isinstance(tool, str) for tool in sequence), "tool_surface_sequence_invalid")
    mutation = _require_mapping(
        outcome["mutation"],
        {"file_count", "hunk_count", "line_replacement_count"},
        "tool_surface_mutation_invalid",
    )
    adapter_counts = _require_mapping(
        outcome["adapter_counts"], {"apply_patch", "history"}, "tool_surface_adapter_counts_invalid"
    )
    _require_int(outcome["request_error_count"], "tool_surface_request_errors_invalid")
    _require_int(outcome["fallback_count"], "tool_surface_fallbacks_invalid")
    _require_int(mutation["file_count"], "tool_surface_mutation_invalid")
    _require_int(mutation["hunk_count"], "tool_surface_mutation_invalid")
    _require_int(mutation["line_replacement_count"], "tool_surface_mutation_invalid")
    _require_int(adapter_counts["apply_patch"], "tool_surface_adapter_counts_invalid")
    _require_int(adapter_counts["history"], "tool_surface_adapter_counts_invalid")

    if case_id in {"minimal_core", "namespace_200_deferred_core"}:
        _require(status == "green", "tool_surface_green_outcome_missing")
        _require(outcome["termination"] == "completed", "tool_surface_green_termination_invalid")
        _require(sequence == ["shell_command", "apply_patch", "shell_command"], "tool_surface_green_sequence_invalid")
        _require(mutation == {"file_count": 1, "hunk_count": 1, "line_replacement_count": 1}, "tool_surface_green_mutation_invalid")
        _require(adapter_counts == {"apply_patch": 1, "history": 1}, "tool_surface_green_adapters_invalid")
    else:
        _require(case_id == "namespace_200_eager", "tool_surface_case_invalid")
        _require(status == "red", "tool_surface_red_outcome_missing")
        _require(outcome["termination"] == "core_tool_chain_not_started", "tool_surface_red_termination_invalid")
        _require(sequence == [], "tool_surface_red_sequence_invalid")
        _require(mutation == {"file_count": 0, "hunk_count": 0, "line_replacement_count": 0}, "tool_surface_red_mutation_invalid")
        _require(adapter_counts == {"apply_patch": 0, "history": 0}, "tool_surface_red_adapters_invalid")
    _require(outcome["request_error_count"] == 0, "tool_surface_request_error_observed")
    _require(outcome["fallback_count"] == 0, "tool_surface_fallback_observed")
    return status


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
        "minimal_core": ("eager", 0, False),
        "namespace_200_eager": ("eager", 200, False),
        "namespace_200_deferred_core": ("deferred_core", 200, True),
    }
    seen: set[str] = set()
    case_statuses: dict[str, str] = {}
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
                "outcome",
            },
            "tool_surface_case_schema_invalid",
        )
        case_id = case["id"]
        _require(isinstance(case_id, str) and case_id in expectations and case_id not in seen, "tool_surface_case_invalid")
        seen.add(case_id)
        strategy, namespace_count, tool_search_visible = expectations[case_id]
        _require(case["tool_surface_strategy"] == strategy, "tool_surface_strategy_invalid")
        _require(case["namespace_tool_count"] == namespace_count, "tool_surface_namespace_count_invalid")
        _require(isinstance(case["tool_search_visible"], bool) and case["tool_search_visible"] is tool_search_visible, "tool_surface_search_visibility_invalid")
        _require(isinstance(case["source_payload_sha256"], str) and _SHA256.fullmatch(case["source_payload_sha256"]), "tool_surface_digest_invalid")
        _require(isinstance(case["prepared_surface_sha256"], str) and _SHA256.fullmatch(case["prepared_surface_sha256"]), "tool_surface_digest_invalid")
        _require_int(case["direct_tool_count"], "tool_surface_direct_tool_count_invalid", minimum=1)
        _require_int(case["namespace_flattened_count"], "tool_surface_flattened_count_invalid")
        actual = _prepared_surface(workspace, namespace_count, strategy)
        _require(case["source_payload_sha256"] == actual[0], "tool_surface_source_digest_mismatch")
        _require(case["prepared_surface_sha256"] == actual[1], "tool_surface_prepared_digest_mismatch")
        _require(case["direct_tool_count"] == actual[2], "tool_surface_direct_tool_count_mismatch")
        _require(case["namespace_flattened_count"] == actual[3], "tool_surface_flattened_count_mismatch")
        _require(case["tool_search_visible"] is actual[4], "tool_surface_search_visibility_mismatch")
        case_statuses[case_id] = _validate_tool_outcome(case_id, case["outcome"])
        source_digests[case_id] = case["source_payload_sha256"]
        direct_tool_counts[case_id] = case["direct_tool_count"]
        if case_id == "namespace_200_deferred_core":
            deferred_payload_digest = case["prepared_surface_sha256"]

    _require(seen == set(expectations), "tool_surface_cases_invalid")
    _require(source_digests["namespace_200_eager"] == source_digests["namespace_200_deferred_core"], "tool_surface_same_input_missing")
    _require(case_statuses == {"minimal_core": "green", "namespace_200_eager": "red", "namespace_200_deferred_core": "green"}, "tool_surface_ab_outcome_invalid")
    return {
        "mode": "tool_surface_evidence_replay",
        "passed": True,
        "failures": [],
        "case_outcomes": case_statuses,
        "direct_tool_counts": direct_tool_counts,
        "same_200_source_payload": True,
        "deferred_payload_digest": deferred_payload_digest,
    }


def _validate_mutation(value: Any, code: str) -> None:
    mutation = _require_mapping(value, {"file_count", "hunk_count", "line_replacement_count"}, code)
    _require(mutation == {"file_count": 1, "hunk_count": 1, "line_replacement_count": 1}, code)


def _validate_adapter_counts(value: Any, code: str) -> None:
    adapter_counts = _require_mapping(value, {"apply_patch", "history"}, code)
    _require_int(adapter_counts["apply_patch"], code, minimum=1)
    _require_int(adapter_counts["history"], code, minimum=1)


def validate_qualification_fixture(payload: dict[str, Any]) -> dict[str, Any]:
    root = _require_mapping(
        payload,
        {
            "schema",
            "sanitized",
            "route_identity",
            "tool_surface_strategy",
            "tool_sequence",
            "mutation",
            "termination",
            "request_count",
            "request_error_count",
            "fallback_counts",
            "adapter_counts",
            "deferred_payload",
        },
        "qualification_fixture_schema_invalid",
    )
    _require(root["schema"] == QUALIFICATION_SCHEMA, "qualification_fixture_schema_invalid")
    _require(root["sanitized"] is True, "qualification_fixture_not_sanitized")
    _route_identity(root["route_identity"])
    _require(root["tool_surface_strategy"] == "deferred_core", "qualification_strategy_invalid")
    _require(root["tool_sequence"] == ["shell_command", "apply_patch", "shell_command"], "qualification_sequence_invalid")
    _validate_mutation(root["mutation"], "qualification_mutation_invalid")
    _require(root["termination"] == "completed", "qualification_termination_invalid")
    request_count = _require_int(root["request_count"], "qualification_request_count_invalid", minimum=1)
    _require(root["request_error_count"] == 0, "qualification_request_error_observed")
    fallback_counts = _require_mapping(root["fallback_counts"], {"luna", "terra"}, "qualification_fallbacks_invalid")
    _require(fallback_counts == {"luna": 0, "terra": 0}, "qualification_fallback_observed")
    _validate_adapter_counts(root["adapter_counts"], "qualification_adapter_counts_invalid")
    deferred_payload = _require_mapping(
        root["deferred_payload"],
        {"sha256", "equivalent_request_count", "namespace_flattened_count", "tool_search_visible"},
        "qualification_deferred_payload_invalid",
    )
    _require(isinstance(deferred_payload["sha256"], str) and _SHA256.fullmatch(deferred_payload["sha256"]), "qualification_deferred_payload_invalid")
    _require(deferred_payload["equivalent_request_count"] == request_count, "qualification_payload_equivalence_invalid")
    _require(deferred_payload["namespace_flattened_count"] == 0, "qualification_namespace_flattened")
    _require(deferred_payload["tool_search_visible"] is True, "qualification_search_visibility_invalid")
    return {
        "mode": "qualification_evidence_replay",
        "passed": True,
        "failures": [],
        "request_count": request_count,
        "deferred_payload_digest": deferred_payload["sha256"],
    }


def validate_failure_fixture(payload: dict[str, Any]) -> dict[str, Any]:
    root = _require_mapping(
        payload,
        {
            "schema",
            "sanitized",
            "route_identity",
            "last_successful_tool",
            "response_termination",
            "request_count",
            "adapter_counts",
            "timeout_classification",
            "failure_codes",
        },
        "qualification_failure_fixture_schema_invalid",
    )
    _require(root["schema"] == FAILURE_SCHEMA, "qualification_failure_fixture_schema_invalid")
    _require(root["sanitized"] is True, "qualification_failure_fixture_not_sanitized")
    _route_identity(root["route_identity"])
    _require(root["last_successful_tool"] in {"none", "shell_command", "apply_patch"}, "qualification_failure_last_tool_invalid")
    _require(
        root["response_termination"]
        in {"completed", "harness_timeout", "sandbox_rejected", "transport_error", "process_tail_cleanup", "response_error", "readiness_failed"},
        "qualification_failure_termination_invalid",
    )
    _require_int(root["request_count"], "qualification_failure_request_count_invalid")
    adapter_counts = _require_mapping(root["adapter_counts"], {"apply_patch", "history"}, "qualification_failure_adapter_counts_invalid")
    _require_int(adapter_counts["apply_patch"], "qualification_failure_adapter_counts_invalid")
    _require_int(adapter_counts["history"], "qualification_failure_adapter_counts_invalid")
    _require(
        root["timeout_classification"] in {"model_idle", "transport", "sandbox", "process_tail_cleanup", "not_timeout", "readiness"},
        "qualification_failure_timeout_classification_invalid",
    )
    failure_codes = _require_list(root["failure_codes"], "qualification_failure_codes_invalid")
    _require(failure_codes and all(isinstance(code, str) and re.fullmatch(r"[a-z0-9_]+", code) for code in failure_codes), "qualification_failure_codes_invalid")
    return {
        "mode": "qualification_failure_evidence_replay",
        "passed": True,
        "failures": [],
        "request_count": root["request_count"],
        "timeout_classification": root["timeout_classification"],
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
