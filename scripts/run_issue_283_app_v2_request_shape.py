#!/usr/bin/env python3
"""Issue #283 Track B: real Codex Desktop App Collaboration V2 request-shape smoke.

The runner launches the App-managed ``codex app-server`` against a candidate
CodexHub gateway that is fronted by a CLI-facing shim.  The upstream is a
loopback Responses fixture.  We capture the actual ``/v1/responses`` request
bodies emitted by the App, assert the V2 collaboration boundary, and verify
that restarting ``app-server`` against the same ``CODEX_HOME`` does not
rewrite or delete task files.

Every run uses its own isolated ``CODEX_HOME`` and never touches the user's
Desktop configuration, auth files, or internal Codex databases.
"""

from __future__ import annotations

try:
    from scripts.python_runtime_contract import require_python_313
except ModuleNotFoundError:
    from python_runtime_contract import require_python_313

require_python_313(__file__)

import argparse
from collections import deque
import copy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import json
import os
from pathlib import Path
import queue
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Mapping
import uuid


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(REPO_ROOT / "src-python"))

# Reuse the upstream fixture, gateway helpers, and shim injector from Track A.
import run_issue_283_cli_v2_lifecycle as track_a


SCHEMA = "codexhub.issue283.app-v2-request-shape.v1"
DEFAULT_OUTPUT_DIR = Path("docs/evidence/issue-283/app-shape")
HOME_PREFIX = "codexhub-issue283-app-shape-"
MODEL = track_a.MODEL
APP_MODEL = track_a.CLI_MODEL
PROVIDER = track_a.PROVIDER
FIXTURE_PROVIDER_ID = track_a.FIXTURE_PROVIDER_ID
V2_NAMESPACE = track_a.V2_NAMESPACE
V2_TOOLS = track_a.V2_TOOLS
V1_NAMESPACE = track_a.V1_NAMESPACE
V1_TOOLS = track_a.V1_TOOLS
SENSITIVE_ENVIRONMENT_NAMES = track_a.SENSITIVE_ENVIRONMENT_NAMES
DISABLED_FEATURES = track_a.DISABLED_FEATURES
REQUEST_TIMEOUT_SECONDS = track_a.REQUEST_TIMEOUT_SECONDS
APP_SERVER_TIMEOUT_SECONDS = 60
TURN_WAIT_SECONDS = 45

# Files/directories that are expected to change across an app-server restart and
# should therefore be excluded from the "task files not rewritten/deleted" check.
VOLATILE_PATH_PATTERNS = re.compile(
    r"(^|/)("
    r"codex-proxy-events\.jsonl"
    r"|.*\.log"
    r"|.*events\.jsonl"
    r"|app-server-stderr\.log"
    r"|.*\.sqlite"
    r"|.*\.sqlite-shm"
    r"|.*\.sqlite-wal"
    r")$",
    re.IGNORECASE,
)


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


# ---------------------------------------------------------------------------
# App-facing shim: proxies /v1/responses to the gateway and records the raw
# request bodies emitted by the App before any gateway translation happens.
# ---------------------------------------------------------------------------


class _RecordingShimServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, gateway_port: int, app_model: str):
        super().__init__(("127.0.0.1", 0), _RecordingShimHandler)
        self.gateway_port = gateway_port
        self.app_model = app_model
        self.requests: list[dict[str, Any]] = []
        self.lock = threading.Lock()


class _RecordingShimHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args: Any) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802 - stdlib callback name
        if self.path == "/v1/models":
            server = self.server
            if not isinstance(server, _RecordingShimServer):
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
                        "id": server.app_model,
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
        if not isinstance(server, _RecordingShimServer):
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

        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            with server.lock:
                server.requests.append(parsed)

        # Inject the optional agent_type property that the current App build
        # omits from the collaboration.spawn_agent declaration.  This is the
        # same shim-side accommodation used in Track A.
        proxied_body = track_a._inject_collaboration_agent_type(body)

        upstream = http.client.HTTPConnection(
            "127.0.0.1", server.gateway_port, timeout=REQUEST_TIMEOUT_SECONDS
        )
        try:
            headers = {
                name: value
                for name, value in self.headers.items()
                if name.lower() not in {"host", "content-length", "connection"}
            }
            upstream.request("POST", "/v1/responses", body=proxied_body, headers=headers)
            upstream_response = upstream.getresponse()
            try:
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
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                # The App closed the connection while the shim was streaming the
                # upstream response.  This is benign for request-shape capture.
                return
        except Exception:
            try:
                self.send_error(502)
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                pass
        finally:
            upstream.close()


class RecordingGatewayShimServer:
    def __init__(self, gateway_port: int, app_model: str):
        self._server = _RecordingShimServer(gateway_port, app_model)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    @property
    def requests(self) -> list[dict[str, Any]]:
        with self._server.lock:
            return copy.deepcopy(self._server.requests)

    def __enter__(self) -> "RecordingGatewayShimServer":
        self._thread.start()
        return self

    def __exit__(self, *_args: Any) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


# ---------------------------------------------------------------------------
# JSON-RPC client for app-server stdio
# ---------------------------------------------------------------------------


class AppServerFailure(RuntimeError):
    def __init__(self, operation: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(operation)
        self.operation = operation
        self.details = details


class JsonRpcClient:
    def __init__(self, process: subprocess.Popen[str]) -> None:
        self._process = process
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._reader.start()
        self._next_id = 1
        self._notifications: deque[dict[str, Any]] = deque()
        self._responses: dict[int, dict[str, Any]] = {}

    def _read_stdout(self) -> None:
        assert self._process.stdout is not None
        for line in self._process.stdout:
            self._lines.put(line)
        self._lines.put(None)

    def _write(self, payload: dict[str, Any]) -> None:
        if self._process.stdin is None:
            raise AppServerFailure("app_server_stdin_closed")
        self._process.stdin.write(json.dumps(payload, separators=(",", ":")) + "\n")
        self._process.stdin.flush()

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self._write(payload)

    def request(
        self, method: str, params: dict[str, Any], timeout: float
    ) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        self._write(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }
        )
        return self._wait_for_response(request_id, timeout, method)

    def _wait_for_response(
        self, request_id: int, timeout: float, operation: str
    ) -> dict[str, Any]:
        cached = self._responses.pop(request_id, None)
        if cached is not None:
            return cached
        deadline = time.monotonic() + timeout
        while True:
            message = self._next_message(deadline, operation)
            if message.get("id") == request_id and not isinstance(
                message.get("method"), str
            ):
                return message
            self._store_unmatched(message)

    def wait_for_notification(
        self,
        predicate: Callable[[dict[str, Any]], bool],
        timeout: float,
        operation: str,
    ) -> dict[str, Any] | None:
        for notification in tuple(self._notifications):
            if predicate(notification):
                self._notifications.remove(notification)
                return notification
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                line = self._lines.get(timeout=remaining)
            except queue.Empty:
                return None
            if line is None:
                return None
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(message, dict):
                continue
            if isinstance(message.get("method"), str) and predicate(message):
                return message
            self._store_unmatched(message)

    def _next_message(self, deadline: float, operation: str) -> dict[str, Any]:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(operation)
        try:
            line = self._lines.get(timeout=remaining)
        except queue.Empty as error:
            raise TimeoutError(operation) from error
        if line is None:
            raise AppServerFailure("app_server_exited")
        try:
            message = json.loads(line)
        except json.JSONDecodeError as error:
            raise AppServerFailure("app_server_invalid_json") from error
        if not isinstance(message, dict):
            raise AppServerFailure("app_server_invalid_message")
        return message

    def _store_unmatched(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        message_id = message.get("id")
        if isinstance(method, str) and isinstance(message_id, int):
            self._write(
                {
                    "jsonrpc": "2.0",
                    "id": message_id,
                    "error": {
                        "code": -32601,
                        "message": "issue-283 app runner does not service callbacks",
                    },
                }
            )
        elif isinstance(method, str):
            self._notifications.append(message)
        elif isinstance(message_id, int):
            self._responses[message_id] = message

    def close(self) -> None:
        try:
            if self._process.stdin is not None and not self._process.stdin.closed:
                self._process.stdin.close()
        except OSError as error:
            raise AppServerFailure("app_server_client_close_failed") from error
        finally:
            self._reader.join(timeout=1)


def response_result(response: dict[str, Any], operation: str) -> dict[str, Any]:
    if "error" in response:
        error = response["error"]
        details: dict[str, Any] = {"errorKind": "json_rpc"}
        if isinstance(error, dict):
            code = error.get("code")
            if isinstance(code, int) and not isinstance(code, bool):
                details["errorCode"] = code
            else:
                details["errorKind"] = "json_rpc_without_numeric_code"
        else:
            details["errorKind"] = "invalid_json_rpc_error"
        raise AppServerFailure(operation, details)
    result = response.get("result")
    if not isinstance(result, dict):
        raise AppServerFailure(f"{operation}_invalid_result")
    return result


# ---------------------------------------------------------------------------
# App-server lifecycle helpers
# ---------------------------------------------------------------------------


def resolve_app_codex(explicit: str | None) -> Path:
    if explicit:
        candidate = Path(explicit).expanduser().resolve()
        if candidate.is_file():
            return candidate
        raise AppServerFailure("app_cli_not_found")

    sandbox = Path.home() / ".codex" / ".sandbox-bin" / "codex.exe"
    if sandbox.is_file():
        return sandbox

    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        root = Path(local_app_data) / "OpenAI" / "Codex" / "bin"
        candidates = sorted(
            (path for path in root.glob("*/codex.exe") if path.is_file()),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
        if candidates:
            return candidates[0]

    fallback = shutil.which("codex.exe") or shutil.which("codex.cmd") or shutil.which("codex")
    if fallback:
        return Path(fallback).resolve()
    raise AppServerFailure("app_cli_not_found")


def app_server_environment(home: Path) -> dict[str, str]:
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
    environment.pop("CODEX_PROXY_GATEWAY_CLIENT_KEY", None)
    for name in SENSITIVE_ENVIRONMENT_NAMES:
        environment.pop(name, None)
    return environment


def start_app_server(
    codex_command: Path, home: Path, environment: dict[str, str]
) -> subprocess.Popen[str]:
    command = [str(codex_command), "app-server"]
    for service in DISABLED_FEATURES:
        command.extend(("--disable", service))
    command.append("--stdio")
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    stderr_path = home / "app-server-stderr.log"
    try:
        return subprocess.Popen(
            command,
            cwd=home,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=open(stderr_path, "w", encoding="utf-8", errors="replace"),
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
        )
    except OSError as error:
        raise AppServerFailure("app_server_start_failed") from error


def stop_app_server(process: subprocess.Popen[str]) -> None:
    try:
        if process.stdin is not None and not process.stdin.closed:
            try:
                process.stdin.close()
            except OSError:
                pass
        try:
            process.wait(timeout=5)
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            process.terminate()
            process.wait(timeout=5)
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            process.kill()
            process.wait(timeout=5)
            return
        except (OSError, subprocess.TimeoutExpired):
            pass
        if process.poll() is None:
            raise AppServerFailure("app_server_cleanup_failed")
    finally:
        try:
            if process.stdout is not None:
                process.stdout.close()
        except OSError:
            pass
        try:
            if process.stderr is not None:
                process.stderr.close()
        except OSError:
            pass


def initialize(client: JsonRpcClient, timeout: float) -> None:
    response_result(
        client.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "codexhub-issue283-app-shape",
                    "version": "1",
                },
                "capabilities": {
                    "experimentalApi": True,
                    "requestAttestation": False,
                    "optOutNotificationMethods": [],
                },
            },
            timeout,
        ),
        "initialize",
    )
    client.notify("initialized")


def read_model_list(client: JsonRpcClient, timeout: float) -> list[dict[str, Any]]:
    result = response_result(
        client.request("model/list", {"limit": 100}, timeout), "model_list"
    )
    models = result.get("data")
    if not isinstance(models, list):
        raise AppServerFailure("model_list_invalid_data")
    return [model for model in models if isinstance(model, dict)]


def start_custom_thread(
    client: JsonRpcClient, workspace: Path, model: str, timeout: float
) -> str:
    result = response_result(
        client.request(
            "thread/start",
            {
                "cwd": str(workspace),
                "model": model,
                "modelProvider": "custom",
                "approvalPolicy": "never",
                "sandbox": "danger-full-access",
                "ephemeral": False,
            },
            timeout,
        ),
        "thread_start",
    )
    thread = result.get("thread")
    if not isinstance(thread, dict):
        raise AppServerFailure("thread_start_missing_thread")
    thread_id = thread.get("id")
    if not isinstance(thread_id, str) or not thread_id:
        raise AppServerFailure("thread_start_missing_thread_id")
    return thread_id


def start_turn(
    client: JsonRpcClient, thread_id: str, text: str, timeout: float
) -> str:
    result = response_result(
        client.request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": text}],
            },
            timeout,
        ),
        "turn_start",
    )
    turn = result.get("turn")
    turn_id = turn.get("id") if isinstance(turn, dict) else None
    if not isinstance(turn_id, str) or not turn_id:
        raise AppServerFailure("turn_start_missing_turn_id")
    return turn_id


# ---------------------------------------------------------------------------
# Home setup (providers + config overlay + feature flag)
# ---------------------------------------------------------------------------


def _isolated_home() -> Path:
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


def _write_config(home: Path, shim_port: int) -> None:
    import config_overlay

    config_path = home / "config.toml"
    backup_path = home / "proxy" / "config.toml.backup"
    catalog_path = home / "model-catalogs" / "codexhub-model-catalog.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("\n", encoding="utf-8", newline="\n")
    config_overlay.apply_overlay(
        config_path=config_path,
        backup_path=backup_path,
        catalog_path=catalog_path if catalog_path.exists() else None,
        base_url=f"http://127.0.0.1:{shim_port}",
        owner="release",
        takeover=False,
        gateway_key="fixture-key",
    )
    with config_path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(
            "\n[features.multi_agent_v2]\nenabled = true\nnon_code_mode_only = false\n"
        )


# ---------------------------------------------------------------------------
# File-system snapshot for same-Home restart check
# ---------------------------------------------------------------------------


def _home_file_snapshot(home: Path) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    for path in sorted(home.rglob("*")):
        if not path.is_file():
            continue
        try:
            relative = path.relative_to(home).as_posix()
        except ValueError:
            continue
        if VOLATILE_PATH_PATTERNS.search(relative):
            continue
        snapshot[relative] = _sha256_file(path)
    return snapshot


def _sanitize_relative_path(path: str) -> str:
    """Remove timestamps and UUIDs from path strings before writing evidence."""
    path = re.sub(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        "<id>",
        path,
    )
    path = re.sub(r"\d{4}T\d{2}-\d{2}-\d{2}-", "<ts>", path)
    path = re.sub(r"\d{4}/\d{2}/\d{2}", "<date>", path)
    return path


def _compare_snapshots(
    before: dict[str, str], after: dict[str, str]
) -> dict[str, Any]:
    before_paths = set(before)
    after_paths = set(after)
    added = sorted({_sanitize_relative_path(p) for p in after_paths - before_paths})
    removed = sorted({_sanitize_relative_path(p) for p in before_paths - after_paths})
    changed = sorted(
        {
            _sanitize_relative_path(p)
            for p in before_paths & after_paths
            if before[p] != after[p]
        }
    )
    unchanged_count = sum(
        1 for p in before_paths & after_paths if before[p] == after[p]
    )
    return {
        "added": added,
        "removed": removed,
        "changed": changed,
        "unchanged_count": unchanged_count,
        "before_count": len(before),
        "after_count": len(after),
    }


# ---------------------------------------------------------------------------
# Request-shape analysis
# ---------------------------------------------------------------------------


def _extract_tools(request: Mapping[str, Any]) -> list[dict[str, Any]]:
    tools = request.get("tools")
    if not isinstance(tools, list):
        return []
    return [tool for tool in tools if isinstance(tool, Mapping)]


def _tool_id(tool: Mapping[str, Any]) -> str:
    namespace = tool.get("name", "")
    name = ""
    if isinstance(tool.get("tools"), list):
        # Namespace wrapper; collect child names for summary only.
        children = [
            str(child.get("name"))
            for child in tool["tools"]
            if isinstance(child, Mapping)
        ]
        return f"{namespace}::({','.join(sorted(children))})"
    if tool.get("type") == "function":
        name = str(tool.get("function", {}).get("name") if isinstance(tool.get("function"), Mapping) else tool.get("name"))
    return f"{namespace}.{name}" if namespace and name else str(namespace or name)


def _analyze_request_shape(request: Mapping[str, Any]) -> dict[str, Any]:
    tools = _extract_tools(request)
    namespaces: set[str] = set()
    top_level_types: set[str] = set()
    v2_tools_observed: set[str] = set()
    v1_tools_observed: set[str] = set()
    all_function_names: set[str] = set()
    has_fork_context = False

    for tool in tools:
        tool_type = str(tool.get("type", ""))
        top_level_types.add(tool_type)
        name = str(tool.get("name", ""))
        if tool_type == "namespace":
            namespaces.add(name)
            children = tool.get("tools") or []
            if isinstance(children, list):
                for child in children:
                    if not isinstance(child, Mapping):
                        continue
                    child_name = str(child.get("name", ""))
                    if name == V2_NAMESPACE and child_name in V2_TOOLS:
                        v2_tools_observed.add(child_name)
                    if name == V1_NAMESPACE and child_name in V1_TOOLS:
                        v1_tools_observed.add(child_name)
                    all_function_names.add(f"{name}.{child_name}")
        elif tool_type == "function":
            fname = str(
                tool.get("function", {}).get("name")
                if isinstance(tool.get("function"), Mapping)
                else tool.get("name")
            )
            all_function_names.add(fname)
            if fname in V2_TOOLS:
                v2_tools_observed.add(fname)
            if fname in V1_TOOLS:
                v1_tools_observed.add(fname)
            if fname == "fork_context":
                has_fork_context = True
        if "fork_context" in json.dumps(tool, separators=(",", ":")):
            has_fork_context = True

    instructions = request.get("instructions")
    instructions_text = instructions if isinstance(instructions, str) else ""
    if "fork_context" in instructions_text:
        has_fork_context = True
    if "multi_agent_v1" in instructions_text or V1_NAMESPACE in instructions_text:
        # Treat any V1 scheduler mention in the system prompt as a V1 signal.
        v1_tools_observed.update({"scheduler_v1"})

    model = request.get("model")
    return {
        "model": model,
        "tool_count": len(tools),
        "top_level_tool_types": sorted(top_level_types),
        "namespaces": sorted(namespaces),
        "v2_tools_observed": sorted(v2_tools_observed),
        "v1_tools_observed": sorted(v1_tools_observed),
        "all_function_names": sorted(all_function_names),
        "has_collaboration_namespace": V2_NAMESPACE in namespaces,
        "has_multi_agent_v1_namespace": V1_NAMESPACE in namespaces,
        "has_fork_context": has_fork_context,
        "model_unchanged": model == APP_MODEL,
    }


def _sanitized_app_stderr(home: Path) -> str | None:
    path = home / "app-server-stderr.log"
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8", errors="replace")
    # Strip absolute paths and UUIDs.
    text = re.sub(r"[A-Za-z]:[\\/][^\s\"']+", "<path>", text)
    text = re.sub(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", "<id>", text
    )
    return text[:1000] if text else None


def _sanitized_request_summary(requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return a compact, ID-free summary of captured request shapes."""
    summary: list[dict[str, Any]] = []
    for request in requests:
        shape = _analyze_request_shape(request)
        tool_summary: list[dict[str, Any]] = []
        for tool in _extract_tools(request):
            tool_entry: dict[str, Any] = {
                "type": tool.get("type"),
                "name": tool.get("name"),
            }
            if tool.get("type") == "namespace" and isinstance(tool.get("tools"), list):
                tool_entry["child_names"] = sorted(
                    {
                        str(child.get("name"))
                        for child in tool["tools"]
                        if isinstance(child, Mapping)
                    }
                )
            tool_summary.append(tool_entry)
        summary.append(
            {
                "model": shape["model"],
                "tool_count": shape["tool_count"],
                "namespaces": shape["namespaces"],
                "v2_tools_observed": shape["v2_tools_observed"],
                "v1_tools_observed": shape["v1_tools_observed"],
                "has_fork_context": shape["has_fork_context"],
                "tool_summary": tool_summary,
            }
        )
    return summary


# ---------------------------------------------------------------------------
# Main capture
# ---------------------------------------------------------------------------


def capture(
    *,
    output_dir: Path,
    candidate_sha: str,
    app_version: str,
    codex_command: Path,
) -> dict[str, Any]:
    home = _isolated_home()
    workspace = home / "workspace"
    workspace.mkdir()

    gateway_port = track_a._free_port()
    fixture: track_a.FixtureServer | None = None
    shim: RecordingGatewayShimServer | None = None
    gateway_process: subprocess.Popen[str] | None = None
    first_client: JsonRpcClient | None = None
    first_app: subprocess.Popen[str] | None = None
    second_client: JsonRpcClient | None = None
    second_app: subprocess.Popen[str] | None = None

    try:
        fixture = track_a.FixtureServer()
        fixture.__enter__()
        track_a._write_providers_toml(home, fixture.port)
        track_a._sync_catalog(home)
        gateway_process = track_a._start_gateway(home, gateway_port)
        shim = RecordingGatewayShimServer(gateway_port, APP_MODEL)
        shim.__enter__()
        _write_config(home, shim.port)

        environment = app_server_environment(home)

        # ---- First app-server session: create a thread and start a turn ----
        first_app = start_app_server(codex_command, home, environment)
        first_client = JsonRpcClient(first_app)
        initialize(first_client, APP_SERVER_TIMEOUT_SECONDS)
        models = read_model_list(first_client, APP_SERVER_TIMEOUT_SECONDS)
        model_ids = [str(m.get("model") or m.get("id") or "").strip() for m in models]
        selected_model = next(
            (mid for mid in model_ids if MODEL in mid),
            APP_MODEL,
        )

        thread_id = start_custom_thread(
            first_client, workspace, selected_model, APP_SERVER_TIMEOUT_SECONDS
        )
        turn_id = start_turn(
            first_client,
            thread_id,
            (
                "Use the collaboration tools to spawn a worker subagent, "
                "send it a message, follow up on its task, wait for it to finish, "
                "list active agents, and then complete."
            ),
            APP_SERVER_TIMEOUT_SECONDS,
        )

        # Wait for the App to emit at least one /v1/responses request.  The
        # fixture drives the lifecycle; we do not require the turn to finish.
        deadline = time.monotonic() + TURN_WAIT_SECONDS
        while time.monotonic() < deadline and not shim.requests:
            time.sleep(0.25)

        # Also collect any terminal notification for diagnostic purposes.
        terminal_notification = first_client.wait_for_notification(
            lambda item: (
                isinstance(item.get("params"), Mapping)
                and item["params"].get("threadId") == thread_id
                and isinstance(item["params"].get("turn"), Mapping)
                and item["params"]["turn"].get("id") == turn_id
                and str(item.get("method")) in ("turn/completed", "turn/failed")
            ),
            TURN_WAIT_SECONDS,
            "turn_terminal",
        )

        first_client.close()
        first_client = None
        stop_app_server(first_app)
        first_app = None

        # Snapshot the home before restarting app-server.
        config_path = home / "config.toml"
        config_sha256_before = _sha256_file(config_path) if config_path.exists() else None
        snapshot_before = _home_file_snapshot(home)

        # ---- Second app-server session: same CODEX_HOME, just initialize ----
        second_app = start_app_server(codex_command, home, environment)
        second_client = JsonRpcClient(second_app)
        initialize(second_client, APP_SERVER_TIMEOUT_SECONDS)
        # A model/list after restart confirms the App still reads the same catalog.
        read_model_list(second_client, APP_SERVER_TIMEOUT_SECONDS)
        second_client.close()
        second_client = None
        stop_app_server(second_app)
        second_app = None

        config_sha256_after = _sha256_file(config_path) if config_path.exists() else None
        snapshot_after = _home_file_snapshot(home)
        restart_comparison = _compare_snapshots(snapshot_before, snapshot_after)

        captured_requests = shim.requests
        request_shapes = [_analyze_request_shape(req) for req in captured_requests]
        sanitized_summary = _sanitized_request_summary(captured_requests)

        # Aggregate assertions across all captured requests.
        v2_boundary_ok = all(
            shape["has_collaboration_namespace"]
            and set(shape["v2_tools_observed"]) >= V2_TOOLS
            and not shape["has_multi_agent_v1_namespace"]
            and not shape["v1_tools_observed"]
            and not shape["has_fork_context"]
            and shape["model_unchanged"]
            for shape in request_shapes
        )
        # We also require at least one request to have been captured.
        v2_boundary_ok = v2_boundary_ok and bool(captured_requests)

        restart_ok = (
            config_sha256_before is not None
            and config_sha256_after is not None
            and config_sha256_before == config_sha256_after
            and not restart_comparison["removed"]
            and not restart_comparison["changed"]
        )

        gateway_log_path = home / "proxy" / "codex-proxy-events.jsonl"
        gateway_log = track_a._analyze_gateway_log(gateway_log_path)

        result: dict[str, Any] = {
            "schema": SCHEMA,
            "candidate_sha": candidate_sha,
            "app_version": app_version,
            "route": {
                "selected_provider": PROVIDER,
                "selected_model": selected_model,
                "gateway_port": gateway_port,
                "shim_port": shim.port,
                "upstream_fixture_port": fixture.port,
                "upstream_provider_id": FIXTURE_PROVIDER_ID,
            },
            "qualification_status": "passed" if (v2_boundary_ok and restart_ok) else "failed",
            "v2_boundary_check": {
                "passed": v2_boundary_ok,
                "captured_request_count": len(captured_requests),
                "request_shapes": sanitized_summary,
            },
            "same_home_restart_check": {
                "passed": restart_ok,
                "config_sha256_before": config_sha256_before,
                "config_sha256_after": config_sha256_after,
                "file_snapshot": restart_comparison,
            },
            "terminal_notification": {
                "received": terminal_notification is not None,
                "method": terminal_notification.get("method") if terminal_notification else None,
            },
            "gateway_log_summary": gateway_log,
            "app_stderr_excerpt": _sanitized_app_stderr(home),
            "sanitization": {
                "raw_prompts_retained": False,
                "credentials_retained": False,
                "opaque_ids_retained": False,
                "absolute_paths_retained": False,
            },
            "shim_notes": [
                "App-facing shim synthesized an OpenAI-compatible /v1/models list because the candidate gateway serves the CodexHub catalog shape.",
                "The shim recorded each App /v1/responses request body before forwarding it to the gateway and before injecting the optional agent_type property into collaboration.spawn_agent.",
                "The optional agent_type injection is the same test-only accommodation used in Track A; the App build still omits agent_type in the tool declaration it emits.",
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
        if first_client is not None:
            try:
                first_client.close()
            except Exception:
                pass
        if first_app is not None:
            try:
                stop_app_server(first_app)
            except Exception:
                pass
        if second_client is not None:
            try:
                second_client.close()
            except Exception:
                pass
        if second_app is not None:
            try:
                stop_app_server(second_app)
            except Exception:
                pass
        if shim is not None:
            try:
                shim.__exit__(None, None, None)
            except Exception:
                pass
        if gateway_process is not None:
            try:
                track_a._stop_gateway(gateway_process)
            except Exception:
                pass
        if fixture is not None:
            try:
                fixture.__exit__(None, None, None)
            except Exception:
                pass
        _remove_home(home)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--candidate-sha")
    parser.add_argument("--codex", help="Path to the App-managed Codex CLI")
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

    codex_command = resolve_app_codex(args.codex)
    app_version = "unknown"
    try:
        app_version = subprocess.run(
            [str(codex_command), "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        ).stdout.strip()
    except OSError:
        pass

    try:
        result = capture(
            output_dir=args.output_dir,
            candidate_sha=candidate_sha,
            app_version=app_version,
            codex_command=codex_command,
        )
    except CaptureFailure as error:
        print(f"CAPTURE_FAILED:{error}", file=sys.stderr)
        return 2
    except AppServerFailure as error:
        payload: dict[str, Any] = {"failure": error.operation, "outcome": "failed"}
        if error.details is not None:
            payload["details"] = error.details
        print(json.dumps(payload, sort_keys=True))
        return 1
    except TimeoutError as error:
        print(json.dumps({"failure": str(error), "outcome": "failed"}, sort_keys=True))
        return 1

    print(
        json.dumps(
            {"schema": SCHEMA, "qualification_status": result["qualification_status"]},
            sort_keys=True,
        )
    )
    return 0 if result["qualification_status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
