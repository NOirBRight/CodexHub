"""Image input, compaction, epoch revocation, and runtime recovery for ChatGPT Web."""

from __future__ import annotations

import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import chatgpt_web_route
import test_chatgpt_web_route as web


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

IMAGE_URL = "data:image/png;base64,img574privatedata"
PICTURE = "picture prompt 574"
PRE_COMPACTION = "pre-compaction user text 574"


def _post_path(port: int, path: str, body: dict, *, timeout: float = 8.0) -> tuple[int, bytes]:
    import http.client

    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        payload = json.dumps(body).encode("utf-8")
        connection.request(
            "POST",
            path,
            body=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {os.environ['CODEX_PROXY_GATEWAY_CLIENT_KEY']}",
            },
        )
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def _image_body(turn_id: str) -> dict:
    return web._request_body(
        web.MODEL_ID,
        [
            {
                "type": "message",
                "role": "user",
                "id": f"msg_{turn_id}",
                "internal_chat_message_metadata_passthrough": {"turn_id": turn_id},
                "content": [
                    {"type": "input_text", "text": PICTURE},
                    {"type": "input_image", "image_url": IMAGE_URL},
                ],
            }
        ],
        thread_id=f"thread_{turn_id}",
        turn_id=turn_id,
        effort="high",
    )


def _catalog_entry() -> dict:
    catalog = chatgpt_web_route.project_catalog({"models": []})
    models = catalog.get("models")
    assert isinstance(models, list)
    return next(item for item in models if item.get("slug") == web.MODEL_ID)


def _output(thread_id: str, turn_id: str, call_id: str, *, effort: str = "high", model: str = web.MODEL_ID) -> dict:
    body = web._request_body(
        model,
        [
            web._message(f"msg_{turn_id}", turn_id, PRE_COMPACTION),
            {
                "type": "function_call",
                "id": f"fc_{turn_id}",
                "call_id": call_id,
                "name": "shell",
                "arguments": '{"cmd":"pwd"}',
            },
            {
                "type": "function_call_output",
                "call_id": call_id,
                "output": '{"exit_code":0,"stdout":"/workspace"}',
            },
        ],
        thread_id=thread_id,
        turn_id=turn_id,
        effort=effort,
    )
    return body


def test_image_part_is_forwarded_only_when_the_doctor_lists_it(runtime, tmp_path: Path) -> None:
    home, pin = runtime
    with web._gateway(home, pin, tmp_path / "codex-client") as port:
        assert _catalog_entry()["input_modalities"] == ["text"]
        before = len(web._requests(home))
        status, payload = web._post(port, _image_body("no_image"))
        assert status == 400, payload
        assert b"does not list image input" in payload
        assert b"recognized" not in payload.lower()
        assert len(web._requests(home)) == before

        web._write_doctor(
            home,
            web._doctor(
                [
                    {
                        "id": web.MODEL_ID,
                        "display_name": "Sol",
                        "efforts": ["medium", "high"],
                        "input_modalities": ["text", "image"],
                    }
                ]
            ),
        )
        assert _catalog_entry()["input_modalities"] == ["text", "image"]
        status, payload = web._post(port, _image_body("with_image"))
        assert status == 200, payload
        assert b"recognized" not in payload.lower()
        sent = web._requests(home)[-1]["body"]
        parts = sent["input"][0]["content"]
        assert {"type": "input_image", "image_url": IMAGE_URL} in parts
        assert len(web._requests(home)) == before + 1

        status_doc = web._run(home, "status", pin=pin)
        blob = json.dumps(status_doc)
        token = json.loads((home / "web-home" / "config.json").read_text(encoding="utf-8"))["controlToken"]
        assert IMAGE_URL not in blob
        assert PICTURE not in blob
        assert token not in blob
        log = (home / "supervisor.log").read_text(encoding="utf-8")
        assert IMAGE_URL not in log
        assert PICTURE not in log
        assert token not in log


def test_compaction_keeps_the_call_and_does_not_resend_the_prior_message(runtime, tmp_path: Path) -> None:
    home, pin = runtime
    thread_id = "thread_compact"
    turn_id = "turn_compact_pair"
    call_id = "call_turn_compact_pair"
    with web._gateway(home, pin, tmp_path / "codex-client") as port:
        first = web._with_tools(
            web._request_body(
                web.MODEL_ID,
                [web._message("msg_pre", turn_id, PRE_COMPACTION)],
                thread_id=thread_id,
                turn_id=turn_id,
                effort="high",
            )
        )
        status, payload = web._post(port, first)
        assert status == 200, payload
        assert call_id.encode("utf-8") in payload
        assert PRE_COMPACTION in json.dumps(web._requests(home)[-1]["body"])

        compact = web._request_body(
            web.MODEL_ID,
            [
                web._message("msg_pre", turn_id, PRE_COMPACTION),
                web._message("msg_summary", "turn_compact", "Compact the thread."),
                {
                    "type": "function_call",
                    "id": "fc_pair",
                    "call_id": call_id,
                    "name": "shell",
                    "arguments": '{"cmd":"pwd"}',
                },
            ],
            thread_id=thread_id,
            turn_id="turn_compact",
            effort="high",
        )
        status, payload = _post_path(port, "/v1/responses/compact", compact)
        assert status == 200, payload
        compact_body = web._requests(home)[-1]["body"]
        assert PRE_COMPACTION not in json.dumps(compact_body)
        assert "Compact the thread." in json.dumps(compact_body)
        assert b"call_turn_compact" in payload
        after_compact = len(web._requests(home))

        granted = web._function_output(thread_id, "turn_compact", "call_turn_compact")
        status, payload = web._post(port, granted)
        assert status == 400, payload
        assert b"tool call is unknown" in payload
        assert len(web._requests(home)) == after_compact

        status, payload = web._post(port, _output(thread_id, turn_id, call_id))
        assert status == 200, payload
        assert b"tool result accepted" in payload
        continued = web._requests(home)[-1]["body"]
        encoded = json.dumps(continued)
        assert PRE_COMPACTION not in encoded
        metadata = json.loads(continued["client_metadata"]["x-codex-turn-metadata"])
        assert metadata["thread_id"] == thread_id
        assert continued["prompt_cache_key"] == thread_id
        assert [item["call_id"] for item in continued["input"] if item.get("type") == "function_call_output"] == [call_id]
        assert len(web._requests(home)) == after_compact + 1


def test_effort_or_model_change_revokes_the_old_call(runtime, tmp_path: Path) -> None:
    home, pin = runtime
    web._write_doctor(
        home,
        web._doctor(
            [
                {"id": web.MODEL_ID, "display_name": "Sol", "efforts": ["medium", "high"]},
                {"id": web.OTHER_MODEL_ID, "display_name": "Luna", "efforts": ["medium", "high"]},
            ]
        ),
    )
    with web._gateway(home, pin, tmp_path / "codex-client") as port:
        issued = web._with_tools(
            web._request_body(
                web.MODEL_ID,
                [web._message("msg_effort", "turn_effort", web.PROMPT)],
                thread_id="thread_effort",
                turn_id="turn_effort",
                effort="high",
            )
        )
        status, payload = web._post(port, issued)
        assert status == 200, payload
        assert b"call_turn_effort" in payload
        before = len(web._requests(home))
        changed = web._function_output("thread_effort", "turn_effort", "call_turn_effort")
        changed["reasoning"] = {"effort": "medium"}
        status, payload = web._post(port, changed)
        assert status == 400, payload
        assert b"expired" in payload
        assert len(web._requests(home)) == before

        issued = web._with_tools(
            web._request_body(
                web.MODEL_ID,
                [web._message("msg_model", "turn_model", web.PROMPT)],
                thread_id="thread_model",
                turn_id="turn_model",
                effort="high",
            )
        )
        status, payload = web._post(port, issued)
        assert status == 200, payload
        before = len(web._requests(home))
        changed = web._function_output("thread_model", "turn_model", "call_turn_model")
        changed["model"] = web.OTHER_MODEL_ID
        status, payload = web._post(port, changed)
        assert status == 400, payload
        assert b"expired" in payload
        assert len(web._requests(home)) == before


def test_runtime_faults_reject_a_new_request_and_do_not_repost(runtime, tmp_path: Path) -> None:
    home, pin = runtime
    text = web._request_body(
        web.MODEL_ID,
        [web._message("msg_accepted", "turn_accepted", web.PROMPT)],
        thread_id="thread_accepted",
        turn_id="turn_accepted",
        effort="high",
    )
    tool = web._with_tools(
        web._request_body(
            web.MODEL_ID,
            [web._message("msg_fault_tool", "turn_fault_tool", web.PROMPT)],
            thread_id="thread_fault_tool",
            turn_id="turn_fault_tool",
            effort="high",
        )
    )
    healthy = web._doctor([{"id": web.MODEL_ID, "display_name": "Sol", "efforts": ["high"]}])
    with web._gateway(home, pin, tmp_path / "codex-client") as port:
        status, payload = web._post(port, text)
        assert status == 200, payload
        accepted = len(web._requests(home))

        def fail_new(body: dict, needle: bytes, absent: bytes | None = None) -> None:
            status_code, response = web._post(port, body)
            assert status_code == 400, response
            assert needle in response
            if absent is not None:
                assert absent not in response
            assert len(web._requests(home)) == accepted

        web._write_doctor(home, web._doctor([{"id": web.MODEL_ID, "display_name": "Sol", "efforts": ["high"]}], login="error"))
        fail_new(text, b"signed out", b"browser smoke failed")
        web._write_doctor(
            home,
            {
                "ok": False,
                "models": [{"id": web.MODEL_ID, "display_name": "Sol", "efforts": ["high"]}],
                "checks": [
                    {"id": "login", "status": "ok"},
                    {"id": "browser-smoke", "status": "error"},
                    {"id": "tunnel-runtime", "status": "ok"},
                    {"id": "connector", "status": "ok"},
                ],
            },
        )
        fail_new(text, b"browser smoke failed", b"signed out")
        web._write_doctor(home, healthy)
        web._write_doctor(home, web._doctor_layers(tunnel="error", connector="ok"))
        fail_new(tool, b"tool tunnel is not ready", b"tool connector is not selectable")
        web._write_doctor(home, web._doctor_layers(tunnel="ok", connector="error"))
        fail_new(tool, b"tool connector is not selectable", b"tool tunnel is not ready")

    web._run(home, "stop", pin=pin)
    canary_hits: list[str] = []

    class Canary(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            return

        def do_POST(self):  # noqa: N802
            canary_hits.append(self.path)
            self.send_response(500)
            self.end_headers()

    canary = ThreadingHTTPServer(("127.0.0.1", 0), Canary)
    canary_thread = threading.Thread(target=canary.serve_forever, daemon=True)
    canary_thread.start()
    try:
        (home / "process.json").write_text(
            json.dumps(
                {
                    "pid": 1 << 30,
                    "port": canary.server_port,
                    "diagnostic_port": canary.server_port,
                    "executable": str(home / "current" / "runtime" / web.ENTRY_NAME),
                    "private_home": str(home),
                }
            ),
            encoding="utf-8",
        )
        with web._gateway(home, pin, tmp_path / "codex-client-dead") as port:
            status, payload = web._post(port, text)
            assert status == 400, payload
            assert b"process is not running" in payload
            assert b"browser smoke failed" not in payload
            assert len(web._requests(home)) == accepted
            assert canary_hits == []
    finally:
        canary.shutdown()
        canary.server_close()
        canary_thread.join(timeout=2)


def test_accepted_text_is_not_posted_again_when_login_drops(runtime, tmp_path: Path) -> None:
    home, pin = runtime
    (home / "web-home" / "serve-mode").write_text("hold", encoding="utf-8")
    body = web._request_body(
        web.MODEL_ID,
        [web._message("msg_hold", "turn_hold", web.PROMPT)],
        thread_id="thread_hold",
        turn_id="turn_hold",
        effort="high",
    )
    with web._gateway(home, pin, tmp_path / "codex-client") as port:
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
        client.sendall(request)
        seen = b""
        deadline = time.time() + 5
        while b"hello web" not in seen and time.time() < deadline:
            chunk = client.recv(64)
            if not chunk:
                break
            seen += chunk
        assert b"hello web" in seen
        assert len(web._requests(home)) == 1
        web._write_doctor(home, web._doctor([{"id": web.MODEL_ID, "display_name": "Sol", "efforts": ["high"]}], login="error"))
        time.sleep(0.2)
        assert len(web._requests(home)) == 1
        client.shutdown(socket.SHUT_RDWR)
        client.close()
        status, response = web._post(port, body)
        assert status == 400, response
        assert b"signed out" in response
        assert len(web._requests(home)) == 1


def test_restart_accepts_a_new_text_turn_and_rejects_the_old_call(runtime, tmp_path: Path) -> None:
    home, pin = runtime
    with web._gateway(home, pin, tmp_path / "codex-client") as port:
        issued = web._with_tools(
            web._request_body(
                web.MODEL_ID,
                [web._message("msg_restart", "turn_restart", web.PROMPT)],
                thread_id="thread_restart",
                turn_id="turn_restart",
                effort="high",
            )
        )
        status, payload = web._post(port, issued)
        assert status == 200, payload
        assert b"call_turn_restart" in payload
        issued_count = len(web._requests(home))
        stopped = web._run(home, "stop", pin=pin)
        assert stopped.get("process", {}).get("running") is not True
        web._start(home, pin)
        status, payload = web._post(
            port,
            web._request_body(
                web.MODEL_ID,
                [web._message("msg_restart_next", "turn_restart_next", web.PROMPT)],
                thread_id="thread_restart",
                turn_id="turn_restart_next",
                effort="high",
            ),
        )
        assert status == 200, payload
        assert b"hello web" in payload
        assert len(web._requests(home)) == issued_count + 1
        status, payload = web._post(port, web._function_output("thread_restart", "turn_restart", "call_turn_restart"))
        assert status == 400, payload
        assert b"expired" in payload
        assert len(web._requests(home)) == issued_count + 1
