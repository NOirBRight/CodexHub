"""Request-local official Claude CLI exchange with caller-owned MCP tools.

Completed history, including system instructions, is carried as JSON in CLI
user context. This declared adaptation has no native system-message priority.
No process/session is retained after a request, and no tool result is invented.
"""
from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import queue
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable

from claude_native_models import cli_command, concrete_executable
from cli_subscription_discovery import run_cli
from subscription_backend_contract import BackendError

_MAX_BODY = 4 * 1024 * 1024
_MAX_OUTPUT = 16 * 1024 * 1024
_TOOL_NAME = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+\[\]-]{0,255}\Z")
_MCP_PREFIX = "mcp__codexhub__"


def _failure(code: str, status: int = 502) -> BackendError:
    messages = {
        "cli-missing": "Install the official Claude CLI before using this Provider.",
        "auth-required": "Sign in through the official Claude CLI before retrying.",
        "auth-expired": "Renew the current login through the official Claude CLI before retrying.",
        "account-changed": "The official Claude login changed during this request. Retry with the current account.",
        "not-eligible": "The current official Claude account cannot run this request.",
        "rate-limited": "The official Claude account rejected this request because of its usage limit.",
        "cancelled": "The caller cancelled the Claude request.",
        "timeout": "The official Claude request exceeded its time limit.",
        "invalid-request": "The Claude request contains unsupported or incomplete content or tool history.",
        "unsupported-parameter": "The official Claude CLI cannot preserve this requested generation parameter.",
        "backend-contract": "The official Claude CLI returned an unsupported or inconsistent tool stream.",
        "backend-failed": "The official Claude CLI could not complete this request.",
    }
    return BackendError(code, messages[code], status)


def _source_bytes(path: Path) -> bytes:
    try:
        with path.open("rb") as source:
            value = source.read(_MAX_BODY + 1)
    except OSError:
        raise _failure("auth-required", 401) from None
    if len(value) > _MAX_BODY:
        raise _failure("auth-required", 401)
    return value


@dataclass(frozen=True)
class _Account:
    path: Path = field(repr=False)
    original: bytes = field(repr=False)
    oauth: dict[str, Any] = field(repr=False)
    expiry: float

    def check(self) -> None:
        try:
            current = _source_bytes(self.path)
        except BackendError:
            raise _failure("account-changed", 409) from None
        if current != self.original:
            raise _failure("account-changed", 409)
        if self.expiry <= time.time():
            raise _failure("auth-expired", 401)


def _account(home: Path, env: Mapping[str, str]) -> _Account:
    path = Path(env.get("CLAUDE_CONFIG_DIR") or home / ".claude") / ".credentials.json"
    original = _source_bytes(path)
    try:
        value = json.loads(original)
        oauth = value["claudeAiOauth"]
        token = oauth["accessToken"]
        expiry = oauth["expiresAt"]
        if not isinstance(token, str) or not token or any(char.isspace() for char in token):
            raise ValueError()
        if not isinstance(expiry, (float, int)) or isinstance(expiry, bool) or not math.isfinite(expiry):
            raise ValueError()
        expiry = expiry / 1000 if expiry >= 1e12 else expiry
        scopes = oauth.get("scopes")
        if not isinstance(scopes, list) or "user:inference" not in scopes:
            raise _failure("not-eligible", 403)
    except (ValueError, KeyError, TypeError):
        raise _failure("auth-required", 401) from None
    result = _Account(path, original, oauth, expiry)
    result.check()
    return result


def _private_write(path: Path, value: Any) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as output:
        json.dump(value, output, ensure_ascii=False, separators=(",", ":"))


def _environment(source: Mapping[str, str], root: Path, binary: Path) -> dict[str, str]:
    # Preserve only execution prerequisites, never provider settings, proxy,
    # CLI hooks, NODE_OPTIONS, injected Gateway routes, or account overrides.
    env = {key: value for key, value in source.items() if key in {
        "PATH", "SYSTEMROOT", "SystemRoot", "WINDIR", "COMSPEC", "PATHEXT"}}
    env.update({
        "PATH": str(binary.parent) + os.pathsep + env.get("PATH", os.defpath),
        "HOME": str(root), "USERPROFILE": str(root), "CLAUDE_CONFIG_DIR": str(root / ".claude"),
        "APPDATA": str(root / "appdata"), "LOCALAPPDATA": str(root / "localappdata"),
        "XDG_CONFIG_HOME": str(root / "config"), "XDG_CACHE_HOME": str(root / "cache"),
        "XDG_DATA_HOME": str(root / "data"), "TMPDIR": str(root), "TMP": str(root), "TEMP": str(root),
        "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TERM": "dumb", "NO_COLOR": "1",
        "DISABLE_AUTOUPDATER": "1", "DISABLE_TELEMETRY": "1", "DISABLE_ERROR_REPORTING": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1", "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
        "ENABLE_CLAUDEAI_MCP_SERVERS": "0", "DISABLE_AUTO_COMPACT": "1",
    })
    return env


def _validate_content(value: Any) -> None:
    if value is None or isinstance(value, str):
        return
    if not isinstance(value, list):
        raise _failure("invalid-request", 400)
    for block in value:
        # Images/audio are rejected visibly rather than embedded as unviewable
        # JSON or fetched by an unrestricted built-in execution tool.
        if not isinstance(block, dict) or block.get("type") != "text" or not isinstance(block.get("text"), str):
            raise _failure("invalid-request", 400)


def _request(payload: Mapping[str, Any]) -> tuple[str, list[dict[str, Any]], bytes, set[str]]:
    # No observed native flag represents these caller requirements. Reject
    # before credentials/private artifacts/processes rather than drop them.
    for parameter in ("max_tokens", "max_completion_tokens", "temperature", "top_p", "response_format", "stop",
                      "audio", "modalities", "logit_bias", "seed", "frequency_penalty", "presence_penalty",
                      "prediction", "parallel_tool_calls", "functions", "function_call", "logprobs", "top_logprobs"):
        if payload.get(parameter) is not None:
            raise _failure("unsupported-parameter", 400)
    if payload.get("n") not in (None, 1):
        raise _failure("unsupported-parameter", 400)
    model = payload.get("model")
    if not isinstance(model, str) or not _MODEL.fullmatch(model):
        raise _failure("invalid-request", 400)
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise _failure("invalid-request", 400)
    pending: dict[str, dict[str, Any]] = {}
    completed: set[str] = set()
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in {"system", "developer", "user", "assistant", "tool"}:
            raise _failure("invalid-request", 400)
        role = message["role"]
        _validate_content(message.get("content"))
        if role == "tool":
            call_id = message.get("tool_call_id")
            if not isinstance(call_id, str) or call_id not in pending or message.get("content") is None:
                raise _failure("invalid-request", 400)
            del pending[call_id]
            completed.add(call_id)
        elif pending:
            raise _failure("invalid-request", 400)
        if message.get("tool_calls") is not None:
            calls = message["tool_calls"]
            if role != "assistant" or not isinstance(calls, list):
                raise _failure("invalid-request", 400)
            for call in calls:
                if not isinstance(call, dict) or call.get("type") != "function":
                    raise _failure("invalid-request", 400)
                call_id = call.get("id")
                function = call.get("function")
                if not isinstance(call_id, str) or not call_id or call_id in pending or call_id in completed or not isinstance(function, dict):
                    raise _failure("invalid-request", 400)
                if not isinstance(function.get("name"), str) or not _TOOL_NAME.fullmatch(function["name"]):
                    raise _failure("invalid-request", 400)
                try:
                    args = json.loads(function["arguments"])
                except (ValueError, KeyError, TypeError):
                    raise _failure("invalid-request", 400) from None
                if not isinstance(args, dict):
                    raise _failure("invalid-request", 400)
                pending[call_id] = call
    if pending:
        raise _failure("invalid-request", 400)
    tools = payload.get("tools", [])
    if not isinstance(tools, list):
        raise _failure("invalid-request", 400)
    manifest = []
    names: set[str] = set()
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "function" or not isinstance(tool.get("function"), dict):
            raise _failure("invalid-request", 400)
        function = tool["function"]
        name = function.get("name")
        parameters = function.get("parameters", {"type": "object", "properties": {}})
        if not isinstance(name, str) or not _TOOL_NAME.fullmatch(name) or name in names or not isinstance(parameters, dict):
            raise _failure("invalid-request", 400)
        description = function.get("description", "")
        if not isinstance(description, str):
            raise _failure("invalid-request", 400)
        names.add(name)
        manifest.append({"name": name, "description": description, "inputSchema": parameters})
    choice = payload.get("tool_choice", "auto")
    if choice == "none":
        manifest = []
    elif isinstance(choice, dict):
        function = choice.get("function")
        selected = function.get("name") if isinstance(function, dict) else None
        if choice.get("type") != "function" or selected not in names:
            raise _failure("invalid-request", 400)
        manifest = [tool for tool in manifest if tool["name"] == selected]
    elif not isinstance(choice, str) or choice not in {"auto", "required"}:
        raise _failure("invalid-request", 400)
    if choice == "required" and not manifest:
        raise _failure("invalid-request", 400)
    context = {
        "adaptation": "Complete calling-agent history in CLI user context; system/developer priority is not native.",
        "instructions": "Continue this conversation. Preserve roles, exact tool call IDs, arguments and results. "
                        "Completed tool results are authoritative; do not reissue completed side effects. "
                        "Use only the declared MCP caller tools for new calls. The caller executes them.",
        "messages": messages, "tool_choice": choice,
    }
    try:
        prompt = json.dumps(context, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        line = json.dumps({"type": "user", "message": {"role": "user", "content": prompt}},
                          ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode() + b"\n"
        manifest_bytes = json.dumps(manifest, allow_nan=False).encode()
    except (ValueError, TypeError):
        raise _failure("invalid-request", 400) from None
    if len(line) > _MAX_BODY or len(manifest_bytes) > _MAX_BODY:
        raise _failure("invalid-request", 400)
    return model, manifest, line, completed


class _Callback:
    """Request-local authenticated callback which never produces tool results."""

    def __init__(self, tools: list[dict[str, Any]], stop: threading.Event):
        self.calls: dict[str, dict[str, Any]] = {}
        self.lock = threading.Lock()
        self.error = False
        path = "/" + secrets.token_urlsafe(32)
        names = {row["name"] for row in tools}
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if self.path != path or not 0 < length <= _MAX_BODY:
                        self.send_error(404)
                        return
                    self.connection.settimeout(2)
                    call = json.loads(self.rfile.read(length))
                    if (not isinstance(call, dict) or not isinstance(call.get("id"), str) or not call["id"]
                            or call.get("name") not in names or not isinstance(call.get("arguments"), dict)):
                        raise ValueError()
                    with owner.lock:
                        if call["id"] in owner.calls:
                            raise ValueError()
                        owner.calls[call["id"]] = call
                except (ValueError, OSError, TypeError):
                    owner.error = True
                    self.send_error(400)
                    return
                stop.wait()
                # Connection closes on process termination, without a response.
                self.close_connection = True

            def log_message(self, *_args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        self.url = f"http://127.0.0.1:{self.server.server_port}{path}"
        self.thread.start()

    def check(self, calls: Mapping[str, dict[str, Any]]) -> None:
        with self.lock:
            if self.error or any(call_id not in calls or call != calls[call_id] for call_id, call in self.calls.items()):
                raise _failure("backend-contract")

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def _kill(child: subprocess.Popen[bytes]) -> None:
    if os.name == "nt" and child.poll() is None:
        try:
            subprocess.run([str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"),
                            "/PID", str(child.pid), "/T", "/F"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=5, check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass
    elif os.name != "nt":
        try:
            os.killpg(child.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if child.poll() is None:
        child.kill()
    child.wait(timeout=5)


class _WindowsTree:
    """Bind a suspended child to a non-breakaway kill-on-close Windows Job.

    Assign before ResumeThread so no MCP descendant can escape the lifetime.
    https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects
    """

    def __init__(self, child: subprocess.Popen[bytes]):
        import ctypes
        from ctypes import wintypes

        class BasicLimit(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                        ("flags", wintypes.DWORD), ("min_working_set", ctypes.c_size_t),
                        ("max_working_set", ctypes.c_size_t), ("active_processes", wintypes.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]

        class ExtendedLimit(ctypes.Structure):
            _fields_ = [("basic", BasicLimit), ("io_counters", ctypes.c_uint64 * 6),
                        ("process_memory", ctypes.c_size_t), ("job_memory", ctypes.c_size_t),
                        ("peak_process_memory", ctypes.c_size_t), ("peak_job_memory", ctypes.c_size_t)]

        class ThreadEntry(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD), ("thread_id", wintypes.DWORD),
                        ("process_id", wintypes.DWORD), ("base_priority", wintypes.LONG),
                        ("delta_priority", wintypes.LONG), ("flags", wintypes.DWORD)]

        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        prototypes = {
            "CreateJobObjectW": ([ctypes.c_void_p, wintypes.LPCWSTR], wintypes.HANDLE),
            "SetInformationJobObject": ([wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD], wintypes.BOOL),
            "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
            "CreateToolhelp32Snapshot": ([wintypes.DWORD, wintypes.DWORD], wintypes.HANDLE),
            "Thread32First": ([wintypes.HANDLE, ctypes.POINTER(ThreadEntry)], wintypes.BOOL),
            "Thread32Next": ([wintypes.HANDLE, ctypes.POINTER(ThreadEntry)], wintypes.BOOL),
            "OpenThread": ([wintypes.DWORD, wintypes.BOOL, wintypes.DWORD], wintypes.HANDLE),
            "ResumeThread": ([wintypes.HANDLE], wintypes.DWORD),
            "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
        }
        for name, (arguments, result) in prototypes.items():
            function = getattr(self.kernel, name)
            function.argtypes = arguments
            function.restype = result
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        snapshot = None
        try:
            limits = ExtendedLimit()
            limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
                raise ctypes.WinError(ctypes.get_last_error())
            if not self.kernel.AssignProcessToJobObject(self.handle, int(child._handle)):
                raise ctypes.WinError(ctypes.get_last_error())
            snapshot = self.kernel.CreateToolhelp32Snapshot(4, 0)  # TH32CS_SNAPTHREAD
            if snapshot == ctypes.c_void_p(-1).value:
                snapshot = None
                raise ctypes.WinError(ctypes.get_last_error())
            entry = ThreadEntry()
            entry.size = ctypes.sizeof(entry)
            present = self.kernel.Thread32First(snapshot, ctypes.byref(entry))
            while present:
                if entry.process_id == child.pid:
                    thread = self.kernel.OpenThread(2, False, entry.thread_id)  # THREAD_SUSPEND_RESUME
                    if not thread:
                        raise ctypes.WinError(ctypes.get_last_error())
                    try:
                        if self.kernel.ResumeThread(thread) == 0xFFFFFFFF:
                            raise ctypes.WinError(ctypes.get_last_error())
                    finally:
                        self.kernel.CloseHandle(thread)
                    break
                present = self.kernel.Thread32Next(snapshot, ctypes.byref(entry))
            else:
                raise OSError("suspended Claude thread was not found")
        except Exception:
            self.close()
            raise
        finally:
            if snapshot is not None:
                self.kernel.CloseHandle(snapshot)

    def close(self) -> None:
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def _cli_error(event: Mapping[str, Any]) -> BackendError:
    words = str(event.get("result", "")) + " " + str(event.get("errors", ""))
    lowered = words.lower()
    if any(term in lowered for term in ("organization", "disabled", "not eligible", "not allowed", "403")):
        return _failure("not-eligible", 403)
    if any(term in lowered for term in ("expired", "401", "authentication", "not logged in", "invalid token")):
        return _failure("auth-required", 401)
    if any(term in lowered for term in ("rate limit", "usage limit", "429", "quota")):
        return _failure("rate-limited", 429)
    return _failure("backend-failed")


def _reported_usage(value: Mapping[str, Any]) -> dict[str, Any] | None:
    if not all(isinstance(value.get(key), int) and not isinstance(value[key], bool)
               and value[key] >= 0 for key in ("input_tokens", "output_tokens")):
        return None
    input_tokens = value["input_tokens"]
    details = {}
    for source, target in (("cache_read_input_tokens", "cached_tokens"),
                           ("cache_creation_input_tokens", "cache_creation_tokens")):
        count = value.get(source)
        if isinstance(count, int) and not isinstance(count, bool) and count >= 0:
            input_tokens += count
            details[target] = count
    result = {"prompt_tokens": input_tokens, "completion_tokens": value["output_tokens"],
              "total_tokens": input_tokens + value["output_tokens"]}
    if details:
        result["prompt_tokens_details"] = details
    return result


def stream_chat(payload: Mapping[str, Any], *, cancel: threading.Event, timeout: float,
                source_home: Path | None = None, environ: Mapping[str, str] | None = None,
                binary: Path | None = None, status_runner: Callable[..., Any] = run_cli,
                process_factory: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen) -> Iterator[dict[str, Any]]:
    """Yield canonical Chat Completions chunks from an isolated official CLI.

    Binary/status/process ports permit deterministic testing of the real stdio
    and MCP boundary. Only the official source login is admitted. Caller closes
    the iterator or sets cancel to reap all children and remove private files.
    """
    model, tools, input_line, completed_call_ids = _request(payload)
    if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
        raise _failure("invalid-request", 400)
    environment = dict(os.environ if environ is None else environ)
    candidate = binary or shutil.which("claude", path=environment.get("PATH", ""))
    if not candidate or not Path(candidate).is_file():
        raise _failure("cli-missing", 503)
    try:
        executable = concrete_executable(Path(candidate).absolute())
    except (OSError, ValueError):
        raise _failure("cli-missing", 503) from None
    account = _account(source_home or Path.home(), environment)
    deadline = time.monotonic() + timeout
    stop = threading.Event()
    callback = None
    child = None
    windows_tree = None
    threads: list[threading.Thread] = []
    events: queue.Queue[Any] = queue.Queue(maxsize=64)

    def check() -> None:
        if cancel.is_set():
            raise _failure("cancelled", 499)
        if time.monotonic() >= deadline:
            raise _failure("timeout", 504)
        account.check()

    def enqueue(value: Any) -> None:
        while not stop.is_set():
            try:
                events.put(value, timeout=0.05)
                return
            except queue.Full:
                pass

    def read_output() -> None:
        assert child is not None and child.stdout is not None
        total = 0
        try:
            while not stop.is_set():
                line = child.stdout.readline(_MAX_BODY + 1)
                if not line:
                    enqueue(None)
                    return
                total += len(line)
                if len(line) > _MAX_BODY or total > _MAX_OUTPUT:
                    raise ValueError()
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError()
                enqueue(value)
        except (ValueError, OSError):
            enqueue(_failure("backend-contract"))

    def discard_stderr() -> None:
        assert child is not None and child.stderr is not None
        while not stop.is_set() and child.stderr.read(4096):
            pass

    def write_input() -> None:
        assert child is not None and child.stdin is not None
        try:
            child.stdin.write(input_line)
            child.stdin.flush()
        except OSError:
            enqueue(_failure("backend-failed"))

    def dispose() -> None:
        nonlocal child, callback, windows_tree
        stop.set()
        if windows_tree is not None:
            windows_tree.close()
            windows_tree = None
        if child is not None:
            _kill(child)
            for pipe in (child.stdin, child.stdout, child.stderr):
                if pipe is not None:
                    pipe.close()
        if callback is not None:
            callback.close()
            callback = None
        for thread in threads:
            thread.join(timeout=2)
        child = None

    with tempfile.TemporaryDirectory(prefix="codexhub-claude-request-") as directory:
        root = Path(directory)
        root.chmod(0o700)
        config = root / ".claude"
        config.mkdir(mode=0o700)
        _private_write(config / ".credentials.json", {"claudeAiOauth": account.oauth})
        env = _environment(environment, root, executable)
        try:
            check()
            status = status_runner(executable, ["auth", "status", "--json"], env=env, cwd=root,
                                   timeout=min(1, max(0.1, deadline - time.monotonic())))
            try:
                auth = json.loads(status.stdout)
            except (ValueError, TypeError):
                raise _failure("auth-required", 401) from None
            if (status.returncode or not isinstance(auth, dict) or auth.get("loggedIn") is not True
                    or auth.get("apiProvider") != "firstParty" or auth.get("authMethod") not in {"claude.ai", "oauth_token"}):
                raise _failure("auth-required", 401)
            check()
            callback = _Callback(tools, stop)
            manifest = root / "tools.json"
            _private_write(manifest, tools)
            mcp = {"mcpServers": {"codexhub": {"command": sys.executable,
                   "args": [str(Path(__file__).with_name("claude_subscription_mcp.py")), callback.url, str(manifest)]}}}
            mcp_path = root / "mcp.json"
            settings_path = root / "settings.json"
            _private_write(mcp_path, mcp)
            _private_write(settings_path, {"disableAllHooks": True, "enabledPlugins": {}})
            args = ["-p", "--output-format", "stream-json", "--input-format", "stream-json",
                    "--include-partial-messages", "--verbose", "--model", model, "--tools", "",
                    "--strict-mcp-config", "--mcp-config", str(mcp_path), "--setting-sources", "",
                    "--settings", str(settings_path), "--disable-slash-commands",
                    "--no-session-persistence", "--restricted", "--permission-prompts", "none"]
            if tools:
                args.extend(["--allowedTools", *[_MCP_PREFIX + tool["name"] for tool in tools]])
            effort = payload.get("reasoning_effort")
            if effort is not None:
                if effort not in {"low", "medium", "high", "xhigh", "max"}:
                    raise _failure("invalid-request", 400)
                args.extend(["--effort", effort])
            child = process_factory(cli_command(executable, args), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, env=env, cwd=root, start_new_session=os.name != "nt",
                                    creationflags=(subprocess.CREATE_NO_WINDOW | 4) if os.name == "nt" else 0)
            if os.name == "nt":
                windows_tree = _WindowsTree(child)
            for target in (read_output, discard_stderr, write_input):
                thread = threading.Thread(target=target, daemon=True)
                threads.append(thread)
                thread.start()
            stream_id = "chatcmpl-" + secrets.token_hex(12)
            created = int(time.time())

            def chunk(delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
                nonlocal role_pending
                if role_pending and delta:
                    delta = {"role": "assistant", **delta}
                    role_pending = False
                return {"id": stream_id, "object": "chat.completion.chunk", "created": created, "model": model,
                        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

            calls: dict[str, dict[str, Any]] = {}
            blocks: dict[int, dict[str, Any]] = {}
            pending_deltas: list[dict[str, Any]] = []
            buffering = False
            buffered_bytes = 0
            stopped_reason = None
            started = False
            role_pending = True
            usage: dict[str, Any] = {}

            def deliver(delta: dict[str, Any]) -> Iterator[dict[str, Any]]:
                nonlocal buffered_bytes
                if buffering:
                    buffered_bytes += len(json.dumps(delta, ensure_ascii=False).encode())
                    if buffered_bytes > _MAX_BODY:
                        raise _failure("backend-contract")
                    pending_deltas.append(delta)
                else:
                    yield chunk(delta)

            while True:
                check()
                try:
                    event = events.get(timeout=0.05)
                except queue.Empty:
                    continue
                if isinstance(event, BackendError):
                    raise event
                if event is None:
                    raise _failure("backend-failed")
                if event.get("type") == "result":
                    if event.get("is_error"):
                        raise _cli_error(event)
                    if stopped_reason is None:
                        raise _failure("backend-contract")
                    continue
                if event.get("type") == "system" and event.get("subtype") == "init":
                    names = event.get("tools", [])
                    if not isinstance(names, list) or any(name not in {_MCP_PREFIX + row["name"] for row in tools} for name in names):
                        raise _failure("backend-contract")
                    continue
                if event.get("type") != "stream_event":
                    continue
                e = event.get("event")
                if not isinstance(e, dict):
                    raise _failure("backend-contract")
                kind = e.get("type")
                if kind == "message_start":
                    if started:
                        raise _failure("backend-contract")
                    reported = e.get("message", {}).get("model")
                    if reported is not None and reported != model:
                        raise _failure("backend-contract")
                    initial_usage = e.get("message", {}).get("usage")
                    if isinstance(initial_usage, dict):
                        usage.update(initial_usage)
                    started = True
                elif kind == "content_block_start":
                    index, block = e.get("index"), e.get("content_block")
                    if not isinstance(index, int) or not isinstance(block, dict) or index in blocks:
                        raise _failure("backend-contract")
                    if block.get("type") == "tool_use":
                        name, call_id = block.get("name"), block.get("id")
                        if (not isinstance(name, str) or not name.startswith(_MCP_PREFIX) or name[len(_MCP_PREFIX):] not in {row["name"] for row in tools}
                                or not isinstance(call_id, str) or not call_id or call_id in calls or call_id in completed_call_ids
                                or any(old.get("id") == call_id for old in blocks.values())):
                            raise _failure("backend-contract")
                        buffering = True
                        pending_deltas.append({"tool_call_id": call_id})
                        blocks[index] = {"type": "tool_use", "id": call_id, "name": name[len(_MCP_PREFIX):],
                                         "raw": "", "initial": block.get("input", {})}
                    elif block.get("type") in {"text", "thinking", "redacted_thinking"}:
                        blocks[index] = {"type": block["type"]}
                        if block.get("type") == "text" and block.get("text"):
                            yield from deliver({"content": block["text"]})
                    else:
                        raise _failure("backend-contract")
                elif kind == "content_block_delta":
                    block, delta = blocks.get(e.get("index")), e.get("delta")
                    if block is None or not isinstance(delta, dict):
                        raise _failure("backend-contract")
                    if delta.get("type") == "text_delta" and block["type"] == "text" and isinstance(delta.get("text"), str):
                        yield from deliver({"content": delta["text"]})
                    elif delta.get("type") == "input_json_delta" and block["type"] == "tool_use" and isinstance(delta.get("partial_json"), str):
                        block["raw"] += delta["partial_json"]
                        if len(block["raw"]) > _MAX_BODY:
                            raise _failure("backend-contract")
                    elif delta.get("type") == "thinking_delta" and block["type"] == "thinking" and isinstance(delta.get("thinking"), str):
                        yield from deliver({"reasoning_content": delta["thinking"]})
                    elif delta.get("type") != "signature_delta" or block["type"] != "thinking":
                        raise _failure("backend-contract")
                elif kind == "content_block_stop":
                    block = blocks.get(e.get("index"))
                    if block is None or block.get("closed"):
                        raise _failure("backend-contract")
                    block["closed"] = True
                    if block["type"] == "tool_use":
                        try:
                            arguments = json.loads(block["raw"]) if block["raw"] else block["initial"]
                        except ValueError:
                            raise _failure("backend-contract") from None
                        if not isinstance(arguments, dict):
                            raise _failure("backend-contract")
                        calls[block["id"]] = {"id": block["id"], "name": block["name"], "arguments": arguments}
                elif kind == "message_delta":
                    delta = e.get("delta")
                    if not isinstance(delta, dict) or delta.get("stop_reason") not in {"end_turn", "tool_use", "max_tokens", "stop_sequence", None}:
                        raise _failure("backend-contract")
                    stopped_reason = delta.get("stop_reason") or stopped_reason
                    if isinstance(e.get("usage"), dict):
                        usage.update(e["usage"])
                elif kind == "message_stop":
                    if not started or stopped_reason is None or any(not block.get("closed") for block in blocks.values()):
                        raise _failure("backend-contract")
                    if bool(calls) != (stopped_reason == "tool_use"):
                        raise _failure("backend-contract")
                    if role_pending and not calls:
                        raise _failure("backend-contract")
                    choice = payload.get("tool_choice", "auto")
                    if not calls and (choice == "required" or isinstance(choice, dict)):
                        raise _failure("backend-contract")
                    callback.check(calls)
                    check()
                    # Calls must agree with the private MCP callback before any
                    # caller can execute them. Preserve the native order of those
                    # calls and later text, while earlier text streams directly.
                    call_index = 0
                    for delta in pending_deltas:
                        if "tool_call_id" in delta:
                            call = calls[delta["tool_call_id"]]
                            delta = {"tool_calls": [{"index": call_index, "id": call["id"], "type": "function",
                                     "function": {"name": call["name"], "arguments": json.dumps(call["arguments"], ensure_ascii=False, separators=(",", ":"))}}]}
                            call_index += 1
                        yield chunk(delta)
                    final = chunk({}, "tool_calls" if calls else "length" if stopped_reason == "max_tokens" else "stop")
                    reported_usage = _reported_usage(usage)
                    if reported_usage is not None:
                        final["usage"] = reported_usage
                    # A finish event is reviewable only after the backend and
                    # its blocked MCP helpers are gone. The caller may start
                    # its next request as soon as this chunk is observed.
                    dispose()
                    shutil.rmtree(root)
                    yield final
                    return
                elif kind not in {"ping"}:
                    raise _failure("backend-contract")
        except BackendError:
            raise
        except Exception:
            raise _failure("backend-failed") from None
        finally:
            dispose()
