"""Public Chat Completions coverage for non-Codex ChatGPT Web sessions."""

from __future__ import annotations

import http.client
import json
import os
from pathlib import Path

import pytest

from test_chatgpt_web_route import (
    MODEL_ID,
    PROMPT,
    _gateway,
    _install,
    _requests,
    _run,
    _sse_events,
    _start,
)

SHELL_TOOL = {
    "type": "function",
    "function": {
        "name": "shell",
        "description": "Run a command",
        "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}},
    },
}


@pytest.fixture
def runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home, pin = _install(tmp_path)
    _start(home, pin, monkeypatch)
    try:
        yield home, pin
    finally:
        _run(home, "stop", pin=pin)


def _post_chat(
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
            "/v1/chat/completions",
            body=json.dumps(body).encode("utf-8"),
            headers=headers,
        )
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def _chat_body(messages: list[dict], *, session_field: str | None = None, tools: list[dict] | None = None) -> dict:
    body: dict = {
        "model": MODEL_ID,
        "stream": True,
        "messages": messages,
        "prompt_cache_key": "shared-unscoped-cache",
    }
    if session_field is not None:
        body["sessionID"] = session_field
    if tools is not None:
        body["tools"] = tools
    return body


def _thread_id(payload: dict) -> str:
    metadata = json.loads(payload["client_metadata"]["x-codex-turn-metadata"])
    return metadata["thread_id"]


def _tool_call_ids(body: bytes) -> list[str]:
    found: list[str] = []
    for event in _sse_events(body):
        for choice in event.get("choices") or []:
            delta = choice.get("delta") or {}
            for tool_call in delta.get("tool_calls") or []:
                call_id = tool_call.get("id") if isinstance(tool_call, dict) else None
                if isinstance(call_id, str) and call_id:
                    found.append(call_id)
    return found


def test_missing_session_id_is_rejected_and_records_no_responses_post(runtime, tmp_path: Path) -> None:
    home, pin = runtime
    with _gateway(home, pin, tmp_path / "codex-client") as port:
        status, body = _post_chat(port, _chat_body([{"role": "user", "content": PROMPT}]))
        assert status == 400, body
        assert b"client session id is required" in body
        assert _requests(home) == []

        unscoped = _chat_body(
            [
                {"role": "user", "content": PROMPT},
                {
                    "role": "tool",
                    "tool_call_id": "call_unscoped",
                    "content": "pwd",
                },
            ]
        )
        status, body = _post_chat(port, unscoped)
        assert status == 400, body
        assert b"client session id is required" in body
        assert _requests(home) == []


def test_sessions_keep_distinct_threads_and_one_session_reuses_its_thread(runtime, tmp_path: Path) -> None:
    home, pin = runtime
    with _gateway(home, pin, tmp_path / "codex-client") as port:
        first = _chat_body([{"role": "user", "content": PROMPT}])
        status, body = _post_chat(port, first, session_header="session-alpha")
        assert status == 200, body
        other = _chat_body([{"role": "user", "content": PROMPT}], session_field="session-beta")
        status, body = _post_chat(port, other)
        assert status == 200, body
        captured = _requests(home)
        assert [item["path"].split("?", 1)[0] for item in captured] == ["/v1/responses", "/v1/responses"]
        first_thread = _thread_id(captured[0]["body"])
        other_thread = _thread_id(captured[1]["body"])
        assert first_thread != other_thread
        assert captured[0]["body"]["prompt_cache_key"] == first_thread
        assert captured[1]["body"]["prompt_cache_key"] == other_thread
        assert first_thread != "shared-unscoped-cache"
        assert "environment_context" not in json.dumps(captured[0]["body"])

        second = _chat_body(
            [
                {"role": "user", "content": PROMPT},
                {"role": "assistant", "content": "hello web"},
                {"role": "user", "content": "continue"},
            ]
        )
        status, body = _post_chat(port, second, session_header="session-alpha")
        assert status == 200, body
        continued = _requests(home)[2]["body"]
        assert _thread_id(continued) == first_thread
        assert continued["prompt_cache_key"] == first_thread


def test_tool_result_stays_on_the_session_and_replay_adds_no_post(runtime, tmp_path: Path) -> None:
    home, pin = runtime
    with _gateway(home, pin, tmp_path / "codex-client") as port:
        issued = _chat_body(
            [{"role": "user", "content": "run pwd"}],
            tools=[SHELL_TOOL],
        )
        status, body = _post_chat(port, issued, session_header="session-tools")
        assert status == 200, body
        call_ids = _tool_call_ids(body)
        assert len(call_ids) == 1, body
        call_id = call_ids[0]
        issued_upstream = _requests(home)[0]["body"]
        assert _thread_id(issued_upstream)
        assert any(tool.get("name") == "shell" for tool in issued_upstream.get("tools") or [])
        assert "environment_context" not in json.dumps(issued_upstream)
        metadata = json.loads(issued_upstream["client_metadata"]["x-codex-turn-metadata"])
        assert "sandbox" not in metadata

        result = _chat_body(
            [
                {"role": "user", "content": "run pwd"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": "shell", "arguments": "{\"cmd\":\"pwd\"}"},
                        }
                    ],
                },
                {"role": "tool", "tool_call_id": call_id, "content": "/workspace"},
            ],
            tools=[SHELL_TOOL],
        )
        status, body = _post_chat(port, result, session_header="session-tools")
        assert status == 200, body
        assert len(_requests(home)) == 2
        continued = _requests(home)[1]["body"]
        assert _thread_id(continued) == _thread_id(issued_upstream)
        outputs = [
            item.get("call_id")
            for item in continued["input"]
            if item.get("type") == "function_call_output"
        ]
        assert outputs == [call_id]
        assert any(tool.get("name") == "shell" for tool in continued.get("tools") or [])

        copied = _chat_body(
            [
                {"role": "user", "content": "run pwd"},
                {"role": "tool", "tool_call_id": call_id, "content": "/workspace"},
            ],
            session_field="session-other",
            tools=[SHELL_TOOL],
        )
        before = len(_requests(home))
        status, body = _post_chat(port, copied)
        assert status == 400, body
        assert b"another client session" in body
        assert len(_requests(home)) == before

        status, body = _post_chat(port, result, session_header="session-tools")
        assert status == 200, body
        assert len(_requests(home)) == before
