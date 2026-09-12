#!/usr/bin/env python3
"""Isolated live proof that tool-result images are not stringified on compact.

Starts a throwaway Gateway and a recording reverse proxy in front of xAI.
Never writes credentials, image bytes, or conversation text into the report.
Ordinary pytest never invokes the live path.
"""
from __future__ import annotations

from python_runtime_contract import require_python_313

require_python_313(__file__)

import argparse
import hashlib
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import secrets
import shutil
import socket
import ssl
import struct
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import zlib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src-python"))

CONTINUE_SENTINEL = "E2E_IMAGE_COMPACT_CONTINUE_OK"
VISUAL_NOTICE = "Visual content from tool results was omitted from this compact summary."
XAI_BASE = "https://api.x.ai/v1"
REQUIRED_SOURCE_FILES = (
    "proxy/settings.json",
    "proxy/config/providers.toml",
    "proxy/official-editor-catalog.json",
    "proxy/xai_auth.json",
    "model-catalogs/codexhub-model-catalog.json",
)


def png_solid(red: int, green: int, blue: int, size: int = 32) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    row = b"\x00" + bytes((red, green, blue)) * size
    raw = row * size
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def data_url_for_png(png: bytes) -> str:
    import base64

    return "data:image/png;base64," + base64.b64encode(png).decode("ascii")


def collect_non_media_text(value: Any, *, in_media: bool = False) -> str:
    if isinstance(value, str):
        return "" if in_media else value
    if isinstance(value, list):
        return "".join(collect_non_media_text(item, in_media=in_media) for item in value)
    if isinstance(value, dict):
        chunks: list[str] = []
        for key, item in value.items():
            child_media = in_media or key in {"image_url", "url", "file_id"}
            chunks.append(collect_non_media_text(item, in_media=child_media))
        return "".join(chunks)
    return ""


def count_user_input_images(payload: Mapping[str, Any]) -> int:
    items = payload.get("input")
    if not isinstance(items, list):
        return 0
    count = 0
    for item in items:
        if not isinstance(item, Mapping):
            continue
        if item.get("type") != "message" or item.get("role") != "user":
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, Mapping) and part.get("type") == "input_image":
                count += 1
    return count


def rewrite_xai_base_url(toml_text: str, new_url: str) -> str:
    needle = 'id = "xai"\nname = "xAI"\nbase_url = "https://api.x.ai/v1"'
    if needle not in toml_text:
        raise RuntimeError("isolated_providers_missing_xai_base_url")
    return toml_text.replace(needle, f'id = "xai"\nname = "xAI"\nbase_url = "{new_url}"', 1)


def _compact_history(image_urls: list[str]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "Inspect the attached screenshots and remember the unique colors."}],
        }
    ]
    for index, url in enumerate(image_urls, start=1):
        call_id = f"e2e_img_{index}"
        items.append(
            {
                "type": "function_call",
                "call_id": call_id,
                "name": "inspect_image",
                "arguments": "{}",
                "status": "completed",
            }
        )
        items.append(
            {
                "type": "function_call_output",
                "call_id": call_id,
                "output": [
                    {"type": "input_text", "text": f"captured screenshot {index}"},
                    {"type": "input_image", "image_url": url},
                ],
            }
        )
    items.append(
        {
            "type": "message",
            "role": "user",
            "content": [
                {
                    "type": "input_text",
                    "text": (
                        "Create a detailed summary of the conversation so far. "
                        "Do not call any tools. Respond with text only. "
                        "The summary should include <summary> and </summary>."
                    ),
                }
            ],
        }
    )
    return items


def _read_sse_text(body: bytes) -> tuple[str, dict[str, Any] | None]:
    texts: list[str] = []
    completed: dict[str, Any] | None = None
    for raw_line in body.splitlines():
        if not raw_line.startswith(b"data:"):
            continue
        payload_bytes = raw_line[5:].strip()
        if payload_bytes in {b"", b"[DONE]"}:
            continue
        try:
            payload = json.loads(payload_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        event_type = payload.get("type")
        if event_type == "response.output_text.delta" and isinstance(payload.get("delta"), str):
            texts.append(payload["delta"])
        if event_type in {"response.completed", "response.incomplete", "response.failed"}:
            completed = payload
    if completed is None:
        try:
            parsed = json.loads(body.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            parsed = None
        if isinstance(parsed, dict):
            completed = parsed
    return "".join(texts), completed


class RecordingProxy:
    def __init__(self, upstream_base: str):
        self.upstream_base = upstream_base.rstrip("/")
        self.captures: list[dict[str, Any]] = []
        self._server: ThreadingHTTPServer | None = None
        self.port = 0

    def start(self) -> None:
        captures = self.captures
        upstream_base = self.upstream_base
        context = ssl.create_default_context()

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args: object) -> None:
                return

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length) if length else b""
                record: dict[str, Any] = {
                    "path": self.path,
                    "bytes": len(body),
                    "sha256": hashlib.sha256(body).hexdigest(),
                }
                try:
                    payload = json.loads(body.decode("utf-8-sig"))
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                    payload = None
                if isinstance(payload, dict):
                    record["payload"] = payload
                    record["input_count"] = (
                        len(payload["input"]) if isinstance(payload.get("input"), list) else None
                    )
                captures.append(record)
                headers = {
                    key: value
                    for key, value in self.headers.items()
                    if key.lower() not in {"host", "content-length", "connection"}
                }
                headers["Host"] = "api.x.ai"
                request_path = self.path or "/"
                if request_path.startswith("/v1/"):
                    url = "https://api.x.ai" + request_path
                else:
                    url = upstream_base + request_path
                try:
                    with urlopen(
                        Request(url, data=body, headers=headers, method="POST"),
                        context=context,
                        timeout=180,
                    ) as response:
                        response_body = response.read()
                        status = getattr(response, "status", 200)
                        content_type = response.headers.get("Content-Type", "application/json")
                except HTTPError as exc:
                    response_body = exc.read()
                    status = exc.code
                    content_type = exc.headers.get("Content-Type", "application/json") if exc.headers else "application/json"
                    record["upstream_status"] = status
                except (URLError, TimeoutError, OSError) as exc:
                    record["upstream_error"] = type(exc).__name__
                    self.send_response(502)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"error":"recorder_upstream_unavailable"}')
                    return
                record["upstream_status"] = status
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(response_body)))
                self.end_headers()
                self.wfile.write(response_body)

        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            self.port = int(listener.getsockname()[1])
        self._server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        thread.start()

    def close(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()


def _wait_health(port: int, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urlopen(f"http://127.0.0.1:{port}/health", timeout=1):
                return
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("candidate_gateway_unavailable")


def _post_gateway(
    *,
    port: int,
    key: str,
    payload: dict[str, Any],
    compact: bool,
    timeout: int,
) -> tuple[int, bytes]:
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    if compact:
        headers["x-codex-turn-metadata"] = json.dumps({"request_kind": "compaction"})
        headers["x-request-kind"] = "compact"
    body = json.dumps(payload).encode("utf-8")
    request = Request(
        f"http://127.0.0.1:{port}/v1/responses",
        data=body,
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return getattr(response, "status", 200), response.read()
    except HTTPError as exc:
        return exc.code, exc.read()


def _event_fields(events_path: Path, name: str) -> list[dict[str, Any]]:
    if not events_path.is_file():
        return []
    matches: list[dict[str, Any]] = []
    for line in events_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("event") == name or event.get("name") == name:
            matches.append(event)
    return matches


def _sanitize_error_excerpt(body: bytes) -> dict[str, Any]:
    excerpt: dict[str, Any] = {"bytes": len(body)}
    try:
        payload = json.loads(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        excerpt["unreadable"] = True
        return excerpt
    if not isinstance(payload, dict):
        return excerpt
    error = payload.get("error", payload)
    if isinstance(error, str):
        text = error
        excerpt["error_type"] = None
        excerpt["error_code"] = payload.get("code")
    elif isinstance(error, dict):
        text = str(error.get("message") or error.get("error") or "")
        excerpt["error_type"] = error.get("type") or error.get("code")
        excerpt["error_code"] = error.get("code") or payload.get("code")
        excerpt["codexhub_code"] = None
        nested = error.get("codexhub_error") if isinstance(error.get("codexhub_error"), dict) else payload.get("codexhub_error")
        if isinstance(nested, dict):
            excerpt["codexhub_code"] = nested.get("code")
            excerpt["codexhub_source"] = nested.get("source")
            if not text:
                text = str(nested.get("message") or "")
    else:
        text = ""
    lowered = text.lower()
    if "data:image" in lowered:
        text = "redacted_image_payload"
    excerpt["message_excerpt"] = text[:240]
    return excerpt


def _classify_upstream_failure(status: int, body: bytes) -> str | None:
    text = body.decode("utf-8", errors="replace").lower()
    if status == 400 and any(
        needle in text
        for needle in ("maximum prompt", "prompt length", "context length", "too many tokens")
    ):
        return "context_overflow"
    if status in {401, 403}:
        return "auth"
    if status == 429 or "rate" in text:
        return "rate_limit"
    if status >= 500:
        return "upstream_5xx"
    if status >= 400:
        return "upstream_4xx"
    return None


def run_isolated(*, source_home: Path, model: str, timeout: int) -> dict[str, Any]:
    source = source_home.expanduser().resolve()
    missing = [name for name in REQUIRED_SOURCE_FILES if not (source / name).is_file()]
    report: dict[str, Any] = {
        "report_version": 1,
        "model": model,
        "passed": False,
        "status": "failed",
    }
    if missing:
        report["status"] = "unverified"
        report["failure_classification"] = "missing_source_home_inputs"
        report["missing"] = missing
        return report

    png_a = png_solid(13, 87, 211)
    png_b = png_solid(240, 17, 96)
    url_a = data_url_for_png(png_a)
    url_b = data_url_for_png(png_b)
    markers = (url_a, url_b)

    with tempfile.TemporaryDirectory(prefix="codexhub-image-compact-e2e-") as directory:
        private = Path(directory)
        server_home = private / "server"
        for name in REQUIRED_SOURCE_FILES:
            target = server_home / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source / name, target)
        recorder = RecordingProxy(XAI_BASE)
        recorder.start()
        try:
            providers_path = server_home / "proxy/config/providers.toml"
            providers_path.write_text(
                rewrite_xai_base_url(
                    providers_path.read_text(encoding="utf-8"),
                    f"http://127.0.0.1:{recorder.port}/v1",
                ),
                encoding="utf-8",
            )
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", 0))
                port = int(listener.getsockname()[1])
            key = secrets.token_hex(32)
            env = dict(os.environ)
            env["CODEX_HOME"] = str(server_home)
            env["CODEX_PROXY_GATEWAY_CLIENT_KEY"] = key
            env["PYTHONPATH"] = str(ROOT / "src-python")
            for name in ("CODEXHUB_CODEX_TARGET_HOME", "CODEXHUB_RUNTIME_HOME", "CODEXHUB_HOME", "CODEX_PROXY_HOME"):
                env.pop(name, None)
            log_path = private / "gateway.log"
            with log_path.open("w", encoding="utf-8") as log:
                server = subprocess.Popen(
                    [sys.executable, str(ROOT / "src-python/codex_proxy.py"), "--host", "127.0.0.1", "--port", str(port)],
                    env=env,
                    stdout=log,
                    stderr=log,
                    cwd=str(ROOT),
                )
            try:
                _wait_health(port)
                compact_payload = {
                    "model": model,
                    "stream": True,
                    "input": _compact_history([url_a, url_b]),
                }
                compact_status, compact_body = _post_gateway(
                    port=port,
                    key=key,
                    payload=compact_payload,
                    compact=True,
                    timeout=timeout,
                )
                capture = recorder.captures[-1] if recorder.captures else None
                outbound = capture.get("payload") if isinstance(capture, dict) else None
                text_blob = collect_non_media_text(outbound) if isinstance(outbound, dict) else ""
                marker_in_text = any(marker in text_blob for marker in markers)
                marker_anywhere = False
                if isinstance(outbound, dict):
                    dumped = json.dumps(outbound)
                    marker_anywhere = any(marker in dumped for marker in markers)
                lifted = count_user_input_images(outbound) if isinstance(outbound, dict) else 0
                events_path = server_home / "proxy/codex-proxy-events.jsonl"
                adapted_events = _event_fields(events_path, "tool_result_media_adapted")
                adapted_counts = None
                if adapted_events:
                    last = adapted_events[-1]
                    adapted_counts = {
                        key: last.get(key)
                        for key in (
                            "structured_image_count",
                            "lifted_image_count",
                            "omitted_image_count",
                            "placeholder_count",
                            "input_bytes",
                            "output_bytes",
                            "request_kind",
                            "compact_placeholder_authorized",
                        )
                    }
                compact_text, compact_completed = _read_sse_text(compact_body)
                overflow = _classify_upstream_failure(compact_status, compact_body)
                report.update(
                    {
                        "compact_http_status": compact_status,
                        "compact_error": _sanitize_error_excerpt(compact_body) if compact_status >= 400 else None,
                        "outbound_bytes": None if capture is None else capture.get("bytes"),
                        "outbound_sha256": None if capture is None else capture.get("sha256"),
                        "outbound_input_count": None if capture is None else capture.get("input_count"),
                        "lifted_user_images": lifted,
                        "marker_in_non_media_text": marker_in_text,
                        "marker_present_in_structured_fields": marker_anywhere and not marker_in_text,
                        "adapted_event_count": len(adapted_events),
                        "adapted_counts": adapted_counts,
                        "compact_failure_class": overflow,
                        "compact_summary_chars": len(compact_text),
                    }
                )
                if capture is None:
                    report["failure_classification"] = "recorder_missed_outbound"
                    report["status"] = "unverified"
                    return report
                if marker_in_text:
                    report["failure_classification"] = "tool_result_images_stringified"
                    report["status"] = "failed"
                    return report
                if overflow == "context_overflow":
                    report["failure_classification"] = "context_overflow"
                    report["status"] = "failed"
                    return report
                if compact_status >= 400:
                    report["failure_classification"] = overflow or "compact_http_error"
                    report["status"] = "unverified"
                    return report
                if lifted != len(markers):
                    report["failure_classification"] = "images_not_lifted"
                    report["status"] = "failed"
                    return report

                follow_input: list[dict[str, Any]] = []
                if compact_text.strip():
                    follow_input.append(
                        {
                            "type": "message",
                            "role": "developer",
                            "content": f"[Compacted conversation context]\n{compact_text.strip()[:4000]}",
                        }
                    )
                follow_input.append(
                    {
                        "type": "message",
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": f"Reply with exactly {CONTINUE_SENTINEL} and nothing else.",
                            }
                        ],
                    }
                )
                follow_status, follow_body = _post_gateway(
                    port=port,
                    key=key,
                    payload={"model": model, "stream": True, "input": follow_input},
                    compact=False,
                    timeout=timeout,
                )
                follow_text, _follow_completed = _read_sse_text(follow_body)
                report["followup_http_status"] = follow_status
                report["followup_has_sentinel"] = CONTINUE_SENTINEL in follow_text
                if follow_status >= 400:
                    report["failure_classification"] = _classify_upstream_failure(follow_status, follow_body) or "followup_http_error"
                    report["status"] = "unverified"
                    return report
                if CONTINUE_SENTINEL not in follow_text:
                    report["failure_classification"] = "followup_missing_sentinel"
                    report["status"] = "failed"
                    report["passed"] = False
                    return report
                report["passed"] = True
                report["status"] = "passed"
                return report
            finally:
                server.terminate()
                try:
                    server.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    server.kill()
        finally:
            recorder.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-home", type=Path, required=True)
    parser.add_argument("--model", default="xai/grok-4.6")
    parser.add_argument("--timeout", type=int, default=180)
    parser.add_argument("--output", type=Path, default=Path("test-results/image-tool-compact.json"))
    args = parser.parse_args()
    report = run_isolated(source_home=args.source_home, model=args.model, timeout=args.timeout)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key != "adapted_event"}, indent=2))
    if report.get("status") == "passed":
        return 0
    if report.get("status") == "unverified":
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
