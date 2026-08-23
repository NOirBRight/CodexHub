"""Handler-facing relay bindings for Gateway POST requests.

These assemble per-request RelayGlue / RelayContext objects and the
downstream stream-commit seam. Owning-module attributes are read at call
time so test patches stay live.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from http.client import IncompleteRead
from typing import Any
from urllib.error import URLError

import gateway_admission
import gateway_compat
import gateway_errors
import gateway_events
import gateway_sse
import gateway_stream_semantics
import gateway_transport
from gateway_errors import (
    CompactEmptyResponseError,
    UpstreamStreamIdleTimeoutError,
    responses_failed_event_for_stream_error as _responses_failed_event_for_stream_error,
    safe_upstream_error_detail,
)
from gateway_relay import RelayContext, RelayGlue
from gateway_request import (
    UNSET_CONTENT_ENCODING as _UNSET_CONTENT_ENCODING,
    filtered_response_headers as _filtered_response_headers,
    is_event_stream as _is_event_stream,
    decoded_request_body,
)
from gateway_sse import DownstreamStreamCommit
from gateway_stream_semantics import (
    DownstreamKeepaliveFailedError,
    UpstreamEmptyCompletedResponseError,
    UpstreamStreamErrorEvent,
    UpstreamStreamInterruptedError,
)
from gateway_transport import (
    UpstreamSseReaderLifecycle as _UpstreamSseReaderLifecycle,
    get_header as _get_header,
    retry_identity_from_context as _retry_identity_from_context,
)
from protocol_translation import (
    GatewayChatToResponsesStreamConverter as _ChatToResponsesStreamConverter,
    GatewayResponsesToChatStreamConverter as _ResponsesToChatStreamConverter,
    NonForwardable,
    UpstreamStreamIncompleteError,
)
from route_primitives import (
    RETRY_REQUEST_COMPACT,
    RETRY_SAFETY_SUPPRESSED_POST_EXPOSURE,
    RETRY_SAFETY_SUPPRESSED_POST_WRITE,
    StreamingPolicy,
    UsagePolicy,
)
from runtime_tool_compatibility import (
    ToolCompatibilityError as RuntimeToolCompatibilityError,
)
from sse_events import SseFrameTooLargeError

logger = logging.getLogger("codex_proxy")


def _observe_gateway_diagnostic(method: str, *args: Any, **kwargs: Any) -> None:
    gateway_events.observe_gateway_diagnostic(method, *args, **kwargs)


def _write_adapter_event(event_context: Any, event: str, **fields: Any) -> None:
    gateway_events.write_adapter_event(event_context, event, **fields)


def responses_synthetic_terminal_failure(
    handler: Any,
    exc: BaseException,
    *,
    status: int,
    response_id: str | None,
    upstream_name: str,
    model: str | None,
    redact_identity: str | None = None,
) -> tuple[bool, str | None, str | None]:
    """Write a Responses-format synthetic terminal failure event."""
    if not handler._write_sse_event(
        "response.failed",
        _responses_failed_event_for_stream_error(
            upstream_name=upstream_name,
            model=model,
            status=status,
            exc=exc,
            response_id=response_id,
            redact_identity=redact_identity,
        ),
    ):
        seam = _handler_downstream_stream_commit(handler)
        write_exc = seam.last_write_error() if seam is not None else None
        if write_exc is None:
            write_exc = OSError("downstream closed")
        return False, type(write_exc).__name__, safe_upstream_error_detail(write_exc, redact_identity=redact_identity)
    return True, None, None
_responses_synthetic_terminal_failure = responses_synthetic_terminal_failure


# Downstream writes that do not participate in the stream-commit seam.
# Non-streaming JSON responses, WebSocket handshakes/frames, and non-streaming
# body-relay writes are intentionally allowlisted: they either carry no SSE
# terminal semantics or are complete payloads whose lifecycle is bounded by the
# calling context. All production SSE headers/bodies must be authorized by the
# request-scoped stream-commit seam.
DOWNSTREAM_STREAM_COMMIT_ALLOWLIST: frozenset[str] = frozenset({
    "_send_json",
    "_safe_send_json",
    "_send_local_responses_no_content",
    "_send_method_not_allowed",
    "_reject_local_responses_websocket_probe",
    "_handle_websocket_recording_probe",
    "_write_non_streaming_body_relay",
    "_write_sse_bytes",
})

# Low-level seam methods that are the only legitimate direct ``self.wfile.write``
# callers inside the stream-commit seam. They are kept separate from the allowlist
# because they participate in (rather than bypass) the seam.
# ``write`` is _HandlerDownstreamIO.write, the handler-bound byte sink.
DOWNSTREAM_STREAM_COMMIT_SEAM_METHODS: frozenset[str] = frozenset({
    "commit_data",
    "commit_sse_bytes",
    "commit_terminal_failure",
    "write",
})


def handler_downstream_stream_commit(handler: Any) -> DownstreamStreamCommit | None:
    """Return the request-scoped stream-commit seam bound to ``handler`` if active."""
    seam = getattr(handler, "_downstream_stream_commit", None)
    return seam if isinstance(seam, DownstreamStreamCommit) else None
_handler_downstream_stream_commit = handler_downstream_stream_commit


def downstream_has_been_exposed(handler: Any) -> bool:
    """Return True if upstream response content has been exposed downstream.

    Exposure includes both bytes already written to the downstream socket and
    upstream content (visible/tool output) that the relay layer has accepted and
    would relay, even if it is still buffered.  Headers alone and metadata-only
    events such as ``response.created`` do not count as exposure.
    """
    seam = _handler_downstream_stream_commit(handler)
    if seam is None:
        return False
    return bool(
        getattr(seam, "_downstream_output_started", False)
        or getattr(seam, "_downstream_content_exposed", False)
    )
_downstream_has_been_exposed = downstream_has_been_exposed


class _HandlerDownstreamIO:
    """Adapter from CodexProxyHandler to DownstreamStreamCommit write/flush/close."""

    def __init__(self, handler: Any) -> None:
        self._handler = handler

    def write(self, data: bytes) -> None:
        self._handler.wfile.write(data)

    def flush(self) -> None:
        self._handler.wfile.flush()

    def close(self) -> None:
        self._handler.close_connection = True


def _bind_handler_synthetic_terminal_failure(
    handler: Any,
    callback: Callable[..., tuple[bool, str | None, str | None]] | None,
    *,
    redact_identity: str | None = None,
) -> Callable[..., tuple[bool, str | None, str | None]] | None:
    if callback is None:
        return None

    def bound(
        exc: BaseException,
        *,
        status: int,
        response_id: str | None,
        upstream_name: str,
        model: str | None,
    ) -> tuple[bool, str | None, str | None]:
        return callback(
            handler,
            exc,
            status=status,
            response_id=response_id,
            upstream_name=upstream_name,
            model=model,
            redact_identity=redact_identity,
        )

    return bound


def bind_downstream_stream_commit(
    handler: Any,
    upstream: Any | None,
    upstream_name: str,
    **kwargs: Any,
) -> DownstreamStreamCommit:
    redact_identity = kwargs.pop("redact_identity", None)
    kwargs.setdefault("usage_line_callback", gateway_events.offer_official_passthrough_usage_line)
    kwargs.setdefault("diagnostic_observer", _observe_gateway_diagnostic)
    kwargs.setdefault("terminal_observer", gateway_stream_semantics._responses_terminal_observer)
    kwargs.setdefault(
        "output_observer",
        lambda event: gateway_stream_semantics._responses_event_commits_downstream_output(event, ""),
    )
    kwargs.setdefault(
        "error_detail_callback",
        lambda exc: safe_upstream_error_detail(exc, redact_identity=redact_identity),
    )
    kwargs.setdefault(
        "terminal_drain_timeout_seconds",
        gateway_transport.OFFICIAL_TERMINAL_DRAIN_TIMEOUT_SECONDS,
    )
    if "synthetic_terminal_failure_callback" in kwargs:
        kwargs["synthetic_terminal_failure_callback"] = (
            _bind_handler_synthetic_terminal_failure(
                handler,
                kwargs["synthetic_terminal_failure_callback"],
                redact_identity=redact_identity,
            )
        )
    return DownstreamStreamCommit(
        _HandlerDownstreamIO(handler),
        upstream,
        upstream_name,
        **kwargs,
    )
_bind_downstream_stream_commit = bind_downstream_stream_commit


# Relay bindings are rebuilt per request so owning-module patches stay live.
def _relay_write_proxy_event(event: str, **fields: Any) -> None:
    gateway_events.write_proxy_event(event, **fields)


def _relay_compatible_sse_line(*args: Any, **kwargs: Any) -> Any:
    return gateway_compat.compatible_sse_line(*args, **kwargs)


def _relay_active_gateway_request() -> Any:
    return gateway_admission.active_gateway_request()


def _relay_glue() -> RelayGlue:
    return RelayGlue(
        _bind_downstream_stream_commit=_bind_downstream_stream_commit,
        _handler_downstream_stream_commit=_handler_downstream_stream_commit,
        _observe_gateway_diagnostic=_observe_gateway_diagnostic,
        _write_adapter_event=_write_adapter_event,
        _bind_handler_synthetic_terminal_failure=_bind_handler_synthetic_terminal_failure,
        _responses_synthetic_terminal_failure=_responses_synthetic_terminal_failure,
        compatible_sse_line=_relay_compatible_sse_line,
        write_proxy_event=_relay_write_proxy_event,
        _active_gateway_request=_relay_active_gateway_request,
        logger=logger,
    )


def relay_context_for_handler(handler: Any) -> RelayContext:
    return RelayContext(
        handler=handler,
        glue=_relay_glue(),
        transparent_relay=handler._relay_transparent_upstream_response,
        official_passthrough_relay=handler._relay_official_passthrough_sse_response,
        prepared_exchange=getattr(handler, "_active_prepared_exchange", None),
    )
_relay_context_for_handler = relay_context_for_handler
