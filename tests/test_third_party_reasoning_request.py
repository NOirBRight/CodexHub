from __future__ import annotations

import copy
import json
import os
import urllib.error
import urllib.request

import pytest
from live_gateway_support import configured_live_gateway

import gateway_compat
import gateway_request
from collaboration_runtime_contract import COLLABORATION_V2, EXPECTED_PARAMETER_SCHEMAS


PATCH = "*** Begin Patch\n*** Update File: target.txt\n@@\n-before\n+after\n*** End Patch"


def _xai_upstream() -> dict[str, str]:
    return {
        "name": "xai",
        "upstream_model": "grok-4.6",
        "upstream_format": "responses",
        "tool_protocol": "responses_structured",
        "tool_surface_strategy": "eager",
    }


def _v2_namespace() -> dict:
    children = []
    for name, schema in EXPECTED_PARAMETER_SCHEMAS[COLLABORATION_V2].items():
        parameters = copy.deepcopy(schema)
        if not parameters.get("required"):
            parameters.pop("required", None)
        children.append(
            {
                "type": "function",
                "name": name,
                "description": "runtime",
                "strict": False,
                "parameters": parameters,
            }
        )
    return {
        "type": "namespace",
        "name": "collaboration",
        "description": "runtime",
        "tools": children,
    }


def _codex_app_xai_history_request() -> dict:
    return {
        "model": "xai/grok-4.6",
        "include": ["reasoning.encrypted_content"],
        "tools": [
            _v2_namespace(),
            {
                "type": "function",
                "name": "shell",
                "parameters": {
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                },
            },
        ],
        "tool_choice": "auto",
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "hi"}],
            },
            {
                "type": "compaction",
                "summary": {
                    "content": [
                        {"type": "input_text", "text": "Earlier nested context."},
                    ]
                },
            },
            {"type": "compaction_trigger"},
            {
                "type": "reasoning",
                "id": "rs_21",
                "content": None,
                "encrypted_content": "gAAAA-official-blob",
                "summary": [{"type": "summary_text", "text": "thought"}],
            },
            {
                "type": "reasoning",
                "id": "rs_22",
                "content": [
                    {"type": "encrypted_content", "encrypted_content": "part-blob"},
                    {"type": "reasoning_text", "text": "visible"},
                ],
                "summary": None,
            },
            {
                "type": "function_call",
                "name": "shell",
                "call_id": "call_shell",
                "arguments": json.dumps({"command": "pwd"}),
            },
            {
                "type": "function_call_output",
                "call_id": "call_shell",
                "output": "/tmp",
            },
            {
                "type": "custom_tool_call",
                "status": "completed",
                "call_id": "call_patch",
                "name": "apply_patch",
                "input": PATCH,
            },
            {
                "type": "custom_tool_call_output",
                "call_id": "call_patch",
                "output": "Success. Updated target.txt",
            },
            {
                "type": "agent_message",
                "id": "am1",
                "author": "agent/root",
                "recipient": "agent/root/worker",
                "content": [{"type": "input_text", "text": "inspect"}],
            },
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Reply with exactly: E2E_OK"}],
            },
        ],
        "stream": True,
    }


def test_sanitize_third_party_reasoning_items_replaces_null_content_with_empty_sequence():
    payload = {
        "input": [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            {
                "type": "reasoning",
                "id": "rs_21",
                "content": None,
                "summary": [{"type": "summary_text", "text": "thought"}],
            },
        ]
    }

    changed = gateway_request.sanitize_third_party_reasoning_items(payload)

    assert changed is True
    item = payload["input"][1]
    assert item["content"] == []
    assert item["summary"][0]["text"] == "thought"
    assert "id" not in item


def test_sanitize_third_party_reasoning_items_drops_stale_response_references():
    payload = {
        "previous_response_id": "resp_stale",
        "input": [
            {"type": "item_reference", "id": "rs_missing"},
            {"type": "reasoning", "id": "rs_missing", "summary": []},
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
        ],
    }

    changed = gateway_request.sanitize_third_party_reasoning_items(payload)

    assert changed is True
    assert "previous_response_id" not in payload
    assert [item.get("type") for item in payload["input"]] == ["message"]


def test_sanitize_third_party_reasoning_items_drops_empty_reasoning_after_null_summary():
    payload = {"input": [{"type": "reasoning", "id": "rs_null_summary", "summary": None}]}

    changed = gateway_request.sanitize_third_party_reasoning_items(payload)

    assert changed is True
    assert payload["input"] == []


def test_sanitize_third_party_reasoning_items_strips_encrypted_content_everywhere():
    payload = {
        "include": ["reasoning.encrypted_content"],
        "input": [
            {
                "type": "reasoning",
                "encrypted_content": "gAAAA-blob",
                "content": [
                    {"type": "encrypted_content", "encrypted_content": "part-blob"},
                    {"type": "reasoning_text", "text": "keep"},
                ],
            }
        ],
    }

    changed = gateway_request.sanitize_third_party_reasoning_items(payload)

    assert changed is True
    assert "include" not in payload
    assert "encrypted_content" not in json.dumps(payload)
    assert payload["input"][0]["content"] == []
    assert payload["input"][0]["summary"] == [{"type": "summary_text", "text": "keep"}]


def test_xai_v2_codex_app_history_is_safe_for_strict_responses_deserializers():
    context: dict = {}
    transformed = json.loads(
        gateway_compat.compatible_request_body(
            json.dumps(_codex_app_xai_history_request()).encode(),
            _xai_upstream(),
            event_context=context,
            inject_codex_tools=False,
            behavior_profile="codex_app_external_adapter",
        )
    )

    dumped = json.dumps(transformed)
    assert "encrypted_content" not in dumped
    assert "include" not in transformed
    assert context.get("collaboration_protocol") == COLLABORATION_V2
    types = [item.get("type") for item in transformed["input"]]
    assert "compaction" not in types
    assert "compaction_trigger" not in types
    assert any(
        item.get("type") == "message"
        and item.get("role") == "developer"
        and "Earlier nested context." in str(item.get("content"))
        for item in transformed["input"]
    )
    assert "reasoning" in types
    assert "function_call" in types
    assert "function_call_output" in types
    assert any(item.get("name") == "apply_patch" for item in transformed["input"] if isinstance(item, dict))
    for item in transformed["input"]:
        if item.get("type") == "reasoning":
            assert "id" not in item
            assert item.get("content") == []
            assert isinstance(item.get("summary"), list)
            for part in item["summary"]:
                assert part.get("type") == "summary_text"
                assert isinstance(part.get("text"), str)


def test_live_gateway_accepts_sanitized_xai_codex_app_history():
    if os.environ.get("CODEXHUB_SKIP_LIVE_XAI_E2E") == "1":
        pytest.skip("live xAI E2E explicitly disabled")
    gateway = configured_live_gateway()
    body = json.dumps(_codex_app_xai_history_request()).encode()
    req = urllib.request.Request(
        f"{gateway.base_url}/v1/responses",
        data=body,
        headers={
            "Authorization": f"Bearer {gateway.client_key}",
            "Content-Type": "application/json",
            "User-Agent": "codex-app",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            raw = resp.read(8000).decode("utf-8", "replace")
            assert resp.status == 200
            assert "encrypted_content" not in raw.lower() or "error" not in raw.lower()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise AssertionError(f"live xAI E2E HTTP {exc.code}: {detail[:2000]}") from exc
    except urllib.error.URLError as exc:
        raise AssertionError(f"live Gateway unavailable for xAI E2E: {exc}") from exc
