"""Exercise Claude Code settings through the real CodexHub web bridge in an isolated home."""

from __future__ import annotations

try:
    from scripts.python_runtime_contract import require_python_313
except ModuleNotFoundError:
    from python_runtime_contract import require_python_313

require_python_313(__file__)

import argparse
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def invoke(port: int, command: str, args: dict[str, object]) -> dict[str, object]:
    body = json.dumps({"command": command, "args": args}).encode()
    request = Request(
        f"http://127.0.0.1:{port}/api/invoke",
        body,
        {"Content-Type": "application/json", "Origin": "http://127.0.0.1:1420"},
    )
    try:
        with urlopen(request, timeout=10) as response:
            payload = json.load(response)
    except HTTPError as error:
        payload = json.load(error)
    assert isinstance(payload, dict), f"{command}: non-JSON bridge response"
    return payload


def accepted(port: int, command: str, args: dict[str, object]) -> dict[str, object]:
    payload = invoke(port, command, args)
    assert payload.get("ok") is True, f"{command}: {payload.get('error', 'bridge rejected request')}"
    value = payload.get("value")
    assert isinstance(value, dict), f"{command}: missing result object"
    return value


def claude_info(port: int) -> dict[str, object]:
    payload = invoke(port, "list_gateway_clients", {"include_versions": False})
    assert payload.get("ok") is True, "client list failed"
    return next(client for client in payload["value"] if client["id"] == "claude")


def free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def browser_roundtrip(root: Path, env: dict[str, str], bridge_port: int,
                      settings_path: Path) -> None:
    frontend = Path(__file__).resolve().parents[1] / "frontend"
    port = free_port()
    vite_env = {**env, "VITE_CODEXHUB_BRIDGE_URL":
                f"http://127.0.0.1:{bridge_port}/api/invoke"}
    vite = subprocess.Popen(
        ["npm", "run", "dev", "--", "--port", str(port), "--strictPort"],
        cwd=frontend, env=vite_env, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        url = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 20
        while True:
            assert vite.poll() is None, "isolated Vite server exited before readiness"
            try:
                with urlopen(url, timeout=1):
                    break
            except (URLError, TimeoutError):
                if time.monotonic() >= deadline:
                    raise AssertionError("isolated Vite server did not become ready") from None
                time.sleep(0.1)
        subprocess.run(
            ["node", str(frontend / "scripts" / "e2e-claude-settings.mjs"),
             url, str(settings_path)],
            cwd=frontend, env=vite_env, check=True, timeout=90,
        )
    finally:
        vite.terminate()
        try:
            vite.wait(timeout=5)
        except subprocess.TimeoutExpired:
            vite.kill()
            vite.wait(timeout=5)


def real_cli_roundtrip(claude_bin: Path, config: Path, root: Path, gateway_port: int) -> None:
    """Prove Claude Code reads the settings just written by CodexHub."""
    evidence = root / "loopback-evidence"
    tool_file = root / "synthetic.txt"
    tool_file.write_text("synthetic local fixture\n")
    server = subprocess.Popen(
        [sys.executable, str(Path(__file__).with_name("claude_messages_loopback_harness.py")),
         "serve", "--out", str(evidence), "--port", str(gateway_port),
         "--model", "claude-codexhub-e2e-alpha", "--scenario", "text",
         "--max-output-tokens", "32768", "--tool-file", str(tool_file)],
        cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 10
        while True:
            assert server.poll() is None, "loopback Messages server exited before readiness"
            try:
                with socket.create_connection(("127.0.0.1", gateway_port), timeout=1):
                    break
            except OSError:
                if time.monotonic() >= deadline:
                    raise AssertionError("loopback Messages server did not become ready") from None
                time.sleep(0.1)
        proxy = f"http://127.0.0.1:{gateway_port}"
        cli_env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(root),
            "CLAUDE_CONFIG_DIR": str(config),
            "TMPDIR": str(root),
            "TERM": "dumb",
            "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_TELEMETRY": "1",
            "DISABLE_AUTOUPDATER": "1",
            "DISABLE_ERROR_REPORTING": "1",
            "HTTP_PROXY": proxy, "HTTPS_PROXY": proxy,
            "http_proxy": proxy, "https_proxy": proxy,
            "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
        }
        completed = subprocess.run(
            [str(claude_bin), "-p", "--permission-mode", "plan", "--output-format", "json",
             "reply with the word ok"],
            cwd=root, env=cli_env, capture_output=True, text=True, timeout=90,
        )
        if completed.returncode != 0:
            record_path = evidence / "requests.jsonl"
            protocols = []
            if record_path.exists():
                protocols = [(entry.get("protocol"), entry.get("method"),
                              str(entry.get("path", "")).split("?", 1)[0])
                             for line in record_path.read_text().splitlines()
                             if (entry := json.loads(line))]
            detail = (completed.stdout[:500] + " " + completed.stderr[:500]).replace(
                "synthetic-local-gateway-key", "<REDACTED>"
            )
            raise AssertionError(
                f"Claude Code exited {completed.returncode}; protocols={protocols}; detail={detail}"
            )
        answer = json.loads(completed.stdout)
        assert answer.get("is_error") is False, "Claude Code reported a failed response"
        records_path = evidence / "requests.jsonl"
        assert records_path.is_file(), "Claude Code did not contact the configured loopback route"
        records = [json.loads(line) for line in records_path.read_text().splitlines()]
        assert not any(record.get("egress_guard") for record in records), "CLI attempted proxy egress"
        messages = [record for record in records if record.get("protocol") == "messages"]
        assert messages, "Claude Code did not send an Anthropic Messages request"
        assert any(record.get("request", {}).get("model") == "claude-codexhub-e2e-alpha"
                   for record in messages), "Claude Code did not use the saved default model"
        assert all("authorization" in {key.lower() for key in record.get("headers", {})}
                   for record in messages), "Claude Code did not send the saved bearer credential"
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=5)


def run(binary: Path, claude_bin: Path | None, browser: bool) -> None:
    with tempfile.TemporaryDirectory(prefix="codexhub-claude-settings-e2e-") as directory:
        root = Path(directory)
        runtime = root / "runtime"
        config = root / ".claude"
        fake_bin = root / "bin"
        providers_path = runtime / "proxy" / "config" / "providers.toml"
        settings_path = runtime / "proxy" / "settings.json"
        for path in (config, fake_bin, providers_path.parent, root / "codex"):
            path.mkdir(parents=True)
        claude_path = config / "settings.json"
        claude_path.write_text(json.dumps({"theme": "dark", "env": {"EDITOR": "vim"}}))
        providers_path.write_text('''[[providers]]
id = "e2e"
name = "E2E"
base_url = "http://127.0.0.1:1/v1"
upstream_format = "anthropic_messages"
enabled = true

  [[providers.models]]
  id = "alpha"
  display_name = "Alpha"
  context_window = 100000
  max_output_tokens = 4096
  enabled = true

  [[providers.models]]
  id = "beta"
  display_name = "Beta"
  context_window = 100000
  max_output_tokens = 4096
  enabled = true
''')
        gateway_port = free_port()
        settings_path.write_text(json.dumps({
            "include_official_models": False,
            "auto_sync_clients": False,
            "gateway_client_key": "synthetic-local-gateway-key",
            "proxy_port": gateway_port,
        }))
        fake_claude = fake_bin / "claude"
        fake_claude.write_text("#!/bin/sh\necho '2.1.280 (Claude Code)'\n")
        fake_claude.chmod(0o700)
        env = os.environ.copy()
        for key in list(env):
            if key.startswith(("ANTHROPIC_", "CLAUDE_CODE_", "CODEXHUB_CLAUDE_")):
                env.pop(key)
        env.update({
            "HOME": str(root),
            "XDG_CONFIG_HOME": str(root / "config"),
            "XDG_CACHE_HOME": str(root / "cache"),
            "XDG_DATA_HOME": str(root / "data"),
            "CODEX_HOME": str(root / "codex"),
            "CODEXHUB_RUNTIME_HOME": str(runtime),
            "CODEXHUB_ROLLBACK_PROVENANCE_DIR": str(root / "rollback"),
            "CODEXHUB_CLAUDE_HOME": str(config),
            "CODEXHUB_RESOURCE_ROOT": str(Path(__file__).resolve().parents[1]),
            "CODEXHUB_PYTHON": sys.executable,
            "PATH": f"{fake_bin}{os.pathsep}{env.get('PATH', '')}",
        })
        port = free_port()
        process = subprocess.Popen(
            [str(binary), "web-bridge", "--port", str(port)],
            cwd=root, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            deadline = time.monotonic() + 15
            while True:
                assert process.poll() is None, "isolated web bridge exited before readiness"
                try:
                    before = claude_info(port)
                    break
                except (URLError, ConnectionError, TimeoutError):
                    if time.monotonic() >= deadline:
                        raise AssertionError("isolated web bridge did not become ready") from None
                    time.sleep(0.1)
            assert before["installed"] and before["claude_settings"]["default_model"] == ""
            assert before["route_mode"] == "official"

            if browser:
                browser_roundtrip(root, env, port, claude_path)
                assert claude_info(port)["route_mode"] == "official"

            first_roles = {"haiku": "e2e/beta", "sonnet": "", "opus": "", "fable": "", "subagent": ""}
            preview = accepted(port, "preview_gateway_client_config", {
                "client_id": "claude", "model": "e2e/alpha", "role_mappings": first_roles,
            })
            planned = json.loads(preview["next_redacted"])
            assert preview["can_apply"] is True
            assert planned["env"]["ANTHROPIC_MODEL"] == "claude-codexhub-e2e-alpha"
            assert planned["env"]["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "claude-codexhub-e2e-beta"
            assert "ANTHROPIC_AUTH_TOKEN" not in planned["env"]
            assert planned["env"]["ANTHROPIC_CUSTOM_HEADERS"] == "***"

            connected = accepted(port, "switch_gateway_client_route", {
                "client_id": "claude", "mode": "hub", "model": "e2e/alpha",
                "role_mappings": first_roles,
            })
            assert connected["applied"] is True
            written = json.loads(claude_path.read_text())
            assert written["theme"] == "dark" and written["env"]["EDITOR"] == "vim"
            assert written["env"]["ANTHROPIC_MODEL"] == planned["env"]["ANTHROPIC_MODEL"]
            assert written["env"]["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == planned["env"]["ANTHROPIC_DEFAULT_HAIKU_MODEL"]
            assert "ANTHROPIC_AUTH_TOKEN" not in written["env"]
            assert written["env"]["ANTHROPIC_CUSTOM_HEADERS"] == (
                "x-codexhub-gateway-key: synthetic-local-gateway-key"
            )
            assert {"claude-codexhub-e2e-alpha", "claude-codexhub-e2e-beta"} <= {
                row["model"] for row in written["modelPicker"]["options"]
            }
            readback = claude_info(port)
            assert readback["route_mode"] == "hub"
            assert readback["claude_settings"]["default_model"] == "e2e/alpha"
            assert readback["claude_settings"]["role_mappings"]["haiku"] == "e2e/beta"
            if claude_bin is not None:
                real_cli_roundtrip(claude_bin, config, root, gateway_port)

            changed_roles = {**first_roles, "haiku": "", "sonnet": "e2e/alpha"}
            accepted(port, "switch_gateway_client_route", {
                "client_id": "claude", "mode": "hub", "model": "e2e/beta",
                "role_mappings": changed_roles,
            })
            updated = json.loads(claude_path.read_text())
            assert updated["env"]["ANTHROPIC_MODEL"] == "claude-codexhub-e2e-beta"
            assert "ANTHROPIC_DEFAULT_HAIKU_MODEL" not in updated["env"]
            assert updated["env"]["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "claude-codexhub-e2e-alpha"
            assert claude_info(port)["claude_settings"]["default_model"] == "e2e/beta"

            snapshot = claude_path.read_bytes()
            invalid = invoke(port, "switch_gateway_client_route", {
                "client_id": "claude", "mode": "hub", "model": "e2e/removed",
                "role_mappings": changed_roles,
            })
            assert invalid["ok"] is False and claude_path.read_bytes() == snapshot

            updated["env"]["ANTHROPIC_API_KEY"] = "synthetic-conflicting-key"
            claude_path.write_text(json.dumps(updated))
            assert claude_info(port)["claude_settings"]["conflicts"]
            conflict_preview = accepted(port, "preview_gateway_client_config", {
                "client_id": "claude", "model": "e2e/beta", "role_mappings": changed_roles,
            })
            assert conflict_preview["can_apply"] is False
            snapshot = claude_path.read_bytes()
            conflict_apply = invoke(port, "switch_gateway_client_route", {
                "client_id": "claude", "mode": "hub", "model": "e2e/beta",
                "role_mappings": changed_roles,
            })
            assert conflict_apply["ok"] is False and claude_path.read_bytes() == snapshot

            detached = accepted(port, "switch_gateway_client_route", {
                "client_id": "claude", "mode": "official", "role_mappings": {},
            })
            assert detached["applied"] is True
            restored = json.loads(claude_path.read_text())
            assert restored["theme"] == "dark" and restored["env"]["EDITOR"] == "vim"
            assert restored["env"]["ANTHROPIC_API_KEY"] == "synthetic-conflicting-key"
            assert not any(key.startswith("CODEXHUB_") or key in {
                "ANTHROPIC_MODEL", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN",
                "ANTHROPIC_CUSTOM_HEADERS",
                "ANTHROPIC_DEFAULT_SONNET_MODEL",
            } for key in restored["env"])
            assert claude_info(port)["route_mode"] == "official"
            print("PASS: isolated Claude bridge preview, connect, edit, readback, invalid target, conflict, disconnect"
                  + ("; real Claude Code text roundtrip" if claude_bin else ""))
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bin", type=Path, required=True)
    parser.add_argument("--claude-bin", type=Path)
    parser.add_argument("--browser", action="store_true", help="exercise the real frontend in Chromium")
    args = parser.parse_args()
    binary = args.bin.resolve()
    if not binary.is_file():
        parser.error("--bin must point to a built CodexHub executable")
    claude_bin = args.claude_bin.resolve() if args.claude_bin else None
    if claude_bin is not None and not claude_bin.is_file():
        parser.error("--claude-bin must point to an installed Claude Code executable")
    run(binary, claude_bin, args.browser)


if __name__ == "__main__":
    main()
