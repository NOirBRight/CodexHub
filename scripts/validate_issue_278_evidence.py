#!/usr/bin/env python3
"""Validate a sanitized Issue #278/#280 CLI runner summary."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
from typing import Any


SCHEMA = "codexhub.issue278.cli-tool-search.v1"
DEFAULT_SUMMARY = Path("docs/evidence/issue-278/summary.template.json")
CASE_IDS = {"native_explicit", "native_no_hint", "adapted_explicit", "adapted_no_hint"}
EXPLICIT_CASES = {"native_explicit", "adapted_explicit"}
NO_HINT_CASES = CASE_IDS - EXPLICIT_CASES
SENSITIVE_KEYS = {
    "authorization",
    "api_key",
    "access_token",
    "bearer",
    "cookie",
    "credential",
    "credentials",
    "header",
    "headers",
    "password",
    "prompt",
    "request_body",
    "response_body",
}
SENSITIVE_MARKERS = ("sk-", "bearer ", "-----begin ", "http://", "https://", "fixture-target.txt")
SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
SHA1 = re.compile(r"^[0-9a-f]{40}$")
PROTOCOL_BY_CASE = {
    "native_explicit": "responses_structured",
    "native_no_hint": "responses_structured",
    "adapted_explicit": "chat_tools",
    "adapted_no_hint": "chat_tools",
}
TRACE_DECLARATION = "tool_search.declaration"
TRACE_NOT_SELECTED = "tool_search.not_selected"
TRACE_SEARCH_CALL = "tool_search.call"
TRACE_SEARCH_RESULT = "tool_search.result"
TRACE_DISCOVERED_DECLARATION = "discovered.declaration"
TRACE_DISCOVERED_CALL = "discovered.call"
TRACE_DISCOVERED_RESULT = "discovered.result"
WORKFLOW_TOOL_NAMES = ("shell_command", "apply_patch", "shell_command")


class EvidenceValidationError(ValueError):
    pass


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise EvidenceValidationError(code)


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _expected_trace(case_id: str) -> list[str]:
    if case_id in NO_HINT_CASES:
        return [TRACE_DECLARATION, TRACE_NOT_SELECTED]
    return [
        TRACE_DECLARATION,
        TRACE_SEARCH_CALL,
        TRACE_SEARCH_RESULT,
        TRACE_DISCOVERED_DECLARATION,
        TRACE_DISCOVERED_CALL,
        TRACE_DISCOVERED_RESULT,
        "code_mode.shell_command.call",
        "code_mode.shell_command.result",
        "code_mode.apply_patch.call",
        "code_mode.apply_patch.result",
        "code_mode.shell_command.call",
        "code_mode.shell_command.result",
    ]


def _function_call_shapes() -> list[str]:
    return [
        "response.created",
        "response.output_item.added:function_call",
        "response.function_call_arguments.delta",
        "response.function_call_arguments.done",
        "response.output_item.done:function_call",
        "response.completed",
    ]


def _text_response_shapes() -> list[str]:
    return [
        "response.created",
        "response.output_item.added:message",
        "response.output_text.delta",
        "response.output_item.done:message",
        "response.completed",
    ]


def _expected_response_shapes(case_id: str) -> list[str]:
    if case_id in NO_HINT_CASES:
        return _text_response_shapes()
    search = (
        [
            "response.created",
            "response.output_item.done:tool_search_call",
            "response.completed",
        ]
        if case_id.startswith("native_")
        else _function_call_shapes()
    )
    return search + _function_call_shapes() * 4 + _text_response_shapes()


def _expected_provenance(case_id: str) -> dict[str, Any]:
    trace = _expected_trace(case_id)
    explicit = case_id in EXPLICIT_CASES
    search_trace = trace[:6] if explicit else trace
    code_trace = trace[6:] if explicit else []
    code_steps = list(WORKFLOW_TOOL_NAMES) if explicit else []
    protocols = ["responses"] * (6 if explicit else 1)
    response_shapes = _expected_response_shapes(case_id)
    history_order_value = {"trace": trace, "protocols": protocols, "response_shapes": response_shapes}
    identity_slots = [{"ordinal": ordinal, "role": token} for ordinal, token in enumerate(trace, start=1)]
    return {
        "trace": trace,
        "trace_digest": _digest(trace),
        "search": {
            "ordered_stages": search_trace,
            "stage_count": len(search_trace),
            "call_count": search_trace.count(TRACE_SEARCH_CALL),
            "result_count": search_trace.count(TRACE_SEARCH_RESULT),
            "discovered_declaration_count": search_trace.count(TRACE_DISCOVERED_DECLARATION),
            "subsequent_call_count": search_trace.count(TRACE_DISCOVERED_CALL),
            "subsequent_result_count": search_trace.count(TRACE_DISCOVERED_RESULT),
            "order_digest": _digest(search_trace),
        },
        "code_mode": {
            "ordered_steps": code_steps,
            "call_count": sum(token.endswith(".call") for token in code_trace),
            "result_count": sum(token.endswith(".result") for token in code_trace),
            "order_digest": _digest(code_trace),
        },
        "history": {
            "request_count": 6 if explicit else 1,
            "response_count": 6 if explicit else 1,
            "protocol_sequence": protocols,
            "response_shapes": response_shapes,
            "order_digest": _digest(history_order_value),
            "identity_digest": _digest(identity_slots),
        },
    }


def _walk_sanitized(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            _require(str(key).lower() not in SENSITIVE_KEYS, "sensitive_field_retained")
            _walk_sanitized(child)
    elif isinstance(value, list):
        for child in value:
            _walk_sanitized(child)
    elif isinstance(value, str):
        lowered = value.lower()
        _require(not any(marker in lowered for marker in SENSITIVE_MARKERS), "sensitive_value_retained")


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise EvidenceValidationError("summary_unreadable") from error
    _require(isinstance(value, dict), "summary_root_invalid")
    _walk_sanitized(value)
    return value


def _validate_provenance(case_id: str, provenance: Any) -> dict[str, Any]:
    _require(isinstance(provenance, dict), "provenance_missing")
    expected = _expected_provenance(case_id)
    trace = provenance.get("trace")
    _require(trace == expected["trace"], "provenance_trace_invalid")
    _require(provenance.get("trace_digest") == _digest(trace), "provenance_digest_invalid")

    search = provenance.get("search")
    _require(isinstance(search, dict), "search_provenance_missing")
    expected_search = expected["search"]
    for key in (
        "ordered_stages",
        "stage_count",
        "call_count",
        "result_count",
        "discovered_declaration_count",
        "subsequent_call_count",
        "subsequent_result_count",
    ):
        _require(search.get(key) == expected_search[key], "search_provenance_invalid")
    _require(search.get("order_digest") == _digest(search["ordered_stages"]), "provenance_digest_invalid")

    code_mode = provenance.get("code_mode")
    _require(isinstance(code_mode, dict), "code_mode_provenance_missing")
    expected_code_mode = expected["code_mode"]
    for key in ("ordered_steps", "call_count", "result_count"):
        _require(code_mode.get(key) == expected_code_mode[key], "code_mode_provenance_invalid")
    code_trace = []
    for name in code_mode["ordered_steps"]:
        code_trace.extend((f"code_mode.{name}.call", f"code_mode.{name}.result"))
    _require(code_mode.get("order_digest") == _digest(code_trace), "provenance_digest_invalid")

    history = provenance.get("history")
    _require(isinstance(history, dict), "history_provenance_missing")
    expected_history = expected["history"]
    for key in ("request_count", "response_count", "protocol_sequence", "response_shapes"):
        _require(history.get(key) == expected_history[key], "history_provenance_invalid")
    history_order_value = {
        "trace": trace,
        "protocols": history["protocol_sequence"],
        "response_shapes": history["response_shapes"],
    }
    _require(history.get("order_digest") == _digest(history_order_value), "provenance_digest_invalid")
    identity_slots = [{"ordinal": ordinal, "role": token} for ordinal, token in enumerate(trace, start=1)]
    _require(history.get("identity_digest") == _digest(identity_slots), "provenance_identity_invalid")
    return expected


def validate_summary(value: dict[str, Any], *, candidate_sha: str | None = None) -> dict[str, Any]:
    _require(value.get("schema") == SCHEMA, "schema_invalid")
    _require(value.get("evidence_status") == "observed_synthetic_upstream", "evidence_status_invalid")
    _require(value.get("qualification_status") in {"unqualified", "not_run", "failed", "passed"}, "qualification_status_invalid")
    bound_sha = value.get("candidate_sha")
    _require(bound_sha is None or (isinstance(bound_sha, str) and SHA1.fullmatch(bound_sha) is not None), "candidate_sha_invalid")
    if value.get("qualification_status") in {"passed", "failed"}:
        _require(bound_sha is not None, "candidate_sha_required")
    if candidate_sha is not None:
        _require(isinstance(candidate_sha, str) and SHA1.fullmatch(candidate_sha) is not None, "candidate_sha_invalid")
        _require(bound_sha == candidate_sha, "candidate_sha_mismatch")

    route = value.get("route")
    _require(isinstance(route, dict), "route_invalid")
    _require(route.get("selected_model") == "opaque-selected-model", "route_model_not_opaque")
    _require(route.get("selected_provider") == "opaque-selected-provider", "route_provider_not_opaque")
    _require(route.get("cross_provider_requests") == 0, "cross_provider_request")
    _require(route.get("hosted_search_substitution") is False, "hosted_search_substitution")

    sanitization = value.get("sanitization")
    _require(
        sanitization
        == {
            "raw_bodies_retained": False,
            "prompts_retained": False,
            "credentials_retained": False,
            "ids_opaque_or_hashed": True,
        },
        "sanitization_invalid",
    )
    controls = value.get("negative_controls")
    _require(
        controls == {"unknown_alias": "passed", "duplicate_identity": "passed", "malformed_envelope": "passed"},
        "negative_controls_invalid",
    )

    cases = value.get("cases")
    _require(isinstance(cases, list), "cases_invalid")
    seen: set[str] = set()
    for case in cases:
        _require(isinstance(case, dict), "case_invalid")
        case_id = case.get("id")
        _require(isinstance(case_id, str) and case_id in CASE_IDS and case_id not in seen, "case_id_invalid")
        seen.add(case_id)
        _require(case.get("protocol") == PROTOCOL_BY_CASE[case_id], "protocol_invalid")
        _require(case.get("planner_eligible") is True, "planner_eligibility_invalid")
        _require(case.get("tool_search_visible") is True, "tool_search_visibility_invalid")
        _require(case.get("gateway_owned_tool_execution_count") == 0, "gateway_tool_execution_observed")
        _require(case.get("identity_preserved") is True, "identity_not_preserved")
        digest = case.get("history_order_digest")
        _require(isinstance(digest, str) and SHA256.fullmatch(digest) is not None, "history_digest_invalid")
        provenance = _validate_provenance(case_id, case.get("provenance"))
        _require(digest == provenance["history"]["order_digest"], "history_digest_mismatch")
        event_types = case.get("sse_event_types")
        _require(isinstance(event_types, list) and all(isinstance(event, str) for event in event_types), "sse_event_types_invalid")
        if case_id in EXPLICIT_CASES:
            _require(case.get("selection") == "selected", "explicit_selection_invalid")
            _require(case.get("classification") == "completed", "explicit_classification_invalid")
        else:
            _require(case.get("selection") == "model_not_selected", "no_hint_selection_invalid")
            _require(case.get("classification") == "model_not_selected", "no_hint_classification_invalid")
    if value.get("qualification_status") == "passed":
        _require(seen == CASE_IDS, "case_coverage_incomplete")
    return {"schema": SCHEMA, "case_count": len(cases), "status": "pass"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    parser.add_argument("--candidate-sha")
    args = parser.parse_args(argv)
    try:
        result = validate_summary(_load(args.summary), candidate_sha=args.candidate_sha)
    except EvidenceValidationError as error:
        print(f"ISSUE_278_EVIDENCE_INVALID:{error}")
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
