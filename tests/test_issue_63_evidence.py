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
    fixture = _fixture()
    fixture["candidate"]["revision"] = "REPLACE_WITH_FINAL_BETA3_SHA"
    fixture["candidate"]["revision_status"] = "placeholder_pending_final_candidate"
    with pytest.raises(EvidenceValidationError, match="candidate_revision_placeholder"):
        validate_fixture(fixture, require_final_candidate=True)


def test_issue_63_candidate_sha_is_strictly_bound() -> None:
    fixture = _fixture()
    revision = fixture["candidate"]["revision"]

    statuses = validate_fixture(fixture, candidate_sha=revision)
    assert statuses["native_explicit_hint"]

    with pytest.raises(EvidenceValidationError, match="candidate_sha_mismatch"):
        validate_fixture(fixture, candidate_sha="0" * 40)
    with pytest.raises(EvidenceValidationError, match="candidate_sha_invalid"):
        validate_fixture(fixture, candidate_sha="A" * 40)


def test_issue_63_validator_cli_binds_candidate_sha() -> None:
    revision = _fixture()["candidate"]["revision"]
    accepted = subprocess.run(
        [
            sys.executable,
            str(VALIDATOR),
            "--fixture",
            str(FIXTURE),
            "--candidate-sha",
            revision,
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    rejected = subprocess.run(
        [
            sys.executable,
            str(VALIDATOR),
            "--fixture",
            str(FIXTURE),
            "--candidate-sha",
            "0" * 40,
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert accepted.returncode == 0
    assert "ISSUE_63_EVIDENCE_FIXTURE_OK" in accepted.stdout
    assert rejected.returncode == 1
    assert "candidate_sha_mismatch" in rejected.stdout


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
