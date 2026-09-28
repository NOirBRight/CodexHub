"""Seam tests for the ChatGPT coding-setup tool probe (#598)."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest

import chatgpt_web_checks
import chatgpt_web_runtime as runtime
import chatgpt_web_settings
import chatgpt_web_tool_probe as probe


@pytest.fixture(autouse=True)
def _reset_probe_state():
    probe.reset_for_tests()
    yield
    probe.reset_for_tests()


@pytest.fixture
def settings_server(tmp_path: Path):
    home = tmp_path / "managed-runtime"
    server = chatgpt_web_settings.create_server(home)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, home
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _session(server: chatgpt_web_settings._SettingsHTTPServer) -> str:
    import urllib.request

    request = urllib.request.Request(
        server.origin + "/api/session",
        data=json.dumps({"bootstrap": server.bootstrap_token}).encode("utf-8"),
        headers={"Content-Type": "application/json", "Origin": server.origin},
        method="POST",
    )
    response = urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=3)
    payload = json.loads(response.read())
    return payload["session"]


def _call(server, path, *, method="GET", body=None, session=None, origin=None):
    import urllib.error
    import urllib.request

    headers = {"Accept": "application/json"}
    if session:
        headers["Authorization"] = f"Bearer {session}"
    if origin:
        headers["Origin"] = origin
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        server.origin + path, data=data, headers=headers, method=method
    )
    client = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        response = client.open(request, timeout=5)
    except urllib.error.HTTPError as error:
        response = error
    return response.status, json.loads(response.read().decode("utf-8"))


def _binding(suffix: str = "a") -> dict[str, str]:
    return {
        "active_config_sha256": f"config-{suffix}",
        "account_state_sha256": f"account-{suffix}",
        "runtime_instance_sha256": f"runtime-{suffix}",
    }


def _seed_settings(home: Path, *, mode: str = "full") -> None:
    runtime.save_settings(
        home,
        {
            "mode": mode,
            "connector_name": runtime.CONNECTOR_NAME,
            **(
                {
                    "tunnel": {
                        "tunnel_id": "tunnel_0123456789abcdef0123456789abcdef",
                        "runtime_key": {
                            "action": "replace",
                            "value": "sk-test-runtime-key-0123456789",
                        },
                    }
                }
                if mode == "full"
                else {}
            ),
        },
    )


def _ready_status(**overrides: Any) -> dict[str, Any]:
    base = {
        "installed": True,
        "disabled": False,
        "restart_required": False,
        "admitting": True,
        "component": {"compatible": True},
        "process": {
            "running": True,
            "port": 32123,
            "listen_host": "127.0.0.1",
            "pid": 4242,
        },
        "login": {"state": "signed_in"},
        "tunnel": {"state": "ready"},
        "connector": {"selectable": True, "name": runtime.CONNECTOR_NAME},
        "browser_smoke": {"state": "passed"},
        "models": [{"id": "chatgpt-web/gpt-test"}],
        "readiness_checks": {"capabilities_match": True},
    }
    base.update(overrides)
    return base


def _patch_common(monkeypatch, home: Path, *, binding: dict[str, str] | None = None, status: dict[str, Any] | None = None):
    current = binding if binding is not None else _binding()
    monkeypatch.setattr(chatgpt_web_checks, "_binding", lambda _home: dict(current))
    monkeypatch.setattr(runtime, "build_status", lambda _home, pin=None: _ready_status(**(status or {})))
    # When status overrides tunnel/connector, also align cached checks.
    tunnel_state = (status or {}).get("tunnel", {}).get("state", "ready") if isinstance((status or {}).get("tunnel"), dict) else "ready"
    connector_selectable = True
    if isinstance((status or {}).get("connector"), dict):
        connector_selectable = (status or {})["connector"].get("selectable") is True
    monkeypatch.setattr(
        chatgpt_web_checks,
        "cached_checks",
        lambda _home: {
            "tunnel": {"state": tunnel_state},
            "connector": {
                "state": "selectable" if connector_selectable else "not_checked",
                "name": runtime.CONNECTOR_NAME,
            },
            "login": {"state": "signed_in"},
            "tools_ready": tunnel_state == "ready" and connector_selectable,
            "text_ready": True,
            "cache_state": "current",
        },
    )
    monkeypatch.setattr(
        "chatgpt_web_connection.resolve_connection",
        lambda *args, **kwargs: ("http://127.0.0.1:32123", "service-key"),
    )
    return current


def test_successful_roundtrip_marks_passed(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _seed_settings(home)
    binding = _patch_common(monkeypatch, home)

    def exchange(request: probe._ProbeRequest) -> probe._ProbeOutcome:
        assert request.token
        assert request.probe_id
        assert request.base_url.startswith("http://127.0.0.1:")
        # No caller-supplied command surface exists on the request.
        assert not hasattr(request, "command")
        return probe._ProbeOutcome(ok=True, live_attempted=False)

    result = probe.start_probe(home, exchange=exchange, wait=True)
    assert result["state"] == "passed"
    assert result["reason"] is None
    assert result["generation"] == probe._generation_from_binding(binding)
    assert result["live_attempted"] is False
    assert probe.coding_setup_complete(home) is True


def test_missing_tunnel_blocks_without_impersonating_success(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _seed_settings(home)
    _patch_common(monkeypatch, home, status={"tunnel": {"state": "not_ready"}})
    result = probe.start_probe(home, exchange=lambda req: probe._ProbeOutcome(ok=True), wait=True)
    assert result["state"] == "blocked"
    assert result["reason"] == "tunnel_not_ready"
    assert probe.coding_setup_complete(home) is False


def test_missing_connector_blocks(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _seed_settings(home)
    _patch_common(monkeypatch, home, status={"connector": {"selectable": False}})
    result = probe.start_probe(home, exchange=lambda req: probe._ProbeOutcome(ok=True), wait=True)
    assert result["state"] == "blocked"
    assert result["reason"] == "connector_not_selectable"


def test_timeout_failure_is_actionable(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _seed_settings(home)
    _patch_common(monkeypatch, home)

    def exchange(request: probe._ProbeRequest) -> probe._ProbeOutcome:
        raise probe.ProbeError("probe_timeout")

    result = probe.start_probe(home, exchange=exchange, wait=True)
    assert result["state"] == "failed"
    assert result["reason"] == "probe_timeout"
    assert probe.coding_setup_complete(home) is False


def test_duplicate_start_while_running_rejected(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _seed_settings(home)
    _patch_common(monkeypatch, home)
    started = threading.Event()
    release = threading.Event()

    def exchange(request: probe._ProbeRequest) -> probe._ProbeOutcome:
        started.set()
        assert release.wait(timeout=2)
        return probe._ProbeOutcome(ok=True, live_attempted=False)

    worker = threading.Thread(
        target=lambda: probe.start_probe(home, exchange=exchange, wait=True),
        daemon=True,
    )
    worker.start()
    assert started.wait(timeout=2)
    with pytest.raises(probe.ProbeError) as info:
        probe.start_probe(home, exchange=exchange, wait=True)
    assert info.value.reason == "probe_already_running"
    release.set()
    worker.join(timeout=2)
    assert probe.public_status(home)["state"] == "passed"


def test_cancel_marks_incomplete(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _seed_settings(home)
    _patch_common(monkeypatch, home)
    started = threading.Event()

    def exchange(request: probe._ProbeRequest) -> probe._ProbeOutcome:
        started.set()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if request.cancel_event.is_set():
                return probe._ProbeOutcome(ok=False, reason="probe_cancelled", live_attempted=False)
            time.sleep(0.01)
        return probe._ProbeOutcome(ok=True, live_attempted=False)

    worker = threading.Thread(
        target=lambda: probe.start_probe(home, exchange=exchange, wait=True),
        daemon=True,
    )
    worker.start()
    assert started.wait(timeout=2)
    probe.cancel_probe(home)
    worker.join(timeout=2)
    final = probe.public_status(home)
    assert final["state"] in {"cancelled", "failed"}
    assert probe.coding_setup_complete(home) is False


def test_cancel_cannot_be_overwritten_by_late_success(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _seed_settings(home)
    _patch_common(monkeypatch, home)
    started = threading.Event()
    release = threading.Event()

    def exchange(_request: probe._ProbeRequest) -> probe._ProbeOutcome:
        started.set()
        assert release.wait(timeout=2)
        return probe._ProbeOutcome(ok=True, live_attempted=False)

    worker = threading.Thread(target=lambda: probe.start_probe(home, exchange=exchange), daemon=True)
    worker.start()
    assert started.wait(timeout=2)
    assert probe.cancel_probe(home)["state"] == "cancelled"
    with pytest.raises(probe.ProbeError) as info:
        probe.start_probe(home, exchange=exchange)
    assert info.value.reason == "probe_already_running"
    release.set()
    worker.join(timeout=2)
    assert probe.public_status(home)["state"] == "cancelled"
    assert probe.coding_setup_complete(home) is False
    assert probe.start_probe(home, exchange=lambda _request: probe._ProbeOutcome(ok=True))["state"] == "passed"


def test_interrupted_running_probe_can_be_retried(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _seed_settings(home)
    _patch_common(monkeypatch, home)
    probe._write_document(home, {
        "version": 1,
        "state": "running",
        "reason": None,
        "checked_at": "2026-09-28T00:00:00Z",
        "probe_id": "interrupted",
        "binding": _binding(),
        "live_attempted": True,
    })
    assert probe.public_status(home)["reason"] == "probe_interrupted"
    result = probe.start_probe(home, exchange=lambda _request: probe._ProbeOutcome(ok=True), wait=True)
    assert result["state"] == "passed"


def test_probe_write_failure_does_not_leave_an_active_worker(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _seed_settings(home)
    _patch_common(monkeypatch, home)
    write_document = probe._write_document

    def fail_write(_home, _document):
        raise OSError("disk unavailable")

    monkeypatch.setattr(probe, "_write_document", fail_write)
    with pytest.raises(OSError):
        probe.start_probe(home, exchange=lambda _request: probe._ProbeOutcome(ok=True))
    monkeypatch.setattr(probe, "_write_document", write_document)
    assert probe.start_probe(home, exchange=lambda _request: probe._ProbeOutcome(ok=True))["state"] == "passed"


def test_stale_after_generation_change(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _seed_settings(home)
    binding = {"value": _binding("a")}

    def current_binding(_home):
        return dict(binding["value"])

    monkeypatch.setattr(chatgpt_web_checks, "_binding", current_binding)
    monkeypatch.setattr(runtime, "build_status", lambda _home, pin=None: _ready_status())
    monkeypatch.setattr(
        chatgpt_web_checks,
        "cached_checks",
        lambda _home: {
            "tunnel": {"state": "ready"},
            "connector": {"state": "selectable"},
            "cache_state": "current",
        },
    )
    monkeypatch.setattr(
        "chatgpt_web_connection.resolve_connection",
        lambda *args, **kwargs: ("http://127.0.0.1:32123", "service-key"),
    )
    result = probe.start_probe(
        home,
        exchange=lambda req: probe._ProbeOutcome(ok=True, live_attempted=False),
        wait=True,
    )
    assert result["state"] == "passed"
    binding["value"] = _binding("b")
    status = probe.public_status(home)
    assert status["state"] == "stale"
    assert probe.coding_setup_complete(home) is False


def test_status_flags_do_not_impersonate_probe_success(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _seed_settings(home)
    _patch_common(monkeypatch, home)
    assert probe.public_status(home)["state"] == "not_run"
    assert probe.coding_setup_complete(home) is False


def test_settings_http_tool_probe_endpoints(settings_server, monkeypatch):
    server, home = settings_server
    _seed_settings(home)
    _patch_common(monkeypatch, home)
    monkeypatch.setattr(
        probe,
        "start_probe",
        lambda *args, **kwargs: {
            "state": "passed",
            "reason": None,
            "checked_at": "2026-09-28T00:00:00Z",
            "probe_id": "abc",
            "generation": "gen",
            "live_attempted": False,
        },
    )
    session = _session(server)
    status, body = _call(server, "/api/tool-probe", session=session)
    assert status == 200
    assert body["ok"] is True
    assert body["state"] == "not_run"

    status, body = _call(
        server,
        "/api/tool-probe",
        method="POST",
        body={"action": "start"},
        session=session,
        origin=server.origin,
    )
    assert status == 200
    assert body["state"] == "passed"

    status, body = _call(server, "/api/status", session=session)
    assert status == 200
    assert "tool_probe" in body
    assert body["coding_setup_complete"] is False  # real on-disk probe still not_run
    assert body["readiness"]["tools_ready"] in {True, False}


def test_settings_tool_probe_requires_session_and_origin(settings_server):
    server, _home = settings_server
    status, body = _call(server, "/api/tool-probe")
    assert status == 401
    session = _session(server)
    status, body = _call(
        server,
        "/api/tool-probe",
        method="POST",
        body={"action": "start"},
        session=session,
        origin="http://evil.example",
    )
    assert status == 403


def test_default_sse_parser_extracts_function_call():
    body = (
        b'data: {"type":"response.output_item.done","item":'
        b'{"type":"function_call","name":"codexhub_setup_probe","call_id":"call_1"}}\n\n'
        b'data: {"type":"response.completed","response":{"output":[]}}\n\n'
        b"data: [DONE]\n\n"
    )
    events = probe._parse_sse_events(body)
    calls = probe._function_calls(events)
    assert calls == [{"name": "codexhub_setup_probe", "call_id": "call_1"}]


def test_browser_only_mode_blocks_probe(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _seed_settings(home, mode="browser-only")
    _patch_common(monkeypatch, home)
    result = probe.start_probe(home, exchange=lambda req: probe._ProbeOutcome(ok=True), wait=True)
    assert result["state"] == "blocked"
    assert result["reason"] == "full_mode_required"


def test_function_calls_require_call_identity_not_item_id():
    body = (
        b'data: {"type":"response.output_item.done","item":'
        b'{"type":"function_call","name":"codexhub_setup_probe","id":"item_only"}}\n\n'
        b'data: {"type":"response.completed","response":{"output":['
        b'{"type":"function_call","name":"codexhub_setup_probe","id":"item_only"}]}}\n\n'
    )
    events = probe._parse_sse_events(body)
    assert probe._function_calls(events) == []


def test_default_exchange_rejects_incomplete_and_missing_correlation(monkeypatch, tmp_path):
    home = tmp_path / "home"
    _seed_settings(home)
    _patch_common(monkeypatch, home)

    responses = [
        (
            200,
            (
                b'data: {"type":"response.output_item.done","item":'
                b'{"type":"function_call","id":"fc_1","name":"codexhub_setup_probe",'
                b'"call_id":"call_1","arguments":"{\\"key\\":\\"setup\\"}"}}\n\n'
                b'data: {"type":"response.completed","response":{"output":[]}}\n\n'
            ),
        ),
        (
            200,
            b'data: {"type":"response.incomplete","response":{"status":"incomplete"}}\n\n',
        ),
    ]

    def fake_post(**kwargs):
        assert responses
        return responses.pop(0)

    monkeypatch.setattr(probe, "_post_responses", fake_post)
    monkeypatch.setattr(probe, "_select_model", lambda _home: "gpt-test")
    request = probe._ProbeRequest(
        home=home,
        probe_id="probe1234",
        token="token-value",
        base_url="http://127.0.0.1:9",
        service_key="key",
        timeout_seconds=5,
        cancel_event=threading.Event(),
    )
    outcome = probe._default_exchange(request)
    assert outcome.ok is False
    assert outcome.reason == "tool_result_incomplete"

    responses.extend(
        [
            (
                200,
                (
                    b'data: {"type":"response.output_item.done","item":'
                    b'{"type":"function_call","id":"fc_2","name":"codexhub_setup_probe",'
                    b'"call_id":"call_2","arguments":"{\\"key\\":\\"setup\\"}"}}\n\n'
                    b'data: {"type":"response.completed","response":{"output":[]}}\n\n'
                ),
            ),
            (
                200,
                (
                    b'data: {"type":"response.output_text.delta","delta":"ready"}\n\n'
                    b'data: {"type":"response.completed","response":{"output":[]}}\n\n'
                ),
            ),
        ]
    )
    outcome = probe._default_exchange(request)
    assert outcome.ok is False
    assert outcome.reason == "tool_correlation_missing"

    responses.extend(
        [
            (
                200,
                (
                    b'data: {"type":"response.output_item.done","item":'
                    b'{"type":"function_call","id":"fc_3","name":"codexhub_setup_probe",'
                    b'"call_id":"call_3","arguments":"{\\"key\\":\\"setup\\"}"}}\n\n'
                    b'data: {"type":"response.completed","response":{"output":[]}}\n\n'
                ),
            ),
            (
                200,
                (
                    b'data: {"type":"response.output_text.delta","delta":"token-value"}\n\n'
                    b'data: {"type":"response.completed","response":{"output":[]}}\n\n'
                ),
            ),
        ]
    )
    outcome = probe._default_exchange(request)
    assert outcome.ok is True
    assert outcome.reason is None


def test_default_exchange_uses_native_turn_metadata_and_replays_call(monkeypatch, tmp_path):
    home = tmp_path / "home & test"
    _seed_settings(home)
    _patch_common(monkeypatch, home)
    monkeypatch.setattr(probe, "_select_model", lambda _home: "gpt-test")
    sent = []

    def fake_post(**kwargs):
        sent.append(kwargs["payload"])
        if len(sent) == 1:
            return 200, (
                b'data: {"type":"response.output_item.done","item":'
                b'{"type":"function_call","id":"fc_probe","call_id":"call_probe",'
                b'"name":"codexhub_setup_probe","arguments":"{\\"key\\":\\"setup\\"}"}}\n\n'
                b'data: {"type":"response.completed","response":{"output":[]}}\n\n'
            )
        return 200, (
            b'data: {"type":"response.output_text.delta","delta":"token-value"}\n\n'
            b'data: {"type":"response.completed","response":{"output":[]}}\n\n'
        )

    monkeypatch.setattr(probe, "_post_responses", fake_post)
    request = probe._ProbeRequest(
        home=home, probe_id="probe1234", token="token-value",
        base_url="http://127.0.0.1:9", service_key="key", timeout_seconds=5,
        cancel_event=threading.Event(),
    )
    assert probe._default_exchange(request).ok is True
    assert len(sent) == 2
    for payload in sent:
        metadata = json.loads(payload["client_metadata"]["x-codex-turn-metadata"])
        assert metadata == {
            "thread_id": "thread_probe1234", "turn_id": "turn_probe1234", "request_kind": "turn",
            "sandbox_mode": "read-only", "workspaces": {str(home): {}},
        }
        assert payload["prompt_cache_key"] == "thread_probe1234"
        environment, message = payload["input"][:2]
        assert environment["id"] == "env_probe1234"
        assert environment["internal_chat_message_metadata_passthrough"] == {
            "turn_id": "turn_probe1234", "content_item_kinds": ["environments.environment_context"],
        }
        assert f"<cwd>{str(home).replace('&', '&amp;')}</cwd>" in environment["content"][0]["text"]
        assert "<sandbox_mode>read-only</sandbox_mode>" in environment["content"][0]["text"]
        assert message["id"] == "msg_probe1234"
        assert message["internal_chat_message_metadata_passthrough"] == {"turn_id": "turn_probe1234"}
    assert sent[1]["input"][2] == {
        "type": "function_call", "id": "fc_probe", "call_id": "call_probe",
        "name": "codexhub_setup_probe", "arguments": '{"key":"setup"}',
    }
    assert sent[1]["input"][3]["type"] == "function_call_output"
    assert sent[1]["input"][3]["call_id"] == "call_probe"


def test_default_exchange_classifies_failed_response(monkeypatch, tmp_path):
    home = tmp_path / "home"
    _seed_settings(home)
    _patch_common(monkeypatch, home)
    monkeypatch.setattr(probe, "_select_model", lambda _home: "gpt-test")
    monkeypatch.setattr(
        probe, "_post_responses",
        lambda **kwargs: (200, b'data: {"type":"response.failed","response":{"status":"failed"}}\n\n'),
    )
    request = probe._ProbeRequest(
        home=home, probe_id="probe1234", token="token-value",
        base_url="http://127.0.0.1:9", service_key="key", timeout_seconds=5,
        cancel_event=threading.Event(),
    )
    assert probe._default_exchange(request).reason == "probe_request_rejected"


def test_passed_probe_stales_when_connector_becomes_unselectable(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _seed_settings(home)
    status = {"value": _ready_status()}

    def build_status(_home, pin=None):
        return dict(status["value"])

    binding = _binding("a")
    monkeypatch.setattr(chatgpt_web_checks, "_binding", lambda _home: dict(binding))
    monkeypatch.setattr(runtime, "build_status", build_status)
    monkeypatch.setattr(
        chatgpt_web_checks,
        "cached_checks",
        lambda _home: {
            "tunnel": {"state": "ready"},
            "connector": {"state": "selectable"},
            "cache_state": "current",
        },
    )
    monkeypatch.setattr(
        "chatgpt_web_connection.resolve_connection",
        lambda *args, **kwargs: ("http://127.0.0.1:32123", "service-key"),
    )
    result = probe.start_probe(
        home,
        exchange=lambda req: probe._ProbeOutcome(ok=True, live_attempted=False),
        wait=True,
    )
    assert result["state"] == "passed"
    assert probe.coding_setup_complete(home) is True

    status["value"] = _ready_status(connector={"selectable": False})
    monkeypatch.setattr(
        chatgpt_web_checks,
        "cached_checks",
        lambda _home: {
            "tunnel": {"state": "ready"},
            "connector": {"state": "missing"},
            "cache_state": "current",
        },
    )
    public = probe.public_status(home)
    assert public["state"] == "stale"
    assert public["reason"] == "connector_not_selectable"
    assert probe.coding_setup_complete(home) is False
