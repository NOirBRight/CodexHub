from __future__ import annotations

import json

import pytest

from protocol_translation import (
    ChatToResponsesStreamConverter,
    NonForwardable,
    PreparedExchange,
    ResponsesToChatStreamConverter,
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
    assert isinstance(exchange.stream_decoder(), ChatToResponsesStreamConverter)
    assert isinstance(exchange.decode_stream(), ChatToResponsesStreamConverter)


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
    assert exchange.decode_response(b'{"object":"chat.completion"}') == b'{"object":"chat.completion"}'


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
    assert isinstance(exchange.decode_stream(), ResponsesToChatStreamConverter)


def test_prepare_exchange_rejects_unknown_protocol_pair() -> None:
    with pytest.raises(NonForwardable) as caught:
        prepare_exchange(
            _responses_body(),
            inbound_format="responses",
            outbound_format="anthropic_messages",
        )
    assert caught.value.code == "unsupported_protocol_semantics"


def test_prepare_exchange_decode_response_hides_helper_names() -> None:
    request = json.dumps(
        {"model": "placeholder", "messages": [{"role": "user", "content": "hi"}]}
    ).encode("utf-8")
    exchange = prepare_exchange(
        request,
        inbound_format="chat_completions",
        outbound_format="responses",
    )
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
    decoded = json.loads(exchange.decode_response(upstream))
    assert decoded["object"] == "chat.completion"
    assert decoded["choices"][0]["message"]["content"] == "hello"
