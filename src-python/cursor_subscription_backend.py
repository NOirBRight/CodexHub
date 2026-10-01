"""Official Cursor account AgentService exchange, with caller-owned tools.

The transport is duplex HTTP/2: the request remains open while responses ask
for history blobs. No official CLI settings, account, refresh, or project are
modified. See cursor_subscription_wire for protocol attribution.
"""
from __future__ import annotations

import base64
from collections import deque
from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import queue
import re
import shutil
import socket
import ssl
import subprocess
import threading
import tempfile
import time
from typing import Any, Callable, Iterator, Mapping
from urllib.parse import urlsplit
import uuid

from claude_native_models import concrete_executable
from cli_subscription_discovery import run_cli, DiscoveryFailure
from subscription_backend_contract import BackendError, load_http2_dependencies
import cursor_subscription_wire as wire

_VERSION = re.compile(r"\d{4}\.\d{2}\.\d{2}-[0-9a-f]+\Z")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+\[\]-]{0,255}\Z")
_CALL_PREFIX = "call_cursor_"
_CONFIG_URL = "https://api2.cursor.sh/aiserver.v1.ServerConfigService/GetServerConfig"


@dataclass(frozen=True)
class CursorAccount:
    """One immutable official account lease; never include it in public status."""
    source: Path = field(repr=False)
    snapshot: bytes = field(repr=False)
    token: str = field(repr=False)
    version: str
    expiry: float | None = None

    def check(self) -> None:
        try:
            unchanged = _read_source(self.source) == self.snapshot
        except BackendError:
            unchanged = False
        if not unchanged:
            raise BackendError("account-changed", "Cursor account changed during this request. Retry using the current account.", 401)
        if self.expiry is not None and self.expiry <= time.time():
            raise BackendError("auth-expired", "Cursor login expired. Renew it with the official Cursor CLI.", 401)


def _read_source(path: Path) -> bytes:
    try:
        with path.open("rb") as stream:
            data = stream.read(4 * 1024 * 1024 + 1)
    except OSError:
        raise BackendError("auth-required", "Sign in using the official Cursor CLI.", 401) from None
    if len(data) > 4 * 1024 * 1024:
        raise BackendError("auth-required", "Cursor login could not be read. Check the official CLI.", 401)
    return data


def load_cursor_account(*, source_home: Path | None = None, environ: Mapping[str, str] | None = None) -> CursorAccount:
    """Admit only an installed official CLI and its existing current login."""
    env = dict(os.environ if environ is None else environ)
    home = source_home or Path.home()
    candidate = shutil.which("cursor-agent", path=env.get("PATH", ""))
    if not candidate:
        raise BackendError("cli-missing", "Install the official Cursor CLI before using this Provider.", 503)
    try:
        binary = concrete_executable(Path(candidate).absolute(), "cursor-agent").resolve()
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise BackendError("cli-missing", "The official Cursor CLI could not be resolved.", 503) from None
    # Official installers use versions/<date-hash>/{cursor-agent,node}. Never
    # claim an invented fallback version when an unsupported install is found.
    version = next((part for part in binary.parts if _VERSION.fullmatch(part)), None)
    if not version:
        # --version is inference/login free. Empty private settings avoid the
        # ambient Gateway injection even for alternate official install layouts.
        try:
            with tempfile.TemporaryDirectory(prefix="codexhub-cursor-version-") as directory:
                root = Path(directory)
                root.chmod(0o700)
                private_env = {key: value for key, value in env.items() if key in {
                    "PATH", "SystemRoot", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT",
                }}
                private_env.update({"HOME": directory, "USERPROFILE": directory,
                                    "XDG_CONFIG_HOME": directory, "XDG_CACHE_HOME": directory,
                                    "XDG_DATA_HOME": directory, "APPDATA": directory,
                                    "LOCALAPPDATA": directory, "TMPDIR": directory,
                                    "TMP": directory, "TEMP": directory, "NO_COLOR": "1",
                                    "DISABLE_AUTOUPDATER": "1", "DISABLE_TELEMETRY": "1"})
                result = run_cli(binary, ["--version"], env=private_env, cwd=root, timeout=5)
                value = result.stdout.strip()
                if not result.returncode and _VERSION.fullmatch(value):
                    version = value
        except (OSError, ValueError, DiscoveryFailure):
            pass
        if not version:
            raise BackendError("cli-version-unavailable", "Could not identify the installed Cursor CLI version. Reinstall the official CLI.", 503)
    config = Path(env.get("APPDATA") or home / "AppData" / "Roaming") if os.name == "nt" else Path(env.get("XDG_CONFIG_HOME") or home / ".config")
    source = config / ("Cursor" if os.name == "nt" else "cursor") / "auth.json"
    snapshot = _read_source(source)
    try:
        auth = json.loads(snapshot)
        token = auth.get("accessToken") if isinstance(auth, dict) else None
        if not isinstance(token, str) or not token or any(char.isspace() for char in token):
            raise ValueError()
        expiry = None
        if token.count(".") == 2:
            encoded = token.split(".")[1]
            claims = json.loads(base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)))
            expiry = claims.get("exp") if isinstance(claims, dict) else None
            if not isinstance(expiry, (int, float)) or isinstance(expiry, bool) or not math.isfinite(expiry):
                raise ValueError()
    except (ValueError, TypeError, UnicodeError):
        raise BackendError("auth-required", "Cursor login could not be read. Check the official CLI.", 401) from None
    account = CursorAccount(source, snapshot, token, version, expiry)
    account.check()
    return account


class HTTP2Duplex:
    """One TLS h2 request with bounded queues and a dedicated connection pump.

    Only the pump accesses H2Connection. Both directions remain independently
    readable during flow control; closing a consumer always shuts its socket.
    """
    def __init__(self, url: str, headers: Mapping[str, str], *, cancel: threading.Event, timeout: float,
                 tls_context_factory: Callable[[], ssl.SSLContext] = ssl.create_default_context) -> None:
        self.tls_context_factory = tls_context_factory
        self.url, self.headers, self.cancel = url, headers, cancel
        self.deadline = time.monotonic() + timeout
        self.status = 0
        self.response_headers: dict[str, str] = {}
        self._out: queue.Queue[bytes | None] = queue.Queue(maxsize=32)
        self._incoming: queue.Queue[bytes | BackendError] = queue.Queue(maxsize=32)
        self._stop = threading.Event()
        self._socket: ssl.SSLSocket | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> HTTP2Duplex:
        self._thread = threading.Thread(target=self._pump, name="cursor-http2", daemon=True)
        self._thread.start()
        return self

    def _check(self) -> None:
        if self.cancel.is_set() or self._stop.is_set():
            raise BackendError("cancelled", "Cursor request was cancelled.", 499)
        if time.monotonic() >= self.deadline:
            raise BackendError("upstream-timeout", "Cursor request timed out.", 504)

    def send(self, data: bytes) -> None:
        self._put_out(data)

    def finish_request(self) -> None:
        self._put_out(None)

    def _put_out(self, data: bytes | None) -> None:
        if data is not None and len(data) > wire.MAX_BYTES + 5:
            raise wire.malformed()
        while True:
            self._check()
            try:
                self._out.put(data, timeout=.1)
                return
            except queue.Full:
                continue

    def receive(self) -> bytes | None:
        self._check()
        try:
            item = self._incoming.get(timeout=.1)
        except queue.Empty:
            return None
        if isinstance(item, BackendError):
            raise item
        return item

    def _deliver(self, value: bytes | BackendError) -> None:
        while not self._stop.is_set():
            try:
                self._incoming.put(value, timeout=.1)
                return
            except queue.Full:
                continue

    def _pump(self) -> None:
        raw: socket.socket | None = None
        try:
            try:
                load_http2_dependencies()
                from h2.config import H2Configuration
                from h2.connection import H2Connection
                from h2.events import DataReceived, ResponseReceived, StreamEnded, StreamReset, ConnectionTerminated
            except ImportError:
                raise BackendError("backend-unavailable", "Cursor HTTP/2 transport is not installed.", 503) from None
            target = urlsplit(self.url)
            self._check()
            raw = socket.create_connection((target.hostname, target.port or 443), timeout=min(1, max(.1, self.deadline - time.monotonic())))
            context = self.tls_context_factory()
            context.set_alpn_protocols(["h2"])
            self._socket = context.wrap_socket(raw, server_hostname=target.hostname, do_handshake_on_connect=False)
            self._socket.settimeout(.1)
            while True:
                self._check()
                try:
                    self._socket.do_handshake()
                    break
                except (socket.timeout, ssl.SSLWantReadError, ssl.SSLWantWriteError):
                    continue
            if self._socket.selected_alpn_protocol() != "h2":
                raise BackendError("upstream-protocol-error", "Cursor endpoint did not negotiate HTTP/2.")
            self._socket.settimeout(.1)
            connection = H2Connection(config=H2Configuration(client_side=True, header_encoding="utf-8"))
            connection.initiate_connection()
            path = target.path + ("?" + target.query if target.query else "")
            connection.send_headers(1, [(":method", "POST"), (":scheme", "https"), (":authority", target.netloc), (":path", path)] + [(key.lower(), value) for key, value in self.headers.items()])
            pending: deque[bytes | None] = deque()
            heartbeat_at = time.monotonic() + 5
            connect_stream = any(key.lower() == "content-type" and value == "application/connect+proto" for key, value in self.headers.items())
            while True:
                self._check()
                while len(pending) < 32:
                    try:
                        pending.append(self._out.get_nowait())
                    except queue.Empty:
                        break
                if connect_stream and time.monotonic() >= heartbeat_at and len(pending) < 32:
                    pending.append(wire.frame(wire.binary(7, b"")))
                    heartbeat_at = time.monotonic() + 5
                while pending:
                    item = pending[0]
                    if item is None:
                        connection.end_stream(1)
                        pending.popleft()
                        continue
                    size = min(len(item), connection.local_flow_control_window(1), connection.max_outbound_frame_size)
                    if not size:
                        break
                    connection.send_data(1, item[:size])
                    if size == len(item):
                        pending.popleft()
                    else:
                        pending[0] = item[size:]
                outgoing = connection.data_to_send()
                if outgoing:
                    self._socket.sendall(outgoing)
                try:
                    data = self._socket.recv(65536)
                except socket.timeout:
                    continue
                if not data:
                    raise BackendError("upstream-interrupted", "Cursor reply ended before the transport completed.")
                for event in connection.receive_data(data):
                    if isinstance(event, ResponseReceived):
                        self.response_headers = dict(event.headers)
                        self.status = int(self.response_headers.get(":status", "0"))
                    elif isinstance(event, DataReceived):
                        connection.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
                        if event.stream_id == 1:
                            self._deliver(event.data)
                    elif isinstance(event, StreamEnded) and event.stream_id == 1:
                        self._deliver(b"")
                        return
                    elif isinstance(event, (StreamReset, ConnectionTerminated)):
                        raise BackendError("upstream-interrupted", "Cursor closed the active stream.")
        except BackendError as error:
            self._deliver(error)
        except Exception:
            # TLS/socket/h2 diagnostics can contain identities or request data.
            self._deliver(BackendError("upstream-connection-error", "Could not connect to Cursor AgentService."))
        finally:
            if self._socket is not None:
                self._socket.close()
            elif raw is not None:
                raw.close()

    def __exit__(self, *_args: Any) -> None:
        self._stop.set()
        if self._socket is not None:
            try:
                self._socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            self._socket.close()
        if self._thread is not None:
            self._thread.join(timeout=6)


def _headers(account: CursorAccount, content_type: str) -> dict[str, str]:
    return {"Content-Type": content_type, "Connect-Protocol-Version": "1",
            "Authorization": "Bearer " + account.token,
            "x-cursor-client-version": "cli-" + account.version,
            "x-cursor-client-type": "cli", "x-ghost-mode": "true"}


def _failure(status: int, body: bytes = b"") -> BackendError | None:
    code = ""
    if body:
        try:
            payload = json.loads(body)
            if not isinstance(payload, dict):
                raise ValueError()
            error = payload.get("error", payload)
            if not isinstance(error, dict):
                raise ValueError()
            code = error.get("code", "")
            if not isinstance(code, str):
                raise ValueError()
        except (ValueError, UnicodeError):
            if 200 <= status < 300:
                return wire.malformed()
    if 200 <= status < 300 and not code:
        return None
    if status == 401 or code == "unauthenticated":
        return BackendError("auth-expired", "Cursor rejected this login. Renew it with the official CLI.", 401)
    if status == 403 or code == "permission_denied":
        return BackendError("not-eligible", "Cursor denied this account or model request.", 403)
    if status == 429 or code == "resource_exhausted":
        return BackendError("usage-limit", "Cursor usage limit was reached.", 429)
    if status == 400 or code == "invalid_argument":
        return BackendError("invalid-upstream-request", "Cursor rejected this model or conversation.", 400)
    return BackendError("upstream-error", "Cursor AgentService could not complete the request.", 503 if code == "unavailable" else 502)


def _agent_url(account: CursorAccount, factory: Callable[..., Any], cancel: threading.Event, timeout: float) -> str:
    with factory(_CONFIG_URL, _headers(account, "application/json"), cancel=cancel, timeout=min(10, timeout)) as stream:
        stream.send(b"{}")
        stream.finish_request()
        body = bytearray()
        while True:
            account.check()
            chunk = stream.receive()
            if chunk is None:
                continue
            if not chunk:
                break
            account.check()
            body.extend(chunk)
            if len(body) > 4 * 1024 * 1024:
                raise wire.malformed()
        failure = _failure(stream.status, bytes(body)) if not 200 <= stream.status < 300 else None
        if failure:
            raise failure
    try:
        config = json.loads(body)
        # Privacy mode must use the privacy endpoint, never agentn fallback.
        raw = config["agentUrlConfig"]["agentUrl"]
        if not isinstance(raw, str):
            raise ValueError()
        target = urlsplit(raw)
        host = target.hostname or ""
        if (target.scheme != "https" or target.username or target.password or target.port not in (None, 443)
                or target.path not in ("", "/") or target.query or target.fragment
                or not host.endswith(".cursor.sh")):
            raise ValueError()
        return raw.rstrip("/")
    except (ValueError, TypeError, KeyError):
        raise BackendError("upstream-configuration-error", "Cursor did not return a trusted privacy AgentService endpoint.") from None


def _invalid(message: str = "Cursor requires a valid complete Chat Completions history.") -> BackendError:
    return BackendError("invalid-request", message, 400)


def _caller_content(content: Any) -> list[dict[str, Any]]:
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if not isinstance(content, list):
        raise _invalid()
    out = []
    for part in content:
        if not isinstance(part, dict):
            raise _invalid()
        if part.get("type") == "text" and isinstance(part.get("text"), str):
            out.append({"type": "text", "text": part["text"]})
        elif part.get("type") == "image_url":
            url = part.get("image_url", {}).get("url") if isinstance(part.get("image_url"), dict) else None
            if not isinstance(url, str) or not url.startswith("data:image/"):
                raise BackendError("unsupported-content", "Cursor requires inline image data; remote images are unsupported.", 400)
            try:
                prefix, encoded = url.split(",", 1)
                mime = prefix[5:].removesuffix(";base64")
                if not prefix.endswith(";base64"):
                    raise ValueError()
                data = base64.b64decode(encoded, validate=True)
                if len(data) > wire.MAX_BYTES:
                    raise ValueError()
            except ValueError:
                raise _invalid("Cursor image data is malformed or too large.") from None
            out.append({"type": "image", "mimeType": mime, "image": {"__type": "Uint8Array", "hex": data.hex()}})
        else:
            raise BackendError("unsupported-content", "Cursor cannot carry this content type safely.", 400)
    return out


def _encode_call_id(raw: str) -> str:
    # Fully reversible safe encoding, unlike replacing newline with __ which
    # collides with existing IDs. Completed histories need no process registry.
    return _CALL_PREFIX + base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _decode_call_id(caller: str) -> str:
    if not caller.startswith(_CALL_PREFIX):
        return caller
    try:
        value = caller[len(_CALL_PREFIX):]
        decoded = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True).decode()
        if not decoded or _encode_call_id(decoded) != caller:
            raise ValueError()
        return decoded
    except (ValueError, UnicodeError):
        raise _invalid("Cursor tool Call identity is malformed.") from None


def _conversation(payload: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str]:
    if payload.get("n") not in (None, 1):
        raise BackendError("unsupported-parameter", "Cursor supports one generated choice per request.", 400)
    tools = []
    names = set()
    supplied_tools = payload.get("tools") or []
    if not isinstance(supplied_tools, list):
        raise _invalid("Cursor caller tools must be a list.")
    for tool in supplied_tools:
        if not isinstance(tool, dict) or tool.get("type") != "function" or not isinstance(tool.get("function"), dict):
            raise _invalid("Cursor requires adapted caller function tools.")
        item = tool["function"]
        name = item.get("name")
        if not isinstance(name, str) or not name or len(name) > 256 or name in names:
            raise _invalid("Cursor caller tool names must be distinct.")
        schema = item.get("parameters", {"type": "object", "properties": {}})
        if not isinstance(schema, dict) or not isinstance(item.get("description", ""), str):
            raise _invalid()
        try:
            wire.json_bytes(schema)
            wire.google_value(schema)
        except (ValueError, TypeError, OverflowError, RecursionError, BackendError):
            raise _invalid("Cursor caller tool schema must be bounded JSON.") from None
        tools.append({"name": name, "description": item.get("description", ""), "parameters": schema})
        names.add(name)
    if payload.get("tool_choice") not in (None, "auto", "none"):
        raise BackendError("unsupported-tool-choice", "Cursor currently supports automatic caller tool selection.", 400)
    # Refuse declared semantics that AgentService does not represent, rather
    # than quietly dropping caller output or sampling requirements.
    for key in ("response_format", "audio", "modalities", "prediction", "stop", "logit_bias", "temperature", "top_p", "max_tokens", "max_completion_tokens",
                "seed", "frequency_penalty", "presence_penalty", "functions", "function_call", "reasoning_effort"):
        if payload.get(key) is not None:
            raise BackendError("unsupported-parameter", "Cursor cannot represent the requested generation parameter: " + key + ".", 400)
    if payload.get("parallel_tool_calls") is not None and payload["parallel_tool_calls"] is not True:
        raise BackendError("unsupported-parameter", "Cursor cannot enforce serial caller tool selection.", 400)
    history = payload.get("messages")
    if not isinstance(history, list) or not history or len(history) > 100000:
        raise _invalid()
    messages = []
    pending: dict[str, str] = {}
    seen: set[str] = set()
    upstream_calls: set[str] = set()
    results = []
    last_user = "."
    def flush() -> None:
        nonlocal results
        if pending:
            raise _invalid("Cursor continuation requires every preceding tool Call result; no results are fabricated.")
        if results:
            messages.append({"role": "tool", "content": results})
            results = []
    for item in history:
        if not isinstance(item, dict):
            raise _invalid()
        role = item.get("role")
        if item.get("refusal"):
            raise BackendError("unsupported-content", "Cursor cannot safely preserve typed refusal history.", 400)
        if role == "tool":
            call_id = item.get("tool_call_id")
            if not isinstance(call_id, str) or call_id not in pending:
                raise _invalid("Cursor tool result has no matching pending Call.")
            content = item.get("content")
            if not isinstance(content, str):
                raise _invalid("Cursor tool results must be adapted text.")
            pending.pop(call_id)
            try:
                result = json.loads(content)
                wire.json_bytes(result)
            except ValueError:
                result = content
            results.append({"type": "tool-result", "toolCallId": _decode_call_id(call_id), "toolName": wire.DYNAMIC_TOOL,
                            "result": result, "experimental_content": [{"type": "text", "text": content}], **({"isError": True} if item.get("is_error") else {})})
            continue
        flush()
        if role not in ("system", "developer", "user", "assistant"):
            raise _invalid()
        content = _caller_content(item.get("content"))
        if role == "assistant":
            history_calls = item.get("tool_calls") or []
            if not isinstance(history_calls, list):
                raise _invalid()
            for call in history_calls:
                if not isinstance(call, dict) or call.get("type") != "function" or not isinstance(call.get("function"), dict):
                    raise _invalid()
                call_id = call.get("id")
                function = call["function"]
                if not isinstance(call_id, str) or not call_id or call_id in seen or not isinstance(function.get("name"), str) or not function["name"]:
                    raise _invalid("Cursor history contains an invalid or undeclared caller tool Call.")
                upstream_call_id = _decode_call_id(call_id)
                if upstream_call_id in upstream_calls:
                    raise _invalid("Cursor history contains ambiguous upstream Call identities.")
                upstream_calls.add(upstream_call_id)
                try:
                    args = json.loads(function["arguments"])
                    if not isinstance(args, dict):
                        raise ValueError()
                    wire.json_bytes(args)
                except (KeyError, ValueError, TypeError):
                    raise _invalid("Cursor tool arguments must be a JSON object.") from None
                pending[call_id] = function["name"]
                seen.add(call_id)
                content.append({"type": "tool-call", "toolCallId": upstream_call_id, "toolName": wire.DYNAMIC_TOOL,
                                "args": {"namespace": wire.NAMESPACE, "toolName": function["name"], "arguments": args}})
        if role == "user":
            last_user = next((part["text"] for part in content if part["type"] == "text" and part["text"]), last_user)
        if content:
            if role in ("system", "developer"):
                if any(part["type"] != "text" for part in content):
                    raise BackendError("unsupported-content", "Cursor system context requires text.", 400)
                messages.append({"role": "system", "content": "\n".join(part["text"] for part in content)})
            else:
                messages.append({"role": role, "content": content})
    flush()
    if payload.get("tool_choice") == "none":
        tools = []
    if tools:
        catalog = "\n\n<dynamic_tool_catalog>\nCaller tools in MCP namespace codexhub. Use CallDynamicTool with namespace codexhub, toolName and arguments. Only these caller tools are available.\n" + json.dumps(tools, ensure_ascii=False) + "\n</dynamic_tool_catalog>"
        messages.insert(0, {"role": "system", "content": catalog})
    return messages, tools, last_user


def stream_chat(payload: Mapping[str, Any], *, cancel: threading.Event, timeout: float,
                source_home: Path | None = None, environ: Mapping[str, str] | None = None,
                transport_factory: Callable[..., Any] = HTTP2Duplex,
                account_reader: Callable[..., CursorAccount] = load_cursor_account) -> Iterator[dict[str, Any]]:
    """Emit canonical chunks, closing the duplex stream on completion or close.

    Injected account/duplex ports allow deterministic public-boundary tests;
    production always reads the current official CLI account and trusted TLS.
    """
    model = payload.get("model")
    if not isinstance(model, str) or not _MODEL.fullmatch(model):
        raise _invalid("Cursor requires the exact selected vendor model ID.")
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0 or not math.isfinite(timeout):
        raise _invalid("Cursor request timeout must be positive and finite.")
    if cancel.is_set():
        raise BackendError("cancelled", "Cursor request was cancelled.", 499)
    messages, tools, last_user = _conversation(payload)
    account = account_reader(source_home=source_home, environ=environ)
    account.check()
    started = time.monotonic()
    endpoint = _agent_url(account, transport_factory, cancel, timeout)
    account.check()
    run, blobs = wire.build_run(messages, last_user, tools, model)
    headers = _headers(account, "application/connect+proto")
    headers.update({"x-request-id": str(uuid.uuid4()), "x-cursor-agent-allowed-tools": "mcp_tool_call,get_mcp_tools_tool_call"})
    chunk_id = "chatcmpl-cursor-" + uuid.uuid4().hex
    created = int(time.time())
    def chunk(delta: dict[str, Any], finish: str | None = None, usage: dict[str, Any] | None = None) -> dict[str, Any]:
        result = {"id": chunk_id, "object": "chat.completion.chunk", "created": created, "model": model,
                  "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
        if usage is not None:
            result["usage"] = usage
        return result
    calls = 0
    listed: int | None = None
    said = False
    started_reply = False
    emitted_ids: set[str] = set()
    usage = None
    finish = False
    frames = wire.Frames()
    names = {tool["name"] for tool in tools}
    with transport_factory(endpoint + "/agent.v1.AgentService/Run", headers, cancel=cancel,
                           timeout=max(.001, timeout - (time.monotonic() - started))) as stream:
        stream.send(wire.frame(run))
        while not finish:
            account.check()
            if cancel.is_set():
                raise BackendError("cancelled", "Cursor request was cancelled.", 499)
            if time.monotonic() - started >= timeout:
                raise BackendError("upstream-timeout", "Cursor request timed out.", 504)
            received = stream.receive()
            account.check()
            if received is None:
                continue
            if not 200 <= stream.status < 300:
                raise _failure(stream.status, received) or wire.malformed()
            if not received:
                # EOF alone is not a completed turn or completed tool step.
                raise BackendError("upstream-interrupted", "Cursor reply ended before its turn completed.")
            for end, data in frames.feed(received):
                if end:
                    failure = _failure(200, data)
                    if failure:
                        raise failure
                    if not said and not calls:
                        raise BackendError("empty-reply", "Cursor returned an empty reply.")
                    if listed is not None and calls != listed:
                        raise BackendError("upstream-interrupted", "Cursor tool step ended before all Calls arrived.")
                    finish = True
                    break
                for key, body in wire.fields(data):
                    if not isinstance(body, bytes):
                        continue
                    if key == 1:
                        for update, detail in wire.fields(body):
                            if update not in (1, 4, 14, 27):
                                continue
                            if not isinstance(detail, bytes):
                                raise wire.malformed()
                            if update in (1, 4):
                                value = wire.text(detail, 1)
                                if value:
                                    if not started_reply:
                                        yield chunk({"role": "assistant"})
                                        started_reply = True
                                    said = True
                                    yield chunk({"content" if update == 1 else "reasoning_content": value})
                            elif update == 14:
                                supplied = {key: value for key, value in wire.fields(detail) if key in (1, 2, 3, 4)}
                                if any(not isinstance(value, int) for value in supplied.values()):
                                    raise wire.malformed()
                                # Absence remains unknown; never estimate usage.
                                if 1 in supplied and 2 in supplied:
                                    usage = {"prompt_tokens": supplied[1], "completion_tokens": supplied[2], "total_tokens": supplied[1] + supplied[2]}
                                    if 3 in supplied:
                                        usage["prompt_tokens_details"] = {"cached_tokens": supplied[3]}
                                if not calls:
                                    finish = True
                            elif update == 27:
                                listed = wire.get(detail, 1, 0)
                                if not isinstance(listed, int) or listed < calls or listed > 1024:
                                    raise wire.malformed()
                                finish = bool(listed and calls == listed)
                    elif key == 4:
                        request_id = wire.integer(body, 1)
                        for operation, detail in wire.fields(body):
                            if operation == 2:
                                blob_id = wire.get(detail, 1)
                                answer = wire.binary(1, blobs[blob_id]) if blob_id in blobs else wire.binary(2, wire.string(1, "blob not found"))
                                stream.send(wire.frame(wire.binary(3, wire.number(1, request_id) + wire.binary(2, answer))))
                            elif operation == 3:
                                stream.send(wire.frame(wire.binary(3, wire.number(1, request_id) + wire.binary(3, b""))))
                    elif key == 2:
                        request_id = wire.integer(body, 1)
                        exec_id = wire.text(body, 15)
                        recognized = False
                        for operation, detail in wire.fields(body):
                            if operation == 11:
                                recognized = True
                                name = wire.text(detail, 5) or wire.text(detail, 1).removeprefix(wire.NAMESPACE + "-")
                                namespace = wire.text(detail, 4)
                                if name not in names or namespace not in ("", wire.NAMESPACE):
                                    raise BackendError("undeclared-tool", "Cursor requested a tool outside the caller declarations.")
                                raw_id = wire.text(detail, 3)
                                if not raw_id or len(raw_id) > 2048 or raw_id in emitted_ids:
                                    raise wire.malformed()
                                emitted_ids.add(raw_id)
                                arguments = {}
                                for field_id, entry in wire.fields(detail):
                                    if field_id == 2:
                                        arg_name = wire.text(entry, 1)
                                        if not arg_name or arg_name in arguments:
                                            raise wire.malformed()
                                        arguments[arg_name] = wire.decode_value(wire.get(entry, 2))
                                if not started_reply:
                                    yield chunk({"role": "assistant"})
                                    started_reply = True
                                yield chunk({"tool_calls": [{"index": calls, "id": _encode_call_id(raw_id), "type": "function",
                                                              "function": {"name": name, "arguments": wire.json_bytes(arguments).decode()}}]})
                                calls += 1
                            elif operation in (36, 10):
                                recognized = True
                                if operation == 36:
                                    server = wire.string(1, wire.NAMESPACE) + wire.string(2, wire.NAMESPACE) + wire.string(7, "connected") + b"".join(wire.binary(5, wire.tool_definition(tool)) for tool in tools)
                                    result = wire.binary(1, wire.binary(1, server))
                                else:
                                    result = wire.binary(1, wire.binary(1, wire.binary(4, wire.environment())))
                                stream.send(wire.frame(wire.binary(2, wire.number(1, request_id) + wire.string(15, exec_id) + wire.binary(operation, result))))
                                stream.send(wire.frame(wire.binary(5, wire.binary(1, wire.number(1, request_id)))))
                        if not recognized:
                            stream.send(wire.frame(wire.binary(5, wire.binary(2, wire.number(1, request_id) + wire.string(2, "not available")))))
                            stream.send(wire.frame(wire.binary(5, wire.binary(1, wire.number(1, request_id)))))
                        if listed is not None and calls >= listed and calls:
                            if calls != listed:
                                raise wire.malformed()
                            finish = True
                if finish:
                    break
        account.check()
        yield chunk({}, "tool_calls" if calls else "stop", usage)
