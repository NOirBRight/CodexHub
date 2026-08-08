#!/usr/bin/env python3
"""Validate the sanitized Official Collaboration capability matrix."""

from __future__ import annotations

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
GO_MODELS = {"gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"}


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


def validate(path: Path = MATRIX) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema") != SCHEMA:
        raise ValueError("schema")
    if payload.get("evidence_status") != "authenticated_cli_sanitized":
        raise ValueError("evidence_status")
    if not SHA.fullmatch(payload.get("candidate_revision", "")):
        raise ValueError("candidate_revision")
    _walk(payload)
    policy = payload.get("selector_policy", {})
    if policy.get("required_visibility") != "list" or policy.get("required_verdict") != "GO":
        raise ValueError("selector_policy")
    rows = payload.get("models")
    if not isinstance(rows, list) or {row.get("model") for row in rows} != REQUIRED_MODELS:
        raise ValueError("model coverage")
    for row in rows:
        model = row["model"]
        verdict = row.get("verdict")
        selector = row.get("selector") is True
        if selector != (model in GO_MODELS and row.get("visibility") == "list" and verdict == "GO"):
            raise ValueError("selector must fail closed")
        for version in ("v1", "v2"):
            if version not in row or not isinstance(row[version], dict):
                raise ValueError("version evidence")
    return payload


if __name__ == "__main__":
    validate()
    print("ISSUE_369_MATRIX_OK")
