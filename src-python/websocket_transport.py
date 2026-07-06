from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlsplit


WEBSOCKET_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class WebSocketProtocolError(RuntimeError):
    pass


@dataclass(frozen=True)
class WebSocketFrame:
    fin: bool
    opcode: int
    payload: bytes


def websocket_accept_value(sec_websocket_key: str) -> str:
    digest = hashlib.sha1((sec_websocket_key.strip() + WEBSOCKET_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def websocket_upgrade_response_headers(sec_websocket_key: str, protocol: str | None = None) -> list[tuple[str, str]]:
    headers = [
        ("Upgrade", "websocket"),
        ("Connection", "Upgrade"),
        ("Sec-WebSocket-Accept", websocket_accept_value(sec_websocket_key)),
    ]
    if protocol:
        headers.append(("Sec-WebSocket-Protocol", protocol))
    return headers


def _read_exact(stream: Any, length: int) -> bytes:
    data = stream.read(length)
    if data is None:
        data = b""
    if len(data) != length:
        raise EOFError("websocket stream ended before frame was complete")
    return data


def read_frame(stream: Any, *, expect_masked: bool, max_payload_bytes: int) -> WebSocketFrame:
    first = stream.read(1)
    if first in (b"", None):
        raise EOFError("websocket stream ended")
    second = _read_exact(stream, 1)[0]
    first_byte = first[0]
    fin = bool(first_byte & 0x80)
    opcode = first_byte & 0x0F
    masked = bool(second & 0x80)
    payload_length = second & 0x7F

    if expect_masked and not masked:
        raise WebSocketProtocolError("client websocket frame was not masked")
    if not expect_masked and masked:
        raise WebSocketProtocolError("server websocket frame was masked")

    if payload_length == 126:
        payload_length = int.from_bytes(_read_exact(stream, 2), "big")
    elif payload_length == 127:
        payload_length = int.from_bytes(_read_exact(stream, 8), "big")
    if payload_length > max_payload_bytes:
        raise WebSocketProtocolError("websocket frame exceeded maximum payload size")

    mask_key = _read_exact(stream, 4) if masked else b""
    payload = _read_exact(stream, payload_length)
    if masked:
        payload = bytes(byte ^ mask_key[index % 4] for index, byte in enumerate(payload))
    return WebSocketFrame(fin=fin, opcode=opcode, payload=payload)


def write_frame(stream: Any, frame: WebSocketFrame, *, mask: bool = False) -> None:
    payload = frame.payload
    first = (0x80 if frame.fin else 0) | (frame.opcode & 0x0F)
    length = len(payload)
    if length <= 125:
        header = bytes([first, (0x80 if mask else 0) | length])
    elif length <= 65535:
        header = bytes([first, (0x80 if mask else 0) | 126]) + length.to_bytes(2, "big")
    else:
        header = bytes([first, (0x80 if mask else 0) | 127]) + length.to_bytes(8, "big")

    if mask:
        mask_key = b"\x00\x00\x00\x00"
        payload = bytes(byte ^ mask_key[index % 4] for index, byte in enumerate(payload))
        stream.write(header + mask_key + payload)
    else:
        stream.write(header + payload)


def close_frame(code: int = 1000, reason: str = "") -> WebSocketFrame:
    return WebSocketFrame(
        fin=True,
        opcode=0x8,
        payload=code.to_bytes(2, "big") + reason.encode("utf-8", errors="ignore"),
    )


def pong_frame(payload: bytes) -> WebSocketFrame:
    return WebSocketFrame(fin=True, opcode=0xA, payload=payload)


def _header_items(headers: Mapping[str, str] | Any) -> list[tuple[str, str]]:
    if hasattr(headers, "items"):
        return [(str(key), str(value)) for key, value in headers.items()]
    return []


def _header_value(headers: Mapping[str, str] | Any, name: str) -> str | None:
    wanted = name.lower()
    for key, value in _header_items(headers):
        if key.lower() == wanted:
            return value
    return None


def _selected_subprotocol(headers: Mapping[str, str] | Any) -> str | None:
    value = _header_value(headers, "Sec-WebSocket-Protocol")
    if not value:
        return None
    for item in value.split(","):
        protocol = item.strip()
        if protocol:
            return protocol
    return None


def redacted_handshake_metadata(path: str, headers: Mapping[str, str] | Any) -> dict[str, Any]:
    parsed = urlsplit(path)
    query_keys: list[str] = []
    seen_query_keys: set[str] = set()
    for key, _value in parse_qsl(parsed.query, keep_blank_values=True):
        if key not in seen_query_keys:
            seen_query_keys.add(key)
            query_keys.append(key)
    header_names = sorted({key.lower() for key, _value in _header_items(headers)})
    return {
        "path": parsed.path,
        "query_keys": query_keys,
        "header_names": header_names,
        "selected_subprotocol": _selected_subprotocol(headers),
    }
