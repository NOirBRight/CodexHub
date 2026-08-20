"""Gateway upstream response relay primitives.

This module owns downstream body writes and raw response forwarding. It is
deliberately independent of codex_proxy; the facade supplies transport and
request-admission callbacks at the seam.
"""

from collections.abc import Callable, Iterable, Mapping
from typing import Any, Protocol


class RelayWriter(Protocol):
    close_connection: bool

    def send_response(self, status: int) -> None: ...
    def send_header(self, key: str, value: str) -> None: ...
    def end_headers(self) -> None: ...
    @property
    def wfile(self) -> Any: ...


def write_non_streaming_body(writer: RelayWriter, body: bytes) -> bool:
    """Write a complete body, returning False when the downstream closed."""
    try:
        writer.wfile.write(body)
        writer.wfile.flush()
    except OSError:
        writer.close_connection = True
        return False
    return True


def relay_raw_response(
    response: Any,
    upstream_name: str,
    *,
    writer: RelayWriter,
    filtered_headers: Callable[..., Iterable[tuple[str, str]]],
    active_request: Callable[[], Any],
) -> int:
    """Forward a non-SSE upstream response without protocol mutation."""
    status = getattr(response, "status", None) or getattr(response, "code", 502)
    body = response.read()
    admission = active_request()
    if admission is not None:
        admission.raise_if_cancelled()
    writer.send_response(status)
    for key, value in filtered_headers(
        response.headers,
        False,
        content_length=len(body),
    ):
        writer.send_header(key, value)
    writer.send_header("X-Codex-Proxy-Upstream", upstream_name)
    writer.send_header("Connection", "close")
    writer.end_headers()
    if not write_non_streaming_body(writer, body):
        return 499
    writer.close_connection = True
    return status
