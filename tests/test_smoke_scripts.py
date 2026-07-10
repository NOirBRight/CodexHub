from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


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
