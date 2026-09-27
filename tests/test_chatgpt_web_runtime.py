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

import chatgpt_web_collab
import chatgpt_web_route
import chatgpt_web_runtime
from gateway_errors import ModelIdentityResolutionError

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
        assert config["chromeExecutablePath"] == "/usr/bin/chromium"
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


@pytest.mark.parametrize("login_change", [
    "unchanged", "downgraded", "missing-state", "missing-marker", "malformed-marker",
    "unverified", "invalid-capabilities",
])
def test_restart_restores_only_verified_login_capabilities(tmp_path: Path, login_change: str) -> None:
    home = tmp_path / "runtime"
    archive = _archive(tmp_path, _fixture_script(home / "executed-marker"))
    pin = _pin_for(tmp_path, archive.read_bytes())
    assert _run(home, "install", "--source", str(archive), pin=pin)["_exit_code"] == 0
    state = home / "web-home" / "browser" / "storage-state.json"
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text('{"cookies": [], "origins": []}', encoding="utf-8")
    marker = state.with_name(state.name + ".verified.json")
    verified = {
        "version": 1, "authenticated": True, "verifiedAt": "2026-09-27T04:00:00Z",
        "solAvailable": True, "extraHighAvailable": True, "proAvailable": True,
    }
    marker.write_text(json.dumps(verified), encoding="utf-8")
    config_path = home / "web-home" / "config.json"
    try:
        assert _run(home, "start", pin=pin)["_exit_code"] == 0
        config = json.loads(config_path.read_text(encoding="utf-8"))
        for key in ("solAvailable", "extraHighAvailable", "proAvailable"):
            assert config[key] is True, key
        _stop(home, pin)
        # A later login change must supersede the old config, including a downgrade.
        if login_change == "missing-state":
            state.unlink()
        elif login_change == "missing-marker":
            marker.unlink()
        elif login_change == "malformed-marker":
            marker.write_text("{", encoding="utf-8")
        else:
            if login_change == "unverified":
                verified["authenticated"] = False
            elif login_change == "invalid-capabilities":
                verified.update(solAvailable=False, extraHighAvailable="true", proAvailable=1)
            elif login_change == "downgraded":
                verified.update(extraHighAvailable=False, proAvailable=False)
            marker.write_text(json.dumps(verified), encoding="utf-8")
        assert _run(home, "start", pin=pin)["_exit_code"] == 0
        config = json.loads(config_path.read_text(encoding="utf-8"))
        for key in ("solAvailable", "extraHighAvailable", "proAvailable"):
            expected = login_change == "unchanged" or (login_change == "downgraded" and key == "solAvailable")
            assert config[key] is expected, key
    finally:
        _stop(home, pin)


@pytest.mark.parametrize("scenario", ["valid", "missing", "corrupt", "traversal"])
def test_bundled_runtime_installs_only_verified_local_bytes(tmp_path: Path, scenario: str) -> None:
    home = tmp_path / "runtime"
    executed = home / "executed-marker"
    archive = _archive(tmp_path, _fixture_script(executed))
    pin = _pin_for(tmp_path, archive.read_bytes())
    document = json.loads(pin.read_text(encoding="utf-8"))
    document["artifacts"][chatgpt_web_runtime.artifact_key()]["bundled_filename"] = (
        "../runtime.tar.gz" if scenario == "traversal" else archive.name
    )
    pin.write_text(json.dumps(document), encoding="utf-8")
    if scenario == "missing":
        archive.unlink()
    elif scenario == "corrupt":
        archive.write_bytes(b"not the pinned archive")
    installed = _run(home, "install", pin=pin)
    assert (installed["_exit_code"] == 0) is (scenario == "valid")
    assert not executed.exists()
    assert (home / "current" / "runtime" / ENTRY_NAME).exists() is (scenario == "valid")
    if scenario != "valid":
        assert {"missing": "not a file", "corrupt": "checksum mismatch", "traversal": "filename beside"}[scenario] in installed["error"]


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


def _login_files(home: Path) -> dict[Path, bytes]:
    files = {
        home / "account" / "profile.json": b'{"login":"kept"}\n',
        home / "web-home" / "browser" / "storage-state.json": b'{"cookies":[]}\n',
    }
    for path, body in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    return files


def _assert_login_kept(files: dict[Path, bytes]) -> None:
    for path, body in files.items():
        assert path.read_bytes() == body


def _rejected_absent(log: str) -> None:
    for rejected in ("setup", "install.sh", "install-launcher.sh"):
        assert rejected not in log
    assert "\ndev\n" not in f"\n{log}"


def test_upgrade_bad_checksum_does_not_replace_current(tmp_path: Path) -> None:
    home = tmp_path / "runtime"
    marker = home / "executed-marker"
    archive = _archive(tmp_path, _fixture_script(marker))
    pin = _pin_for(tmp_path, archive.read_bytes())
    assert _run(home, "install", "--source", str(archive), pin=pin)["_exit_code"] == 0
    entry = home / "current" / "runtime" / ENTRY_NAME
    good = entry.read_bytes()
    login = _login_files(home)
    bad = _archive(tmp_path / "bad", _fixture_script(marker) + "\n# tampered\n")

    failed = _run(home, "upgrade", "--source", str(bad), pin=pin)

    assert failed["_exit_code"] != 0
    assert "checksum mismatch" in failed["error"]
    assert "not executed" in failed["error"]
    assert entry.read_bytes() == good
    _assert_login_kept(login)
    assert not marker.exists()
    assert not (home / "staging" / "payload.partial").exists()
    try:
        started = _run(home, "start", pin=pin)
        assert started["_exit_code"] == 0
        assert started["process"]["running"] is True
        assert entry.read_bytes() == good
    finally:
        _stop(home, pin)


def test_upgrade_interrupted_download_can_be_retried(tmp_path: Path) -> None:
    home = tmp_path / "runtime"
    marker = home / "executed-marker"
    archive = _archive(tmp_path, _fixture_script(marker))
    pin = _pin_for(tmp_path, archive.read_bytes())
    assert _run(home, "install", "--source", str(archive), pin=pin)["_exit_code"] == 0
    entry = home / "current" / "runtime" / ENTRY_NAME
    good = entry.read_bytes()
    login = _login_files(home)
    partial = home / "staging" / "payload.partial"
    partial.parent.mkdir(parents=True, exist_ok=True)
    partial.write_bytes(b"interrupted")
    incomplete = partial.with_name(partial.name + ".incomplete")
    incomplete.write_text("incomplete\n", encoding="utf-8")

    retried = _run(home, "upgrade", "--source", str(archive), pin=pin)

    assert retried["_exit_code"] == 0, retried
    assert not partial.exists()
    assert not incomplete.exists()
    assert entry.read_bytes() == good
    _assert_login_kept(login)
    assert not marker.exists()


def test_upgrade_downloads_only_the_pinned_artifact(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "runtime"
    marker = home / "executed-marker"
    archive = _archive(tmp_path, _fixture_script(marker))
    pin = _pin_for(tmp_path, archive.read_bytes())
    document = json.loads(pin.read_text(encoding="utf-8"))
    url = document["artifacts"][chatgpt_web_runtime.artifact_key()]["url"]
    assert "/releases/latest/" not in url
    assert url.startswith("https://github.com/miuuyy/codex-chatgpt-web/releases/download/v6.1.1/")
    monkeypatch.setenv("CODEXHUB_CHATGPT_WEB_PIN", str(pin))
    monkeypatch.delenv("CODEX_WEB_GPT_DEV_HOME", raising=False)

    class _Response:
        def __init__(self, payload: bytes) -> None:
            self._payload = payload
            self._offset = 0

        def geturl(self) -> str:
            return url

        def read(self, size: int) -> bytes:
            chunk = self._payload[self._offset : self._offset + size]
            self._offset += len(chunk)
            return chunk

        def __enter__(self) -> "_Response":
            return self

        def __exit__(self, *_args: object) -> bool:
            return False

    seen: list[str] = []

    def _urlopen(request: urllib.request.Request, timeout: int = 60) -> _Response:
        seen.append(request.full_url)
        return _Response(archive.read_bytes())

    monkeypatch.setattr(chatgpt_web_runtime.urllib.request, "urlopen", _urlopen)

    status = chatgpt_web_runtime.upgrade_runtime(home)

    assert seen == [url]
    assert status["component"]["compatible"] is True
    assert status["component"]["version"] == "6.1.1"
    assert (home / "current" / "runtime" / ENTRY_NAME).read_bytes() == (tmp_path / "tree" / ENTRY_NAME).read_bytes()
    assert not marker.exists()
    assert os.stat(home / "current" / "payload").st_mode & 0o111 == 0


def test_failed_promotion_keeps_previous_good(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "runtime"
    marker = home / "executed-marker"
    archive = _archive(tmp_path, _fixture_script(marker))
    pin = _pin_for(tmp_path, archive.read_bytes())
    monkeypatch.setenv("CODEXHUB_CHATGPT_WEB_PIN", str(pin))
    monkeypatch.delenv("CODEX_WEB_GPT_DEV_HOME", raising=False)
    assert chatgpt_web_runtime.install_runtime(home, archive)["installed"] is True
    first = home / "current" / "runtime" / "generation.txt"
    first.write_text("first", encoding="utf-8")
    assert chatgpt_web_runtime.install_runtime(home, archive)["installed"] is True
    assert (home / "previous-good" / "runtime" / "generation.txt").read_text(encoding="utf-8") == "first"
    (home / "current" / "runtime" / "generation.txt").write_text("second", encoding="utf-8")
    login = _login_files(home)
    real_rename = chatgpt_web_runtime.os.rename

    def _rename(src: object, dst: object) -> None:
        if Path(src).name == "incoming" and Path(dst).name == "current":
            raise OSError("simulated promotion failure")
        real_rename(src, dst)

    monkeypatch.setattr(chatgpt_web_runtime.os, "rename", _rename)

    with pytest.raises(RuntimeError, match="previous good install was kept"):
        chatgpt_web_runtime.upgrade_runtime(home, archive)

    assert (home / "current" / "runtime" / "generation.txt").read_text(encoding="utf-8") == "second"
    assert (home / "previous-good" / "runtime" / "generation.txt").read_text(encoding="utf-8") == "first"
    assert (home / "current" / "runtime" / ENTRY_NAME).read_bytes() == (tmp_path / "tree" / ENTRY_NAME).read_bytes()
    _assert_login_kept(login)
    assert not marker.exists()
    try:
        started = chatgpt_web_runtime.start_runtime(home)
        assert started["process"]["running"] is True
        assert (home / "current" / "runtime" / "generation.txt").read_text(encoding="utf-8") == "second"
    finally:
        chatgpt_web_runtime.stop_runtime(home, disable=False)


def test_upgrade_revokes_an_open_tool_permit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "runtime"
    marker = home / "executed-marker"
    archive = _archive(tmp_path, _fixture_script(marker))
    pin = _pin_for(tmp_path, archive.read_bytes())
    monkeypatch.setenv("CODEXHUB_CHATGPT_WEB_HOME", str(home))
    monkeypatch.setenv("CODEXHUB_CHATGPT_WEB_PIN", str(pin))
    monkeypatch.delenv("CODEX_WEB_GPT_DEV_HOME", raising=False)
    assert chatgpt_web_runtime.install_runtime(home, archive)["installed"] is True
    model_id = "chatgpt-web/gpt-5.6-sol"
    thread_id = "thread_upgrade_permit"
    turn_id = "turn_upgrade_permit"
    call_id = "call_upgrade_permit"
    try:
        started = chatgpt_web_runtime.start_runtime(home)
        assert started["process"]["running"] is True
        doctor = {
            "ok": True,
            "models": [{"id": model_id, "display_name": "Sol", "efforts": ["high"], "image_input": False}],
            "checks": [
                {"id": "login", "status": "ok"},
                {"id": "browser-smoke", "status": "ok"},
                {"id": "tunnel-runtime", "status": "ok"},
                {"id": "connector", "status": "ok"},
            ],
        }
        doctor_path = home / "web-home" / "doctor.json"
        doctor_path.write_text(json.dumps(doctor), encoding="utf-8")
        upstream = {"model_id": model_id, "upstream_model": model_id, "provider_id": "chatgpt-web"}
        metadata = json.dumps({"thread_id": thread_id, "turn_id": turn_id, "request_kind": "turn"})
        issue = {
            "model": model_id,
            "input": [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}],
            "prompt_cache_key": thread_id,
            "client_metadata": {"x-codex-turn-metadata": metadata},
            "reasoning": {"effort": "high"},
            "tools": [{"type": "function", "name": "shell", "parameters": {"type": "object"}}],
        }
        event_context: dict[str, object] = {}
        with chatgpt_web_route.submission_guard(event_context):
            assert chatgpt_web_route.prepare_responses_exchange(upstream, issue, event_context, None) is None
            chatgpt_web_route.observe_upstream_event(
                {
                    "type": "response.output_item.done",
                    "item": {
                        "type": "function_call",
                        "id": "fc_upgrade_permit",
                        "call_id": call_id,
                        "name": "shell",
                        "arguments": "{\"cmd\":\"pwd\"}",
                    },
                },
                event_context,
            )
            chatgpt_web_route.observe_upstream_event({"type": "response.completed", "response": {}}, event_context)
            chatgpt_web_route.note_upstream_submitted(event_context)
        login = _login_files(home)
        calls = {"tool": 0, "collab": 0}
        real_tool = chatgpt_web_route.revoke_all_tool_permissions
        real_collab = chatgpt_web_collab.revoke_all_permissions

        def _tool() -> None:
            calls["tool"] += 1
            real_tool()

        def _collab() -> None:
            calls["collab"] += 1
            real_collab()

        monkeypatch.setattr(chatgpt_web_route, "revoke_all_tool_permissions", _tool)
        monkeypatch.setattr(chatgpt_web_collab, "revoke_all_permissions", _collab)
        upgraded = chatgpt_web_runtime.upgrade_runtime(home, archive)
        assert calls["tool"] >= 1
        assert calls["collab"] >= 1
        _assert_login_kept(login)
        assert upgraded["component"]["compatible"] is True
        if upgraded["process"]["running"] is not True:
            chatgpt_web_runtime.start_runtime(home)
        continuation = {
            "model": model_id,
            "input": [
                {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
                {
                    "type": "function_call",
                    "id": "fc_upgrade_permit",
                    "call_id": call_id,
                    "name": "shell",
                    "arguments": "{\"cmd\":\"pwd\"}",
                },
                {"type": "function_call_output", "call_id": call_id, "output": "{\"ok\":true}"},
            ],
            "prompt_cache_key": thread_id,
            "client_metadata": {"x-codex-turn-metadata": metadata},
            "reasoning": {"effort": "high"},
        }
        with pytest.raises(ModelIdentityResolutionError, match="tool call expired"):
            chatgpt_web_route.prepare_responses_exchange(upstream, continuation, {}, None)
    finally:
        chatgpt_web_runtime.stop_runtime(home, disable=True)


def test_disable_removes_process_json_and_leaves_account_files(tmp_path: Path) -> None:
    home = tmp_path / "runtime"
    marker = home / "executed-marker"
    archive = _archive(tmp_path, _fixture_script(marker))
    pin = _pin_for(tmp_path, archive.read_bytes())
    assert _run(home, "install", "--source", str(archive), pin=pin)["_exit_code"] == 0
    try:
        started = _run(home, "start", pin=pin)
        assert started["_exit_code"] == 0
        assert (home / "process.json").is_file()
        login = _login_files(home)
        log_before = (home / "web-home" / "argv.log").read_text(encoding="utf-8")
        disabled = _run(home, "disable", pin=pin)
        assert disabled["_exit_code"] == 0, disabled
        assert disabled["disabled"] is True
        assert disabled["process"]["running"] is False
        assert not (home / "process.json").exists()
        _assert_login_kept(login)
        assert (home / "current" / "runtime" / ENTRY_NAME).is_file()
        log_after = (home / "web-home" / "argv.log").read_text(encoding="utf-8")
        _rejected_absent(log_after)
        assert log_after.startswith(log_before)
    finally:
        _stop(home, pin)


def test_delete_account_removes_account_files_and_does_not_run_during_disable(tmp_path: Path) -> None:
    home = tmp_path / "runtime"
    marker = home / "executed-marker"
    archive = _archive(tmp_path, _fixture_script(marker))
    pin = _pin_for(tmp_path, archive.read_bytes())
    assert _run(home, "install", "--source", str(archive), pin=pin)["_exit_code"] == 0
    try:
        assert _run(home, "start", pin=pin)["_exit_code"] == 0
        login = _login_files(home)
        disabled = _run(home, "disable", pin=pin)
        assert disabled["_exit_code"] == 0, disabled
        assert not (home / "process.json").exists()
        _assert_login_kept(login)
        log_before = (home / "web-home" / "argv.log").read_text(encoding="utf-8")
        deleted = _run(home, "delete-account", pin=pin)
        assert deleted["_exit_code"] == 0, deleted
        assert SECRET not in json.dumps(deleted)
        for path in login:
            assert not path.exists()
        assert not (home / "account").exists()
        assert (home / "current" / "runtime" / ENTRY_NAME).is_file()
        log_after = (home / "web-home" / "argv.log").read_text(encoding="utf-8")
        assert log_after == log_before
        _rejected_absent(log_after)
    finally:
        _stop(home, pin)
