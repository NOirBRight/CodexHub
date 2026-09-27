"""Public Messages coverage for non-Codex ChatGPT Web sessions."""

from __future__ import annotations

import http.client
import json
import os
import socket
import time
from pathlib import Path

import pytest

from test_chatgpt_web_route import (
    MODEL_ID,
    PROMPT,
    _doctor,
    _gateway,
    _install,
    _message,
    _request_body,
    _requests,
    _run,
    _sse_events,
    _start,
    _write_doctor,
)

SHELL_TOOL = {
    "name": "shell",
    "description": "Run a command",
    "input_schema": {"type": "object", "properties": {"cmd": {"type": "string"}}},
}
SYSTEM = [
    {"type": "text", "text": "You are concise."},
    {"type": "text", "text": "Stay on the web thread.", "cache_control": {"type": "ephemeral"}},
]


def runtime(tmp_path: Path):
    home, pin = _install(tmp_path)
    _write_doctor(
        home,
        _doctor(
            [
                {
                    "id": MODEL_ID,
                    "display_name": "Sol",
                    "efforts": ["medium", "high"],
                }
            ]
        ),
    )
    _start(home, pin)
    return home, pin


def _post_messages(
    port: int,
    body: dict,
    *,
    session_header: str | None = None,
    timeout: float = 8.0,
) -> tuple[int, bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {os.environ['CODEX_PROXY_GATEWAY_CLIENT_KEY']}",
            "anthropic-version": "2023-06-01",
        }
        if session_header is not None:
            headers["X-Session-Id"] = session_header
        connection.request(
            "POST",
            "/v1/messages",
            body=json.dumps(body).encode("utf-8"),
            headers=headers,
        )
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def _post_responses(
    port: int,
    body: dict,
    *,
    session_header: str | None = None,
    timeout: float = 8.0,
) -> tuple[int, bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {os.environ['CODEX_PROXY_GATEWAY_CLIENT_KEY']}",
        }
        if session_header is not None:
            headers["X-Session-Id"] = session_header
        connection.request(
            "POST",
            "/v1/responses",
            body=json.dumps(body).encode("utf-8"),
            headers=headers,
        )
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def _messages_body(
    messages: list[dict],
    *,
    session_field: str | None = None,
    tools: list[dict] | None = None,
    metadata_user: str | None = "same-telemetry",
) -> dict:
    body: dict = {
        "model": MODEL_ID,
        "max_tokens": 64,
        "stream": True,
        "system": SYSTEM,
        "messages": messages,
    }
    if metadata_user is not None:
        body["metadata"] = {"user_id": metadata_user}
    if session_field is not None:
        body["session_id"] = session_field
    if tools is not None:
        body["tools"] = tools
    return body


def _user_turn(text: str = PROMPT) -> dict:
    return {
        "role": "user",
        "content": [
            {"type": "text", "text": text},
            {"type": "text", "text": "second block", "cache_control": {"type": "ephemeral"}},
        ],
    }


def _thread_id(payload: dict) -> str:
    metadata = json.loads(payload["client_metadata"]["x-codex-turn-metadata"])
    return metadata["thread_id"]


def _stop_reasons(body: bytes) -> list[str]:
    reasons = []
    for event in _sse_events(body):
        if event.get("type") != "message_delta":
            continue
        delta = event.get("delta")
        if isinstance(delta, dict) and isinstance(delta.get("stop_reason"), str):
            reasons.append(delta["stop_reason"])
    return reasons


def _text_deltas(body: bytes) -> str:
    parts: list[str] = []
    for event in _sse_events(body):
        if event.get("type") != "content_block_delta":
            continue
        delta = event.get("delta")
        if isinstance(delta, dict) and delta.get("type") == "text_delta" and isinstance(delta.get("text"), str):
            parts.append(delta["text"])
    return "".join(parts)


def _tool_uses(body: bytes) -> list[dict]:
    found = []
    for event in _sse_events(body):
        if event.get("type") != "content_block_start":
            continue
        block = event.get("content_block")
        if isinstance(block, dict) and block.get("type") == "tool_use":
            found.append(block)
    return found


def _argument_deltas(body: bytes) -> str:
    parts: list[str] = []
    for event in _sse_events(body):
        if event.get("type") != "content_block_delta":
            continue
        delta = event.get("delta")
        if (
            isinstance(delta, dict)
            and delta.get("type") == "input_json_delta"
            and isinstance(delta.get("partial_json"), str)
        ):
            parts.append(delta["partial_json"])
    return "".join(parts)


def _user_blocks(payload: dict) -> list[list[str]]:
    blocks: list[list[str]] = []
    for item in payload.get("input") or []:
        if not isinstance(item, dict) or item.get("type") != "message" or item.get("role") != "user":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            raise AssertionError(content)
        blocks.append([part.get("text") for part in content if isinstance(part, dict)])
    return blocks


def test_missing_session_id_is_rejected_and_records_no_responses_post(tmp_path: Path) -> None:
    home, pin = runtime(tmp_path)
    try:
        with _gateway(home, pin, tmp_path / "codex-client") as port:
            status, body = _post_messages(port, _messages_body([_user_turn()]))
            assert status == 400, body
            assert b"client session id is required" in body
            assert b"message_stop" not in body
            assert _requests(home) == []

            unscoped = _messages_body(
                [
                    {
                        "role": "user",
                        "content": [
                            {"type": "tool_result", "tool_use_id": "call_unscoped", "content": "pwd"},
                        ],
                    }
                ],
                metadata_user=None,
            )
            status, body = _post_messages(port, unscoped)
            assert status == 400, body
            assert b"client session id is required" in body
            assert _requests(home) == []
    finally:
        _run(home, "stop", pin=pin)


@pytest.mark.parametrize("model", [
    MODEL_ID,
    "claude-codexhub-chatgpt-web-gpt-5.6-sol",
    "claude-codexhub-role/fable/chatgpt-web/gpt-5.6-sol",
])
def test_sessions_keep_distinct_threads_and_one_session_reuses_its_thread(
    tmp_path: Path, model: str,
) -> None:
    home, pin = runtime(tmp_path)
    try:
        with _gateway(home, pin, tmp_path / "codex-client") as port:
            codex = _request_body(
                MODEL_ID,
                [_message("msg_codex", "turn_codex", PROMPT)],
                thread_id="thread_codex",
                turn_id="turn_codex",
                effort="high",
            )
            status, body = _post_responses(port, codex, session_header="session-codex")
            assert status == 200, body
            assert _thread_id(_requests(home)[0]["body"]) == "thread_codex"

            first = _messages_body([_user_turn()])
            first["model"] = model
            status, body = _post_messages(port, first, session_header="session-alpha")
            assert status == 200, body
            assert _text_deltas(body) == "hello web"
            assert _stop_reasons(body) == ["end_turn"]
            assert [event.get("type") for event in _sse_events(body)].count("message_stop") == 1
            assert b'"type":"error"' not in body

            other = _messages_body([_user_turn()], session_field="session-beta")
            other["model"] = model
            status, body = _post_messages(port, other)
            assert status == 200, body
            captured = _requests(home)
            assert all(item["body"]["model"] == MODEL_ID for item in captured)
            assert [item["path"].split("?", 1)[0] for item in captured] == [
                "/v1/responses",
                "/v1/responses",
                "/v1/responses",
            ]
            codex_thread = _thread_id(captured[0]["body"])
            first_thread = _thread_id(captured[1]["body"])
            other_thread = _thread_id(captured[2]["body"])
            assert codex_thread == "thread_codex"
            assert first_thread != other_thread
            assert first_thread != codex_thread
            assert other_thread != codex_thread
            assert captured[1]["body"]["prompt_cache_key"] == first_thread
            assert captured[2]["body"]["prompt_cache_key"] == other_thread
            assert captured[1]["body"]["instructions"] == "You are concise.\n\nStay on the web thread."
            assert _user_blocks(captured[1]["body"]) == [[PROMPT, "second block"]]
            assert "prompt" not in captured[1]["body"]
            assert "environment_context" not in json.dumps(captured[1]["body"])
            assert "sandbox" not in captured[1]["body"]

            second = _messages_body(
                [
                    _user_turn(),
                    {"role": "assistant", "content": [{"type": "text", "text": "hello web"}]},
                    {"role": "user", "content": "continue"},
                ]
            )
            second["model"] = model
            status, body = _post_messages(port, second, session_header="session-alpha")
            assert status == 200, body
            continued = _requests(home)[3]["body"]
            assert _thread_id(continued) == first_thread
            assert continued["prompt_cache_key"] == first_thread
            assert _user_blocks(continued)[0] == [PROMPT, "second block"]
            assert _stop_reasons(body) == ["end_turn"]
            assert [event.get("type") for event in _sse_events(body)].count("message_stop") == 1
    finally:
        _run(home, "stop", pin=pin)


def test_tool_result_stays_on_the_session_and_replay_adds_no_post(tmp_path: Path) -> None:
    home, pin = runtime(tmp_path)
    try:
        with _gateway(home, pin, tmp_path / "codex-client") as port:
            issued = _messages_body([_user_turn("run pwd")], tools=[SHELL_TOOL])
            status, body = _post_messages(port, issued, session_header="session-tools")
            assert status == 200, body
            tool_uses = _tool_uses(body)
            assert len(tool_uses) == 1, body
            assert tool_uses[0]["name"] == "shell"
            call_id = tool_uses[0]["id"]
            assert isinstance(call_id, str) and call_id
            assert _argument_deltas(body) == '{"cmd":"pwd"}'
            assert _stop_reasons(body) == ["tool_use"]
            assert [event.get("type") for event in _sse_events(body)].count("message_stop") == 1
            assert b'"type":"error"' not in body
            issued_upstream = _requests(home)[0]["body"]
            assert _thread_id(issued_upstream)
            assert any(tool.get("name") == "shell" for tool in issued_upstream.get("tools") or [])
            assert "environment_context" not in json.dumps(issued_upstream)
            metadata = json.loads(issued_upstream["client_metadata"]["x-codex-turn-metadata"])
            assert "sandbox" not in metadata

            result = _messages_body(
                [
                    _user_turn("run pwd"),
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": call_id,
                                "name": "shell",
                                "input": {"cmd": "pwd"},
                            }
                        ],
                    },
                    {
                        "role": "user",
                        "content": [
                            {"type": "tool_result", "tool_use_id": call_id, "content": "/workspace"},
                        ],
                    },
                ],
                tools=[SHELL_TOOL],
            )
            status, body = _post_messages(port, result, session_header="session-tools")
            assert status == 200, body
            assert _text_deltas(body) == "tool result accepted"
            assert _stop_reasons(body) == ["end_turn"]
            assert [event.get("type") for event in _sse_events(body)].count("message_stop") == 1
            assert len(_requests(home)) == 2
            continued = _requests(home)[1]["body"]
            assert _thread_id(continued) == _thread_id(issued_upstream)
            calls = [item for item in continued["input"] if item.get("type") == "function_call"]
            outputs = [item for item in continued["input"] if item.get("type") == "function_call_output"]
            assert [item.get("call_id") for item in calls] == [call_id]
            assert [item.get("call_id") for item in outputs] == [call_id]
            assert calls[0]["name"] == "shell"
            assert outputs[0]["output"] == "/workspace"

            copied = _messages_body(
                [
                    {
                        "role": "user",
                        "content": [
                            {"type": "tool_result", "tool_use_id": call_id, "content": "/workspace"},
                        ],
                    }
                ],
                session_field="session-other",
                tools=[SHELL_TOOL],
            )
            before = len(_requests(home))
            status, body = _post_messages(port, copied)
            assert status == 400, body
            assert b"another client session" in body
            assert b"message_stop" not in body
            assert len(_requests(home)) == before

            status, body = _post_messages(port, result, session_header="session-tools")
            assert status == 200, body
            assert _text_deltas(body) == "tool result accepted"
            assert len(_requests(home)) == before
    finally:
        _run(home, "stop", pin=pin)


def test_cancel_closes_the_upstream_body_and_rejects_a_later_tool_result(tmp_path: Path) -> None:
    home, pin = runtime(tmp_path)
    try:
        (home / "web-home" / "serve-mode").write_text("tool-hold", encoding="utf-8")
        with _gateway(home, pin, tmp_path / "codex-client") as port:
            payload = json.dumps(
                _messages_body([_user_turn("run pwd")], tools=[SHELL_TOOL])
            ).encode("utf-8")
            key = os.environ["CODEX_PROXY_GATEWAY_CLIENT_KEY"]
            request = (
                f"POST /v1/messages HTTP/1.1\r\n"
                f"Host: 127.0.0.1:{port}\r\n"
                "Content-Type: application/json\r\n"
                f"Authorization: Bearer {key}\r\n"
                "X-Session-Id: session-cancel\r\n"
                "anthropic-version: 2023-06-01\r\n"
                f"Content-Length: {len(payload)}\r\n"
                "Connection: close\r\n"
                "\r\n"
            ).encode("ascii") + payload
            client = socket.create_connection(("127.0.0.1", port), timeout=5)
            client.sendall(request)
            seen = b""
            marker = b'"type":"tool_use","id":"'
            deadline = time.time() + 5
            call_id = ""
            while not call_id and time.time() < deadline:
                if marker in seen and b'"' in seen.split(marker, 1)[1]:
                    call_id = seen.split(marker, 1)[1].split(b'"', 1)[0].decode("utf-8")
                    break
                chunk = client.recv(256)
                if not chunk:
                    break
                seen += chunk
            assert call_id.startswith("call_"), seen
            client.shutdown(socket.SHUT_RDWR)
            client.close()
            closed = home / "web-home" / "upstream-closed"
            deadline = time.time() + 5
            while not closed.is_file() and time.time() < deadline:
                time.sleep(0.05)
            assert closed.is_file()
            assert len(_requests(home)) == 1
            (home / "web-home" / "serve-mode").write_text("text", encoding="utf-8")
            result = _messages_body(
                [
                    {
                        "role": "user",
                        "content": [
                            {"type": "tool_result", "tool_use_id": call_id, "content": "/workspace"},
                        ],
                    }
                ],
                tools=[SHELL_TOOL],
            )
            status, body = _post_messages(port, result, session_header="session-cancel")
            assert status == 400, body
            assert b"tool call expired" in body
            assert b"message_stop" not in body
            assert len(_requests(home)) == 1
    finally:
        _run(home, "stop", pin=pin)


def test_upstream_error_is_not_also_a_successful_message(tmp_path: Path) -> None:
    home, pin = runtime(tmp_path)
    try:
        (home / "web-home" / "serve-mode").write_text("error", encoding="utf-8")
        with _gateway(home, pin, tmp_path / "codex-client") as port:
            status, body = _post_messages(port, _messages_body([_user_turn()]), session_header="session-error")
            assert status >= 400 or b'"type":"error"' in body
            assert b"message_stop" not in body
            assert b"end_turn" not in body
            assert b'"type":"error"' in body
            assert len(_requests(home)) == 1
    finally:
        _run(home, "stop", pin=pin)
