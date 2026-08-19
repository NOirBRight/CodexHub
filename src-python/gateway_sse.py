from __future__ import annotations

import json
import time
from collections.abc import Mapping
from typing import Any, Callable, Protocol

from sse_events import (
    DEFAULT_MAX_FRAME_BYTES,
    SseEvent,
    SseEventAssembler,
    SseFrameTooLargeError,
)


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


SSE_EVENT_TYPE_TELEMETRY_LIMIT = 64


def _neutral_error_detail(exc: BaseException) -> str:
    """Fallback OSError summary used until a facade sanitizer is injected."""
    return f"{type(exc).__name__}: {exc}".replace("\r", " ").replace("\n", " ")[:300]


def _noop_usage_line(_context: Mapping[str, Any], _line: bytes) -> None:
    return None


class DownstreamIO(Protocol):
    """Downstream byte sink owned by DownstreamStreamCommit.

    Callers bind request-scoped send_headers/write/close here. The commit
    module never receives the HTTP handler.
    """

    def write(self, data: bytes) -> None: ...

    def close(self) -> None: ...


class PassthroughSseSemanticStats:
    def __init__(
        self,
        *,
        terminal_observer: Callable[[str | None, bytes, Any], bool] | None = None,
        output_observer: Callable[[Mapping[str, Any]], bool] | None = None,
        max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.events_streamed = 0
        self.json_events_streamed = 0
        self.terminal_event_seen = False
        self.completed_event_seen = False
        self.done_sentinel_seen = False
        self.response_event_seen = False
        self.downstream_output_seen = False
        self.last_event_type: str | None = None
        self.response_id: str | None = None
        self.event_type_counts: dict[str, int] = {}
        self.event_types_truncated = False
        self._terminal_observer = terminal_observer
        self._output_observer = output_observer
        self._clock = clock
        self._started_at = self._clock()
        self._first_byte_elapsed_ms: int | None = None
        self._first_event_elapsed_ms: int | None = None
        self._last_event_at: float | None = None
        self._max_inter_event_gap_ms: int | None = None
        self._last_inter_event_gap_ms: int | None = None
        self._terminal_elapsed_ms: int | None = None
        self._assembler = SseEventAssembler(max_frame_bytes=max_frame_bytes)
        self._eof_disposition: str | None = None
        self._incomplete_bytes_discarded = 0

    def observe_bytes(self, chunk: bytes) -> None:
        if self._eof_disposition == "size_limit":
            return
        if chunk and self._first_byte_elapsed_ms is None:
            self._first_byte_elapsed_ms = self._elapsed_ms(self._clock() - self._started_at)
        try:
            self._assembler.feed(chunk, on_event=self._observe_event)
        except SseFrameTooLargeError:
            self._eof_disposition = "size_limit"
            raise

    def finalize_pending(self) -> None:
        if self._eof_disposition is not None:
            return
        termination = self._assembler.finish()
        for event in termination.events:
            self._observe_event(event)
        self._eof_disposition = termination.disposition
        self._incomplete_bytes_discarded = termination.discarded_bytes

    def pending_completion_bytes(self) -> bytes:
        if self._eof_disposition == "size_limit":
            return b""
        return self._assembler.completion_bytes()

    def fields(self) -> dict[str, Any]:
        event_types = sorted(self.event_type_counts)
        fields: dict[str, Any] = {
            "sse_events_streamed": self.events_streamed,
            "sse_json_events_streamed": self.json_events_streamed,
            "sse_terminal_event_seen": self.terminal_event_seen,
            "sse_completed_event_seen": self.completed_event_seen,
            "sse_done_sentinel_seen": self.done_sentinel_seen,
            "sse_response_event_seen": self.response_event_seen,
            "sse_downstream_output_seen": self.downstream_output_seen,
            "sse_event_types": event_types,
            "sse_event_type_counts": {key: self.event_type_counts[key] for key in event_types},
            # The Gateway observes the stream after the response has already
            # been opened.  Successful DNS/TCP/TLS phase duration is therefore
            # intentionally explicit as not observed here; transport failures
            # carry their concrete phase from the upstream-open boundary.
            "upstream_connect_timing": "not_observed",
            "upstream_tls_timing": "not_observed",
            "sse_first_byte_elapsed_ms": self._first_byte_elapsed_ms,
            "sse_first_event_elapsed_ms": self._first_event_elapsed_ms,
            "sse_last_inter_event_gap_ms": self._last_inter_event_gap_ms,
            "sse_max_inter_event_gap_ms": self._max_inter_event_gap_ms,
            "sse_terminal_elapsed_ms": self._terminal_elapsed_ms,
        }
        if self.last_event_type is not None:
            fields["sse_last_event_type"] = self.last_event_type
        if self.event_types_truncated:
            fields["sse_event_types_truncated"] = True
        if self._eof_disposition == "incomplete":
            fields["sse_eof_disposition"] = "incomplete"
            fields["sse_incomplete_bytes_discarded"] = self._incomplete_bytes_discarded
        return fields

    def _observe_event(self, event: SseEvent) -> None:
        observed_at = self._clock()
        if self._first_event_elapsed_ms is None:
            self._first_event_elapsed_ms = self._elapsed_ms(observed_at - self._started_at)
        if self._last_event_at is not None:
            gap_ms = self._elapsed_ms(observed_at - self._last_event_at)
            self._last_inter_event_gap_ms = gap_ms
            if self._max_inter_event_gap_ms is None or gap_ms > self._max_inter_event_gap_ms:
                self._max_inter_event_gap_ms = gap_ms
        self._last_event_at = observed_at
        event_name: str | None = None
        if event.event is not None:
            event_name = event.event.decode("utf-8", errors="replace").strip() or None
        has_data_field = any(line.name == b"data" for line in event.lines)
        if event_name is None and not has_data_field:
            return
        data = event.data

        self.events_streamed += 1
        event_type = event_name
        payload: Any = None
        if data == b"[DONE]":
            self.done_sentinel_seen = True
            event_type = event_type or "[DONE]"
        elif data:
            try:
                payload = json.loads(data.decode("utf-8-sig"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                payload = None
            if isinstance(payload, Mapping):
                self.json_events_streamed += 1
                payload_type = payload.get("type")
                if isinstance(payload_type, str) and payload_type:
                    event_type = payload_type
                if self._output_observer is not None and self._output_observer(payload):
                    self.downstream_output_seen = True
                response = payload.get("response")
                if isinstance(response, Mapping):
                    response_id = response.get("id")
                    if isinstance(response_id, str) and response_id:
                        self.response_id = response_id

        if event_type is None:
            return
        self.last_event_type = event_type
        self._record_event_type(event_type)
        if event_type.startswith("response."):
            self.response_event_seen = True
        if event_type == "response.completed":
            self.completed_event_seen = True
        if self._terminal_observer is not None and self._terminal_observer(event_name, data, payload):
            self.terminal_event_seen = True
            if self._terminal_elapsed_ms is None:
                self._terminal_elapsed_ms = self._elapsed_ms(observed_at - self._started_at)

    def _elapsed_ms(self, value: float) -> int:
        """Convert a monotonic interval to a bounded non-negative integer."""

        if value <= 0:
            return 0
        return min(7 * 24 * 60 * 60 * 1000, int(value * 1000))

    def _record_event_type(self, event_type: str) -> None:
        if event_type in self.event_type_counts:
            self.event_type_counts[event_type] += 1
            return
        if len(self.event_type_counts) >= SSE_EVENT_TYPE_TELEMETRY_LIMIT:
            self.event_types_truncated = True
            return
        self.event_type_counts[event_type] = 1


class DownstreamStreamCommit:
    """Owns downstream writes and terminal commitment for an SSE stream.

    The seam is the single owner of all bytes written to the downstream client for
    a passthrough SSE stream: data events, terminal events, and sanitized error
    events. It tracks whether a terminal event has been committed to the
    downstream, classifies the close phase (before output, during an event, or
    after terminal commitment), and hands off cancellation/closure to the owned
    upstream response so that no later write, retry, fallback, or duplicate
    terminal can occur on this stream.

    Protocol-specific observers (terminal detection, downstream-output
    classification, usage extraction, synthetic terminal formatting, and
    error-detail sanitizing) are injected by the caller; the seam itself
    owns only the lifecycle ledger and byte commitment.

    Downstream is a write/flush/close adapter. Upstream is attached later via
    attach_upstream so cancellation or a downstream write failure can still
    close owned upstream work even if the seam was created before the upstream
    opened. The constructor does not take an HTTP handler.
    """

    def __init__(
        self,
        downstream: Any,
        upstream: Any | None,
        upstream_name: str,
        *,
        model: str | None = None,
        request_id: str | None = None,
        inbound_format: str = "responses",
        upstream_format: str = "responses",
        terminal_observer: Callable[[str | None, bytes, Any], bool] | None = None,
        output_observer: Callable[[Mapping[str, Any]], bool] | None = None,
        usage_line_callback: Callable[[Mapping[str, Any], bytes], None] | None = None,
        synthetic_terminal_failure_callback: Callable[..., tuple[bool, str | None, str | None]]
        | None = None,
        diagnostic_observer: Callable[..., None] | None = None,
        error_detail_callback: Callable[[BaseException], str] | None = None,
        terminal_drain_timeout_seconds: float | None = None,
        max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES,
    ) -> None:
        self._downstream = downstream
        self._upstream_response = upstream
        self._upstream_name = upstream_name
        self._model = model
        self._request_id = request_id
        self._inbound_format = inbound_format
        self._upstream_format = upstream_format
        self._usage_line_callback = (
            usage_line_callback if usage_line_callback is not None else _noop_usage_line
        )
        self._synthetic_terminal_failure_callback = synthetic_terminal_failure_callback
        self._diagnostic_observer = diagnostic_observer
        self._error_detail_callback = (
            error_detail_callback if error_detail_callback is not None else _neutral_error_detail
        )
        self._terminal_drain_timeout_seconds = terminal_drain_timeout_seconds
        self._max_frame_bytes = max_frame_bytes
        self._terminal_observer = terminal_observer
        self._output_observer = output_observer
        self._sse_stats = PassthroughSseSemanticStats(
            terminal_observer=terminal_observer,
            output_observer=output_observer,
            max_frame_bytes=max_frame_bytes,
        )
        self._terminal_observed = False
        self._terminal_committed = False
        self._downstream_closed = False
        self._downstream_output_started = False
        self._downstream_content_exposed = False
        self._terminal_drain_timeout_shortened = False
        self._lines_streamed = 0
        self._bytes_streamed = 0
        self._last_upstream_byte_at: float | None = None
        self._last_write_error: OSError | None = None
        self._last_successful_completion_bytes = b""
        self._headers_committed = False
        self._ensure_headers_committed_callback: Callable[[], bool] | None = None

    @property
    def terminal_committed(self) -> bool:
        return self._terminal_committed

    @property
    def downstream_closed(self) -> bool:
        return self._downstream_closed

    @property
    def close_phase(self) -> str:
        if self._terminal_committed:
            return "after_terminal"
        if self._downstream_output_started:
            return "during_event"
        return "before_output"

    def stats(self) -> dict[str, Any]:
        self._sse_stats.finalize_pending()
        return self._sse_stats.fields()

    def attach_upstream(self, response: Any) -> None:
        """Bind the upstream response after the seam has been created."""
        if self._downstream_closed:
            self._close_upstream_response(response)
            return
        self._upstream_response = response

    attach_upstream_response = attach_upstream

    def set_upstream_format(self, upstream_format: str) -> None:
        """Update the active protocol before a planned fallback is opened."""
        if self._downstream_output_started or self._terminal_committed:
            return
        self._upstream_format = upstream_format

    def mark_downstream_content_exposed(self) -> None:
        """Record that upstream response content has been produced for the client.

        This is semantic exposure: it is set when the upstream emits visible or
        tool output that would be relayed downstream, even if the bytes are still
        buffered in the relay layer and have not yet been written to the socket.
        """
        self._downstream_content_exposed = True

    def set_terminal_observer(self, terminal_observer: Callable[[str | None, bytes, Any], bool] | None) -> None:
        self._terminal_observer = terminal_observer
        self._sse_stats = PassthroughSseSemanticStats(
            terminal_observer=terminal_observer,
            output_observer=self._output_observer,
            max_frame_bytes=self._max_frame_bytes,
        )

    def set_output_observer(self, output_observer: Callable[[Mapping[str, Any]], bool] | None) -> None:
        self._output_observer = output_observer
        self._sse_stats = PassthroughSseSemanticStats(
            terminal_observer=self._terminal_observer,
            output_observer=output_observer,
            max_frame_bytes=self._max_frame_bytes,
        )

    def set_usage_line_callback(self, usage_line_callback: Callable[[Mapping[str, Any], bytes], None] | None) -> None:
        self._usage_line_callback = (
            usage_line_callback if usage_line_callback is not None else _noop_usage_line
        )

    def set_synthetic_terminal_failure_callback(
        self,
        synthetic_terminal_failure_callback: Callable[..., tuple[bool, str | None, str | None]]
        | None,
    ) -> None:
        self._synthetic_terminal_failure_callback = synthetic_terminal_failure_callback

    def set_ensure_headers_committed_callback(
        self,
        callback: Callable[[], bool] | None,
    ) -> None:
        self._ensure_headers_committed_callback = None if self._headers_committed else callback

    def _ensure_headers_committed_before_write(self) -> bool:
        if self._headers_committed:
            self._ensure_headers_committed_callback = None
            return True
        callback = self._ensure_headers_committed_callback
        if callback is None:
            return True
        if not callback():
            return False
        self._ensure_headers_committed_callback = None
        return True

    def _emit_bytes(self, data: bytes) -> None:
        self._downstream.write(data)
        flush = getattr(self._downstream, "flush", None)
        if callable(flush):
            flush()

    def _close_downstream_side(self) -> None:
        close = getattr(self._downstream, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    def _close_upstream_response(self, response: Any) -> None:
        for name in ("close", "cancel"):
            action = getattr(response, name, None)
            if callable(action):
                try:
                    action()
                except Exception:
                    pass
                return

    def _close_upstream(self) -> None:
        response = self._upstream_response
        if response is None:
            return
        self._close_upstream_response(response)

    def close(self) -> None:
        """Close the downstream and upstream sides; idempotent."""
        if self._downstream_closed:
            return
        self._downstream_closed = True
        self._close_downstream_side()
        self._close_upstream()

    def cancel(self) -> None:
        """Hand off cancellation: close downstream and upstream once."""
        self.close()

    def _observe_diagnostic(self, method: str, *, forwarded: bool) -> None:
        observer = self._diagnostic_observer
        if observer is None:
            return
        try:
            observer(method, self._request_id, forwarded=forwarded)
        except Exception:
            return

    def _record_terminal(self) -> None:
        if self._terminal_drain_timeout_shortened:
            return
        timeout = self._terminal_drain_timeout_seconds
        if timeout is not None:
            shorten = getattr(self._upstream_response, "shorten_terminal_drain_timeout", None)
            if callable(shorten):
                try:
                    shorten(timeout)
                except Exception:
                    pass
        self._terminal_drain_timeout_shortened = True

    def _observe_line(self, line: bytes) -> bool:
        """Observe one raw SSE line and return True if it contains a terminal event."""
        self._last_upstream_byte_at = time.monotonic()
        self._sse_stats.observe_bytes(line)
        if self._sse_stats.terminal_event_seen and not self._terminal_observed:
            self._terminal_observed = True
            return True
        return False

    def _seal_terminal(self) -> None:
        self._terminal_committed = True
        self._observe_diagnostic("observe_terminal", forwarded=True)
        self._record_terminal()
        # Terminal ledger is sealed: close the downstream side deterministically
        # so no later upstream byte can be written or mislabeled as a disconnect.
        # Upstream is left open so the caller can drain to the natural EOF.
        self._downstream_closed = True
        self._close_downstream_side()

    def commit_data(self, line: bytes) -> bool:
        """Commit one upstream SSE line to the downstream stream.

        Returns True if the line was written (or was empty). Returns False if the
        downstream stream is closed or a terminal event has already been committed,
        in which case the caller must stop writing.
        """
        if self._downstream_closed:
            return False
        if not line:
            return True
        if self._terminal_committed:
            return False
        if not self._ensure_headers_committed_before_write():
            return False
        terminal_observed_now = self._observe_line(line)
        if terminal_observed_now:
            self._observe_diagnostic("observe_terminal", forwarded=False)
        try:
            self._emit_bytes(line)
        except OSError as write_exc:
            self._last_write_error = write_exc
            self.close()
            return False
        self._downstream_output_started = True
        self._lines_streamed += 1
        self._bytes_streamed += len(line)
        self._last_successful_completion_bytes = self._sse_stats.pending_completion_bytes()
        if terminal_observed_now:
            self._seal_terminal()
        self._usage_line_callback(
            {
                "request_id": self._request_id,
                "model": self._model,
                "upstream": self._upstream_name,
                "upstream_format": self._upstream_format,
                "inbound_format": self._inbound_format,
            },
            line,
        )
        return True

    def commit_headers(self, status: int, send_headers: Callable[[], None]) -> bool:
        """Authorize and record the sending of HTTP response headers.

        send_headers is called only when the stream is still open and no
        terminal has been committed. A successful commit owns the one allowed
        header block and disarms any deferred header callback. Later header
        requests are successful no-ops. Any OSError is captured, closes the
        owned upstream work, and seals the downstream side.
        """
        del status
        if self._headers_committed:
            self._ensure_headers_committed_callback = None
            return True
        if self._downstream_closed or self._terminal_committed:
            return False
        try:
            send_headers()
        except OSError as write_exc:
            self._last_write_error = write_exc
            self.close()
            return False
        self._headers_committed = True
        self._ensure_headers_committed_callback = None
        return True

    def commit_sse_bytes(self, data: bytes, *, observe: bool = True) -> bool:
        """Commit constructed SSE bytes to the downstream stream.

        This is used for retry diagnostics, converted-route events, keepalives,
        and error events that are produced above the raw upstream line layer.
        When observe is True the bytes are inspected for terminal events.
        """
        if self._downstream_closed:
            return False
        if not data:
            return True
        if self._terminal_committed:
            return False
        if not self._ensure_headers_committed_before_write():
            return False
        terminal_observed_now = False
        if observe:
            terminal_observed_now = self._observe_line(data)
            if terminal_observed_now:
                self._observe_diagnostic("observe_terminal", forwarded=False)
        try:
            self._emit_bytes(data)
        except OSError as write_exc:
            self._last_write_error = write_exc
            self.close()
            return False
        self._downstream_output_started = True
        self._lines_streamed += 1
        self._bytes_streamed += len(data)
        if observe:
            self._last_successful_completion_bytes = self._sse_stats.pending_completion_bytes()
        if terminal_observed_now:
            self._seal_terminal()
        return True

    def commit_terminal_failure(
        self,
        exc: BaseException,
        *,
        status: int = 502,
    ) -> tuple[bool, str | None, str | None]:
        """Commit a synthetic terminal failure event if terminal is not committed.

        The pending upstream SSE event is first finalized with a blank-line
        boundary so that the synthetic terminal failure is a distinct, valid
        SSE frame. The response ID is taken from the finalized event stream if
        available.

        Returns (sent, write_error_name, write_error_detail). No event is written
        if the stream is already closed, a terminal has already been committed,
        or no synthetic terminal failure callback is configured, which prevents
        duplicate terminals and fallback writes.
        """
        if self._downstream_closed or self._terminal_committed:
            return False, None, None
        if self._synthetic_terminal_failure_callback is None:
            return False, None, None
        if not self._ensure_headers_committed_before_write():
            return False, None, None
        try:
            size_limit_exceeded = isinstance(exc, SseFrameTooLargeError)
            completion = (
                self._last_successful_completion_bytes
                if size_limit_exceeded
                else self._sse_stats.pending_completion_bytes()
            )
            if completion:
                self._emit_bytes(completion)
                if not size_limit_exceeded:
                    self._sse_stats.observe_bytes(completion)
        except OSError as write_exc:
            self._last_write_error = write_exc
            self.close()
            return False, type(write_exc).__name__, self._error_detail_callback(write_exc)
        response_id = self._sse_stats.response_id
        try:
            (
                synthetic_terminal_event_sent,
                synthetic_terminal_write_error,
                synthetic_terminal_write_detail,
            ) = self._synthetic_terminal_failure_callback(
                exc,
                status=status,
                response_id=response_id,
                upstream_name=self._upstream_name,
                model=self._model,
            )
        except OSError as write_exc:
            self._last_write_error = write_exc
            self.close()
            return False, type(write_exc).__name__, self._error_detail_callback(write_exc)
        if synthetic_terminal_event_sent:
            self._terminal_committed = True
            self._downstream_closed = True
            self._close_downstream_side()
        return (
            synthetic_terminal_event_sent,
            synthetic_terminal_write_error,
            synthetic_terminal_write_detail,
        )

    def last_write_error(self) -> OSError | None:
        return self._last_write_error

    def counters(self) -> dict[str, Any]:
        return {
            "lines_streamed": self._lines_streamed,
            "bytes_streamed": self._bytes_streamed,
            "last_upstream_byte_at": self._last_upstream_byte_at,
        }




