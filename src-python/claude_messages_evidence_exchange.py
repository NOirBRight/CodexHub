"""Bounded, isolated HTTP exchange helpers for Claude Messages evidence tests.

Production Gateway routing never imports this module.
"""

from __future__ import annotations

import json
import math
import queue
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Iterable, Mapping

from anthropic_messages_ir import (
    MAX_BUFFERED_RESPONSE_BYTES,
    UPSTREAM_FORMATS,
    AdaptedResponse,
    ChatToAnthropicEmitter,
    NotForwardable,
    anthropic_error_body,
    anthropic_error_sse,
    content_type_is_sse,
    response_refusal,
    adapt_upstream_response,
    adapt_upstream_stream,
    prepare_upstream_request,
)
from gateway_errors import UpstreamStreamIncompleteError
from gateway_sse import DownstreamStreamCommit
from gateway_transport import UpstreamSseReaderLifecycle
from protocol_translation import (
    ResponsesToChatStreamConverter,
    UnsupportedProtocolTranslationError,
    decode_protocol_json,
)
from sse_events import (
    DEFAULT_MAX_FRAME_BYTES,
    SseAssemblerClosedError,
    SseEvent,
    SseEventAssembler,
    SseFrameTooLargeError,
)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


class _IncrementalCancelled(Exception):
    pass


class _IncrementalDeadlineExceeded(TimeoutError):
    pass


class _IncrementalResponseTooLarge(ValueError):
    pass


def _native_terminal_kind(event: SseEvent) -> str | None:
    event_name = (
        event.event.decode("utf-8", errors="replace").strip()
        if event.event is not None
        else ""
    )
    payload: Any = None
    if event.data:
        try:
            payload = decode_protocol_json(event.data)
        except (UnicodeError, json.JSONDecodeError, UnsupportedProtocolTranslationError):
            payload = None
    payload_type = payload.get("type") if isinstance(payload, Mapping) else None
    if event_name == "error" or payload_type == "error":
        return "error"
    if event_name == "message_stop" or payload_type == "message_stop":
        return "success"
    return None


def _close_incremental_response(response: Any) -> None:
    """Abort the underlying socket before closing a blocked stdlib response."""
    for name in ("cancel",):
        action = getattr(response, name, None)
        if callable(action):
            try:
                action()
            except Exception:
                pass
    candidates = (
        getattr(response, "_sock", None),
        getattr(getattr(response, "fp", None), "_sock", None),
        getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None),
        getattr(getattr(response, "connection", None), "sock", None),
        getattr(getattr(getattr(response, "_response", None), "connection", None), "sock", None),
    )
    seen: set[int] = set()
    for candidate in candidates:
        if candidate is None or id(candidate) in seen:
            continue
        seen.add(id(candidate))
        shutdown = getattr(candidate, "shutdown", None)
        if callable(shutdown):
            try:
                shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        close_socket = getattr(candidate, "close", None)
        if callable(close_socket):
            try:
                close_socket()
            except OSError:
                pass
    close = getattr(response, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            pass


class _AbortableNativeResponse:
    """Give the shared reader lifecycle a bounded socket-abort close seam."""

    def __init__(self, response: Any) -> None:
        self._response = response

    def readline(self) -> bytes:
        return self._response.readline()

    def close(self) -> None:
        _close_incremental_response(self._response)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._response, name)


def _open_native_response(request: urllib.request.Request, timeout: float) -> Any:
    """Open exactly one no-redirect, no-retry native attempt."""
    return urllib.request.build_opener(_NoRedirect()).open(request, timeout=timeout)


def relay_incremental_exchange(
    request_body: bytes,
    *,
    upstream_format: str,
    url: str,
    admit: Callable[[str, str, str, bytes], Any],
    commit: DownstreamStreamCommit,
    timeout: float = 30.0,
    headers: Mapping[str, str] | None = None,
    cancelled: Callable[[], bool] | None = None,
    max_response_bytes: int = MAX_BUFFERED_RESPONSE_BYTES,
    open_response: Callable[[urllib.request.Request, float], Any] | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> int | NotForwardable:
    """Relay one SSE attempt without buffering the upstream body.

    Native Anthropic bytes are forwarded as ``SseEvent.raw``. Responses and Chat
    Completions are converted incrementally into Anthropic SSE frames. ``commit``
    remains the sole write/terminal owner. The default opener makes one
    no-redirect attempt and never uses Gateway retry policy.
    """

    selected = str(upstream_format or "").strip().lower()
    if selected not in UPSTREAM_FORMATS:
        return response_refusal("unsupported_upstream_format", selected or "missing")
    prepared = prepare_upstream_request(request_body, selected)
    if isinstance(prepared, NotForwardable):
        return prepared
    try:
        payload = decode_protocol_json(prepared.body)
    except (UnicodeError, json.JSONDecodeError, UnsupportedProtocolTranslationError):
        payload = None
    if not isinstance(payload, Mapping) or payload.get("stream") is not True:
        return response_refusal("incremental_requires_stream", "stream")
    if (
        isinstance(max_response_bytes, bool)
        or not isinstance(max_response_bytes, int)
        or max_response_bytes <= 0
    ):
        return response_refusal("invalid_response_limit", "max_response_bytes")
    try:
        timeout_is_finite = math.isfinite(timeout)
    except (OverflowError, TypeError):
        timeout_is_finite = False
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not timeout_is_finite
        or timeout <= 0
    ):
        return response_refusal("invalid_timeout", "timeout")
    if cancelled is not None and cancelled():
        commit.cancel()
        return 499

    method = "POST"
    request_headers = {"content-type": "application/json", "accept": "text/event-stream"}
    request_headers.update(headers or {})
    admission_started = monotonic()
    reservation = admit(selected, method, url, prepared.body)
    deadline_remaining = getattr(reservation, "deadline_remaining", None)
    try:
        deadline_is_finite = math.isfinite(deadline_remaining)
    except (OverflowError, TypeError):
        deadline_is_finite = False
    if (
        isinstance(deadline_remaining, bool)
        or not isinstance(deadline_remaining, (int, float))
        or not deadline_is_finite
        or deadline_remaining < 0
    ):
        commit.cancel()
        return response_refusal("invalid_admission_reservation", "deadline_remaining")
    absolute_deadline = admission_started + float(deadline_remaining)
    if not math.isfinite(absolute_deadline):
        commit.cancel()
        return response_refusal("invalid_admission_reservation", "deadline_remaining")
    attempt_deadline = min(absolute_deadline, admission_started + float(timeout))
    remaining = attempt_deadline - monotonic()
    if remaining <= 0:
        commit.cancel()
        return response_refusal("admission_deadline_exhausted", "deadline_remaining")

    request = urllib.request.Request(
        url,
        data=prepared.body,
        headers=request_headers,
        method=method,
    )
    opener = open_response or _open_native_response
    response: Any | None = None
    lifecycle: UpstreamSseReaderLifecycle | None = None
    try:
        try:
            response = opener(request, min(float(timeout), remaining))
        except urllib.error.HTTPError as error:
            response = error
        except (OSError, TimeoutError, urllib.error.URLError):
            commit.cancel()
            return 502
        try:
            response_status = int(getattr(response, "status", None) or getattr(response, "code", 200) or 200)
        except (TypeError, ValueError):
            commit.cancel()
            return 502
        response_headers = getattr(response, "headers", {})
        response_type = response_headers.get("content-type", "") if response_headers is not None else ""
        if not isinstance(response_type, str) or not content_type_is_sse(response_type):
            _close_incremental_response(response)
            return response_refusal("unsupported_incremental_response", "content_type")

        lifecycle = UpstreamSseReaderLifecycle(
            _AbortableNativeResponse(response),
            cancellation_requested=cancelled,
        )
        commit.attach_upstream(lifecycle)

        def read_lines(_response: Any, **_kwargs: Any) -> Iterable[bytes]:
            lifecycle.start()
            while True:
                if cancelled is not None and cancelled():
                    raise _IncrementalCancelled()
                remaining_now = attempt_deadline - monotonic()
                if remaining_now <= 0:
                    raise _IncrementalDeadlineExceeded()
                try:
                    kind, value = lifecycle.get(timeout=min(0.1, remaining_now))
                except queue.Empty:
                    continue
                if kind == "error":
                    raise value
                if not isinstance(value, bytes):
                    raise ValueError("upstream SSE reader returned non-bytes")
                yield value
                if not value:
                    return

        def iter_events() -> Iterable[SseEvent]:
            # Match gateway_relay.iter_upstream_sse_events, including deferred
            # SseFrameTooLargeError after pending complete events. Copied so the
            # isolated prototype can cap max_frame_bytes without importing the
            # production relay stack.
            assembler = SseEventAssembler(
                max_frame_bytes=min(DEFAULT_MAX_FRAME_BYTES, max_response_bytes)
            )
            pending_events: list[SseEvent] = []
            assembler_finished = False
            deferred_size_error: SseFrameTooLargeError | None = None

            def assemble_chunk(chunk: bytes) -> None:
                nonlocal deferred_size_error
                events: list[SseEvent] = []
                try:
                    assembler.feed(chunk, on_event=events.append)
                except SseFrameTooLargeError as exc:
                    deferred_size_error = exc
                pending_events.extend(events)

            try:
                for line in read_lines(response):
                    if not line:
                        break
                    assemble_chunk(line)
                    ready = tuple(pending_events)
                    pending_events.clear()
                    yield from ready
                    if deferred_size_error is not None:
                        raise deferred_size_error
                termination = assembler.finish()
                assembler_finished = True
                yield from termination.events
                if termination.disposition == "incomplete":
                    raise UpstreamStreamIncompleteError(
                        "Upstream SSE stream ended with an incomplete pending frame"
                    )
            finally:
                if not assembler_finished:
                    try:
                        assembler.cancel()
                    except SseAssemblerClosedError:
                        pass

        def fail(status: int) -> int:
            if not commit.terminal_committed:
                commit.cancel()
            return status

        terminal_kind: str | None = None
        forwarded = 0
        reader_hung = False
        emitter = ChatToAnthropicEmitter() if selected != "anthropic_messages" else None
        responses_converter = ResponsesToChatStreamConverter() if selected == "responses" else None

        def commit_frames(frames: list[bytes]) -> bool:
            nonlocal forwarded, terminal_kind
            for frame in frames:
                size = len(frame)
                if forwarded + size > max_response_bytes:
                    raise _IncrementalResponseTooLarge()
                if not commit.commit_sse_bytes(frame):
                    terminal_kind = "downstream_closed"
                    return False
                forwarded += size
            return True

        def converted_payload(event: SseEvent) -> Mapping[str, Any] | str | NotForwardable:
            data = event.data.strip() if event.data else b""
            if data == b"[DONE]":
                return "[DONE]"
            if not data:
                return {}
            try:
                payload = decode_protocol_json(data)
            except (UnicodeError, json.JSONDecodeError, UnsupportedProtocolTranslationError):
                return response_refusal("invalid_upstream_stream", "sse.data")
            if isinstance(payload, Mapping):
                return payload
            return response_refusal("invalid_upstream_stream", "sse.data")

        try:
            for event in iter_events():
                if cancelled is not None and cancelled():
                    raise _IncrementalCancelled()
                if selected == "anthropic_messages":
                    size = len(event.raw)
                    if forwarded + size > max_response_bytes:
                        raise _IncrementalResponseTooLarge()
                    if not commit.commit_sse_bytes(event.raw):
                        terminal_kind = "downstream_closed"
                        break
                    forwarded += size
                    terminal_kind = _native_terminal_kind(event)
                    if terminal_kind is not None:
                        break
                    continue

                payload = converted_payload(event)
                if isinstance(payload, NotForwardable):
                    if forwarded == 0:
                        commit.cancel()
                        return payload
                    raise ValueError(payload.reason)
                if isinstance(payload, Mapping) and (
                    event.event == b"error" or payload.get("type") == "error" or payload.get("error") is not None
                    or payload.get("type") in {"response.failed", "response.incomplete"}
                ):
                    error_bytes = anthropic_error_sse(
                        response_status if response_status >= 400 else 502,
                        payload,
                        default="Upstream stream failed",
                    )
                    if not commit_frames([error_bytes]):
                        break
                    terminal_kind = "error"
                    break
                try:
                    if selected == "responses":
                        if not isinstance(payload, Mapping):
                            raise ValueError("responses.event")
                        chat_chunks = responses_converter.chunks_for_event(payload) if responses_converter is not None else []
                        if responses_converter is not None and (
                            not isinstance(responses_converter.response_id, str)
                            or not responses_converter.response_id
                            or not isinstance(responses_converter.model, str)
                            or not responses_converter.model
                        ):
                            missing = response_refusal("unsupported_upstream_stream", "responses.identity")
                            if forwarded == 0:
                                commit.cancel()
                                return missing
                            raise ValueError(missing.reason)
                        for chunk in chat_chunks:
                            frames = emitter.feed(chunk) if emitter is not None else []
                            if isinstance(frames, NotForwardable):
                                if forwarded == 0:
                                    commit.cancel()
                                    return frames
                                raise ValueError(frames.reason)
                            if not commit_frames(frames):
                                break
                        if terminal_kind == "downstream_closed":
                            break
                        if payload.get("type") == "response.completed":
                            frames = emitter.finish(require_done=False) if emitter is not None else []
                            if isinstance(frames, NotForwardable):
                                if forwarded == 0:
                                    commit.cancel()
                                    return frames
                                raise ValueError(frames.reason)
                            if not commit_frames(frames):
                                break
                            terminal_kind = "success"
                            break
                    else:
                        frames = emitter.feed(payload) if emitter is not None else []
                        if isinstance(frames, NotForwardable):
                            if forwarded == 0:
                                commit.cancel()
                                return frames
                            raise ValueError(frames.reason)
                        if not commit_frames(frames):
                            break
                        if payload == "[DONE]":
                            frames = emitter.finish(require_done=True) if emitter is not None else []
                            if isinstance(frames, NotForwardable):
                                if forwarded == 0:
                                    commit.cancel()
                                    return frames
                                raise ValueError(frames.reason)
                            if not commit_frames(frames):
                                break
                            terminal_kind = "success"
                            break
                except UnsupportedProtocolTranslationError as exc:
                    if forwarded == 0:
                        commit.cancel()
                        return response_refusal("unsupported_upstream_stream", getattr(exc, "reason", "stream"))
                    raise ValueError("unsupported_upstream_stream") from exc
                if terminal_kind is not None:
                    break
        finally:
            lifecycle.close()
            joined, outcome = lifecycle.join(timeout=UpstreamSseReaderLifecycle.JOIN_TIMEOUT_SECONDS)
            reader_hung = (not joined) or outcome == "upstream_sse_reader_thread_did_not_terminate"

        cancelled_now = cancelled is not None and cancelled()
        if reader_hung:
            return fail(499 if cancelled_now else 502)
        if terminal_kind is None:
            return fail(499 if cancelled_now else 502)
        if terminal_kind == "downstream_closed":
            return fail(499)
        if terminal_kind == "error":
            if not commit.terminal_committed:
                commit.cancel()
            return response_status if response_status >= 400 else 502
        if not commit.terminal_committed:
            return fail(502)
        return response_status
    except _IncrementalCancelled:
        commit.cancel()
        return 499
    except _IncrementalDeadlineExceeded:
        commit.cancel()
        return 504
    except (_IncrementalResponseTooLarge, SseFrameTooLargeError, UpstreamStreamIncompleteError, OSError, TimeoutError, urllib.error.URLError, ValueError):
        commit.cancel()
        return 502
    finally:
        if response is not None:
            # The reader lifecycle owns normal response closure; this is only a
            # fallback for failures before the lifecycle was attached.
            if lifecycle is None:
                _close_incremental_response(response)


def execute_exchange(
    request_body: bytes,
    *,
    upstream_format: str,
    url: str,
    admit: Callable[[str, str, str, bytes], None],
    timeout: float = 30.0,
    headers: Mapping[str, str] | None = None,
    cancelled: Callable[[], bool] | None = None,
    max_response_bytes: int = MAX_BUFFERED_RESPONSE_BYTES,
) -> AdaptedResponse | NotForwardable:
    """Run one explicitly admitted buffered fixture/live attempt without retries.

    The prototype buffers the response before adapting it; this does not prove
    a production incremental relay or cancellation-safe socket teardown.
    ``admit(protocol, method, final_url, body_bytes)`` is called before any
    socket write.  The callback is the hand-off to the bounded authorized
    runner; this module owns no credentials, budgets, retries, or redirects.
    """

    prepared = prepare_upstream_request(request_body, upstream_format)
    if isinstance(prepared, NotForwardable):
        return prepared
    selected = str(upstream_format).strip().lower()
    method = "POST"
    request_headers = {"content-type": "application/json"}
    try:
        prepared_payload = decode_protocol_json(prepared.body)
    except (UnicodeError, json.JSONDecodeError, UnsupportedProtocolTranslationError):
        prepared_payload = None
    stream_requested = isinstance(prepared_payload, Mapping) and prepared_payload.get("stream") is True
    request_headers["accept"] = "text/event-stream" if stream_requested else "application/json"
    if max_response_bytes <= 0:
        return response_refusal("invalid_response_limit", "max_response_bytes")
    request_headers.update(headers or {})
    admit(selected, method, url, prepared.body)
    request = urllib.request.Request(url, data=prepared.body, headers=request_headers, method=method)
    opener = urllib.request.build_opener(_NoRedirect())
    try:
        with opener.open(request, timeout=timeout) as response:
            response_status = int(response.status)
            response_type = response.headers.get("content-type", "application/json")
            pieces: list[bytes] = []
            total_bytes = 0
            while True:
                piece = response.read(min(64 * 1024, max_response_bytes - total_bytes + 1))
                if not piece:
                    break
                total_bytes += len(piece)
                if total_bytes > max_response_bytes:
                    return response_refusal("upstream_response_too_large", "response.bytes")
                pieces.append(piece)
                if cancelled is not None and cancelled():
                    return adapt_upstream_response(
                        selected,
                        b"",
                        status=response_status,
                        content_type=response_type,
                        cancelled=True,
                    )
            if cancelled is not None and cancelled():
                return adapt_upstream_response(
                    selected,
                    b"",
                    status=response_status,
                    content_type=response_type,
                    cancelled=True,
                )
            if content_type_is_sse(response_type):
                return adapt_upstream_stream(
                    selected,
                    pieces,
                    status=response_status,
                    content_type=response_type,
                    max_buffered_bytes=max_response_bytes,
                )
            return adapt_upstream_response(
                selected,
                b"".join(pieces),
                status=response_status,
                content_type=response_type,
            )
    except urllib.error.HTTPError as error:
        response_type = error.headers.get("content-type", "application/json") if error.headers else "application/json"
        body = error.read(max_response_bytes + 1)
        if len(body) > max_response_bytes:
            return response_refusal("upstream_response_too_large", "error.bytes")
        if not body:
            if content_type_is_sse(response_type):
                return AdaptedResponse(
                    body=anthropic_error_sse(error.code, None, default="Upstream HTTP request failed"),
                    status=error.code,
                    content_type="text/event-stream",
                )
            return AdaptedResponse(
                body=anthropic_error_body(error.code, None, default="Upstream HTTP request failed"),
                status=error.code,
            )
        if content_type_is_sse(response_type):
            return adapt_upstream_stream(selected, (body,), status=error.code, content_type=response_type)
        return adapt_upstream_response(selected, body, status=error.code, content_type=response_type)
