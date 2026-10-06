"""Ordinary adapted Chat streaming through the public HTTP boundary."""

from __future__ import annotations

import http.client
import json
import socket
import threading
from unittest.mock import patch

import pytest

import gateway_catalog_runtime
import gateway_settings
import multimodal_tool_result
import subscription_exchange
from subscription_backend_contract import BackendError
from tests.gateway_harness import GATEWAY_CLIENT_KEY, GatewayHarness, parsed_sse_events, request_gateway
from scripts.qualify_cli_subscriptions import sse_events


UPSTREAM = {
    "name": "cursor-subscription",
    "provider_id": "cursor-subscription",
    "model_id": "cursor-subscription/controlled",
    "base_url": "https://cli-subscription.invalid/v1",
    "auth": "official_cli_session",
    "upstream_model": "controlled",
    "upstream_format": "chat_completions",
    "available_upstream_formats": ("chat_completions",),
    "tool_protocol": "auto",
    "tool_surface_strategy": "eager",
}


def chunk(delta, finish=None):
    return {
        "id": "controlled-chat", "object": "chat.completion.chunk",
        "created": 1700000000, "model": "controlled",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }


def test_standard_chat_delivers_text_before_producer_eof():
    held, release, closed, first = (threading.Event() for _ in range(4))
    rows, errors, openings = [], [], []

    def producer(payload, *, cancel, timeout):
        openings.append(payload)
        try:
            yield chunk({"role": "assistant"})
            yield chunk({"content": "first "})
            held.set()
            assert release.wait(5), "held producer was not released"
            yield chunk({"reasoning_content": "think later"})
            yield chunk({"content": "second"})
            yield chunk({}, "stop")
        finally:
            closed.set()

    def caller(host, port):
        connection = http.client.HTTPConnection(host, port, timeout=5)
        try:
            connection.request(
                "POST", "/v1/chat/completions",
                body=json.dumps({"model": UPSTREAM["model_id"], "stream": True,
                                 "messages": [{"role": "user", "content": "hello"}]}),
                headers={"Authorization": f"Bearer {GATEWAY_CLIENT_KEY}",
                         "Content-Type": "application/json", "Connection": "close"},
            )
            response = connection.getresponse()
            assert response.status == 200
            for row in sse_events(response):
                rows.append(row)
                if isinstance(row, dict) and any(
                    choice.get("delta", {}).get("content")
                    for choice in row.get("choices", [])
                ):
                    first.set()
            response.close()
        except BaseException as exc:
            errors.append(exc)
        finally:
            connection.close()

    with GatewayHarness() as harness, patch.object(
        gateway_catalog_runtime, "choose_upstream", return_value=UPSTREAM
    ), patch.object(subscription_exchange, "backend_for", return_value=producer):
        thread = threading.Thread(target=caller, args=(harness.host, harness.port), daemon=True)
        thread.start()
        try:
            assert held.wait(2), "producer did not supply its first text"
            assert first.wait(1), "first text stayed buffered while producer was open"
            assert not closed.is_set()
        finally:
            release.set()
            thread.join(2)
    assert not thread.is_alive()
    assert not errors
    assert len(openings) == 1
    assert closed.is_set()
    chunks = [row for row in rows if isinstance(row, dict)]
    assert "".join(choice.get("delta", {}).get("content", "")
                   for row in chunks for choice in row.get("choices", [])) == "first second"
    assert "".join(choice.get("delta", {}).get("reasoning_content", "")
                   for row in chunks for choice in row.get("choices", [])) == "think later"
    assert len({row["id"] for row in chunks}) == 1
    assert len({row["created"] for row in chunks}) == 1
    assert sum(choice.get("finish_reason") is not None
               for row in chunks for choice in row.get("choices", [])) == 1
    assert rows.count("[DONE]") == 1


def test_active_chat_disconnect_cancels_and_closes_producer_without_replay():
    held, cancelled, closed = (threading.Event() for _ in range(3))
    openings = []

    def producer(payload, *, cancel, timeout):
        openings.append(payload)
        try:
            yield chunk({"content": "active"})
            held.set()
            if cancel.wait(5):
                cancelled.set()
        finally:
            closed.set()

    with GatewayHarness() as harness, patch.object(
        gateway_catalog_runtime, "choose_upstream", return_value=UPSTREAM
    ), patch.object(subscription_exchange, "backend_for", return_value=producer):
        connection = http.client.HTTPConnection(harness.host, harness.port, timeout=5)
        connection.request(
            "POST", "/v1/chat/completions",
            body=json.dumps({"model": UPSTREAM["model_id"], "stream": True,
                             "messages": [{"role": "user", "content": "hello"}]}),
            headers={"Authorization": f"Bearer {GATEWAY_CLIENT_KEY}",
                     "Content-Type": "application/json", "Connection": "close"},
        )
        caller_socket = connection.sock
        response = connection.getresponse()
        try:
            assert response.status == 200
            for row in sse_events(response):
                assert row != "[DONE]"
                assert all(choice.get("finish_reason") is None for choice in row.get("choices", []))
                if any(choice.get("delta", {}).get("content") for choice in row.get("choices", [])):
                    break
            else:
                pytest.fail("active text was not delivered")
            assert held.wait(1)
            assert not closed.is_set()
        finally:
            caller_socket.shutdown(socket.SHUT_RDWR)
            response.close()
            connection.close()
        assert cancelled.wait(1)
        assert closed.wait(1)
    assert len(openings) == 1


def collect_chat(producer, *, tools=None, stream=True):
    payload = {"model": UPSTREAM["model_id"], "stream": stream,
               "messages": [{"role": "user", "content": "hello"}]}
    if tools is not None:
        payload["tools"] = tools
    with GatewayHarness() as harness, patch.object(
        gateway_catalog_runtime, "choose_upstream", return_value=UPSTREAM
    ), patch.object(subscription_exchange, "backend_for", return_value=producer):
        result = request_gateway(
            harness.host, harness.port, "POST", "/v1/chat/completions",
            body=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {GATEWAY_CLIENT_KEY}",
                     "Content-Type": "application/json", "Connection": "close"},
        )
    assert result.status == 200
    if not stream:
        return json.loads(result.body)
    return ["[DONE]" if event.data == b"[DONE]" else json.loads(event.data)
            for event in parsed_sse_events(result.body) if event.data]


def test_chat_text_then_late_reasoning_is_preserved_once():
    def producer(payload, **options):
        yield chunk({"content": "first "})
        yield chunk({"reasoning_content": "think later"})
        yield chunk({"content": "second"}, "stop")

    rows = collect_chat(producer)
    chunks = [row for row in rows if isinstance(row, dict)]
    deltas = [choice["delta"] for row in chunks for choice in row.get("choices", [])]
    assert "".join(delta.get("content", "") for delta in deltas) == "first second"
    assert "".join(delta.get("reasoning_content", "") for delta in deltas) == "think later"
    assert len({row["id"] for row in chunks}) == 1
    assert sum(choice.get("finish_reason") is not None
               for row in chunks for choice in row.get("choices", [])) == 1
    assert rows.count("[DONE]") == 1


@pytest.mark.parametrize("stream", [True, False])
def test_chat_text_then_fragmented_tool_preserves_inverse_name_and_call_identity(stream):
    tools = [{"type": "function", "function": {
        "name": "get_weather", "description": "Get weather",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}},
    }}]

    def producer(payload, **options):
        wire_name = payload["tools"][0]["function"]["name"]
        yield chunk({"content": "checking"})
        yield chunk({"tool_calls": [{"index": 0, "id": "call_original", "type": "function",
                                    "function": {"name": wire_name, "arguments": '{"city":'}}]})
        yield chunk({"tool_calls": [{"index": 0, "function": {"arguments": '"Oslo"}'}}]}, "tool_calls")

    result = collect_chat(producer, tools=tools, stream=stream)
    if stream:
        chunks = [row for row in result if isinstance(row, dict)]
        deltas = [choice["delta"] for row in chunks for choice in row.get("choices", [])]
        assert "".join(delta.get("content", "") for delta in deltas) == "checking"
        calls = [call for delta in deltas for call in delta.get("tool_calls", [])]
        assert len({row["id"] for row in chunks}) == 1
        assert result.count("[DONE]") == 1
    else:
        message = result["choices"][0]["message"]
        assert message["content"] == "checking"
        calls = message["tool_calls"]
    assert len(calls) == 1
    assert calls[0]["id"] == "call_original"
    assert calls[0]["function"] == {"name": "get_weather", "arguments": '{"city":"Oslo"}'}


@pytest.mark.parametrize("failure", ["error", "incomplete"])
def test_chat_failure_after_text_is_truthful_and_not_replayed(failure):
    openings = []

    def producer(payload, **options):
        openings.append(payload)
        yield chunk({"content": "partial"})
        if failure == "error":
            raise BackendError("controlled-late-error", "Controlled producer failed.")

    rows = collect_chat(producer)
    chunks = [row for row in rows if isinstance(row, dict)]
    assert len(openings) == 1
    assert "".join(choice.get("delta", {}).get("content", "")
                   for row in chunks for choice in row.get("choices", [])) == "partial"
    assert any("error" in row for row in chunks)
    assert not any(choice.get("finish_reason") is not None
                   for row in chunks for choice in row.get("choices", []))


def test_chat_late_unowned_tool_keeps_text_and_returns_typed_error():
    def producer(payload, **options):
        yield chunk({"content": "checking"})
        yield chunk({"tool_calls": [{"index": 0, "id": "call_invalid", "type": "function",
                                    "function": {"name": "weather.get_weather", "arguments": "{}"}}]}, "tool_calls")

    rows = collect_chat(producer)
    text = "".join(choice.get("delta", {}).get("content", "")
                   for row in rows if isinstance(row, dict) for choice in row.get("choices", []))
    assert text == "checking"
    assert any("error" in row for row in rows if isinstance(row, dict))
    assert not any(choice.get("finish_reason") is not None
                   for row in rows if isinstance(row, dict) for choice in row.get("choices", []))


def test_chat_leading_reasoning_keeps_existing_field_order():
    def producer(payload, **options):
        yield chunk({"reasoning_content": "think first"})
        yield chunk({"content": "answer"}, "stop")

    rows = collect_chat(producer)
    deltas = [choice["delta"] for row in rows if isinstance(row, dict)
              for choice in row.get("choices", []) if choice["delta"]]
    assert deltas == [{"role": "assistant"}, {"reasoning_content": "think first"}, {"content": "answer"}]
    assert rows.count("[DONE]") == 1


def test_adapted_http_chat_does_not_retry_incomplete_stream_after_text_exposure():
    with GatewayHarness() as harness, patch.object(
        gateway_settings, "gateway_auto_retry_enabled", return_value=True
    ):
        harness.set_sse_response((b"data: " + json.dumps(chunk({"content": "partial"})).encode() + b"\n\n",))
        result = request_gateway(
            harness.host, harness.port, "POST", "/v1/chat/completions",
            body=json.dumps({"model": "volc/glm-5.2", "stream": True,
                             "messages": [{"role": "user", "content": "hello"}]}).encode(),
            headers={"Authorization": f"Bearer {GATEWAY_CLIENT_KEY}",
                     "Content-Type": "application/json", "Connection": "close"},
        )
        assert len(harness.stub.captures) == 1
    assert result.status == 200
    rows = [json.loads(event.data) for event in parsed_sse_events(result.body)
            if event.data and event.data != b"[DONE]"]
    assert "".join(choice.get("delta", {}).get("content", "")
                   for row in rows for choice in row.get("choices", [])) == "partial"
    assert any("error" in row for row in rows)
    assert not any(choice.get("finish_reason") is not None
                   for row in rows for choice in row.get("choices", []))


def test_compact_chat_omitted_tool_media_keeps_existing_summary_rewrite():
    marker = "data:image/png;base64,AA=="
    openings = []

    def producer(payload, **options):
        openings.append(payload)
        assert marker not in json.dumps(payload)
        assert multimodal_tool_result.VISUAL_CONTENT_OMITTED_NOTICE in json.dumps(payload)
        yield chunk({"content": "Summary text. "})
        yield chunk({}, "stop")

    payload = {
        "model": UPSTREAM["model_id"], "stream": True,
        "messages": [
            {"role": "assistant", "content": None, "tool_calls": [{
                "id": "call_media", "type": "function",
                "function": {"name": "inspect_image", "arguments": "{}"},
            }]},
            {"role": "tool", "tool_call_id": "call_media", "content": [
                {"type": "text", "text": "tool info"},
                {"type": "image_url", "image_url": {"url": marker}},
            ]},
            {"role": "user", "content": "Summarize this history."},
        ],
    }
    with GatewayHarness() as harness, patch.object(
        gateway_catalog_runtime, "choose_upstream", return_value={**UPSTREAM, "input_modalities": ["text"]}
    ), patch.object(subscription_exchange, "backend_for", return_value=producer):
        result = request_gateway(
            harness.host, harness.port, "POST", "/v1/chat/completions",
            body=json.dumps(payload).encode(),
            headers={"Authorization": f"Bearer {GATEWAY_CLIENT_KEY}", "Content-Type": "application/json",
                     "Connection": "close", "x-request-kind": "compact"},
        )
    assert len(openings) == 1
    assert result.status == 200
    rows = ["[DONE]" if event.data == b"[DONE]" else json.loads(event.data)
            for event in parsed_sse_events(result.body) if event.data]
    chunks = [row for row in rows if isinstance(row, dict)]
    assert not any("error" in row for row in chunks)
    assert "".join(choice.get("delta", {}).get("content", "")
                   for row in chunks for choice in row.get("choices", [])) == (
                       "Summary text.\n\n" + multimodal_tool_result.VISUAL_CONTENT_OMITTED_NOTICE
                   )
    assert sum(choice.get("finish_reason") is not None
               for row in chunks for choice in row.get("choices", [])) == 1
    assert rows.count("[DONE]") == 1
