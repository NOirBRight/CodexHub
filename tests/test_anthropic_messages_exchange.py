"""Offline end-to-end response seam checks for the #74 prototype."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from anthropic_messages_prototype import (
    AdaptedResponse,
    NotForwardable,
    adapt_upstream_response,
    adapt_upstream_stream,
    execute_exchange,
    prepare_upstream_request,
)


def _sse(event: str | None, payload: Any) -> bytes:
    data = payload if isinstance(payload, str) else json.dumps(payload, separators=(",", ":"))
    prefix = f"event: {event}\n" if event else ""
    return f"{prefix}data: {data}\n\n".encode()


def _events(body: bytes) -> list[dict[str, Any] | str]:
    result: list[dict[str, Any] | str] = []
    for frame in body.split(b"\n\n"):
        if not frame.strip():
            continue
        data = next(line[6:] for line in frame.splitlines() if line.startswith(b"data: "))
        result.append(data.decode() if data == b"[DONE]" else json.loads(data))
    return result


def _request(*, stream: bool = False) -> bytes:
    return json.dumps(
        {
            "model": "claude-fixture",
            "max_tokens": 128,
            "stream": stream,
            "messages": [{"role": "user", "content": "hello fixture"}],
        },
        separators=(",", ":"),
    ).encode()


def _responses_text() -> bytes:
    return json.dumps(
        {
            "id": "resp_fixture_text",
            "object": "response",
            "status": "completed",
            "model": "fixture-responses",
            "output": [
                {
                    "id": "msg_fixture_text",
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "pong", "annotations": []}],
                }
            ],
            "usage": {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10},
        },
        separators=(",", ":"),
    ).encode()


def _responses_tool() -> bytes:
    return json.dumps(
        {
            "id": "resp_fixture_tool",
            "object": "response",
            "status": "completed",
            "model": "fixture-responses",
            "output": [
                {
                    "id": "fc_fixture_item",
                    "type": "function_call",
                    "status": "completed",
                    "call_id": "call_fixture_1",
                    "name": "Read",
                    "arguments": '{"path":"fixture.txt"}',
                }
            ],
            "usage": {"input_tokens": 8, "output_tokens": 4},
        },
        separators=(",", ":"),
    ).encode()


def _responses_stream() -> bytes:
    events = [
        {
            "type": "response.created",
            "response": {"id": "resp_stream", "model": "fixture-responses", "status": "in_progress"},
        },
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {
                "id": "fc_stream_item",
                "type": "function_call",
                "status": "in_progress",
                "call_id": "call_stream_1",
                "name": "Read",
                "arguments": "",
            },
        },
        {
            "type": "response.function_call_arguments.delta",
            "output_index": 0,
            "item_id": "fc_stream_item",
            "delta": '{"path":',
        },
        {
            "type": "response.function_call_arguments.delta",
            "output_index": 0,
            "item_id": "fc_stream_item",
            "delta": '"fixture.txt"}',
        },
        {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": {
                "id": "fc_stream_item",
                "type": "function_call",
                "status": "completed",
                "call_id": "call_stream_1",
                "name": "Read",
                "arguments": '{"path":"fixture.txt"}',
            },
        },
        {
            "type": "response.completed",
            "response": {
                "id": "resp_stream",
                "model": "fixture-responses",
                "status": "completed",
                "output": [
                    {
                        "id": "fc_stream_item",
                        "type": "function_call",
                        "status": "completed",
                        "call_id": "call_stream_1",
                        "name": "Read",
                        "arguments": '{"path":"fixture.txt"}',
                    }
                ],
                "usage": {"input_tokens": 8, "output_tokens": 4},
            },
        },
    ]
    return b"".join(_sse(None, event) for event in events)


def _chat_text() -> bytes:
    return json.dumps(
        {
            "id": "chat_fixture_text",
            "object": "chat.completion",
            "model": "fixture-chat",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "pong"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
        },
        separators=(",", ":"),
    ).encode()


def _chat_stream() -> bytes:
    chunks = [
        {
            "id": "chat_stream",
            "object": "chat.completion.chunk",
            "model": "fixture-chat",
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": "po"}, "finish_reason": None}],
        },
        {
            "id": "chat_stream",
            "object": "chat.completion.chunk",
            "model": "fixture-chat",
            "choices": [{"index": 0, "delta": {"content": "ng"}, "finish_reason": None}],
        },
        {
            "id": "chat_stream",
            "object": "chat.completion.chunk",
            "model": "fixture-chat",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        },
        {
            "id": "chat_stream",
            "object": "chat.completion.chunk",
            "model": "fixture-chat",
            "choices": [],
            "usage": {"prompt_tokens": 7, "completion_tokens": 2},
        },
    ]
    return b"".join(_sse(None, chunk) for chunk in chunks) + b"data: [DONE]\n\n"


def test_request_seam_keeps_native_bytes_and_refuses_safeguards_for_conversion() -> None:
    body = _request()
    native = prepare_upstream_request(body, "anthropic_messages")
    assert native.body == body

    guarded = json.loads(body)
    guarded["safeguards"] = [{"type": "dangerous_tool_use"}]
    refusal = prepare_upstream_request(json.dumps(guarded).encode(), "responses")
    assert isinstance(refusal, NotForwardable)
    assert refusal.fields == ("safeguards",)


def test_malformed_text_and_tool_name_refuse_conversion_without_coercion() -> None:
    payload = json.loads(_request())
    payload["messages"] = [
        {"role": "user", "content": [{"type": "text", "text": 7}]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": "call-1", "input": {}}]},
    ]
    result = prepare_upstream_request(json.dumps(payload).encode(), "chat_completions")
    assert isinstance(result, NotForwardable)
    assert any(field.endswith(".text") or field.endswith(".name") for field in result.fields)


def test_json_responses_and_chat_keep_text_tool_identity_and_truthful_usage() -> None:
    response = adapt_upstream_response("responses", _responses_text())
    assert isinstance(response, AdaptedResponse)
    payload = json.loads(response.body)
    assert payload["content"] == [{"type": "text", "text": "pong"}]
    assert payload["usage"] == {"input_tokens": 7, "output_tokens": 3}
    assert any(item.field == "usage.total_tokens" for item in response.adaptations)

    tool = adapt_upstream_response("responses", _responses_tool())
    assert isinstance(tool, AdaptedResponse)
    tool_payload = json.loads(tool.body)
    assert tool_payload["content"][0]["id"] == "call_fixture_1"
    assert tool_payload["content"][0]["input"] == {"path": "fixture.txt"}

    chat = adapt_upstream_response("chat_completions", _chat_text())
    assert isinstance(chat, AdaptedResponse)
    assert json.loads(chat.body)["content"][0]["text"] == "pong"


def test_json_missing_usage_is_disclosed_without_fabricating_counts() -> None:
    payload = json.loads(_responses_text())
    payload.pop("usage")
    result = adapt_upstream_response("responses", json.dumps(payload).encode())
    assert isinstance(result, AdaptedResponse)
    assert json.loads(result.body)["usage"] == {}
    assert any(item.policy == "usage_unavailable" for item in result.adaptations)


def test_native_response_is_byte_exact_without_chat_detour() -> None:
    body = b'{"type":"message","id":"native-1","content":[{"type":"text","text":"pong"}]}'
    result = adapt_upstream_response("anthropic_messages", body)
    assert isinstance(result, AdaptedResponse)
    assert result.body == body
    assert result.adaptations == ()

    stream = b"event: message_start\ndata: {\"type\":\"message_start\"}\n\n"
    native_stream = adapt_upstream_response(
        "anthropic_messages", stream, content_type="text/event-stream; charset=utf-8"
    )
    assert isinstance(native_stream, AdaptedResponse)
    assert native_stream.body == stream


def test_incremental_responses_sse_preserves_tool_call_id() -> None:
    result = adapt_upstream_stream("responses", [part for part in (_responses_stream()[:97], _responses_stream()[97:])])
    assert isinstance(result, AdaptedResponse)
    records = _events(result.body)
    deltas = [record for record in records if isinstance(record, dict) and record.get("type") == "content_block_delta"]
    assert [record["delta"]["partial_json"] for record in deltas] == ['{"path":', '"fixture.txt"}']
    start = next(record for record in records if isinstance(record, dict) and record.get("type") == "content_block_start")
    assert start["content_block"]["id"] == "call_stream_1"
    assert records[-1]["type"] == "message_stop"


def test_incremental_chat_sse_keeps_text_fragments_and_usage() -> None:
    result = adapt_upstream_stream("chat_completions", [_chat_stream()])
    assert isinstance(result, AdaptedResponse)
    records = _events(result.body)
    assert records[0]["message"]["usage"] == {}
    text = [record["delta"]["text"] for record in records if isinstance(record, dict) and record.get("type") == "content_block_delta"]
    assert text == ["po", "ng"]
    assert records[-2]["usage"] == {"input_tokens": 7, "output_tokens": 2}
    assert records[-1]["type"] == "message_stop"


def test_responses_stream_preserves_terminal_usage() -> None:
    result = adapt_upstream_stream("responses", [_responses_stream()])
    assert isinstance(result, AdaptedResponse)
    records = _events(result.body)
    assert records[-2]["usage"] == {"input_tokens": 8, "output_tokens": 4}


def test_chat_stream_preserves_explicit_empty_text_block() -> None:
    chunks = [
        {"id": "chat-empty-text", "model": "fixture-chat", "choices": [{"index": 0, "delta": {"content": ""}, "finish_reason": None}]},
        {"id": "chat-empty-text", "model": "fixture-chat", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        "[DONE]",
    ]
    result = adapt_upstream_stream("chat_completions", [b"".join(_sse(None, chunk) for chunk in chunks)])
    assert isinstance(result, AdaptedResponse)
    starts = [record for record in _events(result.body) if record.get("type") == "content_block_start"]
    assert starts[0]["content_block"]["text"] == ""


def test_responses_stream_preserves_explicit_empty_text_block() -> None:
    events = [
        {"type": "response.created", "response": {"id": "resp-empty-text", "model": "fixture", "status": "in_progress"}},
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {"id": "msg-empty", "type": "message", "status": "in_progress", "content": []},
        },
        {
            "type": "response.completed",
            "response": {
                "id": "resp-empty-text",
                "model": "fixture",
                "status": "completed",
                "output": [
                    {
                        "id": "msg-empty",
                        "type": "message",
                        "status": "completed",
                        "content": [{"type": "output_text", "text": ""}],
                    }
                ],
                "usage": {},
            },
        },
    ]
    result = adapt_upstream_stream("responses", [_sse(None, event) for event in events])
    assert isinstance(result, AdaptedResponse)
    starts = [record for record in _events(result.body) if record.get("type") == "content_block_start"]
    assert starts[0]["content_block"]["text"] == ""


def test_chat_stream_without_usage_reports_unavailable_instead_of_fabricating_counts() -> None:
    chunks = [
        {
            "id": "chat_no_usage",
            "model": "fixture-chat",
            "choices": [{"index": 0, "delta": {"content": "pong"}, "finish_reason": None}],
        },
        {
            "id": "chat_no_usage",
            "model": "fixture-chat",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        },
        "[DONE]",
    ]
    body = b"".join(_sse(None, chunk) for chunk in chunks)
    result = adapt_upstream_stream("chat_completions", [body])
    assert isinstance(result, AdaptedResponse)
    records = _events(result.body)
    assert records[-2]["usage"] == {}
    assert "usage_unavailable" in result.diagnostics()[0]
    assert "input_tokens" not in json.dumps(records[-2])


def test_native_cancellation_is_not_a_successful_empty_passthrough() -> None:
    results = [
        adapt_upstream_response(
            "anthropic_messages",
            b"native-body",
            content_type="text/event-stream",
            cancelled=True,
        ),
        adapt_upstream_stream(
            "anthropic_messages",
            [b"native-body"],
            cancelled=lambda: True,
        ),
    ]
    for result in results:
        assert isinstance(result, AdaptedResponse)
        assert result.status == 499
        assert result.body != b"native-body"
        error_frame = next(frame for frame in result.body.split(b"\n\n") if frame.startswith(b"event: error"))
        error_payload = json.loads(error_frame[len(b"event: error\ndata: ") :])
        assert error_payload["type"] == "error"


def test_responses_stream_without_identity_is_refused_before_conversion() -> None:
    body = _sse(None, {"type": "response.created", "response": {"model": "fixture", "status": "in_progress"}})
    result = adapt_upstream_stream("responses", [body])
    assert isinstance(result, NotForwardable)
    assert result.fields == ("responses.id",)


def test_chat_stream_does_not_emit_identity_placeholder() -> None:
    body = b"".join(
        _sse(None, chunk)
        for chunk in (
            {"choices": [{"index": 0, "delta": {"content": "x"}, "finish_reason": None}]},
            {"id": "chat-real", "model": "fixture-chat", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        )
    ) + b"data: [DONE]\n\n"
    result = adapt_upstream_stream("chat_completions", [body])
    assert isinstance(result, NotForwardable)
    assert result.fields == ("chat.id",)


def test_valid_cr_terminated_sse_keeps_final_chat_frame() -> None:
    body = _chat_stream().replace(b"\n", b"\r")
    result = adapt_upstream_stream("chat_completions", [body])
    assert isinstance(result, AdaptedResponse)
    assert _events(result.body)[-1]["type"] == "message_stop"


def test_responses_and_chat_refuse_empty_success_payloads() -> None:
    responses = adapt_upstream_response(
        "responses",
        json.dumps({"id": "resp-empty", "status": "completed", "model": "fixture", "output": []}).encode(),
    )
    chat = adapt_upstream_response(
        "chat_completions",
        json.dumps({"id": "chat-empty", "model": "fixture", "choices": []}).encode(),
    )
    missing_role = adapt_upstream_response(
        "chat_completions",
        json.dumps(
            {
                "id": "chat-role",
                "model": "fixture",
                "choices": [{"index": 0, "message": {"content": "x"}, "finish_reason": "stop"}],
            }
        ).encode(),
    )
    assert isinstance(responses, NotForwardable)
    assert isinstance(chat, NotForwardable)
    assert isinstance(missing_role, NotForwardable)


def test_converted_errors_do_not_echo_upstream_details() -> None:
    result = adapt_upstream_response(
        "responses",
        json.dumps(
            {
                "id": "resp-failed",
                "status": "failed",
                "error": {"type": "provider-secret", "message": "Bearer secret https://upstream.invalid/prompt"},
            }
        ).encode(),
    )
    assert isinstance(result, AdaptedResponse)
    error_payload = json.loads(result.body)
    assert error_payload["type"] == "error"
    assert b"Bearer" not in result.body
    assert b"upstream.invalid" not in result.body


def test_responses_stream_without_completed_event_is_refused() -> None:
    body = _sse(None, {"type": "response.created", "response": {"id": "resp-open", "model": "fixture", "status": "in_progress"}})
    result = adapt_upstream_stream("responses", [body])
    assert isinstance(result, NotForwardable)
    assert result.reason == "incomplete_upstream_stream"


def test_chat_stream_done_after_partial_delta_without_finish_is_refused() -> None:
    body = _sse(
        None,
        {"id": "chat_premature", "model": "fixture-chat", "choices": [{"index": 0, "delta": {"content": "partial"}, "finish_reason": None}]},
    ) + b"data: [DONE]\n\n"
    result = adapt_upstream_stream("chat_completions", [body])
    assert isinstance(result, NotForwardable)
    assert result.reason == "incomplete_upstream_stream"


def test_chat_stream_without_done_is_refused_even_after_finish_reason() -> None:
    body = b"".join(
        _sse(
            None,
            chunk,
        )
        for chunk in (
            {"id": "chat_eof", "model": "fixture-chat", "choices": [{"index": 0, "delta": {"content": "pong"}, "finish_reason": None}]},
            {"id": "chat_eof", "model": "fixture-chat", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        )
    )
    result = adapt_upstream_stream("chat_completions", [body])
    assert isinstance(result, NotForwardable)
    assert result.reason == "incomplete_upstream_stream"


def test_responses_without_terminal_status_are_refused() -> None:
    result = adapt_upstream_response(
        "responses",
        json.dumps({"id": "resp-open", "model": "fixture", "output": []}).encode(),
    )
    assert isinstance(result, NotForwardable)
    assert result.fields == ("response.status",)


def test_terminal_error_and_cancellation_never_fabricate_success() -> None:
    failed = adapt_upstream_response(
        "responses",
        json.dumps({"id": "resp-failed", "status": "failed", "error": {"type": "server_error", "message": "busy"}}).encode(),
    )
    assert isinstance(failed, AdaptedResponse)
    assert failed.status == 502
    assert json.loads(failed.body)["type"] == "error"
    assert "content" not in json.loads(failed.body)

    cancelled = adapt_upstream_stream("chat_completions", [b"data: {\"choices\":[]}\n\n"], cancelled=lambda: True)
    assert isinstance(cancelled, AdaptedResponse)
    assert cancelled.status == 499
    assert [record["type"] for record in _events(cancelled.body)] == ["error"]


def test_execute_exchange_admits_exact_attempt_before_loopback_http() -> None:
    seen: list[bytes] = []
    requests = 0

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            nonlocal requests
            requests += 1
            size = int(self.headers.get("content-length", "0"))
            seen.append(self.rfile.read(size))
            body = _responses_text()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    attempts: list[tuple[str, str, str, bytes]] = []
    try:
        url = f"http://127.0.0.1:{server.server_port}/v1/responses?fixture=1"
        result = execute_exchange(
            _request(),
            upstream_format="responses",
            url=url,
            admit=lambda protocol, method, final_url, body: attempts.append(
                (protocol, method, final_url, body)
            ),
        )
    finally:
        server.shutdown()
        thread.join(timeout=2)
    assert isinstance(result, AdaptedResponse)
    assert requests == 1
    assert len(attempts) == 1
    assert attempts[0][0] == "responses"
    assert attempts[0][1] == "POST"
    assert attempts[0][2] == url
    assert attempts[0][3] == seen[0]
    assert json.loads(result.body)["content"][0]["text"] == "pong"


def test_execute_exchange_bounds_http_error_body_before_adaptation() -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            body = b"x" * 20
            self.send_response(503)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = execute_exchange(
            _request(),
            upstream_format="responses",
            url=f"http://127.0.0.1:{server.server_port}/error",
            admit=lambda _protocol, _method, _url, _body: None,
            max_response_bytes=8,
        )
    finally:
        server.shutdown()
        thread.join(timeout=2)
    assert isinstance(result, NotForwardable)
    assert result.fields == ("error.bytes",)


def test_execute_exchange_does_not_follow_loopback_redirect() -> None:
    requests: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            requests.append(self.path)
            self.send_response(302)
            self.send_header("location", "/other")
            self.end_headers()

        def log_message(self, *_args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = execute_exchange(
            _request(),
            upstream_format="responses",
            url=f"http://127.0.0.1:{server.server_port}/first",
            admit=lambda _protocol, _method, _url, _body: None,
        )
    finally:
        server.shutdown()
        thread.join(timeout=2)
    assert isinstance(result, AdaptedResponse)
    assert requests == ["/first"]
    assert result.status == 302
    assert json.loads(result.body)["type"] == "error"
