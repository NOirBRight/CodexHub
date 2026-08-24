"""Subscription credential seam (ADR-0005).

Callers materialize Authorization and account headers through this registry.
Subscription adapters (ChatGPT Codex auth, later XAI OAuth, ...) register here.
Simple API-key strategies are not subscriptions and stay out of this registry.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Mapping, Protocol, runtime_checkable
import sys

AUTH_REQUIRED = "auth-required"
REFRESH_FAILED = "refresh-failed"
NOT_ELIGIBLE = "not-eligible"
_ERROR_CLASSIFICATIONS = frozenset({AUTH_REQUIRED, REFRESH_FAILED, NOT_ELIGIBLE})


class SubscriptionAuthError(RuntimeError):
    """Raised when a subscription credential cannot be used."""

    def __init__(self, message: str, *, classification: str) -> None:
        if classification not in _ERROR_CLASSIFICATIONS:
            raise ValueError(f"unsupported subscription auth classification: {classification}")
        self.classification = classification
        super().__init__(message)


@runtime_checkable
class SubscriptionCredential(Protocol):
    def access_token(self) -> str:
        """Return a live bearer token, refreshing if needed."""

    def account_headers(self) -> Mapping[str, str]:
        """Optional account/session headers (never includes Authorization)."""

    def refresh(self) -> str:
        """Force a refresh and return the new access token."""


_REGISTRY: dict[str, SubscriptionCredential] = {}
_PROVIDER_AUTH: dict[str, tuple[str, Callable[[], bool]]] = {}


def register(auth_mode: str, credential: SubscriptionCredential) -> None:
    if not auth_mode:
        raise ValueError("auth_mode is required")
    _REGISTRY[auth_mode] = credential


def unregister(auth_mode: str) -> None:
    _REGISTRY.pop(auth_mode, None)


def credential_for(auth_mode: str | None) -> SubscriptionCredential | None:
    if not auth_mode:
        return None
    return _REGISTRY.get(auth_mode)


def registered_modes() -> tuple[str, ...]:
    return tuple(sorted(_REGISTRY))


def register_provider_auth(
    provider_id: str,
    auth_mode: str,
    has_session: Callable[[], bool],
) -> None:
    if not provider_id:
        raise ValueError("provider_id is required")
    if not auth_mode:
        raise ValueError("auth_mode is required")
    _PROVIDER_AUTH[provider_id] = (auth_mode, has_session)


def unregister_provider_auth(provider_id: str) -> None:
    _PROVIDER_AUTH.pop(provider_id, None)


def provider_auth_mode(provider_id: str | None) -> str | None:
    if not provider_id:
        return None
    binding = _PROVIDER_AUTH.get(provider_id)
    if binding is None:
        return None
    auth_mode, has_session = binding
    return auth_mode if has_session() else None


class CodexAuthAdapter:
    """ADR-0005 adapter #1: ChatGPT Codex subscription via ``codex_auth``."""

    _OFFICIAL_ALIAS_PREFIX = "openai/"
    _RESPONSES_LITE_HEADER = "x-openai-internal-codex-responses-lite"
    _RESPONSES_LITE_UNSUPPORTED_MODELS = frozenset({"gpt-5.4", "gpt-5.4-mini"})

    def access_token(self) -> str:
        from codex_auth import CodexAuthError, access_token

        try:
            return access_token()
        except CodexAuthError as exc:
            raise SubscriptionAuthError(str(exc), classification=AUTH_REQUIRED) from exc

    def account_headers(self) -> Mapping[str, str]:
        from codex_auth import account_id

        account = account_id()
        if not account:
            return {}
        return {"Chatgpt-account-id": account}

    def refresh(self) -> str:
        from codex_auth import CodexAuthError, load_auth_json, refresh as refresh_codex

        try:
            auth_data = load_auth_json()
        except CodexAuthError as exc:
            raise SubscriptionAuthError(str(exc), classification=AUTH_REQUIRED) from exc
        try:
            return refresh_codex(auth_data)
        except CodexAuthError as exc:
            raise SubscriptionAuthError(str(exc), classification=REFRESH_FAILED) from exc

    def drop_incoming_header(self, name: str, *, model_id: str) -> bool:
        """Drop Codex responses-lite on models that reject the official header."""
        if name.lower() != self._RESPONSES_LITE_HEADER:
            return False
        model = model_id.strip().lower()
        prefix = self._OFFICIAL_ALIAS_PREFIX
        if model.startswith(prefix):
            model = model[len(prefix) :]
        return model in self._RESPONSES_LITE_UNSUPPORTED_MODELS

    def apply_identity_headers(
        self,
        outgoing: dict[str, str],
        *,
        strict_official_passthrough: bool,
        session_id: str | None,
        client_request_id: str | None,
        read_header: Callable[..., str | None],
        make_id: Callable[[], str],
    ) -> None:
        """Materialize Codex session/thread/window/request identity headers."""
        if strict_official_passthrough:
            return
        if not read_header(outgoing, "Accept"):
            outgoing["Accept"] = "text/event-stream"
        if not read_header(outgoing, "Originator"):
            outgoing["Originator"] = "codexhub-proxy"
        if not read_header(outgoing, "User-Agent"):
            outgoing["User-Agent"] = "Codex Desktop/0.142.4 (CodexHub proxy)"
        resolved_session_id = read_header(outgoing, "Session-id")
        if not resolved_session_id:
            resolved_session_id = session_id or make_id()
            if not resolved_session_id:
                raise ValueError("materialized Codex auth is missing session identity")
            outgoing["Session-id"] = resolved_session_id
        if not read_header(outgoing, "Thread-id"):
            outgoing["Thread-id"] = resolved_session_id
        if not read_header(outgoing, "X-codex-window-id"):
            outgoing["X-codex-window-id"] = f"{resolved_session_id}:1"
        if not read_header(outgoing, "X-client-request-id"):
            resolved_request_id = client_request_id or make_id()
            if not resolved_request_id:
                raise ValueError("materialized Codex auth is missing request identity")
            outgoing["X-client-request-id"] = resolved_request_id


def register_builtin_adapters() -> None:
    """Idempotent registration of built-in subscription adapters."""
    if credential_for("codex_auth") is None:
        register("codex_auth", CodexAuthAdapter())
    if credential_for("xai_oauth") is None:
        xai_mod = sys.modules.get("xai_auth")
        if xai_mod is not None and not hasattr(xai_mod, "register_default"):
            return
        from xai_auth import register_default

        register_default()


register_builtin_adapters()
