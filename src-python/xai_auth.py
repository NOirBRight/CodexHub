"""xAI SuperGrok / X Premium+ OAuth device-code adapter (ADR-0005 adapter #2)."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from atomic_io import atomic_write_text
from subscription_credential import (
    SubscriptionAuthError,
    credential_for,
    register,
)

AUTH_ISSUER = "https://auth.x.ai"
DISCOVERY_URL = f"{AUTH_ISSUER}/.well-known/openid-configuration"
API_HOST_SUFFIXES = (".x.ai",)
API_HOSTS = {"x.ai", "api.x.ai", "auth.x.ai", "accounts.x.ai"}
TOKEN_FILENAME = "xai_auth.json"
PRIVATE_AUTH_FILE_MODE = 0o600
DEFAULT_CLIENT_ID = "codexhub"
POLL_INTERVAL_SECONDS = 5.0
DEVICE_TIMEOUT_SECONDS = 600.0

_lock = threading.Lock()
_cache: dict[str, Any] | None = None


def _codex_home() -> Path:
    env_value = os.environ.get("CODEX_HOME")
    if env_value:
        return Path(env_value)
    return Path.home() / ".codex"


def auth_json_path() -> Path:
    override = os.environ.get("CODEXHUB_XAI_AUTH_PATH")
    if override:
        return Path(override)
    return _codex_home() / "proxy" / TOKEN_FILENAME


def client_id() -> str:
    return os.environ.get("CODEXHUB_XAI_OAUTH_CLIENT_ID", DEFAULT_CLIENT_ID).strip() or DEFAULT_CLIENT_ID


def pin_https_xai_url(url: str) -> str:
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise SubscriptionAuthError(
            "xAI OAuth URL must be HTTPS",
            classification="not-eligible",
        )
    host = (parsed.hostname or "").lower()
    if host not in API_HOSTS and not any(host.endswith(suffix) for suffix in API_HOST_SUFFIXES):
        raise SubscriptionAuthError(
            f"xAI OAuth URL host is not pinned to x.ai: {host}",
            classification="not-eligible",
        )
    return url


def _open_json(
    url: str,
    *,
    payload: dict[str, Any] | None = None,
    opener: Any = None,
    timeout: float = 20.0,
) -> dict[str, Any]:
    pinned = pin_https_xai_url(url)
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    request = Request(
        pinned,
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="GET" if body is None else "POST",
    )
    open_fn = opener if opener is not None else urlopen
    try:
        with open_fn(request, timeout=timeout) as response:
            raw = response.read()
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace") if exc.fp else str(exc)
        raise SubscriptionAuthError(
            f"xAI OAuth HTTP {exc.code}: {detail[:200]}",
            classification="refresh-failed" if payload else "auth-required",
        ) from exc
    except URLError as exc:
        raise SubscriptionAuthError(
            f"xAI OAuth transport error: {exc}",
            classification="auth-required",
        ) from exc
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SubscriptionAuthError(
            f"xAI OAuth returned non-JSON: {exc}",
            classification="refresh-failed",
        ) from exc
    if not isinstance(data, dict):
        raise SubscriptionAuthError("xAI OAuth JSON is not an object", classification="refresh-failed")
    return data


def discover_endpoints(*, opener: Any = None) -> dict[str, str]:
    document = _open_json(DISCOVERY_URL, opener=opener)
    device = document.get("device_authorization_endpoint")
    token = document.get("token_endpoint")
    if not isinstance(device, str) or not isinstance(token, str):
        raise SubscriptionAuthError(
            "xAI OIDC discovery is missing device or token endpoints",
            classification="not-eligible",
        )
    return {
        "device_authorization_endpoint": pin_https_xai_url(device),
        "token_endpoint": pin_https_xai_url(token),
    }


def start_device_login(*, opener: Any = None) -> dict[str, Any]:
    endpoints = discover_endpoints(opener=opener)
    data = _open_json(
        endpoints["device_authorization_endpoint"],
        payload={"client_id": client_id(), "scope": "openid profile offline_access"},
        opener=opener,
    )
    verification_url = data.get("verification_uri_complete") or data.get("verification_uri")
    user_code = data.get("user_code")
    device_code = data.get("device_code")
    if not isinstance(verification_url, str) or not isinstance(user_code, str) or not isinstance(device_code, str):
        raise SubscriptionAuthError(
            "xAI device-code response is missing verification fields",
            classification="auth-required",
        )
    interval = data.get("interval")
    expires_in = data.get("expires_in")
    return {
        "verification_url": verification_url,
        "user_code": user_code,
        "device_code": device_code,
        "interval": float(interval) if isinstance(interval, (int, float)) else POLL_INTERVAL_SECONDS,
        "expires_in": float(expires_in) if isinstance(expires_in, (int, float)) else DEVICE_TIMEOUT_SECONDS,
        "token_endpoint": endpoints["token_endpoint"],
    }


def poll_device_login(
    device: Mapping[str, Any],
    *,
    opener: Any = None,
    sleeper: Any = time.sleep,
    now: Any = time.time,
) -> dict[str, Any]:
    token_endpoint = str(device["token_endpoint"])
    deadline = float(now()) + float(device.get("expires_in") or DEVICE_TIMEOUT_SECONDS)
    interval = float(device.get("interval") or POLL_INTERVAL_SECONDS)
    while float(now()) < deadline:
        try:
            tokens = _open_json(
                token_endpoint,
                payload={
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "device_code": device["device_code"],
                    "client_id": client_id(),
                },
                opener=opener,
            )
        except SubscriptionAuthError:
            sleeper(interval)
            continue
        if tokens.get("error") in {"authorization_pending", "slow_down"}:
            if tokens.get("error") == "slow_down":
                interval += 5
            sleeper(interval)
            continue
        access = tokens.get("access_token")
        if isinstance(access, str) and access:
            _persist_tokens(tokens)
            return tokens
        sleeper(interval)
    raise SubscriptionAuthError("xAI device-code login timed out", classification="auth-required")


def _persist_tokens(tokens: Mapping[str, Any]) -> None:
    global _cache
    path = auth_json_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "auth_mode": "xai_oauth",
        "tokens": {
            "access_token": tokens.get("access_token"),
            "refresh_token": tokens.get("refresh_token"),
            "token_type": tokens.get("token_type", "Bearer"),
        },
    }
    atomic_write_text(
        path,
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
        mode=PRIVATE_AUTH_FILE_MODE,
    )
    with _lock:
        _cache = payload


def load_auth_json(path: Path | None = None) -> dict[str, Any]:
    target = path or auth_json_path()
    if not target.exists():
        raise SubscriptionAuthError(
            f"xAI auth file not found at {target}. Sign in with SuperGrok or X Premium+.",
            classification="auth-required",
        )
    try:
        data = json.loads(target.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SubscriptionAuthError(f"failed to read xAI auth file: {exc}", classification="auth-required") from exc
    if not isinstance(data, dict) or data.get("auth_mode") != "xai_oauth":
        raise SubscriptionAuthError("xAI auth file is not an xai_oauth session", classification="auth-required")
    tokens = data.get("tokens")
    if not isinstance(tokens, dict) or not tokens.get("access_token"):
        raise SubscriptionAuthError("xAI auth file has no access_token", classification="auth-required")
    return data


def has_session() -> bool:
    try:
        load_auth_json()
        return True
    except SubscriptionAuthError:
        return False


def refresh(auth_data: dict[str, Any] | None = None, *, opener: Any = None) -> str:
    data = auth_data or load_auth_json()
    tokens = data.get("tokens") if isinstance(data, dict) else None
    if not isinstance(tokens, dict):
        raise SubscriptionAuthError("xAI auth data has no tokens", classification="refresh-failed")
    refresh_token = tokens.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise SubscriptionAuthError("xAI session has no refresh_token", classification="refresh-failed")
    endpoints = discover_endpoints(opener=opener)
    rotated = _open_json(
        endpoints["token_endpoint"],
        payload={
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id(),
        },
        opener=opener,
    )
    access = rotated.get("access_token")
    if not isinstance(access, str) or not access:
        raise SubscriptionAuthError("xAI refresh did not return an access_token", classification="refresh-failed")
    merged = dict(tokens)
    merged.update({key: rotated[key] for key in ("access_token", "refresh_token", "token_type") if key in rotated})
    _persist_tokens(merged)
    return access


def access_token(*, opener: Any = None) -> str:
    global _cache
    with _lock:
        data = _cache
        if data is None:
            data = load_auth_json()
            _cache = data
        tokens = data.get("tokens", {})
        token = tokens.get("access_token")
        if not isinstance(token, str) or not token:
            raise SubscriptionAuthError("xAI access_token missing", classification="auth-required")
        return token


def reset_cache() -> None:
    global _cache
    with _lock:
        _cache = None


class XaiOauthAdapter:
    def access_token(self) -> str:
        return access_token()

    def account_headers(self) -> Mapping[str, str]:
        return {}

    def refresh(self) -> str:
        return refresh()


def register_default() -> None:
    if credential_for("xai_oauth") is None:
        register("xai_oauth", XaiOauthAdapter())


register_default()


def logout() -> None:
    path = auth_json_path()
    if path.exists():
        path.unlink()
    reset_cache()
