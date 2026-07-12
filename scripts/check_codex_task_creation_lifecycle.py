"""Replay the sanitized Issue #106 task-creation A/B lifecycle contract.

This is deliberately a fixture verifier, not a Task creator. It must never
create a Codex Task, modify global Codex configuration, or inspect internal
Codex databases. A live create replay requires explicit Orchestrator approval.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EVIDENCE_PATH = REPO_ROOT / "docs" / "evidence" / "issue-106" / "task-creation-lifecycle.json"
OWNERSHIP_BOUNDARY_PATHS = (
    Path("src-python/config_overlay.py"),
    Path("src-python/catalog_sync.py"),
    Path("src-tauri/src/catalog.rs"),
    Path("src-tauri/src/config.rs"),
    Path("src-tauri/src/proxy.rs"),
)
GLOBAL_MCP_CONFIGURATION_TOKENS = ("openaideveloperdocs", "mcp_servers")
FORBIDDEN_KEYS = {
    "api_key",
    "authorization",
    "client_thread_id",
    "cwd",
    "session_id",
    "task_id",
    "thread_id",
    "worktree_path",
}
EXPECTED_SANITIZATION = {
    "contains_credentials": False,
    "contains_local_paths": False,
    "contains_prompts_or_messages": False,
    "contains_task_or_session_identifiers": False,
}


def _mapping(value: Any, label: str, mismatches: list[str]) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    mismatches.append(f"{label} must be an object")
    return {}


def _value(mapping: dict[str, Any], key: str, label: str, mismatches: list[str]) -> Any:
    if key in mapping:
        return mapping[key]
    mismatches.append(f"{label}.{key} is missing")
    return None


def _expect_equal(actual: Any, expected: Any, label: str, mismatches: list[str]) -> None:
    if actual != expected:
        mismatches.append(f"{label} did not match the expected contract")


def _forbidden_key_paths(value: Any, prefix: str = "$") -> list[str]:
    if isinstance(value, dict):
        found: list[str] = []
        for key, child in value.items():
            child_path = f"{prefix}.{key}"
            if key in FORBIDDEN_KEYS:
                found.append(child_path)
            found.extend(_forbidden_key_paths(child, child_path))
        return found
    if isinstance(value, list):
        return [
            path
            for index, child in enumerate(value)
            for path in _forbidden_key_paths(child, f"{prefix}[{index}]")
        ]
    return []


def validate_owned_boundary_sources(repo_root: Path = REPO_ROOT) -> list[str]:
    """Check that the known CodexHub ownership boundary has not gained MCP config."""

    mismatches: list[str] = []
    for relative_path in OWNERSHIP_BOUNDARY_PATHS:
        path = repo_root / relative_path
        try:
            source = path.read_text(encoding="utf-8").lower()
        except OSError as error:
            mismatches.append(f"ownership boundary source unavailable: {relative_path}: {error}")
            continue
        for token in GLOBAL_MCP_CONFIGURATION_TOKENS:
            if token in source:
                mismatches.append(f"ownership boundary source configures global MCP token {token!r}: {relative_path}")
    return mismatches


def validate_evidence(evidence: Any) -> list[str]:
    """Return every replay-contract mismatch without exposing fixture values."""

    mismatches: list[str] = []
    root = _mapping(evidence, "evidence", mismatches)
    _expect_equal(_value(root, "schema_version", "evidence", mismatches), 1, "schema_version", mismatches)
    _expect_equal(
        _value(root, "capture_kind", "evidence", mismatches),
        "sanitized_task_creation_ab_evidence",
        "capture_kind",
        mismatches,
    )

    product_boundary = _mapping(_value(root, "product_boundary", "evidence", mismatches), "product_boundary", mismatches)
    _expect_equal(
        _value(product_boundary, "codexhub_manages_global_mcp", "product_boundary", mismatches),
        False,
        "product_boundary.codexhub_manages_global_mcp",
        mismatches,
    )
    _expect_equal(
        _value(product_boundary, "codexhub_has_bounded_app_server_model_probes", "product_boundary", mismatches),
        True,
        "product_boundary.codexhub_has_bounded_app_server_model_probes",
        mismatches,
    )
    _expect_equal(
        _value(product_boundary, "codexhub_exposes_native_task_lifecycle", "product_boundary", mismatches),
        False,
        "product_boundary.codexhub_exposes_native_task_lifecycle",
        mismatches,
    )
    _expect_equal(
        _value(product_boundary, "proven_link_from_model_probes_to_task_materialization", "product_boundary", mismatches),
        False,
        "product_boundary.proven_link_from_model_probes_to_task_materialization",
        mismatches,
    )
    _expect_equal(
        _value(product_boundary, "product_behavior_changed", "product_boundary", mismatches),
        False,
        "product_boundary.product_behavior_changed",
        mismatches,
    )

    red = _mapping(_value(root, "red_case", "evidence", mismatches), "red_case", mismatches)
    for key, expected in {
        "classification": "half_created",
        "client_placeholder_created": True,
        "worktree_provisioned": True,
        "rollout_materialized": False,
        "native_task_listing_contains_placeholder": False,
        "client_timeout_exceeded": True,
    }.items():
        _expect_equal(_value(red, key, "red_case", mismatches), expected, f"red_case.{key}", mismatches)
    red_operations = _mapping(
        _value(red, "supported_task_operations", "red_case", mismatches),
        "red_case.supported_task_operations",
        mismatches,
    )
    for operation, expected in {
        "read": "unavailable_no_session",
        "message": "unavailable_no_session",
        "rename": "unavailable_no_session",
        "archive": "rejected_no_session",
        "delete": "rejected_no_session",
    }.items():
        _expect_equal(
            _value(red_operations, operation, "red_case.supported_task_operations", mismatches),
            expected,
            f"red_case.supported_task_operations.{operation}",
            mismatches,
        )

    green = _mapping(_value(root, "green_case", "evidence", mismatches), "green_case", mismatches)
    _expect_equal(_value(green, "materialized", "green_case", mismatches), True, "green_case.materialized", mismatches)
    materialization_seconds = _value(green, "materialization_seconds", "green_case", mismatches)
    if not isinstance(materialization_seconds, (int, float)) or isinstance(materialization_seconds, bool):
        mismatches.append("green_case.materialization_seconds must be numeric")
    elif not 0 < materialization_seconds <= 15:
        mismatches.append("green_case.materialization_seconds must be within the bounded bootstrap window")
    green_bootstrap = _mapping(_value(green, "bootstrap", "green_case", mismatches), "green_case.bootstrap", mismatches)
    _expect_equal(
        _value(green_bootstrap, "global_openai_developer_docs_mcp_enabled", "green_case.bootstrap", mismatches),
        False,
        "green_case.bootstrap.global_openai_developer_docs_mcp_enabled",
        mismatches,
    )
    green_lifecycle = _mapping(_value(green, "lifecycle", "green_case", mismatches), "green_case.lifecycle", mismatches)
    for operation in ("create", "read", "message"):
        _expect_equal(
            _value(green_lifecycle, operation, "green_case.lifecycle", mismatches),
            "passed",
            f"green_case.lifecycle.{operation}",
            mismatches,
        )
    _expect_equal(
        _value(green, "permission_preflight", "green_case", mismatches),
        {"filesystem": "unrestricted", "network": "enabled", "approval": "never"},
        "green_case.permission_preflight",
        mismatches,
    )
    _expect_equal(
        _value(green, "git_preflight", "green_case", mismatches),
        "passed",
        "green_case.git_preflight",
        mismatches,
    )

    remote_control = _mapping(
        _value(root, "official_remote_control", "evidence", mismatches),
        "official_remote_control",
        mismatches,
    )
    _expect_equal(
        _value(remote_control, "read_existing_materialized_task", "official_remote_control", mismatches),
        "available",
        "official_remote_control.read_existing_materialized_task",
        mismatches,
    )
    _expect_equal(
        _value(remote_control, "create_replay", "official_remote_control", mismatches),
        "not_run_without_orchestrator_approval",
        "official_remote_control.create_replay",
        mismatches,
    )

    cleanup = _mapping(
        _value(root, "cleanup_observation", "evidence", mismatches), "cleanup_observation", mismatches
    )
    for key, expected in {
        "clean_orphan_worktrees_removed": 2,
        "client_held_empty_directory": True,
        "official_client_handle_release_required": True,
        "internal_codex_database_edited": False,
    }.items():
        _expect_equal(_value(cleanup, key, "cleanup_observation", mismatches), expected, f"cleanup_observation.{key}", mismatches)

    leak_detection = _mapping(
        _value(root, "leak_detection", "evidence", mismatches), "leak_detection", mismatches
    )
    _expect_equal(
        _value(leak_detection, "repeated_task_creation_runs", "leak_detection", mismatches),
        0,
        "leak_detection.repeated_task_creation_runs",
        mismatches,
    )
    _expect_equal(
        _value(leak_detection, "app_server_processes", "leak_detection", mismatches),
        "not_run_without_orchestrator_approval",
        "leak_detection.app_server_processes",
        mismatches,
    )
    _expect_equal(
        _value(leak_detection, "claim", "leak_detection", mismatches),
        "no_repeated_run_no_leak_claim",
        "leak_detection.claim",
        mismatches,
    )

    sanitization = _value(root, "sanitization", "evidence", mismatches)
    _expect_equal(sanitization, EXPECTED_SANITIZATION, "sanitization", mismatches)
    for path in _forbidden_key_paths(root):
        mismatches.append(f"sanitization forbids identifier or secret key at {path}")
    mismatches.extend(validate_owned_boundary_sources())
    return mismatches


def apply_replay_case(evidence: dict[str, Any], replay_case: str) -> None:
    if replay_case == "identity":
        return
    if replay_case == "materialize-red":
        evidence["red_case"]["rollout_materialized"] = True
        return
    if replay_case == "unmaterialize-green":
        evidence["green_case"]["materialized"] = False
        return
    if replay_case == "fail-git-preflight":
        evidence["green_case"]["git_preflight"] = "failed"
        return
    if replay_case == "skip-cleanup":
        evidence["cleanup_observation"]["clean_orphan_worktrees_removed"] = 1
        return
    if replay_case == "identifier-leak":
        evidence["red_case"]["task_id"] = "forbidden"
        return
    raise ValueError(f"unknown replay case: {replay_case}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path, default=DEFAULT_EVIDENCE_PATH)
    parser.add_argument(
        "--replay-case",
        choices=(
            "identity",
            "materialize-red",
            "unmaterialize-green",
            "fail-git-preflight",
            "skip-cleanup",
            "identifier-leak",
        ),
        default="identity",
    )
    args = parser.parse_args(argv)

    try:
        evidence = json.loads(args.evidence.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        print("TASK_CREATION_LIFECYCLE_MISMATCH: unable to read sanitized evidence", file=sys.stderr)
        return 1
    if not isinstance(evidence, dict):
        print("TASK_CREATION_LIFECYCLE_MISMATCH: evidence root must be an object", file=sys.stderr)
        return 1

    replay = deepcopy(evidence)
    apply_replay_case(replay, args.replay_case)
    mismatches = validate_evidence(replay)
    if mismatches:
        print("TASK_CREATION_LIFECYCLE_MISMATCH: " + " | ".join(mismatches), file=sys.stderr)
        return 1
    if args.replay_case != "identity":
        print(f"NEGATIVE_REPLAY_CONTROL_DID_NOT_FAIL: {args.replay_case}", file=sys.stderr)
        return 2

    print("Task creation A/B: half_created -> materialized")
    print("Live remote create replay: not run without Orchestrator approval")
    print("TASK_CREATION_LIFECYCLE_COMPLETE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
