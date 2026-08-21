"""Gateway upstream response relay primitives."""

from __future__ import annotations

import json
import queue
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from gateway_interfaces import RequestAdmission, RelayWriter, UpstreamResponseLike
from route_primitives import MutationPolicy, StreamingPolicy, UsagePolicy


RelayResponse = UpstreamResponseLike


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


@dataclass(frozen=True)
class RelaySymbols:
    """Explicit facade callbacks and helper types used by the relay state machine."""

    CompactEmptyResponseError: type[BaseException]
    DownstreamKeepaliveFailedError: type[BaseException]
    IncompleteRead: type[BaseException]
    NonForwardable: type[BaseException]
    RuntimeToolCompatibilityError: type[BaseException]
    SseFrameTooLargeError: type[BaseException]
    URLError: type[BaseException]
    UpstreamEmptyCompletedResponseError: type[BaseException]
    UpstreamProtocolTranslationError: type[BaseException]
    UpstreamSseSemanticError: type[BaseException]
    UpstreamStreamErrorEvent: type[BaseException]
    UpstreamStreamIdleTimeoutError: type[BaseException]
    UpstreamStreamIncompleteError: type[BaseException]
    UpstreamStreamInterruptedError: type[BaseException]
    DownstreamStreamCommit: type[Any]
    MutationPolicy: type[Any]
    PreparedExchange: type[Any]
    StreamingPolicy: type[Any]
    UsagePolicy: type[Any]
    _ChatToResponsesStreamConverter: type[Any]
    _ResponsesToChatStreamConverter: type[Any]
    _ThirdPartyApplyPatchStreamAdapter: type[Any]
    _RuntimeToolInverseStreamError: type[BaseException]
    RETRY_FAILURE_QUICK_TRANSIENT: str
    RETRY_REQUEST_COMPACT: str
    RETRY_SAFETY_SUPPRESSED_POST_EXPOSURE: str
    RETRY_SAFETY_SUPPRESSED_POST_WRITE: str
    _adapt_third_party_apply_patch_stream_events: Callable[..., Any]
    _apply_external_worker_response_contract: Callable[..., Any]
    _apply_patch_adapter_enabled: Callable[..., Any]
    _bind_downstream_stream_commit: Callable[..., Any]
    _bounded_failure_event_context: Callable[..., Any]
    _capture_usage: Callable[..., Any]
    _chat_completion_body_is_empty: Callable[..., Any]
    _chat_completion_body_to_stream_chunks: Callable[..., Any]
    _chat_completion_to_response_body: Callable[..., Any]
    _chat_sse_event_resets_idle_timeout: Callable[..., Any]
    _chat_stream_chunks_have_terminal: Callable[..., Any]
    _chat_stream_chunks_to_response_events: Callable[..., Any]
    _chat_stream_error_detail: Callable[..., Any]
    _chat_stream_lifecycle_final_issue: Callable[..., Any]
    _chat_stream_shape_summary: Callable[..., Any]
    _chat_terminal_observer: Callable[..., Any]
    _coerce_exact_spawn_prompt_tool_calls: Callable[..., Any]
    _coerce_required_subagent_tool_calls: Callable[..., Any]
    _compact_response_body_is_empty: Callable[..., Any]
    _converted_sse_payload: Callable[..., Any]
    _count_sse_reasoning_event: Callable[..., Any]
    _downgrade_invalid_third_party_tool_calls: Callable[..., Any]
    _events_to_responses_body: Callable[..., Any]
    _filtered_response_headers: Callable[..., Any]
    _guard_duplicate_multi_agent_spawn_calls: Callable[..., Any]
    _handler_downstream_stream_commit: Callable[..., Any]
    _incomplete_stream_json_error_body: Callable[..., Any]
    _is_event_stream: Callable[..., Any]
    _is_reasoning_summary_stream_event: Callable[..., Any]
    _is_sse_blank_line: Callable[..., Any]
    _is_sse_event_metadata_line: Callable[..., Any]
    _json_error_payload_for_inbound_format: Callable[..., Any]
    _lifecycle_final_issue_event_name: Callable[..., Any]
    _lifecycle_final_issue_missing_reason: Callable[..., Any]
    _normalize_third_party_tool_call: Callable[..., Any]
    _observe_gateway_diagnostic: Callable[..., Any]
    _offer_usage_observed_body: Callable[..., Any]
    _offer_usage_observed_sse_line: Callable[..., Any]
    _parse_sse_json_payload: Callable[..., Any]
    _parse_sse_json_payloads: Callable[..., Any]
    _public_event_context: Callable[..., Any]
    _raise_lifecycle_final_issue: Callable[..., Any]
    _raise_runtime_tool_compatibility_error: Callable[..., Any]
    _reconcile_function_call_argument_events: Callable[..., Any]
    _redact_identity_in_text: Callable[..., Any]
    _repair_missing_required_subagent_call_events: Callable[..., Any]
    _response_body_lifecycle_final_issue: Callable[..., Any]
    _response_body_to_chat_completion_body: Callable[..., Any]
    _response_body_to_response_sse_events: Callable[..., Any]
    _response_events_shape_summary: Callable[..., Any]
    _responses_body_is_empty: Callable[..., Any]
    _responses_completed_tool_item: Callable[..., Any]
    _responses_event_commits_downstream_output: Callable[..., Any]
    _responses_event_has_visible_or_tool_output: Callable[..., Any]
    _responses_event_is_tool_call_construction: Callable[..., Any]
    _responses_event_starts_downstream_output: Callable[..., Any]
    _responses_events_have_terminal: Callable[..., Any]
    _responses_events_lifecycle_final_issue: Callable[..., Any]
    _responses_failed_event_for_stream_error: Callable[..., Any]
    _responses_sse_event_resets_idle_timeout: Callable[..., Any]
    _responses_sse_line_resets_idle_timeout: Callable[..., Any]
    _responses_stream_error_detail: Callable[..., Any]
    _responses_stream_error_type: Callable[..., Any]
    _responses_terminal_observer: Callable[..., Any]
    _retry_identity_from_context: Callable[..., Any]
    _route_failure_event_fields: Callable[..., Any]
    _runtime_tool_compatibility_stream_for_attempt: Callable[..., Any]
    _sse_event_separator_after_line: Callable[..., Any]
    _sse_json_line: Callable[..., Any]
    _sse_line_ending: Callable[..., Any]
    _suppress_bounded_tool_search_calls: Callable[..., Any]
    _suppress_chat_reasoning_extensions: Callable[..., Any]
    _suppress_coordinator_forbidden_tool_calls: Callable[..., Any]
    _suppress_worker_multi_agent_tool_calls: Callable[..., Any]
    _synthetic_response_completed_from_tool_items: Callable[..., Any]
    _upstream_failure_class: Callable[..., Any]
    _usage_from_json_body: Callable[..., Any]
    _usage_from_payload: Callable[..., Any]
    _usage_from_response_event: Callable[..., Any]
    _usage_observed_context: Callable[..., Any]
    _verified_converted_sse_semantic_error: Callable[..., Any]
    _with_codexhub_http_error: Callable[..., Any]
    _write_adapter_event: Callable[..., Any]
    _write_runtime_tool_adapter_response_evidence: Callable[..., Any]
    compatible_response_body: Callable[..., Any]
    compatible_sse_line: Callable[..., Any]
    safe_upstream_error_detail: Callable[..., Any]
    write_proxy_event: Callable[..., Any]
    _active_gateway_request: Callable[..., Any]
    _bind_handler_synthetic_terminal_failure: Callable[..., Any]
    _responses_synthetic_terminal_failure: Callable[..., Any]
    _offer_official_passthrough_usage_line: Callable[..., Any]
    _UpstreamSseReaderLifecycle: type[Any]
    logger: Any
    _UNSET_CONTENT_ENCODING: Any
    _chat_completion_error_payload: Callable[..., Any]
    _downstream_stream_error_payload: Callable[..., Any]
    _get_header: Callable[..., Any]
    decoded_request_body: Callable[..., Any]
    _sse_payload_bytes: Callable[..., Any]


class RelayHandler(Protocol):
    close_connection: bool
    _active_prepared_exchange: Any
    _downstream_stream_commit: Any

    def _relay_transparent_upstream_response(self, *args: Any, **kwargs: Any) -> int: ...
    def _relay_official_passthrough_sse_response(self, *args: Any, **kwargs: Any) -> int: ...
    def _iter_upstream_sse_events(self, *args: Any, **kwargs: Any) -> Any: ...
    def _send_sse_headers(self, *args: Any, **kwargs: Any) -> bool: ...
    def _write_sse_bytes(self, *args: Any, **kwargs: Any) -> bool: ...
    def _write_sse_data(self, *args: Any, **kwargs: Any) -> bool: ...
    def _write_sse_done(self, *args: Any, **kwargs: Any) -> bool: ...
    def _write_sse_event(self, *args: Any, **kwargs: Any) -> bool: ...
    def _write_downstream_sse_error(self, *args: Any, **kwargs: Any) -> bool: ...
    def _write_non_streaming_body_relay(self, body: bytes) -> bool: ...
    def _send_json(self, status: int, payload: dict[str, Any]) -> None: ...
    def send_response(self, code: int, message: str | None = None) -> None: ...
    def send_header(self, key: str, value: str) -> None: ...
    def end_headers(self) -> None: ...


class RelayPlan(Protocol):
    selected_upstream_format: str
    request_kind: str
    streaming_policy: StreamingPolicy
    usage_policy: UsagePolicy
    response_mutation_policy: MutationPolicy
    sse_mutation_policy: MutationPolicy
    verify_cross_protocol_source: bool
    lifecycle_final_retry_enabled: bool


@dataclass(frozen=True)
class RelayContext:
    handler: RelayHandler
    symbols: RelaySymbols
    transparent_relay: Callable[..., int]
    official_passthrough_relay: Callable[..., int]
    prepared_exchange: Any = None


def relay_upstream_response(
    relay_context: RelayContext,
    response: UpstreamResponseLike,
    upstream_name: str,
    relay_execution_plan: RelayPlan,
    request_id: str | None = None,
    model: str | None = None,
    inbound_format: str = "responses",
    caller_stream: bool = True,
    event_context: Mapping[str, Any] | None = None,
    usage_capture: dict[str, Any] | None = None,
    headers_already_sent: bool = False,
    defer_stream_errors: bool = False,
    mark_downstream_sse_started: Callable[[], None] | None = None,
    response_lifecycle_state: dict[str, str] | None = None,
) -> int:
    self = relay_context.handler
    CompactEmptyResponseError = relay_context.symbols.CompactEmptyResponseError
    DownstreamKeepaliveFailedError = relay_context.symbols.DownstreamKeepaliveFailedError
    IncompleteRead = relay_context.symbols.IncompleteRead
    NonForwardable = relay_context.symbols.NonForwardable
    RuntimeToolCompatibilityError = relay_context.symbols.RuntimeToolCompatibilityError
    SseFrameTooLargeError = relay_context.symbols.SseFrameTooLargeError
    URLError = relay_context.symbols.URLError
    UpstreamEmptyCompletedResponseError = relay_context.symbols.UpstreamEmptyCompletedResponseError
    UpstreamProtocolTranslationError = relay_context.symbols.UpstreamProtocolTranslationError
    UpstreamSseSemanticError = relay_context.symbols.UpstreamSseSemanticError
    UpstreamStreamErrorEvent = relay_context.symbols.UpstreamStreamErrorEvent
    UpstreamStreamIdleTimeoutError = relay_context.symbols.UpstreamStreamIdleTimeoutError
    UpstreamStreamIncompleteError = relay_context.symbols.UpstreamStreamIncompleteError
    UpstreamStreamInterruptedError = relay_context.symbols.UpstreamStreamInterruptedError
    DownstreamStreamCommit = relay_context.symbols.DownstreamStreamCommit
    MutationPolicy = relay_context.symbols.MutationPolicy
    PreparedExchange = relay_context.symbols.PreparedExchange
    StreamingPolicy = relay_context.symbols.StreamingPolicy
    UsagePolicy = relay_context.symbols.UsagePolicy
    _ChatToResponsesStreamConverter = relay_context.symbols._ChatToResponsesStreamConverter
    _ResponsesToChatStreamConverter = relay_context.symbols._ResponsesToChatStreamConverter
    _ThirdPartyApplyPatchStreamAdapter = relay_context.symbols._ThirdPartyApplyPatchStreamAdapter
    _RuntimeToolInverseStreamError = relay_context.symbols._RuntimeToolInverseStreamError
    RETRY_FAILURE_QUICK_TRANSIENT = relay_context.symbols.RETRY_FAILURE_QUICK_TRANSIENT
    RETRY_REQUEST_COMPACT = relay_context.symbols.RETRY_REQUEST_COMPACT
    RETRY_SAFETY_SUPPRESSED_POST_EXPOSURE = relay_context.symbols.RETRY_SAFETY_SUPPRESSED_POST_EXPOSURE
    RETRY_SAFETY_SUPPRESSED_POST_WRITE = relay_context.symbols.RETRY_SAFETY_SUPPRESSED_POST_WRITE
    _adapt_third_party_apply_patch_stream_events = relay_context.symbols._adapt_third_party_apply_patch_stream_events
    _apply_external_worker_response_contract = relay_context.symbols._apply_external_worker_response_contract
    _apply_patch_adapter_enabled = relay_context.symbols._apply_patch_adapter_enabled
    _bind_downstream_stream_commit = relay_context.symbols._bind_downstream_stream_commit
    _bounded_failure_event_context = relay_context.symbols._bounded_failure_event_context
    _capture_usage = relay_context.symbols._capture_usage
    _chat_completion_body_is_empty = relay_context.symbols._chat_completion_body_is_empty
    _chat_completion_body_to_stream_chunks = relay_context.symbols._chat_completion_body_to_stream_chunks
    _chat_completion_to_response_body = relay_context.symbols._chat_completion_to_response_body
    _chat_sse_event_resets_idle_timeout = relay_context.symbols._chat_sse_event_resets_idle_timeout
    _chat_stream_chunks_have_terminal = relay_context.symbols._chat_stream_chunks_have_terminal
    _chat_stream_chunks_to_response_events = relay_context.symbols._chat_stream_chunks_to_response_events
    _chat_stream_error_detail = relay_context.symbols._chat_stream_error_detail
    _chat_stream_lifecycle_final_issue = relay_context.symbols._chat_stream_lifecycle_final_issue
    _chat_stream_shape_summary = relay_context.symbols._chat_stream_shape_summary
    _chat_terminal_observer = relay_context.symbols._chat_terminal_observer
    _coerce_exact_spawn_prompt_tool_calls = relay_context.symbols._coerce_exact_spawn_prompt_tool_calls
    _coerce_required_subagent_tool_calls = relay_context.symbols._coerce_required_subagent_tool_calls
    _compact_response_body_is_empty = relay_context.symbols._compact_response_body_is_empty
    _converted_sse_payload = relay_context.symbols._converted_sse_payload
    _count_sse_reasoning_event = relay_context.symbols._count_sse_reasoning_event
    _downgrade_invalid_third_party_tool_calls = relay_context.symbols._downgrade_invalid_third_party_tool_calls
    _events_to_responses_body = relay_context.symbols._events_to_responses_body
    _filtered_response_headers = relay_context.symbols._filtered_response_headers
    _guard_duplicate_multi_agent_spawn_calls = relay_context.symbols._guard_duplicate_multi_agent_spawn_calls
    _handler_downstream_stream_commit = relay_context.symbols._handler_downstream_stream_commit
    _incomplete_stream_json_error_body = relay_context.symbols._incomplete_stream_json_error_body
    _is_event_stream = relay_context.symbols._is_event_stream
    _is_reasoning_summary_stream_event = relay_context.symbols._is_reasoning_summary_stream_event
    _is_sse_blank_line = relay_context.symbols._is_sse_blank_line
    _is_sse_event_metadata_line = relay_context.symbols._is_sse_event_metadata_line
    _json_error_payload_for_inbound_format = relay_context.symbols._json_error_payload_for_inbound_format
    _lifecycle_final_issue_event_name = relay_context.symbols._lifecycle_final_issue_event_name
    _lifecycle_final_issue_missing_reason = relay_context.symbols._lifecycle_final_issue_missing_reason
    _normalize_third_party_tool_call = relay_context.symbols._normalize_third_party_tool_call
    _observe_gateway_diagnostic = relay_context.symbols._observe_gateway_diagnostic
    _offer_usage_observed_body = relay_context.symbols._offer_usage_observed_body
    _offer_usage_observed_sse_line = relay_context.symbols._offer_usage_observed_sse_line
    _parse_sse_json_payload = relay_context.symbols._parse_sse_json_payload
    _parse_sse_json_payloads = relay_context.symbols._parse_sse_json_payloads
    _public_event_context = relay_context.symbols._public_event_context
    _raise_lifecycle_final_issue = relay_context.symbols._raise_lifecycle_final_issue
    _raise_runtime_tool_compatibility_error = relay_context.symbols._raise_runtime_tool_compatibility_error
    _reconcile_function_call_argument_events = relay_context.symbols._reconcile_function_call_argument_events
    _redact_identity_in_text = relay_context.symbols._redact_identity_in_text
    _repair_missing_required_subagent_call_events = relay_context.symbols._repair_missing_required_subagent_call_events
    _response_body_lifecycle_final_issue = relay_context.symbols._response_body_lifecycle_final_issue
    _response_body_to_chat_completion_body = relay_context.symbols._response_body_to_chat_completion_body
    _response_body_to_response_sse_events = relay_context.symbols._response_body_to_response_sse_events
    _response_events_shape_summary = relay_context.symbols._response_events_shape_summary
    _responses_body_is_empty = relay_context.symbols._responses_body_is_empty
    _responses_completed_tool_item = relay_context.symbols._responses_completed_tool_item
    _responses_event_commits_downstream_output = relay_context.symbols._responses_event_commits_downstream_output
    _responses_event_has_visible_or_tool_output = relay_context.symbols._responses_event_has_visible_or_tool_output
    _responses_event_is_tool_call_construction = relay_context.symbols._responses_event_is_tool_call_construction
    _responses_event_starts_downstream_output = relay_context.symbols._responses_event_starts_downstream_output
    _responses_events_have_terminal = relay_context.symbols._responses_events_have_terminal
    _responses_events_lifecycle_final_issue = relay_context.symbols._responses_events_lifecycle_final_issue
    _responses_failed_event_for_stream_error = relay_context.symbols._responses_failed_event_for_stream_error
    _responses_sse_event_resets_idle_timeout = relay_context.symbols._responses_sse_event_resets_idle_timeout
    _responses_sse_line_resets_idle_timeout = relay_context.symbols._responses_sse_line_resets_idle_timeout
    _responses_stream_error_detail = relay_context.symbols._responses_stream_error_detail
    _responses_stream_error_type = relay_context.symbols._responses_stream_error_type
    _responses_terminal_observer = relay_context.symbols._responses_terminal_observer
    _retry_identity_from_context = relay_context.symbols._retry_identity_from_context
    _route_failure_event_fields = relay_context.symbols._route_failure_event_fields
    _runtime_tool_compatibility_stream_for_attempt = relay_context.symbols._runtime_tool_compatibility_stream_for_attempt
    _sse_event_separator_after_line = relay_context.symbols._sse_event_separator_after_line
    _sse_json_line = relay_context.symbols._sse_json_line
    _sse_line_ending = relay_context.symbols._sse_line_ending
    _suppress_bounded_tool_search_calls = relay_context.symbols._suppress_bounded_tool_search_calls
    _suppress_chat_reasoning_extensions = relay_context.symbols._suppress_chat_reasoning_extensions
    _suppress_coordinator_forbidden_tool_calls = relay_context.symbols._suppress_coordinator_forbidden_tool_calls
    _suppress_worker_multi_agent_tool_calls = relay_context.symbols._suppress_worker_multi_agent_tool_calls
    _synthetic_response_completed_from_tool_items = relay_context.symbols._synthetic_response_completed_from_tool_items
    _upstream_failure_class = relay_context.symbols._upstream_failure_class
    _usage_from_json_body = relay_context.symbols._usage_from_json_body
    _usage_from_payload = relay_context.symbols._usage_from_payload
    _usage_from_response_event = relay_context.symbols._usage_from_response_event
    _usage_observed_context = relay_context.symbols._usage_observed_context
    _verified_converted_sse_semantic_error = relay_context.symbols._verified_converted_sse_semantic_error
    _with_codexhub_http_error = relay_context.symbols._with_codexhub_http_error
    _write_adapter_event = relay_context.symbols._write_adapter_event
    _write_runtime_tool_adapter_response_evidence = relay_context.symbols._write_runtime_tool_adapter_response_evidence
    compatible_response_body = relay_context.symbols.compatible_response_body
    compatible_sse_line = relay_context.symbols.compatible_sse_line
    safe_upstream_error_detail = relay_context.symbols.safe_upstream_error_detail
    upstream_format = relay_execution_plan.selected_upstream_format
    request_kind = relay_execution_plan.request_kind
    streaming_policy = relay_execution_plan.streaming_policy
    usage_policy = relay_execution_plan.usage_policy
    response_mutation_policy = (
        relay_execution_plan.response_mutation_policy
    )
    sse_mutation_policy = relay_execution_plan.sse_mutation_policy
    verify_cross_protocol_source = (
        relay_execution_plan.verify_cross_protocol_source
    )
    lifecycle_final_retry_enabled = (
        relay_execution_plan.lifecycle_final_retry_enabled
    )
    status = getattr(response, "status", None) or getattr(response, "code", 502)
    is_event_stream = _is_event_stream(response.headers)
    # When the caller spoke Chat Completions, the response must be converted
    # back to Chat Completions format regardless of the upstream wire format.
    want_chat_output = inbound_format == "chat_completions"
    request_scoped_seam = _handler_downstream_stream_commit(self)
    seam: DownstreamStreamCommit | None = request_scoped_seam
    if request_scoped_seam is not None:
        request_scoped_seam.set_terminal_observer(
            _chat_terminal_observer if want_chat_output else _responses_terminal_observer
        )
        request_scoped_seam.set_output_observer(
            None
            if want_chat_output
            else (lambda event: _responses_event_commits_downstream_output(event, ""))
        )
        request_scoped_seam.set_usage_line_callback(
            lambda context, line: _offer_usage_observed_sse_line(
                context, line, upstream_format=upstream_format
            )
        )
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
                _chat_terminal_observer if want_chat_output else _responses_terminal_observer
            ),
            output_observer=(
                None
                if want_chat_output
                else (lambda event: _responses_event_commits_downstream_output(event, ""))
            ),
            usage_line_callback=lambda context, line: _offer_usage_observed_sse_line(
                context, line, upstream_format=upstream_format
            ),
        )
        self._downstream_stream_commit = seam
    compatibility_event_context = dict(event_context or {})
    compatibility_event_context["_apply_patch_adapter_enabled"] = not want_chat_output
    # When the caller asked for a non-streaming response but the upstream
    # returns SSE (e.g. chatgpt.com forces stream=true), buffer the entire
    # SSE into a single JSON response body.
    buffer_sse_to_json = is_event_stream and not caller_stream
    buffered_json_response = False
    buffered_chat_sse_to_responses = False
    verified_source_format = (
        upstream_format
        if (
            verify_cross_protocol_source
            and upstream_format != inbound_format
        )
        else None
    )
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

    def observe_diagnostic_sse_line(line: bytes) -> None:
        _observe_gateway_diagnostic("observe_sse_line", request_id, len(line))

    def remember_response_id(payload: Mapping[str, Any]) -> None:
        if response_lifecycle_state is None:
            return
        response_payload = payload.get("response")
        if not isinstance(response_payload, Mapping):
            return
        response_id = response_payload.get("id")
        if isinstance(response_id, str) and response_id:
            response_lifecycle_state["response_id"] = response_id

    if (
        streaming_policy == StreamingPolicy.TRANSPARENT
        and upstream_format == inbound_format
        and not (is_event_stream and not caller_stream and upstream_format == "responses")
    ):
        return relay_context.transparent_relay(
            response,
            upstream_name,
            request_id=request_id,
            model=model,
            upstream_format=upstream_format,
            inbound_format=inbound_format,
            usage_capture=usage_capture,
            headers_already_sent=headers_already_sent,
            mark_downstream_sse_started=mark_downstream_sse_started,
            event_context=event_context,
            defer_stream_errors=defer_stream_errors,
        )
    if (
        streaming_policy == StreamingPolicy.OFFICIAL_PASSTHROUGH
        and is_event_stream
        and inbound_format == "responses"
        and upstream_format == "responses"
        and not want_chat_output
    ):
        return relay_context.official_passthrough_relay(
            response,
            upstream_name,
            request_id=request_id,
            model=model,
            upstream_format=upstream_format,
            inbound_format=inbound_format,
            usage_capture=usage_capture,
            headers_already_sent=headers_already_sent,
            mark_downstream_sse_started=mark_downstream_sse_started,
            event_context=event_context,
            defer_stream_errors=defer_stream_errors,
        )
    defer_stream_headers = (
        is_event_stream
        and caller_stream
        and lifecycle_final_retry_enabled
    )

    def finish_downstream_stream_closed(exc: OSError) -> int:
        self.close_connection = True
        event_fields = _bounded_failure_event_context(event_context)
        for key in ("request_id", "model", "upstream", "status", "error", "detail"):
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
        _capture_usage(
            usage_capture,
            None,
            missing_reason="async_usage_pending"
            if usage_policy == UsagePolicy.ASYNC_TAP
            else "client_disconnected",
        )
        return 499

    def finish_converted_sse_semantic_error(
        exc: UpstreamSseSemanticError | SseFrameTooLargeError,
        *,
        response_id: str | None = None,
        standard_responses_failure: bool = False,
    ) -> int:
        if seam.terminal_committed:
            self.close_connection = True
            _capture_usage(
                usage_capture,
                None,
                missing_reason="async_usage_pending",
            )
            return status
        error_code = getattr(exc, "classification", "upstream_protocol_error")
        self.close_connection = True
        write_proxy_event(
            "upstream_stream_protocol_error",
            request_id=request_id,
            model=model,
            upstream=upstream_name,
            status=502,
            upstream_format=upstream_format,
            inbound_format=inbound_format,
            error=type(exc).__name__,
            detail=str(exc),
        )
        if not send_downstream_response_headers_once():
            return finish_downstream_stream_closed(
                seam.last_write_error() or OSError("downstream closed")
            )
        if inbound_format == "responses" and standard_responses_failure:
            failed_event = _responses_failed_event_for_stream_error(
                upstream_name=upstream_name,
                model=model,
                status=502,
                error=error_code,
                detail=str(exc),
                response_id=response_id,
                redact_identity=relay_redact_identity,
            )
            wrote_error = self._write_sse_bytes(
                _sse_json_line(failed_event, b"\n") + b"\n"
            )
        else:
            wrote_error = self._write_downstream_sse_error(
                inbound_format=inbound_format,
                upstream_name=upstream_name,
                status=502,
                error=error_code,
                detail=str(exc),
                redact_identity=relay_redact_identity,
            )
        if not wrote_error:
            return finish_downstream_stream_closed(
                seam.last_write_error() or OSError("downstream closed")
            )
        _capture_usage(usage_capture, None, missing_reason="stream_protocol_error")
        return 502

    def buffered_protocol_error_body(
        exc: UpstreamSseSemanticError | SseFrameTooLargeError,
    ) -> bytes:
        write_proxy_event(
            "upstream_stream_protocol_error",
            request_id=request_id,
            model=model,
            upstream=upstream_name,
            status=502,
            upstream_format=upstream_format,
            inbound_format=inbound_format,
            error=type(exc).__name__,
            detail=str(exc),
        )
        return json.dumps(
            _json_error_payload_for_inbound_format(
                inbound_format=inbound_format,
                upstream_name=upstream_name,
                status=502,
                error="upstream_protocol_error",
                detail=str(exc),
                error_type="upstream_protocol_error",
                redact_identity=relay_redact_identity,
            ),
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")

    headers_sent = headers_already_sent
    if not is_event_stream or buffer_sse_to_json:
        converted_stream_failure = False
        if buffer_sse_to_json:
            # Buffer the full SSE stream into a list of events.
            events: list[Mapping[str, Any]] = []
            chat_chunks: list[Mapping[str, Any] | str] = []
            incomplete_frame = False

            try:
                for frame in self._iter_upstream_sse_events(
                    response,
                    event_resets_idle_timeout=(
                        _chat_sse_event_resets_idle_timeout
                        if upstream_format == "chat_completions"
                        else _responses_sse_event_resets_idle_timeout
                    ),
                    on_chunk=observe_diagnostic_sse_line,
                ):
                    payload = _converted_sse_payload(
                        frame,
                        verified_source_format=verified_source_format,
                    )
                    if payload is None:
                        continue
                    if upstream_format == "chat_completions":
                        chat_chunks.append(payload)
                    elif payload != "[DONE]":
                        events.append(payload)
            except (UpstreamSseSemanticError, SseFrameTooLargeError) as exc:
                status = 502
                converted_stream_failure = True
                body = buffered_protocol_error_body(exc)
            except UpstreamStreamIncompleteError:
                incomplete_frame = True
            except (IncompleteRead, TimeoutError, OSError, URLError) as exc:
                if defer_stream_errors:
                    raise UpstreamStreamInterruptedError(exc) from exc
                raise
            # Reconstruct a Responses-format body from the events.
            if not converted_stream_failure:
                try:
                    if incomplete_frame:
                        raise UpstreamStreamIncompleteError(
                            "Upstream SSE stream ended with an incomplete pending frame"
                        )
                    if (
                        upstream_format == "chat_completions"
                        and not want_chat_output
                    ):
                        response_events = _chat_stream_chunks_to_response_events(
                            chat_chunks
                        )
                        body = _events_to_responses_body(
                            response_events,
                            require_completed=True,
                        )
                        buffered_chat_sse_to_responses = True
                    else:
                        body = _events_to_responses_body(
                            events,
                            require_completed=True,
                        )
                except UpstreamStreamIncompleteError:
                    if defer_stream_errors:
                        raise
                    status = 502
                    converted_stream_failure = True
                    body = _incomplete_stream_json_error_body(upstream_name)
                    write_proxy_event(
                        "upstream_stream_incomplete",
                        request_id=request_id,
                        model=model,
                        upstream=upstream_name,
                        status=status,
                        upstream_format=upstream_format,
                        inbound_format=inbound_format,
                    )
                except UpstreamProtocolTranslationError:
                    if verified_source_format is None:
                        raise
                    status = 502
                    converted_stream_failure = True
                    body = buffered_protocol_error_body(
                        _verified_converted_sse_semantic_error(
                            verified_source_format
                        )
                    )
            is_event_stream = False
            buffered_json_response = True
        else:
            body = b""
            try:
                while True:
                    chunk = response.read(65536)
                    if not chunk:
                        break
                    body += chunk
            except (IncompleteRead, TimeoutError, OSError, URLError) as exc:
                if defer_stream_errors:
                    raise UpstreamStreamInterruptedError(exc) from exc
                raise
        upstream_body_for_usage = body
        try:
            if converted_stream_failure:
                pass
            elif want_chat_output:
                if upstream_format == "chat_completions":
                    body = _response_body_to_chat_completion_body(
                        compatible_response_body(
                            _chat_completion_to_response_body(body),
                            upstream_name,
                            event_context=compatibility_event_context,
                        )
                    )
                else:
                    exchange = relay_context.prepared_exchange
                    if not isinstance(exchange, PreparedExchange):
                        exchange = PreparedExchange(
                            inbound_format,
                            upstream_format,
                            b"",
                            False,
                        )
                    def decode_to_caller(payload: bytes) -> bytes:
                        try:
                            return exchange.decode_response(payload)
                        except NonForwardable as exc:
                            raise UpstreamProtocolTranslationError(exc) from exc
                    if response_mutation_policy == MutationPolicy.TRANSPARENT:
                        body = decode_to_caller(body)
                    else:
                        body = decode_to_caller(
                            compatible_response_body(
                                body,
                                upstream_name,
                                event_context=compatibility_event_context,
                            )
                        )
            elif upstream_format == "chat_completions":
                if buffered_chat_sse_to_responses:
                    converted_body = body
                else:
                    converted_body = _chat_completion_to_response_body(
                        body,
                        repair=(
                            response_mutation_policy
                            != MutationPolicy.TRANSPARENT
                        ),
                    )
                if (
                    response_mutation_policy
                    == MutationPolicy.TRANSPARENT
                ):
                    body = converted_body
                else:
                    body = compatible_response_body(
                        converted_body,
                        upstream_name,
                        event_context=compatibility_event_context,
                    )
            else:
                body = compatible_response_body(
                    body,
                    upstream_name,
                    event_context=compatibility_event_context,
                )
        except UpstreamProtocolTranslationError:
            if not buffer_sse_to_json or verified_source_format is None:
                raise
            status = 502
            converted_stream_failure = True
            body = buffered_protocol_error_body(
                _verified_converted_sse_semantic_error(
                    verified_source_format
                )
            )
        if status >= 400:
            body = _with_codexhub_http_error(
                body,
                upstream_name=upstream_name,
                status=status,
                exc=response if isinstance(response, BaseException) else None,
            )
        if usage_policy == UsagePolicy.ASYNC_TAP:
            _capture_usage(usage_capture, None, missing_reason="async_usage_pending")
            _offer_usage_observed_body(usage_context, upstream_body_for_usage)
        else:
            _capture_usage(usage_capture, _usage_from_json_body(body))
            if status < 400:
                lifecycle_issue = _response_body_lifecycle_final_issue(body, event_context, request_kind)
                if lifecycle_issue is not None:
                    _write_adapter_event(
                        event_context,
                        _lifecycle_final_issue_event_name(lifecycle_issue),
                        upstream=upstream_name,
                        inbound_format=inbound_format,
                        want_chat_output=want_chat_output,
                        body_format="chat_completions" if want_chat_output else "responses",
                    )
                    _capture_usage(
                        usage_capture,
                        None,
                        missing_reason=_lifecycle_final_issue_missing_reason(lifecycle_issue),
                    )
                    if not headers_already_sent:
                        _raise_lifecycle_final_issue(upstream_name, lifecycle_issue)
                    status = 502
                    body = json.dumps(
                        _json_error_payload_for_inbound_format(
                            inbound_format=inbound_format,
                            upstream_name=upstream_name,
                            status=status,
                            error=_lifecycle_final_issue_missing_reason(lifecycle_issue),
                            detail=(
                                "Upstream returned an empty final response after completed subagent lifecycle."
                                if lifecycle_issue == "empty"
                                else "Upstream returned a final response with extra text outside the requested report format."
                            ),
                            error_type=_lifecycle_final_issue_missing_reason(lifecycle_issue),
                            redact_identity=relay_redact_identity,
                        ),
                        ensure_ascii=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
        if (
            status < 400
            and request_kind == RETRY_REQUEST_COMPACT
            and _compact_response_body_is_empty(body, inbound_format)
        ):
            if not headers_already_sent:
                _capture_usage(usage_capture, None, missing_reason="compact_empty_response")
                raise CompactEmptyResponseError(upstream_name)
            status = 502
            body = json.dumps(
                _json_error_payload_for_inbound_format(
                    inbound_format=inbound_format,
                    upstream_name=upstream_name,
                    status=status,
                    error="compact_empty_response",
                    detail="Upstream returned an empty compact summary.",
                    error_type="compact_empty_response",
                    redact_identity=relay_redact_identity,
                ),
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("utf-8")
            event_fields = _public_event_context(event_context)
            event_fields.pop("request_id", None)
            event_fields.pop("model", None)
            event_fields.pop("upstream", None)
            event_fields.pop("status", None)
            write_proxy_event(
                "compact_empty_response",
                request_id=request_id,
                model=model,
                upstream=upstream_name,
                status=status,
                upstream_format=upstream_format,
                inbound_format=inbound_format,
                **event_fields,
            )
            _capture_usage(usage_capture, None, missing_reason="compact_empty_response")
        else:
            empty_non_compact = (
                _chat_completion_body_is_empty(body)
                if inbound_format == "chat_completions"
                else _responses_body_is_empty(body)
            )
            if status < 400 and request_kind != RETRY_REQUEST_COMPACT and empty_non_compact:
                event_fields = _public_event_context(event_context)
                event_fields.pop("request_id", None)
                event_fields.pop("model", None)
                event_fields.pop("upstream", None)
                write_proxy_event(
                    "empty_assistant_response",
                    request_id=request_id,
                    model=model,
                    upstream=upstream_name,
                    status=status,
                    upstream_format=upstream_format,
                    inbound_format=inbound_format,
                    **event_fields,
                )
        downstream_expects_sse = caller_stream and (
            headers_sent or mark_downstream_sse_started is not None
        )
        if downstream_expects_sse and not want_chat_output and status < 400:
            try:
                response_events = _response_body_to_response_sse_events(body)
            except (UnicodeDecodeError, json.JSONDecodeError):
                response_events = []
            if response_events:
                if not headers_sent:
                    if not self._send_sse_headers(status, upstream_name):
                        return finish_downstream_stream_closed(
                            seam.last_write_error() or OSError("downstream closed")
                        )
                    headers_sent = True
                    if mark_downstream_sse_started is not None:
                        mark_downstream_sse_started()
                for event in response_events:
                    if (
                        sse_mutation_policy
                        != MutationPolicy.TRANSPARENT
                    ):
                        event, _ = _normalize_third_party_tool_call(event, compatibility_event_context)
                        event, _ = _suppress_bounded_tool_search_calls(
                            event,
                            compatibility_event_context,
                        )
                        if event is None:
                            continue
                        event, _ = _downgrade_invalid_third_party_tool_calls(event)
                        event, _ = _guard_duplicate_multi_agent_spawn_calls(event, compatibility_event_context)
                    event_type = event.get("type")
                    if isinstance(event_type, str) and event_type:
                        if not self._write_sse_event(event_type, event):
                            return finish_downstream_stream_closed(
                                seam.last_write_error() or OSError("downstream closed")
                            )
                sse_seam = _handler_downstream_stream_commit(self)
                if (
                    sse_seam is not None
                    and sse_seam.downstream_closed
                    and sse_seam.last_write_error() is not None
                ):
                    return finish_downstream_stream_closed(
                        sse_seam.last_write_error() or OSError("downstream closed")
                    )
                self.close_connection = True
                _capture_usage(
                    usage_capture,
                    None,
                    missing_reason="async_usage_pending"
                    if usage_policy == UsagePolicy.ASYNC_TAP
                    else "upstream_missing_usage",
                )
                return status
        if headers_sent:
            if not self._write_downstream_sse_error(
                inbound_format=inbound_format,
                upstream_name=upstream_name,
                status=status,
                error="UpstreamProtocolError",
                detail=f"upstream returned non-SSE response after downstream SSE retry status: HTTP {status}",
                redact_identity=relay_redact_identity,
            ):
                return finish_downstream_stream_closed(
                    seam.last_write_error() or OSError("downstream closed")
                )
            self.close_connection = True
            _capture_usage(usage_capture, None, missing_reason="stream_protocol_error")
            return status

    def send_downstream_response_headers_once() -> bool:
        nonlocal headers_sent
        if headers_sent:
            return True
        content_length = None if is_event_stream else len(body)
        content_type = "application/json" if buffered_json_response else None

        def _send() -> None:
            self.send_response(status)
            for key, value in _filtered_response_headers(
                response.headers,
                is_event_stream,
                content_length,
                content_type=content_type,
            ):
                self.send_header(key, value)
            self.send_header("X-Codex-Proxy-Upstream", upstream_name)
            self.send_header("Connection", "close")
            self.end_headers()

        if not seam.commit_headers(status, _send):
            return False
        headers_sent = True
        if mark_downstream_sse_started is not None:
            mark_downstream_sse_started()
        return True

    seam.set_ensure_headers_committed_callback(
        send_downstream_response_headers_once if defer_stream_headers else None
    )
    if not defer_stream_headers:
        if not send_downstream_response_headers_once():
            return finish_downstream_stream_closed(
                seam.last_write_error() or OSError("downstream closed")
            )

    if is_event_stream:
        if (
            streaming_policy == StreamingPolicy.TRANSPARENT_CONVERTED
            and want_chat_output
            and upstream_format != "chat_completions"
        ):
            line_ending = b"\n"
            converter = _ResponsesToChatStreamConverter()
            incomplete_frame = False
            try:
                for frame in self._iter_upstream_sse_events(
                    response,
                    event_resets_idle_timeout=_responses_sse_event_resets_idle_timeout,
                    on_chunk=observe_diagnostic_sse_line,
                ):
                    line_ending = _sse_line_ending(frame.raw)
                    payload = _converted_sse_payload(
                        frame,
                        verified_source_format=verified_source_format,
                    )
                    if payload is None or payload == "[DONE]":
                        continue
                    event = payload
                    _offer_usage_observed_sse_line(
                        usage_context,
                        frame.raw,
                        upstream_format=upstream_format,
                    )
                    error_type = _responses_stream_error_type(event)
                    if error_type is not None:
                        detail = _redact_identity_in_text(
                            _responses_stream_error_detail(event),
                            relay_redact_identity,
                        )
                        write_proxy_event(
                            "upstream_stream_error_event",
                            request_id=request_id,
                            model=model,
                            upstream=upstream_name,
                            status=502,
                            upstream_format=upstream_format,
                            inbound_format=inbound_format,
                            error=error_type,
                            detail=detail,
                        )
                        if not self._write_downstream_sse_error(
                            inbound_format=inbound_format,
                            upstream_name=upstream_name,
                            status=502,
                            error=error_type,
                            detail=detail,
                            redact_identity=relay_redact_identity,
                        ):
                            return finish_downstream_stream_closed(
                                seam.last_write_error() or OSError("downstream closed")
                            )
                        _capture_usage(usage_capture, None, missing_reason="stream_error_event")
                        return 502
                    for chunk in converter.chunks_for_event(event):
                        if not self._write_sse_data(chunk):
                            return finish_downstream_stream_closed(
                                seam.last_write_error() or OSError("downstream closed")
                            )
            except (UpstreamSseSemanticError, SseFrameTooLargeError) as exc:
                return finish_converted_sse_semantic_error(exc)
            except UpstreamProtocolTranslationError:
                return finish_converted_sse_semantic_error(
                    _verified_converted_sse_semantic_error("responses")
                )
            except UpstreamStreamIncompleteError:
                incomplete_frame = True
            except UpstreamStreamIdleTimeoutError as exc:
                self.close_connection = True
                idle_detail = safe_upstream_error_detail(exc, redact_identity=relay_redact_identity)
                write_proxy_event(
                    "upstream_stream_idle_timeout",
                    request_id=request_id,
                    model=model,
                    upstream=upstream_name,
                    status=502,
                    upstream_format=upstream_format,
                    inbound_format=inbound_format,
                    stream_idle_timeout_seconds=exc.timeout_seconds,
                    stream_idle_phase=exc.phase,
                    detail=idle_detail,
                )
                if not send_downstream_response_headers_once():
                    return finish_downstream_stream_closed(
                        seam.last_write_error() or OSError("downstream closed")
                    )
                if not self._write_downstream_sse_error(
                    inbound_format=inbound_format,
                    upstream_name=upstream_name,
                    status=502,
                    error="upstream_stream_idle_timeout",
                    detail=idle_detail,
                    redact_identity=relay_redact_identity,
                ):
                    return finish_downstream_stream_closed(
                        seam.last_write_error() or OSError("downstream closed")
                    )
                _capture_usage(usage_capture, None, missing_reason="stream_idle_timeout")
                return 502
            except DownstreamKeepaliveFailedError:
                return finish_downstream_stream_closed(
                    seam.last_write_error() or OSError("downstream closed")
                )
            except (IncompleteRead, TimeoutError, OSError, URLError) as exc:
                self.close_connection = True
                stream_detail = safe_upstream_error_detail(exc, redact_identity=relay_redact_identity)
                write_proxy_event(
                    "upstream_stream_interrupted",
                    request_id=request_id,
                    model=model,
                    upstream=upstream_name,
                    status=502,
                    error=type(exc).__name__,
                    detail=stream_detail,
                )
                if not send_downstream_response_headers_once():
                    return finish_downstream_stream_closed(
                        seam.last_write_error() or OSError("downstream closed")
                    )
                if not self._write_downstream_sse_error(
                    inbound_format=inbound_format,
                    upstream_name=upstream_name,
                    exc=exc,
                    redact_identity=relay_redact_identity,
                ):
                    return finish_downstream_stream_closed(
                        seam.last_write_error() or OSError("downstream closed")
                    )
                _capture_usage(usage_capture, None, missing_reason="stream_interrupted")
                return 502
            if incomplete_frame or not converter.completed:
                self.close_connection = True
                write_proxy_event(
                    "upstream_stream_incomplete",
                    request_id=request_id,
                    model=model,
                    upstream=upstream_name,
                    status=502,
                    upstream_format=upstream_format,
                    inbound_format=inbound_format,
                )
                if not send_downstream_response_headers_once():
                    return finish_downstream_stream_closed(
                        seam.last_write_error() or OSError("downstream closed")
                    )
                if not self._write_downstream_sse_error(
                    inbound_format=inbound_format,
                    upstream_name=upstream_name,
                    status=502,
                    error="upstream_stream_incomplete",
                    detail="Upstream stream ended before response.completed.",
                    redact_identity=relay_redact_identity,
                ):
                    return finish_downstream_stream_closed(
                        seam.last_write_error() or OSError("downstream closed")
                    )
                _capture_usage(usage_capture, None, missing_reason="stream_incomplete")
                return 502
            if not self._write_sse_done():
                return finish_downstream_stream_closed(
                    seam.last_write_error() or OSError("downstream closed")
                )
            self.close_connection = True
            _capture_usage(usage_capture, None, missing_reason="async_usage_pending")
            return status

        if (
            streaming_policy == StreamingPolicy.TRANSPARENT_CONVERTED
            and upstream_format == "chat_completions"
            and not want_chat_output
        ):
            line_ending = b"\n"
            converter = _ChatToResponsesStreamConverter()
            incomplete_frame = False

            def write_converted_response_event(event: Mapping[str, Any]) -> bool:
                line = _sse_json_line(event, line_ending) + line_ending
                try:
                    compatible_line = compatible_sse_line(
                        line,
                        upstream_name,
                        event_context=compatibility_event_context,
                        runtime_tool_inverse_only=True,
                    )
                except UpstreamProtocolTranslationError as exc:
                    raise _RuntimeToolInverseStreamError(exc) from exc
                if not compatible_line:
                    return True
                return self._write_sse_bytes(compatible_line)

            try:
                for frame in self._iter_upstream_sse_events(
                    response,
                    event_resets_idle_timeout=_chat_sse_event_resets_idle_timeout,
                    on_chunk=observe_diagnostic_sse_line,
                ):
                    payload = _converted_sse_payload(
                        frame,
                        verified_source_format=verified_source_format,
                    )
                    if payload is None:
                        continue
                    events: list[dict[str, Any]] = []
                    if payload == "[DONE]":
                        events = converter.events_for_done()
                    else:
                        chat_error_detail = _redact_identity_in_text(
                            _chat_stream_error_detail(payload) or "",
                            relay_redact_identity,
                        )
                        if chat_error_detail:
                            write_proxy_event(
                                "upstream_stream_error_event",
                                request_id=request_id,
                                model=model,
                                upstream=upstream_name,
                                status=502,
                                upstream_format=upstream_format,
                                inbound_format=inbound_format,
                                error="chat_completions_error",
                                detail=chat_error_detail,
                            )
                            if not self._write_downstream_sse_error(
                                inbound_format=inbound_format,
                                upstream_name=upstream_name,
                                status=502,
                                error="chat_completions_error",
                                detail=chat_error_detail,
                                redact_identity=relay_redact_identity,
                            ):
                                return finish_downstream_stream_closed(
                                    seam.last_write_error() or OSError("downstream closed")
                                )
                            _capture_usage(usage_capture, None, missing_reason="stream_error_event")
                            return 502
                        _offer_usage_observed_sse_line(
                            usage_context,
                            frame.raw,
                            upstream_format=upstream_format,
                        )
                        events = converter.events_for_chunk(payload)
                    for event in events:
                        try:
                            if not write_converted_response_event(event):
                                return finish_downstream_stream_closed(
                                    seam.last_write_error() or OSError("downstream closed")
                                )
                        except OSError as exc:
                            return finish_downstream_stream_closed(exc)
            except (UpstreamSseSemanticError, SseFrameTooLargeError) as exc:
                return finish_converted_sse_semantic_error(
                    exc,
                    response_id=converter.response_id,
                )
            except _RuntimeToolInverseStreamError as exc:
                return finish_converted_sse_semantic_error(
                    UpstreamSseSemanticError(
                        str(exc.translation_error),
                        classification=exc.translation_error.classification,
                    ),
                    response_id=converter.response_id,
                    standard_responses_failure=True,
                )
            except UpstreamProtocolTranslationError:
                return finish_converted_sse_semantic_error(
                    _verified_converted_sse_semantic_error(
                        "chat_completions"
                    ),
                    response_id=converter.response_id,
                )
            except UpstreamStreamIncompleteError:
                incomplete_frame = True
            except UpstreamStreamIdleTimeoutError as exc:
                self.close_connection = True
                idle_detail = safe_upstream_error_detail(exc, redact_identity=relay_redact_identity)
                write_proxy_event(
                    "upstream_stream_idle_timeout",
                    request_id=request_id,
                    model=model,
                    upstream=upstream_name,
                    status=502,
                    upstream_format=upstream_format,
                    inbound_format=inbound_format,
                    stream_idle_timeout_seconds=exc.timeout_seconds,
                    stream_idle_phase=exc.phase,
                    detail=idle_detail,
                )
                if not self._write_downstream_sse_error(
                    inbound_format=inbound_format,
                    upstream_name=upstream_name,
                    status=502,
                    error="upstream_stream_idle_timeout",
                    detail=idle_detail,
                    redact_identity=relay_redact_identity,
                ):
                    return finish_downstream_stream_closed(
                        seam.last_write_error() or OSError("downstream closed")
                    )
                _capture_usage(usage_capture, None, missing_reason="stream_idle_timeout")
                return 502
            except DownstreamKeepaliveFailedError:
                return finish_downstream_stream_closed(
                    seam.last_write_error() or OSError("downstream closed")
                )
            except (IncompleteRead, TimeoutError, OSError, URLError) as exc:
                if defer_stream_errors:
                    raise UpstreamStreamInterruptedError(exc) from exc
                self.close_connection = True
                stream_detail = safe_upstream_error_detail(exc, redact_identity=relay_redact_identity)
                write_proxy_event(
                    "upstream_stream_interrupted",
                    request_id=request_id,
                    model=model,
                    upstream=upstream_name,
                    status=502,
                    error=type(exc).__name__,
                    detail=stream_detail,
                )
                if not self._write_downstream_sse_error(
                    inbound_format=inbound_format,
                    upstream_name=upstream_name,
                    exc=exc,
                    redact_identity=relay_redact_identity,
                ):
                    return finish_downstream_stream_closed(
                        seam.last_write_error() or OSError("downstream closed")
                    )
                _capture_usage(usage_capture, None, missing_reason="stream_interrupted")
                return 502
            if (
                not incomplete_frame
                and not converter.completed
                and converter.pending_incomplete is not None
            ):
                for event in converter.events_for_done():
                    try:
                        if not write_converted_response_event(event):
                            return finish_downstream_stream_closed(
                                seam.last_write_error() or OSError("downstream closed")
                            )
                    except OSError as exc:
                        return finish_downstream_stream_closed(exc)
            if incomplete_frame or not converter.completed:
                self.close_connection = True
                write_proxy_event(
                    "upstream_stream_incomplete",
                    request_id=request_id,
                    model=model,
                    upstream=upstream_name,
                    status=502,
                    upstream_format=upstream_format,
                    inbound_format=inbound_format,
                )
                if not self._write_downstream_sse_error(
                    inbound_format=inbound_format,
                    upstream_name=upstream_name,
                    status=502,
                    error="upstream_stream_incomplete",
                    detail="Upstream Chat Completions stream ended without finish_reason or [DONE].",
                    redact_identity=relay_redact_identity,
                ):
                    return finish_downstream_stream_closed(
                        seam.last_write_error() or OSError("downstream closed")
                    )
                _capture_usage(usage_capture, None, missing_reason="stream_incomplete")
                return 502
            self.close_connection = True
            _capture_usage(usage_capture, None, missing_reason="async_usage_pending")
            return status

        if want_chat_output and upstream_format != "chat_completions":
            # Upstream returns Responses SSE; convert to Chat Completions SSE.
            line_ending = b"\n"
            events: list[Mapping[str, Any]] = []
            incomplete_frame = False
            try:
                for frame in self._iter_upstream_sse_events(
                    response,
                    event_resets_idle_timeout=_responses_sse_event_resets_idle_timeout,
                    on_chunk=observe_diagnostic_sse_line,
                ):
                    line_ending = _sse_line_ending(frame.raw)
                    event = _converted_sse_payload(
                        frame,
                        verified_source_format=verified_source_format,
                    )
                    if event is None or event == "[DONE]":
                        continue
                    events.append(event)
                    if usage_policy == UsagePolicy.ASYNC_TAP:
                        _offer_usage_observed_sse_line(
                            usage_context,
                            frame.raw,
                            upstream_format=upstream_format,
                        )
                    else:
                        _capture_usage(usage_capture, _usage_from_response_event(event))
            except (UpstreamSseSemanticError, SseFrameTooLargeError) as exc:
                return finish_converted_sse_semantic_error(exc)
            except UpstreamStreamIncompleteError:
                incomplete_frame = True
            except UpstreamStreamIdleTimeoutError as exc:
                if defer_stream_errors:
                    raise
                self.close_connection = True
                idle_detail = safe_upstream_error_detail(exc, redact_identity=relay_redact_identity)
                write_proxy_event(
                    "upstream_stream_idle_timeout",
                    request_id=request_id,
                    model=model,
                    upstream=upstream_name,
                    status=502,
                    upstream_format=upstream_format,
                    inbound_format=inbound_format,
                    stream_idle_timeout_seconds=exc.timeout_seconds,
                    stream_idle_phase=exc.phase,
                    detail=idle_detail,
                )
                if not send_downstream_response_headers_once():
                    return finish_downstream_stream_closed(
                        seam.last_write_error() or OSError("downstream closed")
                    )
                if not self._write_downstream_sse_error(
                    inbound_format=inbound_format,
                    upstream_name=upstream_name,
                    status=502,
                    error="upstream_stream_idle_timeout",
                    detail=idle_detail,
                    redact_identity=relay_redact_identity,
                ):
                    return finish_downstream_stream_closed(
                        seam.last_write_error() or OSError("downstream closed")
                    )
                _capture_usage(usage_capture, None, missing_reason="stream_idle_timeout")
                return 502
            except DownstreamKeepaliveFailedError:
                return finish_downstream_stream_closed(
                    seam.last_write_error() or OSError("downstream closed")
                )
            except (IncompleteRead, TimeoutError, OSError, URLError) as exc:
                if defer_stream_errors:
                    raise UpstreamStreamInterruptedError(exc) from exc
                self.close_connection = True
                stream_detail = safe_upstream_error_detail(exc, redact_identity=relay_redact_identity)
                write_proxy_event(
                    "upstream_stream_interrupted",
                    request_id=request_id,
                    model=model,
                    upstream=upstream_name,
                    status=502,
                    error=type(exc).__name__,
                    detail=stream_detail,
                )
                if not send_downstream_response_headers_once():
                    return finish_downstream_stream_closed(
                        seam.last_write_error() or OSError("downstream closed")
                    )
                if not self._write_downstream_sse_error(
                    inbound_format=inbound_format,
                    upstream_name=upstream_name,
                    exc=exc,
                    redact_identity=relay_redact_identity,
                ):
                    return finish_downstream_stream_closed(
                        seam.last_write_error() or OSError("downstream closed")
                    )
                _capture_usage(usage_capture, None, missing_reason="stream_interrupted")
                return 502
            try:
                if incomplete_frame:
                    raise UpstreamStreamIncompleteError(
                        "Upstream SSE stream ended with an incomplete pending frame"
                    )
                response_body = compatible_response_body(
                    _events_to_responses_body(events, require_completed=True),
                    upstream_name,
                    event_context=compatibility_event_context,
                )
            except UpstreamStreamIncompleteError:
                if defer_stream_errors:
                    raise
                self.close_connection = True
                write_proxy_event(
                    "upstream_stream_incomplete",
                    request_id=request_id,
                    model=model,
                    upstream=upstream_name,
                    status=502,
                    upstream_format=upstream_format,
                    inbound_format=inbound_format,
                )
                if not send_downstream_response_headers_once():
                    return finish_downstream_stream_closed(
                        seam.last_write_error() or OSError("downstream closed")
                    )
                if not self._write_downstream_sse_error(
                    inbound_format=inbound_format,
                    upstream_name=upstream_name,
                    status=502,
                    error="upstream_stream_incomplete",
                    detail="Upstream stream ended before response.completed.",
                    redact_identity=relay_redact_identity,
                ):
                    return finish_downstream_stream_closed(
                        seam.last_write_error() or OSError("downstream closed")
                    )
                _capture_usage(usage_capture, None, missing_reason="stream_incomplete")
                return 502

            try:
                converted_chat_chunks = _chat_completion_body_to_stream_chunks(
                    _response_body_to_chat_completion_body(response_body)
                )
            except UpstreamProtocolTranslationError:
                if verified_source_format is None:
                    raise
                return finish_converted_sse_semantic_error(
                    _verified_converted_sse_semantic_error(
                        verified_source_format
                    )
                )

            if not send_downstream_response_headers_once():
                return finish_downstream_stream_closed(
                    seam.last_write_error() or OSError("downstream closed")
                )
            for chunk in converted_chat_chunks:
                if not self._write_sse_data(chunk):
                    return finish_downstream_stream_closed(
                        seam.last_write_error() or OSError("downstream closed")
                    )
            if not self._write_sse_done():
                return finish_downstream_stream_closed(
                    seam.last_write_error() or OSError("downstream closed")
                )
            self.close_connection = True
            _capture_usage(
                usage_capture,
                None,
                missing_reason="async_usage_pending"
                if usage_policy == UsagePolicy.ASYNC_TAP
                else "upstream_missing_usage",
            )
            return status

        if upstream_format == "chat_completions":
            line_ending = b"\n"
            chunks: list[Mapping[str, Any] | str] = []
            incomplete_frame = False
            try:
                for frame in self._iter_upstream_sse_events(
                    response,
                    event_resets_idle_timeout=_chat_sse_event_resets_idle_timeout,
                    on_chunk=observe_diagnostic_sse_line,
                ):
                    line_ending = _sse_line_ending(frame.raw)
                    payload = _converted_sse_payload(
                        frame,
                        verified_source_format=verified_source_format,
                    )
                    if payload is None:
                        continue
                    if payload == "[DONE]":
                        chunks.append("[DONE]")
                        continue
                    chunks.append(payload)
                    if usage_policy == UsagePolicy.ASYNC_TAP:
                        _offer_usage_observed_sse_line(
                            usage_context,
                            frame.raw,
                            upstream_format=upstream_format,
                        )
                    else:
                        _capture_usage(usage_capture, _usage_from_payload(payload))
            except (UpstreamSseSemanticError, SseFrameTooLargeError) as exc:
                return finish_converted_sse_semantic_error(exc)
            except UpstreamStreamIncompleteError:
                incomplete_frame = True
            except UpstreamStreamIdleTimeoutError as exc:
                if defer_stream_errors:
                    raise
                self.close_connection = True
                idle_detail = safe_upstream_error_detail(exc, redact_identity=relay_redact_identity)
                write_proxy_event(
                    "upstream_stream_idle_timeout",
                    request_id=request_id,
                    model=model,
                    upstream=upstream_name,
                    status=502,
                    upstream_format=upstream_format,
                    inbound_format=inbound_format,
                    stream_idle_timeout_seconds=exc.timeout_seconds,
                    stream_idle_phase=exc.phase,
                    detail=idle_detail,
                )
                if not self._write_downstream_sse_error(
                    inbound_format=inbound_format,
                    upstream_name=upstream_name,
                    status=502,
                    error="upstream_stream_idle_timeout",
                    detail=idle_detail,
                    redact_identity=relay_redact_identity,
                ):
                    return finish_downstream_stream_closed(
                        seam.last_write_error() or OSError("downstream closed")
                    )
                _capture_usage(usage_capture, None, missing_reason="stream_idle_timeout")
                return 502
            except DownstreamKeepaliveFailedError:
                return finish_downstream_stream_closed(
                    seam.last_write_error() or OSError("downstream closed")
                )
            except (IncompleteRead, TimeoutError, OSError, URLError) as exc:
                if defer_stream_errors:
                    raise UpstreamStreamInterruptedError(exc) from exc
                self.close_connection = True
                stream_detail = safe_upstream_error_detail(exc, redact_identity=relay_redact_identity)
                write_proxy_event(
                    "upstream_stream_interrupted",
                    request_id=request_id,
                    model=model,
                    upstream=upstream_name,
                    status=502,
                    error=type(exc).__name__,
                    detail=stream_detail,
                )
                if not self._write_downstream_sse_error(
                    inbound_format=inbound_format,
                    upstream_name=upstream_name,
                    exc=exc,
                    redact_identity=relay_redact_identity,
                ):
                    return finish_downstream_stream_closed(
                        seam.last_write_error() or OSError("downstream closed")
                    )
                _capture_usage(usage_capture, None, missing_reason="stream_interrupted")
                return 502
            if incomplete_frame or not _chat_stream_chunks_have_terminal(chunks):
                if defer_stream_errors:
                    raise UpstreamStreamIncompleteError(
                        "Chat Completions stream ended without finish_reason or [DONE]"
                    )
                self.close_connection = True
                write_proxy_event(
                    "upstream_stream_incomplete",
                    request_id=request_id,
                    model=model,
                    upstream=upstream_name,
                    status=502,
                    upstream_format=upstream_format,
                    inbound_format=inbound_format,
                )
                if not self._write_downstream_sse_error(
                    inbound_format=inbound_format,
                    upstream_name=upstream_name,
                    status=502,
                    error="upstream_stream_incomplete",
                    detail="Upstream Chat Completions stream ended without finish_reason or [DONE].",
                    redact_identity=relay_redact_identity,
                ):
                    return finish_downstream_stream_closed(
                        seam.last_write_error() or OSError("downstream closed")
                    )
                _capture_usage(usage_capture, None, missing_reason="stream_incomplete")
                return 502
            if upstream_name != "official" and not want_chat_output:
                chunks, _ = _suppress_chat_reasoning_extensions(
                    chunks,
                    event_context=event_context,
                    upstream_name=upstream_name,
                )
            chat_summary = _chat_stream_shape_summary(chunks)
            _write_adapter_event(
                event_context,
                "chat_stream_shape_summary",
                upstream=upstream_name,
                inbound_format=inbound_format,
                want_chat_output=want_chat_output,
                **chat_summary,
            )
            lifecycle_issue = (
                _chat_stream_lifecycle_final_issue(chunks, chat_summary, event_context, request_kind)
                if status < 400
                else None
            )
            if lifecycle_issue is not None:
                _write_adapter_event(
                    event_context,
                    _lifecycle_final_issue_event_name(lifecycle_issue),
                    upstream=upstream_name,
                    inbound_format=inbound_format,
                    want_chat_output=want_chat_output,
                    **chat_summary,
                )
                _capture_usage(
                    usage_capture,
                    None,
                    missing_reason=_lifecycle_final_issue_missing_reason(lifecycle_issue),
                )
                _raise_lifecycle_final_issue(upstream_name, lifecycle_issue)
            if want_chat_output:
                response_body = compatible_response_body(
                    _events_to_responses_body(_chat_stream_chunks_to_response_events(chunks)),
                    upstream_name,
                    event_context=compatibility_event_context,
                )
                if not send_downstream_response_headers_once():
                    return finish_downstream_stream_closed(
                        seam.last_write_error() or OSError("downstream closed")
                    )
                for chunk in _chat_completion_body_to_stream_chunks(
                    _response_body_to_chat_completion_body(response_body)
                ):
                    if not self._write_sse_data(chunk):
                        return finish_downstream_stream_closed(
                            seam.last_write_error() or OSError("downstream closed")
                        )
            else:
                events = _chat_stream_chunks_to_response_events(chunks)
                runtime_tool_plan, runtime_tool_stream = (
                    _runtime_tool_compatibility_stream_for_attempt(
                        compatibility_event_context
                    )
                )
                if runtime_tool_plan is not None and runtime_tool_stream is not None:
                    decoded_events: list[Mapping[str, Any]] = []
                    try:
                        for event in events:
                            decoded_events.extend(
                                runtime_tool_stream.decode_events_for_event(event)
                            )
                    except RuntimeToolCompatibilityError as exc:
                        _raise_runtime_tool_compatibility_error(exc)
                    _write_runtime_tool_adapter_response_evidence(
                        runtime_tool_plan,
                        events,
                        decoded_events,
                        event_context,
                        surface="sse",
                    )
                    events = decoded_events
                _write_adapter_event(
                    event_context,
                    "chat_to_responses_event_summary",
                    upstream=upstream_name,
                    inbound_format=inbound_format,
                    want_chat_output=want_chat_output,
                    stage="converted",
                    **_response_events_shape_summary(events),
                )
                events, _ = _repair_missing_required_subagent_call_events(events, event_context)
                events, _ = _adapt_third_party_apply_patch_stream_events(
                    events,
                    event_context=compatibility_event_context,
                )
                events, _ = _normalize_third_party_tool_call(events, compatibility_event_context)
                events, _ = _suppress_bounded_tool_search_calls(
                    events,
                    compatibility_event_context,
                )
                _write_adapter_event(
                    event_context,
                    "chat_to_responses_event_summary",
                    upstream=upstream_name,
                    inbound_format=inbound_format,
                    want_chat_output=want_chat_output,
                    stage="normalized",
                    **_response_events_shape_summary(events),
                )
                events, _ = _suppress_worker_multi_agent_tool_calls(events, event_context)
                events, _ = _suppress_coordinator_forbidden_tool_calls(events, event_context)
                events, _ = _downgrade_invalid_third_party_tool_calls(events)
                _write_adapter_event(
                    event_context,
                    "chat_to_responses_event_summary",
                    upstream=upstream_name,
                    inbound_format=inbound_format,
                    want_chat_output=want_chat_output,
                    stage="downgraded",
                    **_response_events_shape_summary(events),
                )
                events, _ = _guard_duplicate_multi_agent_spawn_calls(events, event_context)
                events, _ = _apply_external_worker_response_contract(
                    events,
                    compatibility_event_context,
                    surface="sse",
                    attach_sidecars=False,
                )
                events, _ = _coerce_exact_spawn_prompt_tool_calls(events, event_context)
                events, _ = _coerce_required_subagent_tool_calls(
                    events,
                    event_context,
                    surface="sse",
                )
                events, _ = _reconcile_function_call_argument_events(events)
                events, _ = _repair_missing_required_subagent_call_events(events, event_context)
                events, _ = _apply_external_worker_response_contract(
                    events,
                    compatibility_event_context,
                    surface="sse",
                    validate_selectors=False,
                )
                _write_adapter_event(
                    event_context,
                    "chat_to_responses_event_summary",
                    upstream=upstream_name,
                    inbound_format=inbound_format,
                    want_chat_output=want_chat_output,
                    stage="final",
                    **_response_events_shape_summary(events),
                )
                if not send_downstream_response_headers_once():
                    return finish_downstream_stream_closed(
                        seam.last_write_error() or OSError("downstream closed")
                    )
                for event in events:
                    if not self._write_sse_bytes(
                        _sse_json_line(event, line_ending) + line_ending
                    ):
                        return finish_downstream_stream_closed(
                            seam.last_write_error() or OSError("downstream closed")
                        )
            if not self._write_sse_done():
                return finish_downstream_stream_closed(
                    seam.last_write_error() or OSError("downstream closed")
                )
            self.close_connection = True
            _capture_usage(
                usage_capture,
                None,
                missing_reason="async_usage_pending"
                if usage_policy == UsagePolicy.ASYNC_TAP
                else "upstream_missing_usage",
            )
            return status

        if lifecycle_final_retry_enabled:
            reasoning_stats: dict[str, Any] = {
                "seen": False,
                "original_event_counts": {},
                "rewritten_event_counts": {},
                "delta_events": 0,
                "delta_chars": 0,
            }
            saw_response_event = False
            saw_terminal_event = False
            downstream_output_started = False
            buffered_lines: list[tuple[bytes, bool]] = []
            rewritten_events: list[Mapping[str, Any]] = []
            apply_patch_stream_adapter = (
                _ThirdPartyApplyPatchStreamAdapter(compatibility_event_context)
                if (
                    upstream_name != "official"
                    and not want_chat_output
                    and _apply_patch_adapter_enabled(compatibility_event_context)
                )
                else None
            )
            try:
                for line in self._iter_upstream_sse_lines(
                    response,
                    line_resets_idle_timeout=_responses_sse_line_resets_idle_timeout,
                    on_line=observe_diagnostic_sse_line,
                ):
                    if not line:
                        break
                    original_payload = _parse_sse_json_payload(line) if upstream_name != "official" else None
                    usage_payload = _parse_sse_json_payload(line)
                    if isinstance(usage_payload, Mapping):
                        remember_response_id(usage_payload)
                        event_type = usage_payload.get("type")
                        if isinstance(event_type, str) and (event_type.startswith("response.") or event_type == "error"):
                            saw_response_event = True
                        if _responses_events_have_terminal([usage_payload]):
                            saw_terminal_event = True
                        if _responses_event_starts_downstream_output(usage_payload):
                            downstream_output_started = True
                            if seam is not None:
                                seam.mark_downstream_content_exposed()
                        _capture_usage(usage_capture, _usage_from_response_event(usage_payload))
                    rewritten_line = line
                    if apply_patch_stream_adapter is not None and isinstance(usage_payload, Mapping):
                        replacement_events, apply_patch_changed = apply_patch_stream_adapter.events_for_event(
                            usage_payload
                        )
                        if apply_patch_changed:
                            rewritten_line = (
                                _sse_json_line(replacement_events[0], _sse_line_ending(line))
                                if replacement_events
                                else b""
                            )
                    rewritten_line = compatible_sse_line(
                        rewritten_line,
                        upstream_name,
                        event_context=compatibility_event_context,
                    )
                    rewritten_payloads = (
                        _parse_sse_json_payloads(rewritten_line)
                        if upstream_name != "official"
                        else ([usage_payload] if isinstance(usage_payload, Mapping) else [])
                    )
                    if rewritten_payloads:
                        _count_sse_reasoning_event(reasoning_stats, original_payload, rewritten_payloads[0])
                        for emitted_payload in rewritten_payloads[1:]:
                            _count_sse_reasoning_event(reasoning_stats, None, emitted_payload)
                        rewritten_events.extend(rewritten_payloads)
                    else:
                        _count_sse_reasoning_event(reasoning_stats, original_payload, None)
                    terminal = _responses_events_have_terminal(rewritten_payloads)
                    buffered_lines.append((rewritten_line, terminal))
                    if saw_terminal_event:
                        break
            except UpstreamStreamIdleTimeoutError as exc:
                self.close_connection = True
                write_proxy_event(
                    "upstream_stream_idle_timeout",
                    request_id=request_id,
                    model=model,
                    upstream=upstream_name,
                    status=502,
                    upstream_format=upstream_format,
                    inbound_format=inbound_format,
                    stream_idle_timeout_seconds=exc.timeout_seconds,
                    stream_idle_phase=exc.phase,
                    terminal_seen=saw_terminal_event,
                    downstream_output_started=downstream_output_started,
                    detail=safe_upstream_error_detail(exc, redact_identity=relay_redact_identity),
                )
                if not send_downstream_response_headers_once():
                    return finish_downstream_stream_closed(
                        seam.last_write_error() or OSError("downstream closed")
                    )
                idle_detail = safe_upstream_error_detail(exc, redact_identity=relay_redact_identity)
                if not self._write_downstream_sse_error(
                    inbound_format=inbound_format,
                    upstream_name=upstream_name,
                    status=502,
                    error="upstream_stream_idle_timeout",
                    detail=idle_detail,
                    redact_identity=relay_redact_identity,
                ):
                    return finish_downstream_stream_closed(
                        seam.last_write_error() or OSError("downstream closed")
                    )
                _capture_usage(usage_capture, None, missing_reason="stream_idle_timeout")
                return 502
            except DownstreamKeepaliveFailedError:
                return finish_downstream_stream_closed(
                    seam.last_write_error() or OSError("downstream closed")
                )
            except (IncompleteRead, TimeoutError, OSError, URLError) as exc:
                self.close_connection = True
                stream_detail = safe_upstream_error_detail(exc, redact_identity=relay_redact_identity)
                write_proxy_event(
                    "upstream_stream_interrupted",
                    request_id=request_id,
                    model=model,
                    upstream=upstream_name,
                    status=502,
                    error=type(exc).__name__,
                    detail=stream_detail,
                )
                if not send_downstream_response_headers_once():
                    return finish_downstream_stream_closed(
                        seam.last_write_error() or OSError("downstream closed")
                    )
                if not self._write_downstream_sse_error(
                    inbound_format=inbound_format,
                    upstream_name=upstream_name,
                    exc=exc,
                    redact_identity=relay_redact_identity,
                ):
                    return finish_downstream_stream_closed(
                        seam.last_write_error() or OSError("downstream closed")
                    )
                _capture_usage(usage_capture, None, missing_reason="stream_interrupted")
                return 502
            if apply_patch_stream_adapter is not None and saw_terminal_event:
                apply_patch_stream_adapter.finish()
            if status < 400 and saw_response_event and not saw_terminal_event:
                self.close_connection = True
                write_proxy_event(
                    "upstream_stream_incomplete",
                    request_id=request_id,
                    model=model,
                    upstream=upstream_name,
                    status=502,
                    upstream_format=upstream_format,
                    inbound_format=inbound_format,
                )
                if not send_downstream_response_headers_once():
                    return finish_downstream_stream_closed(
                        seam.last_write_error() or OSError("downstream closed")
                    )
                if not self._write_downstream_sse_error(
                    inbound_format=inbound_format,
                    upstream_name=upstream_name,
                    status=502,
                    error="upstream_stream_incomplete",
                    detail="Upstream Responses stream ended without a terminal event.",
                    redact_identity=relay_redact_identity,
                ):
                    return finish_downstream_stream_closed(
                        seam.last_write_error() or OSError("downstream closed")
                    )
                _capture_usage(usage_capture, None, missing_reason="stream_incomplete")
                return 502
            lifecycle_issue = (
                _responses_events_lifecycle_final_issue(rewritten_events, event_context, request_kind)
                if status < 400
                else None
            )
            if lifecycle_issue is not None:
                _write_adapter_event(
                    event_context,
                    _lifecycle_final_issue_event_name(lifecycle_issue),
                    upstream=upstream_name,
                    inbound_format=inbound_format,
                    want_chat_output=want_chat_output,
                    **_response_events_shape_summary(list(rewritten_events)),
                )
                _capture_usage(
                    usage_capture,
                    None,
                    missing_reason=_lifecycle_final_issue_missing_reason(lifecycle_issue),
                )
                _raise_lifecycle_final_issue(upstream_name, lifecycle_issue)
            if upstream_name != "official" and reasoning_stats["seen"]:
                write_proxy_event(
                    "sse_reasoning_summary",
                    request_id=request_id,
                    model=model,
                    upstream=upstream_name,
                    original_event_counts=reasoning_stats["original_event_counts"],
                    rewritten_event_counts=reasoning_stats["rewritten_event_counts"],
                    delta_events=reasoning_stats["delta_events"],
                    delta_chars=reasoning_stats["delta_chars"],
                )
            if not send_downstream_response_headers_once():
                return finish_downstream_stream_closed(
                    seam.last_write_error() or OSError("downstream closed")
                )
            for buffered_line, terminal in buffered_lines:
                if not self._write_sse_bytes(buffered_line):
                    return finish_downstream_stream_closed(
                        seam.last_write_error() or OSError("downstream closed")
                    )
                if terminal:
                    separator = _sse_event_separator_after_line(buffered_line)
                    if separator:
                        if not self._write_sse_bytes(separator):
                            return finish_downstream_stream_closed(
                                seam.last_write_error() or OSError("downstream closed")
                            )
                if terminal:
                    break
            self.close_connection = True
            _capture_usage(usage_capture, None)
            return status

        reasoning_stats: dict[str, Any] = {
            "seen": False,
            "original_event_counts": {},
            "rewritten_event_counts": {},
            "delta_events": 0,
            "delta_chars": 0,
        }
        saw_terminal_event = False
        saw_completed_event = False
        visible_or_tool_output_seen = False
        downstream_output_started = False
        pending_sse_event_metadata: list[bytes] = []
        pending_downstream_lines: list[bytes] = []
        drop_next_sse_separator = False
        created_response: dict[str, Any] | None = None
        completed_tool_output_items: list[dict[str, Any]] = []
        last_response_event_type: str | None = None
        apply_patch_stream_adapter = (
            _ThirdPartyApplyPatchStreamAdapter(compatibility_event_context)
            if (
                upstream_name != "official"
                and not want_chat_output
                and _apply_patch_adapter_enabled(compatibility_event_context)
            )
            else None
        )

        class DownstreamWriteFailedError(Exception):
            """Raised when a required downstream SSE commit fails."""

        def write_or_queue_downstream_line(out_line: bytes, *, buffer: bool = False, force: bool = False) -> None:
            if not out_line:
                return
            if buffer and not force:
                pending_downstream_lines.append(out_line)
                return
            if pending_downstream_lines:
                for pending_line in pending_downstream_lines:
                    if not self._write_sse_bytes(pending_line):
                        raise DownstreamWriteFailedError()
                pending_downstream_lines.clear()
            if not self._write_sse_bytes(out_line):
                raise DownstreamWriteFailedError()

        def flush_pending_downstream_lines() -> None:
            if not pending_downstream_lines:
                return
            for pending_line in pending_downstream_lines:
                if not self._write_sse_bytes(pending_line):
                    raise DownstreamWriteFailedError()
            pending_downstream_lines.clear()

        def write_response_failed_event(error_payload: Mapping[str, Any]) -> None:
            pending_downstream_lines.clear()
            error_value = error_payload.get("error")
            if isinstance(error_value, Mapping):
                sanitized_error: dict[str, Any] = {
                    key: _redact_identity_in_text(str(value), relay_redact_identity)
                    for key, value in error_value.items()
                }
            else:
                sanitized_error = {
                    "message": _redact_identity_in_text(
                        str(error_value or "Upstream stream error"),
                        relay_redact_identity,
                    )
                }
            response_payload = {
                "id": f"resp_{uuid.uuid4().hex[:12]}",
                "object": "response",
                "status": "failed",
                "model": model,
                "output": [],
                "error": sanitized_error,
            }
            if not self._write_sse_event(
                "response.failed",
                {"type": "response.failed", "response": response_payload},
            ):
                raise DownstreamWriteFailedError()

        def remember_completed_tool_event(payload: Mapping[str, Any]) -> None:
            nonlocal created_response
            event_type = payload.get("type")
            if event_type == "response.created":
                response_payload = payload.get("response")
                if isinstance(response_payload, Mapping):
                    created_response = dict(response_payload)
                return
            if event_type != "response.output_item.done":
                return
            item = payload.get("item")
            if not isinstance(item, Mapping):
                return
            completed = _responses_completed_tool_item(item)
            if completed is not None:
                completed_tool_output_items.append(completed)

        def synthesize_completed_tool_response() -> bool:
            if upstream_name == "official" or downstream_output_started or not completed_tool_output_items:
                return False
            event = _synthetic_response_completed_from_tool_items(
                created_response=created_response,
                model=model,
                output_items=completed_tool_output_items,
            )
            if event is None:
                return False
            pending_line_count = len(pending_downstream_lines)
            pending_byte_count = sum(len(pending_line) for pending_line in pending_downstream_lines)
            flush_pending_downstream_lines()
            if not self._write_sse_event("response.completed", event):
                raise DownstreamWriteFailedError()
            write_proxy_event(
                "upstream_stream_incomplete_synthesized_terminal",
                request_id=request_id,
                model=model,
                upstream=upstream_name,
                status=200,
                upstream_format=upstream_format,
                inbound_format=inbound_format,
                completed_tool_calls=len(completed_tool_output_items),
                pending_downstream_lines=pending_line_count,
                pending_downstream_bytes=pending_byte_count,
                last_event_type=last_response_event_type,
            )
            return True

        try:
            for line in self._iter_upstream_sse_lines(
                response,
                line_resets_idle_timeout=_responses_sse_line_resets_idle_timeout,
                on_line=observe_diagnostic_sse_line,
            ):
                if not line:
                    break
                if upstream_name != "official" and _is_sse_blank_line(line):
                    if drop_next_sse_separator:
                        drop_next_sse_separator = False
                        pending_sse_event_metadata = []
                        continue
                    if pending_sse_event_metadata:
                        pending_sse_event_metadata = []
                        continue
                    write_or_queue_downstream_line(line, buffer=bool(pending_downstream_lines))
                    continue
                if upstream_name != "official" and _is_sse_event_metadata_line(line):
                    pending_sse_event_metadata.append(line)
                    continue
                original_payload = _parse_sse_json_payload(line) if upstream_name != "official" else None
                usage_payload = _parse_sse_json_payload(line)
                buffer_current_line = False
                if isinstance(usage_payload, Mapping):
                    remember_response_id(usage_payload)
                    event_type = usage_payload.get("type")
                    if isinstance(event_type, str) and (event_type.startswith("response.") or event_type == "error"):
                        last_response_event_type = event_type
                    if event_type == "error":
                        exc = UpstreamStreamErrorEvent(usage_payload)
                        if defer_stream_errors and not downstream_output_started:
                            pending_downstream_lines.clear()
                            pending_sse_event_metadata = []
                            raise exc
                        self.close_connection = True
                        stream_error_detail = safe_upstream_error_detail(
                            exc, redact_identity=relay_redact_identity
                        )
                        write_proxy_event(
                            "upstream_stream_error_event",
                            request_id=request_id,
                            model=model,
                            upstream=upstream_name,
                            status=502,
                            upstream_format=upstream_format,
                            inbound_format=inbound_format,
                            failure_class=_upstream_failure_class(exc),
                            detail=stream_error_detail,
                        )
                        write_response_failed_event(usage_payload)
                        _capture_usage(usage_capture, None, missing_reason="stream_error_event")
                        return 502
                    if _responses_events_have_terminal([usage_payload]):
                        if not saw_terminal_event:
                            _observe_gateway_diagnostic(
                                "observe_terminal",
                                request_id,
                                forwarded=False,
                            )
                        saw_terminal_event = True
                    if event_type == "response.completed":
                        saw_completed_event = True
                    if _responses_event_has_visible_or_tool_output(usage_payload, upstream_name):
                        visible_or_tool_output_seen = True
                        if seam is not None:
                            seam.mark_downstream_content_exposed()
                    empty_completed_candidate = (
                        upstream_name != "official"
                        and event_type == "response.completed"
                        and not visible_or_tool_output_seen
                    )
                    is_tool_construction = _responses_event_is_tool_call_construction(usage_payload)
                    if (
                        is_tool_construction
                        and not downstream_output_started
                        and not saw_terminal_event
                    ):
                        buffer_current_line = True
                    else:
                        item = usage_payload.get("item") if event_type == "response.output_item.done" else None
                        is_reasoning_done = isinstance(item, Mapping) and item.get("type") == "reasoning"
                        if (
                            _responses_event_commits_downstream_output(usage_payload, upstream_name)
                            and (
                                upstream_name == "official"
                                or is_reasoning_done
                                or _is_reasoning_summary_stream_event(usage_payload)
                            )
                        ):
                            downstream_output_started = True
                    buffer_current_line = (
                        buffer_current_line
                        or empty_completed_candidate
                        or not downstream_output_started
                        and not saw_terminal_event
                    )
                    _capture_usage(usage_capture, _usage_from_response_event(usage_payload))
                elif (
                    pending_downstream_lines
                    and not downstream_output_started
                    and not saw_terminal_event
                ):
                    buffer_current_line = True
                if apply_patch_stream_adapter is not None and isinstance(usage_payload, Mapping):
                    replacement_events, apply_patch_changed = apply_patch_stream_adapter.events_for_event(usage_payload)
                    if apply_patch_changed:
                        if not replacement_events:
                            line = b""
                        else:
                            line = _sse_json_line(replacement_events[0], _sse_line_ending(line))
                line = compatible_sse_line(line, upstream_name, event_context=compatibility_event_context)
                rewritten_payloads = (
                    _parse_sse_json_payloads(line)
                    if upstream_name != "official"
                    else []
                )
                if rewritten_payloads:
                    for emitted_payload in rewritten_payloads:
                        remember_completed_tool_event(emitted_payload)
                    _count_sse_reasoning_event(reasoning_stats, original_payload, rewritten_payloads[0])
                    for emitted_payload in rewritten_payloads[1:]:
                        _count_sse_reasoning_event(reasoning_stats, None, emitted_payload)
                elif isinstance(usage_payload, Mapping):
                    remember_completed_tool_event(usage_payload)
                    _count_sse_reasoning_event(reasoning_stats, original_payload, None)
                else:
                    _count_sse_reasoning_event(reasoning_stats, original_payload, None)

                if not line and upstream_name != "official":
                    pending_sse_event_metadata = []
                    drop_next_sse_separator = True
                    continue

                if pending_sse_event_metadata:
                    for metadata_line in pending_sse_event_metadata:
                        write_or_queue_downstream_line(metadata_line, buffer=buffer_current_line)
                    pending_sse_event_metadata = []
                write_or_queue_downstream_line(line, buffer=buffer_current_line)
                if saw_terminal_event:
                    separator = _sse_event_separator_after_line(line)
                    if separator:
                        flush_terminal = not (
                            upstream_name != "official"
                            and isinstance(usage_payload, Mapping)
                            and usage_payload.get("type") == "response.completed"
                            and not visible_or_tool_output_seen
                        )
                        write_or_queue_downstream_line(
                            separator,
                            buffer=not flush_terminal,
                            force=flush_terminal,
                        )
                        if flush_terminal:
                            _observe_gateway_diagnostic(
                                "observe_terminal",
                                request_id,
                                forwarded=True,
                            )
                if saw_terminal_event:
                    break
        except UpstreamStreamIdleTimeoutError as exc:
            if defer_stream_errors and not downstream_output_started:
                raise
            self.close_connection = True
            idle_detail = safe_upstream_error_detail(exc, redact_identity=relay_redact_identity)
            write_proxy_event(
                "upstream_stream_idle_timeout",
                request_id=request_id,
                model=model,
                upstream=upstream_name,
                status=502,
                upstream_format=upstream_format,
                inbound_format=inbound_format,
                stream_idle_timeout_seconds=exc.timeout_seconds,
                stream_idle_phase=exc.phase,
                terminal_seen=saw_terminal_event,
                downstream_output_started=downstream_output_started,
                detail=idle_detail,
            )
            if not self._write_downstream_sse_error(
                inbound_format=inbound_format,
                upstream_name=upstream_name,
                status=502,
                error="upstream_stream_idle_timeout",
                detail=idle_detail,
                redact_identity=relay_redact_identity,
            ):
                return finish_downstream_stream_closed(
                    seam.last_write_error() or OSError("downstream closed")
                )
            _capture_usage(usage_capture, None, missing_reason="stream_idle_timeout")
            return 502
        except DownstreamKeepaliveFailedError:
            return finish_downstream_stream_closed(
                seam.last_write_error() or OSError("downstream closed")
            )
        except (IncompleteRead, TimeoutError, OSError, URLError) as exc:
            if defer_stream_errors and not downstream_output_started:
                raise UpstreamStreamInterruptedError(exc) from exc
            self.close_connection = True
            stream_detail = safe_upstream_error_detail(exc, redact_identity=relay_redact_identity)
            write_proxy_event(
                "upstream_stream_interrupted",
                request_id=request_id,
                model=model,
                upstream=upstream_name,
                status=502,
                error=type(exc).__name__,
                detail=stream_detail,
            )
            if not self._write_downstream_sse_error(
                inbound_format=inbound_format,
                upstream_name=upstream_name,
                exc=exc,
                redact_identity=relay_redact_identity,
            ):
                return finish_downstream_stream_closed(
                    seam.last_write_error() or OSError("downstream closed")
                )
            _capture_usage(usage_capture, None, missing_reason="stream_interrupted")
            return 502
        except DownstreamWriteFailedError:
            return finish_downstream_stream_closed(
                seam.last_write_error() or OSError("downstream closed")
            )
        if status < 400 and not saw_terminal_event:
            try:
                synthesized_terminal = synthesize_completed_tool_response()
            except DownstreamWriteFailedError:
                return finish_downstream_stream_closed(
                    seam.last_write_error() or OSError("downstream closed")
                )
            if synthesized_terminal:
                if apply_patch_stream_adapter is not None:
                    apply_patch_stream_adapter.finish(allow_missing_terminal=True)
                self.close_connection = True
                _capture_usage(usage_capture, None, missing_reason="synthetic_tool_terminal")
                return status
            if defer_stream_errors and not downstream_output_started:
                raise UpstreamStreamIncompleteError("Responses stream ended before response.completed")
            self.close_connection = True
            retry_forbidden = bool(
                downstream_output_started
                or completed_tool_output_items
                or seam._downstream_content_exposed
            )
            write_proxy_event(
                "upstream_stream_incomplete",
                request_id=request_id,
                model=model,
                upstream=upstream_name,
                status=502,
                upstream_format=upstream_format,
                inbound_format=inbound_format,
                terminal_seen=saw_terminal_event,
                downstream_output_started=downstream_output_started,
                terminal=False,
                failure_class=RETRY_FAILURE_QUICK_TRANSIENT,
                failure_phase="stream_body",
                failure_side="upstream_read",
                retry_forbidden=retry_forbidden,
                retry_safety_class=(
                    RETRY_SAFETY_SUPPRESSED_POST_EXPOSURE
                    if retry_forbidden
                    else RETRY_SAFETY_SUPPRESSED_POST_WRITE
                ),
                completed_tool_calls=len(completed_tool_output_items),
                pending_downstream_lines=len(pending_downstream_lines),
                pending_downstream_bytes=sum(len(pending_line) for pending_line in pending_downstream_lines),
                last_event_type=last_response_event_type,
            )
            if not self._write_downstream_sse_error(
                inbound_format=inbound_format,
                upstream_name=upstream_name,
                status=502,
                error="upstream_stream_incomplete",
                detail="Upstream Responses stream ended without a terminal event.",
                redact_identity=relay_redact_identity,
            ):
                return finish_downstream_stream_closed(
                    seam.last_write_error() or OSError("downstream closed")
                )
            _capture_usage(usage_capture, None, missing_reason="stream_incomplete")
            return 502
        if apply_patch_stream_adapter is not None:
            apply_patch_stream_adapter.finish()
        if (
            status < 400
            and upstream_name != "official"
            and saw_completed_event
            and not visible_or_tool_output_seen
        ):
            pending_line_count = len(pending_downstream_lines)
            pending_byte_count = sum(len(pending_line) for pending_line in pending_downstream_lines)
            pending_downstream_lines.clear()
            detail = "Upstream Responses stream completed without visible output or tool calls."
            if defer_stream_errors:
                raise UpstreamEmptyCompletedResponseError(
                    f"Responses stream returned empty completed response: {detail}"
                )
            self.close_connection = True
            write_proxy_event(
                "upstream_empty_completed_response",
                request_id=request_id,
                model=model,
                upstream=upstream_name,
                status=502,
                upstream_format=upstream_format,
                inbound_format=inbound_format,
                terminal_seen=saw_terminal_event,
                completed_seen=saw_completed_event,
                visible_or_tool_output_seen=visible_or_tool_output_seen,
                completed_tool_calls=len(completed_tool_output_items),
                pending_downstream_lines=pending_line_count,
                pending_downstream_bytes=pending_byte_count,
                last_event_type=last_response_event_type,
            )
            if not self._write_downstream_sse_error(
                inbound_format=inbound_format,
                upstream_name=upstream_name,
                status=502,
                error="upstream_empty_completed_response",
                detail=detail,
                redact_identity=relay_redact_identity,
            ):
                return finish_downstream_stream_closed(
                    seam.last_write_error() or OSError("downstream closed")
                )
            _capture_usage(usage_capture, None, missing_reason="empty_completed_response")
            return 502
        if upstream_name != "official" and reasoning_stats["seen"]:
            write_proxy_event(
                "sse_reasoning_summary",
                request_id=request_id,
                model=model,
                upstream=upstream_name,
                original_event_counts=reasoning_stats["original_event_counts"],
                rewritten_event_counts=reasoning_stats["rewritten_event_counts"],
                delta_events=reasoning_stats["delta_events"],
                delta_chars=reasoning_stats["delta_chars"],
            )
        self.close_connection = True
        _capture_usage(
            usage_capture,
            None,
            missing_reason="async_usage_pending"
            if usage_policy == UsagePolicy.ASYNC_TAP
            else "upstream_missing_usage",
        )
        return status

    if not self._write_non_streaming_body_relay(body):
        return finish_downstream_stream_closed(
            seam.last_write_error() or OSError("downstream closed")
        )
    self.close_connection = True
    _capture_usage(
        usage_capture,
        None,
        missing_reason="async_usage_pending"
        if usage_policy == UsagePolicy.ASYNC_TAP
        else "upstream_missing_usage",
    )
    return status


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
