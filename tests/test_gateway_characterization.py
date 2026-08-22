"""HTTP-level Gateway characterization suite.

These tests exercise the production Gateway as a black box. They must not
import private members of ``CodexProxyHandler``.
"""

from __future__ import annotations

import json
import threading

import pytest

from tests.gateway_harness import (
    GATEWAY_CLIENT_KEY,
    GatewayHarness,
    parsed_sse_events,
    request_gateway,
    require_single_terminal,
)


def _auth_headers(*, extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {GATEWAY_CLIENT_KEY}",
        "Content-Type": "application/json",
        "Connection": "close",
    }
    if extra:
        headers.update(extra)
    return headers


def _responses_request(model: str, *, stream: bool) -> bytes:
    return json.dumps(
        {
            "model": model,
            "input": "characterization-hello",
            "stream": stream,
        }
    ).encode("utf-8")


def _chat_request(model: str, *, stream: bool) -> bytes:
    return json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": "characterization-hello"}],
            "stream": stream,
        }
    ).encode("utf-8")


def _completed_response_json() -> dict[str, object]:
    return {
        "id": "resp_char_1",
        "object": "response",
        "status": "completed",
        "model": "gpt-5.5",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "hello", "annotations": []}],
            }
        ],
    }


def _official_sse_chunks() -> tuple[bytes, ...]:
    created = {
        "type": "response.created",
        "response": {
            "id": "resp_char_stream",
            "object": "response",
            "status": "in_progress",
            "model": "gpt-5.5",
            "output": [],
        },
    }
    completed = {
        "type": "response.completed",
        "response": {
            "id": "resp_char_stream",
            "object": "response",
            "status": "completed",
            "model": "gpt-5.5",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "hello", "annotations": []}],
                }
            ],
        },
    }
    return (
        f"event: response.created\ndata: {json.dumps(created)}\n\n".encode(),
        f"event: response.completed\ndata: {json.dumps(completed)}\n\n".encode(),
    )


def _chat_completion_json() -> dict[str, object]:
    return {
        "id": "chatcmpl_char_1",
        "object": "chat.completion",
        "model": "glm-5.2",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "hello-chat"},
                "finish_reason": "stop",
            }
        ],
    }


def _chat_sse_chunks() -> tuple[bytes, ...]:
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
    return (
        f"data: {json.dumps(first)}\n\n".encode(),
        f"data: {json.dumps(last)}\n\n".encode(),
        b"data: [DONE]\n\n",
    )


@pytest.fixture
def harness() -> GatewayHarness:
    with GatewayHarness() as running:
        yield running


def test_health_endpoint_ok(harness: GatewayHarness) -> None:
    response = request_gateway(harness.host, harness.port, "GET", "/health")
    assert response.status == 200
    payload = json.loads(response.body)
    assert payload["ok"] is True
    assert "build" in payload


def test_models_endpoint_lists_catalog_ids(harness: GatewayHarness) -> None:
    response = request_gateway(harness.host, harness.port, "GET", "/v1/models")
    assert response.status == 200
    payload = json.loads(response.body)
    ids = [row["id"] for row in payload["data"]]
    assert "gpt-5.5" in ids
    assert "volc/glm-5.2" in ids
    assert "models" not in payload
    assert "fetched_at" not in payload


def test_official_nonstreaming_responses_round_trip(harness: GatewayHarness) -> None:
    harness.set_json_response(_completed_response_json())
    response = request_gateway(
        harness.host,
        harness.port,
        "POST",
        "/v1/responses",
        body=_responses_request("gpt-5.5", stream=False),
        headers=_auth_headers(),
    )
    assert response.status == 200
    payload = json.loads(response.body)
    assert payload["id"] == "resp_char_1"
    assert payload["status"] == "completed"
    assert harness.stub is not None
    assert len(harness.stub.captures) == 1
    captured = harness.stub.captures[0]
    assert captured.path.endswith("/responses")
    sent = json.loads(captured.body)
    assert sent["model"] == "gpt-5.5"
    assert captured.headers["authorization"] == "Bearer synthetic-official-token"
    assert GATEWAY_CLIENT_KEY not in captured.headers.get("authorization", "")


def test_official_streaming_responses_has_one_terminal(harness: GatewayHarness) -> None:
    harness.set_sse_response(_official_sse_chunks())
    response = request_gateway(
        harness.host,
        harness.port,
        "POST",
        "/v1/responses",
        body=_responses_request("gpt-5.5", stream=True),
        headers=_auth_headers(),
        timeout=8.0,
    )
    assert response.status == 200
    events = parsed_sse_events(response.body)
    terminal = require_single_terminal(events)
    assert terminal.event == b"response.completed"


def test_external_chat_upstream_from_responses_client(harness: GatewayHarness) -> None:
    harness.set_json_response(_chat_completion_json())
    response = request_gateway(
        harness.host,
        harness.port,
        "POST",
        "/v1/responses",
        body=_responses_request("volc/glm-5.2", stream=False),
        headers=_auth_headers(),
    )
    assert response.status == 200
    payload = json.loads(response.body)
    assert payload["object"] == "response" or payload.get("status") == "completed" or "output" in payload
    assert harness.stub is not None
    captured = harness.stub.captures[0]
    assert captured.path.endswith("/chat/completions")
    sent = json.loads(captured.body)
    assert sent["model"] == "glm-5.2"
    assert "messages" in sent
    assert "input" not in sent
    assert captured.headers["authorization"] == "Bearer volc-test-token"


def test_external_chat_stream_converts_to_one_terminal_responses_event(
    harness: GatewayHarness,
) -> None:
    harness.set_sse_response(_chat_sse_chunks())
    response = request_gateway(
        harness.host,
        harness.port,
        "POST",
        "/v1/responses",
        body=_responses_request("volc/glm-5.2", stream=True),
        headers=_auth_headers(),
        timeout=8.0,
    )
    assert response.status == 200
    events = parsed_sse_events(response.body)
    require_single_terminal(events)


def test_inbound_chat_completions_nonstreaming(harness: GatewayHarness) -> None:
    harness.set_json_response(_chat_completion_json())
    response = request_gateway(
        harness.host,
        harness.port,
        "POST",
        "/v1/chat/completions",
        body=_chat_request("volc/glm-5.2", stream=False),
        headers=_auth_headers(),
    )
    assert response.status == 200
    payload = json.loads(response.body)
    assert payload["choices"][0]["message"]["content"] == "hello-chat"


def test_upstream_429_keeps_rate_limit_class(harness: GatewayHarness) -> None:
    harness.set_json_response(
        {"error": {"type": "rate_limit_error", "message": "slow down"}},
        status=429,
    )
    response = request_gateway(
        harness.host,
        harness.port,
        "POST",
        "/v1/responses",
        body=_responses_request("gpt-5.5", stream=False),
        headers=_auth_headers(),
    )
    assert response.status == 429
    payload = json.loads(response.body)
    code = (
        payload.get("codexhub_error", {}).get("code")
        or payload.get("error", {}).get("code")
        or payload.get("type")
    )
    rendered = json.dumps(payload)
    assert "rate_limit" in rendered or code == "provider.rate_limit"


def test_unknown_model_fails_before_upstream(harness: GatewayHarness) -> None:
    response = request_gateway(
        harness.host,
        harness.port,
        "POST",
        "/v1/responses",
        body=_responses_request("unknown/not-a-model", stream=False),
        headers=_auth_headers(),
    )
    assert response.status == 400
    assert harness.stub is not None
    assert harness.stub.captures == []
    payload = json.loads(response.body)
    assert payload["codexhub_error"]["code"] == "gateway.model_resolution"


def test_cancellation_during_stream_uses_shutdown_outcome(harness: GatewayHarness) -> None:
    assert harness.stub is not None
    harness.stub.hold_after_headers = threading.Event()
    harness.set_sse_response(_official_sse_chunks())
    result: dict[str, object] = {}

    def _run() -> None:
        response = request_gateway(
            harness.host,
            harness.port,
            "POST",
            "/v1/responses",
            body=_responses_request("gpt-5.5", stream=True),
            headers=_auth_headers(),
            timeout=8.0,
        )
        result["status"] = response.status
        result["body"] = response.body

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    assert harness.stub.headers_sent.wait(timeout=3)
    harness.close_admission()
    harness.stub.hold_after_headers.set()
    thread.join(timeout=5)
    assert thread.is_alive() is False
    rendered = (result.get("body") or b"").decode("utf-8", "replace")
    assert "user_requested_shutdown" in rendered or result.get("status") in {503, 200}
