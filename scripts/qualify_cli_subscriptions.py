"""Bounded real-Gateway qualification for current official CLI subscriptions.

Each run uses private CODEX_HOME, a random loopback port/key and fresh HTTP
callers. Source HOME/XDG credentials stay read-only in the official account;
private Gateway logs/config/fixtures are destroyed. Evidence contains hashes,
status and lifecycle facts, never prompts, tool results or credentials.
"""
from __future__ import annotations

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from python_runtime_contract import require_python_313
require_python_313(__file__)

import argparse
import ctypes
import errno
from dataclasses import dataclass, field
import hashlib
import http.client
import json
import os
import re
import secrets
import signal
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from typing import Any, Iterator, Mapping

PROTOCOLS = ("chat", "responses", "messages")
ENDPOINTS = {"chat": "/v1/chat/completions", "responses": "/v1/responses", "messages": "/v1/messages"}
_MAX_RESPONSE = 16 * 1024 * 1024
_ERROR_CODE = re.compile(r"[a-z][a-z0-9_-]{0,80}\Z")
_BACKEND_CODES = ("unsupported-parameter", "unsupported-tool-choice", "invalid-request", "not-eligible",
                  "auth-required", "auth-expired", "account-changed", "usage-limit", "backend-contract", "backend-failed",
                  "unsupported_protocol_fields", "unsupported_protocol_semantics", "unpaired_tool_call")


class QualificationFailure(RuntimeError):
    def __init__(self, code: str, status: int | None = None, evidence: dict[str, Any] | None = None):
        self.code = code if _ERROR_CODE.fullmatch(code) else "upstream-error"
        self.status = status
        self.evidence = evidence or {}
        super().__init__(self.code)


def fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def installed_cli_version(provider: str) -> str | None:
    """Observe the installed official package identity without a CLI request."""
    name = "cursor-agent" if provider == "cursor-subscription" else "claude"
    found = shutil.which(name)
    if not found:
        return None
    pattern = (r"\d{4}\.\d{2}\.\d{2}-[0-9a-f]+" if provider == "cursor-subscription"
               else r"\d+\.\d+\.\d+(?:-[A-Za-z0-9.]+)?")
    return next((part for part in Path(found).resolve().parts if re.fullmatch(pattern, part)), None)


def request_for(protocol: str, model: str, history: list[dict[str, Any]], *,
                tools: list[dict[str, Any]] | None = None, stream: bool = False) -> dict[str, Any]:
    """Build native downstream requests; required Messages max_tokens survives."""
    if protocol not in PROTOCOLS:
        raise ValueError("unknown protocol")
    result = {"model": model, "stream": stream}
    result["input" if protocol == "responses" else "messages"] = history
    if protocol == "messages":
        result["max_tokens"] = 1024
    if tools:
        if protocol == "chat":
            result["tools"] = [{"type": "function", "function": tool} for tool in tools]
        elif protocol == "responses":
            result["tools"] = [{"type": "function", **tool} for tool in tools]
        else:
            result["tools"] = [{"name": tool["name"], "description": tool.get("description", ""),
                                "input_schema": tool["parameters"]} for tool in tools]
    return result


@dataclass(frozen=True)
class CallerTool:
    id: str
    name: str
    arguments: dict[str, Any]
    item_id: str | None = None


@dataclass
class Reply:
    text: str
    calls: list[CallerTool]
    history_items: list[dict[str, Any]]
    usage_present: bool
    finish: str | None
    stream_events: int = 0

    def evidence(self) -> dict[str, Any]:
        return {"text_sha256": fingerprint(self.text), "text_bytes": len(self.text.encode()),
                "tool_calls": len(self.calls), "call_id_sha256": [fingerprint(call.id) for call in self.calls],
                "usage_present": self.usage_present, "stream_events": self.stream_events,
                "finish": self.finish if self.finish in {"stop", "length", "tool_calls", "tool_use", "end_turn", "completed", "max_tokens"} else None}


def _error(value: Mapping[str, Any], status: int | None = None) -> QualificationFailure:
    error = value.get("error")
    if not isinstance(error, dict):
        response = value.get("response")
        error = response.get("error") if isinstance(response, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    if not isinstance(code, str) or not _ERROR_CODE.fullmatch(code):
        # Existing Gateway error envelopes may name HTTPError while carrying
        # its original bounded reason in detail/message. Record only a known
        # backend enum, never the free-text wrapper or raw provider response.
        detail = " ".join(str(part) for part in (value.get("detail", ""), value.get("error", ""),
                                                  value.get("codexhub_error", "")))
        code = next((known for known in _BACKEND_CODES if known in detail), "upstream-error")
    return QualificationFailure(code, status)


def decode_reply(protocol: str, value: Mapping[str, Any]) -> Reply:
    """Preserve native history Items and original Calls without inventing IDs."""
    if value.get("error"):
        raise _error(value)
    text = ""
    calls = []
    if protocol == "chat":
        choices = value.get("choices")
        if not isinstance(choices, list) or len(choices) != 1:
            raise QualificationFailure("invalid-response")
        message = choices[0].get("message")
        if not isinstance(message, dict):
            raise QualificationFailure("invalid-response")
        content = message.get("content")
        if isinstance(content, str):
            text = content
        elif content is not None:
            raise QualificationFailure("unsupported-response-content")
        for call in message.get("tool_calls", []):
            function = call["function"]
            arguments = json.loads(function["arguments"])
            calls.append(CallerTool(call["id"], function["name"], arguments))
        history_items = [message]
        finish = choices[0].get("finish_reason")
    elif protocol == "responses":
        if value.get("status") in {"failed", "incomplete"}:
            raise _error(value)
        history_items = value.get("output")
        if not isinstance(history_items, list):
            raise QualificationFailure("invalid-response")
        for item in history_items:
            if item.get("type") == "function_call":
                calls.append(CallerTool(item["call_id"], item["name"], json.loads(item["arguments"]), item.get("id")))
            elif item.get("type") == "message":
                for block in item.get("content", []):
                    if block.get("type") == "output_text":
                        text += block["text"]
                    elif block.get("type") not in {"refusal"}:
                        raise QualificationFailure("unsupported-response-content")
        finish = value.get("status")
    elif protocol == "messages":
        blocks = value.get("content")
        if not isinstance(blocks, list):
            raise QualificationFailure("invalid-response")
        for block in blocks:
            if block.get("type") == "text":
                text += block["text"]
            elif block.get("type") == "tool_use":
                calls.append(CallerTool(block["id"], block["name"], block["input"]))
            elif block.get("type") not in {"thinking", "redacted_thinking"}:
                raise QualificationFailure("unsupported-response-content")
        history_items = [{"role": "assistant", "content": blocks}]
        finish = value.get("stop_reason")
    else:
        raise ValueError("unknown protocol")
    if len({call.id for call in calls}) != len(calls) or any(not call.id or not isinstance(call.arguments, dict) for call in calls):
        raise QualificationFailure("invalid-call-identity")
    return Reply(text, calls, history_items, isinstance(value.get("usage"), dict), finish)


def tool_result_item(protocol: str, call: CallerTool, result: str) -> dict[str, Any]:
    if protocol == "chat":
        return {"role": "tool", "tool_call_id": call.id, "content": result}
    if protocol == "responses":
        return {"type": "function_call_output", "call_id": call.id, "output": result}
    if protocol == "messages":
        return {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call.id, "content": result}]}
    raise ValueError("unknown protocol")


def sse_events(response: Any) -> Iterator[dict[str, Any] | str]:
    """Read bounded SSE without requiring one physical line per event."""
    data = []
    total = 0
    while True:
        raw = response.readline(_MAX_RESPONSE + 1)
        if not raw:
            if data:
                raise QualificationFailure("incomplete-sse-frame")
            return
        total += len(raw)
        if len(raw) > _MAX_RESPONSE or total > _MAX_RESPONSE:
            raise QualificationFailure("response-too-large")
        line = raw.decode("utf-8").rstrip("\r\n")
        if not line:
            if data:
                joined = "\n".join(data)
                data = []
                if joined == "[DONE]":
                    yield joined
                else:
                    value = json.loads(joined)
                    if not isinstance(value, dict):
                        raise QualificationFailure("invalid-sse-event")
                    if value.get("error") or value.get("type") in {"error", "response.failed"}:
                        raise _error(value)
                    yield value
        elif line.startswith("data:"):
            data.append(line[5:].lstrip(" "))


def decode_stream(protocol: str, events: Iterator[dict[str, Any] | str]) -> Reply:
    """Collect actual terminal events; an HTTP 200 alone cannot pass."""
    text = ""
    count = 0
    usage = False
    terminal = False
    finish = None
    for value in events:
        count += 1
        if value == "[DONE]":
            if protocol == "chat":
                terminal = True
            continue
        assert isinstance(value, dict)
        usage |= isinstance(value.get("usage"), dict)
        if protocol == "chat":
            for choice in value.get("choices", []):
                delta = choice.get("delta", {})
                text += delta.get("content", "") or ""
                if delta.get("tool_calls"):
                    raise QualificationFailure("unexpected-stream-tool")
                finish = choice.get("finish_reason") or finish
        elif protocol == "responses":
            if value.get("type") == "response.output_text.delta":
                text += value.get("delta", "")
            elif value.get("type") == "response.completed":
                response = value.get("response", {})
                usage |= isinstance(response.get("usage"), dict)
                terminal = response.get("status") == "completed"
                finish = "completed"
        else:
            if value.get("type") == "content_block_delta" and value.get("delta", {}).get("type") == "text_delta":
                text += value["delta"]["text"]
            elif value.get("type") == "message_delta":
                finish = value.get("delta", {}).get("stop_reason") or finish
            elif value.get("type") == "message_stop":
                terminal = True
            elif value.get("type") == "message_start":
                usage |= isinstance(value.get("message", {}).get("usage"), dict)
    if not terminal or not finish:
        raise QualificationFailure("incomplete-stream")
    return Reply(text, [], [], usage, finish, count)


def _socket_inodes(pid: int) -> list[str] | None:
    """One inode per surviving socket descriptor, including duplicate FDs."""
    directory = Path(f"/proc/{pid}/fd")
    try:
        if not directory.exists():
            return None
        inodes = []
        for path in directory.iterdir():
            try:
                target = os.readlink(path)
            except OSError as error:
                if error.errno != errno.ENOENT or not directory.exists():
                    raise
                continue
            if target.startswith("socket:["):
                inodes.append(target[8:-1])
        return inodes if directory.exists() else None
    except OSError:
        return None


def _socket_count(pid: int) -> int | None:
    inodes = _socket_inodes(pid)
    return len(inodes) if inodes is not None else None


def _upstream_tls_count(pid: int) -> int | None:
    """Observe this private Gateway's established vendor TLS sockets on Linux."""
    descriptors = _socket_inodes(pid)
    if descriptors is None:
        return None
    inodes = set(descriptors)
    try:
        count = 0
        for table in ("tcp", "tcp6"):
            for line in Path("/proc/net", table).read_text().splitlines()[1:]:
                fields = line.split()
                if fields[3] == "01" and fields[2].rsplit(":", 1)[1] == "01BB" and fields[9] in inodes:
                    count += 1
        return count
    except OSError:
        return None


class _Tcp4Row(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint32) for name in
                ("state", "local_addr", "local_port", "remote_addr", "remote_port", "pid")]


class _Tcp6Row(ctypes.Structure):
    _fields_ = [("local_addr", ctypes.c_ubyte * 16), ("local_scope", ctypes.c_uint32),
                ("local_port", ctypes.c_uint32), ("remote_addr", ctypes.c_ubyte * 16),
                ("remote_scope", ctypes.c_uint32), ("remote_port", ctypes.c_uint32),
                ("state", ctypes.c_uint32), ("pid", ctypes.c_uint32)]


class WindowsOwnedTcp:
    """TCP-only counts bound to an actual Popen's retained Windows handle.

    Borrow the Popen handle; only Popen owns/closes it. Never open an arbitrary
    PID. Both family tables are transient: persist no rows or endpoint addresses.
    remote443 is a proxy metric, not TLS/request/vendor/billing proof.
    """

    def __init__(self, process: subprocess.Popen):
        self.process = process
        self.creation = None
        try:
            if not isinstance(process, subprocess.Popen):
                raise ValueError("not an owned Popen")
            self.pid, self.handle = process.pid, int(process._handle)
            self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            self.table = ctypes.WinDLL("iphlpapi", use_last_error=True).GetExtendedTcpTable
            self.kernel.GetProcessId.argtypes = [ctypes.c_void_p]
            self.kernel.GetProcessId.restype = ctypes.c_uint32
            self.kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            self.kernel.WaitForSingleObject.restype = ctypes.c_uint32
            self.kernel.GetProcessTimes.argtypes = [ctypes.c_void_p] + [ctypes.c_void_p] * 4
            self.kernel.GetProcessTimes.restype = ctypes.c_int
            self.table.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32), ctypes.c_int,
                                   ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
            self.table.restype = ctypes.c_uint32
            self.creation = self._identity()
        except Exception:
            # Observation failure must never interrupt the caller-owned cleanup.
            self.creation = None

    def _identity(self) -> int:
        if (self.process.pid != self.pid or int(self.process._handle) != self.handle
                or self.process.poll() is not None
                or self.kernel.GetProcessId(self.handle) != self.pid
                or self.kernel.WaitForSingleObject(self.handle, 0) != 258):
            raise ValueError("owned process unavailable")
        times = [(ctypes.c_uint32 * 2)() for _ in range(4)]
        if not self.kernel.GetProcessTimes(self.handle, *(ctypes.byref(value) for value in times)):
            raise OSError("process creation unavailable")
        creation = times[0][0] | (times[0][1] << 32)
        if not creation:
            raise ValueError("process creation unavailable")
        return creation

    def _family_counts(self, family: int, row: type[ctypes.Structure]) -> tuple[int, int]:
        class Table(ctypes.Structure):
            _fields_ = [("count", ctypes.c_uint32), ("rows", row * 1)]

        size = ctypes.c_uint32()
        if self.table(None, ctypes.byref(size), False, family, 5, 0) != 122:
            raise OSError("TCP sizing unavailable")
        for _ in range(3):
            if not Table.rows.offset <= size.value <= 1024 * 1024:
                raise ValueError("TCP allocation bound")
            capacity = size.value
            buffer = ctypes.create_string_buffer(capacity)
            code = self.table(buffer, ctypes.byref(size), False, family, 5, 0)
            if code == 122:
                continue
            if code != 0 or not Table.rows.offset <= size.value <= capacity:
                raise OSError("TCP fetch unavailable")
            count = ctypes.c_uint32.from_buffer(buffer).value
            stride, offset = ctypes.sizeof(row), Table.rows.offset
            if count > (size.value - offset) // stride:
                raise ValueError("TCP table truncated")
            owned = proxy = 0
            for index in range(count):
                item = row.from_buffer(buffer, offset + index * stride)
                if item.pid == self.pid:
                    owned += 1
                    proxy += item.state == 5 and socket.ntohs(item.remote_port & 0xffff) == 443
            return owned, proxy
        raise OSError("TCP buffer growth bound")

    def observe(self) -> tuple[int | None, int | None]:
        """Return available owned TCP rows/proxy rows, or two nulls on failure."""
        try:
            if self.creation is None or self._identity() != self.creation:
                return None, None
            ipv4 = self._family_counts(2, _Tcp4Row)
            ipv6 = self._family_counts(23, _Tcp6Row)
            if self._identity() != self.creation:
                return None, None
            return ipv4[0] + ipv6[0], ipv4[1] + ipv6[1]
        except Exception:
            return None, None


def freeze_candidate(repo: Path, destination: Path) -> str:
    """Copy a bounded runtime snapshot so a restart cannot load worker edits."""
    digest = hashlib.sha256()
    for name in ("src-python", "config", "model-catalogs"):
        source = repo / name
        if not source.is_dir():
            if name == "model-catalogs":
                (destination / name).mkdir(parents=True)
                continue
            raise QualificationFailure("candidate-runtime-missing")
        shutil.copytree(source, destination / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for path in sorted((destination / name).rglob("*")):
            if path.is_file():
                digest.update(str(path.relative_to(destination)).encode() + b"\0")
                digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


class PrivateGateway:
    """Launch the selected checkout's actual Gateway, never a relay prototype."""

    def __init__(self, repo: Path, root: Path, provider: str, model: str, timeout: float):
        self.repo, self.root, self.timeout = repo, root, timeout
        self.key = secrets.token_hex(24)
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0))
            self.port = reserved.getsockname()[1]
        self.env = {key: value for key, value in os.environ.items() if key in {
            "PATH", "HOME", "USERPROFILE", "XDG_CONFIG_HOME", "APPDATA", "CLAUDE_CONFIG_DIR",
            "SYSTEMROOT", "SystemRoot", "WINDIR", "COMSPEC", "PATHEXT", "CODEXHUB_PYTHON"}}
        self.env.update(CODEX_HOME=str(root / "codex"), CODEX_PROXY_GATEWAY_CLIENT_KEY=self.key,
                        TMPDIR=str(root), TMP=str(root), TEMP=str(root),
                        CODEX_PROXY_REQUEST_TIMEOUT_SECONDS=str(timeout),
                        CODEX_PROXY_GATEWAY_AUTO_RETRY_ENABLED="0", PYTHONPATH=str(repo / "src-python"))
        self.process = None
        self.windows_tcp = None
        self.log = None
        setup = '''import json, os, sys
from pathlib import Path
from providers_config import ModelConfig, ProviderConfig, save_providers, build_external_model_index
from catalog_sync import build_external_provider_model
from catalog import CatalogPolicy
from subscription_exchange import SYSTEM_CONTEXT_CONSENT
root = Path(os.environ['CODEX_HOME'])
provider, model, timeout = sys.argv[1:]
p = ProviderConfig(provider, provider, '', '', upstream_format='chat_completions', tool_protocol='chat_tools',
                   system_context_consent=SYSTEM_CONTEXT_CONSENT if provider == 'claude-subscription' else None,
                   models=[ModelConfig(model, upstream_model=model, display_name=model, multi_agent_version='v2')])
path = root / 'proxy/config/providers.toml'
path.parent.mkdir(parents=True, exist_ok=True)
save_providers([p], path)
path.chmod(0o600)
settings = root / 'proxy/settings.json'
settings.write_text(json.dumps({'gateway_auto_retry_enabled': False, 'gateway_auto_retry_max_attempts': 1,
 'gateway_request_timeout_seconds': float(timeout), 'gateway_image_proxy_enabled': False}))
settings.chmod(0o600)
entry = build_external_model_index([p])[provider + '/' + model]
row = build_external_provider_model(entry, CatalogPolicy(set(), set(), {}), {})
path = root / 'model-catalogs/codexhub-model-catalog.json'
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps({'models':[row]}))
path.chmod(0o600)
'''
        result = subprocess.run([sys.executable, "-c", setup, provider, model, str(timeout)], env=self.env,
                                cwd=repo, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
        if result.returncode:
            raise QualificationFailure("private-setup-failed")

    def start(self) -> None:
        self.log = (self.root / "gateway-private.log").open("ab")
        spawn_options = {"env": self.env}
        if os.name == "nt":
            # Bypass the venv redirector so the retained Popen handle owns the
            # socket interpreter, while getpath still selects the same venv.
            spawn_options = {"executable": sys._base_executable,
                             "env": {**self.env, "__PYVENV_LAUNCHER__": sys.executable}}
        self.process = subprocess.Popen([sys.executable, str(self.repo / "src-python/codex_proxy.py"),
                                         "--host", "127.0.0.1", "--port", str(self.port)],
                                        cwd=self.root, stdout=self.log, stderr=self.log,
                                        start_new_session=os.name != "nt", **spawn_options)
        self.windows_tcp = WindowsOwnedTcp(self.process) if os.name == "nt" else None
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise QualificationFailure("gateway-start-failed")
            try:
                connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=.3)
                connection.request("GET", "/health")
                response = connection.getresponse()
                value = json.loads(response.read())
                connection.close()
                if response.status == 200 and value.get("ok"):
                    return
            except (OSError, ValueError):
                pass
            time.sleep(.05)
        raise QualificationFailure("gateway-start-timeout")

    def stop(self) -> None:
        if self.process is not None:
            if os.name != "nt":
                try:
                    os.killpg(self.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            elif self.process.poll() is None:
                subprocess.run(["taskkill", "/PID", str(self.process.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=False)
            self.process.wait(timeout=5)
            self.windows_tcp = None
            self.process = None
        if self.log is not None:
            self.log.close()
            self.log = None

    def restart(self) -> None:
        self.stop()
        self.start()

    def open(self, protocol: str, payload: dict[str, Any]) -> tuple[http.client.HTTPConnection, Any]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=self.timeout)
        headers = {"Content-Type": "application/json", "Authorization": "Bearer " + self.key,
                   "Connection": "close", "anthropic-version": "2023-06-01"}
        connection.request("POST", ENDPOINTS[protocol], body=json.dumps(payload).encode(), headers=headers)
        response = connection.getresponse()
        if response.status != 200:
            body = response.read(_MAX_RESPONSE + 1)
            connection.close()
            try:
                value = json.loads(body)
            except ValueError:
                value = {}
            raise _error(value, response.status)
        return connection, response

    def exchange(self, protocol: str, payload: dict[str, Any]) -> Reply:
        connection, response = self.open(protocol, payload)
        try:
            if payload.get("stream"):
                return decode_stream(protocol, sse_events(response))
            body = response.read(_MAX_RESPONSE + 1)
            if len(body) > _MAX_RESPONSE:
                raise QualificationFailure("response-too-large")
            return decode_reply(protocol, json.loads(body))
        finally:
            response.close()
            connection.close()

    def cancel_stream(self, model: str) -> dict[str, Any]:
        assert self.process is not None
        baseline = _socket_count(self.process.pid)
        upstream_baseline = _upstream_tls_count(self.process.pid)
        payload = request_for("chat", model, [{"role": "user", "content":
            "Write an extremely long numbered list of unique sentences. Continue for at least 10000 lines."}], stream=True)
        # Cancel while the HTTP caller is waiting, with a real vendor TLS
        # socket open. Waiting for text can let a short reply naturally finish
        # and would not prove upstream cancellation.
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=self.timeout)
        connection.request("POST", ENDPOINTS["chat"], body=json.dumps(payload).encode(), headers={
            "Content-Type": "application/json", "Authorization": "Bearer " + self.key, "Connection": "close"})
        finished = threading.Event()
        headers_received = threading.Event()
        caller_error = []
        def waiting_caller() -> None:
            try:
                response = connection.getresponse()
                headers_received.set()
                response.read(_MAX_RESPONSE + 1)
                response.close()
            except Exception as error:
                caller_error.append(type(error).__name__)
            finally:
                finished.set()
        thread = threading.Thread(target=waiting_caller, daemon=True)
        thread.start()
        upstream_at_disconnect = _upstream_tls_count(self.process.pid)
        wait_deadline = time.monotonic() + min(15, self.timeout)
        try:
            while (upstream_baseline is not None and upstream_at_disconnect is not None
                   and upstream_at_disconnect <= upstream_baseline and not finished.is_set()
                   and time.monotonic() < wait_deadline):
                time.sleep(.01)
                upstream_at_disconnect = _upstream_tls_count(self.process.pid)
        finally:
            if connection.sock is not None:
                try:
                    connection.sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
            connection.close()
        thread.join(timeout=1)
        deadline = time.monotonic() + 3
        count = _socket_count(self.process.pid)
        upstream_after = _upstream_tls_count(self.process.pid)
        while (upstream_baseline is not None and upstream_after is not None
               and upstream_after > upstream_baseline and time.monotonic() < deadline):
            time.sleep(.05)
            count = _socket_count(self.process.pid)
            upstream_after = _upstream_tls_count(self.process.pid)
        return {"cancel_phase": "upstream-wait", "headers_received_before_cancel": headers_received.is_set(),
                "caller_wait_ended": finished.is_set(), "caller_failure_observed": bool(caller_error),
                "socket_baseline": baseline, "socket_after_cancel": count,
                "upstream_tls_baseline": upstream_baseline, "upstream_tls_at_disconnect": upstream_at_disconnect,
                "upstream_tls_after_cancel": upstream_after,
                "upstream_socket_cleanup_observed": (upstream_baseline is not None and upstream_at_disconnect is not None
                    and upstream_at_disconnect > upstream_baseline and upstream_after == upstream_baseline),
                "billing_cessation_claimed": False}

    def cancel_stream_after_first_text(self, model: str) -> dict[str, Any]:
        """Disconnect an active text stream, observing only this Gateway's sockets."""
        assert self.process is not None
        start = time.monotonic()
        windows = self.windows_tcp is not None
        baseline, upstream_baseline = (self.windows_tcp.observe() if windows else
                                      (_socket_count(self.process.pid), _upstream_tls_count(self.process.pid)))
        payload = request_for("chat", model, [{"role": "user", "content":
            "Write an extremely long numbered list of unique sentences. Continue for at least 10000 lines."}], stream=True)
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=self.timeout)
        finished = threading.Event()
        lock = threading.Lock()
        evidence = {"cancel_phase": "after-first-nonempty-text", "protocol": "chat", "gateway_model": model,
                    "first_nonempty_text_observed": False, "first_text_sha256": None, "first_text_bytes": 0,
                    "first_text_elapsed_seconds": None, "terminal_observed_before_cancel": False,
                    "headers_received_before_cancel": False, "billing_cessation_claimed": False,
                    "wait_bound_seconds": self.timeout, "caller_join_bound_seconds": 1, "cleanup_bound_seconds": 3}
        errors = []
        first_text_at = None
        first_text_within_bound = False

        def reading_caller() -> None:
            nonlocal first_text_at
            response = None
            try:
                response = connection.getresponse()
                with lock:
                    evidence["headers_received_before_cancel"] = True
                if response.status != 200:
                    body = response.read(_MAX_RESPONSE + 1)
                    try:
                        value = json.loads(body)
                    except ValueError:
                        value = {}
                    raise _error(value, response.status)
                for value in sse_events(response):
                    with lock:
                        if value == "[DONE]":
                            evidence["terminal_observed_before_cancel"] = True
                            continue
                        for choice in value.get("choices", []):
                            # A finish in the same frame as text is already complete.
                            if choice.get("finish_reason") is not None:
                                evidence["terminal_observed_before_cancel"] = True
                            text = choice.get("delta", {}).get("content")
                            if isinstance(text, str) and text and not evidence["first_nonempty_text_observed"]:
                                first_text_at = time.monotonic()
                                evidence.update(first_nonempty_text_observed=True, first_text_sha256=fingerprint(text),
                                                first_text_bytes=len(text.encode()),
                                                first_text_elapsed_seconds=round(first_text_at - start, 3))
            except Exception as error:
                with lock:
                    errors.append(error if isinstance(error, QualificationFailure)
                                  else QualificationFailure("harness-or-transport-failure"))
            finally:
                if response is not None:
                    response.close()
                with lock:
                    finished.set()

        thread = None
        caller_socket = None
        active = False
        disconnected = False
        upstream_at_disconnect = None
        terminal_at_disconnect = False
        finished_at_disconnect = False
        error_at_disconnect = None
        try:
            connection.request("POST", ENDPOINTS["chat"], body=json.dumps(payload).encode(), headers={
                "Content-Type": "application/json", "Authorization": "Bearer " + self.key, "Connection": "close"})
            # HTTPConnection detaches its socket on Connection: close headers;
            # retain our owned socket so a blocked SSE read can still be interrupted.
            caller_socket = connection.sock
            thread = threading.Thread(target=reading_caller, daemon=True)
            thread.start()
            wait_deadline = start + self.timeout
            while time.monotonic() < wait_deadline:
                with lock:
                    if evidence["first_nonempty_text_observed"] or finished.is_set() or errors:
                        break
                time.sleep(.01)
            # Seal the wait result before socket observation can admit late text.
            with lock:
                first_text_within_bound = first_text_at is not None and first_text_at < wait_deadline
        finally:
            # Socket observation must not prevent the reader recording a
            # natural terminal while the owned snapshot is being collected.
            upstream_at_disconnect = (self.windows_tcp.observe()[1] if windows else
                                      _upstream_tls_count(self.process.pid))
            with lock:
                terminal_at_disconnect = evidence["terminal_observed_before_cancel"]
                finished_at_disconnect = finished.is_set()
                error_at_disconnect = errors[0] if errors else None
                active = (baseline is not None and upstream_baseline is not None and upstream_at_disconnect is not None
                          and upstream_at_disconnect > upstream_baseline and self.process.poll() is None
                          and not finished_at_disconnect and not terminal_at_disconnect and not errors
                          and evidence["first_nonempty_text_observed"])
                if caller_socket is not None:
                    try:
                        caller_socket.shutdown(socket.SHUT_RDWR)
                        disconnected = True
                    except OSError:
                        pass
                evidence["disconnect_elapsed_seconds"] = round(time.monotonic() - start, 3)
            connection.close()
            if thread is not None:
                thread.join(timeout=1)
        cleanup_start = time.monotonic()
        cleanup_deadline = cleanup_start + 3
        count, upstream_after = (self.windows_tcp.observe() if windows else
                                 (_socket_count(self.process.pid), _upstream_tls_count(self.process.pid)))
        while (baseline is not None and upstream_baseline is not None and count is not None and upstream_after is not None
               and (count > baseline or upstream_after > upstream_baseline) and time.monotonic() < cleanup_deadline):
            time.sleep(.05)
            count, upstream_after = (self.windows_tcp.observe() if windows else
                                     (_socket_count(self.process.pid), _upstream_tls_count(self.process.pid)))
        evidence.update(terminal_observed_before_cancel=terminal_at_disconnect,
                        caller_finished_before_cancel=finished_at_disconnect,
                        request_active_at_disconnect=active and disconnected, caller_disconnect_observed=disconnected,
                        caller_wait_ended=finished.is_set(), caller_failure_observed=bool(errors),
                        socket_baseline=None if windows else baseline, socket_after_cancel=None if windows else count,
                        upstream_tls_baseline=None if windows else upstream_baseline,
                        upstream_tls_at_disconnect=None if windows else upstream_at_disconnect,
                        upstream_tls_after_cancel=None if windows else upstream_after,
                        cleanup_elapsed_seconds=round(time.monotonic() - cleanup_start, 3),
                        upstream_socket_cleanup_observed=(active and disconnected and baseline is not None and count is not None and count <= baseline
                            and upstream_baseline is not None and upstream_after == upstream_baseline
                            and self.process.poll() is None
                            and (not windows or time.monotonic() < cleanup_deadline)))
        if windows:
            evidence.update(socket_observation_kind="windows-owned-tcp-endpoints",
                            owned_tcp_endpoint_baseline=baseline, owned_tcp_endpoint_after_cancel=count,
                            established_remote443_proxy_endpoint_baseline=upstream_baseline,
                            established_remote443_proxy_endpoint_at_disconnect=upstream_at_disconnect,
                            established_remote443_proxy_endpoint_after_cancel=upstream_after,
                            windows_tcp_endpoint_cleanup_observed=evidence["upstream_socket_cleanup_observed"])
        if error_at_disconnect is not None:
            raise QualificationFailure(error_at_disconnect.code, error_at_disconnect.status, evidence)
        if not evidence["first_nonempty_text_observed"]:
            raise QualificationFailure("cancel-first-text-unobserved", evidence=evidence)
        if terminal_at_disconnect:
            raise QualificationFailure("cancel-response-already-ended", evidence=evidence)
        if baseline is None or upstream_baseline is None or upstream_at_disconnect is None:
            raise QualificationFailure("cancel-ownership-unobserved", evidence=evidence)
        if not evidence["request_active_at_disconnect"]:
            raise QualificationFailure("cancel-request-inactive", evidence=evidence)
        if not finished.is_set() or not evidence["upstream_socket_cleanup_observed"]:
            raise QualificationFailure("cancel-cleanup-unobserved", evidence=evidence)
        if not first_text_within_bound:
            raise QualificationFailure("cancel-first-text-timeout", evidence=evidence)
        return evidence


def run_qualification(repo: Path, provider: str, model: str, *, timeout: float = 60,
                      total_timeout: float = 900, protocols: tuple[str, ...] = PROTOCOLS,
                      progress: Any = None, include_cancel: bool = True, include_text: bool = True,
                      cancel_after_first_text: bool = False) -> dict[str, Any]:
    """Execute bounded ordinary acceptance; advanced/Windows gates stay separate."""
    start = time.monotonic()
    deadline = start + total_timeout
    report = {"provider": provider, "model": model, "risk": "strict", "kind": "ordinary-real-gateway",
              "bound_seconds_per_exchange": timeout, "bound_seconds_total": total_timeout,
              "success_cues": ["exact randomized text with complete protocol terminal event", "actual caller tool with original Call identity",
                               "real tool result recovered from completed history after Gateway restart", "upstream sockets close after caller disconnect"],
              "failure_cues": ["bounded HTTP/SSE error", "wrong exact output", "orphan or malformed caller tool", "incomplete stream", "unobserved cleanup"],
              "protocols": list(protocols), "cases": [], "advanced_codemode_v2_qualified": False, "dual_platform_qualified": False,
              "windows_gate": {"state": "blocked", "reason": "yoga SSH No route to host; actual Windows verification pending"}}
    histories = {}
    records = report["cases"]
    qualified = True
    report["ordinary_scope"] = "text-stream-tool-continuation" if include_text else "tool-continuation-only"
    report["cli_version_observed_before_run"] = installed_cli_version(provider)

    def case(name: str, operation: Any) -> Any:
        nonlocal qualified
        before = time.monotonic()
        if before >= deadline:
            records.append({"case": name, "state": "not-run", "code": "total-bound-reached"})
            qualified = False
            return None
        try:
            if server is not None:
                server.timeout = min(timeout, max(.05, deadline - before))
            result = operation()
            evidence = result.evidence() if isinstance(result, Reply) else dict(result)
            records.append({"case": name, "state": "passed", "elapsed_seconds": round(time.monotonic() - before, 2), **evidence})
            if progress is not None:
                progress(records[-1])
            return result
        except QualificationFailure as error:
            qualified = False
            records.append({"case": name, "state": "failed", "code": error.code, "http_status": error.status,
                            "elapsed_seconds": round(time.monotonic() - before, 2), **error.evidence})
        except Exception:
            qualified = False
            records.append({"case": name, "state": "failed", "code": "harness-or-transport-failure",
                            "elapsed_seconds": round(time.monotonic() - before, 2)})
        if progress is not None:
            progress(records[-1])
        return None

    with tempfile.TemporaryDirectory(prefix="codexhub-production-qualification-") as directory:
        root = Path(directory)
        root.chmod(0o700)
        server = None
        try:
            frozen = root / "candidate"
            source_revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                                             capture_output=True, text=True, timeout=5)
            revision = source_revision.stdout.strip()
            report["candidate_sha"] = revision if source_revision.returncode == 0 and re.fullmatch(r"[a-f0-9]{40}", revision) else None
            changes = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all", "--", "src-python", "config", "model-catalogs"],
                                     cwd=repo, capture_output=True, text=True, timeout=5)
            report["candidate_runtime_tracked_dirty"] = changes.returncode != 0 or bool(changes.stdout.strip())
            report["candidate_sha_is_exact_runtime"] = bool(report["candidate_sha"]) and not report["candidate_runtime_tracked_dirty"]
            if not report["candidate_sha_is_exact_runtime"]:
                raise QualificationFailure("candidate-runtime-not-clean")
            report["runtime_source_sha256"] = freeze_candidate(repo.resolve(), frozen)
            server = PrivateGateway(frozen, root, provider, model, timeout)
            server.start()
            selected = provider + "/" + model
            for protocol in protocols:
                if include_text:
                    nonce = "probe-" + secrets.token_hex(8)
                    history = [{"role": "user", "content": "Reply exactly: " + nonce}]
                    def text_check(stream=False):
                        reply = server.exchange(protocol, request_for(protocol, selected, history, stream=stream))
                        if reply.text != nonce or reply.calls:
                            raise QualificationFailure("wrong-exact-output", evidence=reply.evidence())
                        return reply
                    result = case(protocol + ".text", text_check)
                    # A denied Claude generation is a failure observation, not
                    # a reason to repeatedly hit that account.
                    if provider == "claude-subscription" and result is None:
                        break
                    case(protocol + ".stream", lambda: text_check(True))
                fixture_key = secrets.token_hex(8)
                fixture_value = "fixture-" + secrets.token_hex(12)
                fixture = root / ("fixture-" + protocol + ".txt")
                fixture.write_text(fixture_value, encoding="utf-8")
                fixture.chmod(0o600)
                history = [{"role": "user", "content": "Call read_fixture with fixture_id=" + fixture_key +
                            ". Its value is unknown to you. After its real result, reply exactly with that value. Do not guess."}]
                tools = [{"name": "read_fixture", "description": "Caller-owned read of a disposable random fixture.",
                          "parameters": {"type": "object", "properties": {"fixture_id": {"type": "string"}},
                                         "required": ["fixture_id"], "additionalProperties": False}}]
                def request_tool():
                    reply = server.exchange(protocol, request_for(protocol, selected, history, tools=tools))
                    if len(reply.calls) != 1 or reply.calls[0].name != "read_fixture" or reply.calls[0].arguments != {"fixture_id": fixture_key}:
                        raise QualificationFailure("wrong-caller-tool")
                    return reply
                requested = case(protocol + ".caller-tool", request_tool)
                if requested is None:
                    if provider == "claude-subscription":
                        break
                    continue
                # Execution happens here in the HTTP caller, exactly once;
                # no Gateway tool executor or artificial result is involved.
                history.extend(requested.history_items)
                history.append(tool_result_item(protocol, requested.calls[0], fixture.read_text(encoding="utf-8")))
                def result_tool():
                    reply = server.exchange(protocol, request_for(protocol, selected, history, tools=tools))
                    if reply.text != fixture_value or reply.calls:
                        raise QualificationFailure("wrong-real-tool-result", evidence=reply.evidence())
                    return reply
                completed = case(protocol + ".tool-result", result_tool)
                if completed is not None:
                    history.extend(completed.history_items)
                    histories[protocol] = (history, fixture_value)
            if histories:
                old_pid = server.process.pid
                server.restart()
                report["gateway_restart_observed"] = old_pid != server.process.pid
                for protocol, (history, value) in histories.items():
                    next_history = [*history, {"role": "user", "content": "What was the exact fixture value already returned? Reply exactly with it; do not call tools."}]
                    def continuation():
                        reply = server.exchange(protocol, request_for(protocol, selected, next_history))
                        if reply.text != value or reply.calls:
                            raise QualificationFailure("wrong-history-after-restart", evidence=reply.evidence())
                        return reply
                    case(protocol + ".restart-history-fresh-caller", continuation)
            if provider == "cursor-subscription" and include_cancel:
                def cancellation():
                    evidence = server.cancel_stream(selected)
                    if not evidence["caller_wait_ended"] or not evidence["upstream_socket_cleanup_observed"]:
                        raise QualificationFailure("cancel-cleanup-unobserved", evidence=evidence)
                    return evidence
                case("chat.caller-cancel", cancellation)
            if provider == "cursor-subscription" and cancel_after_first_text:
                case("chat.caller-cancel-after-first-text", lambda: server.cancel_stream_after_first_text(selected))
        except QualificationFailure as error:
            qualified = False
            records.append({"case": "gateway.setup", "state": "failed", "code": error.code})
        finally:
            if server is not None:
                server.stop()
    report["private_artifacts_removed"] = not root.exists()
    report["cli_version_observed_after_run"] = installed_cli_version(provider)
    report["ordinary_qualified"] = qualified and bool(records) and bool(protocols)
    report["caller_cancel_qualified"] = any(row["case"] == "chat.caller-cancel" and row["state"] == "passed" for row in records)
    report["caller_cancel_after_first_text_qualified"] = any(row["case"] == "chat.caller-cancel-after-first-text"
                                                            and row["state"] == "passed" for row in records)
    report["generation_qualified"] = False  # full product acceptance includes separate advanced/platform gates
    report["elapsed_seconds"] = round(time.monotonic() - start, 2)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--provider", choices=("cursor-subscription", "claude-subscription"), required=True)
    parser.add_argument("--model", required=True, help="Exact vendor ID without Provider prefix")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=60)
    parser.add_argument("--total-timeout", type=float, default=900)
    parser.add_argument("--protocol", choices=PROTOCOLS, action="append")
    parser.add_argument("--cancel-only", action="store_true", help="Run only the actual upstream-wait caller cancellation probe")
    parser.add_argument("--cancel-after-first-text-only", action="store_true", help="Run only cancellation after an actual nonempty Chat text delta")
    parser.add_argument("--skip-cancel", action="store_true", help="Retain earlier cancellation evidence while verifying only protocol deltas")
    parser.add_argument("--tool-continuation-only", action="store_true", help="Verify new real tool call/result and restart without repeating text/stream/cancel")
    args = parser.parse_args(argv)
    if args.timeout <= 0 or args.total_timeout <= 0 or not (args.repo_root / "src-python/codex_proxy.py").is_file():
        parser.error("positive bounds and a real Gateway checkout are required")
    if args.cancel_only and args.tool_continuation_only:
        parser.error("select cancellation or tool continuation, not both")
    if args.cancel_after_first_text_only and (args.cancel_only or args.skip_cancel or args.tool_continuation_only or args.protocol):
        parser.error("select the after-first-text cancellation scenario alone")
    if args.cancel_after_first_text_only and args.provider != "cursor-subscription":
        parser.error("after-first-text socket observation currently requires Cursor")
    result = run_qualification(args.repo_root, args.provider, args.model, timeout=args.timeout,
                               total_timeout=args.total_timeout, protocols=() if (args.cancel_only or args.cancel_after_first_text_only) else tuple(args.protocol or PROTOCOLS),
                               progress=lambda case: print(json.dumps(case), flush=True),
                               include_cancel=not (args.skip_cancel or args.tool_continuation_only or args.cancel_after_first_text_only),
                               include_text=not args.tool_continuation_only,
                               cancel_after_first_text=args.cancel_after_first_text_only)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"provider": args.provider, "ordinary_qualified": result["ordinary_qualified"],
                      "cases": len(result["cases"]), "private_artifacts_removed": result["private_artifacts_removed"]}))
    passed = (result["caller_cancel_after_first_text_qualified"] if args.cancel_after_first_text_only else
              result["caller_cancel_qualified"] if args.cancel_only else result["ordinary_qualified"])
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
