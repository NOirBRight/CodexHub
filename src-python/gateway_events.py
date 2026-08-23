"""Gateway telemetry writer, usage observation, and diagnostic observation.

This module owns the process-wide ``GATEWAY_EVENT_WRITER`` singleton so importing
it cannot fork a second event stream. Callers must import these symbols rather
than redefine them.
"""

from __future__ import annotations

import json
import os
import queue
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import bounded_event_writer
import diagnostic_recorder
import proxy_telemetry
from gateway_admission import USER_REQUESTED_SHUTDOWN_OUTCOME
from gateway_settings import runtime_proxy_dir as _runtime_proxy_dir
from gateway_sse import sse_payload_bytes as _sse_payload_bytes


def _env_positive_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, str(default)) or str(default)))
    except ValueError:
        return default


def _runtime_codex_dir() -> Path:
    return _runtime_proxy_dir().parent


RUNTIME_CODEX_DIR = _runtime_codex_dir()
PROXY_EVENT_LOG_PATH = _runtime_proxy_dir() / "codex-proxy-events.jsonl"
GATEWAY_EVENT_QUEUE_MAX_RECORDS = _env_positive_int("CODEX_GATEWAY_EVENT_QUEUE_MAX_RECORDS", 4096)
GATEWAY_EVENT_QUEUE_MAX_BYTES = _env_positive_int("CODEX_GATEWAY_EVENT_QUEUE_MAX_BYTES", 4 * 1024 * 1024)
GATEWAY_EVENT_WRITER_SHUTDOWN_TIMEOUT_SECONDS = 5.0

GATEWAY_DIAGNOSTIC_RECORDER: diagnostic_recorder.DiagnosticRecorderProtocol | None = None


def _gateway_event_writer_recovery_record(
    summary: bounded_event_writer.RecoverySummary,
) -> Mapping[str, Any]:
    return proxy_telemetry.prepare_event_payload(
        "telemetry_writer_recovered",
        {
            "overflow_records": summary.overflow_records,
            "overflow_bytes": summary.overflow_bytes,
            "failed_records": summary.failed_records,
            "failure_count": summary.failure_count,
            "failure_categories": list(summary.failure_categories),
        },
        RUNTIME_CODEX_DIR,
    )


def _build_event_writer() -> bounded_event_writer.BoundedEventWriter:
    return bounded_event_writer.BoundedEventWriter(
        bounded_event_writer.JsonlFileSink(PROXY_EVENT_LOG_PATH),
        max_records=GATEWAY_EVENT_QUEUE_MAX_RECORDS,
        max_bytes=GATEWAY_EVENT_QUEUE_MAX_BYTES,
        recovery_record_factory=_gateway_event_writer_recovery_record,
        thread_name="codex-gateway-event-writer",
    )


def refresh_runtime_paths() -> None:
    """Recompute CODEX_HOME-derived paths and replace the event writer.

    ``codex_proxy`` reload tests change ``CODEX_HOME`` and re-exec the facade;
    the owning module must rebuild the singleton against the new home.
    """

    global RUNTIME_CODEX_DIR, PROXY_EVENT_LOG_PATH, GATEWAY_EVENT_WRITER
    global GATEWAY_EVENT_QUEUE_MAX_RECORDS, GATEWAY_EVENT_QUEUE_MAX_BYTES
    previous = globals().get("GATEWAY_EVENT_WRITER")
    RUNTIME_CODEX_DIR = _runtime_codex_dir()
    PROXY_EVENT_LOG_PATH = _runtime_proxy_dir() / "codex-proxy-events.jsonl"
    GATEWAY_EVENT_QUEUE_MAX_RECORDS = _env_positive_int("CODEX_GATEWAY_EVENT_QUEUE_MAX_RECORDS", 4096)
    GATEWAY_EVENT_QUEUE_MAX_BYTES = _env_positive_int("CODEX_GATEWAY_EVENT_QUEUE_MAX_BYTES", 4 * 1024 * 1024)
    if isinstance(previous, bounded_event_writer.BoundedEventWriter):
        previous.shutdown(timeout=1.0)
    GATEWAY_EVENT_WRITER = _build_event_writer()


GATEWAY_EVENT_WRITER = _build_event_writer()


def observe_gateway_diagnostic(method: str, *args: Any, **kwargs: Any) -> None:
    """Keep optional recorder failures out of Gateway request behavior."""

    recorder = GATEWAY_DIAGNOSTIC_RECORDER
    if recorder is None:
        return
    try:
        observation = getattr(recorder, method, None)
        if callable(observation):
            observation(*args, **kwargs)
    except Exception:
        return


def public_event_context(context: Mapping[str, Any] | None) -> dict[str, Any]:
    return {
        key: value
        for key, value in (context or {}).items()
        if not str(key).startswith("_")
    }


def enqueue_gateway_event_payload(payload: Mapping[str, Any]) -> bool:
    return GATEWAY_EVENT_WRITER.enqueue(payload)


def write_proxy_event(event: str, **fields: Any) -> None:
    public_fields = public_event_context(fields)
    observe_gateway_diagnostic("observe_proxy_event", event, public_fields)
    payload = proxy_telemetry.prepare_event_payload(event, public_fields, RUNTIME_CODEX_DIR)
    enqueue_gateway_event_payload(payload)


def flush_proxy_event_writer(timeout: float = 5.0) -> bool:
    return GATEWAY_EVENT_WRITER.flush(timeout).completed


def record_user_requested_shutdown() -> None:
    write_proxy_event(
        "request_cancelled",
        shutdown_outcome=USER_REQUESTED_SHUTDOWN_OUTCOME,
        status=503,
        error=USER_REQUESTED_SHUTDOWN_OUTCOME,
        detail="Gateway shutdown requested by user",
    )


def _usage_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    return None


def _usage_nested_int(usage: Mapping[str, Any], object_key: str, value_key: str) -> int | None:
    value = usage.get(object_key)
    if not isinstance(value, Mapping):
        return None
    return _usage_int(value.get(value_key))


def normalize_usage_for_event(
    usage: Mapping[str, Any] | None,
    *,
    missing_reason: str = "upstream_missing_usage",
) -> dict[str, Any]:
    if not isinstance(usage, Mapping):
        return {
            "usage_source": "missing",
            "usage_missing_reason": missing_reason,
        }

    input_tokens = _usage_int(usage.get("input_tokens"))
    if input_tokens is None:
        input_tokens = _usage_int(usage.get("prompt_tokens"))
    output_tokens = _usage_int(usage.get("output_tokens"))
    if output_tokens is None:
        output_tokens = _usage_int(usage.get("completion_tokens"))
    total_tokens = _usage_int(usage.get("total_tokens"))
    cached_input_tokens = _usage_nested_int(usage, "input_tokens_details", "cached_tokens")
    if cached_input_tokens is None:
        cached_input_tokens = _usage_nested_int(usage, "prompt_tokens_details", "cached_tokens")
    reasoning_tokens = _usage_nested_int(usage, "output_tokens_details", "reasoning_tokens")
    if reasoning_tokens is None:
        reasoning_tokens = _usage_nested_int(usage, "completion_tokens_details", "reasoning_tokens")

    fields: dict[str, Any] = {"usage_source": "upstream"}
    if input_tokens is not None:
        fields["usage_input_tokens"] = input_tokens
    if output_tokens is not None:
        fields["usage_output_tokens"] = output_tokens
    if total_tokens is not None:
        fields["usage_total_tokens"] = total_tokens
    elif input_tokens is not None and output_tokens is not None:
        fields["usage_total_tokens"] = input_tokens + output_tokens
    if cached_input_tokens is not None:
        fields["usage_cached_input_tokens"] = cached_input_tokens
    if reasoning_tokens is not None:
        fields["usage_reasoning_tokens"] = reasoning_tokens
    if len(fields) == 1:
        return {
            "usage_source": "missing",
            "usage_missing_reason": "upstream_usage_unrecognized",
        }
    return fields


def usage_from_payload(payload: Any) -> Mapping[str, Any] | None:
    if not isinstance(payload, Mapping):
        return None
    usage = payload.get("usage")
    return usage if isinstance(usage, Mapping) else None
_usage_from_payload = usage_from_payload


def _usage_from_json_body(body: bytes) -> Mapping[str, Any] | None:
    try:
        payload = json.loads(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return _usage_from_payload(payload)


def _usage_from_response_event(event: Mapping[str, Any]) -> Mapping[str, Any] | None:
    if event.get("type") != "response.completed":
        return None
    response = event.get("response")
    return _usage_from_payload(response)


def capture_usage(
    usage_capture: dict[str, Any] | None,
    usage: Mapping[str, Any] | None,
    *,
    missing_reason: str = "upstream_missing_usage",
) -> None:
    if usage_capture is None:
        return
    if usage_capture.get("usage_source") == "upstream":
        return
    usage_capture.clear()
    usage_capture.update(normalize_usage_for_event(usage, missing_reason=missing_reason))


_FAILURE_EVENT_CONTEXT_FIELD_ALLOWLIST = frozenset(
    {
        "behavior_profile",
        "client_id",
        "client_inference_source",
        "inbound_format",
        "model",
        "model_canonical",
        "model_requested",
        "provider_hint",
        "request_id",
        "request_kind",
        "route_attempt_fallback_http_statuses",
        "route_attempt_index",
        "route_attempt_endpoint_url",
        "route_attempt_model_canonical",
        "route_attempt_model_requested",
        "route_attempt_mutation_summary",
        "route_attempt_protocol",
        "route_attempt_provider_id",
        "route_attempt_upstream_model",
        "route_endpoint_url",
        "route_model_canonical",
        "route_model_requested",
        "route_provider_id",
        "route_mode",
        "route_reason",
        "route_upstream_model",
        "upstream_format",
    }
)

_FAILURE_EVENT_CONTEXT_BOUNDED_LIST_FIELDS = frozenset(
    {
        "route_attempt_fallback_http_statuses",
        "route_attempt_mutation_summary",
    }
)


def bounded_failure_event_context(context: Mapping[str, Any] | None) -> dict[str, Any]:
    """Keep only bounded routing metadata on failure-classification events."""

    bounded: dict[str, Any] = {}
    for key, value in (context or {}).items():
        if key not in _FAILURE_EVENT_CONTEXT_FIELD_ALLOWLIST:
            continue
        if isinstance(value, str):
            bounded[key] = value[:200]
        elif isinstance(value, (bool, int, float)) or value is None:
            bounded[key] = value
        elif key in _FAILURE_EVENT_CONTEXT_BOUNDED_LIST_FIELDS and isinstance(value, (list, tuple)):
            bounded[key] = [
                item[:80] if isinstance(item, str) else item
                for item in value[:32]
                if isinstance(item, (str, bool, int, float)) or item is None
            ]
    return bounded


def route_failure_event_fields(context: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return only route identity fields for direct failure events."""

    return {
        key: value
        for key, value in bounded_failure_event_context(context).items()
        if key.startswith("route_")
    }


def _usage_observed_context(
    event_context: Mapping[str, Any] | None,
    *,
    request_id: str | None,
    model: str | None,
    upstream: str,
    upstream_format: str,
    inbound_format: str,
) -> dict[str, Any]:
    context = public_event_context(event_context)
    context.update(
        {
            "request_id": request_id,
            "model": model,
            "upstream": upstream,
            "upstream_format": upstream_format,
            "inbound_format": inbound_format,
        }
    )
    return context


def write_usage_observed_event(
    context: Mapping[str, Any],
    usage: Mapping[str, Any] | None,
    *,
    missing_reason: str | None = None,
) -> None:
    if usage is None:
        if missing_reason is None:
            return
        usage_fields = normalize_usage_for_event(None, missing_reason=missing_reason)
    else:
        usage_fields = normalize_usage_for_event(usage)
    write_proxy_event(
        "usage_observed",
        request_id=context.get("request_id"),
        model=context.get("model"),
        model_requested=context.get("model_requested"),
        model_canonical=context.get("model_canonical"),
        upstream=context.get("upstream"),
        provider_id=context.get("provider_id") or context.get("upstream"),
        upstream_format=context.get("upstream_format"),
        inbound_format=context.get("inbound_format"),
        route_mode=context.get("route_mode"),
        client_id=context.get("client_id"),
        client_inference_source=context.get("client_inference_source"),
        **usage_fields,
    )


def write_usage_observed_body_event(context: Mapping[str, Any], body: bytes) -> None:
    usage = _usage_from_json_body(body)
    write_usage_observed_event(
        context,
        usage,
        missing_reason="upstream_missing_usage",
    )


OFFICIAL_PASSTHROUGH_USAGE_QUEUE: queue.Queue[tuple[dict[str, Any], bytes]] = queue.Queue(maxsize=2048)
_OFFICIAL_PASSTHROUGH_USAGE_WORKER_STARTED = False
_OFFICIAL_PASSTHROUGH_USAGE_WORKER_LOCK = threading.Lock()
USAGE_OBSERVED_QUEUE: queue.Queue[tuple[str, dict[str, Any], bytes, str | None]] = queue.Queue(maxsize=2048)
_USAGE_OBSERVED_WORKER_STARTED = False
_USAGE_OBSERVED_WORKER_LOCK = threading.Lock()


def _start_official_passthrough_usage_worker() -> None:
    global _OFFICIAL_PASSTHROUGH_USAGE_WORKER_STARTED
    if _OFFICIAL_PASSTHROUGH_USAGE_WORKER_STARTED:
        return
    with _OFFICIAL_PASSTHROUGH_USAGE_WORKER_LOCK:
        if _OFFICIAL_PASSTHROUGH_USAGE_WORKER_STARTED:
            return
        threading.Thread(
            target=_official_passthrough_usage_worker,
            name="codex-proxy-official-usage",
            daemon=True,
        ).start()
        _OFFICIAL_PASSTHROUGH_USAGE_WORKER_STARTED = True


def offer_official_passthrough_usage_line(context: Mapping[str, Any], line: bytes) -> None:
    if not line.startswith(b"data:"):
        return
    _start_official_passthrough_usage_worker()
    try:
        OFFICIAL_PASSTHROUGH_USAGE_QUEUE.put_nowait((public_event_context(context), line))
    except queue.Full:
        return


def _official_passthrough_usage_worker() -> None:
    while True:
        context, line = OFFICIAL_PASSTHROUGH_USAGE_QUEUE.get()
        try:
            payload_bytes = _sse_payload_bytes(line)
            if payload_bytes is None:
                continue
            try:
                payload = json.loads(payload_bytes.decode("utf-8-sig"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(payload, Mapping):
                continue
            usage = _usage_from_response_event(payload)
            if usage is None:
                continue
            write_usage_observed_event(context, usage)
        finally:
            OFFICIAL_PASSTHROUGH_USAGE_QUEUE.task_done()


def _start_usage_observed_worker() -> None:
    global _USAGE_OBSERVED_WORKER_STARTED
    if _USAGE_OBSERVED_WORKER_STARTED:
        return
    with _USAGE_OBSERVED_WORKER_LOCK:
        if _USAGE_OBSERVED_WORKER_STARTED:
            return
        threading.Thread(
            target=_usage_observed_worker,
            name="codex-proxy-usage-observed",
            daemon=True,
        ).start()
        _USAGE_OBSERVED_WORKER_STARTED = True


def offer_usage_observed_body(context: Mapping[str, Any], body: bytes) -> None:
    if not body:
        return
    _start_usage_observed_worker()
    try:
        USAGE_OBSERVED_QUEUE.put_nowait(("body", public_event_context(context), body, None))
    except queue.Full:
        return


def offer_usage_observed_sse_line(
    context: Mapping[str, Any],
    line: bytes,
    *,
    upstream_format: str,
) -> None:
    if not line.startswith(b"data:"):
        return
    _start_usage_observed_worker()
    try:
        USAGE_OBSERVED_QUEUE.put_nowait(("sse", public_event_context(context), line, upstream_format))
    except queue.Full:
        return


def _usage_observed_worker() -> None:
    while True:
        item_type, context, payload_bytes, upstream_format = USAGE_OBSERVED_QUEUE.get()
        try:
            usage: Mapping[str, Any] | None = None
            if item_type == "body":
                write_usage_observed_body_event(context, payload_bytes)
                continue
            if item_type == "sse":
                payload = None
                sse_payload_bytes = _sse_payload_bytes(payload_bytes)
                if sse_payload_bytes is not None and sse_payload_bytes != b"[DONE]":
                    try:
                        payload = json.loads(sse_payload_bytes.decode("utf-8-sig"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        payload = None
                if isinstance(payload, Mapping):
                    usage = (
                        _usage_from_payload(payload)
                        if upstream_format == "chat_completions"
                        else _usage_from_response_event(payload)
                    )
            write_usage_observed_event(context, usage)
        finally:
            USAGE_OBSERVED_QUEUE.task_done()


def write_adapter_event(event_context: Mapping[str, Any] | None, event: str, **fields: Any) -> None:
    if event_context is None:
        return
    payload = public_event_context(event_context)
    payload.update(fields)
    write_proxy_event(event, **payload)


def write_failure_event(
    event_context: Mapping[str, Any] | None,
    event: str,
    **fields: Any,
) -> None:
    write_adapter_event(
        bounded_failure_event_context(event_context),
        event,
        **fields,
    )
