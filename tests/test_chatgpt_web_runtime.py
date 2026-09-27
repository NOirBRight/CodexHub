"""Public CLI tests for extracting and starting the pinned ChatGPT Web Runtime."""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import socket
import subprocess
import sys
import threading
import time
import tarfile
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

import chatgpt_web_collab
import chatgpt_web_route
import chatgpt_web_runtime
import test_chatgpt_web_route as web
from gateway_errors import ModelIdentityResolutionError

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "src-python" / "chatgpt_web_runtime.py"
PIN_PATH = ROOT / "config" / "chatgpt_web_runtime_pin.json"
SECRET = "sk-chatgpt-web-test-secret-DO-NOT-LOG"
ENTRY_NAME = "bin/codex-chatgpt-web"


def _run(
    home: Path,
    *args: str,
    pin: Path | None = None,
    extra_env: dict[str, str] | None = None,
    stdin: str | None = None,
) -> dict:
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
        input=stdin,
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


def _fixture_health_server() -> str:
    return '''
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

home = Path(os.environ["CODEX_CHATGPT_WEB_HOME"])
config = json.loads((home / "config.json").read_text(encoding="utf-8"))

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def do_GET(self):
        if self.path != "/healthz":
            self.send_error(404)
            return
        body = json.dumps({
            "status": "ok",
            "service": "codex-chatgpt-web",
            "version": config["releaseVersion"],
            "mode": config["mode"],
            "pid": os.getpid(),
            "port": int(config["port"]),
            "accepting_turns": True,
        }).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        if os.environ.get("STARTUP_RUNTIME_OUTPUT"):
            print(os.environ["STARTUP_CONTROL_TOKEN"], os.environ["STARTUP_RUNTIME_KEY"], file=sys.stderr, flush=True)

server = ThreadingHTTPServer((config["host"], int(config["port"])), Handler)
server.serve_forever()
'''


def _fixture_script(marker: Path, health_server: str | None = None) -> str:
    health_server = _fixture_health_server() if health_server is None else health_server
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
if [ "$1" = "login" ]; then
  trap 'exit 1' TERM INT
  while [ ! -f "$CODEX_CHATGPT_WEB_HOME/finish-login" ]; do
    sleep 0.1
  done
  exit 0
fi
if [ "$1" = "serve" ]; then
  printf executed > '{marker}'
  exec {shlex.quote(sys.executable)} -c {shlex.quote(health_server)} "$0" "$@"
fi
exit 0
"""


def _delayed_start_failure_script(marker: Path) -> str:
    return f"""#!/bin/sh
set -eu
if [ "$1" = "doctor" ]; then
  printf '%s\\n' '{{"ok":false}}'
  exit 0
fi
if [ "$1" = "serve" ]; then
  : > {shlex.quote(str(marker))}
  while [ ! -f "$CODEX_CHATGPT_WEB_HOME/fail-startup" ]; do
    sleep 0.01
  done
  printf '%s\\n' 'codex-chatgpt-web: simulated delayed startup failure' >&2
  exit 1
fi
exit 0
"""


def _startup_diagnostics_failure_script() -> str:
    script = """#!/bin/sh
set -eu
if [ "$1" = "doctor" ]; then
  printf '%s\\n' '{"ok":false}'
  exit 0
fi
if [ "$1" = "serve" ]; then
  if [ -n "${STARTUP_CAUSE-}" ]; then
    for attempt in 1 2 3 4; do
      printf '%4096s' '' | tr ' ' x >&2
    done
    printf '\\n%s\\n' "$STARTUP_CAUSE $STARTUP_CONTROL_TOKEN $STARTUP_RUNTIME_KEY" >&2
    exit 1
  fi
  exec __PYTHON__ -c __HEALTH_SERVER__ "$0" "$@"
fi
exit 0
"""
    return script.replace("__PYTHON__", shlex.quote(sys.executable)).replace(
        "__HEALTH_SERVER__", shlex.quote(_fixture_health_server())
    )


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


def _fake_tunnel_client_script(state_path: Path) -> str:
    template = '''#!__PYTHON__
import json
import os
import sys
import time
from pathlib import Path

state_path = Path(__STATE_PATH__)
args = sys.argv[1:]

def option(name):
    return args[args.index(name) + 1]

def read_state():
    return json.loads(state_path.read_text(encoding="utf-8")) if state_path.is_file() else None

def write_state(state):
    state_path.write_text(json.dumps(state), encoding="utf-8")

if args == ["--version"]:
    print("tunnel-client 0.0.12")
    raise SystemExit(0)

state = read_state()
if args[:2] == ["runtimes", "cleanup"]:
    entries = [] if not state or state["runtime_state"] == "stopped" else [{
        "alias": state["alias"], "tunnel_id": state["tunnel_id"], "runtime_state": state["runtime_state"]
    }]
    print(json.dumps({"entries": entries}))
    print("warning: inventory is JSON on stdout", file=sys.stderr)
elif args[:2] == ["runtimes", "connect"]:
    state = {
        "alias": option("--alias"),
        "tunnel_id": option("--tunnel-id"),
        "profile_dir": option("--profile-dir"),
        "mcp_command": option("--mcp-command"),
        "runtime_state": "ready",
        "mode": "process",
        "pid": 424242,
    }
    write_state(state)
    mode = os.environ.get("FAKE_TUNNEL_CONNECT_MODE", "success")
    if mode in {"timeout", "delayed_success"}:
        time.sleep(float(os.environ.get("FAKE_TUNNEL_CONNECT_DELAY", "5")))
    print(json.dumps({"running": True, "healthy": mode in {"success", "delayed_success"}, "launched": True}))
    print("warning: connect details are on stderr", file=sys.stderr)
    raise SystemExit(2 if mode == "nonzero" else 0)
elif args[:2] == ["runtimes", "status"]:
    print(json.dumps({
        "alias": state["alias"],
        "tunnel_id": state["tunnel_id"],
        "runtime_state": state["runtime_state"],
        "process_running": state["runtime_state"] != "stopped",
        "process": {
            "mode": state["mode"], "pid": state["pid"], "profile_dir": state["profile_dir"]
        },
        "tmux": {"running": False},
    }))
elif args[:2] == ["runtimes", "stop"]:
    state["runtime_state"] = "stopped"
    write_state(state)
    print(json.dumps({"stopped": True}))
else:
    print("unexpected tunnel command", file=sys.stderr)
    raise SystemExit(3)
'''
    return template.replace("__PYTHON__", sys.executable).replace("__STATE_PATH__", repr(str(state_path)))


def test_broker_endpoint_is_platform_appropriate_and_survives_tunnel_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(chatgpt_web_runtime, "sys", SimpleNamespace(platform="win32"))
    home = tmp_path / "Managed Runtime with spaces"
    entry = home / "current" / "runtime" / ENTRY_NAME
    chatgpt_web_runtime._write_minimum_config(home, entry)
    web_home = home / "web-home"
    config = json.loads((web_home / "config.json").read_text(encoding="utf-8"))
    identity = hashlib.sha256(str(web_home.resolve()).lower().encode("utf-8")).hexdigest()[:20]
    endpoint = rf"\\.\pipe\codex-chatgpt-web-{identity}"

    assert config["brokerSocketPath"] == endpoint
    assert str(web_home) not in endpoint
    assert not (web_home / "socket").exists()
    assert shlex.split(chatgpt_web_runtime._tunnel_mcp_command(entry, endpoint)) == [
        str(entry), "mcp", "--contract", "native", "--broker-socket", endpoint,
    ]

    other_home = tmp_path / "Other Managed Runtime"
    chatgpt_web_runtime._write_minimum_config(other_home, other_home / ENTRY_NAME)
    other_config = json.loads((other_home / "web-home" / "config.json").read_text(encoding="utf-8"))
    assert other_config["brokerSocketPath"] != endpoint

    monkeypatch.setattr(chatgpt_web_runtime, "sys", SimpleNamespace(platform="linux"))
    chatgpt_web_runtime._write_minimum_config(home, entry)
    linux_config = json.loads((web_home / "config.json").read_text(encoding="utf-8"))
    assert linux_config["brokerSocketPath"] == str(web_home / "socket" / "turn-broker.sock")


def _prepare_full_tunnel(home: Path, pin: Path) -> tuple[Path, Path]:
    entry = home / "current" / "runtime" / ENTRY_NAME
    chatgpt_web_runtime.save_settings(
        home,
        {
            "mode": "full",
            "tunnel": {
                "tunnel_id": "tunnel_0123456789abcdef0123456789abcdef",
                "profile_name": "codex-chatgpt-web",
                "alias": "managed-runtime",
                "runtime_key": {"action": "replace", "value": SECRET},
            },
        },
    )
    chatgpt_web_runtime._write_minimum_config(home, entry)
    binary = chatgpt_web_runtime._managed_tunnel_binary(home)
    binary.parent.mkdir(parents=True, exist_ok=True)
    binary.write_text(_fake_tunnel_client_script(home / "web-home" / "tunnel-state.json"), encoding="utf-8")
    binary.chmod(0o700)
    pin_document = json.loads(pin.read_text(encoding="utf-8"))
    artifact = pin_document["tunnel_client"]["artifacts"][chatgpt_web_runtime.artifact_key()]
    manifest = {
        "version": 1,
        "tunnelClientVersion": chatgpt_web_runtime.TUNNEL_CLIENT_VERSION,
        "asset": artifact["filename"],
        "archiveSha256": artifact["sha256"],
        "binarySha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
    }
    chatgpt_web_runtime._tunnel_manifest_path(home).write_text(json.dumps(manifest), encoding="utf-8")
    return entry, home / "web-home" / "tunnel-state.json"


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
    source = document["runtime_source"]
    assert source["commit"] == document["commit"]
    assert source["build_revision"] == "93b8e6fc3eda8a81176964be87f8c7b8fc637a7f"
    assert source["git_tree"] == "641caaf875fcf908dfaa919c242f24fdede26a69"
    assert source["contract"] == "admin-status-v1"
    assert source["patch_sha256"] == "6e89c190cd6d01126a3d64d52bf09cb6a5fde8f24361f37e3056f8d1729104e1"
    patch = ROOT / source["patch_file"]
    assert hashlib.sha256(patch.read_bytes()).hexdigest() == source["patch_sha256"]
    assert document["license"] == "MIT"
    assert "setup" in document["rejected_entries"]
    assert "dev" in document["rejected_entries"]
    assert "--replace-codex-route" in document["rejected_entries"]
    assert set(document["artifacts"]) == {"linux-x64", "windows-x64"}
    expected_artifacts = {
        "linux-x64": "e370f6916e1d9e4bc60af81ef79269f16970e34c985db3f625e20c891dfc2782",
        "windows-x64": "5cbb11d6d848018d1f079c89151f4dd46cc7d5ebb83a878970626b5120b35a95",
    }
    for key, artifact in document["artifacts"].items():
        assert artifact["bundled_only"] is True
        assert artifact["sha256"] == expected_artifacts[key]
        assert artifact["build_revision"] == source["build_revision"]
        assert artifact["git_tree"] == source["git_tree"]
        assert "url" not in artifact


def test_bundled_only_runtime_never_falls_back_to_upstream_download(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "runtime"
    archive = _archive(tmp_path, _fixture_script(tmp_path / "executed-marker"))
    pin = _pin_for(tmp_path, archive.read_bytes())
    document = json.loads(pin.read_text(encoding="utf-8"))
    artifact = document["artifacts"][chatgpt_web_runtime.artifact_key()]
    artifact["bundled_filename"] = archive.name
    artifact["bundled_only"] = True
    artifact.pop("url", None)
    pin.write_text(json.dumps(document), encoding="utf-8")
    archive.unlink()

    def reject_download(*_args: object, **_kwargs: object) -> None:
        pytest.fail("bundled-only runtime attempted a download")

    monkeypatch.setattr(chatgpt_web_runtime.urllib.request, "urlopen", reject_download)
    result = _run(home, "install", pin=pin)

    assert result["_exit_code"] != 0
    assert result["error"] == "required patched ChatGPT Web Runtime bundle is missing"
    assert not (home / "current").exists()


def test_settings_api_redacts_runtime_key_and_supports_explicit_secret_actions(tmp_path: Path) -> None:
    home = tmp_path / "runtime"
    initial = _run(
        home,
        "settings-save",
        stdin=json.dumps({"tunnel": {"runtime_key": {"action": "replace", "value": SECRET}}}),
    )

    assert initial["_exit_code"] == 0
    assert initial["saved"]["tunnel"]["runtime_key_configured"] is True
    assert SECRET not in json.dumps(initial)
    saved_file = home / "runtime-settings.json"
    assert saved_file.stat().st_mode & 0o777 == 0o600

    kept = _run(home, "settings-save", stdin=json.dumps({"tunnel": {"runtime_key": {"action": "keep"}}}))
    assert kept["_exit_code"] == 0
    assert json.loads(saved_file.read_text(encoding="utf-8"))["tunnel"]["runtime_key"] == SECRET
    assert SECRET not in json.dumps(_run(home, "settings-get"))

    replacement = "sk-chatgpt-web-replacement-secret-DO-NOT-LOG"
    replaced = _run(
        home,
        "settings-save",
        stdin=json.dumps({"tunnel": {"runtime_key": {"action": "replace", "value": replacement}}}),
    )
    assert replaced["_exit_code"] == 0
    assert SECRET not in saved_file.read_text(encoding="utf-8")
    assert replacement not in json.dumps(replaced)

    cleared = _run(home, "settings-save", stdin=json.dumps({"tunnel": {"runtime_key": {"action": "clear"}}}))
    assert cleared["_exit_code"] == 0
    assert cleared["saved"]["tunnel"]["runtime_key_configured"] is False
    assert replacement not in json.dumps(cleared)
    short_key = _run(
        home,
        "settings-save",
        stdin=json.dumps({"tunnel": {"runtime_key": {"action": "replace", "value": "short"}}}),
    )
    assert short_key["_exit_code"] == 0
    assert short_key["saved"]["tunnel"]["runtime_key_configured"] is True
    assert "short" not in json.dumps(short_key)


def test_failed_settings_write_keeps_previous_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "runtime"
    original = chatgpt_web_runtime.save_settings(home, {"connector_name": "Original"})
    path = home / "runtime-settings.json"
    original_bytes = path.read_bytes()
    replace = chatgpt_web_runtime.os.replace

    def fail_promotion(source: object, destination: object) -> None:
        if Path(destination) == path:
            raise OSError("simulated settings promotion failure")
        replace(source, destination)

    monkeypatch.setattr(chatgpt_web_runtime.os, "replace", fail_promotion)
    with pytest.raises(chatgpt_web_runtime.RuntimeError_, match="could not be saved"):
        chatgpt_web_runtime.save_settings(home, {"connector_name": "Changed"})

    assert path.read_bytes() == original_bytes
    assert chatgpt_web_runtime.read_settings(home)["saved"]["connector_name"] == "Original"


def test_start_migrates_legacy_options_and_preserves_service_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "runtime"
    marker = home / "executed-marker"
    archive = _archive(tmp_path, _fixture_script(marker))
    pin = _pin_for(tmp_path, archive.read_bytes())
    monkeypatch.setenv("CODEXHUB_CHATGPT_WEB_PIN", str(pin))
    assert _run(home, "install", "--source", str(archive), pin=pin)["_exit_code"] == 0
    old_token = "service-control-token-stable-across-restarts"
    legacy = {
        "version": 3,
        "mode": "browser-only",
        "browserInteractionMode": "automatic",
        "automaticAppName": "Legacy Connector",
        "contextWindow": 131072,
        "headed": False,
        "autoApproveToolCalls": True,
        "controlToken": old_token,
    }
    config_path = home / "web-home" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(legacy), encoding="utf-8")

    try:
        first = chatgpt_web_runtime.start_runtime(home)
        generated = json.loads(config_path.read_text(encoding="utf-8"))
        assert generated["automaticAppName"] == "Legacy Connector"
        assert generated["contextWindow"] == 131072
        assert generated["headed"] is False
        assert generated["autoApproveToolCalls"] is True
        assert generated["controlToken"] == old_token
        (home / "runtime-settings.json").unlink(missing_ok=True)
        chatgpt_web_runtime.stop_runtime(home, disable=False)
        second = chatgpt_web_runtime.start_runtime(home)
        generated_again = json.loads(config_path.read_text(encoding="utf-8"))
        assert second["process"]["running"] is True
        assert generated_again["automaticAppName"] == "Legacy Connector"
        assert generated_again["contextWindow"] == 131072
        assert generated_again["controlToken"] == old_token
        assert first["process"]["pid"] != second["process"]["pid"]
    finally:
        chatgpt_web_runtime.stop_runtime(home, disable=True)


def test_legacy_zero_risk_pro_is_visible_but_not_enabled_by_managed_serve(tmp_path: Path) -> None:
    home = tmp_path / "runtime"
    marker = home / "executed-marker"
    archive = _archive(tmp_path, _fixture_script(marker))
    pin = _pin_for(tmp_path, archive.read_bytes())
    assert _run(home, "install", "--source", str(archive), pin=pin)["_exit_code"] == 0
    config_path = home / "web-home" / "config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    legacy = {
        "version": 3,
        "mode": "browser-only",
        "browserInteractionMode": "automatic",
        "browserHost": "managed-chrome",
        "automaticAppName": "Legacy Connector",
        "zeroRiskProEnabled": True,
    }
    original = json.dumps(legacy).encode("utf-8")
    config_path.write_bytes(original)

    migrated = chatgpt_web_runtime.read_settings(home)["saved"]
    assert migrated["options"]["zero_risk_pro_enabled"] is True
    assert "zero_risk_pro_enabled" in migrated["unsupported_options"]
    with pytest.raises(chatgpt_web_runtime.RuntimeError_, match="Zero Risk Pro requires"):
        chatgpt_web_runtime.save_settings(home, {"options": {"zero_risk_pro_enabled": True}})

    refused = _run(home, "supervise", pin=pin)
    assert refused["_exit_code"] != 0
    assert "Zero Risk Pro requires" in refused["error"]
    assert config_path.read_bytes() == original
    assert not (home / "process.json").exists()


@pytest.mark.skipif(os.name == "nt", reason="uses a Unix executable fixture for the pinned Tunnel client")
def test_full_mode_starts_and_stops_owned_tunnel_with_spaced_mcp_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "Managed Runtime with spaces"
    marker = home / "executed-marker"
    archive = _archive(tmp_path, _fixture_script(marker))
    pin = _pin_for(tmp_path, archive.read_bytes())
    assert _run(home, "install", "--source", str(archive), pin=pin)["_exit_code"] == 0
    monkeypatch.setenv("CODEXHUB_CHATGPT_WEB_PIN", str(pin))
    entry, state_path = _prepare_full_tunnel(home, pin)

    try:
        started = chatgpt_web_runtime.start_runtime(home)
        assert started["process"]["running"] is True
        config = json.loads((home / "web-home" / "config.json").read_text(encoding="utf-8"))
        state = json.loads(state_path.read_text(encoding="utf-8"))
        owner = json.loads((home / "tunnel-ownership.json").read_text(encoding="utf-8"))
        assert state["runtime_state"] == "ready"
        assert owner["alias"] == config["tunnel"]["alias"]
        assert owner["pid"] == state["pid"]
        assert (chatgpt_web_runtime._managed_runtime_key(home).stat().st_mode & 0o777) == 0o600
        assert shlex.split(state["mcp_command"]) == [
            str(entry),
            "mcp",
            "--contract",
            "native",
            "--broker-socket",
            config["brokerSocketPath"],
        ]
        assert SECRET not in json.dumps(chatgpt_web_runtime.read_settings(home))
    finally:
        chatgpt_web_runtime.stop_runtime(home, disable=True)

    assert json.loads(state_path.read_text(encoding="utf-8"))["runtime_state"] == "stopped"
    assert not (home / "tunnel-ownership.json").exists()


@pytest.mark.skipif(os.name == "nt", reason="uses a Unix executable fixture for the pinned Tunnel client")
def test_settings_saved_during_startup_remain_pending_until_next_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "Managed Runtime"
    marker = home / "executed-marker"
    archive = _archive(tmp_path, _fixture_script(marker))
    pin = _pin_for(tmp_path, archive.read_bytes())
    assert _run(home, "install", "--source", str(archive), pin=pin)["_exit_code"] == 0
    monkeypatch.setenv("CODEXHUB_CHATGPT_WEB_PIN", str(pin))
    monkeypatch.setenv("FAKE_TUNNEL_CONNECT_MODE", "delayed_success")
    monkeypatch.setenv("FAKE_TUNNEL_CONNECT_DELAY", "1.5")
    _, state_path = _prepare_full_tunnel(home, pin)
    startup: dict[str, object] = {}

    def start() -> None:
        try:
            startup["status"] = chatgpt_web_runtime.start_runtime(home)
        except Exception as exc:
            startup["error"] = exc

    worker = threading.Thread(target=start)
    worker.start()
    try:
        deadline = time.monotonic() + 5
        while not state_path.is_file() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert state_path.is_file(), "Tunnel startup did not reach the controlled delay"
        assert worker.is_alive(), "startup completed before settings were saved"

        saved = chatgpt_web_runtime.save_settings(home, {"connector_name": "Pending Startup Settings"})
        assert saved["pending_restart"] is True

        worker.join(timeout=15)
        assert not worker.is_alive(), "runtime startup did not finish"
        assert "error" not in startup, startup.get("error")
        status = startup["status"]
        assert isinstance(status, dict)
        assert status["settings_pending_restart"] is True
        config = json.loads((home / "web-home" / "config.json").read_text(encoding="utf-8"))
        assert config["automaticAppName"] == chatgpt_web_runtime.CONNECTOR_NAME
        assert chatgpt_web_runtime.read_settings(home)["saved"]["connector_name"] == "Pending Startup Settings"
    finally:
        if worker.is_alive():
            try:
                chatgpt_web_runtime.stop_runtime(home, disable=True)
            except chatgpt_web_runtime.RuntimeError_:
                pass
            worker.join(timeout=15)
        if "status" in startup:
            chatgpt_web_runtime.stop_runtime(home, disable=True)


@pytest.mark.skipif(os.name == "nt", reason="uses a Unix executable fixture for the pinned Tunnel client")
@pytest.mark.parametrize("connect_mode", ["nonzero", "timeout"])
def test_failed_full_mode_connect_cleans_its_started_tunnel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    connect_mode: str,
) -> None:
    home = tmp_path / "Managed Runtime"
    marker = home / "executed-marker"
    archive = _archive(tmp_path, _fixture_script(marker))
    pin = _pin_for(tmp_path, archive.read_bytes())
    assert _run(home, "install", "--source", str(archive), pin=pin)["_exit_code"] == 0
    monkeypatch.setenv("CODEXHUB_CHATGPT_WEB_PIN", str(pin))
    monkeypatch.setenv("FAKE_TUNNEL_CONNECT_MODE", connect_mode)
    entry, state_path = _prepare_full_tunnel(home, pin)
    if connect_mode == "timeout":
        monkeypatch.setattr(chatgpt_web_runtime, "TUNNEL_CONNECT_TIMEOUT_SECONDS", 0.1)

    pin_document = json.loads(pin.read_text(encoding="utf-8"))
    expected_error = "startup failed" if connect_mode == "nonzero" else "timed out"
    with pytest.raises(chatgpt_web_runtime.RuntimeError_, match=expected_error):
        chatgpt_web_runtime._start_tunnel_runtime(home, pin_document, entry)

    assert json.loads(state_path.read_text(encoding="utf-8"))["runtime_state"] == "stopped"
    assert not (home / "tunnel-ownership.json").exists()


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
    assert status["login"]["state"] == "unknown"
    assert status["upstream_executed"] is False


def test_repeated_start_uses_one_entry_and_status_does_not_trust_doctor(tmp_path: Path) -> None:
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
                    "ok": True,
                    "models": [{"id": "chatgpt-web/gpt-5.6-sol", "efforts": ["high"]}],
                    "checks": [
                        {"id": "login", "status": "ok"},
                        {"id": "browser-smoke", "status": "ok"},
                        {"id": "tunnel-runtime", "status": "ok", "message": SECRET},
                        {"id": "connector", "status": "ok"},
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
        assert status["login"]["state"] == "unknown"
        assert status["browser_smoke"]["state"] == "not_run"
        assert status["tunnel"]["state"] == "not_started"
        assert status["connector"]["selectable"] is False
        assert status["ready"] is False
        assert SECRET not in json.dumps(status)
        log = (home / "web-home" / "argv.log").read_text(encoding="utf-8")
        assert "\nsetup\n" not in f"\n{log}"
        assert " dev\n" not in log
        assert "--replace-codex-route" not in log
        assert "doctor" not in log.splitlines()
    finally:
        _stop(home, pin)


@pytest.mark.skipif(os.name == "nt", reason="uses a Unix executable fixture")
def test_start_does_not_acknowledge_a_child_that_fails_during_initialization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "runtime"
    child_started = home / "web-home" / "child-started"
    archive = _archive(tmp_path, _delayed_start_failure_script(child_started))
    pin = _pin_for(tmp_path, archive.read_bytes())
    assert _run(home, "install", "--source", str(archive), pin=pin)["_exit_code"] == 0
    monkeypatch.setenv("CODEXHUB_CHATGPT_WEB_PIN", str(pin))

    status_response_ready = threading.Event()
    release_status_response = threading.Event()
    real_urlopen = chatgpt_web_runtime.urllib.request.urlopen

    def hold_supervisor_status(url: object, *args: object, **kwargs: object) -> object:
        response = real_urlopen(url, *args, **kwargs)
        if isinstance(url, str) and url.endswith("/status") and not status_response_ready.is_set():
            status_response_ready.set()
            if not release_status_response.wait(10):
                response.close()
                raise TimeoutError("test status-response gate timed out")
        return response

    monkeypatch.setattr(chatgpt_web_runtime.urllib.request, "urlopen", hold_supervisor_status)
    startup: dict[str, object] = {}

    def start() -> None:
        try:
            startup["status"] = chatgpt_web_runtime.start_runtime(home)
        except Exception as exc:
            startup["error"] = exc

    worker = threading.Thread(target=start)
    worker.start()
    try:
        deadline = time.monotonic() + 10
        while not child_started.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert child_started.is_file(), "upstream serve child did not start"
        assert status_response_ready.wait(10), "supervisor loopback status did not respond"

        (home / "web-home" / "fail-startup").touch()
        deadline = time.monotonic() + 10
        while (home / "process.json").exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not (home / "process.json").exists(), "failed child did not complete supervisor shutdown"
    finally:
        release_status_response.set()
        worker.join(timeout=10)

    assert not worker.is_alive(), "start call did not finish after child failure"
    assert "error" in startup, f"start reported a running child after it exited: {startup.get('status')}"
    assert "simulated delayed startup failure" in str(startup["error"])


@pytest.mark.skipif(os.name == "nt", reason="uses a Unix executable fixture")
def test_startup_diagnostics_are_current_bounded_and_secret_safe(tmp_path: Path) -> None:
    home = tmp_path / "runtime"
    runtime_key = "synthetic-tunnel-runtime-key-DO-NOT-LOG"
    control_token = "synthetic-control-token-0123456789-DO-NOT-LOG"
    archive = _archive(tmp_path, _startup_diagnostics_failure_script())
    pin = _pin_for(tmp_path, archive.read_bytes())
    assert _run(home, "install", "--source", str(archive), pin=pin)["_exit_code"] == 0
    assert _run(
        home,
        "settings-save",
        pin=pin,
        stdin=json.dumps({"tunnel": {"runtime_key": {"action": "replace", "value": runtime_key}}}),
    )["_exit_code"] == 0
    web_home = home / "web-home"
    web_home.mkdir(exist_ok=True)
    (web_home / "config.json").write_text(json.dumps({"controlToken": control_token}), encoding="utf-8")

    causes = ("first attempt: stale ephemeral path", "second attempt: current ephemeral path")
    for index, cause in enumerate(causes):
        result = _run(
            home,
            "start",
            pin=pin,
            extra_env={
                "STARTUP_CAUSE": cause,
                "STARTUP_CONTROL_TOKEN": control_token,
                "STARTUP_RUNTIME_KEY": runtime_key,
            },
        )
        assert result["_exit_code"] == 1
        error = json.dumps(result)
        assert cause in error
        assert runtime_key not in error
        assert control_token not in error
        if index:
            assert causes[0] not in error

    diagnostic = (home / "startup-diagnostic.log").read_text(encoding="utf-8")
    assert causes[1] in diagnostic
    assert causes[0] not in diagnostic
    assert (home / "startup-diagnostic.log").stat().st_size <= 8192
    assert not (home / "runtime-entry.log").exists()

    try:
        healthy = _run(
            home,
            "start",
            pin=pin,
            extra_env={
                "STARTUP_CONTROL_TOKEN": control_token,
                "STARTUP_RUNTIME_KEY": runtime_key,
                "STARTUP_RUNTIME_OUTPUT": "1",
            },
        )
        assert healthy["_exit_code"] == 0
        assert not (home / "startup-diagnostic.log").exists()
    finally:
        _stop(home, pin)

    log_files = [
        path for path in home.rglob("*")
        if path.is_file() and (path.suffix == ".log" or "diagnostic" in path.name)
    ]
    assert log_files
    for path in log_files:
        payload = path.read_bytes()
        assert runtime_key.encode() not in payload, path
        assert control_token.encode() not in payload, path


def test_owned_login_starts_cancels_and_does_not_claim_authentication(tmp_path: Path) -> None:
    home = tmp_path / "runtime"
    marker = home / "executed-marker"
    archive = _archive(tmp_path, _fixture_script(marker))
    pin = _pin_for(tmp_path, archive.read_bytes())
    assert _run(home, "install", "--source", str(archive), pin=pin)["_exit_code"] == 0
    try:
        opened = _run(home, "open-login", pin=pin)
        assert opened["_exit_code"] == 0
        assert opened["login"]["state"] == "unknown"
        assert opened["login"]["window"] == "open"
        assert opened["ready"] is False
        assert "login_url" not in opened
        login_url = f"http://127.0.0.1:{opened['process']['diagnostic_port']}/login"
        again = _run(home, "open-login", pin=pin)
        assert again["login"]["window"] == "open"
        log = (home / "web-home" / "argv.log").read_text()
        assert log.splitlines().count("login") == 1
        with urllib.request.urlopen(login_url, timeout=2) as response:
            page = response.read().decode("utf-8")
        assert "does not mark the runtime signed in" in page
        assert "/releases/download/v6.1.1/" in page
        assert "/releases/latest/" not in page
        assert SECRET not in page
        assert not (home / "account.json").exists()
        rejected = urllib.request.Request(
            login_url.rsplit("/", 1)[0] + "/account",
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
        assert closed["login"]["state"] == "unknown"
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
        assert started["login"]["state"] == "unknown"
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
        expected_errors = {
            "missing": "required patched ChatGPT Web Runtime bundle is missing",
            "corrupt": "checksum mismatch",
            "traversal": "filename beside",
        }
        assert expected_errors[scenario] in installed["error"]


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
    artifact = document["artifacts"][chatgpt_web_runtime.artifact_key()]
    # Keep the legacy public-release path covered for ordinary upstream pins.
    artifact.pop("bundled_only", None)
    artifact.pop("bundled_filename", None)
    artifact["url"] = (
        "https://github.com/miuuyy/codex-chatgpt-web/releases/download/v6.1.1/"
        "codex-chatgpt-web-linux-amd64.tar.gz"
    )
    url = artifact["url"]
    pin.write_text(json.dumps(document), encoding="utf-8")
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
        assert web._seed_check(home, monkeypatch)["state"] == "ready"
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
            web._set_browser_only_mode(home)
            chatgpt_web_runtime.start_runtime(home)
        assert web._seed_check(home, monkeypatch)["state"] == "ready"
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


def test_login_completion_requires_explicit_restart_and_stop_cancels_login(tmp_path: Path) -> None:
    home = tmp_path / "runtime"
    archive = _archive(tmp_path, _fixture_script(home / "marker"))
    pin = _pin_for(tmp_path, archive.read_bytes())
    assert _run(home, "install", "--source", str(archive), pin=pin)["_exit_code"] == 0
    try:
        opened = _run(home, "open-login", pin=pin)
        pid = opened["process"]["pid"]
        (home / "web-home" / "finish-login").touch()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            finished = _run(home, "status", pin=pin)
            if finished["login"]["window"] == "closed":
                break
        assert finished["restart_required"] is True
        assert finished["admitting"] is False
        assert finished["login"]["state"] == "unknown"  # Exit zero never fabricates authentication.
        assert finished["process"]["pid"] == pid
        restarted = _run(home, "start", pin=pin)
        assert restarted["process"]["pid"] != pid
        assert restarted["restart_required"] is False
        assert restarted["admitting"] is True
        (home / "web-home" / "finish-login").unlink()
        assert _run(home, "open-login", pin=pin)["login"]["window"] == "open"
        assert _run(home, "stop", pin=pin)["login"]["window"] == "closed"
    finally:
        _stop(home, pin)


def test_login_failure_is_visible_without_exposing_process_output(tmp_path: Path) -> None:
    home = tmp_path / "runtime"
    script = _fixture_script(home / "marker").replace('if [ "$1" = "login" ]; then',
        f'if [ "$1" = "login" ]; then\n  printf "{SECRET}" >&2\n  sleep 0.4\n  exit 9')
    archive = _archive(tmp_path, script)
    pin = _pin_for(tmp_path, archive.read_bytes())
    assert _run(home, "install", "--source", str(archive), pin=pin)["_exit_code"] == 0
    try:
        _run(home, "open-login", pin=pin)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status = _run(home, "status", pin=pin)
            if status["login"]["error"]:
                break
        assert status["login"]["error"] == "login_failed"
        assert status["login"]["window"] == "closed"
        assert status["restart_required"] is False
        assert SECRET not in json.dumps(status)
    finally:
        _stop(home, pin)


def test_concurrent_login_commands_are_acknowledged_without_duplicate_browser(tmp_path: Path) -> None:
    from concurrent.futures import ThreadPoolExecutor

    home = tmp_path / "runtime"
    archive = _archive(tmp_path, _fixture_script(home / "marker"))
    pin = _pin_for(tmp_path, archive.read_bytes())
    assert _run(home, "install", "--source", str(archive), pin=pin)["_exit_code"] == 0
    assert _run(home, "start", pin=pin)["_exit_code"] == 0
    try:
        with ThreadPoolExecutor(max_workers=3) as pool:
            results = list(pool.map(lambda _: _run(home, "open-login", pin=pin), range(3)))
        assert all(result["_exit_code"] == 0 for result in results), results
        assert all(result["login"]["window"] == "open" for result in results)
        assert (home / "web-home" / "argv.log").read_text().splitlines().count("login") == 1
        assert _run(home, "close-login", pin=pin)["login"]["window"] == "closed"
    finally:
        _stop(home, pin)


def test_legacy_supervisor_requires_explicit_restart_before_owned_login(tmp_path: Path) -> None:
    home = tmp_path / "runtime"
    archive = _archive(tmp_path, _fixture_script(home / "marker"))
    pin = _pin_for(tmp_path, archive.read_bytes())
    assert _run(home, "install", "--source", str(archive), pin=pin)["_exit_code"] == 0
    try:
        first = _run(home, "start", pin=pin)
        path = home / "process.json"
        record = json.loads(path.read_text())
        record.pop("login_control")
        path.write_text(json.dumps(record))
        assert _run(home, "status", pin=pin)["restart_required"] is True
        refused = _run(home, "open-login", pin=pin)
        assert refused["_exit_code"] != 0
        assert "Restart" in refused["error"]
        restarted = _run(home, "start", pin=pin)
        assert restarted["process"]["pid"] != first["process"]["pid"]
        assert restarted["restart_required"] is False
        assert _run(home, "open-login", pin=pin)["login"]["window"] == "open"
    finally:
        _stop(home, pin)
