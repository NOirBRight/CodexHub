import json
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_issue_108_lifecycle_replay_stops_only_the_retained_process_tree(tmp_path):
    powershell = shutil.which("powershell.exe")
    if powershell is None:
        pytest.skip("Windows PowerShell is required for the lifecycle replay")

    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ROOT / "scripts" / "qualify-issue-108-glm-tool-surface.ps1"),
            "-LifecycleReplay",
            "-OutputDir",
            str(tmp_path),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=20,
    )

    assert result.returncode == 0, result.stderr
    summaries = list(tmp_path.glob("run-*/summary.json"))
    assert len(summaries) == 1
    summary = json.loads(summaries[0].read_text(encoding="utf-8-sig"))
    assert summary["mode"] == "lifecycle_replay"
    assert summary["passed"] is True
    assert summary["failures"] == []
    assert summary["tracked_root_exited"] is True
    assert summary["tracked_child_exited"] is True
    assert summary["tracked_child_exit_before_natural_timeout"] is True


def test_issue_108_qualification_has_no_harness_history_bridge():
    source = (
        ROOT / "scripts" / "qualify-issue-108-glm-tool-surface.ps1"
    ).read_text(encoding="utf-8-sig")

    assert "StructuredApplyPatchHistoryBridge" not in source
    assert "CODEXHUB_ENABLE_APPLY_PATCH_HISTORY_BRIDGE" not in source
    assert "apply_patch_history_bridge" not in source


def test_issue_108_qualification_requires_history_adapter_evidence():
    source = (
        ROOT / "scripts" / "qualify-issue-108-glm-tool-surface.ps1"
    ).read_text(encoding="utf-8-sig")

    assert "third_party_apply_patch_freeform_history_adapter" in source
    assert "apply_patch_history_adapter_outcomes" in source
    assert "apply_patch_adapter_adapted_count" in source
    assert "apply_patch_history_adapter_adapted_count" in source
    assert "history adapter never reported adapted" in source


def test_codex_tool_smoke_prefers_app_cli_and_runs_ephemeral():
    source = (ROOT / "scripts" / "codex-tool-exposure-smoke.ps1").read_text(encoding="utf-8-sig")

    assert "[string]$CodexCommand = ''" in source
    assert "[string]$OfficialProxyModel = 'gpt-5.5'" in source
    assert "function Resolve-CodexCommand" in source
    assert "OpenAI\\Codex\\bin" in source
    assert "codex.exe" in source
    assert "codex.cmd" in source
    assert "'--ephemeral'" in source


def test_codex_tool_smoke_launches_command_shims_through_cmd_exe():
    source = (ROOT / "scripts" / "codex-tool-exposure-smoke.ps1").read_text(encoding="utf-8-sig")

    assert "function Test-CodexCommandShim" in source
    assert "cmd.exe" in source
    assert "@('/d', '/s', '/c')" in source
    assert "ConvertTo-ProcessArgument $CodexCommand" in source


def test_codex_tool_smoke_requires_exact_completed_child_status():
    source = (ROOT / "scripts" / "codex-tool-exposure-smoke.ps1").read_text(encoding="utf-8-sig")

    assert "$stateText -cne 'completed'" in source
    assert "$stateText -notmatch '(?i)completed'" not in source


def test_codex_tool_smoke_validates_structured_subagent_lifecycle():
    source = (ROOT / "scripts" / "codex-tool-exposure-smoke.ps1").read_text(encoding="utf-8-sig")

    assert "[string]$ThirdPartyModel = 'ollama-cloud/glm-5.2'" in source
    assert "function Test-SubagentLifecycle" in source
    assert "collab_tool_call" in source
    assert "@('spawn_agent', 'wait', 'close_agent')" in source
    assert "receiver_thread_ids" in source
    assert "agents_states" in source
    assert "SENTINEL:third-party-subagent-child-ok" in source
    assert "$lifecycleFailures" in source
    assert "Get-NewestSessionAfter" not in source
    assert "Where-Object { $_.status -ne 'passed' }" in source


def test_codex_mode_persists_bare_official_model_ids():
    source = (ROOT / "scripts" / "codex-mode.ps1").read_text(encoding="utf-8-sig")

    assert 'model = `"gpt-5.5`"' in source
    assert 'model = "gpt-5.5"' in source
    assert 'model = "openai/gpt-5.5"' not in source


def test_active_gateway_diagnostics_default_to_bare_official_model_ids():
    launcher = (ROOT / "scripts" / "launch-codex-proxy-app.ps1").read_text(encoding="utf-8-sig")
    replay = (ROOT / "scripts" / "replay_official_transport.py").read_text(encoding="utf-8")

    assert '{"model":"gpt-5.5","input":"proxy upstream preflight"}' in launcher
    assert 'DEFAULT_MODEL = "gpt-5.5-fast"' in replay


def test_online_history_e2e_uses_app_cli_and_isolated_codex_home():
    source = (ROOT / "scripts" / "e2e_history_online_sync.py").read_text(encoding="utf-8")

    assert "OpenAI" in source and "Codex" in source and "bin" in source
    assert '"app-server"' in source
    assert '"CODEX_HOME"' in source
    assert '"migrate-official-to-unified"' in source
    assert 'expected deferred while SQLite writer lock is held' in source
    assert 'expected completed after releasing SQLite writer lock' in source
    assert "app-server exited during online history migration" in source


def test_embedded_python_runtime_bundles_zstandard_for_app_request_bodies():
    source = (ROOT / "scripts" / "Prepare-PythonRuntime.ps1").read_text(encoding="utf-8-sig")

    assert '[string]$ZstandardVersion = "0.25.0"' in source
    assert '[string]$ZstandardWheelSha256' in source
    assert "zstandard-$ZstandardVersion-cp313-cp313-win_amd64.whl" in source
    assert "& $python -m zipfile -e $zstandardWheelPath $runtimeDir" in source
    assert "import http.server, pathlib, sqlite3, tomllib, urllib.request, zstandard" in source


def test_codex_app_transport_e2e_uses_app_server_and_requires_completed_turns():
    source = (ROOT / "scripts" / "e2e_codex_app_transport.py").read_text(encoding="utf-8")

    assert '"app-server"' in source
    assert '"thread/start"' in source
    assert '"turn/start"' in source
    assert 'message.get("method") == "turn/completed"' in source
    assert 'message["params"]["turn"].get("id") == turn_id' in source
    assert 'returned no turn id' in source
    assert '"--pause-between-turns"' in source
    assert 'completed_status != "completed"' in source
    assert 'thread_params["dynamicTools"] = dynamic_tools' in source


def test_codex_catalog_roundtrip_e2e_uses_live_app_catalog_and_isolated_custom_provider():
    source = (ROOT / "scripts" / "e2e_codex_catalog_roundtrip.py").read_text(encoding="utf-8")

    assert "OpenAI" in source and "Codex" in source and "bin" in source
    assert '"model/list"' in source
    assert '"CODEX_HOME"' in source
    assert '"catalog_sync.py"' in source
    assert '"config_overlay.py"' in source
    assert "official model order changed after custom catalog roundtrip" in source
    assert "custom catalog exposed a prefixed official model id" in source
    assert "reasoning contract must preserve Light through Max" in source
