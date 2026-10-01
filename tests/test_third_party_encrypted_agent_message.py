"""Opaque task content cannot be silently dropped at an external boundary."""
from __future__ import annotations

import json
import copy

import pytest

import gateway_compat
import gateway_stream_semantics
import route_primitives
import protocol_translation
from gateway_errors import UpstreamProtocolTranslationError

CIPHER = "opaque-test-only-ciphertext"

OPENCODE_GO = {
    "name": "opencode_go",
    "upstream_model": "deepseek-v4.1-flash",
    "upstream_format": "responses",
    "tool_protocol": "responses_structured",
    "tool_surface_strategy": "eager",
}


def _encrypted_agent_message() -> dict[str, object]:
    return {
        "type": "agent_message",
        "id": "agent-message-1",
        "author": "/root",
        "recipient": "/root/tray_research",
        "content": [{"type": "encrypted_content", "encrypted_content": CIPHER}],
    }


def _adapt(payload: dict[str, object], context: dict[str, object]) -> dict[str, object]:
    return json.loads(
        gateway_compat.compatible_request_body(
            json.dumps(payload).encode(),
            OPENCODE_GO,
            event_context=context,
            inject_codex_tools=False,
            behavior_profile=route_primitives.BEHAVIOR_CODEX_APP_EXTERNAL_ADAPTER,
        )
    )


def _assert_portable(adapted: dict[str, object]) -> None:
    text = json.dumps(adapted)
    assert CIPHER not in text
    items = adapted.get("input")
    assert isinstance(items, list) and items
    for item in items:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if item.get("type") == "agent_message":
            assert not any(
                isinstance(part, dict) and part.get("type") == "encrypted_content"
                for part in (content if isinstance(content, list) else [])
            )
        assert "encrypted_content" not in item


def _assert_unavailable_task(payload: dict[str, object], context: dict[str, object]) -> None:
    original = copy.deepcopy(payload)
    with pytest.raises(UpstreamProtocolTranslationError, match="caller must restate") as error:
        _adapt(payload, context)
    assert error.value.cause.code == "encrypted_agent_message_unavailable"
    assert CIPHER not in str(error.value)
    assert payload == original


def test_opencode_go_main_generation_cannot_replace_opaque_task_with_placeholder() -> None:
    _assert_unavailable_task({
        "model": "opencode-go/deepseek-v4.1-flash",
        "input": [_encrypted_agent_message()],
    }, {"request_kind": "main_generation"})


def test_opencode_go_compact_cannot_claim_unavailable_task_is_preserved() -> None:
    payload = {
        "model": "opencode-go/deepseek-v4.1-flash",
        "input": [
            {"type": "message", "role": "user", "content": "research the tray"},
            _encrypted_agent_message(),
        ],
        "tools": [],
        "tool_choice": "auto",
    }
    context = {"request_kind": "compact", "compact_placeholder_authorized": True}
    gateway_stream_semantics.strip_tools_for_compact_payload(payload, event_context=context)
    _assert_unavailable_task(payload, context)


def test_opencode_go_partial_plaintext_does_not_prove_complete_task() -> None:
    _assert_unavailable_task(
        {
            "model": "opencode-go/deepseek-v4.1-flash",
            "input": [
                {
                    "type": "agent_message",
                    "id": "agent-message-1",
                    "author": "/root",
                    "recipient": "/root/tray_research",
                    "content": [
                        {"type": "input_text", "text": "inspect the tray"},
                        {"type": "encrypted_content", "encrypted_content": CIPHER},
                    ],
                }
            ],
        },
        {"request_kind": "main_generation"},
    )


def test_object_input_is_not_valid_responses_history_evidence() -> None:
    # A previous test passed a mapping to the compatibility-only helper and
    # called its lossy rewrite success. The Responses input contract requires
    # a string or item list; that shape cannot establish task preservation.
    payload = {"model": "opencode-go/deepseek-v4.1-flash", "input": _encrypted_agent_message()}
    original = copy.deepcopy(payload)
    with pytest.raises(protocol_translation.NonForwardable) as error:
        protocol_translation.prepare_exchange(json.dumps(payload).encode(), inbound_format="responses", outbound_format="chat_completions")
    assert error.value.code == "unsupported_protocol_semantics"
    assert payload == original


def test_opencode_go_plaintext_assignment_retains_directed_task() -> None:
    item = {**_encrypted_agent_message(), "content": [{"type": "input_text", "text": "inspect the tray"}]}
    payload = {"model": "opencode-go/deepseek-v4.1-flash", "input": [item]}
    context = {"request_kind": "main_generation"}
    adapted = _adapt(payload, context)
    _assert_portable(adapted)
    envelope = adapted["input"][0]["content"][0]["text"]
    assert envelope.startswith("__codexhub_agent_message_v2__:")
    assert json.loads(envelope.split(":", 1)[1]) == item
