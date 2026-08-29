"""xAI OAuth adapter pin, persist, and device-code polling."""

from __future__ import annotations

import io
import json
import os
from http.client import HTTPMessage
from typing import Any
from urllib.error import HTTPError

import pytest

import xai_auth
from subscription_credential import SubscriptionAuthError


class _FakeResponse:
    def __init__(self, payload: dict[str, Any], status: int = 200) -> None:
        self._raw = json.dumps(payload).encode("utf-8")
        self.status = status

    def read(self) -> bytes:
        return self._raw

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None


def test_pin_https_xai_billing_url_accepts_grok_proxy() -> None:
    assert xai_auth.pin_https_xai_billing_url(
        "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
    ).startswith("https://cli-chat-proxy.grok.com/")
    with pytest.raises(SubscriptionAuthError, match="not pinned"):
        xai_auth.pin_https_xai_billing_url("https://api.x.ai/v1/billing")


def test_usage_snapshot_maps_credit_percent(tmp_path, monkeypatch) -> None:
    path = tmp_path / "xai_auth.json"
    monkeypatch.setenv("CODEXHUB_XAI_AUTH_PATH", str(path))
    xai_auth.persist_tokens(
        {"access_token": "live-token", "refresh_token": "r1", "token_type": "Bearer"}
    )

    def opener(request: Any, timeout: float = 20.0) -> _FakeResponse:
        assert request.full_url.startswith("https://cli-chat-proxy.grok.com/v1/billing")
        assert request.get_header("Authorization") == "Bearer live-token"
        return _FakeResponse(
            {
                "config": {
                    "creditUsagePercent": 35,
                    "currentPeriod": {"end": "2026-09-01T00:00:00Z"},
                }
            }
        )

    snapshot = xai_auth.fetch_usage(opener=opener)
    assert snapshot["limits"][0]["remaining"] == 65.0
    assert snapshot["limits"][0]["used"] == 35.0
    assert snapshot["limits"][0]["period"] == "week"
    assert snapshot["limits"][0]["resets_at"] == "2026-09-01T00:00:00Z"


def test_usage_snapshot_includes_product_rows(tmp_path, monkeypatch) -> None:
    path = tmp_path / "xai_auth.json"
    monkeypatch.setenv("CODEXHUB_XAI_AUTH_PATH", str(path))
    xai_auth.persist_tokens(
        {"access_token": "live-token", "refresh_token": "r1", "token_type": "Bearer"}
    )

    def opener(request: Any, timeout: float = 20.0) -> _FakeResponse:
        return _FakeResponse(
            {
                "config": {
                    "creditUsagePercent": 16,
                    "currentPeriod": {"end": "2026-08-30T16:26:18Z"},
                    "productUsage": [
                        {"product": "GrokBuild", "usagePercent": 15.0},
                        {"product": "GrokChat", "usagePercent": 1.0},
                        {"product": "GrokImagine", "usagePercent": None},
                    ],
                }
            }
        )

    snapshot = xai_auth.fetch_usage(opener=opener)
    assert [row["key"] for row in snapshot["limits"]] == ["week", "grokbuild", "grokchat"]
    assert snapshot["limits"][1]["name"] == "Grok Build"
    assert snapshot["limits"][1]["used"] == 15.0
    assert snapshot["limits"][1]["remaining"] == 85.0
    assert snapshot["limits"][1]["period"] == "product"


def test_usage_snapshot_treats_open_period_without_percent_as_zero(tmp_path, monkeypatch) -> None:
    path = tmp_path / "xai_auth.json"
    monkeypatch.setenv("CODEXHUB_XAI_AUTH_PATH", str(path))
    xai_auth.persist_tokens(
        {"access_token": "live-token", "refresh_token": "r1", "token_type": "Bearer"}
    )

    def opener(request: Any, timeout: float = 20.0) -> _FakeResponse:
        return _FakeResponse({"config": {"currentPeriod": {"end": "2026-09-01T00:00:00Z"}}})

    snapshot = xai_auth.fetch_usage(opener=opener)
    assert snapshot["limits"][0]["used"] == 0.0
    assert snapshot["limits"][0]["remaining"] == 100.0


def test_pin_https_xai_url_rejects_non_xai_hosts() -> None:
    with pytest.raises(SubscriptionAuthError, match="not pinned"):
        xai_auth.pin_https_xai_url("https://evil.example/token")
    with pytest.raises(SubscriptionAuthError, match="HTTPS"):
        xai_auth.pin_https_xai_url("http://auth.x.ai/token")
    assert xai_auth.pin_https_xai_url("https://auth.x.ai/oauth/token").startswith("https://")


def test_persist_and_load_tokens_uses_private_mode(tmp_path, monkeypatch) -> None:
    path = tmp_path / "xai_auth.json"
    monkeypatch.setenv("CODEXHUB_XAI_AUTH_PATH", str(path))
    xai_auth.persist_tokens(
        {"access_token": "access-1", "refresh_token": "refresh-1", "token_type": "Bearer"}
    )
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
    loaded = xai_auth.load_auth_json()
    assert loaded["auth_mode"] == "xai_oauth"
    assert loaded["tokens"]["access_token"] == "access-1"
    assert xai_auth.has_session() is True
    xai_auth.logout()
    assert xai_auth.has_session() is False


def test_start_device_login_reads_oidc_discovery(monkeypatch) -> None:
    calls: list[str] = []

    def opener(request: Any, timeout: float = 20.0) -> _FakeResponse:
        calls.append(request.full_url)
        if request.full_url.endswith("openid-configuration"):
            assert request.get_method() == "GET"
            return _FakeResponse(
                {
                    "device_authorization_endpoint": "https://auth.x.ai/oauth/device",
                    "token_endpoint": "https://auth.x.ai/oauth/token",
                }
            )
        assert request.get_header("Content-type") == "application/x-www-form-urlencoded"
        body = request.data.decode("utf-8") if isinstance(request.data, bytes) else ""
        assert "client_id=" in body
        return _FakeResponse(
            {
                "device_code": "dev-1",
                "user_code": "ABCD-EFGH",
                "verification_uri": "https://auth.x.ai/device",
                "verification_uri_complete": "https://auth.x.ai/device?user_code=ABCD-EFGH",
                "interval": 1,
                "expires_in": 30,
            }
        )

    started = xai_auth.start_device_login(opener=opener)
    assert started["user_code"] == "ABCD-EFGH"
    assert started["device_code"] == "dev-1"
    assert "auth.x.ai" in started["verification_url"]
    assert any(url.endswith("openid-configuration") for url in calls)


def test_poll_device_login_persists_access_token(tmp_path, monkeypatch) -> None:
    path = tmp_path / "xai_auth.json"
    monkeypatch.setenv("CODEXHUB_XAI_AUTH_PATH", str(path))
    pending = {"count": 0}

    def opener(request: Any, timeout: float = 20.0) -> _FakeResponse:
        if "token" not in request.full_url:
            raise AssertionError(request.full_url)
        pending["count"] += 1
        if pending["count"] < 2:
            return _FakeResponse({"error": "authorization_pending"})
        return _FakeResponse(
            {"access_token": "live-token", "refresh_token": "r1", "token_type": "Bearer"}
        )

    tokens = xai_auth.poll_device_login(
        {
            "device_code": "dev-1",
            "token_endpoint": "https://auth.x.ai/oauth/token",
            "interval": 0,
            "expires_in": 10,
        },
        opener=opener,
        sleeper=lambda _seconds: None,
        now=lambda: 0.0,
    )
    assert tokens["access_token"] == "live-token"
    assert xai_auth.access_token() == "live-token"


def test_http_error_on_token_endpoint_is_refresh_failed(tmp_path, monkeypatch) -> None:
    path = tmp_path / "xai_auth.json"
    monkeypatch.setenv("CODEXHUB_XAI_AUTH_PATH", str(path))
    xai_auth.persist_tokens(
        {"access_token": "old", "refresh_token": "r1", "token_type": "Bearer"}
    )

    def opener(request: Any, timeout: float = 20.0) -> _FakeResponse:
        if request.full_url.endswith("openid-configuration"):
            return _FakeResponse(
                {
                    "device_authorization_endpoint": "https://auth.x.ai/oauth/device",
                    "token_endpoint": "https://auth.x.ai/oauth/token",
                }
            )
        raise HTTPError(
            request.full_url,
            401,
            "unauthorized",
            HTTPMessage(),
            io.BytesIO(b'{"error":"invalid_grant"}'),
        )

    with pytest.raises(SubscriptionAuthError) as exc:
        xai_auth.refresh(opener=opener)
    assert exc.value.classification == "refresh-failed"


def test_builtin_registry_includes_xai_oauth() -> None:
    from subscription_credential import credential_for, register_builtin_adapters

    register_builtin_adapters()
    adapter = credential_for("xai_oauth")
    assert adapter is not None
    assert adapter.account_headers() == {}


def test_xai_oauth_headers_use_the_adapter_not_a_transport_branch(tmp_path, monkeypatch) -> None:
    from gateway_transport import build_upstream_headers

    path = tmp_path / "xai_auth.json"
    monkeypatch.setenv("CODEXHUB_XAI_AUTH_PATH", str(path))
    xai_auth.persist_tokens({"access_token": "xai-live", "refresh_token": "refresh-1"})
    headers = build_upstream_headers(
        {"Content-Type": "application/json"},
        {"auth": "xai_oauth", "name": "xai", "upstream_model": "grok-4"},
    )
    assert headers["Authorization"] == "Bearer xai-live"


def test_catalog_selects_xai_oauth_without_a_transport_branch() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "src-python"
    catalog = (root / "gateway_catalog_runtime.py").read_text(encoding="utf-8")
    transport = (root / "gateway_transport.py").read_text(encoding="utf-8")
    assert "provider_auth_mode" in catalog
    assert "provider_id == " not in catalog
    assert "xai_oauth" not in catalog
    assert "xai_oauth" not in transport


def test_access_token_refreshes_when_expires_at_is_near(tmp_path, monkeypatch) -> None:
    path = tmp_path / "xai_auth.json"
    monkeypatch.setenv("CODEXHUB_XAI_AUTH_PATH", str(path))
    xai_auth.persist_tokens(
        {
            "access_token": "old-token",
            "refresh_token": "r1",
            "token_type": "Bearer",
            "expires_in": 1,
        },
        now=lambda: 0.0,
    )

    def opener(request: Any, timeout: float = 20.0) -> _FakeResponse:
        if request.full_url.endswith("openid-configuration"):
            return _FakeResponse(
                {
                    "device_authorization_endpoint": "https://auth.x.ai/oauth/device",
                    "token_endpoint": "https://auth.x.ai/oauth/token",
                }
            )
        return _FakeResponse(
            {"access_token": "rotated-token", "refresh_token": "r2", "token_type": "Bearer"}
        )

    token = xai_auth.access_token(opener=opener, now=lambda: 100.0)
    assert token == "rotated-token"
    assert xai_auth.load_auth_json()["tokens"]["access_token"] == "rotated-token"


def test_access_token_skips_refresh_without_expires_at(tmp_path, monkeypatch) -> None:
    path = tmp_path / "xai_auth.json"
    monkeypatch.setenv("CODEXHUB_XAI_AUTH_PATH", str(path))
    xai_auth.persist_tokens(
        {"access_token": "live-token", "refresh_token": "r1", "token_type": "Bearer"}
    )

    def opener(request: Any, timeout: float = 20.0) -> _FakeResponse:
        raise AssertionError(f"unexpected refresh against {request.full_url}")

    assert xai_auth.access_token(opener=opener) == "live-token"


def test_access_token_cli_prints_token_json(tmp_path, monkeypatch, capsys) -> None:
    import importlib.util
    from pathlib import Path

    monkeypatch.setenv("CODEXHUB_XAI_AUTH_PATH", str(tmp_path / "xai_auth.json"))
    xai_auth.persist_tokens(
        {"access_token": "cli-token", "refresh_token": "r1", "token_type": "Bearer"}
    )
    script = Path(__file__).resolve().parents[1] / "scripts" / "xai_device_login.py"
    spec = importlib.util.spec_from_file_location("xai_device_login_cli", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.main(["access-token"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["access_token"] == "cli-token"


def test_poll_device_login_honors_cli_timeout_env(monkeypatch) -> None:
    monkeypatch.setenv("CODEXHUB_XAI_CLI_TIMEOUT_SECONDS", "1")
    clock = {"now": 0.0}

    def opener(request: Any, timeout: float = 20.0) -> _FakeResponse:
        return _FakeResponse({"error": "authorization_pending"})

    with pytest.raises(SubscriptionAuthError, match="timed out") as exc:
        xai_auth.poll_device_login(
            {
                "device_code": "dev-1",
                "token_endpoint": "https://auth.x.ai/oauth/token",
                "interval": 0,
                "expires_in": 600,
            },
            opener=opener,
            sleeper=lambda _seconds: clock.__setitem__("now", clock["now"] + 1),
            now=lambda: clock["now"],
        )
    assert exc.value.classification == "auth-required"
