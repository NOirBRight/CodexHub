from __future__ import annotations

import json

import gateway_compat
import gateway_stream_semantics
import route_primitives


def _compacted_request() -> bytes:
    return json.dumps(
        {
            "model": "gpt-5.6-sol",
            "input": [
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
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "Continue."},
                        {
                            "type": "input_image",
                            "image_url": "data:image/png;base64,AA==",
                        },
                    ],
                },
                {
                    "type": "function_call",
                    "name": "lookup",
                    "call_id": "call-1",
                    "arguments": "{}",
                },
            ],
            "stream": True,
        }
    ).encode("utf-8")


def test_official_passthrough_converts_compacted_history_without_local_failure():
    transformed = json.loads(
        gateway_compat.compatible_request_body(
            _compacted_request(),
            {"name": "official", "upstream_model": "gpt-5.6-sol"},
            behavior_profile=(
                route_primitives.BEHAVIOR_OFFICIAL_CODEX_APP_HTTP_PASSTHROUGH
            ),
        )
    )

    assert transformed["input"][0] == {
        "type": "message",
        "role": "developer",
        "content": "[Compacted conversation context]\nEarlier nested context.",
    }
    assert all(item.get("type") != "compaction_trigger" for item in transformed["input"])
    assert transformed["input"][1]["content"][1]["type"] == "input_image"
    tool_history = json.dumps(transformed["input"][2], sort_keys=True)
    assert "lookup" in tool_history
    assert "call-1" in tool_history
    assert transformed["store"] is False


def test_official_passthrough_uses_opaque_message_for_empty_compaction_summary():
    body = json.dumps(
        {
            "model": "gpt-5.6-sol",
            "input": [{"type": "compaction", "summary": {"content": []}}],
        }
    ).encode("utf-8")

    transformed = json.loads(
        gateway_compat.compatible_request_body(
            body,
            {"name": "official", "upstream_model": "gpt-5.6-sol"},
            behavior_profile=(
                route_primitives.BEHAVIOR_OFFICIAL_CODEX_APP_HTTP_PASSTHROUGH
            ),
        )
    )

    assert transformed["input"] == [
        {
            "type": "message",
            "role": "developer",
            "content": (
                "[Compacted conversation context — opaque, details unavailable]"
            ),
        }
    ]


def test_xai_responses_route_converts_compacted_history_and_preserves_mixed_input():
    transformed = json.loads(
        gateway_compat.compatible_request_body(
            _compacted_request(),
            {
                "name": "xai",
                "upstream_model": "grok-4.6",
                "upstream_format": "responses",
                "tool_protocol": "responses_structured",
                "tool_surface_strategy": "eager",
            },
            event_context={},
            inject_codex_tools=False,
        )
    )

    assert transformed["model"] == "grok-4.6"
    assert transformed["input"][0]["role"] == "developer"
    assert "Earlier nested context." in transformed["input"][0]["content"]
    assert all(item.get("type") != "compaction_trigger" for item in transformed["input"])
    assert transformed["input"][1]["content"][1]["type"] == "input_image"
    tool_history = json.dumps(transformed["input"][2], sort_keys=True)
    assert "lookup" in tool_history
    assert "call-1" in tool_history


def test_worker_guidance_with_compacted_history_uses_same_public_text_collector():
    request = json.loads(_compacted_request())
    request["input"].append(
        {
            "type": "message",
            "role": "user",
            "content": "Return exactly this line: WORKER_DONE",
        }
    )

    transformed = json.loads(
        gateway_compat.compatible_request_body(
            json.dumps(request).encode("utf-8"),
            {
                "name": "xai",
                "upstream_model": "grok-4.6",
                "upstream_format": "responses",
                "tool_protocol": "responses_structured",
                "tool_surface_strategy": "eager",
            },
            event_context={"repair_policy": "codex_subagent_repair"},
            inject_codex_tools=False,
        )
    )

    assert any(
        item.get("role") == "developer"
        and "worker_subagent_finalization_required" in item.get("content", "")
        for item in transformed["input"]
    )


def test_compaction_compatibility_reads_public_text_collector_at_call_time(monkeypatch):
    monkeypatch.setattr(
        gateway_stream_semantics,
        "collect_text_fragments",
        lambda _value: ["Collector replacement observed."],
    )

    transformed = json.loads(
        gateway_compat.compatible_request_body(
            _compacted_request(),
            {"name": "official", "upstream_model": "gpt-5.6-sol"},
            behavior_profile=(
                route_primitives.BEHAVIOR_OFFICIAL_CODEX_APP_HTTP_PASSTHROUGH
            ),
        )
    )

    assert transformed["input"][0]["content"].endswith(
        "Collector replacement observed."
    )
