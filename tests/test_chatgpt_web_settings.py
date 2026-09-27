from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

import chatgpt_web_checks
import chatgpt_web_runtime as runtime
import chatgpt_web_settings
import test_chatgpt_web_runtime as runtime_fixtures


@pytest.fixture
def settings_server(tmp_path: Path, request):
    home = tmp_path / "managed-runtime"
    server = chatgpt_web_settings.create_server(home, **getattr(request, "param", {}))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, home
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _url(server: chatgpt_web_settings._SettingsHTTPServer, path: str) -> str:
    return server.origin + path


def _call(
    server: chatgpt_web_settings._SettingsHTTPServer,
    path: str,
    *,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    session: str | None = None,
):
    request_headers = {"Accept": "application/json", **(headers or {})}
    if session:
        request_headers.setdefault("Authorization", f"Bearer {session}")
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")
    request = urllib.request.Request(
        _url(server, path), data=data, headers=request_headers, method=method
    )
    client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        response = client.open(request, timeout=3)
    except urllib.error.HTTPError as error:
        response = error
    return response.status, response.headers, response.read()


def _session(server: chatgpt_web_settings._SettingsHTTPServer) -> str:
    status, headers, payload = _call(
        server,
        "/api/session",
        method="POST",
        body={"bootstrap": server.bootstrap_token},
        headers={"Origin": server.origin},
    )
    assert status == 200, payload
    assert b'"ok":true' in payload
    assert headers.get_all("Set-Cookie", []) == []
    return json.loads(payload)["session"]


def _fixture_streaming_health_server() -> str:
    source = runtime_fixtures._fixture_health_server()
    needle = '    def do_GET(self):\n        if self.path != "/healthz":'
    replacement = '''    def do_GET(self):
        if self.path == "/stream":
            import time
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for phase in ("before", "after"):
                if phase == "after":
                    deadline = time.monotonic() + 15
                    while not (home / "release-stream").exists():
                        if time.monotonic() > deadline:
                            return
                        time.sleep(0.02)
                value = {"phase": phase, "context_window": config["contextWindow"]}
                self.wfile.write(("data: " + json.dumps(value) + "\\n\\n").encode())
                self.wfile.flush()
            return
        if self.path != "/healthz":'''
    assert needle in source
    return source.replace(needle, replacement, 1)


def test_page_is_local_and_bootstrap_fragment_is_one_use(settings_server):
    server, _home = settings_server
    page_url = chatgpt_web_settings.settings_url(server)
    assert page_url.startswith(server.origin + "/#")
    assert "?" not in page_url

    status, headers, body = _call(server, "/")
    assert status == 200
    assert headers.get("Cache-Control") == "no-store"
    assert headers.get("Referrer-Policy") == "no-referrer"
    assert "script-src 'nonce-" in headers.get("Content-Security-Policy", "")
    assert b'nonce="__CODEXHUB_CSP_NONCE__"' not in body
    assert b'history.replaceState(null, "", window.location.pathname)' in body
    assert b'const key = "codexhub.runtime-settings.session"' in body
    assert b'sessionStorage.setItem(key, result.session)' in body
    assert b'finish-login-button' not in body
    assert b'/api/login/finish' not in body
    assert b'runtime.active_mode === "browser-only"' in body
    assert b"check_runtime" not in body
    assert b"cdn." not in body

    session = _session(server)
    status, _headers, _payload = _call(server, "/api/settings", session=session)
    assert status == 200
    status, _headers, payload = _call(
        server,
        "/api/session",
        method="POST",
        body={"bootstrap": server.bootstrap_token},
        headers={"Origin": server.origin},
    )
    assert status == 401
    assert b"expired" in payload.lower()


def test_bootstrap_token_is_consumed_atomically_under_concurrent_exchange(settings_server):
    server, _home = settings_server
    bootstrap = server.bootstrap_token
    barrier = threading.Barrier(2)

    def exchange():
        barrier.wait(timeout=2)
        return _call(
            server,
            "/api/session",
            method="POST",
            body={"bootstrap": bootstrap},
            headers={"Origin": server.origin},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _index: exchange(), range(2)))
    assert sorted(result[0] for result in results) == [200, 401]


def test_origin_and_host_checks_reject_foreign_mutations(settings_server):
    server, _home = settings_server
    status, _headers, _body = _call(
        server,
        "/api/session",
        method="POST",
        body={"bootstrap": server.bootstrap_token},
        headers={"Origin": "http://attacker.example"},
    )
    assert status == 403

    status, _headers, _body = _call(
        server,
        "/api/session",
        method="POST",
        body={"bootstrap": server.bootstrap_token},
        headers={"Origin": server.origin, "Host": "attacker.example"},
    )
    assert status == 421

    status, _headers, _body = _call(
        server,
        "/api/settings",
        method="POST",
        body={"mode": "full"},
        headers={"Origin": server.origin},
    )
    assert status == 401


def test_settings_http_preserves_replaces_and_clears_secret_without_returning_it(settings_server):
    server, home = settings_server
    session = _session(server)
    status, _headers, _payload = _call(server, "/api/settings", session=session)
    assert status == 200

    secret = "sk-local-runtime-secret-do-not-return"
    update = {
        "mode": "full",
        "connector_name": "Codex Native2",
        "tunnel": {
            "tunnel_id": "tunnel_0123456789abcdef0123456789abcdef",
            "runtime_key": {"action": "replace", "value": secret},
        },
        "options": {"headed": False, "auto_approve_tool_calls": False},
    }
    status, _headers, body = _call(server, "/api/settings", method="POST", body=update, headers={"Origin": server.origin}, session=session)
    assert status == 200, body
    assert secret.encode() not in body
    saved = json.loads(body)
    assert saved["saved"]["mode"] == "full"
    assert saved["saved"]["tunnel"]["runtime_key_configured"] is True

    status, _headers, body = _call(server, "/api/settings", session=session)
    assert status == 200
    assert secret.encode() not in body
    assert json.loads(body)["saved"]["tunnel"]["runtime_key_configured"] is True

    update["connector_name"] = "Codex Native2 Next"
    update["tunnel"]["runtime_key"] = {"action": "keep"}
    status, _headers, body = _call(server, "/api/settings", method="POST", body=update, headers={"Origin": server.origin}, session=session)
    assert status == 200, body
    assert secret.encode() not in body

    update["tunnel"]["runtime_key"] = {"action": "clear"}
    status, _headers, body = _call(server, "/api/settings", method="POST", body=update, headers={"Origin": server.origin}, session=session)
    assert status == 200, body
    assert json.loads(body)["saved"]["tunnel"]["runtime_key_configured"] is False
    assert secret.encode() not in (home / "runtime-settings.json").read_bytes()


@pytest.mark.skipif(os.name == "nt", reason="uses a Unix executable runtime fixture")
def test_settings_save_preserves_an_open_runtime_stream_until_explicit_restart(
    settings_server, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    server, home = settings_server
    archive = runtime_fixtures._archive(
        tmp_path,
        runtime_fixtures._fixture_script(
            tmp_path / "executed-marker", health_server=_fixture_streaming_health_server()
        ),
    )
    pin = runtime_fixtures._pin_for(tmp_path, archive.read_bytes())
    monkeypatch.setenv("CODEXHUB_CHATGPT_WEB_PIN", str(pin))
    assert runtime.install_runtime(home, archive)["installed"] is True
    runtime.save_settings(home, {"options": {"context_window": 131072}})
    started = runtime.start_runtime(home)
    release_stream = home / "web-home" / "release-stream"

    try:
        session = _session(server)
        config = json.loads((home / "web-home" / "config.json").read_text(encoding="utf-8"))
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{config['port']}/stream", timeout=10) as stream:
            before_line = stream.readline()
            assert before_line.startswith(b"data: ")
            before = json.loads(before_line.removeprefix(b"data: "))
            assert stream.readline() == b"\n"
            assert before == {"phase": "before", "context_window": 131072}

            save_started = time.monotonic()
            status, _headers, body = _call(
                server,
                "/api/settings",
                method="POST",
                body={"options": {"context_window": 262144}},
                headers={"Origin": server.origin},
                session=session,
            )
            save_duration = time.monotonic() - save_started
            assert status == 200, body
            saved = json.loads(body)
            assert save_duration < 3
            assert saved["pending_restart"] is True
            assert saved["active"]["options"]["context_window"] == 131072
            assert saved["saved"]["options"]["context_window"] == 262144
            assert not release_stream.exists()

            release_stream.touch()
            after_line = stream.readline()
            assert after_line.startswith(b"data: ")
            after = json.loads(after_line.removeprefix(b"data: "))
            assert stream.readline() == b"\n"
            assert after == {"phase": "after", "context_window": 131072}

        time.sleep(2)
        status, _headers, body = _call(server, "/api/settings", session=session)
        pending = json.loads(body)
        assert status == 200
        assert pending["pending_restart"] is True
        assert runtime.build_status(home)["process"]["pid"] == started["process"]["pid"]

        runtime.stop_runtime(home, disable=False)
        restarted = runtime.start_runtime(home)
        status, _headers, body = _call(server, "/api/settings", session=session)
        active = json.loads(body)
        assert status == 200
        assert restarted["process"]["pid"] != started["process"]["pid"]
        assert active["pending_restart"] is False
        assert active["active"]["options"]["context_window"] == 262144
    finally:
        release_stream.parent.mkdir(parents=True, exist_ok=True)
        release_stream.touch()
        runtime.stop_runtime(home, disable=True)


def test_invalid_save_keeps_last_configuration_and_runtime_lifecycle_files(settings_server):
    server, home = settings_server
    session = _session(server)
    process_record = {"pid": 123456, "private_home": str(home), "ownership": "test"}
    lifecycle = {"enabled": True, "restart_required": False, "admitting": True}
    (home / "process.json").write_text(json.dumps(process_record), encoding="utf-8")
    (home / "lifecycle.json").write_text(json.dumps(lifecycle), encoding="utf-8")

    valid = {"connector_name": "Before"}
    status, _headers, body = _call(server, "/api/settings", method="POST", body=valid, headers={"Origin": server.origin}, session=session)
    assert status == 200, body
    before = (home / "runtime-settings.json").read_bytes()

    status, _headers, body = _call(
        server,
        "/api/settings",
        method="POST",
        body={"tunnel": {"tunnel_id": "invalid", "runtime_key": {"action": "keep"}}},
        headers={"Origin": server.origin},
        session=session,
    )
    assert status == 400
    assert json.loads(body)["error_code"] == "tunnel_id_invalid"
    assert (home / "runtime-settings.json").read_bytes() == before
    assert json.loads((home / "process.json").read_text(encoding="utf-8")) == process_record
    assert json.loads((home / "lifecycle.json").read_text(encoding="utf-8")) == lifecycle


def test_settings_page_status_reads_cached_checks_without_running_explicit_check(settings_server, monkeypatch):
    server, _home = settings_server
    session = _session(server)
    explicit_calls = 0

    def forbidden_check(_home):
        nonlocal explicit_calls
        explicit_calls += 1
        raise AssertionError("a normal status read must not run an explicit browser check")

    monkeypatch.setattr(chatgpt_web_checks, "check_runtime", forbidden_check)
    status, _headers, body = _call(server, "/api/status", session=session)
    assert status == 200, body
    payload = json.loads(body)
    assert payload["readiness"]["cache_state"] in {"missing", "stale"}
    assert explicit_calls == 0


@pytest.mark.parametrize(
    ("status_changes", "settings_changes", "active_mode", "expected_text", "expected_tools"),
    [
        ({}, {}, "full", True, True),
        ({"running": False}, {}, "full", False, False),
        ({"compatible": False}, {}, "full", False, False),
        ({"disabled": True}, {}, "full", False, False),
        ({"restart_required": True}, {}, "full", False, False),
        ({"admitting": False}, {}, "full", False, False),
        ({"login": {"state": "signed_out"}}, {}, "full", False, False),
        ({"browser_smoke": {"state": "failed"}}, {}, "full", False, False),
        ({"tunnel": {"state": "not_ready"}}, {}, "full", True, False),
        ({"connector": {"selectable": False}}, {}, "full", True, False),
        (
            {},
            {"pending_restart": True, "saved": {"mode": "browser-only", "configuration_complete": True}},
            "full",
            True,
            True,
        ),
        ({}, {"saved": {"mode": "full", "configuration_complete": True}}, "browser-only", True, False),
    ],
)
def test_status_readiness_requires_live_compatible_runtime_and_active_mode(
    settings_server, monkeypatch, status_changes, settings_changes, active_mode, expected_text, expected_tools
):
    server, home = settings_server
    session = _session(server)
    runtime_status = {
        "installed": True,
        "component": {"compatible": True},
        "process": {"running": True},
        "disabled": False,
        "restart_required": False,
        "admitting": True,
        "login": {"state": "signed_in", "window": "closed"},
        "browser_smoke": {"state": "passed"},
        "tunnel": {"state": "ready"},
        "connector": {"selectable": True},
    }
    runtime_status.update(status_changes)
    if "running" in status_changes:
        runtime_status["process"] = {"running": status_changes["running"]}
    if "compatible" in status_changes:
        runtime_status["component"] = {"compatible": status_changes["compatible"]}
    settings = {
        "active": {"mode": active_mode, "configuration_complete": True},
        "saved": {"mode": "full" if active_mode == "browser-only" else active_mode, "configuration_complete": True},
        "pending_restart": False,
    }
    settings.update(settings_changes)
    cached = {
        "cache_state": "current",
        "state": "ready",
        "checked_at": "2026-09-27T00:00:00Z",
        "login": {"state": "signed_in"},
        "browser": {"state": "passed"},
        "connector": {"state": "selectable"},
        "tunnel": {"state": "ready"},
        "text_ready": True,
        "tools_ready": True,
    }
    monkeypatch.setattr(runtime, "read_settings", lambda _home: settings)
    monkeypatch.setattr(runtime, "build_status", lambda _home: runtime_status)
    monkeypatch.setattr(chatgpt_web_checks, "cached_checks", lambda _home: cached)

    status, _headers, body = _call(server, "/api/status", session=session)
    assert status == 200, body
    payload = json.loads(body)
    readiness = payload["readiness"]
    assert readiness["text_ready"] is expected_text
    assert readiness["tools_ready"] is expected_tools
    assert payload["runtime"]["active_mode"] == active_mode
    assert payload["runtime"]["settings_pending_restart"] is (settings["pending_restart"] is True)
    assert payload["settings"]["pending_restart"] is (settings["pending_restart"] is True)


def test_login_routes_open_and_cancel_separately(settings_server, monkeypatch):
    server, home = settings_server
    session = _session(server)
    calls = []

    def open_login(request_home):
        calls.append(("open", request_home))
        return {"ok": True}

    def close_login(request_home):
        calls.append(("cancel", request_home))
        return {"ok": True}

    monkeypatch.setattr(runtime, "open_login", open_login)
    monkeypatch.setattr(runtime, "close_login", close_login)
    headers = {"Origin": server.origin}
    status, _headers, body = _call(
        server, "/api/login", method="POST", body={}, headers=headers, session=session
    )
    assert status == 200, body
    assert json.loads(body)["started"] is True
    status, _headers, body = _call(
        server, "/api/login/cancel", method="POST", body={}, headers=headers, session=session
    )
    assert status == 200, body
    assert json.loads(body)["cancelled"] is True
    status, _headers, _body = _call(
        server, "/api/login/finish", method="POST", body={}, headers=headers, session=session
    )
    assert status == 404
    assert calls == [("open", home), ("cancel", home)]


@pytest.mark.parametrize("settings_server", [{"session_ttl": 0.01}], indirect=True)
def test_expired_session_cannot_read_settings(settings_server):
    server, _home = settings_server
    session = _session(server)
    time.sleep(0.02)
    status, _headers, body = _call(server, "/api/settings", session=session)
    assert status == 401
    assert b"expired" in body.lower()


def test_page_asset_is_declared_for_release_packages():
    repo = Path(__file__).resolve().parents[1]
    config = json.loads((repo / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))
    resources = config["bundle"]["resources"]
    assert resources["../src-python/chatgpt_web_settings.html"] == "src-python/chatgpt_web_settings.html"
    assert resources[
        "resources/chatgpt-web-runtime/chatgpt_web_runtime_pin.json"
    ] == "config/chatgpt_web_runtime_pin.json"
    assert resources["resources/chatgpt-web-runtime/*.tar.gz"] == "config"
    assert resources["resources/chatgpt-web-runtime/*.zip"] == "config"
    assert "../config/chatgpt_web_runtime_pin.json" not in resources
    windows_builder = (repo / "scripts" / "build-windows-portable.ps1").read_text(encoding="utf-8-sig")
    linux_builder = (repo / "scripts" / "build-linux-portable.sh").read_text(encoding="utf-8")
    windows_release = (repo / "scripts" / "build-windows-release.ps1").read_text(encoding="utf-8-sig")
    linux_release = (repo / "scripts" / "build-linux-release.sh").read_text(encoding="utf-8")
    assert '"src-python"' in windows_builder
    assert '"bundle"]["resources"]' in linux_builder
    for builder in (windows_builder, linux_builder, windows_release, linux_release):
        assert "prepare_chatgpt_web_runtime.py" in builder
