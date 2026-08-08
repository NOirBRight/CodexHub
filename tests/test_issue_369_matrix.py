from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.validate_issue_369_matrix import MATRIX, validate


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


def test_issue_369_rejects_forged_unqualified_selector(tmp_path: Path) -> None:
    payload = _matrix()
    row = next(row for row in payload["models"] if row["model"] == "gpt-5.5")
    row["selector"] = True

    with pytest.raises(ValueError, match="selector must fail closed"):
        validate(_write_matrix(tmp_path, payload))


def test_issue_369_rejects_incomplete_go_lifecycle(tmp_path: Path) -> None:
    payload = _matrix()
    row = next(row for row in payload["models"] if row["model"] == "gpt-5.6-terra")
    row["v2"]["terminal_status"] = None

    with pytest.raises(ValueError, match="GO lifecycle evidence"):
        validate(_write_matrix(tmp_path, payload))


def test_issue_369_rejects_complete_lifecycle_on_non_go_row(tmp_path: Path) -> None:
    payload = _matrix()
    row = next(row for row in payload["models"] if row["model"] == "gpt-5.5")
    row["v1"] = {
        "selection": "model_catalog_multi_agent_version",
        "lifecycle": "spawn_send_wait_close",
        "terminal_status": 200,
    }
    row["v2"] = {
        "selection": "model_catalog_multi_agent_version",
        "lifecycle": "spawn_list_send_followup_wait_list",
        "terminal_status": 200,
    }

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
