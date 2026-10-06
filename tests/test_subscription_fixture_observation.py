"""Synthetic observation controls, never historical reproduction/live acceptance."""
import importlib.util
import json
import hashlib
import http.client
import gzip
import os
import sys
import subprocess
import shutil
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import subscription_fixture_observer as observation
from cursor_subscription_backend import stream_chat
from subscription_backend_contract import BackendError
from tests.test_cursor_subscription_backend import Transport, pb, framed, update, call, tool_payload

spec = importlib.util.spec_from_file_location("fixture_qualification", ROOT / "scripts/qualify_subscription_codemode.py")
qualification = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qualification)


def capture(value="AA\u200b界", request="request"):
    return observation.FixtureCapture(observation.fixture_plan(value),
        {"run_id": "run", "case": "cursor-to-official", "epoch": 1, "request_id": request})


def test_unsupported_private_storage_rejects_before_serialization_or_file_creation(tmp_path, monkeypatch):
    # Exercise the real Windows capability on Windows and its refusal on POSIX.
    monkeypatch.setattr(os, "fchmod", None, raising=False)
    monkeypatch.setattr(observation.json, "dumps", lambda *a, **kw: pytest.fail("serialization before capability"))
    with pytest.raises(NotImplementedError, match="private-storage-unsupported"):
        observation.private_json(tmp_path / "raw.json", {"value": "inert fixture"})
    with pytest.raises(NotImplementedError, match="private-storage-unsupported"):
        capture().persist(tmp_path / "capture.json")
    with pytest.raises(NotImplementedError, match="private-storage-unsupported"):
        observation.install_gateway_observation(tmp_path, observation.fixture_plan())
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("purpose", ["prepare", "observe", "run-case", "gateway"])
def test_unsupported_private_storage_precedes_import_accounts_cli_and_output(tmp_path, monkeypatch, purpose):
    monkeypatch.setattr(os, "fchmod", None, raising=False)
    def forbidden(*args, **kwargs):
        pytest.fail("unsupported storage must precede observer/account/CLI/code execution")
    monkeypatch.setattr(qualification, "load_observation", forbidden)
    monkeypatch.setattr(qualification, "freeze_observation", forbidden)
    monkeypatch.setattr(qualification, "freeze_candidate", forbidden)
    monkeypatch.setattr(qualification, "run_case", forbidden if purpose != "run-case" else qualification.run_case)
    monkeypatch.setattr(qualification.shutil, "which", forbidden)
    monkeypatch.setattr(qualification.tempfile, "TemporaryDirectory", forbidden)
    monkeypatch.setattr(observation, "install_gateway_observation", forbidden)
    monkeypatch.setattr(observation.runpy, "run_path", forbidden)
    # Source admission remains earlier than storage admission; only Git is allowed.
    def git_only(command, **kwargs):
        assert command[0] == "git", "CLI probe before capability refusal"
        return SimpleNamespace(returncode=0, stdout="" if "status" in command else "fixture-sha")
    monkeypatch.setattr(qualification.subprocess, "run", git_only)
    output = tmp_path / "output.json"
    with pytest.raises(NotImplementedError, match="private-storage-unsupported"):
        if purpose == "run-case":
            qualification.run_case("cursor-to-official", ROOT, tmp_path, tmp_path, tmp_path / "cli", 1,
                                   observation_plan=observation.fixture_plan())
        elif purpose == "gateway":
            observation.main(["--gateway", "--plan", str(tmp_path / "absent-plan"),
                              "--tickets", str(tmp_path / "tickets"), "--port", "1"])
        else:
            args = ["--checkout", str(ROOT), "--codex", str(tmp_path / "cli"), "--output", str(output)]
            args += (["--prepare-fixture", str(output)] if purpose == "prepare" else
                     ["--observe-fixture", str(tmp_path / "absent-plan"), "--case", "cursor-to-official"])
            qualification.main(args)
    assert list(tmp_path.iterdir()) == []


def observed_run(tap, transport, payload=None, cancel=None):
    return observation.observe_cursor(payload or {"model": "fixture-high", "messages": [{"role": "user", "content": "AA\u200b界"}]},
        capture=tap, cancel=cancel or threading.Event(), timeout=2, transport_factory=transport,
        account_reader=lambda **kwargs: SimpleNamespace(check=lambda: None, token="SECRET-INERT", version="fixture"))


@pytest.mark.parametrize("terminal,exit_code,timed_out", [(None, 0, False), ("error", 0, False),
    ("turn.failed", 0, False), (None, 1, False), (None, 0, True)])
def test_public_caller_parser_admits_only_successful_completed_finals(terminal, exit_code, timed_out):
    value = "AA\u200b界"
    events = [{"type": "item.completed", "item": {"type": "agent_message", "text": value}}]
    if terminal:
        events.append({"type": terminal, "error": {"message": "inert failure"}})
    tap = capture()
    qualification.parse_turn("\n".join(map(json.dumps, events)),
        SimpleNamespace(returncode=exit_code, pid=1), timed_out, value, tap)
    row = tap.report()["boundaries"]["callerstdout"]
    successful = terminal is None and exit_code == 0 and not timed_out
    assert row["complete"] is successful
    assert row["sha256"] == hashlib.sha256(value.encode()).hexdigest()
    assert row["utf8_bytes"] == len(value.encode())
    assert ("utf8_hex" in row) is successful
    assert row["rejection"] == (None if successful else "incomplete")


def test_native_remaining_budget_precedes_decompression(monkeypatch):
    import cursor_subscription_wire as wire
    monkeypatch.setattr(observation, "MAX_PARSE_BYTES", 100)
    reads = []
    original = gzip.GzipFile
    class MeasuredGzip(original):
        def read(self, size=-1):
            data = super().read(size)
            reads.append((size, len(data)))
            return data
        def read1(self, size=-1):
            data = super().read1(size)
            reads.append((size, len(data)))
            return data
    monkeypatch.setattr(gzip, "GzipFile", MeasuredGzip)
    tap = capture()
    native = observation.NativeTap(tap)
    # Incoming and outgoing share the same decoded allowance, across calls.
    first = framed(b"{}", 2)
    tap.guard(native.feed, first, False)
    compressed = framed(gzip.compress(pb(1, pb(1, pb(1, "A" * 200)))), 1)
    tap.guard(native.feed, compressed, True)
    assert tap.failure == "observation-incomplete"
    assert reads and all(0 < requested <= 99 and produced <= 99 for requested, produced in reads)


@pytest.mark.parametrize("compressed", [False, True])
def test_owner_optional_budget_bounds_multiple_frames_and_preserves_default(compressed):
    from cursor_subscription_wire import Frames
    bodies = [b"ab", b"cd", b"ef"]
    data = b"".join(framed(gzip.compress(body), 1) if compressed else framed(body) for body in bodies)
    expected = [(False, body) for body in bodies]
    assert Frames().feed(data) == expected
    assert Frames().feed(data, decoded_budget=6, frame_budget=3) == expected
    with pytest.raises(BackendError) as error:
        Frames().feed(data, decoded_budget=5)
    assert error.value.code == "upstream-protocol-error"
    with pytest.raises(BackendError):
        Frames().feed(data, frame_budget=2)
    parser = Frames()
    assert parser.feed(data[:4], decoded_budget=6) == []
    assert parser.feed(data[4:], decoded_budget=6) == expected


def test_observed_request_json_capacity_scan_leaves_strings_to_stdlib():
    payload = {"input": [{"role": "user", "content": '界\\\"' + '[{:0,}]' * 600}]}
    body = json.dumps(payload, ensure_ascii=False).encode()
    for encoding, data in [("identity", body), ("gzip", gzip.compress(body))]:
        assert observation.request_payload(data, encoding) == payload


@pytest.mark.parametrize("mode", ["rejected", "owner-error", "cancel", "writer-error", "writer-complete", "writer-cancel"])
def test_public_exchange_persists_rejection_and_preserves_owner_lifecycle(tmp_path, monkeypatch, mode):
    from urllib.request import Request
    from urllib.error import HTTPError
    import cursor_subscription_backend as backend
    import subscription_exchange as exchange
    import gateway_exchange_adapters
    plan = observation.fixture_plan("123456789abcdddef1234567")
    correlation = {"run_id": plan["run_id"], "case": plan["case"], "epoch": 1, "request_id": "b" * 32}
    # Inert correlation fixture, not an assertion of private storage support.
    (tmp_path / "peer-12345.json").write_bytes(json.dumps(correlation).encode())
    closed, cancelled = threading.Event(), threading.Event()
    native_captures = []
    original_capture = observation.FixtureCapture
    def collect(*args, **kwargs):
        captured = original_capture(*args, **kwargs)
        native_captures.append(captured)
        return captured
    monkeypatch.setattr(observation, "FixtureCapture", collect)
    def inert(payload, *, cancel, **kwargs):
        try:
            if mode in ("owner-error", "writer-error"):
                raise BackendError("inert-owner-error", "synthetic owner failure")
            yield {"choices": [{"delta": {"content": plan["value"]}, "finish_reason": None}]}
            if mode in ("cancel", "writer-cancel"):
                assert cancel.wait(2)
                cancelled.set()
                raise BackendError("cancelled", "synthetic cancellation", 499)
            yield {"choices": [{"delta": {}, "finish_reason": "stop"}]}
        finally:
            closed.set()
    monkeypatch.setattr(backend, "stream_chat", inert)
    monkeypatch.setattr(exchange, "open_subscription", exchange.open_subscription)
    monkeypatch.setattr(gateway_exchange_adapters.LiveTransport, "open", gateway_exchange_adapters.LiveTransport.open)
    if os.name != "posix":
        with pytest.raises(NotImplementedError, match="private-storage-unsupported"):
            observation.install_gateway_observation(tmp_path, plan)
        return
    observation.install_gateway_observation(tmp_path, plan)
    if mode.startswith("writer-"):
        monkeypatch.setattr(observation, "private_json", lambda *a: (_ for _ in ()).throw(OSError("inert writer failure")))
    class Socket:
        def getpeername(self):
            return ("127.0.0.1", 12345)
    payload = {"stream": True, "messages": [{"role": "user", "content": plan["value"]}] * 65}
    request = Request("https://inert.invalid", data=json.dumps(payload).encode())
    if mode in ("owner-error", "writer-error"):
        with pytest.raises(HTTPError) as error:
            with exchange.open_subscription(request, provider_id="cursor-subscription", timeout=3, downstream_socket=Socket()):
                pytest.fail("owner error must win")
        assert json.loads(error.value.read())["error"]["code"] == "inert-owner-error"
    else:
        with exchange.open_subscription(request, provider_id="cursor-subscription", timeout=3, downstream_socket=Socket()) as response:
            if mode in ("cancel", "writer-cancel"):
                assert plan["value"].encode() in response.readline()
            else:
                forwarded = response.read()
                assert plan["value"].encode() in forwarded and b"[DONE]" in forwarded
    assert closed.wait(1)
    if mode in ("cancel", "writer-cancel"):
        assert cancelled.is_set()
    assert len(native_captures) == 1 and native_captures[0].failure == "observation-incomplete"
    sidecar = tmp_path / (correlation["request_id"] + ".json")
    parent = original_capture(plan, correlation)
    parent.value("fixtureinput", plan["value"])
    requests = [{"fixture_request_id": correlation["request_id"], "epoch": 1, "model": qualification.CURSOR_MODEL}]
    # Serialize the same public aggregation result that run_case publishes.
    item = json.loads(json.dumps(qualification.aggregate_fixture_observations([parent], tmp_path, observation, requests)))[0]
    assert item["capture_failure"] is None
    assert item["boundaries"]["fixtureinput"]["utf8_hex"] == plan["value"].encode().hex()
    if mode.startswith("writer-") or os.name != "posix":
        assert not sidecar.exists()
        assert item["native_report"] == {"state": "unavailable", "reason": "missing", "cause": "unknown"}
        assert "native_capture_failure" not in item  # Its actual cause cannot be recovered from absence.
        for name in ("adaptedhistory", "servedhistoryblob", "nativefield", "canonicalchunks"):
            assert item["boundaries"][name] == {"state": "incomplete", "complete": False}
    else:
        report = json.loads(sidecar.read_bytes())
        assert report["capture_failure"] == "observation-incomplete"
        assert len(report["boundaries"]["adaptedhistory"]["leaves"]) == 64
        assert "utf8_hex" not in json.dumps(report) and "codepoints" not in json.dumps(report)
        assert sidecar.stat().st_mode & 0o777 == 0o600
        assert item["native_capture_failure"] == report["capture_failure"]
        for name in ("adaptedhistory", "servedhistoryblob", "nativefield", "canonicalchunks"):
            assert item["boundaries"][name] == report["boundaries"][name]


@pytest.mark.parametrize("kind", ["over-limit", "unreadable", "invalid-json"])
def test_public_aggregation_bounds_unavailable_native_reports(tmp_path, monkeypatch, kind):
    tap = capture()
    path = tmp_path / "request.json"
    limit = observation.MAX_REPORT_BYTES
    if kind == "over-limit":
        path.write_bytes(b"PRIVATE" * (limit // 7 + 1))
        original_open = Path.open
        reads = []
        class BoundedRead:
            def __enter__(self):
                return self
            def read(self, size):
                reads.append(size)
                assert size == limit + 1
                return self.source.read(size)
            def __exit__(self, *args):
                self.source.close()
        def measured_open(target, *args, **kwargs):
            source = original_open(target, *args, **kwargs)
            if target != path:
                return source
            measured = BoundedRead()
            measured.source = source
            return measured
        monkeypatch.setattr(Path, "open", measured_open)
    elif kind == "unreadable":
        path.mkdir()  # Deterministic read failure, including under privileged test users.
    else:
        path.write_bytes(b"PRIVATE-invalid-json")
    requests = [{"fixture_request_id": "request", "epoch": 1, "model": qualification.CURSOR_MODEL}]
    item = qualification.aggregate_fixture_observations([tap], tmp_path, observation, requests)[0]
    assert item["native_report"] == {"state": "unavailable", "reason": kind, "cause": "unknown"}
    assert "PRIVATE" not in json.dumps(item) and "native_capture_failure" not in item
    for name in ("adaptedhistory", "servedhistoryblob", "nativefield", "canonicalchunks"):
        assert item["boundaries"][name] == {"state": "incomplete", "complete": False}
    if kind == "over-limit":
        assert reads == [limit + 1]


@pytest.mark.parametrize("association", ["official", "caller-only", "different-request", "different-epoch", "unknown-model"])
def test_public_aggregation_does_not_infer_expected_native_capture(tmp_path, association):
    tap = capture(request=None if association == "caller-only" else "request")
    requests = [{"fixture_request_id": "other" if association == "different-request" else "request",
                 "epoch": 0 if association == "different-epoch" else 1,
                 "model": qualification.OFFICIAL_MODEL if association == "official" else None if association == "unknown-model" else qualification.CURSOR_MODEL}]
    item = qualification.aggregate_fixture_observations([tap], tmp_path, observation, requests)[0]
    if association == "official":
        assert item["native_report"] == {"state": "unavailable", "reason": "missing", "cause": "unknown"}
    else:
        assert "native_report" not in item
    assert "native_capture_failure" not in item and "actual_route" not in item
    assert item["correlation"] == tap.correlation
    for name in ("adaptedhistory", "servedhistoryblob", "nativefield", "canonicalchunks"):
        assert item["boundaries"][name] == {"state": "incomplete" if association == "official" else "absent", "complete": False}


@pytest.mark.parametrize("key,value", [("run_id", "other"), ("case", "other"), ("epoch", 0), ("request_id", "other")])
def test_public_aggregation_rejects_mismatched_native_correlation(tmp_path, key, value):
    parent = capture()
    native = observation.FixtureCapture(parent.plan, dict(parent.correlation, **{key: value}))
    native.value("nativefield", parent.plan["value"])
    (tmp_path / "request.json").write_bytes(json.dumps(native.report()).encode())  # inert mismatched report
    requests = [{"fixture_request_id": "request", "epoch": 1, "model": qualification.CURSOR_MODEL}]
    with pytest.raises(ValueError, match="fixture-correlation-mismatch"):
        qualification.aggregate_fixture_observations([parent], tmp_path, observation, requests)
    assert parent.report()["boundaries"]["nativefield"]["state"] == "absent"


@pytest.mark.parametrize("value", ["AA\u200b界", "A\u200b界", "AA界"])
@pytest.mark.parametrize("compressed", [False, True])
def test_public_native_stream_exact_loss_controls_and_split_utf8(tmp_path, value, compressed):
    # Independent wire fixtures, fragmented inside UTF8, real public decoder.
    wire = b"".join(framed(gzip.compress(pb(1, pb(1, pb(1, char)))), 1) if compressed
                    else update(1, pb(1, char)) for char in value) + framed(b"{}", 2)
    transport = Transport([bytes([byte]) for byte in wire])
    tap = capture()
    chunks = list(observed_run(tap, transport))
    text = "".join(chunk["choices"][0]["delta"].get("content", "") for chunk in chunks)
    rows = tap.report()["boundaries"]
    assert text == value
    for stage in ("nativefield", "canonicalchunks"):
        assert rows[stage]["utf8_hex"] == value.encode().hex()
        assert rows[stage]["codepoints"] == list(map(ord, value))
        assert rows[stage]["exact_expected_reverse"] == (value == "AA\u200b界"[::-1])
        assert rows[stage]["exact_fixture"] == (value == "AA\u200b界")
    assert all(s.closed for s in transport.streams)
    assert "SECRET" not in json.dumps(tap.report())
    if os.name != "posix":
        with pytest.raises(NotImplementedError, match="private-storage-unsupported"):
            tap.persist(tmp_path / "request.json")
        assert not (tmp_path / "request.json").exists()
    else:
        tap.persist(tmp_path / "request.json")
    parent = observation.FixtureCapture(tap.plan, tap.correlation)
    requests = [{"fixture_request_id": "request", "epoch": 1, "model": qualification.CURSOR_MODEL}]
    item = qualification.aggregate_fixture_observations([parent], tmp_path, observation, requests)[0]
    if os.name == "posix":
        assert "native_report" not in item and item["native_capture_failure"] is None
        for stage in ("nativefield", "canonicalchunks"):
            assert item["boundaries"][stage] == rows[stage]
    else:
        assert item["native_report"] == {"state": "unavailable", "reason": "missing", "cause": "unknown"}
        for stage in ("adaptedhistory", "servedhistoryblob", "nativefield", "canonicalchunks"):
            assert item["boundaries"][stage] == {"state": "incomplete", "complete": False}


def test_public_stream_noninterference_calls_order_errors_and_close():
    payload = tool_payload()
    raw = [update(27, pb(1, 1)), call(call_id="raw\nitem")]
    plain, observed = Transport(raw), Transport(raw)
    kwargs = dict(cancel=threading.Event(), timeout=2, account_reader=lambda **kw: SimpleNamespace(check=lambda: None, token="inert", version="fixture"))
    before = json.dumps(payload)
    expected = list(stream_chat(payload, transport_factory=plain, **kwargs))
    tap = capture()
    actual = list(observed_run(tap, observed, payload))
    # Time/response IDs are backend-owned and intentionally unpredictable.
    assert [c["choices"] for c in actual] == [c["choices"] for c in expected]
    assert json.dumps(payload) == before
    assert [s.sent[1:] for s in plain.streams] == [s.sent[1:] for s in observed.streams]
    assert all(s.closed for s in observed.streams)
    for hooked in (False, True):
        transport = Transport([update(1, pb(1, "AA\u200b界")), b""])
        cancel = threading.Event()
        iterator = observed_run(capture(), transport, cancel=cancel) if hooked else stream_chat(
            {"model": "fixture-high", "messages": [{"role": "user", "content": "AA\u200b界"}]}, transport_factory=transport, **kwargs)
        next(iterator); next(iterator)
        with pytest.raises(BackendError) as error:
            next(iterator)
        assert error.value.code == "upstream-interrupted"
        assert all(s.closed for s in transport.streams)
    transport = Transport([update(1, pb(1, "AA\u200b界"))])
    tap = capture()
    iterator = observed_run(tap, transport)
    next(iterator)
    iterator.close()
    assert all(s.closed for s in transport.streams)
    assert not tap.report()["boundaries"]["nativefield"]["complete"]
    assert "utf8_hex" not in tap.report()["boundaries"]["nativefield"]

    transport = Transport([update(1, pb(1, "AA\u200b界"))])
    cancel = threading.Event()
    tap = capture()
    iterator = observed_run(tap, transport, cancel=cancel)
    next(iterator); next(iterator)
    cancel.set()
    with pytest.raises(BackendError) as error:
        next(iterator)
    assert error.value.code == "cancelled"
    assert all(s.closed for s in transport.streams)
    assert "utf8_hex" not in tap.report()["boundaries"]["canonicalchunks"]


@pytest.mark.parametrize("value,complete", [("SECRET-AA\u200b界", True), ("界" * 100, True), ("AA\u200b界", False)])
def test_privacy_red_controls_never_publish_raw(value, complete):
    tap = capture()
    wire = [update(1, pb(1, value))] + ([framed(b"{}", 2)] if complete else [b""])
    if complete:
        assert "".join(c["choices"][0]["delta"].get("content", "") for c in observed_run(tap, Transport(wire))) == value
    else:
        with pytest.raises(BackendError):
            list(observed_run(tap, Transport(wire)))
    stdout = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": value}})
    qualification.parse_turn(stdout, SimpleNamespace(returncode=0, pid=1), not complete, "AA\u200b界", tap)
    row = tap.report()["boundaries"]["callerstdout"]
    assert "utf8_hex" not in row and "codepoints" not in row
    assert row["rejection"] in ("unapproved", "over-limit", "incomplete")
    assert "SECRET" not in json.dumps(tap.report())
    assert len(json.dumps(tap.report())) < 10000
    for stage in ("nativefield", "canonicalchunks"):
        assert "utf8_hex" not in tap.report()["boundaries"][stage]


def test_incomplete_utf8_and_fixture_crlf_are_never_normalized():
    for raw, reason in [(b"\xe7", "invalid-utf8"), ("AA\u200b界\r\n".encode(), "unapproved")]:
        tap = capture()
        tap.value("fixtureinput", raw)
        row = tap.report()["boundaries"]["fixtureinput"]
        assert row["sha256"] == hashlib.sha256(raw).hexdigest()
        assert row["utf8_bytes"] == len(raw)
        assert row["rejection"] == reason and "utf8_hex" not in row


def test_sse_and_caller_boundaries_are_independent_and_incomplete_stays_private():
    tap = capture()
    sse = observation.DownstreamTap(tap)
    value = "A\u200b界"  # approved deletion control
    events = [{"type": "response.output_text.delta", "delta": value},
              {"type": "response.completed", "response": {"status": "completed", "output": [{"type": "message", "content": [{"type": "output_text", "text": "AA界"}]}]}}]
    raw = b"".join(b"data: " + json.dumps(e, ensure_ascii=False).encode() + b"\n\n" for e in events)
    for byte in raw:
        sse.feed(bytes([byte]))
    sse.finish()
    tap.value("callerstdout", "AA\u200b界")
    rows = tap.report()["boundaries"]
    assert rows["downstreamSSEdelta"]["utf8_hex"] == value.encode().hex()
    assert rows["downstreamSSEcompleted"]["utf8_hex"] == "AA界".encode().hex()
    assert rows["callerstdout"]["utf8_hex"] == "AA\u200b界".encode().hex()
    assert rows["nativefield"]["state"] == "absent"
    tap = capture()
    sse = observation.DownstreamTap(tap)
    sse.feed(raw[:-1]); sse.finish()
    assert "utf8_hex" not in tap.report()["boundaries"]["downstreamSSEdelta"]


@pytest.mark.parametrize("inject_canonical_loss", [False, True])
def test_public_http_correlates_real_served_history_and_preserves_item_call_identity(tmp_path, monkeypatch, inject_canonical_loss):
    import cursor_subscription_backend as backend
    import gateway_catalog_runtime
    import gateway_exchange_adapters
    import subscription_exchange
    from tests.gateway_harness import GatewayHarness, GATEWAY_CLIENT_KEY, parsed_sse_events, require_single_terminal
    value = "AA\u200b界"
    plan = observation.fixture_plan(value)
    native_captures = []
    original_capture = observation.FixtureCapture
    def collect(*args, **kwargs):
        tap = original_capture(*args, **kwargs)
        native_captures.append(tap)
        return tap
    monkeypatch.setattr(observation, "FixtureCapture", collect)
    original_exchange = subscription_exchange.open_subscription
    monkeypatch.setattr(gateway_exchange_adapters.LiveTransport, "open", gateway_exchange_adapters.LiveTransport.open)
    if os.name != "posix":
        with pytest.raises(NotImplementedError, match="private-storage-unsupported"):
            observation.install_gateway_observation(tmp_path, plan)
        return
    observation.install_gateway_observation(tmp_path, plan)
    real = backend.stream_chat
    class BlobTransport(Transport):
        def __call__(self, *args, **kwargs):
            stream = super().__call__(*args, **kwargs)
            if args[0].endswith("/Run"):
                send = stream.send
                def request_blobs(data):
                    send(data)
                    if len(stream.sent) == 1:
                        keys = {data[i+2:i+34] for i in range(len(data)-33) if data[i:i+2] == b"\x0a\x20"}
                        stream.chunks[:0] = [framed(pb(4, pb(1, n) + pb(2, pb(1, key)))) for n, key in enumerate(sorted(keys), 1)]
                stream.send = request_blobs
            return stream
    transport = BlobTransport([update(1, pb(1, value[::-1])), framed(b"{}", 2)])
    monkeypatch.setattr(backend, "HTTP2Duplex", transport)
    def fixture_stream(payload, **kwargs):
        with_stream = real(payload, account_reader=lambda **kw: SimpleNamespace(check=lambda: None, token="INERT-SECRET", version="fixture"), **kwargs)
        try:
            for chunk in with_stream:
                content = chunk["choices"][0]["delta"].get("content")
                if inject_canonical_loss and content:
                    # Explicit SYNTHETIC translation-loss control, no production fix.
                    chunk["choices"][0]["delta"]["content"] = content.replace("AA", "A")
                yield chunk
        finally:
            with_stream.close()
    monkeypatch.setattr(backend, "stream_chat", fixture_stream)
    payload = {"model": qualification.CURSOR_MODEL, "stream": True, "tools": [{"type": "custom", "name": "exec", "format": {"type": "text"}}],
               "input": [{"role": "user", "content": "Continue completed history"},
                         {"type": "custom_tool_call", "id": "typed-item", "call_id": "original-call", "name": "exec", "input": "read fixture"},
                         {"type": "custom_tool_call_output", "id": "typed-result", "call_id": "original-call", "output": value},
                         {"role": "user", "content": "Return reversed fixture"}]}
    before = json.dumps(payload, ensure_ascii=False)
    try:
        with GatewayHarness() as gateway, monkeypatch.context() as gateway_patch:
            upstream = {"name": "cursor-subscription", "provider_id": "cursor-subscription", "model_id": qualification.CURSOR_MODEL,
                        "base_url": "https://fixture.invalid", "auth": "official_cli_session", "upstream_model": "gpt-5.6-luna-high",
                        "upstream_format": "chat_completions", "tool_protocol": "chat_tools", "tool_surface_strategy": "eager", "supported_reasoning_levels": ()}
            gateway_patch.setattr(gateway_catalog_runtime, "choose_upstream", lambda *a, **kw: upstream)
            connection = http.client.HTTPConnection(gateway.host, gateway.port, timeout=3)
            connection.connect()
            correlation = {"run_id": plan["run_id"], "case": plan["case"], "epoch": 1, "request_id": "a" * 32}
            ticket = tmp_path / f"peer-{connection.sock.getsockname()[1]}.json"
            ticket.write_bytes(json.dumps(correlation).encode())  # inert test correlation only
            tap = original_capture(plan, correlation)
            tap.leaves("callerpayload", payload["input"])
            sse = observation.DownstreamTap(tap)
            connection.request("POST", "/v1/responses", body=before.encode(), headers={"Authorization": "Bearer " + GATEWAY_CLIENT_KEY, "Content-Type": "application/json"})
            response = connection.getresponse()
            assert response.status == 200
            data = response.read()
            connection.close()
            ticket.unlink()
            for byte in data:
                sse.feed(bytes([byte]))
            sse.finish()
            require_single_terminal(parsed_sse_events(data))
            sidecar = tmp_path / ("a" * 32 + ".json")
            if os.name == "posix":
                report = json.loads(sidecar.read_bytes())
                assert sidecar.stat().st_mode & 0o777 == 0o600
            else:
                assert not sidecar.exists()
                assert len(native_captures) == 1
                report = native_captures[0].report()  # bounded memory is not private persistence
            assert report["correlation"] == correlation
            rows = report["boundaries"]
            for stage in ("adaptedhistory", "servedhistoryblob"):
                assert any(row.get("utf8_hex") == value.encode().hex() for row in rows[stage]["leaves"])
            assert rows["nativefield"]["utf8_hex"] == value[::-1].encode().hex()
            canonical = value[::-1].replace("AA", "A") if inject_canonical_loss else value[::-1]
            assert rows["canonicalchunks"]["utf8_hex"] == canonical.encode().hex()
            assert tap.report()["boundaries"]["downstreamSSEdelta"]["exact_expected_reverse"] == (not inject_canonical_loss)
            assert "SECRET" not in json.dumps(report)
            assert json.dumps(payload, ensure_ascii=False) == before
            assert qualification.observe_request(payload, 1, value)["tool_outputs"][0]["call_sha256"] == hashlib.sha256(b"original-call").hexdigest()
            assert all(s.closed for s in transport.streams)
    finally:
        subscription_exchange.open_subscription = original_exchange


def test_preparation_has_no_cli_account_access_and_rollouts_do_not_relabel_old_epochs(tmp_path, monkeypatch):
    source = tmp_path / "source"
    for name in qualification.OBSERVATION_WRAPPERS:
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "config", "core.autocrlf", "false"], check=True)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture"], check=True)
    spec = importlib.util.spec_from_file_location("preparation_fixture", source / qualification.OBSERVATION_WRAPPERS[0])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    real_run = subprocess.run
    def no_cli(command, **kwargs):
        assert command[0] == "git", "setup must not launch a CLI/account operation"
        return real_run(command, **kwargs)
    monkeypatch.setattr(module.subprocess, "run", no_cli)
    plan = tmp_path / "approved.json"
    if os.name == "posix":
        assert module.main(["--checkout", str(source), "--prepare-fixture", str(plan)]) == 0
        assert observation.validate_plan(json.loads(plan.read_bytes()))
        value = json.loads(plan.read_bytes())["value"]
        assert len(value.encode()) == 24 and any(value[i:i+3] == value[i] * 3 for i in range(len(value) - 2))
        assert plan.stat().st_mode & 0o777 == 0o600
    else:
        with pytest.raises(NotImplementedError, match="private-storage-unsupported"):
            module.main(["--checkout", str(source), "--prepare-fixture", str(plan)])
        assert not plan.exists()
    path = tmp_path / "rollout.jsonl"
    def final(text):
        return {"type": "response_item", "payload": {"type": "message", "role": "assistant", "phase": "final_answer", "content": [{"type": "output_text", "text": text}]}}
    path.write_text(json.dumps(final("AA\u200b界")) + "\n")
    offsets = {}
    first = capture(request=None)
    qualification.summarize_rollouts([path], "AA\u200b界", first, offsets)
    with path.open("a") as output:
        output.write(json.dumps(final("A\u200b界")) + "\n")
    second = capture(request=None)
    qualification.summarize_rollouts([path], "AA\u200b界", second, offsets)
    assert len(second.report()["boundaries"]["rolloutfinal"]["leaves"]) == 1
    assert second.report()["boundaries"]["rolloutfinal"]["leaves"][0]["utf8_hex"] == "A\u200b界".encode().hex()


def test_resource_rejection_does_not_change_public_stream(tmp_path, monkeypatch):
    monkeypatch.setattr(observation, "MAX_PARSE_BYTES", 8)
    transport = Transport([update(1, pb(1, "AA\u200b界")), framed(b"{}", 2)])
    tap = capture()
    chunks = list(observed_run(tap, transport))
    assert "".join(c["choices"][0]["delta"].get("content", "") for c in chunks) == "AA\u200b界"
    report = tap.report()
    assert report["capture_failure"] == "observation-incomplete"
    assert "utf8_hex" not in json.dumps(report)
    assert all(s.closed for s in transport.streams)
    # Byte budget is independent of completion/whitelist admission.
    error, reason = (ValueError, "file-limit") if os.name == "posix" else (NotImplementedError, "private-storage-unsupported")
    with pytest.raises(error, match=reason):
        observation.private_json(tmp_path / "large.json", {"value": "x" * observation.MAX_REPORT_BYTES})
    assert not (tmp_path / "large.json").exists()
    monkeypatch.setattr(observation, "MAX_PARSE_BYTES", 100)
    value = "A" * 200
    tap = capture()
    compressed = framed(gzip.compress(pb(1, pb(1, pb(1, value)))), 1)
    assert len(compressed) < 100  # decoded budget, rather than input-byte budget
    chunks = list(observed_run(tap, Transport([compressed, framed(b"{}", 2)])))
    assert "".join(c["choices"][0]["delta"].get("content", "") for c in chunks) == value
    assert tap.report()["capture_failure"] == "observation-incomplete"
    assert "utf8_hex" not in json.dumps(tap.report())


@pytest.mark.parametrize("change", ["staged", "unstaged", "untracked"])
@pytest.mark.parametrize("purpose", ["observe", "prepare"])
def test_observation_admission_includes_executed_wrappers(tmp_path, monkeypatch, change, purpose):
    source = tmp_path / "source"
    source.mkdir()
    for name in qualification.OBSERVATION_WRAPPERS:
        target = source / name
        target.parent.mkdir(exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    (source / "src-python").mkdir()
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "config", "core.autocrlf", "false"], check=True)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture"], check=True)
    path = source / "scripts/subscription_fixture_observer.py"
    if change == "untracked":
        subprocess.run(["git", "-C", str(source), "rm", "--cached", str(path)], check=True, stdout=subprocess.DEVNULL)
        subprocess.run(["git", "-C", str(source), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "untrack"], check=True)
    with path.open("a") as output:
        output.write(f"\nfrom pathlib import Path\nPath({str(tmp_path / 'dirty-observer-ran')!r}).touch()\n")
    if change == "staged":
        subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    spec = importlib.util.spec_from_file_location("selected_qualification", source / qualification.OBSERVATION_WRAPPERS[0])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    plan = tmp_path / "plan.json"
    plan.write_bytes(json.dumps(observation.fixture_plan()).encode())  # inert approval fixture
    monkeypatch.setattr(module, "freeze_candidate", lambda *a: pytest.fail("dirty wrapper must fail before freeze/accounts"))
    real_run = subprocess.run
    probes = []
    def no_cli_probe(command, **kwargs):
        if command[0] != "git":
            probes.append(command)
            return SimpleNamespace(stdout="inert-version", returncode=0)
        return real_run(command, **kwargs)
    monkeypatch.setattr(module.subprocess, "run", no_cli_probe)
    if purpose == "prepare":
        with pytest.raises(ValueError, match="candidate-runtime-not-clean"):
            module.main(["--checkout", str(source), "--prepare-fixture", str(tmp_path / "prepared.json")])
        assert not (tmp_path / "prepared.json").exists()
    else:
        args = ["--checkout", str(source), "--codex", sys.executable, "--observe-fixture", str(plan),
                "--case", "cursor-to-official", "--output", str(tmp_path / "report.json")]
        if os.name == "posix":
            assert module.main(args) == 1
            report = json.loads((tmp_path / "report.json").read_bytes())
            assert report["source_runtime_dirty"] and not report["candidate_sha_is_exact_runtime"]
        else:
            with pytest.raises(NotImplementedError, match="private-storage-unsupported"):
                module.main(args)
            assert not (tmp_path / "report.json").exists()
    assert not (tmp_path / "dirty-observer-ran").exists()
    assert probes == []


def test_observation_rejects_different_checkout_before_import_or_probe(tmp_path, monkeypatch):
    plan = tmp_path / "plan.json"
    plan.write_bytes(json.dumps(observation.fixture_plan()).encode())  # inert approval fixture
    monkeypatch.setattr(qualification, "load_observation", lambda *a: pytest.fail("unadmitted import"))
    monkeypatch.setattr(qualification.subprocess, "run", lambda *a, **kw: pytest.fail("unadmitted probe"))
    with pytest.raises(SystemExit):
        qualification.main(["--checkout", str(tmp_path), "--observe-fixture", str(plan),
            "--case", "cursor-to-official", "--output", str(tmp_path / "report.json")])


def test_freeze_hashes_and_executes_exact_wrapper_bytes(tmp_path):
    source, frozen = tmp_path / "source", tmp_path / "frozen"
    source.mkdir()
    for name in qualification.OBSERVATION_WRAPPERS:
        path = source / name
        path.parent.mkdir(exist_ok=True)
        shutil.copyfile(ROOT / name, path)
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "config", "core.autocrlf", "false"], check=True)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture"], check=True)
    sha = subprocess.run(["git", "-C", str(source), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    module, manifest = qualification.freeze_observation(source, frozen, sha)
    assert module.run_case.__code__.co_filename == str(frozen / qualification.OBSERVATION_WRAPPERS[0])
    for name, digest in manifest.items():
        assert digest == hashlib.sha256((frozen / name).read_bytes()).hexdigest()
    with (source / qualification.OBSERVATION_WRAPPERS[1]).open("a") as output:
        output.write("\n# worker edit after freeze\n")
    assert module.load_observation(frozen / qualification.OBSERVATION_WRAPPERS[1]).observe_cursor.__code__.co_filename.startswith(str(frozen))
    with pytest.raises(ValueError, match="does-not-match"):
        qualification.freeze_observation(source, tmp_path / "other", sha)


@pytest.mark.parametrize("mode", ["exact", "toolonly", "utf8", "deletion", "u200b", "partial", "unapproved",
                                  "identity", "order", "delta", "opaque", "undeclared", "missing", "error",
                                  "bytes", "leaves", "parts", "arguments", "ownererror", "captureerror", "frozen", "readerror",
                                  "item-identity", "failed", "argument-index-bool", "text-index-float",
                                  "item-index-bool", "item-index-float"])
def test_public_official_transport_relay_capture_and_join(tmp_path, monkeypatch, mode):
    """Authored offline bytes through real pooled transport and public relay, not a provider trace."""
    from urllib.request import Request
    import gateway_exchange_adapters
    import subscription_exchange
    from gateway_exchange import OpenExchangeRequest
    from gateway_exchange_bindings import relay_context_for_handler
    from gateway_relay_passthrough import relay_official_passthrough_sse_response
    from tests.gateway_harness import GatewayHarness
    from tests.test_gateway_relay import Writer
    from gateway_compat import collaboration_delivery
    value = "0123456789abbbcd01234567" if mode != "utf8" else "AA\u200b界"
    plan = observation.fixture_plan(value)
    result = value[::-1]
    if mode == "deletion":
        result = result.replace("bbb", "bb")
    elif mode == "u200b":
        result += "\u200b"
    elif mode == "unapproved":
        result = "unapproved surrounding " + result
    namespace = "codexhub_plaintext_collaboration"
    arguments = json.dumps({"target": "UNRELATED-TARGET" + ("x" * (observation.MAX_JSON_BYTES + 10) if mode == "arguments" else ""), "message": result}, ensure_ascii=False)
    call_item = {"type": "function_call", "id": "actual-item", "call_id": "distinct-actual-call",
                 "namespace": namespace, "name": "send_message", "arguments": arguments,
                 "encrypted_function_args": ["opaque"] if mode == "opaque" else []}
    text_item = {"type": "message", "role": "assistant", "id": "text-item", "status": "completed",
                 "content": [{"type": "output_text", "text": result}]}
    events = [{"type": "response.created", "response": {"id": "actual-response", "status": "in_progress"}},
              {"type": "response.output_item.added", "output_index": 0, "item": {**call_item, "arguments": ""}},
              {"type": "response.function_call_arguments.delta", "output_index": 0, "item_id": "actual-item", "delta": arguments[:len(arguments)//2]},
              {"type": "response.function_call_arguments.delta", "output_index": 0, "item_id": "actual-item", "delta": arguments[len(arguments)//2:]},
              {"type": "response.function_call_arguments.done", "output_index": 0, "item_id": "actual-item", "arguments": arguments},
              {"type": "response.output_item.done", "output_index": 0, "item": call_item}]
    output = [call_item]
    if mode != "toolonly":
        events += [{"type": "response.output_item.added", "output_index": 1, "item": {**text_item, "content": []}},
                   {"type": "response.output_text.delta", "output_index": 1, "item_id": "text-item", "content_index": 0, "delta": result},
                   {"type": "response.output_text.done", "output_index": 1, "item_id": "text-item", "content_index": 0, "text": result},
                   {"type": "response.output_item.done", "output_index": 1, "item": text_item}]
        output.append(text_item)
        # Identical Call/text values still own distinct fragmentation receipts.
        events[7]["delta"] = result[:1]
        events.insert(8, {**events[7], "delta": result[1:]})
    if mode == "identity":
        output = [{**call_item, "call_id": "wrong-call"}] + output[1:]
    if mode == "delta":
        events[3]["delta"] += "wrong"
    if mode == "item-identity":
        events[3]["item_id"] = "distinct-actual-call"
    malformed_index = {
        "argument-index-bool": ("response.function_call_arguments.delta", False),
        "text-index-float": ("response.output_text.done", 1.0),
        "item-index-bool": ("response.output_item.done", False),
        "item-index-float": ("response.output_item.done", 0.0),
    }.get(mode)
    if malformed_index:
        next(event for event in events if event["type"] == malformed_index[0])["output_index"] = malformed_index[1]
    if mode != "partial":
        events.append({"type": "response.completed", "response": {"id": "actual-response", "status": "completed", "output": output}})
    if mode == "failed":
        events[-1]["type"] = "response.failed"
        events[-1]["response"]["status"] = "failed"
    for sequence, event in enumerate(events):
        event["sequence_number"] = sequence + (1 if mode == "order" and sequence == 2 else 0)
    raw = b"".join(b"data: " + json.dumps(e, ensure_ascii=False).encode() + b"\n\n" for e in events)
    correlation = {"run_id": plan["run_id"], "case": plan["case"], "epoch": 1, "request_id": "a" * 32}
    (tmp_path / "peer-12345.json").write_bytes(json.dumps(correlation).encode())
    payload = {"model": "not-route-authority", "input": [text_item, call_item], "tools": [{"type": "namespace", "name": namespace,
               "tools": [{"type": "function", "name": "send_message", "parameters": {"type": "object", "properties": {"message": {"type": "string"}}}}]}]}
    if mode == "undeclared":
        payload["tools"] = []
    if mode == "arguments":
        payload["input"] = [text_item]  # Small history; test incoming assembly, not history refusal.
    monkeypatch.setattr(subscription_exchange, "open_subscription", subscription_exchange.open_subscription)
    monkeypatch.setattr(gateway_exchange_adapters.LiveTransport, "open", gateway_exchange_adapters.LiveTransport.open)
    original_capture = observation.FixtureCapture
    # History and native stream spend the same allowance. Each fits alone.
    if mode == "bytes":
        monkeypatch.setattr(observation, "MAX_PARSE_BYTES", len(raw) + len(json.dumps(payload).encode()) - 1)
    elif mode == "leaves":
        monkeypatch.setattr(observation, "MAX_LEAVES", 3)
    elif mode == "parts":
        monkeypatch.setattr(observation, "MAX_PARTS", 2)
    argument_parses, argument_lengths = [], []
    if mode == "arguments":
        # Every fragment fits; assembled argument bytes do not. Spy checks the
        # bounded parser never receives an over-limit argument.
        original_json = observation.bounded_json
        def bounded(data, limit=observation.MAX_JSON_BYTES):
            if data.startswith(b'{"target":'):
                argument_parses.append(len(data))
            return original_json(data, limit)
        monkeypatch.setattr(observation, "bounded_json", bounded)
    elif mode == "captureerror":
        monkeypatch.setattr(observation.FixtureCapture, "persist", lambda *a: (_ for _ in ()).throw(ValueError("inert writer failure")))
    if mode != "missing":
        if os.name != "posix":
            with pytest.raises(NotImplementedError, match="private-storage-unsupported"):
                observation.install_gateway_observation(tmp_path, plan)
            return
        observation.install_gateway_observation(tmp_path, plan)
    context = {"request_id": "actual-gateway-request", "route_attempt_index": 2,
               "official_plaintext_collaboration": ("send_message",) if mode != "opaque" else ()}
    retry = SimpleNamespace(request_timeout_seconds=2, request_kind="main_generation", policy=SimpleNamespace(value="default"),
                            retry_http_errors=False, base_open_attempts=1, open_attempt_budget=None,
                            new_open_attempt_budget=lambda: None)
    writer = Writer()
    writer._relay_transparent_upstream_response = lambda *a, **kw: pytest.fail("wrong relay")
    writer._relay_official_passthrough_sse_response = lambda *a, **kw: pytest.fail("wrong relay")
    # The view must hand back exactly the owner's byte objects, in order.
    import gateway_http_pool
    received, observed = [], []
    original_line = gateway_http_pool.PooledResponse.readline
    original_feed = observation.OfficialTap.feed if hasattr(observation, "OfficialTap") else None
    def line(owner, *args):
        if mode == "readerror":
            raise read_error
        data = original_line(owner, *args)
        received.append(data)
        return data
    def feed(tap, data):
        observed.append(data)
        try:
            return original_feed(tap, data)
        finally:
            if mode == "arguments":
                argument_lengths.extend(len(state["arguments"]) for state in tap.items.values())
    monkeypatch.setattr(gateway_http_pool.PooledResponse, "readline", line)
    if original_feed:
        monkeypatch.setattr(observation.OfficialTap, "feed", feed)
    read_error = OSError("inert reader exception")
    transport = gateway_exchange_adapters.LiveTransport(SimpleNamespace(handler=SimpleNamespace(
        connection=SimpleNamespace(getpeername=lambda: ("127.0.0.1", 12345)))))
    with GatewayHarness() as gateway:
        partition = 4096 if mode == "arguments" else 7
        gateway.stub.stream_chunks = tuple(raw[i:i+partition] for i in range(0, len(raw), partition))
        gateway.stub.response_status = 500 if mode == "error" else 200
        opening = OpenExchangeRequest(Request(gateway.stub_base_url + "/responses", data=json.dumps(payload).encode()),
            SimpleNamespace(retry=retry, transport_policy=None), "official", "responses", context, None, lambda: False, None, None)
        if mode == "error":
            from urllib.error import HTTPError
            with pytest.raises(HTTPError):
                with transport.open(opening):
                    pytest.fail("owner error must propagate")
        elif mode == "ownererror":
            marker = RuntimeError("inert owner exception")
            with pytest.raises(RuntimeError) as failure:
                with transport.open(opening) as response:
                    relay_official_passthrough_sse_response(relay_context_for_handler(writer), response, "official", event_context=context)
                    raise marker
            assert failure.value is marker
            assert response._released
        elif mode == "readerror":
            with transport.open(opening) as response:
                with pytest.raises(OSError) as failure:
                    response.readline()
                assert failure.value is read_error
            assert response._released
        else:
            with transport.open(opening) as response:
                if mode == "frozen":
                    (tmp_path / "peer-12345.json").write_bytes(json.dumps({**correlation, "epoch": 0, "request_id": "b" * 32}).encode())
                    context["request_id"] = "later-mutated-context"
                relay_official_passthrough_sse_response(relay_context_for_handler(writer), response, "official", event_context=context)
            assert response._released
            expected = b"".join(collaboration_delivery.decode_sse_line(line, context)
                                for line in raw.splitlines(keepends=True))
            assert writer.wfile.getvalue() == expected
    if mode not in ("missing", "captureerror"):
        assert (tmp_path / (correlation["request_id"] + ".json")).exists(), "Official public transport capture missing"
    if mode not in ("missing", "error") and original_feed:
        assert len(observed) <= len(received)  # A rejected tap stops; the owner's reader continues unchanged.
        assert all(left is right for left, right in zip(received, observed))
        if mode in ("exact", "toolonly", "utf8", "deletion", "u200b", "unapproved", "ownererror", "captureerror", "undeclared", "opaque", "partial", "frozen"):
            assert len(received) == len(observed)
    if mode == "arguments":
        assert argument_parses == [] and max(argument_lengths) <= observation.MAX_JSON_BYTES
    caller = original_capture(plan, correlation)
    downstream = observation.DownstreamTap(caller, official_payload=payload)
    for byte in writer.wfile.getvalue():
        downstream.feed(bytes([byte]))
    downstream.finish(mode not in ("error", "ownererror", "readerror"))
    joined = qualification.aggregate_fixture_observations([caller], tmp_path, observation,
        [{"fixture_request_id": correlation["request_id"], "epoch": 1, "model": qualification.OFFICIAL_MODEL}])[0]
    if mode in ("missing", "captureerror"):
        assert joined["native_report"] == {"state": "unavailable", "reason": "missing", "cause": "unknown"}
        return
    rows = joined["boundaries"]
    assert (tmp_path / (correlation["request_id"] + ".json")).exists(), "Official public transport capture missing"
    assert rows["canonicalchunks"]["state"] == rows["servedhistoryblob"]["state"] == "not-applicable"
    native = rows["officialreceived"]
    good = mode in ("exact", "toolonly", "utf8", "deletion", "u200b", "frozen")
    assert native["complete"] is good
    assert joined["actual_route"] == "official"
    assert joined["opening_association"]["gateway_request_sha256"] == hashlib.sha256(b"actual-gateway-request").hexdigest()
    assert joined["opening_association"]["route_attempt_index"] == 2
    assert "UNRELATED-TARGET" not in json.dumps(joined)
    if good:
        leaf = native["leaves"][0]
        assert leaf["channel"] == "arguments.message" and leaf["item_sha256"] != leaf["call_sha256"]
        assert leaf["utf8_bytes"] == len(result.encode())
        assert leaf["exact_expected_reverse"] is (mode in ("exact", "toolonly", "utf8", "frozen"))
        assert "utf8_hex" in leaf
        assert leaf["response_sha256"] == hashlib.sha256(b"actual-response").hexdigest()
        assert leaf["output_index"] == 0 and leaf["event_ordinal"] == 6
        assert leaf["part_count"] == 1
        if mode != "toolonly":
            assert native["leaves"][1]["part_count"] == 2
        messages = rows["downstreamOfficialmessages"]["leaves"]
        assert messages[0]["sha256"] == leaf["sha256"]
        if mode == "toolonly":
            assert rows["downstreamSSEcompleted"]["utf8_bytes"] == 0
    else:
        assert "utf8_hex" not in json.dumps(native)
        assert native["cause"] == "unknown"
        if malformed_index:
            assert joined["capture_failure"] == "observation-incomplete"
            assert rows["downstreamOfficialmessages"]["complete"] is False
            assert "utf8_hex" not in json.dumps(rows["downstreamOfficialmessages"])
        elif mode == "unapproved":
            assert joined["capture_failure"] is None
            for boundary in (native, rows["downstreamOfficialmessages"]):
                assert boundary["state"] == "unavailable" and boundary["complete"] is False
                assert boundary["cause"] == "unknown" and boundary["terminal_event_ordinal"] == len(events)
                assert len(boundary["leaves"]) == 2
                for leaf in boundary["leaves"]:
                    assert leaf["state"] == "unavailable" and leaf["complete"] is False
                    assert leaf["rejection"] == "unapproved"
                    assert leaf["sha256"] == hashlib.sha256(result.encode()).hexdigest()
                    assert leaf["utf8_bytes"] == len(result.encode())
                assert "utf8_hex" not in json.dumps(boundary) and "codepoints" not in json.dumps(boundary)
