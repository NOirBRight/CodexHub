import inspect
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import codex_proxy
from gateway_relay import (
    SseLineRelayContext,
    iter_upstream_sse_lines,
    relay_raw_response,
    write_non_streaming_body,
    write_sse_bytes,
    write_sse_done,
)


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


def test_relay_raw_response_preserves_injected_body_hook():
    writer = Writer()
    seen = []
    response = SimpleNamespace(status=200, headers={}, read=lambda: b"body")
    status = relay_raw_response(
        response,
        "official",
        writer=writer,
        filtered_headers=headers,
        active_request=lambda: None,
        write_body=lambda body: seen.append(body) or True,
    )
    assert status == 200
    assert seen == [b"body"]
    assert writer.wfile.getvalue() == b""


def test_relay_raw_response_checks_request_admission_before_headers():
    writer = Writer()

    class Admission:
        def raise_if_cancelled(self):
            raise RuntimeError("cancelled")

    response = SimpleNamespace(status=200, headers={}, read=lambda: b"body")
    try:
        relay_raw_response(
            response,
            "official",
            writer=writer,
            filtered_headers=headers,
            active_request=lambda: Admission(),
        )
    except RuntimeError as error:
        assert str(error) == "cancelled"
    else:
        raise AssertionError("cancellation was not propagated")
    assert writer.responses == []


def test_sse_commit_callback_receives_keyword_only_observe():
    writer = Writer()
    calls = []

    def commit(data, *, observe=True):
        calls.append((data, observe))
        return True

    assert write_sse_bytes(writer, b"frame", commit_sse_bytes=commit, observe=False)
    assert calls == [(b"frame", False)]
    assert writer.wfile.getvalue() == b""


def test_sse_done_respects_terminal_commit_guard():
    writer = Writer()
    calls = []

    def commit(data, *, observe=True):
        calls.append(data)
        return True

    assert write_sse_done(writer, commit_sse_bytes=commit, terminal_committed=True)
    assert calls == []


def test_sse_bytes_preserves_direct_write_errors():
    writer = Writer()
    writer.wfile = SimpleNamespace(
        write=lambda _: (_ for _ in ()).throw(OSError("closed")),
        flush=lambda: None,
    )
    try:
        write_sse_bytes(writer, b"frame")
    except OSError as error:
        assert str(error) == "closed"
    else:
        raise AssertionError("direct SSE write error was swallowed")


def test_relay_context_and_facade_adapter_are_the_seam():
    from gateway_relay import (
        RelayContext,
        relay_official_passthrough_sse_response,
        relay_transparent_upstream_response,
        relay_upstream_response,
    )

    assert {
        "handler",
        "glue",
        "transparent_relay",
        "official_passthrough_relay",
        "prepared_exchange",
    } <= set(RelayContext.__annotations__)
    for method_name, extracted_name in (
        ("_relay_upstream_response", "relay_upstream_response("),
        ("_relay_transparent_upstream_response", "relay_transparent_upstream_response("),
        ("_relay_official_passthrough_sse_response", "relay_official_passthrough_sse_response("),
    ):
        source = inspect.getsource(getattr(codex_proxy.CodexProxyHandler, method_name))
        assert len(source.splitlines()) < 50, method_name
        assert extracted_name in source, method_name
        assert "_relay_context_for_handler(" in source, method_name
    module_source = Path(relay_upstream_response.__code__.co_filename).read_text(encoding="utf-8")
    assert "import codex_proxy" not in module_source
    assert "import *" not in module_source
    assert 'globals()[' not in module_source
    assert "from gateway_relay_passthrough import" in module_source
    passthrough_source = Path(
        relay_transparent_upstream_response.__code__.co_filename
    ).read_text(encoding="utf-8")
    assert "def relay_transparent_upstream_response(" in passthrough_source
    assert "def relay_official_passthrough_sse_response(" in passthrough_source
    assert "import codex_proxy" not in passthrough_source
    assert relay_official_passthrough_sse_response.__code__.co_filename == (
        relay_transparent_upstream_response.__code__.co_filename
    )
    assert Path(relay_transparent_upstream_response.__code__.co_filename).name == (
        "gateway_relay_passthrough.py"
    )


def test_sse_line_iterator_uses_injected_lifecycle():
    class Lifecycle:
        closed = False

        def __init__(self):
            self.values = [("line", b"one"), ("line", b"")]
            self.started = False
            self.closed_calls = 0
            self.joined = False

        def start(self):
            self.started = True

        def get(self, timeout=None):
            return self.values.pop(0)

        def close(self):
            self.closed_calls += 1

        def join(self, timeout):
            self.joined = True

    lifecycle = Lifecycle()
    attached = []
    context = SseLineRelayContext(
        admission=None,
        keepalive_interval=0,
        transport_timeout_seconds=0,
        model_event_timeout_seconds=0,
        lifecycle_factory=lambda response, admission: lifecycle,
        attach_upstream=attached.append,
        write_keepalive=lambda: True,
        idle_timeout_error=lambda seconds, phase: RuntimeError(phase),
        keepalive_failure_error=RuntimeError,
        join_timeout_seconds=1,
    )
    assert list(iter_upstream_sse_lines(object(), context=context)) == [b"one", b""]
    assert attached == [lifecycle]
    assert lifecycle.started
    assert lifecycle.closed_calls == 1
    assert lifecycle.joined


def test_relay_assembled_output_commits_headers_and_terminal_once():
    from gateway_relay import RelaySseOutput, iter_upstream_sse_events
    from gateway_exchange_bindings import bind_downstream_stream_commit

    writer = Writer()
    commit = bind_downstream_stream_commit(writer, None, "official")
    output = RelaySseOutput(writer, commit)

    def read_lines(response, *, line_resets_idle_timeout, on_line):
        for chunk in response:
            line_resets_idle_timeout(chunk)
            if on_line:
                on_line(chunk)
            yield chunk

    frames = iter_upstream_sse_events(
        [b'event: response.completed\ndata: {"type":"response.completed"}\n', b'\n'],
        read_lines=read_lines, event_resets_idle_timeout=lambda event: True,
    )
    assert output.headers(200, "official")
    assert output.headers(200, "official")
    for frame in frames:
        assert output.write(frame.raw)
    assert commit.terminal_committed
    before = writer.wfile.getvalue()
    output.data({"late": True})
    output.done()
    assert writer.wfile.getvalue() == before
    assert writer.responses == [200]


def test_relay_incomplete_frame_fails_after_delivering_complete_event():
    import pytest
    from gateway_relay import iter_upstream_sse_events
    from protocol_translation import UpstreamStreamIncompleteError

    def read_lines(response, *, line_resets_idle_timeout, on_line):
        for chunk in response:
            line_resets_idle_timeout(chunk)
            yield chunk

    events = iter_upstream_sse_events(
        [b'data: {"ok":true}\n\ndata: unfinished'],
        read_lines=read_lines, event_resets_idle_timeout=lambda event: True,
    )
    assert next(events).data == b'{"ok":true}'
    with pytest.raises(UpstreamStreamIncompleteError):
        next(events)


def test_relay_complete_event_precedes_frame_size_failure():
    import pytest
    from gateway_relay import iter_upstream_sse_events
    from sse_events import DEFAULT_MAX_FRAME_BYTES, SseFrameTooLargeError

    def read_lines(response, *, line_resets_idle_timeout, on_line):
        for chunk in response:
            line_resets_idle_timeout(chunk)
            yield chunk

    events = iter_upstream_sse_events(
        [b'data: ok\n\n' + b'x' * (DEFAULT_MAX_FRAME_BYTES + 1)],
        read_lines=read_lines, event_resets_idle_timeout=lambda event: True,
    )
    assert next(events).data == b'ok'
    with pytest.raises(SseFrameTooLargeError):
        next(events)


def test_cancelled_relay_output_closes_upstream_and_rejects_more_bytes():
    from gateway_relay import RelaySseOutput
    from gateway_exchange_bindings import bind_downstream_stream_commit

    closed = []
    upstream = SimpleNamespace(close=lambda: closed.append(True))
    writer = Writer()
    commit = bind_downstream_stream_commit(writer, upstream, "official")
    output = RelaySseOutput(writer, commit)
    assert output.headers(200, "official")
    commit.cancel()
    assert not output.write(b'data: late\n\n')
    assert writer.close_connection
    assert closed == [True]
    assert writer.wfile.getvalue() == b''
