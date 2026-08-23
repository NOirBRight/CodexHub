from __future__ import annotations

import json
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from http.client import IncompleteRead
from typing import Any
from urllib.error import HTTPError, URLError

from gateway_admission import USER_REQUESTED_SHUTDOWN_OUTCOME
from route_primitives import (
    DEFAULT_MAIN_GENERATION_PRE_RESPONSE_BUDGET_SECONDS,
    RETRY_FAILURE_PERMANENT,
    RETRY_FAILURE_QUICK_TRANSIENT,
)


class UpstreamStreamIncompleteError(RuntimeError):
    """Raised when an upstream stream ends without a terminal event."""


class ImageProxyError(Exception):
    """Raised when a Vision Proxy request cannot be prepared safely."""


class ModelIdentityResolutionError(ValueError):
    """Raised when an exact provider/model pair cannot be proven safe.

    ``classification`` is deliberately a small, non-secret vocabulary used by
    diagnostics and callers.  ``catalog_inconsistency`` means the published
    snapshot itself is contradictory or ambiguous; ``local_resolution_failure``
    means the requested identity is absent, internal, stale, or unsupported.
    """

    CLASSIFICATIONS = frozenset({"catalog_inconsistency", "local_resolution_failure"})

    def __init__(
        self,
        message: str,
        *,
        classification: str,
        reason: str,
        provider_id: str | None = None,
        model_slug: str | None = None,
    ) -> None:
        if classification not in self.CLASSIFICATIONS:
            raise ValueError(f"unsupported model identity classification: {classification}")
        self.classification = classification
        self.reason = reason
        self.provider_id = provider_id
        self.model_slug = model_slug
        super().__init__(message)


class UpstreamProtocolTranslationError(ValueError):
    """Marks an unsupported upstream wire shape for the downstream error mapper."""

    def __init__(
        self,
        cause: BaseException,
        *,
        classification: str | None = None,
    ) -> None:
        self.cause = cause
        self.classification = classification or getattr(cause, "code", None)
        super().__init__(str(cause))


class UnsupportedRouteProtocolError(ValueError):
    """Raised when a configured route has no executable protocol attempt."""


class UnqualifiedRouteProtocolError(ValueError):
    """Raised when a configured route protocol has no qualified identity."""


class CompactEmptyResponseError(RuntimeError):
    """Raised when a compact request succeeds with no summary text."""

    def __init__(self, upstream_name: str):
        self.upstream_name = upstream_name
        super().__init__("Upstream returned an empty compact summary.")


class LifecycleEmptyFinalResponseError(RuntimeError):
    """Raised when a completed subagent lifecycle ends with no visible final text."""

    def __init__(self, upstream_name: str):
        self.upstream_name = upstream_name
        super().__init__("Upstream returned an empty final response after completed subagent lifecycle.")


class LifecycleFinalFormatResponseError(RuntimeError):
    """Raised when a completed subagent lifecycle emits a final report with extra prose."""

    def __init__(self, upstream_name: str):
        self.upstream_name = upstream_name
        super().__init__("Upstream returned a final response that did not start with the requested report format.")




class UpstreamStreamIdleTimeoutError(TimeoutError):
    """Raised when an upstream SSE stream stalls before completion."""

    def __init__(self, timeout_seconds: float, phase: str = "model_event"):
        self.timeout_seconds = timeout_seconds
        self.phase = phase
        if phase == "transport":
            detail = "without upstream bytes"
        elif phase == "model_event":
            detail = "without a valid model event"
        else:
            detail = "before output started" if phase == "pre_output" else "after output started"
        super().__init__(f"Upstream stream stalled for {timeout_seconds:g} seconds {detail}.")


class GatewayPreResponseBudgetExhausted(TimeoutError):
    """Raised when main generation cannot reach a usable response within its shared budget."""

    def __init__(
        self,
        *,
        phase: str = "pre_response",
        attempt: int | None = None,
        budget_seconds: float = DEFAULT_MAIN_GENERATION_PRE_RESPONSE_BUDGET_SECONDS,
    ):
        self.phase = phase
        self.attempt = attempt
        self.budget_seconds = budget_seconds
        super().__init__("Gateway pre-response budget exhausted before a usable upstream response.")



def identity_failure(
    message: str,
    *,
    reason: str,
    provider_id: str | None = None,
    model_slug: str | None = None,
) -> ModelIdentityResolutionError:
    return ModelIdentityResolutionError(
        message,
        classification="local_resolution_failure",
        reason=reason,
        provider_id=provider_id,
        model_slug=model_slug,
    )
_identity_failure = identity_failure


def catalog_failure(
    message: str,
    *,
    reason: str,
    provider_id: str | None = None,
    model_slug: str | None = None,
) -> ModelIdentityResolutionError:
    return ModelIdentityResolutionError(
        message,
        classification="catalog_inconsistency",
        reason=reason,
        provider_id=provider_id,
        model_slug=model_slug,
    )
_catalog_failure = catalog_failure


def redact_identity_in_text(text: str, identity: str | None) -> str:
    if identity and identity in text:
        return text.replace(identity, "[retry_identity_redacted]")
    return text
_redact_identity_in_text = redact_identity_in_text


def safe_upstream_error_detail(exc: BaseException, *, redact_identity: str | None = None) -> str:
    reason = getattr(exc, "reason", None)
    source = reason if reason is not None else exc
    detail = f"{type(source).__name__}: {source}"
    detail = detail.replace("\r", " ").replace("\n", " ")
    if "Bearer " in detail:
        detail = detail.split("Bearer ", 1)[0] + "Bearer [redacted]"
    if redact_identity:
        detail = detail.replace(redact_identity, "[retry_identity_redacted]")
    return detail[:300]


def _safe_error_identity(value: Any) -> str | None:
    """Keep credential-like or malformed identities out of error payloads."""

    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate or len(candidate) > 128 or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]*", candidate):
        return None
    lowered = candidate.lower()
    if any(marker in lowered for marker in ("bearer", "secret", "token", "password", "api_key", "api-key", "authorization", "cookie")):
        return None
    return candidate


def _exception_failure_class(exc: BaseException) -> str | None:
    """Classify via the transport module without creating a load-time cycle."""

    import gateway_transport

    return gateway_transport._upstream_failure_class(exc)


@dataclass(frozen=True)
class DownstreamErrorSpec:
    inbound_format: str
    upstream_name: str
    status: int = 502
    exc: BaseException | None = None
    error: str | None = None
    detail: str | None = None
    error_type: str = "upstream_error"
    preserve_explicit_error: bool = False
    redact_identity: str | None = None


def _typed_error_code(
    *,
    error_type: str,
    error_code: str,
    exc: BaseException | None,
    status: int | None,
) -> str:
    if isinstance(exc, ModelIdentityResolutionError):
        return (
            "catalog.inconsistency"
            if exc.classification == "catalog_inconsistency"
            else "gateway.model_resolution"
        )
    if error_type == "gateway_auth_error":
        return "gateway.auth"
    if error_type == "gateway_pre_response_budget_exhausted":
        return "gateway.pre_response_budget_exhausted"
    if error_type == USER_REQUESTED_SHUTDOWN_OUTCOME:
        return "gateway.user_requested_shutdown"
    if error_type in {"invalid_request_error", "validation_error"}:
        return "provider.request"
    if error_code in {"UpstreamProtocolError", "upstream_stream_incomplete", "upstream_stream_idle_timeout"}:
        return "upstream.protocol"
    if status in {401, 403}:
        return "provider.auth"
    if status == 429:
        return "provider.rate_limit"
    if isinstance(exc, HTTPError):
        return "upstream.http"
    if isinstance(exc, (IncompleteRead, OSError, TimeoutError, URLError)):
        return "upstream.transport"
    if status is not None and status >= 500:
        return "upstream.http"
    return "upstream.error"


def _codexhub_error_payload(
    *,
    source: str,
    message: str,
    status: int | None = None,
    exc: BaseException | None = None,
    error: str | None = None,
    error_type: str = "upstream_error",
    failure_class: str | None = None,
) -> dict[str, Any]:
    error_code = error or (type(exc).__name__ if exc is not None else "UpstreamError")
    resolved_failure_class = failure_class
    if error_code == "gateway_pre_response_budget_exhausted":
        resolved_failure_class = RETRY_FAILURE_PERMANENT
    if resolved_failure_class is None and exc is not None:
        resolved_failure_class = _exception_failure_class(exc)
    if resolved_failure_class is None and (
        error_type in {"invalid_request_error", "validation_error"}
        or (status is not None and 400 <= status < 500 and status != 429)
    ):
        resolved_failure_class = RETRY_FAILURE_PERMANENT
    if resolved_failure_class is None and (status == 429 or (status is not None and status >= 500)):
        resolved_failure_class = RETRY_FAILURE_QUICK_TRANSIENT
    if resolved_failure_class is None:
        resolved_failure_class = RETRY_FAILURE_PERMANENT
    details: dict[str, Any] = {
        "error": error_code,
        "type": error_type,
    }
    if isinstance(exc, ModelIdentityResolutionError):
        details["classification"] = exc.classification
        details["reason"] = exc.reason
        safe_provider_id = _safe_error_identity(exc.provider_id)
        safe_model_slug = _safe_error_identity(exc.model_slug)
        if safe_provider_id:
            details["provider_id"] = safe_provider_id
        if safe_model_slug:
            details["model_slug"] = safe_model_slug
    if status is not None:
        details["status"] = status
    if resolved_failure_class is not None:
        details["failure_class"] = resolved_failure_class
    return {
        "code": _typed_error_code(
            error_type=error_type,
            error_code=error_code,
            exc=exc,
            status=status,
        ),
        "message": message,
        "source": source,
        "retryable": resolved_failure_class != RETRY_FAILURE_PERMANENT,
        "details": details,
    }


def user_requested_shutdown_payload(inbound_format: str) -> dict[str, Any]:
    message = "Gateway stopped because the user requested shutdown."
    codexhub_error = _codexhub_error_payload(
        source="gateway",
        message=message,
        status=503,
        error=USER_REQUESTED_SHUTDOWN_OUTCOME,
        error_type=USER_REQUESTED_SHUTDOWN_OUTCOME,
        failure_class=RETRY_FAILURE_PERMANENT,
    )
    if inbound_format == "chat_completions":
        return {
            "error": {
                "message": message,
                "type": USER_REQUESTED_SHUTDOWN_OUTCOME,
                "code": USER_REQUESTED_SHUTDOWN_OUTCOME,
                "status": 503,
            },
            "codexhub_error": codexhub_error,
        }
    return {
        "type": USER_REQUESTED_SHUTDOWN_OUTCOME,
        "error": USER_REQUESTED_SHUTDOWN_OUTCOME,
        "detail": message,
        "codexhub_error": codexhub_error,
    }


def local_gateway_auth_error_payload() -> dict[str, Any]:
    message = "missing or invalid local Gateway client key"
    return {
        "error": "unauthorized",
        "codexhub_error": _codexhub_error_payload(
            source="gateway",
            message=message,
            status=401,
            error="UnauthorizedLocalClient",
            error_type="gateway_auth_error",
            failure_class=RETRY_FAILURE_PERMANENT,
        ),
    }
_local_gateway_auth_error_payload = local_gateway_auth_error_payload


def _downstream_stream_error_payload(
    *,
    upstream_name: str,
    status: int = 502,
    exc: BaseException | None = None,
    error: str | None = None,
    detail: str | None = None,
    error_type: str = "upstream_stream_error",
    redact_identity: str | None = None,
) -> dict[str, Any]:
    error_code = error or (type(exc).__name__ if exc is not None else "UpstreamStreamError")
    if detail is not None:
        error_detail = _redact_identity_in_text(detail, redact_identity)
    elif exc is not None:
        error_detail = safe_upstream_error_detail(exc, redact_identity=redact_identity)
    else:
        error_detail = ""
    failure_class = _exception_failure_class(exc) if exc is not None else None
    if failure_class is None and error_code in {
        "upstream_stream_idle_timeout",
        "upstream_stream_incomplete",
        "UpstreamStreamError",
        "UpstreamProtocolError",
    }:
        failure_class = RETRY_FAILURE_QUICK_TRANSIENT
    payload = {
        "type": error_type,
        "status": status,
        "upstream": upstream_name,
        "error": error_code,
        "detail": error_detail,
        "retry_owner": "client",
    }
    if failure_class is not None:
        payload["failure_class"] = failure_class
        payload["retryable"] = failure_class != RETRY_FAILURE_PERMANENT
    payload["codexhub_error"] = _codexhub_error_payload(
        source=upstream_name,
        message=error_detail or error_code,
        status=status,
        exc=exc,
        error=error_code,
        error_type=error_type,
        failure_class=failure_class,
    )
    return payload


def downstream_sse_error_payload_for_inbound_format(error: DownstreamErrorSpec) -> dict[str, Any]:
    error_type = error.error_type
    if error_type == "upstream_error":
        error_type = "upstream_stream_error"
    if error.inbound_format == "chat_completions":
        return _chat_completion_error_payload(
            upstream_name=error.upstream_name,
            status=error.status,
            exc=error.exc,
            error=error.error,
            detail=error.detail,
            error_type=error_type,
            redact_identity=error.redact_identity,
        )
    if error.exc is not None:
        if not error.preserve_explicit_error:
            return _downstream_stream_error_payload(
                upstream_name=error.upstream_name,
                exc=error.exc,
                redact_identity=error.redact_identity,
            )
        return _downstream_stream_error_payload(
            upstream_name=error.upstream_name,
            status=error.status,
            exc=error.exc,
            error=error.error,
            detail=error.detail,
            error_type=error_type,
            redact_identity=error.redact_identity,
        )
    return _downstream_stream_error_payload(
        upstream_name=error.upstream_name,
        status=error.status,
        error=error.error or "UpstreamProtocolError",
        detail=error.detail or error.error or "upstream stream failed",
        error_type=error_type,
        redact_identity=error.redact_identity,
    )
_downstream_sse_error_payload_for_inbound_format = downstream_sse_error_payload_for_inbound_format


def responses_failed_event_for_stream_error(
    *,
    upstream_name: str,
    model: str | None,
    status: int,
    exc: BaseException | None = None,
    error: str | None = None,
    detail: str | None = None,
    response_id: str | None = None,
    redact_identity: str | None = None,
) -> dict[str, Any]:
    stream_error = _downstream_stream_error_payload(
        upstream_name=upstream_name,
        status=status,
        exc=exc,
        error=error,
        detail=detail,
        redact_identity=redact_identity,
    )
    error_payload: dict[str, Any] = {
        "code": stream_error.get("error") or "UpstreamStreamError",
        "message": stream_error.get("detail") or stream_error.get("error") or "Upstream stream error",
        "type": stream_error.get("type") or "upstream_stream_error",
        "status": status,
        "upstream": upstream_name,
    }
    if "failure_class" in stream_error:
        error_payload["failure_class"] = stream_error["failure_class"]
    if "retryable" in stream_error:
        error_payload["retryable"] = stream_error["retryable"]
    return {
        "type": "response.failed",
        "response": {
            "id": response_id if isinstance(response_id, str) and response_id else f"resp_{uuid.uuid4().hex[:12]}",
            "object": "response",
            "status": "failed",
            "model": model,
            "output": [],
            "error": error_payload,
        },
    }
_responses_failed_event_for_stream_error = responses_failed_event_for_stream_error


def _chat_completion_error_payload(
    *,
    upstream_name: str,
    status: int = 502,
    exc: BaseException | None = None,
    error: str | None = None,
    detail: str | None = None,
    error_type: str = "upstream_error",
    redact_identity: str | None = None,
) -> dict[str, Any]:
    error_code = error or (type(exc).__name__ if exc is not None else "UpstreamError")
    if detail is not None:
        error_detail = _redact_identity_in_text(detail, redact_identity)
    elif exc is not None:
        error_detail = safe_upstream_error_detail(exc, redact_identity=redact_identity)
    else:
        error_detail = ""
    message = error_detail or error_code
    return {
        "error": {
            "message": message,
            "type": error_type,
            "code": error_code,
            "status": status,
            "upstream": upstream_name,
        },
        "codexhub_error": _codexhub_error_payload(
            source=upstream_name,
            message=message,
            status=status,
            exc=exc,
            error=error_code,
            error_type=error_type,
        ),
    }


def _with_codexhub_http_error(
    body: bytes,
    *,
    upstream_name: str,
    status: int,
    exc: BaseException | None = None,
) -> bytes:
    try:
        payload = json.loads(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return body
    if not isinstance(payload, dict) or "codexhub_error" in payload:
        return body
    upstream_error = payload.get("error")
    if isinstance(upstream_error, Mapping):
        message = str(upstream_error.get("message") or upstream_error.get("detail") or "HTTPError")
        error_type = str(upstream_error.get("type") or "upstream_error")
    else:
        message = str(upstream_error or payload.get("detail") or "HTTPError")
        error_type = "upstream_error"
    payload["codexhub_error"] = _codexhub_error_payload(
        source=upstream_name,
        message=message,
        status=status,
        exc=exc,
        error="HTTPError",
        error_type=error_type,
    )
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


def downstream_json_error_payload(error: DownstreamErrorSpec) -> dict[str, Any]:
    return _json_error_payload_for_inbound_format(
        inbound_format=error.inbound_format,
        upstream_name=error.upstream_name,
        status=error.status,
        exc=error.exc,
        error=error.error,
        detail=error.detail,
        error_type=error.error_type,
        redact_identity=error.redact_identity,
    )
_downstream_json_error_payload = downstream_json_error_payload


def _json_error_payload_for_inbound_format(
    *,
    inbound_format: str,
    upstream_name: str,
    status: int,
    exc: BaseException | None = None,
    error: str | None = None,
    detail: str | None = None,
    error_type: str = "upstream_error",
    redact_identity: str | None = None,
) -> dict[str, Any]:
    if inbound_format == "chat_completions":
        return _chat_completion_error_payload(
            upstream_name=upstream_name,
            status=status,
            exc=exc,
            error=error,
            detail=detail,
            error_type=error_type,
            redact_identity=redact_identity,
        )
    error_code = error or (type(exc).__name__ if exc is not None else "UpstreamError")
    if detail is not None:
        error_detail = _redact_identity_in_text(detail, redact_identity)
    elif exc is not None:
        error_detail = safe_upstream_error_detail(exc, redact_identity=redact_identity)
    else:
        error_detail = ""
    payload: dict[str, Any] = {"error": error_detail or error_code}
    if error_detail:
        payload["detail"] = error_detail
    payload["codexhub_error"] = _codexhub_error_payload(
        source=upstream_name,
        message=error_detail or error_code,
        status=status,
        exc=exc,
        error=error_code,
        error_type=error_type,
    )
    return payload


# Public aliases matching the former facade surface.
codexhub_error_payload = _codexhub_error_payload
downstream_json_error_payload = _downstream_json_error_payload
downstream_sse_error_payload_for_inbound_format = _downstream_sse_error_payload_for_inbound_format
