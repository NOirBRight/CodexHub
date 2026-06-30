"""ChatGPT subscription token management for the CodexHub proxy.

Reads and refreshes the access token stored by the Codex CLI in
``~/.codex/auth.json`` so the proxy can inject it into official OpenAI
requests without relying on the caller to supply credentials.
"""

from __future__ import annotations

import base64
import json
import os
import threading
import time
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

CODEX_HOME_ENV = "CODEX_HOME"
DEFAULT_CODEX_HOME = Path.home() / ".codex"
AUTH_FILENAME = "auth.json"

# Refresh when the access token has less than this many seconds of life left.
REFRESH_SAFETY_MARGIN_SECONDS = 60

# OpenAI OAuth token endpoint used by the Codex CLI.
OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"

_lock = threading.Lock()
_cache: dict[str, Any] | None = None


class CodexAuthError(RuntimeError):
    """Raised when the ChatGPT subscription credential is unavailable."""


def codex_home() -> Path:
    env_value = os.environ.get(CODEX_HOME_ENV)
    if env_value:
        return Path(env_value)
    return DEFAULT_CODEX_HOME


def auth_json_path() -> Path:
    return codex_home() / AUTH_FILENAME


def decode_jwt_payload(token: str) -> dict[str, Any]:
    """Decode a JWT payload without verifying the signature.

    Only the payload claims (``exp``, ``client_id``, ``scp`` ...) are needed,
    so the header and signature are ignored.
    """
    parts = token.split(".")
    if len(parts) < 2:
        raise CodexAuthError("malformed JWT: expected header.payload.signature")
    payload_segment = parts[1]
    # JWT uses base64url without padding.
    padding = "=" * (-len(payload_segment) % 4)
    decoded = base64.urlsafe_b64decode(payload_segment + padding)
    try:
        payload = json.loads(decoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CodexAuthError(f"malformed JWT payload: {exc}") from exc
    if not isinstance(payload, dict):
        raise CodexAuthError("JWT payload is not a JSON object")
    return payload


def _is_expired(exp: int | float | None, now: float | None = None) -> bool:
    if exp is None:
        # No exp claim — treat as expired so we refresh defensively.
        return True
    current = time.time() if now is None else now
    return (int(exp) - current) < REFRESH_SAFETY_MARGIN_SECONDS


def load_auth_json(path: Path | None = None) -> dict[str, Any]:
    """Read and validate ``auth.json``.

    Raises :class:`CodexAuthError` when the file is missing or not in
    ``chatgpt`` subscription mode.
    """
    target = path or auth_json_path()
    if not target.exists():
        raise CodexAuthError(
            f"Codex auth file not found at {target}. Log in with the Codex CLI first."
        )
    try:
        data = json.loads(target.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CodexAuthError(f"failed to read Codex auth file: {exc}") from exc
    if not isinstance(data, dict):
        raise CodexAuthError("Codex auth file is not a JSON object")
    if data.get("auth_mode") != "chatgpt":
        raise CodexAuthError(
            f"Codex auth mode is {data.get('auth_mode')!r}, expected 'chatgpt'. "
            "Log in with ChatGPT in the Codex CLI first."
        )
    tokens = data.get("tokens")
    if not isinstance(tokens, dict) or not tokens.get("access_token"):
        raise CodexAuthError("Codex auth file has no access_token")
    return data


def _persist_auth_json(path: Path, data: dict[str, Any]) -> None:
    """Write ``auth.json`` back, preserving the original structure shape."""
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def refresh(
    auth_data: dict[str, Any],
    path: Path | None = None,
    *,
    token_url: str = OAUTH_TOKEN_URL,
    _opener: Any = None,
) -> str:
    """Refresh the access token using the stored refresh token.

    On success the new tokens are written back to ``auth.json`` and the new
    access token is returned.  Raises :class:`CodexAuthError` on any failure.
    """
    tokens = auth_data.get("tokens")
    if not isinstance(tokens, dict):
        raise CodexAuthError("auth data has no tokens to refresh")
    refresh_token = tokens.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        raise CodexAuthError(
            "No refresh_token available. Log in with the Codex CLI again."
        )

    current_access = tokens.get("access_token")
    client_id: str | None = None
    scope: str | None = None
    if isinstance(current_access, str):
        try:
            payload = decode_jwt_payload(current_access)
        except CodexAuthError:
            payload = {}
        client_id = payload.get("client_id") if isinstance(payload, dict) else None
        scp = payload.get("scp") if isinstance(payload, dict) else None
        if isinstance(scp, list):
            scope = " ".join(str(s) for s in scp)
        elif isinstance(scp, str):
            scope = scp

    if not client_id:
        raise CodexAuthError(
            "Could not determine OAuth client_id from the current access token."
        )

    body = json.dumps(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "scope": scope or "openid profile email offline_access",
        }
    ).encode("utf-8")
    request = Request(
        token_url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    opener = _opener if _opener is not None else urlopen
    try:
        with opener(request, timeout=30) as response:
            raw = response.read()
    except (HTTPError, URLError) as exc:
        raise CodexAuthError(
            f"Token refresh failed: {type(exc).__name__}: {exc}. "
            "Log in with the Codex CLI again."
        ) from exc

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CodexAuthError(f"Token refresh response was not valid JSON: {exc}") from exc

    new_access = payload.get("access_token")
    new_refresh = payload.get("refresh_token", refresh_token)
    if not isinstance(new_access, str) or not new_access:
        raise CodexAuthError("Token refresh response did not contain an access_token")

    tokens["access_token"] = new_access
    tokens["refresh_token"] = new_refresh
    auth_data["last_refresh"] = time.strftime("%Y-%m-%dT%H:%M:%S.000000Z", time.gmtime())
    if "id_token" in payload and isinstance(payload["id_token"], str):
        tokens["id_token"] = payload["id_token"]

    target = path or auth_json_path()
    _persist_auth_json(target, auth_data)
    return new_access


def access_token(
    path: Path | None = None,
    *,
    _now: float | None = None,
    _opener: Any = None,
) -> str:
    """Return a valid ChatGPT subscription access token.

    Refreshes automatically when the cached token is within
    :data:`REFRESH_SAFETY_MARGIN_SECONDS` of expiry.
    """
    global _cache
    with _lock:
        now = time.time() if _now is None else _now
        cached = _cache
        if cached is not None:
            token = cached.get("tokens", {}).get("access_token")
            if isinstance(token, str) and token:
                try:
                    payload = decode_jwt_payload(token)
                except CodexAuthError:
                    payload = {}
                exp = payload.get("exp") if isinstance(payload, dict) else None
                if not _is_expired(exp, now):
                    return token
            # fall through to reload + refresh

        auth_data = load_auth_json(path)
        tokens = auth_data.get("tokens", {})
        token = tokens.get("access_token")
        if not isinstance(token, str) or not token:
            raise CodexAuthError("access_token missing from auth.json")

        try:
            payload = decode_jwt_payload(token)
        except CodexAuthError:
            payload = {}
        exp = payload.get("exp") if isinstance(payload, dict) else None
        if _is_expired(exp, now):
            token = refresh(auth_data, path, _opener=_opener)

        _cache = auth_data
        return token


def account_id(path: Path | None = None) -> str | None:
    """Return the ChatGPT account id from auth.json, if available."""
    global _cache
    with _lock:
        auth_data = _cache
        if auth_data is None:
            try:
                auth_data = load_auth_json(path)
            except CodexAuthError:
                return None
            _cache = auth_data
        tokens = auth_data.get("tokens", {})
        account = tokens.get("account_id")
        return account if isinstance(account, str) and account else None


def reset_cache() -> None:
    """Clear the in-memory token cache (used by tests)."""
    global _cache
    with _lock:
        _cache = None