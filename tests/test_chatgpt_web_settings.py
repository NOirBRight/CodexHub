from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

import chatgpt_web_checks
import chatgpt_web_runtime as runtime
import chatgpt_web_settings
import test_chatgpt_web_runtime as runtime_fixtures
from tests.gateway_harness import GATEWAY_CLIENT_KEY, GatewayHarness, request_gateway


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


@contextmanager
def _failing_runtime_startup(home: Path, entered: Path) -> Iterator[Callable[[], None]]:
    fail_startup = home / "web-home" / "fail-startup"
    startup: dict[str, str] = {}

    def start_runtime():
        try:
            runtime_fixtures._start_runtime(home)
        except runtime.RuntimeError_ as error:
            startup["error"] = str(error)

    worker = threading.Thread(target=start_runtime)
    worker.start()
    try:
        deadline = time.monotonic() + 10
        while not entered.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert entered.exists(), startup.get("error", "fake runtime did not start")

        def fail():
            fail_startup.touch()
            worker.join(timeout=10)
            assert not worker.is_alive(), "failed startup did not finish"
            assert "error" in startup

        yield fail
    finally:
        fail_startup.parent.mkdir(parents=True, exist_ok=True)
        fail_startup.touch()
        worker.join(timeout=10)


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
    assert "img-src 'self' data:" in headers.get("Content-Security-Policy", "")
    assert b'nonce="__CODEXHUB_CSP_NONCE__"' not in body
    assert b'history.replaceState(null, "", window.location.pathname)' in body
    assert b'const key = "codexhub.runtime-settings.session"' in body
    assert b'sessionStorage.setItem(key, result.session)' in body
    assert b'finish-login-button' not in body
    assert b'/api/login/finish' not in body
    assert b'id="mode"' not in body
    assert b'name="mode"' not in body
    assert b"browser-only" not in body or b'legacyBrowserOnly' in body
    assert b"studio-steps" in body
    assert b"Codex Native2" in body
    assert b"Tunnels Read + Use" in body or b"Tunnels Read + Use" in body.lower()
    assert b"platform.openai.com/tunnels" in body
    assert b"platform.openai.com/api-keys" in body
    assert b"simulate-" not in body
    assert b"prototype-banner" not in body
    assert b"check_runtime" not in body
    assert b"cdn." not in body
    assert b'mode: "full"' in body
    assert b"/codexhub.svg" in body
    assert b"/openai.svg" in body
    assert b"/api/tool-probe" in body
    assert b"data.tool_probe" in body or b"tool_probe" in body
    # Managed browser is the default account path; daily-browser extension is optional.
    assert b"/api/login" in body
    assert b'action === "open-login"' in body
    assert b"const openLogin" in body
    assert b"Open sign-in window" in body
    assert "打开登录窗口".encode() in body
    assert b"dailyBrowserOption" in body
    assert b"Prefer your daily Chrome session?" in body
    assert b"Where: CodexHub sign-in window" in body
    assert "操作位置：CodexHub 登录窗口".encode() in body
    account_js = body.split(b"const stepContent", 1)[1].split(b"if (step === 1)", 1)[0]
    assert b'"open-login"' in account_js
    assert b"dailyBrowserOption" in account_js
    assert account_js.find(b'"open-login"') < account_js.find(b"dailyBrowserOption")
    assert b'"open-chatgpt"' in account_js
    assert account_js.find(b"dailyBrowserOption") < account_js.find(b'"open-chatgpt"')
    assert b"details class=\"advanced\"" in account_js or b"details class='advanced'" in account_js or b'details class="advanced"' in account_js

    for asset_path, needle in (("/codexhub.svg", b"<svg"), ("/openai.svg", b"<svg")):
        status, asset_headers, asset_body = _call(server, asset_path)
        assert status == 200, asset_body
        assert "svg" in asset_headers.get("Content-Type", "")
        assert needle in asset_body

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


def test_coding_setup_save_forces_full_mode_and_reuses_secret(settings_server):
    server, home = settings_server
    session = _session(server)
    secret = "sk-local-runtime-secret-do-not-return"
    update = {
        "mode": "full",
        "connector_name": "Codex Native2",
        "tunnel": {
            "tunnel_id": "tunnel_" + ("a" * 32),
            "runtime_key": {"action": "replace", "value": secret},
        },
    }
    status, _headers, body = _call(
        server,
        "/api/settings",
        method="POST",
        body=update,
        headers={"Origin": server.origin},
        session=session,
    )
    assert status == 200, body
    assert secret.encode() not in body
    payload = json.loads(body)
    assert payload["saved"]["mode"] == "full"
    assert payload["saved"]["connector_name"] == "Codex Native2"
    assert payload["saved"]["configuration_complete"] is True
    assert payload["saved"]["tunnel"]["runtime_key_configured"] is True
    assert "runtime_key" not in payload["saved"]["tunnel"]

    status, _headers, body = _call(server, "/api/status", session=session)
    assert status == 200, body
    assert secret.encode() not in body
    status_payload = json.loads(body)
    assert status_payload["settings"]["saved"]["mode"] == "full"
    assert "tool_probe" in status_payload
    assert status_payload["tool_probe"]["state"] in {
        "not_run", "running", "passed", "failed", "stale", "blocked", "cancelled"
    }
    assert status_payload["coding_setup_complete"] is False

    keep = {
        "mode": "full",
        "connector_name": "Codex Native2",
        "tunnel": {
            "tunnel_id": "tunnel_" + ("a" * 32),
            "runtime_key": {"action": "keep"},
        },
    }
    status, _headers, body = _call(
        server,
        "/api/settings",
        method="POST",
        body=keep,
        headers={"Origin": server.origin},
        session=session,
    )
    assert status == 200, body
    assert secret.encode() not in body
    assert json.loads(body)["saved"]["tunnel"]["runtime_key_configured"] is True
    disk = (home / "runtime-settings.json").read_text(encoding="utf-8")
    assert secret in disk


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


@pytest.mark.parametrize("attempt", range(20 if os.name == "nt" else 1))
def test_origin_and_host_checks_reject_foreign_mutations(settings_server, attempt):
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


def test_settings_save_does_not_disrupt_inflight_gateway_request(settings_server):
    server, _home = settings_server
    response_body = {
        "id": "chatcmpl_settings_concurrency",
        "object": "chat.completion",
        "model": "glm-5.2",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "hello-chat"},
                "finish_reason": "stop",
            }
        ],
    }

    with GatewayHarness() as gateway:
        assert gateway.stub is not None
        gateway.set_json_response(response_body)
        gateway.stub.hold_after_headers = threading.Event()

        with ThreadPoolExecutor(max_workers=1) as pool:
            request = pool.submit(
                request_gateway,
                gateway.host,
                gateway.port,
                "POST",
                "/v1/responses",
                body=json.dumps(
                    {"model": "volc/glm-5.2", "input": "hello", "stream": False}
                ).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {GATEWAY_CLIENT_KEY}",
                    "Content-Type": "application/json",
                    "Connection": "close",
                },
                timeout=8.0,
            )
            try:
                assert gateway.stub.headers_sent.wait(timeout=3)
                session = _session(server)
                status, _headers, body = _call(
                    server,
                    "/api/settings",
                    method="POST",
                    body={"connector_name": "CodexHub edited during request"},
                    headers={"Origin": server.origin},
                    session=session,
                )
                assert status == 200, body
                assert json.loads(body)["saved"]["connector_name"] == (
                    "CodexHub edited during request"
                )
            finally:
                gateway.stub.hold_after_headers.set()

            response = request.result(timeout=8)

        assert response.status == 200, response.body
        payload = json.loads(response.body)
        assert payload["output"][0]["content"][0]["text"] == "hello-chat"
        assert gateway.stub.captures[0].path.endswith("/chat/completions")
        sent = json.loads(gateway.stub.captures[0].body)
        assert sent["model"] == "glm-5.2"
        assert gateway.stub.captures[0].headers["authorization"] == (
            "Bearer volc-test-token"
        )


def test_settings_http_does_not_claim_failed_first_startup_config_is_active(
    settings_server, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    server, home = settings_server
    entered = tmp_path / "serve-entered"
    archive = runtime_fixtures._archive(tmp_path, runtime_fixtures._delayed_start_failure_script(entered))
    pin = runtime_fixtures._pin_for(tmp_path, archive.read_bytes())
    monkeypatch.setenv("CODEXHUB_CHATGPT_WEB_PIN", str(pin))
    assert runtime.install_runtime(home, archive)["installed"] is True
    runtime.save_settings(home, {"options": {"context_window": 131072}})

    try:
        with _failing_runtime_startup(home, entered) as fail:
            config = json.loads((home / "web-home" / "config.json").read_text(encoding="utf-8"))
            assert config["contextWindow"] == 131072
            fail()

            session = _session(server)
            status, _headers, body = _call(server, "/api/settings", session=session)
            assert status == 200, body
            snapshot = json.loads(body)
            assert snapshot["active"] is None
            assert snapshot["active_state"] == "unavailable"
            assert snapshot["saved"]["options"]["context_window"] == 131072
            assert snapshot["pending_restart"] is True
    finally:
        runtime.stop_runtime(home, disable=True)


def test_settings_http_keeps_last_loaded_values_after_failed_restart(
    settings_server, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    server, home = settings_server
    good_dir = tmp_path / "good"
    good_dir.mkdir()
    good_archive = runtime_fixtures._archive(good_dir, runtime_fixtures._fixture_script(good_dir / "served"))
    good_pin = runtime_fixtures._pin_for(good_dir, good_archive.read_bytes())
    monkeypatch.setenv("CODEXHUB_CHATGPT_WEB_PIN", str(good_pin))
    assert runtime.install_runtime(home, good_archive)["installed"] is True
    runtime.save_settings(home, {"options": {"context_window": 131072}})
    runtime_fixtures._start_runtime(home)

    try:
        session = _session(server)
        active_path = home / "active-runtime-settings.json"
        assert active_path.is_file()

        # A healthy legacy runtime can establish its active snapshot without restarting.
        active_path.unlink()
        status, _headers, body = _call(server, "/api/settings", session=session)
        assert status == 200, body
        migrated = json.loads(body)
        assert migrated["active_state"] == "loaded"
        assert migrated["active"]["options"]["context_window"] == 131072
        assert active_path.is_file()

        status, _headers, body = _call(
            server,
            "/api/settings",
            method="POST",
            body={"options": {"context_window": 262144}},
            headers={"Origin": server.origin},
            session=session,
        )
        assert status == 200, body
        assert json.loads(body)["pending_restart"] is True
        runtime.stop_runtime(home, disable=False)

        bad_dir = tmp_path / "bad"
        bad_dir.mkdir()
        entered = bad_dir / "serve-entered"
        bad_archive = runtime_fixtures._archive(
            bad_dir, runtime_fixtures._delayed_start_failure_script(entered)
        )
        bad_pin = runtime_fixtures._pin_for(bad_dir, bad_archive.read_bytes())
        monkeypatch.setenv("CODEXHUB_CHATGPT_WEB_PIN", str(bad_pin))
        assert runtime.install_runtime(home, bad_archive)["installed"] is True
        with _failing_runtime_startup(home, entered) as fail:
            config = json.loads((home / "web-home" / "config.json").read_text(encoding="utf-8"))
            assert config["contextWindow"] == 262144
            fail()

            status, _headers, body = _call(server, "/api/settings", session=session)
            assert status == 200, body
            snapshot = json.loads(body)
            assert snapshot["active_state"] == "loaded"
            assert snapshot["active"]["options"]["context_window"] == 131072
            assert snapshot["saved"]["options"]["context_window"] == 262144
            assert snapshot["pending_restart"] is True
    finally:
        runtime.stop_runtime(home, disable=True)


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
    started = runtime_fixtures._start_runtime(home)
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
        restarted = runtime_fixtures._start_runtime(home)
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


@pytest.mark.parametrize(
    ("compatible", "running", "restart", "control", "reason"),
    [
        (False, True, True, None, "component_upgrade_required"),
        (False, False, False, None, "component_upgrade_required"),
        (True, True, True, runtime.LOGIN_CONTROL, "component_restart_required"),
        (True, True, False, None, "component_restart_required"),
    ],
)
def test_login_reports_component_action_without_starting(
    settings_server, monkeypatch, compatible, running, restart, control, reason
):
    server, _home = settings_server
    session = _session(server)
    monkeypatch.setattr(runtime, "build_status", lambda home: {
        "installed": True, "component": {"compatible": compatible},
        "process": {"running": running}, "restart_required": restart,
        "login": {"control": control},
    })
    calls = []
    monkeypatch.setattr(runtime, "open_login", lambda home: calls.append(home))
    status, _, body = _call(server, "/api/login", method="POST", body={},
                            headers={"Origin": server.origin}, session=session)
    assert status == 409
    assert json.loads(body)["error_code"] == reason
    assert calls == []
    status, _, body = _call(server, "/api/status", session=session)
    assert status == 200
    assert json.loads(body)["runtime"]["login_blocked_reason"] == reason


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


def test_browser_import_requires_settings_origin_and_session(settings_server, monkeypatch):
    import chatgpt_web_browser_account as accounts
    server, home = settings_server
    calls = []
    def imported(request_home, cookies):
        calls.append((request_home, cookies))
        return {"ok": True, "authenticated": True, "restart_required": True}
    monkeypatch.setattr(accounts, "import_session", imported)
    session = _session(server)
    for origin, credential, expected in [("https://attacker.test", session, 403),
                                         (server.origin, None, 401)]:
        status, _, _ = _call(server, "/api/browser-session", method="POST",
                            body={"cookies": []}, headers={"Origin": origin}, session=credential)
        assert status == expected
    assert calls == []
    status, _, body = _call(server, "/api/browser-session", method="POST",
                            body={"cookies": []}, headers={"Origin": server.origin}, session=session)
    assert status == 200
    assert json.loads(body)["restart_required"] is True
    assert calls == [(home, [])]


def test_browser_connector_download_is_authenticated_and_scoped(settings_server):
    import io
    import zipfile
    server, _ = settings_server
    status, _, _ = _call(server, "/api/browser-extension")
    assert status == 401
    status, _, body = _call(server, "/api/browser-extension", session=_session(server))
    assert status == 200
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["host_permissions"] == ["https://chatgpt.com/*"]
        assert set(manifest["permissions"]) == {"activeTab", "scripting", "cookies"}
        assert "background" not in manifest


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
        "../src-python/chatgpt_web_settings_codexhub.svg"
    ] == "src-python/chatgpt_web_settings_codexhub.svg"
    assert resources[
        "../src-python/chatgpt_web_settings_openai.svg"
    ] == "src-python/chatgpt_web_settings_openai.svg"
    assert resources[
        "resources/chatgpt-web-runtime/chatgpt_web_runtime_pin.json"
    ] == "config/chatgpt_web_runtime_pin.json"
    assert "resources/chatgpt-web-runtime/*.tar.gz" not in resources
    assert "resources/chatgpt-web-runtime/*.zip" not in resources
    linux = json.loads((repo / "src-tauri" / "tauri.linux.conf.json").read_text(encoding="utf-8"))
    windows = json.loads((repo / "src-tauri" / "tauri.windows.conf.json").read_text(encoding="utf-8"))
    assert linux["bundle"]["resources"]["resources/chatgpt-web-runtime/*.tar.gz"] == "config"
    assert windows["bundle"]["resources"]["resources/chatgpt-web-runtime/*.zip"] == "config"
    assert "../config/chatgpt_web_runtime_pin.json" not in resources
    windows_builder = (repo / "scripts" / "build-windows-portable.ps1").read_text(encoding="utf-8-sig")
    linux_builder = (repo / "scripts" / "build-linux-portable.sh").read_text(encoding="utf-8")
    windows_release = (repo / "scripts" / "build-windows-release.ps1").read_text(encoding="utf-8-sig")
    linux_release = (repo / "scripts" / "build-linux-release.sh").read_text(encoding="utf-8")
    assert '"src-python"' in windows_builder
    assert '"bundle"]["resources"]' in linux_builder
    for builder in (windows_builder, linux_builder, windows_release, linux_release):
        assert "prepare_chatgpt_web_runtime.py" in builder
