"""Direct seam tests for the Gateway tool-surface adapter."""

from __future__ import annotations

import inspect
from pathlib import Path

import tool_surface_adapter
from tool_surface_adapter import (
    NODE_REPL_NAMESPACE,
    TOOL_SEARCH_EMPTY_MISS_BOUND,
    ToolSurfaceAdapter,
    ToolSurfaceFacts,
)


FORBIDDEN_SOURCE_MARKERS = (
    "import codex_proxy",
    "from codex_proxy",
    "gateway_sse",
    "transport",
    "urllib3",
    "urlopen",
    "BaseHTTPRequestHandler",
)


def _scripted_discovery_tools() -> tuple[dict, ...]:
    return (
        {
            "type": "namespace",
            "name": "multi_agent_v1",
            "description": "scripted multi-agent tools",
            "tools": [
                {
                    "type": "function",
                    "name": "spawn_agent",
                    "description": "Spawn a scripted child.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "agent_type": {"type": "string"},
                            "message": {"type": "string"},
                        },
                        "required": ["agent_type"],
                    },
                }
            ],
        },
    )


def _adapter(**overrides) -> ToolSurfaceAdapter:
    facts = overrides.pop("facts", None)
    if facts is None:
        facts = ToolSurfaceFacts(multi_agent_discovery_tools=_scripted_discovery_tools())
    return ToolSurfaceAdapter(facts=facts, **overrides)


def test_tool_surface_adapter_typed_context_is_the_seam():
    assert {
        "facts",
        "adapt_apply_patch_history",
        "compatible_internal_message",
        "transcript_message",
    } <= set(ToolSurfaceAdapter.__annotations__)
    source = Path(tool_surface_adapter.__file__).read_text(encoding="utf-8")
    for marker in FORBIDDEN_SOURCE_MARKERS:
        assert marker not in source
    assert inspect.isclass(ToolSurfaceFacts)
    assert "dispatch" not in source
    assert "getattr(self, " not in source


def test_inject_explicit_codex_tools_uses_scripted_declarations():
    adapter = _adapter()
    payload: dict = {"tools": []}
    changed = adapter.inject_explicit_codex_tools(
        payload,
        include_node_repl_tools=False,
        include_local_tool_gateway_tools=False,
        include_flattened_namespace_tools=False,
    )
    assert changed is True
    names = {tool["name"] for tool in payload["tools"]}
    assert "tool_search" in names
    assert "multi_agent_v1__spawn_agent" in names
    spawn = next(tool for tool in payload["tools"] if tool["name"] == "multi_agent_v1__spawn_agent")
    assert spawn["description"] == "Spawn a scripted child."


def test_bounded_tool_search_terminalizes_and_suppresses_identical_query():
    adapter = _adapter()
    query = "node_repl js"
    history = [
        {
            "type": "tool_search_call",
            "execution": "client",
            "call_id": "call_1",
            "arguments": {"query": query},
        },
        {"type": "tool_search_output", "call_id": "call_1", "tools": []},
        {
            "type": "tool_search_call",
            "execution": "client",
            "call_id": "call_2",
            "arguments": {"query": query},
        },
        {"type": "tool_search_output", "call_id": "call_2", "tools": []},
    ]
    terminals = adapter.bounded_empty_tool_search_terminal_calls(history)
    assert terminals["call_2"] == (query, TOOL_SEARCH_EMPTY_MISS_BOUND)
    payload = {"input": history, "tools": [dict(adapter.facts.tool_search_explicit_function_tool)]}
    assert adapter.terminalize_bounded_empty_tool_search_misses(payload, terminals) is True
    assert payload["input"][3]["terminal"] is True
    assert adapter.restrict_bounded_tool_search_queries(payload, {query}) is True
    assert payload["tools"][0]["parameters"]["properties"]["query"]["not"]["enum"] == [query]

    context = {
        "_bounded_tool_search_query_digests": frozenset({adapter.tool_search_query_digest(query)})
    }
    rewritten, changed = adapter.suppress_bounded_tool_search_calls(
        {
            "type": "tool_search_call",
            "execution": "client",
            "id": "item_new",
            "arguments": {"query": query},
        },
        context,
    )
    assert changed is True
    assert rewritten["type"] == "message"
    assert "tool_search_unavailable" in rewritten["content"][0]["text"]


def test_rewrite_structured_tool_input_uses_scripted_hooks():
    seen = []

    def adapt_history(input_items, *, event_context=None):
        seen.append(event_context)
        return input_items, {"call_patch"}, False

    def internal(item):
        return {"type": "message", "role": "developer", "content": f"internal:{item.get('name')}"}

    adapter = _adapter(
        adapt_apply_patch_history=adapt_history,
        compatible_internal_message=internal,
    )
    payload = {
        "tools": [{"type": "function", "name": "shell"}],
        "input": [
            {
                "type": "function_call",
                "namespace": "multi_agent_v1",
                "name": "spawn_agent",
                "call_id": "call_spawn",
                "arguments": {"agent_type": "worker", "message": "do work"},
            },
            {
                "type": "function_call",
                "name": "mystery_tool",
                "call_id": "call_mystery",
                "arguments": "{}",
            },
            {"type": "reasoning", "summary": []},
        ],
    }
    assert adapter.rewrite_structured_tool_input_items(payload, event_context={"trace": 1}) is True
    assert seen == [{"trace": 1}]
    assert payload["input"][0]["name"] == "multi_agent_v1__spawn_agent"
    assert "namespace" not in payload["input"][0]
    assert payload["input"][1]["role"] == "developer"
    assert all(item.get("type") != "reasoning" for item in payload["input"])


def test_rewrite_preserves_scripted_compatibility_plan_owned_items():
    class Plan:
        entries = ()

        def owns_wire_value(self, value):
            return value.get("name") == "owned_tool"

    adapter = _adapter()
    owned = {
        "type": "function_call",
        "name": "owned_tool",
        "call_id": "call_owned",
        "arguments": "{}",
    }
    payload = {"input": [owned]}
    changed = adapter.rewrite_structured_tool_input_items(payload, compatibility_plan=Plan())
    assert changed is False
    assert payload["input"][0] is owned


def test_normalize_third_party_tool_call_rewrites_aliases():
    adapter = _adapter()
    rewritten, changed = adapter.normalize_third_party_tool_call(
        {
            "type": "function_call",
            "name": f"{NODE_REPL_NAMESPACE}.js",
            "arguments": {"code": "1"},
        }
    )
    assert changed is True
    assert rewritten["namespace"] == NODE_REPL_NAMESPACE
    assert rewritten["name"] == "js"

    spawn, spawn_changed = adapter.normalize_third_party_tool_call(
        {
            "type": "function_call",
            "name": "multi_agent_v1__spawn_agent",
            "arguments": {"agent_type": "worker", "message": "hi"},
        }
    )
    assert spawn_changed is True
    assert spawn["namespace"] == "multi_agent_v1"
    assert spawn["name"] == "spawn_agent"


def test_v2_context_skips_third_party_normalization():
    adapter = _adapter()
    item = {"type": "function_call", "name": f"{NODE_REPL_NAMESPACE}.js", "arguments": {"code": "1"}}
    rewritten, changed = adapter.normalize_third_party_tool_call(
        item,
        {"collaboration_protocol": adapter.facts.collaboration_v2},
    )
    assert changed is False
    assert rewritten is item


def test_downgrade_invalid_tool_calls_uses_scripted_transcript():
    def transcript(title, item):
        return {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": f"SCRIPT:{title}:{item.get('name')}"}],
        }

    adapter = _adapter(transcript_message=transcript)
    rewritten, changed = adapter.downgrade_invalid_third_party_tool_calls(
        {"type": "function_call", "name": "bad name!", "arguments": "{}"}
    )
    assert changed is True
    assert rewritten["content"][0]["text"] == "SCRIPT:Invalid third-party function call transcript:bad name!"


def test_facade_wrappers_use_live_apply_patch_hook(monkeypatch):
    import codex_proxy

    seen = []

    def adapt(input_items, *, event_context=None):
        seen.append(event_context)
        return input_items, set(), False

    monkeypatch.setattr(codex_proxy, "_adapt_apply_patch_custom_tool_history", adapt)
    payload = {
        "input": [
            {
                "type": "function_call",
                "name": "shell",
                "call_id": "call_shell",
                "arguments": "{}",
            }
        ]
    }
    codex_proxy._rewrite_structured_tool_input_items(payload, event_context={"live": True})
    assert seen == [{"live": True}]
