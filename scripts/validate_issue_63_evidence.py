#!/usr/bin/env python3
"""Validate the bounded, offline Issue #63 tool-search evidence fixture.

The fixture is a structural contract and replay ledger.  This command never
opens a network connection, starts Codex, reads credentials, or calls an
upstream Provider.  A placeholder candidate revision is accepted for local
fixture checks and can be rejected with ``--require-final-candidate`` before a
candidate is qualified.
"""

from __future__ import annotations

from python_runtime_contract import require_python_313

require_python_313(__file__)

import argparse
import json
import re
from pathlib import Path
from typing import Any, Mapping


DEFAULT_FIXTURE = Path("docs/evidence/issue-63/tool-search-lifecycle.json")
SCHEMA = "codexhub.issue63.tool-search-lifecycle.v1"
PLACEHOLDER_REVISION = "REPLACE_WITH_FINAL_BETA3_SHA"
ID_PATTERN = re.compile(r"^(?:item|call|response)-[a-z0-9_-]+$")
FORBIDDEN_KEYS = {
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
    "session_id",
    "user_content",
}
FORBIDDEN_VALUE_MARKERS = (
    "sk-",
    "bearer ",
    "-----begin ",
    "http://",
    "https://",
)


class EvidenceValidationError(ValueError):
    """A bounded fixture failure that contains no user or wire content."""


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise EvidenceValidationError(code)


def _load_fixture(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise EvidenceValidationError("fixture_unreadable") from error
    _require(isinstance(value, dict), "fixture_root_invalid")
    return value


def _check_redaction(value: Any, *, path: str = "fixture") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key).lower()
            _require(key_text not in FORBIDDEN_KEYS, "sensitive_field_retained")
            _check_redaction(child, path=f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, child in enumerate(value):
            _check_redaction(child, path=f"{path}[{index}]")
        return
    if isinstance(value, str):
        lowered = value.lower()
        _require(not any(marker in lowered for marker in FORBIDDEN_VALUE_MARKERS), "sensitive_value_retained")


def _check_id(value: Any) -> str:
    _require(isinstance(value, str) and ID_PATTERN.fullmatch(value) is not None, "opaque_identity_required")
    return value


def _check_history(history: Any, expected: list[str]) -> None:
    _require(history == expected, "history_order_or_identity_mismatch")
    for value in history:
        _check_id(value)


def _check_tool_declaration(declaration: Any) -> None:
    _require(
        declaration == {
            "type": "tool_search",
            "execution": "client",
        },
        "tool_search_declaration_mismatch",
    )


def _check_discovered_tools(value: Any) -> None:
    _require(
        value
        == [
            {
                "type": "function",
                "name": "opaque_discovered_tool",
                "parameters": {"type": "object"},
            }
        ],
        "discovered_declaration_mismatch",
    )


def _check_canonical_lifecycle(case: Mapping[str, Any]) -> None:
    canonical = case.get("canonical_lifecycle")
    _require(isinstance(canonical, Mapping), "canonical_lifecycle_missing")
    _check_tool_declaration(canonical.get("declaration"))

    search_call = canonical.get("search_call")
    search_output = canonical.get("search_output")
    follow_up_call = canonical.get("follow_up_call")
    follow_up_result = canonical.get("follow_up_result")
    for item in (search_call, search_output, follow_up_call, follow_up_result):
        _require(isinstance(item, Mapping), "canonical_item_missing")
        _check_id(item.get("id"))
        _check_id(item.get("call_id"))

    _require(search_call["type"] == "tool_search_call", "search_call_type_invalid")
    _require(search_call["execution"] == "client", "search_call_owner_invalid")
    _require(search_call["arguments"] == {"query": "opaque_query_sentinel"}, "search_query_invalid")
    _require(search_output["type"] == "tool_search_output", "search_output_type_invalid")
    _require(search_output["execution"] == "client", "search_output_owner_invalid")
    _check_discovered_tools(search_output.get("tools"))
    _require(search_output["call_id"] == search_call["call_id"], "search_call_output_link_invalid")
    _require(follow_up_call["type"] == "function_call", "follow_up_call_type_invalid")
    _require(follow_up_call["name"] == "opaque_discovered_tool", "follow_up_name_invalid")
    _require(follow_up_call["arguments"] == "{}", "follow_up_arguments_invalid")
    _require(follow_up_result["type"] == "function_call_output", "follow_up_result_type_invalid")
    _require(follow_up_result["call_id"] == follow_up_call["call_id"], "follow_up_call_result_link_invalid")
    _require(follow_up_result["output"] == "opaque_result_sentinel", "follow_up_result_invalid")
    expected_history = [
        search_call["id"],
        search_output["id"],
        follow_up_call["id"],
        follow_up_result["id"],
    ]
    _check_history(canonical.get("history"), expected_history)


def _check_sse_shape(value: Any, expected_events: list[str], *, require_added: bool) -> None:
    _require(isinstance(value, list), "sse_sequence_missing")
    _require([event.get("event") for event in value] == expected_events, "sse_event_order_invalid")
    for event in value:
        _require(isinstance(event, Mapping), "sse_event_invalid")
        event_name = event.get("event")
        _require(isinstance(event_name, str), "sse_event_name_invalid")
        if event_name == "response.completed":
            _check_id(event.get("response"))
        elif event_name != "response.completed":
            _check_id(event.get("item"))
    if require_added:
        _require(expected_events[0] == "response.output_item.added", "adapted_sse_must_assemble")


def _check_native_case(case: Mapping[str, Any]) -> None:
    canonical = case["canonical_lifecycle"]
    wire = case.get("wire")
    _require(isinstance(wire, Mapping), "native_wire_missing")
    _require(wire.get("declaration") == canonical["declaration"], "native_declaration_not_preserved")
    for key in ("search_call", "search_output", "follow_up_call", "follow_up_result"):
        _require(wire.get(key) == canonical[key], f"native_{key}_not_preserved")
    _check_sse_shape(
        wire.get("sse", {}).get("search"),
        ["response.output_item.done", "response.completed"],
        require_added=False,
    )
    _check_sse_shape(
        wire.get("sse", {}).get("follow_up"),
        [
            "response.output_item.added",
            "response.function_call_arguments.delta",
            "response.function_call_arguments.done",
            "response.output_item.done",
            "response.completed",
        ],
        require_added=True,
    )
    _check_history(wire.get("history"), canonical["history"])


def _check_adapted_case(case: Mapping[str, Any]) -> None:
    canonical = case["canonical_lifecycle"]
    adapter = case.get("adapter")
    wire = case.get("wire")
    _require(isinstance(adapter, Mapping), "adapter_contract_missing")
    _require(adapter.get("source_name") == "tool_search", "adapter_source_invalid")
    _require(adapter.get("wire_alias") == "opaque_tool_search_alias", "adapter_alias_invalid")
    _require(adapter.get("inverse") == "injective_and_reversible_contract", "adapter_inverse_invalid")
    _require(isinstance(wire, Mapping), "adapted_wire_missing")
    _require(
        wire.get("declaration")
        == {
            "type": "function",
            "name": adapter["wire_alias"],
            "parameters": {"type": "object"},
        },
        "adapted_declaration_invalid",
    )

    search_call = wire.get("search_call")
    search_output = wire.get("search_output")
    follow_up_call = wire.get("follow_up_call")
    follow_up_result = wire.get("follow_up_result")
    for item in (search_call, search_output, follow_up_call, follow_up_result):
        _require(isinstance(item, Mapping), "adapted_item_missing")
        _check_id(item.get("id"))
        _check_id(item.get("call_id"))
    _require(search_call["name"] == adapter["wire_alias"], "adapted_search_alias_invalid")
    try:
        encoded_input = json.loads(search_call["arguments"])
        encoded_output = json.loads(search_output["output"])
    except (TypeError, ValueError, KeyError) as error:
        raise EvidenceValidationError("adapted_envelope_invalid") from error
    _require(
        encoded_input == {adapter["input_key"]: canonical["search_call"]["arguments"]},
        "adapted_input_inverse_invalid",
    )
    _require(
        encoded_output == {adapter["output_key"]: {"tools": canonical["search_output"]["tools"]}},
        "adapted_output_inverse_invalid",
    )
    _require(follow_up_call == canonical["follow_up_call"], "adapted_follow_up_call_not_preserved")
    _require(follow_up_result == canonical["follow_up_result"], "adapted_follow_up_result_not_preserved")
    _check_sse_shape(
        wire.get("sse", {}).get("search"),
        [
            "response.output_item.added",
            "response.function_call_arguments.delta",
            "response.function_call_arguments.done",
            "response.output_item.done",
            "response.completed",
        ],
        require_added=True,
    )
    _check_sse_shape(
        wire.get("sse", {}).get("follow_up"),
        [
            "response.output_item.added",
            "response.function_call_arguments.delta",
            "response.function_call_arguments.done",
            "response.output_item.done",
            "response.completed",
        ],
        require_added=True,
    )
    _check_history(wire.get("history"), canonical["history"])


def _check_no_hint_case(case: Mapping[str, Any]) -> None:
    planner = case.get("planner")
    wire = case.get("wire")
    _require(isinstance(planner, Mapping), "no_hint_planner_missing")
    _require(planner.get("eligible") is True, "no_hint_planner_not_eligible")
    _require(planner.get("tool_search_visible") is True, "no_hint_tool_search_not_visible")
    _require(planner.get("hint") == "none", "no_hint_marker_invalid")
    _require(planner.get("selection") == "not_selected", "no_hint_selection_invalid")
    _require(planner.get("classification") == "model_not_selected", "no_hint_classification_invalid")
    _require(case.get("outcome") == "no_search_selected", "no_hint_outcome_invalid")
    _require(case.get("upstream_sampling") == "selected_route_only", "no_hint_route_invalid")
    _require(isinstance(wire, Mapping), "no_hint_wire_missing")
    _require(wire.get("search_lifecycle") == [], "no_hint_search_lifecycle_present")
    _check_history(wire.get("history"), [])


def validate_fixture(
    value: Mapping[str, Any],
    *,
    require_final_candidate: bool = False,
    candidate_sha: str | None = None,
) -> dict[str, str]:
    _require(value.get("schema") == SCHEMA, "schema_invalid")
    _require(value.get("artifact_kind") == "issue63_tool_search_lifecycle_fixture", "artifact_kind_invalid")
    _require(value.get("evidence_status") == "synthetic_fixture_only", "evidence_status_invalid")
    _require(value.get("qualification_status") == "unqualified", "qualification_status_invalid")
    _check_redaction(value)

    candidate = value.get("candidate")
    _require(isinstance(candidate, Mapping), "candidate_identity_missing")
    revision = candidate.get("revision")
    if candidate_sha is not None:
        _require(
            isinstance(candidate_sha, str) and re.fullmatch(r"[0-9a-f]{40}", candidate_sha) is not None,
            "candidate_sha_invalid",
        )
    if revision == PLACEHOLDER_REVISION:
        _require(
            candidate.get("revision_status") == "placeholder_pending_final_candidate",
            "candidate_placeholder_status_invalid",
        )
        _require(not require_final_candidate, "candidate_revision_placeholder")
    else:
        _require(isinstance(revision, str) and re.fullmatch(r"[0-9a-f]{40}", revision) is not None, "candidate_revision_invalid")
        _require(candidate.get("revision_status") == "final_candidate", "candidate_revision_status_invalid")
    if candidate_sha is not None:
        _require(revision == candidate_sha, "candidate_sha_mismatch")

    route = value.get("route")
    _require(isinstance(route, Mapping), "route_identity_missing")
    _require(route.get("selected_route") == "opaque_selected_route", "route_identity_invalid")
    _require(route.get("route_identity_preserved") is True, "route_identity_not_preserved")
    _require(route.get("cross_provider_requests") == 0, "cross_provider_request_observed")
    _require(route.get("hosted_search_substitution") is False, "hosted_search_substitution_observed")

    cases = value.get("cases")
    _require(isinstance(cases, list), "cases_invalid")
    by_id = {case.get("id"): case for case in cases if isinstance(case, Mapping)}
    _require(set(by_id) == {"native_explicit_hint", "adapted_explicit_hint", "native_no_hint", "adapted_no_hint"}, "case_set_invalid")
    for case in cases:
        _require(isinstance(case, Mapping), "case_invalid")
        planner = case.get("planner")
        _require(isinstance(planner, Mapping), "planner_missing")
        _require(planner.get("eligible") is True, "planner_eligibility_invalid")
        _require(planner.get("tool_search_visible") is True, "tool_search_visibility_invalid")
        if case["id"] in {"native_explicit_hint", "adapted_explicit_hint"}:
            _require(planner.get("hint") == "explicit", "explicit_hint_missing")
            _require(planner.get("selection") == "selected", "explicit_hint_not_selected")
            _require(case.get("outcome") == "completed", "explicit_hint_outcome_invalid")
            _check_canonical_lifecycle(case)
            if case["id"] == "native_explicit_hint":
                _require(case.get("disposition") == "native", "native_disposition_invalid")
                _check_native_case(case)
            else:
                _require(case.get("disposition") == "adapt", "adapted_disposition_invalid")
                _check_adapted_case(case)
        else:
            _require(case.get("disposition") in {"native", "adapt"}, "no_hint_disposition_invalid")
            _check_no_hint_case(case)
    return {
        "native_explicit_hint": "native lifecycle contract checked",
        "adapted_explicit_hint": "adapted contract fixture checked (not runtime qualification)",
        "native_no_hint": "model_not_selected classification checked",
        "adapted_no_hint": "model_not_selected classification checked",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument(
        "--require-final-candidate",
        action="store_true",
        help="reject the documented placeholder revision",
    )
    parser.add_argument(
        "--candidate-sha",
        help="require the fixture candidate revision to equal this lowercase 40-hex SHA",
    )
    args = parser.parse_args(argv)
    try:
        statuses = validate_fixture(
            _load_fixture(args.fixture),
            require_final_candidate=args.require_final_candidate,
            candidate_sha=args.candidate_sha,
        )
    except EvidenceValidationError as error:
        print(f"ISSUE_63_EVIDENCE_INVALID:{error}")
        return 1
    print("ISSUE_63_EVIDENCE_FIXTURE_OK")
    for case_id, status in statuses.items():
        print(f"{case_id}: {status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
