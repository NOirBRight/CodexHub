from __future__ import annotations

import json

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
