"""Synthetic observation controls, never historical reproduction/live acceptance."""
import importlib.util
import json
import hashlib
import http.client
import gzip
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


def observed_run(tap, transport, payload=None, cancel=None):
    return observation.observe_cursor(payload or {"model": "fixture-high", "messages": [{"role": "user", "content": "AA\u200b界"}]},
        capture=tap, cancel=cancel or threading.Event(), timeout=2, transport_factory=transport,
        account_reader=lambda **kwargs: SimpleNamespace(check=lambda: None, token="SECRET-INERT", version="fixture"))


@pytest.mark.parametrize("value", ["AA\u200b界", "A\u200b界", "AA界"])
@pytest.mark.parametrize("compressed", [False, True])
def test_public_native_stream_exact_loss_controls_and_split_utf8(value, compressed):
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
    import subscription_exchange
    from tests.gateway_harness import GatewayHarness, GATEWAY_CLIENT_KEY, parsed_sse_events, require_single_terminal
    value = "AA\u200b界"
    plan = observation.fixture_plan(value)
    original_exchange = subscription_exchange.open_subscription
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
            observation.private_json(ticket, correlation)
            tap = observation.FixtureCapture(plan, correlation)
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
            report = json.loads((tmp_path / ("a" * 32 + ".json")).read_bytes())
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
            assert (tmp_path / ("a" * 32 + ".json")).stat().st_mode & 0o777 == 0o600
    finally:
        subscription_exchange.open_subscription = original_exchange


def test_preparation_has_no_cli_account_access_and_rollouts_do_not_relabel_old_epochs(tmp_path, monkeypatch):
    monkeypatch.setattr(qualification.subprocess, "run", lambda *a, **kw: pytest.fail("setup must not launch a CLI/git/account operation"))
    plan = tmp_path / "approved.json"
    assert qualification.main(["--prepare-fixture", str(plan)]) == 0
    assert observation.validate_plan(json.loads(plan.read_bytes()))
    value = json.loads(plan.read_bytes())["value"]
    assert len(value.encode()) == 24 and any(value[i:i+3] == value[i] * 3 for i in range(len(value) - 2))
    assert plan.stat().st_mode & 0o777 == 0o600
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
    with pytest.raises(ValueError, match="file-limit"):
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
def test_observation_admission_includes_executed_wrappers(tmp_path, monkeypatch, change):
    source = tmp_path / "source"
    source.mkdir()
    for name in qualification.OBSERVATION_WRAPPERS:
        target = source / name
        target.parent.mkdir(exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    (source / "src-python").mkdir()
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "fixture"], check=True)
    path = source / "scripts/subscription_fixture_observer.py"
    if change == "untracked":
        subprocess.run(["git", "-C", str(source), "rm", "--cached", str(path)], check=True, stdout=subprocess.DEVNULL)
        subprocess.run(["git", "-C", str(source), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", "untrack"], check=True)
    else:
        with path.open("a") as output:
            output.write("\n# uncommitted fixture change\n")
        if change == "staged":
            subprocess.run(["git", "-C", str(source), "add", "."], check=True)
    spec = importlib.util.spec_from_file_location("selected_qualification", source / qualification.OBSERVATION_WRAPPERS[0])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    plan = tmp_path / "plan.json"
    observation.private_json(plan, observation.fixture_plan())
    monkeypatch.setattr(module, "freeze_candidate", lambda *a: pytest.fail("dirty wrapper must fail before freeze/accounts"))
    assert module.main(["--checkout", str(source), "--codex", sys.executable, "--observe-fixture", str(plan),
                        "--case", "cursor-to-official", "--output", str(tmp_path / "report.json")]) == 1
    report = json.loads((tmp_path / "report.json").read_bytes())
    assert report["source_runtime_dirty"] and not report["candidate_sha_is_exact_runtime"]


def test_freeze_hashes_and_executes_exact_wrapper_bytes(tmp_path):
    source, frozen = tmp_path / "source", tmp_path / "frozen"
    source.mkdir()
    for name in qualification.OBSERVATION_WRAPPERS:
        path = source / name
        path.parent.mkdir(exist_ok=True)
        shutil.copyfile(ROOT / name, path)
    subprocess.run(["git", "init", "-q", str(source)], check=True)
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
