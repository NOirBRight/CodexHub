from __future__ import annotations

import hashlib
import json

import pytest

from runtime_tool_compatibility import (
    ADAPT,
    CompatibilityStreamState,
    CUSTOM_FREEFORM,
    NATIVE,
    OMIT,
    PLAIN_FUNCTION,
    ProtocolCapabilities,
    SELECTED_PROVIDER_HOSTED,
    ToolCompatibilityError,
    build_tool_compatibility_plan,
)


def _namespace(namespace: str = "collaboration", *, child: str = "spawn_agent") -> dict:
    return {
        "type": "namespace",
        "name": namespace,
        "tools": [{"type": "function", "name": child}],
    }


def _adapted_namespace_plan(declaration: dict | None = None):
    return build_tool_compatibility_plan(
        [declaration or _namespace("vendor", child="run")],
        selected_protocol="chat_tools",
        protocol_capabilities=ProtocolCapabilities.chat_tools(),
        request_token="boundary",
    )


def _native_plan(declaration: dict):
    return build_tool_compatibility_plan(
        [declaration],
        selected_protocol="responses_structured",
        protocol_capabilities=ProtocolCapabilities.responses_structured(),
        request_token="native-boundary",
    )


def _native_hosted_plan():
    return build_tool_compatibility_plan(
        [{"type": "web_search"}],
        selected_protocol="responses_structured",
        provider_hosted_capabilities={"web_search": True},
        protocol_capabilities=ProtocolCapabilities.responses_structured(
            hosted_lifecycles=frozenset({"web_search"}),
        ),
        request_token="native-hosted-boundary",
    )


def test_adapted_decode_rejects_orphan_and_duplicate_result_call_ids():
    orphan_plan = _adapted_namespace_plan()
    with pytest.raises(ToolCompatibilityError):
        orphan_plan.decode_payload(
            {
                "output": [
                    {"type": "function_call_output", "call_id": "orphan", "output": "ignored"}
                ]
            }
        )

    duplicate_plan = _adapted_namespace_plan()
    with pytest.raises(ToolCompatibilityError):
        duplicate_plan.decode_payload(
            {
                "output": [
                    {"type": "function_call_output", "call_id": "result", "output": "one"},
                    {"type": "function_call_output", "call_id": "result", "output": "two"},
                ]
            }
        )


@pytest.mark.parametrize(
    "history",
    [
        [
            {"type": "function_call", "namespace": "vendor", "name": "run", "arguments": "{}"},
            {"type": "function_call_output", "output": "orphan"},
        ],
        [
            {"type": "function_call", "namespace": "vendor", "name": "run", "call_id": "same", "arguments": "{}"},
            {"type": "function_call", "name": "keep", "call_id": "same", "arguments": "{}"},
        ],
    ],
    ids=["missing-omitted-call-id", "omitted-id-conflicts-with-retained-call"],
)
def test_omit_history_rejects_missing_or_conflicting_call_identity(history):
    plan = build_tool_compatibility_plan(
        [
            _namespace("vendor", child="run"),
            {"type": "function", "name": "keep"},
        ],
        selected_protocol="chat_tools",
        protocol_capabilities=ProtocolCapabilities(
            function_lifecycle=True,
            namespace_lifecycle=False,
            custom_lifecycle=False,
            tool_search_lifecycle=False,
            accepts_namespace_adapter=False,
            accepts_custom_adapter=False,
        ),
        request_token="omit-boundary",
    )
    with pytest.raises(ToolCompatibilityError):
        plan.encode_payload({"input": history})


@pytest.mark.parametrize(
    "event",
    [
        {
            "type": "response.function_call_arguments.delta",
            "item_id": "unknown-item",
            "delta": "{}",
        },
        {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "name": "__codexhub_ns_unknown_1",
                "item_id": "unknown-item",
                "call_id": "unknown-call",
                "arguments": "{}",
            },
        },
    ],
    ids=["unknown-delta-item", "unknown-done-alias"],
)
def test_adapted_namespace_sse_rejects_unknown_identity(event):
    plan = _adapted_namespace_plan()
    state = CompatibilityStreamState(plan)
    with pytest.raises(ToolCompatibilityError):
        state.decode_events_for_event(event)


@pytest.mark.parametrize(
    "added, done",
    [
        (
            {
                "type": "response.output_item.added",
                "item": {
                    "type": "function_call",
                    "name": "__codexhub_ns_ignored",
                    "item_id": "item",
                    "call_id": "call",
                    "arguments": "",
                },
            },
            None,
        ),
        (
            {
                "type": "response.output_item.added",
                "item": {
                    "type": "function_call",
                    "name": "__codexhub_ns_ignored",
                    "item_id": "item",
                    "call_id": "call",
                    "arguments": "",
                },
            },
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "name": "__codexhub_ns_ignored",
                    "item_id": "item",
                    "call_id": "other-call",
                    "arguments": "{}",
                },
            },
        ),
    ],
    ids=["missing-call-id-on-added", "mismatched-call-id-on-done"],
)
def test_adapted_namespace_sse_rejects_missing_or_mismatched_ids(added, done):
    plan = _adapted_namespace_plan()
    alias = plan.entries[0].aliases[0]
    added["item"]["name"] = alias
    if done is not None:
        done["item"]["name"] = alias
    if done is None:
        added["item"].pop("call_id")
    state = CompatibilityStreamState(plan)
    with pytest.raises(ToolCompatibilityError):
        state.decode_events_for_event(added)
        if done is not None:
            state.decode_events_for_event(done)


def test_adapted_namespace_sse_rejects_done_for_unknown_item_id():
    plan = _adapted_namespace_plan()
    alias = plan.entries[0].aliases[0]
    state = CompatibilityStreamState(plan)
    state.decode_events_for_event(
        {
            "type": "response.output_item.added",
            "item": {
                "type": "function_call",
                "name": alias,
                "item_id": "item",
                "call_id": "call",
                "arguments": "",
            },
        }
    )
    with pytest.raises(ToolCompatibilityError):
        state.decode_events_for_event(
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "name": alias,
                    "item_id": "other-item",
                    "call_id": "other-call",
                    "arguments": "{}",
                },
            }
        )


@pytest.mark.parametrize(
    "declaration",
    [
        {"type": "namespace", "name": "vendor", "tools": []},
        {
            "type": "namespace",
            "name": "vendor",
            "tools": [{"type": "function", "name": "run"}, {"type": "function", "name": "run"}],
        },
    ],
    ids=["missing-child", "ambiguous-duplicate-child"],
)
def test_namespace_boundary_rejects_missing_or_ambiguous_children(declaration):
    with pytest.raises(ToolCompatibilityError):
        build_tool_compatibility_plan(
            [declaration],
            selected_protocol="chat_tools",
            protocol_capabilities=ProtocolCapabilities.chat_tools(),
            request_token="namespace-boundary",
        )


@pytest.mark.parametrize(
    "item",
    [
        {"type": "function_call", "name": "run", "call_id": "call", "arguments": "{}"},
        {
            "type": "function_call",
            "name": "__codexhub_ns_invalid_1",
            "namespace": "wrong",
            "call_id": "call",
            "arguments": "{}",
        },
    ],
    ids=["flattened-original-name", "alias-with-wrong-namespace"],
)
def test_adapted_namespace_rejects_original_or_flattened_alias_without_exact_mapping(item):
    plan = _adapted_namespace_plan()
    if item["name"].startswith("__codexhub_ns_"):
        item["name"] = plan.entries[0].aliases[0]
    with pytest.raises(ToolCompatibilityError):
        plan.decode_payload({"output": [item]})


@pytest.mark.parametrize(
    "namespace, forbidden",
    [("multi_agent_v1", "task_path"), ("collaboration", "agent_id")],
    ids=["v1-json-arguments", "v2-json-arguments"],
)
def test_v1_v2_forbidden_fields_inside_json_arguments_fail(namespace, forbidden):
    declaration = _namespace(namespace, child="spawn_agent")
    plan = _adapted_namespace_plan(declaration)
    alias = plan.entries[0].aliases[0]
    with pytest.raises(ToolCompatibilityError):
        plan.encode_payload(
            {
                "input": [
                    {
                        "type": "function_call",
                        "namespace": namespace,
                        "name": "spawn_agent",
                        "call_id": "call",
                        "arguments": json.dumps({forbidden: "mixed"}),
                    }
                ],
                "tools": [declaration],
                "tool_choice": {"type": "function", "name": "spawn_agent"},
            }
        )


def test_adapted_namespace_tool_choice_with_duplicate_child_name_fails_preflight():
    declaration = {
        "type": "namespace",
        "name": "vendor",
        "tools": [{"type": "function", "name": "run"}, {"type": "function", "name": "run"}],
    }
    with pytest.raises(ToolCompatibilityError):
        _adapted_namespace_plan(declaration)


@pytest.mark.parametrize(
    "other_declaration",
    [
        {"type": "web_search", "name": "placeholder", "executor": "selected_provider"},
        {"type": "future_kind", "name": "placeholder"},
    ],
    ids=["hosted-name", "unknown-name"],
)
def test_namespace_alias_reserves_names_of_hosted_and_unknown_declarations(other_declaration):
    token = hashlib.sha256(b"alias-collision").hexdigest()[:10]
    candidate = f"__codexhub_ns_{token}_1"
    other_declaration["name"] = candidate
    plan = build_tool_compatibility_plan(
        [_namespace("vendor", child="run"), other_declaration],
        selected_protocol="chat_tools",
        protocol_capabilities=ProtocolCapabilities.chat_tools(),
        request_token="alias-collision",
    )
    assert plan.entries[0].aliases[0] != candidate


@pytest.mark.parametrize(
    "declarations",
    [
        [None],
        ["not-a-declaration"],
        [{"type": "namespace", "name": "vendor", "tools": "not-a-list"}],
        [{"type": "custom", "name": "paint"}],
    ],
    ids=["none", "string", "namespace-malformed", "custom-malformed"],
)
def test_non_mapping_and_malformed_declarations_fail_closed(declarations):
    with pytest.raises(ToolCompatibilityError):
        build_tool_compatibility_plan(
            declarations,
            selected_protocol="chat_tools",
            protocol_capabilities=ProtocolCapabilities.chat_tools(),
            request_token="malformed-boundary",
        )


def _native_declaration_and_missing_body_item(family: str) -> tuple[dict, dict]:
    if family == "plain":
        return {"type": "function", "name": "plain"}, {
            "type": "function_call",
            "id": "item",
            "name": "plain",
            "arguments": "{}",
        }
    if family == "namespace":
        return _namespace("vendor", child="run"), {
            "type": "function_call",
            "id": "item",
            "namespace": "vendor",
            "name": "run",
            "arguments": "{}",
        }
    if family == "custom":
        return {"type": "custom", "name": "paint", "format": {"type": "text"}}, {
            "type": "custom_tool_call",
            "id": "item",
            "name": "paint",
            "input": "opaque",
        }
    if family == "tool_search":
        return {"type": "tool_search", "execution": "client"}, {
            "type": "tool_search_call",
            "id": "item",
            "arguments": {"query": "find"},
        }
    raise AssertionError(family)


@pytest.mark.parametrize("family", ["plain", "namespace", "custom", "tool_search"])
def test_native_known_families_reject_missing_call_identity_in_body(family):
    declaration, item = _native_declaration_and_missing_body_item(family)
    plan = _native_plan(declaration)
    with pytest.raises(ToolCompatibilityError):
        plan.decode_payload({"output": [item]})


@pytest.mark.parametrize("family", ["plain", "namespace", "custom", "tool_search"])
def test_native_known_families_reject_missing_item_identity_in_sse(family):
    declaration, item = _native_declaration_and_missing_body_item(family)
    item.pop("id")
    item["call_id"] = "call"
    plan = _native_plan(declaration)
    state = CompatibilityStreamState(plan)
    with pytest.raises(ToolCompatibilityError):
        state.decode_events_for_event(
            {
                "type": "response.output_item.added",
                "item": item,
            }
        )


@pytest.mark.parametrize("family", ["plain", "namespace", "custom", "tool_search"])
def test_native_known_families_reject_duplicate_item_identity_in_sse(family):
    declaration, item = _native_declaration_and_missing_body_item(family)
    item["call_id"] = "call"
    plan = _native_plan(declaration)
    state = CompatibilityStreamState(plan)
    added = {"type": "response.output_item.added", "item": item}
    with pytest.raises(ToolCompatibilityError):
        state.decode_events_for_event(added)
        state.decode_events_for_event({"type": "response.output_item.added", "item": dict(item)})


def test_native_tool_search_rejects_duplicate_call_identity_in_body():
    declaration, item = _native_declaration_and_missing_body_item("tool_search")
    first = {**item, "call_id": "same"}
    second = {**item, "call_id": "same"}
    plan = _native_plan(declaration)
    with pytest.raises(ToolCompatibilityError):
        plan.decode_payload({"output": [first, second]})


def test_native_custom_sse_done_requires_string_input_and_matching_call_identity():
    declaration, item = _native_declaration_and_missing_body_item("custom")
    item["call_id"] = "call"
    plan = _native_plan(declaration)

    def added_event():
        return {"type": "response.output_item.added", "item": dict(item)}

    invalid_input = CompatibilityStreamState(plan)
    invalid_input.decode_events_for_event(added_event())
    with pytest.raises(ToolCompatibilityError):
        invalid_input.decode_events_for_event(
            {
                "type": "response.custom_tool_call_input.done",
                "item_id": "item",
                "call_id": "call",
                "input": {"not": "text"},
            }
        )

    mismatched_identity = CompatibilityStreamState(plan)
    mismatched_identity.decode_events_for_event(added_event())
    with pytest.raises(ToolCompatibilityError):
        mismatched_identity.decode_events_for_event(
            {
                "type": "response.custom_tool_call_input.done",
                "item_id": "item",
                "call_id": "different-call",
                "input": "opaque",
            }
        )


def test_custom_and_plain_same_name_resolve_history_by_item_family():
    declarations = [
        {"type": "function", "name": "paint"},
        {"type": "custom", "name": "paint", "format": {"type": "text"}},
    ]
    plan = build_tool_compatibility_plan(
        declarations,
        selected_protocol="chat_tools",
        protocol_capabilities=ProtocolCapabilities.chat_tools(),
        request_token="custom-plain-collision",
    )
    encoded = plan.encode_payload(
        {
            "tools": declarations,
            "input": [
                {
                    "type": "custom_tool_call",
                    "name": "paint",
                    "call_id": "custom-call",
                    "input": "opaque",
                },
                {
                    "type": "function_call",
                    "name": "paint",
                    "call_id": "plain-call",
                    "arguments": "{}",
                },
            ],
        }
    )
    assert encoded["input"][0]["type"] == "function_call"
    assert encoded["input"][0]["name"].startswith("__codexhub_custom_")
    assert encoded["input"][1]["type"] == "function_call"
    assert encoded["input"][1]["name"] == "paint"


def test_adapted_custom_same_name_rejects_native_custom_body_and_sse():
    plan = build_tool_compatibility_plan(
        [
            {"type": "function", "name": "paint"},
            {"type": "custom", "name": "paint", "format": {"type": "text"}},
        ],
        selected_protocol="chat_tools",
        protocol_capabilities=ProtocolCapabilities.chat_tools(),
        request_token="adapted-custom-native-collision",
    )
    with pytest.raises(ToolCompatibilityError):
        plan.decode_payload(
            {
                "output": [
                    {
                        "type": "custom_tool_call",
                        "name": "paint",
                        "call_id": "native-custom-call",
                        "id": "native-custom-item",
                        "input": "opaque",
                    }
                ]
            }
        )

    state = CompatibilityStreamState(plan)
    with pytest.raises(ToolCompatibilityError):
        state.decode_events_for_event(
            {
                "type": "response.output_item.added",
                "item": {
                    "type": "custom_tool_call",
                    "name": "paint",
                    "call_id": "native-custom-call",
                    "id": "native-custom-item",
                },
            }
        )


def test_duplicate_custom_names_have_unique_aliases_but_native_history_is_ambiguous():
    declarations = [
        {"type": "custom", "name": "paint", "format": {"type": "text"}},
        {"type": "custom", "name": "paint", "format": {"type": "text"}},
    ]
    adapted = build_tool_compatibility_plan(
        declarations,
        selected_protocol="chat_tools",
        protocol_capabilities=ProtocolCapabilities.chat_tools(),
        request_token="duplicate-custom-adapted",
    )
    assert adapted.entries[0].aliases[0] != adapted.entries[1].aliases[0]
    with pytest.raises(ToolCompatibilityError):
        adapted.encode_payload(
            {
                "input": [
                    {
                        "type": "custom_tool_call",
                        "name": "paint",
                        "call_id": "custom-call",
                        "input": "opaque",
                    }
                ]
            }
        )

    native = build_tool_compatibility_plan(
        declarations,
        selected_protocol="responses_structured",
        protocol_capabilities=ProtocolCapabilities.responses_structured(),
        request_token="duplicate-custom-native",
    )
    with pytest.raises(ToolCompatibilityError):
        native.decode_payload(
            {
                "output": [
                    {
                        "type": "custom_tool_call",
                        "name": "paint",
                        "call_id": "custom-call",
                        "id": "custom-item",
                        "input": "opaque",
                    }
                ]
            }
        )


def test_unknown_selected_provider_kind_is_omitted_even_with_explicit_facts():
    plan = build_tool_compatibility_plan(
        [{"type": "vendor_search", "executor": "selected_provider"}],
        selected_protocol="responses_structured",
        provider_hosted_capabilities={"vendor_search": True},
        protocol_capabilities=ProtocolCapabilities.responses_structured(
            hosted_lifecycles=frozenset({"vendor_search"}),
        ),
        request_token="unknown-hosted-kind",
    )
    assert plan.entries[0].family != SELECTED_PROVIDER_HOSTED
    assert plan.entries[0].disposition == "omit"


def test_native_hosted_body_requires_completed_item_identity():
    plan = _native_hosted_plan()
    valid = {"type": "web_search_call", "id": "search-item", "status": "completed"}
    assert plan.decode_payload({"output": [valid]})["output"] == [valid]

    with pytest.raises(ToolCompatibilityError):
        plan.decode_payload({"output": [{"type": "web_search_call", "status": "completed"}]})
    with pytest.raises(ToolCompatibilityError):
        plan.decode_payload({"output": [valid, dict(valid)]})


def test_native_hosted_sse_tracks_added_done_and_terminal_identity():
    def added(item_id: str = "search-item"):
        return {
            "type": "response.output_item.added",
            "item": {"type": "web_search_call", "id": item_id, "status": "in_progress"},
        }

    done = {
        "type": "response.output_item.done",
        "item": {"type": "web_search_call", "id": "search-item", "status": "completed"},
    }
    terminal = {"type": "response.completed", "response": {"id": "response"}}
    state = CompatibilityStreamState(_native_hosted_plan())
    state.decode_events_for_event(added())
    state.decode_events_for_event(
        {"type": "response.web_search_call.in_progress", "item_id": "search-item"}
    )
    state.decode_events_for_event(
        {"type": "response.web_search_call.searching", "item_id": "search-item"}
    )
    state.decode_events_for_event(
        {"type": "response.web_search_call.completed", "item_id": "search-item"}
    )
    state.decode_events_for_event(done)
    state.decode_events_for_event(terminal)

    missing = CompatibilityStreamState(_native_hosted_plan())
    with pytest.raises(ToolCompatibilityError):
        missing.decode_events_for_event(added(item_id=""))

    mismatched = CompatibilityStreamState(_native_hosted_plan())
    mismatched.decode_events_for_event(added())
    with pytest.raises(ToolCompatibilityError):
        mismatched.decode_events_for_event(
            {
                "type": "response.output_item.done",
                "item": {"type": "web_search_call", "id": "other-item", "status": "completed"},
            }
        )

    incomplete = CompatibilityStreamState(_native_hosted_plan())
    incomplete.decode_events_for_event(added())
    with pytest.raises(ToolCompatibilityError):
        incomplete.decode_events_for_event(terminal)

    unsupported_delta = CompatibilityStreamState(_native_hosted_plan())
    with pytest.raises(ToolCompatibilityError):
        unsupported_delta.decode_events_for_event(
            {"type": "response.web_search_call.delta", "item_id": "search-item", "delta": "opaque"}
        )


def test_native_hosted_sse_accepts_only_the_exact_web_search_stage_sequence():
    state = CompatibilityStreamState(_native_hosted_plan())
    state.decode_events_for_event(
        {
            "type": "response.output_item.added",
            "item": {"type": "web_search_call", "id": "search-item", "status": "in_progress"},
        }
    )
    state.decode_events_for_event(
        {"type": "response.web_search_call.in_progress", "item_id": "search-item"}
    )
    state.decode_events_for_event(
        {"type": "response.web_search_call.searching", "item_id": "search-item"}
    )
    state.decode_events_for_event(
        {"type": "response.web_search_call.completed", "item_id": "search-item"}
    )
    state.decode_events_for_event(
        {
            "type": "response.output_item.done",
            "item": {"type": "web_search_call", "id": "search-item", "status": "completed"},
        }
    )
    state.decode_events_for_event({"type": "response.completed", "response": {"id": "response"}})

    out_of_order = CompatibilityStreamState(_native_hosted_plan())
    out_of_order.decode_events_for_event(
        {
            "type": "response.output_item.added",
            "item": {"type": "web_search_call", "id": "search-item", "status": "in_progress"},
        }
    )
    with pytest.raises(ToolCompatibilityError):
        out_of_order.decode_events_for_event(
            {"type": "response.web_search_call.searching", "item_id": "search-item"}
        )
    with pytest.raises(ToolCompatibilityError):
        out_of_order.decode_events_for_event(
            {"type": "response.web_search_call.in_progress", "item_id": "other-item"}
        )


def test_hosted_kinds_without_a_static_event_contract_are_not_native():
    plan = build_tool_compatibility_plan(
        [{"type": "computer_use_preview"}],
        selected_protocol="responses_structured",
        provider_hosted_capabilities={"computer_use_preview": True},
        protocol_capabilities=ProtocolCapabilities.responses_structured(
            hosted_lifecycles=frozenset({"computer_use_preview"}),
        ),
        request_token="unmapped-hosted-kind",
    )
    assert plan.entries[0].disposition == "omit"

    with pytest.raises(Exception):
        build_tool_compatibility_plan(
            [{"type": "computer_use_preview"}],
            selected_protocol="responses_structured",
            provider_hosted_capabilities={"computer_use_preview": True},
            protocol_capabilities=ProtocolCapabilities.responses_structured(
                hosted_lifecycles=frozenset({"computer_use_preview"}),
            ),
            required=True,
            request_token="required-unmapped-hosted-kind",
        )


def test_optional_unsupported_hosted_history_omits_item_and_same_kind_output_by_item_id():
    plan = build_tool_compatibility_plan(
        [{"type": "web_search"}],
        selected_protocol="chat_tools",
        provider_hosted_capabilities={},
        protocol_capabilities=ProtocolCapabilities.chat_tools(),
        request_token="omitted-hosted-history",
    )
    encoded = plan.encode_payload(
        {
            "tools": [{"type": "web_search"}],
            "input": [
                {"type": "web_search_call", "id": "search-item", "status": "completed"},
                {"type": "web_search_call_output", "id": "search-item", "output": "ignored"},
                {"type": "message", "role": "user", "content": "keep"},
            ],
        }
    )
    assert encoded["input"] == [{"type": "message", "role": "user", "content": "keep"}]

    with pytest.raises(ToolCompatibilityError):
        plan.encode_payload(
            {
                "input": [{"type": "web_search_call", "status": "completed"}],
            }
        )
    with pytest.raises(ToolCompatibilityError):
        plan.encode_payload(
            {
                "input": [
                    {"type": "web_search_call", "id": "duplicate", "status": "completed"},
                    {"type": "web_search_call", "id": "duplicate", "status": "completed"},
                ],
            }
        )


def test_optional_hosted_history_omits_same_type_call_and_result_with_distinct_item_ids():
    plan = build_tool_compatibility_plan(
        [{"type": "web_search"}],
        selected_protocol="chat_tools",
        provider_hosted_capabilities={},
        protocol_capabilities=ProtocolCapabilities.chat_tools(),
        request_token="omitted-hosted-distinct-item-ids",
    )
    encoded = plan.encode_payload(
        {
            "input": [
                {
                    "type": "web_search_call",
                    "item_id": "item_call_hosted_001",
                    "status": "completed",
                    "action": {"query": "ignored"},
                },
                {
                    "type": "web_search_call",
                    "item_id": "item_output_hosted_001",
                    "status": "completed",
                    "provider_scope": "selected_provider_only",
                },
                {"type": "message", "role": "user", "content": "keep"},
            ],
        }
    )
    assert encoded["input"] == [{"type": "message", "role": "user", "content": "keep"}]


def test_optional_unknown_selected_provider_hosted_history_omits_without_call_id():
    plan = build_tool_compatibility_plan(
        [{"type": "vendor_search", "executor": "selected_provider"}],
        selected_protocol="responses_structured",
        provider_hosted_capabilities={},
        protocol_capabilities=ProtocolCapabilities.responses_structured(),
        request_token="omitted-unknown-hosted-history",
    )
    encoded = plan.encode_payload(
        {
            "input": [
                {"type": "vendor_search_call", "item_id": "vendor-item", "status": "completed"},
                {"type": "message", "role": "user", "content": "keep"},
            ],
        }
    )
    assert encoded["input"] == [{"type": "message", "role": "user", "content": "keep"}]


def test_with_final_declarations_does_not_expose_hosted_without_provider_fact():
    plan = build_tool_compatibility_plan(
        [],
        selected_protocol="responses_structured",
        provider_hosted_capabilities={},
        protocol_capabilities=ProtocolCapabilities.responses_structured(
            hosted_lifecycles=frozenset({"web_search"}),
        ),
        request_token="final-hosted-provider-fact-required",
    )
    final = plan.with_final_declarations(
        [{"type": "web_search", "executor": "selected_provider"}]
    )
    assert final.entries[0].disposition == OMIT


def test_shared_hosted_event_kind_with_native_and_omitted_entries_fails_closed():
    plan = build_tool_compatibility_plan(
        [{"type": "web_search"}, {"type": "web_search_preview"}],
        selected_protocol="responses_structured",
        provider_hosted_capabilities={"web_search": True},
        protocol_capabilities=ProtocolCapabilities.responses_structured(
            hosted_lifecycles=frozenset({"web_search"}),
        ),
        request_token="mixed-shared-hosted-kind",
    )
    assert [entry.disposition for entry in plan.entries] == [NATIVE, OMIT]

    with pytest.raises(ToolCompatibilityError) as exc_info:
        plan.encode_payload(
            {
                "input": [
                    {"type": "web_search_call", "id": "shared-item", "status": "completed"},
                ],
            }
        )
    assert exc_info.value.classification == "ambiguous_native_identity"


def test_native_plain_wins_over_adapted_custom_with_same_original_name_in_body_and_stream():
    plan = build_tool_compatibility_plan(
        [
            {"type": "function", "name": "paint", "parameters": {"type": "object"}},
            {"type": "custom", "name": "paint", "format": {"type": "text"}},
        ],
        selected_protocol="chat_tools",
        protocol_capabilities=ProtocolCapabilities.chat_tools(),
        request_token="native-plain-adapted-custom-same-name",
    )
    body_item = {
        "type": "function_call",
        "name": "paint",
        "call_id": "plain-call",
        "item_id": "plain-item",
        "arguments": "{}",
    }
    assert plan.decode_payload({"output": [body_item]})["output"] == [body_item]

    state = CompatibilityStreamState(plan)
    added = {
        "type": "response.output_item.added",
        "item": dict(body_item, arguments=""),
    }
    assert state.decode_events_for_event(added) == [added]


def test_optional_unmapped_hosted_history_omits_item_and_same_kind_output_by_item_id():
    plan = build_tool_compatibility_plan(
        [{"type": "computer_use_preview"}],
        selected_protocol="chat_tools",
        provider_hosted_capabilities={},
        protocol_capabilities=ProtocolCapabilities.chat_tools(),
        request_token="omitted-unmapped-hosted-history",
    )
    encoded = plan.encode_payload(
        {
            "input": [
                {"type": "computer_use_preview_call", "id": "computer-item", "status": "completed"},
                {
                    "type": "computer_use_preview_call_output",
                    "id": "computer-item",
                    "output": {"ignored": True},
                },
                {"type": "message", "role": "user", "content": "keep"},
            ],
        }
    )
    assert encoded["input"] == [{"type": "message", "role": "user", "content": "keep"}]


def test_adapted_custom_original_name_cannot_be_used_as_plain_function_call():
    plan = build_tool_compatibility_plan(
        [{"type": "custom", "name": "paint", "format": {"type": "text"}}],
        selected_protocol="chat_tools",
        protocol_capabilities=ProtocolCapabilities.chat_tools(),
        request_token="adapted-custom-original-name",
    )
    with pytest.raises(ToolCompatibilityError):
        plan.decode_payload(
            {
                "output": [
                    {
                        "type": "function_call",
                        "name": "paint",
                        "call_id": "plain-call",
                        "arguments": "{}",
                    }
                ]
            }
        )

    state = CompatibilityStreamState(plan)
    with pytest.raises(ToolCompatibilityError):
        state.decode_events_for_event(
            {
                "type": "response.output_item.added",
                "item": {
                    "type": "function_call",
                    "id": "plain-item",
                    "name": "paint",
                    "call_id": "plain-call",
                    "arguments": "",
                },
            }
        )
