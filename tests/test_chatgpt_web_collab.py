"""ChatGPT Web plaintext collaboration V2 submissions stay on the caller's wire."""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from pathlib import Path

import pytest

import chatgpt_web_collab
import test_chatgpt_web_route as web

COLLAB_TOOLS = [
    {
        "type": "namespace",
        "name": "collaboration",
        "description": "collaboration",
        "tools": [
            {
                "type": "function",
                "name": "spawn_agent",
                "description": "spawn_agent",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "task_name": {"type": "string"},
                        "message": {"type": "string"},
                    },
                    "required": ["task_name", "message"],
                },
            }
        ],
    }
]


@pytest.fixture
def runtime(tmp_path: Path):
    home, pin = web._install(tmp_path)
    web._write_doctor(
        home,
        web._doctor([{"id": web.MODEL_ID, "display_name": "Sol", "efforts": ["medium", "high"]}]),
    )
    web._start(home, pin)
    try:
        yield home, pin
    finally:
        web._run(home, "stop", pin=pin)


@pytest.fixture(autouse=True)
def _reset_collab_process_state():
    chatgpt_web_collab.reset_process_state()
    yield
    chatgpt_web_collab.reset_process_state()


def _collab_request(thread_id: str, turn_id: str, items: list[dict] | None = None) -> dict:
    body = web._request_body(
        web.MODEL_ID,
        items if items is not None else [web._message(f"msg_{turn_id}", turn_id, web.PROMPT)],
        thread_id=thread_id,
        turn_id=turn_id,
        effort="high",
    )
    body["tools"] = json.loads(json.dumps(COLLAB_TOOLS))
    return body


def _hold_open(
    port: int,
    body: dict,
    ready: threading.Event,
    stop: threading.Event,
    errors: list[BaseException],
) -> None:
    payload = json.dumps(body).encode("utf-8")
    key = os.environ["CODEX_PROXY_GATEWAY_CLIENT_KEY"]
    request = (
        f"POST /v1/responses HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        "Content-Type: application/json\r\n"
        f"Authorization: Bearer {key}\r\n"
        f"Content-Length: {len(payload)}\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("ascii") + payload
    client = socket.create_connection(("127.0.0.1", port), timeout=5)
    client.settimeout(0.2)
    try:
        client.sendall(request)
        seen = b""
        while not stop.is_set():
            try:
                chunk = client.recv(4096)
            except socket.timeout:
                continue
            if not chunk:
                break
            seen += chunk
            if b'"namespace":"collaboration"' in seen or b'"namespace": "collaboration"' in seen:
                ready.set()
    except BaseException as exc:
        errors.append(exc)
    finally:
        try:
            client.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        client.close()


def _wait_ready(ready: threading.Event, errors: list[BaseException]) -> None:
    deadline = time.time() + 5
    while time.time() < deadline:
        if ready.is_set():
            return
        if errors:
            raise AssertionError(errors[0])
        time.sleep(0.05)
    raise AssertionError(errors or "collaboration stream did not start")


def test_plaintext_spawn_agent_keeps_namespace_marker_and_call_id(runtime, tmp_path: Path) -> None:
    home, pin = runtime
    (home / "web-home" / "serve-mode").write_text("collab", encoding="utf-8")
    with web._gateway(home, pin, tmp_path / "codex-client") as port:
        first = _collab_request("thread_spawn", "turn_spawn")
        status, payload = web._post(port, first)
        assert status == 200, payload
        events = web._sse_events(payload)
        done = [
            event["item"]
            for event in events
            if event.get("type") == "response.output_item.done" and isinstance(event.get("item"), dict)
        ]
        assert done
        call = done[0]
        assert call["namespace"] == "collaboration"
        assert call["name"] == "spawn_agent"
        assert call["encrypted_function_args"] == []
        assert call["call_id"] == "call_turn_spawn"
        assert call["id"] == "fc_turn_spawn"
        assert call["arguments"] == '{"task_name":"child","message":"draw"}'
        assert "multi_agent_v1" not in payload.decode("utf-8")

        continuation = web._request_body(
            web.MODEL_ID,
            [
                web._message("msg_turn_spawn", "turn_spawn", web.PROMPT),
                dict(call),
                {
                    "type": "function_call_output",
                    "call_id": call["call_id"],
                    "output": '{"task_name":"child"}',
                },
            ],
            thread_id="thread_spawn",
            turn_id="turn_spawn",
            effort="high",
        )
        status, payload = web._post(port, continuation)
        assert status == 200, payload
        assert b"tool result accepted" in payload
        posted = web._requests(home)
        assert len(posted) == 2
        forwarded = posted[1]["body"]
        assert forwarded["model"] == web.MODEL_ID
        assert web._identity(forwarded)["thread_id"] == "thread_spawn"
        assert web._identity(forwarded)["turn_id"] == "turn_spawn"
        forwarded_call = next(item for item in forwarded["input"] if item.get("type") == "function_call")
        forwarded_result = next(item for item in forwarded["input"] if item.get("type") == "function_call_output")
        assert forwarded_call["namespace"] == "collaboration"
        assert forwarded_call["name"] == "spawn_agent"
        assert forwarded_call["encrypted_function_args"] == []
        assert forwarded_call["call_id"] == "call_turn_spawn"
        assert forwarded_call["id"] == "fc_turn_spawn"
        assert forwarded_call["arguments"] == call["arguments"]
        assert forwarded_result["call_id"] == "call_turn_spawn"
        assert forwarded_result["output"] == '{"task_name":"child"}'

        before = len(web._requests(home))
        status, payload = web._post(port, continuation)
        assert status == 400, payload
        assert b"revoked" in payload
        assert len(web._requests(home)) == before


def test_encrypted_collaboration_arguments_are_rejected_before_post(runtime, tmp_path: Path) -> None:
    home, pin = runtime
    with web._gateway(home, pin, tmp_path / "codex-client") as port:
        before = len(web._requests(home))
        body = _collab_request(
            "thread_encrypted",
            "turn_encrypted",
            [
                web._message("msg_turn_encrypted", "turn_encrypted", web.PROMPT),
                {
                    "type": "function_call",
                    "id": "fc_encrypted",
                    "call_id": "call_encrypted",
                    "namespace": "collaboration",
                    "name": "spawn_agent",
                    "arguments": '{"task_name":"child"}',
                    "encrypted_function_args": ["message"],
                },
            ],
        )
        status, payload = web._post(port, body)
        assert status == 400, payload
        assert b"encrypted" in payload
        assert b"unsupported" in payload
        assert len(web._requests(home)) == before


def test_sixth_inflight_collaboration_submission_is_rejected(runtime, tmp_path: Path) -> None:
    home, pin = runtime
    (home / "web-home" / "serve-mode").write_text("collab-hold", encoding="utf-8")
    with web._gateway(home, pin, tmp_path / "codex-client") as port:
        stop = threading.Event()
        errors: list[BaseException] = []
        ready_flags = [threading.Event() for _ in range(5)]
        threads = [
            threading.Thread(
                target=_hold_open,
                args=(port, _collab_request(f"thread_cap_{index}", f"turn_cap_{index}"), ready, stop, errors),
                daemon=True,
            )
            for index, ready in enumerate(ready_flags)
        ]
        for thread in threads:
            thread.start()
        try:
            for ready in ready_flags:
                _wait_ready(ready, errors)
            assert len(web._requests(home)) == 5
            started = time.monotonic()
            status, payload = web._post(port, _collab_request("thread_cap_6", "turn_cap_6"), timeout=3)
            elapsed = time.monotonic() - started
            assert elapsed < 2, elapsed
            assert status == 400, payload
            assert b"bounded capacity" in payload
            assert b"encrypted" not in payload
            assert len(web._requests(home)) == 5
        finally:
            stop.set()
            for thread in threads:
                thread.join(timeout=3)


def test_child_request_is_not_blocked_behind_waiting_parent(runtime, tmp_path: Path) -> None:
    home, pin = runtime
    (home / "web-home" / "serve-mode").write_text("collab-hold", encoding="utf-8")
    with web._gateway(home, pin, tmp_path / "codex-client") as port:
        stop = threading.Event()
        errors: list[BaseException] = []
        ready = threading.Event()
        parent = threading.Thread(
            target=_hold_open,
            args=(port, _collab_request("thread_parent", "turn_parent"), ready, stop, errors),
            daemon=True,
        )
        parent.start()
        try:
            _wait_ready(ready, errors)
            assert len(web._requests(home)) == 1
            (home / "web-home" / "serve-mode").write_text("collab", encoding="utf-8")
            started = time.monotonic()
            status, payload = web._post(port, _collab_request("thread_child", "turn_child"), timeout=3)
            elapsed = time.monotonic() - started
            assert elapsed < 2, elapsed
            assert status == 200, payload
            assert errors == []
            posted = web._requests(home)
            assert len(posted) == 2
            assert web._identity(posted[0]["body"])["thread_id"] == "thread_parent"
            child = posted[1]["body"]
            assert web._identity(child)["thread_id"] == "thread_child"
            assert child["model"] == web.MODEL_ID
            assert all(item.get("type") != "function_call" for item in child["input"])
            assert ready.is_set()
            assert parent.is_alive()
        finally:
            stop.set()
            parent.join(timeout=3)


def test_cancel_then_late_collaboration_result_adds_no_post(runtime, tmp_path: Path) -> None:
    home, pin = runtime
    (home / "web-home" / "serve-mode").write_text("collab-hold", encoding="utf-8")
    with web._gateway(home, pin, tmp_path / "codex-client") as port:
        stop = threading.Event()
        errors: list[BaseException] = []
        ready = threading.Event()
        worker = threading.Thread(
            target=_hold_open,
            args=(port, _collab_request("thread_cancel", "turn_cancel"), ready, stop, errors),
            daemon=True,
        )
        worker.start()
        try:
            _wait_ready(ready, errors)
            assert len(web._requests(home)) == 1
        finally:
            stop.set()
            worker.join(timeout=3)
        closed = home / "web-home" / "upstream-closed"
        deadline = time.time() + 5
        while not closed.is_file() and time.time() < deadline:
            time.sleep(0.05)
        assert closed.is_file()
        (home / "web-home" / "serve-mode").write_text("collab", encoding="utf-8")
        late = web._request_body(
            web.MODEL_ID,
            [
                web._message("msg_turn_cancel", "turn_cancel", web.PROMPT),
                {
                    "type": "function_call",
                    "id": "fc_turn_cancel",
                    "call_id": "call_turn_cancel",
                    "namespace": "collaboration",
                    "name": "spawn_agent",
                    "arguments": '{"task_name":"child","message":"draw"}',
                    "encrypted_function_args": [],
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_turn_cancel",
                    "output": '{"task_name":"child"}',
                },
            ],
            thread_id="thread_cancel",
            turn_id="turn_cancel",
            effort="high",
        )
        before = len(web._requests(home))
        status, payload = web._post(port, late)
        assert status == 400, payload
        assert b"revoked" in payload or b"expired" in payload
        assert len(web._requests(home)) == before
