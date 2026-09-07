from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from e2e_linux_dock_icon import (  # noqa: E402
    ENVIRONMENT_PASSTHROUGH,
    _copy_portable_candidate,
    _safe_environment,
    _sanitized_result_lines,
)


def test_dock_probe_environment_is_a_whitelist_without_provider_credentials(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "operator-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "operator-secret")
    monkeypatch.setenv("HTTPS_PROXY", "https://operator-proxy.invalid")
    monkeypatch.setenv("PATH", "/usr/bin")

    environment = _safe_environment(HOME="/isolated/home")

    assert set(environment).issubset(set(ENVIRONMENT_PASSTHROUGH) | {"HOME"})
    assert environment["HOME"] == "/isolated/home"
    assert "OPENAI_API_KEY" not in environment
    assert "ANTHROPIC_API_KEY" not in environment
    assert "HTTPS_PROXY" not in environment


def test_dock_probe_copies_distinct_portable_candidates(tmp_path) -> None:
    source = tmp_path / "candidate"
    (source / "config").mkdir(parents=True)
    (source / "src-python").mkdir()
    (source / "config/providers.toml").write_text("[providers]", encoding="utf-8")
    (source / "src-python/codex_proxy.py").write_text("", encoding="utf-8")
    executable = source / "CodexHub"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)

    old = _copy_portable_candidate(source, tmp_path / "portable-old")
    new = _copy_portable_candidate(source, tmp_path / "portable-new")

    assert old != new
    assert old.read_text(encoding="utf-8") == new.read_text(encoding="utf-8")


def test_dock_probe_reports_only_anonymous_structured_results() -> None:
    output = """dbus-daemon: /home/operator/.config and OPENAI_API_KEY=secret\n
{"phase":"first_launch","passed":true,"identity":[],"secret":"operator-secret"}\n
Traceback: /tmp/private.log\n
{"phase":"unexpected","passed":true,"identity":[]}\n"""

    assert _sanitized_result_lines(output) == [
        '{"passed": true, "phase": "first_launch"}'
    ]
