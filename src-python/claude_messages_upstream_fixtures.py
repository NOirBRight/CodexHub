"""Deterministic loopback upstream fixtures for the #74 evidence seam.

These fixtures never call a model.  They expose the three wire formats needed by
an authorized runner while recording only request shape, not body content.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

__all__ = ["UpstreamFixtureServer", "fixture_response"]


def _sse(event: str | None, payload: Any) -> bytes:
    encoded = payload if isinstance(payload, str) else json.dumps(payload, separators=(",", ":"))
    prefix = f"event: {event}\n" if event else ""
    return f"{prefix}data: {encoded}\n\n".encode()


def _json(status: int, payload: dict[str, Any]) -> tuple[int, str, bytes]:
    return status, "application/json", json.dumps(payload, separators=(",", ":")).encode()


def _responses(*, scenario: str, stream: bool) -> tuple[int, str, bytes]:
    if scenario == "error":
        return _json(503, {"error": {"type": "server_error", "message": "synthetic upstream failure"}})
    if scenario == "tool":
        item = {
            "id": "fc_fixture_item",
            "type": "function_call",
            "status": "completed",
            "call_id": "call_fixture_1",
            "name": "Read",
            "arguments": '{"path":"fixture.txt"}',
        }
        if stream:
            events = [
                {"type": "response.created", "response": {"id": "resp_fixture", "model": "fixture-responses", "status": "in_progress"}},
                {"type": "response.output_item.added", "output_index": 0, "item": {**item, "status": "in_progress", "arguments": ""}},
                {"type": "response.function_call_arguments.delta", "output_index": 0, "item_id": item["id"], "delta": '{"path":'},
                {"type": "response.function_call_arguments.delta", "output_index": 0, "item_id": item["id"], "delta": '"fixture.txt"}'},
                {"type": "response.output_item.done", "output_index": 0, "item": item},
                {"type": "response.completed", "response": {"id": "resp_fixture", "model": "fixture-responses", "status": "completed", "output": [item], "usage": {"input_tokens": 8, "output_tokens": 4}}},
            ]
            return 200, "text/event-stream", b"".join(_sse(None, event) for event in events)
        return _json(200, {"id": "resp_fixture", "object": "response", "status": "completed", "model": "fixture-responses", "output": [item], "usage": {"input_tokens": 8, "output_tokens": 4}})
    message = {
        "id": "msg_fixture",
        "type": "message",
        "status": "completed",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "loopback response", "annotations": []}],
    }
    if stream:
        events = [
            {"type": "response.created", "response": {"id": "resp_fixture", "model": "fixture-responses", "status": "in_progress"}},
            {"type": "response.output_item.added", "output_index": 0, "item": {**message, "status": "in_progress", "content": []}},
            {"type": "response.output_text.delta", "output_index": 0, "item_id": message["id"], "delta": "loopback "},
            {"type": "response.output_text.delta", "output_index": 0, "item_id": message["id"], "delta": "response"},
            {"type": "response.output_item.done", "output_index": 0, "item": message},
            {"type": "response.completed", "response": {"id": "resp_fixture", "model": "fixture-responses", "status": "completed", "output": [message], "usage": {"input_tokens": 7, "output_tokens": 3}}},
        ]
        return 200, "text/event-stream", b"".join(_sse(None, event) for event in events)
    return _json(200, {"id": "resp_fixture", "object": "response", "status": "completed", "model": "fixture-responses", "output": [message], "usage": {"input_tokens": 7, "output_tokens": 3}})


def _chat(*, scenario: str, stream: bool) -> tuple[int, str, bytes]:
    if scenario == "error":
        payload = {"error": {"type": "server_error", "message": "synthetic upstream failure"}}
        return (503, "text/event-stream", _sse(None, payload)) if stream else _json(503, payload)
    if scenario == "tool":
        chunks = [
            {"id": "chat_fixture", "object": "chat.completion.chunk", "model": "fixture-chat", "choices": [{"index": 0, "delta": {"role": "assistant", "tool_calls": [{"index": 0, "id": "call_fixture_1", "type": "function", "function": {"name": "Read", "arguments": '{"path":'}}]}, "finish_reason": None}]},
            {"id": "chat_fixture", "object": "chat.completion.chunk", "model": "fixture-chat", "choices": [{"index": 0, "delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"fixture.txt"}'}}]}, "finish_reason": None}]},
            {"id": "chat_fixture", "object": "chat.completion.chunk", "model": "fixture-chat", "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
            {"id": "chat_fixture", "object": "chat.completion.chunk", "model": "fixture-chat", "choices": [], "usage": {"prompt_tokens": 8, "completion_tokens": 4}},
        ]
        if stream:
            return 200, "text/event-stream", b"".join(_sse(None, chunk) for chunk in chunks) + b"data: [DONE]\n\n"
        return _json(200, {"id": "chat_fixture", "object": "chat.completion", "model": "fixture-chat", "choices": [{"index": 0, "message": {"role": "assistant", "content": None, "tool_calls": [{"id": "call_fixture_1", "type": "function", "function": {"name": "Read", "arguments": '{"path":"fixture.txt"}'}}]}, "finish_reason": "tool_calls"}], "usage": {"prompt_tokens": 8, "completion_tokens": 4}})
    chunks = [
        {"id": "chat_fixture", "object": "chat.completion.chunk", "model": "fixture-chat", "choices": [{"index": 0, "delta": {"role": "assistant", "content": "loopback "}, "finish_reason": None}]},
        {"id": "chat_fixture", "object": "chat.completion.chunk", "model": "fixture-chat", "choices": [{"index": 0, "delta": {"content": "response"}, "finish_reason": None}]},
        {"id": "chat_fixture", "object": "chat.completion.chunk", "model": "fixture-chat", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        {"id": "chat_fixture", "object": "chat.completion.chunk", "model": "fixture-chat", "choices": [], "usage": {"prompt_tokens": 7, "completion_tokens": 3}},
    ]
    if stream:
        return 200, "text/event-stream", b"".join(_sse(None, chunk) for chunk in chunks) + b"data: [DONE]\n\n"
    return _json(200, {"id": "chat_fixture", "object": "chat.completion", "model": "fixture-chat", "choices": [{"index": 0, "message": {"role": "assistant", "content": "loopback response"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 7, "completion_tokens": 3}})


def _anthropic(*, scenario: str, stream: bool) -> tuple[int, str, bytes]:
    if scenario == "error":
        payload = {"type": "error", "error": {"type": "api_error", "message": "synthetic upstream failure"}}
        return (503, "text/event-stream", _sse("error", payload)) if stream else _json(503, payload)
    if scenario == "tool":
        content = [{"type": "tool_use", "id": "call_fixture_1", "name": "Read", "input": {"path": "fixture.txt"}}]
        events = [
            ("message_start", {"type": "message_start", "message": {"id": "msg_fixture", "type": "message", "role": "assistant", "model": "fixture-anthropic", "content": [], "usage": {"input_tokens": 8, "output_tokens": 1}}}),
            ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use", "id": "call_fixture_1", "name": "Read", "input": {}}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": '{"path":"fixture.txt"}'}}),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "tool_use", "stop_sequence": None}, "usage": {"output_tokens": 4}}),
            ("message_stop", {"type": "message_stop"}),
        ]
        if stream:
            return 200, "text/event-stream", b"".join(_sse(name, event) for name, event in events)
        return _json(200, {"id": "msg_fixture", "type": "message", "role": "assistant", "model": "fixture-anthropic", "content": content, "stop_reason": "tool_use", "stop_sequence": None, "usage": {"input_tokens": 8, "output_tokens": 4}})
    events = [
        ("message_start", {"type": "message_start", "message": {"id": "msg_fixture", "type": "message", "role": "assistant", "model": "fixture-anthropic", "content": [], "usage": {"input_tokens": 7, "output_tokens": 1}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "loopback "}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "response"}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_sequence": None}, "usage": {"output_tokens": 3}}),
        ("message_stop", {"type": "message_stop"}),
    ]
    if stream:
        return 200, "text/event-stream", b"".join(_sse(name, event) for name, event in events)
    return _json(200, {"id": "msg_fixture", "type": "message", "role": "assistant", "model": "fixture-anthropic", "content": [{"type": "text", "text": "loopback response"}], "stop_reason": "end_turn", "stop_sequence": None, "usage": {"input_tokens": 7, "output_tokens": 3}})


class _FixtureHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args: Any) -> None:
        pass

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length) if length else b""
        try:
            request = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            request = {}
        stream = isinstance(request, dict) and request.get("stream") is True
        if self.path.startswith("/v1/responses"):
            protocol, result = "responses", _responses(scenario=self.server.scenario, stream=stream)  # type: ignore[attr-defined]
        elif self.path.startswith("/v1/chat/completions"):
            protocol, result = "chat_completions", _chat(scenario=self.server.scenario, stream=stream)
        elif self.path.startswith("/v1/messages"):
            protocol, result = "anthropic_messages", _anthropic(scenario=self.server.scenario, stream=stream)
        else:
            self.send_error(404)
            return
        status, content_type, payload = result
        self.server.records.append({"protocol": protocol, "path": self.path, "keys": sorted(request) if isinstance(request, dict) else [], "stream": stream})  # type: ignore[attr-defined]
        self.send_response(status)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
        self.wfile.flush()


class UpstreamFixtureServer(ThreadingHTTPServer):
    """Loopback-only upstream fixture; request records contain no body values."""

    daemon_threads = True

    def __init__(self, address: tuple[str, int], *, scenario: str = "text") -> None:
        super().__init__(address, _FixtureHandler)
        self.scenario = scenario
        self.records: list[dict[str, Any]] = []


def fixture_response(protocol: str, *, scenario: str = "text", stream: bool = False) -> tuple[int, str, bytes]:
    """Return one deterministic response without opening a socket."""

    if protocol == "responses":
        return _responses(scenario=scenario, stream=stream)
    if protocol == "chat_completions":
        return _chat(scenario=scenario, stream=stream)
    if protocol == "anthropic_messages":
        return _anthropic(scenario=scenario, stream=stream)
    raise ValueError(f"unsupported fixture protocol: {protocol}")
