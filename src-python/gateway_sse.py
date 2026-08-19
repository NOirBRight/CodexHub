from __future__ import annotations

import json
from typing import Any


def _sse_line_ending(line: bytes) -> bytes:
    for candidate in (b"\r\n", b"\n", b"\r"):
        if line.endswith(candidate):
            return candidate
    return b"\n"


def _sse_event_separator_after_line(line: bytes) -> bytes:
    if line.endswith((b"\r\n\r\n", b"\n\n", b"\r\r")):
        return b""
    line_ending = _sse_line_ending(line)
    if line.endswith(line_ending):
        return line_ending
    return line_ending + line_ending


def _is_sse_blank_line(line: bytes) -> bool:
    return line in {b"\n", b"\r\n", b"\r"}


def _is_sse_event_metadata_line(line: bytes) -> bool:
    return line.startswith((b"event:", b"id:", b"retry:"))


def _sse_payload_bytes(line: bytes) -> bytes | None:
    if not line.startswith(b"data:"):
        return None

    content = line
    for candidate in (b"\r\n", b"\n", b"\r"):
        if line.endswith(candidate):
            content = line[: -len(candidate)]
            break

    payload_bytes = content[5:].lstrip()
    if not payload_bytes:
        return None
    return payload_bytes


def _parse_sse_json_payload(line: bytes) -> dict[str, Any] | None:
    payload_bytes = _sse_payload_bytes(line)
    if payload_bytes is None:
        return None
    try:
        payload = json.loads(payload_bytes.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _parse_sse_json_payloads(blob: bytes) -> list[dict[str, Any]]:
    """Parse every JSON data frame emitted for one upstream SSE line.

    Runtime compatibility adapters can buffer one upstream function lifecycle
    and emit several native Responses events as a single byte string.  Relay
    bookkeeping must inspect each emitted frame rather than treating that
    byte string as one JSON payload.
    """

    return [
        payload
        for line in blob.splitlines(keepends=True)
        for payload in [_parse_sse_json_payload(line)]
        if payload is not None
    ]


