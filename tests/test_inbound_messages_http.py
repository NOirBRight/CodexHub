"""Public HTTP dispatch for inbound Anthropic Messages."""

from __future__ import annotations

import json

import pytest

from tests.gateway_harness import GATEWAY_CLIENT_KEY, GatewayHarness, parsed_sse_events, request_gateway
from tests.gateway_harness.sse import event_payload


def _auth_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {GATEWAY_CLIENT_KEY}",
        "Content-Type": "application/json",
        "Connection": "close",
    }


def _messages_body() -> bytes:
    return json.dumps(
        {
            "model": "volc/glm-5.2",
            "max_tokens": 32,
            "messages": [{"role": "user", "content": "hello"}],
        }
    ).encode()


@pytest.fixture
def harness() -> GatewayHarness:
    with GatewayHarness() as running:
        yield running


def test_messages_query_variant_is_not_404(harness: GatewayHarness) -> None:
    response = request_gateway(
        harness.host,
        harness.port,
        "POST",
        "/v1/messages?beta=true",
        body=_messages_body(),
        headers=_auth_headers(),
        timeout=8.0,
    )
    assert response.status != 404


def test_count_tokens_returns_best_effort_estimate(harness: GatewayHarness) -> None:
    response = request_gateway(
        harness.host,
        harness.port,
        "POST",
        "/v1/messages/count_tokens",
        body=_messages_body(),
        headers=_auth_headers(),
        timeout=8.0,
    )
    assert response.status == 200
    payload = json.loads(response.body)
    assert payload["type"] == "message_count_tokens"
    assert payload["input_tokens"] >= 1
    assert harness.stub is not None
    assert harness.stub.captures == []


def test_count_tokens_requires_gateway_auth(harness: GatewayHarness) -> None:
    response = request_gateway(
        harness.host,
        harness.port,
        "POST",
        "/v1/messages/count_tokens",
        body=_messages_body(),
        headers={"Content-Type": "application/json", "Connection": "close"},
        timeout=8.0,
    )
    assert response.status == 401


def test_estimate_input_tokens_counts_cjk_near_one_per_char() -> None:
    import anthropic_messages

    latin = anthropic_messages.estimate_input_tokens({"messages": [{"content": "abcd"}]})
    cjk = anthropic_messages.estimate_input_tokens({"messages": [{"content": "你好"}]})
    assert latin == 1
    assert cjk == 2


def test_messages_to_chat_upstream_converts_request(harness: GatewayHarness) -> None:
    harness.set_json_response(
        {
            "id": "chat_char_1",
            "object": "chat.completion",
            "model": "glm-5.2",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "hello-chat"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        }
    )
    response = request_gateway(
        harness.host,
        harness.port,
        "POST",
        "/v1/messages",
        body=_messages_body(),
        headers=_auth_headers(),
        timeout=8.0,
    )
    assert harness.stub is not None
    assert harness.stub.captures
    captured = harness.stub.captures[0]
    assert captured.path.endswith("/chat/completions")
    sent = json.loads(captured.body)
    assert "messages" in sent
    assert sent["messages"][0]["role"] == "user"
    assert response.status == 200
    payload = json.loads(response.body)
    assert payload["type"] == "message"
    assert payload["role"] == "assistant"
    text = "".join(
        block.get("text", "")
        for block in payload.get("content", [])
        if isinstance(block, dict) and block.get("type") == "text"
    )
    assert "hello-chat" in text


def test_messages_to_chat_stream_returns_anthropic_sse(harness: GatewayHarness) -> None:
    first = {
        "id": "chatcmpl_char_stream",
        "object": "chat.completion.chunk",
        "model": "glm-5.2",
        "choices": [{"index": 0, "delta": {"role": "assistant", "content": "hello"}, "finish_reason": None}],
    }
    last = {
        "id": "chatcmpl_char_stream",
        "object": "chat.completion.chunk",
        "model": "glm-5.2",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    harness.set_sse_response(
        (
            f"data: {json.dumps(first)}\n\n".encode(),
            f"data: {json.dumps(last)}\n\n".encode(),
            b"data: [DONE]\n\n",
        )
    )
    body = json.dumps(
        {
            "model": "volc/glm-5.2",
            "max_tokens": 32,
            "stream": True,
            "messages": [{"role": "user", "content": "hello"}],
        }
    ).encode()
    response = request_gateway(
        harness.host,
        harness.port,
        "POST",
        "/v1/messages",
        body=body,
        headers=_auth_headers(),
        timeout=8.0,
    )
    assert response.status == 200
    events = parsed_sse_events(response.body)
    payloads = [payload for payload in (event_payload(event) for event in events) if payload]
    names = [payload.get("type") for payload in payloads]
    assert "message_start" in names, (names, response.body[:500])
    assert "content_block_delta" in names
    assert "message_stop" in names
    text = "".join(
        str(payload.get("delta", {}).get("text", ""))
        for payload in payloads
        if payload.get("type") == "content_block_delta"
    )
    assert "hello" in text


def test_prepare_exchange_keeps_named_adaptations() -> None:
    from protocol_translation import prepare_exchange

    body = json.dumps(
        {
            "model": "volc/glm-5.2",
            "max_tokens": 32,
            "metadata": {"user_id": "synthetic"},
            "messages": [{"role": "user", "content": "hello"}],
        }
    ).encode()
    prepared = prepare_exchange(
        body,
        inbound_format="anthropic_messages",
        outbound_format="chat_completions",
    )
    assert prepared.adaptations
    assert all(len(item) == 3 for item in prepared.adaptations)
    assert "synthetic" not in repr(prepared.adaptations)


@pytest.mark.parametrize("outbound", ["chat_completions", "responses"])
def test_model_switch_preserves_image_in_tool_result(outbound: str) -> None:
    from protocol_translation import prepare_exchange

    body = json.dumps({
        "model": "volc/glm-5.2",
        "max_tokens": 32,
        "messages": [
            {"role": "user", "content": "inspect"},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "call_1", "name": "read", "input": {}},
            ]},
            {"role": "user", "content": [{
                "type": "tool_result", "tool_use_id": "call_1", "content": [
                    {"type": "text", "text": "screenshot"},
                    {"type": "image", "source": {
                        "type": "base64", "media_type": "image/png", "data": "AA==",
                    }},
                ],
            }]},
        ],
    }).encode()
    prepared = prepare_exchange(body, inbound_format="anthropic_messages", outbound_format=outbound)
    payload = json.loads(prepared.upstream_body)
    if outbound == "chat_completions":
        tool, image = payload["messages"][-2:]
        assert tool == {"role": "tool", "tool_call_id": "call_1", "content": "screenshot"}
        assert image["content"][1] == {
            "type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="},
        }
    else:
        tool, image = payload["input"][-2:]
        assert tool == {"type": "function_call_output", "call_id": "call_1", "output": "screenshot"}
        assert image["content"][1] == {"type": "input_image", "image_url": "data:image/png;base64,AA=="}
    assert image["role"] == "user"
    assert "call_1" in image["content"][0]["text"]
    assert any(policy == "tool_result_image_lifted_to_user_message" for _, policy, _ in prepared.adaptations)


@pytest.mark.parametrize("outbound", ["chat_completions", "responses"])
def test_parallel_tool_results_keep_replies_before_lifted_images(outbound: str) -> None:
    from protocol_translation import prepare_exchange

    image = {"type": "image", "source": {"type": "url", "url": "https://example.test/image.png"}}
    body = json.dumps({
        "model": "volc/glm-5.2", "max_tokens": 32,
        "messages": [
            {"role": "user", "content": "inspect"},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "call_1", "name": "read", "input": {}},
                {"type": "tool_use", "id": "call_2", "name": "read", "input": {}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "call_1", "content": [image, image]},
                {"type": "tool_result", "tool_use_id": "call_2", "content": [image]},
            ]},
        ],
    }).encode()
    prepared = prepare_exchange(body, inbound_format="anthropic_messages", outbound_format=outbound)
    payload = json.loads(prepared.upstream_body)
    items = payload["messages"] if outbound == "chat_completions" else payload["input"]
    tail = items[-5:]
    id_field = "tool_call_id" if outbound == "chat_completions" else "call_id"
    assert [item[id_field] for item in tail[:2]] == ["call_1", "call_2"]
    assert [item["content"][0]["text"] for item in tail[2:]] == [
        "Tool result image 1/2 from call_id=call_1.",
        "Tool result image 2/2 from call_id=call_1.",
        "Tool result image 1/1 from call_id=call_2.",
    ]


def test_chat_conversion_refusal_names_unmodelled_fields() -> None:
    from protocol_translation import NonForwardable, prepare_exchange

    body = json.dumps(
        {
            "model": "volc/glm-5.2",
            "max_tokens": 8,
            "mcp_servers": [],
            "messages": [{"role": "user", "content": "hi"}],
        }
    ).encode()
    try:
        prepare_exchange(
            body,
            inbound_format="anthropic_messages",
            outbound_format="chat_completions",
        )
    except NonForwardable as error:
        assert "mcp_servers" in str(error)
        return
    raise AssertionError("expected NonForwardable")


def test_rewritten_messages_headers_drop_upstream_zstd() -> None:
    import gateway_request

    headers = gateway_request.filtered_response_headers(
        {"Content-Type": "application/json", "Content-Encoding": "zstd", "Content-Length": "9"},
        False,
        content_length=2,
        content_type="application/json",
        content_encoding=None,
    )
    lowered = {key.lower(): value for key, value in headers}
    assert "content-encoding" not in lowered
    assert lowered["content-length"] == "2"


def test_tool_result_is_error_prefixes_chat_content() -> None:
    from anthropic_messages_ir import Adapted, prepare_upstream_request

    body = json.dumps(
        {
            "model": "volc/glm-5.2",
            "max_tokens": 8,
            "messages": [
                {"role": "user", "content": "run"},
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "call_1",
                            "name": "Task",
                            "input": {"prompt": "ok"},
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "call_1",
                            "is_error": True,
                            "content": "Explore failed",
                        }
                    ],
                },
            ],
        }
    ).encode()
    prepared = prepare_upstream_request(body, "chat_completions")
    assert isinstance(prepared, Adapted)
    payload = json.loads(prepared.body)
    tool_messages = [m for m in payload["messages"] if m.get("role") == "tool"]
    assert tool_messages
    assert tool_messages[0]["content"].startswith("[tool_error]")
    assert any(a.policy == "tool_error_prefixed_in_chat_content" for a in prepared.adaptations)


@pytest.mark.parametrize("protocol", ["responses", "chat_completions"])
def test_converted_messages_stream_persists_upstream_usage(protocol, tmp_path, monkeypatch):
    import sqlite3
    import gateway_catalog_runtime
    import gateway_events
    from proxy_telemetry import prepare_event_payload, write_event_to_sqlite

    database = tmp_path / "telemetry.sqlite"

    def record(event, **fields):
        write_event_to_sqlite(database, prepare_event_payload(event, fields, tmp_path))

    monkeypatch.setattr(gateway_events, "write_proxy_event", record)
    usage = {"input_tokens": 7, "output_tokens": 3, "total_tokens": 10}
    if protocol == "responses":
        output = [{"type": "message", "id": "msg_fixture", "role": "assistant", "status": "completed",
                   "content": [{"type": "output_text", "text": "hello", "annotations": []}]}]
        payloads = [
            {"type": "response.created", "response": {"id": "resp_fixture", "model": "gpt-5.5", "output": []}},
            {"type": "response.output_text.delta", "item_id": "msg_fixture", "output_index": 0, "content_index": 0, "delta": "hello"},
            {"type": "response.completed", "response": {"id": "resp_fixture", "model": "gpt-5.5", "status": "completed", "output": output, "usage": usage}},
        ]
    else:
        payloads = [
            {"id": "chat_fixture", "model": "glm-5.2", "choices": [{"index": 0, "delta": {"role": "assistant", "content": "hello"}, "finish_reason": None}]},
            {"id": "chat_fixture", "model": "glm-5.2", "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10}},
        ]
    frames = [f"data: {json.dumps(p)}\n\n".encode() for p in payloads]
    if protocol == "chat_completions":
        frames.append(b"data: [DONE]\n\n")
    with GatewayHarness() as running, monkeypatch.context() as scoped:
        running.set_sse_response(tuple(frames))
        upstream = {
            "name": "fixture", "provider_id": "fixture", "model_id": "fixture/model",
            "base_url": running.stub_base_url, "auth": "api_key", "api_key": "synthetic",
            "upstream_model": "model", "upstream_format": protocol, "tool_protocol": "auto",
        }
        scoped.setattr(gateway_catalog_runtime, "choose_upstream", lambda *a, **kw: upstream)
        body = json.loads(_messages_body()); body["stream"] = True
        response = request_gateway(running.host, running.port, "POST", "/v1/messages",
                                   body=json.dumps(body).encode(), headers=_auth_headers(), timeout=8.0)
        assert response.status == 200
        assert b"message_stop" in response.body
    with sqlite3.connect(database) as connection:
        rows = connection.execute("SELECT path,usage_source,usage_input_tokens,usage_output_tokens,usage_total_tokens FROM gateway_requests WHERE status=200").fetchall()
    assert rows == [("/v1/messages", "upstream", 7, 3, 10)]
