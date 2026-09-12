#!/usr/bin/env python3
"""Prove xAI Grok tool-parameter roots stay JSON Schema objects.

Always-on path: in-process sanitizer against the documented xAI 400 shapes
(root anyOf/oneOf with a non-object branch, exclusive-required anyOf), plus
flatten/inverse-map of a Codex App namespace child through
`compatible_request_body` / `compatible_response_body`. Nested unions and
nullable type arrays must survive.

Set CODEXHUB_E2E_XAI=1 to also POST the sanitized Responses payload to live
xAI using the local xai_auth session. Live mode never sends an unsanitized
union root. This is not part of the eight-case Official+OpenCode Go CLI gate.
"""
from __future__ import annotations

try:
    from scripts.python_runtime_contract import require_python_313
except ModuleNotFoundError:
    from python_runtime_contract import require_python_313

require_python_313(__file__)

import json
import os
import sys
import tempfile
import atexit
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src-python"))

if os.environ.get("CODEXHUB_E2E_XAI") != "1":
    _probe_home = tempfile.TemporaryDirectory(prefix="codexhub-xai-preflight-")
    atexit.register(_probe_home.cleanup)
    for _home_key in ("CODEX_HOME", "CODEXHUB_RUNTIME_HOME"):
        os.environ[_home_key] = _probe_home.name

import gateway_compat  # noqa: E402
import xai_auth  # noqa: E402

ROOT_UNION_WITH_NULL = {
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
        ]
    },
}

EXCLUSIVE_REQUIRED = {
    "type": "function",
    "name": "search",
    "description": "search files or scopes",
    "parameters": {
        "type": "object",
        "properties": {
            "project": {"type": "string"},
            "paths": {"type": "array", "items": {"type": "string"}},
            "scopes": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["project"],
        "anyOf": [{"required": ["paths"]}, {"required": ["scopes"]}],
    },
}

NESTED_ALLOWED = {
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
}

# Captured from the Desktop tool catalog. Its nested oneOf/$ref structure
# previously passed our simplified probes but prevented every Grok turn.
DESKTOP_AUTOMATION = {
    "type": "function",
    "name": "__codexhub_ns_fa0cf542e6_1",
    "parameters": json.loads(
        (ROOT / "tests/fixtures/tool_schemas/codex_app_automation_update.json").read_text(encoding="utf-8")
    ),
}


def _parameters(tool: dict) -> dict:
    function = tool.get("function")
    if isinstance(function, dict) and isinstance(function.get("parameters"), dict):
        return function["parameters"]
    parameters = tool.get("parameters")
    if isinstance(parameters, dict):
        return parameters
    raise SystemExit(f"tool has no parameters object: {tool!r}")


def _sanitize(tools: list[dict]) -> tuple[list[dict], int]:
    body = json.dumps({"model": "grok-4.6", "tools": tools}).encode("utf-8")
    rewritten, count = gateway_compat.normalize_transparent_tool_schema_booleans(body)
    payload = json.loads(rewritten.decode("utf-8"))
    return payload["tools"], count


def _assert_object_root(params: dict, *, allow_object_union: bool = False) -> None:
    union = params.get("anyOf") or params.get("oneOf")
    if allow_object_union and isinstance(union, list) and union:
        if any(
            not isinstance(branch, dict) or branch.get("type") != "object"
            for branch in union
        ):
            raise SystemExit(f"union root still has a non-object branch: {params!r}")
        if params.get("type") not in (None, "object"):
            raise SystemExit(f"object-union root has a non-object type: {params!r}")
        return
    if params.get("type") != "object" or "anyOf" in params or "oneOf" in params:
        raise SystemExit(f"sanitizer left a non-object tool root: {params!r}")


def _sanitizer_suite() -> dict:
    desktop_tools, _ = _sanitize([DESKTOP_AUTOMATION])
    _assert_object_root(_parameters(desktop_tools[0]))
    union_tools, union_count = _sanitize([ROOT_UNION_WITH_NULL])
    union_params = _parameters(union_tools[0])
    _assert_object_root(union_params)
    if union_params.get("properties", {}).get("action", {}).get("type") != "string":
        raise SystemExit(f"object branch was lost: {union_params!r}")
    if union_count < 1:
        raise SystemExit("root oneOf+null was not rewritten")

    exclusive_tools, exclusive_count = _sanitize([EXCLUSIVE_REQUIRED])
    exclusive_params = _parameters(exclusive_tools[0])
    _assert_object_root(exclusive_params, allow_object_union=True)
    if exclusive_count < 1:
        raise SystemExit("exclusive-required anyOf was not annotated")

    nested_tools, nested_count = _sanitize([NESTED_ALLOWED])
    nested_params = _parameters(nested_tools[0])
    if nested_count != 0:
        raise SystemExit(f"nested unions or type arrays were rewritten: {nested_params!r}")
    if nested_params["properties"]["combo"]["anyOf"] != [{"type": "number"}, {"type": "string"}]:
        raise SystemExit(f"nested anyOf was flattened: {nested_params!r}")
    if nested_params["properties"]["label"]["type"] != ["string", "null"]:
        raise SystemExit(f"nested type array was stripped: {nested_params!r}")

    context: dict = {}
    namespace_child = dict(ROOT_UNION_WITH_NULL)
    namespace_child["name"] = "automation_update"
    encoded = json.loads(
        gateway_compat.compatible_request_body(
            json.dumps(
                {
                    "model": "xai/grok-4.6",
                    "input": [{"role": "user", "content": "update"}],
                    "tools": [
                        {
                            "type": "namespace",
                            "name": "codex_app",
                            "tools": [namespace_child],
                        }
                    ],
                }
            ).encode("utf-8"),
            {
                "name": "xai",
                "upstream_model": "grok-4.6",
                "upstream_format": "responses",
                "tool_protocol": "responses_structured",
                "tool_surface_strategy": "eager",
            },
            event_context=context,
            inject_codex_tools=False,
            behavior_profile="codex_app_external_adapter",
        )
    )
    alias = encoded["tools"][0]["name"]
    if not isinstance(alias, str) or not alias.startswith("__codexhub_ns_"):
        raise SystemExit(f"namespace child was not flattened: {encoded['tools']!r}")
    decoded = json.loads(
        gateway_compat.compatible_response_body(
            json.dumps(
                {
                    "output": [
                        {
                            "type": "function_call",
                            "name": alias,
                            "call_id": "call_update",
                            "arguments": '{"action":"ping"}',
                        }
                    ]
                }
            ).encode("utf-8"),
            "xai",
            context,
        )
    )
    call = decoded["output"][0]
    if call.get("namespace") != "codex_app" or call.get("name") != "automation_update":
        raise SystemExit(f"function_call was not inverse-mapped: {call!r}")
    if alias in json.dumps(decoded):
        raise SystemExit("alias leaked to the Codex App response")

    web_search_body = json.loads(
        gateway_compat.compatible_request_body(
            json.dumps(
                {
                    "model": "xai/grok-4.6",
                    "input": [{"role": "user", "content": "search"}],
                    "tools": [
                        {
                            "type": "web_search",
                            "external_web_access": True,
                            "search_context_size": "low",
                        }
                    ],
                    "tool_choice": "auto",
                }
            ).encode("utf-8"),
            {
                "name": "xai",
                "upstream_model": "grok-4.6",
                "upstream_format": "responses",
                "tool_protocol": "responses_structured",
                "tool_surface_strategy": "eager",
            },
            event_context={},
            inject_codex_tools=False,
            behavior_profile="codex_app_external_adapter",
        )
    )
    web_search = next(tool for tool in web_search_body["tools"] if tool.get("type") == "web_search")
    if "external_web_access" in web_search:
        raise SystemExit(f"external_web_access leaked to xAI: {web_search!r}")

    return {
        "sanitizer": "passed",
        "root_union_rewritten": union_count,
        "exclusive_required_rewritten": exclusive_count,
        "nested_untouched": True,
        "inverse_mapped": True,
        "web_search_external_web_access_dropped": True,
        "alias_prefix": "__codexhub_ns_",
    }


def _live_xai_roundtrip(tools: list[dict]) -> None:
    token = xai_auth.access_token()
    if not token:
        raise SystemExit("CODEXHUB_E2E_XAI=1 requires a local xAI session")
    payload = {
        "model": "grok-4.6",
        "input": [{"role": "user", "content": "Reply with the single word pong."}],
        "tools": tools,
    }
    request = Request(
        "https://api.x.ai/v1/responses",
        data=json.dumps(payload, ensure_ascii=True).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            status = getattr(response, "status", 200)
    except HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        if "tool parameter root must be an object type" in detail:
            raise SystemExit(f"live xAI still rejected the sanitized tool root: {detail}") from error
        if error.code in {401, 403}:
            raise SystemExit(f"live xAI auth failed: {error.code}") from error
        raise SystemExit(f"live xAI HTTP {error.code}: {detail}") from error
    print(json.dumps({"live": True, "status": status, "schema_accepted": True}))


def main() -> int:
    result = _sanitizer_suite()
    if os.environ.get("CODEXHUB_E2E_XAI") == "1":
        tools, _count = _sanitize([ROOT_UNION_WITH_NULL, EXCLUSIVE_REQUIRED, NESTED_ALLOWED, DESKTOP_AUTOMATION])
        tools.append({"type": "web_search"})
        _live_xai_roundtrip(tools)
        return 0
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
