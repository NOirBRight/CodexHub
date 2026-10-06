"""Opt-in phase receipts through a fresh child and the public exchange ports."""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import socket
import subprocess
import sys
from threading import Event
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
if __name__ == "__main__":
    sys.path.insert(0, str(ROOT))

from scripts.qualify_subscription_codemode import load_observation

SECRET = "SYNTHETIC-PHASE-SECRET-NOT-FOR-OUTPUT"


def control(root, mode):
    from cursor_subscription_backend import HTTP2Duplex, stream_chat
    from subscription_backend_contract import load_http2_dependencies
    import subscription_exchange
    from tests.test_cursor_subscription_backend import account, framed, pb, update

    load_http2_dependencies()
    from h2.config import H2Configuration
    from h2.connection import H2Connection
    from h2.events import DataReceived, RequestReceived

    observer = load_observation(ROOT / "scripts/subscription_fixture_observer.py")
    if mode in {"cap", "drop-cap"}:
        observer.MAX_PHASE_RECORDS = 1
    lease = account(root)
    calls = []

    class Peer:
        def __init__(self):
            self.server = H2Connection(config=H2Configuration(client_side=False))
            self.server.initiate_connection()
            self.incoming = queue.Queue()
            self.incoming.put(self.server.data_to_send())
            self.answered = False
            calls.append(self)
            self.config = len(calls) == 1

        def settimeout(self, timeout):
            pass

        def do_handshake(self):
            pass

        def selected_alpn_protocol(self):
            return "h2"

        def sendall(self, data):
            for event in self.server.receive_data(data):
                if isinstance(event, RequestReceived):
                    self.server.send_headers(event.stream_id, [(":status", "200")])
                if isinstance(event, DataReceived):
                    self.server.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
                    if not self.answered:
                        self.answered = True
                        body = (json.dumps({"agentUrlConfig": {"agentUrl": "https://agent.api5.cursor.sh"}}).encode()
                                if self.config else update(1, pb(1, SECRET)) + (
                                    b"" if mode == "incomplete" else framed(b"{}", 2)))
                        self.server.send_data(event.stream_id, body,
                                              end_stream=self.config or mode != "incomplete")
            outgoing = self.server.data_to_send()
            if outgoing:
                self.incoming.put(outgoing)

        def recv(self, size):
            try:
                return self.incoming.get(timeout=.1)
            except queue.Empty:
                raise socket.timeout() from None

        def shutdown(self, how):
            pass

        def close(self):
            pass

    class TLS:
        def set_alpn_protocols(self, protocols):
            pass

        def wrap_socket(self, raw, **options):
            return raw

    def connect(*args, **options):
        if mode == "error":
            raise socket.gaierror(SECRET)
        return Peer()

    def backend(payload, *, cancel, timeout):
        yield from stream_chat(payload, cancel=cancel, timeout=timeout,
                               account_reader=lambda **options: lease,
                               transport_factory=lambda *args, **options: HTTP2Duplex(
                                   *args, **options, tls_context_factory=TLS))

    bound = observer.install_cursor_phase_observation(root)
    snapshot = None
    try:
        from urllib.request import Request
        request = Request("https://cli-subscription.invalid/v1/chat/completions",
                          data=json.dumps({"model": "controlled", "stream": True,
                                           "messages": [{"role": "user", "content": SECRET}]}).encode())
        with patch("socket.create_connection", connect):
            if mode == "drop-cap":
                # Public backend call outside exchange has no request deadline:
                # disclose drop first, then let the actual exchange reach cap.
                list(backend(json.loads(request.data), cancel=Event(), timeout=180))
                calls.clear()
            try:
                with subscription_exchange.open_subscription(request, provider_id="cursor-subscription",
                                                               timeout=180, backend=backend) as response:
                    if mode == "incomplete":
                        response.readline()
                        snapshot = observer.collect_cursor_phase_observation(root, [os.getpid()])
                    else:
                        response.read()
            except Exception:
                if mode != "error":
                    raise
    finally:
        bound.close()
        lease.source.unlink()
    result = snapshot or observer.collect_cursor_phase_observation(root, [os.getpid()])
    (root / "result.json").write_text(json.dumps(result))


@pytest.mark.skipif(os.name != "posix", reason="Existing qualification owner-only storage is POSIX-only")
@pytest.mark.parametrize("mode", ["complete", "error", "incomplete", "cap", "drop-cap"])
def test_child_phase_observer_covers_exchange_and_http2_without_network(tmp_path, mode, record_property):
    result = subprocess.run([str(ROOT / "scripts/codexhub-python.sh"), str(Path(__file__).resolve()),
                             str(tmp_path), mode], cwd=ROOT, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr.decode()
    text = (tmp_path / "result.json").read_text()
    assert SECRET not in text
    report = json.loads(text)
    assert report["process_ids"] == [report["records"][0]["pid"]]
    assert report["process_ids"] != [os.getpid()]
    assert all(row["parent_pid"] == os.getpid() for row in report["records"])
    record_property("child_pid", report["process_ids"][0])
    record_property("parent_pid", os.getpid())
    starts = [row for row in report["records"] if row["event"] == "begin"]
    if mode in {"cap", "drop-cap"}:
        assert "cap" in report["closure"] and "drop" in report["closure"]
        assert len(report["records"]) <= 2
        return
    assert report["actors"] == {"startup": True, "exchange": True, "http2": True}
    assert starts[0]["phase"] == "config"
    assert starts[0]["request_timeout_seconds"] == 180
    assert starts[0]["phase_timeout_seconds"] == 10
    assert all(row["phase_deadline_monotonic"] > row["begin_monotonic"] for row in starts)
    if mode == "complete":
        assert [row["phase"] for row in starts] == ["config", "run"]
        assert 0 < starts[1]["phase_timeout_seconds"] <= 180
        assert report["closure"] == []
    elif mode == "error":
        assert report["closure"] == ["missing-phase"]
        assert any(row.get("coded_error") == "upstream-dns-error" for row in report["records"])
    else:
        assert report["closure"] == ["missing-end"]


@pytest.mark.skipif(os.name != "posix", reason="Existing qualification private storage is POSIX-only")
def test_existing_profile_is_retained_and_missing_binding_is_typed(tmp_path):
    observer = load_observation(ROOT / "scripts/subscription_fixture_observer.py")
    original = sys.getprofile()
    def existing(frame, event, argument):
        pass
    sys.setprofile(existing)
    try:
        bound = observer.install_cursor_phase_observation(tmp_path)
        assert sys.getprofile() is existing
        bound.close()
        assert sys.getprofile() is existing
        report = observer.collect_cursor_phase_observation(tmp_path, [os.getpid()])
        assert report["closure"] == ["missing-binding", "missing-phase"]
    finally:
        sys.setprofile(original)


@pytest.mark.skipif(os.name != "posix", reason="Existing qualification private storage is POSIX-only")
@pytest.mark.parametrize("field", ["url", "phase"])
def test_unapproved_receipt_fields_are_never_published(tmp_path, field):
    observer = load_observation(ROOT / "scripts/subscription_fixture_observer.py")
    folder = tmp_path / f"phase-{os.getpid()}"
    folder.mkdir()
    (folder / "1.json").write_text(json.dumps({"pid": os.getpid(), "parent_pid": os.getppid(),
                                             "event": "binding", "closure": [],
                                             "actors": {"startup": True, "exchange": False, "http2": False},
                                             field: SECRET}))
    report = observer.collect_cursor_phase_observation(tmp_path, [os.getpid()])
    assert "observer-error" in report["closure"]
    assert SECRET not in json.dumps(report)


@pytest.mark.skipif(os.name != "posix", reason="Existing qualification private storage is POSIX-only")
def test_default_startup_preserves_profiles(tmp_path, monkeypatch):
    import threading
    import gateway_exchange_adapters
    import subscription_exchange
    observer = load_observation(ROOT / "scripts/subscription_fixture_observer.py")
    plan = tmp_path / "plan.json"
    observer.private_json(plan, observer.fixture_plan())
    profiles = (sys.getprofile(), threading.getprofile())
    monkeypatch.setattr(subscription_exchange, "open_subscription", subscription_exchange.open_subscription)
    monkeypatch.setattr(gateway_exchange_adapters.LiveTransport, "open", gateway_exchange_adapters.LiveTransport.open)
    monkeypatch.setattr(sys, "argv", list(sys.argv))
    def gateway_entry(*args, **options):
        assert (sys.getprofile(), threading.getprofile()) == profiles
    monkeypatch.setattr(observer.runpy, "run_path", gateway_entry)
    assert observer.main(["--gateway", "--plan", str(plan), "--tickets", str(tmp_path), "--port", "12345"]) == 0
    assert not list(tmp_path.glob("phase-*"))


def test_phase_optin_requires_existing_fixture_approval_before_admission():
    from scripts.qualify_subscription_codemode import main
    with pytest.raises(SystemExit):
        main(["--observe-cursor-phases"])


if __name__ == "__main__":
    control(Path(sys.argv[1]), sys.argv[2])
