"""Run a credential-free active-call model-switch regression.

The probe drives an isolated App-managed ``codex app-server`` against a
loopback Responses fixture.  The fixture emits one shell-command function
call, completes the tool item, and then holds the upstream terminal event on a
barrier while the client changes the model for subsequent turns.  It records
only model labels and bounded counts; no request bodies, prompts, identifiers,
or credentials are retained or printed.

This is synthetic protocol evidence.  It does not exercise a live provider or
the user's Desktop/Gateway process.
"""

from __future__ import annotations

try:
    from scripts.python_runtime_contract import require_python_313
except ModuleNotFoundError:
    from python_runtime_contract import require_python_313

require_python_313(__file__)

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable


REPO_ROOT = Path(__file__).resolve().parents[1]
TEMP_HOME_PREFIX = "codexhub-active-call-"
FIXTURE_GATEWAY_KEY = "fixture-only-active-call"
MODEL_A = "glm-5.2"
MODEL_B = "volc/glm-5.2"
FUNCTION_CALL_ID = "fixture-call-1"
FUNCTION_COMMAND = "echo active-call-fixture"
DEFAULT_TIMEOUT_SECONDS = 30.0
BARRIER_TIMEOUT_SECONDS = 20.0
SENSITIVE_ENVIRONMENT_NAMES = (
    "ANTHROPIC_API_KEY",
    "CODEXHUB_CODEX_TARGET_HOME",
    "CODEXHUB_RUNTIME_HOME",
    "OLLAMA_API_KEY",
    "OPENAI_API_KEY",
)
BAD_SIGNAL_MARKERS = (
    "fallback",
    "reconnect",
    "collaboration_boundary",
    "invalid_collaboration_boundary",
    "mixed_v1_v2",
)


class ActiveCallFailure(RuntimeError):
    """A sanitized failure kind suitable for bounded harness output."""


class RecordingJsonRpcClient:
    """Small recording wrapper around the existing isolated JSON-RPC client."""

    def __init__(self, process: subprocess.Popen[str]) -> None:
        # Import lazily so unit tests can inspect this script without starting
        # an app-server and so the existing lifecycle runner remains the source
        # of truth for JSON-RPC framing and cleanup semantics.
        from run_issue_106_task_lifecycle import JsonRpcClient

        class _Client(JsonRpcClient):
            def __init__(self, child: subprocess.Popen[str]) -> None:
                self.messages: list[dict[str, Any]] = []
                super().__init__(child)

            def _next_message(self, deadline: float, operation: str) -> dict[str, Any]:
                message = super()._next_message(deadline, operation)
                self.messages.append(message)
                return message

        self._client = _Client(process)

    @property
    def messages(self) -> list[dict[str, Any]]:
        return self._client.messages

    def request(self, method: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        return self._client.request(method, params, timeout)

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        self._client.notify(method, params)

    def wait_for_notification(
        self,
        predicate: Callable[[dict[str, Any]], bool],
        timeout: float,
        operation: str,
    ) -> dict[str, Any]:
        return self._client.wait_for_notification(predicate, timeout, operation)

    def close(self) -> None:
        self._client.close()


def _response_result(response: dict[str, Any], operation: str) -> dict[str, Any]:
    if "error" in response:
        raise ActiveCallFailure(f"{operation}_rejected")
    result = response.get("result")
    if not isinstance(result, dict):
        raise ActiveCallFailure(f"{operation}_invalid_result")
    return result


def _sse_event(event: dict[str, Any]) -> bytes:
    return ("data: " + json.dumps(event, separators=(",", ":")) + "\n\n").encode(
        "utf-8"
    )


def _text_response(response_id: str, model: str) -> tuple[dict[str, Any], ...]:
    return (
        {
            "type": "response.created",
            "response": {"id": response_id, "model": model, "status": "in_progress"},
        },
        {"type": "response.output_text.delta", "delta": "ACTIVE_CALL_OK"},
        {
            "type": "response.completed",
            "response": {"id": response_id, "model": model, "status": "completed", "output": []},
        },
    )


class FakeResponsesScenario:
    """Deterministic Responses stream with one barrier-delayed function call."""

    def __init__(self) -> None:
        self.active_call_ready = threading.Event()
        self.release_terminal = threading.Event()
        self.lock = threading.Lock()
        self.models: list[str] = []
        self.tool_call_response_count = 0
        self.terminal_response_count = 0
        self.barrier_timed_out = False
        self.invalid_auth = False
        self.unexpected_path = False

    @property
    def request_count(self) -> int:
        with self.lock:
            return len(self.models)

    def record_request(self, body: Any, authorization: str | None, path: str) -> int:
        if authorization != f"Bearer {FIXTURE_GATEWAY_KEY}":
            self.invalid_auth = True
        if path.split("?", 1)[0] != "/v1/responses":
            self.unexpected_path = True
        if not isinstance(body, dict) or not isinstance(body.get("model"), str):
            raise ActiveCallFailure("fake_upstream_invalid_request")
        model = body["model"]
        with self.lock:
            self.models.append(model)
            return len(self.models)

    def response_events(self, index: int, model: str, body: dict[str, Any]) -> tuple[dict[str, Any], ...]:
        response_id = f"fixture-response-{index}"
        if index != 1:
            return _text_response(response_id, model)

        with self.lock:
            self.tool_call_response_count += 1
        # ``output_item.done`` makes the client start the local command item;
        # holding only the terminal response keeps that item genuinely active
        # while settings/update is processed by app-server.
        events: list[dict[str, Any]] = [
            {
                "type": "response.created",
                "response": {"id": response_id, "model": model, "status": "in_progress"},
            },
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {
                    "id": "fixture-function-item-1",
                    "type": "function_call",
                    "status": "in_progress",
                    "call_id": FUNCTION_CALL_ID,
                    "name": "shell_command",
                    "arguments": "",
                },
            },
            {
                "type": "response.function_call_arguments.delta",
                "output_index": 0,
                "item_id": "fixture-function-item-1",
                "delta": json.dumps({"command": FUNCTION_COMMAND}, separators=(",", ":")),
            },
            {
                "type": "response.function_call_arguments.done",
                "output_index": 0,
                "item_id": "fixture-function-item-1",
                "arguments": json.dumps({"command": FUNCTION_COMMAND}, separators=(",", ":")),
            },
            {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": {
                    "id": "fixture-function-item-1",
                    "type": "function_call",
                    "status": "completed",
                    "call_id": FUNCTION_CALL_ID,
                    "name": "shell_command",
                    "arguments": json.dumps({"command": FUNCTION_COMMAND}, separators=(",", ":")),
                },
            },
        ]
        return tuple(events)

    def wait_for_release(self) -> None:
        self.active_call_ready.set()
        if not self.release_terminal.wait(BARRIER_TIMEOUT_SECONDS):
            self.barrier_timed_out = True

    def record_terminal(self) -> None:
        with self.lock:
            self.terminal_response_count += 1


def _make_handler(scenario: FakeResponsesScenario):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, _format: str, *_args: object) -> None:
            return

        def do_POST(self) -> None:  # noqa: N802
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(content_length)
                body = json.loads(raw.decode("utf-8"))
                index = scenario.record_request(
                    body,
                    self.headers.get("Authorization"),
                    self.path,
                )
                if not isinstance(body, dict):
                    raise ActiveCallFailure("fake_upstream_invalid_request")
                events = scenario.response_events(index, str(body["model"]), body)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                for event in events:
                    self.wfile.write(_sse_event(event))
                    self.wfile.flush()
                if index == 1:
                    scenario.wait_for_release()
                    terminal = {
                        "type": "response.completed",
                        "response": {
                            "id": "fixture-response-1",
                            "model": str(body["model"]),
                            "status": "completed",
                            "output": [
                                {
                                    "id": "fixture-function-item-1",
                                    "type": "function_call",
                                    "status": "completed",
                                    "call_id": FUNCTION_CALL_ID,
                                    "name": "shell_command",
                                    "arguments": json.dumps(
                                        {"command": FUNCTION_COMMAND}, separators=(",", ":")
                                    ),
                                }
                            ],
                        },
                    }
                    self.wfile.write(_sse_event(terminal))
                    self.wfile.flush()
                else:
                    # The fixture's normal text responses already include the
                    # terminal event in ``events``; this branch just keeps the
                    # accounting explicit and avoids retaining response data.
                    pass
                scenario.record_terminal()
            except (BrokenPipeError, ConnectionResetError):
                return
            except (ActiveCallFailure, ValueError, UnicodeError, OSError):
                self.send_response(400)
                self.send_header("Connection", "close")
                self.end_headers()

    return Handler


def _safe_environment(home: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment["CODEX_HOME"] = str(home)
    environment.pop("CODEX_CONFIG", None)
    for name in SENSITIVE_ENVIRONMENT_NAMES:
        environment.pop(name, None)
    environment["NO_PROXY"] = "127.0.0.1,localhost"
    environment["no_proxy"] = "127.0.0.1,localhost"
    return environment


def _write_fixture_catalog(home: Path, environment: dict[str, str]) -> Path:
    catalog_dir = home / "model-catalogs"
    catalog_dir.mkdir(parents=True, exist_ok=True)
    catalog_path = catalog_dir / "codexhub-model-catalog.json"
    command = [sys.executable, str(REPO_ROOT / "src-python" / "catalog_sync.py"), "--sync"]
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
        raise ActiveCallFailure("isolated_catalog_setup_failed") from error
    if completed.returncode != 0 or not catalog_path.is_file():
        raise ActiveCallFailure("isolated_catalog_setup_failed")
    try:
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ActiveCallFailure("isolated_catalog_invalid") from error
    models = catalog.get("models") if isinstance(catalog, dict) else None
    model_ids = {
        item.get("slug")
        for item in models
        if isinstance(item, dict) and isinstance(item.get("slug"), str)
    } if isinstance(models, list) else set()
    if not {MODEL_A, MODEL_B}.issubset(model_ids):
        raise ActiveCallFailure("fixture_models_not_available")
    return catalog_path


def _prepare_home(home: Path, environment: dict[str, str], base_url: str) -> None:
    catalog_path = _write_fixture_catalog(home, environment)
    command = [
        sys.executable,
        str(REPO_ROOT / "src-python" / "config_overlay.py"),
        "apply",
        "--config",
        str(home / "config.toml"),
        "--backup",
        str(home / "proxy" / "config.toml.backup"),
        "--catalog",
        str(catalog_path),
        "--base-url",
        base_url,
        "--owner",
        "release",
        "--gateway-key",
        FIXTURE_GATEWAY_KEY,
    ]
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
        raise ActiveCallFailure("isolated_config_setup_failed") from error
    if completed.returncode != 0:
        raise ActiveCallFailure("isolated_config_setup_failed")


def _resolve_codex(explicit: str | None) -> Path:
    if explicit:
        candidate = Path(explicit).expanduser().resolve()
        if candidate.is_file():
            return candidate
        raise ActiveCallFailure("app_cli_not_found")
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
    raise ActiveCallFailure("app_cli_not_found")


def _codex_version(codex: Path) -> str:
    try:
        completed = subprocess.run(
            [str(codex), "--version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ActiveCallFailure("app_cli_version_unavailable") from error
    if completed.returncode != 0:
        raise ActiveCallFailure("app_cli_version_unavailable")
    version = (completed.stdout or completed.stderr).strip().splitlines()
    if not version:
        raise ActiveCallFailure("app_cli_version_unavailable")
    return version[0][:80]


def _start_app_server(codex: Path, home: Path, environment: dict[str, str]) -> subprocess.Popen[str]:
    command = [
        str(codex),
        "app-server",
        "--disable",
        "plugins",
        "--disable",
        "remote_plugin",
        "--disable",
        "plugin_sharing",
        "--stdio",
    ]
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
        raise ActiveCallFailure("app_server_start_failed") from error


def _stop_app_server(process: subprocess.Popen[str]) -> None:
    try:
        if process.stdin is not None and not process.stdin.closed:
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
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ActiveCallFailure("app_server_cleanup_failed") from error


def _initialize(client: RecordingJsonRpcClient, timeout: float) -> None:
    _response_result(
        client.request(
            "initialize",
            {
                "clientInfo": {"name": "codexhub-active-call-regression", "version": "1"},
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


def _wait_notification(
    client: RecordingJsonRpcClient,
    predicate: Callable[[dict[str, Any]], bool],
    timeout: float,
    operation: str,
) -> dict[str, Any]:
    return client.wait_for_notification(predicate, timeout, operation)


def _thread_id(result: dict[str, Any]) -> str:
    thread = result.get("thread")
    thread_id = thread.get("id") if isinstance(thread, dict) else None
    if not isinstance(thread_id, str) or not thread_id:
        raise ActiveCallFailure("thread_start_missing_id")
    return thread_id


def _turn_id(result: dict[str, Any]) -> str:
    turn = result.get("turn")
    turn_id = turn.get("id") if isinstance(turn, dict) else None
    if not isinstance(turn_id, str) or not turn_id:
        raise ActiveCallFailure("turn_start_missing_id")
    return turn_id


def _wait_turn_completed(client: RecordingJsonRpcClient, thread_id: str, turn_id: str, timeout: float) -> None:
    completed = _wait_notification(
        client,
        lambda message: (
            message.get("method") == "turn/completed"
            and isinstance(message.get("params"), dict)
            and message["params"].get("threadId") == thread_id
            and isinstance(message["params"].get("turn"), dict)
            and message["params"]["turn"].get("id") == turn_id
        ),
        timeout,
        "turn_completed",
    )
    turn = completed["params"]["turn"]
    if turn.get("status") != "completed":
        raise ActiveCallFailure("turn_not_completed")


def _has_bad_signal(messages: list[dict[str, Any]]) -> bool:
    text = json.dumps(messages, ensure_ascii=True, separators=(",", ":")).lower()
    return any(marker in text for marker in BAD_SIGNAL_MARKERS)


def _assert_active_item(
    client: RecordingJsonRpcClient,
    thread_id: str,
    turn_id: str,
    timeout: float,
) -> None:
    _wait_notification(
        client,
        lambda message: (
            message.get("method") == "item/started"
            and isinstance(message.get("params"), dict)
            and message["params"].get("threadId") == thread_id
            and message["params"].get("turnId") == turn_id
            and isinstance(message["params"].get("item"), dict)
            and message["params"]["item"].get("type") == "commandExecution"
        ),
        timeout,
        "active_function_call_item",
    )


def _cleanup_active_call_resources(
    scenario: FakeResponsesScenario,
    home: Path,
    *,
    client: RecordingJsonRpcClient | None,
    process: subprocess.Popen[str] | None,
    server: ThreadingHTTPServer | None,
    server_thread: threading.Thread | None,
) -> dict[str, str]:
    """Release and tear down every task-owned resource with bounded waits."""

    statuses = {
        "client": "not_started",
        "appServer": "not_started",
        "gateway": "not_started",
        "gatewayThread": "not_started",
        "temporaryHome": "not_started",
    }
    failures: list[str] = []
    scenario.release_terminal.set()

    if client is not None:
        try:
            client.close()
            statuses["client"] = "passed"
        except Exception:
            statuses["client"] = "failed"
            failures.append("client_cleanup_failed")

    if process is not None:
        try:
            _stop_app_server(process)
            statuses["appServer"] = "passed"
        except Exception:
            statuses["appServer"] = "failed"
            failures.append("app_server_cleanup_failed")

    if server is not None:
        shutdown_errors: list[BaseException] = []

        def shutdown_server() -> None:
            try:
                server.shutdown()
            except BaseException as error:  # pragma: no cover - defensive thread boundary
                shutdown_errors.append(error)

        shutdown_thread = threading.Thread(target=shutdown_server, daemon=True)
        try:
            shutdown_thread.start()
            shutdown_thread.join(timeout=5)
            if shutdown_thread.is_alive() or shutdown_errors:
                statuses["gateway"] = "failed"
                failures.append("loopback_server_cleanup_failed")
        except Exception:
            statuses["gateway"] = "failed"
            failures.append("loopback_server_cleanup_failed")
        try:
            server.server_close()
        except Exception:
            statuses["gateway"] = "failed"
            failures.append("loopback_server_cleanup_failed")
        else:
            if statuses["gateway"] != "failed":
                statuses["gateway"] = "passed"

    if server_thread is not None:
        try:
            server_thread.join(timeout=2)
            thread_alive = server_thread.is_alive()
        except Exception:
            thread_alive = True
        if thread_alive:
            statuses["gatewayThread"] = "failed"
            failures.append("loopback_server_cleanup_failed")
        else:
            statuses["gatewayThread"] = "passed"

    try:
        resolved_home = home.resolve()
        temp_root = Path(tempfile.gettempdir()).resolve()
        if resolved_home.parent != temp_root or not resolved_home.name.startswith(TEMP_HOME_PREFIX):
            raise ActiveCallFailure("temporary_home_outside_scope")
        shutil.rmtree(resolved_home)
        statuses["temporaryHome"] = "passed"
    except FileNotFoundError:
        statuses["temporaryHome"] = "passed"
    except Exception:
        statuses["temporaryHome"] = "failed"
        failures.append("temp_home_cleanup_failed")

    if failures:
        raise ActiveCallFailure(failures[0])
    return statuses


def run_regression(codex: Path, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> dict[str, Any]:
    cli_version = _codex_version(codex)
    scenario = FakeResponsesScenario()
    home = Path(tempfile.mkdtemp(prefix=TEMP_HOME_PREFIX))
    server: ThreadingHTTPServer | None = None
    server_thread: threading.Thread | None = None
    process: subprocess.Popen[str] | None = None
    client: RecordingJsonRpcClient | None = None
    result: dict[str, Any] | None = None
    cleanup_error: ActiveCallFailure | None = None
    try:
        server = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(scenario))
        server.daemon_threads = True
        server_thread = threading.Thread(target=server.serve_forever, daemon=True)
        server_thread.start()
        environment = _safe_environment(home)
        _prepare_home(home, environment, f"http://127.0.0.1:{server.server_port}")
        process = _start_app_server(codex, home, environment)
        client = RecordingJsonRpcClient(process)
        _initialize(client, timeout)

        listed = _response_result(
            client.request("model/list", {"limit": 100}, timeout), "model_list"
        ).get("data")
        listed_ids = {
            item.get("id")
            for item in listed
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        if not {MODEL_A, MODEL_B}.issubset(listed_ids):
            raise ActiveCallFailure("fixture_models_not_listed")

        workspace = home / "workspace"
        workspace.mkdir()
        thread_result = _response_result(
            client.request(
                "thread/start",
                {
                    "cwd": str(workspace),
                    "model": MODEL_A,
                    "modelProvider": "custom",
                    "approvalPolicy": "never",
                    "sandbox": "read-only",
                    "ephemeral": True,
                },
                timeout,
            ),
            "thread_start",
        )
        thread_id = _thread_id(thread_result)
        first_turn_result = _response_result(
            client.request(
                "turn/start",
                {"threadId": thread_id, "input": [{"type": "text", "text": "trigger fixture function call"}]},
                timeout,
            ),
            "first_turn_start",
        )
        first_turn_id = _turn_id(first_turn_result)
        if not scenario.active_call_ready.wait(timeout):
            raise ActiveCallFailure("active_call_barrier_not_reached")
        _assert_active_item(client, thread_id, first_turn_id, timeout)

        _response_result(
            client.request(
                "thread/settings/update",
                {"threadId": thread_id, "model": MODEL_B},
                timeout,
            ),
            "settings_update",
        )
        _wait_notification(
            client,
            lambda message: (
                message.get("method") == "thread/settings/updated"
                and isinstance(message.get("params"), dict)
                and message["params"].get("threadId") == thread_id
                and isinstance(message["params"].get("threadSettings"), dict)
                and message["params"]["threadSettings"].get("model") == MODEL_B
            ),
            timeout,
            "settings_updated",
        )
        scenario.release_terminal.set()
        _wait_turn_completed(client, thread_id, first_turn_id, timeout)

        second_turn_result = _response_result(
            client.request(
                "turn/start",
                {"threadId": thread_id, "input": [{"type": "text", "text": "continue fixture"}]},
                timeout,
            ),
            "second_turn_start",
        )
        second_turn_id = _turn_id(second_turn_result)
        _wait_turn_completed(client, thread_id, second_turn_id, timeout)

        with scenario.lock:
            observed_models = list(scenario.models)
            terminal_count = scenario.terminal_response_count
            tool_call_count = scenario.tool_call_response_count
        if observed_models != [MODEL_A, MODEL_A, MODEL_B]:
            raise ActiveCallFailure("active_call_model_rebound_or_duplicate")
        if tool_call_count != 1 or terminal_count != len(observed_models):
            raise ActiveCallFailure("active_call_duplicate_or_missing_terminal")
        if scenario.invalid_auth or scenario.unexpected_path or scenario.barrier_timed_out:
            raise ActiveCallFailure("fake_gateway_contract_failed")
        if _has_bad_signal(client.messages):
            raise ActiveCallFailure("app_server_bad_signal")
        result = {
            "outcome": "passed",
            "evidence": "synthetic_app_server_loopback_only",
            "cliVersion": cli_version,
            "modelSequence": observed_models,
            "upstreamRequestCount": len(observed_models),
            "toolCallResponseCount": tool_call_count,
            "terminalResponseCount": terminal_count,
            "activeItemObserved": True,
            "fallbackCount": 0,
            "reconnectCount": 0,
            "boundaryErrorCount": 0,
        }
    finally:
        try:
            cleanup_statuses = _cleanup_active_call_resources(
                scenario,
                home,
                client=client,
                process=process,
                server=server,
                server_thread=server_thread,
            )
        except ActiveCallFailure as error:
            cleanup_error = error
        except Exception:
            cleanup_error = ActiveCallFailure("active_call_cleanup_failed")
        if cleanup_error is not None:
            raise cleanup_error
    if result is not None:
        result["cleanup"] = cleanup_statuses
    if result is None:
        raise ActiveCallFailure("active_call_missing_result")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run isolated active-call model-switch regression.")
    parser.add_argument("--codex", type=Path, help="Path to codex CLI executable.")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    args = parser.parse_args(argv)
    try:
        result = run_regression(_resolve_codex(str(args.codex) if args.codex else None), args.timeout)
    except ActiveCallFailure as error:
        print(json.dumps({"outcome": "bounded_not_run", "reason": str(error)}, sort_keys=True))
        return 2
    except (OSError, TimeoutError):
        print(json.dumps({"outcome": "bounded_not_run", "reason": "isolated_probe_failed"}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
