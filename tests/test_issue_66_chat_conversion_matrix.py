from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import protocol_translation


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build_issue_66_chat_conversion_matrix.py"
MATRIX = ROOT / "docs" / "evidence" / "issue-66" / "chat-conversion-matrix.json"
SCHEMA = "codexhub.issue66.chat-conversion-matrix.v1"
V2_TOOLS = {
    "spawn_agent",
    "send_message",
    "followup_task",
    "wait_agent",
    "interrupt_agent",
    "list_agents",
}


def _load() -> dict:
    return json.loads(MATRIX.read_text(encoding="utf-8"))


def _python() -> str:
    return sys.executable


def test_matrix_artifact_reconciles() -> None:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src-python")
    completed = subprocess.run(
        [_python(), str(SCRIPT), "--check"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    report = json.loads(completed.stdout)
    assert report["schema"] == SCHEMA
    assert report["reconciled"] is True


def test_matrix_covers_required_chat_and_v2_surface() -> None:
    payload = _load()
    assert payload["schema"] == SCHEMA
    assert payload["artifact_kind"] == "chat_conversion_matrix"
    assert set(payload["v2_tools"]) == V2_TOOLS
    rows = {row["id"]: row for row in payload["rows"]}
    assert "request.unknown_field" in rows
    assert "item.unknown" in rows
    assert "item.function_call" in rows
    assert "stream.text.delta" in rows
    assert "stream.terminal.completed" in rows
    assert "identity.call_id" in rows
    assert "v2.namespace" in rows
    for name in V2_TOOLS:
        assert rows[f"v2.tool.{name}"]["disposition"] == "reversibly_adapted"
        assert rows[f"v2.tool.{name}"]["fail_closed"] is True
    assert rows["request.tools.custom"]["disposition"] == "reversibly_adapted"
    assert rows["request.tools.custom.unavailable"]["disposition"] == "unavailable"
    assert rows["request.tools.hosted"]["disposition"] == "unavailable"
    assert rows["request.reasoning.controls"]["disposition"] == "consumed_locally"
    assert all(
        key in rows["request.reasoning.controls"]["responses"]
        for key in ("effort", "summary", "generate_summary", "mode", "context")
    )
    assert rows["v2.encrypted_fields"]["disposition"] == "unavailable"
    assert payload["invariants"]["fail_closed_unknown"] is True
    assert payload["invariants"]["not_keyed_by"] == ["model_name", "provider_name"]
    assert "official_openai" in payload["invariants"]["hidden_fallback_forbidden"]


def test_unavailable_rows_fail_closed_and_no_model_name_keys() -> None:
    payload = _load()
    for row in payload["rows"]:
        if row["disposition"] == "unavailable":
            assert row["fail_closed"] is True
        assert "glm" not in row["id"].lower()
        assert "k2" not in row["id"].lower()
        assert "deepseek" not in row["id"].lower()


def test_protocol_translation_matches_empty_transport_defaults() -> None:
    body = protocol_translation.responses_request_to_chat_completion_body(
        json.dumps(
            {
                "model": "placeholder",
                "input": [{"type": "message", "role": "user", "content": "hi"}],
                "client_metadata": {},
                "include": [],
                "prompt_cache_key": "",
                "store": False,
                "text": {},
            }
        ).encode("utf-8")
    )
    payload = json.loads(body)
    assert payload["model"] == "placeholder"
    assert payload["messages"][0]["role"] == "user"


def test_protocol_translation_rejects_unknown_request_fields() -> None:
    try:
        protocol_translation.responses_request_to_chat_completion_body(
            json.dumps(
                {
                    "model": "placeholder",
                    "input": "hi",
                    "mystery_field": True,
                }
            ).encode("utf-8")
        )
    except protocol_translation.UnsupportedProtocolTranslationError as error:
        assert error.code == "unsupported_protocol_semantics"
        return
    raise AssertionError("expected unknown Responses fields to fail closed")


def test_protocol_translation_rejects_store_true() -> None:
    try:
        protocol_translation.responses_request_to_chat_completion_body(
            json.dumps(
                {
                    "model": "placeholder",
                    "input": "hi",
                    "store": True,
                }
            ).encode("utf-8")
        )
    except protocol_translation.UnsupportedProtocolTranslationError:
        return
    raise AssertionError("expected store=true to fail closed")
