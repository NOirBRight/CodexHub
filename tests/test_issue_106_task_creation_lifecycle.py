from copy import deepcopy
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "docs" / "evidence" / "issue-106" / "task-creation-lifecycle.json"
REPLAY_SCRIPT = ROOT / "scripts" / "check_codex_task_creation_lifecycle.py"
CHECKER_SPEC = importlib.util.spec_from_file_location("issue_106_lifecycle_checker", REPLAY_SCRIPT)
assert CHECKER_SPEC is not None
assert CHECKER_SPEC.loader is not None
CHECKER = importlib.util.module_from_spec(CHECKER_SPEC)
CHECKER_SPEC.loader.exec_module(CHECKER)


def run_replay(case: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(REPLAY_SCRIPT), "--replay-case", case],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def read_evidence() -> dict[str, object]:
    return json.loads(EVIDENCE.read_text(encoding="utf-8"))


def test_task_creation_evidence_matches_the_strict_sanitized_contract() -> None:
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))

    assert CHECKER.validate_evidence(evidence, repo_root=ROOT) == []


def test_task_creation_evidence_requires_a_provenance_source() -> None:
    evidence = read_evidence()
    del evidence["source"]

    mismatches = CHECKER.validate_evidence(evidence, repo_root=ROOT)

    assert "evidence has missing fields: source" in mismatches


def test_task_creation_evidence_rejects_unknown_and_unsafe_source_values() -> None:
    evidence = read_evidence()
    source = evidence["source"]
    assert isinstance(source, dict)
    source["notes"] = r"C:\Synthetic\Private\path\rollout-019f0000"
    source["credential"] = "synthetic-secret-value"
    source["evidence_type"] = r"C:\Synthetic\Private\path\rollout-019f0000"
    source["conclusion_limit"] = "synthetic-secret-value"

    mismatches = CHECKER.validate_evidence(evidence, repo_root=ROOT)
    rendered = " | ".join(mismatches)

    assert "source has unexpected fields" in mismatches
    assert "unsafe local path string at $.source.<unexpected>" in mismatches
    assert "unsafe task or session identifier string at $.source.<unexpected>" in mismatches
    assert "unsafe credential-like string at $.source.<unexpected>" in mismatches
    assert "unsafe local path string at $.source.evidence_type" in mismatches
    assert "unsafe credential-like string at $.source.conclusion_limit" in mismatches
    assert r"C:\Synthetic\Private\path\rollout-019f0000" not in rendered
    assert "synthetic-secret-value" not in rendered


def test_task_creation_evidence_reports_unavailable_boundary_sources_without_local_paths(tmp_path: Path) -> None:
    mismatches = CHECKER.validate_evidence(read_evidence(), repo_root=tmp_path)
    rendered = " | ".join(mismatches)

    assert "ownership boundary source unavailable: src-python/config_overlay.py" in mismatches
    assert str(tmp_path) not in rendered


def test_task_creation_boundary_guard_covers_bounded_app_server_probe_sites() -> None:
    assert Path("src-tauri/src/models.rs") in CHECKER.OWNERSHIP_BOUNDARY_PATHS
    assert Path("src-tauri/src/openai_usage.rs") in CHECKER.OWNERSHIP_BOUNDARY_PATHS
    assert CHECKER.validate_owned_boundary_sources(ROOT) == []


def test_task_creation_identity_replay_passes() -> None:
    result = run_replay("identity")

    assert result.returncode == 0, result.stderr
    assert "TASK_CREATION_LIFECYCLE_COMPLETE" in result.stdout


@pytest.mark.parametrize(
    "case",
    ["materialize-red", "unmaterialize-green", "fail-git-preflight", "skip-cleanup", "identifier-leak"],
)
def test_task_creation_negative_replays_fail_visibly(case: str) -> None:
    result = run_replay(case)

    assert result.returncode == 1
    assert "TASK_CREATION_LIFECYCLE_MISMATCH:" in result.stderr
