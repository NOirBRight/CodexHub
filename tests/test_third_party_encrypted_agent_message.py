"""Third-party routes must not die on Official encrypted agent_message history."""
from __future__ import annotations

import json

import gateway_compat
import gateway_stream_semantics
import route_primitives

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


def test_opencode_go_main_generation_continues_without_encrypted_agent_message() -> None:
    adapted = _adapt(
        {
            "model": "opencode-go/deepseek-v4.1-flash",
            "input": [_encrypted_agent_message()],
        },
        {"request_kind": "main_generation"},
    )
    _assert_portable(adapted)


def test_opencode_go_compact_continues_without_encrypted_agent_message() -> None:
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
    adapted = _adapt(payload, context)
    _assert_portable(adapted)


def test_opencode_go_keeps_plaintext_agent_message_parts() -> None:
    adapted = _adapt(
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
    _assert_portable(adapted)
    text = json.dumps(adapted)
    assert "inspect the tray" in text
