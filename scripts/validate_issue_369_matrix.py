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
FORBIDDEN = re.compile(r"(?i)(?:prompt|reasoning|credential|token|password|secret|session[_ -]?id|call[_ -]?id|item[_ -]?id|authorization|https?://|[a-z]:[\\/])")
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
GO_MODELS = {"gpt-5.6-terra", "gpt-5.6-luna"}
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


def _walk(value: Any) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if FORBIDDEN.search(str(key)):
                raise ValueError("sensitive matrix key")
            _walk(child)
    elif isinstance(value, list):
        for child in value:
            _walk(child)
    elif isinstance(value, str) and FORBIDDEN.search(value):
        raise ValueError("sensitive matrix value")


def _check_lifecycle(row: dict[str, Any], *, verdict: Any) -> None:
    """Validate bounded lifecycle evidence without inferring qualification."""

    complete_versions: set[str] = set()
    for version, complete_lifecycle in COMPLETE_LIFECYCLES.items():
        evidence = row.get(version)
        if not isinstance(evidence, dict):
            raise ValueError("version evidence")
        if any(field not in evidence for field in ("selection", "lifecycle", "terminal_status")):
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

        if verdict == "GO":
            if selection != "model_catalog_multi_agent_version":
                raise ValueError("GO selection evidence")
            if lifecycle != complete_lifecycle or terminal_status != 200:
                raise ValueError("GO lifecycle evidence")
        elif (
            selection == "model_catalog_multi_agent_version"
            and lifecycle == complete_lifecycle
            and terminal_status == 200
        ):
            complete_versions.add(version)

    # A row may have a complete probe for one version while the other version
    # remains unavailable or incomplete.  Only reject a non-GO row when both
    # required version lifecycles are complete; that would be an inconsistent
    # model-level verdict rather than a partial qualification result.
    if verdict != "GO" and complete_versions == set(COMPLETE_LIFECYCLES):
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
        if not isinstance(verdict, str) or verdict not in {"GO", "NO-GO", "UNQUALIFIED"}:
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
