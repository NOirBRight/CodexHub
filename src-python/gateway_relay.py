"""Gateway upstream response relay primitives."""

from collections.abc import Callable, Iterable, Mapping
from typing import BinaryIO, Protocol


class RelayResponse(Protocol):
    status: int | None
    code: int | None
    headers: Mapping[str, str]

    def read(self) -> bytes: ...


class RequestAdmission(Protocol):
    def raise_if_cancelled(self) -> None: ...


class RelayWriter(Protocol):
    close_connection: bool
    wfile: BinaryIO

    def send_response(self, status: int) -> None: ...
    def send_header(self, key: str, value: str) -> None: ...
    def end_headers(self) -> None: ...


class FilteredHeaders(Protocol):
    def __call__(
        self,
        headers: Mapping[str, str],
        is_event_stream: bool,
        *,
        content_length: int | None = None,
    ) -> Iterable[tuple[str, str]]: ...


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
    response: RelayResponse,
    upstream_name: str,
    *,
    writer: RelayWriter,
    filtered_headers: FilteredHeaders,
    active_request: Callable[[], RequestAdmission | None],
) -> int:
    """Forward a non-SSE upstream response without protocol mutation."""
    status = response.status or response.code or 502
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
