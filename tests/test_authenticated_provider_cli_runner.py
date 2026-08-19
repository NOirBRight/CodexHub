from __future__ import annotations

import json
import os
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import qualify_authenticated_provider_cli as runner


def test_cell_matrix_is_generic_and_complete() -> None:
    assert {cell.protocol for cell in runner.CELLS} == {"responses", "chat_completions"}
    assert {cell.model.model for cell in runner.CELLS} == {
        "glm-5.2",
        "kimi-k2.7-code",
        "deepseek-v4-flash:0731",
    }
    assert len(runner.CELLS) == 6
    assert len({cell.key for cell in runner.CELLS}) == 6


def test_chat_provider_config_uses_only_secret_placeholder_and_generic_adapters() -> None:
    cell = runner.CELL_BY_KEY["chat_completions:glm-5.2"]
    value = runner._provider_config(cell, "https://ollama.example/v1")

    assert f"{{env:{runner.QUALIFICATION_KEY_ENV}}}" in value
    assert "secret-value" not in value
    assert 'upstream_format = "chat_completions"' in value
    assert "native_responses_tool_codec" not in value
    assert "accepts_namespace_adapter = true" in value
    assert "accepts_custom_adapter = true" in value
    assert 'id = "glm-5.2"' in value


def test_responses_glm_config_preserves_maintained_codec() -> None:
    value = runner._provider_config(
        runner.CELL_BY_KEY["responses:glm-5.2"],
        "https://ollama.example/v1",
    )

    assert 'upstream_format = "responses"' in value
    assert 'native_responses_tool_codec = "strict_apply_patch"' in value
    assert 'tool_surface_strategy = "deferred_core"' in value
    assert "namespace_lifecycle = true" in value


def test_credential_resolution_never_accepts_placeholder(tmp_path: Path) -> None:
    source = tmp_path / "providers.toml"
    source.write_text(
        '''[[providers]]
id = "ollama-cloud"
base_url = "https://ollama.example/v1"
api_key = "live-secret"
''',
        encoding="utf-8",
    )
    assert runner._credential_provider(source) == (
        "live-secret",
        "https://ollama.example/v1",
    )

    source.write_text(
        '''[[providers]]
id = "ollama-cloud"
base_url = "https://ollama.example/v1"
api_key = "{env:OLLAMA_API_KEY}"
''',
        encoding="utf-8",
    )
    with pytest.raises(runner.QualificationFailure, match="credential_unresolved"):
        runner._credential_provider(source)


def test_qualification_secret_is_scoped_and_restored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(runner.QUALIFICATION_KEY_ENV, "previous")
    with runner._qualification_secret("temporary"):
        assert os.environ[runner.QUALIFICATION_KEY_ENV] == "temporary"
    assert os.environ[runner.QUALIFICATION_KEY_ENV] == "previous"


def test_gateway_summary_retains_only_bounded_route_and_failure_fields(tmp_path: Path) -> None:
    log = tmp_path / "events.jsonl"
    events = [
        {
            "event": "request_start",
            "route_provider_id": "ollama-cloud",
            "route_upstream_model": "glm-5.2",
            "executed_upstream_protocol": "chat_completions",
            "request_id": "must-not-survive",
        },
        {
            "event": "request_complete",
            "status": 200,
            "request_body": "must-not-survive",
        },
        {
            "event": "downstream_stream_closed",
            "status": 499,
            "failure_class": "downstream_client_closed",
            "failure_phase": "downstream_write",
            "detail": "must-not-survive",
        },
    ]
    log.write_text(
        chr(10).join(json.dumps(event) for event in events) + chr(10),
        encoding="utf-8",
    )

    value = runner._gateway_summary(log)
    serialized = json.dumps(value, sort_keys=True)
    assert value["providers"] == ["ollama-cloud"]
    assert value["models"] == ["glm-5.2"]
    assert value["protocols"] == ["chat_completions"]
    assert value["failures"] == [{
        "event": "downstream_stream_closed",
        "status": 499,
        "failure_class": "downstream_client_closed",
        "failure_phase": "downstream_write",
    }]
    assert "must-not-survive" not in serialized




def test_session_structural_evidence_keeps_names_not_identities(tmp_path: Path) -> None:
    session_dir = tmp_path / "sessions" / "2026"
    session_dir.mkdir(parents=True)
    values = [
        {
            "payload": {
                "type": "function_call",
                "namespace": "collaboration",
                "name": "spawn_agent",
                "call_id": "opaque-call",
            }
        },
        {
            "payload": {
                "type": "agent_message",
                "id": "opaque-message",
                "author": "/root/worker",
                "recipient": "/root",
                "content": [],
            }
        },
    ]
    (session_dir / "rollout.jsonl").write_text(
        chr(10).join(json.dumps(value) for value in values) + chr(10),
        encoding="utf-8",
    )

    assert runner._session_collaboration_counts(tmp_path) == {"spawn_agent": 1}
    assert runner._session_agent_message_count(tmp_path) == 1


def test_capture_writes_bounded_summary_without_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "qualification-secret-must-not-survive"
    monkeypatch.setattr(
        runner,
        "_credential_provider",
        lambda _path: (secret, "https://ollama.example/v1"),
    )
    monkeypatch.setattr(
        runner,
        "_run_cell",
        lambda cell, _scenarios, _secret, _base_url: {
            "cell": cell.key,
            "provider_id": runner.PROVIDER_ID,
            "model": cell.model.model,
            "protocol": cell.protocol,
            "status": "passed",
        },
    )
    output = tmp_path / "evidence"
    result = runner.capture(
        cells=(runner.CELL_BY_KEY["chat_completions:glm-5.2"],),
        scenarios=("identity_text",),
        credential_source=tmp_path / "ignored.toml",
        output_dir=output,
        candidate_sha="a" * 40,
        cli_version="codex-cli 0.147.0",
    )

    serialized = (output / "summary.json").read_text(encoding="utf-8")
    assert result["qualification_status"] == "passed"
    assert secret not in serialized
    assert "https://ollama.example" not in serialized
    assert result["sanitization"]["credentials_retained"] is False
