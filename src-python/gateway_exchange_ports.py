"""Four real ports for Gateway exchange orchestration.

Replaces the ExchangeHooks / ExchangeFailureTypes callback bag with four
typed ports. Fixed request policy (mutation order, retry classification,
protocol fallback, elapsed limits, event payload construction) stays inside
gateway_exchange.py; these ports are the genuinely varying dependencies:

- ExchangeTransport  — upstream HTTP open/read
- DownstreamPort     — downstream relay, exposure state, typed actions
- ExecutionControl   — clock, sleep, cancellation checkpoint
- ExchangeObserver   — typed event records (request start, retries, fallback,
  empty-completed handling)

Production adapters live in gateway_exchange_adapters.py and read
owning-module attributes at call time (ADR-0007). Tests use scripted
adapters.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from enum import Enum, auto
from typing import Any, Protocol

from gateway_interfaces import UpstreamResponseLike


class DownstreamAction(Enum):
    """Typed downstream actions the exchange may request."""

    ATTACH_UPSTREAM = auto()
    SET_UPSTREAM_FORMAT = auto()
    EMIT_RETRY_NOTICE = auto()
    FINISH_FAILURE = auto()
    HANDLE_EMPTY_COMPLETED = auto()


@dataclass(frozen=True)
class DownstreamState:
    """Read-only downstream exposure facts."""

    exposed: bool
    sse_started: bool


@dataclass(frozen=True)
class ExchangeEvent:
    """One typed event recorded through ExchangeObserver."""

    kind: str
    fields: Mapping[str, Any]


class ExchangeTransport(Protocol):
    """Upstream transport: open a response for one attempt."""

    def open(
        self,
        opening: Any,  # OpenExchangeRequest (defined in gateway_exchange)
    ) -> AbstractContextManager[UpstreamResponseLike]: ...


class DownstreamPort(Protocol):
    """Downstream relay and exposure state."""

    def relay(
        self,
        response: UpstreamResponseLike,
        relay_request: Any,  # RelayExchangeRequest
    ) -> int: ...

    def state(self) -> DownstreamState: ...

    def perform(self, action: DownstreamAction, **payload: Any) -> Any:
        """Apply one typed downstream action (attach, format, notice, ...)."""
        ...


class ExecutionControl(Protocol):
    """Clock, sleeping, and cancellation checkpoint."""

    def now(self) -> float: ...

    def wait(self, seconds: int | float) -> None: ...

    def checkpoint(self) -> None:
        """Raise if the gateway is shutting down / request cancelled."""
        ...


class ExchangeObserver(Protocol):
    """Typed event sink for exchange lifecycle records."""

    def record(self, event: ExchangeEvent) -> None: ...


@dataclass(frozen=True, slots=True)
class ExchangePorts:
    """The four real ports used by execute_exchange.

    Deliberately small: exactly the varying dependencies. Everything else
    (mutation order, retry policy arithmetic, fallback decisions, event
    payload construction) is fixed policy inside gateway_exchange.py.
    """

    transport: ExchangeTransport
    downstream: DownstreamPort
    control: ExecutionControl
    observer: ExchangeObserver

