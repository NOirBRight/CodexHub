"""Subscription credential seam (ADR-0005).

Callers materialize Authorization and account headers through this registry.
Subscription adapters (ChatGPT Codex auth, later XAI OAuth, ...) register here.
Simple API-key strategies are not subscriptions and stay out of this registry.
"""

from __future__ import annotations

from typing import Mapping, Protocol, runtime_checkable

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


class CodexAuthAdapter:
    """ADR-0005 adapter #1: ChatGPT Codex subscription via ``codex_auth``."""

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


def register_builtin_adapters() -> None:
    """Idempotent registration of built-in subscription adapters."""
    if credential_for("codex_auth") is None:
        register("codex_auth", CodexAuthAdapter())


register_builtin_adapters()
