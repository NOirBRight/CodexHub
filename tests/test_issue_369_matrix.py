from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.validate_issue_369_matrix import MATRIX, PHASE_FIELDS, PHASE_STATUSES, validate


def _matrix() -> dict:
    return json.loads(MATRIX.read_text(encoding="utf-8"))


def _write_matrix(tmp_path: Path, payload: dict) -> Path:
    path = tmp_path / "matrix.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_issue_369_matrix_is_sanitized_and_selector_fails_closed() -> None:
    payload = validate()
    assert payload["candidate_revision"]
    sol = next(row for row in payload["models"] if row["model"] == "gpt-5.6-sol")
    assert sol["verdict"] == "UNQUALIFIED"
    assert sol["selector"] is False
    terra = next(row for row in payload["models"] if row["model"] == "gpt-5.6-terra")
    assert terra["verdict"] == "PARTIAL"
    assert terra["selector"] is False
    assert {row["model"] for row in payload["models"]} == {
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        "gpt-5.5",
        "gpt-5.3-codex-spark",
        "gpt-5.2",
        "gpt-5.4",
        "gpt-5.4-mini",
        "codex-auto-review",
    }


def test_issue_369_candidate_sha_is_strictly_bound() -> None:
    payload = _matrix()

    assert (
        validate(MATRIX, candidate_sha=payload["candidate_revision"])["candidate_revision"]
        == payload["candidate_revision"]
    )
    with pytest.raises(ValueError, match="candidate_sha_mismatch"):
        validate(MATRIX, candidate_sha="0" * 40)
    with pytest.raises(ValueError, match="candidate_sha_invalid"):
        validate(MATRIX, candidate_sha="A" * 40)


def test_issue_369_requires_bounded_lifecycle_fields(tmp_path: Path) -> None:
    payload = _matrix()
    del payload["models"][0]["v1"]["lifecycle"]

    with pytest.raises(ValueError, match="lifecycle fields"):
        validate(_write_matrix(tmp_path, payload))


def test_issue_369_requires_sanitized_evidence_links(tmp_path: Path) -> None:
    payload = _matrix()
    payload.pop("evidence_links")

    with pytest.raises(ValueError, match="evidence links"):
        validate(_write_matrix(tmp_path, payload))


def test_issue_369_requires_all_sanitized_phase_fields_and_bounded_statuses() -> None:
    payload = validate()
    assert all(
        all(field in row[version] for field in PHASE_FIELDS)
        for row in payload["models"]
        for version in ("v1", "v2")
    )
    assert all(
        row[version][field] in PHASE_STATUSES
        for row in payload["models"]
        for version in ("v1", "v2")
        for field in PHASE_FIELDS[1:]
    )


def test_issue_369_rejects_unobserved_go_phase(tmp_path: Path) -> None:
    payload = _matrix()
    row = next(row for row in payload["models"] if row["model"] == "gpt-5.6-terra")
    row["verdict"] = "GO"
    row["selector"] = True

    with pytest.raises(ValueError, match="GO lifecycle evidence"):
        validate(_write_matrix(tmp_path, payload))


def test_issue_369_rejects_unbounded_phase_status(tmp_path: Path) -> None:
    payload = _matrix()
    payload["models"][0]["v2"]["restart_readback"] = "pending"

    with pytest.raises(ValueError, match="lifecycle phase status"):
        validate(_write_matrix(tmp_path, payload))


def test_issue_369_rejects_raw_identity_fields(tmp_path: Path) -> None:
    payload = _matrix()
    payload["models"][0]["v2"]["agent_id"] = "opaque-identity"

    with pytest.raises(ValueError, match="sensitive matrix key"):
        validate(_write_matrix(tmp_path, payload))


def test_issue_369_rejects_wrong_observed_identity_kind(tmp_path: Path) -> None:
    payload = _matrix()
    row = next(row for row in payload["models"] if row["model"] == "gpt-5.6-terra")
    row["v1"]["spawn_identity_kind"] = "task_path"

    with pytest.raises(ValueError, match="lifecycle identity kind"):
        validate(_write_matrix(tmp_path, payload))


def test_issue_369_rejects_forged_unqualified_selector(tmp_path: Path) -> None:
    payload = _matrix()
    row = next(row for row in payload["models"] if row["model"] == "gpt-5.5")
    row["selector"] = True

    with pytest.raises(ValueError, match="selector must fail closed"):
        validate(_write_matrix(tmp_path, payload))


def test_issue_369_rejects_incomplete_go_lifecycle(tmp_path: Path) -> None:
    payload = _matrix()
    row = next(row for row in payload["models"] if row["model"] == "gpt-5.6-terra")
    row["verdict"] = "GO"
    row["selector"] = True
    row["v2"]["terminal_status"] = None

    with pytest.raises(ValueError, match="GO lifecycle evidence"):
        validate(_write_matrix(tmp_path, payload))


def test_issue_369_rejects_complete_lifecycle_on_non_go_row(tmp_path: Path) -> None:
    payload = _matrix()
    row = next(row for row in payload["models"] if row["model"] == "gpt-5.6-terra")
    row["verdict"] = "UNQUALIFIED"
    row["selector"] = False
    for version in ("v1", "v2"):
        evidence = row[version]
        for field in PHASE_FIELDS[1:]:
            evidence[field] = "observed"
    row["v1"]["follow_up"] = "not_applicable"
    row["v1"]["list_interrupt"] = "not_applicable"

    with pytest.raises(ValueError, match="non-GO complete lifecycle evidence"):
        validate(_write_matrix(tmp_path, payload))


def test_issue_369_validator_cli_accepts_and_rejects_candidate_sha() -> None:
    script = Path(__file__).resolve().parents[1] / "scripts" / "validate_issue_369_matrix.py"
    revision = _matrix()["candidate_revision"]

    accepted = subprocess.run(
        [sys.executable, str(script), "--candidate-sha", revision],
        check=False,
        capture_output=True,
        text=True,
    )
    rejected = subprocess.run(
        [sys.executable, str(script), "--candidate-sha", "0" * 40],
        check=False,
        capture_output=True,
        text=True,
    )

    assert accepted.returncode == 0
    assert "ISSUE_369_MATRIX_OK" in accepted.stdout
    assert rejected.returncode == 1
    assert "candidate_sha_mismatch" in rejected.stdout
