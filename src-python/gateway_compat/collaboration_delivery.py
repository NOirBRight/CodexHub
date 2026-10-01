"""Provider-neutral Collaboration delivery through Official ordinary tools."""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from typing import Any

from gateway_errors import UpstreamProtocolTranslationError
from protocol_translation import UnsupportedProtocolTranslationError

from code_mode_collaboration import expose_declared_collaboration, has_code_mode_exec, is_supported_subset

from collaboration_runtime_contract import (
    COLLABORATION_V2,
    CollaborationContractError,
    classify_collaboration_tools,
)

ALIAS = "codexhub_plaintext_collaboration"
CONTEXT_KEY = "official_plaintext_collaboration"
NAMES = {
    "spawn_agent", "send_message", "followup_task",
    "wait_agent", "list_agents", "interrupt_agent",
}
MESSAGE_TOOLS = {"spawn_agent", "send_message", "followup_task"}


def _portable_tools(tools: Any, *, code_mode: bool = False) -> Any:
    if not isinstance(tools, list):
        return tools
    try:
        if classify_collaboration_tools(tools) != COLLABORATION_V2:
            return tools
    except CollaborationContractError:
        # Current Code Mode child subsets retain exactly the declared handlers.
        namespaces = [tool for tool in tools if isinstance(tool, dict) and tool.get("name") == "collaboration"]
        if not code_mode or len(namespaces) != 1 or not is_supported_subset(namespaces[0]):
            return tools
    result = copy.deepcopy(tools)
    for namespace in result:
        if not isinstance(namespace, dict) or namespace.get("name") != "collaboration":
            continue
        namespace["name"] = ALIAS
        for child in namespace["tools"]:
            if child["name"] in MESSAGE_TOOLS:
                child["parameters"]["properties"]["message"].pop("encrypted", None)
    return result


def make_messages_portable(payload: dict[str, Any]) -> bool:
    """Copy declarations and plaintext call identities, preserving ciphertext.

    Official reserves the native schema (including encryption), so use an
    ordinary namespace and inverse-map its responses. The child provider is
    chosen after the parent request; all V2 message tools must be portable.
    Both top-level tools and input additional_tools retain their placement.
    """
    # An existing alias is unowned; do not expand or partially modify a collision.
    original_groups = [payload.get("tools")]
    original_items = payload.get("input")
    if isinstance(original_items, list):
        original_groups.extend(item.get("tools") for item in original_items if isinstance(item, dict) and item.get("type") == "additional_tools")
    if any(isinstance(tool, Mapping) and tool.get("name") == ALIAS for group in original_groups if isinstance(group, list) for tool in group):
        return False
    # Expand actual typed exec handler declarations before aliasing.
    code_mode = any(has_code_mode_exec(group) for group in original_groups)
    code_mode = expose_declared_collaboration(payload) or code_mode
    items = payload.get("input")
    groups = [payload.get("tools")]
    if isinstance(items, list):
        groups.extend(
            item.get("tools") for item in items
            if isinstance(item, dict) and item.get("type") == "additional_tools"
        )
    if any(
        isinstance(tool, dict) and tool.get("name") == ALIAS
        for group in groups if isinstance(group, list) for tool in group
    ):
        return False
    changed = False
    tools = payload.get("tools")
    portable = _portable_tools(tools, code_mode=code_mode)
    if portable != tools:
        payload["tools"] = portable
        changed = True
    if isinstance(items, list):
        result = list(items)
        for index, item in enumerate(items):
            if not isinstance(item, dict) or item.get("type") != "additional_tools":
                continue
            tools = item.get("tools")
            portable = _portable_tools(tools, code_mode=code_mode)
            if portable != tools:
                result[index] = {**item, "tools": portable}
                changed = True
        if changed:
            for index, item in enumerate(result):
                if (
                    isinstance(item, dict)
                    and item.get("type") == "function_call"
                    and item.get("namespace") == "collaboration"
                    and item.get("name") in NAMES
                    and item.get("encrypted_function_args") == []
                ):
                    result[index] = {**item, "namespace": ALIAS}
            payload["input"] = result
    return changed


def portable_handler_names(payload: Mapping[str, Any]) -> tuple[str, ...]:
    """Return exactly the methods exposed by this request's portable declaration."""
    groups = [payload.get("tools")]
    items = payload.get("input")
    if isinstance(items, list):
        groups.extend(item.get("tools") for item in items if isinstance(item, Mapping) and item.get("type") == "additional_tools")
    return tuple(
        child["name"]
        for group in groups if isinstance(group, list)
        for tool in group if isinstance(tool, Mapping) and tool.get("name") == ALIAS
        for child in tool.get("tools", []) if isinstance(child, Mapping) and child.get("name") in NAMES
    )


def decode_body(body: bytes, context: Any) -> bytes:
    if not isinstance(context, Mapping) or not context.get(CONTEXT_KEY):
        return body
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeError):
        return body
    enabled = context[CONTEXT_KEY]
    allowed = NAMES if enabled is True else set(enabled) if isinstance(enabled, (tuple, list, set, frozenset)) else set()
    changed = False

    def visit(value: Any) -> None:
        nonlocal changed
        if isinstance(value, dict):
            if value.get("type") == "function_call" and value.get("namespace") == ALIAS:
                if value.get("name") not in allowed:
                    raise UpstreamProtocolTranslationError(UnsupportedProtocolTranslationError(
                        "collaboration_tool_not_declared", "Upstream returned a collaboration method this caller did not declare.",
                    ))
                if value.get("encrypted_function_args"):
                    raise UpstreamProtocolTranslationError(UnsupportedProtocolTranslationError(
                        "encrypted_collaboration_arguments_unavailable", "Upstream returned opaque collaboration arguments for a plaintext tool.",
                    ))
                value["namespace"] = "collaboration"
                value["encrypted_function_args"] = []
                changed = True
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(payload)
    return json.dumps(payload, separators=(",", ":")).encode() if changed else body


def decode_sse_line(line: bytes, context: Any) -> bytes:
    if not line.startswith(b"data:"):
        return line
    body = line[5:].strip()
    decoded = decode_body(body, context)
    if decoded == body:
        return line
    ending = b"\r\n" if line.endswith(b"\r\n") else b"\n" if line.endswith(b"\n") else b""
    return b"data: " + decoded + ending
