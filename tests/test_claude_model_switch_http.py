"""Same-history switching through the production HTTP surface; upstreams are local."""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
import gateway_catalog_runtime
from tests.gateway_harness import GATEWAY_CLIENT_KEY, GatewayHarness, request_gateway
from tests.test_claude_native_subscription_http import CLAUDE_OAUTH, _native_upstream


def test_same_conversation_switches_native_codex_third_party_and_back() -> None:
    history = [{"role": "user", "content": "Remember switch-marker and read the file."}]
    tool_id = "toolu_switch.1"
    with GatewayHarness() as harness:
        assert harness.stub is not None
        routes = [
            ("claude-opus-5-5", "anthropic_messages", "claude-opus-5-5"),
            ("openai/gpt-5.5", "responses", "gpt-5.5"),
            ("volc/glm-5.2", "chat_completions", "glm-5.2"),
            ("external/responses-model", "responses", "responses-model"),
            ("claude-opus-5-5", "anthropic_messages", "claude-opus-5-5"),
        ]
        for index, (selected, protocol, model) in enumerate(routes):
            content = ([{"type": "tool_use", "id": tool_id, "name": "Read", "input": {"path": "fixture"}}]
                       if index == 0 else [{"type": "text", "text": f"switch-marker-{index}"}])
            reply = {
                "id": f"msg_{index}", "type": "message", "role": "assistant", "model": model,
                "content": content, "stop_reason": "tool_use" if index == 0 else "end_turn",
                "stop_sequence": None, "usage": {"input_tokens": 7, "output_tokens": 3},
            }
            if protocol == "responses":
                reply = {
                    "id": f"resp_{index}", "status": "completed", "model": model,
                    "output": [{"type": "message", "role": "assistant", "content": [
                        {"type": "output_text", "text": f"switch-marker-{index}", "annotations": []}]}],
                    "usage": {"input_tokens": 7, "output_tokens": 3},
                }
            elif protocol == "chat_completions":
                reply = {
                    "id": f"chatcmpl_{index}", "object": "chat.completion", "model": model,
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": f"switch-marker-{index}"},
                                 "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 7, "completion_tokens": 3},
                }
            harness.set_json_response(json.dumps(reply).encode())
            request = {
                "model": selected, "max_tokens": 64, "messages": history,
                "tools": [{"name": "Read", "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}}}],
                "safeguards": [{"type": "dangerous_tool_use", "classifier_context": {"synthetic": True}}],
            }
            if protocol == "anthropic_messages":
                upstream = _native_upstream(harness.stub_base_url.removesuffix("/v1"))
            elif selected.startswith("external/"):
                upstream = {
                    "name": "external", "provider_id": "external", "model_id": selected,
                    "base_url": harness.stub_base_url, "auth": "api_key", "api_key": "external-test-token",
                    "upstream_model": model, "upstream_format": protocol,
                }
            else:
                upstream = harness._choose_upstream(selected)
            with patch.object(gateway_catalog_runtime, "choose_upstream", return_value=upstream):
                response = request_gateway(
                    harness.host, harness.port, "POST", "/v1/messages?beta=true",
                    body=json.dumps(request).encode(),
                    headers={
                        "Authorization": f"Bearer {CLAUDE_OAUTH}",
                        "x-codexhub-gateway-key": GATEWAY_CLIENT_KEY,
                        "Content-Type": "application/json", "Connection": "close",
                        "X-Claude-Code-Session-Id": "same-synthetic-session",
                    }, timeout=8.0,
                )
            assert response.status == 200, (selected, response.body)
            assert len(harness.stub.captures) == index + 1
            capture = harness.stub.captures[-1]
            sent = json.loads(capture.body)
            assert sent["model"] == model
            assert capture.headers["x-claude-code-session-id"] == "same-synthetic-session"
            assert "x-codexhub-gateway-key" not in capture.headers
            if protocol == "anthropic_messages":
                assert sent == request
                assert capture.headers["authorization"] == f"Bearer {CLAUDE_OAUTH}"
            else:
                assert "safeguards" not in sent
                assert capture.headers["authorization"] != f"Bearer {CLAUDE_OAUTH}"
                replay = sent["input"] if protocol == "responses" else sent["messages"]
                calls = ([item for item in replay if item.get("type") == "function_call"]
                         if protocol == "responses" else [call for item in replay for call in item.get("tool_calls", [])])
                results = [item for item in replay if item.get("type") == "function_call_output" or item.get("role") == "tool"]
                assert len(calls) == len(results) == 1, (selected, replay)
                assert calls[0].get("call_id", calls[0].get("id")) == tool_id
                assert results[0].get("call_id", results[0].get("tool_call_id")) == tool_id
                assert "file-result-marker" in json.dumps(replay)
                assert "Continue with selected model" in json.dumps(replay)
            message = json.loads(response.body)
            history.append({"role": "assistant", "content": message["content"]})
            history.append({"role": "user", "content": (
                [{"type": "tool_result", "tool_use_id": tool_id, "content": "file-result-marker"},
                 {"type": "text", "text": "Continue with selected model"}]
                if index == 0 else f"Continue with selected model {index + 1}."
            )})


@pytest.mark.parametrize("protocol,model", [("responses", "gpt-5.5"), ("chat_completions", "volc/glm-5.2")])
@pytest.mark.parametrize("stream", [True, False])
def test_selected_reasoning_model_stream_finishes_through_http(protocol: str, model: str, stream: bool) -> None:
    from tests.test_anthropic_messages_exchange import _chat_stream, _responses_stream, _events, _sse

    events = _events(_responses_stream() if protocol == "responses" else _chat_stream())
    if protocol == "responses":
        events.insert(1, {"type": "response.reasoning_summary_text.delta", "delta": "private reasoning"})
    else:
        events[0]["choices"][0]["delta"]["reasoning_content"] = "private reasoning"
    with GatewayHarness() as harness:
        harness.set_sse_response(tuple(_sse(None, event) for event in events))
        response = request_gateway(
            harness.host, harness.port, "POST", "/v1/messages",
            body=json.dumps({"model": model, "stream": stream, "max_tokens": 64,
                             "messages": [{"role": "user", "content": "hello"}]}).encode(),
            headers={"Authorization": f"Bearer {GATEWAY_CLIENT_KEY}", "Content-Type": "application/json"},
            timeout=8.0,
        )
    assert response.status == 200
    if not stream:
        payload = json.loads(response.body)
        assert payload["type"] == "message"
        assert payload["content"]
        assert payload["stop_reason"] in {"tool_use", "end_turn"}
        assert b"private reasoning" not in response.body
        return
    output = _events(response.body)
    assert output[-1]["type"] == "message_stop", output
    assert not any(event["type"] == "error" for event in output)
    assert b"private reasoning" not in response.body
