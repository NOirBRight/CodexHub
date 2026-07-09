import json

from codex_semantic_adapter import (
    coerce_number,
    coerce_target,
    coerce_targets,
    multi_agent_discovery_arguments,
    normalize_multi_agent_arguments,
    normalize_tool_search_arguments,
)


def test_normalize_tool_search_arguments_accepts_json_string_limit():
    assert normalize_tool_search_arguments('{"query":"spawn tools","limit":"3"}') == {
        "query": "spawn tools",
        "limit": 3,
    }


def test_multi_agent_discovery_arguments_turns_empty_call_into_search_query():
    assert multi_agent_discovery_arguments("{}") == {
        "query": "spawn_agent multi_agent subagent native Codex",
        "limit": 8,
    }


def test_normalize_multi_agent_spawn_arguments_from_prompt_alias():
    value, tool_name, changed = normalize_multi_agent_arguments(
        '{"prompt":"do work","name":"worker","fork_context":"true","timeout_ms":"1500"}',
        "spawn_agent",
    )

    assert changed is True
    assert tool_name == "spawn_agent"
    payload = json.loads(value)
    assert payload["message"] == "do work"
    assert payload["nickname"] == "worker"
    assert payload["fork_context"] is True
    assert payload["timeout_ms"] == 1500
    assert "prompt" not in payload
    assert "name" not in payload


def test_normalize_multi_agent_wait_target_to_targets():
    value, tool_name, changed = normalize_multi_agent_arguments({"target": "agent-1"}, "wait_agent")

    assert changed is True
    assert tool_name == "wait_agent"
    assert value == {"targets": ["agent-1"]}


def test_coercion_helpers_preserve_existing_semantics():
    assert coerce_targets('"agent-1"') == (["agent-1"], True)
    assert coerce_target('["agent-1", "agent-2"]') == ("agent-1", True)
    assert coerce_number("42") == (42, True)
    assert coerce_number("2.5") == (2.5, True)
