"""Current Code Mode declaration, history and provenance boundaries."""
from __future__ import annotations
import copy
import json

import pytest

from code_mode_collaboration import (
    ObservedAssignment, declared_code_mode_handlers, expose_declared_collaboration,
    recover_observed_assignments,
)
from gateway_compat.collaboration_delivery import ALIAS, CONTEXT_KEY, make_messages_portable, decode_body, decode_sse_line
from runtime_tool_compatibility import build_tool_compatibility_plan, ToolCompatibilityError


def exec_tool(names=("spawn_agent", "send_message", "followup_task", "wait_agent")):
    signatures = {
        "spawn_agent": "fork_turns?: string; message: string; model?: string; reasoning_effort?: string; task_name: string;",
        "send_message": "message: string; target: string;",
        "followup_task": "message: string; target: string;",
        "wait_agent": "timeout_ms?: number;",
    }
    description = "Run JavaScript code to orchestrate/compose tool calls\n- All nested tools are available on the global `tools` object.\n"
    for name in names:
        description += f"\n### `collaboration__{name}`\nCaller owns this operation.\n```ts\ndeclare const tools: {{ collaboration__{name}(args: {{ {signatures[name]} }}): Promise<unknown>; }};\n```\n"
    return {"type": "custom", "name": "exec", "description": description, "format": {"type": "grammar", "syntax": "lark", "definition": "start: SOURCE\nSOURCE: /[\\s\\S]+/"}}


@pytest.mark.parametrize("embedded", [False, True])
@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("names", [("send_message",), ("wait_agent", "followup_task"), ("spawn_agent", "send_message", "followup_task", "wait_agent")])
def test_current_exec_exposes_only_actual_caller_subset(embedded, nested, names):
    tool = exec_tool(names)
    tools = [{"type": "namespace", "name": "functions", "tools": [tool]}] if nested else [tool]
    payload = {"tools": [] if embedded else tools, "input": [{"type": "additional_tools", "tools": tools}] if embedded else []}
    original = copy.deepcopy(payload)
    assert make_messages_portable(payload)
    group = payload["input"][0]["tools"] if embedded else payload["tools"]
    assert group[0] == tools[0]
    assert group[-1]["name"] == ALIAS
    assert [child["name"] for child in group[-1]["tools"]] == list(names)
    for child in group[-1]["tools"]:
        if "message" in child["parameters"]["properties"]:
            assert child["parameters"]["properties"]["message"] == {"type": "string"}
    assert payload != original
    assert tools == (original["input"][0]["tools"] if embedded else original["tools"])


@pytest.mark.parametrize("corruption", ["mention", "missing_signature", "wrong_method", "unknown_field", "wrong_required", "wrong_type", "duplicate", "unsupported", "overlarge"])
def test_description_mentions_or_unsupported_signatures_do_not_grant_tools(corruption):
    tool = exec_tool(("send_message",))
    replacements = {
        "mention": ("### `collaboration__send_message`", "Use collaboration__send_message"),
        "missing_signature": ("declare const tools", "example const tools"),
        "wrong_method": ("collaboration__send_message(args", "collaboration__spawn_agent(args"),
        "unknown_field": ("target: string;", "target: string; execute_shell?: boolean;"),
        "wrong_required": ("message: string;", "message?: string;"),
        "wrong_type": ("message: string;", "message: number;"),
        "unsupported": ("collaboration__send_message", "collaboration__unlisted"),
    }
    if corruption in replacements:
        tool["description"] = tool["description"].replace(*replacements[corruption])
    elif corruption == "duplicate":
        tool["description"] += tool["description"]
    elif corruption == "overlarge":
        tool["description"] += "x" * (512 * 1024)
    assert declared_code_mode_handlers(tool) == []
    payload = {"tools": [tool]}
    assert not expose_declared_collaboration(payload)
    assert payload == {"tools": [tool]}


def test_portable_collaboration_preserves_call_item_identity_and_same_child_results():
    tool = exec_tool(("send_message", "followup_task"))
    call = {"type": "function_call", "namespace": "collaboration", "name": "followup_task", "id": "fc_native", "call_id": "real-call", "arguments": '{"target":"/root/existing","message":"Continue with the saved result."}', "encrypted_function_args": []}
    output = {"type": "function_call_output", "id": "fco_original", "call_id": "real-call", "output": "null"}
    payload = {"tools": [tool], "input": [call, output]}
    assert make_messages_portable(payload)
    assert payload["input"] == [{**call, "namespace": ALIAS}, output]
    wire_call = {**call, "namespace": ALIAS}
    response = json.dumps({"output": [wire_call]}).encode()
    assert json.loads(decode_body(response, {CONTEXT_KEY: True}))["output"] == [call]
    line = decode_sse_line(b"data: " + response + b"\r\n", {CONTEXT_KEY: True})
    assert line.endswith(b"\r\n")
    assert json.loads(line[5:])["output"] == [call]


def agent_message(message="literal caller assignment", author="/root", recipient="/root/child"):
    return {"type": "agent_message", "id": "am_typed", "author": author, "recipient": recipient, "content": [{"type": "encrypted_content", "encrypted_content": message}]}


def test_plaintext_recovery_is_exact_addressed_and_bound_to_observed_call_item():
    assignment = ObservedAssignment("/root", "/root/child", "literal caller assignment", "call_real", "ctc_real")
    original = agent_message()
    payload = {"input": [original]}
    assert recover_observed_assignments(payload, [assignment]) == 1
    assert payload["input"] == [{**original, "content": [{"type": "input_text", "text": assignment.message}]}]
    assert original == agent_message()
    assert recover_observed_assignments(payload, [assignment]) == 0


@pytest.mark.parametrize("mutation", ["author", "recipient", "message", "missing_call", "missing_item", "ambiguous_calls", "ciphertext"])
def test_unproven_or_ambiguous_tasks_stay_opaque(mutation):
    assignment = ObservedAssignment("/root", "/root/child", "literal caller assignment", "call_real", "ctc_real")
    assignments = [assignment]
    item = agent_message()
    if mutation in {"author", "recipient"}:
        item[mutation] += "_other"
    elif mutation in {"message", "ciphertext"}:
        item["content"][0]["encrypted_content"] = "b3BhcXVl" if mutation == "ciphertext" else "literal caller assignment!"
    elif mutation == "missing_call":
        assignments = [ObservedAssignment(assignment.author, assignment.recipient, assignment.message, "", assignment.source_item_id)]
    elif mutation == "missing_item":
        assignments = [ObservedAssignment(assignment.author, assignment.recipient, assignment.message, assignment.source_call_id, "")]
    elif mutation == "ambiguous_calls":
        assignments.append(ObservedAssignment(assignment.author, assignment.recipient, assignment.message, "call_other", "ctc_other"))
    payload = {"input": [item]}
    original = copy.deepcopy(payload)
    assert recover_observed_assignments(payload, assignments) == 0
    assert payload == original


def test_opaque_only_history_fails_visibly_without_restatement_or_replaying_effects():
    payload = {"tools": [exec_tool(("followup_task",))], "input": [agent_message("b3BhcXVl")], "tool_choice": "auto"}
    assert make_messages_portable(payload)
    plan = build_tool_compatibility_plan(payload["tools"], selected_protocol="chat_completions", collaboration_protocol="collaboration_v2")
    with pytest.raises(ToolCompatibilityError, match="encrypted_agent_message_unavailable"):
        plan.encode_payload(payload)


@pytest.mark.parametrize("placement", ["tools", "additional_tools"])
@pytest.mark.parametrize("provider", ["cursor-subscription", "claude-subscription", "custom-endpoint"])
@pytest.mark.parametrize("strategy", ["eager", "deferred_core"])
def test_external_gateway_current_code_mode_subset_uses_existing_codec(placement, provider, strategy):
    import gateway_compat
    tool = exec_tool(("followup_task", "wait_agent"))
    declaration = {"type": "namespace", "name": "functions", "description": "Caller tools", "tools": [tool]}
    payload = {"model": "selected-model", "tools": [declaration] if placement == "tools" else [], "input": [{"type": "additional_tools", "tools": [declaration]}] if placement == "additional_tools" else [], "tool_choice": "auto"}
    upstream = {"name": provider, "upstream_format": "responses", "tool_protocol": "chat_tools", "tool_surface_strategy": strategy}
    context = {}
    prepared = json.loads(gateway_compat.compatible_request_body(json.dumps(payload).encode(), upstream, event_context=context, inject_codex_tools=False))
    assert context["collaboration_protocol"] == "collaboration_v2"
    assert len(prepared["tools"]) == 3
    assert all(tool["type"] == "function" for tool in prepared["tools"])
    followup = next(tool for tool in prepared["tools"] if "Caller owns this operation" in tool.get("description", "") and "message" in tool["parameters"]["properties"])
    call = {"type": "function_call", "name": followup["name"], "id": "fc_real", "call_id": "real-child-followup", "arguments": '{"target":"/root/same_child","message":"Use the prior result and continue."}'}
    response = json.loads(gateway_compat.compatible_response_body(json.dumps({"output": [call]}).encode(), provider, context))
    assert response["output"] == [{**call, "namespace": "collaboration", "name": "followup_task", "encrypted_function_args": []}]


@pytest.mark.parametrize("provider", ["cursor-subscription", "claude-subscription", "custom-endpoint"])
def test_external_gateway_opaque_task_requires_caller_restatement(provider):
    import gateway_compat
    from gateway_errors import UpstreamProtocolTranslationError
    payload = {"model": "selected-model", "tools": [exec_tool(("followup_task",))], "input": [agent_message("opaque-private-task")], "tool_choice": "auto"}
    original = copy.deepcopy(payload)
    upstream = {"name": provider, "upstream_format": "responses", "tool_protocol": "chat_tools", "tool_surface_strategy": "eager"}
    with pytest.raises(UpstreamProtocolTranslationError, match="caller must restate") as error:
        gateway_compat.compatible_request_body(json.dumps(payload).encode(), upstream, event_context={}, inject_codex_tools=False)
    assert "opaque-private-task" not in str(error.value)
    assert payload == original


@pytest.mark.parametrize("later_text", ["Continue", "Use prior results to finish the remaining task."])
def test_same_address_plaintext_is_not_proof_of_complete_task_restatement(later_text):
    import gateway_compat
    from gateway_errors import UpstreamProtocolTranslationError
    opaque = agent_message("opaque-private-task")
    later = {**agent_message(), "id": "am_later", "content": [{"type": "input_text", "text": later_text}]}
    payload = {"model": "selected-model", "tools": [exec_tool(("followup_task",))], "input": [opaque, later], "tool_choice": "auto"}
    original = copy.deepcopy(payload)
    upstream = {"name": "cursor-subscription", "upstream_format": "responses", "tool_protocol": "chat_tools", "tool_surface_strategy": "eager"}
    with pytest.raises(UpstreamProtocolTranslationError, match="caller must restate") as error:
        gateway_compat.compatible_request_body(json.dumps(payload).encode(), upstream, event_context={}, inject_codex_tools=False)
    assert "opaque-private-task" not in str(error.value)
    assert payload == original


def test_official_inverse_cannot_expand_a_child_subset_or_accept_opaque_arguments():
    import gateway_compat
    import route_primitives
    from gateway_errors import UpstreamProtocolTranslationError
    payload = {"tools": [exec_tool(("send_message",))], "input": [], "tool_choice": "auto"}
    context = {}
    prepared = json.loads(gateway_compat.compatible_request_body(json.dumps(payload).encode(), {"name": "official"}, event_context=context, behavior_profile=route_primitives.BEHAVIOR_OFFICIAL_CODEX_APP_HTTP_PASSTHROUGH))
    assert prepared["tools"][-1]["name"] == ALIAS
    assert context[CONTEXT_KEY] == ("send_message",)
    unowned = {"type": "function_call", "namespace": ALIAS, "name": "spawn_agent", "arguments": "{}"}
    with pytest.raises(UpstreamProtocolTranslationError, match="did not declare"):
        gateway_compat.compatible_response_body(json.dumps({"output": [unowned]}).encode(), "official", context)
    opaque = {**unowned, "name": "send_message", "encrypted_function_args": ["message"]}
    with pytest.raises(UpstreamProtocolTranslationError, match="opaque collaboration arguments"):
        decode_sse_line(b"data: " + json.dumps({"item": opaque}).encode() + b"\n", context)


def test_alias_collision_does_not_partially_expand_current_exec():
    payload = {"tools": [exec_tool(("send_message",)), {"type": "namespace", "name": ALIAS, "tools": []}]}
    original = copy.deepcopy(payload)
    assert not make_messages_portable(payload)
    assert payload == original
