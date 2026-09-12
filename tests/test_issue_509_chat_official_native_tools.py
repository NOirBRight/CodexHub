from __future__ import annotations

import json

import pytest

import gateway_compat
import gateway_errors
from protocol_translation import chat_completions_request_to_responses_body
from tool_compatibility.chat_official_native import (
    CHAT_OFFICIAL_NATIVE_NAME_MAP_KEY,
    collapse_official_native_tools_for_chat,
)
from tool_compatibility.contracts import CUSTOM_INPUT_KEY, TOOL_SEARCH_INPUT_KEY
from tool_compatibility.registry import RequestScopedToolAliasRegistry


def _official() -> dict:
    return {"name": "official", "upstream_format": "responses"}


def _chat_request(tools: list, *, input_items: object | None = None) -> bytes:
    payload = {
        "model": "placeholder",
        "messages": [{"role": "user", "content": "use tools"}],
        "tools": tools,
        "tool_choice": "auto",
    }
    return json.dumps(payload).encode()


def _prepared_official(*, tools: list, input_items: object | None = None) -> tuple[dict, dict]:
    converted = chat_completions_request_to_responses_body(_chat_request(tools))
    payload = json.loads(converted)
    if input_items is not None:
        payload["input"] = input_items
    context: dict = {}
    prepared = gateway_compat.compatible_request_body(
        json.dumps(payload).encode(),
        _official(),
        event_context=context,
        inject_codex_tools=False,
    )
    return json.loads(prepared), context


def _hosted_alias(kind: str) -> str:
    return RequestScopedToolAliasRegistry(request_token="request").allocate_hosted(
        declaration_index=0,
        kind=kind,
    )


def _custom_alias(name: str) -> str:
    return RequestScopedToolAliasRegistry(request_token="request").allocate_custom(
        declaration_index=0,
        original_name=name,
        version=None,
    )


def _search_alias() -> str:
    return RequestScopedToolAliasRegistry(request_token="request").allocate_tool_search(
        declaration_index=0,
    )


def test_native_hosted_type_stays_native_on_official() -> None:
    prepared, context = _prepared_official(tools=[{"type": "web_search", "search_context_size": "low"}])
    assert prepared["tools"] == [{"type": "web_search", "search_context_size": "low"}]
    assert context[CHAT_OFFICIAL_NATIVE_NAME_MAP_KEY]["hosted"]["web_search"] == "web_search"


def test_hosted_alias_expands_to_official_kind() -> None:
    alias = _hosted_alias("file_search")
    prepared, context = _prepared_official(
        tools=[
            {
                "type": "function",
                "function": {"name": alias, "parameters": {"type": "object"}},
            }
        ]
    )
    assert prepared["tools"] == [{"type": "file_search"}]
    assert context[CHAT_OFFICIAL_NATIVE_NAME_MAP_KEY]["hosted"]["file_search"] == alias


def test_user_function_named_web_search_is_not_hosted() -> None:
    prepared, context = _prepared_official(
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "web_search",
                    "parameters": {"type": "object"},
                },
            }
        ]
    )
    assert prepared["tools"] == [
        {"type": "function", "name": "web_search", "parameters": {"type": "object"}}
    ]
    assert CHAT_OFFICIAL_NATIVE_NAME_MAP_KEY not in context


def test_custom_type_and_apply_patch_alias_expand() -> None:
    alias = _custom_alias("apply_patch")
    native, _ = _prepared_official(
        tools=[{"type": "custom", "name": "apply_patch", "format": {"type": "text"}}]
    )
    aliased, context = _prepared_official(
        tools=[
            {
                "type": "function",
                "function": {
                    "name": alias,
                    "parameters": {
                        "type": "object",
                        "properties": {CUSTOM_INPUT_KEY: {"type": "string"}},
                        "required": [CUSTOM_INPUT_KEY],
                    },
                },
            }
        ]
    )
    assert native["tools"][0]["type"] == "custom"
    assert native["tools"][0]["name"] == "apply_patch"
    assert aliased["tools"][0]["type"] == "custom"
    assert aliased["tools"][0]["name"] == "apply_patch"
    assert context[CHAT_OFFICIAL_NATIVE_NAME_MAP_KEY]["custom"]["apply_patch"] == alias


def test_unknown_custom_alias_fails_closed() -> None:
    with pytest.raises(gateway_errors.UpstreamProtocolTranslationError):
        _prepared_official(
            tools=[
                {
                    "type": "function",
                    "function": {"name": "__codexhub_custom_deadbeef00_1"},
                }
            ]
        )


def test_tool_search_type_and_alias_expand() -> None:
    alias = _search_alias()
    native, _ = _prepared_official(tools=[{"type": "tool_search", "execution": "client"}])
    aliased, context = _prepared_official(
        tools=[
            {
                "type": "function",
                "function": {
                    "name": alias,
                    "parameters": {"type": "object"},
                },
            }
        ]
    )
    assert native["tools"][0] == {"type": "tool_search", "execution": "client"}
    assert aliased["tools"][0]["type"] == "tool_search"
    assert aliased["tools"][0]["execution"] == "client"
    assert context[CHAT_OFFICIAL_NATIVE_NAME_MAP_KEY]["tool_search"] == alias


def test_computer_use_preview_fails_closed_on_chat_conversion() -> None:
    from protocol_translation import UnsupportedProtocolTranslationError

    with pytest.raises(UnsupportedProtocolTranslationError):
        chat_completions_request_to_responses_body(
            _chat_request([{"type": "computer_use_preview"}])
        )


def test_web_search_and_preview_together_fail_closed() -> None:
    with pytest.raises(gateway_errors.UpstreamProtocolTranslationError):
        _prepared_official(tools=[{"type": "web_search"}, {"type": "web_search_preview"}])


def test_encrypted_hosted_declaration_fails_closed() -> None:
    from protocol_translation import UnsupportedProtocolTranslationError

    with pytest.raises(UnsupportedProtocolTranslationError):
        chat_completions_request_to_responses_body(
            _chat_request([{"type": "web_search", "encrypted_content": "secret"}])
        )


def test_encrypted_hosted_history_fails_closed() -> None:
    from tool_compatibility.contracts import ToolCompatibilityError

    with pytest.raises((gateway_errors.UpstreamProtocolTranslationError, ToolCompatibilityError)):
        _prepared_official(
            tools=[{"type": "web_search"}],
            input_items=[
                {
                    "type": "function_call",
                    "call_id": "call_ws",
                    "name": "web_search",
                    "arguments": json.dumps({"action": {"query": "hi"}}),
                    "encrypted_content": "secret",
                }
            ],
        )


def test_hosted_history_and_custom_envelope_expand() -> None:
    alias = _custom_alias("apply_patch")
    prepared, _ = _prepared_official(
        tools=[
            {"type": "web_search"},
            {
                "type": "function",
                "function": {
                    "name": alias,
                    "parameters": {"type": "object"},
                },
            },
        ],
        input_items=[
            {
                "type": "function_call",
                "call_id": "call_ws",
                "name": "web_search",
                "arguments": json.dumps({"action": {"query": "hi"}}),
            },
            {
                "type": "function_call",
                "call_id": "call_patch",
                "name": alias,
                "arguments": json.dumps({CUSTOM_INPUT_KEY: "patch"}),
            },
            {
                "type": "function_call_output",
                "call_id": "call_patch",
                "output": json.dumps({"__codexhub_custom_output": "ok"}),
            },
        ],
    )
    types = [item["type"] for item in prepared["input"] if isinstance(item, dict)]
    assert "web_search_call" in types
    assert "custom_tool_call" in types
    assert "custom_tool_call_output" in types
    custom_call = next(item for item in prepared["input"] if item.get("type") == "custom_tool_call")
    assert custom_call["name"] == "apply_patch"
    assert custom_call["input"] == "patch"


def test_collapse_official_native_output_for_chat() -> None:
    from tool_compatibility.contracts import CUSTOM_OUTPUT_KEY, TOOL_SEARCH_OUTPUT_KEY

    context = {
        CHAT_OFFICIAL_NATIVE_NAME_MAP_KEY: {
            "hosted": {"web_search": "web_search"},
            "hosted_event": {"web_search_call": "web_search"},
            "custom": {"apply_patch": "apply_patch"},
            "tool_search": "tool_search",
        }
    }
    payload = {
        "output": [
            {
                "type": "web_search_call",
                "id": "ws_1",
                "call_id": "call_ws",
                "status": "completed",
                "action": {"query": "codex"},
            },
            {
                "type": "custom_tool_call",
                "call_id": "call_patch",
                "name": "apply_patch",
                "input": "diff",
            },
            {
                "type": "tool_search_call",
                "call_id": "call_search",
                "execution": "client",
                "arguments": {"query": "x"},
            },
        ]
    }
    assert collapse_official_native_tools_for_chat(payload, context) is True
    names = [item["name"] for item in payload["output"]]
    assert names == ["web_search", "apply_patch", "tool_search"]
    assert json.loads(payload["output"][0]["arguments"]) == {"action": {"query": "codex"}}
    assert json.loads(payload["output"][1]["arguments"]) == {CUSTOM_INPUT_KEY: "diff"}
    assert json.loads(payload["output"][2]["arguments"]) == {TOOL_SEARCH_INPUT_KEY: {"query": "x"}}
    result_payload = {
        "output": [
            {
                "type": "custom_tool_call_output",
                "call_id": "call_patch",
                "output": "ok",
            },
            {
                "type": "tool_search_output",
                "call_id": "call_search",
                "execution": "client",
                "status": "completed",
            },
        ]
    }
    assert collapse_official_native_tools_for_chat(result_payload, context) is True
    assert result_payload["output"][0]["type"] == "function_call_output"
    assert CUSTOM_OUTPUT_KEY in result_payload["output"][0]["output"]
    assert result_payload["output"][1]["type"] == "function_call_output"
    assert TOOL_SEARCH_OUTPUT_KEY in json.loads(result_payload["output"][1]["output"])


def test_collapse_official_web_search_drops_response_ciphertext() -> None:
    context = {
        CHAT_OFFICIAL_NATIVE_NAME_MAP_KEY: {
            "hosted": {"web_search": "web_search"},
            "hosted_event": {"web_search_call": "web_search"},
            "custom": {},
            "tool_search": "",
        }
    }
    payload = {
        "output": [
            {
                "type": "web_search_call",
                "id": "ws_1",
                "call_id": "call_ws",
                "status": "completed",
                "action": {"type": "search", "query": "Codex CLI"},
                "encrypted_content": "gAAAA-official-search",
                "encrypted_output": "gAAAA-search-result",
            },
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Codex CLI is the terminal client."}],
            },
        ]
    }
    assert collapse_official_native_tools_for_chat(payload, context) is True
    call = payload["output"][0]
    assert call["type"] == "function_call"
    assert call["name"] == "web_search"
    assert json.loads(call["arguments"]) == {"action": {"type": "search", "query": "Codex CLI"}}
    dumped = json.dumps(payload)
    assert "encrypted_content" not in dumped
    assert "encrypted_output" not in dumped
    assert payload["output"][1]["type"] == "message"


def test_incomplete_hosted_output_fails_closed() -> None:
    from tool_compatibility.contracts import ToolCompatibilityError

    with pytest.raises(ToolCompatibilityError):
        collapse_official_native_tools_for_chat(
            {"output": [{"type": "computer_use_preview_call", "id": "cu_1"}]},
            {
                CHAT_OFFICIAL_NATIVE_NAME_MAP_KEY: {
                    "hosted": {"web_search": "web_search"},
                    "hosted_event": {"web_search_call": "web_search"},
                    "custom": {},
                    "tool_search": "",
                }
            },
        )


def _third_party_responses() -> dict:
    return {
        "name": "opencode_go",
        "upstream_format": "responses",
        "tool_protocol": "responses_structured",
        "tool_surface_strategy": "eager",
    }


def _third_party_chat_tools() -> dict:
    return {
        "name": "opencode_go",
        "upstream_format": "chat_completions",
        "tool_protocol": "chat_tools",
        "tool_surface_strategy": "eager",
    }


def test_third_party_responses_keeps_caller_hosted_web_search() -> None:
    context: dict = {}
    prepared = json.loads(
        gateway_compat.compatible_request_body(
            json.dumps(
                {
                    "model": "muse",
                    "input": [{"role": "user", "content": "search"}],
                    "tools": [{"type": "web_search"}],
                    "tool_choice": "auto",
                }
            ).encode(),
            _third_party_responses(),
            event_context=context,
            inject_codex_tools=False,
        )
    )
    assert prepared["tools"] == [{"type": "web_search"}]
    assert context["_runtime_tool_compatibility_plan"].entries[0].disposition == "native"


def test_third_party_chat_tools_still_omits_hosted_web_search() -> None:
    context: dict = {}
    prepared = json.loads(
        gateway_compat.compatible_request_body(
            json.dumps(
                {
                    "model": "muse",
                    "input": [{"role": "user", "content": "search"}],
                    "tools": [{"type": "web_search"}],
                    "tool_choice": "auto",
                }
            ).encode(),
            _third_party_chat_tools(),
            event_context=context,
            inject_codex_tools=False,
        )
    )
    assert prepared.get("tools") in ([], None)
    assert context["_runtime_tool_compatibility_plan"].entries[0].disposition == "omit"


def test_collapse_third_party_hosted_search_for_chat_inbound() -> None:
    payload = {
        "output": [
            {
                "type": "web_search_call",
                "id": "ws_1",
                "call_id": "call_ws",
                "status": "completed",
                "action": {"query": "Codex CLI"},
                "encrypted_content": "gAAAA-third-party",
            }
        ]
    }
    assert collapse_official_native_tools_for_chat(
        payload,
        {"_caller_wire_format": "chat_completions"},
    )
    call = payload["output"][0]
    assert call["type"] == "function_call"
    assert call["name"] == "web_search"
    assert json.loads(call["arguments"]) == {"action": {"query": "Codex CLI"}}
    assert "encrypted_content" not in json.dumps(payload)


def test_third_party_chat_collapse_leaves_custom_and_tool_search() -> None:
    payload = {
        "output": [
            {
                "type": "custom_tool_call",
                "call_id": "call_patch",
                "name": "apply_patch",
                "input": "*** Begin Patch",
            },
            {
                "type": "tool_search_call",
                "call_id": "call_search",
                "arguments": {},
            },
            {
                "type": "custom_tool_call_output",
                "call_id": "call_patch",
                "output": "ok",
            },
            {
                "type": "tool_search_output",
                "call_id": "call_search",
                "output": [],
            },
        ]
    }
    assert (
        collapse_official_native_tools_for_chat(
            payload,
            {"_caller_wire_format": "chat_completions"},
        )
        is False
    )
    assert [item["type"] for item in payload["output"]] == [
        "custom_tool_call",
        "tool_search_call",
        "custom_tool_call_output",
        "tool_search_output",
    ]


def test_collapse_skips_third_party_hosted_search_on_responses_inbound() -> None:
    payload = {
        "output": [
            {
                "type": "web_search_call",
                "id": "ws_1",
                "call_id": "call_ws",
                "status": "completed",
                "action": {"query": "Codex CLI"},
            }
        ]
    }
    assert collapse_official_native_tools_for_chat(payload, {"_caller_wire_format": "responses"}) is False
    assert payload["output"][0]["type"] == "web_search_call"


def test_third_party_chat_response_body_collapses_hosted_search() -> None:
    context: dict = {"_caller_wire_format": "chat_completions"}
    gateway_compat.compatible_request_body(
        json.dumps(
            {
                "model": "muse",
                "input": [{"role": "user", "content": "search"}],
                "tools": [{"type": "web_search"}],
                "tool_choice": "auto",
            }
        ).encode(),
        _third_party_responses(),
        event_context=context,
        inject_codex_tools=False,
    )
    body = gateway_compat.compatible_response_body(
        json.dumps(
            {
                "id": "resp_muse",
                "status": "completed",
                "output": [
                    {
                        "type": "web_search_call",
                        "id": "ws_1",
                        "call_id": "call_ws",
                        "status": "completed",
                        "action": {"query": "Codex CLI"},
                    }
                ],
            }
        ).encode(),
        "opencode_go",
        event_context=context,
    )
    payload = json.loads(body)
    assert payload["output"][0]["type"] == "function_call"
    assert payload["output"][0]["name"] == "web_search"
