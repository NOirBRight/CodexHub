from __future__ import annotations

import json
import os
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import qualify_authenticated_provider_cli as runner


def test_chat_cell_matrix_is_generic_and_complete() -> None:
    assert {cell.protocol for cell in runner.CELLS} == {"chat_completions"}
    assert {cell.model.model for cell in runner.CELLS} == {
        "glm-5.2",
        "kimi-k2.7-code",
        "deepseek-v4-flash:0731",
    }
    assert len(runner.CELLS) == 3
    assert len({cell.key for cell in runner.CELLS}) == 3


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
            "reasoning_policy": "explicit",
            "request_id": "must-not-survive",
        },
        {
            "event": "chat_stream_shape_summary",
            "chunk_count": 4,
            "delta_source_count": 3,
            "text_chars": 19,
            "request_body": "must-not-survive",
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
        "category": "client_cancellation",
    }]
    assert value["reasoning_policies"] == ["explicit"]
    assert value["streaming"] == {
        "summary_count": 1,
        "max_chunk_count": 4,
        "text_delta_source_count": 3,
        "progressive_text_stream_count": 1,
    }
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






def test_session_tool_evidence_requires_exact_call_result_and_task_identity(tmp_path: Path) -> None:
    session_dir = tmp_path / "sessions"
    session_dir.mkdir()
    values: list[dict[str, object]] = []

    def lifecycle(item_type: str, name: str, call_id: str, output: str, namespace: str | None = None) -> None:
        call: dict[str, object] = {"type": item_type, "name": name, "call_id": call_id}
        if namespace is not None:
            call["namespace"] = namespace
        output_type = "custom_tool_call_output" if item_type == "custom_tool_call" else "function_call_output"
        values.extend((call, {"type": output_type, "call_id": call_id, "output": output}))

    lifecycle("function_call", "exec_command", "exec-1", "ok")
    lifecycle("custom_tool_call", "apply_patch", "patch-1", "Done!")
    results = {
        "spawn_agent": json.dumps({"task_name": "/root/provider_worker"}),
        "wait_agent": json.dumps({"message": "CHILD_DONE", "timed_out": False}),
        "send_message": "",
        "followup_task": "",
        "list_agents": json.dumps(
            {"agents": [{"agent_name": "/root/provider_worker", "agent_status": "running"}]}
        ),
        "interrupt_agent": json.dumps({"previous_status": "running"}),
    }
    for index, name in enumerate(runner.EXPECTED_V2_SEQUENCE):
        lifecycle("function_call", name, f"collab-{index}", results[name], "collaboration")
    values.append(
        {
            "type": "agent_message",
            "author": "/root/provider_worker",
            "recipient": "/root",
            "content": [],
        }
    )
    (session_dir / "rollout.jsonl").write_text(
        chr(10).join(json.dumps(value) for value in values) + chr(10), encoding="utf-8"
    )

    evidence = runner._session_tool_evidence(tmp_path)
    assert evidence == {
        "exec_command_call_count": 1,
        "exec_command_identity_preserved": True,
        "apply_patch_call_count": 1,
        "apply_patch_identity_preserved": True,
        "collaboration_sequence": list(runner.EXPECTED_V2_SEQUENCE),
        "collaboration_call_count": 6,
        "collaboration_history_identity_preserved": True,
        "canonical_task_identity_observed": True,
        "child_result_delivery_observed": True,
        "wait_result_shape_valid": True,
        "list_result_shape_valid": True,
        "interrupt_result_shape_valid": True,
    }


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
