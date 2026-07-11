#!/usr/bin/env python3
"""Smoke replay harness for official Gateway transport diagnostics.

The harness intentionally keeps the default path conservative:

- ``--dry-run`` prints the exact request shape without touching the network.
- Real runs send a single long streaming Responses request through a local
  CodexHub Gateway and record a compact JSON summary.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen


DEFAULT_URL = "http://127.0.0.1:9099/v1/responses"
DEFAULT_MODEL = "gpt-5.5-fast"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def build_payload(*, model: str, prompt_repeat: int, stream: bool) -> dict[str, Any]:
    seed = (
        "Write a detailed architecture-review style response. "
        "Use multiple sections, concrete examples, and avoid tool calls. "
    )
    prompt = seed * max(1, prompt_repeat)
    return {
        "model": model,
        "input": prompt,
        "stream": stream,
        "store": False,
    }


def _gateway_headers(payload_bytes: bytes) -> dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Content-Length": str(len(payload_bytes)),
        "X-Codex-Client-Id": "codex-app",
    }
    gateway_key = os.environ.get("CODEX_PROXY_GATEWAY_CLIENT_KEY")
    if gateway_key:
        headers["Authorization"] = f"Bearer {gateway_key}"
    return headers


def _write_jsonl(path: Path | None, row: dict[str, Any]) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def run_once(args: argparse.Namespace) -> dict[str, Any]:
    payload = build_payload(model=args.model, prompt_repeat=args.prompt_repeat, stream=not args.no_stream)
    payload_bytes = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = _gateway_headers(payload_bytes)
    summary: dict[str, Any] = {
        "ts": _now(),
        "url": args.url,
        "model": args.model,
        "stream": payload["stream"],
        "payload_bytes": len(payload_bytes),
        "prompt_chars": len(str(payload["input"])),
        "has_gateway_authorization": "Authorization" in headers,
        "dry_run": bool(args.dry_run),
    }
    if args.dry_run:
        summary["status"] = "dry_run"
        return summary

    started = time.monotonic()
    request = Request(args.url, data=payload_bytes, headers=headers, method="POST")
    bytes_read = 0
    lines_read = 0
    try:
        with urlopen(request, timeout=args.timeout) as response:
            summary["http_status"] = getattr(response, "status", None)
            if payload["stream"]:
                while True:
                    chunk = response.readline()
                    if not chunk:
                        break
                    lines_read += 1
                    bytes_read += len(chunk)
            else:
                body = response.read()
                bytes_read = len(body)
                lines_read = 1 if body else 0
        summary["status"] = "ok"
    except (OSError, URLError, TimeoutError) as exc:
        summary["status"] = "error"
        summary["error"] = type(exc).__name__
        summary["detail"] = str(exc)
    finally:
        summary["duration_ms"] = int((time.monotonic() - started) * 1000)
        summary["lines_read"] = lines_read
        summary["bytes_read"] = bytes_read
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=DEFAULT_URL, help=f"Gateway Responses URL. Default: {DEFAULT_URL}")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Model to replay. Default: {DEFAULT_MODEL}")
    parser.add_argument("--prompt-repeat", type=int, default=400, help="Repeat count for the long prompt seed.")
    parser.add_argument("--timeout", type=float, default=300.0, help="Socket timeout in seconds for real runs.")
    parser.add_argument("--output", type=Path, default=None, help="Optional JSONL path for run summaries.")
    parser.add_argument("--dry-run", action="store_true", help="Print request metadata without sending it.")
    parser.add_argument("--no-stream", action="store_true", help="Send a non-streaming request.")
    args = parser.parse_args(argv)

    summary = run_once(args)
    _write_jsonl(args.output, summary)
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2))
    return 0 if summary.get("status") in {"ok", "dry_run"} else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
