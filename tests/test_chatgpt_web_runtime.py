"""Public CLI and loopback status tests for the ChatGPT Web Runtime supervisor."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import chatgpt_web_runtime

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "src-python" / "chatgpt_web_runtime.py"
PIN_PATH = ROOT / "config" / "chatgpt_web_runtime_pin.json"
SECRET = "sk-chatgpt-web-test-secret-DO-NOT-LOG"


def _run(home: Path, *args: str, pin: Path | None = None, extra_env: dict[str, str] | None = None) -> dict:
    command = [sys.executable, str(SCRIPT), *args, "--home", str(home)]
    env = os.environ.copy()
    env["CODEXHUB_CHATGPT_WEB_HOME"] = str(home)
    if pin is not None:
        env["CODEXHUB_CHATGPT_WEB_PIN"] = str(pin)
    if extra_env:
        env.update(extra_env)
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=20,
    )
    payload = json.loads(completed.stdout)
    payload["_exit_code"] = completed.returncode
    payload["_stderr"] = completed.stderr
    return payload


def _pin_for(directory: Path, payload: bytes) -> Path:
    document = json.loads(PIN_PATH.read_text(encoding="utf-8"))
    document["artifacts"][chatgpt_web_runtime.artifact_key()]["sha256"] = hashlib.sha256(payload).hexdigest()
    path = directory / "pin.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _script(marker: Path) -> bytes:
    return f"#!/bin/sh\nprintf executed > '{marker}'\nprintf '{SECRET}'\n".encode()


def _supervise_pids(home: Path) -> list[int]:
    found: list[int] = []
    needle = str(home)
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            command = (entry / "cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", "replace")
        except OSError:
            continue
        if "chatgpt_web_runtime.py" in command and "supervise" in command and needle in command:
            found.append(int(entry.name))
    return found


def _stop(home: Path, pin: Path) -> None:
    if _supervise_pids(home):
        _run(home, "stop", pin=pin)


def test_repo_pin_is_the_accepted_upstream_release() -> None:
    document = json.loads(PIN_PATH.read_text(encoding="utf-8"))

    assert document["commit"] == chatgpt_web_runtime.PINNED_COMMIT == "a13cd09950969f43e3b7e25c71fa43efaf5446c5"
    assert document["version"] == chatgpt_web_runtime.PINNED_VERSION == "6.1.1"
    assert document["license"] == "MIT"
    assert document["rejected_entries"] == ["setup", "dev", "--replace-codex-route"]
    assert document["artifacts"]["linux-x64"]["sha256"] == "e86371aa677722811c34e4ac9b8984a3859f24f57c5ad0e3e1d6014a96298531"
    assert document["artifacts"]["windows-x64"]["sha256"] == "88341bd2818894799be98f1283f64a2124de0f43193ee2dc1d2a774362ea59d5"
    for artifact in document["artifacts"].values():
        assert "/releases/latest/" not in artifact["url"]
        assert artifact["url"].startswith(
            "https://github.com/miuuyy/codex-chatgpt-web/releases/download/v6.1.1/"
        )


def test_preset_does_not_publish_a_gateway_model() -> None:
    from providers_config import DEFAULT_PROVIDERS_PATH, build_external_model_index, load_providers

    providers = load_providers(DEFAULT_PROVIDERS_PATH)
    preset = next(provider for provider in providers if provider.id == "chatgpt-web")

    assert preset.models == []
    assert preset.base_url == ""
    index = build_external_model_index(providers, require_api_key=False)
    assert not any(key == "chatgpt-web" or key.startswith("chatgpt-web/") for key in index)


def test_bad_checksum_is_refused_and_not_executed(tmp_path: Path) -> None:
    home = tmp_path / "runtime"
    marker = home / "executed-marker"
    payload = _script(marker)
    pin = _pin_for(tmp_path, b"expected-payload")
    source = tmp_path / "payload.sh"
    source.write_bytes(payload)
    source.chmod(0o755)

    result = _run(home, "install", "--source", str(source), pin=pin)

    assert result["_exit_code"] != 0
    assert "checksum mismatch" in result["error"]
    assert "not executed" in result["error"]
    assert not marker.exists()
    assert not (home / "current" / "payload").exists()
    assert SECRET not in json.dumps(result)
    assert SECRET not in (home / "supervisor.log").read_text(encoding="utf-8")


def test_interrupted_install_can_retry_without_deleting_a_good_install(tmp_path: Path) -> None:
    home = tmp_path / "runtime"
    marker = home / "executed-marker"
    good = _script(marker)
    pin = _pin_for(tmp_path, good)
    source = tmp_path / "payload.sh"
    source.write_bytes(good)
    source.chmod(0o755)
    installed = _run(home, "install", "--source", str(source), pin=pin)
    assert installed["_exit_code"] == 0
    assert (home / "current" / "payload").read_bytes() == good

    partial = home / "staging" / "payload.partial"
    partial.parent.mkdir(parents=True, exist_ok=True)
    partial.write_bytes(b"interrupted")
    retried = _run(home, "install", "--source", str(source), pin=pin)
    assert retried["_exit_code"] == 0
    assert not partial.exists()
    assert (home / "current" / "payload").read_bytes() == good
    assert not marker.exists()
    assert (home / "current" / "payload").stat().st_mode & 0o111 == 0

    bad = tmp_path / "bad.sh"
    bad.write_bytes(_script(marker) + b"\n# tampered\n")
    failed = _run(home, "install", "--source", str(bad), pin=pin)
    assert failed["_exit_code"] != 0
    assert (home / "current" / "payload").read_bytes() == good
    assert (home / "previous-good" / "payload").read_bytes() == good
    assert not marker.exists()


def test_incompatible_pin_and_version_mismatch_are_refused(tmp_path: Path) -> None:
    home = tmp_path / "runtime"
    marker = home / "executed-marker"
    good = _script(marker)
    pin = _pin_for(tmp_path, good)
    source = tmp_path / "payload.sh"
    source.write_bytes(good)
    incompatible = json.loads(pin.read_text(encoding="utf-8"))
    incompatible["commit"] = "0" * 40
    incompatible_path = tmp_path / "incompatible.json"
    incompatible_path.write_text(json.dumps(incompatible), encoding="utf-8")

    refused = _run(home, "install", "--source", str(source), pin=incompatible_path)
    assert refused["_exit_code"] != 0
    assert "incompatible" in refused["error"]
    assert not marker.exists()
    assert not (home / "current").exists()

    installed = _run(home, "install", "--source", str(source), pin=pin)
    assert installed["_exit_code"] == 0
    install_path = home / "current" / "install.json"
    document = json.loads(install_path.read_text(encoding="utf-8"))
    document["sha256"] = "f" * 64
    install_path.write_text(json.dumps(document), encoding="utf-8")

    started = _run(home, "start", pin=pin)
    assert started["_exit_code"] != 0
    assert "version mismatch" in started["error"]
    assert _supervise_pids(home) == []
    status = _run(home, "status", pin=pin)
    assert status["component"]["compatible"] is False
    assert status["ready"] is False
    assert status["upstream_executed"] is False


def test_repeated_start_keeps_one_process_and_distinct_status_layers(tmp_path: Path) -> None:
    home = tmp_path / "runtime"
    payload = b"pinned-runtime-bytes"
    pin = _pin_for(tmp_path, payload)
    source = tmp_path / "payload.bin"
    source.write_bytes(payload)
    assert _run(home, "install", "--source", str(source), pin=pin)["_exit_code"] == 0
    (home / "layers.json").write_text(
        json.dumps(
            {
                "login": "signed_in",
                "browser_smoke": "not_run",
                "tunnel": "ready",
                "connector_selectable": False,
                "detail": SECRET,
            }
        ),
        encoding="utf-8",
    )
    try:
        first = _run(home, "start", pin=pin)
        second = _run(home, "start", pin=pin)
        assert first["_exit_code"] == 0
        assert second["_exit_code"] == 0
        assert first["process"]["pid"] == second["process"]["pid"]
        assert _supervise_pids(home) == [first["process"]["pid"]]
        assert first["process"]["listen_host"] == "127.0.0.1"
        assert str(home) == first["process"]["private_home"]
        with socket.create_connection(("127.0.0.1", first["process"]["port"]), timeout=2):
            pass
        status = _run(home, "status", pin=pin)
        assert status["login"]["state"] == "signed_out"
        assert status["browser_smoke"]["state"] == "not_run"
        assert status["tunnel"]["state"] == "ready"
        assert status["connector"]["selectable"] is False
        assert status["ready"] is False
        assert SECRET not in json.dumps(status)
        assert "[redacted]" in json.dumps(status["tunnel"])
    finally:
        _stop(home, pin)


def test_log_status_and_errors_redact_the_account_secret(tmp_path: Path) -> None:
    home = tmp_path / "runtime"
    payload = b"pinned-runtime-bytes"
    pin = _pin_for(tmp_path, payload)
    source = tmp_path / "payload.bin"
    source.write_bytes(payload)
    assert _run(home, "install", "--source", str(source), pin=pin)["_exit_code"] == 0
    try:
        started = _run(home, "start", pin=pin)
        assert started["_exit_code"] == 0
        port = started["process"]["port"]
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/account",
            data=json.dumps({"secret": SECRET}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            stored = json.loads(response.read().decode("utf-8"))
        assert stored["login"]["state"] == "signed_in"
        assert SECRET not in json.dumps(stored)
        leaked = urllib.request.Request(
            f"http://127.0.0.1:{port}/account",
            data=f"not-json {SECRET}".encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as captured:
            urllib.request.urlopen(leaked, timeout=2)
        error_body = captured.value.read().decode("utf-8")
        assert SECRET not in error_body
        log = (home / "supervisor.log").read_text(encoding="utf-8")
        status = json.dumps(_run(home, "status", pin=pin))
        assert SECRET not in log
        assert SECRET not in status
        assert SECRET not in error_body
        account_mode = (home / "account.json").stat().st_mode & 0o077
        assert account_mode == 0
    finally:
        _stop(home, pin)


def test_login_window_keeps_one_account_across_close_and_restart(tmp_path: Path) -> None:
    home = tmp_path / "runtime"
    payload = b"pinned-runtime-bytes"
    pin = _pin_for(tmp_path, payload)
    source = tmp_path / "payload.bin"
    source.write_bytes(payload)
    assert _run(home, "install", "--source", str(source), pin=pin)["_exit_code"] == 0
    try:
        opened = _run(home, "open-login", pin=pin)
        assert opened["_exit_code"] == 0
        assert opened["login"]["window"] == "open"
        assert opened["login_url"].startswith("http://127.0.0.1:")
        assert opened["login_url"].endswith("/login")
        with urllib.request.urlopen(opened["login_url"], timeout=2) as response:
            page = response.read().decode("utf-8")
        assert "chatgpt.com" in page
        assert "does not sign in" in page
        assert SECRET not in page
        request = urllib.request.Request(
            opened["login_url"].rsplit("/", 1)[0] + "/account",
            data=json.dumps({"secret": SECRET}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            stored = json.loads(response.read().decode("utf-8"))
        account_id = stored["login"]["account_id"]
        (home / "layers.json").write_text(
            json.dumps(
                {
                    "browser_smoke": "passed",
                    "tunnel": "ready",
                    "connector_selectable": True,
                }
            ),
            encoding="utf-8",
        )
        ready = _run(home, "status", pin=pin)
        assert ready["ready"] is True
        (home / "layers.json").write_text(
            json.dumps(
                {
                    "browser_smoke": "passed",
                    "tunnel": "ready",
                    "connector_selectable": False,
                }
            ),
            encoding="utf-8",
        )
        separated = _run(home, "status", pin=pin)
        assert separated["login"]["state"] == "signed_in"
        assert separated["tunnel"]["state"] == "ready"
        assert separated["connector"]["selectable"] is False
        assert separated["ready"] is False
        closed = _run(home, "close-login", pin=pin)
        assert closed["login"]["window"] == "closed"
        assert closed["login"]["state"] == "signed_in"
        assert closed["login"]["account_id"] == account_id
        assert _run(home, "stop", pin=pin)["restart_required"] is True
        restarted = _run(home, "start", pin=pin)
        assert restarted["login"]["state"] == "signed_in"
        assert restarted["login"]["account_id"] == account_id
        assert restarted["process"]["ownership"] == "codexhub-supervisor"
        duplicate = urllib.request.Request(
            f"http://127.0.0.1:{restarted['process']['port']}/account",
            data=json.dumps({"secret": SECRET + "-other"}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as captured:
            urllib.request.urlopen(duplicate, timeout=2)
        assert captured.value.code == 409
        assert SECRET not in captured.value.read().decode("utf-8")
        disabled = _run(home, "disable", pin=pin)
        assert disabled["disabled"] is True
        assert disabled["restart_required"] is False
        assert disabled["process"]["running"] is False
        assert disabled["process"]["ownership"] == "codexhub-supervisor"
        refused = _run(home, "open-login", pin=pin)
        assert refused["_exit_code"] != 0
        assert "disabled" in refused["error"]
    finally:
        _stop(home, pin)


def test_launch_leaves_client_config_bytes_unchanged(tmp_path: Path) -> None:
    user_home = tmp_path / "user"
    codex = user_home / ".codex" / "config.toml"
    claude = user_home / ".claude" / "settings.json"
    opencode = user_home / ".config" / "opencode" / "opencode.json"
    dev_home = tmp_path / "dev-home"
    for path, body in (
        (codex, b"model = \"gpt-5\"\npreferred = \"v2\"\n"),
        (claude, b"{\"model\":\"claude-opus-5-5\"}\n"),
        (opencode, b"{\"plugin\":[]}\n"),
        (dev_home / "secret.txt", SECRET.encode()),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    before = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (codex, claude, opencode, dev_home / "secret.txt")}
    home = tmp_path / "runtime"
    payload = b"pinned-runtime-bytes"
    pin = _pin_for(tmp_path, payload)
    source = tmp_path / "payload.bin"
    source.write_bytes(payload)
    env = {
        "HOME": str(user_home),
        "CODEX_HOME": str(codex.parent),
        "XDG_CONFIG_HOME": str(user_home / ".config"),
        "CODEX_WEB_GPT_DEV_HOME": str(dev_home),
    }
    assert _run(home, "install", "--source", str(source), pin=pin, extra_env=env)["_exit_code"] == 0
    try:
        started = _run(home, "start", pin=pin, extra_env=env)
        assert started["_exit_code"] == 0
        assert started["upstream_executed"] is False
        for path, digest in before.items():
            assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
        private_text = "\n".join(
            item.read_text(encoding="utf-8", errors="replace")
            for item in home.rglob("*")
            if item.is_file()
        )
        assert SECRET not in private_text
    finally:
        _stop(home, pin)
        for path, digest in before.items():
            assert hashlib.sha256(path.read_bytes()).hexdigest() == digest


def test_remote_bind_is_refused(tmp_path: Path) -> None:
    home = tmp_path / "runtime"
    payload = b"pinned-runtime-bytes"
    pin = _pin_for(tmp_path, payload)
    source = tmp_path / "payload.bin"
    source.write_bytes(payload)
    assert _run(home, "install", "--source", str(source), pin=pin)["_exit_code"] == 0

    refused = _run(home, "start", pin=pin, extra_env={"CODEXHUB_CHATGPT_WEB_BIND": "0.0.0.0"})

    assert refused["_exit_code"] != 0
    assert "127.0.0.1" in refused["error"]
    assert _supervise_pids(home) == []
