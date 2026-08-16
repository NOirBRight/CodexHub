from __future__ import annotations

import json

import pytest

from runtime_tool_compatibility import (
    ADAPT,
    NATIVE,
    OMIT,
    REQUIRED_BUT_UNAVAILABLE,
    CompatibilityStreamState,
    ProtocolCapabilities,
    RequiredToolUnavailableError,
    ToolCompatibilityError,
    build_tool_compatibility_plan,
)


def _namespace(namespace: str = "vendor", *, tool: str = "run") -> dict:
    return {
        "type": "namespace",
        "name": namespace,
        "tools": [{"type": "function", "name": tool, "parameters": {"type": "object"}}],
    }


def test_issue65_table_classifies_every_family_without_implicit_fallback() -> None:
    declarations = [
        {"type": "function", "name": "plain"},
        _namespace(),
        {"type": "custom", "name": "freeform", "format": {"type": "text"}},
        {"type": "tool_search", "execution": "client"},
        {"type": "web_search", "executor": "selected_provider"},
        {"type": "future_kind", "opaque": {"value": 1}},
    ]
    plan = build_tool_compatibility_plan(
        declarations,
        selected_protocol="chat_tools",
        provider_hosted_capabilities={"web_search": True},
        protocol_capabilities=ProtocolCapabilities(
            function_lifecycle=True,
            namespace_lifecycle=False,
            custom_lifecycle=False,
            tool_search_lifecycle=False,
            hosted_lifecycles=frozenset({"web_search"}),
            unknown_lifecycles=frozenset(),
            accepts_namespace_adapter=True,
            accepts_custom_adapter=True,
        ),
        required={"plain": False, "vendor": False, "freeform": False},
        request_token="table",
    )

    assert [entry.disposition for entry in plan.entries] == [
        NATIVE,
        ADAPT,
        ADAPT,
        OMIT,
        NATIVE,
        OMIT,
    ]
    assert {entry.family for entry in plan.entries} == {
        "plain_function",
        "namespace",
        "custom_freeform",
        "tool_search",
        "selected_provider_hosted",
        "unknown_future_kind",
    }


def test_required_unavailable_fails_before_request_encoding() -> None:
    with pytest.raises(RequiredToolUnavailableError) as raised:
        build_tool_compatibility_plan(
            [{"type": "tool_search", "execution": "client"}],
            selected_protocol="chat_tools",
            protocol_capabilities=ProtocolCapabilities(),
            required=True,
        )

    assert raised.value.code == "tool_compatibility_required_unavailable"
    assert "tool_search" not in str(raised.value)


def test_namespace_alias_round_trip_preserves_fields_and_ids() -> None:
    declaration = _namespace()
    plan = build_tool_compatibility_plan(
        [declaration],
        selected_protocol="chat_tools",
        protocol_capabilities=ProtocolCapabilities.chat_tools(),
        request_token="roundtrip",
    )
    alias = plan.entries[0].aliases[0]
    payload = {
        "tools": [declaration],
        "input": [
            {
                "type": "function_call",
                "namespace": "vendor",
                "name": "run",
                "call_id": "call_v2",
                "item_id": "item_v2",
                "arguments": '{"value":"opaque"}',
            },
            {
                "type": "function_call_output",
                "call_id": "call_v2",
                "item_id": "item_out_v2",
                "output": "ok",
            },
        ],
        "tool_choice": {
            "type": "function",
            "namespace": "vendor",
            "name": "run",
        },
    }

    encoded = plan.encode_payload(payload)
    assert encoded["tools"] == [
        {"type": "function", "name": alias, "parameters": {"type": "object"}}
    ]
    assert encoded["input"][0]["name"] == alias
    assert "namespace" not in encoded["input"][0]
    assert encoded["tool_choice"] == {"type": "function", "name": alias}

    decoded = plan.decode_payload({"output": [encoded["input"][0]]})
    assert decoded["output"][0]["namespace"] == "vendor"
    assert decoded["output"][0]["name"] == "run"
    assert decoded["output"][0]["call_id"] == "call_v2"
    assert plan.entries[0].aliases == (alias,)


def test_history_allows_legacy_duplicate_ids_on_ordinary_messages() -> None:
    plan = build_tool_compatibility_plan(
        [{"type": "function", "name": "keep", "parameters": {"type": "object"}}],
        selected_protocol="responses_structured",
        protocol_capabilities=ProtocolCapabilities.responses_structured(),
        request_token="legacy-duplicate-message-id",
    )
    history = [
        {"type": "message", "id": "message_1", "role": "assistant", "content": "first"},
        {"type": "message", "id": "message_1", "role": "assistant", "content": "second"},
    ]

    assert plan.encode_payload({"input": history})["input"] == history


@pytest.mark.parametrize("message_id", ["shared_message", "123"])
def test_history_rejects_duplicate_nonlegacy_message_ids(message_id: str) -> None:
    plan = build_tool_compatibility_plan(
        [{"type": "function", "name": "keep", "parameters": {"type": "object"}}],
        selected_protocol="responses_structured",
        protocol_capabilities=ProtocolCapabilities.responses_structured(),
        request_token="duplicate-message-id",
    )
    history = [
        {"type": "message", "id": message_id, "role": "assistant", "content": "first"},
        {"type": "message", "id": message_id, "role": "assistant", "content": "second"},
    ]

    with pytest.raises(ToolCompatibilityError) as raised:
        plan.encode_payload({"input": history})

    assert raised.value.classification == "duplicate_item_identity"


def test_history_rejects_conflicting_dual_ids_on_ordinary_messages() -> None:
    plan = build_tool_compatibility_plan(
        [{"type": "function", "name": "keep", "parameters": {"type": "object"}}],
        selected_protocol="responses_structured",
        protocol_capabilities=ProtocolCapabilities.responses_structured(),
        request_token="legacy-message-ambiguous-id",
    )

    with pytest.raises(ToolCompatibilityError) as raised:
        plan.encode_payload(
            {
                "input": [
                    {
                        "type": "message",
                        "id": "message_1",
                        "item_id": "message_2",
                        "role": "assistant",
                        "content": "legacy",
                    }
                ]
            }
        )

    assert raised.value.classification == "ambiguous_native_identity"


@pytest.mark.parametrize("message_first", [True, False])
def test_history_rejects_message_tool_item_identity_collisions(
    message_first: bool,
) -> None:
    plan = build_tool_compatibility_plan(
        [{"type": "function", "name": "keep", "parameters": {"type": "object"}}],
        selected_protocol="responses_structured",
        protocol_capabilities=ProtocolCapabilities.responses_structured(),
        request_token="legacy-message-tool-id-collision",
    )
    message = {
        "type": "message",
        "id": "shared_item",
        "role": "assistant",
        "content": "legacy",
    }
    call = {
        "type": "function_call",
        "id": "shared_item",
        "name": "keep",
        "call_id": "call_keep",
        "arguments": "{}",
    }

    with pytest.raises(ToolCompatibilityError) as raised:
        plan.encode_payload(
            {"input": [message, call] if message_first else [call, message]}
        )

    assert raised.value.classification == "duplicate_item_identity"


@pytest.mark.parametrize("tool_choice", ["string", "mapping"])
def test_native_name_collision_preserves_explicit_native_tool_choice(tool_choice) -> None:
    declaration = _namespace(namespace="vendor", tool="run")
    initial = build_tool_compatibility_plan(
        [declaration],
        selected_protocol="chat_tools",
        protocol_capabilities=ProtocolCapabilities.chat_tools(),
        request_token="native-choice-collision",
    )
    old_alias = initial.entries[0].aliases[0]
    assert old_alias != "__codexhub_ns_collision_1"

    # Use the generated alias as the native name so the test exercises the
    # actual reserve/remap path rather than a hand-written alias format.
    native = {"type": "function", "name": old_alias}
    original_choice = (
        old_alias
        if tool_choice == "string"
        else {"type": "function", "name": old_alias}
    )
    finalized = initial.with_final_declarations(
        [native],
        tool_choice=original_choice,
    )
    new_alias = finalized.entries[0].aliases[0]
    assert new_alias != old_alias

    encoded = finalized.encode_payload(
        {
            "tools": [declaration, native],
            "tool_choice": original_choice,
        }
    )
    assert encoded["tool_choice"] == original_choice

    decoded = finalized.decode_payload(
        {
            "output": [
                {
                    "type": "function_call",
                    "name": new_alias,
                    "call_id": "collision-call",
                    "item_id": "collision-item",
                    "arguments": "{}",
                }
            ]
        }
    )
    assert decoded["output"][0]["namespace"] == "vendor"
    assert decoded["output"][0]["name"] == "run"


def test_native_name_collision_is_inverse_decoded_on_stream() -> None:
    declaration = _namespace(namespace="vendor", tool="run")
    initial = build_tool_compatibility_plan(
        [declaration],
        selected_protocol="chat_tools",
        protocol_capabilities=ProtocolCapabilities.chat_tools(),
        request_token="native-stream-collision",
    )
    old_alias = initial.entries[0].aliases[0]
    finalized = initial.with_final_declarations(
        [{"type": "function", "name": old_alias}],
        tool_choice=old_alias,
    )
    alias = finalized.entries[0].aliases[0]
    stream = CompatibilityStreamState(finalized)
    added = stream.decode_events_for_event(
        {
            "type": "response.output_item.added",
            "item": {
                "type": "function_call",
                "name": alias,
                "call_id": "stream-call",
                "item_id": "stream-item",
                "arguments": "",
            },
        }
    )
    assert added[0]["item"]["namespace"] == "vendor"
    assert added[0]["item"]["name"] == "run"


def test_repeated_native_collisions_keep_the_explicit_native_tool_choice() -> None:
    declaration = _namespace(namespace="vendor", tool="run")
    initial = build_tool_compatibility_plan(
        [declaration],
        selected_protocol="chat_tools",
        protocol_capabilities=ProtocolCapabilities.chat_tools(),
        request_token="native-recollision",
    )
    first = initial.with_final_declarations(
        [{"type": "function", "name": initial.entries[0].aliases[0]}],
        tool_choice=initial.entries[0].aliases[0],
    )
    second = first.with_final_declarations(
        [{"type": "function", "name": first.entries[0].aliases[0]}],
        tool_choice=initial.entries[0].aliases[0],
    )

    current_alias = second.entries[0].aliases[0]
    assert current_alias not in {initial.entries[0].aliases[0], first.entries[0].aliases[0]}
    encoded = second.encode_payload(
        {
            "tools": [declaration],
            "tool_choice": initial.entries[0].aliases[0],
        }
    )
    assert encoded["tool_choice"] == initial.entries[0].aliases[0]


def test_namespace_alias_count_is_not_limited_by_collision_attempt_budget() -> None:
    declaration = {
        "type": "namespace",
        "name": "mcp__bulk",
        "tools": [
            {"type": "function", "name": f"tool_{index}", "parameters": {"type": "object"}}
            for index in range(129)
        ],
    }
    plan = build_tool_compatibility_plan(
        [declaration],
        selected_protocol="chat_tools",
        # A single probe is enough when there are no collisions.  This also
        # proves the limit is per allocation, not a request-wide alias count.
        protocol_capabilities=ProtocolCapabilities.chat_tools(max_alias_attempts=1),
        request_token="bulk-namespace-aliases",
    )

    aliases = plan.entries[0].aliases
    assert len(aliases) == 129
    assert len(set(aliases)) == 129
    assert aliases[-1].rsplit("_", 1)[-1] == "129"


def test_namespace_child_names_are_part_of_final_declaration_identity() -> None:
    initial = build_tool_compatibility_plan(
        [_namespace(namespace="vendor", tool="run")],
        selected_protocol="chat_tools",
        protocol_capabilities=ProtocolCapabilities.chat_tools(),
        request_token="declaration-child-key",
    )
    finalized = initial.with_final_declarations(
        [
            _namespace(namespace="vendor", tool="stop"),
        ]
    )
    assert len(finalized.entries) == 2
    assert finalized.entries[0].child_names == ("run",)
    assert finalized.entries[1].child_names == ("stop",)


def test_native_plain_name_wins_over_unqualified_namespace_child() -> None:
    plain = {"type": "function", "name": "run"}
    namespace = {
        "type": "namespace",
        "name": "vendor",
        "tools": [{"type": "function", "name": "run"}],
    }
    plan = build_tool_compatibility_plan(
        [plain, namespace],
        selected_protocol="chat_tools",
        protocol_capabilities=ProtocolCapabilities.chat_tools(),
        request_token="plain-namespace-collision",
    )

    decoded = plan.decode_payload(
        {
            "output": [
                {
                    "type": "function_call",
                    "name": "run",
                    "call_id": "plain-call",
                    "item_id": "plain-item",
                    "arguments": "{}",
                }
            ]
        }
    )

    assert decoded["output"][0]["name"] == "run"
    assert "namespace" not in decoded["output"][0]
    assert plan.encode_payload(
        {
            "input": [
                {
                    "type": "function_call",
                    "namespace": "vendor",
                    "name": "run",
                    "call_id": "namespace-call",
                    "arguments": "{}",
                }
            ]
        }
    )["input"][0]["name"].startswith("__codexhub_ns_")


def test_ambiguous_unqualified_namespace_child_fails_closed() -> None:
    declarations = [
        {
            "type": "namespace",
            "name": "vendor_a",
            "tools": [{"type": "function", "name": "run"}],
        },
        {
            "type": "namespace",
            "name": "vendor_b",
            "tools": [{"type": "function", "name": "run"}],
        },
    ]
    plan = build_tool_compatibility_plan(
        declarations,
        selected_protocol="chat_tools",
        protocol_capabilities=ProtocolCapabilities.chat_tools(),
        request_token="ambiguous-namespace-child",
    )

    with pytest.raises(ToolCompatibilityError):
        plan.decode_payload(
            {
                "output": [
                    {
                        "type": "function_call",
                        "name": "run",
                        "call_id": "ambiguous-call",
                        "item_id": "ambiguous-item",
                        "arguments": "{}",
                    }
                ]
            }
        )


def test_named_tool_choice_requires_an_exact_declaration_match() -> None:
    with pytest.raises(RequiredToolUnavailableError):
        build_tool_compatibility_plan(
            [{
                "type": "namespace",
                "name": "vendor",
                "tools": [{"type": "function", "name": "run"}],
            }],
            selected_protocol="chat_tools",
            protocol_capabilities=ProtocolCapabilities.chat_tools(),
            tool_choice={"type": "function", "name": "run"},
            request_token="unqualified-choice",
        )


@pytest.mark.parametrize("format_value", [None, [], "text"])
def test_custom_declaration_requires_mapping_format(format_value: object) -> None:
    with pytest.raises(ToolCompatibilityError):
        build_tool_compatibility_plan(
            [{"type": "custom", "name": "paint", "format": format_value}],
            selected_protocol="chat_tools",
            protocol_capabilities=ProtocolCapabilities.chat_tools(),
            request_token="custom-format-boundary",
        )


def test_custom_adapter_uses_exact_inverse_envelopes() -> None:
    declaration = {"type": "custom", "name": "paint", "format": {"type": "text"}}
    plan = build_tool_compatibility_plan(
        [declaration],
        selected_protocol="chat_tools",
        protocol_capabilities=ProtocolCapabilities.chat_tools(),
        request_token="custom",
    )
    alias = plan.entries[0].aliases[0]
    encoded = plan.encode_payload(
        {
            "tools": [declaration],
            "input": [
                {
                    "type": "custom_tool_call",
                    "name": "paint",
                    "call_id": "call_custom",
                    "item_id": "item_custom",
                    "input": "raw <opaque> value",
                },
                {
                    "type": "custom_tool_call_output",
                    "call_id": "call_custom",
                    "item_id": "item_custom_out",
                    "output": {"opaque": [1, 2]},
                },
            ],
        }
    )
    assert encoded["tools"][0]["name"] == alias
    assert set(json.loads(encoded["input"][0]["arguments"])) == {"__codexhub_custom_input"}
    assert set(json.loads(encoded["input"][1]["output"])) == {"__codexhub_custom_output"}

    decoded = plan.decode_payload({"output": encoded["input"]})
    assert decoded["output"][0]["type"] == "custom_tool_call"
    assert decoded["output"][0]["input"] == "raw <opaque> value"
    assert decoded["output"][1]["type"] == "custom_tool_call_output"
    assert decoded["output"][1]["output"] == {"opaque": [1, 2]}


def test_stream_assembly_rejects_unknown_alias_and_duplicate_terminal() -> None:
    plan = build_tool_compatibility_plan(
        [_namespace("ns")],
        selected_protocol="chat_tools",
        protocol_capabilities=ProtocolCapabilities.chat_tools(),
        request_token="stream",
    )
    state = CompatibilityStreamState(plan)
    with pytest.raises(ToolCompatibilityError):
        state.decode_event(
            {
                "type": "response.output_item.added",
                "item": {"type": "function_call", "name": "unknown_alias", "item_id": "item"},
            }
        )

    state = CompatibilityStreamState(plan)
    alias = plan.entries[0].aliases[0]
    event = {"type": "response.completed", "response": {"id": "resp"}}
    state.decode_event(
        {
            "type": "response.output_item.added",
            "item": {
                "type": "function_call",
                "name": alias,
                "call_id": "call",
                "item_id": "item",
            },
        }
    )
    state.decode_event(
        {
            "type": "response.function_call_arguments.done",
            "item_id": "item",
            "call_id": "call",
            "arguments": "{}",
        }
    )
    state.decode_event(
        {
            "type": "response.output_item.done",
            "item": {"type": "function_call", "name": alias, "call_id": "call", "item_id": "item", "arguments": "{}"},
        }
    )
    state.decode_event(event)
    with pytest.raises(ToolCompatibilityError):
        state.decode_event(event)


def test_diagnostics_are_bounded_and_do_not_capture_declaration_values() -> None:
    secret = "SECRET_PROMPT"
    plan = build_tool_compatibility_plan(
        [{"type": "future_kind", "name": secret, "opaque": {"secret": secret}}],
        selected_protocol="text_compat",
        request_token="diagnostics",
    )
    diagnostics = repr(plan.diagnostics)
    assert secret not in diagnostics
    assert plan.diagnostics.counts == (("unknown_future_kind", OMIT, 1),)


def test_global_required_choice_needs_one_available_tool_not_every_declaration() -> None:
    plan = build_tool_compatibility_plan(
        [
            {"type": "function", "name": "available"},
            {"type": "web_search", "executor": "selected_provider"},
        ],
        selected_protocol="chat_tools",
        tool_choice="required",
        request_token="global-required",
    )

    assert [entry.disposition for entry in plan.entries] == [NATIVE, OMIT]

    with pytest.raises(RequiredToolUnavailableError):
        build_tool_compatibility_plan(
            [{"type": "web_search", "executor": "selected_provider"}],
            selected_protocol="chat_tools",
            tool_choice="required",
            request_token="global-required-empty",
        )


def test_omitted_declarations_remove_their_call_and_result_history_pairs() -> None:
    declarations = [
        {"type": "function", "name": "plain"},
        _namespace("unsupported_namespace", tool="run"),
        {"type": "custom", "name": "freeform", "format": {"type": "text"}},
        {"type": "tool_search", "execution": "client"},
        {"type": "web_search", "executor": "selected_provider"},
    ]
    plan = build_tool_compatibility_plan(
        declarations,
        selected_protocol="none",
        request_token="omit-history",
    )
    payload = {
        "tools": declarations,
        "input": [
            {"type": "function_call", "name": "plain", "call_id": "call_plain", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "call_plain", "output": "plain"},
            {
                "type": "function_call",
                "namespace": "unsupported_namespace",
                "name": "run",
                "call_id": "call_ns",
                "arguments": "{}",
            },
            {"type": "function_call_output", "call_id": "call_ns", "output": "ns"},
            {"type": "custom_tool_call", "name": "freeform", "call_id": "call_custom", "input": "raw"},
            {"type": "custom_tool_call_output", "call_id": "call_custom", "output": "custom"},
            {"type": "tool_search_call", "call_id": "call_search", "arguments": {"query": "x"}},
            {"type": "tool_search_output", "call_id": "call_search", "execution": "client", "tools": []},
            {"type": "web_search_call", "id": "web-item", "call_id": "call_web", "status": "completed"},
            {"type": "message", "role": "user", "content": "keep"},
        ],
    }

    encoded = plan.encode_payload(payload)

    assert encoded["tools"] == []
    assert encoded["input"] == [{"type": "message", "role": "user", "content": "keep"}]


def test_custom_stream_buffers_the_function_envelope_before_emitting_native_events() -> None:
    plan = build_tool_compatibility_plan(
        [{"type": "custom", "name": "paint", "format": {"type": "text"}}],
        selected_protocol="chat_tools",
        protocol_capabilities=ProtocolCapabilities.chat_tools(),
        request_token="buffered-custom",
    )
    alias = plan.entries[0].aliases[0]
    state = CompatibilityStreamState(plan)
    added = {
        "type": "response.output_item.added",
        "output_index": 0,
        "item": {
            "type": "function_call",
            "id": "item_custom",
            "call_id": "call_custom",
            "name": alias,
            "arguments": "",
        },
    }
    first_delta = {
        "type": "response.function_call_arguments.delta",
        "item_id": "item_custom",
        "output_index": 0,
        "delta": '{"__codexhub_custom_',
    }
    second_delta = {
        "type": "response.function_call_arguments.delta",
        "item_id": "item_custom",
        "output_index": 0,
        "delta": 'input":"opaque"}',
    }
    arguments_done = {
        "type": "response.function_call_arguments.done",
        "item_id": "item_custom",
        "output_index": 0,
        "arguments": '{"__codexhub_custom_input":"opaque"}',
    }
    item_done = {
        "type": "response.output_item.done",
        "output_index": 0,
        "item": {
            "type": "function_call",
            "id": "item_custom",
            "call_id": "call_custom",
            "name": alias,
            "arguments": '{"__codexhub_custom_input":"opaque"}',
        },
    }

    assert state.decode_events_for_event(added) == []
    assert state.decode_events_for_event(first_delta) == []
    assert state.decode_events_for_event(second_delta) == []
    assert state.decode_events_for_event(arguments_done) == []
    emitted = state.decode_events_for_event(item_done)

    assert [event["type"] for event in emitted] == [
        "response.output_item.added",
        "response.custom_tool_call_input.delta",
        "response.custom_tool_call_input.done",
        "response.output_item.done",
    ]
    assert emitted[0]["item"]["type"] == "custom_tool_call"
    assert emitted[0]["item"]["name"] == "paint"
    assert emitted[1]["delta"] == "opaque"
    assert emitted[2]["input"] == "opaque"
    assert emitted[3]["item"]["input"] == "opaque"


def test_custom_stream_rejects_a_bad_envelope_before_emitting_any_native_fragment() -> None:
    plan = build_tool_compatibility_plan(
        [{"type": "custom", "name": "paint", "format": {"type": "text"}}],
        selected_protocol="chat_tools",
        protocol_capabilities=ProtocolCapabilities.chat_tools(),
        request_token="bad-custom",
    )
    alias = plan.entries[0].aliases[0]
    state = CompatibilityStreamState(plan)
    assert state.decode_events_for_event(
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
    ) == []

    with pytest.raises(ToolCompatibilityError) as raised:
        state.decode_events_for_event(
            {
                "type": "response.function_call_arguments.done",
                "item_id": "item_custom",
                "arguments": '{"wrong":"value"}',
            }
        )

    assert raised.value.surface == "stream"
