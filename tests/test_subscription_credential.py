"""SubscriptionCredential seam (ADR-0005)."""

from __future__ import annotations

from typing import Mapping

import pytest

from subscription_credential import (
    AUTH_REQUIRED,
    CodexAuthAdapter,
    SubscriptionAuthError,
    credential_for,
    provider_auth_mode,
    register,
    register_builtin_adapters,
    register_provider_auth,
    registered_modes,
    unregister,
    unregister_provider_auth,
)


class _StubCredential:
    def __init__(self, token: str = "tok") -> None:
        self._token = token

    def access_token(self) -> str:
        return self._token

    def account_headers(self) -> Mapping[str, str]:
        return {"Chatgpt-account-id": "acct-1"}

    def refresh(self) -> str:
        self._token = "tok-refreshed"
        return self._token


def test_register_and_lookup_round_trip() -> None:
    unregister("stub")
    register("stub", _StubCredential())
    try:
        adapter = credential_for("stub")
        assert adapter is not None
        assert adapter.access_token() == "tok"
        assert adapter.account_headers()["Chatgpt-account-id"] == "acct-1"
        assert adapter.refresh() == "tok-refreshed"
        assert "stub" in registered_modes()
    finally:
        unregister("stub")


def test_unknown_mode_is_none() -> None:
    assert credential_for("not-a-subscription") is None
    assert credential_for(None) is None


def test_error_taxonomy_is_closed() -> None:
    with pytest.raises(ValueError, match="unsupported"):
        SubscriptionAuthError("nope", classification="expired")
    err = SubscriptionAuthError("login", classification=AUTH_REQUIRED)
    assert err.classification == AUTH_REQUIRED


def test_simple_api_keys_stay_out_of_the_registry() -> None:
    register_builtin_adapters()
    assert credential_for("api_key") is None
    assert credential_for("ollama_api_key") is None
    assert credential_for("incoming") is None


def test_builtin_adapters_register_codex_and_xai() -> None:
    unregister("codex_auth")
    unregister("xai_oauth")
    try:
        register_builtin_adapters()
        assert isinstance(credential_for("codex_auth"), CodexAuthAdapter)
        assert credential_for("xai_oauth") is not None
        register_builtin_adapters()
        assert credential_for("codex_auth") is not None
        assert credential_for("xai_oauth") is not None
    finally:
        register_builtin_adapters()


def test_provider_auth_registry_does_not_require_catalog_edits() -> None:
    from pathlib import Path

    unregister_provider_auth("acme")
    try:
        register_provider_auth("acme", "acme_oauth", lambda: True)
        assert provider_auth_mode("acme") == "acme_oauth"
        register_provider_auth("acme", "acme_oauth", lambda: False)
        assert provider_auth_mode("acme") is None
        assert provider_auth_mode(None) is None
        catalog = (
            Path(__file__).resolve().parents[1] / "src-python" / "gateway_catalog_runtime.py"
        ).read_text(encoding="utf-8")
        assert "acme" not in catalog
        assert "acme_oauth" not in catalog
        assert "provider_id == " not in catalog
    finally:
        unregister_provider_auth("acme")


def test_codex_adapter_maps_missing_auth_to_auth_required(monkeypatch: pytest.MonkeyPatch) -> None:
    from codex_auth import CodexAuthError

    monkeypatch.setattr(
        "codex_auth.access_token",
        lambda: (_ for _ in ()).throw(CodexAuthError("missing auth.json")),
    )
    with pytest.raises(SubscriptionAuthError) as exc:
        CodexAuthAdapter().access_token()
    assert exc.value.classification == AUTH_REQUIRED


def test_codex_adapter_refresh_maps_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    from codex_auth import CodexAuthError

    monkeypatch.setattr(
        "codex_auth.load_auth_json",
        lambda: (_ for _ in ()).throw(CodexAuthError("missing auth.json")),
    )
    with pytest.raises(SubscriptionAuthError) as missing:
        CodexAuthAdapter().refresh()
    assert missing.value.classification == AUTH_REQUIRED

    monkeypatch.setattr("codex_auth.load_auth_json", lambda: {"tokens": {"refresh_token": "r"}})
    monkeypatch.setattr(
        "codex_auth.refresh",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(CodexAuthError("refresh broke")),
    )
    with pytest.raises(SubscriptionAuthError) as failed:
        CodexAuthAdapter().refresh()
    assert failed.value.classification == "refresh-failed"


def test_codex_adapter_drops_responses_lite_for_unsupported_models() -> None:
    adapter = CodexAuthAdapter()
    assert adapter.drop_incoming_header(
        "x-openai-internal-codex-responses-lite",
        model_id="openai/gpt-5.4",
    )
    assert adapter.drop_incoming_header(
        "X-OpenAI-Internal-Codex-Responses-Lite",
        model_id="gpt-5.4-mini",
    )
    assert not adapter.drop_incoming_header(
        "x-openai-internal-codex-responses-lite",
        model_id="gpt-4.1",
    )
    assert not adapter.drop_incoming_header("authorization", model_id="gpt-5.4")


def test_codex_adapter_applies_identity_headers() -> None:
    adapter = CodexAuthAdapter()
    outgoing = {"Content-Type": "application/json"}
    adapter.apply_identity_headers(
        outgoing,
        strict_official_passthrough=False,
        session_id="sess",
        client_request_id="req",
        read_header=lambda headers, name: next(
            (value for key, value in headers.items() if key.lower() == name.lower()),
            None,
        ),
        make_id=lambda: "unused",
    )
    assert outgoing["Session-id"] == "sess"
    assert outgoing["Thread-id"] == "sess"
    assert outgoing["X-codex-window-id"] == "sess:1"
    assert outgoing["X-client-request-id"] == "req"
    passthrough = {"Content-Type": "application/json"}
    adapter.apply_identity_headers(
        passthrough,
        strict_official_passthrough=True,
        session_id="sess",
        client_request_id="req",
        read_header=lambda headers, name: None,
        make_id=lambda: "unused",
    )
    assert passthrough == {"Content-Type": "application/json"}
