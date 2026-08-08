from __future__ import annotations

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.validate_issue_63_evidence import (
    EvidenceValidationError,
    validate_fixture,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "docs" / "evidence" / "issue-63" / "tool-search-lifecycle.json"
VALIDATOR = ROOT / "scripts" / "validate_issue_63_evidence.py"


def _fixture() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_issue_63_fixture_validates_without_network_or_candidate_sha() -> None:
    statuses = validate_fixture(_fixture())

    assert set(statuses) == {
        "native_explicit_hint",
        "adapted_explicit_hint",
        "native_no_hint",
        "adapted_no_hint",
    }


def test_issue_63_validator_cli_is_read_only_and_bounded() -> None:
    result = subprocess.run(
        [sys.executable, str(VALIDATOR), "--fixture", str(FIXTURE)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "ISSUE_63_EVIDENCE_FIXTURE_OK" in result.stdout
    assert result.stderr == ""


def test_issue_63_final_candidate_gate_rejects_placeholder() -> None:
    with pytest.raises(EvidenceValidationError, match="candidate_revision_placeholder"):
        validate_fixture(_fixture(), require_final_candidate=True)


def test_issue_63_fixture_rejects_sensitive_fields() -> None:
    fixture = _fixture()
    fixture["route"]["headers"] = {"authorization": "redacted"}

    with pytest.raises(EvidenceValidationError, match="sensitive_field_retained"):
        validate_fixture(fixture)


def test_issue_63_fixture_rejects_adapted_envelope_mutation() -> None:
    fixture = _fixture()
    adapted = next(case for case in fixture["cases"] if case["id"] == "adapted_explicit_hint")
    mutated = copy.deepcopy(adapted)
    mutated["wire"]["search_call"]["arguments"] = "{\"opaque_search_input\":{\"query\":\"changed\"}}"
    fixture["cases"] = [mutated if case["id"] == mutated["id"] else case for case in fixture["cases"]]

    with pytest.raises(EvidenceValidationError, match="adapted_input_inverse_invalid"):
        validate_fixture(fixture)
