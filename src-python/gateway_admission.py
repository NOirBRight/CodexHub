"""Request admission and user-requested Gateway shutdown."""

from __future__ import annotations

import threading
import time
from typing import Any, Callable


GATEWAY_USER_REQUESTED_SHUTDOWN_BUDGET_SECONDS = 2.0
USER_REQUESTED_SHUTDOWN_OUTCOME = "user_requested_shutdown"


class GatewayUserRequestedShutdown(RuntimeError):
    """Raised when an in-flight request is cancelled by local shutdown."""


class GatewayRequestAdmission:
    """One admitted Gateway request and the upstream transport it may cancel."""

    def __init__(self) -> None:
        self._cancelled = threading.Event()
        self._lock = threading.Lock()
        self._upstream_transport: Any | None = None

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    def attach_upstream_transport(self, transport: Any) -> None:
        with self._lock:
            self._upstream_transport = transport
            cancelled = self._cancelled.is_set()
        if cancelled:
            self._close_upstream_transport(transport)

    def cancel(self) -> None:
        self._cancelled.set()
        with self._lock:
            transport = self._upstream_transport
        if transport is not None:
            self._close_upstream_transport(transport)

    def raise_if_cancelled(self) -> None:
        if self.cancelled:
            raise GatewayUserRequestedShutdown(USER_REQUESTED_SHUTDOWN_OUTCOME)

    def wait_for_cancellation(self, timeout: float) -> bool:
        return self._cancelled.wait(timeout=max(0.0, timeout))

    @staticmethod
    def _close_upstream_transport(transport: Any) -> None:
        close = getattr(transport, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass


class GatewayShutdownController:
    """Authenticated local control-plane state for Gateway retirement."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        shutdown_budget_seconds: float = GATEWAY_USER_REQUESTED_SHUTDOWN_BUDGET_SECONDS,
    ) -> None:
        self._clock = clock
        self._shutdown_budget_seconds = max(0.0, shutdown_budget_seconds)
        self._lock = threading.Lock()
        self._admission_open = True
        self._shutdown_started_at: float | None = None
        self._active: set[GatewayRequestAdmission] = set()
        self._active_drained = threading.Event()
        self._active_drained.set()

    def admit(self) -> GatewayRequestAdmission | None:
        with self._lock:
            if not self._admission_open:
                return None
            admission = GatewayRequestAdmission()
            self._active.add(admission)
            self._active_drained.clear()
            return admission

    def complete(self, admission: GatewayRequestAdmission) -> None:
        with self._lock:
            self._active.discard(admission)
            if not self._active:
                self._active_drained.set()

    def close_admission(self) -> int:
        with self._lock:
            self._admission_open = False
            if self._shutdown_started_at is None:
                self._shutdown_started_at = self._clock()
            active = tuple(self._active)
        for admission in active:
            admission.cancel()
        return len(active)

    def remaining_shutdown_budget_seconds(self) -> float:
        with self._lock:
            started_at = self._shutdown_started_at
        if started_at is None:
            return self._shutdown_budget_seconds
        return max(0.0, self._shutdown_budget_seconds - (self._clock() - started_at))

    @property
    def shutdown_requested(self) -> bool:
        with self._lock:
            return self._shutdown_started_at is not None

    def wait_for_active_requests(self) -> bool:
        return self._active_drained.wait(timeout=self.remaining_shutdown_budget_seconds())


GATEWAY_SHUTDOWN_CONTROLLER = GatewayShutdownController()
_GATEWAY_REQUEST_ADMISSION = threading.local()


def gateway_shutdown_controller_for_handler(handler: Any) -> GatewayShutdownController:
    server = getattr(handler, "server", None)
    controller = getattr(server, "gateway_shutdown_controller", None)
    return controller if isinstance(controller, GatewayShutdownController) else GATEWAY_SHUTDOWN_CONTROLLER


def activate_gateway_request(admission: GatewayRequestAdmission) -> GatewayRequestAdmission | None:
    previous = getattr(_GATEWAY_REQUEST_ADMISSION, "current", None)
    _GATEWAY_REQUEST_ADMISSION.current = admission
    return previous if isinstance(previous, GatewayRequestAdmission) else None


def restore_gateway_request(previous: GatewayRequestAdmission | None) -> None:
    if previous is None:
        try:
            del _GATEWAY_REQUEST_ADMISSION.current
        except AttributeError:
            pass
        return
    _GATEWAY_REQUEST_ADMISSION.current = previous


def active_gateway_request() -> GatewayRequestAdmission | None:
    current = getattr(_GATEWAY_REQUEST_ADMISSION, "current", None)
    return current if isinstance(current, GatewayRequestAdmission) else None


def sleep_for_retry_with_gateway_cancellation(delay_seconds: float) -> None:
    admission = active_gateway_request()
    if admission is None:
        time.sleep(delay_seconds)
        return
    if admission.wait_for_cancellation(delay_seconds):
        admission.raise_if_cancelled()
