"""Deterministic checks for the isolated Messages representation prototype (#74).

Two fixture classes, kept visibly separate:

* :func:`observed_shape_body` mirrors the *top-level field set, option set,
  system-block array, message roles, and block types* recorded from the
  installed Claude Code CLI v2.1.278 during isolated loopback observation
  (see the evidence note). Values are synthetic replacements.
* Adversarial/synthetic cases (unknown fields, unsupported block types,
  mid-conversation ordering, thinking blocks) are built inline and labelled as
  such; they are not presented as observed Claude Code behavior.

No prompt text, credential, or user content is stored in this file.
"""

from __future__ import annotations

import json

import pytest

from anthropic_messages_prototype import (
    Adapted,
    NotForwardable,
    NATIVE_GATEWAY_REWRITES,
    classify_headers,
    parse_request,
)

# Top-level keys recorded from Claude Code v2.1.278 POST /v1/messages?beta=true.
OBSERVED_TOP_LEVEL_KEYS = {
    "context_management",
    "max_tokens",
    "messages",
    "metadata",
    "model",
    "output_config",
    "safeguards",
    "stream",
    "system",
    "thinking",
    "tools",
}


def observed_shape_body() -> bytes:
    """Observed top-level shape, observed roles/block types, synthetic values."""

    return json.dumps(
        {
            "model": "claude-synthetic-1",
            "max_tokens": 32000,
            "stream": True,
            "system": [
                {"type": "text", "text": "<attribution block>"},
                {"type": "text", "text": "system block", "cache_control": {"type": "ephemeral"}},
            ],
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "read the file"}],
                },
                # Turn 2 of the observed tool lifecycle: the client echoes the
                # assistant tool_use and its user tool_result (observed roles and
                # block types; ids and bodies are synthetic).
                {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "toolu_bdrk_synthetic.1", "name": "Read",
                         "input": {"file_path": "/tmp/synthetic.txt"}},
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "toolu_bdrk_synthetic.1",
                         "content": [{"type": "text", "text": "file body"}]},
                    ],
                },
            ],
            "tools": [
                {"name": "Read", "description": "read a file", "input_schema": {"type": "object"},
                 "cache_control": {"type": "ephemeral"}},
            ],
            "thinking": {"type": "adaptive", "display": "summarized"},
            "output_config": {"effort": "high"},
            "context_management": {"edits": []},
            "metadata": {"user_id": "synthetic"},
            # Observed shape on turn 1 only (list of classifier-context blocks).
            "safeguards": [{"type": "dangerous_tool_use", "classifier_context": {}}],
        },
        separators=(",", ":"),
    ).encode("utf-8")


def observed_headers() -> dict[str, str]:
    return {
        "Authorization": "Bearer synthetic",
        "content-type": "application/json",
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "claude-code-20250219,context-management-2025-06-27",
        "X-Claude-Code-Session-Id": "00000000-0000-0000-0000-000000000000",
        "X-Stainless-Retry-Count": "0",
        "x-app": "cli",
        "x-unknown-future": "1",
    }


def test_fixture_matches_the_observed_top_level_key_set() -> None:
    assert set(json.loads(observed_shape_body())) == OBSERVED_TOP_LEVEL_KEYS


def test_native_pass_through_is_byte_exact() -> None:
    body = observed_shape_body()
    request = parse_request(body)
    assert request.to_native_body() == body
    plan = request.to_native_body()
    assert b"cache_control" in plan and b"safeguards" in plan


def test_native_rewrites_are_named_not_implicit() -> None:
    assert any(entry.startswith("model:") for entry in NATIVE_GATEWAY_REWRITES)
    assert any("credential" in entry for entry in NATIVE_GATEWAY_REWRITES)


def test_representation_keeps_every_field_including_unmodelled() -> None:
    body = observed_shape_body()
    request = parse_request(body)
    assert request.reserialize_body() != body  # re-serialization is not the wire path
    assert json.loads(request.reserialize_body()) == json.loads(body)
    assert request.unmodelled_fields == ()


def test_unknown_top_level_field_is_reported_not_dropped() -> None:
    # Adversarial/synthetic: a future field the prototype does not model.
    body = json.loads(observed_shape_body())
    body.pop("safeguards")
    body["future_beta_option"] = {"enabled": True}
    request = parse_request(json.dumps(body).encode())
    assert request.unmodelled_fields == ("future_beta_option",)
    assert request.to_native_body() == json.dumps(body).encode()
    converted = request.to_chat_request()
    assert isinstance(converted, NotForwardable)
    assert converted.fields == ("future_beta_option",)


def test_content_block_order_and_opaque_tool_id_round_trip() -> None:
    body = json.loads(observed_shape_body())
    body.pop("safeguards")
    request = parse_request(json.dumps(body).encode())
    assistant = request.messages[1]
    assert [block.type for block in assistant.content] == ["tool_use"]
    assert assistant.content[0].data["id"] == "toolu_bdrk_synthetic.1"
    converted = request.to_chat_request()
    assert isinstance(converted, Adapted)
    chat = json.loads(converted.body)
    tool_call = next(m for m in chat["messages"] if m.get("tool_calls"))["tool_calls"][0]
    assert tool_call["id"] == "toolu_bdrk_synthetic.1"
    tool_result = next(m for m in chat["messages"] if m["role"] == "tool")
    assert tool_result["tool_call_id"] == "toolu_bdrk_synthetic.1"


def test_safety_control_is_never_adapted_away() -> None:
    request = parse_request(observed_shape_body())
    converted = request.to_chat_request()
    assert isinstance(converted, NotForwardable)
    assert converted.fields == ("safeguards",)
    assert converted.diagnostic() == "unsupported_for_chat_conversion: safeguards"


def test_effort_maps_through_the_chat_seam_instead_of_being_dropped() -> None:
    body = json.loads(observed_shape_body())
    body.pop("safeguards")
    request = parse_request(json.dumps(body).encode())
    converted = request.to_chat_request()
    assert isinstance(converted, Adapted)
    policies = {adaptation.field: adaptation.policy for adaptation in converted.adaptations}
    assert policies["output_config.effort"] == "output_config_effort_mapped_to_chat_reasoning_effort"
    assert json.loads(converted.body)["reasoning_effort"] == "high"
    responses = request.to_responses_request()
    assert isinstance(responses, Adapted)
    assert json.loads(responses.body)["reasoning"] == {"effort": "high"}


def test_output_config_with_unmapped_keys_fails_closed() -> None:
    body = json.loads(observed_shape_body())
    body.pop("safeguards")
    body["output_config"] = {"effort": "high", "task_budget": 100}
    converted = parse_request(json.dumps(body).encode()).to_chat_request()
    assert isinstance(converted, NotForwardable)
    assert converted.fields == ("output_config.task_budget",)


def test_every_omitted_field_has_a_named_policy() -> None:
    body = json.loads(observed_shape_body())
    body.pop("safeguards")  # covered by its own fail-closed test
    request = parse_request(json.dumps(body).encode())
    converted = request.to_chat_request()
    assert isinstance(converted, Adapted)
    policies = {adaptation.field: adaptation.policy for adaptation in converted.adaptations}
    assert policies["context_management"] == "context_management_omitted_for_chat"
    assert policies["metadata"] == "request_metadata_omitted_for_chat"
    assert policies["thinking"] == "adaptive_thinking_not_representable_in_chat"
    assert policies["system"] == "system_block_array_joined_for_chat"
    assert policies["system[1].cache_control"] == "system_block_metadata_dropped_for_chat"
    assert policies["tools[0].cache_control"] == "client_tool_metadata_dropped_for_chat"
    chat = json.loads(converted.body)
    declared = set(policies) | {"messages"}
    for name in json.loads(request.to_native_body()):
        covered = name in chat or name in declared or any(entry.startswith(f"{name}.") for entry in declared)
        assert covered, name
    assert all(adaptation.diagnostic() for adaptation in converted.adaptations)


def test_unrepresentable_content_is_not_forwardable() -> None:
    # Adversarial/synthetic: a server-tool block has no client-side equivalent here.
    body = json.loads(observed_shape_body())
    body.pop("safeguards")
    body["messages"][0]["content"].append(
        {"type": "server_tool_use", "id": "srvtoolu_1", "name": "web_search"}
    )
    converted = parse_request(json.dumps(body).encode()).to_chat_request()
    assert isinstance(converted, NotForwardable)
    assert "user_content_block:server_tool_use" in converted.fields


def test_future_tool_choice_is_not_forwardable() -> None:
    # Adversarial/synthetic: tool_choice was not present in the observed request.
    body = json.loads(observed_shape_body())
    body.pop("safeguards")
    body["tool_choice"] = {"type": "future_choice"}
    converted = parse_request(json.dumps(body).encode()).to_chat_request()
    assert isinstance(converted, NotForwardable)
    assert "tool_choice" in converted.fields


def test_mid_conversation_system_is_declared_when_flattened() -> None:
    # Observed role in v2.1.278 traffic; the ordering loss is named, not silent.
    body = json.loads(observed_shape_body())
    body.pop("safeguards")
    body["messages"].append({"role": "system", "content": [{"type": "text", "text": "mid-conversation note"}]})
    request = parse_request(json.dumps(body).encode())
    assert request.messages[-1].role == "system"
    converted = request.to_chat_request()
    assert isinstance(converted, Adapted)
    policies = {adaptation.field: adaptation.policy for adaptation in converted.adaptations}
    assert policies["messages[].role=system"] == "mid_conversation_system_flattened_to_chat_system"


def test_responses_path_reuses_the_existing_chat_seam() -> None:
    body = json.loads(observed_shape_body())
    body.pop("safeguards")
    body["messages"].append({"role": "system", "content": [{"type": "text", "text": "mid-conversation note"}]})
    request = parse_request(json.dumps(body).encode())
    converted = request.to_responses_request()
    assert isinstance(converted, Adapted)
    responses = json.loads(converted.body)
    assert responses["model"] == "claude-synthetic-1"
    assert responses["max_output_tokens"] == 32000
    assert responses["stream"] is True
    assert responses["instructions"] == (
        "<attribution block>\n\nsystem block\n\nmid-conversation note"
    )
    assert [tool["name"] for tool in responses["tools"]] == ["Read"]
    function_call = next(item for item in responses["input"] if item["type"] == "function_call")
    assert function_call["call_id"] == "toolu_bdrk_synthetic.1"
    output = next(item for item in responses["input"] if item["type"] == "function_call_output")
    assert output["call_id"] == "toolu_bdrk_synthetic.1"


def test_header_classification_is_case_insensitive_and_open() -> None:
    plan = classify_headers(observed_headers())
    assert plan.names("credential") == ("Authorization",)
    assert set(plan.names("semantic")) == {"anthropic-version", "anthropic-beta"}
    assert set(plan.names("client_metadata")) == {
        "X-Claude-Code-Session-Id",
        "X-Stainless-Retry-Count",
        "x-app",
    }
    assert "content-type" in plan.names("transport")
    assert plan.names("unclassified") == ("x-unknown-future",)


def test_x_api_key_is_a_credential_carrier_too() -> None:
    plan = classify_headers({"x-api-key": "synthetic", "Authorization": "Bearer synthetic"})
    assert plan.names("credential") == ("x-api-key", "Authorization")


def test_diagnostics_do_not_render_credentials_or_prompt_content() -> None:
    request = parse_request(observed_shape_body())
    plan = classify_headers(observed_headers())
    assert "synthetic" not in repr(plan)
    assert "Authorization" not in repr(plan)  # neither the name nor the value
    assert "Bearer synthetic" not in repr(plan)
    assert plan.names("credential") == ("Authorization",)  # programmatic access stays
    rendered = repr(request) + repr(request.messages[0]) + repr(request.system[0])
    assert "read the file" not in rendered
    assert "file body" not in rendered
    assert "<attribution block>" not in rendered
    assert "claude-synthetic-1" in repr(request)


@pytest.mark.parametrize(
    "body",
    [
        b"[]",
        b"{",
        b'{"messages": {"role": "user"}}',
        b'{"model": 7, "messages": []}',
        b'{"messages": [{"role": "user", "content": 3}]}',
    ],
)
def test_malformed_bodies_fail_closed(body: bytes) -> None:
    with pytest.raises(ValueError):
        parse_request(body)


def _base(body: dict) -> bytes:
    body.pop("safeguards", None)
    return json.dumps(body).encode()


def test_stop_sequences_map_to_chat_stop_and_refuse_on_responses() -> None:
    body = json.loads(observed_shape_body())
    body["stop_sequences"] = ["STOP"]
    request = parse_request(_base(body))
    converted = request.to_chat_request()
    assert isinstance(converted, Adapted)
    assert json.loads(converted.body)["stop"] == ["STOP"]
    policies = {adaptation.field: adaptation.policy for adaptation in converted.adaptations}
    assert policies["stop_sequences"] == "anthropic_stop_sequences_mapped_to_chat_stop"
    # Responses has no stop parameter: the existing seam refuses, and the
    # refusal names the rejected Chat field rather than only quoting the seam.
    responses = request.to_responses_request()
    assert isinstance(responses, NotForwardable)
    assert responses.fields == ("stop",)


def test_message_level_unknown_key_is_kept_and_refused_by_name() -> None:
    body = json.loads(observed_shape_body())
    body["messages"][0]["future_message_field"] = {"x": 1}
    request = parse_request(_base(body))
    assert request.messages[0].extra == {"future_message_field": {"x": 1}}
    assert json.loads(request.reserialize_body())["messages"][0]["future_message_field"] == {"x": 1}
    converted = request.to_chat_request()
    assert isinstance(converted, NotForwardable)
    assert converted.fields == ("messages[0].future_message_field",)


def test_top_level_type_field_is_retained_not_excluded() -> None:
    body = json.loads(observed_shape_body())
    body["type"] = "message"
    request = parse_request(_base(body))
    assert request.unmodelled_fields == ("type",)
    assert json.loads(request.reserialize_body())["type"] == "message"
    assert isinstance(request.to_chat_request(), NotForwardable)


def test_failed_tool_result_is_not_downgraded_to_success() -> None:
    body = json.loads(observed_shape_body())
    body["messages"][2]["content"][0]["is_error"] = True
    converted = parse_request(_base(body)).to_chat_request()
    assert isinstance(converted, NotForwardable)
    assert converted.fields == ("messages[2].content[0].is_error",)


def test_nested_block_metadata_is_declared_not_dropped() -> None:
    body = json.loads(observed_shape_body())
    body["messages"][0]["content"][0]["cache_control"] = {"type": "ephemeral"}
    body["system"][0]["citations"] = {"enabled": True}
    body["system"][0]["future_flag"] = 1
    request = parse_request(_base(body))
    converted = request.to_chat_request()
    assert isinstance(converted, Adapted)
    policies = {adaptation.field for adaptation in converted.adaptations}
    assert "messages[0].content[0].cache_control" in policies
    assert {"system[0].citations", "system[0].future_flag"} <= policies


def test_interleaved_assistant_blocks_are_diagnosed() -> None:
    body = json.loads(observed_shape_body())
    body["messages"][1]["content"].append({"type": "text", "text": "trailing text"})
    request = parse_request(_base(body))
    assert [block.type for block in request.messages[1].content] == ["tool_use", "text"]
    converted = request.to_chat_request()
    assert isinstance(converted, Adapted)
    policies = {adaptation.field: adaptation.policy for adaptation in converted.adaptations}
    assert policies["messages[1].content"] == "assistant_block_interleaving_flattened_for_chat"
    # A non-interleaved assistant turn declares no ordering loss.
    plain = json.loads(observed_shape_body())
    plain["messages"][1]["content"] = [{"type": "text", "text": "text first"},
                                       {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {}}]
    clean = parse_request(_base(plain)).to_chat_request()
    assert isinstance(clean, Adapted)
    assert "assistant_block_interleaving_flattened_for_chat" not in {
        adaptation.policy for adaptation in clean.adaptations
    }
