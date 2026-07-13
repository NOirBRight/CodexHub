"""Exercise the Issue #106 Task lifecycle through an isolated App CLI session.

The runner uses only the App-managed ``codex app-server`` API.  Every run has
its own temporary ``CODEX_HOME`` and its own config/catalog overlay; it never
reads or changes the user's Desktop configuration, auth files, or internal
Codex databases.  The connected scenarios require a running local CodexHub
Gateway, while the catalog comparison can run disconnected.
"""

from __future__ import annotations

import argparse
from collections import deque
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable


REPO_ROOT = Path(__file__).resolve().parents[1]
TEMP_HOME_PREFIX = "codexhub-issue106-task-lifecycle-"
DISABLED_APP_SERVER_SERVICES = ("plugins", "remote_plugin", "plugin_sharing")
SENSITIVE_ENVIRONMENT_NAMES = (
    "ANTHROPIC_API_KEY",
    "OLLAMA_API_KEY",
    "OPENAI_API_KEY",
)
BOOTSTRAP_INPUT = "Reply exactly READY. Do not use tools."
PREFLIGHT_INPUT = "Reply exactly PREFLIGHT_DONE. Do not use tools."
CONTINUATION_INPUT = "Reply exactly CONTINUED. Do not use tools."
SAFE_THREAD_NAME = "Issue 106 isolated lifecycle"


class AppServerFailure(RuntimeError):
    """A sanitized app-server failure; do not retain raw server error text."""

    def __init__(self, operation: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(operation)
        self.operation = operation
        self.details = details


class AppServerRequestRejected(AppServerFailure):
    """A completed JSON-RPC error response, distinct from transport failure."""


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
    ) -> dict[str, Any]:
        for notification in tuple(self._notifications):
            if predicate(notification):
                self._notifications.remove(notification)
                return notification

        deadline = time.monotonic() + timeout
        while True:
            message = self._next_message(deadline, operation)
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
                        "message": "issue-106 lifecycle runner does not service callbacks",
                    },
                }
            )
        elif isinstance(method, str):
            self._notifications.append(message)
        elif isinstance(message_id, int):
            self._responses[message_id] = message

    def close(self) -> None:
        if self._process.stdin is not None:
            self._process.stdin.close()
        self._reader.join(timeout=1)


def response_result(response: dict[str, Any], operation: str) -> dict[str, Any]:
    if "error" in response:
        raise AppServerRequestRejected(operation)
    result = response.get("result")
    if not isinstance(result, dict):
        raise AppServerFailure(f"{operation}_invalid_result")
    return result


def resolve_app_codex(explicit: str | None) -> Path:
    if explicit:
        candidate = Path(explicit).expanduser().resolve()
        if candidate.is_file():
            return candidate
        raise AppServerFailure("app_cli_not_found")

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


def is_task_owned_temporary_home(home: Path) -> bool:
    try:
        resolved_home = home.resolve()
        temporary_root = Path(tempfile.gettempdir()).resolve()
        return (
            resolved_home.parent == temporary_root
            and resolved_home.name.startswith(TEMP_HOME_PREFIX)
        )
    except OSError:
        return False


def create_temporary_home() -> Path:
    home = Path(tempfile.mkdtemp(prefix=TEMP_HOME_PREFIX))
    if not is_task_owned_temporary_home(home):
        raise AppServerFailure("temporary_home_outside_task_scope")
    return home


def remove_temporary_home(home: Path) -> None:
    if not is_task_owned_temporary_home(home):
        raise AppServerFailure("temporary_home_outside_task_scope")
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
    raise AppServerFailure("temporary_home_cleanup_failed") from last_error


def isolated_environment(home: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment["CODEX_HOME"] = str(home)
    environment.pop("CODEX_CONFIG", None)
    for name in SENSITIVE_ENVIRONMENT_NAMES:
        environment.pop(name, None)
    return environment


def run_checked(command: list[str], environment: dict[str, str]) -> None:
    try:
        completed = subprocess.run(
            command,
            cwd=REPO_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise AppServerFailure("isolated_configuration_setup_failed") from error
    if completed.returncode != 0:
        raise AppServerFailure("isolated_configuration_setup_failed")


def prepare_connected_home(
    home: Path,
    environment: dict[str, str],
    gateway_base_url: str,
    gateway_key: str | None,
) -> None:
    run_checked(
        [sys.executable, str(REPO_ROOT / "src-python" / "catalog_sync.py"), "--sync"],
        environment,
    )
    overlay_command = [
        sys.executable,
        str(REPO_ROOT / "src-python" / "config_overlay.py"),
        "apply",
        "--config",
        str(home / "config.toml"),
        "--backup",
        str(home / "proxy" / "config.toml.backup"),
        "--catalog",
        str(home / "model-catalogs" / "codexhub-model-catalog.json"),
        "--base-url",
        gateway_base_url,
        "--owner",
        "release",
    ]
    if gateway_key:
        overlay_command.extend(("--gateway-key", gateway_key))
    run_checked(overlay_command, environment)


def start_app_server(
    codex_command: Path, home: Path, environment: dict[str, str]
) -> subprocess.Popen[str]:
    command = [str(codex_command), "app-server"]
    for service in DISABLED_APP_SERVER_SERVICES:
        command.extend(("--disable", service))
    command.append("--stdio")
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        return subprocess.Popen(
            command,
            cwd=home,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
        )
    except OSError as error:
        raise AppServerFailure("app_server_start_failed") from error


def stop_app_server(process: subprocess.Popen[str]) -> None:
    if process.stdin is not None and not process.stdin.closed:
        process.stdin.close()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
    if process.stdout is not None:
        process.stdout.close()


def initialize(client: JsonRpcClient, timeout: float) -> None:
    response_result(
        client.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "codexhub-issue106-lifecycle",
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
    result = response_result(client.request("model/list", {"limit": 100}, timeout), "model_list")
    models = result.get("data")
    if not isinstance(models, list):
        raise AppServerFailure("model_list_invalid_data")
    return [model for model in models if isinstance(model, dict)]


def model_id(model: dict[str, Any]) -> str:
    return str(model.get("model") or model.get("id") or "").strip()


def model_efforts(model: dict[str, Any]) -> set[str]:
    raw_efforts = model.get("supportedReasoningEfforts") or model.get(
        "supported_reasoning_levels"
    )
    if not isinstance(raw_efforts, list):
        return set()
    return {
        str(item.get("reasoningEffort") or item.get("effort") or "").strip()
        for item in raw_efforts
        if isinstance(item, dict)
        and str(item.get("reasoningEffort") or item.get("effort") or "").strip()
    }


def catalog_summary(
    models: list[dict[str, Any]], requested_official_model: str
) -> dict[str, Any]:
    ids = {model_id(model) for model in models}
    requested = next(
        (model for model in models if model_id(model) == requested_official_model),
        None,
    )
    return {
        "modelCount": len(models),
        "requestedOfficialModelListed": requested_official_model in ids,
        "requestedOfficialModelSupportsMax": bool(
            requested is not None and "max" in model_efforts(requested)
        ),
    }


def read_account_summary(client: JsonRpcClient, timeout: float) -> dict[str, bool]:
    result = response_result(client.request("account/read", {}, timeout), "account_read")
    return {
        "authenticated": result.get("account") is not None,
        "requiresOpenaiAuth": result.get("requiresOpenaiAuth") is True,
    }


def get_thread(result: dict[str, Any], operation: str) -> dict[str, Any]:
    thread = result.get("thread")
    if not isinstance(thread, dict):
        raise AppServerFailure(f"{operation}_missing_thread")
    return thread


def get_thread_id(thread: dict[str, Any], operation: str) -> str:
    thread_id = thread.get("id")
    if not isinstance(thread_id, str) or not thread_id:
        raise AppServerFailure(f"{operation}_missing_thread_id")
    return thread_id


def get_turn_id(result: dict[str, Any], operation: str) -> str:
    turn = result.get("turn")
    turn_id = turn.get("id") if isinstance(turn, dict) else None
    if not isinstance(turn_id, str) or not turn_id:
        raise AppServerFailure(f"{operation}_missing_turn_id")
    return turn_id


def turn_started(
    client: JsonRpcClient,
    thread_id: str,
    text: str,
    timeout: float,
    *,
    model: str | None = None,
    effort: str | None = None,
    permission_preflight: bool = False,
) -> str:
    params: dict[str, Any] = {
        "threadId": thread_id,
        "input": [{"type": "text", "text": text}],
    }
    if model is not None:
        params["model"] = model
    if effort is not None:
        params["effort"] = effort
    if permission_preflight:
        params["approvalPolicy"] = "never"
        params["sandboxPolicy"] = {"type": "dangerFullAccess"}
    result = response_result(client.request("turn/start", params, timeout), "turn_start")
    return get_turn_id(result, "turn_start")


def wait_for_turn(
    client: JsonRpcClient, thread_id: str, turn_id: str, timeout: float
) -> dict[str, Any]:
    notification = client.wait_for_notification(
        lambda item: (
            item.get("method") == "turn/completed"
            and isinstance(item.get("params"), dict)
            and item["params"].get("threadId") == thread_id
            and isinstance(item["params"].get("turn"), dict)
            and item["params"]["turn"].get("id") == turn_id
        ),
        timeout,
        "turn_completed",
    )
    turn = notification["params"]["turn"]
    if turn.get("status") != "completed":
        raise AppServerFailure("turn_not_completed")
    return turn


def thread_snapshot(client: JsonRpcClient, thread_id: str, timeout: float) -> dict[str, Any]:
    result = response_result(
        client.request("thread/read", {"threadId": thread_id, "includeTurns": True}, timeout),
        "thread_read",
    )
    thread = get_thread(result, "thread_read")
    turns = thread.get("turns")
    if not isinstance(turns, list):
        raise AppServerFailure("thread_read_missing_turns")
    status = thread.get("status")
    return {
        "threadStatus": status.get("type") if isinstance(status, dict) else None,
        "turnCount": len(turns),
        "turnStatuses": [
            turn.get("status") if isinstance(turn, dict) else None for turn in turns
        ],
        "turnItemCounts": [
            len(turn.get("items", [])) if isinstance(turn, dict) else None
            for turn in turns
        ],
        "assistantOutputTurns": sum(
            1
            for turn in turns
            if isinstance(turn, dict)
            and any(
                isinstance(item, dict)
                and (
                    item.get("role") == "assistant"
                    or item.get("type") in {"agent_message", "agentMessage"}
                )
                for item in turn.get("items", [])
                if isinstance(turn.get("items", []), list)
            )
        ),
    }


def thread_list_contains(
    client: JsonRpcClient, thread_id: str, timeout: float
) -> bool:
    result = response_result(client.request("thread/list", {}, timeout), "thread_list")
    threads = result.get("data")
    if not isinstance(threads, list):
        raise AppServerFailure("thread_list_invalid_data")
    return any(isinstance(thread, dict) and thread.get("id") == thread_id for thread in threads)


def binding_summary(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "model": result.get("model"),
        "modelProvider": result.get("modelProvider"),
        "reasoningEffort": result.get("reasoningEffort"),
        "approvalPolicy": result.get("approvalPolicy"),
        "sandbox": result.get("sandbox"),
    }


def is_danger_full_access(value: object) -> bool:
    return value == "danger-full-access" or (
        isinstance(value, dict) and value.get("type") == "dangerFullAccess"
    )


def assert_green_snapshot(snapshot: dict[str, Any], expected_turn_count: int) -> None:
    if snapshot["turnCount"] != expected_turn_count:
        raise AppServerFailure("thread_replay_turn_count_mismatch")
    if snapshot["turnStatuses"] != ["completed"] * expected_turn_count:
        raise AppServerFailure("thread_replay_turn_status_mismatch")
    if any(
        not isinstance(count, int) or count < 2
        for count in snapshot["turnItemCounts"]
    ):
        raise AppServerFailure("thread_replay_missing_output")
    if snapshot["assistantOutputTurns"] != expected_turn_count:
        raise AppServerFailure("thread_replay_missing_assistant_output")


def run_green_lifecycle(
    client: JsonRpcClient,
    home: Path,
    timeout: float,
    external_model: str,
    requested_official_model: str,
) -> dict[str, Any]:
    stages: list[str] = ["list"]
    models = read_model_list(client, timeout)
    model_ids = {model_id(model) for model in models}
    if external_model not in model_ids:
        raise AppServerFailure("external_model_not_listed")
    catalog = catalog_summary(models, requested_official_model)
    if not catalog["requestedOfficialModelListed"] or not catalog[
        "requestedOfficialModelSupportsMax"
    ]:
        raise AppServerFailure("requested_official_binding_not_listed")

    workspace = home / "workspace"
    workspace.mkdir()
    thread_id: str | None = None
    native_cleanup = "not_started"
    result: dict[str, Any] | None = None
    try:
        stages.append("create")
        started = response_result(
            client.request(
                "thread/start",
                {
                    "cwd": str(workspace),
                    "model": external_model,
                    "modelProvider": "custom",
                    "approvalPolicy": "never",
                    "sandbox": "danger-full-access",
                    "ephemeral": False,
                },
                timeout,
            ),
            "thread_start",
        )
        thread_id = get_thread_id(get_thread(started, "thread_start"), "thread_start")

        stages.append("bootstrap")
        bootstrap_turn = turn_started(
            client,
            thread_id,
            BOOTSTRAP_INPUT,
            timeout,
            model=external_model,
            effort="low",
        )
        wait_for_turn(client, thread_id, bootstrap_turn, timeout)

        stages.append("read")
        bootstrap_snapshot = thread_snapshot(client, thread_id, timeout)
        assert_green_snapshot(bootstrap_snapshot, 1)

        stages.append("rename")
        response_result(
            client.request(
                "thread/name/set",
                {"threadId": thread_id, "name": SAFE_THREAD_NAME},
                timeout,
            ),
            "thread_name_set",
        )

        stages.append("full_binding_permission_preflight")
        full_turn = turn_started(
            client,
            thread_id,
            PREFLIGHT_INPUT,
            timeout,
            model=external_model,
            effort="max",
            permission_preflight=True,
        )
        wait_for_turn(client, thread_id, full_turn, timeout)

        stages.append("resume")
        resumed = response_result(
            client.request("thread/resume", {"threadId": thread_id}, timeout),
            "thread_resume",
        )
        binding = binding_summary(resumed)
        if (
            binding["model"] != external_model
            or binding["reasoningEffort"] != "max"
            or binding["approvalPolicy"] != "never"
            or not is_danger_full_access(binding["sandbox"])
        ):
            raise AppServerFailure("full_binding_not_replayed", binding)

        stages.append("continue")
        continuation_turn = turn_started(
            client, thread_id, CONTINUATION_INPUT, timeout
        )
        wait_for_turn(client, thread_id, continuation_turn, timeout)

        stages.append("read_replay")
        replay_snapshot = thread_snapshot(client, thread_id, timeout)
        assert_green_snapshot(replay_snapshot, 3)
        result = {
            "outcome": "passed",
            "stages": stages,
            "catalog": catalog,
            "binding": binding,
            "bootstrap": bootstrap_snapshot,
            "replay": replay_snapshot,
        }
    finally:
        if thread_id is not None:
            try:
                response_result(
                    client.request("thread/delete", {"threadId": thread_id}, 10),
                    "thread_delete",
                )
                native_cleanup = (
                    "failed"
                    if thread_list_contains(client, thread_id, 10)
                    else "passed"
                )
            except (AppServerFailure, TimeoutError):
                native_cleanup = "failed"
        if result is not None:
            result["nativeCleanup"] = native_cleanup
    if native_cleanup != "passed":
        raise AppServerFailure("native_cleanup_failed")
    if result is None:
        raise AppServerFailure("green_lifecycle_missing_result")
    return result


def classify_red_snapshot(snapshot: dict[str, Any]) -> str:
    if (
        snapshot["threadStatus"] == "active"
        and snapshot["turnStatuses"] == ["inProgress"]
        and snapshot["turnItemCounts"] == [1]
        and snapshot["assistantOutputTurns"] == 0
    ):
        return "in_progress_without_output"
    if (
        snapshot["threadStatus"] == "systemError"
        and snapshot["turnStatuses"] == ["completed"]
        and snapshot["turnItemCounts"] == [1]
        and snapshot["assistantOutputTurns"] == 0
    ):
        return "failed_without_output"
    return "unexpected_missing_model_state"


def red_continuation(
    client: JsonRpcClient, thread_id: str, timeout: float
) -> dict[str, Any]:
    try:
        response_result(
            client.request("thread/resume", {"threadId": thread_id}, timeout),
            "red_thread_resume",
        )
    except (AppServerFailure, TimeoutError):
        return {"status": "resume_rejected"}
    try:
        turn_id = turn_started(client, thread_id, CONTINUATION_INPUT, timeout)
    except (AppServerFailure, TimeoutError):
        return {"status": "turn_rejected"}
    try:
        wait_for_turn(client, thread_id, turn_id, timeout)
    except (AppServerFailure, TimeoutError):
        snapshot = thread_snapshot(client, thread_id, timeout)
        if snapshot["assistantOutputTurns"] == 0:
            return {"status": "no_usable_rollout", "snapshot": snapshot}
        return {"status": "unexpected_output", "snapshot": snapshot}
    return {"status": "unexpected_turn_completion"}


def run_red_missing_model(
    client: JsonRpcClient,
    home: Path,
    timeout: float,
    red_timeout: float,
    red_model: str,
) -> dict[str, Any]:
    models = read_model_list(client, timeout)
    if red_model in {model_id(model) for model in models}:
        raise AppServerFailure("red_model_is_listed")
    workspace = home / "red-workspace"
    workspace.mkdir()
    thread_id: str | None = None
    native_cleanup = "not_started"
    result: dict[str, Any] | None = None
    try:
        try:
            started = response_result(
                client.request(
                    "thread/start",
                    {
                        "cwd": str(workspace),
                        "model": red_model,
                        "modelProvider": "custom",
                        "approvalPolicy": "never",
                        "sandbox": "danger-full-access",
                        "ephemeral": False,
                    },
                    timeout,
                ),
                "red_thread_start",
            )
        except AppServerRequestRejected:
            result = {
                "outcome": "atomic_rejection",
                "stages": ["list", "create_rejected"],
            }
            native_cleanup = "not_needed"
            return result
        thread_id = get_thread_id(get_thread(started, "red_thread_start"), "red_thread_start")
        try:
            turn_id = turn_started(
                client,
                thread_id,
                BOOTSTRAP_INPUT,
                timeout,
                model=red_model,
                effort="low",
            )
        except AppServerRequestRejected:
            result = {
                "outcome": "atomic_rejection",
                "stages": ["list", "create", "turn_start_rejected"],
            }
            return result
        try:
            wait_for_turn(client, thread_id, turn_id, red_timeout)
        except (AppServerFailure, TimeoutError):
            snapshot = thread_snapshot(client, thread_id, timeout)
            initial_state = classify_red_snapshot(snapshot)
            continuation = red_continuation(
                client, thread_id, min(red_timeout, 20.0)
            )
            if (
                initial_state
                in {"in_progress_without_output", "failed_without_output"}
                and continuation["status"]
                in {"resume_rejected", "turn_rejected", "no_usable_rollout"}
            ):
                outcome = "non_atomic_missing_model"
            else:
                outcome = "unexpected_missing_model_state"
            result = {
                "outcome": outcome,
                "stages": [
                    "list",
                    "create",
                    "turn_start",
                    "bounded_wait",
                    "read",
                    "continue_without_binding_override",
                ],
                "initialState": initial_state,
                "snapshot": snapshot,
                "continuation": continuation,
            }
            return result
        result = {
            "outcome": "unexpected_turn_completion",
            "stages": ["list", "create", "turn_start", "turn_completed"],
        }
        return result
    finally:
        if thread_id is not None:
            try:
                response_result(
                    client.request("thread/delete", {"threadId": thread_id}, 10),
                    "thread_delete",
                )
                native_cleanup = "passed"
            except (AppServerFailure, TimeoutError):
                native_cleanup = "failed"
        if result is not None:
            result["nativeCleanup"] = native_cleanup


def with_isolated_client(
    *,
    codex_command: Path,
    connected: bool,
    gateway_base_url: str,
    gateway_key: str | None,
    action: Callable[[JsonRpcClient, Path], dict[str, Any]],
) -> dict[str, Any]:
    home = create_temporary_home()
    environment = isolated_environment(home)
    process: subprocess.Popen[str] | None = None
    client: JsonRpcClient | None = None
    temporary_cleanup = "not_started"
    result: dict[str, Any] | None = None
    try:
        if connected:
            prepare_connected_home(home, environment, gateway_base_url, gateway_key)
        process = start_app_server(codex_command, home, environment)
        client = JsonRpcClient(process)
        result = action(client, home)
    finally:
        if client is not None:
            client.close()
        if process is not None:
            stop_app_server(process)
        try:
            remove_temporary_home(home)
            temporary_cleanup = "passed"
        except AppServerFailure:
            temporary_cleanup = "failed"
        if result is not None:
            result["temporaryHomeCleanup"] = temporary_cleanup
    if temporary_cleanup != "passed":
        raise AppServerFailure("temporary_home_cleanup_failed")
    if result is None:
        raise AppServerFailure("isolated_run_missing_result")
    return result


def run_catalog_comparison(
    codex_command: Path,
    gateway_base_url: str,
    gateway_key: str | None,
    timeout: float,
    requested_official_model: str,
) -> dict[str, Any]:
    def inspect(connected: bool) -> dict[str, Any]:
        def action(client: JsonRpcClient, _home: Path) -> dict[str, Any]:
            initialize(client, timeout)
            return {
                "account": read_account_summary(client, timeout),
                "catalog": catalog_summary(
                    read_model_list(client, timeout), requested_official_model
                ),
            }

        return with_isolated_client(
            codex_command=codex_command,
            connected=connected,
            gateway_base_url=gateway_base_url,
            gateway_key=gateway_key,
            action=action,
        )

    return {"officialDisconnected": inspect(False), "codexHubConnected": inspect(True)}


def run_repeated_green_lifecycle(
    codex_command: Path,
    gateway_base_url: str,
    gateway_key: str | None,
    timeout: float,
    external_model: str,
    requested_official_model: str,
    repeat: int,
) -> dict[str, Any]:
    runs: list[dict[str, Any]] = []
    for _ in range(repeat):
        def action(client: JsonRpcClient, home: Path) -> dict[str, Any]:
            initialize(client, timeout)
            return run_green_lifecycle(
                client, home, timeout, external_model, requested_official_model
            )

        runs.append(
            with_isolated_client(
                codex_command=codex_command,
                connected=True,
                gateway_base_url=gateway_base_url,
                gateway_key=gateway_key,
                action=action,
            )
        )
    return {
        "outcome": "passed" if all(run.get("outcome") == "passed" for run in runs) else "failed",
        "runs": runs,
    }


def run_red_scenario(
    codex_command: Path,
    gateway_base_url: str,
    gateway_key: str | None,
    timeout: float,
    red_timeout: float,
    red_model: str,
) -> dict[str, Any]:
    def action(client: JsonRpcClient, home: Path) -> dict[str, Any]:
        initialize(client, timeout)
        return run_red_missing_model(client, home, timeout, red_timeout, red_model)

    return with_isolated_client(
        codex_command=codex_command,
        connected=True,
        gateway_base_url=gateway_base_url,
        gateway_key=gateway_key,
        action=action,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", help="Path to the App-managed Codex CLI")
    parser.add_argument(
        "--scenario",
        choices=("compare", "green", "red"),
        default="compare",
    )
    parser.add_argument("--gateway-base-url", default="http://127.0.0.1:9099")
    parser.add_argument("--gateway-key")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--red-timeout", type=float, default=30.0)
    parser.add_argument("--external-model", default="glm-5.2")
    parser.add_argument("--requested-official-model", default="gpt-5.6-terra")
    parser.add_argument("--red-model", default="codexhub-issue106-missing-model")
    parser.add_argument("--repeat", type=int, default=1)
    args = parser.parse_args(argv)
    if args.timeout <= 0 or args.red_timeout <= 0 or args.repeat < 1:
        parser.error("timeout values must be positive and repeat must be at least one")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        codex_command = resolve_app_codex(args.codex)
        if args.scenario == "compare":
            result = run_catalog_comparison(
                codex_command,
                args.gateway_base_url,
                args.gateway_key,
                args.timeout,
                args.requested_official_model,
            )
        elif args.scenario == "green":
            result = run_repeated_green_lifecycle(
                codex_command,
                args.gateway_base_url,
                args.gateway_key,
                args.timeout,
                args.external_model,
                args.requested_official_model,
                args.repeat,
            )
        else:
            result = run_red_scenario(
                codex_command,
                args.gateway_base_url,
                args.gateway_key,
                args.timeout,
                args.red_timeout,
                args.red_model,
            )
    except AppServerFailure as error:
        payload: dict[str, Any] = {"failure": error.operation, "outcome": "failed"}
        if error.details is not None:
            payload["details"] = error.details
        print(json.dumps(payload, sort_keys=True))
        return 1
    except TimeoutError as error:
        print(json.dumps({"failure": str(error), "outcome": "failed"}, sort_keys=True))
        return 1

    print(json.dumps(result, sort_keys=True))
    if args.scenario == "red":
        return 0 if result.get("outcome") == "non_atomic_missing_model" else 1
    return 0 if result.get("outcome", "passed") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
