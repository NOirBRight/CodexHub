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
