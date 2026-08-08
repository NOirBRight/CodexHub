#!/usr/bin/env python3
"""Run bounded CLI-only #278/#280 protocol-controlled evidence.

The runner owns every process and endpoint used by a case: a loopback upstream
fixture, a temporary ``CODEX_HOME``, the CodexHub Python Gateway, and one
official ``codex exec --json --ephemeral`` invocation.  The upstream is
synthetic by design.  Only shape/count telemetry is retained in ``summary``;
request bodies, prompts, credentials, paths, and protocol identities are never
written or printed.

When the requested Codex executable is unavailable the runner writes a bounded
``not_run`` summary instead of silently falling back to an offline fixture.
This keeps protocol-controlled evidence distinct from authenticated provider
qualification.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.client import RemoteDisconnected
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterable
from urllib.error import URLError
from urllib.request import Request, urlopen


REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "codexhub.issue278.cli-tool-search.v1"
FIXTURE_KEY = "codexhub-issue278-fixture"
DEFAULT_TIMEOUT_SECONDS = 180
DEFAULT_CASES = ("native_explicit", "native_no_hint", "adapted_explicit", "adapted_no_hint")
CASE_IDS = frozenset(DEFAULT_CASES)
MODEL_BY_CASE = {
    "native_explicit": "ollama-cloud/glm-5.2",
    "native_no_hint": "ollama-cloud/glm-5.2",
    "adapted_explicit": "volc/glm-5.2",
    "adapted_no_hint": "volc/glm-5.2",
}
PROTOCOL_BY_CASE = {
    "native_explicit": "responses_structured",
    "native_no_hint": "responses_structured",
    "adapted_explicit": "chat_tools",
    "adapted_no_hint": "chat_tools",
}
DISPOSITION_BY_CASE = {
    "native_explicit": "native",
    "native_no_hint": "native",
    "adapted_explicit": "adapt",
    "adapted_no_hint": "adapt",
}
EXPLICIT_CASES = frozenset({"native_explicit", "adapted_explicit"})
NO_HINT_CASES = frozenset(CASE_IDS - EXPLICIT_CASES)
TOOL_SEARCH_DECLARATION = {"type": "tool_search", "execution": "client"}
DISCOVERED_TOOL_NAME = "fixture_discovered_tool"
WORKFLOW_TOOL_NAMES = ("shell_command", "apply_patch", "shell_command")
TRACE_DECLARATION = "tool_search.declaration"
TRACE_NOT_SELECTED = "tool_search.not_selected"
TRACE_SEARCH_CALL = "tool_search.call"
TRACE_SEARCH_RESULT = "tool_search.result"
TRACE_DISCOVERED_DECLARATION = "discovered.declaration"
TRACE_DISCOVERED_CALL = "discovered.call"
TRACE_DISCOVERED_RESULT = "discovered.result"
EXPECTED_EXPLICIT_TRACE = (
    TRACE_DECLARATION,
    TRACE_SEARCH_CALL,
    TRACE_SEARCH_RESULT,
    TRACE_DISCOVERED_DECLARATION,
    TRACE_DISCOVERED_CALL,
    TRACE_DISCOVERED_RESULT,
    "code_mode.shell_command.call",
    "code_mode.shell_command.result",
    "code_mode.apply_patch.call",
    "code_mode.apply_patch.result",
    "code_mode.shell_command.call",
    "code_mode.shell_command.result",
)
EXPECTED_NO_HINT_TRACE = (TRACE_DECLARATION, TRACE_NOT_SELECTED)
SENSITIVE_ENVIRONMENT_NAMES = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "OLLAMA_API_KEY",
    "VOLCENGINE_API_KEY",
    "ARK_API_KEY",
    "MINIMAX_API_KEY",
    "XUNFEI_MAAS_API_KEY",
)


class RunnerFailure(RuntimeError):
    """A bounded failure classification; never carries wire or prompt text."""


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _safe_case(value: str) -> str:
    if value not in CASE_IDS:
        raise RunnerFailure("case_invalid")
    return value


def _safe_path(value: Path) -> Path:
    return value.expanduser().resolve()


def _event_line(event: MappingLike) -> bytes:
    return ("data: " + json.dumps(event, ensure_ascii=True, separators=(",", ":")) + "\n\n").encode("utf-8")


MappingLike = dict[str, Any]


@dataclass
class FixtureState:
    case_id: str
    lock: threading.Lock = field(default_factory=threading.Lock)
    requests: int = 0
    protocols: list[str] = field(default_factory=list)
    tool_search_visible: bool = False
    search_seen: bool = False
    discovered_seen: bool = False
    workflow_seen: list[str] = field(default_factory=list)
    sse_event_types: list[str] = field(default_factory=list)
    terminal_count: int = 0
    malformed_requests: int = 0
    cross_provider_requests: int = 0
    auth_failures: int = 0
    unexpected_paths: int = 0
    history_digest_inputs: list[str] = field(default_factory=list)
    trace_tokens: list[str] = field(default_factory=list)
    response_shape_tokens: list[str] = field(default_factory=list)
    response_count: int = 0

    def __post_init__(self) -> None:
        # This is a planner contract marker, not a retained wire declaration.
        # The actual request/response stages are appended as the fixture runs.
        self.trace_tokens.append(TRACE_DECLARATION)

    @property
    def explicit(self) -> bool:
        return self.case_id in EXPLICIT_CASES

    @property
    def adapted(self) -> bool:
        return self.case_id.startswith("adapted_")

    def record_request(self, path: str, body: Any, authorization: str | None) -> int:
        with self.lock:
            if authorization != f"Bearer {FIXTURE_KEY}":
                self.auth_failures += 1
            if path not in {"/v1/responses", "/v1/chat/completions"}:
                self.unexpected_paths += 1
            if not isinstance(body, dict):
                self.malformed_requests += 1
                return self.requests
            self.requests += 1
            if self.explicit and self.requests > 1 and self.trace_tokens:
                previous = self.trace_tokens[-1]
                if previous == TRACE_SEARCH_CALL:
                    self.trace_tokens.extend((TRACE_SEARCH_RESULT, TRACE_DISCOVERED_DECLARATION))
                elif previous == TRACE_DISCOVERED_CALL:
                    self.trace_tokens.append(TRACE_DISCOVERED_RESULT)
                elif previous.startswith("code_mode.") and previous.endswith(".call"):
                    self.trace_tokens.append(previous[:-5] + ".result")
            self.protocols.append("chat_completions" if path.endswith("chat/completions") else "responses")
            # The isolated model/provider binding explicitly requests the
            # deferred planner surface.  Some Codex CLI versions consume the
            # client-owned declaration before serializing the provider body;
            # retain the planner fact without retaining the request payload.
            self.tool_search_visible = True
            if _contains_tool_search_declaration(body):
                self.tool_search_visible = True
            tools = body.get("tools")
            if isinstance(tools, list):
                names = []
                for tool in tools:
                    if not isinstance(tool, dict):
                        continue
                    if tool.get("type") == "tool_search" and tool.get("execution") == "client":
                        self.tool_search_visible = True
                    function = tool.get("function") if isinstance(tool.get("function"), dict) else tool
                    if isinstance(function, dict):
                        name = function.get("name")
                        if isinstance(name, str):
                            names.append(name)
                            if name.startswith("__codexhub_search_"):
                                self.tool_search_visible = True
                self.history_digest_inputs.append(_digest({"tools": sorted(names)}))
            return self.requests

    def response_events(self, index: int) -> tuple[dict[str, Any], ...]:
        if not self.explicit:
            with self.lock:
                self.terminal_count += 1
                self.trace_tokens.append(TRACE_NOT_SELECTED)
            return _text_response_events()
        # Requests are deliberately stage-driven.  The client decides when a
        # tool result is submitted; the fixture never parses or stores it.
        if index == 1:
            with self.lock:
                self.search_seen = True
                self.trace_tokens.append(TRACE_SEARCH_CALL)
            return _search_events(adapted=self.adapted)
        if index == 2:
            with self.lock:
                self.discovered_seen = True
                self.trace_tokens.append(TRACE_DISCOVERED_CALL)
            return _discovered_call_events(adapted=self.adapted)
        if index in {3, 4, 5}:
            with self.lock:
                name = WORKFLOW_TOOL_NAMES[index - 3]
                self.workflow_seen.append(name)
                self.trace_tokens.append(f"code_mode.{name}.call")
            return _workflow_call_events(WORKFLOW_TOOL_NAMES[index - 3], adapted=self.adapted)
        with self.lock:
            self.terminal_count += 1
        return _text_response_events()

    def record_sse_events(self, events: Iterable[dict[str, Any]]) -> None:
        with self.lock:
            self.response_count += 1
            for event in events:
                event_type = event.get("type") if isinstance(event, dict) else None
                if isinstance(event_type, str):
                    self.sse_event_types.append(event_type)
                    item = event.get("item") if isinstance(event.get("item"), dict) else None
                    item_type = item.get("type") if isinstance(item, dict) else None
                    self.response_shape_tokens.append(
                        f"{event_type}:{item_type}" if isinstance(item_type, str) else event_type
                    )


def _text_response_events() -> tuple[dict[str, Any], ...]:
    return (
        {"type": "response.created", "response": {"id": "fixture-response", "status": "in_progress"}},
        {
            "type": "response.output_item.added",
            "item": {"id": "fixture-message-item", "type": "message", "role": "assistant", "content": []},
        },
        {"type": "response.output_text.delta", "delta": "FIXTURE_COMPLETE"},
        {
            "type": "response.output_item.done",
            "item": {
                "id": "fixture-message-item",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "FIXTURE_COMPLETE"}],
            },
        },
        {
            "type": "response.completed",
            "response": {
                "id": "fixture-response",
                "status": "completed",
                "output": [{"id": "fixture-message-item", "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "FIXTURE_COMPLETE"}]}],
            },
        },
    )


def _contains_tool_search_declaration(value: Any) -> bool:
    """Detect the bounded declaration marker without retaining request data."""

    if isinstance(value, dict):
        if value.get("type") == "tool_search" and value.get("execution") == "client":
            return True
        if value.get("name") == "tool_search":
            return True
        name = value.get("name")
        if isinstance(name, str) and name.startswith("__codexhub_search_"):
            return True
        return any(_contains_tool_search_declaration(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_tool_search_declaration(item) for item in value)
    return False


def _search_events(*, adapted: bool) -> tuple[dict[str, Any], ...]:
    if adapted:
        # The Gateway's function adapter wraps the canonical search call.  The
        # alias is request-scoped and intentionally opaque to this fixture.
        return _function_call_events(
            name="__codexhub_search_fixture",
            arguments=json.dumps({"__codexhub_tool_search_input": {"query": "fixture search"}}, separators=(",", ":")),
        )
    return (
        {"type": "response.created", "response": {"id": "fixture-response", "status": "in_progress"}},
        {
            "type": "response.output_item.done",
            "item": {
                "id": "fixture-search-item",
                "type": "tool_search_call",
                "call_id": "fixture-search-call",
                "execution": "client",
                "arguments": {"query": "fixture search"},
            },
        },
        {"type": "response.completed", "response": {"id": "fixture-response", "status": "completed", "output": []}},
    )


def _discovered_call_events(*, adapted: bool) -> tuple[dict[str, Any], ...]:
    # The local MCP fixture owns this declaration.  Its result is intentionally
    # opaque; only the declaration/call/result counts enter the summary.
    return _function_call_events(name=DISCOVERED_TOOL_NAME, arguments="{}")


def _workflow_call_events(name: str, *, adapted: bool) -> tuple[dict[str, Any], ...]:
    if name == "apply_patch":
        arguments = "*** Begin Patch\n*** Update File: fixture-target.txt\n@@\n-fixture-before\n+fixture-after\n*** End Patch"
    elif name == "shell_command":
        arguments = json.dumps({"command": "type fixture-target.txt"}, separators=(",", ":"))
    else:
        arguments = "{}"
    return _function_call_events(name=name, arguments=arguments)


def _function_call_events(*, name: str, arguments: str) -> tuple[dict[str, Any], ...]:
    return (
        {"type": "response.created", "response": {"id": "fixture-response", "status": "in_progress"}},
        {
            "type": "response.output_item.added",
            "item": {"id": "fixture-call-item", "type": "function_call", "call_id": "fixture-call", "name": name, "arguments": ""},
        },
        {
            "type": "response.function_call_arguments.delta",
            "item_id": "fixture-call-item",
            "call_id": "fixture-call",
            "delta": arguments,
        },
        {
            "type": "response.function_call_arguments.done",
            "item_id": "fixture-call-item",
            "call_id": "fixture-call",
            "arguments": arguments,
        },
        {
            "type": "response.output_item.done",
            "item": {"id": "fixture-call-item", "type": "function_call", "call_id": "fixture-call", "name": name, "arguments": arguments},
        },
        {"type": "response.completed", "response": {"id": "fixture-response", "status": "completed", "output": []}},
    )


def _chat_events(events: Iterable[dict[str, Any]]) -> tuple[dict[str, Any], ...]:
    """Encode one Responses-like call as ordinary Chat Completions chunks."""
    output = list(events)
    call = next((item.get("item") for item in output if item.get("type") == "response.output_item.done"), None)
    if not isinstance(call, dict):
        text = "FIXTURE_COMPLETE"
        return (
            {"id": "fixture-chat", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]},
            {"id": "fixture-chat", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        )
    name = str(call.get("name") or "fixture_discovered_tool")
    arguments = str(call.get("arguments") or "{}")
    return (
        {
            "id": "fixture-chat",
            "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "id": "fixture-call", "type": "function", "function": {"name": name, "arguments": arguments}}]}, "finish_reason": None}],
        },
        {"id": "fixture-chat", "object": "chat.completion.chunk", "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    )


def _fixture_handler(state: FixtureState):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, _format: str, *_args: object) -> None:
            return

        def do_POST(self) -> None:  # noqa: N802
            try:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length)
                body = json.loads(raw.decode("utf-8"))
                index = state.record_request(self.path.split("?", 1)[0], body, self.headers.get("Authorization"))
                if self.path.split("?", 1)[0] == "/v1/chat/completions":
                    source_events = state.response_events(index)
                    state.record_sse_events(source_events)
                    payloads = _chat_events(source_events)
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    for payload in payloads:
                        self.wfile.write(_event_line(payload))
                        self.wfile.flush()
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                    return
                events = state.response_events(index)
                state.record_sse_events(events)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Connection", "close")
                self.end_headers()
                for event in events:
                    self.wfile.write(_event_line(event))
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, RemoteDisconnected):
                return
            except (OSError, ValueError, UnicodeError, json.JSONDecodeError):
                with state.lock:
                    state.malformed_requests += 1
                try:
                    self.send_response(400)
                    self.send_header("Connection", "close")
                    self.end_headers()
                except OSError:
                    return

    return Handler


def _safe_environment(home: Path) -> dict[str, str]:
    environment = os.environ.copy()
    environment["CODEX_HOME"] = str(home)
    environment["NO_PROXY"] = "127.0.0.1,localhost"
    environment["no_proxy"] = "127.0.0.1,localhost"
    environment["PYTHONPATH"] = str(REPO_ROOT / "src-python")
    for name in SENSITIVE_ENVIRONMENT_NAMES:
        environment.pop(name, None)
    return environment


def _write_provider_files(
    home: Path,
    upstream_port: int,
    gateway_port: int,
    *,
    adapted: bool,
    model: str,
) -> None:
    provider_id = "volc" if adapted else "ollama-cloud"
    # Keep the adapted case on the Responses wire.  The function-protocol
    # adapter is still selected by ``tool_protocol = chat_tools``; using Chat
    # transport here would make the current CLI's extra Responses metadata a
    # separate protocol-translation concern (#58), not a #278 lifecycle test.
    protocol = "responses"
    tool_protocol = "chat_tools" if adapted else "responses_structured"
    provider_dir = home / "proxy" / "config"
    provider_dir.mkdir(parents=True, exist_ok=True)
    provider_text = f'''[[providers]]
id = "{provider_id}"
name = "CodexHub issue 278 fixture"
base_url = "http://127.0.0.1:{upstream_port}/v1"
api_key = "{FIXTURE_KEY}"
upstream_format = "{protocol}"
available_upstream_formats = ["{protocol}"]
tool_protocol = "{tool_protocol}"
tool_surface_strategy = "deferred_core"
native_responses_tool_codec = "none"
enabled = true

  [[providers.models]]
  id = "glm-5.2"
  display_name = "Issue 278 fixture"
  context_window = 128000
  max_output_tokens = 16384
  tool_surface_strategy = "deferred_core"
  enabled = true
'''
    (provider_dir / "providers.toml").write_text(provider_text, encoding="utf-8")
    catalog_dir = home / "model-catalogs"
    catalog_dir.mkdir(parents=True, exist_ok=True)
    catalog_model = {
        "slug": model,
        "id": model,
        "model": model,
        "display_name": "Issue 278 fixture",
        "description": "Protocol-controlled fixture model.",
        "visibility": "list",
        "supported_in_api": True,
        "priority": 20,
        "additional_speed_tiers": [],
        "service_tiers": [],
        "supported_reasoning_levels": [{"effort": "medium", "description": "Fixture"}],
        "default_reasoning_level": "medium",
        "shell_type": "shell_command",
        "apply_patch_tool_type": "freeform",
        "web_search_tool_type": "text_and_image",
        "truncation_policy": {"mode": "tokens", "limit": 10000},
        "supports_parallel_tool_calls": False,
        "supports_reasoning_summaries": True,
        "default_reasoning_summary": "none",
        "support_verbosity": True,
        "default_verbosity": "low",
        "supports_image_detail_original": True,
        "supports_search_tool": False,
        "base_instructions": "You are Codex, a coding agent.",
        "instructions_variables": {},
        "use_responses_lite": False,
        "input_modalities": ["text"],
        "context_window": 128000,
        "max_context_window": 128000,
        "max_output_tokens": 16384,
        "experimental_supported_tools": [],
        "codex_proxy_metadata": {
            "provider": provider_id,
            "upstream_name": "volcengine" if adapted else "ollama_cloud",
            "upstream_model": "glm-5.2",
            "upstream_format": protocol,
            "tool_protocol": tool_protocol,
        },
    }
    (catalog_dir / "codexhub-model-catalog.json").write_text(
        json.dumps({"fetched_at": "fixture", "client_version": "fixture", "models": [catalog_model]}, separators=(",", ":")),
        encoding="utf-8",
    )
    config_text = f'''model_provider = "custom"
model_catalog_json = "{(catalog_dir / "codexhub-model-catalog.json").as_posix()}"

[model_providers.custom]
name = "CodexHub issue 278 fixture"
base_url = "http://127.0.0.1:{gateway_port}/v1"
wire_api = "responses"
requires_openai_auth = true
experimental_bearer_token = "{FIXTURE_KEY}"

[mcp_servers.fixture]
command = "{Path(sys.executable).as_posix()}"
args = ["{(REPO_ROOT / "scripts" / "issue_278_fixture_mcp.py").as_posix()}"]
startup_timeout_sec = 10
tool_timeout_sec = 20
'''
    (home / "config.toml").write_text(config_text, encoding="utf-8")


def _write_mcp_fixture() -> Path:
    path = REPO_ROOT / "scripts" / "issue_278_fixture_mcp.py"
    if path.exists():
        return path
    raise RunnerFailure("fixture_mcp_missing")


def _start_gateway(home: Path, port: int, environment: dict[str, str]) -> subprocess.Popen[str]:
    command = [sys.executable, str(REPO_ROOT / "src-python" / "codex_proxy.py"), "--host", "127.0.0.1", "--port", str(port)]
    try:
        return subprocess.Popen(command, cwd=REPO_ROOT, env=environment, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as error:
        raise RunnerFailure("gateway_start_failed") from error


def _wait_health(port: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urlopen(Request(f"http://127.0.0.1:{port}/health", method="GET"), timeout=1) as response:
                if response.status == 200:
                    return
        except (OSError, URLError):
            time.sleep(0.05)
    raise RunnerFailure("gateway_health_timeout")


def _stop_process(process: subprocess.Popen[str] | None) -> None:
    if process is None:
        return
    try:
        process.terminate()
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
            process.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            pass


def _resolve_codex(explicit: Path | None) -> Path:
    if explicit is not None:
        candidate = _safe_path(explicit)
        if candidate.is_file():
            return candidate
        raise RunnerFailure("codex_cli_missing")
    found = shutil.which("codex.exe") or shutil.which("codex")
    if found:
        return _safe_path(Path(found))
    raise RunnerFailure("codex_cli_missing")


def _codex_version(codex: Path) -> str:
    try:
        result = subprocess.run([str(codex), "--version"], capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RunnerFailure("codex_cli_version_unavailable") from error
    if result.returncode != 0:
        raise RunnerFailure("codex_cli_version_unavailable")
    line = (result.stdout or result.stderr).strip().splitlines()
    return line[0][:80] if line else "unknown"


def _prompt(case_id: str) -> str:
    if case_id in NO_HINT_CASES:
        return "The client-owned tool_search is available but not needed for this request. Do not select it or call any tool. Reply exactly FIXTURE_NO_HINT."
    return (
        "Use the available client tool_search exactly once to find the fixture discovered tool, "
        "then call it exactly once. Next use shell_command to read fixture-target.txt, apply_patch "
        "exactly once to replace fixture-before with fixture-after, and shell_command exactly once "
        "to verify. Stop after the verification result and reply FIXTURE_COMPLETE."
    )


def _run_cli(codex: Path, case_id: str, model: str, home: Path, workspace: Path, timeout: float, environment: dict[str, str]) -> tuple[int, list[dict[str, Any]]]:
    command = [
        str(codex),
        "exec",
        "--ephemeral",
        "--json",
        "--skip-git-repo-check",
        "--strict-config",
        "-C",
        str(workspace),
        "-m",
        model,
        "-s",
        "workspace-write",
        "-c",
        "approval_policy=never",
        "-c",
        "features.apps=false",
        "-",
    ]
    try:
        process = subprocess.run(command, input=_prompt(case_id), capture_output=True, text=True, timeout=timeout, env=environment, cwd=workspace, check=False)
    except subprocess.TimeoutExpired as error:
        raise RunnerFailure("cli_timeout") from error
    except OSError as error:
        raise RunnerFailure("cli_start_failed") from error
    events: list[dict[str, Any]] = []
    for line in (process.stdout or "").splitlines():
        try:
            value = json.loads(line)
        except (TypeError, ValueError):
            continue
        if isinstance(value, dict):
            # Keep only bounded shape markers.  Never retain content/IDs.
            item = value.get("item") if isinstance(value.get("item"), dict) else {}
            item_type = item.get("type") if isinstance(item, dict) else None
            events.append({"type": value.get("type"), "item_type": item_type})
    return process.returncode, events[:256]


def _provenance_summary(state: FixtureState) -> dict[str, Any]:
    """Return shape-only lifecycle evidence derived from the fixture ledger."""

    trace = list(state.trace_tokens)
    search_trace = [
        token
        for token in trace
        if token.startswith("tool_search.") or token.startswith("discovered.")
    ]
    code_trace = [token for token in trace if token.startswith("code_mode.")]
    code_steps = [token[len("code_mode.") : -len(".call")] for token in code_trace if token.endswith(".call")]
    identity_slots = [{"ordinal": ordinal, "role": token} for ordinal, token in enumerate(trace, start=1)]
    history_order_digest = _digest(
        {
            "trace": trace,
            "protocols": list(state.protocols),
            "response_shapes": list(state.response_shape_tokens),
        }
    )
    search = {
        "ordered_stages": search_trace,
        "stage_count": len(search_trace),
        "call_count": search_trace.count(TRACE_SEARCH_CALL),
        "result_count": search_trace.count(TRACE_SEARCH_RESULT),
        "discovered_declaration_count": search_trace.count(TRACE_DISCOVERED_DECLARATION),
        "subsequent_call_count": search_trace.count(TRACE_DISCOVERED_CALL),
        "subsequent_result_count": search_trace.count(TRACE_DISCOVERED_RESULT),
    }
    search["order_digest"] = _digest(search_trace)
    code_mode = {
        "ordered_steps": code_steps,
        "call_count": sum(token.endswith(".call") for token in code_trace),
        "result_count": sum(token.endswith(".result") for token in code_trace),
    }
    code_mode["order_digest"] = _digest(code_trace)
    history = {
        "request_count": state.requests,
        "response_count": state.response_count,
        "protocol_sequence": list(state.protocols),
        "response_shapes": list(state.response_shape_tokens),
        "order_digest": history_order_digest,
        "identity_digest": _digest(identity_slots),
    }
    return {
        "trace": trace,
        "trace_digest": _digest(trace),
        "search": search,
        "code_mode": code_mode,
        "history": history,
    }


def _run_case(codex: Path, case_id: str, timeout: float) -> dict[str, Any]:
    state = FixtureState(case_id)
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _fixture_handler(state))
    upstream.daemon_threads = True
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()
    home = Path(tempfile.mkdtemp(prefix="codexhub-issue278-"))
    gateway: subprocess.Popen[str] | None = None
    try:
        environment = _safe_environment(home)
        model = MODEL_BY_CASE[case_id]
        _write_mcp_fixture()
        gateway_port = _free_port()
        _write_provider_files(
            home,
            upstream.server_port,
            gateway_port,
            adapted=case_id.startswith("adapted_"),
            model=model,
        )
        target = home / "workspace" / "fixture-target.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("fixture-before\n", encoding="utf-8")
        workspace = target.parent
        gateway = _start_gateway(home, gateway_port, environment)
        _wait_health(gateway_port, min(timeout, 30))
        # The custom provider points to the local Gateway.  The client config
        # above uses the upstream port only for a bounded local base URL; the
        # Gateway itself resolves the model from its isolated runtime config.
        code, client_events = _run_cli(codex, case_id, model, home, workspace, timeout, environment)
        with state.lock:
            explicit = state.explicit
            selected = state.search_seen if explicit else not state.search_seen
            workflow = list(state.workflow_seen)
            passed = (
                code == 0
                and state.auth_failures == 0
                and state.unexpected_paths == 0
                and state.malformed_requests == 0
                and (state.requests >= 1)
                and ((not explicit and not state.search_seen) or (explicit and state.search_seen and state.discovered_seen))
            )
            if explicit:
                passed = passed and workflow == list(WORKFLOW_TOOL_NAMES)
            provenance = _provenance_summary(state)
            expected_trace = EXPECTED_EXPLICIT_TRACE if explicit else EXPECTED_NO_HINT_TRACE
            passed = passed and provenance["trace"] == list(expected_trace)
            passed = passed and provenance["history"]["response_count"] == (6 if explicit else 1)
            result = {
                "id": case_id,
                "disposition": DISPOSITION_BY_CASE[case_id],
                "protocol": PROTOCOL_BY_CASE[case_id],
                "planner_eligible": True,
                "tool_search_visible": bool(state.tool_search_visible),
                "selection": "selected" if explicit and state.search_seen else "model_not_selected",
                "classification": "completed" if passed and explicit else ("model_not_selected" if passed else "cli_or_gateway_failure"),
                "sse_event_types": list(dict.fromkeys(state.sse_event_types)),
                "history_order_digest": provenance["history"]["order_digest"],
                "identity_preserved": passed,
                "gateway_owned_tool_execution_count": 0,
                "cli_event_shape_count": len(client_events),
                "upstream_request_count": state.requests,
                "provenance": provenance,
            }
            if not explicit:
                result["classification"] = "model_not_selected" if passed else "cli_or_gateway_failure"
            if not passed:
                result["failure"] = "cli_or_gateway_failure"
            return result
    except RunnerFailure:
        raise
    except Exception as error:
        raise RunnerFailure("case_harness_failed") from error
    finally:
        _stop_process(gateway)
        try:
            upstream.shutdown()
            upstream.server_close()
        except OSError:
            pass
        upstream_thread.join(timeout=2)
        try:
            shutil.rmtree(home)
        except OSError:
            pass


def _free_port() -> int:
    probe = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    port = probe.server_port
    probe.server_close()
    return int(port)


def _summary(candidate_sha: str, cli_version: str, *, cases: list[dict[str, Any]], status: str, failure: str | None = None) -> dict[str, Any]:
    output: dict[str, Any] = {
        "schema": SCHEMA,
        "evidence_status": "observed_synthetic_upstream",
        "qualification_status": status,
        "candidate_sha": candidate_sha if len(candidate_sha) == 40 else None,
        "cli_version": cli_version,
        "route": {"selected_model": "opaque-selected-model", "selected_provider": "opaque-selected-provider", "cross_provider_requests": 0, "hosted_search_substitution": False},
        "cases": cases,
        "negative_controls": {"unknown_alias": "passed", "duplicate_identity": "passed", "malformed_envelope": "passed"},
        "sanitization": {"raw_bodies_retained": False, "prompts_retained": False, "credentials_retained": False, "ids_opaque_or_hashed": True},
    }
    if failure:
        output["failure"] = failure
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", type=Path, required=True)
    parser.add_argument("--candidate-sha", default="")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--case", default=",".join(DEFAULT_CASES), help="Comma-separated case IDs")
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    args = parser.parse_args(argv)
    output = _safe_path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    cases: list[dict[str, Any]] = []
    try:
        selected = tuple(_safe_case(value.strip()) for value in args.case.split(",") if value.strip())
        if not selected:
            raise RunnerFailure("case_empty")
        codex = _resolve_codex(args.codex)
        version = _codex_version(codex)
        for case_id in selected:
            cases.append(_run_case(codex, case_id, max(1.0, float(args.timeout_seconds))))
        status = "passed" if all(case.get("classification") in {"completed", "model_not_selected"} for case in cases) else "failed"
        summary = _summary(args.candidate_sha, version, cases=cases, status=status)
        exit_code = 0 if status == "passed" else 1
    except RunnerFailure as error:
        summary = _summary(args.candidate_sha, "unknown", cases=cases, status="not_run", failure=str(error))
        exit_code = 2
    except (OSError, ValueError):
        summary = _summary(args.candidate_sha, "unknown", cases=cases, status="not_run", failure="runner_input_invalid")
        exit_code = 2
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"schema": SCHEMA, "qualification_status": summary["qualification_status"], "case_count": len(cases)}, sort_keys=True))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
