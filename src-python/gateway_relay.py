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
    write_body: Callable[[bytes], bool] | None = None,
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
    if not (write_body or (lambda value: write_non_streaming_body(writer, value)))(body):
        return 499
    writer.close_connection = True
    return status


def send_sse_headers(
    writer: RelayWriter,
    status: int,
    upstream_name: str,
    *,
    commit_headers: Callable[[int, Callable[[], None]], bool] | None = None,
) -> bool:
    """Commit standard downstream SSE headers, optionally through the seam."""
    def send() -> None:
        writer.send_response(status)
        writer.send_header("Content-Type", "text/event-stream; charset=utf-8")
        writer.send_header("Cache-Control", "no-cache")
        writer.send_header("X-Codex-Proxy-Upstream", upstream_name)
        writer.send_header("Connection", "close")
        writer.end_headers()

    return commit_headers(status, send) if commit_headers is not None else (send() or True)


def write_sse_bytes(
    writer: RelayWriter,
    data: bytes,
    *,
    commit_sse_bytes: Callable[[bytes, bool], bool] | None = None,
    observe: bool = True,
) -> bool:
    """Write SSE bytes through the request-scoped commit seam when present."""
    if commit_sse_bytes is not None:
        return commit_sse_bytes(data, observe)
    try:
        writer.wfile.write(data)
        writer.wfile.flush()
    except OSError:
        writer.close_connection = True
        return False
    return True


def write_sse_event(
    writer: RelayWriter,
    event: str,
    payload: Mapping[str, object],
    *,
    encode_json_line: Callable[[Mapping[str, object], bytes], bytes],
    commit_sse_bytes: Callable[[bytes, bool], bool] | None = None,
) -> bool:
    data = f"event: {event}\n".encode("utf-8") + encode_json_line(payload, b"\n") + b"\n"
    return write_sse_bytes(writer, data, commit_sse_bytes=commit_sse_bytes)


def write_sse_data(
    writer: RelayWriter,
    payload: Mapping[str, object],
    *,
    encode_json_line: Callable[[Mapping[str, object], bytes], bytes],
    commit_sse_bytes: Callable[[bytes, bool], bool] | None = None,
) -> bool:
    return write_sse_bytes(
        writer,
        encode_json_line(payload, b"\n") + b"\n",
        commit_sse_bytes=commit_sse_bytes,
    )


def write_sse_keepalive(
    writer: RelayWriter,
    *,
    commit_sse_bytes: Callable[[bytes, bool], bool] | None = None,
) -> bool:
    return write_sse_bytes(
        writer,
        b": codexhub.keepalive\n\n",
        commit_sse_bytes=commit_sse_bytes,
        observe=False,
    )


def write_sse_done(
    writer: RelayWriter,
    *,
    commit_sse_bytes: Callable[[bytes, bool], bool] | None = None,
) -> bool:
    return write_sse_bytes(
        writer,
        b"data: [DONE]\n\n",
        commit_sse_bytes=commit_sse_bytes,
    )
