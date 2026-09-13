"""Console Go wire restrictions must not change other provider contracts."""
from __future__ import annotations

import copy
import json

import pytest

import gateway_compat


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
    result = json.loads(encoded)
    for field in ("include", "prompt_cache_key", "store", "max_output_tokens"):
        if provider == "opencode_go":
            assert field not in result
        else:
            assert result[field] == payload[field]
    if provider == "opencode_go":
        assert "strict" not in result["tools"][0]
    else:
        assert result["tools"][0]["strict"] is True
