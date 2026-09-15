"""Official Codex rejects Responses ``role=system`` with HTTP 400.

Grok Build posts to ``/v1/providers/openai/responses``. That route is
transparent-metered (provider_path), so the body used to reach official
unchanged. The live failure was:

    Bad request (400): System messages are not allowed
"""

from __future__ import annotations

import json

import gateway_compat
import route_plan
import route_primitives


_SYSTEM_PROMPT = "You are Grok released by xAI."
_USER_TEXT = "test"

_OFFICIAL = {
    "name": "official",
    "upstream_model": "gpt-5.6-luna",
    "upstream_format": "responses",
}


def _payload(system_item: dict) -> dict:
    return {
        "model": "gpt-5.6-luna",
        "input": [
            system_item,
            {"type": "message", "role": "user", "content": _USER_TEXT},
        ],
        "stream": True,
        "store": False,
    }


def _system_roles_or_types(payload: dict) -> list[tuple[object, object, object]]:
    found: list[tuple[object, object, object]] = []
    for key in ("input", "messages"):
        items = payload.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            role = item.get("role")
            if item_type == "system" or role == "system":
                found.append((key, item_type, role))
    return found


def _assert_official_accepts(payload: dict) -> None:
    leftover = _system_roles_or_types(payload)
    assert leftover == [], (
        "official Codex 400s with 'System messages are not allowed' when "
        f"the body still contains {leftover}"
    )
    assert any(
        isinstance(item, dict) and item.get("role") == "developer"
        and _SYSTEM_PROMPT in str(item.get("content"))
        for item in payload["input"]
    )
    assert any(
        isinstance(item, dict) and item.get("role") == "user"
        and _USER_TEXT in str(item.get("content"))
        for item in payload["input"]
    )


def test_grok_provider_path_to_official_is_transparent_metered():
    decision = route_plan.route_plan_for_request(
        _OFFICIAL,
        {"client_id": "unknown"},
        inbound_format="responses",
        provider_hint="openai",
        model_requested="gpt-5.6-luna",
    )
    assert decision.behavior_profile == (
        route_primitives.BEHAVIOR_THIRD_PARTY_APP_TRANSPARENT_METERED
    )
    assert decision.transparent_metered is True
    assert decision.request_mutation_policy == route_primitives.MutationPolicy.TRANSPARENT


def test_official_transparent_rewrites_typed_system_message():
    payload = _payload(
        {"type": "message", "role": "system", "content": _SYSTEM_PROMPT}
    )
    transformed = json.loads(
        gateway_compat.transparent_request_body(
            json.dumps(payload).encode(),
            payload,
            _OFFICIAL,
        )
    )
    _assert_official_accepts(transformed)


def test_official_transparent_rewrites_untyped_system_message():
    payload = _payload({"role": "system", "content": _SYSTEM_PROMPT})
    transformed = json.loads(
        gateway_compat.transparent_request_body(
            json.dumps(payload).encode(),
            payload,
            _OFFICIAL,
        )
    )
    _assert_official_accepts(transformed)


def test_official_transparent_rewrites_type_system_item():
    payload = _payload({"type": "system", "content": _SYSTEM_PROMPT})
    transformed = json.loads(
        gateway_compat.transparent_request_body(
            json.dumps(payload).encode(),
            payload,
            _OFFICIAL,
        )
    )
    _assert_official_accepts(transformed)


def test_official_passthrough_also_rewrites_system_messages():
    payload = _payload(
        {"type": "message", "role": "system", "content": _SYSTEM_PROMPT}
    )
    transformed = json.loads(
        gateway_compat.compatible_request_body(
            json.dumps(payload).encode(),
            _OFFICIAL,
            behavior_profile=route_primitives.BEHAVIOR_OFFICIAL_CODEX_APP_HTTP_PASSTHROUGH,
        )
    )
    _assert_official_accepts(transformed)


def test_official_gateway_compat_rewrites_system_messages():
    payload = _payload(
        {"type": "message", "role": "system", "content": _SYSTEM_PROMPT}
    )
    transformed = json.loads(
        gateway_compat.compatible_request_body(
            json.dumps(payload).encode(),
            _OFFICIAL,
            event_context={},
            inject_codex_tools=False,
        )
    )
    _assert_official_accepts(transformed)


def test_xai_transparent_keeps_system_messages():
    payload = _payload(
        {"type": "message", "role": "system", "content": _SYSTEM_PROMPT}
    )
    transformed = json.loads(
        gateway_compat.transparent_request_body(
            json.dumps(payload).encode(),
            payload,
            {
                "name": "xai",
                "upstream_model": "grok-4.6",
                "upstream_format": "responses",
            },
        )
    )
    assert _system_roles_or_types(transformed) == [("input", "message", "system")]


def test_official_transparent_rewrites_chat_system_role():
    payload = {
        "model": "gpt-5.6-luna",
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _USER_TEXT},
        ],
        "stream": True,
        "store": False,
    }
    transformed = json.loads(
        gateway_compat.transparent_request_body(
            json.dumps(payload).encode(),
            payload,
            _OFFICIAL,
        )
    )
    leftover = _system_roles_or_types(transformed)
    assert leftover == [], leftover
    assert transformed["messages"][0]["role"] == "developer"
    assert _SYSTEM_PROMPT in str(transformed["messages"][0]["content"])
    assert transformed["messages"][1]["role"] == "user"


def test_official_transparent_folds_chat_instructions_into_developer():
    payload = {
        "model": "gpt-5.6-luna",
        "instructions": _SYSTEM_PROMPT,
        "input": [
            {"type": "message", "role": "user", "content": _USER_TEXT},
        ],
        "stream": True,
        "store": False,
    }
    transformed = json.loads(
        gateway_compat.transparent_request_body(
            json.dumps(payload).encode(),
            payload,
            _OFFICIAL,
        )
    )
    _assert_official_accepts(transformed)
    assert not transformed.get("instructions")
