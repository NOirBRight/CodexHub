"""Client search results must become callable on both provider protocols."""

import copy
import json

import pytest

import catalog_sync
import gateway_compat
from catalog import CatalogPolicy
from tool_discovery import promote_client_search_results


SEARCH = {"type": "tool_search", "execution": "client", "description": "Find deferred tools",
          "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}}
NAMESPACE = {"type": "namespace", "name": "mcp__probe", "tools": [
    {"type": "function", "name": "lookup", "description": "Look up a parcel", "defer_loading": True,
     "parameters": {"type": "object", "properties": {}}},
]}


def discovered_payload():
    return {"model": "probe", "tools": [copy.deepcopy(SEARCH)], "input": [
        {"type": "tool_search_call", "call_id": "search", "execution": "client", "arguments": {"query": "parcel"}},
        {"type": "tool_search_output", "call_id": "search", "execution": "client", "status": "completed", "tools": [copy.deepcopy(NAMESPACE)]},
    ]}


@pytest.mark.parametrize("protocol", ["responses_structured", "chat_tools"])
@pytest.mark.parametrize("strategy", ["eager", "deferred_core"])
def test_search_result_becomes_callable_and_replays(protocol, strategy):
    original = discovered_payload()
    upstream = {"name": "fixture", "upstream_format": "responses", "tool_protocol": protocol,
                "tool_surface_strategy": strategy}
    context = {}
    wire = json.loads(gateway_compat.compatible_request_body(
        json.dumps(original).encode(), upstream, event_context=context, inject_codex_tools=False,
    ))
    discovered = next(t for t in wire["tools"] if t.get("description") == "Look up a parcel")
    assert "defer_loading" not in discovered
    assert discovered["name"].startswith("__codexhub_ns_")
    assert next(t for t in wire["tools"] if t["name"].startswith("__codexhub_search_"))["description"] == SEARCH["description"]
    plan = context["_runtime_tool_compatibility_plan"]
    call = {"type": "function_call", "call_id": "lookup", "name": discovered["name"], "arguments": "{}"}
    client = plan.decode_payload({"output": [call]})["output"][0]
    assert client["namespace"] == "mcp__probe" and client["name"] == "lookup"
    original["input"] += [client, {"type": "function_call_output", "call_id": "lookup", "output": "delivered"}]
    replay = json.loads(gateway_compat.compatible_request_body(
        json.dumps(original).encode(), upstream, event_context={}, inject_codex_tools=False,
    ))
    assert replay["input"][-2] == call
    assert replay["input"][-1]["output"] == "delivered"


def test_discoveries_merge_without_expanding_unrequested_children_or_mutating_results():
    value = discovered_payload()
    result = copy.deepcopy(value["input"][-1])
    names, changed = promote_client_search_results(value)
    assert changed and names == {"mcp__probe"}
    assert value["input"][-1] == result
    assert len(value["tools"][-1]["tools"]) == 1
    assert promote_client_search_results(value) == (names, False)
    value["input"][-1]["tools"][0]["tools"].append({"type": "function", "name": "another", "parameters": {"type": "object"}})
    assert promote_client_search_results(value)[1]
    assert [t["name"] for t in value["tools"][-1]["tools"]] == ["lookup", "another"]


@pytest.mark.parametrize("provider", ["xai", "volc", "minimax-cn", "kimi", "kimi-cn", "commandcode", "opencode-go", "custom-provider"])
@pytest.mark.parametrize("fallback", [None, {"supports_search_tool": False}])
def test_external_catalog_enables_client_search_independent_of_legacy_seed(provider, fallback):
    value = {"alias": provider + "/probe", "provider_alias": provider, "upstream_name": provider,
             "upstream_model": "probe", "upstream_format": "chat_completions"}
    model = catalog_sync.build_external_provider_model(value, CatalogPolicy(set(), set(), {}), fallback)
    assert model["supports_search_tool"] is True
    value["tool_protocol"] = "none"
    assert catalog_sync.build_external_provider_model(value, CatalogPolicy(set(), set(), {}), fallback)["supports_search_tool"] is False


def test_ollama_catalog_enables_client_search_with_old_seed():
    model = catalog_sync.build_ollama_model("probe", CatalogPolicy(set(), set(), {}), {}, {"supports_search_tool": False})
    assert model["supports_search_tool"] is True
