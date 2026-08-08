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


class EvidenceValidationError(ValueError):
    pass


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise EvidenceValidationError(code)


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
        _require(case.get("planner_eligible") is True, "planner_eligibility_invalid")
        _require(case.get("tool_search_visible") is True, "tool_search_visibility_invalid")
        _require(case.get("gateway_owned_tool_execution_count") == 0, "gateway_tool_execution_observed")
        _require(case.get("identity_preserved") is True, "identity_not_preserved")
        digest = case.get("history_order_digest")
        _require(isinstance(digest, str) and SHA256.fullmatch(digest) is not None, "history_digest_invalid")
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
