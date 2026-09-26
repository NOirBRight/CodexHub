"""Public HTTP contract for Claude subscription coexistence routes."""

from __future__ import annotations

import http.client
import json
import os
import time
from contextlib import contextmanager
from unittest.mock import patch

import gateway_catalog_runtime
import gateway_exchange_bindings
import gateway_events
import gateway_transport
import pytest
from scripts import e2e_claude_live_routes as live_e2e
from tests.gateway_harness import GATEWAY_CLIENT_KEY, GatewayHarness, request_gateway


NATIVE_MODEL = "claude-opus-5-5"
CLAUDE_OAUTH = "synthetic-claude-oauth"


@contextmanager
def _isolated_event_log(codex_home):
    with patch.dict(os.environ, {"CODEX_HOME": str(codex_home)}, clear=False):
        gateway_events.refresh_runtime_paths()
        try:
            yield gateway_events.PROXY_EVENT_LOG_PATH
        finally:
            gateway_events.flush_proxy_event_writer()
    gateway_events.refresh_runtime_paths()


def _request_complete_events(event_log):
    return [
        event
        for line in event_log.read_text(encoding="utf-8").splitlines()
        if line.strip()
        if (event := json.loads(line)).get("event") == "request_complete"
    ]


def _native_upstream(base_url: str) -> dict[str, object]:
    return {
        "name": "anthropic_native",
        "provider_id": "anthropic",
        "model_id": NATIVE_MODEL,
        "base_url": base_url,
        "auth": "anthropic_oauth",
        "upstream_model": NATIVE_MODEL,
        "upstream_format": "anthropic_messages",
        "native_anthropic_subscription": True,
        "tool_protocol": "auto",
        "tool_surface_strategy": "eager",
    }


def _route_native_requests_to_stub(harness: GatewayHarness):
    assert harness.stub is not None
    base_url = f"http://127.0.0.1:{harness.stub.server.server_port}"
    return patch.object(
        gateway_catalog_runtime,
        "choose_upstream",
        return_value=_native_upstream(base_url),
    )


def _native_headers(*, local_key: str | None = GATEWAY_CLIENT_KEY) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {CLAUDE_OAUTH}",
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "oauth-2025-04-20, interleaved-thinking-2025-05-14",
        "content-type": "application/json",
        "connection": "close",
    }
    if local_key is not None:
        headers["x-codexhub-gateway-key"] = local_key
    return headers


def test_native_message_keeps_oauth_separate_and_preserves_wire_contract() -> None:
    body = (
        b'{"model":"claude-opus-5-5","max_tokens":64,"messages":'
        b'[{"role":"user","content":"hello"}],"thinking":'
        b'{"type":"enabled","budget_tokens":4096}}'
    )
    upstream_body = b'{"type":"message","model":"claude-opus-5-5","usage":{"input_tokens":2}}'

    with GatewayHarness() as harness:
        assert harness.stub is not None
        stub = harness.stub
        harness.set_json_response(upstream_body)
        with _route_native_requests_to_stub(harness):
            response = request_gateway(
                harness.host,
                harness.port,
                "POST",
                "/v1/messages?beta=true",
                body=body,
                headers={
                    "Authorization": f"Bearer {CLAUDE_OAUTH}",
                    "x-codexhub-gateway-key": GATEWAY_CLIENT_KEY,
                    "x-api-key": "untrusted-caller-api-key",
                    "anthropic-version": "2023-06-01",
                    "anthropic-beta": "oauth-2025-04-20, interleaved-thinking-2025-05-14",
                    "content-type": "application/json",
                    "connection": "close",
                },
                timeout=8.0,
            )

    assert response.status == 200
    assert response.body == upstream_body
    assert len(stub.captures) == 1
    captured = stub.captures[0]
    assert captured.path == "/v1/messages?beta=true"
    assert captured.body == body
    assert captured.headers["authorization"] == f"Bearer {CLAUDE_OAUTH}"
    assert "x-api-key" not in captured.headers
    assert "x-codexhub-gateway-key" not in captured.headers
    assert captured.headers["anthropic-version"] == "2023-06-01"
    assert captured.headers["anthropic-beta"] == "oauth-2025-04-20, interleaved-thinking-2025-05-14"
    assert json.loads(captured.body)["model"] == NATIVE_MODEL


def test_native_json_usage_is_recorded_once_with_cache_input_breakdown(tmp_path) -> None:
    upstream_body = json.dumps(
        {
            "type": "message",
            "model": NATIVE_MODEL,
            "content": [],
            "usage": {
                "input_tokens": 7,
                "cache_read_input_tokens": 3,
                "cache_creation_input_tokens": 2,
                "output_tokens": 5,
            },
        }
    ).encode()
    with _isolated_event_log(tmp_path / "codex") as event_log:
        with GatewayHarness() as harness:
            harness.set_json_response(upstream_body)
            with _route_native_requests_to_stub(harness):
                response = request_gateway(
                    harness.host,
                    harness.port,
                    "POST",
                    "/v1/messages",
                    body=json.dumps(
                        {
                            "model": NATIVE_MODEL,
                            "max_tokens": 16,
                            "messages": [{"role": "user", "content": "hello"}],
                        }
                    ).encode(),
                    headers=_native_headers(),
                    timeout=8.0,
                )

    assert response.status == 200
    assert response.body == upstream_body
    complete = _request_complete_events(event_log)
    assert len(complete) == 1
    assert complete[0]["provider_id"] == "claude_subscription"
    assert complete[0]["model"] == NATIVE_MODEL
    assert complete[0]["usage_source"] == "upstream", complete[0]
    assert complete[0]["usage_input_tokens"] == 12, complete[0]
    assert complete[0]["usage_cached_input_tokens"] == 3
    assert complete[0]["usage_cache_write_input_tokens"] == 2
    assert complete[0]["usage_output_tokens"] == 5
    assert complete[0]["usage_total_tokens"] == 17


def test_native_sse_usage_merges_snapshots_without_repeated_token_sums(tmp_path) -> None:
    frames = (
        b'event: message_start\ndata: {"type":"message_start","message":{"type":"message","model":"claude-opus-5-5","usage":{"input_tokens":6,"cache_read_input_tokens":4,"cache_creation_input_tokens":2,"output_tokens":0}}}\n\n'
        b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"cache_creation_input_tokens":2,"output_tokens":3}}\n\n'
        b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
    )
    with _isolated_event_log(tmp_path / "codex") as event_log:
        with GatewayHarness() as harness:
            harness.set_sse_response(tuple(frames.splitlines(keepends=True)))
            with _route_native_requests_to_stub(harness):
                response = request_gateway(
                    harness.host,
                    harness.port,
                    "POST",
                    "/v1/messages",
                    body=json.dumps(
                        {
                            "model": NATIVE_MODEL,
                            "max_tokens": 16,
                            "stream": True,
                            "messages": [{"role": "user", "content": "hello"}],
                        }
                    ).encode(),
                    headers=_native_headers(),
                    timeout=8.0,
                )

    assert response.status == 200
    assert response.body == frames
    complete = _request_complete_events(event_log)
    assert len(complete) == 1
    assert complete[0].get("usage_input_tokens") == 12, complete[0]
    assert complete[0]["usage_cached_input_tokens"] == 4
    assert complete[0]["usage_cache_write_input_tokens"] == 2
    assert complete[0]["usage_output_tokens"] == 3
    assert complete[0]["usage_total_tokens"] == 15


def test_native_incomplete_sse_preserves_partial_usage_and_marks_failure(tmp_path, monkeypatch) -> None:
    frames = (
        b'event: message_start\ndata: {"type":"message_start","message":{"type":"message","model":"claude-opus-5-5","usage":{"input_tokens":6,"cache_read_input_tokens":4,"cache_creation_input_tokens":2,"output_tokens":0}}}\n\n'
        b'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
    )
    upstream_attempt_count = 0
    with _isolated_event_log(tmp_path / "codex") as event_log:
        with GatewayHarness() as harness:
            assert harness.stub is not None
            harness.set_sse_response(tuple(frames.splitlines(keepends=True)))
            with _route_native_requests_to_stub(harness):
                response = request_gateway(
                    harness.host,
                    harness.port,
                    "POST",
                    "/v1/messages",
                    body=json.dumps(
                        {
                            "model": NATIVE_MODEL,
                            "max_tokens": 16,
                            "stream": True,
                            "messages": [{"role": "user", "content": "hello"}],
                        }
                    ).encode(),
                    headers=_native_headers(),
                    timeout=8.0,
                )
            upstream_attempt_count = len(harness.stub.captures)

    assert response.status == 200
    assert response.body == frames
    assert upstream_attempt_count == 1, "native partial usage must not replay the upstream request"
    complete = _request_complete_events(event_log)
    assert len(complete) == 1
    assert complete[0]["status"] == 502
    assert complete[0]["usage_source"] == "partial"
    assert complete[0]["usage_missing_reason"] == "stream_incomplete"
    assert complete[0]["usage_input_tokens"] == 12
    assert complete[0]["usage_cached_input_tokens"] == 4
    assert complete[0]["usage_cache_write_input_tokens"] == 2
    assert "usage_output_tokens" not in complete[0]
    records = [json.loads(line) for line in event_log.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert not any(
        event.get("event") in {"upstream_retry", "upstream_retry_suppressed"}
        and event.get("request_id") == complete[0]["request_id"]
        for event in records
    ), "native partial usage must remain one downstream request without retry accounting"

    public_events = [
        {
            field: record[field]
            for field in (
                "event", "request_id", "model", "upstream", "inbound_format",
                "upstream_format", "status", "error", "failure_class", "detail",
            )
            if field in record
        } | {"category": "proxy"}
        for record in records
    ]
    monkeypatch.setattr(live_e2e, "events", lambda _port: public_events)
    diagnostics = live_e2e.gateway_route_diagnostics(
        12345,
        model=NATIVE_MODEL,
        inbound="anthropic_messages",
        outbound="anthropic_messages",
    )
    incomplete = next(row for row in diagnostics if row.get("failure_class") == "upstream_stream_incomplete")
    assert incomplete["event"] == "transparent_stream_closed"
    assert incomplete["http_status"] == 502
    assert incomplete["error_class"] == "UpstreamStreamIncompleteError"
    assert "upstream_http_status" not in incomplete
    assert "detail" not in incomplete
    assert "hello" not in json.dumps(diagnostics)
    assert b"hello" not in event_log.read_bytes()


def test_native_sse_without_message_stop_marks_known_usage_incomplete(tmp_path) -> None:
    frames = (
        b'event: message_start\ndata: {"type":"message_start","message":{"type":"message","model":"claude-opus-5-5","usage":{"input_tokens":6,"cache_read_input_tokens":4,"cache_creation_input_tokens":2,"output_tokens":0}}}\n\n'
        b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"input_tokens":6,"cache_read_input_tokens":4,"cache_creation_input_tokens":2,"output_tokens":3}}\n\n'
    )
    with _isolated_event_log(tmp_path / "codex") as event_log:
        with GatewayHarness() as harness:
            assert harness.stub is not None
            harness.set_sse_response(tuple(frames.splitlines(keepends=True)))
            with _route_native_requests_to_stub(harness):
                response = request_gateway(
                    harness.host,
                    harness.port,
                    "POST",
                    "/v1/messages",
                    body=json.dumps(
                        {
                            "model": NATIVE_MODEL,
                            "max_tokens": 16,
                            "stream": True,
                            "messages": [{"role": "user", "content": "hello"}],
                        }
                    ).encode(),
                    headers=_native_headers(),
                    timeout=8.0,
                )
            assert len(harness.stub.captures) == 1

    assert response.status == 200
    complete = _request_complete_events(event_log)
    assert len(complete) == 1
    assert complete[0]["status"] == 502
    assert complete[0]["usage_source"] == "partial"
    assert complete[0]["usage_missing_reason"] == "stream_incomplete"
    assert complete[0]["usage_input_tokens"] == 12
    assert complete[0]["usage_cached_input_tokens"] == 4
    assert complete[0]["usage_cache_write_input_tokens"] == 2
    assert complete[0]["usage_output_tokens"] == 3


def test_native_sse_read_failure_preserves_known_usage_as_partial(tmp_path) -> None:
    frames = (
        b'event: message_start\ndata: {"type":"message_start","message":{"type":"message","model":"claude-opus-5-5","usage":{"input_tokens":6,"cache_read_input_tokens":4,"cache_creation_input_tokens":2,"output_tokens":0}}}\n\n'
        b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"input_tokens":6,"cache_read_input_tokens":4,"cache_creation_input_tokens":2,"output_tokens":3}}\n\n'
    )
    original_readline = gateway_transport.UpstreamSseReaderLifecycle.readline

    def interrupt_after_usage(reader):
        if getattr(reader, "_interrupt_after_usage", False):
            raise OSError("synthetic upstream read failure")
        line = original_readline(reader)
        if line.startswith(b"data:") and b'"type":"message_delta"' in line:
            reader._interrupt_after_usage = True
        return line

    with _isolated_event_log(tmp_path / "codex") as event_log:
        with GatewayHarness() as harness:
            assert harness.stub is not None
            harness.set_sse_response(tuple(frames.splitlines(keepends=True)))
            with _route_native_requests_to_stub(harness):
                with patch.object(
                    gateway_transport.UpstreamSseReaderLifecycle,
                    "readline",
                    interrupt_after_usage,
                ):
                    response = request_gateway(
                        harness.host,
                        harness.port,
                        "POST",
                        "/v1/messages",
                        body=json.dumps(
                            {
                                "model": NATIVE_MODEL,
                                "max_tokens": 16,
                                "stream": True,
                                "messages": [{"role": "user", "content": "hello"}],
                            }
                        ).encode(),
                        headers=_native_headers(),
                        timeout=8.0,
                    )
            assert len(harness.stub.captures) == 1

    assert response.status == 200
    complete = _request_complete_events(event_log)
    assert len(complete) == 1
    assert complete[0]["status"] == 502
    assert complete[0]["usage_source"] == "partial"
    assert complete[0]["usage_missing_reason"] == "stream_incomplete"
    assert complete[0]["usage_input_tokens"] == 12
    assert complete[0]["usage_cached_input_tokens"] == 4
    assert complete[0]["usage_cache_write_input_tokens"] == 2
    assert complete[0]["usage_output_tokens"] == 3


def test_native_sse_downstream_cancel_preserves_known_usage_as_partial(tmp_path) -> None:
    frames = (
        b'event: message_start\ndata: {"type":"message_start","message":{"type":"message","model":"claude-opus-5-5","usage":{"input_tokens":6,"cache_read_input_tokens":4,"cache_creation_input_tokens":2,"output_tokens":0}}}\n\n'
        b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"input_tokens":6,"cache_read_input_tokens":4,"cache_creation_input_tokens":2,"output_tokens":3}}\n\n'
        b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
    )
    original_write = gateway_exchange_bindings._HandlerDownstreamIO.write

    def disconnect_on_message_delta(downstream, data: bytes) -> None:
        if b'"type":"message_delta"' in data:
            raise OSError("synthetic downstream disconnect")
        original_write(downstream, data)

    with _isolated_event_log(tmp_path / "codex") as event_log:
        with GatewayHarness() as harness:
            assert harness.stub is not None
            harness.set_sse_response(tuple(frames.splitlines(keepends=True)))
            with _route_native_requests_to_stub(harness):
                with patch.object(
                    gateway_exchange_bindings._HandlerDownstreamIO,
                    "write",
                    disconnect_on_message_delta,
                ):
                    response = request_gateway(
                        harness.host,
                        harness.port,
                        "POST",
                        "/v1/messages",
                        body=json.dumps(
                            {
                                "model": NATIVE_MODEL,
                                "max_tokens": 16,
                                "stream": True,
                                "messages": [{"role": "user", "content": "hello"}],
                            }
                        ).encode(),
                        headers=_native_headers(),
                        timeout=8.0,
                    )
            assert len(harness.stub.captures) == 1

    assert response.status == 200
    complete = _request_complete_events(event_log)
    assert len(complete) == 1
    assert complete[0]["status"] == 499
    assert complete[0]["usage_source"] == "partial"
    assert complete[0]["usage_missing_reason"] == "downstream_cancelled"
    assert complete[0]["usage_input_tokens"] == 12
    assert complete[0]["usage_cached_input_tokens"] == 4
    assert complete[0]["usage_cache_write_input_tokens"] == 2
    assert complete[0]["usage_output_tokens"] == 3


def test_native_sse_error_keeps_partial_usage_and_marks_failure(tmp_path) -> None:
    frames = (
        b'event: message_start\ndata: {"type":"message_start","message":{"type":"message","model":"claude-opus-5-5","usage":{"input_tokens":6,"cache_read_input_tokens":4,"cache_creation_input_tokens":2,"output_tokens":0}}}\n\n'
        b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"input_tokens":6,"cache_read_input_tokens":4,"cache_creation_input_tokens":2,"output_tokens":3}}\n\n'
        b'event: error\ndata: {"type":"error","error":{"type":"overloaded_error","message":"busy"}}\n\n'
    )
    with _isolated_event_log(tmp_path / "codex") as event_log:
        with GatewayHarness() as harness:
            harness.set_sse_response(tuple(frames.splitlines(keepends=True)))
            with _route_native_requests_to_stub(harness):
                response = request_gateway(
                    harness.host,
                    harness.port,
                    "POST",
                    "/v1/messages",
                    body=json.dumps(
                        {
                            "model": NATIVE_MODEL,
                            "max_tokens": 16,
                            "stream": True,
                            "messages": [{"role": "user", "content": "hello"}],
                        }
                    ).encode(),
                    headers=_native_headers(),
                    timeout=8.0,
                )

    assert response.status == 200
    assert response.body == frames
    complete = _request_complete_events(event_log)
    assert len(complete) == 1
    assert complete[0]["status"] == 502
    assert complete[0]["usage_source"] == "partial"
    assert complete[0]["usage_missing_reason"] == "upstream_stream_error"
    assert complete[0]["usage_input_tokens"] == 12
    assert complete[0]["usage_output_tokens"] == 3


@pytest.mark.parametrize("local_key", [None, "wrong-local-key"])
def test_native_message_rejects_missing_or_wrong_local_key_even_with_oauth(
    local_key: str | None,
) -> None:
    with GatewayHarness() as harness:
        assert harness.stub is not None
        stub = harness.stub
        with _route_native_requests_to_stub(harness):
            response = request_gateway(
                harness.host,
                harness.port,
                "POST",
                "/v1/messages",
                body=json.dumps(
                    {
                        "model": NATIVE_MODEL,
                        "max_tokens": 16,
                        "messages": [{
                            "role": "user",
                            "content": "x" * 131_072 if local_key else "hello",
                        }],
                    }
                ).encode(),
                headers=_native_headers(local_key=local_key),
                timeout=8.0,
            )

    assert response.status == 401
    assert stub.captures == []


def test_native_auth_rejection_returns_401_with_unread_request_body() -> None:
    with GatewayHarness() as harness:
        connection = http.client.HTTPConnection(harness.host, harness.port, timeout=8)
        try:
            connection.putrequest("POST", "/v1/messages")
            connection.putheader("Content-Length", "131072")
            connection.putheader("Authorization", f"Bearer {CLAUDE_OAUTH}")
            connection.putheader("x-codexhub-gateway-key", "wrong-local-key")
            connection.endheaders()
            connection.send(b"x" * 1024)
            time.sleep(0.1)
            response = connection.getresponse()
            assert response.status == 401
            assert json.loads(response.read())["error"] == "unauthorized"
        finally:
            connection.close()


def test_external_provider_uses_its_own_credential_and_discards_claude_secrets() -> None:
    with GatewayHarness() as harness:
        assert harness.stub is not None
        stub = harness.stub
        harness.set_json_response(
            {
                "id": "chatcmpl_external",
                "object": "chat.completion",
                "model": "glm-5.2",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "hello"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
            }
        )
        response = request_gateway(
            harness.host,
            harness.port,
            "POST",
            "/v1/messages",
            body=json.dumps(
                {
                    "model": "volc/glm-5.2",
                    "max_tokens": 16,
                    "messages": [{"role": "user", "content": "hello"}],
                }
            ).encode(),
            headers={
                "Authorization": f"Bearer {CLAUDE_OAUTH}",
                "x-codexhub-gateway-key": GATEWAY_CLIENT_KEY,
                "x-api-key": "caller-api-key",
                "anthropic-version": "2023-06-01",
                "anthropic-beta": "oauth-2025-04-20",
                "accept-encoding": "gzip",
                "content-type": "application/json",
                "connection": "close",
            },
            timeout=8.0,
        )

    assert response.status == 200
    assert len(stub.captures) == 1
    captured = stub.captures[0]
    assert captured.headers["authorization"] == "Bearer volc-test-token"
    assert captured.headers["accept-encoding"] == "gzip"
    assert "x-codexhub-gateway-key" not in captured.headers
    assert "x-api-key" not in captured.headers
    assert CLAUDE_OAUTH not in repr(captured.headers)


def test_native_message_stream_is_forwarded_incrementally_without_oauth_key_rewrite() -> None:
    events = (
        b'event: message_start\ndata: {"type":"message_start","message":{"id":"msg_1","type":"message","role":"assistant","model":"claude-opus-5-5","content":[],"stop_reason":null,"stop_sequence":null,"usage":{"input_tokens":2,"output_tokens":0}}}\n\n'
        b'event: content_block_start\ndata: {"type":"content_block_start","index":0,"content_block":{"type":"text","text":""}}\n\n'
        b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hello"}}\n\n'
        b'event: content_block_stop\ndata: {"type":"content_block_stop","index":0}\n\n'
        b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn","stop_sequence":null},"usage":{"output_tokens":1}}\n\n'
        b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
    )
    with GatewayHarness() as harness:
        assert harness.stub is not None
        stub = harness.stub
        harness.set_sse_response(tuple(events.splitlines(keepends=True)))
        with _route_native_requests_to_stub(harness):
            response = request_gateway(
                harness.host,
                harness.port,
                "POST",
                "/v1/messages",
                body=json.dumps(
                    {
                        "model": NATIVE_MODEL,
                        "max_tokens": 16,
                        "stream": True,
                        "messages": [{"role": "user", "content": "hello"}],
                    }
                ).encode(),
                headers=_native_headers(),
                timeout=8.0,
            )

    assert response.status == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.body == events
    assert len(stub.captures) == 1
    assert stub.captures[0].headers["authorization"] == f"Bearer {CLAUDE_OAUTH}"
    assert "x-api-key" not in stub.captures[0].headers


def test_native_gzip_sse_negotiates_identity_before_gateway_parsing(tmp_path) -> None:
    frames = (
        b'event: message_start\ndata: {"type":"message_start","message":{"type":"message","model":"claude-opus-5-5","usage":{"input_tokens":2,"output_tokens":0}}}\n\n'
        b'event: message_delta\ndata: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":1}}\n\n'
        b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
    )
    with _isolated_event_log(tmp_path / "codex") as event_log:
        with GatewayHarness() as harness:
            assert harness.stub is not None
            stub = harness.stub
            harness.set_sse_response(tuple(frames.splitlines(keepends=True)))
            stub.gzip_sse_when_accepted = True
            with _route_native_requests_to_stub(harness):
                response = request_gateway(
                    harness.host,
                    harness.port,
                    "POST",
                    "/v1/messages",
                    body=json.dumps(
                        {
                            "model": NATIVE_MODEL,
                            "max_tokens": 16,
                            "stream": True,
                            "messages": [{"role": "user", "content": "hello"}],
                        }
                    ).encode(),
                    headers=_native_headers() | {"Accept-Encoding": "gzip"},
                    timeout=8.0,
                )

    assert response.status == 200
    complete = _request_complete_events(event_log)
    assert len(complete) == 1
    assert complete[0]["status"] == 200
    assert response.body == frames
    assert len(stub.captures) == 1
    assert stub.captures[0].headers["accept-encoding"] == "identity"
    assert stub.captures[0].headers["anthropic-beta"] == (
        "oauth-2025-04-20, interleaved-thinking-2025-05-14"
    )


def test_native_upstream_error_status_and_body_are_preserved() -> None:
    error_body = b'{"type":"error","error":{"type":"rate_limit_error"}}'
    with GatewayHarness() as harness:
        assert harness.stub is not None
        stub = harness.stub
        harness.set_json_response(error_body, status=429)
        with _route_native_requests_to_stub(harness):
            response = request_gateway(
                harness.host,
                harness.port,
                "POST",
                "/v1/messages",
                body=json.dumps(
                    {
                        "model": NATIVE_MODEL,
                        "max_tokens": 16,
                        "messages": [{"role": "user", "content": "hello"}],
                    }
                ).encode(),
                headers=_native_headers(),
                timeout=8.0,
            )

    assert response.status == 429
    assert response.body == error_body
    assert len(stub.captures) == 1


def test_native_upstream_redirect_is_blocked_without_relaying_location() -> None:
    with GatewayHarness() as harness:
        assert harness.stub is not None
        stub = harness.stub
        target = f"http://127.0.0.1:{stub.server.server_port}/credential-leak"
        harness.set_json_response(
            b"",
            status=302,
            headers={"Location": target},
        )
        with _route_native_requests_to_stub(harness):
            response = request_gateway(
                harness.host,
                harness.port,
                "POST",
                "/v1/messages",
                body=json.dumps(
                    {
                        "model": NATIVE_MODEL,
                        "max_tokens": 16,
                        "messages": [{"role": "user", "content": "hello"}],
                    }
                ).encode(),
                headers=_native_headers(),
                timeout=8.0,
            )

    assert response.status == 502
    assert "location" not in response.headers
    assert len(stub.captures) == 1
    assert stub.captures[0].path == "/v1/messages"


def test_native_model_resolves_to_fixed_anthropic_host_without_catalog_membership() -> None:
    for model_id in ("claude-opus-5-5", "claude-3-7-sonnet-20250219"):
        upstream = gateway_catalog_runtime.choose_upstream(model_id)
        assert upstream["name"] == "anthropic_native"
        assert upstream["provider_id"] == "anthropic"
        assert upstream["base_url"] == "https://api.anthropic.com/v1"
        assert upstream["model_id"] == model_id
        assert upstream["upstream_model"] == model_id
