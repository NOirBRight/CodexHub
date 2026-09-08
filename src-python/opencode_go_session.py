"""OpenCode Go routing-session header derived from a stable client identity."""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from urllib.parse import urlsplit


def _header(headers: Mapping[str, str], name: str) -> str | None:
    wanted = name.lower()
    for key, value in headers.items():
        if str(key).lower() == wanted:
            return str(value)
    return None


def bind_session_headers(
    headers: dict[str, str],
    endpoint_url: str,
    prompt_cache_key: str | None,
) -> dict[str, str]:
    """Bind Go routing affinity without exposing the caller's cache identity."""

    endpoint = urlsplit(endpoint_url)
    if (
        endpoint.scheme != "https"
        or endpoint.hostname != "opencode.ai"
        or endpoint.port not in (None, 443)
        or not endpoint.path.startswith("/zen/go/v1/")
        or _header(headers, "x-opencode-session")
    ):
        return headers
    identity = next(
        (
            value
            for name in (
                "session_id",
                "session-id",
                "x-session-id",
                "x-codex-session-id",
            )
            if (value := _header(headers, name))
        ),
        prompt_cache_key,
    )
    authorization = _header(headers, "authorization")
    if not isinstance(identity, str) or not identity or not authorization:
        return headers
    digest = hmac.new(
        authorization.encode("utf-8"),
        b"codexhub:opencode-go-session:v1\0" + identity.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return {
        **{key: value for key, value in headers.items() if key.lower() != "x-opencode-session"},
        "x-opencode-session": "codexhub-" + digest,
    }
