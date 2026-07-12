import json
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "docs" / "evidence" / "issue-106" / "task-creation-lifecycle.json"
REPLAY_SCRIPT = ROOT / "scripts" / "check_codex_task_creation_lifecycle.py"


def run_replay(case: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(REPLAY_SCRIPT), "--replay-case", case],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )


def walk_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | set().union(*(walk_keys(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(walk_keys(item) for item in value)) if value else set()
    return set()


def test_task_creation_evidence_covers_red_green_boundary_without_private_identifiers() -> None:
    evidence = json.loads(EVIDENCE.read_text(encoding="utf-8"))

    assert evidence["schema_version"] == 1
    assert evidence["capture_kind"] == "sanitized_task_creation_ab_evidence"
    assert evidence["product_boundary"] == {
        "codexhub_manages_global_mcp": False,
        "codexhub_has_bounded_app_server_model_probes": True,
        "codexhub_exposes_native_task_lifecycle": False,
        "proven_link_from_model_probes_to_task_materialization": False,
        "product_behavior_changed": False,
    }
    assert evidence["red_case"]["classification"] == "half_created"
    assert evidence["red_case"]["worktree_provisioned"] is True
    assert evidence["red_case"]["rollout_materialized"] is False
    assert evidence["green_case"]["materialized"] is True
    assert evidence["green_case"]["permission_preflight"] == {
        "filesystem": "unrestricted",
        "network": "enabled",
        "approval": "never",
    }
    assert evidence["green_case"]["git_preflight"] == "passed"
    assert evidence["cleanup_observation"]["clean_orphan_worktrees_removed"] == 2
    assert evidence["cleanup_observation"]["client_held_empty_directory"] is True
    assert evidence["sanitization"] == {
        "contains_credentials": False,
        "contains_local_paths": False,
        "contains_prompts_or_messages": False,
        "contains_task_or_session_identifiers": False,
    }
    assert not {
        "authorization",
        "api_key",
        "client_thread_id",
        "cwd",
        "session_id",
        "task_id",
        "thread_id",
        "worktree_path",
    } & walk_keys(evidence)


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
