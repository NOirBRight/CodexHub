from __future__ import annotations

import json

import pytest

import codex_proxy
from codex_semantic_adapter import COLLABORATION_V2


def _external_chat_upstream() -> dict:
    return {
        "name": "custom_endpoint",
        "upstream_format": "responses",
        "tool_protocol": "chat_tools",
        "tool_surface_strategy": "eager",
    }


def _request(tools: list[dict], *, model: str = "custom-model", tool_choice="auto") -> bytes:
    return json.dumps(
        {
            "model": model,
            "input": [{"type": "message", "role": "user", "content": "Use the requested tool."}],
            "tools": tools,
            "tool_choice": tool_choice,
        }
    ).encode("utf-8")


def _decoded_sse(line: bytes) -> dict:
    assert line.startswith(b"data:")
    return json.loads(line.split(b":", 1)[1].strip())


def test_gateway_builds_and_applies_one_runtime_plan_before_external_sampling(monkeypatch):
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        codex_proxy,
        "write_proxy_event",
        lambda name, **fields: events.append((name, fields)),
    )
    tools = [
        {"type": "function", "name": "plain", "parameters": {"type": "object"}},
        {
            "type": "namespace",
            "name": "vendor",
            "tools": [{"type": "function", "name": "run", "parameters": {"type": "object"}}],
        },
        {"type": "custom", "name": "editor", "format": {"type": "text"}},
        {"type": "tool_search", "execution": "client"},
        {"type": "web_search", "private": "must-not-be-logged"},
        {"type": "future_kind", "opaque": "must-not-be-logged"},
    ]
    context: dict = {"request_id": "req-private"}

    payload = json.loads(
        codex_proxy.compatible_request_body(
            _request(tools),
            _external_chat_upstream(),
            event_context=context,
            inject_codex_tools=False,
        )
    )

    assert payload["model"] == "custom-model"
    assert payload["tools"][0] == tools[0]
    assert [tool["type"] for tool in payload["tools"]] == ["function", "function", "function"]
    assert payload["tools"][1]["name"].startswith("__codexhub_ns_")
    assert payload["tools"][2]["name"].startswith("__codexhub_custom_")
    assert {entry.disposition for entry in context["_runtime_tool_compatibility_plan"].entries} == {
        "native",
        "adapt",
        "omit",
    }
    compatibility_events = [fields for name, fields in events if name == "runtime_tool_compatibility_planned"]
    assert compatibility_events
    serialized = json.dumps(compatibility_events, sort_keys=True)
    assert "must-not-be-logged" not in serialized
    assert "req-private" not in serialized
    assert "__codexhub_" not in serialized


def test_required_unsupported_hosted_tool_fails_before_request_is_returned():
    with pytest.raises(codex_proxy.UpstreamProtocolTranslationError) as caught:
        codex_proxy.compatible_request_body(
            _request(
                [{"type": "web_search"}],
                tool_choice={"type": "web_search"},
            ),
            _external_chat_upstream(),
            event_context={},
            inject_codex_tools=False,
        )

    assert caught.value.cause.code == "tool_compatibility_required_unavailable"
    assert "web_search" not in str(caught.value)


def test_response_body_uses_the_request_plan_for_exact_namespace_and_custom_inverse():
    context: dict = {}
    request_payload = json.loads(
        codex_proxy.compatible_request_body(
            _request(
                [
                    {
                        "type": "namespace",
                        "name": "vendor",
                        "tools": [{"type": "function", "name": "run", "parameters": {}}],
                    },
                    {"type": "custom", "name": "editor", "format": {"type": "text"}},
                ]
            ),
            _external_chat_upstream(),
            event_context=context,
            inject_codex_tools=False,
        )
    )
    namespace_alias, custom_alias = [tool["name"] for tool in request_payload["tools"]]
    upstream_response = {
        "id": "resp",
        "output": [
            {
                "id": "item_ns",
                "type": "function_call",
                "status": "completed",
                "call_id": "call_ns",
                "name": namespace_alias,
                "arguments": "{}",
            },
            {
                "id": "item_custom",
                "type": "function_call",
                "status": "completed",
                "call_id": "call_custom",
                "name": custom_alias,
                "arguments": '{"__codexhub_custom_input":"opaque"}',
            },
        ],
    }

    decoded = json.loads(
        codex_proxy.compatible_response_body(
            json.dumps(upstream_response).encode("utf-8"),
            "custom_endpoint",
            context,
        )
    )

    assert decoded["output"][0]["namespace"] == "vendor"
    assert decoded["output"][0]["name"] == "run"
    assert decoded["output"][0]["call_id"] == "call_ns"
    assert decoded["output"][1]["type"] == "custom_tool_call"
    assert decoded["output"][1]["name"] == "editor"
    assert decoded["output"][1]["input"] == "opaque"
    assert [item["id"] for item in decoded["output"]] == ["item_ns", "item_custom"]


def test_sse_inverse_uses_the_same_request_plan_and_rejects_unknown_alias():
    context: dict = {}
    request_payload = json.loads(
        codex_proxy.compatible_request_body(
            _request(
                [
                    {
                        "type": "namespace",
                        "name": "vendor",
                        "tools": [{"type": "function", "name": "run", "parameters": {}}],
                    }
                ]
            ),
            _external_chat_upstream(),
            event_context=context,
            inject_codex_tools=False,
        )
    )
    alias = request_payload["tools"][0]["name"]
    added = {
        "type": "response.output_item.added",
        "item": {
            "type": "function_call",
            "name": alias,
            "call_id": "call",
            "item_id": "item",
            "arguments": "",
        },
    }
    mapped = _decoded_sse(
        codex_proxy.compatible_sse_line(
            b"data: " + json.dumps(added).encode("utf-8") + b"\n",
            "custom_endpoint",
            context,
        )
    )
    assert mapped["item"]["namespace"] == "vendor"
    assert mapped["item"]["name"] == "run"

    unknown = {
        "type": "response.output_item.added",
        "item": {
            "type": "function_call",
            "name": "__codexhub_ns_unknown_1",
            "call_id": "other",
            "item_id": "other-item",
        },
    }
    with pytest.raises(codex_proxy.UpstreamProtocolTranslationError):
        codex_proxy.compatible_sse_line(
            b"data: " + json.dumps(unknown).encode("utf-8") + b"\n",
            "custom_endpoint",
            context,
        )


def test_changing_only_model_slug_does_not_change_compatibility_dispositions():
    tools = [
        {
            "type": "namespace",
            "name": "vendor",
            "tools": [{"type": "function", "name": "run", "parameters": {}}],
        },
        {"type": "web_search"},
    ]
    contexts = [{}, {}]
    for model, context in zip(("model-a", "model-b"), contexts, strict=True):
        codex_proxy.compatible_request_body(
            _request(tools, model=model),
            _external_chat_upstream(),
            event_context=context,
            inject_codex_tools=False,
        )

    assert [
        (entry.family, entry.disposition)
        for entry in contexts[0]["_runtime_tool_compatibility_plan"].entries
    ] == [
        (entry.family, entry.disposition)
        for entry in contexts[1]["_runtime_tool_compatibility_plan"].entries
    ]


def test_explicit_selected_provider_hosted_capability_preserves_native_declaration():
    context: dict = {}
    upstream = {
        **_external_chat_upstream(),
        "hosted_tool_capabilities": {"web_search": True},
        "tool_protocol_capabilities": {
            "hosted_lifecycles": ["web_search"],
        },
    }

    payload = json.loads(
        codex_proxy.compatible_request_body(
            _request([{"type": "web_search", "search_context_size": "low"}]),
            upstream,
            event_context=context,
            inject_codex_tools=False,
        )
    )

    assert payload["tools"] == [{"type": "web_search", "search_context_size": "low"}]
    assert context["_runtime_tool_compatibility_plan"].entries[0].disposition == "native"


def test_custom_sse_is_not_forwarded_until_the_complete_envelope_is_valid():
    context: dict = {}
    request_payload = json.loads(
        codex_proxy.compatible_request_body(
            _request([{"type": "custom", "name": "editor", "format": {"type": "text"}}]),
            _external_chat_upstream(),
            event_context=context,
            inject_codex_tools=False,
        )
    )
    alias = request_payload["tools"][0]["name"]

    def send(event: dict) -> bytes:
        return codex_proxy.compatible_sse_line(
            b"data: " + json.dumps(event).encode("utf-8") + b"\n\n",
            "custom_endpoint",
            context,
        )

    assert send(
        {
            "type": "response.output_item.added",
            "item": {
                "type": "function_call",
                "id": "item_custom",
                "call_id": "call_custom",
                "name": alias,
                "arguments": "",
            },
        }
    ) == b""
    assert send(
        {
            "type": "response.function_call_arguments.delta",
            "item_id": "item_custom",
            "delta": '{"__codexhub_custom_input":"opaque"}',
        }
    ) == b""
    assert send(
        {
            "type": "response.function_call_arguments.done",
            "item_id": "item_custom",
            "arguments": '{"__codexhub_custom_input":"opaque"}',
        }
    ) == b""
    emitted = send(
        {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "id": "item_custom",
                "call_id": "call_custom",
                "name": alias,
                "arguments": '{"__codexhub_custom_input":"opaque"}',
            },
        }
    )
    events = [
        json.loads(chunk.removeprefix(b"data: "))
        for chunk in emitted.split(b"\n\n")
        if chunk
    ]
    assert [event["type"] for event in events] == [
        "response.output_item.added",
        "response.custom_tool_call_input.delta",
        "response.custom_tool_call_input.done",
        "response.output_item.done",
    ]
    assert events[1]["delta"] == "opaque"


def test_collaboration_v2_is_adapted_without_v1_injection_or_repair():
    context: dict = {
        "request_id": "req",
        "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT,
    }
    tools = [
        {
            "type": "namespace",
            "name": "collaboration",
            "tools": [
                {
                    "type": "function",
                    "name": "followup_task",
                }
            ],
        }
    ]
    history = [
        {
            "type": "function_call",
            "namespace": "collaboration",
            "name": "followup_task",
            "call_id": "call_v2",
            "arguments": '{"task_path":"/root/a","task_name":"a","fork_turns":"all"}',
        },
        {"type": "function_call_output", "call_id": "call_v2", "output": "queued"},
    ]

    payload = json.loads(
        codex_proxy.compatible_request_body(
            json.dumps(
                {
                    "model": "custom-model",
                    "input": history,
                    "tools": tools,
                }
            ).encode("utf-8"),
            _external_chat_upstream(),
            event_context=context,
        )
    )

    aliases = [tool["name"] for tool in payload["tools"] if tool.get("type") == "function"]
    assert len(aliases) == 1
    assert aliases[0].startswith("__codexhub_ns_")
    assert payload["input"][0]["name"] == aliases[0]
    assert "namespace" not in payload["input"][0]
    assert json.loads(payload["input"][0]["arguments"])["task_path"] == "/root/a"
    assert context["collaboration_protocol"] == COLLABORATION_V2
    assert not any(name.startswith("multi_agent_v1__") for name in aliases)


def _external_responses_upstream() -> dict:
    return {
        "name": "responses_endpoint",
        "upstream_format": "responses",
        "tool_protocol": "responses_structured",
        "tool_surface_strategy": "eager",
    }


def _responses_structural_tools() -> list[dict]:
    return [
        {
            "type": "namespace",
            "name": "vendor",
            "tools": [{"type": "function", "name": "run", "parameters": {"type": "object"}}],
        },
        {"type": "custom", "name": "editor", "format": {"type": "text"}},
        {"type": "tool_search", "execution": "client"},
    ]


def test_responses_structured_without_explicit_facts_uses_conservative_tool_defaults():
    context: dict = {}
    payload = json.loads(
        codex_proxy.compatible_request_body(
            _request(_responses_structural_tools()),
            _external_responses_upstream(),
            event_context=context,
            inject_codex_tools=False,
        )
    )

    entries = context["_runtime_tool_compatibility_plan"].entries
    assert [entry.disposition for entry in entries] == ["adapt", "adapt", "omit"]
    assert [tool["type"] for tool in payload["tools"]] == ["function", "function"]
    assert payload["tools"][0]["name"].startswith("__codexhub_ns_")
    assert payload["tools"][1]["name"].startswith("__codexhub_custom_")


def test_responses_structured_explicit_lifecycle_facts_preserve_native_shapes():
    context: dict = {}
    upstream = {
        **_external_responses_upstream(),
        "tool_protocol_capabilities": {
            "function_lifecycle": True,
            "namespace_lifecycle": True,
            "custom_lifecycle": True,
            "tool_search_lifecycle": True,
        },
    }
    tools = _responses_structural_tools()
    payload = json.loads(
        codex_proxy.compatible_request_body(
            _request(tools),
            upstream,
            event_context=context,
            inject_codex_tools=False,
        )
    )

    entries = context["_runtime_tool_compatibility_plan"].entries
    assert [entry.disposition for entry in entries] == ["native", "native", "native"]
    assert payload["tools"] == tools


def test_text_compat_without_explicit_facts_omits_plain_tools_and_fails_required_choice():
    upstream = {
        **_external_chat_upstream(),
        "tool_protocol": "text_compat",
    }
    context: dict = {}
    payload = json.loads(
        codex_proxy.compatible_request_body(
            _request([{"type": "function", "name": "plain"}]),
            upstream,
            event_context=context,
            inject_codex_tools=False,
        )
    )
    assert payload["tools"] == []
    assert context["_runtime_tool_compatibility_plan"].entries[0].disposition == "omit"

    with pytest.raises(codex_proxy.UpstreamProtocolTranslationError) as caught:
        codex_proxy.compatible_request_body(
            _request(
                [{"type": "function", "name": "plain"}],
                tool_choice={"type": "function", "name": "plain"},
            ),
            upstream,
            event_context={},
            inject_codex_tools=False,
        )
    assert caught.value.cause.code == "tool_compatibility_required_unavailable"
    assert "plain" not in str(caught.value)


@pytest.mark.parametrize(
    "facts",
    [
        {"max_tool_name_length": "not-an-int"},
        {"max_alias_attempts": []},
        {"hosted_lifecycles": 17},
        {"unknown_lifecycles": object()},
        {"function_lifecycle": "yes"},
    ],
    ids=["bad-max-length", "bad-max-attempts", "bad-hosted-iterable", "bad-unknown-type", "bad-boolean"],
)
def test_malformed_tool_protocol_capabilities_are_bounded_translation_errors(facts):
    upstream = {
        **_external_chat_upstream(),
        "tool_protocol_capabilities": facts,
    }
    with pytest.raises(codex_proxy.UpstreamProtocolTranslationError) as caught:
        codex_proxy.compatible_request_body(
            _request([{"type": "function", "name": "plain"}]),
            upstream,
            event_context={},
            inject_codex_tools=False,
        )
    assert caught.value.cause.code == "tool_compatibility_capability_manifest"
    assert "not-an-int" not in str(caught.value)
    assert "yes" not in str(caught.value)


def test_named_namespace_choice_requires_exact_child_identity():
    upstream = _external_chat_upstream()
    with pytest.raises(codex_proxy.UpstreamProtocolTranslationError) as caught:
        codex_proxy.compatible_request_body(
            _request(
                [{
                    "type": "namespace",
                    "name": "vendor",
                    "tools": [{"type": "function", "name": "run"}],
                }],
                tool_choice={"type": "function", "name": "run"},
            ),
            upstream,
            event_context={},
            inject_codex_tools=False,
        )
    assert caught.value.cause.code == "tool_compatibility_required_unavailable"
