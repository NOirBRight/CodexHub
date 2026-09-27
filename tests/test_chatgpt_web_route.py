"""Public Gateway HTTP/SSE coverage for the managed ChatGPT Web text route."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import socket
import subprocess
import sys
import tarfile
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import pytest

import chatgpt_web_runtime
import gateway_admission
import gateway_events
import gateway_transport

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "src-python" / "chatgpt_web_runtime.py"
PIN_PATH = ROOT / "config" / "chatgpt_web_runtime_pin.json"
ENTRY_NAME = "bin/codex-chatgpt-web"
MODEL_ID = "chatgpt-web/gpt-5.6-sol"
OTHER_MODEL_ID = "chatgpt-web/gpt-5.6-luna"
PROMPT = "same prompt"


def _run(home: Path, *args: str, pin: Path) -> dict:
    env = os.environ.copy()
    env["CODEXHUB_CHATGPT_WEB_HOME"] = str(home)
    env["CODEXHUB_CHATGPT_WEB_PIN"] = str(pin)
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), *args, "--home", str(home)],
        check=False,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )
    payload = json.loads(completed.stdout or "{}")
    payload["_exit_code"] = completed.returncode
    payload["_stderr"] = completed.stderr
    return payload


def _pin_for(directory: Path, payload: bytes) -> Path:
    document = json.loads(PIN_PATH.read_text(encoding="utf-8"))
    document["artifacts"][chatgpt_web_runtime.artifact_key()]["sha256"] = hashlib.sha256(payload).hexdigest()
    path = directory / "pin.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def _fixture_script() -> str:
    return f"""#!{sys.executable}
import hashlib
import hmac
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

home = Path(os.environ["CODEX_CHATGPT_WEB_HOME"])
home.mkdir(parents=True, exist_ok=True)
with (home / "argv.log").open("a", encoding="utf-8") as handle:
    handle.write(" ".join(sys.argv[1:]) + "\\n")
command = sys.argv[1] if len(sys.argv) > 1 else ""
if command in {{"setup", "dev"}}:
    sys.exit(3)
if command == "doctor":
    doctor = home / "doctor.json"
    sys.stdout.write(doctor.read_text(encoding="utf-8") if doctor.is_file() else '{{"ok":false,"checks":[{{"id":"login","status":"error"}}]}}')
    sys.exit(0)
if command != "serve":
    sys.exit(0)

config = json.loads((home / "config.json").read_text(encoding="utf-8"))
expected = f"Bearer {{config['controlToken']}}".encode("utf-8")

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, fmt, *args):
        return

    def _record(self, body):
        header = self.headers.get("Authorization", "").encode("utf-8")
        matched = hmac.compare_digest(hashlib.sha256(header).digest(), hashlib.sha256(expected).digest()) and header == expected
        try:
            parsed = json.loads(body.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            parsed = None
        record = {{"path": self.path, "authorization_ok": bool(matched), "body": parsed}}
        with (home / "requests.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\\n")
        return matched

    def do_GET(self):
        if self.path.split("?", 1)[0] != "/v1/models":
            self.send_error(404)
            return
        header = self.headers.get("Authorization", "").encode("utf-8")
        if header != expected:
            self.send_error(401)
            return
        body = json.dumps({{"object":"list","data":[{{"id":{MODEL_ID!r}}}]}}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        body = self.rfile.read(length) if length else b""
        if self.path.split("?", 1)[0] != "/v1/responses":
            self.send_error(404)
            return
        if not self._record(body):
            self.send_error(401)
            return
        try:
            parsed = json.loads(body.decode("utf-8"))
        except Exception:
            parsed = None
        mode = (home / "serve-mode").read_text(encoding="utf-8").strip() if (home / "serve-mode").is_file() else "text"

        def emit(payload):
            self.wfile.write(b"data: " + json.dumps(payload).encode("utf-8") + b"\\n\\n")
            self.wfile.flush()

        def turn_id():
            try:
                meta = json.loads(parsed["client_metadata"]["x-codex-turn-metadata"])
            except Exception:
                return "turn"
            turn = meta.get("turn_id") if isinstance(meta, dict) else None
            return turn if isinstance(turn, str) and turn else "turn"

        def items():
            raw_items = parsed.get("input") if isinstance(parsed, dict) else None
            return raw_items if isinstance(raw_items, list) else []

        has_output = any(isinstance(item, dict) and item.get("type") == "function_call_output" for item in items())
        declares_tools = isinstance(parsed, dict) and isinstance(parsed.get("tools"), list) and any(isinstance(tool, dict) for tool in parsed["tools"])
        has_call = any(isinstance(item, dict) and item.get("type") == "function_call" for item in items())
        if mode == "error":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            emit({{"type": "error", "error": {{"type": "server_error", "message": "upstream failed"}}}})
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        if mode in {{"collab", "collab-hold"}} and not has_output:
            turn = turn_id()
            call_id = "call_" + turn
            item_id = "fc_" + turn
            arguments = '{{"task_name":"child","message":"draw"}}'
            partial = {{
                "type": "function_call",
                "id": item_id,
                "call_id": call_id,
                "namespace": "collaboration",
                "name": "spawn_agent",
                "arguments": "",
                "encrypted_function_args": [],
                "status": "in_progress",
            }}
            full = {{
                "type": "function_call",
                "id": item_id,
                "call_id": call_id,
                "namespace": "collaboration",
                "name": "spawn_agent",
                "arguments": arguments,
                "encrypted_function_args": [],
                "status": "completed",
            }}
            emit({{"type": "response.output_item.added", "output_index": 0, "item": partial}})
            emit({{"type": "response.function_call_arguments.delta", "item_id": item_id, "call_id": call_id, "output_index": 0, "delta": arguments}})
            emit({{"type": "response.function_call_arguments.done", "item_id": item_id, "call_id": call_id, "output_index": 0, "arguments": arguments}})
            emit({{"type": "response.output_item.done", "output_index": 0, "item": full}})
            if mode == "collab-hold":
                try:
                    while True:
                        self.wfile.write(b'data: {{"type":"response.output_text.delta","delta":"."}}\\n\\n')
                        self.wfile.flush()
                        time.sleep(0.05)
                except Exception:
                    (home / "upstream-closed").write_text("closed", encoding="utf-8")
                return
            emit({{
                "type": "response.completed",
                "response": {{
                    "id": "resp_web_collab",
                    "object": "response",
                    "status": "completed",
                    "model": {MODEL_ID!r},
                    "output": [full],
                }},
            }})
            return
        toolish = mode == "tool-hold" or (mode != "hold" and (declares_tools or has_call) and not has_output)
        emit({{
            "type": "response.created",
            "response": {{
                "id": "resp_web_tool" if toolish else "resp_web",
                "object": "response",
                "status": "in_progress",
                "model": {MODEL_ID!r},
                "output": [],
            }},
        }})
        if toolish:
            turn = turn_id()
            call_id = "call_" + turn
            item_id = "fc_" + turn
            arguments = '{{"cmd":"pwd"}}'
            partial = {{"type": "function_call", "id": item_id, "call_id": call_id, "name": "shell", "arguments": "", "status": "in_progress"}}
            full = {{"type": "function_call", "id": item_id, "call_id": call_id, "name": "shell", "arguments": arguments, "status": "completed"}}
            if mode == "tool-hold":
                emit({{"type": "response.output_text.delta", "delta": "hello web"}})
            emit({{"type": "response.output_item.added", "output_index": 0, "item": partial}})
            emit({{"type": "response.function_call_arguments.delta", "item_id": item_id, "call_id": call_id, "output_index": 0, "delta": '{{"cmd":'}})
            emit({{"type": "response.function_call_arguments.delta", "item_id": item_id, "call_id": call_id, "output_index": 0, "delta": '"pwd"}}'}})
            emit({{"type": "response.function_call_arguments.done", "item_id": item_id, "call_id": call_id, "output_index": 0, "arguments": arguments}})
            emit({{"type": "response.output_item.done", "output_index": 0, "item": full}})
            if mode == "tool-hold":
                try:
                    while True:
                        self.wfile.write(b'data: {{"type":"response.output_text.delta","delta":"."}}\\n\\n')
                        self.wfile.flush()
                        time.sleep(0.05)
                except Exception:
                    (home / "upstream-closed").write_text("closed", encoding="utf-8")
                return
            emit({{
                "type": "response.completed",
                "response": {{
                    "id": "resp_web_tool",
                    "object": "response",
                    "status": "completed",
                    "model": {MODEL_ID!r},
                    "output": [full],
                }},
            }})
            return
        text = "tool result accepted" if has_output else "hello web"
        emit({{"type": "response.output_text.delta", "delta": text}})
        if mode == "hold":
            try:
                while True:
                    self.wfile.write(b'data: {{"type":"response.output_text.delta","delta":"."}}\\n\\n')
                    self.wfile.flush()
                    time.sleep(0.05)
            except Exception:
                (home / "upstream-closed").write_text("closed", encoding="utf-8")
                return
        completed = {{
            "type": "response.completed",
            "response": {{
                "id": "resp_web",
                "object": "response",
                "status": "completed",
                "model": {MODEL_ID!r},
                "output": [{{
                    "type": "message",
                    "id": "msg_web",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{{"type": "output_text", "text": text, "annotations": []}}],
                }}],
            }},
        }}
        emit(completed)

server = ThreadingHTTPServer(("127.0.0.1", int(config["port"])), Handler)
(home / "listening").write_text(str(config["port"]), encoding="utf-8")
server.serve_forever()
"""


def _archive(directory: Path) -> Path:
    tree = directory / "tree"
    entry = tree / ENTRY_NAME
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text(_fixture_script(), encoding="utf-8")
    entry.chmod(0o755)
    archive = directory / "runtime.tar.gz"
    with tarfile.open(archive, "w:gz") as bundle:
        bundle.add(entry, arcname=ENTRY_NAME)
    return archive


def _doctor(models: list[dict] | None, *, login: str = "ok") -> dict:
    checks = [
        {"id": "login", "status": login},
        {"id": "browser-smoke", "status": "ok"},
        {"id": "tunnel-runtime", "status": "ok"},
        {"id": "connector", "status": "ok"},
    ]
    if login != "ok":
        checks[0]["status"] = "error"
    return {"ok": login == "ok", "models": models or [], "checks": checks}


def _write_doctor(home: Path, document: dict) -> None:
    path = home / "web-home" / "doctor.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")


def _requests(home: Path) -> list[dict]:
    path = home / "web-home" / "requests.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _install(directory: Path) -> tuple[Path, Path]:
    home = directory / "runtime"
    archive = _archive(directory)
    pin = _pin_for(directory, archive.read_bytes())
    installed = _run(home, "install", "--source", str(archive), pin=pin)
    assert installed["_exit_code"] == 0, installed
    return home, pin


def _turn_metadata(thread_id: str, turn_id: str) -> dict[str, str]:
    return {
        "x-codex-turn-metadata": json.dumps(
            {"thread_id": thread_id, "turn_id": turn_id, "request_kind": "turn"}
        )
    }


def _message(item_id: str, turn_id: str, text: str) -> dict:
    return {
        "type": "message",
        "role": "user",
        "id": item_id,
        "internal_chat_message_metadata_passthrough": {"turn_id": turn_id},
        "content": [{"type": "input_text", "text": text}],
    }


def _request_body(model: str, items: list[dict], *, thread_id: str, turn_id: str, effort: str | None) -> dict:
    body = {
        "model": model,
        "stream": True,
        "input": items,
        "prompt_cache_key": thread_id,
        "client_metadata": _turn_metadata(thread_id, turn_id),
    }
    if effort is not None:
        body["reasoning"] = {"effort": effort}
    return body


def _post(port: int, body: dict, *, timeout: float = 8.0) -> tuple[int, bytes]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        payload = json.dumps(body).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {os.environ['CODEX_PROXY_GATEWAY_CLIENT_KEY']}",
        }
        connection.request("POST", "/v1/responses", body=payload, headers=headers)
        response = connection.getresponse()
        return response.status, response.read()
    finally:
        connection.close()


def _get_models(port: int) -> dict:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=8)
    try:
        connection.request(
            "GET",
            "/v1/models",
            headers={"Authorization": f"Bearer {os.environ['CODEX_PROXY_GATEWAY_CLIENT_KEY']}"},
        )
        response = connection.getresponse()
        assert response.status == 200, response.read()
        return json.loads(response.read().decode("utf-8"))
    finally:
        connection.close()


def _identity(payload: dict) -> dict:
    metadata = json.loads(payload["client_metadata"]["x-codex-turn-metadata"])
    items = []
    for item in payload["input"]:
        passthrough = item.get("internal_chat_message_metadata_passthrough") or {}
        text = ""
        content = item.get("content")
        if isinstance(content, list) and content:
            text = str(content[0].get("text") or "")
        items.append({"id": item.get("id"), "turn_id": passthrough.get("turn_id"), "text": text})
    return {
        "model": payload.get("model"),
        "thread_id": metadata.get("thread_id"),
        "turn_id": metadata.get("turn_id"),
        "prompt_cache_key": payload.get("prompt_cache_key"),
        "effort": (payload.get("reasoning") or {}).get("effort"),
        "items": items,
    }


def _sse_events(body: bytes) -> list[dict]:
    events = []
    for chunk in body.split(b"\n\n"):
        data = b"\n".join(
            line[5:].strip() if line.startswith(b"data:") else b""
            for line in chunk.split(b"\n")
            if line.startswith(b"data:")
        )
        if not data or data == b"[DONE]":
            continue
        events.append(json.loads(data.decode("utf-8")))
    return events


@contextmanager
def _gateway(home: Path, pin: Path, codex_home: Path):
    previous = {
        key: os.environ.get(key)
        for key in (
            "CODEX_HOME",
            "CODEXHUB_CHATGPT_WEB_HOME",
            "CODEXHUB_CHATGPT_WEB_PIN",
            "CODEX_PROXY_GATEWAY_CLIENT_KEY",
            "CODEX_WEB_GPT_DEV_HOME",
        )
    }
    os.environ["CODEX_HOME"] = str(codex_home)
    os.environ["CODEXHUB_CHATGPT_WEB_HOME"] = str(home)
    os.environ["CODEXHUB_CHATGPT_WEB_PIN"] = str(pin)
    os.environ["CODEX_PROXY_GATEWAY_CLIENT_KEY"] = "codexhub-web-route-test"
    os.environ.pop("CODEX_WEB_GPT_DEV_HOME", None)
    gateway_events.refresh_runtime_paths()
    from codex_proxy import CodexProxyHandler

    with patch("gateway_transport.getproxies", return_value={"no": "localhost,127.0.0.1"}):
        gateway_transport.OFFICIAL_HTTP_POOLS.clear()
        gateway_transport.STANDARD_HTTP_POOLS.clear()
        server = ThreadingHTTPServer(("127.0.0.1", 0), CodexProxyHandler)
        server.daemon_threads = True
        server.gateway_shutdown_controller = gateway_admission.GatewayShutdownController()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server.server_port
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            gateway_events.refresh_runtime_paths()


def _wait_ready(home: Path, pin: Path) -> None:
    deadline = time.time() + 10
    last: dict = {}
    while time.time() < deadline:
        if (home / "web-home" / "listening").is_file():
            last = _run(home, "status", pin=pin)
            if last.get("ready") is True and last.get("models"):
                return
        time.sleep(0.05)
    raise AssertionError(f"runtime did not become ready: {last}")


def _start(home: Path, pin: Path) -> None:
    started = _run(home, "start", pin=pin)
    assert started["_exit_code"] == 0, started
    try:
        _wait_ready(home, pin)
    except Exception:
        _run(home, "stop", pin=pin)
        raise


@pytest.fixture
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
    try:
        yield home, pin
    finally:
        _run(home, "stop", pin=pin)


def test_streamed_turns_keep_caller_identity_and_do_not_share_a_web_turn(runtime, tmp_path: Path) -> None:
    home, pin = runtime
    with _gateway(home, pin, tmp_path / "codex-client") as port:
        catalog = _get_models(port)
        ids = {item["id"]: item["owned_by"] for item in catalog["data"]}
        assert ids[MODEL_ID] == "chatgpt-web"
        assert OTHER_MODEL_ID not in ids

        first_items = [_message("msg_a_1", "turn_a_1", PROMPT)]
        first = _request_body(MODEL_ID, first_items, thread_id="thread_a", turn_id="turn_a_1", effort="high")
        status, body = _post(port, first)
        assert status == 200, body
        events = _sse_events(body)
        assert any(event.get("type") == "response.output_text.delta" and event.get("delta") == "hello web" for event in events)
        terminals = [event for event in events if event.get("type") in {"response.completed", "response.failed", "response.incomplete"}]
        assert len(terminals) == 1
        assert terminals[0]["type"] == "response.completed"
        assert terminals[0]["response"]["status"] == "completed"

        second_items = [
            _message("msg_a_1", "turn_a_1", PROMPT),
            _message("msg_a_2", "turn_a_2", PROMPT),
        ]
        second = _request_body(MODEL_ID, second_items, thread_id="thread_a", turn_id="turn_a_2", effort="high")
        status, body = _post(port, second)
        assert status == 200, body
        other = _request_body(
            MODEL_ID,
            [_message("msg_b_1", "turn_b_1", PROMPT)],
            thread_id="thread_b",
            turn_id="turn_b_1",
            effort="medium",
        )
        status, body = _post(port, other)
        assert status == 200, body

        captured = [_identity(item["body"]) for item in _requests(home)]
        assert [item["authorization_ok"] for item in _requests(home)] == [True, True, True]
        assert [item["model"] for item in captured] == [MODEL_ID, MODEL_ID, MODEL_ID]
        assert captured[0]["thread_id"] == "thread_a"
        assert captured[0]["turn_id"] == "turn_a_1"
        assert captured[0]["items"] == [{"id": "msg_a_1", "turn_id": "turn_a_1", "text": PROMPT}]
        assert captured[0]["items"][0]["id"] != captured[0]["turn_id"]
        assert captured[1]["thread_id"] == "thread_a"
        assert captured[1]["turn_id"] == "turn_a_2"
        assert captured[1]["prompt_cache_key"] == "thread_a"
        assert captured[1]["items"] == [
            {"id": "msg_a_1", "turn_id": "turn_a_1", "text": PROMPT},
            {"id": "msg_a_2", "turn_id": "turn_a_2", "text": PROMPT},
        ]
        assert captured[2]["thread_id"] == "thread_b"
        assert captured[2]["turn_id"] == "turn_b_1"
        assert captured[2]["turn_id"] not in {captured[0]["turn_id"], captured[1]["turn_id"]}
        assert captured[2]["items"][0]["text"] == PROMPT
        assert {item["effort"] for item in captured} <= {"high", "medium"}

        before = len(_requests(home))
        rejected = _request_body(
            MODEL_ID,
            [_message("msg_a_3", "turn_a_3", PROMPT)],
            thread_id="thread_a",
            turn_id="turn_a_3",
            effort="xhigh",
        )
        status, body = _post(port, rejected)
        assert status == 400, body
        assert b"effort is not in the runtime doctor list" in body
        assert b"hello web" not in body
        assert len(_requests(home)) == before


def test_not_ready_unknown_model_pin_and_dead_process_do_not_open_responses(runtime, tmp_path: Path) -> None:
    home, pin = runtime
    with _gateway(home, pin, tmp_path / "codex-client") as port:
        _write_doctor(home, _doctor([{"id": MODEL_ID, "display_name": "Sol", "efforts": ["high"]}], login="error"))
        body = _request_body(
            MODEL_ID,
            [_message("msg_signed_out", "turn_signed_out", PROMPT)],
            thread_id="thread_signed_out",
            turn_id="turn_signed_out",
            effort="high",
        )
        before = len(_requests(home))
        status, payload = _post(port, body)
        assert status == 400, payload
        assert b"signed out" in payload and b"not ready" in payload
        assert payload.strip()
        assert b"hello web" not in payload
        assert len(_requests(home)) == before

        _write_doctor(home, _doctor([{"id": MODEL_ID, "display_name": "Sol", "efforts": ["high"]}]))
        deadline = time.time() + 5
        while time.time() < deadline:
            status_payload = _run(home, "status", pin=pin)
            if status_payload.get("ready") is True:
                break
            time.sleep(0.05)
        else:
            raise AssertionError(status_payload)
        unknown = _request_body(
            OTHER_MODEL_ID,
            [_message("msg_unknown", "turn_unknown", PROMPT)],
            thread_id="thread_unknown",
            turn_id="turn_unknown",
            effort=None,
        )
        unknown["model"] = OTHER_MODEL_ID
        status, payload = _post(port, unknown)
        assert status == 400, payload
        assert b"not in the runtime doctor list" in payload
        assert len(_requests(home)) == before

        _write_doctor(home, _doctor([]))
        status, payload = _post(port, body)
        assert status == 400, payload
        assert b"not in the runtime doctor list" in payload
        assert len(_requests(home)) == before
        catalog = _get_models(port)
        assert all(not str(item["id"]).startswith("chatgpt-web/") for item in catalog["data"])

    _run(home, "stop", pin=pin)
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
                    "executable": str(home / "current" / "runtime" / ENTRY_NAME),
                    "private_home": str(home),
                }
            ),
            encoding="utf-8",
        )
        with _gateway(home, pin, tmp_path / "codex-client-dead") as port:
            status, payload = _post(port, body)
            assert status == 400, payload
            assert b"process is not running" in payload
            assert canary_hits == []
    finally:
        canary.shutdown()
        canary.server_close()
        canary_thread.join(timeout=2)

    pin_home, pin_path = _install(tmp_path / "incompatible")
    install_path = pin_home / "current" / "install.json"
    document = json.loads(install_path.read_text(encoding="utf-8"))
    document["sha256"] = "f" * 64
    install_path.write_text(json.dumps(document), encoding="utf-8")
    with _gateway(pin_home, pin_path, tmp_path / "codex-client-pin") as port:
        status, payload = _post(port, body)
        assert status == 400, payload
        assert b"pin is incompatible" in payload
        assert _requests(pin_home) == []
        assert canary_hits == []


def test_text_route_ignores_tunnel_and_connector_when_login_and_smoke_passed(runtime, tmp_path: Path) -> None:
    home, pin = runtime
    _write_doctor(
        home,
        {
            "ok": False,
            "models": [{"id": MODEL_ID, "display_name": "Sol", "efforts": ["high"]}],
            "checks": [
                {"id": "login", "status": "ok"},
                {"id": "browser-smoke", "status": "ok"},
                {"id": "connector", "status": "error", "message": "not attached"},
            ],
        },
    )
    supervisor = _run(home, "status", pin=pin)
    assert supervisor["ready"] is False
    assert supervisor["tunnel"]["state"] != "ready"
    assert supervisor["connector"]["selectable"] is False
    assert supervisor["login"]["state"] == "signed_in"
    assert supervisor["browser_smoke"]["state"] == "passed"
    before = len(_requests(home))
    with _gateway(home, pin, tmp_path / "codex-client") as port:
        catalog = _get_models(port)
        ids = {item["id"] for item in catalog["data"]}
        assert MODEL_ID in ids
        body = _request_body(
            MODEL_ID,
            [_message("msg_text_only", "turn_text_only", PROMPT)],
            thread_id="thread_text_only",
            turn_id="turn_text_only",
            effort="high",
        )
        status, payload = _post(port, body)
        assert status == 200, payload
        assert b"hello web" in payload
    captured = _requests(home)
    assert len(captured) == before + 1
    assert captured[-1]["path"].split("?", 1)[0] == "/v1/responses"
    assert captured[-1]["body"]["model"] == MODEL_ID


def test_client_disconnect_closes_the_upstream_body(runtime, tmp_path: Path) -> None:
    home, pin = runtime
    (home / "web-home" / "serve-mode").write_text("hold", encoding="utf-8")
    with _gateway(home, pin, tmp_path / "codex-client") as port:
        payload = json.dumps(
            _request_body(
                MODEL_ID,
                [_message("msg_cancel", "turn_cancel", PROMPT)],
                thread_id="thread_cancel",
                turn_id="turn_cancel",
                effort="high",
            )
        ).encode("utf-8")
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
        client.shutdown(socket.SHUT_RDWR)
        client.close()
        closed = home / "web-home" / "upstream-closed"
        deadline = time.time() + 5
        while not closed.is_file() and time.time() < deadline:
            time.sleep(0.05)
        assert closed.is_file()
        assert len(_requests(home)) == 1
        assert _requests(home)[0]["body"]["client_metadata"]["x-codex-turn-metadata"]


def test_settings_save_during_live_gateway_request_does_not_wait(runtime, tmp_path: Path) -> None:
    home, pin = runtime
    (home / "web-home" / "serve-mode").write_text("hold", encoding="utf-8")
    with _gateway(home, pin, tmp_path / "codex-client") as port:
        payload = json.dumps(
            _request_body(
                MODEL_ID,
                [_message("msg_settings_during_request", "turn_settings_during_request", PROMPT)],
                thread_id="thread_settings_during_request",
                turn_id="turn_settings_during_request",
                effort="high",
            )
        ).encode("utf-8")
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
        try:
            client.sendall(request)
            seen = b""
            deadline = time.time() + 5
            while b"hello web" not in seen and time.time() < deadline:
                chunk = client.recv(128)
                if not chunk:
                    break
                seen += chunk
            assert b"hello web" in seen
            assert len(_requests(home)) == 1

            before = chatgpt_web_runtime.build_status(home)
            started_at = time.monotonic()
            saved = chatgpt_web_runtime.save_settings(home, {"connector_name": "Pending Gateway Settings"})
            elapsed = time.monotonic() - started_at
            after = chatgpt_web_runtime.build_status(home)

            assert elapsed < 2
            assert saved["pending_restart"] is True
            assert after["settings_pending_restart"] is True
            assert after["restart_required"] is False
            assert before["process"]["pid"] == after["process"]["pid"]
            assert after["process"]["running"] is True
        finally:
            client.shutdown(socket.SHUT_RDWR)
            client.close()

        closed = home / "web-home" / "upstream-closed"
        deadline = time.time() + 5
        while not closed.is_file() and time.time() < deadline:
            time.sleep(0.05)
        assert closed.is_file()


SHELL_TOOL = {
    "type": "function",
    "name": "shell",
    "description": "Run one command",
    "parameters": {
        "type": "object",
        "properties": {"cmd": {"type": "string"}},
        "required": ["cmd"],
    },
}


def _with_tools(body: dict, *, sandbox: str | None = None, environment: dict | None = None) -> dict:
    copied = dict(body)
    copied["tools"] = [dict(SHELL_TOOL)]
    if sandbox is not None:
        copied["sandbox"] = sandbox
    if environment is not None:
        copied["environment"] = dict(environment)
    return copied


def _function_output(thread_id: str, turn_id: str, call_id: str, *, item_id: str | None = None) -> dict:
    return _request_body(
        MODEL_ID,
        [
            _message(f"msg_{turn_id}", turn_id, PROMPT),
            {
                "type": "function_call",
                "id": item_id or f"fc_{turn_id}",
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
        effort="high",
    )


def _doctor_layers(*, tunnel: str, connector: str) -> dict:
    return {
        "ok": False,
        "models": [{"id": MODEL_ID, "display_name": "Sol", "efforts": ["high"]}],
        "checks": [
            {"id": "login", "status": "ok"},
            {"id": "browser-smoke", "status": "ok"},
            {"id": "tunnel-runtime", "status": tunnel, "message": "down"},
            {"id": "connector", "status": connector, "message": "not attached"},
        ],
    }


def test_tool_request_without_a_ready_tunnel_or_connector_does_not_open_responses(runtime, tmp_path: Path) -> None:
    home, _pin = runtime
    _write_doctor(home, _doctor_layers(tunnel="error", connector="ok"))
    with _gateway(home, _pin, tmp_path / "codex-client") as port:
        before = len(_requests(home))
        tool = _with_tools(
            _request_body(
                MODEL_ID,
                [_message("msg_tool_blocked", "turn_tool_blocked", PROMPT)],
                thread_id="thread_tool_blocked",
                turn_id="turn_tool_blocked",
                effort="high",
            )
        )
        status, payload = _post(port, tool)
        assert status == 400, payload
        assert b"tool tunnel is not ready" in payload
        assert b"tool connector is not selectable" not in payload
        assert len(_requests(home)) == before

        text = _request_body(
            MODEL_ID,
            [_message("msg_text_same_status", "turn_text_same_status", PROMPT)],
            thread_id="thread_text_same_status",
            turn_id="turn_text_same_status",
            effort="high",
        )
        status, payload = _post(port, text)
        assert status == 200, payload
        assert b"hello web" in payload
        assert len(_requests(home)) == before + 1

        _write_doctor(home, _doctor_layers(tunnel="ok", connector="error"))
        status, payload = _post(port, tool)
        assert status == 400, payload
        assert b"tool connector is not selectable" in payload
        assert b"tool tunnel is not ready" not in payload
        assert len(_requests(home)) == before + 1


def test_function_call_round_trip_stays_on_one_turn_and_replay_does_not_post(runtime, tmp_path: Path) -> None:
    home, _pin = runtime
    with _gateway(home, _pin, tmp_path / "codex-client") as port:
        environment = {"cwd": "/workspace/demo"}
        first = _with_tools(
            _request_body(
                MODEL_ID,
                [_message("msg_tool", "turn_tool", PROMPT)],
                thread_id="thread_tool",
                turn_id="turn_tool",
                effort="high",
            ),
            sandbox="workspace-write",
            environment=environment,
        )
        status, payload = _post(port, first)
        assert status == 200, payload
        events = _sse_events(payload)
        deltas = [event for event in events if event.get("type") == "response.function_call_arguments.delta"]
        assert [event.get("call_id") for event in deltas] == ["call_turn_tool", "call_turn_tool"]
        assert [event.get("item_id") for event in deltas] == ["fc_turn_tool", "fc_turn_tool"]
        assert "".join(str(event.get("delta") or "") for event in deltas) == '{"cmd":"pwd"}'
        function_calls = [
            event["item"]
            for event in events
            if isinstance(event.get("item"), dict) and event["item"].get("type") == "function_call"
        ]
        assert function_calls
        assert {item["call_id"] for item in function_calls} == {"call_turn_tool"}
        assert {item["id"] for item in function_calls} == {"fc_turn_tool"}

        sent = _requests(home)[0]["body"]
        assert sent["tools"] == [SHELL_TOOL]
        assert sent["sandbox"] == "workspace-write"
        assert sent["environment"] == environment
        assert _identity(sent)["thread_id"] == "thread_tool"
        assert _identity(sent)["turn_id"] == "turn_tool"
        assert "environment_context" not in json.dumps(sent)

        unknown = _function_output("thread_tool", "turn_tool", "call_missing")
        status, payload = _post(port, unknown)
        assert status == 400, payload
        assert b"tool call is unknown" in payload
        assert len(_requests(home)) == 1

        output = _function_output("thread_tool", "turn_tool", "call_turn_tool", item_id="fc_turn_tool")
        assert "sandbox" not in output
        assert "environment" not in output
        status, payload = _post(port, output)
        assert status == 200, payload
        assert b"tool result accepted" in payload
        assert len(_requests(home)) == 2
        continued = _requests(home)[1]["body"]
        assert _identity(continued)["thread_id"] == "thread_tool"
        assert _identity(continued)["turn_id"] == "turn_tool"
        assert continued["prompt_cache_key"] == "thread_tool"
        assert "sandbox" not in continued
        assert "environment" not in continued
        calls = [item for item in continued["input"] if item.get("type") == "function_call"]
        results = [item for item in continued["input"] if item.get("type") == "function_call_output"]
        assert calls[0]["call_id"] == "call_turn_tool"
        assert calls[0]["id"] == "fc_turn_tool"
        assert results[0]["call_id"] == "call_turn_tool"

        copied = _function_output("thread_other", "turn_other", "call_turn_tool", item_id="fc_turn_tool")
        status, payload = _post(port, copied)
        assert status == 400, payload
        assert b"another session" in payload
        assert len(_requests(home)) == 2

        other_turn = _function_output("thread_tool", "turn_tool_other", "call_turn_tool", item_id="fc_turn_tool")
        status, payload = _post(port, other_turn)
        assert status == 400, payload
        assert b"not active on this turn" in payload
        assert len(_requests(home)) == 2

        status, payload = _post(port, output)
        assert status == 200, payload
        assert b"tool result accepted" in payload
        assert len(_requests(home)) == 2


def test_tool_client_disconnect_closes_upstream_and_rejects_later_output(runtime, tmp_path: Path) -> None:
    home, _pin = runtime
    (home / "web-home" / "serve-mode").write_text("tool-hold", encoding="utf-8")
    with _gateway(home, _pin, tmp_path / "codex-client") as port:
        body = _with_tools(
            _request_body(
                MODEL_ID,
                [_message("msg_cancel_tool", "turn_cancel_tool", PROMPT)],
                thread_id="thread_cancel_tool",
                turn_id="turn_cancel_tool",
                effort="high",
            )
        )
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
        while b"call_turn_cancel_tool" not in seen and time.time() < deadline:
            chunk = client.recv(256)
            if not chunk:
                break
            seen += chunk
        assert b"call_turn_cancel_tool" in seen
        assert b"fc_turn_cancel_tool" in seen
        client.shutdown(socket.SHUT_RDWR)
        client.close()
        closed = home / "web-home" / "upstream-closed"
        deadline = time.time() + 5
        while not closed.is_file() and time.time() < deadline:
            time.sleep(0.05)
        assert closed.is_file()
        assert len(_requests(home)) == 1
        (home / "web-home" / "serve-mode").write_text("text", encoding="utf-8")
        status, payload = _post(
            port,
            _function_output("thread_cancel_tool", "turn_cancel_tool", "call_turn_cancel_tool"),
        )
        assert status == 400, payload
        assert b"tool call expired" in payload
        assert len(_requests(home)) == 1
