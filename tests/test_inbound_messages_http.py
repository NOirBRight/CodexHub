"""Public HTTP dispatch for inbound Anthropic Messages."""

from __future__ import annotations

import json

import pytest

from tests.gateway_harness import GATEWAY_CLIENT_KEY, GatewayHarness, request_gateway


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


def test_count_tokens_is_explicitly_unsupported(harness: GatewayHarness) -> None:
    response = request_gateway(
        harness.host,
        harness.port,
        "POST",
        "/v1/messages/count_tokens",
        body=_messages_body(),
        headers=_auth_headers(),
        timeout=8.0,
    )
    assert response.status == 400
    payload = json.loads(response.body)
    assert payload["type"] == "error"
    assert payload["error"]["type"] == "invalid_request_error"
    assert "count_tokens" in payload["error"]["message"]
    assert harness.stub is not None
    assert harness.stub.captures == []


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
    assert response.status != 404
    assert response.status != 500
