from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

from scripts import qualify_beta3_protocol_cli as runner


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "qualify_beta3_protocol_cli.py"


def test_runner_summary_is_bounded_and_sanitized() -> None:
    summary = runner._summary("a" * 40, "codex-cli fixture", cases=[], status="not_run", failure="codex_cli_missing")

    encoded = json.dumps(summary, ensure_ascii=True)
    assert summary["schema"] == "codexhub.issue278.cli-tool-search.v1"
    assert summary["evidence_status"] == "observed_synthetic_upstream"
    assert summary["sanitization"] == {
        "raw_bodies_retained": False,
        "prompts_retained": False,
        "credentials_retained": False,
        "ids_opaque_or_hashed": True,
    }
    assert "fixture-target.txt" not in encoded
    assert "FIXTURE_COMPLETE" not in encoded
    assert "Authorization" not in encoded


def test_fixture_state_is_explicitly_protocol_controlled() -> None:
    native = runner.FixtureState("native_explicit")
    assert native.response_events(1)[1]["item"]["type"] == "tool_search_call"
    assert native.response_events(2)[1]["item"]["name"] == runner.DISCOVERED_TOOL_NAME
    assert runner.FixtureState("adapted_explicit").response_events(1)[1]["item"]["type"] == "function_call"


def test_cli_command_contains_isolation_and_strict_config_controls(monkeypatch, tmp_path) -> None:
    captured: dict[str, object] = {}

    class Completed:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake_run(command, **kwargs):
        captured["command"] = command
        return Completed()

    monkeypatch.setattr(subprocess, "run", fake_run)
    code, events = runner._run_cli(
        Path("codex.exe"),
        "native_no_hint",
        "opaque/model",
        tmp_path,
        tmp_path,
        1,
        {"CODEX_HOME": str(tmp_path)},
    )

    command = captured["command"]
    assert code == 0
    assert events == []
    assert isinstance(command, list)
    assert command[:2] == ["codex.exe", "exec"]
    assert "--json" in command
    assert "--ephemeral" in command
    assert "--strict-config" in command
    assert "approval_policy=never" in command
    assert "features.apps=false" in command
    assert "-s" in command and command[command.index("-s") + 1] == "workspace-write"


def test_provider_files_are_scoped_and_protocol_bound(tmp_path) -> None:
    runner._write_provider_files(tmp_path, 12345, 23456, adapted=True, model="volc/glm-5.2")

    providers = (tmp_path / "proxy" / "config" / "providers.toml").read_text(encoding="utf-8")
    config = (tmp_path / "config.toml").read_text(encoding="utf-8")
    catalog = json.loads((tmp_path / "model-catalogs" / "codexhub-model-catalog.json").read_text(encoding="utf-8"))

    assert 'base_url = "http://127.0.0.1:12345/v1"' in providers
    assert 'upstream_format = "responses"' in providers
    assert 'available_upstream_formats = ["responses"]' in providers
    assert 'tool_protocol = "chat_tools"' in providers
    assert 'base_url = "http://127.0.0.1:23456/v1"' in config
    assert catalog["models"][0]["slug"] == "volc/glm-5.2"
    assert catalog["models"][0]["codex_proxy_metadata"]["tool_protocol"] == "chat_tools"


def test_runner_missing_cli_writes_bounded_not_run_summary(tmp_path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--codex",
            str(tmp_path / "missing-codex.exe"),
            "--output",
            str(tmp_path / "out"),
            "--candidate-sha",
            "a" * 40,
            "--case",
            "native_no_hint",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert result.stderr == ""
    summary = json.loads((tmp_path / "out" / "summary.json").read_text(encoding="utf-8"))
    assert summary["qualification_status"] == "not_run"
    assert summary["failure"] == "codex_cli_missing"
    assert "fixture_no_hint" not in json.dumps(summary).lower()
