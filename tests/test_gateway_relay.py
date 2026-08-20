from io import BytesIO
from types import SimpleNamespace

from gateway_relay import relay_raw_response, write_non_streaming_body


class Writer:
    def __init__(self):
        self.close_connection = False
        self.wfile = BytesIO()
        self.responses = []
        self.headers = []
        self.ended = False

    def send_response(self, status):
        self.responses.append(status)

    def send_header(self, key, value):
        self.headers.append((key, value))

    def end_headers(self):
        self.ended = True


def headers(source, is_event_stream, *, content_length=None, **_):
    return [(key, value) for key, value in source.items() if key.lower() != "content-length"] + (
        [("Content-Length", str(content_length))] if content_length is not None else []
    )


def test_relay_raw_response_forwards_body_and_headers():
    writer = Writer()
    response = SimpleNamespace(status=201, headers={"Content-Type": "application/json"}, read=lambda: b"{}")
    status = relay_raw_response(
        response,
        "official",
        writer=writer,
        filtered_headers=headers,
        active_request=lambda: None,
    )
    assert status == 201
    assert writer.responses == [201]
    assert writer.ended
    assert writer.wfile.getvalue() == b"{}"
    assert ("X-Codex-Proxy-Upstream", "official") in writer.headers
    assert writer.close_connection


def test_relay_writer_reports_closed_socket_as_499():
    writer = Writer()
    writer.wfile = SimpleNamespace(write=lambda _: (_ for _ in ()).throw(OSError()), flush=lambda: None)
    response = SimpleNamespace(status=200, headers={}, read=lambda: b"body")
    status = relay_raw_response(
        response,
        "third_party",
        writer=writer,
        filtered_headers=headers,
        active_request=lambda: None,
    )
    assert status == 499
    assert writer.close_connection


def test_write_non_streaming_body_isolated_seam():
    writer = Writer()
    assert write_non_streaming_body(writer, b"payload")
    assert writer.wfile.getvalue() == b"payload"
