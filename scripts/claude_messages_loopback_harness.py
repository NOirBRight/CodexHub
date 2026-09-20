"""Bounded, deny-by-default smoke scaffold for the Claude Code Messages gate (#74).

Two modes:

* ``serve`` — a synthetic Anthropic-Messages gateway on loopback that records
  sanitized request facts and answers text/tool/error scenarios. It is the same
  server the ``run`` mode starts.
* ``run`` — starts ``serve`` on loopback, launches the installed Claude Code CLI
  with a scrubbed environment and its own temporary HOME/``CLAUDE_CONFIG_DIR``,
  then reports the observation. Network is denied by default: the CLI gets a
  loopback base URL plus loopback HTTP(S) proxies so any egress attempt lands in
  the recorder as a 502 instead of leaving the machine.

``self-check`` exercises serve + admission without any CLI, so the scaffold is
deterministically testable.

Enforcement is pre-dispatch, not log-based: :class:`Admission` counts every
would-be upstream request (messages, count_tokens, discovery, probe) and refuses
the N+1 request before any response is written, and every request body's
``max_tokens`` is validated against the budget before dispatch.

Records contain method, path, header names, and a content-free structure
summary. Never credential values, prompt text, or tool output.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from python_runtime_contract import require_python_313  # noqa: E402

require_python_313(__file__)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src-python"))

from anthropic_messages_prototype import (  # noqa: E402
    AdaptedResponse,
    NotForwardable,
    execute_exchange,
    parse_request,
)
from claude_messages_upstream_fixtures import UpstreamFixtureServer  # noqa: E402

DEFAULT_MAX_REQUESTS = 6
DEFAULT_MAX_OUTPUT_TOKENS = 2048
DEFAULT_TIMEOUT_SECONDS = 90
LOOPBACK_HOSTS = {"127.0.0.1", "::1", "localhost"}
CREDENTIAL_HEADERS = {"authorization", "x-api-key", "proxy-authorization"}


class AdmissionError(RuntimeError):
    """Raised before any network write when the bound would be exceeded."""


@dataclass
class Admission:
    """Per-protocol pre-dispatch budget for every outbound-attempt equivalent."""

    max_requests: int = DEFAULT_MAX_REQUESTS
    max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS
    counts: dict[str, int] = field(default_factory=dict)
    refusals: list[dict[str, Any]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def admit(self, protocol: str, body: bytes | None = None) -> None:
        with self._lock:
            used = self.counts.get(protocol, 0)
            if used >= self.max_requests:
                self.refusals.append({"protocol": protocol, "kind": "request_cap", "used": used})
                raise AdmissionError(f"{protocol}: request cap {self.max_requests} reached")
            if body:
                limit = _declared_max_tokens(body)
                if limit is not None and limit > self.max_output_tokens:
                    self.refusals.append(
                        {"protocol": protocol, "kind": "output_budget", "max_tokens": limit}
                    )
                    raise AdmissionError(
                        f"{protocol}: max_tokens {limit} exceeds budget {self.max_output_tokens}"
                    )
            self.counts[protocol] = used + 1


def _declared_max_tokens(body: bytes) -> int | None:
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    value = payload.get("max_tokens") if isinstance(payload, dict) else None
    return value if isinstance(value, int) else None


def _structure(body: bytes) -> dict[str, Any]:
    """Content-free summary of an inbound Messages request."""

    try:
        request = parse_request(body)
    except ValueError as exc:
        return {"unparsed": str(exc)}
    return {
        "model": request.model,
        "unmodelled_fields": list(request.unmodelled_fields),
        "system_blocks": [block.type for block in request.system],
        "messages": [
            {"role": message.role, "blocks": [block.type for block in message.content]}
            for message in request.messages
        ],
        "tools": len(request.options.get("tools") or []),
        "options": sorted(request.options),
        "option_shapes": {name: _shape(request.options[name]) for name in sorted(request.options)},
        "tool_ids": _tool_id_fingerprints(request),
    }


# Only short enum-like values are recorded; free text and ids never are.
ENUM_KEYS = {"type", "role", "effort", "stop_reason", "mode", "name"}


def _shape(value: Any) -> Any:
    if isinstance(value, dict):
        shape: dict[str, Any] = {"keys": sorted(value)}
        enums = {
            key: item for key, item in value.items()
            if key in ENUM_KEYS and isinstance(item, str) and len(item) < 32
        }
        if enums:
            shape["enums"] = enums
        return shape
    if isinstance(value, list):
        shape = {"len": len(value)}
        if value:
            shape["item"] = _shape(value[0])
        return shape
    return type(value).__name__


def _tool_id_fingerprints(request: Any) -> dict[str, list[str]]:
    """Opaque tool ids as short hashes: proves round-trip identity without storing them."""

    use: list[str] = []
    result: list[str] = []
    for message in request.messages:
        for block in message.content:
            if block.type == "tool_use" and isinstance(block.data.get("id"), str):
                use.append(_fingerprint(block.data["id"]))
            if block.type == "tool_result" and isinstance(block.data.get("tool_use_id"), str):
                result.append(_fingerprint(block.data["tool_use_id"]))
    return {"tool_use": use, "tool_result": result}


def _fingerprint(value: str) -> str:
    import hashlib

    return hashlib.sha256(value.encode()).hexdigest()[:12]


def _header_names(headers: Any) -> dict[str, str]:
    """Header names only; credential values are replaced by their length."""

    names: dict[str, str] = {}
    for name, value in headers.items():
        names[name] = f"<redacted chars={len(value)}>" if name.lower() in CREDENTIAL_HEADERS else "present"
    return names


def _messages_sse(model: str, scenario: str, tool_file: Path) -> bytes:
    if scenario == "tool":
        events = [
            ("message_start", {"type": "message_start", "message": {
                "id": "msg_synthetic_tool", "type": "message", "role": "assistant", "model": model,
                "content": [], "stop_reason": None, "stop_sequence": None,
                "usage": {"input_tokens": 7, "output_tokens": 1}}}),
            ("content_block_start", {"type": "content_block_start", "index": 0,
                                     "content_block": {"type": "tool_use", "id": "toolu_synthetic_loopback_1",
                                                       "name": "Read", "input": {}}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                     "delta": {"type": "input_json_delta",
                                               "partial_json": json.dumps({"file_path": str(tool_file)})}}),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            ("message_delta", {"type": "message_delta",
                               "delta": {"stop_reason": "tool_use", "stop_sequence": None},
                               "usage": {"output_tokens": 5}}),
            ("message_stop", {"type": "message_stop"}),
        ]
    else:
        events = [
            ("message_start", {"type": "message_start", "message": {
                "id": "msg_synthetic_text", "type": "message", "role": "assistant", "model": model,
                "content": [], "stop_reason": None, "stop_sequence": None,
                "usage": {"input_tokens": 7, "output_tokens": 1}}}),
            ("content_block_start", {"type": "content_block_start", "index": 0,
                                     "content_block": {"type": "text", "text": ""}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                     "delta": {"type": "text_delta", "text": "loopback synthetic answer"}}),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            ("message_delta", {"type": "message_delta",
                               "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                               "usage": {"output_tokens": 4}}),
            ("message_stop", {"type": "message_stop"}),
        ]
    return b"".join(f"event: {name}\ndata: {json.dumps(payload)}\n\n".encode() for name, payload in events)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "codexhub-t74-loopback"
    admission: Admission
    records: list[dict[str, Any]]
    record_path: Path
    scenario: str
    tool_file: Path
    message_count = 0
    lock = threading.Lock()

    def log_message(self, *args: Any) -> None:  # keep the scaffold quiet
        pass

    # -- helpers ---------------------------------------------------------
    def _body(self) -> bytes:
        length = int(self.headers.get("content-length") or 0)
        return self.rfile.read(length) if length else b""

    def _record(self, entry: dict[str, Any]) -> None:
        with self.lock:
            self.records.append(entry)
            with self.record_path.open("a") as handle:
                handle.write(json.dumps(entry) + "\n")

    def _reply(self, status: int, payload: bytes, content_type: str = "application/json") -> None:
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(payload)))
        self.send_header("request-id", "req_synthetic_loopback")
        self.end_headers()
        self.wfile.write(payload)
        self.wfile.flush()

    def _proxy_form(self) -> bool:
        return self.path.startswith(("http://", "https://")) or self.command == "CONNECT"

    def _entry(self, body: bytes, protocol: str, decided: str) -> dict[str, Any]:
        return {
            "t": round(time.time(), 3),
            "protocol": protocol,
            "method": self.command,
            "path": self.path,
            "http_version": self.request_version,
            "headers": _header_names(self.headers),
            "egress_guard": self._proxy_form(),
            "decision": decided,
            "request": _structure(body) if body else None,
        }

    def _guard(self, protocol: str, body: bytes, entry: dict[str, Any]) -> bool:
        try:
            self.admission.admit(protocol, body)
        except AdmissionError as exc:
            entry["decision"] = f"refused:{exc}"
            self._record(entry)
            self._reply(429, json.dumps({"type": "error", "error": {
                "type": "rate_limit_error", "message": f"bounded scaffold refusal: {exc}"}}).encode())
            return False
        return True

    # -- endpoints -------------------------------------------------------
    def do_HEAD(self) -> None:
        entry = self._entry(b"", "probe", "served")
        if not self._guard("probe", b"", entry):
            return
        self._record(entry)
        self.send_response(200)
        self.send_header("content-length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        self._handle(b"")

    def do_POST(self) -> None:
        self._handle(self._body())

    def do_CONNECT(self) -> None:
        self._record({"t": round(time.time(), 3), "protocol": "egress", "method": "CONNECT",
                      "path": self.path, "egress_guard": True, "decision": "blocked"})
        self._reply(502, b"")

    def do_PUT(self) -> None:
        self._handle(self._body())

    def _handle(self, body: bytes) -> None:
        if self._proxy_form():
            self._record({"t": round(time.time(), 3), "protocol": "egress", "method": self.command,
                          "path": self.path, "egress_guard": True, "decision": "blocked"})
            self._reply(502, json.dumps({"error": "egress blocked by task #74 scaffold"}).encode())
            return
        if self.path.startswith("/v1/messages/count_tokens"):
            self._route("count_tokens", body, lambda: self._reply(200, b'{"input_tokens":11}'))
            return
        if self.path.startswith("/v1/messages"):
            self._route("messages", body, self._serve_message)
            return
        if self.command == "GET" and self.path.startswith("/v1/models"):
            model = self.server.model_id  # type: ignore[attr-defined]
            payload = json.dumps({"data": [
                {"id": model, "display_name": "Synthetic One", "description": "loopback scaffold"},
                # Ticket #77 asked for one matching and one non-matching id so the
                # discovery substring filter can be observed without live access.
                {"id": "claude-codexhub-test", "display_name": "CodexHub Test"},
                {"id": "provider/gpt-test", "display_name": "Must not match"},
            ]}).encode()
            self._route("discovery", body, lambda: self._reply(200, payload))
            return
        entry = self._entry(body, "unknown", "not_found")
        self._record(entry)
        self._reply(404, json.dumps({"type": "error", "error": {
            "type": "not_found_error", "message": "scaffold serves /v1/messages only"}}).encode())

    def _route(self, protocol: str, body: bytes, action: Any) -> None:
        entry = self._entry(body, protocol, "served")
        if not self._guard(protocol, body, entry):
            return
        self._record(entry)
        action()

    def _serve_message(self) -> None:
        if self.scenario == "error":
            self._reply(400, json.dumps({"type": "error", "error": {
                "type": "invalid_request_error", "message": "synthetic scaffold rejection"}}).encode())
            return
        with self.lock:
            type(self).message_count += 1
            turn = type(self).message_count
        # The tool scenario calls one tool, then answers, so the CLI's follow-up
        # request carries the tool_result for the opaque tool_use id.
        scenario = "tool" if self.scenario == "tool" and turn == 1 else "text"
        model = self.server.model_id  # type: ignore[attr-defined]
        self._reply(200, _messages_sse(model, scenario, self.tool_file),
                    content_type="text/event-stream")


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], handler: Any, *, model_id: str) -> None:
        super().__init__(address, handler)
        self.model_id = model_id


def _build_server(port: int, out: Path, *, scenario: str, admission: Admission,
                  model_id: str, tool_file: Path) -> _Server:
    handler = type("BoundHandler", (_Handler,), {
        "admission": admission,
        "records": [],
        "record_path": out / "requests.jsonl",
        "scenario": scenario,
        "tool_file": tool_file,
    })
    return _Server(("127.0.0.1", port), handler, model_id=model_id)


def _free_port() -> int:
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def command_serve(args: argparse.Namespace) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    admission = Admission(max_requests=args.max_requests, max_output_tokens=args.max_output_tokens)
    server = _build_server(args.port, out, scenario=args.scenario, admission=admission,
                           model_id=args.model, tool_file=Path(args.tool_file))
    print(f"serving loopback Anthropic Messages gateway on 127.0.0.1:{args.port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def _cli_environment(home: Path, base_url: str, proxy_url: str, args: argparse.Namespace) -> dict[str, str]:
    """Scrubbed environment: no inherited credentials or provider selection."""

    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(home),
        "CLAUDE_CONFIG_DIR": str(home / ".claude"),
        "TMPDIR": str(home / "tmp"),
        "TERM": "dumb",
        "ANTHROPIC_BASE_URL": base_url,
        "ANTHROPIC_MODEL": args.model,
        "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "DISABLE_TELEMETRY": "1",
        "DISABLE_AUTOUPDATER": "1",
        "DISABLE_ERROR_REPORTING": "1",
        "HTTP_PROXY": proxy_url,
        "HTTPS_PROXY": proxy_url,
        "http_proxy": proxy_url,
        "https_proxy": proxy_url,
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    }
    if args.credential_carrier in ("auth-token", "both"):
        env["ANTHROPIC_AUTH_TOKEN"] = args.synthetic_token
    if args.credential_carrier in ("api-key", "both"):
        env["ANTHROPIC_API_KEY"] = args.synthetic_token
    if args.enable_discovery:
        env["CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"] = "1"
    return env


def _strace_verdict(log_path: Path) -> dict[str, Any]:
    if not log_path.exists():
        return {"available": False}
    connects = [line for line in log_path.read_text(errors="replace").splitlines() if "connect(" in line]
    outside = [line for line in connects if ("127.0.0.1" not in line and "AF_UNIX" not in line
                                             and "AF_NETLINK" not in line and "AF_INET6" not in line)]
    return {"available": True, "connect_calls": len(connects), "non_loopback": outside[:5]}


def command_run(args: argparse.Namespace) -> int:
    base_port = args.port or _free_port()
    proxy_port = base_port + 1 if args.port else _free_port()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    home = out / "home"
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (home / "tmp").mkdir(parents=True, exist_ok=True)
    work = out / "work"
    work.mkdir(parents=True, exist_ok=True)
    (home / ".claude" / "CLAUDE.md").write_text("synthetic sentinel memory\n")
    (work / "CLAUDE.md").write_text("synthetic sentinel workspace\n")
    tool_file = work / "synthetic-read-target.txt"
    tool_file.write_text("synthetic file body\n")

    base_url = f"http://127.0.0.1:{base_port}"
    proxy_url = f"http://127.0.0.1:{proxy_port}"
    if not args.claude_bin:
        raise SystemExit("run needs --claude-bin; no client is launched automatically")
    if args.allow_network:
        raise SystemExit("live upstream use is not authorized for #74; the scaffold is loopback-only")

    admission = Admission(max_requests=args.max_requests, max_output_tokens=args.max_output_tokens)
    gateway = _build_server(base_port, out, scenario=args.scenario, admission=admission,
                            model_id=args.model, tool_file=tool_file)
    # The proxy listener is the same bound handler: any egress attempt is recorded and 502ed.
    egress = _build_server(proxy_port, out, scenario=args.scenario, admission=admission,
                           model_id=args.model, tool_file=tool_file)
    threads = [
        threading.Thread(target=gateway.serve_forever, daemon=True),
        threading.Thread(target=egress.serve_forever, daemon=True),
    ]
    for thread in threads:
        thread.start()
    time.sleep(0.3)

    env = _cli_environment(home, base_url, proxy_url, args)
    command = [args.claude_bin, "-p", "--debug", "--model", args.model, "--permission-mode", "plan",
               "--output-format", "json", args.prompt]
    strace = shutil.which("strace")
    connect_log = out / "connects.log"
    if strace:
        command = [strace, "-f", "-e", "trace=connect", "-o", str(connect_log), *command]

    started = time.time()
    try:
        completed = subprocess.run(command, cwd=work, env=env, capture_output=True,
                                   timeout=args.timeout, text=True)
        exit_code, stdout, stderr = completed.returncode, completed.stdout, completed.stderr
    except subprocess.TimeoutExpired as exc:
        exit_code = 124
        stdout = (exc.stdout or b"").decode(errors="replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = (exc.stderr or b"").decode(errors="replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
    finally:
        gateway.shutdown()
        egress.shutdown()

    records = [json.loads(line) for line in (out / "requests.jsonl").read_text().splitlines()] if (out / "requests.jsonl").exists() else []
    egress_attempts = [record for record in records if record.get("egress_guard")]
    summary = {
        "claude_bin": args.claude_bin,
        "claude_version": _cli_version(args.claude_bin),
        "scenario": args.scenario,
        "exit_code": exit_code,
        "duration_seconds": round(time.time() - started, 2),
        "admission_counts": admission.counts,
        "admission_refusals": admission.refusals,
        "protocols_observed": sorted({record["protocol"] for record in records}),
        "requests": records,
        "egress_attempts": len(egress_attempts),
        "connect_verdict": _strace_verdict(connect_log),
        "discovery_debug": _discovery_debug_lines(home),
        "cli_stdout_head": stdout[:2000],
        "cli_stderr_head": stderr[:2000],
    }
    (out / "observation.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps({key: summary[key] for key in (
        "claude_version", "scenario", "exit_code", "protocols_observed",
        "admission_counts", "egress_attempts", "connect_verdict")}, indent=2))
    ok = exit_code == 0 and not egress_attempts and not admission.refusals
    if summary["connect_verdict"].get("non_loopback"):
        ok = False
    return 0 if ok else 1


def _discovery_debug_lines(home: Path) -> list[str]:
    """Only the client's own [gatewayDiscovery] status lines; no prompt content."""

    debug_dir = home / ".claude" / "debug"
    lines: list[str] = []
    if not debug_dir.is_dir():
        return lines
    for path in sorted(debug_dir.glob("*.txt")):
        for line in path.read_text(errors="replace").splitlines():
            if "[gatewayDiscovery]" in line:
                lines.append(line.strip()[:300])
    return lines


def _cli_version(claude_bin: str) -> str:
    try:
        result = subprocess.run([claude_bin, "--version"], capture_output=True, text=True, timeout=30)
        return result.stdout.strip() or result.stderr.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return f"unavailable: {exc}"


def command_self_check(args: argparse.Namespace) -> int:
    """Deterministic serve + admission check without launching any client."""

    import urllib.error
    import urllib.request

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    record_path = out / "requests.jsonl"
    record_path.unlink(missing_ok=True)
    admission = Admission(max_requests=2, max_output_tokens=2048)
    server = _build_server(args.port, out, scenario="text", admission=admission,
                           model_id=args.model, tool_file=out / "tool.txt")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    time.sleep(0.3)
    base = f"http://127.0.0.1:{args.port}"
    body = json.dumps({"model": args.model, "max_tokens": 16, "stream": True,
                       "messages": [{"role": "user", "content": [{"type": "text", "text": "synthetic"}]}]}).encode()
    request = urllib.request.Request(f"{base}/v1/messages?beta=true", data=body,
                                     headers={"content-type": "application/json",
                                              "Authorization": "Bearer synthetic"})
    # 1. Output budget is validated before dispatch, not after.
    over = json.dumps({"model": args.model, "max_tokens": 99999, "stream": True,
                       "messages": [{"role": "user", "content": [{"type": "text", "text": "x"}]}]}).encode()
    budget_request = urllib.request.Request(f"{base}/v1/messages", data=over,
                                            headers={"content-type": "application/json"})
    try:
        urllib.request.urlopen(budget_request, timeout=10).read()
        raise AssertionError("output budget was not enforced")
    except urllib.error.HTTPError as exc:
        assert exc.code == 429, exc.code
    # 2. Two admitted requests succeed, and the stream is real SSE.
    with urllib.request.urlopen(request, timeout=10) as response:
        stream = response.read().decode()
        assert response.status == 200, response.status
        assert response.headers["content-type"] == "text/event-stream"
    assert "event: message_start" in stream and "event: message_stop" in stream, stream[:200]
    urllib.request.urlopen(request, timeout=10).read()
    # 3. The N+1 request is refused before dispatch.
    try:
        urllib.request.urlopen(request, timeout=10).read()
        raise AssertionError("request cap was not enforced")
    except urllib.error.HTTPError as exc:
        assert exc.code == 429, exc.code
    server.shutdown()
    records = [json.loads(line) for line in record_path.read_text().splitlines()]
    assert all(record.get("decision", "").startswith(("served", "refused")) for record in records), records
    kinds = [entry["kind"] for entry in admission.refusals]
    assert kinds == ["output_budget", "request_cap"], kinds
    print(json.dumps({"self_check": "pass", "records": len(records),
                      "admission_counts": admission.counts,
                      "refusals": kinds}, indent=2))
    return 0


def command_upstream_self_check(args: argparse.Namespace) -> int:
    """Exercise all three upstream wire formats on a loopback fixture."""

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    admission = Admission(max_requests=20, max_output_tokens=2048)
    attempts: list[dict[str, Any]] = []
    fixture = UpstreamFixtureServer(("127.0.0.1", 0), scenario=args.scenario)
    thread = threading.Thread(target=fixture.serve_forever, daemon=True)
    thread.start()
    paths = {
        "responses": "/v1/responses",
        "chat_completions": "/v1/chat/completions",
        "anthropic_messages": "/v1/messages",
    }
    results: list[dict[str, Any]] = []

    def admit(protocol: str, method: str, final_url: str, body: bytes) -> None:
        admission.admit(protocol, body)
        attempts.append({"protocol": protocol, "method": method, "url": final_url, "body_bytes": len(body)})

    try:
        for protocol, path in paths.items():
            for stream in (False, True):
                url = f"http://127.0.0.1:{fixture.server_port}{path}"
                request = json.dumps(
                    {
                        "model": "claude-synthetic-1",
                        "max_tokens": 128,
                        "stream": stream,
                        "messages": [{"role": "user", "content": "synthetic"}],
                    },
                    separators=(",", ":"),
                ).encode()
                try:
                    response = execute_exchange(
                        request,
                        upstream_format=protocol,
                        url=url,
                        admit=admit,
                    )
                except Exception as exc:  # noqa: BLE001 - self-check reports a bounded failure
                    results.append({"protocol": protocol, "stream": stream, "ok": False, "error": type(exc).__name__})
                    continue
                ok = isinstance(response, AdaptedResponse)
                if isinstance(response, NotForwardable):
                    results.append({"protocol": protocol, "stream": stream, "ok": False, "error": response.reason})
                else:
                    results.append({
                        "protocol": protocol,
                        "stream": stream,
                        "ok": ok and bool(response.body),
                        "status": response.status,
                        "content_type": response.content_type.split(";", 1)[0],
                        "body_bytes": len(response.body),
                        "adaptations": len(response.adaptations),
                    })
    finally:
        fixture.shutdown()
        thread.join(timeout=2)
    summary = {
        "self_check": "pass" if all(item.get("ok") for item in results) else "fail",
        "scenario": args.scenario,
        "streaming_gate": "buffered_fixture_conversion_only",
        "cancellation_gate": "synthetic_terminal_checks_only",
        "attempts": attempts,
        "admission_counts": admission.counts,
        "fixture_records": fixture.records,
        "results": results,
    }
    (out / "upstream-observation.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps({key: summary[key] for key in ("self_check", "scenario", "streaming_gate", "cancellation_gate", "admission_counts", "results")}, indent=2))
    return 0 if summary["self_check"] == "pass" else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("serve", "run", "self-check", "upstream-self-check"):
        child = sub.add_parser(name)
        child.add_argument("--out", default="/tmp/codexhub-t74-loopback")
        child.add_argument("--port", type=int, default=0)
        child.add_argument("--model", default="claude-synthetic-1")
        child.add_argument("--scenario", choices=("text", "tool", "error"), default="text")
        child.add_argument("--max-requests", type=int, default=DEFAULT_MAX_REQUESTS)
        child.add_argument("--max-output-tokens", type=int, default=DEFAULT_MAX_OUTPUT_TOKENS)
        child.add_argument("--tool-file", default="/tmp/codexhub-t74-loopback/work/synthetic.txt")
        if name == "run":
            child.add_argument("--claude-bin", default="")
            child.add_argument("--prompt", default="reply with the word ok")
            child.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
            child.add_argument("--synthetic-token", default="sk-ant-synthetic-loopback-not-a-credential")
            child.add_argument("--credential-carrier", choices=("auth-token", "api-key", "both"),
                               default="auth-token")
            child.add_argument("--enable-discovery", action="store_true")
            child.add_argument("--allow-network", action="store_true")
        if name == "self-check":
            child.set_defaults(max_requests=2)
    args = parser.parse_args(argv)
    if args.command == "serve" and not args.port:
        args.port = _free_port()
    if args.command == "self-check" and not args.port:
        args.port = _free_port()
    if args.command == "serve":
        return command_serve(args)
    if args.command == "run":
        return command_run(args)
    if args.command == "upstream-self-check":
        return command_upstream_self_check(args)
    return command_self_check(args)


if __name__ == "__main__":
    raise SystemExit(main())
