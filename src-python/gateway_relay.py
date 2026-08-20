"""Gateway upstream response relay primitives."""

import queue
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
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
        return commit_sse_bytes(data, observe=observe)
    writer.wfile.write(data)
    writer.wfile.flush()
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
    terminal_committed: bool = False,
) -> bool:
    if terminal_committed:
        return True
    return write_sse_bytes(
        writer,
        b"data: [DONE]\n\n",
        commit_sse_bytes=commit_sse_bytes,
    )

class SseLineLifecycle(Protocol):
    closed: bool

    def start(self) -> None: ...
    def get(self, timeout: float | None = None) -> tuple[str, object]: ...
    def close(self) -> None: ...
    def join(self, timeout: float) -> None: ...


class SseLineContext(Protocol):
    admission: RequestAdmission | None
    keepalive_interval: float
    transport_timeout_seconds: float
    model_event_timeout_seconds: float
    lifecycle_factory: Callable[[object, RequestAdmission | None], SseLineLifecycle]
    attach_upstream: Callable[[SseLineLifecycle], None]
    write_keepalive: Callable[[], bool]
    idle_timeout_error: Callable[[float, str], BaseException]
    keepalive_failure_error: Callable[[str], BaseException]
    join_timeout_seconds: float


@dataclass(frozen=True)
class SseLineRelayContext:
    admission: RequestAdmission | None
    keepalive_interval: float
    transport_timeout_seconds: float
    model_event_timeout_seconds: float
    lifecycle_factory: Callable[[object, RequestAdmission | None], SseLineLifecycle]
    attach_upstream: Callable[[SseLineLifecycle], None]
    write_keepalive: Callable[[], bool]
    idle_timeout_error: Callable[[float, str], BaseException]
    keepalive_failure_error: Callable[[str], BaseException]
    join_timeout_seconds: float


def iter_upstream_sse_lines(
    response: object,
    *,
    context: SseLineContext,
    downstream_output_started: Callable[[], bool] | None = None,
    line_resets_idle_timeout: Callable[[bytes], bool] | None = None,
    on_line: Callable[[bytes], None] | None = None,
):
    admission = context.admission
    lifecycle = context.lifecycle_factory(response, admission)
    context.attach_upstream(lifecycle)
    lifecycle.start()
    keepalive_interval = context.keepalive_interval
    transport_timeout_seconds = context.transport_timeout_seconds
    model_event_timeout_seconds = context.model_event_timeout_seconds
    transport_idle_guard_enabled = transport_timeout_seconds > 0
    model_event_idle_guard_enabled = model_event_timeout_seconds > 0 and line_resets_idle_timeout is not None
    try:
        stream_started_at = time.monotonic()
        last_transport_at = stream_started_at
        last_model_event_at = stream_started_at
        last_keepalive_at = stream_started_at
        while True:
            if admission is not None:
                admission.raise_if_cancelled()
            now = time.monotonic()
            timeout_seconds: float | None = None
            if keepalive_interval > 0:
                timeout_seconds = max(0.001, keepalive_interval - (now - last_keepalive_at))
            if transport_idle_guard_enabled:
                remaining_idle = transport_timeout_seconds - (now - last_transport_at)
                if remaining_idle <= 0:
                    lifecycle.close()
                    raise context.idle_timeout_error(transport_timeout_seconds, "transport")
                timeout_seconds = remaining_idle if timeout_seconds is None else max(0.001, min(timeout_seconds, remaining_idle))
            if model_event_idle_guard_enabled:
                remaining_idle = model_event_timeout_seconds - (now - last_model_event_at)
                if remaining_idle <= 0:
                    lifecycle.close()
                    raise context.idle_timeout_error(model_event_timeout_seconds, "model_event")
                timeout_seconds = remaining_idle if timeout_seconds is None else max(0.001, min(timeout_seconds, remaining_idle))
            if admission is not None:
                timeout_seconds = 0.1 if timeout_seconds is None else max(0.001, min(timeout_seconds, 0.1))
            try:
                kind, value = lifecycle.get(timeout=timeout_seconds)
            except queue.Empty:
                if admission is not None:
                    admission.raise_if_cancelled()
                if lifecycle.closed:
                    return
                now = time.monotonic()
                if transport_idle_guard_enabled and now - last_transport_at >= transport_timeout_seconds:
                    lifecycle.close()
                    raise context.idle_timeout_error(transport_timeout_seconds, "transport")
                if model_event_idle_guard_enabled and now - last_model_event_at >= model_event_timeout_seconds:
                    lifecycle.close()
                    raise context.idle_timeout_error(model_event_timeout_seconds, "model_event")
                if keepalive_interval > 0:
                    if not context.write_keepalive():
                        lifecycle.close()
                        raise context.keepalive_failure_error("downstream keepalive write failed")
                    last_keepalive_at = time.monotonic()
                continue
            if kind == "error":
                if admission is not None:
                    admission.raise_if_cancelled()
                raise value
            if isinstance(value, bytes) and value:
                now = time.monotonic()
                last_transport_at = now
                if model_event_idle_guard_enabled and line_resets_idle_timeout(value):
                    last_model_event_at = now
                if on_line is not None:
                    try:
                        on_line(value)
                    except Exception:
                        pass
            yield value
            if not value:
                return
    finally:
        lifecycle.close()
        lifecycle.join(timeout=context.join_timeout_seconds)
