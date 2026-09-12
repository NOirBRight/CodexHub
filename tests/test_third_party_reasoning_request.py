from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import urllib.error
import urllib.request

import pytest
from live_gateway_support import configured_live_gateway

import gateway_compat
import gateway_request
from collaboration_runtime_contract import COLLABORATION_V2, EXPECTED_PARAMETER_SCHEMAS


PATCH = "*** Begin Patch\n*** Update File: target.txt\n@@\n-before\n+after\n*** End Patch"


def test_desktop_automation_nested_union_has_provider_compatible_object_root():
    schema = json.loads((Path(__file__).parent / "fixtures/tool_schemas/codex_app_automation_update.json").read_text())
    body = json.dumps({"input": [], "tools": [{
        "type": "namespace", "name": "mcp__codex_app", "tools": [{
            "type": "function", "name": "automation_update", "parameters": schema,
        }],
    }]}).encode()
    transformed = json.loads(gateway_compat.compatible_request_body(
        body, _xai_upstream(), inject_codex_tools=False,
        event_context={},
        behavior_profile="codex_app_external_adapter",
    ))
    tool = transformed["tools"][0]
    assert tool["name"] == "__codexhub_ns_fa0cf542e6_1"
    parameters = tool["parameters"]
    assert parameters.get("type") == "object"
    assert "oneOf" not in parameters
    branches = parameters["allOf"][0]["oneOf"]
    assert len(branches) == 4
    assert len(branches[1]["oneOf"]) == 2
    assert len(branches[2]["oneOf"]) == 2
    assert branches[0]["required"] == ["mode", "id"]
    assert branches[1]["oneOf"][0]["additionalProperties"] is False


@pytest.mark.parametrize("union", ["anyOf", "oneOf"])
def test_nested_root_union_preserves_conjunctions_and_exclusivity(union):
    from gateway_compat.tool_parameter_root import coerce_tool_parameter_root

    root = {
        "type": "object", "required": ["scope"],
        "allOf": [{"minProperties": 1}],
        union: [
            {"type": "object", "required": ["a"]},
            {"type": "object", "oneOf": [
                {"type": "object", "required": ["b"]},
                {"type": "object", "required": ["c"]},
            ]},
        ],
    }
    before = copy.deepcopy(root)
    projected, changed = coerce_tool_parameter_root(root)
    assert changed
    assert root == before
    assert projected == {
        "type": "object", "required": ["scope"],
        "allOf": [{"minProperties": 1}, {union: root[union]}],
    }
    # In particular, keep the inner oneOf as one outer branch: flattening it
    # changes acceptance when a, b, and c are all present.
    assert coerce_tool_parameter_root(projected) == (projected, False)


def _xai_upstream() -> dict[str, str]:
    return {
        "name": "xai",
        "upstream_model": "grok-4.6",
        "upstream_format": "responses",
        "tool_protocol": "responses_structured",
        "tool_surface_strategy": "eager",
    }


def _v2_namespace() -> dict:
    children = []
    for name, schema in EXPECTED_PARAMETER_SCHEMAS[COLLABORATION_V2].items():
        parameters = copy.deepcopy(schema)
        if not parameters.get("required"):
            parameters.pop("required", None)
        children.append(
            {
                "type": "function",
                "name": name,
                "description": "runtime",
                "strict": False,
                "parameters": parameters,
            }
        )
    return {
        "type": "namespace",
        "name": "collaboration",
        "description": "runtime",
        "tools": children,
    }


def _codex_app_xai_history_request() -> dict:
    return {
        "model": "xai/grok-4.6",
        "include": ["reasoning.encrypted_content"],
        "tools": [
            _v2_namespace(),
            {
                "type": "function",
                "name": "shell",
                "parameters": {
                    "type": "object",
                    "properties": {"command": {"type": "string"}},
                    "required": ["command"],
                },
            },
        ],
        "tool_choice": "auto",
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "hi"}],
            },
            {
                "type": "compaction",
                "summary": {
                    "content": [
                        {"type": "input_text", "text": "Earlier nested context."},
                    ]
                },
            },
            {"type": "compaction_trigger"},
            {
                "type": "reasoning",
                "id": "rs_21",
                "content": None,
                "encrypted_content": "gAAAA-official-blob",
                "summary": [{"type": "summary_text", "text": "thought"}],
            },
            {
                "type": "reasoning",
                "id": "rs_22",
                "content": [
                    {"type": "encrypted_content", "encrypted_content": "part-blob"},
                    {"type": "reasoning_text", "text": "visible"},
                ],
                "summary": None,
            },
            {
                "type": "function_call",
                "name": "shell",
                "call_id": "call_shell",
                "arguments": json.dumps({"command": "pwd"}),
            },
            {
                "type": "function_call_output",
                "call_id": "call_shell",
                "output": "/tmp",
            },
            {
                "type": "custom_tool_call",
                "status": "completed",
                "call_id": "call_patch",
                "name": "apply_patch",
                "input": PATCH,
            },
            {
                "type": "custom_tool_call_output",
                "call_id": "call_patch",
                "output": "Success. Updated target.txt",
            },
            {
                "type": "agent_message",
                "id": "am1",
                "author": "agent/root",
                "recipient": "agent/root/worker",
                "content": [{"type": "input_text", "text": "inspect"}],
            },
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "Reply with exactly: E2E_OK"}],
            },
        ],
        "stream": True,
    }


def test_sanitize_third_party_reasoning_items_replaces_null_content_with_empty_sequence():
    payload = {
        "input": [
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            {
                "type": "reasoning",
                "id": "rs_21",
                "content": None,
                "summary": [{"type": "summary_text", "text": "thought"}],
            },
        ]
    }

    changed = gateway_request.sanitize_third_party_reasoning_items(payload)

    assert changed is True
    item = payload["input"][1]
    assert item["content"] == []
    assert item["summary"][0]["text"] == "thought"
    assert "id" not in item


def test_sanitize_third_party_reasoning_items_drops_stale_response_references():
    payload = {
        "previous_response_id": "resp_stale",
        "input": [
            {"type": "item_reference", "id": "rs_missing"},
            {"type": "reasoning", "id": "rs_missing", "summary": []},
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]},
        ],
    }

    changed = gateway_request.sanitize_third_party_reasoning_items(payload)

    assert changed is True
    assert "previous_response_id" not in payload
    assert [item.get("type") for item in payload["input"]] == ["message"]


def test_sanitize_third_party_reasoning_items_drops_empty_reasoning_after_null_summary():
    payload = {"input": [{"type": "reasoning", "id": "rs_null_summary", "summary": None}]}

    changed = gateway_request.sanitize_third_party_reasoning_items(payload)

    assert changed is True
    assert payload["input"] == []


def test_sanitize_third_party_reasoning_items_strips_encrypted_content_everywhere():
    payload = {
        "include": ["reasoning.encrypted_content"],
        "input": [
            {
                "type": "reasoning",
                "encrypted_content": "gAAAA-blob",
                "content": [
                    {"type": "encrypted_content", "encrypted_content": "part-blob"},
                    {"type": "reasoning_text", "text": "keep"},
                ],
            }
        ],
    }

    changed = gateway_request.sanitize_third_party_reasoning_items(payload)

    assert changed is True
    assert "include" not in payload
    assert "encrypted_content" not in json.dumps(payload)
    assert payload["input"][0]["content"] == []
    assert payload["input"][0]["summary"] == [{"type": "summary_text", "text": "keep"}]


def test_xai_v2_codex_app_history_is_safe_for_strict_responses_deserializers():
    context: dict = {}
    transformed = json.loads(
        gateway_compat.compatible_request_body(
            json.dumps(_codex_app_xai_history_request()).encode(),
            _xai_upstream(),
            event_context=context,
            inject_codex_tools=False,
            behavior_profile="codex_app_external_adapter",
        )
    )

    dumped = json.dumps(transformed)
    assert "encrypted_content" not in dumped
    assert "include" not in transformed
    assert context.get("collaboration_protocol") == COLLABORATION_V2
    types = [item.get("type") for item in transformed["input"]]
    assert "compaction" not in types
    assert "compaction_trigger" not in types
    assert any(
        item.get("type") == "message"
        and item.get("role") == "developer"
        and "Earlier nested context." in str(item.get("content"))
        for item in transformed["input"]
    )
    assert "reasoning" in types
    assert "function_call" in types
    assert "function_call_output" in types
    assert any(item.get("name") == "apply_patch" for item in transformed["input"] if isinstance(item, dict))
    for item in transformed["input"]:
        if item.get("type") == "reasoning":
            assert "id" not in item
            assert item.get("content") == []
            assert isinstance(item.get("summary"), list)
            for part in item["summary"]:
                assert part.get("type") == "summary_text"
                assert isinstance(part.get("text"), str)


def test_live_gateway_accepts_sanitized_xai_codex_app_history():
    if os.environ.get("CODEXHUB_SKIP_LIVE_XAI_E2E") == "1":
        pytest.skip("live xAI E2E explicitly disabled")
    gateway = configured_live_gateway()
    body = json.dumps(_codex_app_xai_history_request()).encode()
    req = urllib.request.Request(
        f"{gateway.base_url}/v1/responses",
        data=body,
        headers={
            "Authorization": f"Bearer {gateway.client_key}",
            "Content-Type": "application/json",
            "User-Agent": "codex-app",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            raw = resp.read(8000).decode("utf-8", "replace")
            assert resp.status == 200
            assert "encrypted_content" not in raw.lower() or "error" not in raw.lower()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise AssertionError(f"live xAI E2E HTTP {exc.code}: {detail[:2000]}") from exc
    except urllib.error.URLError as exc:
        raise AssertionError(f"live Gateway unavailable for xAI E2E: {exc}") from exc


def _xai_root_union_tools_request() -> dict:
    return {
        "model": "xai/grok-4.6",
        "input": [{"role": "user", "content": "Reply with the single word pong."}],
        "tools": [
            {
                "type": "function",
                "name": "__codexhub_ns_a5e9029afd_33",
                "description": "namespaced child",
                "parameters": {
                    "oneOf": [
                        {
                            "type": "object",
                            "properties": {"action": {"type": "string"}},
                            "required": ["action"],
                        },
                        {"type": "null"},
                    ],
                },
            },
            {
                "type": "function",
                "name": "note",
                "description": "nested unions stay",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "combo": {"anyOf": [{"type": "number"}, {"type": "string"}]},
                        "label": {"type": ["string", "null"]},
                    },
                },
            },
        ],
        "tool_choice": "auto",
    }


def _third_party_web_search_request(model: str, *, external_web_access: bool) -> dict:
    return {
        "model": model,
        "input": [{"role": "user", "content": "search"}],
        "tools": [
            {
                "type": "web_search",
                "external_web_access": external_web_access,
                "search_context_size": "low",
                "filters": {"allowed_domains": ["example.com"]},
            }
        ],
        "tool_choice": "auto",
    }


def test_compatible_request_drops_third_party_web_search_live_access_flag():
    # One non-official path: compatible_request_body gates on upstream_name != "official"
    # (xAI, Ollama Cloud, Volc, OpenCode Go Responses all share it).
    transformed = json.loads(
        gateway_compat.compatible_request_body(
            json.dumps(_third_party_web_search_request("grok-4.6", external_web_access=True)).encode(),
            _xai_upstream(),
            inject_codex_tools=False,
            behavior_profile="codex_app_external_adapter",
            event_context={},
        )
    )
    web_search = next(tool for tool in transformed["tools"] if tool.get("type") == "web_search")
    assert "external_web_access" not in web_search
    assert web_search["search_context_size"] == "low"
    assert web_search["filters"] == {"allowed_domains": ["example.com"]}


def test_official_passthrough_keeps_web_search_external_web_access():
    import route_primitives

    transformed = json.loads(
        gateway_compat.compatible_request_body(
            json.dumps(_third_party_web_search_request("gpt-5.6-luna", external_web_access=False)).encode(),
            {"name": "official", "upstream_model": "gpt-5.6-luna", "upstream_format": "responses"},
            inject_codex_tools=False,
            behavior_profile=route_primitives.BEHAVIOR_OFFICIAL_CODEX_APP_HTTP_PASSTHROUGH,
            event_context={},
        )
    )
    web_search = next(tool for tool in transformed["tools"] if tool.get("type") == "web_search")
    assert web_search["external_web_access"] is False


def test_compatible_request_rejects_third_party_cache_only_web_search():
    from gateway_errors import UpstreamProtocolTranslationError

    with pytest.raises(UpstreamProtocolTranslationError, match="external_web_access=false"):
        gateway_compat.compatible_request_body(
            json.dumps(_third_party_web_search_request("grok-4.6", external_web_access=False)).encode(),
            _xai_upstream(),
            inject_codex_tools=False,
            behavior_profile="codex_app_external_adapter",
            event_context={},
        )


def test_compatible_request_rewrites_xai_root_union_and_keeps_nested_unions():
    transformed = json.loads(
        gateway_compat.compatible_request_body(
            json.dumps(_xai_root_union_tools_request()).encode(),
            _xai_upstream(),
            inject_codex_tools=False,
            behavior_profile="codex_app_external_adapter",
        )
    )
    tools = {tool["name"]: tool for tool in transformed["tools"] if isinstance(tool, dict)}
    union_params = tools["__codexhub_ns_a5e9029afd_33"]["parameters"]
    assert union_params["type"] == "object"
    assert "oneOf" not in union_params
    assert union_params["properties"]["action"] == {"type": "string"}
    nested = tools["note"]["parameters"]["properties"]
    assert nested["combo"]["anyOf"] == [{"type": "number"}, {"type": "string"}]
    assert nested["label"]["type"] == ["string", "null"]


def test_live_gateway_accepts_xai_root_union_tools():
    if os.environ.get("CODEXHUB_SKIP_LIVE_XAI_E2E") == "1":
        pytest.skip("live xAI E2E explicitly disabled")
    gateway = configured_live_gateway()
    body = json.dumps(_xai_root_union_tools_request()).encode()
    req = urllib.request.Request(
        f"{gateway.base_url}/v1/responses",
        data=body,
        headers={
            "Authorization": f"Bearer {gateway.client_key}",
            "Content-Type": "application/json",
            "User-Agent": "codex-app",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            raw = resp.read(8000).decode("utf-8", "replace")
            assert resp.status == 200
            assert "tool parameter root must be an object type" not in raw
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise AssertionError(f"live xAI tool-schema E2E HTTP {exc.code}: {detail[:2000]}") from exc
    except urllib.error.URLError as exc:
        raise AssertionError(f"live Gateway unavailable for xAI tool-schema E2E: {exc}") from exc


def _codex_app_xai_namespace_request() -> dict:
    """Codex App namespace whose child schema is the live xAI 400 shape."""

    return {
        "model": "xai/grok-4.6",
        "input": [{"role": "user", "content": "update then echo"}],
        "tools": [
            {
                "type": "namespace",
                "name": "codex_app",
                "description": "desktop",
                "tools": [
                    {
                        "type": "function",
                        "name": "automation_update",
                        "description": "update automation",
                        "strict": False,
                        "parameters": {
                            "oneOf": [
                                {
                                    "type": "object",
                                    "properties": {"action": {"type": "string"}},
                                    "required": ["action"],
                                },
                                {"type": "null"},
                            ],
                        },
                    },
                    {
                        "type": "function",
                        "name": "echo",
                        "description": "echo text",
                        "strict": False,
                        "parameters": {
                            "type": "object",
                            "properties": {"text": {"type": "string"}},
                            "required": ["text"],
                        },
                    },
                ],
            }
        ],
        "tool_choice": "auto",
    }


def _encode_xai_namespace_request() -> tuple[dict, dict]:
    context: dict = {}
    encoded = json.loads(
        gateway_compat.compatible_request_body(
            json.dumps(_codex_app_xai_namespace_request()).encode(),
            _xai_upstream(),
            event_context=context,
            inject_codex_tools=False,
            behavior_profile="codex_app_external_adapter",
        )
    )
    return encoded, context


def _alias_for_property(tools: list[dict], property_name: str) -> str:
    for tool in tools:
        parameters = tool.get("parameters") if isinstance(tool, dict) else None
        properties = parameters.get("properties") if isinstance(parameters, dict) else None
        if isinstance(properties, dict) and property_name in properties:
            name = tool.get("name")
            if isinstance(name, str) and name.startswith("__codexhub_ns_"):
                return name
    raise AssertionError(f"no __codexhub_ns_ alias exposed {property_name}")


def test_xai_codex_app_namespace_alias_inverse_maps_function_call():
    encoded, context = _encode_xai_namespace_request()
    tools = encoded["tools"]
    assert all(tool["type"] == "function" for tool in tools)
    update_alias = _alias_for_property(tools, "action")
    echo_alias = _alias_for_property(tools, "text")
    assert update_alias != echo_alias
    update_params = next(tool["parameters"] for tool in tools if tool["name"] == update_alias)
    assert update_params["type"] == "object"
    assert "oneOf" not in update_params
    assert update_params["properties"]["action"] == {"type": "string"}

    decoded = json.loads(
        gateway_compat.compatible_response_body(
            json.dumps(
                {
                    "id": "resp_xai_ns",
                    "output": [
                        {
                            "type": "function_call",
                            "name": update_alias,
                            "call_id": "call_update",
                            "arguments": '{"action":"ping"}',
                        },
                        {
                            "type": "function_call",
                            "name": echo_alias,
                            "call_id": "call_echo",
                            "arguments": '{"text":"pong"}',
                        },
                    ],
                }
            ).encode(),
            "xai",
            context,
        )
    )

    calls = decoded["output"]
    assert calls[0]["namespace"] == "codex_app"
    assert calls[0]["name"] == "automation_update"
    assert calls[0]["call_id"] == "call_update"
    assert json.loads(calls[0]["arguments"]) == {"action": "ping"}
    assert calls[1]["namespace"] == "codex_app"
    assert calls[1]["name"] == "echo"
    assert "__codexhub_ns_" not in json.dumps(decoded)


def test_xai_codex_app_namespace_alias_inverse_maps_sse():
    encoded, context = _encode_xai_namespace_request()
    update_alias = _alias_for_property(encoded["tools"], "action")
    added = {
        "type": "response.output_item.added",
        "item": {
            "type": "function_call",
            "name": update_alias,
            "call_id": "call_update",
            "item_id": "item_update",
            "arguments": "",
        },
    }
    mapped = json.loads(
        gateway_compat.compatible_sse_line(
            b"data: " + json.dumps(added).encode() + b"\n",
            "xai",
            context,
        )
        .split(b":", 1)[1]
        .strip()
    )
    assert mapped["item"]["namespace"] == "codex_app"
    assert mapped["item"]["name"] == "automation_update"
    assert mapped["item"]["name"].startswith("__codexhub_ns_") is False


def test_root_union_preserves_outer_constraints():
    from gateway_compat.tool_parameter_root import coerce_tool_parameter_root
    original = {"type": "object", "properties": {"token": {"type": "string"}},
                "required": ["token"], "anyOf": [
                    {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
                    {"type": "null"}]}
    projected, changed = coerce_tool_parameter_root(original)
    assert changed
    assert projected["required"] == ["token"]
    assert projected["properties"] == original["properties"]
    assert projected["anyOf"][0]["required"] == ["path"]
    assert len(original["anyOf"]) == 2


@pytest.mark.parametrize("root", [{"type": "string"}, {"type": "array"}, {"type": ["number", "null"]}])
def test_scalar_tool_root_rejected_without_argument_wrapping(root):
    from protocol_translation import UnsupportedProtocolTranslationError
    body = json.dumps({"model":"xai/grok-4.6", "input":[], "tools":[
        {"type":"function", "name":"scalar", "parameters":root}]}).encode()
    with pytest.raises(UnsupportedProtocolTranslationError, match="object|Scalar"):
        gateway_compat.compatible_request_body(body, _xai_upstream(), inject_codex_tools=False)

@pytest.mark.parametrize("root", [
    False,
    {"not": {}},
    {"type": "object", "not": {}},
    {"type": "object", "not": True},
    {"type": "string", "anyOf": [{"type": "object"}]},
])
def test_impossible_tool_roots_are_not_broadened(root):
    from gateway_compat.tool_parameter_root import coerce_tool_parameter_root
    from protocol_translation import UnsupportedProtocolTranslationError
    with pytest.raises(UnsupportedProtocolTranslationError):
        coerce_tool_parameter_root(root)


@pytest.mark.parametrize("root", [
    {"allOf": [{"type": "string"}]},
    {"allOf": [{"type": "object"}, {"type": "string"}]},
    {"anyOf": [{"type": "string"}, {"type": "number"}]},
    {"oneOf": [{"type": "string"}, {"type": "null"}]},
    {"not": {"type": "object", "description": "all objects"}},
    {"enum": ["scalar"]},
    {"const": "scalar"},
])
def test_indirect_scalar_tool_roots_are_not_broadened(root):
    from gateway_compat.tool_parameter_root import coerce_tool_parameter_root
    from protocol_translation import UnsupportedProtocolTranslationError

    with pytest.raises(UnsupportedProtocolTranslationError):
        coerce_tool_parameter_root(root)


def test_mixed_root_union_drops_provably_scalar_branch():
    body = json.dumps({"input": [], "tools": [
        {"type": "function", "name": "inspect", "parameters": {
            "anyOf": [
                {"allOf": [{"type": "string"}]},
                {"type": "object", "required": ["path"]},
                {"type": "null"},
            ],
        }},
    ]}).encode()
    transformed = json.loads(gateway_compat.compatible_request_body(
        body, _xai_upstream(), inject_codex_tools=False,
    ))
    assert transformed["tools"][0]["parameters"] == {
        "type": "object", "required": ["path"],
    }


@pytest.mark.parametrize("union", ["anyOf", "oneOf"])
def test_nullable_outer_type_on_object_union_is_normalized(union):
    from gateway_compat.tool_parameter_root import coerce_tool_parameter_root
    root = {"type": ["object", "null"], union: [{"type": "object", "required": ["path"]}]}
    result, changed = coerce_tool_parameter_root(root)
    assert changed
    assert result["type"] == "object"
    assert result[union] == root[union]
    assert root["type"] == ["object", "null"]


@pytest.mark.parametrize("union", ["anyOf", "oneOf"])
@pytest.mark.parametrize("branch", [
    {"allOf": [{"type": "object", "required": ["path"]}]},
    {"$ref": "#/$defs/path_args"},
    {"not": {"required": ["forbidden"]}},
    {"minProperties": 1},
])
def test_tool_union_retains_indirect_object_constraints(union, branch):
    root = {
        "$defs": {"path_args": {"type": "object", "required": ["path"]}},
        union: [branch, {"type": "object", "required": ["other"]}, {"type": "null"}],
    }
    body = json.dumps({"input": [], "tools": [
        {"type": "function", "name": "inspect", "parameters": root},
    ]}).encode()
    transformed = json.loads(gateway_compat.compatible_request_body(
        body, _xai_upstream(), inject_codex_tools=False,
    ))
    parameters = transformed["tools"][0]["parameters"]
    assert parameters["$defs"] == root["$defs"]
    # The public adapter resolves local references before normalizing roots.
    expected_branch = root["$defs"]["path_args"] if "$ref" in branch else branch
    assert parameters[union] == [
        {**expected_branch, "type": "object"}, {"type": "object", "required": ["other"]},
    ]
