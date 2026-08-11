from __future__ import annotations

import copy
import json
from unittest.mock import patch

import pytest

import codex_proxy
from collaboration_runtime_contract import EXPECTED_PARAMETER_SCHEMAS
from codex_semantic_adapter import COLLABORATION_V1, COLLABORATION_V2


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


def _collaboration_namespace(version: str) -> dict:
    namespace = "multi_agent_v1" if version == COLLABORATION_V1 else "collaboration"
    children = []
    for name, schema in EXPECTED_PARAMETER_SCHEMAS[version].items():
        parameters = copy.deepcopy(schema)
        if not parameters["required"]:
            del parameters["required"]
        children.append(
            {
                "type": "function",
                "name": name,
                "description": "dynamic",
                "strict": False,
                "parameters": parameters,
            }
        )
    return {
        "type": "namespace",
        "name": namespace,
        "description": "dynamic",
        "tools": children,
    }


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
    assert [tool["type"] for tool in payload["tools"]] == ["function", "function", "function", "function"]
    assert payload["tools"][1]["name"].startswith("__codexhub_ns_")
    assert payload["tools"][2]["name"].startswith("__codexhub_custom_")
    assert payload["tools"][3]["name"].startswith("__codexhub_search_")
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


def test_runtime_plan_aliases_are_deterministic_for_identical_external_request():
    tools = [
        {"type": "function", "name": "plain", "parameters": {"type": "object"}},
        {
            "type": "namespace",
            "name": "vendor",
            "tools": [{"type": "function", "name": "run", "parameters": {"type": "object"}}],
        },
        {"type": "custom", "name": "editor", "format": {"type": "text"}},
        {"type": "tool_search", "execution": "client"},
    ]
    request = json.loads(_request(tools))
    request["prompt_cache_key"] = "stable-runtime-tool-prefix"
    body = json.dumps(request, ensure_ascii=True, separators=(",", ":")).encode("utf-8")

    first = codex_proxy.compatible_request_body(
        body,
        _external_chat_upstream(),
        event_context={},
        inject_codex_tools=False,
    )
    second = codex_proxy.compatible_request_body(
        body,
        _external_chat_upstream(),
        event_context={},
        inject_codex_tools=False,
    )

    assert first == second
    payload = json.loads(first)
    assert payload["prompt_cache_key"] == "stable-runtime-tool-prefix"
    assert [tool["name"].rsplit("_", 2)[0] for tool in payload["tools"][1:]] == [
        "__codexhub_ns",
        "__codexhub_custom",
        "__codexhub_search",
    ]


def test_runtime_plan_alias_seed_normalizes_and_covers_capability_set():
    body = _request(
        [
            {
                "type": "namespace",
                "name": "vendor",
                "tools": [
                    {"type": "function", "name": "run", "parameters": {"type": "object"}}
                ],
            }
        ]
    )
    first_upstream = {
        **_external_chat_upstream(),
        "hosted_tool_capabilities": {"web_search": True, "file_search": True},
        "tool_protocol_capabilities": {
            "hosted_lifecycles": ["web_search", "file_search"],
            "max_alias_attempts": 128,
        },
    }
    equivalent_upstream = {
        **_external_chat_upstream(),
        "hosted_tool_capabilities": {"file_search": True, "web_search": True},
        "tool_protocol_capabilities": {
            "hosted_lifecycles": ["file_search", "web_search"],
            "max_alias_attempts": 128,
        },
    }
    changed_capabilities = copy.deepcopy(first_upstream)
    changed_capabilities["tool_protocol_capabilities"]["max_alias_attempts"] = 127

    first = codex_proxy.compatible_request_body(
        body,
        first_upstream,
        event_context={},
        inject_codex_tools=False,
    )
    equivalent = codex_proxy.compatible_request_body(
        body,
        equivalent_upstream,
        event_context={},
        inject_codex_tools=False,
    )
    changed = codex_proxy.compatible_request_body(
        body,
        changed_capabilities,
        event_context={},
        inject_codex_tools=False,
    )

    assert first == equivalent
    assert json.loads(first)["tools"][0]["name"] != json.loads(changed)["tools"][0]["name"]


def test_deferred_core_runtime_plan_does_not_restore_namespace_children():
    core_tools = [
        {"type": "function", "name": "plain", "parameters": {"type": "object"}},
        {"type": "custom", "name": "editor", "format": {"type": "text"}},
    ]
    namespace = {
        "type": "namespace",
        "name": "vendor",
        "tools": [
            {"type": "function", "name": f"child_{index:03d}", "parameters": {"type": "object"}}
            for index in range(249)
        ],
    }

    def prepare(tools: list[dict], strategy: str) -> tuple[dict, dict]:
        context: dict = {}
        upstream = {**_external_chat_upstream(), "tool_surface_strategy": strategy}
        payload = json.loads(
            codex_proxy.compatible_request_body(
                _request(tools),
                upstream,
                event_context=context,
            )
        )
        return payload, context

    bounded, bounded_context = prepare(core_tools, "deferred_core")
    deferred, deferred_context = prepare([*core_tools, namespace], "deferred_core")
    eager_baseline, _ = prepare(core_tools, "eager")
    eager, eager_context = prepare([*core_tools, namespace], "eager")

    assert len(bounded["tools"]) == 8
    assert len(deferred["tools"]) == len(bounded["tools"])
    assert not any(tool.get("type") == "namespace" for tool in deferred["tools"])
    assert not any(
        entry.family == "namespace"
        for entry in deferred_context["_runtime_tool_compatibility_plan"].entries
    )
    assert len(eager["tools"]) == len(eager_baseline["tools"]) + 249
    eager_namespace_entries = [
        entry
        for entry in eager_context["_runtime_tool_compatibility_plan"].entries
        if entry.family == "namespace"
    ]
    assert len(eager_namespace_entries) == 1
    assert len(eager_namespace_entries[0].aliases) == 249
    assert bounded_context["_runtime_tool_compatibility_plan"].entries


def test_official_passthrough_does_not_apply_runtime_aliases_or_expand_namespaces():
    namespace = {
        "type": "namespace",
        "name": "vendor",
        "tools": [
            {"type": "function", "name": f"child_{index:03d}", "parameters": {"type": "object"}}
            for index in range(249)
        ],
    }
    request = json.loads(_request([namespace], model="gpt-5.6-luna"))
    request["prompt_cache_key"] = "stable-official-prefix"
    body = json.dumps(request, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
    first_context: dict = {}
    second_context: dict = {}

    first = codex_proxy.compatible_request_body(
        body,
        {"name": "official"},
        event_context=first_context,
        behavior_profile=codex_proxy.BEHAVIOR_OFFICIAL_CODEX_APP_HTTP_PASSTHROUGH,
    )
    second = codex_proxy.compatible_request_body(
        body,
        {"name": "official"},
        event_context=second_context,
        behavior_profile=codex_proxy.BEHAVIOR_OFFICIAL_CODEX_APP_HTTP_PASSTHROUGH,
    )

    assert first == second
    payload = json.loads(first)
    assert payload["prompt_cache_key"] == "stable-official-prefix"
    assert payload["tools"] == [namespace]
    assert "__codexhub_" not in first.decode("utf-8")
    assert "_runtime_tool_compatibility_plan" not in first_context
    assert "_runtime_tool_compatibility_plan" not in second_context


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


def _runtime_attempt_context() -> tuple[dict, str]:
    context: dict = {}
    request_payload = json.loads(
        codex_proxy.compatible_request_body(
            _request(
                [{
                    "type": "namespace",
                    "name": "vendor",
                    "tools": [{"type": "function", "name": "run", "parameters": {}}],
                }]
            ),
            _external_chat_upstream(),
            event_context=context,
            inject_codex_tools=False,
        )
    )
    return context, request_payload["tools"][0]["name"]


def _send_runtime_sse_event(context: dict, event: dict) -> bytes:
    return codex_proxy.compatible_sse_line(
        b"data: " + json.dumps(event).encode("utf-8") + b"\n",
        "custom_endpoint",
        event_context=context,
    )


def test_retry_attempt_generation_accepts_a_second_terminal_responses_stream():
    context, _alias = _runtime_attempt_context()
    context["_runtime_tool_compatibility_attempt_generation"] = 1

    _send_runtime_sse_event(
        context,
        {"type": "response.created", "response": {"id": "first"}},
    )
    first_terminal = _send_runtime_sse_event(
        context,
        {
            "type": "response.completed",
            "response": {"id": "first", "status": "completed", "output": []},
        },
    )
    assert first_terminal

    # The first terminal is the lifecycle-final response that permits a retry.
    context["_runtime_tool_compatibility_attempt_generation"] = 2
    _send_runtime_sse_event(
        context,
        {"type": "response.created", "response": {"id": "second"}},
    )
    second_terminal = _send_runtime_sse_event(
        context,
        {
            "type": "response.completed",
            "response": {"id": "second", "status": "completed", "output": []},
        },
    )
    assert second_terminal


def test_retry_attempt_generation_rebinds_partial_call_identity_for_second_stream():
    context, alias = _runtime_attempt_context()
    context["_runtime_tool_compatibility_attempt_generation"] = 1
    first_item = {
        "type": "function_call",
        "id": "item-reused",
        "call_id": "call-reused",
        "name": alias,
        "arguments": "",
    }
    _send_runtime_sse_event(
        context,
        {"type": "response.output_item.added", "item": first_item},
    )
    _send_runtime_sse_event(
        context,
        {
            "type": "response.function_call_arguments.delta",
            "item_id": "item-reused",
            "call_id": "call-reused",
            "delta": "{",
        },
    )

    # Simulate transport failure after a partial added/delta.  A permitted
    # retry reuses the provider's call/item identities from the fresh attempt.
    context["_runtime_tool_compatibility_attempt_generation"] = 2
    second_item = {**first_item, "arguments": ""}
    _send_runtime_sse_event(
        context,
        {"type": "response.output_item.added", "item": second_item},
    )
    _send_runtime_sse_event(
        context,
        {
            "type": "response.function_call_arguments.delta",
            "item_id": "item-reused",
            "call_id": "call-reused",
            "delta": "{}",
        },
    )
    _send_runtime_sse_event(
        context,
        {
            "type": "response.function_call_arguments.done",
            "item_id": "item-reused",
            "call_id": "call-reused",
            "arguments": "{}",
        },
    )
    _send_runtime_sse_event(
        context,
        {
            "type": "response.output_item.done",
            "item": {**first_item, "arguments": "{}"},
        },
    )
    terminal = _send_runtime_sse_event(
        context,
        {
            "type": "response.completed",
            "response": {
                "id": "second",
                "status": "completed",
                "output": [{**first_item, "arguments": "{}"}],
            },
        },
    )
    assert terminal


def test_required_tool_restriction_diagnostics_keep_generated_aliases_private(monkeypatch):
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        codex_proxy,
        "write_proxy_event",
        lambda name, **fields: events.append((name, fields)),
    )
    prompt = """
Use the real subagent-driven-development skill.

Execution constraints:
1. The coordinator may read the plan once with node_repl.
2. Spawn exactly one implementer, wait for it, close it, then spawn exactly one spec reviewer, wait for it, close it, then spawn exactly one code-quality reviewer, wait for it, and close it.
"""
    context = {"request_id": "req-diagnostics", "repair_policy": codex_proxy.REPAIR_CODEX_SUBAGENT}
    with monkeypatch.context() as patches:
        # Keep both declarations on the synthetic surface so the required-tool
        # restriction itself (rather than coordinator filtering) is exercised.
        patches.setattr(codex_proxy, "_filter_tools_for_subagent_coordinator", lambda *args, **kwargs: False)
        patches.setattr(codex_proxy, "_inject_explicit_codex_tools", lambda *args, **kwargs: False)
        patches.setattr(
            codex_proxy,
            "_runtime_alias_for_namespace_child",
            lambda *args, **kwargs: "__codexhub_ns_generated_1",
        )
        transformed = codex_proxy.compatible_request_body(
            json.dumps(
                {
                    "model": "custom-model",
                    "input": [{"type": "message", "role": "user", "content": prompt}],
                    "tools": [
                        {
                            "type": "function",
                            "name": "__codexhub_ns_generated_1",
                            "parameters": {},
                        },
                        {"type": "function", "name": "other", "parameters": {}},
                    ],
                }
            ).encode("utf-8"),
            {
                "name": "custom_endpoint",
                "upstream_format": "chat_completions",
                "tool_protocol": "chat_tools",
            },
            event_context=context,
        )

    restricted = [fields for name, fields in events if name == "required_tool_tools_restricted"]
    assert restricted
    fields = restricted[-1]
    assert set(fields) == {
        "tool_choice_required",
        "required_tool_family",
        "required_tool_disposition",
    }
    assert fields["tool_choice_required"] is True
    assert fields["required_tool_family"] in {"namespace", "plain_function", "unknown"}
    assert "__codexhub_" not in json.dumps(fields)
    assert json.loads(transformed)["tool_choice"]["type"] == "function"


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
    tools = [_collaboration_namespace(COLLABORATION_V2)]
    history = [
        {
            "type": "function_call",
            "id": "item_v2",
            "namespace": "collaboration",
            "name": "followup_task",
            "call_id": "call_v2",
            "arguments": '{"target":"/root/a","message":"continue"}',
        },
        {
            "type": "function_call_output",
            "id": "item_v2_output",
            "call_id": "call_v2",
            "output": "null",
        },
    ]

    payload = json.loads(
        codex_proxy.compatible_request_body(
            json.dumps(
                {
                    "model": "custom-model",
                    "input": history,
                    "tools": tools,
                    "tool_choice": "auto",
                }
            ).encode("utf-8"),
            _external_responses_upstream(),
            event_context=context,
        )
    )

    aliases = [tool["name"] for tool in payload["tools"] if tool.get("type") == "function"]
    assert len(aliases) == 6
    assert all(alias.startswith("__codexhub_ns_") for alias in aliases)
    assert payload["input"][0]["name"] in aliases
    assert "namespace" not in payload["input"][0]
    assert json.loads(payload["input"][0]["arguments"])["target"] == "/root/a"
    assert context["collaboration_protocol"] == COLLABORATION_V2
    assert not any(name.startswith("multi_agent_v1__") for name in aliases)


@pytest.mark.parametrize(
    ("current_namespace", "foreign_history"),
    [
        (
            "multi_agent_v1",
            [
                {
                    "type": "function_call",
                    "namespace": "collaboration",
                    "name": "followup_task",
                    "call_id": "old-v2-call",
                    "arguments": '{"task_name":"old","fork_turns":"all"}',
                },
                {
                    "type": "function_call_output",
                    "call_id": "old-v2-call",
                    "output": "done",
                },
            ],
        ),
        (
            "collaboration",
            [
                {
                    "type": "function_call",
                    "namespace": "multi_agent_v1",
                    "name": "spawn_agent",
                    "call_id": "old-v1-call",
                    "arguments": '{"agent_type":"general","message":"old"}',
                },
                {
                    "type": "function_call_output",
                    "call_id": "old-v1-call",
                    "output": "done",
                },
            ],
        ),
    ],
    ids=["current-v1-foreign-v2", "current-v2-foreign-v1"],
)
def test_gateway_rejects_foreign_collaboration_history_before_planning(
    current_namespace: str,
    foreign_history: list[dict],
) -> None:
    body = {
        "model": "custom-model",
        "input": foreign_history,
        "tool_choice": "auto",
        "tools": [
            _collaboration_namespace(
                COLLABORATION_V1
                if current_namespace == "multi_agent_v1"
                else COLLABORATION_V2
            )
        ],
    }

    context: dict = {}
    with patch.object(codex_proxy, "_prepare_runtime_tool_compatibility") as prepare:
        with pytest.raises(codex_proxy.UpstreamProtocolTranslationError) as caught:
            codex_proxy.compatible_request_body(
                json.dumps(body).encode("utf-8"),
                _external_responses_upstream(),
                event_context=context,
                inject_codex_tools=False,
            )

    assert caught.value.cause.code == codex_proxy.COLLABORATION_BOUNDARY_ERROR_CODE
    prepare.assert_not_called()


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
    assert [entry.disposition for entry in entries] == ["adapt", "adapt", "adapt"]
    assert [tool["type"] for tool in payload["tools"]] == ["function", "function", "function"]
    assert payload["tools"][0]["name"].startswith("__codexhub_ns_")
    assert payload["tools"][1]["name"].startswith("__codexhub_custom_")
    assert payload["tools"][2]["name"].startswith("__codexhub_search_")


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


def test_deferred_core_v2_keeps_collaboration_core_without_expanding_other_namespaces():
    collaboration = _collaboration_namespace(COLLABORATION_V2)
    vendor = {
        "type": "namespace",
        "name": "vendor",
        "tools": [
            {"type": "function", "name": f"child_{index:03d}", "parameters": {"type": "object"}}
            for index in range(249)
        ],
    }
    context: dict = {}

    with patch.object(codex_proxy, "write_proxy_event") as write_proxy_event:
        payload = json.loads(
            codex_proxy.compatible_request_body(
                _request([collaboration, vendor]),
                {**_external_responses_upstream(), "tool_surface_strategy": "deferred_core"},
                event_context=context,
            )
        )

    plan = context["_runtime_tool_compatibility_plan"]
    namespace_entries = [entry for entry in plan.entries if entry.family == "namespace"]
    assert len(namespace_entries) == 1
    assert namespace_entries[0].namespace == "collaboration"
    assert len(namespace_entries[0].aliases) == len(EXPECTED_PARAMETER_SCHEMAS[COLLABORATION_V2])
    assert len(payload["tools"]) == len(EXPECTED_PARAMETER_SCHEMAS[COLLABORATION_V2])
    assert not any("child_" in tool.get("name", "") for tool in payload["tools"])
    surface_event = next(
        call.kwargs
        for call in write_proxy_event.call_args_list
        if call.args and call.args[0] == "external_tool_surface_prepared"
    )
    assert surface_event == {
        "tool_surface_strategy": "deferred_core",
        "namespace_declaration_count": 1,
        "eager_tool_count": 0,
        "retained_core_count": 0,
        "deferred_tool_count": 249,
        "final_tool_count": len(EXPECTED_PARAMETER_SCHEMAS[COLLABORATION_V2]),
    }


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


def test_text_compat_does_not_expose_injected_codex_functions():
    body = json.dumps(
        {
            "model": "third-party-model",
            "input": "spawn a child",
        }
    ).encode("utf-8")
    payload = json.loads(
        codex_proxy.compatible_request_body(
            body,
            {
                "name": "ollama_cloud",
                "tool_protocol": "text_compat",
                "tool_surface_strategy": "eager",
            },
            event_context={"request_id": "text-compat-injection"},
            inject_codex_tools=True,
        )
    )

    assert not any(
        isinstance(tool, dict) and tool.get("type") == "function"
        for tool in payload.get("tools", [])
    )
