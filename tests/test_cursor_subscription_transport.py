"""Sanitized transport diagnostics through the public subscription HTTP boundary.

All peers are local fixtures. No installed CLI, account, or vendor is contacted.
"""
from __future__ import annotations

import json
import shutil
import socket
import ssl
import subprocess
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from cursor_subscription_backend import CursorAccount, HTTP2Duplex, stream_chat
from subscription_backend_contract import load_http2_dependencies
from subscription_backend_contract import BackendError
from subscription_exchange import open_subscription


SECRET = "PRIVATE-CREDENTIAL user@example.invalid private-host.invalid"
_PEER_SETUP_SECONDS = 3
_IDLE_SECONDS = .3


@pytest.fixture(scope="module")
def certificate(tmp_path_factory):
    openssl = shutil.which("openssl")
    if not openssl:
        pytest.skip("OpenSSL test certificate utility unavailable")
    root = tmp_path_factory.mktemp("cursor-local-tls")
    cert, key = root / "cert.pem", root / "key.pem"
    subprocess.run([openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                    "-keyout", str(key), "-out", str(cert), "-days", "1",
                    "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    return cert, key


def public_error(tmp_path, url, context_factory=ssl.create_default_context):
    source = tmp_path / "auth.json"
    source.write_text(json.dumps({"accessToken": SECRET}))
    lease = CursorAccount(source, source.read_bytes(), SECRET, "2026.09.28-64d2043")
    opens = []

    def transport(_url, headers, **options):
        opens.append(True)
        return HTTP2Duplex(url, headers, **options, tls_context_factory=context_factory)

    def backend(payload, **options):
        return stream_chat(payload, **options, account_reader=lambda **_kwargs: lease,
                           transport_factory=transport)

    request = Request("https://cli-subscription.invalid", data=json.dumps({
        "model": "exact-model", "messages": [{"role": "user", "content": "hello"}],
    }).encode())
    with pytest.raises(HTTPError) as caught:
        with open_subscription(request, provider_id="cursor-subscription", timeout=3, backend=backend):
            pytest.fail("transport failure must precede any successful response")
    body = caught.value.read()
    assert caught.value.code == 502
    assert len(opens) == 1  # Never retry or fall back after a transport exception.
    for private in (SECRET, "localhost", "127.0.0.1", "private-host.invalid", str(source)):
        assert private.encode() not in body
    assert not any(thread.name == "cursor-http2" and thread.is_alive()
                   for thread in threading.enumerate())
    return json.loads(body)["error"]


def test_dns_failure_is_distinct_sanitized_http_error(tmp_path, monkeypatch):
    def fail_lookup(*_args, **_kwargs):
        raise socket.gaierror(socket.EAI_NONAME, SECRET)
    monkeypatch.setattr(socket, "getaddrinfo", fail_lookup)
    assert public_error(tmp_path, "https://private-host.invalid/Run")["code"] == "upstream-dns-error"


def test_connect_refused_is_distinct_sanitized_http_error(tmp_path):
    # A bound, non-listening local socket owns the port throughout this test.
    with socket.socket() as reserved:
        reserved.bind(("127.0.0.1", 0))
        url = "https://localhost:" + str(reserved.getsockname()[1]) + "/Run"
        assert public_error(tmp_path, url)["code"] == "upstream-connect-error"


@pytest.fixture
def local_peer(certificate):
    cert, key = certificate
    peers = []

    def start(mode):
        load_http2_dependencies()
        from h2.config import H2Configuration
        from h2.connection import H2Connection
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        context.set_alpn_protocols(["http/1.1"] if mode == "alpn" else ["h2"])
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(3)
        # The fixture listens on IPv4; do not spend the behavior deadline on
        # Windows localhost resolution/IPv6 fallback. TLS still verifies SAN.
        url = "https://127.0.0.1:" + str(listener.getsockname()[1]) + "/Run"
        accepted = []
        connected = threading.Event()

        def serve():
            try:
                with listener:
                    raw, _address = listener.accept()
                    accepted.append(True)
                    with context.wrap_socket(raw, server_side=True) as peer:
                        if mode != "idle":
                            connected.set()
                        peer.settimeout(3)
                        if mode == "tls":
                            peer.recv(65536)
                        elif mode == "read":
                            # Corrupt TLS record after a successful handshake:
                            # exercise a real SSLSocket.recv exception, not EOF.
                            with socket.socket(fileno=peer.detach()) as plain:
                                plain.sendall(b"INVALID-TLS-RECORD" + SECRET.encode())
                        elif mode == "h2":
                            connection = H2Connection(config=H2Configuration(client_side=False))
                            connection.initiate_connection()
                            # DATA on stream zero is forbidden by HTTP/2.
                            peer.sendall(connection.data_to_send() + bytes.fromhex("00000100000000000078"))
                            peer.recv(65536)
                        elif mode == "reset":
                            from h2.events import RequestReceived
                            connection = H2Connection(config=H2Configuration(client_side=False))
                            connection.initiate_connection()
                            peer.sendall(connection.data_to_send())
                            while data := peer.recv(65536):
                                if any(isinstance(event, RequestReceived) for event in connection.receive_data(data)):
                                    break
                            else:
                                return
                            connection.reset_stream(1)
                            peer.sendall(connection.data_to_send())
                            # Let the actual RST_STREAM reach the client. Closing
                            # with unread request data races it with a Winsock
                            # read error, which is a different behavior.
                            while peer.recv(65536):
                                pass
                        elif mode == "idle":
                            peer.settimeout(_PEER_SETUP_SECONDS + _IDLE_SECONDS + 1)
                            from h2.events import DataReceived
                            connection = H2Connection(config=H2Configuration(client_side=False))
                            connection.initiate_connection()
                            peer.sendall(connection.data_to_send())
                            while data := peer.recv(65536):
                                if any(isinstance(event, DataReceived) for event in connection.receive_data(data)):
                                    connected.set()
                        else:
                            peer.recv(65536)
            except (OSError, ssl.SSLError):
                # Expected peer abort/certificate rejection; retain no diagnostics.
                pass

        thread = threading.Thread(target=serve, daemon=True, name="local-cursor-peer")
        thread.start()
        peers.append((thread, listener, accepted))
        return url, lambda: ssl.create_default_context(cafile=str(cert)), connected

    yield start
    for thread, listener, accepted in peers:
        listener.close()
        thread.join(timeout=4)
        assert not thread.is_alive()
        assert accepted == [True]


def test_tls_verification_failure_is_distinct_sanitized_http_error(tmp_path, local_peer):
    url, _trusted, _connected = local_peer("tls")
    assert public_error(tmp_path, url)["code"] == "upstream-tls-error"


def test_tls_record_read_failure_is_distinct_sanitized_http_error(tmp_path, local_peer):
    url, trusted, _connected = local_peer("read")
    assert public_error(tmp_path, url, trusted)["code"] == "upstream-read-error"


def test_invalid_http2_frame_is_distinct_sanitized_http_error(tmp_path, local_peer):
    url, trusted, _connected = local_peer("h2")
    assert public_error(tmp_path, url, trusted)["code"] == "upstream-http2-error"


def test_socket_write_failure_is_distinct_sanitized_http_error(tmp_path, local_peer):
    url, trusted, _connected = local_peer("idle")

    class FailingWriteSocket:
        def __init__(self, delegate):
            self.delegate = delegate

        def __getattr__(self, name):
            return getattr(self.delegate, name)

        def sendall(self, _data):
            raise OSError(SECRET)

    class Context:
        def __init__(self):
            self.delegate = trusted()

        def set_alpn_protocols(self, protocols):
            self.delegate.set_alpn_protocols(protocols)

        def wrap_socket(self, *args, **kwargs):
            return FailingWriteSocket(self.delegate.wrap_socket(*args, **kwargs))

    assert public_error(tmp_path, url, Context)["code"] == "upstream-write-error"


@pytest.mark.parametrize("mode,code", [("eof", "upstream-interrupted"),
                                      ("reset", "upstream-interrupted"),
                                      ("alpn", "upstream-protocol-error")])
def test_existing_transport_terminal_errors_remain_distinct(tmp_path, local_peer, mode, code):
    url, trusted, _connected = local_peer(mode)
    assert public_error(tmp_path, url, trusted)["code"] == code


@pytest.mark.parametrize("cancelled", [False, True])
def test_transport_deadline_and_cancel_remain_distinct(local_peer, cancelled):
    url, trusted, connected = local_peer("idle")
    cancel = threading.Event()
    started = time.monotonic()
    # The real transport has a total deadline. Reserve a bounded fixture
    # setup budget plus the idle interval; never change production deadlines.
    with HTTP2Duplex(url, {}, cancel=cancel, timeout=_PEER_SETUP_SECONDS + _IDLE_SECONDS,
                     tls_context_factory=trusted) as transport:
        transport.send(b"request")
        assert connected.wait(_PEER_SETUP_SECONDS)
        assert time.monotonic() - started < _PEER_SETUP_SECONDS
        if cancelled:
            cancel.set()
        with pytest.raises(BackendError) as caught:
            while transport.receive() is None:
                pass
    assert caught.value.code == ("cancelled" if cancelled else "upstream-timeout")


def test_cancellation_takes_precedence_over_closed_connect_socket(tmp_path, monkeypatch):
    cancel = threading.Event()

    def fail_connect(*_args, **_kwargs):
        cancel.set()
        raise OSError(SECRET)

    monkeypatch.setattr(socket, "create_connection", fail_connect)
    with HTTP2Duplex("https://private-host.invalid/Run", {}, cancel=cancel, timeout=1) as transport:
        with pytest.raises(BackendError) as caught:
            while transport.receive() is None:
                pass
    assert caught.value.code == "cancelled"
    assert SECRET not in str(caught.value)
