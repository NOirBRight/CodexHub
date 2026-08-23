"""Official-passthrough and transparent Gateway SSE relay paths.

Frame helpers and ``relay_upstream_response`` stay in ``gateway_relay``.
These two paths only read ``RelayContext`` symbols at call time.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from typing import Any

from gateway_relay import RelayContext


def relay_official_passthrough_sse_response(
    relay_context: RelayContext,
    response: Any,
    upstream_name: str,
    *,
    request_id: str | None = None,
    model: str | None = None,
    upstream_format: str = "responses",
    inbound_format: str = "responses",
    usage_capture: dict[str, Any] | None = None,
    headers_already_sent: bool = False,
    mark_downstream_sse_started: Callable[[], None] | None = None,
    event_context: Mapping[str, Any] | None = None,
    defer_stream_errors: bool = False,
) -> int:
    self = relay_context.handler
    IncompleteRead = relay_context.symbols.IncompleteRead
    SseFrameTooLargeError = relay_context.symbols.SseFrameTooLargeError
    URLError = relay_context.symbols.URLError
    UpstreamStreamIncompleteError = relay_context.symbols.UpstreamStreamIncompleteError
    UpstreamStreamInterruptedError = relay_context.symbols.UpstreamStreamInterruptedError
    _UpstreamSseReaderLifecycle = relay_context.symbols._UpstreamSseReaderLifecycle
    _active_gateway_request = relay_context.symbols._active_gateway_request
    _bind_downstream_stream_commit = relay_context.symbols._bind_downstream_stream_commit
    _bind_handler_synthetic_terminal_failure = relay_context.symbols._bind_handler_synthetic_terminal_failure
    _capture_usage = relay_context.symbols._capture_usage
    _filtered_response_headers = relay_context.symbols._filtered_response_headers
    _handler_downstream_stream_commit = relay_context.symbols._handler_downstream_stream_commit
    _observe_gateway_diagnostic = relay_context.symbols._observe_gateway_diagnostic
    _offer_official_passthrough_usage_line = relay_context.symbols._offer_official_passthrough_usage_line
    _responses_event_commits_downstream_output = relay_context.symbols._responses_event_commits_downstream_output
    _responses_synthetic_terminal_failure = relay_context.symbols._responses_synthetic_terminal_failure
    _responses_terminal_observer = relay_context.symbols._responses_terminal_observer
    _route_failure_event_fields = relay_context.symbols._route_failure_event_fields
    logger = relay_context.symbols.logger
    safe_upstream_error_detail = relay_context.symbols.safe_upstream_error_detail

    status = getattr(response, "status", None) or getattr(response, "code", 502)
    headers_sent_downstream = bool(headers_already_sent)
    route_failure_event_fields = _route_failure_event_fields(event_context)
    _write_proxy_event = relay_context.symbols.write_proxy_event

    def write_proxy_event(event: str, **fields: Any) -> None:
        enriched = dict(fields)
        enriched.update(route_failure_event_fields)
        _write_proxy_event(event, **enriched)

    admission = _active_gateway_request()
    request_scoped_seam = _handler_downstream_stream_commit(self)
    if request_scoped_seam is not None:
        request_scoped_seam.set_terminal_observer(_responses_terminal_observer)
        request_scoped_seam.set_output_observer(
            lambda event: _responses_event_commits_downstream_output(event, "")
        )
        request_scoped_seam.set_synthetic_terminal_failure_callback(
            _bind_handler_synthetic_terminal_failure(self, _responses_synthetic_terminal_failure)
        )
        request_scoped_seam.set_usage_line_callback(_offer_official_passthrough_usage_line)
        seam = request_scoped_seam
    else:
        seam = _bind_downstream_stream_commit(
            self,
            response,
            upstream_name,
            model=model,
            request_id=request_id,
            inbound_format=inbound_format,
            terminal_observer=_responses_terminal_observer,
            synthetic_terminal_failure_callback=_responses_synthetic_terminal_failure,
        )
    _capture_usage(usage_capture, None, missing_reason="async_official_passthrough")

    def _last_upstream_byte_age_ms(now: float, last_at: float | None) -> int | None:
        return None if last_at is None else int(max(0.0, now - last_at) * 1000)

    def _emit_stream_closed(
        *,
        status_code: int,
        error: str,
        detail: str,
        failure_phase: str,
        failure_side: str,
        failure_class: str,
        client_disconnected: bool,
        synthetic_terminal_event_sent: bool,
        synthetic_terminal_event_type: str | None,
        synthetic_terminal_write_error: str | None,
        synthetic_terminal_write_detail: str | None,
    ) -> None:
        close_phase = seam.close_phase
        counters = seam.counters()
        now = time.monotonic()
        write_proxy_event(
            "official_passthrough_stream_closed",
            request_id=request_id,
            model=model,
            upstream=upstream_name,
            status=status_code,
            upstream_format=upstream_format,
            inbound_format=inbound_format,
            error=error,
            detail=detail,
            failure_phase=failure_phase,
            failure_side=failure_side,
            failure_class=failure_class,
            client_disconnected=client_disconnected,
            synthetic_terminal_event_sent=synthetic_terminal_event_sent,
            synthetic_terminal_event_type=synthetic_terminal_event_type,
            synthetic_terminal_write_error=synthetic_terminal_write_error,
            synthetic_terminal_write_detail=synthetic_terminal_write_detail,
            lines_streamed=counters["lines_streamed"],
            bytes_streamed=counters["bytes_streamed"],
            last_upstream_byte_age_ms=_last_upstream_byte_age_ms(
                now, counters["last_upstream_byte_at"]
            ),
            headers_sent_downstream=headers_sent_downstream,
            downstream_sse_started=headers_sent_downstream,
            close_phase=close_phase,
            **seam.stats(),
        )

    def _handle_downstream_header_failure() -> int:
        write_error = seam.last_write_error()
        exc = write_error if write_error is not None else OSError("downstream closed")
        _emit_stream_closed(
            status_code=499,
            error=type(exc).__name__,
            detail=safe_upstream_error_detail(exc),
            failure_phase=seam.close_phase,
            failure_side="downstream_write",
            failure_class="client_disconnected",
            client_disconnected=True,
            synthetic_terminal_event_sent=False,
            synthetic_terminal_event_type=None,
            synthetic_terminal_write_error=None,
            synthetic_terminal_write_detail=None,
        )
        return 499

    def send_downstream_headers_once() -> bool:
        nonlocal headers_sent_downstream
        if headers_sent_downstream:
            return True

        def _send() -> None:
            self.send_response(status)
            for key, value in _filtered_response_headers(response.headers, True):
                self.send_header(key, value)
            self.send_header("X-Codex-Proxy-Upstream", upstream_name)
            self.send_header("Connection", "close")
            self.end_headers()

        if not seam.commit_headers(status, _send):
            return False
        headers_sent_downstream = True
        if mark_downstream_sse_started is not None:
            mark_downstream_sse_started()
        return True

    if not defer_stream_errors:
        if not send_downstream_headers_once():
            return _handle_downstream_header_failure()

    def _handle_cancellation() -> int:
        seam.cancel()
        _emit_stream_closed(
            status_code=503,
            error="GatewayUserRequestedShutdown",
            detail="request cancelled by gateway shutdown",
            failure_phase="upstream_read",
            failure_side="upstream_read",
            failure_class="gateway_shutdown",
            client_disconnected=False,
            synthetic_terminal_event_sent=False,
            synthetic_terminal_event_type=None,
            synthetic_terminal_write_error=None,
            synthetic_terminal_write_detail=None,
        )
        return 503

    def _observed_cancellation() -> int | None:
        """Return a status if cancellation was observed, honoring terminal commitment."""
        if admission is None or not admission.cancelled:
            return None
        if seam.terminal_committed:
            sse_fields = seam.stats()
            if usage_capture is not None:
                usage_capture.update(sse_fields)
            return status
        return _handle_cancellation()

    lifecycle = _UpstreamSseReaderLifecycle(
        response,
        admission=admission,
        logger_hook=logger,
    )
    seam.attach_upstream(lifecycle)
    try:
        while True:
            result = _observed_cancellation()
            if result is not None:
                return result
            line = lifecycle.readline()
            result = _observed_cancellation()
            if result is not None:
                return result
            if not line:
                if defer_stream_errors and not headers_sent_downstream:
                    raise UpstreamStreamIncompleteError("Official stream ended before its first SSE byte")
                break
            if not send_downstream_headers_once():
                return _handle_downstream_header_failure()
            _observe_gateway_diagnostic("observe_sse_line", request_id, len(line))
            if not seam.commit_data(line):
                if seam.terminal_committed:
                    # Terminal ledger is sealed; suppress the post-terminal
                    # upstream line without writing or mislabeling it as a
                    # downstream client disconnect.
                    return status
                close_phase = seam.close_phase
                _emit_stream_closed(
                    status_code=499,
                    error="OSError",
                    detail=f"downstream_client_closed ({close_phase})",
                    failure_phase="downstream_write",
                    failure_side="downstream_write",
                    failure_class="downstream_client_closed",
                    client_disconnected=True,
                    synthetic_terminal_event_sent=False,
                    synthetic_terminal_event_type=None,
                    synthetic_terminal_write_error=None,
                    synthetic_terminal_write_detail=None,
                )
                return 499
    except (IncompleteRead, TimeoutError, OSError, URLError, SseFrameTooLargeError) as exc:
        if seam.terminal_committed:
            sse_fields = seam.stats()
            if usage_capture is not None:
                usage_capture.update(sse_fields)
            return status
        if admission is not None and admission.cancelled:
            return _handle_cancellation()
        if defer_stream_errors and not headers_sent_downstream:
            raise UpstreamStreamInterruptedError(exc) from exc
        if seam.downstream_closed:
            _emit_stream_closed(
                status_code=499,
                error=type(exc).__name__,
                detail=safe_upstream_error_detail(exc),
                failure_phase="stream_body",
                failure_side="upstream_read",
                failure_class="downstream_client_closed",
                client_disconnected=True,
                synthetic_terminal_event_sent=False,
                synthetic_terminal_event_type=None,
                synthetic_terminal_write_error=None,
                synthetic_terminal_write_detail=None,
            )
            return 499
        (
            synthetic_terminal_event_sent,
            synthetic_terminal_write_error,
            synthetic_terminal_write_detail,
        ) = seam.commit_terminal_failure(exc, status=502)
        if seam.downstream_closed and seam.last_write_error() is not None:
            return _handle_downstream_header_failure()
        sse_fields = seam.stats()
        if usage_capture is not None:
            usage_capture.update(sse_fields)
            usage_capture["synthetic_terminal_event_sent"] = synthetic_terminal_event_sent
            if synthetic_terminal_event_sent:
                usage_capture["synthetic_terminal_event_type"] = "response.failed"
            if synthetic_terminal_write_error is not None:
                usage_capture["synthetic_terminal_write_error"] = synthetic_terminal_write_error
        _emit_stream_closed(
            status_code=502,
            error=type(exc).__name__,
            detail=safe_upstream_error_detail(exc),
            failure_phase="stream_body",
            failure_side="upstream_read",
            failure_class=getattr(exc, "classification", "upstream_stream_interrupted"),
            client_disconnected=False,
            synthetic_terminal_event_sent=synthetic_terminal_event_sent,
            synthetic_terminal_event_type="response.failed" if synthetic_terminal_event_sent else None,
            synthetic_terminal_write_error=synthetic_terminal_write_error,
            synthetic_terminal_write_detail=synthetic_terminal_write_detail,
        )
        return 502
    finally:
        lifecycle.close()
        lifecycle.join(timeout=_UpstreamSseReaderLifecycle.JOIN_TIMEOUT_SECONDS)

    self.close_connection = True
    sse_fields = seam.stats()
    if usage_capture is not None:
        usage_capture.update(sse_fields)
    return status


def relay_transparent_upstream_response(
    relay_context: RelayContext,
    response: Any,
    upstream_name: str,
    *,
    request_id: str | None = None,
    model: str | None = None,
    upstream_format: str = "responses",
    inbound_format: str = "responses",
    usage_capture: dict[str, Any] | None = None,
    headers_already_sent: bool = False,
    mark_downstream_sse_started: Callable[[], None] | None = None,
    event_context: Mapping[str, Any] | None = None,
    defer_stream_errors: bool = False,
) -> int:
    self = relay_context.handler
    IncompleteRead = relay_context.symbols.IncompleteRead
    RETRY_SAFETY_SUPPRESSED_POST_EXPOSURE = relay_context.symbols.RETRY_SAFETY_SUPPRESSED_POST_EXPOSURE
    RETRY_SAFETY_SUPPRESSED_POST_WRITE = relay_context.symbols.RETRY_SAFETY_SUPPRESSED_POST_WRITE
    SseFrameTooLargeError = relay_context.symbols.SseFrameTooLargeError
    URLError = relay_context.symbols.URLError
    UpstreamStreamErrorEvent = relay_context.symbols.UpstreamStreamErrorEvent
    UpstreamStreamInterruptedError = relay_context.symbols.UpstreamStreamInterruptedError
    _UNSET_CONTENT_ENCODING = relay_context.symbols._UNSET_CONTENT_ENCODING
    _UpstreamSseReaderLifecycle = relay_context.symbols._UpstreamSseReaderLifecycle
    _active_gateway_request = relay_context.symbols._active_gateway_request
    _bind_downstream_stream_commit = relay_context.symbols._bind_downstream_stream_commit
    _bind_handler_synthetic_terminal_failure = relay_context.symbols._bind_handler_synthetic_terminal_failure
    _bounded_failure_event_context = relay_context.symbols._bounded_failure_event_context
    _capture_usage = relay_context.symbols._capture_usage
    _chat_completion_error_payload = relay_context.symbols._chat_completion_error_payload
    _chat_stream_error_detail = relay_context.symbols._chat_stream_error_detail
    _chat_terminal_observer = relay_context.symbols._chat_terminal_observer
    _downstream_stream_error_payload = relay_context.symbols._downstream_stream_error_payload
    _filtered_response_headers = relay_context.symbols._filtered_response_headers
    _get_header = relay_context.symbols._get_header
    _handler_downstream_stream_commit = relay_context.symbols._handler_downstream_stream_commit
    _is_event_stream = relay_context.symbols._is_event_stream
    _observe_gateway_diagnostic = relay_context.symbols._observe_gateway_diagnostic
    _offer_usage_observed_body = relay_context.symbols._offer_usage_observed_body
    _offer_usage_observed_sse_line = relay_context.symbols._offer_usage_observed_sse_line
    _redact_identity_in_text = relay_context.symbols._redact_identity_in_text
    _responses_event_commits_downstream_output = relay_context.symbols._responses_event_commits_downstream_output
    _responses_event_starts_downstream_output = relay_context.symbols._responses_event_starts_downstream_output
    _responses_stream_error_type = relay_context.symbols._responses_stream_error_type
    _responses_synthetic_terminal_failure = relay_context.symbols._responses_synthetic_terminal_failure
    _responses_terminal_observer = relay_context.symbols._responses_terminal_observer
    _retry_identity_from_context = relay_context.symbols._retry_identity_from_context
    _route_failure_event_fields = relay_context.symbols._route_failure_event_fields
    _sse_payload_bytes = relay_context.symbols._sse_payload_bytes
    _usage_observed_context = relay_context.symbols._usage_observed_context
    decoded_request_body = relay_context.symbols.decoded_request_body
    logger = relay_context.symbols.logger
    safe_upstream_error_detail = relay_context.symbols.safe_upstream_error_detail

    status = getattr(response, "status", None) or getattr(response, "code", 502)
    is_event_stream = _is_event_stream(response.headers)
    usage_context = _usage_observed_context(
        event_context,
        request_id=request_id,
        model=model,
        upstream=upstream_name,
        upstream_format=upstream_format,
        inbound_format=inbound_format,
    )
    relay_redact_identity = _retry_identity_from_context(event_context)
    route_failure_event_fields = _route_failure_event_fields(event_context)
    _write_proxy_event = relay_context.symbols.write_proxy_event

    def write_proxy_event(event: str, **fields: Any) -> None:
        enriched = dict(fields)
        enriched.update(route_failure_event_fields)
        _write_proxy_event(event, **enriched)

    admission = _active_gateway_request()
    headers_sent = bool(headers_already_sent)
    chat_mode = inbound_format == "chat_completions"

    request_scoped_seam = _handler_downstream_stream_commit(self)
    synthetic_terminal_failure_callback = (
        None
        if chat_mode
        else _responses_synthetic_terminal_failure
    )
    if request_scoped_seam is not None:
        request_scoped_seam.set_terminal_observer(
            _chat_terminal_observer if chat_mode else _responses_terminal_observer
        )
        request_scoped_seam.set_output_observer(
            None
            if chat_mode
            else (lambda event: _responses_event_commits_downstream_output(event, ""))
        )
        request_scoped_seam.set_usage_line_callback(
            lambda context, line: _offer_usage_observed_sse_line(
                context, line, upstream_format=upstream_format
            )
        )
        request_scoped_seam.set_synthetic_terminal_failure_callback(
            _bind_handler_synthetic_terminal_failure(
                self,
                synthetic_terminal_failure_callback,
                redact_identity=relay_redact_identity,
            )
        )
        seam = request_scoped_seam
    else:
        seam = _bind_downstream_stream_commit(
            self,
            response,
            upstream_name,
            model=model,
            request_id=request_id,
            inbound_format=inbound_format,
            upstream_format=upstream_format,
            terminal_observer=(
                _chat_terminal_observer if chat_mode else _responses_terminal_observer
            ),
            output_observer=(
                None
                if chat_mode
                else (lambda event: _responses_event_commits_downstream_output(event, ""))
            ),
            usage_line_callback=lambda context, line: _offer_usage_observed_sse_line(
                context, line, upstream_format=upstream_format
            ),
            synthetic_terminal_failure_callback=synthetic_terminal_failure_callback,
            redact_identity=relay_redact_identity,
        )
    _capture_usage(usage_capture, None, missing_reason="async_usage_pending")

    def _handle_write_failure() -> int:
        """Emit downstream_stream_closed using the actual OSError when available.

        If commit_data returned False because the terminal was already
        committed, no client disconnect occurred; just return success.
        """
        close_phase = seam.close_phase
        write_error = seam.last_write_error()
        if write_error is None and seam.terminal_committed:
            # Stopped only because a terminal event was already committed.
            return status
        exc = write_error if write_error is not None else OSError("downstream closed")
        event_fields = _bounded_failure_event_context(event_context)
        for key in (
            "request_id",
            "model",
            "upstream",
            "status",
            "upstream_format",
            "inbound_format",
            "error",
            "detail",
        ):
            event_fields.pop(key, None)
        write_proxy_event(
            "downstream_stream_closed",
            request_id=request_id,
            model=model,
            upstream=upstream_name,
            status=499,
            upstream_format=upstream_format,
            inbound_format=inbound_format,
            error=type(exc).__name__,
            detail=safe_upstream_error_detail(exc),
            close_phase=close_phase,
            failure_phase="downstream_write",
            failure_side="downstream_write",
            failure_class="downstream_client_closed",
            client_disconnected=True,
            terminal=seam.terminal_committed,
            terminal_seen=seam._sse_stats.terminal_event_seen,
            downstream_output_started=seam._downstream_output_started,
            retry_forbidden=True,
            retry_safety_class=(
                RETRY_SAFETY_SUPPRESSED_POST_EXPOSURE
                if seam._downstream_content_exposed or seam._downstream_output_started
                else RETRY_SAFETY_SUPPRESSED_POST_WRITE
            ),
            **event_fields,
        )
        return 499

    def send_downstream_headers_once(
        content_length: int | None = None,
        content_encoding: str | None | object = _UNSET_CONTENT_ENCODING,
    ) -> bool:
        nonlocal headers_sent
        if headers_sent:
            return True

        def _send() -> None:
            self.send_response(status)
            for key, value in _filtered_response_headers(
                response.headers,
                is_event_stream,
                content_length=content_length,
                content_encoding=content_encoding,
            ):
                self.send_header(key, value)
            self.send_header("X-Codex-Proxy-Upstream", upstream_name)
            self.send_header("Connection", "close")
            self.end_headers()

        if not seam.commit_headers(status, _send):
            return False
        headers_sent = True
        if is_event_stream and mark_downstream_sse_started is not None:
            mark_downstream_sse_started()
        return True

    if is_event_stream and not (defer_stream_errors and not headers_already_sent):
        if not send_downstream_headers_once():
            return _handle_write_failure()

    def _last_upstream_byte_age_ms(now: float, last_at: float | None) -> int | None:
        return None if last_at is None else int(max(0.0, now - last_at) * 1000)

    def _emit_stream_closed(
        *,
        status_code: int,
        error: str,
        detail: str,
        failure_phase: str,
        failure_side: str,
        failure_class: str,
        client_disconnected: bool,
        synthetic_terminal_event_sent: bool,
        synthetic_terminal_event_type: str | None,
        synthetic_terminal_write_error: str | None,
        synthetic_terminal_write_detail: str | None,
    ) -> None:
        close_phase = seam.close_phase
        counters = seam.counters()
        now = time.monotonic()
        write_proxy_event(
            "transparent_stream_closed",
            request_id=request_id,
            model=model,
            upstream=upstream_name,
            status=status_code,
            upstream_format=upstream_format,
            inbound_format=inbound_format,
            error=error,
            detail=_redact_identity_in_text(detail, relay_redact_identity),
            failure_phase=failure_phase,
            failure_side=failure_side,
            failure_class=failure_class,
            client_disconnected=client_disconnected,
            synthetic_terminal_event_sent=synthetic_terminal_event_sent,
            synthetic_terminal_event_type=synthetic_terminal_event_type,
            synthetic_terminal_write_error=synthetic_terminal_write_error,
            synthetic_terminal_write_detail=synthetic_terminal_write_detail,
            lines_streamed=counters["lines_streamed"],
            bytes_streamed=counters["bytes_streamed"],
            last_upstream_byte_age_ms=_last_upstream_byte_age_ms(
                now, counters["last_upstream_byte_at"]
            ),
            headers_sent_downstream=headers_sent,
            downstream_sse_started=headers_sent,
            close_phase=close_phase,
            **route_failure_event_fields,
            **seam.stats(),
        )

    def _handle_cancellation() -> int:
        seam.cancel()
        _emit_stream_closed(
            status_code=503,
            error="GatewayUserRequestedShutdown",
            detail="request cancelled by gateway shutdown",
            failure_phase="upstream_read",
            failure_side="upstream_read",
            failure_class="gateway_shutdown",
            client_disconnected=False,
            synthetic_terminal_event_sent=False,
            synthetic_terminal_event_type=None,
            synthetic_terminal_write_error=None,
            synthetic_terminal_write_detail=None,
        )
        return 503

    def _observed_cancellation() -> int | None:
        """Return a status if cancellation was observed, honoring terminal commitment."""
        if admission is None or not admission.cancelled:
            return None
        if seam.terminal_committed:
            sse_fields = seam.stats()
            if usage_capture is not None:
                usage_capture.update(sse_fields)
            return status
        return _handle_cancellation()

    def _send_terminal_json_error(
        status_code: int,
        detail: str,
        error_type: str = "upstream_error",
        *,
        telemetry_event: str | None = None,
        telemetry_error: str | None = None,
    ) -> int:
        sanitized_detail = _redact_identity_in_text(detail, relay_redact_identity)
        if telemetry_event is not None:
            write_proxy_event(
                telemetry_event,
                request_id=request_id,
                model=model,
                upstream=upstream_name,
                status=status_code,
                upstream_format=upstream_format,
                inbound_format=inbound_format,
                error=telemetry_error or error_type,
                detail=sanitized_detail,
                **route_failure_event_fields,
            )
        if inbound_format == "chat_completions":
            terminal_payload = _chat_completion_error_payload(
                upstream_name=upstream_name,
                status=status_code,
                detail=sanitized_detail,
                error_type=error_type,
                redact_identity=relay_redact_identity,
            )
        else:
            terminal_payload = _downstream_stream_error_payload(
                upstream_name=upstream_name,
                status=status_code,
                detail=sanitized_detail,
                error_type=error_type,
                redact_identity=relay_redact_identity,
            )
        self._send_json(status_code, terminal_payload)
        self.close_connection = True
        return status_code

    if not is_event_stream:
        body = b""
        try:
            while True:
                result = _observed_cancellation()
                if result is not None:
                    return result
                chunk = response.read(65536)
                result = _observed_cancellation()
                if result is not None:
                    return result
                if not chunk:
                    break
                body += chunk
        except (IncompleteRead, TimeoutError, OSError, URLError) as exc:
            if seam.terminal_committed:
                return status
            if admission is not None and admission.cancelled:
                return _handle_cancellation()
            return _send_terminal_json_error(
                502,
                safe_upstream_error_detail(exc, redact_identity=relay_redact_identity),
                telemetry_event="transparent_body_read_failed",
                telemetry_error=type(exc).__name__,
            )

        drop_content_encoding = False
        if relay_redact_identity is not None and status >= 400:
            content_encoding_value = _get_header(response.headers, "Content-Encoding")
            if content_encoding_value:
                decoded_body, did_decode, decode_error = decoded_request_body(
                    body, content_encoding_value
                )
                if did_decode:
                    body = decoded_body
                    drop_content_encoding = True
                else:
                    detail = (
                        f"upstream {status} response body uses unsupported or malformed "
                        f"Content-Encoding ({content_encoding_value}); cannot safely relay"
                    )
                    if decode_error:
                        detail = f"{detail}: {decode_error}"
                    return _send_terminal_json_error(
                        502,
                        detail,
                        error_type="upstream_protocol_error",
                        telemetry_event="transparent_body_decode_failed",
                        telemetry_error="ContentEncodingDecodeError",
                    )
            body = body.replace(
                relay_redact_identity.encode("utf-8"),
                b"[retry_identity_redacted]",
            )
        content_encoding = None if drop_content_encoding else _UNSET_CONTENT_ENCODING
        if not send_downstream_headers_once(
            content_length=len(body),
            content_encoding=content_encoding,
        ):
            return _handle_write_failure()
        if not self._write_non_streaming_body_relay(body):
            return _handle_write_failure()
        _offer_usage_observed_body(usage_context, body)
        self.close_connection = True
        return status

    pending_lines: list[bytes] = []

    def transparent_error_event(payload: Mapping[str, Any]) -> UpstreamStreamErrorEvent | None:
        if upstream_format == "responses" and _responses_stream_error_type(payload) is not None:
            return UpstreamStreamErrorEvent(payload)
        if upstream_format == "chat_completions" and _chat_stream_error_detail(payload) is not None:
            return UpstreamStreamErrorEvent(payload)
        return None

    def _commit_pending_lines() -> bool:
        """Commit buffered pending lines through the seam. Returns True on success."""
        for pending_line in pending_lines:
            if not seam.commit_data(pending_line):
                return False
        pending_lines.clear()
        return True

    def _handle_stream_failure(exc: BaseException) -> int:
        result = _observed_cancellation()
        if result is not None:
            return result
        if seam.terminal_committed:
            sse_fields = seam.stats()
            if usage_capture is not None:
                usage_capture.update(sse_fields)
            return status
        if defer_stream_errors and not headers_sent:
            raise UpstreamStreamInterruptedError(exc) from exc
        stream_failure_detail = safe_upstream_error_detail(exc, redact_identity=relay_redact_identity)
        if seam.downstream_closed:
            _emit_stream_closed(
                status_code=499,
                error=type(exc).__name__,
                detail=stream_failure_detail,
                failure_phase="stream_body",
                failure_side="upstream_read",
                failure_class="downstream_client_closed",
                client_disconnected=True,
                synthetic_terminal_event_sent=False,
                synthetic_terminal_event_type=None,
                synthetic_terminal_write_error=None,
                synthetic_terminal_write_detail=None,
            )
            return 499
        (
            synthetic_terminal_event_sent,
            synthetic_terminal_write_error,
            synthetic_terminal_write_detail,
        ) = seam.commit_terminal_failure(exc, status=502)
        if seam.downstream_closed and seam.last_write_error() is not None:
            return _handle_write_failure()
        sse_fields = seam.stats()
        if usage_capture is not None:
            usage_capture.update(sse_fields)
            usage_capture["synthetic_terminal_event_sent"] = synthetic_terminal_event_sent
            if synthetic_terminal_event_sent:
                usage_capture["synthetic_terminal_event_type"] = (
                    "response.failed" if inbound_format == "responses" else "chat.error"
                )
            if synthetic_terminal_write_error is not None:
                usage_capture["synthetic_terminal_write_error"] = synthetic_terminal_write_error
        synthetic_terminal_event_type = None
        if synthetic_terminal_event_sent:
            synthetic_terminal_event_type = (
                "response.failed" if inbound_format == "responses" else "chat.error"
            )
        self.close_connection = True
        _emit_stream_closed(
            status_code=502,
            error=type(exc).__name__,
            detail=stream_failure_detail,
            failure_phase="stream_body",
            failure_side="upstream_read",
            failure_class=getattr(exc, "classification", "upstream_stream_interrupted"),
            client_disconnected=False,
            synthetic_terminal_event_sent=synthetic_terminal_event_sent,
            synthetic_terminal_event_type=synthetic_terminal_event_type,
            synthetic_terminal_write_error=synthetic_terminal_write_error,
            synthetic_terminal_write_detail=synthetic_terminal_write_detail,
        )
        return 502

    lifecycle = _UpstreamSseReaderLifecycle(
        response,
        admission=admission,
        logger_hook=logger,
    )
    seam.attach_upstream(lifecycle)
    try:
        while True:
            result = _observed_cancellation()
            if result is not None:
                return result
            try:
                line = lifecycle.readline()
            except (IncompleteRead, TimeoutError, OSError, URLError) as exc:
                return _handle_stream_failure(exc)
            result = _observed_cancellation()
            if result is not None:
                return result
            if not line:
                break
            _observe_gateway_diagnostic("observe_sse_line", request_id, len(line))
            if defer_stream_errors and not headers_sent:
                pending_lines.append(line)
                payload_bytes = _sse_payload_bytes(line)
                if payload_bytes is None:
                    continue
                release_pending = True
                if payload_bytes != b"[DONE]":
                    try:
                        payload = json.loads(payload_bytes.decode("utf-8-sig"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        payload = None
                    if isinstance(payload, Mapping):
                        stream_error = transparent_error_event(payload)
                        if stream_error is not None:
                            raise stream_error
                        if upstream_format == "responses":
                            event_type = payload.get("type")
                            release_pending = (
                                event_type == "response.completed"
                                or _responses_event_starts_downstream_output(payload)
                            )
                if not release_pending:
                    continue
                if not send_downstream_headers_once():
                    return _handle_write_failure()
                if not _commit_pending_lines():
                    return _handle_write_failure()
                continue
            if not send_downstream_headers_once():
                return _handle_write_failure()
            if not seam.commit_data(line):
                return _handle_write_failure()
        if pending_lines and not headers_sent:
            if not send_downstream_headers_once():
                return _handle_write_failure()
            if not _commit_pending_lines():
                return _handle_write_failure()
        self.close_connection = True
        sse_fields = seam.stats()
        if usage_capture is not None:
            usage_capture.update(sse_fields)
        return status
    except SseFrameTooLargeError as exc:
        return _handle_stream_failure(exc)
    except UpstreamStreamErrorEvent:
        # Stream error events are intentionally raised without sending headers
        # so the caller can retry. The seam owns any bytes already committed.
        raise
    finally:
        lifecycle.close()
        lifecycle.join(timeout=_UpstreamSseReaderLifecycle.JOIN_TIMEOUT_SECONDS)
