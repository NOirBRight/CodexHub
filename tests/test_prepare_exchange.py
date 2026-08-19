from __future__ import annotations

import json

import pytest

from protocol_translation import (
    ChatToResponsesStreamConverter,
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
    assert isinstance(exchange.stream_decoder(), ChatToResponsesStreamConverter)


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


@pytest.mark.parametrize(
    "field,value",
    [
        ("client_metadata", "not-an-object"),
        ("include", ["unknown.include"]),
        ("prompt_cache_key", {"not": "a string"}),
        ("text", {"format": {"type": "json_schema"}}),
        ("text", {"verbosity": "maximum"}),
        ("text", {"verbosity": {"invalid": True}}),
        ("reasoning", {"summary": "auto"}),
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
    assert exchange.upstream_body == body
