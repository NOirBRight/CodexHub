"""AgentService public exchange contract, using independent wire fixtures."""
from __future__ import annotations

import base64
import gzip
import json
import os
from pathlib import Path
import struct
import threading
import time

import pytest

from cursor_subscription_backend import CursorAccount, load_cursor_account, stream_chat
from subscription_backend_contract import BackendError


def vi(value):
    out = bytearray()
    while value >= 128:
        out.append((value & 127) | 128)
        value >>= 7
    return bytes(out + bytes([value]))


def pb(field, value):
    if isinstance(value, int):
        return vi(field << 3) + vi(value)
    if isinstance(value, str):
        value = value.encode()
    return vi(field << 3 | 2) + vi(len(value)) + value


def framed(data, flag=0):
    return bytes([flag]) + struct.pack(">I", len(data)) + data


def update(field, body):
    return framed(pb(1, pb(field, body) + pb(99, 1)))


def call(name="random_tool", call_id="call_123\nfc_typed", args=None):
    body = pb(1, "codexhub-" + name) + pb(3, call_id) + pb(5, name)
    for key, value in (args or {"nonce": "unpredictable"}).items():
        body += pb(2, pb(1, key) + pb(2, pb(3, value)))
    return framed(pb(2, pb(1, 7) + pb(15, "exec-id") + pb(11, body)))


def account(tmp_path, expiry=None):
    source = tmp_path / "auth.json"
    source.write_text('{"accessToken":"PRIVATE-TOKEN"}')
    return CursorAccount(source, source.read_bytes(), "PRIVATE-TOKEN", "2026.09.28-64d2043", expiry)


class Duplex:
    def __init__(self, factory, url, headers, **_kwargs):
        self.factory, self.url, self.headers = factory, url, headers
        self.sent = []
        self.closed = False
        self.ended_request = False
        self.status = 200
        self.response_headers = {}
        self.chunks = list(factory.chunks) if url.endswith("/Run") else [json.dumps(factory.config).encode(), b""]
        if url.endswith("/Run"):
            self.status = factory.status
        factory.streams.append(self)

    def __enter__(self):
        return self

    def send(self, data):
        self.sent.append(data)

    def finish_request(self):
        self.ended_request = True

    def receive(self):
        if self.url.endswith("/Run") and self.factory.on_receive:
            self.factory.on_receive(self)
        if self.chunks:
            return self.chunks.pop(0)
        return b""

    def __exit__(self, *_args):
        self.closed = True


class Transport:
    def __init__(self, chunks, status=200, config=None, on_receive=None):
        self.chunks, self.status, self.on_receive = chunks, status, on_receive
        self.config = config or {"agentUrlConfig": {"agentUrl": "https://agent.api5.cursor.sh"}}
        self.streams = []

    def __call__(self, *args, **kwargs):
        return Duplex(self, *args, **kwargs)


def run(lease, transport, payload=None, cancel=None, timeout=1):
    return stream_chat(payload or {"model": "gpt-5.6-luna-high-fast", "messages": [{"role": "user", "content": "hello"}]},
                       cancel=cancel or threading.Event(), timeout=timeout, transport_factory=transport,
                       account_reader=lambda **_kwargs: lease)


def tool_payload(messages=None):
    return {"model": "exact-vendor-high-fast", "messages": messages or [{"role": "user", "content": "Use random_tool"}],
            "tools": [{"type": "function", "function": {"name": "random_tool", "parameters": {"type": "object", "properties": {"nonce": {"type": "string"}}}}}]}


def test_text_exact_model_truthful_usage_and_isolated_headers(tmp_path):
    lease = account(tmp_path)
    transport = Transport([update(1, pb(1, "hi\u200b")), update(14, pb(1, 10) + pb(2, 3) + pb(3, 2) + pb(5, "vendor-model"))])
    chunks = list(run(lease, transport))
    assert chunks[1]["choices"][0]["delta"] == {"content": "hi\u200b"}
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    assert chunks[-1]["usage"] == {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13, "prompt_tokens_details": {"cached_tokens": 2}}
    assert all(row["model"] == "gpt-5.6-luna-high-fast" for row in chunks)
    connection = transport.streams[-1]
    assert connection.sent[0].count(b"gpt-5.6-luna-high-fast") == 4
    assert connection.headers["x-cursor-agent-allowed-tools"] == "mcp_tool_call,get_mcp_tools_tool_call"
    assert connection.headers["x-cursor-client-version"] == "cli-2026.09.28-64d2043"
    assert connection.closed and transport.streams[0].closed
    assert "PRIVATE-TOKEN" not in repr(lease)


def test_unreported_usage_stays_unknown(tmp_path):
    chunks = list(run(account(tmp_path), Transport([update(1, pb(1, "answer")), framed(b"{}", 2)])))
    assert all("usage" not in chunk for chunk in chunks)


def test_real_calls_are_not_executed_and_ids_replay_after_fresh_backend(tmp_path):
    lease = account(tmp_path)
    transport = Transport([update(27, pb(1, 1)), call()])
    chunks = list(run(lease, transport, tool_payload()))
    emitted = chunks[1]["choices"][0]["delta"]["tool_calls"][0]
    call_id = emitted["id"]
    assert chunks[-1]["choices"][0]["finish_reason"] == "tool_calls"
    assert emitted["function"] == {"name": "random_tool", "arguments": '{"nonce":"unpredictable"}'}
    assert len(transport.streams[-1].sent) == 1  # no fake tool answer on the stream
    messages = [{"role": "user", "content": "Use random_tool"}, {"role": "assistant", "content": None, "tool_calls": [dict(emitted, index=None)]},
                {"role": "tool", "tool_call_id": call_id, "content": '{"result":"caller-owned-random-value"}'}, {"role": "user", "content": "continue"}]
    followup = Transport([update(1, pb(1, "continued")), update(14, b"")])
    list(run(lease, followup, tool_payload(messages)))
    initial = followup.streams[-1].sent[0]
    # Request holds hashes rather than the transcript; ask every hash for blobs.
    # Independent protobuf parser finds length-delimited hash fields recursively.
    def hashes(data):
        found = set()
        for start in range(len(data) - 33):
            if data[start:start + 2] == b"\x0a\x20":
                found.add(data[start + 2:start + 34])
        return found
    requests = [framed(pb(4, pb(1, number) + pb(2, pb(1, key)))) for number, key in enumerate(hashes(initial), 1)]
    replay = Transport(requests + [update(1, pb(1, "continued")), update(14, b"")])
    list(run(lease, replay, tool_payload(messages)))
    blobs = b"".join(replay.streams[-1].sent[1:])
    assert b"call_123\\nfc_typed" in blobs
    assert b"caller-owned-random-value" in blobs
    assert b"unpredictable" in blobs


def test_partial_tool_step_fails_instead_of_finishing(tmp_path):
    stream = run(account(tmp_path), Transport([update(27, pb(1, 2)), call(), b""]), tool_payload())
    with pytest.raises(BackendError, match="before its turn completed"):
        list(stream)


@pytest.mark.parametrize("chunks,code", [([framed(b"\x0a\x80")], "upstream-protocol-error"),
                                         ([framed(b'{"error":{"code":"resource_exhausted","message":"PRIVATE-TOKEN"}}', 2)], "usage-limit"),
                                         ([call("not_declared")], "undeclared-tool"),
                                         ([framed(b"{}", 2)], "empty-reply")])
def test_errors_are_bounded_and_close(tmp_path, chunks, code):
    transport = Transport(chunks)
    with pytest.raises(BackendError) as error:
        list(run(account(tmp_path), transport, tool_payload()))
    assert error.value.code == code
    assert "PRIVATE-TOKEN" not in str(error.value)
    assert all(stream.closed for stream in transport.streams)


@pytest.mark.parametrize("status,code", [(401, "auth-expired"), (403, "not-eligible"), (429, "usage-limit"), (500, "upstream-error")])
def test_http_status_before_first_chunk(tmp_path, status, code):
    iterator = run(account(tmp_path), Transport([b'PRIVATE-TOKEN user@example.com'], status))
    with pytest.raises(BackendError) as error:
        next(iterator)
    assert error.value.code == code
    assert "PRIVATE-TOKEN" not in str(error.value)


@pytest.mark.parametrize("url", ["http://agent.cursor.sh", "https://evil.example", "https://cursor.sh.evil.example", "https://agent.cursor.sh:444", "https://user@agent.cursor.sh", "https://agent.cursor.sh/x", "https://agent.cursor.sh?query"])
def test_server_config_never_redirects_credentials_to_untrusted_host(tmp_path, url):
    transport = Transport([], config={"agentUrlConfig": {"agentUrl": url}})
    with pytest.raises(BackendError) as error:
        list(run(account(tmp_path), transport))
    assert error.value.code == "upstream-configuration-error"
    assert len(transport.streams) == 1


def test_cancellation_and_consumer_close_cleanup(tmp_path):
    cancel = threading.Event()
    transport = Transport([update(1, pb(1, "one"))])
    iterator = run(account(tmp_path), transport, cancel=cancel)
    next(iterator)
    next(iterator)
    cancel.set()
    with pytest.raises(BackendError) as error:
        next(iterator)
    assert error.value.code == "cancelled" and transport.streams[-1].closed
    transport = Transport([update(1, pb(1, "one"))])
    iterator = run(account(tmp_path), transport)
    next(iterator)
    iterator.close()
    assert transport.streams[-1].closed


def test_changed_account_invalidates_active_stream(tmp_path):
    lease = account(tmp_path)
    def switch(_stream):
        lease.source.write_text('{"accessToken":"different-account"}')
    transport = Transport([update(1, pb(1, "one"))], on_receive=switch)
    with pytest.raises(BackendError) as error:
        list(run(lease, transport))
    assert error.value.code == "account-changed"
    assert all(item.closed for item in transport.streams)


@pytest.mark.parametrize("payload", [tool_payload([{"role": "assistant", "tool_calls": [{"id": "call_missing", "type": "function", "function": {"name": "random_tool", "arguments": "{}"}}]}]),
                                      {"model": "exact", "messages": [{"role": "tool", "tool_call_id": "orphan", "content": "value"}]},
                                      {"model": "exact", "messages": [{"role": "user", "content": [{"type": "audio", "data": "essential"}]}]},
                                      {"model": "exact", "messages": [{"role": "user", "content": "hello"}], "temperature": .2}])
def test_essential_unrepresentable_inputs_fail_before_connection(tmp_path, payload):
    transport = Transport([])
    with pytest.raises(BackendError):
        list(run(account(tmp_path), transport, payload))
    assert not transport.streams


def test_compressed_fragmented_frames_and_blob_refusal(tmp_path):
    lease = account(tmp_path)
    message = pb(1, pb(1, pb(1, "fragmented")))
    data = framed(gzip.compress(message), 1)
    transport = Transport([framed(pb(4, pb(1, 22) + pb(2, pb(1, b"unknown-blob"))))] + [data[:3], data[3:7], data[7:]] + [update(14, b"")])
    chunks = list(run(lease, transport))
    assert chunks[1]["choices"][0]["delta"]["content"] == "fragmented"
    assert b"blob not found" in transport.streams[-1].sent[1]


def test_builtin_execution_is_refused(tmp_path):
    transport = Transport([framed(pb(2, pb(1, 42) + pb(15, "exec") + pb(8, pb(1, "rm anything")))), update(1, pb(1, "answer")), update(14, b"")])
    list(run(account(tmp_path), transport))
    assert b"not available" in transport.streams[-1].sent[1]
    assert len(transport.streams[-1].sent) == 3


def test_official_source_expiry_and_version(tmp_path):
    version = "2026.09.28-64d2043"
    binary = tmp_path / "versions" / version / ("cursor-agent.exe" if os.name == "nt" else "cursor-agent")
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\nexit 1\n")
    binary.chmod(0o700)
    config = tmp_path / "config" / ("Cursor" if os.name == "nt" else "cursor")
    config.mkdir(parents=True)
    encoded = base64.urlsafe_b64encode(json.dumps({"exp": time.time() - 1}).encode()).decode().rstrip("=")
    (config / "auth.json").write_text(json.dumps({"accessToken": "x." + encoded + ".sig"}))
    env = {"PATH": str(binary.parent), "XDG_CONFIG_HOME": str(config.parent), "APPDATA": str(config.parent)}
    with pytest.raises(BackendError) as error:
        load_cursor_account(source_home=tmp_path, environ=env)
    assert error.value.code == "auth-expired"
    (config / "auth.json").write_text('{"accessToken":"official-token"}')
    lease = load_cursor_account(source_home=tmp_path, environ=env)
    assert lease.version == version
    assert lease.token == "official-token"


def test_duplex_http2_reads_before_request_end_and_respects_flow_control(tmp_path):
    """Real TLS/h2 peer: response arrives while the client upload is still open."""
    import shutil
    import socket
    import ssl
    import subprocess
    from cursor_subscription_backend import HTTP2Duplex
    from subscription_backend_contract import load_http2_dependencies
    load_http2_dependencies()
    from h2.config import H2Configuration
    from h2.connection import H2Connection
    from h2.events import DataReceived, RequestReceived
    openssl = shutil.which("openssl")
    if not openssl:
        pytest.skip("OpenSSL test certificate utility unavailable")
    cert, key = tmp_path / "cert.pem", tmp_path / "key.pem"
    subprocess.run([openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
                    "-out", str(cert), "-days", "1", "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost"],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert, key)
    server_context.set_alpn_protocols(["h2"])
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    received_bytes = []
    server_errors = []
    initial = b"initial-request"
    response_blob = b"blob-result-" * 25000  # exceeds h2's initial flow-control window
    def serve():
        try:
            with listener:
                raw, _address = listener.accept()
                with server_context.wrap_socket(raw, server_side=True) as peer:
                    peer.settimeout(8)
                    connection = H2Connection(config=H2Configuration(client_side=False))
                    connection.initiate_connection()
                    peer.sendall(connection.data_to_send())
                    total = bytearray()
                    asked = False
                    while True:
                        data = peer.recv(65536)
                        if not data:
                            return
                        for event in connection.receive_data(data):
                            if isinstance(event, RequestReceived):
                                connection.send_headers(event.stream_id, [(":status", "200")])
                            if isinstance(event, DataReceived):
                                total.extend(event.data)
                                connection.acknowledge_received_data(event.flow_controlled_length, event.stream_id)
                                if not asked and len(total) >= len(initial):
                                    connection.send_data(event.stream_id, b"request-a-blob")
                                    asked = True
                                if len(total) >= len(initial) + len(response_blob) + len(framed(pb(7, b""))):
                                    received_bytes.append(bytes(total))
                                    connection.send_data(event.stream_id, b"turn-completed", end_stream=True)
                                    peer.sendall(connection.data_to_send())
                                    return
                        outgoing = connection.data_to_send()
                        if outgoing:
                            peer.sendall(outgoing)
        except Exception as error:
            server_errors.append(type(error).__name__)
    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    cancel = threading.Event()
    with HTTP2Duplex("https://localhost:" + str(listener.getsockname()[1]) + "/Run", {"content-type": "application/connect+proto"},
                     cancel=cancel, timeout=10, tls_context_factory=lambda: ssl.create_default_context(cafile=str(cert))) as transport:
        transport.send(initial)
        reply = None
        while reply is None:
            reply = transport.receive()
        assert reply == b"request-a-blob"
        assert transport.status == 200
        # Pause the consumer: keepalives belong to the connection pump.
        time.sleep(5.2)
        transport.send(response_blob)
        result = bytearray()
        while True:
            reply = transport.receive()
            if reply is None:
                continue
            if not reply:
                break
            result.extend(reply)
        assert bytes(result) == b"turn-completed"
    thread.join(timeout=2)
    assert not thread.is_alive() and not server_errors
    assert received_bytes == [initial + framed(pb(7, b"")) + response_blob]


def test_dynamic_tool_listing_and_context_expose_only_caller_tools(tmp_path):
    transport = Transport([framed(pb(2, pb(1, 1) + pb(15, "list") + pb(36, b""))),
                           framed(pb(2, pb(1, 2) + pb(15, "context") + pb(10, b""))),
                           update(1, pb(1, "answer")), update(14, b"")])
    list(run(account(tmp_path), transport, tool_payload()))
    sent = transport.streams[-1].sent
    assert b"random_tool" in sent[1]
    assert b"codexhub" in sent[1]
    assert b"PRIVATE-TOKEN" not in b"".join(sent)
    assert len(sent) == 5  # list/context response, each with exec-close


def test_none_tool_choice_removes_catalog_and_definition(tmp_path):
    payload = tool_payload()
    payload["tool_choice"] = "none"
    transport = Transport([call()])
    with pytest.raises(BackendError) as error:
        list(run(account(tmp_path), transport, payload))
    assert error.value.code == "undeclared-tool"
    assert b"random_tool" not in transport.streams[-1].sent[0]


def test_expired_admission_does_not_open_transport(tmp_path):
    lease = account(tmp_path, time.time() - 1)
    transport = Transport([])
    with pytest.raises(BackendError) as error:
        list(run(lease, transport))
    assert error.value.code == "auth-expired"
    assert not transport.streams


@pytest.mark.parametrize("model", ["gpt-5.6-luna-high", "gpt-5.6-luna-high-fast", "vendor.new:exact+id[extended]", "vendor/raw-id", "auto", "new-future-model"])
def test_vendor_model_is_never_remapped_or_whitelisted(tmp_path, model):
    transport = Transport([update(1, pb(1, "okay")), update(14, b"")])
    payload = {"model": model, "messages": [{"role": "user", "content": "hello"}]}
    chunks = list(run(account(tmp_path), transport, payload))
    assert all(chunk["model"] == model for chunk in chunks)
    assert transport.streams[-1].sent[0].count(model.encode()) == 4


def test_cli_version_fallback_runs_with_empty_private_configuration(tmp_path):
    if os.name == "nt":
        pytest.skip("POSIX executable fixture; Windows layout is exercised by release qualification")
    binary = tmp_path / "installed" / "cursor-agent"
    binary.parent.mkdir()
    binary.write_text('#!/bin/sh\n[ "$ANTHROPIC_API_KEY" = "" ] || exit 1\n[ "$HTTP_PROXY" = "" ] || exit 1\n[ "$1" = "--version" ] || exit 1\n[ "$HOME" != "' + str(tmp_path) + '" ] || exit 1\nprintf "2026.09.28-64d2043\\n"\n')
    binary.chmod(0o700)
    config = tmp_path / "config" / "cursor"
    config.mkdir(parents=True)
    (config / "auth.json").write_text('{"accessToken":"official-token"}')
    lease = load_cursor_account(source_home=tmp_path, environ={"PATH": str(binary.parent), "XDG_CONFIG_HOME": str(config.parent),
                                                             "ANTHROPIC_API_KEY": "private-key", "HTTP_PROXY": "http://gateway.invalid"})
    assert lease.version == "2026.09.28-64d2043"
    assert lease.token == "official-token"
