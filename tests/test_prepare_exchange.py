from __future__ import annotations

import json

import pytest

from gateway_stream_semantics import (
    response_body_to_chat_completion_body,
)
from protocol_translation import (
    GatewayChatToResponsesStreamConverter,
    GatewayResponsesToChatStreamConverter,
    NonForwardable,
    PreparedExchange,
    prepare_exchange,
)


def _responses_body(**fields: object) -> bytes:
    payload = {"model": "placeholder", "input": "hi"}
    payload.update(fields)
    return json.dumps(payload).encode("utf-8")


def test_prepare_exchange_responses_to_chat_standard_floor() -> None:
    exchange = prepare_exchange(
        _responses_body(
            client_metadata={},
            include=[],
            prompt_cache_key="",
            store=False,
            text={},
            stream=True,
        ),
        inbound_format="responses",
        outbound_format="chat_completions",
    )
    assert isinstance(exchange, PreparedExchange)
    assert exchange.stream is True
    chat = json.loads(exchange.upstream_body)
    assert chat["model"] == "placeholder"
    assert chat["messages"][0] == {"role": "user", "content": "hi"}
    converter = GatewayChatToResponsesStreamConverter()
    events = converter.events_for_chunk(
        {
            "id": "chatcmpl_1",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": "placeholder",
            "choices": [{"index": 0, "delta": {"content": "hi"}, "finish_reason": None}],
        }
    )
    assert any(event.get("type") == "response.output_text.delta" for event in events)


def test_prepare_exchange_consumes_real_codex_transport_defaults() -> None:
    exchange = prepare_exchange(
        _responses_body(
            client_metadata={
                "turn_id": "turn-1",
                "session_id": "session-1",
                "thread_id": "thread-1",
                "x-codex-turn-metadata": "opaque-local-metadata",
            },
            include=["reasoning.encrypted_content"],
            prompt_cache_key="thread-1",
            store=False,
            text={"verbosity": "low"},
            reasoning={"effort": "high"},
            stream=True,
        ),
        inbound_format="responses",
        outbound_format="chat_completions",
    )
    chat = json.loads(exchange.upstream_body)
    for field in (
        "client_metadata",
        "include",
        "prompt_cache_key",
        "store",
        "text",
        "reasoning",
    ):
        assert field not in chat
    assert chat["stream"] is True


def test_prepare_exchange_consumes_desktop_reasoning_summary_selector_for_chat() -> None:
    """Legacy Desktop catalogs must not turn a valid Chat request into a 400."""

    exchange = prepare_exchange(
        _responses_body(
            reasoning={"effort": "max", "summary": "auto"},
            stream=True,
        ),
        inbound_format="responses",
        outbound_format="chat_completions",
    )

    chat = json.loads(exchange.upstream_body)
    assert chat["messages"] == [{"role": "user", "content": "hi"}]
    assert "reasoning" not in chat


def test_prepare_exchange_consumes_full_desktop_reasoning_controls_for_chat() -> None:
    """Stale Desktop catalogs may send every documented Responses selector."""

    exchange = prepare_exchange(
        _responses_body(
            reasoning={
                "effort": "max",
                "summary": "auto",
                "generate_summary": "auto",
                "mode": "standard",
                "context": "all_turns",
            },
            stream=True,
        ),
        inbound_format="responses",
        outbound_format="chat_completions",
    )

    chat = json.loads(exchange.upstream_body)
    assert chat["messages"] == [{"role": "user", "content": "hi"}]
    assert "reasoning" not in chat


@pytest.mark.parametrize(
    "field,value",
    [
        ("client_metadata", "not-an-object"),
        ("include", ["unknown.include"]),
        ("prompt_cache_key", {"not": "a string"}),
        ("text", {"format": {"type": "json_schema"}}),
        ("text", {"verbosity": "maximum"}),
        ("text", {"verbosity": {"invalid": True}}),
        ("reasoning", {"summary": "verbose"}),
        ("reasoning", {"generate_summary": "verbose"}),
        ("reasoning", {"mode": "experimental"}),
        ("reasoning", {"context": "future_turn"}),
        ("reasoning", {"effort": "extreme"}),
        ("reasoning", {"effort": {"invalid": True}}),
    ],
)
def test_prepare_exchange_rejects_unknown_codex_transport_values(
    field: str,
    value: object,
) -> None:
    with pytest.raises(NonForwardable) as caught:
        prepare_exchange(
            _responses_body(**{field: value}),
            inbound_format="responses",
            outbound_format="chat_completions",
        )
    assert caught.value.code == "unsupported_protocol_semantics"


def test_prepare_exchange_rejects_unknown_fields() -> None:
    try:
        prepare_exchange(
            _responses_body(mystery_field=True),
            inbound_format="responses",
            outbound_format="chat_completions",
        )
    except NonForwardable as error:
        assert error.code == "unsupported_protocol_semantics"
        return
    raise AssertionError("expected NonForwardable")


def test_prepare_exchange_rejects_store_true() -> None:
    try:
        prepare_exchange(
            _responses_body(store=True),
            inbound_format="responses",
            outbound_format="chat_completions",
        )
    except NonForwardable:
        return
    raise AssertionError("expected store=true to be non-forwardable")


def test_prepare_exchange_passthrough_same_protocol() -> None:
    body = json.dumps({"model": "placeholder", "messages": [{"role": "user", "content": "hi"}]}).encode()
    exchange = prepare_exchange(
        body,
        inbound_format="chat_completions",
        outbound_format="chat_completions",
    )
    assert exchange.upstream_body is body
    assert exchange.stream is False


def test_prepare_exchange_identity_reads_stream_true() -> None:
    body = json.dumps({"model": "placeholder", "stream": True, "input": "hi"}).encode()
    exchange = prepare_exchange(
        body,
        inbound_format="responses",
        outbound_format="responses",
    )
    assert exchange.upstream_body is body
    assert exchange.stream is True


def test_prepare_exchange_responses_identity_does_not_hop() -> None:
    body = _responses_body(stream=False)
    exchange = prepare_exchange(
        body,
        inbound_format="responses",
        outbound_format="responses",
    )
    assert exchange.upstream_body is body
    assert exchange.stream is False


def test_prepare_exchange_chat_to_responses_one_hop() -> None:
    body = json.dumps(
        {
            "model": "placeholder",
            "messages": [
                {"role": "system", "content": "Be concise."},
                {"role": "user", "content": "Hello"},
            ],
            "max_tokens": 32,
            "stream": True,
        }
    ).encode("utf-8")
    exchange = prepare_exchange(
        body,
        inbound_format="chat_completions",
        outbound_format="responses",
    )
    payload = json.loads(exchange.upstream_body)
    assert exchange.stream is True
    assert payload["model"] == "placeholder"
    assert payload["instructions"] == "Be concise."
    assert payload["input"][0]["role"] == "user"
    assert payload["max_output_tokens"] == 32
    assert "messages" not in payload
    converter = GatewayResponsesToChatStreamConverter()
    chunks = converter.chunks_for_event({"type": "response.output_text.delta", "delta": "hello"})
    assert [chunk["choices"][0]["delta"].get("content") for chunk in chunks] == ["hello"]


def test_prepare_exchange_rejects_unknown_protocol_pair() -> None:
    with pytest.raises(NonForwardable) as caught:
        prepare_exchange(
            _responses_body(),
            inbound_format="responses",
            outbound_format="unknown_wire",
        )
    assert caught.value.code == "unsupported_protocol_semantics"


def test_prepare_exchange_responses_to_anthropic_keeps_text_and_tools() -> None:
    exchange = prepare_exchange(
        json.dumps(
            {
                "model": "union-alpha",
                "input": [
                    {"type": "message", "role": "system", "content": "Be concise."},
                    {"type": "message", "role": "user", "content": "hi"},
                ],
                "tools": [
                    {
                        "type": "function",
                        "name": "read_file",
                        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
                    }
                ],
                "stream": True,
            }
        ).encode("utf-8"),
        inbound_format="responses",
        outbound_format="anthropic_messages",
    )
    payload = json.loads(exchange.upstream_body)
    assert payload["model"] == "union-alpha"
    assert payload["stream"] is True
    assert payload["system"] == "Be concise."
    assert payload["messages"][0]["role"] == "user"
    assert payload["messages"][0]["content"] == "hi"
    assert payload["tools"][0]["name"] == "read_file"
    assert payload["tools"][0]["input_schema"]["properties"]["path"]["type"] == "string"
    assert payload["max_tokens"] == 131072


def test_prepare_exchange_responses_to_anthropic_maps_reasoning_effort() -> None:
    exchange = prepare_exchange(
        json.dumps(
            {
                "model": "union-alpha",
                "input": [{"type": "message", "role": "user", "content": "hi"}],
                "reasoning": {"effort": "high"},
                "stream": True,
            }
        ).encode("utf-8"),
        inbound_format="responses",
        outbound_format="anthropic_messages",
    )
    payload = json.loads(exchange.upstream_body)
    assert payload["thinking"] == {"type": "enabled", "budget_tokens": 8192}


def test_prepare_exchange_chat_upstream_body_translates_to_chat_completion() -> None:
    request = json.dumps(
        {"model": "placeholder", "messages": [{"role": "user", "content": "hi"}]}
    ).encode("utf-8")
    exchange = prepare_exchange(
        request,
        inbound_format="chat_completions",
        outbound_format="responses",
    )
    assert exchange.inbound_format == "chat_completions"
    upstream = json.dumps(
        {
            "id": "resp_1",
            "object": "response",
            "status": "completed",
            "model": "placeholder",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "hello", "annotations": []}],
                }
            ],
        }
    ).encode("utf-8")
    decoded = json.loads(response_body_to_chat_completion_body(upstream))
    assert decoded["object"] == "chat.completion"
    assert decoded["choices"][0]["message"]["content"] == "hello"


def test_gateway_body_conversion_keeps_namespaced_tool_names_and_call_ids() -> None:
    upstream = json.dumps(
        {
            "id": "resp_2",
            "object": "response",
            "status": "completed",
            "model": "placeholder",
            "output": [
                {
                    "type": "function_call",
                    "call_id": "call_agent_1",
                    "name": "spawn_agent",
                    "namespace": "multi_agent_v1",
                    "arguments": '{"task": "x"}',
                }
            ],
        }
    ).encode("utf-8")
    decoded = json.loads(response_body_to_chat_completion_body(upstream))
    tool_call = decoded["choices"][0]["message"]["tool_calls"][0]
    assert tool_call["id"] == "call_agent_1"
    assert tool_call["function"]["name"] == "multi_agent_v1__spawn_agent"
    assert tool_call["function"]["arguments"] == '{"task": "x"}'


def test_gateway_body_conversion_maps_protocol_errors() -> None:
    from gateway_errors import UpstreamProtocolTranslationError

    with pytest.raises(UpstreamProtocolTranslationError) as caught:
        response_body_to_chat_completion_body(b'{"output": {}}')

    assert caught.value.classification == "unsupported_protocol_semantics"


def test_gateway_body_conversion_reads_translator_at_call_time(monkeypatch) -> None:
    import protocol_translation

    calls = []

    def translate(body, **options):
        calls.append((body, options["preserve_reasoning_history"]))
        return b'{"object":"chat.completion"}'

    monkeypatch.setattr(protocol_translation, "response_body_to_chat_completion_body", translate)
    result = response_body_to_chat_completion_body(b'{"output":[]}', preserve_reasoning_history=True)

    assert result == b'{"object":"chat.completion"}'
    assert calls == [(b'{"output":[]}', True)]
