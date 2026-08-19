#!/usr/bin/env python3
"""Issue #395: real Codex CLI Collaboration V2 through a Chat fixture.

The real CLI speaks Responses to the Gateway. The protocol-controlled upstream
accepts only Chat Completions, records bounded wire-shape observations, and
drives the six-tool V2 lifecycle without executing or scheduling agents.
"""

from __future__ import annotations

try:
    from scripts.python_runtime_contract import require_python_313
except ModuleNotFoundError:
    from python_runtime_contract import require_python_313

require_python_313(__file__)

import argparse
from collections.abc import Mapping
import json
from http.server import BaseHTTPRequestHandler
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from typing import Any

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
SRC_ROOT = REPO_ROOT / "src-python"
sys.path.insert(0, str(SCRIPT_DIR))
sys.path.insert(0, str(SRC_ROOT))

import run_issue_283_cli_v2_lifecycle as track_a
from collaboration_runtime_contract import V2_TOOLS

SCHEMA = "codexhub.issue395.cli-chat-v2-lifecycle.v1"
DEFAULT_OUTPUT_DIR = Path("docs/evidence/issue-395/cli-chat")
AGENT_MESSAGE_PREFIX = "__codexhub_agent_message_v2__:"
ALIAS_PREFIX = "__codexhub_ns_"


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise track_a.CaptureFailure(code)


def _chat_aliases(body: Mapping[str, Any]) -> tuple[dict[str, str], dict[str, str]]:
    declarations = body.get("tools")
    functions: list[str] = []
    if isinstance(declarations, list):
        for declaration in declarations:
            if not isinstance(declaration, Mapping) or declaration.get("type") != "function":
                continue
            function = declaration.get("function")
            name = function.get("name") if isinstance(function, Mapping) else None
            if isinstance(name, str) and name.startswith(ALIAS_PREFIX):
                functions.append(name)
    if functions:
        _require(len(functions) == len(V2_TOOLS), "chat_v2_alias_count_invalid")
    original_to_alias = dict(zip(V2_TOOLS, functions, strict=True)) if functions else {}
    return original_to_alias, {alias: name for name, alias in original_to_alias.items()}


def _message_text(message: Mapping[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(part.get("text", ""))
        for part in content
        if isinstance(part, Mapping) and part.get("type") in {"text", "input_text", "output_text"}
    )


def _decode_agent_message(text: str) -> dict[str, Any] | None:
    if not text.startswith(AGENT_MESSAGE_PREFIX):
        return None
    try:
        item = json.loads(text.removeprefix(AGENT_MESSAGE_PREFIX))
    except json.JSONDecodeError as exc:
        raise track_a.CaptureFailure("chat_agent_message_envelope_invalid") from exc
    _require(isinstance(item, dict), "chat_agent_message_envelope_invalid")
    _require(item.get("type") == "agent_message", "chat_agent_message_type_invalid")
    return item


def _chat_to_fixture_request(
    body: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, str], bool]:
    original_to_alias, alias_to_original = _chat_aliases(body)
    items: list[dict[str, Any]] = []
    child_task: str | None = None
    messages = body.get("messages")
    _require(isinstance(messages, list), "chat_messages_invalid")
    for index, message in enumerate(messages):
        _require(isinstance(message, Mapping), "chat_message_invalid")
        role = message.get("role")
        if role == "assistant" and isinstance(message.get("tool_calls"), list):
            for call in message["tool_calls"]:
                _require(isinstance(call, Mapping), "chat_tool_call_invalid")
                function = call.get("function")
                _require(isinstance(function, Mapping), "chat_tool_call_invalid")
                alias = function.get("name")
                call_id = call.get("id")
                _require(isinstance(alias, str), "chat_tool_alias_missing")
                _require(alias in alias_to_original, "chat_tool_alias_unknown")
                _require(isinstance(call_id, str) and bool(call_id), "chat_tool_call_id_missing")
                items.append(
                    {
                        "type": "function_call",
                        "id": f"fc_{call_id}",
                        "namespace": track_a.V2_NAMESPACE,
                        "name": alias_to_original[alias],
                        "call_id": call_id,
                        "arguments": function.get("arguments", ""),
                    }
                )
            continue
        if role == "tool":
            call_id = message.get("tool_call_id")
            _require(isinstance(call_id, str) and bool(call_id), "chat_tool_result_id_missing")
            items.append(
                {
                    "type": "function_call_output",
                    "id": f"fco_{call_id}",
                    "call_id": call_id,
                    "output": _message_text(message),
                }
            )
            continue
        text = _message_text(message)
        agent_message = _decode_agent_message(text)
        if agent_message is not None:
            items.append(agent_message)
            if "Message Type: NEW_TASK" in track_a._agent_message_text(agent_message):
                recipient = agent_message.get("recipient")
                if isinstance(recipient, str) and recipient:
                    child_task = recipient
            continue
        items.append(
            {
                "type": "message",
                "id": f"message_{index}",
                "role": role if role in {"system", "developer", "user", "assistant"} else "user",
                "content": [{"type": "input_text", "text": text}],
            }
        )

    synthetic_thread = f"child:{child_task}" if child_task else "root"
    fixture_request = {
        "model": body.get("model") or track_a.MODEL,
        "input": items,
        "tools": (
            [
                {
                    "type": "namespace",
                    "name": track_a.V2_NAMESPACE,
                    "version": "2",
                    "tools": [{"name": name} for name in V2_TOOLS],
                }
            ]
            if original_to_alias
            else []
        ),
        "client_metadata": {"thread_id": synthetic_thread},
    }
    return fixture_request, original_to_alias, child_task is not None


def _events_to_chat_chunks(
    events: list[dict[str, Any]], original_to_alias: Mapping[str, str], model: str
) -> list[dict[str, Any]]:
    done_item: Mapping[str, Any] | None = None
    for event in events:
        if event.get("type") == "response.output_item.done" and isinstance(event.get("item"), Mapping):
            done_item = event["item"]
    _require(done_item is not None, "fixture_response_item_missing")
    if done_item.get("type") == "function_call":
        name = done_item.get("name")
        _require(isinstance(name, str) and name in original_to_alias, "fixture_alias_unavailable")
        call_id = done_item.get("call_id")
        arguments = done_item.get("arguments")
        _require(isinstance(call_id, str) and bool(call_id), "fixture_call_id_missing")
        _require(isinstance(arguments, str), "fixture_arguments_invalid")
        split = max(1, len(arguments) // 2)
        return [
            {
                "id": f"chatcmpl_{call_id}",
                "object": "chat.completion.chunk",
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": call_id,
                                    "type": "function",
                                    "function": {
                                        "name": original_to_alias[name],
                                        "arguments": arguments[:split],
                                    },
                                }
                            ],
                        },
                        "finish_reason": None,
                    }
                ],
            },
            {
                "id": f"chatcmpl_{call_id}",
                "object": "chat.completion.chunk",
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "function": {"arguments": arguments[split:]},
                                }
                            ]
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
            },
        ]

    content = done_item.get("content")
    text = ""
    if isinstance(content, list):
        text = "".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, Mapping) and part.get("type") == "output_text"
        )
    split = max(1, len(text) // 2)
    return [
        {
            "id": "chatcmpl_message",
            "object": "chat.completion.chunk",
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": text[:split]},
                    "finish_reason": None,
                }
            ],
        },
        {
            "id": "chatcmpl_message",
            "object": "chat.completion.chunk",
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": text[split:]},
                    "finish_reason": "stop",
                }
            ],
        },
    ]


class _ChatFixtureServer(track_a._FixtureServer):
    # The parent deliberately calls wait_agent(timeout_ms=30000). Keep the
    # child HTTP turn open beyond that boundary so interrupt_agent, not a
    # fixture timeout race, owns the terminal transition.
    CHILD_WAIT_TIMEOUT = 90.0

    def __init__(self) -> None:
        super().__init__()
        self.RequestHandlerClass = _ChatFixtureHandler
        self.wire_observations: list[dict[str, Any]] = []

    def child_events(self, body: dict[str, Any], model: str) -> list[dict[str, Any]]:
        thread_id = str((body.get("client_metadata") or {}).get("thread_id", ""))
        task_name = self._child_task_name(body)
        _require(bool(thread_id) and bool(task_name), "chat_child_identity_missing")
        with self.lock:
            self.requests.append(body)
            index = len(self.requests)
            state = self.child_loops.get(thread_id)
        if state is None:
            state = self._register_child(thread_id, str(task_name))
        deadline = time.monotonic() + self.CHILD_WAIT_TIMEOUT
        final_payload = "interrupted"
        while time.monotonic() < deadline:
            state["event"].wait(timeout=max(0.0, deadline - time.monotonic()))
            state["event"].clear()
            with self.lock:
                queued = list(state["queue"])
                state["queue"].clear()
            signals = self._classify_child_signals(state, queued)
            if signals["final_payload"] is not None:
                final_payload = str(signals["final_payload"])
                break
            if signals["interrupted"]:
                break
        events = track_a._message_events(
            index,
            self._child_text(state, final_payload, final=True),
            model,
        )
        state["completed"] = True
        with self.lock:
            self.responses.append(events)
        return events


class _ChatFixtureHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args: Any) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        if self.path != "/v1/models":
            self.send_error(404)
            return
        payload = {
            "object": "list",
            "data": [{"id": track_a.MODEL, "object": "model", "owned_by": "fixture"}],
        }
        encoded = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(encoded)

    def do_POST(self) -> None:  # noqa: N802
        server = self.server
        _require(isinstance(server, _ChatFixtureServer), "chat_fixture_server_invalid")
        if self.path != "/v1/chat/completions":
            self.send_error(404)
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(size))
            _require(isinstance(body, dict), "chat_request_invalid")
            fixture_request, aliases, is_child = _chat_to_fixture_request(body)
            model = str(body.get("model") or track_a.MODEL)
            if is_child:
                events = server.child_events(fixture_request, model)
            else:
                events = server.events_for(fixture_request)
            chunks = _events_to_chat_chunks(events, aliases, model)
        except track_a.CaptureFailure as exc:
            encoded = json.dumps({"error": {"code": str(exc)}}).encode()
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)
            return

        with server.lock:
            server.wire_observations.append(
                {
                    "alias_count": len(aliases),
                    "message_count": len(body.get("messages", [])),
                    "is_child": is_child,
                    "chunk_count": len(chunks),
                    "tool_call": any(
                        chunk.get("choices", [{}])[0].get("finish_reason") == "tool_calls"
                        for chunk in chunks
                    ),
                }
            )
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        for chunk in chunks:
            self.wfile.write(
                b"data: "
                + json.dumps(chunk, separators=(",", ":")).encode()
                + b"\n\n"
            )
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()


class ChatFixtureServer:
    last_server: _ChatFixtureServer | None = None

    def __init__(self) -> None:
        self._server = _ChatFixtureServer()
        ChatFixtureServer.last_server = self._server
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return int(self._server.server_address[1])

    def __enter__(self) -> "ChatFixtureServer":
        self._thread.start()
        return self

    def __exit__(self, *_args: Any) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def _write_chat_providers_toml(home: Path, fixture_port: int) -> None:
    config_dir = home / "proxy" / "config"
    config_dir.mkdir(parents=True)
    (config_dir / "providers.toml").write_text(
        f'''[[providers]]
id = "{track_a.FIXTURE_PROVIDER_ID}"
name = "Chat Fixture"
base_url = "http://127.0.0.1:{fixture_port}/v1"
api_key = "fixture-key"
upstream_format = "chat_completions"
available_upstream_formats = ["chat_completions"]
tool_protocol = "chat_tools"
display_prefix = "Chat Fixture"
sort_order = 1
enabled = true

[providers.tool_protocol_capabilities]
namespace_lifecycle = false
function_lifecycle = true
custom_lifecycle = false
tool_search_lifecycle = false
accepts_namespace_adapter = true

  [[providers.models]]
  id = "{track_a.MODEL}"
  display_name = "Chat Fixture V2"
  context_window = 128000
  max_output_tokens = 8192
  sort_order = 1
  enabled = true
''',
        encoding="utf-8",
        newline="\n",
    )


def capture(
    *, output_dir: Path, candidate_sha: str, cli_version: str, debug_capture_path: Path | None
) -> dict[str, Any]:
    original_fixture = track_a.FixtureServer
    original_writer = track_a._write_providers_toml
    original_schema = track_a.SCHEMA
    try:
        track_a.FixtureServer = ChatFixtureServer
        track_a._write_providers_toml = _write_chat_providers_toml
        track_a.SCHEMA = SCHEMA
        result = track_a.capture(
            output_dir=output_dir,
            candidate_sha=candidate_sha,
            cli_version=cli_version,
            debug_capture_path=debug_capture_path,
            same_home_restart=True,
        )
    finally:
        track_a.FixtureServer = original_fixture
        track_a._write_providers_toml = original_writer
        track_a.SCHEMA = original_schema

    server = ChatFixtureServer.last_server
    observations = list(server.wire_observations) if server is not None else []
    result["schema"] = SCHEMA
    result["route"]["upstream_format"] = "chat_completions"
    result["chat_wire_summary"] = {
        "request_count": len(observations),
        "root_request_count": sum(not item["is_child"] for item in observations),
        "child_request_count": sum(item["is_child"] for item in observations),
        "six_alias_requests": sum(item["alias_count"] == len(V2_TOOLS) for item in observations),
        "progressive_two_chunk_responses": sum(item["chunk_count"] == 2 for item in observations),
        "tool_call_responses": sum(item["tool_call"] for item in observations),
        "raw_aliases_retained": False,
        "raw_messages_retained": False,
    }
    result["root_cause_verdict"] = (
        "chat_v2_lifecycle_verified"
        if result["qualification_status"] == "passed"
        else "chat_v2_lifecycle_in_progress"
    )
    result["root_cause_details"] = {
        "fixture_boundary": "Codex CLI -> Gateway Responses; Gateway -> fixture Chat Completions",
        "alias_mapping": "Six request-scoped aliases are mapped by the frozen V2 declaration order; unknown aliases fail closed.",
        "agent_message": "Plaintext request-bound envelopes identify child NEW_TASK handoff; the fixture never emits envelopes.",
        "child_execution": "The fixture holds the child HTTP turn open; the Codex client owns agent creation, messaging, and interruption.",
    }
    result["shim_notes"] = [
        "The CLI-facing shim only supplies /v1/models and the accepted optional agent_type declaration field.",
        "The upstream fixture accepts only /v1/chat/completions and emits split Chat tool-call argument chunks.",
    ]
    (output_dir / "summary.json").write_text(
        json.dumps(result, ensure_ascii=True, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--candidate-sha")
    parser.add_argument("--debug-capture", type=Path)
    args = parser.parse_args(argv)
    candidate_sha = args.candidate_sha
    if candidate_sha is None:
        candidate_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, check=False
        ).stdout.strip()
    cli_version = "unknown"
    codex_executable = shutil.which("codex")
    if codex_executable:
        cli_version = subprocess.run(
            [str(Path(codex_executable).resolve()), "--version"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
    try:
        result = capture(
            output_dir=args.output_dir,
            candidate_sha=candidate_sha,
            cli_version=cli_version,
            debug_capture_path=args.debug_capture,
        )
    except track_a.CaptureFailure as exc:
        print(f"CAPTURE_FAILED:{exc}", file=sys.stderr)
        return 2
    print(json.dumps({"schema": SCHEMA, "qualification_status": result["qualification_status"]}))
    return 0 if result["qualification_status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
