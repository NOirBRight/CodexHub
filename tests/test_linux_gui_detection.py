from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

SPEC = importlib.util.spec_from_file_location(
    "linux_gui_detection",
    Path(__file__).resolve().parents[1] / "scripts/e2e_linux_gui_clients.py",
)
assert SPEC and SPEC.loader
E2E = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(E2E)


@pytest.mark.parametrize(
    ("manager", "owner", "version", "expected"),
    [
        (
            "pacman",
            "openai-codex-desktop\n",
            "openai-codex-desktop 2:26.915.31945-1\n",
            "26.915.31945",
        ),
        ("dpkg-query", "chatgpt: /usr/bin/chatgpt\n", "26.715.8383-1", "26.715.8383"),
        (
            "dpkg-query",
            "chatgpt:amd64: /usr/bin/chatgpt\n",
            "1:26.715.8383-1",
            "26.715.8383",
        ),
    ],
)
def test_selected_executable_owner_supplies_version(manager, owner, version, expected):
    with (
        patch.object(
            E2E.shutil,
            "which",
            side_effect=lambda name: name if name == manager else None,
        ),
        patch.object(
            E2E.subprocess,
            "run",
            side_effect=[
                subprocess.CompletedProcess([], 0, owner, ""),
                subprocess.CompletedProcess([], 0, version, ""),
            ],
        ) as run,
    ):
        assert E2E.installed_package(Path("/usr/bin/chatgpt")) == (
            owner.strip().rsplit(": ", 1)[0],
            expected,
        )
        assert run.call_args_list[0].args[0][-1] == str(
            Path("/usr/bin/chatgpt").resolve()
        )


@pytest.mark.parametrize(
    "failure", [FileNotFoundError(), subprocess.TimeoutExpired("pacman", 5)]
)
def test_unavailable_package_manager_is_unknown(failure):
    with (
        patch.object(E2E.shutil, "which", return_value="pacman"),
        patch.object(E2E.subprocess, "run", side_effect=failure),
    ):
        assert E2E.installed_package(Path("/missing/client")) == (None, None)


def test_missing_client_does_not_borrow_installed_package_version():
    with patch.object(E2E.subprocess, "run") as run:
        assert E2E.installed_package(None) == (None, None)
        run.assert_not_called()


def test_unowned_executable_fails_closed():
    with (
        patch.object(E2E.shutil, "which", return_value="pacman"),
        patch.object(
            E2E.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 1, "", "not owned"),
        ),
    ):
        assert E2E.installed_package(Path("/tmp/client")) == (None, None)


def test_path_client_is_detected_and_unknown_version_fails_floor(tmp_path, monkeypatch):
    client = tmp_path / "ZCode"
    client.touch()
    monkeypatch.delenv("CODEXHUB_ZCODE_EXE", raising=False)
    with patch.object(
        E2E.shutil,
        "which",
        side_effect=lambda name: str(client) if name == "ZCode" else None,
    ):
        result = E2E.detect_zcode()
    assert result["executable"] == str(client)
    assert result["version"] is None
    assert result["meets_floor"] is False


@pytest.mark.skipif(os.name != "posix", reason="Linux launcher")
def test_launcher_does_not_pass_a_process_that_exits_during_observation(
    tmp_path, monkeypatch
):
    client = tmp_path / "short-lived-client"
    client.write_text("#!/bin/sh\nsleep 0.05\nexit 7\n")
    client.chmod(0o755)
    monkeypatch.setattr(E2E, "LAUNCH_WAIT_SECONDS", 0.3)
    result = E2E.launch_isolated(str(client), "fixture", tmp_path)
    assert result["ok"] is False
    assert "7" in result["error"]
