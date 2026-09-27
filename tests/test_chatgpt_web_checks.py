from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import chatgpt_web_checks as checks
import pytest


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "managed-runtime"
    web_home = home / "web-home"
    storage = web_home / "browser" / "storage-state.json"
    storage.parent.mkdir(parents=True)
    storage.write_text('{"cookies":[],"origins":[]}\n', encoding="utf-8")
    storage.with_name(storage.name + ".verified.json").write_text(
        '{"version":1,"authenticated":true,"verifiedAt":"2026-09-27T00:00:00Z"}\n',
        encoding="utf-8",
    )
    config = {
        "version": 3,
        "mode": "full",
        "appName": "Codex Native2",
        "automaticAppName": "Codex Native2",
        "controlToken": "synthetic-control-token-for-tests-only",
        "storageStatePath": str(storage),
        "chromeExecutablePath": "/synthetic/chromium",
    }
    web_home.joinpath("config.json").write_text(json.dumps(config), encoding="utf-8")
    return home


class _FakeBrowser:
    def __init__(self, _home: Path, _config: dict):
        self.operations: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return None

    def operation(self, operation: str, *, detect_capabilities: bool = False):
        self.operations.append(operation)
        if operation == "inspect" and detect_capabilities:
            return {"value": {
                "authenticated": True,
                "temporary": True,
                "solAvailable": True,
                "extraHighAvailable": True,
                "proAvailable": False,
            }}
        if operation == "verify":
            return {"text": "Codex Native2"}
        if operation == "smoke":
            return {"value": {"effort": "high", "response": "CODEXHUB_SMOKE_OK"}}
        raise AssertionError(operation)


def _install_fake_runtime(monkeypatch, home: Path, *, active_http: int = 0, active_browser: int = 0):
    import chatgpt_web_runtime as runtime

    monkeypatch.setattr(runtime, "_process_record", lambda _home: {"port": 18090})
    monkeypatch.setattr(runtime, "_pin_compatible", lambda *_args: True)
    monkeypatch.setattr(checks, "_PinnedBrowserSession", _FakeBrowser)
    monkeypatch.setattr(
        runtime,
        "_run_doctor",
        lambda *_args: {"checks": [{"id": "tunnel-runtime", "status": "ok"}]},
    )
    monkeypatch.setattr(runtime, "_find_entry", lambda _root: Path("/synthetic/entry"))
    monkeypatch.setattr(runtime, "_runtime_root", lambda _home: Path("/synthetic/runtime"))
    def request(url: str, _token: str, *, timeout: float = 5.0):
        if url.endswith("/healthz"):
            return {"active_http_turns": active_http, "active_browser_turns": active_browser}
        if url.endswith("/v1/models"):
            return {"models": [
                {"slug": "official/gpt-5.6-sol", "name": "official model"},
                {"slug": "chatgpt-web/gpt-5.6-sol", "display_name": "GPT 5.6 Sol",
                 "supported_reasoning_levels": [{"effort": "medium"}, {"effort": "high"}],
                 "input_modalities": ["text", "image"]},
                {"slug": "chatgpt-web/gpt-5.6-luna", "name": "GPT 5.6 Luna", "efforts": ["low"]},
            ]}
        raise AssertionError(url)
    monkeypatch.setattr(checks, "_request_json", request)


def test_explicit_check_reports_independent_evidence_and_only_web_models(monkeypatch, tmp_path):
    home = _home(tmp_path)
    _install_fake_runtime(monkeypatch, home)

    result = checks.check_runtime(home)

    assert result["state"] == "ready"
    assert result["login"] == {
        "state": "signed_in",
        "capabilities": {"solAvailable": True, "extraHighAvailable": True, "proAvailable": False},
    }
    assert result["browser"]["state"] == "passed"
    assert result["connector"] == {"state": "selectable", "name": "Codex Native2"}
    assert result["text_ready"] is True
    assert result["tools_ready"] is True
    assert result["models"] == [
        {"id": "chatgpt-web/gpt-5.6-sol", "display_name": "GPT 5.6 Sol",
         "efforts": ["medium", "high"], "image_input": True},
        {"id": "chatgpt-web/gpt-5.6-luna", "display_name": "GPT 5.6 Luna",
         "efforts": ["low"], "image_input": False},
    ]
    assert checks.cached_checks(home)["cache_state"] == "current"


def test_busy_check_does_not_open_browser_and_keeps_model_evidence(monkeypatch, tmp_path):
    home = _home(tmp_path)
    _install_fake_runtime(monkeypatch, home, active_browser=1)

    def should_not_open(*_args, **_kwargs):
        raise AssertionError("a busy runtime must not open the browser helper")

    monkeypatch.setattr(checks, "_PinnedBrowserSession", should_not_open)
    result = checks.check_runtime(home)

    assert result["state"] == "blocked"
    assert result["reason"] == "active_turns"
    assert result["active_turns"] == {"http": 0, "browser": 1}
    assert result["login"]["state"] == "not_checked"
    assert result["models"]


def test_cached_evidence_is_invalidated_by_active_config_or_account_change(monkeypatch, tmp_path):
    home = _home(tmp_path)
    _install_fake_runtime(monkeypatch, home)
    checks.check_runtime(home)
    assert checks.cached_checks(home)["state"] == "ready"

    config_path = home / "web-home" / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["automaticAppName"] = "Different connector"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    stale = checks.cached_checks(home)
    assert stale["cache_state"] == "stale"
    assert stale["state"] == "unchecked"
    assert stale["reason"] == "active_config_or_account_changed"


def test_cached_evidence_is_invalidated_when_managed_account_state_changes(monkeypatch, tmp_path):
    home = _home(tmp_path)
    _install_fake_runtime(monkeypatch, home)
    checks.check_runtime(home)
    assert checks.cached_checks(home)["cache_state"] == "current"

    storage = home / "web-home" / "browser" / "storage-state.json"
    storage.write_text('{"cookies":[{"name":"changed"}],"origins":[]}\n', encoding="utf-8")
    stale = checks.cached_checks(home)

    assert stale["cache_state"] == "stale"
    assert stale["reason"] == "active_config_or_account_changed"


def test_cached_evidence_is_invalidated_after_runtime_process_changes(monkeypatch, tmp_path):
    home = _home(tmp_path)
    _install_fake_runtime(monkeypatch, home)
    checks.check_runtime(home)
    assert checks.cached_checks(home)["cache_state"] == "current"

    import chatgpt_web_runtime as runtime

    monkeypatch.setattr(runtime, "_process_record", lambda _home: {"port": 18091, "pid": 4302})
    stale = checks.cached_checks(home)

    assert stale["cache_state"] == "stale"
    assert stale["reason"] == "runtime_instance_changed"


def test_cached_evidence_does_not_expire_while_account_and_runtime_are_unchanged(monkeypatch, tmp_path):
    home = _home(tmp_path)
    _install_fake_runtime(monkeypatch, home)
    checks.check_runtime(home)

    cache_path = home / "readiness-checks.json"
    document = json.loads(cache_path.read_text(encoding="utf-8"))
    document["result"]["checked_at"] = "2020-01-01T00:00:00Z"
    cache_path.write_text(json.dumps(document), encoding="utf-8")

    cached = checks.cached_checks(home)

    assert cached["cache_state"] == "current"
    assert cached["state"] == "ready"


def test_unavailable_active_turn_count_fails_closed_without_browser(monkeypatch, tmp_path):
    home = _home(tmp_path)
    _install_fake_runtime(monkeypatch, home)
    monkeypatch.setattr(checks, "_request_json", lambda *_args, **_kwargs: {"active_http_turns": 0})
    monkeypatch.setattr(
        checks,
        "_PinnedBrowserSession",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("browser opened")),
    )

    result = checks.check_runtime(home)

    assert result["state"] == "blocked"
    assert result["reason"] == "active_turn_state_unavailable"
    assert result["tools_ready"] is False


def test_loopback_redirect_does_not_forward_runtime_control_token(monkeypatch, tmp_path):
    home = _home(tmp_path)
    captured: list[str | None] = []

    class DestinationHandler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            captured.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *_args):
            return

    destination = ThreadingHTTPServer(("127.0.0.1", 0), DestinationHandler)
    destination_thread = threading.Thread(target=destination.serve_forever, daemon=True)
    destination_thread.start()

    class RedirectHandler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{destination.server_port}/capture")
            self.end_headers()

        def log_message(self, *_args):
            return

    source = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    source_thread = threading.Thread(target=source.serve_forever, daemon=True)
    source_thread.start()
    import chatgpt_web_runtime as runtime

    monkeypatch.setattr(runtime, "_process_record", lambda _home: {"port": source.server_port, "pid": 4301})
    monkeypatch.setattr(runtime, "_pin_compatible", lambda *_args: True)

    try:
        result = checks.check_runtime(home)
    finally:
        source.shutdown()
        destination.shutdown()
        source.server_close()
        destination.server_close()
        source_thread.join(timeout=2)
        destination_thread.join(timeout=2)

    assert result["reason"] == "active_turn_state_unavailable"
    assert captured == []


def test_evidence_fingerprint_does_not_copy_config_or_account_values(tmp_path):
    home = _home(tmp_path)
    result = checks.cached_checks(home)
    assert result["cache_state"] == "missing"
    assert "synthetic-control-token-for-tests-only" not in json.dumps(result)
    assert "cookies" not in json.dumps(result)


@pytest.mark.skipif(
    not os.environ.get("CODEXHUB_ISSUE_590_RUNTIME_ROOT"),
    reason="explicit signed-out pinned-helper smoke only",
)
def test_pinned_helper_does_not_report_an_empty_profile_as_authenticated(monkeypatch, tmp_path):
    runtime_root = Path(os.environ["CODEXHUB_ISSUE_590_RUNTIME_ROOT"]).resolve()
    chrome_value = os.environ.get("CODEXHUB_ISSUE_590_CHROME")
    if chrome_value:
        chrome = Path(chrome_value).resolve()
    else:
        import shutil

        chrome = Path(shutil.which("chromium") or shutil.which("google-chrome") or "")
    if not runtime_root.is_dir() or not chrome.is_file():
        pytest.fail("explicit pinned-helper smoke needs the code-only runtime archive and Chromium")

    home = _home(tmp_path)
    config_path = home / "web-home" / "config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    storage = Path(config["storageStatePath"])
    storage.write_text('{"cookies":[],"origins":[]}\n', encoding="utf-8")
    storage.with_name(storage.name + ".verified.json").write_text(
        '{"version":1,"authenticated":false,"verifiedAt":"2026-09-27T00:00:00Z"}\n',
        encoding="utf-8",
    )
    config["chromeExecutablePath"] = str(chrome)
    config_path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setattr(checks.runtime, "_runtime_root", lambda _home: runtime_root)

    with checks._PinnedBrowserSession(home, config) as browser:
        with pytest.raises(RuntimeError) as failure:
            browser.operation("inspect", detect_capabilities=True)
        assert str(failure.value) == "browser_check_failed"
