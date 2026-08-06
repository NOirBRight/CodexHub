from __future__ import annotations

import hashlib
import json

import pytest

from codex_semantic_adapter import (
    CollaborationBoundaryError,
    classify_collaboration_payload,
)
from runtime_tool_compatibility import (
    ADAPT,
    CompatibilityStreamState,
    CUSTOM_FREEFORM,
    NATIVE,
    OMIT,
    PLAIN_FUNCTION,
    ProtocolCapabilities,
    SELECTED_PROVIDER_HOSTED,
    RequiredToolUnavailableError,
    ToolCompatibilityError,
    _is_opaque_collaboration_history_item,
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
    ("declarations", "items"),
    [
        (
            [{"type": "function", "name": "keep"}],
            [
                {"type": "function_call", "name": "keep", "call_id": "same", "arguments": "{}"},
                {"type": "function_call", "name": "keep", "call_id": "same", "arguments": "{}"},
            ],
        ),
        (
            [
                {"type": "function", "name": "paint"},
                {"type": "custom", "name": "paint", "format": {"type": "text"}},
            ],
            [
                {"type": "function_call", "name": "paint", "call_id": "same", "arguments": "{}"},
                {"type": "custom_tool_call", "name": "paint", "call_id": "same", "input": "opaque"},
            ],
        ),
    ],
    ids=["same-family", "cross-family"],
)
def test_encode_history_rejects_duplicate_retained_call_identity(declarations, items):
    plan = build_tool_compatibility_plan(
        declarations,
        selected_protocol="responses_structured",
        protocol_capabilities=ProtocolCapabilities.responses_structured(),
        request_token="duplicate-retained-call-id",
    )

    with pytest.raises(ToolCompatibilityError) as exc_info:
        plan.encode_payload({"input": items})

    assert exc_info.value.classification == "duplicate_call_identity"


@pytest.mark.parametrize("retained_first", [True, False], ids=["retained-first", "omitted-first"])
def test_encode_history_rejects_retained_and_omitted_call_identity_in_either_order(retained_first):
    plan = build_tool_compatibility_plan(
        [
            {"type": "function", "name": "keep"},
            {"type": "custom", "name": "omit", "format": {"type": "text"}},
        ],
        selected_protocol="chat_tools",
        protocol_capabilities=ProtocolCapabilities.chat_tools(accepts_custom_adapter=False),
        request_token="retained-omitted-call-id",
    )
    retained = {"type": "function_call", "name": "keep", "call_id": "shared", "arguments": "{}"}
    omitted = {"type": "custom_tool_call", "name": "omit", "call_id": "shared", "input": "opaque"}

    with pytest.raises(ToolCompatibilityError) as exc_info:
        plan.encode_payload({"input": [retained, omitted] if retained_first else [omitted, retained]})

    assert exc_info.value.classification == "ambiguous_call_identity"


def test_decode_rejects_bound_adapter_call_id_reused_by_another_alias_in_body_and_terminal():
    plan = build_tool_compatibility_plan(
        [_namespace("vendor_a", child="run"), _namespace("vendor_b", child="run")],
        selected_protocol="chat_tools",
        protocol_capabilities=ProtocolCapabilities.chat_tools(),
        request_token="bound-call-owner",
    )
    first_alias, second_alias = [entry.aliases[0] for entry in plan.entries]
    first = {
        "type": "function_call",
        "name": first_alias,
        "id": "item-a",
        "call_id": "shared",
        "arguments": "{}",
    }
    second = {**first, "name": second_alias, "id": "item-b"}

    plan.decode_payload({"output": [first]})
    with pytest.raises(ToolCompatibilityError) as exc_info:
        plan.decode_payload({"output": [second]})
    assert exc_info.value.classification == "ambiguous_call_identity"

    state = CompatibilityStreamState(plan)
    state.decode_events_for_event({"type": "response.output_item.added", "item": first})
    state.decode_events_for_event(
        {
            "type": "response.function_call_arguments.done",
            "item_id": "item-a",
            "call_id": "shared",
            "arguments": "{}",
        }
    )
    state.decode_events_for_event(
        {
            "type": "response.output_item.done",
            "item": first,
        }
    )
    with pytest.raises(ToolCompatibilityError) as exc_info:
        state.decode_events_for_event(
            {
                "type": "response.completed",
                "response": {"output": [second]},
            }
        )
    assert exc_info.value.classification == "ambiguous_call_identity"


def test_registered_adapter_call_id_rejects_unknown_output_family_in_body_sse_and_terminal():
    plan = _adapted_namespace_plan()
    alias = plan.entries[0].aliases[0]
    call = {
        "type": "function_call",
        "name": alias,
        "id": "item-adapter",
        "call_id": "adapter-call",
        "arguments": "{}",
    }
    bad_output = {
        "type": "vendor_extension_call_output",
        "id": "vendor-output",
        "call_id": "adapter-call",
        "output": "opaque",
    }

    plan.decode_payload({"output": [call]})
    with pytest.raises(ToolCompatibilityError) as exc_info:
        plan.decode_payload({"output": [bad_output]})
    assert exc_info.value.classification == "ambiguous_call_identity"

    state = CompatibilityStreamState(plan)
    state.decode_events_for_event({"type": "response.output_item.added", "item": call})
    with pytest.raises(ToolCompatibilityError) as exc_info:
        state.decode_events_for_event({"type": "response.output_item.added", "item": bad_output})
    assert exc_info.value.classification == "ambiguous_call_identity"

    state = CompatibilityStreamState(plan)
    state.decode_events_for_event({"type": "response.output_item.added", "item": call})
    state.decode_events_for_event(
        {
            "type": "response.function_call_arguments.done",
            "item_id": "item-adapter",
            "call_id": "adapter-call",
            "arguments": "{}",
        }
    )
    state.decode_events_for_event({"type": "response.output_item.done", "item": call})
    with pytest.raises(ToolCompatibilityError) as exc_info:
        state.decode_events_for_event(
            {
                "type": "response.completed",
                "response": {"output": [bad_output]},
            }
        )
    assert exc_info.value.classification == "ambiguous_call_identity"


@pytest.mark.parametrize(
    ("declaration", "wrong_call"),
    [
        (
            {"type": "function", "name": "declared"},
            {"type": "function_call", "name": "other", "call_id": "wrong", "arguments": "{}"},
        ),
        (
            {"type": "custom", "name": "declared", "format": {"type": "text"}},
            {"type": "custom_tool_call", "name": "other", "call_id": "wrong", "input": "opaque"},
        ),
    ],
    ids=["function", "custom"],
)
def test_omitted_standard_wrong_name_is_rejected_across_history_body_sse_and_terminal(declaration, wrong_call):
    plan = build_tool_compatibility_plan(
        [declaration],
        selected_protocol="none",
        protocol_capabilities=ProtocolCapabilities(),
        request_token="omitted-standard-wrong-name",
    )

    with pytest.raises(ToolCompatibilityError):
        plan.encode_payload({"input": [wrong_call]})
    with pytest.raises(ToolCompatibilityError):
        plan.decode_payload({"output": [wrong_call]})
    with pytest.raises(ToolCompatibilityError):
        CompatibilityStreamState(plan).decode_events_for_event(
            {"type": "response.output_item.added", "item": wrong_call}
        )
    with pytest.raises(ToolCompatibilityError):
        CompatibilityStreamState(plan).decode_events_for_event(
            {"type": "response.completed", "response": {"output": [wrong_call]}}
        )


def test_response_owner_ledger_preserves_native_custom_pair_with_omitted_unknown_custom_tool():
    plan = build_tool_compatibility_plan(
        [
            {"type": "custom", "name": "paint", "format": {"type": "text"}},
            {"type": "custom_tool", "name": "vendor_custom", "executor": "selected_provider"},
        ],
        selected_protocol="responses_structured",
        protocol_capabilities=ProtocolCapabilities.responses_structured(),
        request_token="response-owner-ledger",
    )
    output = [
        {
            "type": "custom_tool_call",
            "name": "paint",
            "id": "native-item",
            "call_id": "native-call",
            "input": "opaque",
        },
        {
            "type": "custom_tool_call_output",
            "id": "native-output",
            "call_id": "native-call",
            "output": "native",
        },
    ]

    assert plan.decode_payload({"output": output})["output"] == output


@pytest.mark.parametrize(
    "output_type",
    ["function_call_output", "vendor_extension_call_output"],
    ids=["adapted-output", "unknown-output"],
)
def test_response_output_before_adapted_call_fails_closed_without_partial_reorder(output_type):
    plan = _adapted_namespace_plan()
    alias = plan.entries[0].aliases[0]
    output = {
        "type": output_type,
        "id": "output-first",
        "call_id": "adapted-call",
        "output": "opaque",
    }
    call = {
        "type": "function_call",
        "id": "call-item",
        "call_id": "adapted-call",
        "name": alias,
        "arguments": "{}",
    }

    with pytest.raises(ToolCompatibilityError):
        plan.decode_payload({"output": [output, call]})
    with pytest.raises(ToolCompatibilityError):
        CompatibilityStreamState(plan).decode_events_for_event(
            {"type": "response.completed", "response": {"output": [output, call]}}
        )


def test_adapted_stream_allows_ordinary_message_output_item_done():
    """Non-tool assistant output has no compatibility owner to reconcile."""
    plan = _adapted_namespace_plan()
    event = {
        "type": "response.output_item.done",
        "item": {
            "type": "message",
            "id": "msg-ordinary",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "OK"}],
        },
    }

    state = CompatibilityStreamState(plan)
    assert state.decode_events_for_event(event) == [event]


@pytest.mark.parametrize("call_id", [None, ""], ids=["missing", "empty"])
def test_encode_history_rejects_retained_call_without_call_identity(call_id):
    plan = _native_plan({"type": "function", "name": "keep"})
    item = {"type": "function_call", "name": "keep", "id": "item", "arguments": "{}"}
    if call_id is not None:
        item["call_id"] = call_id

    with pytest.raises(ToolCompatibilityError) as exc_info:
        plan.encode_payload({"input": [item]})

    assert exc_info.value.classification == "missing_call_identity"


@pytest.mark.parametrize("identity_key", ["id", "item_id"], ids=["id", "item-id"])
def test_encode_history_rejects_duplicate_item_identity_even_with_distinct_call_ids(identity_key):
    plan = _native_plan({"type": "function", "name": "keep"})
    first = {
        "type": "function_call",
        "name": "keep",
        identity_key: "duplicate-item",
        "call_id": "call-a",
        "arguments": "{}",
    }
    second = {**first, "call_id": "call-b"}

    with pytest.raises(ToolCompatibilityError) as exc_info:
        plan.encode_payload({"input": [first, second]})

    assert exc_info.value.classification == "duplicate_item_identity"


def test_encode_history_rejects_duplicate_outputs_with_one_call_id():
    plan = _native_plan({"type": "function", "name": "keep"})
    history = [
        {
            "type": "function_call",
            "name": "keep",
            "id": "call-item",
            "call_id": "call-one",
            "arguments": "{}",
        },
        {"type": "function_call_output", "id": "output-one", "call_id": "call-one", "output": "first"},
        {"type": "function_call_output", "id": "output-two", "call_id": "call-one", "output": "second"},
    ]

    with pytest.raises(ToolCompatibilityError) as exc_info:
        plan.encode_payload({"input": history})

    assert exc_info.value.classification == "duplicate_call_identity"


def _complete_native_stream(plan, *, family: str = "plain") -> tuple[CompatibilityStreamState, dict]:
    declaration, item = _native_declaration_and_missing_body_item(family)
    del declaration
    state = CompatibilityStreamState(plan)
    if family == "plain":
        item["name"] = "keep"
    item = {**item, "id": "native-item", "call_id": "native-call"}
    state.decode_events_for_event({"type": "response.output_item.added", "item": item})
    if family != "tool_search":
        state.decode_events_for_event(
            {
                "type": "response.function_call_arguments.done",
                "item_id": "native-item",
                "call_id": "native-call",
                "arguments": "{}",
            }
        )
    state.decode_events_for_event({"type": "response.output_item.done", "item": item})
    return state, item


@pytest.mark.parametrize(
    "terminal_item",
    [
        {"type": "function_call", "id": "other-item", "call_id": "native-call", "name": "keep", "arguments": "{}"},
        {"type": "function_call", "id": "native-item", "call_id": "other-call", "name": "keep", "arguments": "{}"},
        {"type": "function_call", "id": "native-item", "call_id": "native-call", "name": "other", "arguments": "{}"},
    ],
    ids=["different-item", "different-call", "different-family-name"],
)
def test_native_stream_terminal_output_must_match_pending_item_identity(terminal_item):
    plan = _native_plan({"type": "function", "name": "keep"})
    state, _item = _complete_native_stream(plan)

    with pytest.raises(ToolCompatibilityError):
        state.decode_events_for_event(
            {"type": "response.completed", "response": {"output": [terminal_item]}}
        )


def _complete_adapted_stream(plan) -> tuple[CompatibilityStreamState, dict]:
    alias = plan.entries[0].aliases[0]
    item = {
        "type": "function_call",
        "id": "adapted-item",
        "call_id": "adapted-call",
        "name": alias,
        "arguments": "",
    }
    state = CompatibilityStreamState(plan)
    state.decode_events_for_event({"type": "response.output_item.added", "item": item})
    state.decode_events_for_event(
        {
            "type": "response.function_call_arguments.done",
            "item_id": "adapted-item",
            "call_id": "adapted-call",
            "arguments": '{"__codexhub_custom_input":"opaque"}'
            if plan.entries[0].family == CUSTOM_FREEFORM
            else "{}",
        }
    )
    done = {
        **item,
        "arguments": '{"__codexhub_custom_input":"opaque"}'
        if plan.entries[0].family == CUSTOM_FREEFORM
        else "{}",
    }
    state.decode_events_for_event({"type": "response.output_item.done", "item": done})
    return state, done


def test_native_terminal_matches_exact_declaration_not_only_family():
    plan = build_tool_compatibility_plan(
        [{"type": "function", "name": "one"}, {"type": "function", "name": "two"}],
        selected_protocol="responses_structured",
        protocol_capabilities=ProtocolCapabilities.responses_structured(),
        request_token="native-exact-terminal-owner",
    )
    state = CompatibilityStreamState(plan)
    first = {"type": "function_call", "id": "native-item", "call_id": "native-call", "name": "one", "arguments": ""}
    state.decode_events_for_event({"type": "response.output_item.added", "item": first})
    state.decode_events_for_event(
        {
            "type": "response.function_call_arguments.done",
            "item_id": "native-item",
            "call_id": "native-call",
            "arguments": "{}",
        }
    )
    state.decode_events_for_event(
        {"type": "response.output_item.done", "item": {**first, "arguments": "{}"}}
    )

    with pytest.raises(ToolCompatibilityError):
        state.decode_events_for_event(
            {
                "type": "response.completed",
                "response": {
                    "output": [
                        {
                            "type": "function_call",
                            "id": "native-item",
                            "call_id": "native-call",
                            "name": "two",
                            "arguments": "{}",
                        }
                    ]
                },
            }
        )


def test_native_terminal_cannot_complete_without_pending_native_owner():
    plan = _native_plan({"type": "function", "name": "keep"})
    state, _item = _complete_native_stream(plan)
    with pytest.raises(ToolCompatibilityError):
        state.decode_events_for_event(
            {"type": "response.completed", "response": {"output": []}}
        )


def test_adapted_terminal_item_id_must_match_pending_owner():
    plan = _adapted_namespace_plan()
    state, done = _complete_adapted_stream(plan)
    with pytest.raises(ToolCompatibilityError):
        state.decode_events_for_event(
            {
                "type": "response.completed",
                "response": {"output": [{**done, "id": "different-item"}]},
            }
        )


def test_adapted_terminal_cannot_complete_without_pending_owner():
    plan = _adapted_namespace_plan()
    state, _done = _complete_adapted_stream(plan)
    with pytest.raises(ToolCompatibilityError):
        state.decode_events_for_event(
            {"type": "response.completed", "response": {"output": []}}
        )


def test_buffered_custom_terminal_reconciles_semantic_input_not_only_identity():
    plan = build_tool_compatibility_plan(
        [{"type": "custom", "name": "paint", "format": {"type": "text"}}],
        selected_protocol="chat_tools",
        protocol_capabilities=ProtocolCapabilities.chat_tools(),
        request_token="custom-terminal-payload",
    )
    alias = plan.entries[0].aliases[0]
    state = CompatibilityStreamState(plan)
    added = {
        "type": "response.output_item.added",
        "item": {"type": "function_call", "id": "custom-item", "call_id": "custom-call", "name": alias, "arguments": ""},
    }
    state.decode_events_for_event(added)
    arguments = '{"__codexhub_custom_input":"one"}'
    state.decode_events_for_event(
        {"type": "response.function_call_arguments.done", "item_id": "custom-item", "arguments": arguments}
    )
    done = {
        "type": "response.output_item.done",
        "item": {"type": "function_call", "id": "custom-item", "call_id": "custom-call", "name": alias, "arguments": arguments},
    }
    state.decode_events_for_event(done)

    changed_payload = {**done["item"], "arguments": '{"__codexhub_custom_input":"two"}'}
    with pytest.raises(ToolCompatibilityError):
        state.decode_events_for_event(
            {"type": "response.completed", "response": {"output": [changed_payload]}}
        )


def test_native_terminal_reconciles_semantic_arguments_not_only_identity():
    plan = _native_plan({"type": "function", "name": "keep"})
    state = CompatibilityStreamState(plan)
    first = {"type": "function_call", "id": "native-item", "call_id": "native-call", "name": "keep", "arguments": ""}
    state.decode_events_for_event({"type": "response.output_item.added", "item": first})
    arguments = '{"value":1}'
    state.decode_events_for_event(
        {"type": "response.function_call_arguments.done", "item_id": "native-item", "call_id": "native-call", "arguments": arguments}
    )
    state.decode_events_for_event(
        {"type": "response.output_item.done", "item": {**first, "arguments": arguments}}
    )
    with pytest.raises(ToolCompatibilityError):
        state.decode_events_for_event(
            {"type": "response.completed", "response": {"output": [{**first, "arguments": '{"value":2}'}]}}
        )


@pytest.mark.parametrize("adapted", [False, True], ids=["native", "namespace-adapter"])
def test_arguments_done_must_match_received_delta_fragments(adapted):
    plan = _adapted_namespace_plan() if adapted else _native_plan({"type": "function", "name": "keep"})
    state = CompatibilityStreamState(plan)
    name = plan.entries[0].aliases[0] if adapted else "keep"
    item = {"type": "function_call", "id": "delta-item", "call_id": "delta-call", "name": name, "arguments": ""}
    state.decode_events_for_event({"type": "response.output_item.added", "item": item})
    state.decode_events_for_event(
        {"type": "response.function_call_arguments.delta", "item_id": "delta-item", "call_id": "delta-call", "delta": '{"value":1}' }
    )
    with pytest.raises(ToolCompatibilityError):
        state.decode_events_for_event(
            {"type": "response.function_call_arguments.done", "item_id": "delta-item", "call_id": "delta-call", "arguments": '{"value":2}' }
        )


def test_native_tool_search_body_and_history_preserve_exact_client_execution_marker():
    plan = _native_plan({"type": "tool_search", "execution": "client"})
    call = {"type": "tool_search_call", "id": "search-item", "call_id": "search-call", "execution": "client", "arguments": {"query": "x"}}
    output = {"type": "tool_search_output", "id": "search-output", "call_id": "search-call", "execution": "client", "tools": []}
    assert plan.decode_payload({"output": [call, output]})["output"] == [call, output]
    assert plan.encode_payload({"input": [call, output]})["input"] == [call, output]


@pytest.mark.parametrize(
    "item",
    [
        {"type": "tool_search_call", "id": "search-item", "call_id": "search-call", "arguments": {"query": "x"}},
        {"type": "tool_search_output", "id": "search-output", "call_id": "search-call", "tools": []},
        {"type": "tool_search_output", "id": "search-output", "call_id": "search-call", "execution": "provider", "tools": []},
    ],
    ids=["missing-call-marker", "missing-output-marker", "wrong-output-marker"],
)
def test_native_tool_search_requires_exact_client_execution_marker(item):
    plan = _native_plan({"type": "tool_search", "execution": "client"})
    with pytest.raises(ToolCompatibilityError):
        plan.decode_payload({"output": [item]})


@pytest.mark.parametrize(
    "event",
    [
        {
            "type": "response.function_call_arguments.delta",
            "item_id": "search-item",
            "call_id": "search-call",
            "delta": "opaque",
        },
        {
            "type": "response.function_call_arguments.done",
            "item_id": "search-item",
            "call_id": "search-call",
            "arguments": "opaque",
        },
    ],
    ids=["arguments-delta", "arguments-done"],
)
def test_native_tool_search_stream_rejects_function_argument_lifecycle(event):
    plan = _native_plan({"type": "tool_search", "execution": "client"})
    state = CompatibilityStreamState(plan)
    state.decode_events_for_event(
        {
            "type": "response.output_item.added",
            "item": {
                "type": "tool_search_call",
                "id": "search-item",
                "call_id": "search-call",
                "execution": "client",
                "arguments": {"query": "x"},
            },
        }
    )

    with pytest.raises(ToolCompatibilityError) as exc_info:
        state.decode_events_for_event(event)

    assert exc_info.value.classification == "unsupported_hosted_lifecycle"


@pytest.mark.parametrize("surface", ["done", "terminal"])
def test_native_tool_search_stream_revalidates_client_execution_marker(surface):
    plan = _native_plan({"type": "tool_search", "execution": "client"})
    state = CompatibilityStreamState(plan)
    item = {
        "type": "tool_search_call",
        "id": "search-item",
        "call_id": "search-call",
        "execution": "client",
        "arguments": {"query": "x"},
    }
    state.decode_events_for_event({"type": "response.output_item.added", "item": item})
    changed = {**item, "execution": "provider"}

    with pytest.raises(ToolCompatibilityError) as exc_info:
        if surface == "done":
            state.decode_events_for_event(
                {"type": "response.output_item.done", "item": changed}
            )
        else:
            state.decode_events_for_event(
                {"type": "response.output_item.done", "item": item}
            )
            state.decode_events_for_event(
                {"type": "response.completed", "response": {"output": [changed]}}
            )

    assert exc_info.value.classification == "invalid_tool_search_execution"


def test_native_response_output_before_call_fails_closed():
    plan = _native_plan({"type": "function", "name": "keep"})
    call = {"type": "function_call", "id": "call-item", "call_id": "call", "name": "keep", "arguments": "{}"}
    output = {"type": "function_call_output", "id": "call-output", "call_id": "call", "output": "done"}
    with pytest.raises(ToolCompatibilityError):
        plan.decode_payload({"output": [output, call]})


@pytest.mark.parametrize(
    "event",
    [
        {
            "type": "response.function_call_arguments.done",
            "item_id": "orphan-item",
            "call_id": "orphan-call",
            "arguments": "{}",
        },
        {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "id": "orphan-item",
                "call_id": "orphan-call",
                "name": "keep",
                "arguments": "{}",
            },
        },
    ],
    ids=["arguments-done", "output-item-done"],
)
def test_native_stream_requires_added_owner_before_terminal_item_event(event):
    plan = _native_plan({"type": "function", "name": "keep"})
    state = CompatibilityStreamState(plan)

    with pytest.raises(ToolCompatibilityError) as exc_info:
        state.decode_events_for_event(event)

    assert exc_info.value.classification == "missing_stream_identity"


def test_unknown_custom_stream_owner_preserves_legacy_apply_patch_lifecycle():
    plan = _native_plan({"type": "function", "name": "shell_command"})
    state = CompatibilityStreamState(plan)
    added_item = {
        "type": "custom_tool_call",
        "id": "patch-item",
        "call_id": "patch-call",
        "name": "apply_patch",
        "input": "",
    }
    input_prefix = "*** Begin Patch\n"
    input_text = f"{input_prefix}*** End Patch\n"

    assert state.decode_events_for_event(
        {"type": "response.output_item.added", "item": added_item}
    )[0]["item"] == added_item
    assert state.decode_events_for_event(
        {
            "type": "response.custom_tool_call_input.delta",
            "item_id": "patch-item",
            "call_id": "patch-call",
            "delta": input_prefix,
        }
    )[0]["delta"] == input_prefix
    assert state.decode_events_for_event(
        {
            "type": "response.custom_tool_call_input.done",
            "item_id": "patch-item",
            "call_id": "patch-call",
            "input": input_text,
        }
    )[0]["input"] == input_text
    done_item = {**added_item, "input": input_text}
    assert state.decode_events_for_event(
        {"type": "response.output_item.done", "item": done_item}
    )[0]["item"] == done_item
    state.decode_events_for_event(
        {"type": "response.completed", "response": {"output": []}}
    )


def test_unknown_custom_stream_rejects_done_input_that_diverges_from_delta_prefix():
    plan = _native_plan({"type": "function", "name": "shell_command"})
    state = CompatibilityStreamState(plan)
    state.decode_events_for_event(
        {
            "type": "response.output_item.added",
            "item": {
                "type": "custom_tool_call",
                "id": "patch-item",
                "call_id": "patch-call",
                "name": "apply_patch",
                "input": "",
            },
        }
    )
    state.decode_events_for_event(
        {
            "type": "response.custom_tool_call_input.delta",
            "item_id": "patch-item",
            "call_id": "patch-call",
            "delta": "first",
        }
    )

    with pytest.raises(ToolCompatibilityError) as exc_info:
        state.decode_events_for_event(
            {
                "type": "response.custom_tool_call_input.done",
                "item_id": "patch-item",
                "call_id": "patch-call",
                "input": "different",
            }
        )

    assert exc_info.value.classification == "incomplete_stream_delta"


@pytest.mark.parametrize(
    "event",
    [
        {
            "type": "response.custom_tool_call_input.delta",
            "item_id": "orphan-item",
            "call_id": "orphan-call",
            "delta": "opaque",
        },
        {
            "type": "response.custom_tool_call_input.done",
            "item_id": "orphan-item",
            "call_id": "orphan-call",
            "input": "opaque",
        },
        {
            "type": "response.output_item.done",
            "item": {
                "type": "custom_tool_call",
                "id": "orphan-item",
                "call_id": "orphan-call",
                "name": "apply_patch",
                "input": "opaque",
            },
        },
    ],
    ids=["delta", "arguments-done", "item-done"],
)
def test_unknown_custom_stream_requires_added_owner(event):
    plan = _native_plan({"type": "function", "name": "shell_command"})
    with pytest.raises(ToolCompatibilityError) as exc_info:
        CompatibilityStreamState(plan).decode_events_for_event(event)
    assert exc_info.value.classification == "missing_stream_identity"


def test_native_plain_stream_accepts_exact_flattened_namespace_wire_identity():
    plan = _native_plan({"type": "function", "name": "multi_agent_v1__spawn_agent"})
    state = CompatibilityStreamState(plan)
    item = {
        "type": "function_call",
        "namespace": "multi_agent_v1",
        "name": "spawn_agent",
        "id": "flattened-item",
        "call_id": "flattened-call",
        "arguments": "",
    }
    state.decode_events_for_event({"type": "response.output_item.added", "item": item})
    state.decode_events_for_event(
        {
            "type": "response.function_call_arguments.done",
            "item_id": "flattened-item",
            "call_id": "flattened-call",
            "arguments": "{}",
        }
    )
    state.decode_events_for_event(
        {"type": "response.output_item.done", "item": {**item, "arguments": "{}"}}
    )


def test_native_flattened_item_done_can_complete_arguments_without_separate_done_event():
    plan = _native_plan({"type": "function", "name": "multi_agent_v1__spawn_agent"})
    state = CompatibilityStreamState(plan)
    item = {
        "type": "function_call",
        "namespace": "multi_agent_v1",
        "name": "spawn_agent",
        "id": "flattened-item",
        "call_id": "flattened-call",
        "arguments": "",
    }
    state.decode_events_for_event({"type": "response.output_item.added", "item": item})
    completed = {**item, "arguments": "{}"}
    state.decode_events_for_event({"type": "response.output_item.done", "item": completed})
    state.decode_events_for_event(
        {"type": "response.completed", "response": {"output": [completed]}}
    )


def test_legacy_flattened_worker_selector_item_done_keeps_no_added_passthrough():
    plan = _native_plan({"type": "function", "name": "multi_agent_v1__spawn_agent"})
    item = {
        "type": "function_call",
        "namespace": "multi_agent_v1",
        "name": "spawn_agent",
        "id": "selector-item",
        "call_id": "selector-call",
        "arguments": "{}",
    }
    CompatibilityStreamState(plan).decode_events_for_event(
        {"type": "response.output_item.done", "item": item}
    )


def test_native_plain_tool_search_wrapper_keeps_no_added_terminal_path():
    plan = _native_plan({"type": "function", "name": "tool_search"})
    state = CompatibilityStreamState(plan)
    item = {
        "type": "function_call",
        "name": "tool_search",
        "id": "tool-search-item",
        "call_id": "tool-search-call",
        "arguments": "",
    }
    state.decode_events_for_event({"type": "response.output_item.done", "item": item})


@pytest.mark.parametrize("surface", ["body", "history"])
def test_native_namespace_rejects_unqualified_child_without_plain_owner(surface):
    plan = _native_plan(_namespace("vendor", child="run"))
    item = {
        "type": "function_call",
        "id": "namespace-item",
        "call_id": "namespace-call",
        "name": "run",
        "arguments": "{}",
    }

    with pytest.raises(ToolCompatibilityError) as exc_info:
        if surface == "body":
            plan.decode_payload({"output": [item]})
        else:
            plan.encode_payload({"input": [item]})

    assert exc_info.value.classification == "unknown_native_identity"


def test_foreign_collaboration_history_is_preserved_when_current_plan_is_different() -> None:
    plan = _adapted_namespace_plan(_namespace("multi_agent_v1", child="spawn_agent"))
    history = [{
        "type": "function_call",
        "namespace": "collaboration",
        "name": "followup_task",
        "call_id": "old-v2-call",
        "arguments": '{"task_name":"old","fork_turns":"all"}',
    }]

    encoded = plan.encode_payload({"input": history})

    assert encoded["input"] == history


@pytest.mark.parametrize(
    ("item", "classification"),
    [
        (
            {
                "type": "function_call",
                "namespace": "multi_agent_v1",
                "name": "unknown_child",
                "call_id": "unknown-child",
                "arguments": "{}",
            },
            "unknown_v1_tool",
        ),
        (
            {
                "type": "function_call",
                "namespace": "multi_agent_v1",
                "name": "followup_task",
                "call_id": "mixed-fields",
                "arguments": '{"task_name":"foreign-v2"}',
            },
            "mixed_v1_v2",
        ),
    ],
    ids=["unknown-v1-child", "v1-v2-only-tool"],
)
def test_malformed_foreign_collaboration_history_stays_at_semantic_boundary(
    item: dict,
    classification: str,
) -> None:
    assert not _is_opaque_collaboration_history_item(item)

    with pytest.raises(CollaborationBoundaryError) as exc_info:
        classify_collaboration_payload({"input": [item]})

    assert exc_info.value.classification == classification


def test_native_namespace_stream_rejects_unqualified_child_without_plain_owner():
    plan = _native_plan(_namespace("vendor", child="run"))
    state = CompatibilityStreamState(plan)

    with pytest.raises(ToolCompatibilityError) as exc_info:
        state.decode_events_for_event(
            {
                "type": "response.output_item.added",
                "item": {
                    "type": "function_call",
                    "id": "namespace-item",
                    "call_id": "namespace-call",
                    "name": "run",
                    "arguments": "",
                },
            }
        )

    assert exc_info.value.classification == "unknown_native_identity"


def test_multiple_native_namespaces_reject_ambiguous_unqualified_child():
    plan = build_tool_compatibility_plan(
        [
            _namespace("vendor_a", child="run"),
            _namespace("vendor_b", child="run"),
        ],
        selected_protocol="responses_structured",
        protocol_capabilities=ProtocolCapabilities.responses_structured(),
        request_token="native-ambiguous-namespace-child",
    )
    item = {
        "type": "function_call",
        "id": "namespace-item",
        "call_id": "namespace-call",
        "name": "run",
        "arguments": "{}",
    }

    with pytest.raises(ToolCompatibilityError) as exc_info:
        plan.decode_payload({"output": [item]})

    assert exc_info.value.classification == "unknown_native_identity"


def test_native_plain_owner_still_wins_over_unqualified_native_namespace_child():
    plan = build_tool_compatibility_plan(
        [
            {"type": "function", "name": "run"},
            _namespace("vendor", child="run"),
        ],
        selected_protocol="responses_structured",
        protocol_capabilities=ProtocolCapabilities.responses_structured(),
        request_token="native-plain-namespace-collision",
    )
    item = {
        "type": "function_call",
        "id": "plain-item",
        "call_id": "plain-call",
        "name": "run",
        "arguments": "{}",
    }

    assert plan.decode_payload({"output": [item]})["output"] == [item]


@pytest.mark.parametrize("native", [True, False], ids=["native", "adapted"])
@pytest.mark.parametrize(
    ("namespace", "name"),
    [("evil", "run"), ("vendor", "evil")],
    ids=["wrong-namespace", "wrong-child"],
)
@pytest.mark.parametrize("surface", ["body", "history", "stream"])
def test_namespace_contract_rejects_inexact_namespace_owner(native, namespace, name, surface):
    declaration = _namespace("vendor", child="run")
    if native:
        plan = _native_plan(declaration)
    else:
        plan = build_tool_compatibility_plan(
            [declaration],
            selected_protocol="chat_tools",
            protocol_capabilities=ProtocolCapabilities.chat_tools(),
            request_token="adapted-inexact-namespace-owner",
        )
    item = {
        "type": "function_call",
        "id": "namespace-item",
        "call_id": "namespace-call",
        "namespace": namespace,
        "name": name,
        "arguments": "{}",
    }

    with pytest.raises(ToolCompatibilityError) as exc_info:
        if surface == "body":
            plan.decode_payload({"output": [item]})
        elif surface == "history":
            plan.encode_payload({"input": [item]})
        else:
            CompatibilityStreamState(plan).decode_events_for_event(
                {
                    "type": "response.output_item.added",
                    "item": {**item, "arguments": ""},
                }
            )

    assert exc_info.value.classification == "unknown_native_identity"


def test_omitted_plain_function_cannot_use_flattened_namespace_escape():
    plan = build_tool_compatibility_plan(
        [{"type": "function", "name": "vendor__run"}],
        selected_protocol="none",
        request_token="omitted-flattened-plain",
    )

    with pytest.raises(ToolCompatibilityError) as exc_info:
        plan.encode_payload(
            {
                "input": [
                    {
                        "type": "function_call",
                        "id": "item",
                        "call_id": "call",
                        "namespace": "vendor",
                        "name": "run",
                        "arguments": "{}",
                    }
                ]
            }
        )

    assert exc_info.value.classification == "unknown_native_identity"


@pytest.mark.parametrize(
    ("declaration", "item"),
    [
        (
            {"type": "function", "name": "tool_search"},
            {"type": "function_call", "name": "tool_search", "arguments": "{}"},
        ),
        (
            {"type": "function", "name": "multi_agent_v1__spawn_agent"},
            {
                "type": "function_call",
                "namespace": "multi_agent_v1",
                "name": "spawn_agent",
                "arguments": "{}",
            },
        ),
        (
            {"type": "tool_search", "execution": "client"},
            {
                "type": "tool_search_call",
                "execution": "client",
                "arguments": {"query": "x"},
            },
        ),
    ],
    ids=["tool-search-wrapper", "flattened-worker-selector", "native-tool-search"],
)
@pytest.mark.parametrize(
    ("present_field", "expected_classification"),
    [
        ("call_id", "missing_item_identity"),
        ("id", "missing_call_identity"),
    ],
)
def test_legacy_no_added_terminal_requires_nonempty_item_and_call_identity(
    declaration,
    item,
    present_field,
    expected_classification,
):
    item = {**item, present_field: "present"}
    state = CompatibilityStreamState(_native_plan(declaration))

    with pytest.raises(ToolCompatibilityError) as exc_info:
        state.decode_events_for_event({"type": "response.output_item.done", "item": item})

    assert exc_info.value.classification == expected_classification


@pytest.mark.parametrize(
    ("declaration", "item"),
    [
        (
            {"type": "function", "name": "tool_search"},
            {
                "type": "function_call",
                "id": "terminal-item",
                "call_id": "terminal-call",
                "name": "tool_search",
                "arguments": "{}",
            },
        ),
        (
            {"type": "function", "name": "multi_agent_v1__spawn_agent"},
            {
                "type": "function_call",
                "id": "terminal-item",
                "call_id": "terminal-call",
                "namespace": "multi_agent_v1",
                "name": "spawn_agent",
                "arguments": "{}",
            },
        ),
        (
            {"type": "tool_search", "execution": "client"},
            {
                "type": "tool_search_call",
                "id": "terminal-item",
                "call_id": "terminal-call",
                "execution": "client",
                "arguments": {"query": "x"},
            },
        ),
    ],
    ids=["tool-search-wrapper", "flattened-worker-selector", "native-tool-search"],
)
def test_legacy_no_added_terminal_rejects_duplicate_item_and_call_identity(declaration, item):
    state = CompatibilityStreamState(_native_plan(declaration))
    event = {"type": "response.output_item.done", "item": item}
    state.decode_events_for_event(event)

    with pytest.raises(ToolCompatibilityError) as exc_info:
        state.decode_events_for_event(event)

    assert exc_info.value.classification == "duplicate_item_identity"


def test_native_tool_search_no_added_terminal_requires_client_execution_marker():
    state = CompatibilityStreamState(
        _native_plan({"type": "tool_search", "execution": "client"})
    )

    with pytest.raises(ToolCompatibilityError) as exc_info:
        state.decode_events_for_event(
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "tool_search_call",
                    "id": "terminal-item",
                    "call_id": "terminal-call",
                    "execution": "provider",
                    "arguments": {"query": "x"},
                },
            }
        )

    assert exc_info.value.classification == "invalid_tool_search_execution"


@pytest.mark.parametrize(
    ("declaration", "done_item", "terminal_item"),
    [
        (
            {"type": "function", "name": "tool_search"},
            {
                "type": "function_call",
                "id": "item",
                "call_id": "call",
                "name": "tool_search",
                "arguments": '{"query":"one"}',
            },
            {
                "type": "function_call",
                "id": "item",
                "call_id": "call",
                "name": "tool_search",
                "arguments": '{"query":"two"}',
            },
        ),
        (
            {"type": "function", "name": "multi_agent_v1__spawn_agent"},
            {
                "type": "function_call",
                "id": "item",
                "call_id": "call",
                "namespace": "multi_agent_v1",
                "name": "spawn_agent",
                "arguments": '{"task":"one"}',
            },
            {
                "type": "function_call",
                "id": "item",
                "call_id": "call",
                "namespace": "multi_agent_v1",
                "name": "spawn_agent",
                "arguments": '{"task":"two"}',
            },
        ),
        (
            {"type": "tool_search", "execution": "client"},
            {
                "type": "tool_search_call",
                "id": "item",
                "call_id": "call",
                "execution": "client",
                "arguments": {"query": "one"},
            },
            {
                "type": "tool_search_call",
                "id": "item",
                "call_id": "call",
                "execution": "client",
                "arguments": {"query": "two"},
            },
        ),
    ],
    ids=["tool-search-wrapper", "flattened-worker-selector", "native-tool-search"],
)
def test_legacy_no_added_terminal_reconciles_semantic_snapshot(
    declaration,
    done_item,
    terminal_item,
):
    state = CompatibilityStreamState(_native_plan(declaration))
    state.decode_events_for_event(
        {"type": "response.output_item.done", "item": done_item}
    )

    with pytest.raises(ToolCompatibilityError) as exc_info:
        state.decode_events_for_event(
            {"type": "response.completed", "response": {"output": [terminal_item]}}
        )

    assert exc_info.value.classification == "ambiguous_native_identity"


def test_legacy_no_added_terminal_rejects_changed_call_identity():
    declaration = {"type": "function", "name": "tool_search"}
    item = {
        "type": "function_call",
        "id": "item",
        "call_id": "call",
        "name": "tool_search",
        "arguments": "{}",
    }
    state = CompatibilityStreamState(_native_plan(declaration))
    state.decode_events_for_event({"type": "response.output_item.done", "item": item})

    with pytest.raises(ToolCompatibilityError) as exc_info:
        state.decode_events_for_event(
            {
                "type": "response.completed",
                "response": {"output": [{**item, "call_id": "evil"}]},
            }
        )

    assert exc_info.value.classification == "ambiguous_call_identity"


def test_buffered_custom_delta_and_done_require_bound_call_id():
    plan = build_tool_compatibility_plan(
        [{"type": "custom", "name": "paint", "format": {"type": "text"}}],
        selected_protocol="chat_tools",
        protocol_capabilities=ProtocolCapabilities.chat_tools(),
        request_token="custom-call-id-boundary",
    )
    alias = plan.entries[0].aliases[0]
    state = CompatibilityStreamState(plan)
    state.decode_events_for_event(
        {"type": "response.output_item.added", "item": {"type": "function_call", "id": "item", "call_id": "call", "name": alias, "arguments": ""}}
    )
    with pytest.raises(ToolCompatibilityError):
        state.decode_events_for_event(
            {"type": "response.function_call_arguments.delta", "item_id": "item", "call_id": "evil", "delta": "{}"}
        )


@pytest.mark.parametrize("item_type", ["custom_tool_call", "message"], ids=["custom-wire-family", "other-wire-family"])
def test_buffered_custom_added_requires_function_call_wire_type(item_type):
    plan = build_tool_compatibility_plan(
        [{"type": "custom", "name": "paint", "format": {"type": "text"}}],
        selected_protocol="chat_tools",
        protocol_capabilities=ProtocolCapabilities.chat_tools(),
        request_token="custom-added-wire-family",
    )
    alias = plan.entries[0].aliases[0]
    state = CompatibilityStreamState(plan)

    with pytest.raises(ToolCompatibilityError) as exc_info:
        state.decode_events_for_event(
            {
                "type": "response.output_item.added",
                "item": {
                    "type": item_type,
                    "id": "item",
                    "call_id": "call",
                    "name": alias,
                    "arguments": "",
                },
            }
        )

    assert exc_info.value.classification == "ambiguous_call_identity"


@pytest.mark.parametrize("surface", ["body", "direct-stream"])
def test_adapted_custom_alias_rejects_namespace_decoration(surface):
    plan = build_tool_compatibility_plan(
        [{"type": "custom", "name": "paint", "format": {"type": "text"}}],
        selected_protocol="chat_tools",
        protocol_capabilities=ProtocolCapabilities.chat_tools(),
        request_token=f"custom-namespace-decoration-{surface}",
    )
    alias = plan.entries[0].aliases[0]
    item = {
        "type": "function_call",
        "id": "item",
        "call_id": "call",
        "namespace": "evil",
        "name": alias,
        "arguments": '{"__codexhub_custom_input":"opaque"}',
    }

    with pytest.raises(ToolCompatibilityError) as exc_info:
        if surface == "body":
            plan.decode_payload({"output": [item]})
        else:
            CompatibilityStreamState(plan).decode_event(
                {"type": "response.output_item.added", "item": item}
            )

    assert exc_info.value.classification == "unknown_alias"


def test_adapted_custom_native_history_rejects_namespace_decoration():
    plan = build_tool_compatibility_plan(
        [{"type": "custom", "name": "paint", "format": {"type": "text"}}],
        selected_protocol="chat_tools",
        protocol_capabilities=ProtocolCapabilities.chat_tools(),
        request_token="custom-native-history-namespace",
    )

    with pytest.raises(ToolCompatibilityError) as exc_info:
        plan.encode_payload(
            {
                "input": [
                    {
                        "type": "custom_tool_call",
                        "id": "item",
                        "call_id": "call",
                        "namespace": "evil",
                        "name": "paint",
                        "input": "opaque",
                    }
                ]
            }
        )

    assert exc_info.value.classification == "unknown_native_identity"


@pytest.mark.parametrize(
    ("surface", "expected_classification"),
    [
        ("body", "unknown_alias"),
        ("history", "ambiguous_call_identity"),
        ("stream", "unknown_alias"),
    ],
)
def test_adapted_custom_result_rejects_namespace_decoration(surface, expected_classification):
    declaration = {"type": "custom", "name": "paint", "format": {"type": "text"}}
    plan = build_tool_compatibility_plan(
        [declaration],
        selected_protocol="chat_tools",
        protocol_capabilities=ProtocolCapabilities.chat_tools(),
        request_token=f"custom-result-namespace-{surface}",
    )
    encoded = plan.encode_payload(
        {
            "input": [
                {
                    "type": "custom_tool_call",
                    "id": "call-item",
                    "call_id": "call",
                    "name": "paint",
                    "input": "opaque",
                },
                {
                    "type": "custom_tool_call_output",
                    "id": "result-item",
                    "call_id": "call",
                    "output": "done",
                },
            ]
        }
    )["input"]
    call_item, result_item = encoded
    result_item = {**result_item, "namespace": "evil"}

    with pytest.raises(ToolCompatibilityError) as exc_info:
        if surface == "body":
            plan.decode_payload({"output": [call_item, result_item]})
        elif surface == "history":
            plan.decode_payload({"history": [call_item, result_item]})
        else:
            state = CompatibilityStreamState(plan)
            state.decode_event(
                {"type": "response.output_item.added", "item": call_item}
            )
            state.decode_event(
                {"type": "response.output_item.added", "item": result_item}
            )

    assert exc_info.value.classification == expected_classification


def test_buffered_adapted_custom_added_rejects_namespace_decoration():
    plan = build_tool_compatibility_plan(
        [{"type": "custom", "name": "paint", "format": {"type": "text"}}],
        selected_protocol="chat_tools",
        protocol_capabilities=ProtocolCapabilities.chat_tools(),
        request_token="buffered-custom-namespace-decoration",
    )
    alias = plan.entries[0].aliases[0]

    with pytest.raises(ToolCompatibilityError) as exc_info:
        CompatibilityStreamState(plan).decode_events_for_event(
            {
                "type": "response.output_item.added",
                "item": {
                    "type": "function_call",
                    "id": "item",
                    "call_id": "call",
                    "namespace": "evil",
                    "name": alias,
                    "arguments": "",
                },
            }
        )

    assert exc_info.value.classification == "invalid_custom_stream_identity"


def test_buffered_adapted_custom_done_requires_exact_added_namespace_identity():
    plan = build_tool_compatibility_plan(
        [{"type": "custom", "name": "paint", "format": {"type": "text"}}],
        selected_protocol="chat_tools",
        protocol_capabilities=ProtocolCapabilities.chat_tools(),
        request_token="custom-done-namespace-identity",
    )
    alias = plan.entries[0].aliases[0]
    state = CompatibilityStreamState(plan)
    arguments = '{"__codexhub_custom_input":"opaque"}'
    state.decode_events_for_event(
        {
            "type": "response.output_item.added",
            "item": {
                "type": "function_call",
                "id": "item",
                "call_id": "call",
                "name": alias,
                "arguments": "",
            },
        }
    )
    state.decode_events_for_event(
        {
            "type": "response.function_call_arguments.done",
            "item_id": "item",
            "call_id": "call",
            "arguments": arguments,
        }
    )

    with pytest.raises(ToolCompatibilityError) as exc_info:
        state.decode_events_for_event(
            {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "id": "item",
                    "call_id": "call",
                    "namespace": "evil",
                    "name": alias,
                    "arguments": arguments,
                },
            }
        )

    assert exc_info.value.classification == "ambiguous_native_identity"


@pytest.mark.parametrize("buffered", [True, False], ids=["buffered", "direct"])
def test_adapted_custom_done_requires_function_call_wire_type(buffered):
    plan = build_tool_compatibility_plan(
        [{"type": "custom", "name": "paint", "format": {"type": "text"}}],
        selected_protocol="chat_tools",
        protocol_capabilities=ProtocolCapabilities.chat_tools(),
        request_token=f"custom-done-wire-family-{buffered}",
    )
    alias = plan.entries[0].aliases[0]
    state = CompatibilityStreamState(plan)
    arguments = '{"__codexhub_custom_input":"opaque"}'
    added = {
        "type": "response.output_item.added",
        "item": {
            "type": "function_call",
            "id": "item",
            "call_id": "call",
            "name": alias,
            "arguments": "" if buffered else arguments,
        },
    }
    arguments_done = {
        "type": "response.function_call_arguments.done",
        "item_id": "item",
        "call_id": "call",
        "arguments": arguments,
    }
    item_done = {
        "type": "response.output_item.done",
        "item": {
            "type": "custom_tool_call",
            "id": "item",
            "call_id": "call",
            "name": alias,
            "arguments": arguments,
        },
    }
    decode = state.decode_events_for_event if buffered else state.decode_event
    decode(added)
    decode(arguments_done)

    with pytest.raises(ToolCompatibilityError) as exc_info:
        decode(item_done)

    expected = "ambiguous_native_identity" if buffered else "ambiguous_call_identity"
    assert exc_info.value.classification == expected


def test_conflicting_item_id_and_id_fail_closed():
    plan = _native_plan({"type": "function", "name": "keep"})
    with pytest.raises(ToolCompatibilityError):
        plan.decode_payload(
            {"output": [{"type": "function_call", "item_id": "item", "id": "evil", "call_id": "call", "name": "keep", "arguments": "{}"}]}
        )


def test_conflicting_stream_item_id_and_id_fail_closed():
    plan = _native_plan({"type": "function", "name": "keep"})
    state = CompatibilityStreamState(plan)
    state.decode_events_for_event(
        {
            "type": "response.output_item.added",
            "item": {
                "type": "function_call",
                "id": "item",
                "call_id": "call",
                "name": "keep",
                "arguments": "",
            },
        }
    )

    with pytest.raises(ToolCompatibilityError) as exc_info:
        state.decode_events_for_event(
            {
                "type": "response.function_call_arguments.delta",
                "item_id": "item",
                "id": "evil",
                "call_id": "call",
                "delta": "{}",
            }
        )

    assert exc_info.value.classification == "ambiguous_native_identity"
    assert exc_info.value.surface == "stream"


def test_conflicting_nested_and_top_level_stream_identity_fails_closed():
    state = CompatibilityStreamState(_native_hosted_plan())
    state.decode_events_for_event(
        {
            "type": "response.output_item.added",
            "item": {
                "type": "web_search_call",
                "id": "search-item",
                "status": "in_progress",
            },
        }
    )

    with pytest.raises(ToolCompatibilityError) as exc_info:
        state.decode_events_for_event(
            {
                "type": "response.web_search_call.in_progress",
                "item_id": "evil",
                "item": {"id": "search-item"},
            }
        )

    assert exc_info.value.classification == "ambiguous_native_identity"
    assert exc_info.value.surface == "stream"


def test_invented_hosted_output_shape_fails_closed():
    plan = _native_hosted_plan()
    invented = {"type": "web_search_call_output", "id": "output", "output": {"evil": True}}
    with pytest.raises(ToolCompatibilityError):
        plan.decode_payload({"output": [invented]})
    with pytest.raises(ToolCompatibilityError):
        plan.decode_payload(
            {
                "output": [
                    {"type": "web_search_call", "id": "call", "status": "completed"},
                    invented,
                ]
            }
        )


@pytest.mark.parametrize("output_type", ["function_call_output", "vendor_extension_call_output"])
def test_sse_output_before_adapted_call_fails_closed(output_type):
    plan = _adapted_namespace_plan()
    state = CompatibilityStreamState(plan)
    with pytest.raises(ToolCompatibilityError):
        state.decode_events_for_event(
            {
                "type": "response.output_item.added",
                "item": {"type": output_type, "id": "output-first", "call_id": "call"},
            }
        )


def test_history_retained_call_cannot_be_borrowed_by_unknown_output_family():
    plan = _native_plan({"type": "function", "name": "keep"})
    history = [
        {"type": "function_call", "id": "call-item", "name": "keep", "call_id": "call", "arguments": "{}"},
        {"type": "vendor_extension_call_output", "id": "output", "call_id": "call", "output": "opaque"},
    ]
    with pytest.raises(ToolCompatibilityError):
        plan.encode_payload({"input": history})


def test_encode_history_rejects_duplicate_tool_search_outputs_by_call_id():
    plan = _native_plan({"type": "tool_search", "execution": "client"})
    history = [
        {"type": "tool_search_call", "id": "search-call", "call_id": "search", "arguments": {"query": "x"}},
        {"type": "tool_search_output", "id": "search-output-1", "call_id": "search", "tools": []},
        {"type": "tool_search_output", "id": "search-output-2", "call_id": "search", "tools": []},
    ]
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


@pytest.mark.parametrize(
    "nested_item",
    [
        {"id": "search-item"},
        {"id": "search-item", "type": "file_search_call"},
        {"id": "search-item", "type": "web_search_call", "name": "other"},
    ],
    ids=["missing-wire-type", "wrong-wire-type", "wrong-wire-name"],
)
def test_native_hosted_sse_rejects_nested_item_wire_identity_mismatch(nested_item):
    state = CompatibilityStreamState(_native_hosted_plan())
    state.decode_events_for_event(
        {
            "type": "response.output_item.added",
            "item": {"type": "web_search_call", "id": "search-item", "status": "in_progress"},
        }
    )

    with pytest.raises(ToolCompatibilityError) as exc_info:
        state.decode_events_for_event(
            {
                "type": "response.web_search_call.in_progress",
                "item_id": "search-item",
                "item": nested_item,
            }
        )

    assert exc_info.value.classification == "ambiguous_native_identity"
    assert exc_info.value.surface == "stream"

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


@pytest.mark.parametrize(
    "items",
    [
        [
            {"type": "web_search_call", "id": "same-item", "status": "completed"},
            {"type": "web_search_call_output", "id": "same-item", "output": "ignored"},
            {"type": "web_search_call_output", "id": "same-item", "output": "duplicate"},
        ],
        [
            {"type": "web_search_call_output", "id": "same-item", "output": "ignored"},
            {"type": "web_search_call", "id": "same-item", "status": "completed"},
            {"type": "web_search_call", "id": "same-item", "status": "duplicate"},
        ],
    ],
    ids=["root-output-output", "output-root-root"],
)
def test_omitted_hosted_history_rejects_reused_pair_identity(items):
    plan = build_tool_compatibility_plan(
        [{"type": "web_search"}],
        selected_protocol="chat_tools",
        provider_hosted_capabilities={},
        protocol_capabilities=ProtocolCapabilities.chat_tools(),
        request_token="omitted-hosted-reused-pair",
    )
    with pytest.raises(ToolCompatibilityError) as exc_info:
        plan.encode_payload({"input": items})
    assert exc_info.value.classification == "duplicate_item_identity"


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


def test_omitted_unknown_call_output_fails_closed_in_body_and_sse_boundaries():
    declaration = {"type": "vendor_extension", "executor": "codex_client"}
    plan = build_tool_compatibility_plan(
        [declaration],
        selected_protocol="responses_structured",
        protocol_capabilities=ProtocolCapabilities.responses_structured(),
        request_token="omitted-unknown-call-output",
    )
    output_item = {
        "type": "vendor_extension_call_output",
        "id": "extension-output-item",
        "output": {"opaque": True},
    }
    with pytest.raises(ToolCompatibilityError):
        plan.decode_payload({"output": [output_item]})

    with pytest.raises(ToolCompatibilityError):
        CompatibilityStreamState(plan).decode_events_for_event(
            {"type": "response.output_item.added", "item": output_item}
        )
    with pytest.raises(ToolCompatibilityError):
        CompatibilityStreamState(plan).decode_events_for_event(
            {"type": "response.output_item.done", "item": output_item}
        )
    with pytest.raises(ToolCompatibilityError):
        CompatibilityStreamState(plan).decode_events_for_event(
            {"type": "response.completed", "response": {"output": [output_item]}}
        )


def test_omitted_known_hosted_call_output_fails_closed_in_body_and_sse_boundaries():
    plan = build_tool_compatibility_plan(
        [{"type": "web_search"}],
        selected_protocol="chat_tools",
        provider_hosted_capabilities={},
        protocol_capabilities=ProtocolCapabilities.chat_tools(),
        request_token="omitted-known-hosted-call-output",
    )
    output_item = {
        "type": "web_search_call_output",
        "id": "search-output-item",
        "output": {"opaque": True},
    }
    with pytest.raises(ToolCompatibilityError):
        plan.decode_payload({"output": [output_item]})
    with pytest.raises(ToolCompatibilityError):
        CompatibilityStreamState(plan).decode_events_for_event(
            {"type": "response.output_item.added", "item": output_item}
        )
    with pytest.raises(ToolCompatibilityError):
        CompatibilityStreamState(plan).decode_events_for_event(
            {"type": "response.output_item.done", "item": output_item}
        )
    with pytest.raises(ToolCompatibilityError):
        CompatibilityStreamState(plan).decode_events_for_event(
            {"type": "response.completed", "response": {"output": [output_item]}}
        )


def test_native_custom_wins_over_omitted_unknown_custom_tool_type_in_body_and_terminal():
    plan = build_tool_compatibility_plan(
        [
            {"type": "custom", "name": "paint", "format": {"type": "text"}},
            {"type": "custom_tool", "executor": "codex_client"},
        ],
        selected_protocol="responses_structured",
        protocol_capabilities=ProtocolCapabilities.responses_structured(),
        request_token="native-custom-omitted-unknown-collision",
    )
    item = {
        "type": "custom_tool_call",
        "id": "custom-item",
        "call_id": "custom-call",
        "name": "paint",
        "input": "opaque",
    }
    assert plan.decode_payload({"output": [item]})["output"] == [item]
    assert CompatibilityStreamState(plan).decode_events_for_event(
        {"type": "response.output_item.added", "item": item}
    )
    assert CompatibilityStreamState(plan).decode_events_for_event(
        {"type": "response.completed", "response": {"output": [item]}}
    )


def test_native_custom_history_survives_omitted_unknown_custom_tool_type():
    plan = build_tool_compatibility_plan(
        [
            {"type": "custom", "name": "paint", "format": {"type": "text"}},
            {"type": "custom_tool", "executor": "selected_provider"},
        ],
        selected_protocol="responses_structured",
        protocol_capabilities=ProtocolCapabilities.responses_structured(),
        request_token="native-custom-omitted-unknown-history-collision",
    )
    history = [
        {
            "type": "custom_tool_call",
            "id": "custom-item",
            "call_id": "custom-call",
            "name": "paint",
            "input": "opaque",
        },
        {
            "type": "custom_tool_call_output",
            "id": "custom-output",
            "call_id": "custom-call",
            "output": "opaque",
        },
        {"type": "message", "role": "user", "content": "keep"},
    ]

    assert plan.encode_payload({"input": history})["input"] == history


def test_unknown_custom_tool_history_is_omitted_despite_reserved_standard_item_spelling():
    plan = build_tool_compatibility_plan(
        [
            {
                "type": "custom_tool",
                "name": "vendor_custom",
                "executor": "selected_provider",
            }
        ],
        selected_protocol="responses_structured",
        protocol_capabilities=ProtocolCapabilities.responses_structured(),
        request_token="unknown-custom-tool-history",
    )
    encoded = plan.encode_payload(
        {
            "input": [
                {
                    "type": "custom_tool_call",
                    "id": "unknown-item",
                    "call_id": "unknown-call",
                    "name": "vendor_custom",
                    "input": "opaque",
                },
                {
                    "type": "custom_tool_call_output",
                    "id": "unknown-output",
                    "call_id": "unknown-call",
                    "output": "opaque",
                },
                {"type": "message", "role": "user", "content": "keep"},
            ]
        }
    )

    assert encoded["input"] == [{"type": "message", "role": "user", "content": "keep"}]


def test_native_and_unknown_custom_tool_history_pairs_use_call_ownership():
    plan = build_tool_compatibility_plan(
        [
            {"type": "custom", "name": "paint", "format": {"type": "text"}},
            {
                "type": "custom_tool",
                "name": "vendor_custom",
                "executor": "selected_provider",
            },
        ],
        selected_protocol="responses_structured",
        protocol_capabilities=ProtocolCapabilities.responses_structured(),
        request_token="native-unknown-custom-tool-history",
    )
    native_pair = [
        {
            "type": "custom_tool_call",
            "id": "native-item",
            "call_id": "native-call",
            "name": "paint",
            "input": "native",
        },
        {
            "type": "custom_tool_call_output",
            "id": "native-output",
            "call_id": "native-call",
            "output": "native",
        },
    ]
    encoded = plan.encode_payload(
        {
            "input": [
                *native_pair,
                {
                    "type": "custom_tool_call",
                    "id": "unknown-item",
                    "call_id": "unknown-call",
                    "name": "vendor_custom",
                    "input": "unknown",
                },
                {
                    "type": "custom_tool_call_output",
                    "id": "unknown-output",
                    "call_id": "unknown-call",
                    "output": "unknown",
                },
                {"type": "message", "role": "user", "content": "keep"},
            ]
        }
    )

    assert encoded["input"] == [
        *native_pair,
        {"type": "message", "role": "user", "content": "keep"},
    ]


def test_registered_namespace_call_cannot_claim_unknown_custom_tool_output_boundaries():
    declarations = [
        _namespace("vendor", child="run"),
        {
            "type": "custom_tool",
            "name": "vendor_custom",
            "executor": "selected_provider",
        },
    ]
    plan = build_tool_compatibility_plan(
        declarations,
        selected_protocol="chat_tools",
        protocol_capabilities=ProtocolCapabilities.chat_tools(),
        request_token="registered-namespace-custom-output-collision",
    )
    alias = plan.entries[0].aliases[0]
    plan.decode_payload(
        {
            "output": [
                {
                    "type": "function_call",
                    "id": "namespace-item",
                    "call_id": "shared-call",
                    "name": alias,
                    "arguments": "{}",
                }
            ]
        }
    )
    output_item = {
        "type": "custom_tool_call_output",
        "id": "unknown-output",
        "call_id": "shared-call",
        "output": "opaque",
    }

    with pytest.raises(ToolCompatibilityError):
        plan.decode_payload({"output": [output_item]})
    with pytest.raises(ToolCompatibilityError):
        CompatibilityStreamState(plan).decode_events_for_event(
            {"type": "response.output_item.added", "item": output_item}
        )
    with pytest.raises(ToolCompatibilityError):
        CompatibilityStreamState(plan).decode_events_for_event(
            {"type": "response.output_item.done", "item": output_item}
        )
    with pytest.raises(ToolCompatibilityError):
        CompatibilityStreamState(plan).decode_events_for_event(
            {"type": "response.completed", "response": {"output": [output_item]}}
        )


@pytest.mark.parametrize(
    ("declaration", "items"),
    [
        (
            {"type": "function", "name": "plain"},
            [
                {"type": "function_call", "id": "plain-item", "call_id": "plain-call", "name": "plain"},
                {"type": "function_call_output", "id": "plain-output", "call_id": "plain-call", "output": "opaque"},
            ],
        ),
        (
            {"type": "custom", "name": "paint", "format": {"type": "text"}},
            [
                {"type": "custom_tool_call", "id": "custom-item", "call_id": "custom-call", "name": "paint", "input": "opaque"},
                {"type": "custom_tool_call_output", "id": "custom-output", "call_id": "custom-call", "output": "opaque"},
            ],
        ),
        (
            {"type": "tool_search", "execution": "client"},
            [
                {"type": "tool_search_call", "id": "search-item", "call_id": "search-call", "execution": "client"},
                {"type": "tool_search_output", "id": "search-output", "call_id": "search-call", "execution": "client", "tools": []},
            ],
        ),
    ],
    ids=["plain", "custom", "tool-search"],
)
def test_omitted_standard_call_and_output_fail_closed_in_body_and_sse(declaration, items):
    plan = build_tool_compatibility_plan(
        [declaration],
        selected_protocol="none",
        request_token="omitted-standard-response-item",
    )
    for item in items:
        with pytest.raises(ToolCompatibilityError):
            plan.decode_payload({"output": [item]})
        with pytest.raises(ToolCompatibilityError):
            CompatibilityStreamState(plan).decode_events_for_event(
                {"type": "response.output_item.added", "item": item}
            )
        with pytest.raises(ToolCompatibilityError):
            CompatibilityStreamState(plan).decode_events_for_event(
                {"type": "response.output_item.done", "item": item}
            )
        with pytest.raises(ToolCompatibilityError):
            CompatibilityStreamState(plan).decode_events_for_event(
                {"type": "response.completed", "response": {"output": [item]}}
            )


def test_unknown_selected_provider_lifecycle_remains_omitted_without_full_contract():
    declaration = {"type": "vendor_extension", "executor": "selected_provider"}
    protocol = ProtocolCapabilities.responses_structured(
        unknown_lifecycles=frozenset({"vendor_extension"}),
    )
    without_provider_fact = build_tool_compatibility_plan(
        [declaration],
        selected_protocol="responses_structured",
        provider_hosted_capabilities={},
        protocol_capabilities=protocol,
        request_token="unknown-hosted-no-provider-fact",
    )
    assert without_provider_fact.entries[0].disposition == OMIT

    with_provider_fact = build_tool_compatibility_plan(
        [declaration],
        selected_protocol="responses_structured",
        provider_hosted_capabilities={"vendor_extension": True},
        protocol_capabilities=protocol,
        request_token="unknown-hosted-with-provider-fact",
    )
    assert with_provider_fact.entries[0].disposition == OMIT


def test_unknown_lifecycle_name_fact_alone_is_not_a_complete_native_contract():
    declaration = {"type": "vendor_extension", "executor": "codex_client"}
    protocol = ProtocolCapabilities.responses_structured(
        unknown_lifecycles=frozenset({"vendor_extension"}),
    )
    optional = build_tool_compatibility_plan(
        [declaration],
        selected_protocol="responses_structured",
        protocol_capabilities=protocol,
        request_token="unknown-name-only-optional",
    )
    assert optional.entries[0].disposition == OMIT

    with pytest.raises(RequiredToolUnavailableError):
        build_tool_compatibility_plan(
            [declaration],
            selected_protocol="responses_structured",
            protocol_capabilities=protocol,
            required=True,
            request_token="unknown-name-only-required",
        )


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


def test_stream_rejects_lifecycle_events_after_terminal_but_keeps_terminal_marker_guard():
    plan = _native_plan({"type": "function", "name": "keep"})
    state = CompatibilityStreamState(plan)
    added = {
        "type": "function_call",
        "id": "native-item",
        "call_id": "native-call",
        "name": "keep",
        "arguments": "",
    }
    state.decode_events_for_event({"type": "response.output_item.added", "item": added})
    state.decode_events_for_event(
        {
            "type": "response.function_call_arguments.done",
            "item_id": "native-item",
            "call_id": "native-call",
            "arguments": "{}",
        }
    )
    completed_item = {**added, "arguments": "{}"}
    state.decode_events_for_event({"type": "response.output_item.done", "item": completed_item})
    terminal = {"type": "response.completed", "response": {"output": [completed_item]}}
    state.decode_events_for_event(terminal)

    for event in (
        {
            "type": "response.output_item.added",
            "item": {**added, "id": "late-item", "call_id": "late-call", "arguments": ""},
        },
        {
            "type": "response.function_call_arguments.delta",
            "item_id": "native-item",
            "call_id": "native-call",
            "delta": "late",
        },
    ):
        with pytest.raises(ToolCompatibilityError) as exc_info:
            state.decode_events_for_event(event)
        assert exc_info.value.classification == "stream_after_terminal"

    with pytest.raises(ToolCompatibilityError) as exc_info:
        state.decode_event(terminal)
    assert exc_info.value.classification == "duplicate_terminal"
