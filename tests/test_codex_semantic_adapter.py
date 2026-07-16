import json
from pathlib import Path

import pytest
import codex_semantic_adapter

from codex_semantic_adapter import (
    coerce_number,
    coerce_target,
    coerce_targets,
    multi_agent_discovery_arguments,
    normalize_multi_agent_arguments,
    normalize_tool_search_arguments,
)


def _worker_binding_fixture():
    fixture_path = Path(__file__).parent / "fixtures" / "worker_effective_binding.json"
    return json.loads(fixture_path.read_text(encoding="utf-8"))


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


def test_normalize_multi_agent_spawn_preserves_worker_selector():
    value, tool_name, changed = normalize_multi_agent_arguments(
        '{"message":"do work","agent_type":"worker"}',
        "spawn_agent",
    )

    assert changed is True
    assert tool_name == "spawn_agent"
    assert json.loads(value)["agent_type"] == "worker"


@pytest.mark.parametrize(
    ("arguments", "classification"),
    [
        ({"message": "do work"}, "missing_selector"),
        ({"message": "do work", "agent_type": "synthetic-unknown"}, "unsupported_selector"),
    ],
)
def test_validate_worker_selector_fails_closed(arguments, classification):
    assert codex_semantic_adapter.validate_worker_selector(arguments) == (
        codex_semantic_adapter.BindingValidation("rejected", classification)
    )


def test_validate_effective_worker_binding_accepts_supported_exact_readback():
    fixture = _worker_binding_fixture()
    assert fixture["fixture_kind"] == "synthetic_codexhub_internal_normalized_adapter_contract"

    assert codex_semantic_adapter.validate_effective_worker_binding(
        fixture["requested"],
        fixture["readbacks"]["matching"],
    ) == codex_semantic_adapter.BindingValidation(codex_semantic_adapter.BINDING_ACCEPTED, "matched")


def test_validate_effective_worker_binding_rejects_unversioned_extensions():
    fixture = _worker_binding_fixture()
    readback = fixture["readbacks"]["matching"]
    readback["effective_binding"]["synthetic_extension"] = "not-part-of-v1"

    assert codex_semantic_adapter.validate_effective_worker_binding(
        fixture["requested"],
        readback,
    ) == codex_semantic_adapter.BindingValidation("rejected", "unknown_readback")


@pytest.mark.parametrize(
    ("readback_name", "classification"),
    [
        ("missing", "missing_readback"),
        ("unknown", "unknown_readback"),
        ("contradictory", "contradictory_binding"),
        ("rejected", "rejected_readback"),
        ("unsupported", "unsupported_readback"),
        ("gpt_substituted", "gpt_substitution"),
        ("aliased", "unknown_readback"),
    ],
)
def test_validate_effective_worker_binding_fails_closed(readback_name, classification):
    fixture = _worker_binding_fixture()

    assert codex_semantic_adapter.validate_effective_worker_binding(
        fixture["requested"],
        fixture["readbacks"][readback_name],
    ) == codex_semantic_adapter.BindingValidation("rejected", classification)


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
