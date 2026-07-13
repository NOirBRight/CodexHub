import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]
REPLAY_SUBPROCESS_TIMEOUT_SECONDS = 12
REPLAY_COMPLETION_BOUND_SECONDS = 8


def test_issue_108_lifecycle_replay_stops_only_the_retained_process_tree(tmp_path):
    powershell = shutil.which("powershell.exe")
    if powershell is None:
        pytest.skip("Windows PowerShell is required for the lifecycle replay")

    started_at = time.monotonic()
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
        timeout=REPLAY_SUBPROCESS_TIMEOUT_SECONDS,
    )
    elapsed_seconds = time.monotonic() - started_at

    assert result.returncode == 0, result.stdout + result.stderr
    assert elapsed_seconds < REPLAY_COMPLETION_BOUND_SECONDS, result.stderr
    summaries = list(tmp_path.glob("run-*/summary.json"))
    assert len(summaries) == 1
    summary = json.loads(summaries[0].read_text(encoding="utf-8-sig"))
    assert summary["mode"] == "lifecycle_replay"
    assert summary["passed"] is True
    assert summary["failures"] == []
    assert summary["tracked_root_exited"] is True
    assert summary["tracked_child_exited"] is True
    assert summary["tracked_child_exit_before_natural_timeout"] is True
    assert summary["cleanup_within_budget"] is True
    assert summary["cleanup_elapsed_milliseconds"] <= summary["cleanup_budget_milliseconds"]


def test_issue_108_environment_isolation_replay_keeps_cli_secrets_out_of_child(tmp_path):
    powershell = shutil.which("powershell.exe")
    if powershell is None:
        pytest.skip("Windows PowerShell is required for the environment isolation replay")

    environment = dict(os.environ)
    environment["OLLAMA_API_KEY"] = "ambient-test-key-must-not-reach-cli"
    environment["CODEXHUB_TEST_SECRET"] = "ambient-test-secret-must-not-reach-cli"
    started_at = time.monotonic()
    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ROOT / "scripts" / "qualify-issue-108-glm-tool-surface.ps1"),
            "-EnvironmentIsolationReplay",
            "-OutputDir",
            str(tmp_path),
        ],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        timeout=REPLAY_SUBPROCESS_TIMEOUT_SECONDS,
    )
    elapsed_seconds = time.monotonic() - started_at

    assert result.returncode == 0, result.stdout + result.stderr
    assert elapsed_seconds < REPLAY_COMPLETION_BOUND_SECONDS, result.stderr
    summaries = list(tmp_path.glob("run-*/summary.json"))
    assert len(summaries) == 1
    summary = json.loads(summaries[0].read_text(encoding="utf-8-sig"))
    assert summary["mode"] == "environment_isolation_replay"
    assert summary["passed"] is True
    assert summary["cli_has_ollama_api_key"] is False
    assert summary["cli_has_test_secret"] is False
    assert summary["cli_home_is_isolated"] is True
    assert summary["cleanup_within_budget"] is True
    assert summary["cleanup_elapsed_milliseconds"] <= summary["cleanup_budget_milliseconds"]


def test_issue_108_history_adapter_negative_control_replay_is_bounded_and_sanitized(tmp_path):
    powershell = shutil.which("powershell.exe")
    if powershell is None:
        pytest.skip("Windows PowerShell is required for the history-adapter replay")

    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ROOT / "scripts" / "qualify-issue-108-glm-tool-surface.ps1"),
            "-HistoryAdapterReplay",
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
    assert summary["mode"] == "history_adapter_replay"
    assert summary["passed"] is True
    assert summary["disabled_structured_history_pair_count"] == 0
    assert summary["disabled_developer_item_count"] == 2
    assert summary["adapted_structured_history_pair_count"] == 1
    assert summary["adapted_developer_item_count"] == 0
    assert summary["adapted_patch_argument_key_count"] == 1


def test_issue_108_tool_surface_evidence_replay_has_semantic_three_case_ab(tmp_path):
    powershell = shutil.which("powershell.exe")
    if powershell is None:
        pytest.skip("Windows PowerShell is required for the tool-surface replay")

    started_at = time.monotonic()
    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ROOT / "scripts" / "qualify-issue-108-glm-tool-surface.ps1"),
            "-ToolSurfaceEvidenceReplay",
            "-OutputDir",
            str(tmp_path),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=REPLAY_SUBPROCESS_TIMEOUT_SECONDS,
    )
    elapsed_seconds = time.monotonic() - started_at

    assert result.returncode == 0, result.stdout + result.stderr
    assert elapsed_seconds < REPLAY_COMPLETION_BOUND_SECONDS, result.stderr
    summaries = list(tmp_path.glob("run-*/summary.json"))
    assert len(summaries) == 1
    summary = json.loads(summaries[0].read_text(encoding="utf-8-sig"))
    assert summary["mode"] == "tool_surface_evidence_replay"
    assert summary["passed"] is True
    assert summary["failures"] == []
    assert summary["case_outcomes"] == {
        "minimal_core": "green",
        "namespace_200_eager": "red",
        "namespace_200_deferred_core": "green",
    }
    assert summary["direct_tool_counts"] == {
        "minimal_core": 7,
        "namespace_200_eager": 207,
        "namespace_200_deferred_core": 8,
    }
    assert summary["same_200_source_payload"] is True
    assert summary["deferred_payload_digest"].startswith("sha256:")
    assert "timeout" not in json.dumps(summary).lower()


def test_issue_108_capture_gateway_digest_replay_executes_generated_capture_path(tmp_path):
    powershell = shutil.which("powershell.exe")
    if powershell is None:
        pytest.skip("Windows PowerShell is required for the capture Gateway replay")

    started_at = time.monotonic()
    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ROOT / "scripts" / "qualify-issue-108-glm-tool-surface.ps1"),
            "-CaptureGatewayDigestReplay",
            "-OutputDir",
            str(tmp_path),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=REPLAY_SUBPROCESS_TIMEOUT_SECONDS,
    )
    elapsed_seconds = time.monotonic() - started_at

    assert result.returncode == 0, result.stdout + result.stderr
    assert elapsed_seconds < REPLAY_COMPLETION_BOUND_SECONDS, result.stderr
    summaries = list(tmp_path.glob("run-*/summary.json"))
    assert len(summaries) == 1
    summary = json.loads(summaries[0].read_text(encoding="utf-8-sig"))
    assert summary["mode"] == "capture_gateway_digest_replay"
    assert summary["passed"] is True
    assert summary["failures"] == []
    assert summary["capture_harness_error_count"] == 0
    assert {"before", "after"}.issubset(summary["capture_stages"])
    assert summary["tool_surface_digest"].startswith("sha256:")


def test_issue_108_evidence_replay_rejects_unknown_fixture_fields(tmp_path):
    powershell = shutil.which("powershell.exe")
    if powershell is None:
        pytest.skip("Windows PowerShell is required for the evidence replay")

    fixture = json.loads(
        (ROOT / "tests" / "fixtures" / "issue_108_tool_surface_replay.json").read_text(encoding="utf-8")
    )
    fixture["unexpected"] = True
    invalid_fixture = tmp_path / "invalid-evidence.json"
    invalid_fixture.write_text(json.dumps(fixture), encoding="utf-8")
    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ROOT / "scripts" / "qualify-issue-108-glm-tool-surface.ps1"),
            "-ToolSurfaceEvidenceReplay",
            "-EvidenceFixture",
            str(invalid_fixture),
            "-OutputDir",
            str(tmp_path / "result"),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=REPLAY_SUBPROCESS_TIMEOUT_SECONDS,
    )

    assert result.returncode != 0
    summaries = list((tmp_path / "result").glob("run-*/summary.json"))
    assert len(summaries) == 1
    summary = json.loads(summaries[0].read_text(encoding="utf-8-sig"))
    assert summary["passed"] is False
    assert summary["failures"] == ["evidence_fixture_invalid"]
    assert "unexpected" not in result.stdout


def test_issue_108_qualification_evidence_replay_fails_closed_without_live_fixture(tmp_path):
    powershell = shutil.which("powershell.exe")
    if powershell is None:
        pytest.skip("Windows PowerShell is required for the evidence replay")

    result = subprocess.run(
        [
            powershell,
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ROOT / "scripts" / "qualify-issue-108-glm-tool-surface.ps1"),
            "-QualificationEvidenceReplay",
            "-OutputDir",
            str(tmp_path),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=REPLAY_SUBPROCESS_TIMEOUT_SECONDS,
    )

    assert result.returncode != 0
    summaries = list(tmp_path.glob("run-*/summary.json"))
    assert len(summaries) == 1
    summary = json.loads(summaries[0].read_text(encoding="utf-8-sig"))
    assert summary["mode"] == "qualification_evidence_replay"
    assert summary["passed"] is False
    assert summary["failures"] == ["qualification_evidence_fixture_missing"]
    assert not (ROOT / "tests" / "fixtures" / "issue_108_glm_qualification_evidence.json").exists()


def test_issue_108_failure_validator_preserves_sanitized_harness_error_details(tmp_path):
    fixture = {
        "schema": "codexhub.issue108.qualification-failure.v1",
        "sanitized": True,
        "phase": "readiness_preflight",
        "route_identity": {
            "model": "glm-5.2",
            "upstream": "ollama_cloud",
            "route_mode": "codexhub",
        },
        "last_successful_tool": "shell_command",
        "response_termination": "harness_error",
        "failure_classification": "harness_error",
        "request_count": 18,
        "adapter_counts": {"apply_patch": 0, "history": 0},
        "timeout_classification": "harness_error",
        "error_class": "NameError",
        "http_status": 500,
        "failure_codes": ["capture_gateway_harness_error", "qualification_readiness_failed"],
    }
    fixture_path = tmp_path / "harness-error.json"
    fixture_path.write_text(json.dumps(fixture), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "tests" / "validate_issue_108_evidence.py"),
            "--mode",
            "qualification-failure",
            "--fixture",
            str(fixture_path),
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=REPLAY_SUBPROCESS_TIMEOUT_SECONDS,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report == {
        "mode": "qualification_failure_evidence_replay",
        "passed": True,
        "failures": [],
        "request_count": 18,
        "timeout_classification": "harness_error",
        "failure_classification": "harness_error",
    }


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
    assert "HistoryAdapterNegativeControl" in source
    assert "CODEXHUB_HISTORY_ADAPTER_NEGATIVE_CONTROL" in source
    assert "post_success_tool_choice_failed" in source


def test_issue_108_qualification_rejects_retry_and_protocol_fallback_evidence():
    source = (
        ROOT / "scripts" / "qualify-issue-108-glm-tool-surface.ps1"
    ).read_text(encoding="utf-8-sig")

    assert "upstream_retry" in source
    assert "upstream_protocol_fallback" in source
    assert "upstream_retry_event_count" in source
    assert "upstream_protocol_fallback_event_count" in source
    assert "qualification recorded an upstream retry" in source
    assert "qualification recorded an upstream protocol fallback" in source


def test_issue_108_qualification_uses_synthetic_gateway_bearer_and_whitelisted_children():
    source = (
        ROOT / "scripts" / "qualify-issue-108-glm-tool-surface.ps1"
    ).read_text(encoding="utf-8-sig")

    assert "UseCliSandboxBypass" not in source
    assert "dangerously-bypass-approvals-and-sandbox" not in source
    assert "SharedAuthPath" not in source
    assert "authCopyPath" not in source
    assert ".codex\\auth.json" not in source
    assert "$startInfo.Environment.Clear()" in source
    assert "$gatewayEnvironment" in source
    assert "$cliEnvironment" in source
    assert "$cliHome = if ($ExternalIsolationQualification)" in source
    assert "$cliTemp = if ($ExternalIsolationQualification)" in source
    assert "$cliSandbox = if ($ExternalIsolationQualification) { 'danger-full-access' } else { 'workspace-write' }" in source
    assert "-CodexHome $cliHome -TempRoot $cliTemp" in source
    assert "OLLAMA_API_KEY = $ollamaApiKey" in source
    assert "$cliEnvironment['OLLAMA_API_KEY']" not in source
    assert "-Environment $gatewayEnvironment" in source
    assert "-Environment $cliEnvironment" in source
    assert "experimental_bearer_token" in source
    assert "[windows]" in source
    assert 'sandbox = "elevated"' in source
    assert 'sandbox = "unelevated"' not in source
    assert "'--sandbox', $cliSandbox" in source
    assert "'-a', 'never'" in source
    assert "External qualification scratch directory must be outside the repository workspace" in source
    assert "Readiness preflight: use the accepted GLM route" in source
    assert "ReadinessTimeoutSeconds" in source
    assert "qualification_readiness_failed" in source
    assert "'--add-dir', $testWorkspace" not in source
    assert "Isolated Gateway did not become healthy" in source
    assert "Isolated proxy did not become healthy" not in source
    assert "Wait-GatewayHealth" in source
    assert "Gateway did not record a request_start event" in source
    assert "Gateway recorded a request_error during qualification" in source
    assert "proxy did not record a request_start event" not in source
    assert "proxy recorded a request_error during qualification" not in source
    assert "gateway_startup_failed" in source
    assert "proxy_startup_failed" not in source
    assert "workspace_write_sandbox_rejected" in source
    assert "writing is blocked by read-only sandbox" in source
    assert "apply_patch_execution_failed" in source


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
