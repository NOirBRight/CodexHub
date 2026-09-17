"""Grok Build talks to every injected provider on the transparent provider-path.

Grok is not in ``_infer_client_id``, so ``client_id`` stays unknown. Combined
with ``/v1/providers/<id>``, every Grok turn is
``third_party_app_transparent_metered``. The live Luna 400 was one instance;
these cases pin the same Grok request shape against the other catalog
upstreams that route uses.

Grok picks ``api_backend`` per provider, but the Gateway still serves both
``/responses`` and ``/chat/completions``. Both inbound endpoints must prepare
a body the selected upstream can accept.
"""

from __future__ import annotations

import json

import pytest

import gateway_compat
import route_plan
import route_primitives
from gateway_errors import UpstreamProtocolTranslationError
from protocol_translation import NonForwardable


_SYSTEM = "You are Grok released by xAI."
_USER = "test"

# Live Grok→Luna request carried prompt_cache_key and ~27 function tools.
_GROK_RESPONSES = {
    "model": "fixture-model",
    "input": [
        {"type": "message", "role": "system", "content": _SYSTEM},
        {"type": "message", "role": "user", "content": _USER},
    ],
    "tools": [
        {
            "type": "function",
            "name": "read_file",
            "strict": True,
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        }
    ],
    "stream": True,
    "store": False,
    "prompt_cache_key": "grok-session-cache",
    "include": ["reasoning.encrypted_content"],
    "max_output_tokens": 128,
    "reasoning": {"effort": "xhigh"},
}

_GROK_CHAT = {
    "model": "fixture-model",
    "messages": [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": _USER},
    ],
    "tools": [
        {
            "type": "function",
            "name": "read_file",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        }
    ],
    "stream": True,
}

# Grok's Responses serializer emits flat function tools. Chat Completions
# providers (Command Code, Kimi, MiniMax) require the nested envelope.
_GROK_CHAT_FLAT_TOOLS = {
    **_GROK_CHAT,
    "tools": [
        {
            "type": "function",
            "name": "read_file",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        }
    ],
}

_RESPONSES_PROVIDERS = (
    {
        "id": "openai",
        "upstream": {
            "name": "official",
            "upstream_model": "gpt-5.6-luna",
            "upstream_format": "responses",
        },
    },
    {
        "id": "opencode-go",
        "upstream": {
            "name": "opencode_go",
            "upstream_model": "muse-spark-1.3-contributor",
            "upstream_format": "auto",
        },
    },
    {
        "id": "ollama-cloud",
        "upstream": {
            "name": "ollama_cloud",
            "upstream_model": "glm-5.3-flash",
            "upstream_format": "responses",
        },
    },
    {
        "id": "volc",
        "upstream": {
            "name": "volcengine",
            "upstream_model": "glm-5.3",
            "upstream_format": "responses",
        },
    },
    {
        "id": "xai",
        "upstream": {
            "name": "xai",
            "upstream_model": "grok-4.6",
            "upstream_format": "responses",
        },
    },
)

_CHAT_PROVIDERS = (
    {
        "id": "commandcode",
        "upstream": {
            "name": "commandcode",
            "upstream_model": "deepseek/deepseek-v4.1-flash",
            "upstream_format": "chat_completions",
        },
    },
    {
        "id": "kimi",
        "upstream": {
            "name": "kimi",
            "upstream_model": "kimi-k3",
            "upstream_format": "chat_completions",
            "supports_developer_role": False,
        },
    },
    {
        "id": "kimi-cn",
        "upstream": {
            "name": "kimi",
            "upstream_model": "kimi-k3",
            "upstream_format": "chat_completions",
            "supports_developer_role": False,
        },
    },
    {
        "id": "minimax-cn",
        "upstream": {
            "name": "minimax_cn",
            "upstream_model": "MiniMax-M3",
            "upstream_format": "chat_completions",
        },
    },
)


_ALL_PROVIDERS = _RESPONSES_PROVIDERS + _CHAT_PROVIDERS
_ENDPOINTS = ("responses", "chat_completions")


def _inbound_payload(inbound_format: str) -> dict:
    return dict(_GROK_RESPONSES) if inbound_format == "responses" else dict(_GROK_CHAT_FLAT_TOOLS)


def _route(case: dict, inbound_format: str):
    return route_plan.route_plan_for_request(
        case["upstream"],
        {"client_id": "unknown"},
        inbound_format=inbound_format,
        provider_hint=case["id"],
    )


def _prepare_endpoint(payload: dict, case: dict, inbound_format: str) -> tuple[dict, object]:
    """Mirror the transparent branch of gateway_exchange body prep."""
    decision = _route(case, inbound_format)
    attempt = decision.primary_attempt
    body = json.dumps(payload).encode()
    prepared = None
    if inbound_format == "chat_completions" and attempt.selected_upstream_format == "responses":
        prepared = attempt.prepare_body(body)
        body = prepared.upstream_body
    mutation_upstream = dict(case["upstream"])
    mutation_upstream["upstream_format"] = (
        attempt.selected_upstream_format if prepared is not None else inbound_format
    )
    mapped = json.loads(body)
    body = gateway_compat.transparent_request_body(
        body,
        mapped,
        mutation_upstream,
        model_id=payload.get("model"),
    )
    body, _ = gateway_compat.normalize_transparent_tool_schema_booleans(body)
    if prepared is None:
        prepared = attempt.prepare_body(body)
        body = prepared.upstream_body
    return json.loads(body), decision


def _mutate(payload: dict, upstream: dict) -> dict:
    return json.loads(
        gateway_compat.transparent_request_body(
            json.dumps(payload).encode(),
            payload,
            upstream,
        )
    )


def _system_roles(payload: dict) -> list[object]:
    leftover: list[object] = []
    for key in ("input", "messages"):
        items = payload.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "system" or item.get("role") == "system":
                leftover.append((key, item.get("type"), item.get("role")))
    return leftover


@pytest.mark.parametrize("case", _ALL_PROVIDERS, ids=lambda case: case["id"])
@pytest.mark.parametrize("inbound_format", _ENDPOINTS)
def test_grok_both_endpoints_are_transparent(case, inbound_format):
    decision = _route(case, inbound_format)
    assert decision.behavior_profile == (
        route_primitives.BEHAVIOR_THIRD_PARTY_APP_TRANSPARENT_METERED
    )
    assert decision.request_mutation_policy == route_primitives.MutationPolicy.TRANSPARENT


@pytest.mark.parametrize("case", _ALL_PROVIDERS, ids=lambda case: case["id"])
@pytest.mark.parametrize("inbound_format", _ENDPOINTS)
def test_grok_both_endpoints_prepare_upstream_body(case, inbound_format):
    try:
        transformed, decision = _prepare_endpoint(
            _inbound_payload(inbound_format),
            case,
            inbound_format,
        )
    except (NonForwardable, UpstreamProtocolTranslationError) as exc:
        pytest.fail(
            f"{case['id']} {inbound_format} could not prepare a Grok body: {exc}"
        )
    selected = decision.primary_attempt.selected_upstream_format
    name = case["upstream"]["name"]
    if selected == "responses":
        assert "messages" not in transformed
        assert isinstance(transformed.get("input"), list)
        if name == "official":
            assert _system_roles(transformed) == []
            assert any(
                isinstance(item, dict) and item.get("role") == "developer"
                and _SYSTEM in str(item.get("content"))
                for item in transformed["input"]
            )
            assert not transformed.get("instructions")
            params = transformed["tools"][0]["parameters"]
            if inbound_format == "responses":
                assert params["additionalProperties"] is False
                assert "path" in params["required"]
        elif inbound_format == "chat_completions":
            assert _SYSTEM in str(transformed.get("instructions", ""))
        else:
            assert _system_roles(transformed)
        if name == "opencode_go":
            for field in ("include", "prompt_cache_key", "store", "max_output_tokens"):
                assert field not in transformed
        if name == "ollama_cloud" and inbound_format == "responses":
            assert transformed["reasoning"]["effort"] == "max"
        tool = transformed["tools"][0]
        assert tool["type"] == "function"
        assert tool["name"] == "read_file"
        assert "function" not in tool
        return
    assert "input" not in transformed
    assert transformed["messages"][0]["role"] == "system"
    assert transformed["messages"][1]["role"] == "user"
    tool = transformed["tools"][0]
    assert tool["type"] == "function"
    assert tool["function"]["name"] == "read_file"
    assert "name" not in tool


def test_grok_official_responses_drops_system_messages():
    transformed = _mutate(_GROK_RESPONSES, _RESPONSES_PROVIDERS[0]["upstream"])
    assert _system_roles(transformed) == []
    assert any(
        isinstance(item, dict) and item.get("role") == "developer"
        for item in transformed["input"]
    )


def test_grok_official_strict_tools_get_additional_properties():
    transformed = _mutate(_GROK_RESPONSES, _RESPONSES_PROVIDERS[0]["upstream"])
    params = transformed["tools"][0]["parameters"]
    assert params["additionalProperties"] is False
    assert params["required"] == ["path"]
    assert "additionalProperties" not in _GROK_RESPONSES["tools"][0]["parameters"]


def test_grok_opencode_go_union_alpha_prepares_anthropic_messages():
    case = {
        "id": "opencode-go",
        "upstream": {
            "name": "opencode_go",
            "upstream_model": "union-alpha",
            "upstream_format": "auto",
            "base_url": "https://opencode.ai/zen/go/v1",
        },
    }
    transformed, decision = _prepare_endpoint(
        {**_GROK_RESPONSES, "model": "union-alpha"},
        case,
        "responses",
    )
    attempt = decision.primary_attempt
    assert decision.behavior_profile == (
        route_primitives.BEHAVIOR_THIRD_PARTY_APP_TRANSPARENT_METERED
    )
    assert attempt.selected_upstream_format == "anthropic_messages"
    assert attempt.endpoint_url.endswith("/messages")
    assert transformed["model"] == "union-alpha"
    assert transformed["messages"][0]["role"] == "user"
    assert transformed["tools"][0]["name"] == "read_file"
    assert "input_schema" in transformed["tools"][0]


def test_grok_opencode_go_drops_console_go_rejected_fields():
    transformed = _mutate(_GROK_RESPONSES, _RESPONSES_PROVIDERS[1]["upstream"])
    for field in ("include", "prompt_cache_key", "store", "max_output_tokens"):
        assert field not in transformed
    assert "strict" not in transformed["tools"][0]
    # Console Go accepts system; do not rewrite it into developer.
    assert _system_roles(transformed)


def test_grok_opencode_go_drops_search_content_types_on_web_search():
    payload = {
        **_GROK_RESPONSES,
        "tools": [
            {
                "type": "web_search",
                "search_content_types": ["text", "image"],
                "image_settings": {"max_results": 3, "caption": True},
                "search_context_size": "low",
            }
        ],
    }
    transformed = _mutate(payload, _RESPONSES_PROVIDERS[1]["upstream"])
    web_search = next(tool for tool in transformed["tools"] if tool.get("type") == "web_search")
    assert "search_content_types" not in web_search
    assert "image_settings" not in web_search
    assert web_search["search_context_size"] == "low"


def test_grok_ollama_aliases_xhigh_effort():
    transformed = _mutate(_GROK_RESPONSES, _RESPONSES_PROVIDERS[2]["upstream"])
    assert transformed["reasoning"]["effort"] == "max"


def test_grok_volc_keeps_system_and_cache_fields():
    transformed = _mutate(_GROK_RESPONSES, _RESPONSES_PROVIDERS[3]["upstream"])
    assert _system_roles(transformed)
    assert transformed["prompt_cache_key"] == "grok-session-cache"
    assert "additionalProperties" not in transformed["tools"][0]["parameters"]


def test_grok_xai_keeps_system_messages():
    transformed = _mutate(_GROK_RESPONSES, _RESPONSES_PROVIDERS[4]["upstream"])
    assert _system_roles(transformed)


def test_grok_official_keeps_web_search_external_web_access():
    payload = {
        **_GROK_RESPONSES,
        "tools": [
            {
                "type": "web_search",
                "external_web_access": True,
                "search_context_size": "low",
            }
        ],
    }
    transformed = _mutate(payload, _RESPONSES_PROVIDERS[0]["upstream"])
    web_search = next(tool for tool in transformed["tools"] if tool.get("type") == "web_search")
    assert web_search["external_web_access"] is True


def test_grok_responses_function_tools_stay_flat():
    transformed = _mutate(_GROK_RESPONSES, _RESPONSES_PROVIDERS[3]["upstream"])
    tool = transformed["tools"][0]
    assert tool["type"] == "function"
    assert tool["name"] == "read_file"
    assert "function" not in tool


def test_grok_xai_drops_web_search_external_web_access():
    payload = {
        **_GROK_RESPONSES,
        "tools": [
            {
                "type": "web_search",
                "external_web_access": True,
                "search_context_size": "low",
            }
        ],
    }
    transformed = _mutate(payload, _RESPONSES_PROVIDERS[4]["upstream"])
    web_search = next(tool for tool in transformed["tools"] if tool.get("type") == "web_search")
    assert "external_web_access" not in web_search


def test_grok_xai_rejects_cache_only_web_search():
    payload = {
        **_GROK_RESPONSES,
        "tools": [
            {
                "type": "web_search",
                "external_web_access": False,
                "search_context_size": "low",
            }
        ],
    }
    with pytest.raises(UpstreamProtocolTranslationError, match="external_web_access=false"):
        _mutate(payload, _RESPONSES_PROVIDERS[4]["upstream"])


@pytest.mark.parametrize("case", _CHAT_PROVIDERS, ids=lambda case: case["id"])
def test_grok_chat_providers_keep_system_messages(case):
    transformed = _mutate(_GROK_CHAT, case["upstream"])
    assert transformed["messages"][0]["role"] == "system"
    assert transformed["messages"][1]["role"] == "user"


@pytest.mark.parametrize("case", _CHAT_PROVIDERS, ids=lambda case: case["id"])
def test_grok_chat_flat_tools_are_wrapped(case):
    transformed = _mutate(_GROK_CHAT_FLAT_TOOLS, case["upstream"])
    tool = transformed["tools"][0]
    assert tool == {
        "type": "function",
        "function": {
            "name": "read_file",
            "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
        },
    }


@pytest.mark.parametrize("case", _CHAT_PROVIDERS, ids=lambda case: case["id"])
def test_grok_chat_nested_tools_stay_nested(case):
    payload = {
        **_GROK_CHAT,
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "read_file",
                    "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
                },
            }
        ],
    }
    transformed = _mutate(payload, case["upstream"])
    assert transformed["tools"] == payload["tools"]
