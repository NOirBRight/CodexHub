"""Shared urllib3 connection-pool machinery for Official and STANDARD transports.

Official and third-party registries stay separate. This module owns connection
classes, idle expiry, write-budget tagging, and pooled response wrapping.
"""

from __future__ import annotations

import socket
import ssl
import sys
import threading
import time
from http.client import IncompleteRead
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request

VENDOR_DIR = Path(__file__).resolve().parent / "vendor"
VENDORED_URLLIB3_WHEEL = VENDOR_DIR / "urllib3-2.7.0-py3-none-any.whl"
if not VENDORED_URLLIB3_WHEEL.is_file():
    raise RuntimeError(
        f"missing pinned Gateway transport dependency: {VENDORED_URLLIB3_WHEEL}"
    )
if str(VENDORED_URLLIB3_WHEEL) not in sys.path:
    sys.path.insert(0, str(VENDORED_URLLIB3_WHEEL))

import urllib3

POOL_MAX_CONNECTIONS = 16
POOL_MAX_IDLE_SECONDS = 30.0
PROXY_POOL_MAX_IDLE_SECONDS = 300.0
CONNECT_TIMEOUT_SECONDS = 15.0
TERMINAL_DRAIN_TIMEOUT_SECONDS = 1.0
TCP_KEEPALIVE_IDLE_MS = 5000
TCP_KEEPALIVE_INTERVAL_MS = 5000
_ATTEMPT_CONNECTION_STATE = threading.local()
_REQUEST_WRITE_DEADLINE_ATTRIBUTE = "_codexhub_request_write_deadline"
_REQUEST_WRITE_ACTIVE_ATTRIBUTE = "_codexhub_request_write_active"
_REQUEST_WRITE_ERROR_ATTRIBUTE = "_codexhub_request_write_error"
TRANSPORT_PHASE_ATTRIBUTE = "_codexhub_transport_phase"
_SOCKET_TIMEOUT_UNSET = object()
_SUPPORTED_TRANSPORT_PHASES = {
    "dns",
    "tcp_connect",
    "tls_handshake",
    "request_write",
    "response_headers",
    "response_body",
    "stream_body",
}


def pooled_socket_options() -> list[tuple[int, int, int]]:
    options = list(urllib3.connection.HTTPConnection.default_socket_options)
    options.append((socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1))
    if not sys.platform.startswith("win"):
        if hasattr(socket, "TCP_KEEPIDLE"):
            options.append(
                (
                    socket.IPPROTO_TCP,
                    socket.TCP_KEEPIDLE,
                    max(1, TCP_KEEPALIVE_IDLE_MS // 1000),
                )
            )
        if hasattr(socket, "TCP_KEEPINTVL"):
            options.append(
                (
                    socket.IPPROTO_TCP,
                    socket.TCP_KEEPINTVL,
                    max(1, TCP_KEEPALIVE_INTERVAL_MS // 1000),
                )
            )
        if hasattr(socket, "TCP_KEEPCNT"):
            options.append((socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 3))
    return options


def configure_windows_keepalive(sock: Any) -> None:
    if sys.platform.startswith("win") and hasattr(socket, "SIO_KEEPALIVE_VALS"):
        sock.ioctl(
            socket.SIO_KEEPALIVE_VALS,
            (1, TCP_KEEPALIVE_IDLE_MS, TCP_KEEPALIVE_INTERVAL_MS),
        )


def tag_transport_phase(exc: BaseException, phase: str) -> None:
    try:
        setattr(exc, TRANSPORT_PHASE_ATTRIBUTE, phase)
    except Exception:
        pass


def reset_attempt_state(timeout: float) -> None:
    _ATTEMPT_CONNECTION_STATE.disposition = "unobserved"
    _ATTEMPT_CONNECTION_STATE.request_write_deadline = time.monotonic() + timeout


def set_attempt_connection_disposition(disposition: str) -> None:
    if disposition in {"new", "reused"}:
        _ATTEMPT_CONNECTION_STATE.disposition = disposition


def attempt_connection_disposition() -> str:
    disposition = getattr(_ATTEMPT_CONNECTION_STATE, "disposition", "unobserved")
    return disposition if disposition in {"new", "reused"} else "unobserved"


def attempt_request_write_deadline() -> float | None:
    deadline = getattr(_ATTEMPT_CONNECTION_STATE, "request_write_deadline", None)
    return deadline if isinstance(deadline, (int, float)) else None


def clear_attempt_state() -> None:
    for attribute in ("disposition", "request_write_deadline"):
        try:
            delattr(_ATTEMPT_CONNECTION_STATE, attribute)
        except AttributeError:
            pass


def connection_disposition(connection: Any) -> str:
    try:
        disposition = getattr(
            connection, "_codexhub_diagnostic_connection_disposition", "unobserved"
        )
    except Exception:
        return "unobserved"
    return disposition if disposition in {"new", "reused"} else "unobserved"


def explicit_transport_phase(exc: BaseException | None) -> str | None:
    pending: list[Any] = [exc]
    seen: set[int] = set()
    while pending:
        candidate = pending.pop(0)
        if not isinstance(candidate, BaseException) or id(candidate) in seen:
            continue
        seen.add(id(candidate))
        try:
            phase = getattr(candidate, TRANSPORT_PHASE_ATTRIBUTE, None)
        except Exception:
            phase = None
        if phase in _SUPPORTED_TRANSPORT_PHASES:
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


def propagate_transport_metadata(
    target: BaseException,
    *,
    source: BaseException | None = None,
    disposition: str | None = None,
    phase: str | None = None,
) -> BaseException:
    resolved_phase = (
        phase if phase in _SUPPORTED_TRANSPORT_PHASES else explicit_transport_phase(source)
    )
    if resolved_phase is not None:
        try:
            setattr(target, TRANSPORT_PHASE_ATTRIBUTE, resolved_phase)
        except Exception:
            pass
    if disposition in {"new", "reused"}:
        try:
            setattr(target, "_codexhub_diagnostic_connection_disposition", disposition)
        except Exception:
            pass
    return target


def stdlib_transport_error(exc: BaseException) -> BaseException:
    pending: list[Any] = [exc]
    seen: set[int] = set()
    while pending:
        candidate = pending.pop(0)
        if not isinstance(candidate, BaseException) or id(candidate) in seen:
            continue
        seen.add(id(candidate))
        if isinstance(
            candidate,
            (ssl.SSLError, TimeoutError, ConnectionError, OSError, IncompleteRead),
        ):
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


def pool_origin(url: str) -> str:
    parsed = urlsplit(url)
    scheme = (parsed.scheme or "https").lower()
    host = (parsed.hostname or "").lower()
    port = parsed.port
    if port is None:
        port = 443 if scheme == "https" else 80 if scheme == "http" else 0
    return f"{scheme}://{host}:{port}"


def standard_pool_key(url: str, proxy_url: str | None) -> str:
    return f"{proxy_url or 'direct'}|{pool_origin(url)}"


class _PooledConnectionMixin:
    def connect(self) -> None:
        super().connect()  # type: ignore[misc]
        sock = getattr(self, "sock", None)
        if sock is not None:
            configure_windows_keepalive(sock)

    def endheaders(
        self, message_body: Any = None, *, encode_chunked: bool = False
    ) -> None:
        setattr(self, _REQUEST_WRITE_ACTIVE_ATTRIBUTE, True)
        super().endheaders(message_body=message_body, encode_chunked=encode_chunked)  # type: ignore[misc]

    def send(self, data: Any) -> None:
        request_write_active = getattr(self, _REQUEST_WRITE_ACTIVE_ATTRIBUTE, False)
        sock = getattr(self, "sock", None)
        previous_timeout: Any = _SOCKET_TIMEOUT_UNSET
        if request_write_active and sock is not None:
            gettimeout = getattr(sock, "gettimeout", None)
            if callable(gettimeout):
                try:
                    previous_timeout = gettimeout()
                except Exception:
                    previous_timeout = _SOCKET_TIMEOUT_UNSET
        try:
            deadline = getattr(self, _REQUEST_WRITE_DEADLINE_ATTRIBUTE, None)
            if request_write_active and isinstance(deadline, (int, float)):
                remaining_timeout = deadline - time.monotonic()
                if remaining_timeout <= 0:
                    raise TimeoutError("Upstream request write budget exhausted")
                if sock is not None:
                    sock.settimeout(remaining_timeout)
            super().send(data)  # type: ignore[misc]
        except BaseException as exc:
            if request_write_active:
                tag_transport_phase(exc, "request_write")
                try:
                    setattr(self, _REQUEST_WRITE_ERROR_ATTRIBUTE, exc)
                except Exception:
                    pass
            raise
        finally:
            if (
                request_write_active
                and sock is not None
                and previous_timeout is not _SOCKET_TIMEOUT_UNSET
            ):
                try:
                    sock.settimeout(previous_timeout)
                except Exception:
                    pass


class PooledHTTPSConnection(_PooledConnectionMixin, urllib3.connection.HTTPSConnection):
    pass


class PooledHTTPConnection(_PooledConnectionMixin, urllib3.connection.HTTPConnection):
    pass


class _PooledConnectionPoolMixin:
    def _make_request(self, conn: Any, *args: Any, **kwargs: Any) -> Any:
        request_write_deadline = attempt_request_write_deadline()
        if request_write_deadline is None:
            timeout = kwargs.get("timeout")
            request_write_timeout = getattr(timeout, "read_timeout", timeout)
            if (
                isinstance(request_write_timeout, (int, float))
                and request_write_timeout > 0
            ):
                request_write_deadline = time.monotonic() + request_write_timeout
        try:
            setattr(conn, _REQUEST_WRITE_DEADLINE_ATTRIBUTE, request_write_deadline)
            return super()._make_request(conn, *args, **kwargs)  # type: ignore[misc]
        except BaseException as exc:
            if getattr(conn, _REQUEST_WRITE_ERROR_ATTRIBUTE, None) is not None:
                tag_transport_phase(exc, "request_write")
            raise
        finally:
            try:
                delattr(conn, _REQUEST_WRITE_DEADLINE_ATTRIBUTE)
            except AttributeError:
                pass
            try:
                delattr(conn, _REQUEST_WRITE_ACTIVE_ATTRIBUTE)
            except AttributeError:
                pass
            try:
                delattr(conn, _REQUEST_WRITE_ERROR_ATTRIBUTE)
            except AttributeError:
                pass

    def _get_conn(self, timeout: float | None = None) -> Any:
        connection = super()._get_conn(timeout)  # type: ignore[misc]
        released_at = getattr(connection, "_codexhub_released_at", None)
        try:
            disposition = (
                "reused"
                if isinstance(released_at, (int, float))
                and getattr(connection, "sock", None)
                else "new"
            )
        except Exception:
            disposition = "unobserved"
        idle_seconds = (
            time.monotonic() - released_at if isinstance(released_at, (int, float)) else None
        )
        max_idle_seconds = (
            PROXY_POOL_MAX_IDLE_SECONDS
            if getattr(self, "proxy", None) is not None
            else POOL_MAX_IDLE_SECONDS
        )
        if idle_seconds is not None and idle_seconds >= max_idle_seconds:
            connection.close()
            disposition = "new"
        try:
            connection._codexhub_diagnostic_connection_disposition = disposition
        except Exception:
            pass
        set_attempt_connection_disposition(disposition)
        return connection

    def _put_conn(self, connection: Any) -> None:
        if connection is not None:
            connection._codexhub_released_at = time.monotonic()
        super()._put_conn(connection)  # type: ignore[misc]


class PooledHTTPSConnectionPool(
    _PooledConnectionPoolMixin, urllib3.connectionpool.HTTPSConnectionPool
):
    ConnectionCls = PooledHTTPSConnection


class PooledHTTPConnectionPool(
    _PooledConnectionPoolMixin, urllib3.connectionpool.HTTPConnectionPool
):
    ConnectionCls = PooledHTTPConnection


class PooledResponse:
    def __init__(self, response: Any):
        self._response = response
        self._exhausted = False
        self._released = False
        self.status = response.status
        self.reason = response.reason
        self.headers = response.headers
        self.connection_disposition = connection_disposition(
            getattr(response, "connection", None)
        )
        self._terminal_drain_socket: Any = None
        self._terminal_drain_original_timeout: float | None = None

    def read(self, amount: int | None = None) -> bytes:
        try:
            data = self._response.read(amount)
        except (urllib3.exceptions.HTTPError, OSError, IncompleteRead) as exc:
            translated = stdlib_transport_error(exc)
            propagate_transport_metadata(
                translated,
                source=exc,
                disposition=self.connection_disposition,
                phase=explicit_transport_phase(exc) or "response_body",
            )
            raise translated from exc
        if amount is None or data == b"":
            self._exhausted = True
        return data

    def readline(self, limit: int = -1) -> bytes:
        try:
            data = self._response.readline(limit)
        except (urllib3.exceptions.HTTPError, OSError, IncompleteRead) as exc:
            translated = stdlib_transport_error(exc)
            propagate_transport_metadata(
                translated,
                source=exc,
                disposition=self.connection_disposition,
                phase=explicit_transport_phase(exc) or "stream_body",
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

    def __enter__(self) -> "PooledResponse":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        self.close()
        return False


def attach_pool_classes(manager: Any, *, include_http: bool) -> None:
    classes = {
        **manager.pool_classes_by_scheme,
        "https": PooledHTTPSConnectionPool,
    }
    if include_http:
        classes["http"] = PooledHTTPConnectionPool
    manager.pool_classes_by_scheme = classes


def create_pool_manager(
    *,
    proxy_url: str | None,
    socket_options: list[tuple[int, int, int]] | None = None,
    include_http: bool = False,
) -> Any:
    pool_options = {
        "num_pools": 4,
        "maxsize": POOL_MAX_CONNECTIONS,
        "block": True,
        "retries": False,
        "socket_options": (
            socket_options if socket_options is not None else pooled_socket_options()
        ),
    }
    manager = (
        urllib3.ProxyManager(proxy_url, **pool_options)
        if proxy_url is not None
        else urllib3.PoolManager(**pool_options)
    )
    attach_pool_classes(manager, include_http=include_http)
    return manager


def standard_pool_manager(
    url: str,
    *,
    pools: dict[str, Any],
    pools_lock: threading.Lock,
    proxy_url: str | None,
    socket_options: list[tuple[int, int, int]] | None = None,
) -> Any:
    pool_key = standard_pool_key(url, proxy_url)
    existing = pools.get(pool_key)
    if existing is not None:
        return existing
    with pools_lock:
        existing = pools.get(pool_key)
        if existing is None:
            existing = create_pool_manager(
                proxy_url=proxy_url,
                socket_options=socket_options,
                include_http=True,
            )
            pools[pool_key] = existing
        return existing


def execute_pooled_urlopen(
    request: Request,
    *,
    timeout: float,
    manager: Any,
) -> Any:
    headers = {
        key: value
        for key, value in request.header_items()
        if key.lower() != "connection"
    }
    try:
        response = manager.request(
            request.get_method(),
            request.full_url,
            body=request.data,
            headers=headers,
            preload_content=False,
            decode_content=False,
            redirect=False,
            retries=False,
            timeout=urllib3.Timeout(
                connect=min(timeout, CONNECT_TIMEOUT_SECONDS), read=timeout
            ),
            pool_timeout=timeout,
        )
    except (urllib3.exceptions.HTTPError, OSError, IncompleteRead) as exc:
        translated = stdlib_transport_error(exc)
        propagate_transport_metadata(
            translated,
            source=exc,
            disposition=attempt_connection_disposition(),
            phase=explicit_transport_phase(exc)
            or (
                "response_headers"
                if isinstance(exc, urllib3.exceptions.ReadTimeoutError)
                else None
            ),
        )
        raise translated from exc

    pooled_response = PooledResponse(response)
    if response.status >= 400:
        error = HTTPError(
            request.full_url,
            response.status,
            str(response.reason or "upstream error"),
            response.headers,
            pooled_response,
        )
        propagate_transport_metadata(
            error,
            disposition=pooled_response.connection_disposition,
        )
        raise error
    return pooled_response
