"""Replay the xAI empty-completed stream through the live Gateway HTTP path."""

from __future__ import annotations

import json

from tests.gateway_harness import (
    GATEWAY_CLIENT_KEY,
    GatewayHarness,
    parsed_sse_events,
    request_gateway,
    require_single_terminal,
)


def _auth_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {GATEWAY_CLIENT_KEY}",
        "Content-Type": "application/json",
        "Connection": "close",
    }


def _sse(payload: dict) -> bytes:
    return ("data: " + json.dumps(payload) + "\n\n").encode()


def _ayaspace_reasoning_only_completed() -> tuple[bytes, ...]:
    created = {
        "type": "response.created",
        "response": {"id": "resp_repro", "object": "response", "status": "in_progress", "output": []},
    }
    summary = {"type": "response.reasoning_summary_text.delta", "delta": "Let me view the screenshot."}
    item = {
        "type": "response.output_item.done",
        "item": {"type": "reasoning", "summary": [{"type": "summary_text", "text": "view screenshot"}]},
    }
    completed = {
        "type": "response.completed",
        "response": {
            "id": "resp_repro",
            "object": "response",
            "status": "completed",
            "output": [{"type": "reasoning", "summary": [{"type": "summary_text", "text": "view screenshot"}]}],
        },
    }
    return (_sse(created), _sse(summary), _sse(item), _sse(completed))


def _raw_reasoning_then_completed() -> tuple[bytes, ...]:
    created = {
        "type": "response.created",
        "response": {"id": "resp_raw", "object": "response", "status": "in_progress", "output": []},
    }
    raw = {"type": "response.reasoning_text.delta", "delta": "hidden chain of thought"}
    completed = {
        "type": "response.completed",
        "response": {"id": "resp_raw", "object": "response", "status": "completed", "output": [{"type": "reasoning", "summary": []}]},
    }
    return (_sse(created), _sse(raw), _sse(completed))


def _post_xai_stream(harness: GatewayHarness, chunks: tuple[bytes, ...]):
    harness.set_sse_response(chunks)
    return request_gateway(
        harness.host,
        harness.port,
        "POST",
        "/v1/responses",
        body=json.dumps({"model": "xai/grok-4.6", "input": "continue", "stream": True}).encode(),
        headers=_auth_headers(),
        timeout=8.0,
    )


def _payload_types(body: bytes) -> list[str]:
    types: list[str] = []
    for event in parsed_sse_events(body):
        if event.data == b"[DONE]":
            types.append("[DONE]")
            continue
        try:
            payload = json.loads(event.data) if event.data else None
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and isinstance(payload.get("type"), str):
            types.append(payload["type"])
        elif event.event:
            types.append(event.event.decode("utf-8", "replace"))
    return types


def test_xai_reasoning_only_completed_forwards_terminal() -> None:
    with GatewayHarness() as harness:
        response = _post_xai_stream(harness, _ayaspace_reasoning_only_completed())
    assert response.status == 200, response.body[:500]
    types = _payload_types(response.body)
    assert "response.completed" in types, types
    require_single_terminal(parsed_sse_events(response.body))


def test_xai_raw_reasoning_drop_still_forwards_completed() -> None:
    with GatewayHarness() as harness:
        response = _post_xai_stream(harness, _raw_reasoning_then_completed())
    assert response.status == 200, response.body[:500]
    types = _payload_types(response.body)
    assert "response.reasoning_text.delta" not in types, types
    assert "response.completed" in types or b"codexhub.keepalive" in response.body, types
    assert b"response.completed" in response.body or b"response.failed" in response.body, response.body[:800]
