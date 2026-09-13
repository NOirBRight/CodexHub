"""Compaction must not mix transcripts into pending structured tool groups."""
from __future__ import annotations

import json

import pytest

import gateway_compat
import gateway_stream_semantics


def parallel_history() -> list[dict]:
    # A fast command returns while the parallel agent wait is still pending.
    return [
        {"type": "function_call", "name": "exec_command", "namespace": "functions",
         "call_id": "call_command", "arguments": '{"cmd":"echo command-done"}'},
        {"type": "function_call", "name": "wait_agent", "namespace": "multi_agent_v1",
         "call_id": "call_wait", "arguments": '{"targets":["agent_fixture"],"timeout_ms":1000}'},
        {"type": "function_call_output", "call_id": "call_command", "output": "command-done"},
        {"type": "function_call_output", "call_id": "call_wait",
         "output": '{"status":{},"timed_out":true}'},
        {"type": "message", "role": "user", "content": "Summarize this history."},
    ]


@pytest.mark.parametrize("upstream_format", ["responses", "chat_completions"])
def test_compact_parallel_tool_history_is_a_complete_transcript(upstream_format):
    payload = {"model": "opencode-go/deepseek-v4.1-flash", "input": parallel_history(),
               "tools": [], "tool_choice": "auto"}
    context = {"request_kind": "compact", "compact_placeholder_authorized": True}
    gateway_stream_semantics.strip_tools_for_compact_payload(payload, event_context=context)
    adapted = json.loads(gateway_compat.compatible_request_body(
        json.dumps(payload).encode(),
        {"name": "opencode_go", "upstream_model": "deepseek-v4.1-flash",
         "upstream_format": upstream_format, "tool_protocol": "responses_structured",
         "tool_surface_strategy": "eager"},
        event_context=context, inject_codex_tools=False,
    ))
    # Console Go rejects a text message between a structured call and result,
    # even if the matching result occurs later. Compaction needs history only.
    assert [item["type"] for item in adapted["input"]] == ["message"] * 5
    text = json.dumps(adapted["input"])
    for evidence in ("exec_command", "call_command", "command-done", "wait_agent",
                     "call_wait", "agent_fixture", "timed_out"):
        assert evidence in text
    assert adapted["input"][-1]["content"] == "Summarize this history."


def test_main_generation_still_preserves_structured_agent_history():
    adapted = json.loads(gateway_compat.compatible_request_body(
        json.dumps({"model": "opencode-go/deepseek-v4.1-flash",
                    "input": parallel_history(), "tools": []}).encode(),
        {"name": "opencode_go", "upstream_model": "deepseek-v4.1-flash",
         "upstream_format": "responses", "tool_protocol": "responses_structured",
         "tool_surface_strategy": "eager"},
        event_context={"request_kind": "main_generation"}, inject_codex_tools=False,
    ))
    assert [(item["type"], item["call_id"]) for item in adapted["input"]
            if item["type"] in {"function_call", "function_call_output"}] == [
        ("function_call", "call_wait"), ("function_call_output", "call_wait"),
    ]
