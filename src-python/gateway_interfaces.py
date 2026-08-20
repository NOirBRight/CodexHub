"""Small shared Protocols for Gateway adapter seams."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any, BinaryIO, Protocol


class RequestAdmission(Protocol):
    cancelled: bool

    def attach_upstream_transport(self, transport: Any) -> None: ...
    def raise_if_cancelled(self) -> None: ...


class AdapterEventWriter(Protocol):
    def __call__(self, event_context: Mapping[str, Any] | None, event: str, **fields: Any) -> None: ...


class UpstreamResponseLike(Protocol):
    status: int | None
    code: int | None
    headers: Mapping[str, str]

    def read(self, size: int = -1) -> bytes: ...
    def readline(self) -> bytes: ...


class RelayWriter(Protocol):
    close_connection: bool
    wfile: BinaryIO

    def send_response(self, status: int) -> None: ...
    def send_header(self, key: str, value: str) -> None: ...
    def end_headers(self) -> None: ...
