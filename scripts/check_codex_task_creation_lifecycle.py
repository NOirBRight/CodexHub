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
import re
import sys
from typing import Any


class NumberRange:
    """A numeric schema leaf with an exclusive lower and inclusive upper bound."""

    def __init__(self, minimum: float, maximum: float) -> None:
        self.minimum = minimum
        self.maximum = maximum


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EVIDENCE_PATH = (
    REPO_ROOT / "docs" / "evidence" / "issue-106" / "task-creation-lifecycle.json"
)
OWNERSHIP_BOUNDARY_PATHS = (
    Path("src-python/config_overlay.py"),
    Path("src-python/catalog_sync.py"),
    Path("src-tauri/src/catalog.rs"),
    Path("src-tauri/src/config.rs"),
    Path("src-tauri/src/proxy.rs"),
    Path("src-tauri/src/models.rs"),
    Path("src-tauri/src/openai_usage.rs"),
)
APP_SERVER_PROBE_MARKERS = {
    Path("src-tauri/src/models.rs"): 'args(["app-server", "--stdio"])',
    Path("src-tauri/src/openai_usage.rs"): 'args(["app-server", "--stdio"])',
}
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

EVIDENCE_SCHEMA: dict[str, Any] = {
    "schema_version": 1,
    "capture_kind": "sanitized_task_creation_ab_evidence",
    "source": {
        "evidence_type": "observed A/B plus read-only repository inspection",
        "provenance": {
            "observation_basis": "orchestrator_observed_ab",
            "repository_inspection": "read_only",
            "live_task_creation": "not_authorized",
        },
        "conclusion_limit": (
            "This fixture verifies the retained structural facts. It is not a live Task-create replay "
            "and does not establish a repeated-run process-leak result."
        ),
    },
    "product_boundary": {
        "codexhub_manages_global_mcp": False,
        "codexhub_has_bounded_app_server_model_probes": True,
        "bounded_app_server_probe_sources": [
            "src-tauri/src/models.rs",
            "src-tauri/src/openai_usage.rs",
        ],
        "codexhub_exposes_native_task_lifecycle": False,
        "proven_link_from_model_probes_to_task_materialization": False,
        "product_behavior_changed": False,
    },
    "red_case": {
        "classification": "half_created",
        "client_placeholder_created": True,
        "worktree_provisioned": True,
        "rollout_materialized": False,
        "native_task_listing_contains_placeholder": False,
        "client_timeout_exceeded": True,
        "supported_task_operations": {
            "read": "unavailable_no_session",
            "message": "unavailable_no_session",
            "rename": "unavailable_no_session",
            "archive": "rejected_no_session",
            "delete": "rejected_no_session",
        },
    },
    "green_case": {
        "bootstrap": {
            "global_openai_developer_docs_mcp_enabled": False,
            "profile": "short_low_cost",
        },
        "materialized": True,
        "materialization_seconds": NumberRange(0, 15),
        "lifecycle": {
            "create": "passed",
            "read": "passed",
            "message": "passed",
        },
        "permission_preflight": {
            "filesystem": "unrestricted",
            "network": "enabled",
            "approval": "never",
        },
        "git_preflight": "passed",
    },
    "official_remote_control": {
        "read_existing_materialized_task": "available",
        "create_replay": "not_run_without_orchestrator_approval",
    },
    "cleanup_observation": {
        "clean_orphan_worktrees_removed": 2,
        "client_held_empty_directory": True,
        "official_client_handle_release_required": True,
        "internal_codex_database_edited": False,
    },
    "leak_detection": {
        "repeated_task_creation_runs": 0,
        "app_server_processes": "not_run_without_orchestrator_approval",
        "claim": "no_repeated_run_no_leak_claim",
    },
    "sanitization": {
        "contains_credentials": False,
        "contains_local_paths": False,
        "contains_prompts_or_messages": False,
        "contains_task_or_session_identifiers": False,
    },
}


def _schema_field_names(schema: Any) -> frozenset[str]:
    if not isinstance(schema, dict):
        return frozenset()
    return frozenset(schema).union(
        *(_schema_field_names(child) for child in schema.values())
    )


ALLOWED_EVIDENCE_FIELD_NAMES = _schema_field_names(EVIDENCE_SCHEMA)
WINDOWS_LOCAL_PATH_PATTERN = re.compile(
    r"(?:\b[a-z]:[\\/]|\\\\[^\\/\r\n]+[\\/]|%(?:userprofile|appdata|localappdata|home)%)",
    re.IGNORECASE,
)
POSIX_LOCAL_PATH_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_.-])/(?:users|home|tmp|var|private|mnt)(?:/|$)",
    re.IGNORECASE,
)
TASK_OR_SESSION_IDENTIFIER_PATTERN = re.compile(
    r"(?:\b(?:client[_-]?thread|task|thread|session|rollout)(?:[_-]?id)?[-_:]?[0-9a-f]{8,}\b|"
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b)",
    re.IGNORECASE,
)
CREDENTIAL_SHAPE_PATTERN = re.compile(
    r"(?:\b(?:sk|ghp|gho|github_pat)_[a-z0-9_-]+\b|\bbearer\s+\S+|"
    r"\b(?:api[_ -]?key|authorization|credential|secret|password|token)\b)",
    re.IGNORECASE,
)


def _require_object(
    value: Any, label: str, mismatches: list[str]
) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    mismatches.append(f"{label} must be an object")
    return None


def _require_exact_fields(
    mapping: dict[str, Any],
    expected_fields: frozenset[str],
    label: str,
    mismatches: list[str],
) -> None:
    missing_fields = sorted(expected_fields - mapping.keys())
    if missing_fields:
        mismatches.append(f"{label} has missing fields: {', '.join(missing_fields)}")
    if set(mapping) - expected_fields:
        mismatches.append(f"{label} has unexpected fields")


def _expect_equal(
    actual: Any, expected: Any, label: str, mismatches: list[str]
) -> None:
    if actual != expected:
        mismatches.append(f"{label} did not match the expected contract")


def _schema_child_label(label: str, key: str) -> str:
    return key if label == "evidence" else f"{label}.{key}"


def _validate_schema(
    value: Any, schema: Any, label: str, mismatches: list[str]
) -> None:
    if isinstance(schema, dict):
        mapping = _require_object(value, label, mismatches)
        if mapping is None:
            return
        _require_exact_fields(mapping, frozenset(schema), label, mismatches)
        for key, child_schema in schema.items():
            if key in mapping:
                _validate_schema(
                    mapping[key],
                    child_schema,
                    _schema_child_label(label, key),
                    mismatches,
                )
        return

    if isinstance(schema, NumberRange):
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            mismatches.append(f"{label} must be numeric")
        elif not schema.minimum < value <= schema.maximum:
            mismatches.append(f"{label} must be within the bounded bootstrap window")
        return

    _expect_equal(value, schema, label, mismatches)


def _safe_child_path(prefix: str, key: object) -> str:
    if isinstance(key, str) and key in ALLOWED_EVIDENCE_FIELD_NAMES:
        return f"{prefix}.{key}"
    return f"{prefix}.<unexpected>"


def _forbidden_key_count(value: Any) -> int:
    if isinstance(value, dict):
        found = 0
        for key, child in value.items():
            if isinstance(key, str) and key.casefold() in FORBIDDEN_KEYS:
                found += 1
            found += _forbidden_key_count(child)
        return found
    if isinstance(value, list):
        return sum(_forbidden_key_count(child) for child in value)
    return 0


def _unsafe_string_mismatches(value: Any, prefix: str = "$") -> list[str]:
    if isinstance(value, dict):
        return [
            mismatch
            for key, child in value.items()
            for mismatch in _unsafe_string_mismatches(
                child, _safe_child_path(prefix, key)
            )
        ]
    if isinstance(value, list):
        return [
            mismatch
            for index, child in enumerate(value)
            for mismatch in _unsafe_string_mismatches(child, f"{prefix}[{index}]")
        ]
    if not isinstance(value, str):
        return []

    mismatches: list[str] = []
    if WINDOWS_LOCAL_PATH_PATTERN.search(value) or POSIX_LOCAL_PATH_PATTERN.search(
        value
    ):
        mismatches.append(f"unsafe local path string at {prefix}")
    if TASK_OR_SESSION_IDENTIFIER_PATTERN.search(value):
        mismatches.append(f"unsafe task or session identifier string at {prefix}")
    if CREDENTIAL_SHAPE_PATTERN.search(value):
        mismatches.append(f"unsafe credential-like string at {prefix}")
    return mismatches


def validate_owned_boundary_sources(repo_root: Path = REPO_ROOT) -> list[str]:
    """Check the bounded CodexHub source boundary without exposing host paths."""

    mismatches: list[str] = []
    for relative_path in OWNERSHIP_BOUNDARY_PATHS:
        path = repo_root / relative_path
        try:
            source = path.read_text(encoding="utf-8").lower()
        except OSError:
            mismatches.append(
                f"ownership boundary source unavailable: {relative_path.as_posix()}"
            )
            continue
        for token in GLOBAL_MCP_CONFIGURATION_TOKENS:
            if token in source:
                mismatches.append(
                    "ownership boundary source configures global MCP token "
                    f"{token!r}: {relative_path.as_posix()}"
                )
        marker = APP_SERVER_PROBE_MARKERS.get(relative_path)
        if marker is not None and marker.lower() not in source:
            mismatches.append(
                "ownership boundary source missing bounded app-server probe: "
                f"{relative_path.as_posix()}"
            )
    return mismatches


def validate_evidence(
    evidence: Any, repo_root: Path = REPO_ROOT
) -> list[str]:
    """Return every contract mismatch without exposing untrusted fixture values."""

    mismatches: list[str] = []
    _validate_schema(evidence, EVIDENCE_SCHEMA, "evidence", mismatches)
    if _forbidden_key_count(evidence):
        mismatches.append("sanitization forbids sensitive field names")
    mismatches.extend(_unsafe_string_mismatches(evidence))
    mismatches.extend(validate_owned_boundary_sources(repo_root))
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
        print(
            "TASK_CREATION_LIFECYCLE_MISMATCH: unable to read sanitized evidence",
            file=sys.stderr,
        )
        return 1
    if not isinstance(evidence, dict):
        print(
            "TASK_CREATION_LIFECYCLE_MISMATCH: evidence root must be an object",
            file=sys.stderr,
        )
        return 1

    replay = deepcopy(evidence)
    apply_replay_case(replay, args.replay_case)
    mismatches = validate_evidence(replay)
    if mismatches:
        print(
            "TASK_CREATION_LIFECYCLE_MISMATCH: " + " | ".join(mismatches),
            file=sys.stderr,
        )
        return 1
    if args.replay_case != "identity":
        print(
            f"NEGATIVE_REPLAY_CONTROL_DID_NOT_FAIL: {args.replay_case}",
            file=sys.stderr,
        )
        return 2

    print("Task creation A/B: half_created -> materialized")
    print("Live remote create replay: not run without Orchestrator approval")
    print("TASK_CREATION_LIFECYCLE_COMPLETE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
