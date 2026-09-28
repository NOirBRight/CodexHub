#!/usr/bin/env python3
"""Loopback settings page for the CodexHub-managed ChatGPT Web Runtime."""

from __future__ import annotations

from python_runtime_contract import require_python_313

require_python_313(__file__)

import argparse
import hmac
import io
import json
import secrets
import sys
import threading
import time
import zipfile
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import chatgpt_web_checks
import chatgpt_web_runtime as runtime

_HOST = "127.0.0.1"
_SESSION_HEADER = "Authorization"
_MAX_BODY_BYTES = 128 * 1024
_BOOTSTRAP_TTL_SECONDS = 90
_SESSION_TTL_SECONDS = 15 * 60
_SESSION_MAX_AGE_SECONDS = 2 * 60 * 60
_SERVER_IDLE_SECONDS = 15 * 60
_SERVER_MAX_AGE_SECONDS = 2 * 60 * 60


def _safe_reason(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) > 80:
        return None
    if not value or not all(character.islower() or character.isdigit() or character == "_" for character in value):
        return None
    return value


def _public_readiness(value: dict[str, Any]) -> dict[str, Any]:
    """Whitelist account-bound check fields safe for the local settings page."""
    browser = value.get("browser") if isinstance(value.get("browser"), dict) else {}
    login = value.get("login") if isinstance(value.get("login"), dict) else {}
    connector = value.get("connector") if isinstance(value.get("connector"), dict) else {}
    tunnel = value.get("tunnel") if isinstance(value.get("tunnel"), dict) else {}
    return {
        "state": _safe_reason(value.get("state")) or "unchecked",
        "cache_state": _safe_reason(value.get("cache_state")) or "missing",
        "checked_at": value.get("checked_at") if isinstance(value.get("checked_at"), str) else None,
        "browser": {"state": _safe_reason(browser.get("state")) or "not_checked"},
        "login": {
            "state": _safe_reason(login.get("state")) or "not_checked",
            "capabilities": {
                key: item is True
                for key, item in (login.get("capabilities") or {}).items()
                if key in {"solAvailable", "extraHighAvailable", "proAvailable"}
                and isinstance(item, bool)
            } if isinstance(login.get("capabilities"), dict) else {},
        },
        "connector": {
            "state": _safe_reason(connector.get("state")) or "not_checked",
            "name": connector.get("name") if isinstance(connector.get("name"), str) else None,
        },
        "tunnel": {"state": _safe_reason(tunnel.get("state")) or "not_checked"},
        "text_ready": value.get("text_ready") is True,
        "tools_ready": value.get("tools_ready") is True,
        "reason": _safe_reason(value.get("reason")),
    }


def _login_blocked_reason(status: dict[str, Any]) -> str | None:
    if status.get("installed") and (status.get("component") or {}).get("compatible") is False:
        return "component_upgrade_required"
    if (status.get("process") or {}).get("running") and (
        status.get("restart_required")
        or (status.get("login") or {}).get("control") != runtime.LOGIN_CONTROL
    ):
        return "component_restart_required"
    return None


def _public_status(home: Path) -> dict[str, Any]:
    settings = runtime.read_settings(home)
    status = runtime.build_status(home)
    readiness = _public_readiness(chatgpt_web_checks.cached_checks(home))
    component = status.get("component") if isinstance(status.get("component"), dict) else {}
    process = status.get("process") if isinstance(status.get("process"), dict) else {}
    login = status.get("login") if isinstance(status.get("login"), dict) else {}
    browser = status.get("browser_smoke") if isinstance(status.get("browser_smoke"), dict) else {}
    tunnel = status.get("tunnel") if isinstance(status.get("tunnel"), dict) else {}
    connector = status.get("connector") if isinstance(status.get("connector"), dict) else {}
    active = settings.get("active") if isinstance(settings.get("active"), dict) else {}
    active_mode = active.get("mode") if active.get("mode") in {"browser-only", "full"} else None
    disabled = status.get("disabled") is True
    restart_required = status.get("restart_required") is True
    settings_pending_restart = settings.get("pending_restart") is True
    running = process.get("running") is True
    compatible = component.get("compatible") is True
    installed = status.get("installed") is True
    serving = (
        installed
        and compatible
        and running
        and status.get("admitting") is True
        and not disabled
        and not restart_required
        and login.get("state") == "signed_in"
        and browser.get("state") == "passed"
    )
    checks_current = readiness.get("cache_state") == "current"
    readiness["text_ready"] = checks_current and serving and readiness["text_ready"]
    readiness["tools_ready"] = (
        checks_current
        and serving
        and active_mode == "full"
        and tunnel.get("state") == "ready"
        and connector.get("selectable") is True
        and readiness["tools_ready"]
    )
    return {
        "ok": True,
        "settings": settings,
        "runtime": {
            "installed": installed,
            "compatible": compatible,
            "running": running,
            "admitting": status.get("admitting") is True,
            "disabled": disabled,
            "restart_required": restart_required,
            "settings_pending_restart": settings_pending_restart,
            "active_mode": active_mode,
            "login_state": _safe_reason(login.get("state")) or "not_checked",
            "login_window_open": login.get("window") == "open",
            "login_error": _safe_reason(login.get("error")),
            "login_blocked_reason": _login_blocked_reason(status),
        },
        "readiness": readiness,
    }


def _read_request_json(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = handler.headers.get("Content-Length")
    if length is None or not length.isdecimal():
        raise ValueError("A valid Content-Length is required")
    size = int(length)
    if size > _MAX_BODY_BYTES:
        raise OverflowError("The settings request is too large")
    if handler.headers.get_content_type() != "application/json":
        raise ValueError("The settings request must use JSON")
    try:
        payload = json.loads(handler.rfile.read(size))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ValueError("The settings request contains invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("The settings request must be an object")
    return payload


class _SettingsHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(
        self,
        home: Path,
        *,
        port: int,
        bootstrap_ttl: float,
        session_ttl: float,
        idle_seconds: float,
    ):
        runtime.read_settings(home)
        self.home = Path(home).expanduser().resolve()
        page_path = Path(__file__).with_name("chatgpt_web_settings.html")
        try:
            self.page = page_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeError("ChatGPT Runtime Settings page is unavailable") from exc
        self.bootstrap_token: str | None = secrets.token_urlsafe(32)
        self.bootstrap_expires_at = time.monotonic() + bootstrap_ttl
        self.sessions: dict[str, tuple[float, float]] = {}
        self.sessions_lock = threading.Lock()
        self.bootstrap_lock = threading.Lock()
        self.session_ttl = session_ttl
        self.activity_lock = threading.Lock()
        self.last_activity = time.monotonic()
        self.idle_seconds = idle_seconds
        super().__init__((_HOST, port), _SettingsHandler)
        self.timeout = 1

    @property
    def origin(self) -> str:
        return f"http://{_HOST}:{self.server_port}"

    def mark_activity(self) -> None:
        with self.activity_lock:
            self.last_activity = time.monotonic()

    def issue_session(self, bootstrap: str) -> str | None:
        if not bootstrap:
            return None
        with self.bootstrap_lock:
            now = time.monotonic()
            token = self.bootstrap_token
            if token is None or now > self.bootstrap_expires_at or not hmac.compare_digest(bootstrap, token):
                return None
            # Consume the capability before issuing a session to prevent replay.
            self.bootstrap_token = None
            session = secrets.token_urlsafe(32)
            with self.sessions_lock:
                self.sessions[session] = (
                    now + self.session_ttl,
                    now + _SESSION_MAX_AGE_SECONDS,
                )
        return session

    def session_valid(self, value: str | None) -> bool:
        if not value:
            return False
        now = time.monotonic()
        with self.sessions_lock:
            expiration = self.sessions.get(value)
            if expiration is None or now > expiration[0] or now > expiration[1]:
                self.sessions.pop(value, None)
                return False
            self.sessions[value] = (
                min(now + self.session_ttl, expiration[1]),
                expiration[1],
            )
            return True

    def serve_with_expiry(self) -> None:
        deadline = time.monotonic() + _SERVER_MAX_AGE_SECONDS
        while time.monotonic() < deadline:
            with self.activity_lock:
                idle = time.monotonic() - self.last_activity
            if idle >= self.idle_seconds:
                break
            self.handle_request()


class _SettingsHandler(BaseHTTPRequestHandler):
    server_version = "CodexHubRuntimeSettings/1"
    timeout = 5

    @property
    def service(self) -> _SettingsHTTPServer:
        return self.server  # type: ignore[return-value]

    def log_message(self, _fmt: str, *_args: Any) -> None:
        # Never put request bodies, bootstrap fragments, or credentials in logs.
        return

    def _headers(self, content_type: str, length: int, *, nonce: str | None = None) -> None:
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Pragma", "no-cache")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        script_policy = f"'nonce-{nonce}'" if nonce else "'none'"
        style_policy = f"'nonce-{nonce}'" if nonce else "'none'"
        self.send_header(
            "Content-Security-Policy",
            "default-src 'none'; "
            f"script-src {script_policy}; "
            f"style-src {style_policy}; connect-src 'self'; "
            "img-src 'self' data:; font-src 'self'; form-action 'self'; "
            "base-uri 'none'; object-src 'none'; frame-ancestors 'none'",
        )

    def _send_bytes(self, status: HTTPStatus, payload: bytes, content_type: str, *, nonce: str | None = None) -> None:
        self.send_response(status.value)
        self._headers(content_type, len(payload), nonce=nonce)
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8")

    def _valid_host(self) -> bool:
        return self.headers.get("Host", "") == f"{_HOST}:{self.server.server_port}"

    def _same_origin(self) -> bool:
        return self.headers.get("Origin", "") == self.service.origin

    def _session(self) -> str | None:
        authorization = self.headers.get(_SESSION_HEADER, "")
        scheme, separator, value = authorization.partition(" ")
        return value.strip() if separator and scheme.lower() == "bearer" and value.strip() else None

    def _allowed(self, *, mutation: bool) -> bool:
        if self.client_address[0] != _HOST or not self._valid_host():
            self._json(HTTPStatus.MISDIRECTED_REQUEST, {"ok": False, "error": "Open Runtime Settings from CodexHub."})
            return False
        if mutation and not self._same_origin():
            self._json(HTTPStatus.FORBIDDEN, {"ok": False, "error": "This request did not come from Runtime Settings."})
            return False
        return True

    def _authenticated(self) -> bool:
        if self.service.session_valid(self._session()):
            return True
        self._json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "This settings session expired. Reopen Runtime Settings from CodexHub."})
        return False

    def _path(self) -> str:
        parsed = urlsplit(self.path)
        if parsed.query:
            return ""
        return parsed.path

    def do_GET(self) -> None:  # noqa: N802
        self.service.mark_activity()
        if not self._allowed(mutation=False):
            return
        path = self._path()
        if path == "/":
            nonce = secrets.token_urlsafe(18)
            page = self.service.page.replace("__CODEXHUB_CSP_NONCE__", nonce).encode("utf-8")
            self._send_bytes(HTTPStatus.OK, page, "text/html; charset=utf-8", nonce=nonce)
            return
        if path not in {"/api/settings", "/api/status", "/api/browser-extension"}:
            self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Page not found."})
            return
        if not self._authenticated():
            return
        if path == "/api/browser-extension":
            try:
                root = Path(__file__).resolve().parents[1] / "browser-extension"
                output = io.BytesIO()
                with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
                    for name in ("manifest.json", "popup.html", "popup.js", "popup.css", "README.md"):
                        archive.write(root / name, name)
                self._send_bytes(HTTPStatus.OK, output.getvalue(), "application/zip")
            except OSError:
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error": "Browser connector files are unavailable. Update CodexHub."})
            return
        try:
            payload = runtime.read_settings(self.service.home) if path == "/api/settings" else _public_status(self.service.home)
        except runtime.RuntimeError_:
            self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error": "Runtime Settings are temporarily unavailable."})
            return
        self._json(HTTPStatus.OK, payload)

    def do_POST(self) -> None:  # noqa: N802
        self.service.mark_activity()
        path = self._path()
        try:
            # Consume the bounded body before rejecting its origin. Closing with
            # unread bytes can reset the socket on Windows and lose the response.
            payload = _read_request_json(self)
        except TimeoutError:
            self._json(HTTPStatus.REQUEST_TIMEOUT, {"ok": False, "error": "The settings request timed out."})
            return
        except OverflowError:
            self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"ok": False, "error": "The settings request is too large."})
            return
        except ValueError:
            self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "The settings request is invalid."})
            return

        if not self._allowed(mutation=True):
            return

        if path == "/api/session":
            token = payload.get("bootstrap")
            session = self.service.issue_session(token) if isinstance(token, str) else None
            if session is None:
                self._json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "This settings link expired. Reopen Runtime Settings from CodexHub."})
                return
            self._json(HTTPStatus.OK, {"ok": True, "session": session, "expires_in": int(self.service.session_ttl)})
            return
        if not self._authenticated():
            return

        if path == "/api/browser-session":
            from chatgpt_web_browser_account import import_session
            try:
                result = import_session(self.service.home, payload.get("cookies"))
            except ValueError as exc:
                reason = str(exc)
                allowed = {"invalid_browser_session", "component_upgrade_required", "component_start_required",
                           "browser_sign_in_required", "browser_session_verification_failed", "browser_session_changed"}
                self._json(HTTPStatus.CONFLICT, {"ok": False,
                    "error_code": reason if reason in allowed else "browser_session_verification_failed",
                    "error": "The browser session could not be connected. Your existing account is unchanged."})
                return
            self._json(HTTPStatus.OK, result)
            return
        if path == "/api/settings":
            try:
                result = runtime.save_settings(self.service.home, payload)
            except runtime.RuntimeError_ as exc:
                reason = str(exc)
                error_code = {
                    "ChatGPT Tunnel ID is invalid": "tunnel_id_invalid",
                    "ChatGPT Runtime Key is invalid": "runtime_key_invalid",
                    "ChatGPT Runtime Key action must be keep, replace, or clear": "runtime_key_invalid",
                    "ChatGPT connector name must contain 1 to 80 characters": "connector_name_invalid",
                    "ChatGPT connector name contains unsupported characters": "connector_name_invalid",
                    "ChatGPT mode must be browser-only or full": "mode_invalid",
                    "ChatGPT runtime options request is invalid": "runtime_options_invalid",
                    "ChatGPT context window must be a positive safe integer": "runtime_options_invalid",
                    "ChatGPT stall timeout must be a positive number": "runtime_options_invalid",
                }.get(reason)
                message = runtime.redact(str(exc), [
                    value
                    for value in (
                        ((payload.get("tunnel") or {}).get("runtime_key") or {}).get("value")
                        if isinstance(payload.get("tunnel"), dict)
                        and isinstance((payload.get("tunnel") or {}).get("runtime_key"), dict)
                        else None,
                    )
                    if isinstance(value, str) and value
                ])
                self._json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": message, "error_code": error_code})
                return
            except OSError:
                self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"ok": False, "error": "Settings could not be saved. Your previous settings are unchanged."})
                return
            self._json(HTTPStatus.OK, result)
            return
        if path == "/api/check":
            try:
                result = chatgpt_web_checks.check_runtime(self.service.home)
            except Exception:  # noqa: BLE001
                self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"ok": False, "error": "The ChatGPT connection check could not finish."})
                return
            self._json(HTTPStatus.OK, _public_readiness(result))
            return
        if path == "/api/login":
            try:
                reason = _login_blocked_reason(runtime.build_status(self.service.home))
                if reason:
                    action = "Upgrade and start" if reason == "component_upgrade_required" else "Restart"
                    self._json(HTTPStatus.CONFLICT, {
                        "ok": False, "error_code": reason,
                        "error": f"{action} the ChatGPT component from CodexHub before opening sign-in.",
                    })
                    return
                runtime.open_login(self.service.home)
            except runtime.RuntimeError_:
                self._json(HTTPStatus.CONFLICT, {"ok": False, "error": "ChatGPT sign-in could not start. Check that the component is installed and available."})
                return
            self._json(HTTPStatus.OK, {"ok": True, "started": True})
            return
        if path == "/api/login/cancel":
            try:
                runtime.close_login(self.service.home)
            except runtime.RuntimeError_:
                self._json(HTTPStatus.CONFLICT, {"ok": False, "error": "The sign-in window could not be closed. You can close it manually and refresh status."})
                return
            self._json(HTTPStatus.OK, {"ok": True, "cancelled": True})
            return
        self._json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "Action not found."})

    def do_PUT(self) -> None:  # noqa: N802
        self._json(HTTPStatus.METHOD_NOT_ALLOWED, {"ok": False, "error": "Method not allowed."})

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._json(HTTPStatus.METHOD_NOT_ALLOWED, {"ok": False, "error": "Method not allowed."})


def create_server(
    home: Path,
    *,
    port: int = 0,
    bootstrap_ttl: float = _BOOTSTRAP_TTL_SECONDS,
    session_ttl: float = _SESSION_TTL_SECONDS,
    idle_seconds: float = _SERVER_IDLE_SECONDS,
) -> _SettingsHTTPServer:
    """Bind one authenticated settings session to a private managed runtime home."""
    return _SettingsHTTPServer(
        Path(home).expanduser().resolve(),
        port=port,
        bootstrap_ttl=bootstrap_ttl,
        session_ttl=session_ttl,
        idle_seconds=idle_seconds,
    )


def settings_url(server: _SettingsHTTPServer) -> str:
    """Create the one-use browser URL; its capability is in the fragment only."""
    token = server.bootstrap_token
    if not token:
        raise RuntimeError("settings link was already consumed")
    return f"{server.origin}/#{token}"


def serve(home: Path, *, port: int = 0) -> None:
    server = create_server(home, port=port)
    try:
        sys.stdout.write(json.dumps({"url": settings_url(server)}) + "\n")
        sys.stdout.flush()
        server.serve_with_expiry()
    finally:
        server.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("serve",))
    parser.add_argument("--home", type=Path, default=runtime.default_home())
    parser.add_argument("--port", type=int, default=0)
    args = parser.parse_args(argv)
    try:
        serve(args.home, port=args.port)
    except (OSError, runtime.RuntimeError_, RuntimeError):
        sys.stdout.write(json.dumps({"ok": False, "error": "ChatGPT Runtime Settings could not be opened."}) + "\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
