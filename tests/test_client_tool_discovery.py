"""Client search results must become callable on both provider protocols."""

import copy
import json

import pytest

import catalog_sync
import gateway_compat
from catalog import CatalogPolicy
from gateway_compat.official_passthrough import request_tool_plan
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
    plan = request_tool_plan(context)
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


@pytest.mark.parametrize("protocol", ["responses_structured", "chat_tools"])
def test_existing_discovered_namespace_becomes_callable_without_defer_loading(protocol):
    value = discovered_payload()
    value["tools"].append(copy.deepcopy(NAMESPACE))
    original_history = copy.deepcopy(value["input"])
    upstream = {"name": "fixture", "upstream_format": "responses", "tool_protocol": protocol,
                "tool_surface_strategy": "eager"}
    wire = json.loads(gateway_compat.compatible_request_body(
        json.dumps(value).encode(), upstream, event_context={}, inject_codex_tools=False,
    ))
    discovered = next(tool for tool in wire["tools"] if tool.get("description") == "Look up a parcel")
    assert "defer_loading" not in discovered
    assert value["input"] == original_history


@pytest.mark.parametrize("upstream_format,tool_protocol,expected", [
    ("responses", "auto", True), ("chat_completions", "auto", True),
    ("auto", "auto", True),
    ("unknown", "auto", False), ("responses", "text_compat", False),
    ("chat_completions", "none", False), ("responses", "none", False),
    ("chat_completions", "chat_tools", True), ("responses", "responses_structured", True),
])
def test_catalog_discovery_matches_function_protocol(upstream_format, tool_protocol, expected):
    value = {"alias": "fixture/model", "provider_alias": "fixture", "upstream_name": "fixture",
             "upstream_model": "model", "upstream_format": upstream_format, "tool_protocol": tool_protocol}
    model = catalog_sync.build_external_provider_model(value, CatalogPolicy(set(), set(), {}), None)
    assert model["supports_search_tool"] is expected


@pytest.mark.parametrize("upstream_format,tool_protocol,expected", [
    ("responses", "auto", True), ("chat_completions", "auto", True),
    ("auto", "auto", True), ("unknown", "auto", False),
    ("responses", "text_compat", False), ("responses", "none", False),
])
def test_ollama_discovery_respects_provider_protocol_metadata(upstream_format, tool_protocol, expected):
    metadata = catalog_sync.ollama_provider_model_metadata([{
        "upstream_model": "probe", "upstream_format": upstream_format, "tool_protocol": tool_protocol,
    }])
    model = catalog_sync.build_ollama_model("probe", CatalogPolicy(set(), set(), {}), {}, None, metadata)
    assert model["supports_search_tool"] is expected


def test_malformed_discovered_namespace_is_ignored_without_mutating_history():
    value = discovered_payload()
    value["input"][-1]["tools"][0]["tools"] = None
    original = copy.deepcopy(value)
    assert promote_client_search_results(value) == (set(), False)
    assert value == original


@pytest.mark.parametrize("kind", ["function", "custom"])
def test_existing_discovered_function_is_promoted_without_replacing_its_schema(kind):
    value = discovered_payload()
    existing = {"type": kind, "name": "lookup", "description": "current", "defer_loading": True}
    value["tools"].append(existing)
    value["input"][-1]["tools"] = [{**existing, "description": "historical"}]
    history = copy.deepcopy(value["input"])
    assert promote_client_search_results(value) == (set(), True)
    assert value["tools"][-1] == {"type": kind, "name": "lookup", "description": "current"}
    assert value["input"] == history
    assert existing["defer_loading"] is True
    assert promote_client_search_results(value) == (set(), False)


@pytest.mark.parametrize("ollama", [False, True])
@pytest.mark.parametrize("facts,expected", [
    ({"function_lifecycle": False}, False),
    ({"supports_functions": False}, False),
    ({"accepts_tool_search_adapter": False}, False),
    ({"tool_search_adapter": False}, False),
    ({"function_lifecycle": True}, True),
    ({"tool_search_lifecycle": True, "accepts_tool_search_adapter": False}, True),
])
def test_catalog_discovery_respects_explicit_lifecycle_capabilities(ollama, facts, expected):
    value = {"alias": "fixture/probe", "provider_alias": "fixture", "upstream_name": "fixture",
             "upstream_model": "probe", "upstream_format": "responses", "tool_protocol_capabilities": facts}
    policy = CatalogPolicy(set(), set(), {})
    if ollama:
        metadata = catalog_sync.ollama_provider_model_metadata([value])
        model = catalog_sync.build_ollama_model("probe", policy, {}, None, metadata)
    else:
        model = catalog_sync.build_external_provider_model(value, policy, None)
    assert model["supports_search_tool"] is expected
