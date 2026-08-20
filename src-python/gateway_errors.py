from __future__ import annotations

from route_primitives import DEFAULT_MAIN_GENERATION_PRE_RESPONSE_BUDGET_SECONDS


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



def _identity_failure(
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


def _catalog_failure(
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
