from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

import chatgpt_web_connection


TOKEN = "managed-service-token"


def _status(port: int) -> dict:
    return {
        "installed": True,
        "process": {"running": True, "listen_host": "127.0.0.1", "port": port},
    }


def _write_runtime_key(home: Path) -> None:
    web_home = home / "web-home"
    web_home.mkdir(parents=True)
    (web_home / "config.json").write_text(json.dumps({"controlToken": TOKEN}), encoding="utf-8")


def test_connection_overrides_are_limited_to_the_current_managed_loopback(tmp_path: Path) -> None:
    home = tmp_path / "runtime"
    _write_runtime_key(home)
    status = _status(43123)

    assert chatgpt_web_connection.resolve_connection(home=home, status=status) == (
        "http://127.0.0.1:43123",
        TOKEN,
    )
    assert chatgpt_web_connection.resolve_connection(
        "http://127.0.0.1:43123/", TOKEN, home=home, status=status
    ) == ("http://127.0.0.1:43123", TOKEN)
    for base_url, key in (
        ("https://127.0.0.1:43123", TOKEN),
        ("http://localhost:43123", TOKEN),
        ("http://127.0.0.1:43124", TOKEN),
        ("http://example.test:43123", TOKEN),
        ("", "a different credential"),
        ("", "\N{SNOWMAN}"),
    ):
        with pytest.raises(ValueError):
            chatgpt_web_connection.resolve_connection(base_url, key, home=home, status=status)


def test_connection_check_makes_an_authenticated_read_only_models_request(tmp_path: Path) -> None:
    class ModelsHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: object) -> None:
            return

        def do_GET(self) -> None:  # noqa: N802
            assert self.path == "/v1/models"
            assert self.headers.get("Authorization") == f"Bearer {TOKEN}"
            body = b'{"object":"list","data":[{"id":"chatgpt-web/gpt-5.6-sol"}]}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), ModelsHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    home = tmp_path / "runtime"
    _write_runtime_key(home)
    try:
        result = chatgpt_web_connection.check_connection(
            home=home, status=_status(server.server_port)
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert result == {
        "ok": True,
        "reachable": True,
        "base_url": f"http://127.0.0.1:{server.server_port}",
        "credential_configured": True,
        "model_count": 1,
    }
    assert TOKEN not in json.dumps(result)


def test_provider_selection_uses_enabled_gateway_models_and_preserves_legacy_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from providers_config import ModelConfig, ProviderConfig, save_providers

    codex_home = tmp_path / "codex"
    provider_path = codex_home / "proxy" / "config" / "providers.toml"
    provider_path.parent.mkdir(parents=True)
    provider = ProviderConfig(
        id="chatgpt-web",
        name="ChatGPT Web",
        base_url="",
        api_key="",
        enabled=True,
        models=[
            ModelConfig(id="gpt-5.6-sol", enabled=True, gateway_exported=True),
            ModelConfig(id="gpt-5.6-luna", enabled=False, gateway_exported=True),
            ModelConfig(id="not-exported", enabled=True, gateway_exported=False),
        ],
    )
    save_providers([provider], provider_path)
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    status = {"models": [
        {"id": "chatgpt-web/gpt-5.6-sol"},
        {"id": "chatgpt-web/gpt-5.6-luna"},
    ]}

    assert chatgpt_web_connection.selected_model_ids(status) == frozenset(
        {"chatgpt-web/gpt-5.6-sol"}
    )

    provider.models.clear()
    save_providers([provider], provider_path)
    assert chatgpt_web_connection.selected_model_ids(status) == frozenset(
        {"chatgpt-web/gpt-5.6-sol", "chatgpt-web/gpt-5.6-luna"}
    )

    provider.enabled = False
    save_providers([provider], provider_path)
    assert chatgpt_web_connection.selected_model_ids(status) == frozenset()
