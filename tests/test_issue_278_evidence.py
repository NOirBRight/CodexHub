from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.validate_issue_278_evidence import EvidenceValidationError, validate_summary


ROOT = Path(__file__).resolve().parents[1]
SUMMARY = ROOT / "docs" / "evidence" / "issue-278" / "summary.template.json"


def _summary() -> dict:
    return json.loads(SUMMARY.read_text(encoding="utf-8"))


def test_issue_278_template_remains_valid_without_a_run() -> None:
    assert validate_summary(_summary())["status"] == "pass"


def test_issue_278_candidate_sha_argument_requires_exact_lowercase_binding() -> None:
    summary = _summary()
    candidate = "a" * 40
    summary["candidate_sha"] = candidate

    assert validate_summary(summary, candidate_sha=candidate)["status"] == "pass"
    with pytest.raises(EvidenceValidationError, match="candidate_sha_mismatch"):
        validate_summary(summary, candidate_sha="b" * 40)
    with pytest.raises(EvidenceValidationError, match="candidate_sha_invalid"):
        validate_summary(summary, candidate_sha="A" * 40)


def test_issue_278_passed_summary_requires_candidate_binding() -> None:
    summary = _summary()
    summary["qualification_status"] = "passed"
    summary["cases"] = [
        {
            "id": case_id,
            "planner_eligible": True,
            "tool_search_visible": True,
            "gateway_owned_tool_execution_count": 0,
            "identity_preserved": True,
            "history_order_digest": "a" * 64,
            "sse_event_types": [],
            "selection": "selected" if "explicit" in case_id else "model_not_selected",
            "classification": "completed" if "explicit" in case_id else "model_not_selected",
        }
        for case_id in ("native_explicit", "native_no_hint", "adapted_explicit", "adapted_no_hint")
    ]

    with pytest.raises(EvidenceValidationError, match="candidate_sha_required"):
        validate_summary(summary)
