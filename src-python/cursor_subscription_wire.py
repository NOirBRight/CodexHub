"""Bounded Cursor AgentService protobuf/Connect codec (no generated runtime).

Protocol field mapping adapted from yetone/magpie internal/gateway/cursor.go.
MIT License
Copyright (c) 2026 yetone
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:
The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.
THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import math
import platform
import struct
import uuid
from typing import Any

from subscription_backend_contract import BackendError

MAX_BYTES = 16 * 1024 * 1024
NAMESPACE = "codexhub"
DYNAMIC_TOOL = "CallDynamicTool"


def malformed() -> BackendError:
    return BackendError("upstream-protocol-error", "Cursor returned a malformed stream.")


def varint(value: int) -> bytes:
    if value < 0 or value >= 1 << 64:
        raise malformed()
    out = bytearray()
    while value > 127:
        out.append((value & 127) | 128)
        value >>= 7
    out.append(value)
    return bytes(out)


def number(field: int, value: int) -> bytes:
    return varint(field << 3) + varint(value)


def binary(field: int, value: bytes) -> bytes:
    return varint((field << 3) | 2) + varint(len(value)) + value


def string(field: int, value: str) -> bytes:
    return binary(field, value.encode("utf-8"))


def fields(data: bytes) -> list[tuple[int, int | bytes]]:
    out = []
    at = 0
    def read_varint() -> int:
        nonlocal at
        value = 0
        for shift in range(0, 70, 7):
            if at >= len(data):
                raise malformed()
            byte = data[at]
            at += 1
            if shift == 63 and byte > 1:
                raise malformed()
            value |= (byte & 127) << shift
            if byte < 128:
                return value
        raise malformed()
    while at < len(data):
        key = read_varint()
        field, kind = key >> 3, key & 7
        if not field:
            raise malformed()
        if kind == 0:
            value = read_varint()
        elif kind in (1, 2, 5):
            size = read_varint() if kind == 2 else 8 if kind == 1 else 4
            if size > MAX_BYTES or at + size > len(data):
                raise malformed()
            raw = data[at:at + size]
            at += size
            value = raw if kind == 2 else int.from_bytes(raw, "little")
        else:
            raise malformed()
        out.append((field, value))
        if len(out) > 100000:
            raise malformed()
    return out


def get(data: bytes, field: int, default: Any = b"") -> Any:
    return next((value for key, value in fields(data) if key == field), default)


def text(data: bytes, field: int) -> str:
    value = get(data, field)
    if not isinstance(value, bytes):
        raise malformed()
    try:
        return value.decode("utf-8")
    except UnicodeError:
        raise malformed() from None


def json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def google_value(value: Any, depth: int = 0) -> bytes:
    if depth > 64:
        raise malformed()
    if value is None:
        return number(1, 0)
    if isinstance(value, bool):
        return number(4, int(value))
    if isinstance(value, (float, int)):
        if not math.isfinite(value):
            raise malformed()
        return varint(17) + struct.pack("<d", value)
    if isinstance(value, str):
        return string(3, value)
    if isinstance(value, dict):
        return binary(5, b"".join(binary(1, string(1, key) + binary(2, google_value(item, depth + 1)))
                                 for key, item in sorted(value.items())))
    if isinstance(value, list):
        return binary(6, b"".join(binary(1, google_value(item, depth + 1)) for item in value))
    raise malformed()


def decode_value(data: bytes, depth: int = 0) -> Any:
    if depth > 64:
        raise malformed()
    known = [(key, value) for key, value in fields(data) if key in range(1, 7)]
    if len(known) != 1:
        raise malformed()
    key, value = known[0]
    if key == 1:
        return None
    if key == 2:
        if not isinstance(value, int):
            raise malformed()
        result = struct.unpack("<d", value.to_bytes(8, "little"))[0]
        if not math.isfinite(result):
            raise malformed()
        return result
    if key == 3:
        if not isinstance(value, bytes):
            raise malformed()
        try:
            return value.decode("utf-8")
        except UnicodeError:
            raise malformed() from None
    if key == 4:
        if value not in (0, 1):
            raise malformed()
        return bool(value)
    if not isinstance(value, bytes):
        raise malformed()
    if key == 5:
        result = {}
        for field, entry in fields(value):
            if field == 1:
                name = text(entry, 1)
                if name in result:
                    raise malformed()
                result[name] = decode_value(get(entry, 2), depth + 1)
        return result
    return [decode_value(item, depth + 1) for field, item in fields(value) if field == 1]


def frame(data: bytes, *, end: bool = False) -> bytes:
    if len(data) > MAX_BYTES:
        raise malformed()
    return bytes([2 if end else 0]) + struct.pack(">I", len(data)) + data


class Frames:
    """Incremental framing with both compressed and uncompressed size bounds."""
    def __init__(self) -> None:
        self.buffer = bytearray()

    def feed(self, chunk: bytes) -> list[tuple[bool, bytes]]:
        self.buffer.extend(chunk)
        result = []
        while len(self.buffer) >= 5:
            flags = self.buffer[0]
            size = int.from_bytes(self.buffer[1:5], "big")
            if flags & ~3 or size > MAX_BYTES:
                raise malformed()
            if len(self.buffer) < size + 5:
                break
            data = bytes(self.buffer[5:5 + size])
            del self.buffer[:size + 5]
            if flags & 1:
                try:
                    with gzip.GzipFile(fileobj=io.BytesIO(data)) as stream:
                        data = stream.read(MAX_BYTES + 1)
                except (OSError, EOFError):
                    raise malformed() from None
                if len(data) > MAX_BYTES:
                    raise malformed()
            result.append((bool(flags & 2), data))
        return result


def tool_definition(tool: dict[str, Any]) -> bytes:
    return (string(1, tool["name"]) + string(2, tool.get("description", ""))
            + binary(3, google_value(tool["parameters"])) + string(4, NAMESPACE)
            + string(5, tool["name"]) + string(6, json_bytes(tool["parameters"]).decode()))


def environment() -> bytes:
    # No real workspace path is disclosed; the server cannot execute local tools.
    return string(1, "windows" if platform.system() == "Windows" else "linux") + string(2, "/") + string(10, "UTC")


def build_run(messages: list[dict[str, Any]], last_user: str, tools: list[dict[str, Any]], model: str) -> tuple[bytes, dict[bytes, bytes]]:
    blobs: dict[bytes, bytes] = {}
    blob_bytes = 0
    def put(data: bytes) -> bytes:
        nonlocal blob_bytes
        if len(data) > MAX_BYTES or blob_bytes + len(data) > 64 * 1024 * 1024:
            raise BackendError("input-too-large", "Cursor conversation exceeds the transport size limit.", 400)
        key = hashlib.sha256(data).digest()
        if key not in blobs:
            blob_bytes += len(data)
        blobs[key] = data
        return key
    state = b"".join(binary(1, put(json_bytes(message))) for message in messages)
    mid = str(uuid.uuid4())
    user = string(1, last_user) + string(2, mid) + number(4, 1)
    turn = binary(1, binary(1, put(user)) + string(10, mid))
    state += binary(8, put(turn)) + number(10, 1) + string(22, "cli")
    definitions = [tool_definition(tool) for tool in tools]
    context = binary(4, environment()) + b"".join(binary(7, tool) for tool in definitions)
    request = (binary(1, state) + binary(2, binary(2, binary(2, context)))
               + binary(3, string(1, model) + string(3, model) + string(4, model))
               + binary(4, b"".join(binary(1, tool) for tool in definitions))
               + string(5, str(uuid.uuid4())) + binary(9, string(1, model)) + number(19, 1))
    return binary(1, request), blobs
