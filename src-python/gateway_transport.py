"""Gateway upstream transport: official adapter, open/retry, headers, and stream lifecycle.

This module owns the Official urllib3 adapter, URL/header/auth materialization,
HTTP error classification, Retry-After calculation, pre-response retry budgets,
upstream open/retry, and the upstream SSE reader lifecycle. It is deliberately
independent of the Gateway HTTP handler and SSE framing modules. The facade
supplies live-patchable callables through GatewayTransport.

Relay state stays in gateway_relay. Protocol codecs stay in
protocol_translation.
"""

from __future__ import annotations

import io
import json
import logging
import math
import os
import queue
import socket
import ssl
import sys
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import timezone
from email.utils import parsedate_to_datetime
from http.client import IncompleteRead
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, getproxies, proxy_bypass, urlopen

try:
    from urllib.request import getproxies_registry
except ImportError:  # pragma: no cover - Windows-only urllib helper.
    getproxies_registry = None

VENDOR_DIR = Path(__file__).resolve().parent / "vendor"
VENDORED_URLLIB3_WHEEL = VENDOR_DIR / "urllib3-2.7.0-py3-none-any.whl"
if not VENDORED_URLLIB3_WHEEL.is_file():
    raise RuntimeError(f"missing pinned Gateway transport dependency: {VENDORED_URLLIB3_WHEEL}")
sys.path.insert(0, str(VENDORED_URLLIB3_WHEEL))

import urllib3

import gateway_admission
import gateway_events
from gateway_admission import sleep_for_retry_with_gateway_cancellation
from gateway_errors import (
    CompactEmptyResponseError,
    GatewayPreResponseBudgetExhausted,
    UpstreamStreamIdleTimeoutError,
    UpstreamStreamIncompleteError,
    safe_upstream_error_detail,
)
from gateway_settings import (
    request_kind_retry_attempts_configured as _request_kind_retry_attempts_configured,
    upstream_retry_attempts as _upstream_retry_attempts,
    gateway_auto_retry_max_attempts,
    gateway_capacity_retry_elapsed_limit_seconds,
    gateway_retry_delay_seconds,
)
from route_primitives import UPSTREAM_USER_AGENT
from route_primitives import authentication_strategy as _authentication_strategy
from route_primitives import (
    BEHAVIOR_OFFICIAL_CODEX_APP_HTTP_PASSTHROUGH,
    CAPACITY_RETRY_FAILURE_CLASSES,
    PERMANENT_HTTP_ERROR_STATUSES,
    PERMANENT_UPSTREAM_AUTH_NEEDLES,
    PERMANENT_UPSTREAM_ERROR_NEEDLES,
    PERMANENT_UPSTREAM_ERROR_VALUES,
    PROVIDER_OVERLOADED_ERROR_NEEDLES,
    PROVIDER_OVERLOADED_ERROR_VALUES,
    PROVIDER_THROTTLE_ERROR_NEEDLES,
    PROVIDER_THROTTLE_ERROR_VALUES,
    RETRY_CONSERVATIVE_PRE_OUTPUT,
    RETRY_FAILURE_PERMANENT,
    RETRY_FAILURE_PROVIDER_OVERLOADED,
    RETRY_FAILURE_PROVIDER_THROTTLE,
    RETRY_FAILURE_QUICK_TRANSIENT,
    RETRY_GATEWAY_FULL,
    RETRY_REQUEST_MAIN_GENERATION,
    RETRY_SAFETY_GUARANTEED_IDEMPOTENT,
    RETRY_SAFETY_SAFE_PREWRITE,
    RETRY_SAFETY_SUPPRESSED_POST_EXPOSURE,
    RETRY_SAFETY_SUPPRESSED_POST_WRITE,
    RETRY_SAFETY_UNKNOWN,
    TRANSIENT_HTTP_RETRY_STATUSES,
    AuthenticationStrategy,
    MutationPolicy,
    OperationalAuthentication,
    TransportPolicy,
)
from subscription_credential import credential_for, register_builtin_adapters

logger = logging.getLogger("gateway_transport")
register_builtin_adapters()

HOP_BY_HOP_REQUEST_HEADERS = {
    "connection",
    "content-length",
    "host",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}

OFFICIAL_ALIAS_PREFIX = "openai/"
OFFICIAL_RESPONSES_LITE_UNSUPPORTED_MODELS = {"gpt-5.4", "gpt-5.4-mini"}
_UNSET_PROXY = object()


class _UnboundStreamInterruptedError(RuntimeError):
    def __init__(self, cause: BaseException | None = None):
        self.cause = cause if isinstance(cause, BaseException) else RuntimeError("unbound")
        super().__init__(str(self.cause))


class _UnboundStreamErrorEvent(RuntimeError):
    def __init__(self, payload: Mapping[str, Any] | None = None):
        self.payload = dict(payload or {})
        super().__init__("unbound stream error event")


_STREAM_INTERRUPTED_ERROR: type[BaseException] = _UnboundStreamInterruptedError
_STREAM_ERROR_EVENT: type[BaseException] = _UnboundStreamErrorEvent


def bind_transport_failure_types(
    *,
    stream_interrupted_error: type[BaseException],
    stream_error_event: type[BaseException],
) -> None:
    """Bind facade stream-error types used by HTTP failure classification."""

    global _STREAM_INTERRUPTED_ERROR, _STREAM_ERROR_EVENT
    _STREAM_INTERRUPTED_ERROR = stream_interrupted_error
    _STREAM_ERROR_EVENT = stream_error_event


def _noop(*_args: Any, **_kwargs: Any) -> None:
    return None


def _diagnostic_connection_disposition_value(response: Any) -> str:
    try:
        disposition = getattr(response, "connection_disposition", "unobserved")
    except Exception:
        return "unobserved"
    return disposition if disposition in {"new", "reused"} else "unobserved"


def _diagnostic_error_connection_disposition_value(exc: BaseException) -> str:
    try:
        disposition = getattr(exc, "_codexhub_diagnostic_connection_disposition", "unobserved")
    except Exception:
        return "unobserved"
    return disposition if disposition in {"new", "reused"} else "unobserved"


def _response_metadata(response: Any) -> tuple[Any, Any]:
    try:
        status = getattr(response, "status", None)
        if not status:
            status = getattr(response, "code", None)
    except Exception:
        status = None
    try:
        headers = getattr(response, "headers", None)
    except Exception:
        headers = None
    return status, headers


def _diagnostic_phase_name(failure_phase: str | None) -> str | None:
    return {
        "dns": "upstream_dns",
        "tcp_connect": "upstream_tcp",
        "tls": "upstream_tls",
        "request_write": "upstream_request_write",
    }.get(failure_phase)


class RetryExecutionPlanLike(Protocol):
    request_kind: str
    policy: Any
    retry_http_errors: bool
    request_timeout_seconds: int | float
    base_open_attempts: int
    open_attempt_budget: Any

    def new_open_attempt_budget(self) -> dict[str, int]: ...
    def open_attempts_for_failure_class(self, failure_class: str) -> int: ...
    def retry_delay_seconds(self, attempt: int, failure_class: str) -> int: ...
    def capacity_elapsed_limit_allows(self, started_at: float, delay_seconds: int | float) -> bool: ...


class DiagnosticRecorder(Protocol):
    def observe_upstream_phase(self, request_key: str | None, **fields: Any) -> None: ...
    def observe_upstream_attempt(self, request_key: str | None, **fields: Any) -> None: ...
    def observe_upstream_headers(self, request_key: str | None, **fields: Any) -> None: ...


@dataclass(frozen=True)
class TransportFacts:
    """Immutable transport constants and exception types."""

    hop_by_hop_request_headers: frozenset[str] = field(
        default_factory=lambda: frozenset(HOP_BY_HOP_REQUEST_HEADERS)
    )
    official_alias_prefix: str = OFFICIAL_ALIAS_PREFIX
    official_responses_lite_unsupported_models: frozenset[str] = field(
        default_factory=lambda: frozenset(OFFICIAL_RESPONSES_LITE_UNSUPPORTED_MODELS)
    )
    official_passthrough_behavior: str = BEHAVIOR_OFFICIAL_CODEX_APP_HTTP_PASSTHROUGH
    official_upstream_name: str = "official"
    suppressed_retry_safety_classes: frozenset[str] = field(default_factory=frozenset)
    downstream_closed_before_retry_error: type[BaseException] = RuntimeError


@dataclass
class GatewayTransport:
    """Typed upstream-transport seam.

    Callers get open, header/auth materialization, failure classification,
    Retry-After, and pre-response budget behavior behind this adapter.
    The facade constructs one per call so monkeypatches of official open,
    stdlib urlopen, tokens, sleep, and pool helpers stay live.
    """

    facts: TransportFacts = field(default_factory=TransportFacts)
    official_open: Callable[..., Any] | None = None
    standard_open: Callable[..., Any] | None = None
    open_once_hook: Callable[..., Any] | None = None
    sleep: Callable[[float], None] | None = None
    active_request: Callable[[], Any | None] | None = None
    access_token: Callable[[], str] | None = None
    account_id: Callable[[], str | None] | None = None
    ollama_api_key: Callable[[], str | None] | None = None
    new_id: Callable[[], str] = field(default_factory=lambda: (lambda: str(uuid.uuid4())))
    diagnostic_recorder: DiagnosticRecorder | None = None
    diagnostic_context_value: Callable[..., Any] | None = None
    diagnostic_connection_disposition: Callable[..., str] | None = None
    diagnostic_error_connection_disposition: Callable[..., str] | None = None
    diagnostic_response_metadata: Callable[..., tuple[Any, Any]] | None = None
    diagnostic_transport_phase: Callable[..., str | None] | None = None
    emit_retry: Callable[..., None] = _noop
    emit_retry_suppressed: Callable[..., None] = _noop
    retry_delay_seconds: Callable[..., int] | None = None
    failure_class_hook: Callable[[BaseException], str] | None = None
    retry_after_hook: Callable[[BaseException | None], int | None] | None = None
    retry_attempts_for_failure_class: Callable[..., int] | None = None
    capacity_elapsed_allows: Callable[..., bool] | None = None
    retry_safety_class: Callable[..., str] | None = None
    retry_safety_failure_phase: Callable[..., str | None] | None = None
    failure_phase: Callable[..., str | None] | None = None
    model_access_path: Callable[..., tuple[str, ...]] | None = None
    model_access_path_idempotent: Callable[..., bool] | None = None
    ensure_retry_identity: Callable[..., Any] = _noop
    retry_identity_from_context: Callable[..., str | None] | None = None
    downstream_retry_payload: Callable[..., Any] | None = None
    get_header: Callable[..., str | None] | None = None
    header_items: Callable[..., list[tuple[str, str]]] | None = None
    upstream_retry_attempts: Callable[[str], int] | None = None
    getproxies: Callable[[], Mapping[str, str]] | None = None
    getproxies_registry: Callable[[], Mapping[str, str]] | None = None
    proxy_bypass: Callable[[str], bool] | None = None
    platform: str | None = None
    official_pools: dict[str, Any] | None = None
    official_pools_lock: threading.Lock | None = None
    pool_manager_hook: Callable[[str], Any] | None = None
    proxy_url_hook: Callable[[str], str | None] | None = None
    endpoint_url_hook: Callable[[Mapping[str, Any], str], str] | None = None

    def _observe(self, callback: Callable[..., None], request_key: str | None, **fields: Any) -> None:
        recorder = self.diagnostic_recorder
        if recorder is not None:
            try:
                callback(recorder, request_key, **fields)
            except Exception:
                pass

    def observe_upstream_phase(self, request_key: str | None, **fields: Any) -> None:
        self._observe(lambda recorder, key, **values: recorder.observe_upstream_phase(key, **values), request_key, **fields)

    def observe_upstream_attempt(self, request_key: str | None, **fields: Any) -> None:
        self._observe(lambda recorder, key, **values: recorder.observe_upstream_attempt(key, **values), request_key, **fields)

    def observe_upstream_headers(self, request_key: str | None, **fields: Any) -> None:
        self._observe(lambda recorder, key, **values: recorder.observe_upstream_headers(key, **values), request_key, **fields)

    def current_admission(self) -> Any | None:
        if self.active_request is None:
            return None
        return self.active_request()

    def _get_header(self, headers: Mapping[str, str] | Any, name: str) -> str | None:
        return (self.get_header or _get_header)(headers, name)

    def _header_items(self, headers: Mapping[str, str] | Any) -> list[tuple[str, str]]:
        return (self.header_items or _header_items)(headers)

    def resolved_access_token(self) -> str:
        if self.access_token is not None:
            return self.access_token()
        return _default_access_token()

    def resolved_account_id(self) -> str | None:
        if self.account_id is not None:
            return self.account_id()
        return _default_account_id()

    def resolved_ollama_api_key(self) -> str | None:
        if self.ollama_api_key is not None:
            return self.ollama_api_key()
        return os.environ.get("OLLAMA_API_KEY")

    def _retry_attempts(self, **kwargs: Any) -> int:
        fn = self.retry_attempts_for_failure_class or _retry_attempts_for_failure_class
        return fn(**kwargs)

    def _capacity_allows(self, started_at: float, delay_seconds: int) -> bool:
        fn = self.capacity_elapsed_allows or _capacity_retry_elapsed_limit_allows
        return fn(started_at, delay_seconds)

    def _retry_safety(self, *args: Any, **kwargs: Any) -> str:
        fn = self.retry_safety_class
        if fn is None:
            return RETRY_SAFETY_SAFE_PREWRITE
        return fn(*args, **kwargs)

    def _retry_safety_phase(self, exc: BaseException | None) -> str | None:
        fn = self.retry_safety_failure_phase
        if fn is None:
            return None
        return fn(exc)

    def _failure_phase(self, exc: BaseException | None) -> str | None:
        fn = self.failure_phase or transport_failure_phase
        return fn(exc)

    def _model_access_path(
        self,
        event_context: Mapping[str, Any] | None,
        upstream_name: str,
        upstream_format: str,
    ) -> tuple[str, ...]:
        fn = self.model_access_path
        if fn is None:
            model = ""
            behavior_profile = ""
            inbound_format = ""
            if isinstance(event_context, Mapping):
                model = str(event_context.get("model") or "")
                behavior_profile = str(event_context.get("behavior_profile") or "")
                inbound_format = str(event_context.get("_caller_wire_format") or "")
            return (upstream_name, model, behavior_profile, upstream_format, inbound_format)
        return fn(event_context, upstream_name, upstream_format)

    def _path_idempotent(self, path: tuple[str, ...]) -> bool:
        fn = self.model_access_path_idempotent
        if fn is None:
            return False
        return fn(path)

    def _retry_identity(self, event_context: Mapping[str, Any] | None) -> str | None:
        fn = self.retry_identity_from_context
        if fn is None:
            return None
        return fn(event_context)

    def _retry_payload(self, **fields: Any) -> Any:
        fn = self.downstream_retry_payload
        if fn is None:
            return fields
        return fn(**fields)

    def _observe_context_value(self, event_context: Mapping[str, Any] | None, key: str) -> Any:
        fn = self.diagnostic_context_value
        if fn is None:
            if event_context is None:
                return None
            try:
                return event_context.get(key)
            except Exception:
                return None
        return fn(event_context, key)

    def _observe_connection_disposition(self, response: Any) -> str:
        fn = self.diagnostic_connection_disposition
        if fn is None:
            return _diagnostic_connection_disposition_value(response)
        return fn(response)

    def _observe_error_disposition(self, exc: BaseException) -> str:
        fn = self.diagnostic_error_connection_disposition
        if fn is None:
            return _diagnostic_error_connection_disposition_value(exc)
        return fn(exc)

    def _observe_response_metadata(self, response: Any) -> tuple[Any, Any]:
        fn = self.diagnostic_response_metadata
        if fn is None:
            return _response_metadata(response)
        return fn(response)

    def _observe_transport_phase(self, failure_phase: str | None) -> str | None:
        fn = self.diagnostic_transport_phase
        if fn is None:
            return _diagnostic_phase_name(failure_phase)
        return fn(failure_phase)

    def open_once(
        self,
        request: Request,
        *,
        upstream_name: str,
        timeout: int | float,
        transport_policy: TransportPolicy | None = None,
    ) -> Any:
        selected_transport = transport_policy or (
            TransportPolicy.OFFICIAL_KEEPALIVE
            if upstream_name == self.facts.official_upstream_name
            else TransportPolicy.STANDARD
        )
        if selected_transport == TransportPolicy.OFFICIAL_KEEPALIVE:
            opener = self.official_open or self.official_urlopen
            return opener(request, timeout=timeout)
        opener = self.standard_open or urlopen
        return opener(request, timeout=timeout)

    def build_request_url(
        self,
        url: str,
        *,
        data: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        method: str = "POST",
    ) -> Request:
        return Request(url, data=data, headers=dict(headers or {}), method=method)

    def build_request(
        self,
        upstream: Mapping[str, Any],
        path: str,
        *,
        data: bytes | None = None,
        headers: Mapping[str, str] | None = None,
        method: str = "POST",
    ) -> Request:
        if self.endpoint_url_hook is None:
            raise RuntimeError("Gateway transport endpoint URL hook is not configured")
        parsed = urlsplit(path)
        endpoint_path = parsed.path or "/"
        url = self.endpoint_url_hook(upstream, endpoint_path)
        if parsed.query:
            url += ("&" if "?" in url else "?") + parsed.query
        return self.build_request_url(url, data=data, headers=headers, method=method)

    def proxy_url(self, url: str) -> str | None:
        if self.proxy_url_hook is not None:
            return self.proxy_url_hook(url)
        return official_proxy_url(
            url,
            getproxies_fn=self.getproxies or getproxies,
            getproxies_registry_fn=self.getproxies_registry or getproxies_registry,
            proxy_bypass_fn=self.proxy_bypass or proxy_bypass,
            platform=self.platform,
        )

    def pool_manager(self, url: str) -> Any:
        if self.pool_manager_hook is not None:
            return self.pool_manager_hook(url)
        return official_pool_manager(
            url,
            pools=self.official_pools,
            pools_lock=self.official_pools_lock,
            proxy_url=self.proxy_url(url),
        )

    def official_urlopen(self, request: Request, *, timeout: float) -> Any:
        return official_urlopen(
            request,
            timeout=timeout,
            pool_manager=self.pool_manager,
        )

    def materialize_authentication(
        self,
        incoming_headers: Mapping[str, str] | Any,
        upstream: Mapping[str, Any],
    ) -> OperationalAuthentication:
        return materialize_operational_authentication(
            incoming_headers,
            upstream,
            access_token=self.resolved_access_token,
            account_id=self.resolved_account_id,
            ollama_api_key=self.resolved_ollama_api_key,
            get_header=self._get_header,
            new_id=self.new_id,
        )

    def build_headers(
        self,
        incoming_headers: Mapping[str, str] | Any,
        upstream: Mapping[str, Any],
        drop_content_encoding: bool = False,
        behavior_profile: str | None = None,
        model_id: str | None = None,
        authentication_strategy: AuthenticationStrategy | None = None,
        request_mutation_policy: MutationPolicy | None = None,
        operational_authentication: OperationalAuthentication | None = None,
    ) -> dict[str, str]:
        return build_upstream_headers(
            incoming_headers,
            upstream,
            drop_content_encoding=drop_content_encoding,
            behavior_profile=behavior_profile,
            model_id=model_id,
            authentication_strategy=authentication_strategy,
            request_mutation_policy=request_mutation_policy,
            operational_authentication=operational_authentication,
            facts=self.facts,
            access_token=self.resolved_access_token,
            account_id=self.resolved_account_id,
            ollama_api_key=self.resolved_ollama_api_key,
            get_header=self._get_header,
            header_items=self._header_items,
            new_id=self.new_id,
        )

    def failure_class(self, exc: BaseException) -> str:
        return (self.failure_class_hook or _upstream_failure_class)(exc)

    def retry_after_seconds(self, exc: BaseException | None) -> int | None:
        return (self.retry_after_hook or _retry_after_delay_seconds)(exc)

    def open_response(self, request: Request, **kwargs: Any) -> Any:
        return _open_upstream_response(request, transport=self, **kwargs)


OFFICIAL_POOL_MAX_CONNECTIONS = 16
OFFICIAL_POOL_MAX_IDLE_SECONDS = 30.0
OFFICIAL_PROXY_POOL_MAX_IDLE_SECONDS = 300.0
OFFICIAL_CONNECT_TIMEOUT_SECONDS = 15.0
OFFICIAL_TERMINAL_DRAIN_TIMEOUT_SECONDS = 1.0
OFFICIAL_TCP_KEEPALIVE_IDLE_MS = 5000
OFFICIAL_TCP_KEEPALIVE_INTERVAL_MS = 5000
OFFICIAL_HTTP_POOLS: dict[str, Any] = {}
OFFICIAL_HTTP_POOLS_LOCK = threading.Lock()
_OFFICIAL_ATTEMPT_CONNECTION_STATE = threading.local()
_OFFICIAL_REQUEST_WRITE_DEADLINE_ATTRIBUTE = "_codexhub_request_write_deadline"
_OFFICIAL_REQUEST_WRITE_ACTIVE_ATTRIBUTE = "_codexhub_request_write_active"
_TRANSPORT_PHASE_ATTRIBUTE = "_codexhub_transport_phase"
_OFFICIAL_SOCKET_TIMEOUT_UNSET = object()

def _reset_official_attempt_state(timeout: float) -> None:
    """Initialize request-scoped state before a new Official pool request."""

    _OFFICIAL_ATTEMPT_CONNECTION_STATE.disposition = "unobserved"
    _OFFICIAL_ATTEMPT_CONNECTION_STATE.request_write_deadline = time.monotonic() + timeout


def _set_official_attempt_connection_disposition(disposition: str) -> None:
    if disposition in {"new", "reused"}:
        _OFFICIAL_ATTEMPT_CONNECTION_STATE.disposition = disposition


def _official_attempt_connection_disposition() -> str:
    disposition = getattr(_OFFICIAL_ATTEMPT_CONNECTION_STATE, "disposition", "unobserved")
    return disposition if disposition in {"new", "reused"} else "unobserved"


def _official_attempt_request_write_deadline() -> float | None:
    deadline = getattr(_OFFICIAL_ATTEMPT_CONNECTION_STATE, "request_write_deadline", None)
    return deadline if isinstance(deadline, (int, float)) else None


def _clear_official_attempt_state() -> None:
    for attribute in ("disposition", "request_write_deadline"):
        try:
            delattr(_OFFICIAL_ATTEMPT_CONNECTION_STATE, attribute)
        except AttributeError:
            pass


def _official_socket_options() -> list[tuple[int, int, int]]:
    options = list(urllib3.connection.HTTPConnection.default_socket_options)
    options.append((socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1))
    if not sys.platform.startswith("win"):
        if hasattr(socket, "TCP_KEEPIDLE"):
            options.append(
                (socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, max(1, OFFICIAL_TCP_KEEPALIVE_IDLE_MS // 1000))
            )
        if hasattr(socket, "TCP_KEEPINTVL"):
            options.append(
                (socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, max(1, OFFICIAL_TCP_KEEPALIVE_INTERVAL_MS // 1000))
            )
        if hasattr(socket, "TCP_KEEPCNT"):
            options.append((socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3))
    return options


def _configure_official_windows_keepalive(sock: Any) -> None:
    if sys.platform.startswith("win") and hasattr(socket, "SIO_KEEPALIVE_VALS"):
        sock.ioctl(
            socket.SIO_KEEPALIVE_VALS,
            (1, OFFICIAL_TCP_KEEPALIVE_IDLE_MS, OFFICIAL_TCP_KEEPALIVE_INTERVAL_MS),
        )


class _OfficialHTTPSConnection(urllib3.connection.HTTPSConnection):
    def connect(self) -> None:
        super().connect()
        if self.sock is not None:
            _configure_official_windows_keepalive(self.sock)

    def endheaders(self, message_body: Any = None, *, encode_chunked: bool = False) -> None:
        setattr(self, _OFFICIAL_REQUEST_WRITE_ACTIVE_ATTRIBUTE, True)
        super().endheaders(message_body=message_body, encode_chunked=encode_chunked)

    def send(self, data: Any) -> None:
        request_write_active = getattr(self, _OFFICIAL_REQUEST_WRITE_ACTIVE_ATTRIBUTE, False)
        sock = self.sock
        previous_timeout: Any = _OFFICIAL_SOCKET_TIMEOUT_UNSET
        if request_write_active and sock is not None:
            gettimeout = getattr(sock, "gettimeout", None)
            if callable(gettimeout):
                try:
                    previous_timeout = gettimeout()
                except Exception:
                    previous_timeout = _OFFICIAL_SOCKET_TIMEOUT_UNSET
        try:
            deadline = getattr(self, _OFFICIAL_REQUEST_WRITE_DEADLINE_ATTRIBUTE, None)
            if request_write_active and isinstance(deadline, (int, float)):
                remaining_timeout = deadline - time.monotonic()
                if remaining_timeout <= 0:
                    raise TimeoutError("Official request write budget exhausted")
                if sock is not None:
                    sock.settimeout(remaining_timeout)
            super().send(data)
        except TimeoutError as exc:
            if request_write_active:
                try:
                    setattr(exc, _TRANSPORT_PHASE_ATTRIBUTE, "request_write")
                except Exception:
                    pass
            raise
        finally:
            if (
                request_write_active
                and sock is not None
                and previous_timeout is not _OFFICIAL_SOCKET_TIMEOUT_UNSET
            ):
                try:
                    sock.settimeout(previous_timeout)
                except Exception:
                    pass


class _OfficialHTTPSConnectionPool(urllib3.connectionpool.HTTPSConnectionPool):
    ConnectionCls = _OfficialHTTPSConnection

    def _make_request(self, conn: Any, *args: Any, **kwargs: Any) -> Any:
        request_write_deadline = _official_attempt_request_write_deadline()
        if request_write_deadline is None:
            timeout = kwargs.get("timeout")
            request_write_timeout = getattr(timeout, "read_timeout", timeout)
            if isinstance(request_write_timeout, (int, float)) and request_write_timeout > 0:
                request_write_deadline = time.monotonic() + request_write_timeout
        try:
            setattr(conn, _OFFICIAL_REQUEST_WRITE_DEADLINE_ATTRIBUTE, request_write_deadline)
            return super()._make_request(conn, *args, **kwargs)
        finally:
            try:
                delattr(conn, _OFFICIAL_REQUEST_WRITE_DEADLINE_ATTRIBUTE)
            except AttributeError:
                pass
            try:
                delattr(conn, _OFFICIAL_REQUEST_WRITE_ACTIVE_ATTRIBUTE)
            except AttributeError:
                pass

    def _get_conn(self, timeout: float | None = None) -> Any:
        connection = super()._get_conn(timeout)
        released_at = getattr(connection, "_codexhub_released_at", None)
        try:
            disposition = (
                "reused" if isinstance(released_at, (int, float)) and getattr(connection, "sock", None) else "new"
            )
        except Exception:
            disposition = "unobserved"
        idle_seconds = time.monotonic() - released_at if isinstance(released_at, (int, float)) else None
        max_idle_seconds = (
            OFFICIAL_PROXY_POOL_MAX_IDLE_SECONDS if self.proxy is not None else OFFICIAL_POOL_MAX_IDLE_SECONDS
        )
        if idle_seconds is not None and idle_seconds >= max_idle_seconds:
            connection.close()
            disposition = "new"
        try:
            connection._codexhub_diagnostic_connection_disposition = disposition
        except Exception:
            pass
        _set_official_attempt_connection_disposition(disposition)
        return connection

    def _put_conn(self, connection: Any) -> None:
        if connection is not None:
            connection._codexhub_released_at = time.monotonic()
        super()._put_conn(connection)


class _OfficialPooledResponse:
    def __init__(self, response: Any):
        self._response = response
        self._exhausted = False
        self._released = False
        self.status = response.status
        self.reason = response.reason
        self.headers = response.headers
        self.connection_disposition = _connection_disposition(getattr(response, "connection", None))
        self._terminal_drain_socket: Any = None
        self._terminal_drain_original_timeout: float | None = None

    def read(self, amount: int | None = None) -> bytes:
        try:
            data = self._response.read(amount)
        except (urllib3.exceptions.HTTPError, OSError, IncompleteRead) as exc:
            translated = _stdlib_transport_error(exc)
            _propagate_transport_metadata(
                translated,
                source=exc,
                disposition=self.connection_disposition,
                phase=_explicit_transport_phase(exc) or "response_body",
            )
            raise translated from exc
        if amount is None or data == b"":
            self._exhausted = True
        return data

    def readline(self, limit: int = -1) -> bytes:
        try:
            data = self._response.readline(limit)
        except (urllib3.exceptions.HTTPError, OSError, IncompleteRead) as exc:
            translated = _stdlib_transport_error(exc)
            _propagate_transport_metadata(
                translated,
                source=exc,
                disposition=self.connection_disposition,
                phase=_explicit_transport_phase(exc) or "stream_body",
            )
            raise translated from exc
        if data == b"":
            self._exhausted = True
        return data

    def getcode(self) -> int:
        return self.status

    def shorten_terminal_drain_timeout(self, timeout_seconds: float) -> None:
        connection = getattr(self._response, "connection", None)
        sock = getattr(connection, "sock", None)
        if sock is None or self._terminal_drain_socket is not None:
            return
        try:
            original_timeout = sock.gettimeout()
            sock.settimeout(timeout_seconds)
        except OSError:
            return
        self._terminal_drain_socket = sock
        self._terminal_drain_original_timeout = original_timeout

    def _restore_terminal_drain_timeout(self) -> None:
        if self._terminal_drain_socket is None:
            return
        try:
            self._terminal_drain_socket.settimeout(self._terminal_drain_original_timeout)
        except OSError:
            pass
        self._terminal_drain_socket = None
        self._terminal_drain_original_timeout = None

    def close(self) -> None:
        if self._released:
            return
        self._released = True
        if self._exhausted:
            self._restore_terminal_drain_timeout()
            self._response.release_conn()
        else:
            self._response.close()
            self._response.release_conn()

    def __enter__(self) -> "_OfficialPooledResponse":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        self.close()
        return False


def _connection_disposition(connection: Any) -> str:
    try:
        disposition = getattr(connection, "_codexhub_diagnostic_connection_disposition", "unobserved")
    except Exception:
        return "unobserved"
    return disposition if disposition in {"new", "reused"} else "unobserved"


def _explicit_transport_phase(exc: BaseException | None) -> str | None:
    pending: list[Any] = [exc]
    seen: set[int] = set()
    supported = {
        "request_write",
        "response_headers",
        "response_body",
        "stream_body",
    }
    while pending:
        candidate = pending.pop(0)
        if not isinstance(candidate, BaseException) or id(candidate) in seen:
            continue
        seen.add(id(candidate))
        try:
            phase = getattr(candidate, _TRANSPORT_PHASE_ATTRIBUTE, None)
        except Exception:
            phase = None
        if phase in supported:
            return phase
        pending.extend(
            value
            for value in (
                getattr(candidate, "reason", None),
                candidate.__cause__,
                candidate.__context__,
                *candidate.args,
            )
            if isinstance(value, BaseException)
        )
    return None


def _propagate_transport_metadata(
    target: BaseException,
    *,
    source: BaseException | None = None,
    disposition: str | None = None,
    phase: str | None = None,
) -> BaseException:
    resolved_phase = phase if phase in {
        "request_write",
        "response_headers",
        "response_body",
        "stream_body",
    } else _explicit_transport_phase(source)
    if resolved_phase is not None:
        try:
            setattr(target, _TRANSPORT_PHASE_ATTRIBUTE, resolved_phase)
        except Exception:
            pass
    if disposition in {"new", "reused"}:
        try:
            setattr(target, "_codexhub_diagnostic_connection_disposition", disposition)
        except Exception:
            pass
    return target


def _stdlib_transport_error(exc: BaseException) -> BaseException:
    pending: list[Any] = [exc]
    seen: set[int] = set()
    while pending:
        candidate = pending.pop(0)
        if not isinstance(candidate, BaseException) or id(candidate) in seen:
            continue
        seen.add(id(candidate))
        if isinstance(candidate, (ssl.SSLError, TimeoutError, ConnectionError, OSError, IncompleteRead)):
            return candidate
        pending.extend(
            value
            for value in (
                getattr(candidate, "reason", None),
                candidate.__cause__,
                candidate.__context__,
                *candidate.args,
            )
            if isinstance(value, BaseException)
        )
    if isinstance(exc, urllib3.exceptions.TimeoutError):
        return TimeoutError(str(exc))
    return URLError(exc)


def official_proxy_url(
    url: str,
    *,
    getproxies_fn: Callable[[], Mapping[str, str]] | None = None,
    getproxies_registry_fn: Callable[[], Mapping[str, str]] | None = None,
    proxy_bypass_fn: Callable[[str], bool] | None = None,
    platform: str | None = None,
) -> str | None:
    parsed = urlsplit(url)
    resolved_bypass = proxy_bypass if proxy_bypass_fn is None else proxy_bypass_fn
    resolved_getproxies = getproxies if getproxies_fn is None else getproxies_fn
    resolved_registry = getproxies_registry if getproxies_registry_fn is None else getproxies_registry_fn
    resolved_platform = sys.platform if platform is None else platform
    if parsed.hostname:
        try:
            if resolved_bypass(parsed.hostname):
                return None
        except OSError:
            pass
    proxies = resolved_getproxies()
    proxy = proxies.get(parsed.scheme)
    if (
        not proxy
        and resolved_platform.startswith("win")
        and callable(resolved_registry)
        and not any(proxies.get(scheme) for scheme in ("http", "https"))
    ):
        try:
            proxy = resolved_registry().get(parsed.scheme)
        except OSError:
            proxy = None
    return str(proxy) if proxy else None


def _official_proxy_url(url: str) -> str | None:
    return official_proxy_url(url)


def official_pool_manager(
    url: str,
    *,
    pools: dict[str, Any] | None = None,
    pools_lock: threading.Lock | None = None,
    proxy_url: Any = _UNSET_PROXY,
    socket_options: list[tuple[int, int, int]] | None = None,
) -> Any:
    resolved_pools = OFFICIAL_HTTP_POOLS if pools is None else pools
    resolved_lock = OFFICIAL_HTTP_POOLS_LOCK if pools_lock is None else pools_lock
    resolved_proxy = _official_proxy_url(url) if proxy_url is _UNSET_PROXY else proxy_url
    pool_key = resolved_proxy or "direct"
    existing = resolved_pools.get(pool_key)
    if existing is not None:
        return existing
    with resolved_lock:
        existing = resolved_pools.get(pool_key)
        if existing is None:
            pool_options = {
                "num_pools": 4,
                "maxsize": OFFICIAL_POOL_MAX_CONNECTIONS,
                "block": True,
                "retries": False,
                "socket_options": socket_options if socket_options is not None else _official_socket_options(),
            }
            existing = (
                urllib3.ProxyManager(resolved_proxy, **pool_options)
                if resolved_proxy is not None
                else urllib3.PoolManager(**pool_options)
            )
            existing.pool_classes_by_scheme = {
                **existing.pool_classes_by_scheme,
                "https": _OfficialHTTPSConnectionPool,
            }
            resolved_pools[pool_key] = existing
        return existing


def _official_pool_manager(url: str) -> Any:
    return official_pool_manager(url)


def official_urlopen(
    request: Request,
    *,
    timeout: float,
    pool_manager: Callable[[str], Any] | None = None,
) -> Any:
    _reset_official_attempt_state(timeout)
    try:
        manager = (pool_manager or _official_pool_manager)(request.full_url)
        headers = {key: value for key, value in request.header_items() if key.lower() != "connection"}
        response = manager.request(
            request.get_method(),
            request.full_url,
            body=request.data,
            headers=headers,
            preload_content=False,
            decode_content=False,
            redirect=False,
            retries=False,
            timeout=urllib3.Timeout(connect=min(timeout, OFFICIAL_CONNECT_TIMEOUT_SECONDS), read=timeout),
            pool_timeout=timeout,
        )
    except (urllib3.exceptions.HTTPError, OSError, IncompleteRead) as exc:
        translated = _stdlib_transport_error(exc)
        _propagate_transport_metadata(
            translated,
            source=exc,
            disposition=_official_attempt_connection_disposition(),
            phase=_explicit_transport_phase(exc)
            or (
                "response_headers"
                if isinstance(exc, urllib3.exceptions.ReadTimeoutError)
                else None
            ),
        )
        raise translated from exc
    finally:
        _clear_official_attempt_state()

    pooled_response = _OfficialPooledResponse(response)
    if response.status >= 400:
        error = HTTPError(
            request.full_url,
            response.status,
            str(response.reason or "upstream error"),
            response.headers,
            pooled_response,
        )
        _propagate_transport_metadata(
            error,
            disposition=pooled_response.connection_disposition,
        )
        raise error
    return pooled_response


def _official_urlopen(request: Request, *, timeout: float) -> Any:
    return official_urlopen(request, timeout=timeout)



def header_items(headers: Mapping[str, str] | Any) -> list[tuple[str, str]]:
    return [(str(key), str(value)) for key, value in headers.items()]
_header_items = header_items


def get_header(headers: Mapping[str, str] | Any, name: str) -> str | None:
    wanted = name.lower()
    for key, value in _header_items(headers):
        if key.lower() == wanted:
            return value
    return None
_get_header = get_header


def _default_access_token() -> str:
    adapter = credential_for("codex_auth")
    if adapter is not None:
        return adapter.access_token()
    from codex_auth import access_token as load_access_token
    return load_access_token()


def _default_account_id() -> str | None:
    adapter = credential_for("codex_auth")
    if adapter is not None:
        account = adapter.account_headers().get("Chatgpt-account-id")
        return str(account) if account else None
    from codex_auth import account_id as load_account_id
    return load_account_id()


def _catalog_auth_mode(upstream: Mapping[str, Any]) -> str | None:
    auth_mode = upstream.get("auth")
    return str(auth_mode) if auth_mode else None


def _resolved_auth_mode(
    *,
    upstream: Mapping[str, Any],
    operational_authentication: OperationalAuthentication | None,
    authentication_strategy: AuthenticationStrategy | None,
) -> str | None:
    catalog_auth = _catalog_auth_mode(upstream)
    if credential_for(catalog_auth) is not None:
        return catalog_auth
    if operational_authentication is not None:
        return operational_authentication.strategy.value
    if authentication_strategy is not None:
        return authentication_strategy.value
    return catalog_auth


def _subscription_authorization(
    adapter: Any,
    *,
    auth_mode: str | None,
    operational_authentication: OperationalAuthentication | None,
    access_token: Callable[[], str] | None,
    load_token: Callable[[], str],
) -> str:
    if operational_authentication is not None:
        authorization = operational_authentication.authorization
    elif access_token is not None:
        authorization = f"Bearer {load_token()}"
    else:
        authorization = f"Bearer {adapter.access_token()}"
    if not authorization:
        raise ValueError(f"subscription credential is missing for auth mode: {auth_mode}")
    return authorization


def _subscription_account_headers(
    adapter: Any,
    *,
    operational_authentication: OperationalAuthentication | None,
    account_id: Callable[[], str | None] | None,
    load_account: Callable[[], str | None],
) -> dict[str, str]:
    if operational_authentication is not None:
        account = operational_authentication.account_id
        return {"Chatgpt-account-id": account} if account else {}
    if account_id is not None:
        account = load_account()
        return {"Chatgpt-account-id": account} if account else {}
    return dict(adapter.account_headers())


def materialize_operational_authentication(
    incoming_headers: Mapping[str, str] | Any,
    upstream: Mapping[str, Any],
    *,
    access_token: Callable[[], str] | None = None,
    account_id: Callable[[], str | None] | None = None,
    ollama_api_key: Callable[[], str | None] | None = None,
    get_header: Callable[..., str | None] | None = None,
    new_id: Callable[[], str] | None = None,
) -> OperationalAuthentication:
    read_header = get_header or _get_header
    make_id = new_id or (lambda: str(uuid.uuid4()))
    load_token = access_token or _default_access_token
    load_account = account_id or _default_account_id
    auth_mode = _catalog_auth_mode(upstream) or "unknown"
    strategy = _authentication_strategy(auth_mode)
    adapter = credential_for(auth_mode)
    if adapter is not None:
        token = load_token() if access_token is not None else adapter.access_token()
        account_headers = _subscription_account_headers(
            adapter,
            operational_authentication=None,
            account_id=account_id,
            load_account=load_account,
        )
        account = account_headers.get("Chatgpt-account-id")
        if strategy == AuthenticationStrategy.CODEX_AUTH:
            return OperationalAuthentication(
                strategy,
                authorization=f"Bearer {token}",
                account_id=account,
                generated_session_id=(
                    read_header(incoming_headers, "Session-id")
                    or make_id()
                ),
                generated_client_request_id=(
                    read_header(incoming_headers, "X-client-request-id")
                    or make_id()
                ),
            )
        return OperationalAuthentication(
            strategy,
            authorization=f"Bearer {token}",
            account_id=account,
        )
    if strategy == AuthenticationStrategy.INCOMING:
        return OperationalAuthentication(
            strategy,
            authorization=read_header(incoming_headers, "Authorization"),
        )
    if strategy == AuthenticationStrategy.OLLAMA_API_KEY:
        api_key = ollama_api_key() if ollama_api_key is not None else os.environ.get("OLLAMA_API_KEY")
        return OperationalAuthentication(
            strategy,
            authorization=f"Bearer {api_key}" if api_key else None,
        )
    if strategy == AuthenticationStrategy.API_KEY:
        api_key = upstream.get("api_key")
        return OperationalAuthentication(
            strategy,
            authorization=f"Bearer {api_key}" if api_key else None,
        )
    return OperationalAuthentication(strategy, authorization=None)


def build_upstream_headers(
    incoming_headers: Mapping[str, str] | Any,
    upstream: Mapping[str, Any],
    drop_content_encoding: bool = False,
    behavior_profile: str | None = None,
    model_id: str | None = None,
    authentication_strategy: AuthenticationStrategy | None = None,
    request_mutation_policy: MutationPolicy | None = None,
    operational_authentication: OperationalAuthentication | None = None,
    *,
    facts: TransportFacts | None = None,
    access_token: Callable[[], str] | None = None,
    account_id: Callable[[], str | None] | None = None,
    ollama_api_key: Callable[[], str | None] | None = None,
    get_header: Callable[..., str | None] | None = None,
    header_items: Callable[..., list[tuple[str, str]]] | None = None,
    new_id: Callable[[], str] | None = None,
) -> dict[str, str]:
    resolved_facts = facts or TransportFacts()
    read_header = get_header or _get_header
    items = header_items or _header_items
    make_id = new_id or (lambda: str(uuid.uuid4()))
    load_token = access_token or _default_access_token
    load_account = account_id or _default_account_id
    auth_mode = _resolved_auth_mode(
        upstream=upstream,
        operational_authentication=operational_authentication,
        authentication_strategy=authentication_strategy,
    )
    outgoing: dict[str, str] = {}
    adapter = credential_for(auth_mode)
    drop_incoming_header = (
        getattr(adapter, "drop_incoming_header", None) if adapter is not None else None
    )
    model_id_for_adapter = str(upstream.get("upstream_model") or model_id or "")

    for key, value in items(incoming_headers):
        lowered = key.lower()
        if lowered in resolved_facts.hop_by_hop_request_headers or lowered == "authorization":
            continue
        if drop_incoming_header is not None and drop_incoming_header(
            lowered, model_id=model_id_for_adapter
        ):
            continue
        if drop_content_encoding and lowered == "content-encoding":
            continue
        outgoing[key] = value

    if not any(key.lower() == "user-agent" for key in outgoing):
        outgoing["User-Agent"] = UPSTREAM_USER_AGENT

    if adapter is not None:
        outgoing["Authorization"] = _subscription_authorization(
            adapter,
            auth_mode=auth_mode,
            operational_authentication=operational_authentication,
            access_token=access_token,
            load_token=load_token,
        )
        for header_name, header_value in _subscription_account_headers(
            adapter,
            operational_authentication=operational_authentication,
            account_id=account_id,
            load_account=load_account,
        ).items():
            if header_value and not read_header(outgoing, header_name):
                outgoing[header_name] = header_value
        apply_identity_headers = getattr(adapter, "apply_identity_headers", None)
        if apply_identity_headers is not None:
            strict_official_passthrough = (
                request_mutation_policy == MutationPolicy.OFFICIAL_PASSTHROUGH
                if request_mutation_policy is not None
                else behavior_profile == resolved_facts.official_passthrough_behavior
            )
            apply_identity_headers(
                outgoing,
                strict_official_passthrough=strict_official_passthrough,
                session_id=(
                    operational_authentication.generated_session_id
                    if operational_authentication is not None
                    else None
                ),
                client_request_id=(
                    operational_authentication.generated_client_request_id
                    if operational_authentication is not None
                    else None
                ),
                read_header=read_header,
                make_id=make_id,
            )
        return outgoing
    if auth_mode == "incoming":
        incoming_auth = (
            operational_authentication.authorization
            if operational_authentication is not None
            else read_header(incoming_headers, "Authorization")
        )
        if incoming_auth:
            outgoing["Authorization"] = incoming_auth
    elif auth_mode == "ollama_api_key":
        if operational_authentication is not None:
            authorization = operational_authentication.authorization
            if authorization is None:
                raise ValueError("OLLAMA_API_KEY is not set")
        else:
            api_key = ollama_api_key() if ollama_api_key is not None else os.environ.get("OLLAMA_API_KEY")
            if not api_key:
                raise ValueError("OLLAMA_API_KEY is not set")
            authorization = f"Bearer {api_key}"
        outgoing["Authorization"] = authorization
    elif auth_mode == "api_key":
        if operational_authentication is not None:
            authorization = operational_authentication.authorization
            if authorization is None:
                raise ValueError(
                    "API key is not set for upstream: "
                    f"{upstream.get('name', 'unknown')}"
                )
        else:
            api_key = upstream.get("api_key")
            if not api_key:
                raise ValueError(
                    "API key is not set for upstream: "
                    f"{upstream.get('name', 'unknown')}"
                )
            authorization = f"Bearer {api_key}"
        outgoing["Authorization"] = authorization
    else:
        raise ValueError(f"unsupported upstream auth mode: {auth_mode}")
    return outgoing


def _redact_error_detail(exc: BaseException) -> str:
    reason = getattr(exc, "reason", None)
    source = reason if reason is not None else exc
    detail = f"{type(source).__name__}: {source}"
    detail = detail.replace("\r", " ").replace("\n", " ")
    if "Bearer " in detail:
        detail = detail.split("Bearer ", 1)[0] + "Bearer [redacted]"
    return detail[:300]



def transport_failure_phase(exc: BaseException | None) -> str | None:
    """Best-effort phase label for failures before an upstream response is relayed."""
    if exc is None:
        return None
    explicit_phase = _explicit_transport_phase(exc)
    if explicit_phase is not None:
        return explicit_phase
    reason = getattr(exc, "reason", None)
    if isinstance(exc, URLError) and isinstance(reason, BaseException):
        nested = transport_failure_phase(reason)
        if nested:
            return nested
    if isinstance(exc, HTTPError):
        return "response_headers"
    if isinstance(exc, ssl.SSLEOFError):
        return "tls_handshake"
    if isinstance(exc, ssl.SSLError):
        return "tls_handshake"
    if isinstance(exc, TimeoutError):
        return "tcp_connect"
    if isinstance(exc, IncompleteRead):
        return "response_headers"
    detail = _redact_error_detail(exc).lower()
    if "unexpected_eof" in detail or "ssleoferror" in detail or "eof occurred in violation" in detail:
        return "tls_handshake"
    if "timed out" in detail or "timeout" in detail or "winerror 10060" in detail:
        return "tcp_connect"
    if "connection reset" in detail or "connectionreseterror" in detail or "winerror 10054" in detail:
        return "request_write"
    if isinstance(exc, (OSError, URLError)):
        return "tcp_connect"
    return None



def _upstream_retry_status(exc: BaseException) -> int | None:
    status = getattr(exc, "code", None)
    return status if isinstance(status, int) else None


def _retry_attempts_for_failure_class(
    *,
    request_kind: str,
    base_attempts: int,
    failure_class: str,
    explicit_max_attempts: bool,
    stream_failure: bool = False,
) -> int:
    if (
        explicit_max_attempts
        or base_attempts <= 1
        or _request_kind_retry_attempts_configured(request_kind)
    ):
        return base_attempts
    if failure_class in CAPACITY_RETRY_FAILURE_CLASSES:
        return max(base_attempts, gateway_auto_retry_max_attempts())
    if stream_failure and failure_class == RETRY_FAILURE_QUICK_TRANSIENT:
        return max(base_attempts, gateway_auto_retry_max_attempts())
    return base_attempts


def _http_retry_header_override(exc: HTTPError) -> bool | None:
    value = _get_header(getattr(exc, "headers", {}), "x-should-retry")
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if normalized in {"true", "1", "yes", "on"}:
        return True
    if normalized in {"false", "0", "no", "off"}:
        return False
    return None


def _http_error_body_bytes(exc: HTTPError) -> bytes:
    cached = getattr(exc, "_codexhub_error_body", None)
    if isinstance(cached, bytes):
        return cached
    fp = getattr(exc, "fp", None)
    if fp is None:
        return b""
    try:
        body = fp.read()
    except OSError:
        body = b""
    finally:
        try:
            fp.close()
        except OSError:
            pass
    replacement = io.BytesIO(body)
    exc.fp = replacement
    exc.file = replacement
    setattr(exc, "_codexhub_error_body", body)
    return body


def _http_error_payload(exc: HTTPError) -> Mapping[str, Any] | None:
    body = _http_error_body_bytes(exc)
    if not body:
        return None
    try:
        payload = json.loads(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, Mapping) else None


def _payload_error_values(payload: Mapping[str, Any] | None) -> set[str]:
    if not isinstance(payload, Mapping):
        return set()
    error = payload.get("error")
    values: set[str] = set()
    value_keys = (
        "__type",
        "code",
        "detail",
        "error_code",
        "error_type",
        "errorCode",
        "errorType",
        "message",
        "param",
        "reason",
        "status",
        "type",
    )

    def add_value(value: Any) -> None:
        if isinstance(value, str) and value:
            values.add(value.strip().lower())
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            values.add(str(value))

    def add_mapping_values(mapping: Mapping[str, Any]) -> None:
        for key in value_keys:
            add_value(mapping.get(key))
        nested_errors = mapping.get("errors")
        if isinstance(nested_errors, list):
            for item in nested_errors:
                if isinstance(item, Mapping):
                    add_mapping_values(item)
        nested_response = mapping.get("response")
        if isinstance(nested_response, Mapping):
            nested_error = nested_response.get("error")
            if isinstance(nested_error, Mapping):
                add_mapping_values(nested_error)

    if isinstance(error, Mapping):
        add_mapping_values(error)
    elif isinstance(error, str) and error:
        values.add(error.strip().lower())
    add_mapping_values(payload)
    return values


def _http_error_values(exc: HTTPError) -> set[str]:
    return _payload_error_values(_http_error_payload(exc))


def _http_error_values_contain(values: set[str], needles: tuple[str, ...]) -> bool:
    return any(needle in value for value in values for needle in needles)


def _failure_class_from_error_values(values: set[str]) -> str | None:
    if not values:
        return None
    if any(value in PERMANENT_UPSTREAM_ERROR_VALUES for value in values):
        return RETRY_FAILURE_PERMANENT
    if _http_error_values_contain(values, PERMANENT_UPSTREAM_ERROR_NEEDLES):
        return RETRY_FAILURE_PERMANENT
    if any(value in PROVIDER_THROTTLE_ERROR_VALUES for value in values) or _http_error_values_contain(
        values,
        PROVIDER_THROTTLE_ERROR_NEEDLES,
    ):
        return RETRY_FAILURE_PROVIDER_THROTTLE
    if any(value in PROVIDER_OVERLOADED_ERROR_VALUES for value in values) or _http_error_values_contain(
        values,
        PROVIDER_OVERLOADED_ERROR_NEEDLES,
    ):
        return RETRY_FAILURE_PROVIDER_OVERLOADED
    if _http_error_values_contain(values, PERMANENT_UPSTREAM_AUTH_NEEDLES):
        return RETRY_FAILURE_PERMANENT
    return None


def _status_allows_capacity_error_value(status: int | None) -> bool:
    if status is None:
        return True
    if status == 400:
        return True
    return status not in PERMANENT_HTTP_ERROR_STATUSES


def retry_after_delay_seconds(exc: BaseException | None) -> int | None:
    if not isinstance(exc, HTTPError):
        return None
    value = _get_header(getattr(exc, "headers", {}), "retry-after")
    if not isinstance(value, str) or not value.strip():
        return None
    stripped = value.strip()
    try:
        seconds = float(stripped)
    except ValueError:
        seconds = None
    if seconds is not None:
        return max(0, math.ceil(seconds))
    try:
        retry_at = parsedate_to_datetime(stripped)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=timezone.utc)
    return max(0, math.ceil(retry_at.timestamp() - time.time()))
_retry_after_delay_seconds = retry_after_delay_seconds


def upstream_failure_class(exc: BaseException) -> str:
    if isinstance(exc, _STREAM_INTERRUPTED_ERROR):
        return _upstream_failure_class(exc.cause)
    if isinstance(exc, _STREAM_ERROR_EVENT):
        values = _payload_error_values(getattr(exc, "payload", None))
        value_class = _failure_class_from_error_values(values)
        if value_class is not None:
            return value_class
        return RETRY_FAILURE_QUICK_TRANSIENT
    if isinstance(exc, HTTPError):
        override = _http_retry_header_override(exc)
        if override is False:
            return RETRY_FAILURE_PERMANENT
        status = _upstream_retry_status(exc)
        values = _http_error_values(exc)
        value_class = _failure_class_from_error_values(values)
        if value_class in CAPACITY_RETRY_FAILURE_CLASSES and _status_allows_capacity_error_value(status):
            return value_class
        if value_class == RETRY_FAILURE_PERMANENT and override is not True:
            return RETRY_FAILURE_PERMANENT
        if status in PERMANENT_HTTP_ERROR_STATUSES:
            return RETRY_FAILURE_QUICK_TRANSIENT if override is True else RETRY_FAILURE_PERMANENT
        if status == 429:
            if value_class == RETRY_FAILURE_PERMANENT:
                return RETRY_FAILURE_PERMANENT
            return RETRY_FAILURE_PROVIDER_THROTTLE
        if status == 503:
            return RETRY_FAILURE_PROVIDER_OVERLOADED
        if override is True:
            return RETRY_FAILURE_QUICK_TRANSIENT
        if status in TRANSIENT_HTTP_RETRY_STATUSES:
            return RETRY_FAILURE_QUICK_TRANSIENT
        if status is not None and 520 <= status <= 599:
            return RETRY_FAILURE_QUICK_TRANSIENT
        return RETRY_FAILURE_PERMANENT
    if isinstance(
        exc,
        (
            CompactEmptyResponseError,
            IncompleteRead,
            OSError,
            TimeoutError,
            URLError,
            UpstreamStreamIdleTimeoutError,
            UpstreamStreamIncompleteError,
        ),
    ):
        return RETRY_FAILURE_QUICK_TRANSIENT
    return RETRY_FAILURE_PERMANENT
_upstream_failure_class = upstream_failure_class


def _capacity_retry_elapsed_limit_allows(started_at: float, delay_seconds: int) -> bool:
    limit_seconds = gateway_capacity_retry_elapsed_limit_seconds()
    if limit_seconds <= 0:
        return True
    return (time.monotonic() - started_at + delay_seconds) <= limit_seconds


def _upstream_error_retryable(
    exc: BaseException,
    *,
    request_kind: str = RETRY_REQUEST_MAIN_GENERATION,
) -> bool:
    return _upstream_failure_class(exc) != RETRY_FAILURE_PERMANENT



def _remaining_pre_response_budget_seconds(deadline: float | None) -> float | None:
    if deadline is None:
        return None
    return deadline - time.monotonic()


def _clamp_timeout_to_pre_response_budget(
    timeout: int | float,
    deadline: float | None,
    *,
    phase: str,
    attempt: int | None = None,
) -> int | float:
    remaining = _remaining_pre_response_budget_seconds(deadline)
    if remaining is None:
        return timeout
    if remaining <= 0:
        raise GatewayPreResponseBudgetExhausted(phase=phase, attempt=attempt)
    return min(float(timeout), remaining)


def _require_retry_delay_within_pre_response_budget(
    deadline: float | None,
    delay_seconds: int | float,
    *,
    phase: str,
    attempt: int | None = None,
) -> None:
    remaining = _remaining_pre_response_budget_seconds(deadline)
    if remaining is not None and delay_seconds >= remaining:
        raise GatewayPreResponseBudgetExhausted(phase=phase, attempt=attempt)




def _open_upstream_response(
    request: Request,
    *,
    transport: GatewayTransport,
    upstream_name: str,
    upstream_format: str,
    timeout: int | float,
    event_context: Mapping[str, Any] | None = None,
    downstream_retry_callback: Any = None,
    request_kind: str = RETRY_REQUEST_MAIN_GENERATION,
    max_attempts: int | None = None,
    retry_policy: str = RETRY_GATEWAY_FULL,
    retry_http_errors: bool = True,
    retry_execution: RetryExecutionPlanLike | None = None,
    transport_policy: TransportPolicy | None = None,
    downstream_exposed: Callable[[], bool] | None = None,
    pre_response_deadline: float | None = None,
    open_attempt_budget: dict[str, int] | None = None,
) -> Any:
    opener = transport.open_once_hook or transport.open_once
    if retry_execution is not None:
        request_kind = retry_execution.request_kind
        retry_policy = retry_execution.policy.value
        retry_http_errors = retry_execution.retry_http_errors
        timeout = retry_execution.request_timeout_seconds
        base_retry_attempts = retry_execution.base_open_attempts
        explicit_max_attempts = (
            retry_execution.open_attempt_budget is not None
        )
        if open_attempt_budget is None:
            open_attempt_budget = retry_execution.new_open_attempt_budget()
    else:
        explicit_max_attempts = max_attempts is not None
        base_retry_attempts = (
            (transport.upstream_retry_attempts or _upstream_retry_attempts)(request_kind)
            if max_attempts is None
            else max(1, max_attempts)
        )
    if open_attempt_budget is not None:
        remaining_open_attempts = max(
            0,
            open_attempt_budget["max_attempts"] - open_attempt_budget["attempts_started"],
        )
        if remaining_open_attempts <= 0:
            raise GatewayPreResponseBudgetExhausted(
                phase="upstream_open_attempts",
                attempt=open_attempt_budget["attempts_started"],
            )
        base_retry_attempts = min(base_retry_attempts, remaining_open_attempts)
    retry_started_at = time.monotonic()
    diagnostic_request_key = transport._observe_context_value(event_context, "request_id")
    diagnostic_model = transport._observe_context_value(event_context, "model")
    model_access_path = transport._model_access_path(
        event_context,
        upstream_name,
        upstream_format,
    )
    attempt = 1
    while True:
        if open_attempt_budget is not None:
            request_attempt = open_attempt_budget["attempts_started"] + 1
            request_retry_budget = open_attempt_budget["max_attempts"]
        else:
            request_attempt = attempt
            request_retry_budget = base_retry_attempts
        admission = transport.current_admission()
        if admission is not None:
            admission.raise_if_cancelled()
        if transport._path_idempotent(model_access_path):
            transport.ensure_retry_identity(
                event_context if isinstance(event_context, dict) else None,
                request,
                model_access_path,
            )
        attempt_timeout = _clamp_timeout_to_pre_response_budget(
            timeout,
            pre_response_deadline,
            phase="upstream_open",
            attempt=request_attempt,
        )
        if open_attempt_budget is not None:
            open_attempt_budget["attempts_started"] = request_attempt
        attempt_started_at = time.monotonic()
        try:
            response = opener(
                request,
                upstream_name=upstream_name,
                timeout=attempt_timeout,
                transport_policy=transport_policy,
            )
            remaining_budget = _remaining_pre_response_budget_seconds(pre_response_deadline)
            if remaining_budget is not None and remaining_budget <= 0:
                close_response = getattr(response, "close", None)
                if callable(close_response):
                    try:
                        close_response()
                    except Exception:
                        pass
                raise GatewayPreResponseBudgetExhausted(
                    phase="response_headers",
                    attempt=request_attempt,
                )
            if admission is not None:
                admission.attach_upstream_transport(response)
                admission.raise_if_cancelled()
            elapsed_ms = int(max(0.0, time.monotonic() - attempt_started_at) * 1000)
            connection_disposition = transport._observe_connection_disposition(response)
            # A returned response proves this Gateway attempt reached response
            # completion after writing its request. It cannot prove DNS, TCP,
            # or TLS occurred for this attempt (especially on a reused lease),
            # so those success phases remain absent unless a lower-level seam
            # later exposes them.
            transport.observe_upstream_phase(
                diagnostic_request_key,
                phase="upstream_request_write",
                attempt=request_attempt,
                retry_budget=request_retry_budget,
                elapsed_ms=elapsed_ms,
                outcome="ok",
                provider=upstream_name,
                model=diagnostic_model,
            )
            transport.observe_upstream_attempt(
                diagnostic_request_key,
                attempt=request_attempt,
                retry_budget=request_retry_budget,
                elapsed_ms=elapsed_ms,
                outcome="ok",
                connection_disposition=connection_disposition,
                provider=upstream_name,
                model=diagnostic_model,
            )
            diagnostic_status, diagnostic_headers = transport._observe_response_metadata(response)
            transport.observe_upstream_headers(
                diagnostic_request_key,
                status=diagnostic_status,
                headers=diagnostic_headers,
            )
            return response
        except GatewayPreResponseBudgetExhausted:
            raise
        except (HTTPError, IncompleteRead, OSError, URLError) as exc:
            if admission is not None:
                admission.raise_if_cancelled()
            elapsed_ms = int(max(0.0, time.monotonic() - attempt_started_at) * 1000)
            connection_disposition = transport._observe_error_disposition(exc)
            try:
                transport_phase = transport._failure_phase(exc)
            except Exception:
                transport_phase = "unknown"
            # The conservative retry-safety phase is authoritative for the
            # request-scoped retry decision and for any retry telemetry that
            # downstream consumers may treat as classification evidence.  The
            # best-effort transport phase is retained only for low-level
            # diagnostics that are explicitly marked as heuristic.
            retry_safety_failure_phase = transport._retry_safety_phase(exc) or "unknown"
            apply_retry_safety = (
                upstream_name != transport.facts.official_upstream_name
                and request_kind == RETRY_REQUEST_MAIN_GENERATION
                and request.get_method() == "POST"
            )
            telemetry_failure_phase = retry_safety_failure_phase if apply_retry_safety else transport_phase
            diagnostic_phase = transport._observe_transport_phase(transport_phase)
            if diagnostic_phase is not None:
                transport.observe_upstream_phase(
                    diagnostic_request_key,
                    phase=diagnostic_phase,
                    attempt=request_attempt,
                    retry_budget=request_retry_budget,
                    elapsed_ms=elapsed_ms,
                    outcome="error",
                    failure_phase=transport_phase,
                    provider=upstream_name,
                    model=diagnostic_model,
                )
            if isinstance(exc, HTTPError) and not retry_http_errors:
                transport.observe_upstream_attempt(
                    diagnostic_request_key,
                    attempt=request_attempt,
                    retry_budget=request_attempt,
                    elapsed_ms=elapsed_ms,
                    outcome="error",
                    failure_phase=telemetry_failure_phase,
                    connection_disposition=connection_disposition,
                    provider=upstream_name,
                    model=diagnostic_model,
                )
                raise
            failure_class = transport.failure_class(exc)
            downstream_exposed_now = bool(downstream_exposed is not None and downstream_exposed())
            retry_safety_class = transport._retry_safety(
                exc,
                request=request,
                upstream_name=upstream_name,
                request_kind=request_kind,
                downstream_exposed=downstream_exposed_now,
                model_access_path=model_access_path,
                failure_phase=retry_safety_failure_phase,
            )
            retry_attempts = (
                (
                    min(
                        base_retry_attempts,
                        retry_execution.open_attempts_for_failure_class(
                            failure_class
                        ),
                    )
                    if open_attempt_budget is not None
                    else retry_execution.open_attempts_for_failure_class(
                        failure_class
                    )
                )
                if retry_execution is not None
                else transport._retry_attempts(
                    request_kind=request_kind,
                    base_attempts=base_retry_attempts,
                    failure_class=failure_class,
                    explicit_max_attempts=explicit_max_attempts,
                )
            )
            error_retry_budget = (
                open_attempt_budget["max_attempts"]
                if open_attempt_budget is not None
                else retry_attempts
            )
            if retry_safety_class in transport.facts.suppressed_retry_safety_classes:
                transport.observe_upstream_attempt(
                    diagnostic_request_key,
                    attempt=request_attempt,
                    retry_budget=error_retry_budget,
                    elapsed_ms=elapsed_ms,
                    outcome="error",
                    failure_phase=telemetry_failure_phase,
                    connection_disposition=connection_disposition,
                    provider=upstream_name,
                    model=diagnostic_model,
                )
                remaining_budget = _remaining_pre_response_budget_seconds(pre_response_deadline)
                if remaining_budget is not None and remaining_budget <= 0:
                    raise GatewayPreResponseBudgetExhausted(
                        phase=telemetry_failure_phase,
                        attempt=request_attempt,
                    ) from exc
                transport.emit_retry_suppressed(
                    event_context,
                    upstream_name=upstream_name,
                    upstream_format=upstream_format,
                    request_kind=request_kind,
                    attempt=request_attempt,
                    max_attempts=error_retry_budget,
                    exc=exc,
                    failure_class=failure_class,
                    failure_phase=telemetry_failure_phase,
                    retry_safety_class=retry_safety_class,
                )
                raise
            transport.observe_upstream_attempt(
                diagnostic_request_key,
                attempt=request_attempt,
                retry_budget=error_retry_budget,
                elapsed_ms=elapsed_ms,
                outcome="error",
                failure_phase=telemetry_failure_phase,
                connection_disposition=connection_disposition,
                provider=upstream_name,
                model=diagnostic_model,
            )
            remaining_budget = _remaining_pre_response_budget_seconds(pre_response_deadline)
            if remaining_budget is not None and remaining_budget <= 0:
                raise GatewayPreResponseBudgetExhausted(
                    phase=telemetry_failure_phase,
                    attempt=request_attempt,
                ) from exc
            if attempt >= retry_attempts or failure_class == RETRY_FAILURE_PERMANENT:
                raise
            delay_seconds = (
                retry_execution.retry_delay_seconds(
                    request_attempt,
                    failure_class=failure_class,
                    retry_after_seconds=transport.retry_after_seconds(exc),
                )
                if retry_execution is not None
                else (transport.retry_delay_seconds or gateway_retry_delay_seconds)(
                    request_attempt,
                    failure_class=failure_class,
                    retry_after_seconds=transport.retry_after_seconds(exc),
                )
            )
            retry_elapsed_seconds = max(
                0.0,
                time.monotonic() - retry_started_at,
            )
            if (
                failure_class in CAPACITY_RETRY_FAILURE_CLASSES
                and not (
                    retry_execution.capacity_elapsed_limit_allows(
                        retry_elapsed_seconds,
                        delay_seconds,
                    )
                    if retry_execution is not None
                    else transport._capacity_allows(
                        retry_started_at,
                        delay_seconds,
                    )
                )
            ):
                raise
            _require_retry_delay_within_pre_response_budget(
                pre_response_deadline,
                delay_seconds,
                phase="retry_delay",
                attempt=request_attempt,
            )
            transport.emit_retry(
                event_context,
                upstream_name=upstream_name,
                upstream_format=upstream_format,
                request_kind=request_kind,
                attempt=request_attempt,
                max_attempts=error_retry_budget,
                exc=exc,
                delay_seconds=delay_seconds,
                failure_class=failure_class,
                failure_phase=telemetry_failure_phase,
                retry_safety_class=retry_safety_class,
            )
            if downstream_retry_callback is not None and retry_policy != RETRY_CONSERVATIVE_PRE_OUTPUT:
                if not downstream_retry_callback(
                    transport._retry_payload(
                        upstream_name=upstream_name,
                        upstream_format=upstream_format,
                        request_kind=request_kind,
                        attempt=request_attempt,
                        max_attempts=error_retry_budget,
                        exc=exc,
                        failure_phase=telemetry_failure_phase,
                        delay_seconds=delay_seconds,
                        failure_class=failure_class,
                        redact_identity=transport._retry_identity(event_context),
                    )
                ):
                    raise transport.facts.downstream_closed_before_retry_error("downstream closed before upstream retry")
            (transport.sleep or sleep_for_retry_with_gateway_cancellation)(delay_seconds)
            attempt += 1



class UpstreamSseReaderLifecycle:
    """Owns one upstream SSE reader thread, a bounded queue, and deterministic close/join.

    This lifecycle is the single owner of the thread that reads raw SSE lines from
    an upstream response. It uses a bounded queue (capacity 32) so a slow or stalled
    downstream cannot create unbounded buffering. The producer observes close while
    waiting on a full queue; the consumer observes close while waiting on an empty
    queue. Close is idempotent and wakes both sides. Join is bounded and classifies a
    non-terminating reader without hiding it.
    """

    QUEUE_CAPACITY = 32
    PRODUCER_PUT_TIMEOUT_SECONDS = 0.05
    CONSUMER_POLL_TIMEOUT_SECONDS = 0.1
    JOIN_TIMEOUT_SECONDS = 1.0
    default_logger_provider: Callable[[], logging.Logger] = lambda: logger

    def __init__(
        self,
        response: Any,
        *,
        admission: RequestAdmission | None = None,
        cancellation_requested: Callable[[], bool] | None = None,
        thread_name: str = "gateway-sse-reader",
        logger_hook: logging.Logger | Callable[[], logging.Logger] | None = None,
    ) -> None:
        self._response = response
        self._queue: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=self.QUEUE_CAPACITY)
        self._closed = threading.Event()
        self._close_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._thread_name = thread_name
        selected_logger = logger_hook if logger_hook is not None else type(self).default_logger_provider
        self._logger = selected_logger() if callable(selected_logger) else selected_logger
        self._cancellation_requested = (
            (lambda: admission.cancelled) if admission is not None else cancellation_requested
        )
        self._started = False
        self._response_closed = False
        self._join_outcome: str | None = None
        if admission is not None:
            admission.attach_upstream_transport(self)

    @property
    def closed(self) -> bool:
        return self._closed.is_set()

    @property
    def queue_depth(self) -> int:
        return self._queue.qsize()

    @property
    def reader_alive(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    @property
    def join_outcome(self) -> str | None:
        return self._join_outcome

    def _cancelled(self) -> bool:
        if self._closed.is_set():
            return True
        cancellation_requested = self._cancellation_requested
        if cancellation_requested is None:
            return False
        try:
            return bool(cancellation_requested())
        except Exception:
            return False

    def start(self) -> None:
        """Start the reader thread idempotently."""
        with self._close_lock:
            if self._started or self._closed.is_set():
                return
            self._started = True
            thread = threading.Thread(target=self._read_upstream, name=self._thread_name, daemon=True)
            self._thread = thread
        thread.start()

    def _read_upstream(self) -> None:
        """Producer loop: read lines and enqueue them with bounded backpressure."""
        response = self._response
        try:
            while not self._cancelled():
                try:
                    line = response.readline()
                except BaseException as exc:
                    if not self._cancelled():
                        self._enqueue(("error", exc))
                    return
                if not self._enqueue(("line", line)):
                    return
                if not line:
                    return
        finally:
            self.close()

    def _enqueue(self, item: tuple[str, Any]) -> bool:
        """Enqueue one item, respecting close/cancellation. Returns False if closed."""
        while not self._cancelled():
            try:
                self._queue.put(item, timeout=self.PRODUCER_PUT_TIMEOUT_SECONDS)
                return True
            except queue.Full:
                continue
        return False

    def get(self, timeout: float | None = None) -> tuple[str, Any]:
        """Get one queued item.

        Raises ``queue.Empty`` on timeout. The caller should check :attr:`closed`
        to distinguish a transient empty queue from a closed lifecycle.
        """
        self.start()
        if timeout is not None:
            try:
                return self._queue.get(timeout=max(0.0, timeout))
            except queue.Empty:
                if self._cancelled():
                    return "line", b""
                raise
        while True:
            try:
                return self._queue.get(timeout=self.CONSUMER_POLL_TIMEOUT_SECONDS)
            except queue.Empty:
                if self._cancelled():
                    return "line", b""

    def readline(self) -> bytes:
        """Read one upstream SSE line.

        Returns ``b""`` when the lifecycle is closed. Raises the stored upstream
        exception when the reader encountered an error.
        """
        self.start()
        while True:
            kind, value = self.get()
            if kind == "error":
                raise value
            return value

    def iter_lines(self):
        """Yield raw upstream SSE lines until EOF or close."""
        try:
            while True:
                line = self.readline()
                yield line
                if not line:
                    return
        finally:
            self.close()

    def shorten_terminal_drain_timeout(self, timeout_seconds: float) -> None:
        """Forward the existing pooled-response terminal drain optimization."""
        shorten = getattr(self._response, "shorten_terminal_drain_timeout", None)
        if callable(shorten):
            shorten(timeout_seconds)

    def close(self) -> None:
        """Close the lifecycle idempotently and wake both producer and consumer."""
        with self._close_lock:
            self._closed.set()
            should_close_response = not self._response_closed
            self._response_closed = True
        if should_close_response:
            close = getattr(self._response, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:
                    pass

    def join(self, timeout: float = JOIN_TIMEOUT_SECONDS) -> tuple[bool, str | None]:
        """Join the reader thread for at most ``timeout`` seconds.

        Returns ``(joined, outcome)``. A started reader receives a sanitized
        termination classification; ``outcome`` is ``None`` only when no reader
        was started. Once a join timeout is observed, that classification remains
        retained even if a later join observes termination.
        """
        thread = self._thread
        if thread is None:
            return True, None
        bounded_timeout = min(self.JOIN_TIMEOUT_SECONDS, max(0.0, timeout))
        thread.join(timeout=bounded_timeout)
        if thread.is_alive():
            outcome = "upstream_sse_reader_thread_did_not_terminate"
            if self._join_outcome != outcome:
                self._logger.warning("upstream SSE reader join ended with %s", outcome)
            self._join_outcome = outcome
            return False, self._join_outcome
        if self._join_outcome is None:
            self._join_outcome = "upstream_sse_reader_thread_terminated"
        return True, self._join_outcome


_UpstreamSseReaderLifecycle = UpstreamSseReaderLifecycle


# ---------------------------------------------------------------------------
# Retry-safety orchestration for non-Official main-generation POSTs.
# ---------------------------------------------------------------------------


def retry_identity_from_context(event_context: Mapping[str, Any] | None) -> str | None:
    """Return the private stable retry identity if one exists in the context."""
    if event_context is None:
        return None
    identity = event_context.get("_retry_attempt_identity")
    return identity if isinstance(identity, str) and identity else None
_retry_identity_from_context = retry_identity_from_context


def _retry_safety_failure_phase(exc: BaseException | None) -> str | None:
    """Phase label used for the request-scoped retry-safety decision.

    This function is conservative: it returns a pre-write phase only when the
    exception itself is a structurally unambiguous instance of that phase.
    Generic TimeoutError, SSLError, OSError, and URLError are ambiguous (they
    can occur during connect, request write, or body read) and therefore return
    ``None`` so the caller-supplied ``failure_phase`` or the ``unknown`` safety
    class is used.  Only specifically inspected exception types carry
    authoritative phase evidence: socket.gaierror (DNS), ConnectionRefusedError
    (TCP connect), and ssl.SSLCertVerificationError (TLS handshake).  Text
    needles in OS-level error messages are never treated as proof because the
    same message can occur after the request has been written.
    """
    if exc is None:
        return None
    explicit_phase = _explicit_transport_phase(exc)
    if explicit_phase is not None:
        return explicit_phase
    if isinstance(exc, (UpstreamStreamIncompleteError, _STREAM_ERROR_EVENT)):
        return "stream_body"
    if isinstance(exc, HTTPError):
        return "response_headers"
    if isinstance(exc, IncompleteRead):
        return "response_headers"
    if isinstance(exc, socket.gaierror):
        return "dns"
    if isinstance(exc, ConnectionRefusedError):
        return "tcp_connect"
    if isinstance(exc, ssl.SSLCertVerificationError):
        return "tls_handshake"
    if isinstance(exc, URLError):
        nested = _retry_safety_failure_phase(exc.reason)
        if nested is not None:
            return nested
    return None


def _model_access_path_idempotency_guaranteed(model_access_path: tuple[str, ...]) -> bool:
    """Return True when the exact Model Access Path carries an explicit guarantee.

    The default allowlist is empty: no non-Official main-generation POST is
    assumed idempotent unless it is explicitly enrolled here.  This keeps the
    conservative default while providing a single seam for future guarantees.
    """
    return False


def model_access_path_from_event_context(
    event_context: Mapping[str, Any] | None,
    upstream_name: str,
    upstream_format: str,
) -> tuple[str, ...]:
    model = ""
    behavior_profile = ""
    inbound_format = ""
    if event_context is not None:
        try:
            model = str(event_context.get("model") or "")
        except Exception:
            model = ""
        try:
            behavior_profile = str(event_context.get("behavior_profile") or "")
        except Exception:
            behavior_profile = ""
        try:
            inbound_format = str(event_context.get("_caller_wire_format") or "")
        except Exception:
            inbound_format = ""
    return (upstream_name, model, behavior_profile, upstream_format, inbound_format)
_model_access_path_from_event_context = model_access_path_from_event_context


def _ensure_retry_attempt_identity(
    event_context: dict[str, Any] | None,
    request: Request,
    model_access_path: tuple[str, ...],
) -> str | None:
    """Return the stable attempt identity for this logical request.

    The identity is stored in the mutable event context under a private key so
    it is not emitted by public_event_context.  When the Model Access Path is
    explicitly idempotent, the identity is attached to the upstream request as
    an idempotency key; it is never logged, returned to the client, or placed
    in telemetry payloads.
    """
    if event_context is None:
        return None
    identity = event_context.get("_retry_attempt_identity")
    if not isinstance(identity, str):
        identity = uuid.uuid4().hex
        event_context["_retry_attempt_identity"] = identity
    if _model_access_path_idempotency_guaranteed(model_access_path):
        request.headers["X-CodexHub-Retry-Attempt-Identity"] = identity
    return identity


SUPPRESSED_RETRY_SAFETY_CLASSES = frozenset({
    RETRY_SAFETY_SUPPRESSED_POST_WRITE,
    RETRY_SAFETY_SUPPRESSED_POST_EXPOSURE,
    RETRY_SAFETY_UNKNOWN,
})
_SUPPRESSED_RETRY_SAFETY_CLASSES = SUPPRESSED_RETRY_SAFETY_CLASSES


def _retry_safety_class(
    exc: BaseException,
    *,
    request: Request,
    upstream_name: str,
    request_kind: str,
    downstream_exposed: bool,
    model_access_path: tuple[str, ...],
    failure_phase: str | None = None,
) -> str:
    """Classify whether this failure is safe to retry for a non-Official main-generation POST.

    Official main-generation POSTs and all other request kinds preserve their
    existing retry behavior and receive no classification here.

    Callers that already know the failure occurred during the response stream
    may pass ``failure_phase`` to override the exception-based heuristic; this
    is required to distinguish a connect-time ``TimeoutError`` from a body-read
    ``TimeoutError``.
    """
    if upstream_name == "official" or request_kind != RETRY_REQUEST_MAIN_GENERATION:
        return RETRY_SAFETY_SAFE_PREWRITE
    if request.get_method() != "POST":
        return RETRY_SAFETY_SAFE_PREWRITE
    if downstream_exposed:
        return RETRY_SAFETY_SUPPRESSED_POST_EXPOSURE
    if _model_access_path_idempotency_guaranteed(model_access_path):
        return RETRY_SAFETY_GUARANTEED_IDEMPOTENT
    phase = failure_phase if failure_phase is not None else _retry_safety_failure_phase(exc)
    if phase in {"dns", "tcp_connect", "tls_handshake"}:
        return RETRY_SAFETY_SAFE_PREWRITE
    if phase in {"request_write", "response_headers", "stream_body"}:
        return RETRY_SAFETY_SUPPRESSED_POST_WRITE
    return RETRY_SAFETY_UNKNOWN


def _emit_upstream_retry_suppressed_event(
    event_context: Mapping[str, Any] | None,
    *,
    upstream_name: str,
    upstream_format: str,
    request_kind: str,
    attempt: int,
    max_attempts: int,
    exc: BaseException,
    failure_class: str,
    failure_phase: str | None,
    retry_safety_class: str,
) -> None:
    identity = _retry_identity_from_context(event_context)
    detail = safe_upstream_error_detail(exc, redact_identity=identity)
    if isinstance(exc, _STREAM_ERROR_EVENT):
        detail = "Upstream SSE error event"
    gateway_events.write_failure_event(
        event_context,
        "upstream_retry_suppressed",
        upstream=upstream_name,
        provider_id=upstream_name,
        upstream_format=upstream_format,
        request_kind=request_kind,
        retryable=False,
        failure_class=failure_class,
        status=_upstream_retry_status(exc),
        attempt=attempt,
        max_attempts=max_attempts,
        delay_ms=0,
        error=type(exc).__name__,
        detail=detail,
        failure_phase=failure_phase or transport_failure_phase(exc),
        retry_safety_class=retry_safety_class,
        terminal=False,
        downstream_output_started=(
            retry_safety_class == RETRY_SAFETY_SUPPRESSED_POST_EXPOSURE
        ),
        retry_forbidden=True,
    )


def _emit_upstream_retry_event(
    event_context: Mapping[str, Any] | None,
    *,
    upstream_name: str,
    upstream_format: str,
    request_kind: str,
    attempt: int,
    max_attempts: int,
    exc: BaseException,
    delay_seconds: int,
    failure_class: str | None = None,
    failure_phase: str | None = None,
    retry_safety_class: str | None = None,
) -> None:
    identity = _retry_identity_from_context(event_context)
    resolved_failure_class = failure_class or _upstream_failure_class(exc)
    if isinstance(exc, _STREAM_ERROR_EVENT):
        detail = "Upstream SSE error event"
    else:
        detail = safe_upstream_error_detail(exc, redact_identity=identity)
    fields: dict[str, Any] = {
        "upstream": upstream_name,
        "provider_id": upstream_name,
        "upstream_format": upstream_format,
        "request_kind": request_kind,
        "retryable": True,
        "failure_class": resolved_failure_class,
        "status": _upstream_retry_status(exc),
        "attempt": attempt,
        "max_attempts": max_attempts,
        "delay_ms": delay_seconds * 1000,
        "error": type(exc).__name__,
        "detail": detail,
        "failure_phase": failure_phase or transport_failure_phase(exc),
    }
    if retry_safety_class is not None:
        fields["retry_safety_class"] = retry_safety_class
    gateway_events.write_failure_event(
        event_context,
        "upstream_retry",
        **fields,
    )


def _downstream_retry_payload(
    *,
    upstream_name: str,
    upstream_format: str,
    request_kind: str,
    attempt: int,
    max_attempts: int,
    exc: BaseException,
    delay_seconds: int,
    failure_class: str | None = None,
    failure_phase: str | None = None,
    redact_identity: str | None = None,
) -> dict[str, Any]:
    return {
        "type": "codexhub.retry",
        "upstream": upstream_name,
        "upstream_format": upstream_format,
        "request_kind": request_kind,
        "failure_class": failure_class or _upstream_failure_class(exc),
        "status": _upstream_retry_status(exc),
        "attempt": attempt,
        "max_attempts": max_attempts,
        "delay_ms": delay_seconds * 1000,
        "error": type(exc).__name__,
        "detail": safe_upstream_error_detail(exc, redact_identity=redact_identity),
        "failure_phase": failure_phase or transport_failure_phase(exc),
    }


def _diagnostic_context_value(event_context: Mapping[str, Any] | None, key: str) -> Any:
    """Read optional diagnostic context without changing a request on failure."""

    if event_context is None:
        return None
    try:
        return event_context.get(key)
    except Exception:
        return None


def default_gateway_transport() -> GatewayTransport:
    """Build a request-time transport seam that reads owning-module attributes.

    A new adapter is constructed per request so monkeypatches on the owning
    modules (codex_auth tokens, gateway_events diagnostics, gateway_settings
    retry knobs, and this module's retry hooks) stay live.
    """

    import codex_auth
    import route_plan
    import gateway_settings as _settings

    module = sys.modules[__name__]
    stream_error_event = _STREAM_ERROR_EVENT

    return GatewayTransport(
        facts=TransportFacts(
            suppressed_retry_safety_classes=module._SUPPRESSED_RETRY_SAFETY_CLASSES,
            downstream_closed_before_retry_error=_downstream_closed_before_retry_error(),
        ),
        active_request=lambda: gateway_admission.active_gateway_request(),
        access_token=lambda: codex_auth.access_token(),
        account_id=lambda: codex_auth.account_id(),
        diagnostic_recorder=gateway_events.GATEWAY_DIAGNOSTIC_RECORDER,
        diagnostic_context_value=module._diagnostic_context_value,
        emit_retry=module._emit_upstream_retry_event,
        emit_retry_suppressed=module._emit_upstream_retry_suppressed_event,
        retry_delay_seconds=lambda *args, **kwargs: _settings.gateway_retry_delay_seconds(*args, **kwargs),
        retry_safety_class=module._retry_safety_class,
        retry_safety_failure_phase=module._retry_safety_failure_phase,
        model_access_path=module._model_access_path_from_event_context,
        model_access_path_idempotent=module._model_access_path_idempotency_guaranteed,
        ensure_retry_identity=module._ensure_retry_attempt_identity,
        retry_identity_from_context=module._retry_identity_from_context,
        downstream_retry_payload=module._downstream_retry_payload,
        upstream_retry_attempts=lambda kind: _settings._upstream_retry_attempts(kind),
        endpoint_url_hook=lambda upstream, path: route_plan._upstream_endpoint_url(upstream, path),
    )


def _downstream_closed_before_retry_error() -> type[BaseException]:
    import gateway_stream_semantics

    return gateway_stream_semantics.DownstreamClosedBeforeRetryError


def open_upstream_once(
    request: Request,
    *,
    upstream_name: str,
    timeout: int | float,
    transport_policy: TransportPolicy | None = None,
) -> Any:
    return default_gateway_transport().open_once(
        request,
        upstream_name=upstream_name,
        timeout=timeout,
        transport_policy=transport_policy,
    )


def open_upstream_response(
    request: Request,
    *,
    upstream_name: str,
    upstream_format: str,
    timeout: int | float,
    event_context: Mapping[str, Any] | None = None,
    downstream_retry_callback: Any = None,
    request_kind: str = RETRY_REQUEST_MAIN_GENERATION,
    max_attempts: int | None = None,
    retry_policy: str = RETRY_GATEWAY_FULL,
    retry_http_errors: bool = True,
    retry_execution: RetryExecutionPlanLike | None = None,
    transport_policy: TransportPolicy | None = None,
    downstream_exposed: Callable[[], bool] | None = None,
    pre_response_deadline: float | None = None,
    open_attempt_budget: dict[str, int] | None = None,
) -> Any:
    return default_gateway_transport().open_response(
        request,
        upstream_name=upstream_name,
        upstream_format=upstream_format,
        timeout=timeout,
        event_context=event_context,
        downstream_retry_callback=downstream_retry_callback,
        request_kind=request_kind,
        max_attempts=max_attempts,
        retry_policy=retry_policy,
        retry_http_errors=retry_http_errors,
        retry_execution=retry_execution,
        transport_policy=transport_policy,
        downstream_exposed=downstream_exposed,
        pre_response_deadline=pre_response_deadline,
        open_attempt_budget=open_attempt_budget,
    )


def upstream_headers(
    incoming_headers: Mapping[str, str] | Any,
    upstream: Mapping[str, Any],
    drop_content_encoding: bool = False,
    behavior_profile: str | None = None,
    model_id: str | None = None,
    authentication_strategy: AuthenticationStrategy | None = None,
    request_mutation_policy: MutationPolicy | None = None,
    operational_authentication: OperationalAuthentication | None = None,
) -> dict[str, str]:
    return default_gateway_transport().build_headers(
        incoming_headers,
        upstream,
        drop_content_encoding=drop_content_encoding,
        behavior_profile=behavior_profile,
        model_id=model_id,
        authentication_strategy=authentication_strategy,
        request_mutation_policy=request_mutation_policy,
        operational_authentication=operational_authentication,
    )


def bind_route_plan_operational_authentication(
    plan: Any,
    incoming_headers: Mapping[str, str] | Any,
    upstream: Mapping[str, Any],
    operational_authentication: OperationalAuthentication,
    *,
    drop_content_encoding: bool = False,
) -> Any:
    """Return a new plan whose attempts freeze one request-scoped auth snapshot."""

    from dataclasses import replace

    from route_primitives import FrozenRequestHeaders

    if not plan.attempts:
        raise ValueError(
            "cannot materialize authentication for a route plan without attempts"
        )
    primary_attempt = plan.attempts[0]
    if any(
        attempt.request_headers.materialized
        for attempt in plan.attempts
    ):
        raise ValueError("route attempt authentication was already materialized")
    if any(
        attempt.authentication_strategy
        != operational_authentication.strategy
        or attempt.request_mutation_policy
        != primary_attempt.request_mutation_policy
        for attempt in plan.attempts
    ):
        raise ValueError(
            "route attempts do not share one authentication/header policy"
        )
    request_headers = FrozenRequestHeaders(
        upstream_headers(
            incoming_headers,
            upstream,
            drop_content_encoding=drop_content_encoding,
            model_id=plan.canonical_model or plan.model_requested,
            authentication_strategy=primary_attempt.authentication_strategy,
            request_mutation_policy=primary_attempt.request_mutation_policy,
            operational_authentication=operational_authentication,
        ),
        materialized=True,
    )
    return replace(
        plan,
        attempts=tuple(
            replace(attempt, request_headers=request_headers)
            for attempt in plan.attempts
        ),
    )


# Public aliases matching the former facade surface.
OfficialHTTPSConnection = _OfficialHTTPSConnection
OfficialHTTPSConnectionPool = _OfficialHTTPSConnectionPool
OfficialPooledResponse = _OfficialPooledResponse
TRANSPORT_PHASE_ATTRIBUTE = _TRANSPORT_PHASE_ATTRIBUTE
explicit_transport_phase = _explicit_transport_phase
connection_disposition = _connection_disposition
set_official_attempt_connection_disposition = _set_official_attempt_connection_disposition
diagnostic_connection_disposition = _diagnostic_connection_disposition_value
diagnostic_error_connection_disposition = _diagnostic_error_connection_disposition_value
