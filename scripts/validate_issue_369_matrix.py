#!/usr/bin/env python3
"""Validate the sanitized Official Collaboration capability matrix."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
MATRIX = ROOT / "docs" / "evidence" / "issue-369" / "official-v1-v2-cli-matrix.json"
SCHEMA = "codexhub.issue369.official-v1-v2-cli-matrix.v1"
SHA = re.compile(r"^[0-9a-f]{40}$")
FORBIDDEN = re.compile(
    r"(?i)(?:prompt|reasoning|credential|token|password|secret|session[_ -]?id|"
    r"call[_ -]?id|item[_ -]?id|agent[_ -]?id|task[_ -]?path|continuation[_ -]?id|"
    r"authorization|https?://|[a-z]:[\\/])"
)
REQUIRED_MODELS = {
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
GO_MODELS = {"gpt-5.6-luna"}
SELECTIONS = {
    "model_catalog_multi_agent_version",
    "not_qualified",
    "not_testable",
    "catalog_null",
    "never_exposed",
}
LIFECYCLES = {
    "spawn_send_wait_close",
    "spawn_list_send_followup_wait_list",
    "not_observed",
    "native_surface_unavailable",
    "incomplete_probe",
    "not_testable",
    "not_qualified",
}
COMPLETE_LIFECYCLES = {
    "v1": "spawn_send_wait_close",
    "v2": "spawn_list_send_followup_wait_list",
}
PHASE_FIELDS = (
    "spawn_identity_kind",
    "message_agent_message",
    "follow_up",
    "wait_result",
    "list_interrupt",
    "stream_history",
    "restart_readback",
    "terminal_error_replayable",
)
PHASE_STATUSES = {"observed", "not_observed", "not_applicable"}
IDENTITY_KINDS = {"agent_id", "task_path", "not_observed", "not_applicable"}


def _walk(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if FORBIDDEN.search(str(key)):
                raise ValueError("sensitive matrix key")
            _walk(child)
    elif isinstance(value, list):
        for child in value:
            _walk(child)
    elif isinstance(value, str) and value not in IDENTITY_KINDS and FORBIDDEN.search(value):
        raise ValueError("sensitive matrix value")


def _check_lifecycle(row: dict[str, Any], *, verdict: Any) -> None:
    """Validate bounded lifecycle evidence without inferring qualification."""

    complete_versions: set[str] = set()
    all_required_phases_pass = True
    for version, complete_lifecycle in COMPLETE_LIFECYCLES.items():
        evidence = row.get(version)
        if not isinstance(evidence, dict):
            raise ValueError("version evidence")
        if any(
            field not in evidence
            for field in ("selection", "lifecycle", "terminal_status", *PHASE_FIELDS)
        ):
            raise ValueError("lifecycle fields")
        selection = evidence.get("selection")
        lifecycle = evidence.get("lifecycle")
        terminal_status = evidence.get("terminal_status")
        if not isinstance(selection, str) or selection not in SELECTIONS:
            raise ValueError("lifecycle selection")
        if not isinstance(lifecycle, str) or lifecycle not in LIFECYCLES:
            raise ValueError("lifecycle phase")
        if terminal_status is not None and (
            isinstance(terminal_status, bool)
            or not isinstance(terminal_status, int)
            or terminal_status != 200
        ):
            raise ValueError("lifecycle terminal status")

        identity_kind = evidence.get("spawn_identity_kind")
        if not isinstance(identity_kind, str) or identity_kind not in IDENTITY_KINDS:
            raise ValueError("lifecycle identity kind")
        for field in PHASE_FIELDS[1:]:
            status = evidence.get(field)
            if not isinstance(status, str) or status not in PHASE_STATUSES:
                raise ValueError("lifecycle phase status")

        expected_identity_kind = "agent_id" if version == "v1" else "task_path"
        if (
            selection == "model_catalog_multi_agent_version"
            and lifecycle == complete_lifecycle
            and terminal_status == 200
            and identity_kind not in {expected_identity_kind, "not_observed"}
        ):
            raise ValueError("lifecycle identity kind")

        required_statuses = [evidence[field] for field in PHASE_FIELDS[1:]]
        version_phases_pass = all(
            status == "observed"
            or (
                version == "v1"
                and field in {"follow_up", "list_interrupt"}
                and status == "not_applicable"
            )
            for field, status in zip(PHASE_FIELDS[1:], required_statuses)
        )
        if not version_phases_pass:
            all_required_phases_pass = False

        complete_lifecycle_observed = (
            selection == "model_catalog_multi_agent_version"
            and lifecycle == complete_lifecycle
            and terminal_status == 200
        )
        if complete_lifecycle_observed:
            complete_versions.add(version)

        if verdict == "GO":
            if selection != "model_catalog_multi_agent_version":
                raise ValueError("GO selection evidence")
            if lifecycle != complete_lifecycle or terminal_status != 200 or not version_phases_pass:
                raise ValueError("GO lifecycle evidence")

    # A row may have a complete coarse probe for one version while the other
    # version remains unavailable or incomplete.  A non-GO row is inconsistent
    # only when both versions also claim every required phase as observed.
    if verdict == "GO" and not all_required_phases_pass:
        raise ValueError("GO lifecycle evidence")
    if verdict == "GO" and complete_versions != set(COMPLETE_LIFECYCLES):
        raise ValueError("GO lifecycle evidence")
    if verdict == "PARTIAL" and all_required_phases_pass:
        raise ValueError("PARTIAL complete lifecycle evidence")
    if (
        verdict not in {"GO", "PARTIAL"}
        and complete_versions == set(COMPLETE_LIFECYCLES)
        and all_required_phases_pass
    ):
        raise ValueError("non-GO complete lifecycle evidence")


def validate(path: Path = MATRIX, candidate_sha: str | None = None) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != SCHEMA:
        raise ValueError("schema")
    if payload.get("evidence_status") != "authenticated_cli_sanitized":
        raise ValueError("evidence_status")
    candidate_revision = payload.get("candidate_revision", "")
    if not isinstance(candidate_revision, str) or SHA.fullmatch(candidate_revision) is None:
        raise ValueError("candidate_revision")
    if candidate_sha is not None:
        if not isinstance(candidate_sha, str) or SHA.fullmatch(candidate_sha) is None:
            raise ValueError("candidate_sha_invalid")
        if candidate_sha != candidate_revision:
            raise ValueError("candidate_sha_mismatch")
    _walk(payload)
    evidence_links = payload.get("evidence_links")
    if (
        not isinstance(evidence_links, list)
        or not evidence_links
        or any(
            not isinstance(link, str) or not link.startswith("docs/")
            for link in evidence_links
        )
    ):
        raise ValueError("evidence links")
    policy = payload.get("selector_policy", {})
    if not isinstance(policy, dict):
        raise ValueError("selector_policy")
    if (
        policy.get("required_visibility") != "list"
        or policy.get("required_verdict") != "GO"
        or policy.get("required_versions") != ["v1", "v2"]
        or policy.get("model_scoped") is not True
        or policy.get("global_switch") is not False
        or policy.get("third_party_rows_changed") is not False
    ):
        raise ValueError("selector_policy")
    rows = payload.get("models")
    if (
        not isinstance(rows, list)
        or len(rows) != len(REQUIRED_MODELS)
        or any(not isinstance(row, dict) for row in rows)
    ):
        raise ValueError("model coverage")
    if (
        any(not isinstance(row.get("model"), str) for row in rows)
        or {row.get("model") for row in rows} != REQUIRED_MODELS
    ):
        raise ValueError("model coverage")
    for row in rows:
        model = row["model"]
        verdict = row.get("verdict")
        if not isinstance(verdict, str) or verdict not in {"GO", "PARTIAL", "NO-GO", "UNQUALIFIED"}:
            raise ValueError("verdict")
        selector = row.get("selector") is True
        if selector != (model in GO_MODELS and row.get("visibility") == "list" and verdict == "GO"):
            raise ValueError("selector must fail closed")
        if verdict == "GO" and model not in GO_MODELS:
            raise ValueError("GO model not qualified")
        if verdict == "GO" and row.get("visibility") != "list":
            raise ValueError("GO row not visible")
        _check_lifecycle(row, verdict=verdict)
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--matrix", type=Path, default=MATRIX)
    parser.add_argument(
        "--candidate-sha",
        help="require the matrix candidate_revision to equal this lowercase 40-hex SHA",
    )
    args = parser.parse_args(argv)
    try:
        validate(args.matrix, candidate_sha=args.candidate_sha)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as error:
        print(f"ISSUE_369_MATRIX_INVALID:{error}")
        return 1
    print("ISSUE_369_MATRIX_OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
