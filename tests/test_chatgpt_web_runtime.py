"""Public CLI tests for extracting and starting the pinned ChatGPT Web Runtime."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import tarfile
import urllib.error
import urllib.request
from pathlib import Path

import pytest

import chatgpt_web_runtime

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "src-python" / "chatgpt_web_runtime.py"
PIN_PATH = ROOT / "config" / "chatgpt_web_runtime_pin.json"
SECRET = "sk-chatgpt-web-test-secret-DO-NOT-LOG"
ENTRY_NAME = "bin/codex-chatgpt-web"


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
        timeout=30,
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


def _fixture_script(marker: Path) -> str:
    return f"""#!/bin/sh
set -eu
mkdir -p "$CODEX_CHATGPT_WEB_HOME"
printf '%s\\n' "$*" >> "$CODEX_CHATGPT_WEB_HOME/argv.log"
printf 'CODEX_HOME=%s\\n' "${{CODEX_HOME-}}" >> "$CODEX_CHATGPT_WEB_HOME/argv.log"
printf 'WEB_HOME=%s\\n' "${{CODEX_CHATGPT_WEB_HOME-}}" >> "$CODEX_CHATGPT_WEB_HOME/argv.log"
if [ -n "${{CODEX_WEB_GPT_DEV_HOME-}}" ]; then
  printf 'DEV=%s\\n' "$CODEX_WEB_GPT_DEV_HOME" >> "$CODEX_CHATGPT_WEB_HOME/argv.log"
fi
if [ "$1" = "setup" ] || [ "$1" = "dev" ]; then
  exit 3
fi
if [ "$1" = "doctor" ]; then
  if [ -f "$CODEX_CHATGPT_WEB_HOME/doctor.json" ]; then
    cat "$CODEX_CHATGPT_WEB_HOME/doctor.json"
  else
    printf '%s\\n' '{{"ok":false,"checks":[{{"id":"login","status":"error","message":"missing"}}]}}'
  fi
  exit 0
fi
if [ "$1" = "serve" ]; then
  printf executed > '{marker}'
  trap 'exit 0' TERM INT
  while true; do
    sleep 1
  done
fi
exit 0
"""


def _archive(directory: Path, script: str) -> Path:
    tree = directory / "tree"
    entry = tree / ENTRY_NAME
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text(script, encoding="utf-8")
    entry.chmod(0o755)
    archive = directory / "runtime.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.add(entry, arcname=ENTRY_NAME)
    return archive


def _entry_pids(home: Path) -> list[int]:
    needle = str(home / "current" / "runtime" / ENTRY_NAME)
    found: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            command = (entry / "cmdline").read_bytes().replace(b"\x00", b" ").decode("utf-8", "replace")
        except OSError:
            continue
        if needle in command and " serve" in f" {command}":
            found.append(int(entry.name))
    return found


def _stop(home: Path, pin: Path) -> None:
    _run(home, "stop", pin=pin)


def test_repo_pin_is_the_accepted_upstream_release() -> None:
    document = json.loads(PIN_PATH.read_text(encoding="utf-8"))

    assert document["commit"] == chatgpt_web_runtime.PINNED_COMMIT == "a13cd09950969f43e3b7e25c71fa43efaf5446c5"
    assert document["version"] == chatgpt_web_runtime.PINNED_VERSION == "6.1.1"
    assert document["license"] == "MIT"
    assert "setup" in document["rejected_entries"]
    assert "dev" in document["rejected_entries"]
    assert "--replace-codex-route" in document["rejected_entries"]
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
    archive = _archive(tmp_path, _fixture_script(marker))
    pin = _pin_for(tmp_path, b"expected-payload")

    result = _run(home, "install", "--source", str(archive), pin=pin)

    assert result["_exit_code"] != 0
    assert "checksum mismatch" in result["error"]
    assert "not executed" in result["error"]
    assert not marker.exists()
    assert not (home / "current" / "runtime" / ENTRY_NAME).exists()
    assert SECRET not in json.dumps(result)


def test_interrupted_install_retries_and_extracts_without_dropping_a_good_install(tmp_path: Path) -> None:
    home = tmp_path / "runtime"
    marker = home / "executed-marker"
    archive = _archive(tmp_path, _fixture_script(marker))
    pin = _pin_for(tmp_path, archive.read_bytes())
    installed = _run(home, "install", "--source", str(archive), pin=pin)
    assert installed["_exit_code"] == 0
    entry = home / "current" / "runtime" / ENTRY_NAME
    assert entry.is_file()
    assert not marker.exists()
    assert (home / "current" / "payload").stat().st_mode & 0o111 == 0

    partial = home / "staging" / "payload.partial"
    partial.parent.mkdir(parents=True, exist_ok=True)
    partial.write_bytes(b"interrupted")
    retried = _run(home, "install", "--source", str(archive), pin=pin)
    assert retried["_exit_code"] == 0
    assert not partial.exists()
    assert entry.read_bytes() == (tmp_path / "tree" / ENTRY_NAME).read_bytes()
    assert not marker.exists()

    bad = _archive(tmp_path / "bad", _fixture_script(marker) + "\n# tampered\n")
    failed = _run(home, "install", "--source", str(bad), pin=pin)
    assert failed["_exit_code"] != 0
    assert entry.read_bytes() == (tmp_path / "tree" / ENTRY_NAME).read_bytes()
    assert (home / "previous-good" / "runtime" / ENTRY_NAME).is_file()
    assert not marker.exists()


def test_incompatible_pin_and_version_mismatch_are_refused(tmp_path: Path) -> None:
    home = tmp_path / "runtime"
    marker = home / "executed-marker"
    archive = _archive(tmp_path, _fixture_script(marker))
    pin = _pin_for(tmp_path, archive.read_bytes())
    incompatible = json.loads(pin.read_text(encoding="utf-8"))
    incompatible["commit"] = "0" * 40
    incompatible_path = tmp_path / "incompatible.json"
    incompatible_path.write_text(json.dumps(incompatible), encoding="utf-8")

    refused = _run(home, "install", "--source", str(archive), pin=incompatible_path)
    assert refused["_exit_code"] != 0
    assert "incompatible" in refused["error"]
    assert not marker.exists()
    assert not (home / "current").exists()

    installed = _run(home, "install", "--source", str(archive), pin=pin)
    assert installed["_exit_code"] == 0
    install_path = home / "current" / "install.json"
    document = json.loads(install_path.read_text(encoding="utf-8"))
    document["sha256"] = "f" * 64
    install_path.write_text(json.dumps(document), encoding="utf-8")

    started = _run(home, "start", pin=pin)
    assert started["_exit_code"] != 0
    assert "version mismatch" in started["error"]
    assert _entry_pids(home) == []
    status = _run(home, "status", pin=pin)
    assert status["component"]["compatible"] is False
    assert status["ready"] is False
    assert status["login"]["state"] == "signed_out"
    assert status["upstream_executed"] is False


def test_repeated_start_uses_one_entry_and_doctor_layers_stay_distinct(tmp_path: Path) -> None:
    home = tmp_path / "runtime"
    marker = home / "executed-marker"
    archive = _archive(tmp_path, _fixture_script(marker))
    pin = _pin_for(tmp_path, archive.read_bytes())
    assert _run(home, "install", "--source", str(archive), pin=pin)["_exit_code"] == 0
    try:
        first = _run(home, "start", pin=pin)
        (home / "web-home" / "doctor.json").write_text(
            json.dumps(
                {
                    "ok": False,
                    "checks": [
                        {"id": "login", "status": "error", "message": "missing"},
                        {"id": "tunnel-runtime", "status": "ok", "message": SECRET},
                        {"id": "connector", "status": "warning", "message": "not attached"},
                    ],
                }
            ),
            encoding="utf-8",
        )
        second = _run(home, "start", pin=pin)
        assert first["_exit_code"] == 0
        assert second["_exit_code"] == 0
        assert first["process"]["pid"] == second["process"]["pid"]
        assert first["process"]["executable"].endswith(ENTRY_NAME)
        assert _entry_pids(home) == [first["process"]["pid"]]
        assert marker.is_file()
        with socket.create_connection(("127.0.0.1", first["process"]["diagnostic_port"]), timeout=2):
            pass
        status = _run(home, "status", pin=pin)
        assert status["login"]["state"] == "signed_out"
        assert status["browser_smoke"]["state"] == "not_run"
        assert status["tunnel"]["state"] == "ready"
        assert status["connector"]["selectable"] is False
        assert status["ready"] is False
        assert SECRET not in json.dumps(status)
        assert "[redacted]" in json.dumps(status["tunnel"])
        log = (home / "web-home" / "argv.log").read_text(encoding="utf-8")
        assert "\nsetup\n" not in f"\n{log}"
        assert " dev\n" not in log
        assert "--replace-codex-route" not in log
    finally:
        _stop(home, pin)


def test_diagnostic_page_does_not_become_a_signed_in_account(tmp_path: Path) -> None:
    home = tmp_path / "runtime"
    marker = home / "executed-marker"
    archive = _archive(tmp_path, _fixture_script(marker))
    pin = _pin_for(tmp_path, archive.read_bytes())
    assert _run(home, "install", "--source", str(archive), pin=pin)["_exit_code"] == 0
    try:
        opened = _run(home, "open-login", pin=pin)
        assert opened["_exit_code"] == 0
        assert opened["login"]["state"] == "signed_out"
        assert opened["login"]["window"] == "open"
        assert opened["ready"] is False
        assert opened["login_url"].startswith("http://127.0.0.1:")
        assert opened["login_url"].endswith("/login")
        with urllib.request.urlopen(opened["login_url"], timeout=2) as response:
            page = response.read().decode("utf-8")
        assert "does not mark the runtime signed in" in page
        assert "/releases/download/v6.1.1/" in page
        assert "/releases/latest/" not in page
        assert SECRET not in page
        assert not (home / "account.json").exists()
        rejected = urllib.request.Request(
            opened["login_url"].rsplit("/", 1)[0] + "/account",
            data=json.dumps({"secret": SECRET}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as captured:
            urllib.request.urlopen(rejected, timeout=2)
        assert captured.value.code == 404
        assert SECRET not in captured.value.read().decode("utf-8")
        closed = _run(home, "close-login", pin=pin)
        assert closed["login"]["window"] == "closed"
        assert closed["login"]["state"] == "signed_out"
        stopped = _run(home, "stop", pin=pin)
        assert stopped["restart_required"] is True
        assert stopped["process"]["running"] is False
        disabled = _run(home, "disable", pin=pin)
        assert disabled["disabled"] is True
        assert disabled["restart_required"] is False
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
    before = {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (codex, claude, opencode, dev_home / "secret.txt")
    }
    home = tmp_path / "runtime"
    marker = home / "executed-marker"
    archive = _archive(tmp_path, _fixture_script(marker))
    pin = _pin_for(tmp_path, archive.read_bytes())
    env = {
        "HOME": str(user_home),
        "CODEX_HOME": str(codex.parent),
        "XDG_CONFIG_HOME": str(user_home / ".config"),
        "CODEX_WEB_GPT_DEV_HOME": str(dev_home),
    }
    assert _run(home, "install", "--source", str(archive), pin=pin, extra_env=env)["_exit_code"] == 0
    try:
        started = _run(home, "start", pin=pin, extra_env=env)
        assert started["_exit_code"] == 0
        assert started["upstream_executed"] is False
        assert started["login"]["state"] == "signed_out"
        config = json.loads((home / "web-home" / "config.json").read_text(encoding="utf-8"))
        assert config["mode"] == "browser-only"
        assert "purpose" not in config
        assert config["runtimeCommand"] == [str(home / "current" / "runtime" / ENTRY_NAME)]
        assert not (home / "web-home" / "browser" / "storage-state.json").exists()
        log = (home / "web-home" / "argv.log").read_text(encoding="utf-8")
        assert f"CODEX_HOME={home / 'codex-home'}" in log
        assert "DEV=" not in log
        assert str(codex.parent) not in log.split("CODEX_HOME=", 1)[1].split("\n", 1)[0]
        for path, digest in before.items():
            assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    finally:
        _stop(home, pin)
        for path, digest in before.items():
            assert hashlib.sha256(path.read_bytes()).hexdigest() == digest


def test_remote_bind_is_refused(tmp_path: Path) -> None:
    home = tmp_path / "runtime"
    marker = home / "executed-marker"
    archive = _archive(tmp_path, _fixture_script(marker))
    pin = _pin_for(tmp_path, archive.read_bytes())
    assert _run(home, "install", "--source", str(archive), pin=pin)["_exit_code"] == 0

    refused = _run(home, "start", pin=pin, extra_env={"CODEXHUB_CHATGPT_WEB_BIND": "0.0.0.0"})

    assert refused["_exit_code"] != 0
    assert "127.0.0.1" in refused["error"]
    assert _entry_pids(home) == []
    assert not marker.exists()
