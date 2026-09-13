"""Read-only transcript rendering for Gateway compatibility surfaces.

The module owns the text form of internal Responses items used when a route
cannot carry their native shape: compaction context, tool call/result
transcripts, and assistant transcripts. Callers import it one-way; the
collaborating adapters are imported lazily inside the rendering functions so
importing this module never starts a Gateway package import.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from protocol_translation import UnsupportedProtocolTranslationError

__all__ = ["internal_message", "transcript_message"]


def _compatible_compaction_message(item: Mapping[str, Any]) -> dict[str, str] | None:
    from gateway_stream_semantics import collect_text_fragments

    seen: set[str] = set()
    fragments: list[str] = []
    for fragment in collect_text_fragments(dict(item)):
        if fragment not in seen:
            seen.add(fragment)
            fragments.append(fragment)

    if not fragments:
        return _developer_text_message(
            "[Compacted conversation context — opaque, details unavailable]"
        )

    return _developer_text_message("[Compacted conversation context]\n" + "\n\n".join(fragments))


def _developer_text_message(content: str) -> dict[str, str]:
    return {"type": "message", "role": "developer", "content": content}


def _stringify_internal_field(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    try:
        return json.dumps(value, ensure_ascii=True, separators=(",", ":"))
    except (TypeError, ValueError):
        return str(value).strip()


def _append_internal_field(lines: list[str], label: str, value: Any) -> None:
    text = _stringify_internal_field(value)
    if not text:
        return
    lines.append(f"{label}:")
    lines.append(text)


def _transcript_text(title: str, item: Mapping[str, Any]) -> str:
    lines = [title]
    for label, key in (
        ("type", "type"),
        ("namespace", "namespace"),
        ("name", "name"),
        ("call_id", "call_id"),
        ("status", "status"),
    ):
        value = _stringify_internal_field(item.get(key))
        if value:
            lines.append(f"{label}: {value}")
    _append_internal_field(lines, "input", item.get("input"))
    _append_internal_field(lines, "arguments", item.get("arguments"))
    _append_internal_field(lines, "output", item.get("output"))
    _append_internal_field(lines, "action", item.get("action"))
    _append_internal_field(lines, "execution", item.get("execution"))
    _append_internal_field(lines, "tools", item.get("tools"))
    return "\n".join(lines)


def transcript_message(title: str, item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": _transcript_text(title, item)}],
    }


def _result_transcript_lines(title: str, item: Mapping[str, Any]) -> list[str]:
    """Render one call/result pair; structured media cannot become text."""

    import multimodal_tool_result as _multimodal_tool_result

    if _multimodal_tool_result.output_contains_structured_media(item.get("output")):
        raise UnsupportedProtocolTranslationError(
            "unsupported_protocol_semantics",
            "Structured tool-result media cannot be stringified into transcript text.",
        )
    lines = [title]
    value = _stringify_internal_field(item.get("call_id"))
    if value:
        lines.append(f"call_id: {value}")
    _append_internal_field(lines, "output", item.get("output"))
    return lines


def _compatible_tool_message(item: Mapping[str, Any]) -> dict[str, str] | None:
    from tool_surface_adapter import (
        TOOL_SEARCH_EMPTY_MISS_BOUND,
        TOOL_SEARCH_UNAVAILABLE_QUERY_CLASSIFICATION,
    )

    item_type = item.get("type")
    if item_type == "custom_tool_call":
        lines = ["Read-only Codex tool call transcript"]
        for label, key in (("tool", "name"), ("call_id", "call_id"), ("status", "status")):
            value = _stringify_internal_field(item.get(key))
            if value:
                lines.append(f"{label}: {value}")
        _append_internal_field(lines, "input", item.get("input"))
    elif item_type == "custom_tool_call_output":
        lines = _result_transcript_lines("Read-only Codex tool result transcript", item)
    elif item_type == "function_call":
        lines = ["Read-only Codex function call transcript"]
        for label, key in (("namespace", "namespace"), ("function", "name"), ("call_id", "call_id"), ("status", "status")):
            value = _stringify_internal_field(item.get(key))
            if value:
                lines.append(f"{label}: {value}")
        _append_internal_field(lines, "arguments", item.get("arguments"))
    elif item_type == "function_call_output":
        lines = _result_transcript_lines("Read-only Codex function result transcript", item)
    elif item_type == "web_search_call":
        lines = ["Read-only Codex web search call transcript"]
        value = _stringify_internal_field(item.get("status"))
        if value:
            lines.append(f"status: {value}")
        _append_internal_field(lines, "action", item.get("action"))
    elif item_type == "tool_search_call":
        lines = ["Read-only Codex tool search call transcript"]
        for label, key in (("call_id", "call_id"), ("status", "status")):
            value = _stringify_internal_field(item.get(key))
            if value:
                lines.append(f"{label}: {value}")
        _append_internal_field(lines, "arguments", item.get("arguments"))
        _append_internal_field(lines, "execution", item.get("execution"))
    elif item_type == "tool_search_output":
        lines = ["Read-only Codex tool search result transcript"]
        for label, key in (("call_id", "call_id"), ("status", "status")):
            value = _stringify_internal_field(item.get(key))
            if value:
                lines.append(f"{label}: {value}")
        _append_internal_field(lines, "execution", item.get("execution"))
        import gateway_compat.multi_agent as _multi_agent

        if _multi_agent._has_multi_agent_discovery_tools(item.get("tools")):
            lines.append("status: discovered_codex_native_multi_agent_tools")
            lines.append(
                "available_function_tools: multi_agent_v1__spawn_agent, multi_agent_v1__wait_agent, multi_agent_v1__close_agent, multi_agent_v1__resume_agent, multi_agent_v1__send_input"
            )
        if item.get("query_classification") == TOOL_SEARCH_UNAVAILABLE_QUERY_CLASSIFICATION:
            lines.append(f"query_classification: {TOOL_SEARCH_UNAVAILABLE_QUERY_CLASSIFICATION}")
            lines.append(f"empty_miss_count: {TOOL_SEARCH_EMPTY_MISS_BOUND}")
            lines.append("terminal: true")
        _append_internal_field(lines, "tools", item.get("tools"))
    else:
        return None

    if len(lines) == 1:
        return None
    return _developer_text_message("\n".join(lines))


def internal_message(item: Mapping[str, Any]) -> dict[str, str] | None:
    if item.get("type") == "compaction":
        return _compatible_compaction_message(item)
    if item.get("type") == "reasoning":
        return None
    return _compatible_tool_message(item)
