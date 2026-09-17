"""Console Go wire restrictions must not change other provider contracts."""
from __future__ import annotations

import copy
import json

import pytest

import gateway_compat


def _encode_responses(payload, provider, surface, alias_model):
    upstream = {
        "name": provider, "upstream_format": "responses",
        "tool_protocol": "responses_structured", "tool_surface_strategy": "eager",
    }
    if alias_model:
        upstream["upstream_model"] = "resolved-model"
    body = json.dumps(payload).encode()
    if surface == "compatible":
        encoded = gateway_compat.compatible_request_body(
            body, upstream, inject_codex_tools=False, event_context={},
        )
    else:
        encoded = gateway_compat.transparent_request_body(body, copy.deepcopy(payload), upstream)
    return json.loads(encoded)


@pytest.mark.parametrize("surface", ["compatible", "transparent"])
@pytest.mark.parametrize("provider", ["opencode_go", "xai", "custom-provider"])
@pytest.mark.parametrize("alias_model", [False, True])
def test_responses_field_restrictions_are_scoped_to_console_go(surface, provider, alias_model):
    payload = {
        "model": "fixture-model",
        "input": [{"type": "message", "role": "user", "content": "hello"}],
        "include": ["web_search_call.action.sources"],
        "prompt_cache_key": "caller-cache-identity",
        "store": False,
        "max_output_tokens": 128,
        "tools": [{
            "type": "function", "name": "read", "strict": True,
            "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
        }],
    }
    result = _encode_responses(payload, provider, surface, alias_model)
    for field in ("include", "prompt_cache_key", "store", "max_output_tokens"):
        if provider == "opencode_go":
            assert field not in result
        else:
            assert result[field] == payload[field]
    if provider == "opencode_go":
        assert "strict" not in result["tools"][0]
    else:
        assert result["tools"][0]["strict"] is True


@pytest.mark.parametrize("surface", ["compatible", "transparent"])
@pytest.mark.parametrize("provider", ["opencode_go", "xai", "custom-provider"])
@pytest.mark.parametrize("alias_model", [False, True])
def test_console_go_drops_search_content_types_on_non_preview_tools(
    surface, provider, alias_model
):
    # Official now documents search_content_types on web_search. Console Go still
    # 400s: "`tools[].search_content_types` is only supported for web_search_preview tools."
    image_settings = {"max_results": 3, "caption": True}
    payload = {
        "model": "fixture-model",
        "input": [{"type": "message", "role": "user", "content": "search"}],
        "tools": [
            {
                "type": "web_search",
                "search_content_types": ["text", "image"],
                "image_settings": image_settings,
                "search_context_size": "low",
            },
            {
                "type": "web_search_preview",
                "search_content_types": ["text"],
                "search_context_size": "medium",
            },
        ],
    }
    result = _encode_responses(payload, provider, surface, alias_model)
    by_type = {tool["type"]: tool for tool in result["tools"]}
    if provider == "opencode_go":
        assert "search_content_types" not in by_type["web_search"]
        assert "image_settings" not in by_type["web_search"]
        assert by_type["web_search"]["search_context_size"] == "low"
        assert by_type["web_search_preview"]["search_content_types"] == ["text"]
    else:
        assert by_type["web_search"]["search_content_types"] == ["text", "image"]
        assert by_type["web_search"]["image_settings"] == image_settings
        assert by_type["web_search_preview"]["search_content_types"] == ["text"]
