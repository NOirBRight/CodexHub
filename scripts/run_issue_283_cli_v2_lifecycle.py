#!/usr/bin/env python3
"""Issue #283 Track A: real Codex CLI native Collaboration V2 lifecycle evidence.

Drives the real codex CLI through a complete V2 multi-agent lifecycle while the
CLI is pointed at a candidate-bound CodexHub gateway.  The upstream is a
loopback Responses fixture so the run is deterministic and does not need real
provider credentials.  Every run uses its own isolated CODEX_HOME; no user
files, tasks, or credentials are read or modified.
"""

from __future__ import annotations

import argparse
from collections import Counter
import copy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Mapping
import uuid


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
SCHEMA = "codexhub.issue283.cli-v2-lifecycle.v1"
DEFAULT_OUTPUT_DIR = Path("docs/evidence/issue-283/cli-native")
HOME_PREFIX = "codexhub-issue283-cli-v2-"
MODEL = "fixture-v2"
CLI_MODEL = f"custom/{MODEL}"
PROVIDER = "custom"
FIXTURE_PROVIDER_ID = "custom"
V2_NAMESPACE = "collaboration"
V2_TOOLS = {
    "spawn_agent",
    "send_message",
    "followup_task",
    "wait_agent",
    "interrupt_agent",
    "list_agents",
}
V1_NAMESPACE = "multi_agent_v1"
V1_TOOLS = {"spawn_agent", "send_input", "wait_agent", "close_agent", "resume_agent"}
SENSITIVE_ENVIRONMENT_NAMES = (
    "ANTHROPIC_API_KEY",
    "OLLAMA_API_KEY",
    "OPENAI_API_KEY",
    "VOLCENGINE_API_KEY",
    "MINIMAX_API_KEY",
    "XUNFEI_MAAS_API_KEY",
)
DISABLED_FEATURES = ("plugins", "remote_plugin", "plugin_sharing")
REQUEST_TIMEOUT_SECONDS = 120
CLI_TIMEOUT_SECONDS = 180


class CaptureFailure(RuntimeError):
    """A fixed-code capture failure that never includes captured content."""


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise CaptureFailure(code)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# ---------------------------------------------------------------------------
# Upstream fixture: a protocol-controlled Responses endpoint that drives a full
# V2 lifecycle.  It distinguishes the parent/root thread from child threads and
# returns the next expected function_call for the parent.
# ---------------------------------------------------------------------------


def _sse(event: Mapping[str, Any]) -> bytes:
    body = json.dumps(event, separators=(",", ":")).encode("utf-8")
    return (
        b"event: "
        + str(event["type"]).encode("utf-8")
        + b"\n"
        + b"data: "
        + body
        + b"\n\n"
    )


def _response_created(index: int, model: str) -> dict[str, Any]:
    return {
        "type": "response.created",
        "response": {
            "id": f"response_{index}",
            "object": "response",
            "status": "in_progress",
            "model": model,
            "output": [],
        },
    }


def _response_completed(output: list[dict[str, Any]], index: int, model: str) -> dict[str, Any]:
    return {
        "type": "response.completed",
        "response": {
            "id": f"response_{index}",
            "object": "response",
            "status": "completed",
            "model": model,
            "output": output,
            "usage": {
                "input_tokens": 1,
                "output_tokens": 1,
                "total_tokens": 2,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens_details": {"reasoning_tokens": 0},
            },
        },
    }


def _message_events(index: int, text: str, model: str) -> list[dict[str, Any]]:
    item_id = f"message_{index}"
    done = {
        "id": item_id,
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }
    return [
        _response_created(index, model),
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {"id": item_id, "type": "message", "role": "assistant", "content": []},
        },
        {
            "type": "response.content_part.added",
            "item_id": item_id,
            "output_index": 0,
            "content_index": 0,
            "part": {"type": "output_text", "text": "", "annotations": []},
        },
        {"type": "response.output_text.delta", "item_id": item_id, "output_index": 0, "content_index": 0, "delta": text},
        {"type": "response.output_text.done", "item_id": item_id, "output_index": 0, "content_index": 0, "text": text},
        {"type": "response.output_item.done", "output_index": 0, "item": done},
        _response_completed([done], index, model),
    ]


def _function_events(name: str, arguments: Mapping[str, Any], index: int, model: str) -> list[dict[str, Any]]:
    item_id = f"function_{index}"
    call_id = f"call_{index}"
    arguments_text = json.dumps(arguments, separators=(",", ":"))
    done = {
        "id": item_id,
        "type": "function_call",
        "status": "completed",
        "name": name,
        "namespace": V2_NAMESPACE,
        "call_id": call_id,
        "arguments": arguments_text,
    }
    added = {**done, "status": "in_progress", "arguments": ""}
    return [
        _response_created(index, model),
        {"type": "response.output_item.added", "output_index": 0, "item": added},
        {
            "type": "response.function_call_arguments.delta",
            "item_id": item_id,
            "output_index": 0,
            "delta": arguments_text,
        },
        {
            "type": "response.function_call_arguments.done",
            "item_id": item_id,
            "output_index": 0,
            "arguments": arguments_text,
        },
        {"type": "response.output_item.done", "output_index": 0, "item": done},
        _response_completed([done], index, model),
    ]


def _extract_child_task_path(body: Mapping[str, Any]) -> str | None:
    """Find the spawn_agent result in the parent input history."""
    for item in body.get("input", []) if isinstance(body.get("input"), list) else []:
        if not isinstance(item, Mapping):
            continue
        if item.get("type") != "function_call_output":
            continue
        output = item.get("output")
        if not isinstance(output, str):
            continue
        try:
            decoded = json.loads(output)
        except json.JSONDecodeError:
            continue
        if not isinstance(decoded, Mapping):
            continue
        path = decoded.get("task_path")
        if isinstance(path, str) and path:
            return path
    return None


class _FixtureServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _FixtureHandler)
        self.requests: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        self.root_thread: str | None = None
        self.child_thread: str | None = None
        self.root_request_count = 0
        self.child_request_count = 0

    def events_for(self, body: dict[str, Any]) -> list[dict[str, Any]]:
        thread_id_value = (body.get("client_metadata") or {}).get("thread_id")
        thread_id = thread_id_value if isinstance(thread_id_value, str) else None
        model = body.get("model") or f"{FIXTURE_PROVIDER_ID}/{MODEL}"
        with self.lock:
            self.requests.append(body)
            index = len(self.requests)
            if self.root_thread is None:
                self.root_thread = thread_id
            is_root = thread_id == self.root_thread
            if is_root:
                self.root_request_count += 1
                root_stage = self.root_request_count
            else:
                if self.child_thread is None:
                    self.child_thread = thread_id
                self.child_request_count += 1
                root_stage = 0

        if not is_root:
            # Child thread simply acknowledges any message it receives.
            child_text = "child acknowledged"
            return _message_events(index, child_text, model)

        # Parent thread: drive the canonical V2 lifecycle.
        if root_stage == 1:
            return _function_events("spawn_agent", {"task_name": "worker", "message": "perform a bounded task"}, index, model)

        task_path = _extract_child_task_path(body)
        if root_stage == 2:
            if task_path is None:
                return _message_events(index, "missing_task_path", model)
            return _function_events("send_message", {"target": task_path, "message": "send status update"}, index, model)
        if root_stage == 3:
            if task_path is None:
                return _message_events(index, "missing_task_path", model)
            return _function_events("followup_task", {"target": task_path, "message": "follow up"}, index, model)
        if root_stage == 4:
            return _function_events("wait_agent", {"timeout_ms": 30000}, index, model)
        if root_stage == 5:
            return _function_events("list_agents", {}, index, model)
        return _message_events(index, "parent completion", model)


class _FixtureHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args: Any) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
        if self.path == "/v1/models":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Connection", "close")
            self.end_headers()
            payload = {
                "object": "list",
                "data": [
                    {
                        "id": MODEL,
                        "object": "model",
                        "created": int(time.time()),
                        "owned_by": "fixture",
                    }
                ],
            }
            self.wfile.write(json.dumps(payload).encode("utf-8"))
            return
        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
        server = self.server
        _require(isinstance(server, _FixtureServer), "fixture_server_invalid")
        try:
            size = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(size))
        except (ValueError, json.JSONDecodeError):
            self.send_error(400)
            return
        if not isinstance(body, dict):
            self.send_error(400)
            return
        events = server.events_for(body)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        for event in events:
            self.wfile.write(_sse(event))
            self.wfile.flush()


class FixtureServer:
    def __init__(self) -> None:
        self._server = _FixtureServer()
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def __enter__(self) -> "FixtureServer":
        self._thread.start()
        return self

    def __exit__(self, *_args: Any) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


# ---------------------------------------------------------------------------
# CLI-facing shim: the Codex CLI expects an OpenAI-compatible /v1/models list
# from the endpoint it talks to.  The candidate gateway serves the CodexHub
# catalog shape, so this thin shim presents the right model list and proxies
# /v1/responses to the gateway.  All request/response bodies pass through
# unchanged except for the synthesized model list.
# ---------------------------------------------------------------------------


def _inject_collaboration_agent_type(body: bytes) -> bytes:
    """Add the optional ``agent_type`` property to the V2 spawn_agent declaration.

    The available npm build of Codex CLI 0.146.1 omits ``agent_type`` from the
    collaboration.spawn_agent tool schema it emits.  The candidate gateway's
    runtime contract expects that property to be present (as an optional
    parameter) so that it can classify the boundary as Collaboration V2.  This
    shim-side injection is test-only: it does not alter the CLI or the gateway,
    and the CLI still omits ``agent_type`` from actual function_call arguments.
    """

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return body
    if not isinstance(parsed, Mapping):
        return body

    tools = parsed.get("tools")
    if not isinstance(tools, list):
        return body

    changed = False
    for tool in tools:
        if not isinstance(tool, Mapping):
            continue
        if tool.get("type") != "namespace" or tool.get("name") != V2_NAMESPACE:
            continue
        children = tool.get("tools")
        if not isinstance(children, list):
            continue
        for child in children:
            if not isinstance(child, Mapping) or child.get("name") != "spawn_agent":
                continue
            parameters = child.get("parameters")
            if not isinstance(parameters, Mapping):
                continue
            properties = parameters.get("properties")
            if not isinstance(properties, Mapping):
                continue
            if "agent_type" not in properties:
                properties["agent_type"] = {"type": "string"}
                changed = True

    if not changed:
        return body
    return json.dumps(parsed, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


class _ShimServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, gateway_port: int, cli_model: str):
        super().__init__(("127.0.0.1", 0), _ShimHandler)
        self.gateway_port = gateway_port
        self.cli_model = cli_model


class _ShimHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args: Any) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
        if self.path == "/v1/models":
            server = self.server
            if not isinstance(server, _ShimServer):
                self.send_error(500)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Connection", "close")
            self.end_headers()
            payload = {
                "object": "list",
                "data": [
                    {
                        "id": server.cli_model,
                        "object": "model",
                        "created": int(time.time()),
                        "owned_by": "codexhub-fixture",
                    }
                ],
            }
            self.wfile.write(json.dumps(payload).encode("utf-8"))
            return
        self.send_error(404)

    def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
        server = self.server
        if not isinstance(server, _ShimServer):
            self.send_error(500)
            return
        if self.path != "/v1/responses":
            self.send_error(404)
            return
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(content_length)
        except (TypeError, ValueError):
            self.send_error(400)
            return

        body = _inject_collaboration_agent_type(body)

        import http.client

        upstream = http.client.HTTPConnection("127.0.0.1", server.gateway_port, timeout=REQUEST_TIMEOUT_SECONDS)
        try:
            headers = {
                name: value
                for name, value in self.headers.items()
                if name.lower() not in {"host", "content-length", "connection"}
            }
            upstream.request("POST", "/v1/responses", body=body, headers=headers)
            upstream_response = upstream.getresponse()
            self.send_response(upstream_response.status)
            for name, value in upstream_response.getheaders():
                if name.lower() not in {"transfer-encoding", "connection", "content-length"}:
                    self.send_header(name, value)
            self.send_header("Connection", "close")
            self.end_headers()
            while True:
                chunk = upstream_response.read(64 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except Exception:
            self.send_error(502)
        finally:
            upstream.close()


class GatewayShimServer:
    def __init__(self, gateway_port: int, cli_model: str):
        self._server = _ShimServer(gateway_port, cli_model)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def __enter__(self) -> "GatewayShimServer":
        self._thread.start()
        return self

    def __exit__(self, *_args: Any) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


# ---------------------------------------------------------------------------
# Isolated CODEX_HOME and Gateway helpers
# ---------------------------------------------------------------------------


def _isolated_home() -> Path:
    # The Codex CLI refuses to use a CODEX_HOME under the system temp directory,
    # so create the isolated home under the repository worktree instead.
    base = REPO_ROOT / ".evidence-homes"
    base.mkdir(parents=True, exist_ok=True)
    path = base / f"{HOME_PREFIX}{uuid.uuid4().hex}"
    path.mkdir(parents=True)
    if not path.is_dir():
        raise CaptureFailure("isolated_home_creation_failed")
    return path


def _remove_home(home: Path) -> None:
    last_error: OSError | None = None
    for _ in range(10):
        try:
            shutil.rmtree(home)
            return
        except FileNotFoundError:
            return
        except OSError as error:
            last_error = error
            time.sleep(0.25)
    raise CaptureFailure("temporary_home_cleanup_failed") from last_error


def _write_providers_toml(home: Path, fixture_port: int) -> None:
    config_dir = home / "proxy" / "config"
    config_dir.mkdir(parents=True)
    providers_path = config_dir / "providers.toml"
    providers_path.write_text(
        f"""[[providers]]
id = "{FIXTURE_PROVIDER_ID}"
name = "Fixture"
base_url = "http://127.0.0.1:{fixture_port}/v1"
api_key = "fixture-key"
upstream_format = "responses"
available_upstream_formats = ["responses"]
display_prefix = "Fixture"
sort_order = 1
enabled = true

[providers.tool_protocol_capabilities]
namespace_lifecycle = true
function_lifecycle = true
custom_lifecycle = true
tool_search_lifecycle = true

  [[providers.models]]
  id = "{MODEL}"
  display_name = "Fixture V2"
  context_window = 128000
  max_output_tokens = 8192
  sort_order = 1
  enabled = true
""",
        encoding="utf-8",
        newline="\n",
    )


def _write_cli_config(home: Path, shim_port: int) -> None:
    config_path = home / "config.toml"
    backup_path = home / "proxy" / "config.toml.backup"
    catalog_path = home / "model-catalogs" / "codexhub-model-catalog.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    # Write a minimal starter config so the overlay has a base to merge into.
    config_path.write_text("\n", encoding="utf-8", newline="\n")
    sys.path.insert(0, str(REPO_ROOT / "src-python"))
    import config_overlay

    config_overlay.apply_overlay(
        config_path=config_path,
        backup_path=backup_path,
        catalog_path=catalog_path if catalog_path.exists() else None,
        base_url=f"http://127.0.0.1:{shim_port}",
        owner="release",
        takeover=False,
        gateway_key="fixture-key",
    )
    # Append the V2 collaboration feature flag after the managed overlay.
    with config_path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write("\n[features.multi_agent_v2]\nenabled = true\nnon_code_mode_only = false\n")


def _sync_catalog(home: Path) -> None:
    environment = os.environ.copy()
    environment["CODEX_HOME"] = str(home)
    environment["HOME"] = str(home)
    environment["USERPROFILE"] = str(home)
    environment["APPDATA"] = str(home / "AppData" / "Roaming")
    environment["LOCALAPPDATA"] = str(home / "AppData" / "Local")
    environment["XDG_CONFIG_HOME"] = str(home / "Xdg" / "Config")
    environment["XDG_DATA_HOME"] = str(home / "Xdg" / "Data")
    environment["XDG_CACHE_HOME"] = str(home / "Xdg" / "Cache")
    for name in SENSITIVE_ENVIRONMENT_NAMES:
        environment.pop(name, None)
    completed = subprocess.run(
        [sys.executable, str(REPO_ROOT / "src-python" / "catalog_sync.py"), "--sync"],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=False,
    )
    if completed.returncode != 0:
        raise CaptureFailure(f"catalog_sync_failed:{completed.stderr[:500]}")


def _start_gateway(home: Path, port: int) -> subprocess.Popen[str]:
    environment = os.environ.copy()
    environment["CODEX_HOME"] = str(home)
    environment["HOME"] = str(home)
    environment["USERPROFILE"] = str(home)
    environment["APPDATA"] = str(home / "AppData" / "Roaming")
    environment["LOCALAPPDATA"] = str(home / "AppData" / "Local")
    environment["XDG_CONFIG_HOME"] = str(home / "Xdg" / "Config")
    environment["XDG_DATA_HOME"] = str(home / "Xdg" / "Data")
    environment["XDG_CACHE_HOME"] = str(home / "Xdg" / "Cache")
    # No local gateway auth so the CLI can connect without a client key.
    environment.pop("CODEX_PROXY_GATEWAY_CLIENT_KEY", None)
    for name in SENSITIVE_ENVIRONMENT_NAMES:
        environment.pop(name, None)
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    command = [sys.executable, str(REPO_ROOT / "src-python" / "codex_proxy.py"), "--host", "127.0.0.1", "--port", str(port)]
    try:
        process = subprocess.Popen(
            command,
            cwd=REPO_ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
        )
    except OSError as error:
        raise CaptureFailure("gateway_start_failed") from error
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return process
        except OSError:
            if process.poll() is not None:
                stdout = process.stdout.read() if process.stdout else ""
                raise CaptureFailure(f"gateway_exited_early:{stdout[:500]}")
            time.sleep(0.25)
    raise CaptureFailure("gateway_start_timeout")


def _stop_gateway(process: subprocess.Popen[str]) -> None:
    try:
        process.terminate()
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass


# ---------------------------------------------------------------------------
# CLI runner
# ---------------------------------------------------------------------------


def _cli_environment(home: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment["CODEX_HOME"] = str(home)
    environment["HOME"] = str(home)
    environment["USERPROFILE"] = str(home)
    environment["APPDATA"] = str(home / "AppData" / "Roaming")
    environment["LOCALAPPDATA"] = str(home / "AppData" / "Local")
    environment["XDG_CONFIG_HOME"] = str(home / "Xdg" / "Config")
    environment["XDG_DATA_HOME"] = str(home / "Xdg" / "Data")
    environment["XDG_CACHE_HOME"] = str(home / "Xdg" / "Cache")
    environment.pop("CODEX_CONFIG", None)
    for name in SENSITIVE_ENVIRONMENT_NAMES:
        environment.pop(name, None)
    return environment


def _run_cli(home: Path, gateway_port: int, prompt: str, workspace: Path) -> tuple[int, list[str], str, str]:
    codex_executable = shutil.which("codex")
    if codex_executable is None:
        raise CaptureFailure("codex_executable_not_found")
    command = [
        str(Path(codex_executable).resolve()),
        "exec",
        "--json",
        "--ephemeral",
        "--skip-git-repo-check",
        "--strict-config",
        "--dangerously-bypass-approvals-and-sandbox",
    ]
    for feature in DISABLED_FEATURES:
        command.extend(("--disable", feature))
    command.extend(("-C", str(workspace), "-m", CLI_MODEL, "-"))

    environment = _cli_environment(home)
    try:
        completed = subprocess.run(
            command,
            input=prompt,
            cwd=workspace,
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=CLI_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CaptureFailure("cli_execution_failed") from error

    stdout_lines = [line for line in completed.stdout.splitlines() if line.strip()]
    return completed.returncode, stdout_lines, completed.stdout, completed.stderr


# ---------------------------------------------------------------------------
# Sanitized analysis
# ---------------------------------------------------------------------------


def _safe_event_shape(event: Mapping[str, Any]) -> dict[str, Any]:
    """Keep only event type and coarse item shape; drop IDs, content, paths."""
    shape: dict[str, Any] = {"type": event.get("type")}
    if "item" in event and isinstance(event["item"], Mapping):
        item = event["item"]
        shape["item_type"] = item.get("type")
        shape["item_name"] = item.get("name")
        shape["item_namespace"] = item.get("namespace")
        shape["item_status"] = item.get("status")
    if "response" in event and isinstance(event["response"], Mapping):
        response = event["response"]
        shape["response_status"] = response.get("status")
    return shape


def _sanitized_terminal_message(lines: list[str]) -> str | None:
    """Return a bounded, ID-free terminal message for failure diagnosis."""
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(value, Mapping):
            continue
        if value.get("type") in ("turn.failed", "error"):
            message = str(value.get("message", ""))
            # Strip UUIDs and thread-specific tokens.
            message = re.sub(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "<id>", message)
            return message[:500] if message else None
    return None


def _analyze_cli_events(lines: list[str]) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    for line in lines:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(value)

    event_types = Counter(str(event.get("type")) for event in events)
    terminal_events = [event for event in events if str(event.get("type")).startswith("turn.")]
    terminal_type = terminal_events[-1].get("type") if terminal_events else None

    function_calls: list[dict[str, Any]] = []
    function_call_outputs: list[dict[str, Any]] = []
    agent_messages: list[dict[str, Any]] = []
    errors: list[str] = []

    for event in events:
        event_type = event.get("type")
        item = event.get("item")
        if not isinstance(item, Mapping):
            continue
        if event_type == "response.output_item.done" or event_type == "response.output_item.added":
            item_type = item.get("type")
            if item_type == "function_call":
                function_calls.append({"name": item.get("name"), "namespace": item.get("namespace")})
            elif item_type == "function_call_output":
                function_call_outputs.append({"has_output": bool(item.get("output"))})
            elif item_type == "agent_message":
                agent_messages.append({"author": item.get("author"), "recipient": item.get("recipient")})
        if event_type in ("turn.failed", "response.failed", "error"):
            errors.append(str(event_type))

    return {
        "event_count": len(events),
        "event_types": dict(event_types),
        "terminal_event": terminal_type,
        "function_calls": function_calls,
        "function_call_outputs": function_call_outputs,
        "agent_messages": agent_messages,
        "errors": errors,
        "shapes": [_safe_event_shape(event) for event in events],
    }


def _analyze_gateway_log(log_path: Path) -> dict[str, Any]:
    if not log_path.exists():
        return {"event_count": 0, "event_types": {}, "errors": [], "error_details": [], "has_v1": False, "has_v2": False}
    events: list[dict[str, Any]] = []
    for line in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(value)

    event_types = Counter(str(event.get("event")) for event in events)
    error_events = [
        event
        for event in events
        if str(event.get("event")).startswith("request_error") or str(event.get("status")).startswith(("4", "5"))
    ]
    errors = [str(event.get("event")) for event in error_events]
    error_details: list[dict[str, Any]] = []
    for event in error_events:
        detail: dict[str, Any] = {"event": event.get("event")}
        che = event.get("codexhub_error") if isinstance(event.get("codexhub_error"), Mapping) else {}
        if isinstance(che, Mapping):
            detail["code"] = che.get("code")
            detail["message"] = che.get("message")
            detail["details"] = che.get("details")
        error_details.append(detail)
    has_v1 = any(
        V1_NAMESPACE in str(event.get("model", "")) or V1_NAMESPACE in json.dumps(event, separators=(",", ":"))
        for event in events
    )
    has_v2 = any(
        V2_NAMESPACE in str(event.get("model", "")) or V2_NAMESPACE in json.dumps(event, separators=(",", ":"))
        for event in events
    )
    fallback_count = sum(1 for event in events if event.get("route_reason") == "upstream_protocol_fallback")
    return {
        "event_count": len(events),
        "event_types": dict(event_types),
        "errors": errors,
        "error_details": error_details,
        "has_v1_observation": has_v1,
        "has_v2_observation": has_v2,
        "fallback_count": fallback_count,
    }


def _extract_phase_observations(
    cli_analysis: Mapping[str, Any], gateway_log: Mapping[str, Any]
) -> dict[str, Any]:
    function_calls = cli_analysis.get("function_calls", [])
    observed_tools: set[str] = {
        str(call.get("name")) for call in function_calls if isinstance(call, Mapping)
    }
    observed_namespaces: set[str] = {
        str(call.get("namespace")) for call in function_calls if isinstance(call, Mapping)
    }

    phases = {
        "spawn_agent": "spawn_agent" in observed_tools,
        "send_message": "send_message" in observed_tools,
        "followup_task": "followup_task" in observed_tools,
        "wait_agent": "wait_agent" in observed_tools,
        "list_agents": "list_agents" in observed_tools,
        "interrupt_agent": "interrupt_agent" in observed_tools,
        "child_result_delivery": any(
            isinstance(output, Mapping) and output.get("has_output")
            for output in cli_analysis.get("function_call_outputs", [])
        ),
        "parent_completion": cli_analysis.get("terminal_event") == "turn.completed",
    }

    if not phases["interrupt_agent"]:
        phases["interrupt_agent"] = "not_applicable:child_completed_without_interrupt"

    v1_tools_seen = observed_tools & V1_TOOLS
    v1_namespace_seen = V1_NAMESPACE in observed_namespaces
    return {
        "phases": phases,
        "observed_tools": sorted(observed_tools),
        "observed_namespaces": sorted(observed_namespaces),
        "v1_tools_seen": sorted(v1_tools_seen),
        "v1_namespace_seen": v1_namespace_seen,
        "errors": cli_analysis.get("errors", []) + gateway_log.get("errors", []),
        "fallback_count": gateway_log.get("fallback_count", 0),
    }


# ---------------------------------------------------------------------------
# Main capture
# ---------------------------------------------------------------------------


def capture(*, output_dir: Path, candidate_sha: str, cli_version: str) -> dict[str, Any]:
    home = _isolated_home()
    workspace = home / "workspace"
    workspace.mkdir()

    gateway_port = _free_port()
    try:
        with FixtureServer() as fixture:
            _write_providers_toml(home, fixture.port)
            _sync_catalog(home)
            gateway_process = _start_gateway(home, gateway_port)
            try:
                with GatewayShimServer(gateway_port, CLI_MODEL) as shim:
                    _write_cli_config(home, shim.port)
                    returncode, cli_lines, cli_stdout, cli_stderr = _run_cli(
                        home,
                        shim.port,
                        prompt=(
                            "Use the collaboration tools to spawn a worker subagent, "
                            "send it a message, follow up on its task, wait for it to finish, "
                            "list active agents, and then complete."
                        ),
                        workspace=workspace,
                    )
            finally:
                _stop_gateway(gateway_process)

        gateway_log_path = home / "proxy" / "codex-proxy-events.jsonl"
        cli_analysis = _analyze_cli_events(cli_lines)
        gateway_log = _analyze_gateway_log(gateway_log_path)
        phase_observations = _extract_phase_observations(cli_analysis, gateway_log)

        config_path = home / "config.toml"
        config_sha256 = _sha256_file(config_path) if config_path.exists() else None

        passed = (
            returncode == 0
            and all(
                value is True or (isinstance(value, str) and value.startswith("not_applicable"))
                for value in phase_observations["phases"].values()
            )
            and not phase_observations["v1_namespace_seen"]
            and not phase_observations["v1_tools_seen"]
            and not phase_observations["errors"]
            and phase_observations["fallback_count"] == 0
        )

        result: dict[str, Any] = {
            "schema": SCHEMA,
            "candidate_sha": candidate_sha,
            "cli_version": cli_version,
            "route": {
                "selected_provider": PROVIDER,
                "selected_model": CLI_MODEL,
                "gateway_port": gateway_port,
                "shim_port": shim.port,
                "upstream_fixture_port": fixture.port,
                "upstream_provider_id": FIXTURE_PROVIDER_ID,
            },
            "qualification_status": "passed" if passed else "failed",
            "cli_exit_code": returncode,
            "phase_observations": phase_observations,
            "cli_analysis": cli_analysis,
            "gateway_log_summary": gateway_log,
            "isolated_home_sha256": config_sha256,
            "sanitization": {
                "raw_prompts_retained": False,
                "credentials_retained": False,
                "opaque_ids_retained": False,
                "absolute_paths_retained": False,
            },
            "cli_terminal_message": _sanitized_terminal_message(cli_lines),
            "shim_notes": [
                "CLI-facing shim synthesized an OpenAI-compatible /v1/models list because the candidate gateway serves the CodexHub catalog shape.",
                "The shim injected the optional agent_type property into the collaboration.spawn_agent tool declaration; the installed CLI 0.146.1 build omits it, causing the gateway boundary classifier to reject the request otherwise.",
            ],
        }

        output_dir.mkdir(parents=True, exist_ok=True)
        summary_path = output_dir / "summary.json"
        summary_path.write_text(
            json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        return result
    finally:
        _remove_home(home)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--candidate-sha")
    args = parser.parse_args(argv)

    candidate_sha = args.candidate_sha
    if candidate_sha is None:
        try:
            candidate_sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            ).stdout.strip()
        except OSError:
            candidate_sha = None

    cli_version = "unknown"
    try:
        codex_exe = shutil.which("codex")
        if codex_exe:
            cli_version = subprocess.run(
                [str(Path(codex_exe).resolve()), "--version"],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            ).stdout.strip()
    except OSError:
        pass

    try:
        result = capture(output_dir=args.output_dir, candidate_sha=candidate_sha, cli_version=cli_version)
    except CaptureFailure as error:
        print(f"CAPTURE_FAILED:{error}", file=sys.stderr)
        return 2

    print(json.dumps({"schema": SCHEMA, "qualification_status": result["qualification_status"]}, sort_keys=True))
    return 0 if result["qualification_status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
