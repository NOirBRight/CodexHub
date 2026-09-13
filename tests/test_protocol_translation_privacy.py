"""Untrusted discriminator values must not become Gateway error details."""
import json
import pytest

import gateway_errors
from protocol_translation import (
    UnsupportedProtocolTranslationError,
    prepare_exchange,
    chat_completion_to_response_body,
    response_body_to_response_sse_events,
)

SENTINEL = "PRIVATE_PAYLOAD_SENTINEL_do_not_disclose"


@pytest.mark.parametrize("inbound,payload", [
    ("responses", {"model": "fixture", "input": [{"role": "user", "content": [{"type": SENTINEL}]}]}),
    ("responses", {"model": "fixture", "input": [{"type": SENTINEL}]}),
    ("responses", {"model": "fixture", "input": [], SENTINEL: "private"}),
    ("responses", {"model": "fixture", "input": [], "tools": [{"type": SENTINEL}]}),
    ("responses", {"model": "fixture", "input": [], "tool_choice": {"type": SENTINEL}}),
    ("chat_completions", {"model": "fixture", "messages": [{"role": SENTINEL, "content": "text"}]}),
    ("chat_completions", {"model": "fixture", "messages": [{"role": "user", "content": "text", SENTINEL: True}]}),
    ("chat_completions", {"model": "fixture", "messages": [], "tools": [{"type": SENTINEL}]}),
])
def test_request_translation_error_does_not_echo_discriminators(inbound, payload):
    with pytest.raises(UnsupportedProtocolTranslationError) as caught:
        prepare_exchange(json.dumps(payload).encode(), inbound_format=inbound,
                         outbound_format="responses" if inbound == "chat_completions" else "chat_completions")
    _assert_error_private(caught.value)


def _assert_error_private(error):
    wrapped = gateway_errors.UpstreamProtocolTranslationError(error)
    response = gateway_errors.downstream_json_error_payload(gateway_errors.DownstreamErrorSpec(
        inbound_format="responses", upstream_name="fixture", status=400,
        exc=wrapped, error="unsupported_protocol_semantics",
    ))
    assert SENTINEL not in str(error)
    assert SENTINEL not in str(wrapped)
    assert SENTINEL not in json.dumps(response)


@pytest.mark.parametrize("field", ["finish_reason", "index"])
def test_response_translation_error_does_not_echo_discriminators(field):
    choice = {"index": 0, "message": {"role": "assistant", "content": "text"}, "finish_reason": "stop"}
    choice[field] = SENTINEL
    with pytest.raises(UnsupportedProtocolTranslationError) as caught:
        chat_completion_to_response_body(json.dumps({"choices": [choice]}).encode())
    _assert_error_private(caught.value)


def test_sse_synthesis_error_does_not_echo_status():
    with pytest.raises(UnsupportedProtocolTranslationError) as caught:
        response_body_to_response_sse_events(json.dumps({"status": SENTINEL, "output": []}).encode())
    _assert_error_private(caught.value)


def test_multimodal_tool_result_error_does_not_echo_part_type():
    from multimodal_tool_result import ToolResultMediaPolicy, adapt_tool_result_item

    policy = ToolResultMediaPolicy(target_format="chat_completions")
    with pytest.raises(UnsupportedProtocolTranslationError) as caught:
        adapt_tool_result_item({"type": "function_call_output", "call_id": "fixture",
            "output": [{"type": SENTINEL}]}, policy=policy)
    _assert_error_private(caught.value)


@pytest.mark.parametrize("payload", [
    {"model": "fixture", "input": [{"type": SENTINEL}]},
    {"model": "fixture", SENTINEL: "private"},
    {"model": "fixture", "messages": [{"role": SENTINEL, "content": "private"}]},
])
def test_request_shape_diagnostics_do_not_echo_unknown_keys_types_or_roles(tmp_path, payload):
    from proxy_telemetry import enrich_request_observability, prepare_event_payload

    fields = enrich_request_observability(codex_home=tmp_path, body=json.dumps(payload).encode())
    event = prepare_event_payload("request_start", fields, tmp_path)
    assert SENTINEL not in json.dumps(event)


def test_safe_request_shape_categories_remain_observable(tmp_path):
    from proxy_telemetry import enrich_request_observability, prepare_event_payload

    payload = {"model": "fixture", "messages": [
        {"role": "assistant", "reasoning_content": "private", "tool_calls": []},
        {"role": SENTINEL, "content": "private"},
    ], SENTINEL: "private"}
    fields = enrich_request_observability(codex_home=tmp_path, body=json.dumps(payload).encode())
    shape = prepare_event_payload("request_start", fields, tmp_path)["body_shape"]
    assert shape["top_level_keys"] == ["messages", "model"]
    assert shape["unknown_top_level_key_count"] == 1
    assert shape["message_roles"] == ["assistant", "unknown"]
    assert shape["assistant_reasoning_content_count"] == 1


def test_websocket_metadata_does_not_echo_unknown_wire_names():
    from types import SimpleNamespace
    from gateway_request import websocket_probe_frame_metadata
    from websocket_transport import redacted_handshake_metadata

    frame = SimpleNamespace(opcode=1, fin=True, payload=json.dumps({SENTINEL: "private", "type": "response.create"}).encode())
    assert SENTINEL not in json.dumps(websocket_probe_frame_metadata(frame))
    metadata = redacted_handshake_metadata('/'+SENTINEL+'?'+SENTINEL+'=private',
        {SENTINEL: "private", "Sec-WebSocket-Protocol": SENTINEL})
    assert SENTINEL not in json.dumps(metadata)


def test_invalid_verified_sse_does_not_log_wire_discriminators(caplog):
    import logging
    from gateway_stream_semantics import UpstreamSseSemanticError, validate_verified_converted_sse_payload

    with caplog.at_level(logging.WARNING), pytest.raises(UpstreamSseSemanticError):
        validate_verified_converted_sse_payload(
            {"type": SENTINEL, SENTINEL: "private", "choices": "invalid"}, "chat_completions")
    assert caplog.records
    assert SENTINEL not in caplog.text


def test_http_access_log_does_not_record_request_path_or_query(caplog):
    import logging
    import threading
    from http.client import HTTPConnection
    from http.server import ThreadingHTTPServer
    from codex_proxy import CodexProxyHandler

    server = ThreadingHTTPServer(("127.0.0.1", 0), CodexProxyHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with caplog.at_level(logging.INFO):
            client = HTTPConnection(*server.server_address, timeout=5)
            try:
                client.request("GET", "/" + SENTINEL + "?" + SENTINEL + "=private")
                response = client.getresponse()
                assert response.status == 404
                response.read()
            finally:
                client.close()
        assert caplog.records
        assert SENTINEL not in caplog.text
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_provider_sse_error_details_are_not_persisted_as_diagnostics(tmp_path):
    from gateway_stream_semantics import UpstreamStreamErrorEvent
    from proxy_telemetry import prepare_event_payload

    error = UpstreamStreamErrorEvent({"type": "error", "error": {"message": SENTINEL}})
    # Keep the provider's error available to its caller; telemetry is a separate boundary.
    assert SENTINEL in str(error)
    detail = gateway_errors.safe_upstream_error_detail(error)
    event = prepare_event_payload("upstream_stream_error_event", {"detail": detail}, tmp_path)
    assert SENTINEL not in json.dumps(event)


@pytest.mark.parametrize("path,expected", [("/v1/responses?" + SENTINEL, "/v1/responses"), ("/" + SENTINEL, "unknown")])
def test_request_path_diagnostics_are_private_in_sqlite(tmp_path, path, expected):
    import sqlite3
    from proxy_telemetry import prepare_event_payload, write_event_to_sqlite

    event = prepare_event_payload("request_start", {"request_id": "fixture", "path": path}, tmp_path)
    assert event["path"] == expected
    assert SENTINEL not in json.dumps(event)
    database = tmp_path / "telemetry.sqlite"
    write_event_to_sqlite(database, event)
    with sqlite3.connect(database) as connection:
        rows = connection.execute("SELECT payload_json FROM gateway_events").fetchall()
    assert rows and SENTINEL not in json.dumps(rows)
