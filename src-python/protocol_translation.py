"""Pure-ish wire-format translations between Responses and Chat Completions.

The Gateway owns routing, transport, retries, and Codex-specific semantic
repair.  This module owns only the protocol shapes used at that boundary.
Optional callbacks keep the few Gateway-owned naming and repair policies out of
the translation implementation while preserving existing behavior.

Only the documented lossless subset crosses this seam: portable text,
URL-backed images (including detail), and paired function calls.
Responses text-part ``annotations`` (official hosted-search citations)
are dropped so Chat can keep the answer; Chat clients that send
annotations still fail closed.
The longstanding developer-to-system/instructions text compatibility mapping
remains for third-party Chat endpoints. Other semantic items—including new
content fields—raise ``UnsupportedProtocolTranslationError`` instead of being
dropped or rewritten.
"""

from __future__ import annotations

import json
from protocol_json import AmbiguousJSONError, strict_json_loads
import re
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from prompt_cache_policy import PromptCacheKeyPolicy
from tool_compatibility.contracts import ToolCompatibilityError
from tool_compatibility.chat_official_native import collapse_hosted_item_for_chat
from tool_compatibility.dispositions import CHAT_NATIVE_TOOL_TYPES, CHAT_OFFICIAL_HOSTED_KINDS
import uuid

from gateway_errors import (
    UpstreamProtocolTranslationError,
    UpstreamStreamIncompleteError,
)


ChatContentText = Callable[[Any], str]
CollectTextFragments = Callable[[Any], list[str]]
FunctionNameFromResponseItem = Callable[[Mapping[str, Any]], str | None]
NormalizeChatFunctionName = Callable[[str], str]
XmlishToolOutputs = Callable[[str], list[dict[str, Any]]]
ResponseRepair = Callable[[dict[str, Any]], dict[str, Any]]
UsageFromResponse = Callable[[Mapping[str, Any]], Mapping[str, Any] | None]

class UnsupportedProtocolTranslationError(ValueError):
    """Raised when a wire shape cannot cross the protocol seam losslessly."""

    def __init__(self, code: str, detail: str):
        self.code = code
        super().__init__(detail)


def decode_protocol_json(value: str | bytes) -> Any:
    """Reject ambiguous JSON at an explicitly selected conversion seam."""
    try:
        return strict_json_loads(value)
    except AmbiguousJSONError as exc:
        raise UnsupportedProtocolTranslationError(
            "invalid_json_envelope", "Adapted JSON contains ambiguous fields or unsupported numeric/nesting limits."
        ) from exc


def _default_collect_text_fragments(value: Any) -> list[str]:
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, list):
        return [fragment for item in value for fragment in _default_collect_text_fragments(item)]
    if isinstance(value, Mapping):
        return [
            fragment
            for key in ("text", "content", "summary", "message")
            if key in value
            for fragment in _default_collect_text_fragments(value[key])
        ]
    return []


def _default_chat_content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    return "\n".join(_default_collect_text_fragments(value))


def _default_function_name_from_response_item(item: Mapping[str, Any]) -> str | None:
    name = item.get("name")
    return name if isinstance(name, str) and name else None


def _default_usage_from_response(response: Mapping[str, Any]) -> Mapping[str, Any] | None:
    usage = response.get("usage")
    return usage if isinstance(usage, Mapping) else None


def _chat_completion_usage_to_responses_usage(usage: Mapping[str, Any]) -> dict[str, Any]:
    mapped: dict[str, Any] = {}
    for source_name, target_name in (
        ("prompt_tokens", "input_tokens"),
        ("completion_tokens", "output_tokens"),
        ("total_tokens", "total_tokens"),
        ("prompt_tokens_details", "input_tokens_details"),
        ("completion_tokens_details", "output_tokens_details"),
    ):
        if source_name in usage:
            value = usage[source_name]
            mapped[target_name] = dict(value) if isinstance(value, Mapping) else value
    return mapped


def _responses_usage_to_chat_usage(usage: Mapping[str, Any]) -> dict[str, Any]:
    mapped: dict[str, Any] = {}
    for source_name, target_name in (
        ("input_tokens", "prompt_tokens"),
        ("output_tokens", "completion_tokens"),
        ("total_tokens", "total_tokens"),
        ("input_tokens_details", "prompt_tokens_details"),
        ("output_tokens_details", "completion_tokens_details"),
    ):
        if source_name in usage:
            value = usage[source_name]
            mapped[target_name] = dict(value) if isinstance(value, Mapping) else value
    return mapped


def _raise_for_unsupported_chat_message_semantics(message: Mapping[str, Any]) -> None:
    for field in ("refusal", "audio", "annotations"):
        value = message.get(field)
        if value not in (None, "", [], {}):
            raise UnsupportedProtocolTranslationError(
                "unsupported_protocol_semantics",
                f"Cannot translate Chat Completions message field {field!r} to Responses without losing it.",
            )


def _chat_reasoning_output(message: Mapping[str, Any]) -> dict[str, Any] | None:
    texts: list[str] = []

    def add_text(value: Any) -> None:
        if isinstance(value, str):
            text = value.strip()
            if text and text not in texts:
                texts.append(text)

    add_text(message.get("reasoning"))
    add_text(message.get("reasoning_content"))
    details = message.get("reasoning_details")
    if isinstance(details, list):
        for part in details:
            if isinstance(part, Mapping):
                add_text(part.get("text") or part.get("summary"))
            else:
                add_text(part)
    if not texts:
        return None
    return {
        "type": "reasoning",
        "status": "completed",
        "summary": [{"type": "summary_text", "text": text} for text in texts],
    }


def _responses_reasoning_text(item: Mapping[str, Any]) -> str:
    """Return the portable summary text for one Responses reasoning item.

    Chat Completions providers that require thinking history accept the
    provider-neutral ``reasoning_content`` field.  Encrypted/raw reasoning is
    intentionally not guessed or discarded: without a portable summary the
    request cannot cross the seam losslessly and must fail closed.
    """

    text = _portable_output_reasoning_text(item, drop_encrypted=False)
    if text is None:
        raise UnsupportedProtocolTranslationError(
            "unsupported_protocol_semantics",
            "Cannot translate an empty Responses reasoning item to Chat Completions.",
        )
    return text


def _portable_output_reasoning_text(
    item: Mapping[str, Any],
    *,
    drop_encrypted: bool,
) -> str | None:
    """Return portable Chat reasoning text, or None when ciphertext-only output may be skipped."""

    summary = item.get("summary")
    if summary is None:
        summary = []
    elif not isinstance(summary, list):
        raise UnsupportedProtocolTranslationError(
            "unsupported_protocol_semantics",
            "Cannot translate a Responses reasoning item without a summary list.",
        )
    texts: list[str] = []
    for part in summary:
        if not isinstance(part, Mapping):
            raise UnsupportedProtocolTranslationError(
                "unsupported_protocol_semantics",
                "Cannot translate a non-object Responses reasoning summary part.",
            )
        _require_supported_fields(
            part,
            {"type", "text"},
            "Responses reasoning summary part",
        )
        if part.get("type") != "summary_text" or not isinstance(part.get("text"), str):
            raise UnsupportedProtocolTranslationError(
                "unsupported_protocol_semantics",
                "Cannot translate a non-text Responses reasoning summary part.",
            )
        text = part["text"].strip()
        if text and text not in texts:
            texts.append(text)
    content = item.get("content")
    if content not in (None, "", []):
        if not isinstance(content, list):
            raise UnsupportedProtocolTranslationError(
                "unsupported_protocol_semantics",
                "Cannot translate non-list Responses reasoning content.",
            )
        for part in content:
            if not isinstance(part, Mapping):
                raise UnsupportedProtocolTranslationError(
                    "unsupported_protocol_semantics",
                    "Cannot translate a non-object Responses reasoning content part.",
                )
            _require_supported_fields(
                part,
                {"type", "text"},
                "Responses reasoning content part",
            )
            if part.get("type") != "reasoning_text" or not isinstance(part.get("text"), str):
                raise UnsupportedProtocolTranslationError(
                    "unsupported_protocol_semantics",
                    "Cannot translate a non-text Responses reasoning content part.",
                )
            text = part["text"].strip()
            if text and text not in texts:
                texts.append(text)
    encrypted = item.get("encrypted_content")
    if encrypted not in (None, "") and not drop_encrypted:
        raise UnsupportedProtocolTranslationError(
            "unsupported_protocol_semantics",
            "Cannot translate encrypted Responses reasoning content to Chat Completions.",
        )
    if texts:
        return "\n".join(texts)
    if drop_encrypted:
        return None
    raise UnsupportedProtocolTranslationError(
        "unsupported_protocol_semantics",
        "Cannot translate an empty Responses reasoning item to Chat Completions.",
    )


def _require_supported_chat_message_fields(message: Mapping[str, Any], label: str) -> None:
    role = message.get("role")
    allowed_by_role = {
        "system": {"role", "content"},
        "user": {"role", "content"},
        "assistant": {
            "role",
            "content",
            "tool_calls",
            "refusal",
            "audio",
            "annotations",
            "reasoning",
            "reasoning_content",
            "reasoning_details",
        },
        "tool": {"role", "content", "tool_call_id"},
    }
    _require_supported_fields(message, allowed_by_role.get(role, {"role", "content"}), label)
    _raise_for_unsupported_chat_message_semantics(message)
    content = message.get("content")
    if content is not None and not isinstance(content, (str, list)):
        raise UnsupportedProtocolTranslationError(
            "unsupported_protocol_semantics",
            f"Cannot translate a non-string, non-list {label} content value.",
        )


def _require_supported_fields(value: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unsupported = sorted(str(key) for key in value.keys() if key not in allowed)
    if unsupported:
        raise UnsupportedProtocolTranslationError(
            "unsupported_protocol_semantics",
            f"Cannot translate {label} fields without losing them: {', '.join(unsupported)}.",
        )


def _require_omittable_responses_transport_fields(payload: Mapping[str, Any]) -> None:
    """Accept only Responses transport defaults that have no Chat meaning."""

    safe_defaults: dict[str, Any] = {
        "client_metadata": {},
        "include": [],
        "prompt_cache_key": "",
        "store": False,
        "text": {},
    }
    for key, default in safe_defaults.items():
        if key not in payload:
            continue
        value = payload.get(key)
        if value is None:
            continue
        if value != default:
            raise UnsupportedProtocolTranslationError(
                "unsupported_protocol_semantics",
                f"Cannot translate Responses {key!r} without losing its semantics.",
            )


def _consume_codex_chat_transport_fields(
    payload: dict[str, Any],
    *,
    preserve_reasoning_history: bool = False,
) -> None:
    """Consume the bounded Codex transport defaults with no Chat wire form."""

    client_metadata = payload.get("client_metadata")
    if client_metadata is not None and not isinstance(client_metadata, Mapping):
        raise UnsupportedProtocolTranslationError(
            "unsupported_protocol_semantics",
            "Cannot consume non-object Responses client_metadata.",
        )
    payload.pop("client_metadata", None)

    include = payload.get("include")
    if include is not None:
        if not isinstance(include, list) or any(
            value != "reasoning.encrypted_content" for value in include
        ):
            raise UnsupportedProtocolTranslationError(
                "unsupported_protocol_semantics",
                "Cannot consume unknown Responses include semantics.",
            )
    payload.pop("include", None)

    prompt_cache_key = payload.get("prompt_cache_key")
    if prompt_cache_key is not None and not isinstance(prompt_cache_key, str):
        raise UnsupportedProtocolTranslationError(
            "unsupported_protocol_semantics",
            "Cannot consume non-string Responses prompt_cache_key.",
        )
    payload.pop("prompt_cache_key", None)

    store = payload.get("store")
    if store is not None and store is not False:
        raise UnsupportedProtocolTranslationError(
            "unsupported_protocol_semantics",
            "Cannot consume Responses store=true on a Chat route.",
        )
    payload.pop("store", None)

    text = payload.get("text")
    if text is not None:
        if not isinstance(text, Mapping) or set(text) - {"verbosity"}:
            raise UnsupportedProtocolTranslationError(
                "unsupported_protocol_semantics",
                "Cannot consume unknown Responses text semantics.",
            )
        verbosity = text.get("verbosity")
        if verbosity is not None and (
            not isinstance(verbosity, str)
            or verbosity not in {"low", "medium", "high"}
        ):
            raise UnsupportedProtocolTranslationError(
                "unsupported_protocol_semantics",
                "Cannot consume unknown Responses text verbosity.",
            )
    payload.pop("text", None)

    reasoning = payload.get("reasoning")
    if reasoning is not None:
        # Codex Desktop may still send Responses reasoning selectors when it
        # is using a catalog generated before the Chat capability flags were
        # corrected. Chat Completions has no portable representation for these
        # controls, so consume the documented bounded selectors locally rather
        # than rejecting the otherwise valid request. Unknown selectors remain
        # fail-closed; this is compatibility handling, not a claim that Chat
        # can return Responses reasoning controls or summaries.
        if not isinstance(reasoning, Mapping) or set(reasoning) - {
            "effort",
            "summary",
            "mode",
            "context",
            "generate_summary",
        }:
            raise UnsupportedProtocolTranslationError(
                "unsupported_protocol_semantics",
                "Cannot consume unknown Responses reasoning semantics.",
            )
        effort = reasoning.get("effort")
        if effort is not None and (
            not isinstance(effort, str)
            or effort not in {"none", "minimal", "low", "medium", "high", "xhigh", "max"}
        ):
            raise UnsupportedProtocolTranslationError(
                "unsupported_protocol_semantics",
                "Cannot consume unknown Responses reasoning effort.",
            )
        summary = reasoning.get("summary")
        if summary is not None and (
            not isinstance(summary, str)
            or summary not in {"auto", "concise", "detailed"}
        ):
            raise UnsupportedProtocolTranslationError(
                "unsupported_protocol_semantics",
                "Cannot consume unknown Responses reasoning summary.",
            )
        generate_summary = reasoning.get("generate_summary")
        if generate_summary is not None and (
            not isinstance(generate_summary, str)
            or generate_summary not in {"auto", "concise", "detailed"}
        ):
            raise UnsupportedProtocolTranslationError(
                "unsupported_protocol_semantics",
                "Cannot consume unknown Responses reasoning generate_summary.",
            )
        mode = reasoning.get("mode")
        if mode is not None and (
            not isinstance(mode, str)
            or mode not in {"standard", "pro"}
        ):
            raise UnsupportedProtocolTranslationError(
                "unsupported_protocol_semantics",
                "Cannot consume unknown Responses reasoning mode.",
            )
        context = reasoning.get("context")
        if context is not None and (
            not isinstance(context, str)
            or context not in {"auto", "current_turn", "all_turns"}
        ):
            raise UnsupportedProtocolTranslationError(
                "unsupported_protocol_semantics",
                "Cannot consume unknown Responses reasoning context.",
            )
    # Keep an explicit reasoning selector for the capability-bound Chat route
    # so the upstream can actually enter its thinking mode and return the
    # reasoning trace that must be echoed on a follow-up tool call.  Ordinary
    # cross-protocol routes still consume this Responses-only control.
    if not preserve_reasoning_history:
        payload.pop("reasoning", None)


def _function_arguments(value: Mapping[str, Any], label: str) -> str:
    if "arguments" not in value:
        return ""
    arguments = value.get("arguments")
    if not isinstance(arguments, str):
        raise UnsupportedProtocolTranslationError(
            "unsupported_protocol_semantics",
            f"Cannot translate non-string {label} arguments.",
        )
    return arguments


def _validate_function_tool_fields(value: Mapping[str, Any], allowed: set[str], label: str) -> None:
    _require_supported_fields(value, allowed, label)
    description = value.get("description")
    if description is not None and not isinstance(description, str):
        raise UnsupportedProtocolTranslationError(
            "unsupported_protocol_semantics",
            f"Cannot translate non-string {label} description.",
        )
    parameters = value.get("parameters")
    if parameters is not None and not isinstance(parameters, dict):
        raise UnsupportedProtocolTranslationError(
            "unsupported_protocol_semantics",
            f"Cannot translate non-object {label} parameters.",
        )
    strict = value.get("strict")
    if strict is not None and not isinstance(strict, bool):
        raise UnsupportedProtocolTranslationError(
            "unsupported_protocol_semantics",
            f"Cannot translate non-boolean {label} strict value.",
        )


def responses_content_to_chat_content(value: Any) -> str | list[dict[str, Any]]:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    if not isinstance(value, list):
        raise UnsupportedProtocolTranslationError(
            "unsupported_protocol_semantics",
            "Cannot translate a non-string, non-list Responses content value.",
        )

    parts: list[dict[str, Any]] = []
    text_fragments: list[str] = []
    has_image = False
    for part in value:
        if not isinstance(part, Mapping):
            raise UnsupportedProtocolTranslationError(
                "unsupported_protocol_semantics",
                "Cannot translate a non-object Responses content part.",
            )
        part_type = part.get("type")
        if part_type in {"input_text", "output_text", "text"} and isinstance(part.get("text"), str):
            _require_supported_fields(
                part,
                {"type", "text", "annotations", "logprobs"},
                "Responses text content part",
            )
            text = part["text"]
            text_fragments.append(text)
            parts.append({"type": "text", "text": text})
            continue
        if part_type == "input_image" and isinstance(part.get("image_url"), str):
            _require_supported_fields(part, {"type", "image_url", "detail"}, "Responses image content part")
            has_image = True
            image_url: dict[str, str] = {"url": part["image_url"]}
            detail = part.get("detail")
            if isinstance(detail, str):
                image_url["detail"] = detail
            elif detail is not None:
                raise UnsupportedProtocolTranslationError(
                    "unsupported_protocol_semantics",
                    "Cannot translate a non-string Responses image detail value.",
                )
            parts.append({"type": "image_url", "image_url": image_url})
            continue
        if part_type == "input_image" and isinstance(part.get("file_id"), str):
            raise UnsupportedProtocolTranslationError(
                "unsupported_protocol_semantics",
                "Cannot translate a Responses image file reference to Chat Completions without changing it to text.",
            )
        raise UnsupportedProtocolTranslationError(
            "unsupported_protocol_semantics",
            f"Cannot translate Responses content part type {part_type!r} to Chat Completions.",
        )

    if has_image:
        return parts or [{"type": "text", "text": ""}]
    return "\n".join(fragment for fragment in text_fragments if fragment)


def responses_function_call_output_to_chat_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        raise UnsupportedProtocolTranslationError(
            "unsupported_protocol_semantics",
            "Cannot translate a non-string, non-list Responses function-call output to Chat Completions.",
        )

    text_fragments: list[str] = []
    for part in value:
        if not isinstance(part, Mapping):
            raise UnsupportedProtocolTranslationError(
                "unsupported_protocol_semantics",
                "Cannot translate a non-object Responses function-call output part to Chat Completions.",
            )
        if part.get("type") != "input_text" or not isinstance(part.get("text"), str):
            raise UnsupportedProtocolTranslationError(
                "unsupported_protocol_semantics",
                f"Cannot translate Responses function-call output part type {part.get('type')!r} to Chat Completions.",
            )
        _require_supported_fields(part, {"type", "text"}, "Responses function-call output text part")
        text_fragments.append(part["text"])
    return "\n".join(text_fragments)


def responses_input_to_chat_messages(
    value: Any,
    *,
    loaded_tools: list[dict[str, Any]] | None = None,
    preserve_reasoning_history: bool = False,
) -> list[dict[str, Any]]:
    if isinstance(value, str):
        return [{"role": "user", "content": value}]
    if value is None:
        return []
    if not isinstance(value, list):
        raise UnsupportedProtocolTranslationError(
            "unsupported_protocol_semantics",
            "Cannot translate a non-string, non-list Responses input payload.",
        )

    messages: list[dict[str, Any]] = []
    # Chat Completions requires every assistant tool-call message to be
    # followed immediately by its tool result(s).  Codex Responses history
    # can contain an empty/metadata assistant message between a function call
    # and its output; defer such ordinary messages until the pending tool
    # results have been emitted rather than sending an invalid Chat sequence.
    pending_call_ids: list[str] = []
    deferred_messages: list[dict[str, Any]] = []
    pending_reasoning: list[str] = []
    # Keep the association between a Responses assistant tool-call turn and
    # any assistant metadata/message items that had to be deferred until its
    # tool result.  A reasoning item can be replayed after that result; in
    # that case the observed trace may be copied only to the deferred items
    # from the same turn, never to an unrelated newer assistant message.
    tool_turn_records: list[dict[str, Any]] = []
    active_tool_turn: dict[str, Any] | None = None

    def pending_reasoning_text() -> str:
        return "\n".join(dict.fromkeys(text for text in pending_reasoning if text))

    def flush_reasoning_message() -> None:
        if not pending_reasoning:
            return
        text = pending_reasoning_text()
        messages.append(
            {
                "role": "assistant",
                "content": None,
                "reasoning_content": text,
            }
        )
        pending_reasoning.clear()

    def attach_reasoning(message: dict[str, Any]) -> None:
        if pending_reasoning:
            text = pending_reasoning_text()
            existing = message.get("reasoning_content")
            if isinstance(existing, str) and existing:
                if text and text not in existing:
                    message["reasoning_content"] = f"{existing}\n{text}"
            else:
                message["reasoning_content"] = text
            pending_reasoning.clear()

    for item in value:
        if not isinstance(item, dict):
            raise UnsupportedProtocolTranslationError(
                "unsupported_protocol_semantics",
                "Cannot translate a non-object Responses input item.",
            )
        item_type = item.get("type")
        if item_type == "message" or (item_type is None and ("role" in item or "content" in item)):
            _require_supported_fields(item, {"id", "type", "role", "content"}, "Responses message input item")
            role = item.get("role")
            if role == "developer":
                role = "system"
            elif role not in {"system", "user", "assistant"}:
                raise UnsupportedProtocolTranslationError(
                    "unsupported_protocol_semantics",
                    f"Cannot translate Responses message role {role!r} to Chat Completions.",
                )
            translated_message = {
                "role": role,
                "content": responses_content_to_chat_content(item.get("content")),
            }
            if pending_reasoning and role == "assistant":
                attach_reasoning(translated_message)
            elif pending_reasoning:
                # Preserve the original order rather than attaching an
                # assistant's private reasoning to a following user turn.
                flush_reasoning_message()
            elif (
                pending_call_ids
                and preserve_reasoning_history
                and role == "assistant"
                and active_tool_turn is not None
                and active_tool_turn.get("reasoning_text")
            ):
                # Thinking-mode Chat providers validate assistant messages
                # that are deferred around a tool result as well.  The
                # Responses stream can place an assistant metadata/message
                # item between a function call and its output, after the
                # corresponding reasoning item has already been attached to
                # the tool-call turn.  Reuse that observed trace on the
                # deferred assistant message; do not invent or normalize
                # provider-private reasoning.
                translated_message["reasoning_content"] = active_tool_turn["reasoning_text"]
            if pending_call_ids:
                deferred_messages.append(translated_message)
                if role == "assistant" and active_tool_turn is not None:
                    active_tool_turn.setdefault("deferred_assistants", []).append(translated_message)
            else:
                messages.append(translated_message)
            continue
        if item_type == "reasoning":
            if not preserve_reasoning_history:
                raise UnsupportedProtocolTranslationError(
                    "unsupported_protocol_semantics",
                    "Responses reasoning history requires an explicit Chat capability.",
                )
            if pending_call_ids:
                raise UnsupportedProtocolTranslationError(
                    "unsupported_protocol_semantics",
                    "Cannot place Responses reasoning history between a tool call and its result.",
                )
            _require_supported_fields(
                item,
                {"id", "type", "status", "summary", "content", "encrypted_content"},
                "Responses reasoning input item",
            )
            pending_reasoning.append(_responses_reasoning_text(item))
            continue
        if item_type == "function_call":
            _require_supported_fields(
                item,
                {"id", "type", "call_id", "name", "arguments"},
                "Responses function-call input item",
            )
            call_id = item.get("call_id")
            name = item.get("name")
            if not isinstance(call_id, str) or not call_id:
                raise UnsupportedProtocolTranslationError(
                    "unpaired_tool_call",
                    "Cannot translate a function call without a non-empty call_id.",
                )
            if not isinstance(name, str) or not name:
                raise UnsupportedProtocolTranslationError(
                    "unsupported_protocol_semantics",
                    "Cannot translate a function call without a non-empty name.",
                )
            arguments = _function_arguments(item, "Responses function-call")
            tool_call = {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": arguments},
            }
            if pending_call_ids and messages and messages[-1].get("role") == "assistant":
                # Consecutive Responses function calls are one Chat assistant
                # tool-call turn.  Group them so their outputs can follow as a
                # contiguous tool-message block.
                messages[-1].setdefault("tool_calls", []).append(tool_call)
            else:
                translated_call = {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [tool_call],
                }
                attach_reasoning(translated_call)
                messages.append(translated_call)
                active_tool_turn = {
                    "message": translated_call,
                    "reasoning_text": translated_call.get("reasoning_content"),
                    "deferred_assistants": [],
                }
                tool_turn_records.append(active_tool_turn)
            pending_call_ids.append(call_id)
            continue
        if item_type == "function_call_output":
            _require_supported_fields(
                item,
                {"id", "type", "call_id", "output"},
                "Responses function-call output item",
            )
            call_id = item.get("call_id")
            if not isinstance(call_id, str) or not call_id:
                raise UnsupportedProtocolTranslationError(
                    "unpaired_tool_call",
                    "Cannot translate a function result without a non-empty call_id.",
                )
            output = responses_function_call_output_to_chat_content(item.get("output"))
            messages.append({"role": "tool", "tool_call_id": call_id, "content": output})
            if call_id in pending_call_ids:
                pending_call_ids.remove(call_id)
                if not pending_call_ids and deferred_messages:
                    messages.extend(deferred_messages)
                    deferred_messages.clear()
                if not pending_call_ids:
                    active_tool_turn = None
            continue
        if item_type == "tool_search_call":
            _require_supported_fields(
                item,
                {"id", "type", "execution", "call_id", "status", "arguments"},
                "Responses client tool-search call input item",
            )
            if item.get("execution") != "client":
                raise UnsupportedProtocolTranslationError(
                    "unsupported_protocol_semantics",
                    "Cannot translate a non-client tool-search call to Chat Completions.",
                )
            call_id = item.get("call_id")
            if not isinstance(call_id, str) or not call_id:
                raise UnsupportedProtocolTranslationError(
                    "unpaired_tool_call",
                    "Cannot translate a tool-search call without a non-empty call_id.",
                )
            # Chat Completions has no tool-search item.  The loaded definitions
            # are carried in the next request's tools array instead.
            continue
        if item_type == "tool_search_output":
            _require_supported_fields(
                item,
                {"id", "type", "execution", "call_id", "status", "tools"},
                "Responses client tool-search output input item",
            )
            if item.get("execution") != "client":
                raise UnsupportedProtocolTranslationError(
                    "unsupported_protocol_semantics",
                    "Cannot translate a non-client tool-search output to Chat Completions.",
                )
            call_id = item.get("call_id")
            if not isinstance(call_id, str) or not call_id:
                raise UnsupportedProtocolTranslationError(
                    "unpaired_tool_call",
                    "Cannot translate a tool-search output without a non-empty call_id.",
                )
            if loaded_tools is not None:
                loaded_tools.extend(responses_tools_to_chat_tools(item.get("tools")))
            continue
        raise UnsupportedProtocolTranslationError(
            "unsupported_protocol_semantics",
            f"Cannot translate Responses input item type {item_type!r} to Chat Completions.",
        )
    if deferred_messages:
        messages.extend(deferred_messages)
    if pending_reasoning:
        # Some Responses clients replay the reasoning item after the tool
        # result and any newly-entered messages.  It still belongs to the
        # assistant turn that issued the tool call.  Prefer that turn over a
        # newer plain assistant message so thinking-mode Chat providers see
        # ``reasoning_content`` alongside the corresponding tool call.
        target: dict[str, Any] | None = None
        for candidate in reversed(messages):
            if candidate.get("role") != "assistant":
                continue
            tool_calls = candidate.get("tool_calls")
            if isinstance(tool_calls, list) and tool_calls:
                target = candidate
                break
        if target is None:
            for candidate in reversed(messages):
                if candidate.get("role") == "assistant":
                    target = candidate
                    break
        if target is None:
            flush_reasoning_message()
        else:
            attach_reasoning(target)
            # Record the association for the matching tool turn.  The actual
            # deferred messages may already have been flushed after their
            # function-call output, so they are kept by reference in the
            # per-turn record above.
            for record in tool_turn_records:
                if record.get("message") is target:
                    record["reasoning_text"] = target.get("reasoning_content")
                    break
    for record in tool_turn_records:
        reasoning_text = record.get("reasoning_text")
        if not isinstance(reasoning_text, str) or not reasoning_text:
            continue
        for candidate in record.get("deferred_assistants", []):
            if isinstance(candidate, dict) and not candidate.get("reasoning_content"):
                candidate["reasoning_content"] = reasoning_text
    return messages


def responses_tools_to_chat_tools(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise UnsupportedProtocolTranslationError(
            "unsupported_protocol_semantics",
            "Cannot translate a non-list Responses tools payload.",
        )
    tools: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict) or item.get("type") != "function":
            tool_type = item.get("type") if isinstance(item, Mapping) else type(item).__name__
            raise UnsupportedProtocolTranslationError(
                "unsupported_protocol_semantics",
                f"Cannot translate Responses tool type {tool_type!r} to Chat Completions.",
            )
        _validate_function_tool_fields(
            item,
            {"type", "name", "description", "parameters", "strict"},
            "Responses function tool",
        )
        name = item.get("name")
        if not isinstance(name, str) or not name:
            raise UnsupportedProtocolTranslationError(
                "unsupported_protocol_semantics",
                "Cannot translate a Responses function tool without a non-empty name.",
            )
        function: dict[str, Any] = {"name": name}
        description = item.get("description")
        if isinstance(description, str):
            function["description"] = description
        parameters = item.get("parameters")
        if isinstance(parameters, dict):
            function["parameters"] = parameters
        strict = item.get("strict")
        if isinstance(strict, bool):
            function["strict"] = strict
        tools.append({"type": "function", "function": function})
    return tools


def responses_tool_choice_to_chat_tool_choice(value: Any) -> Any:
    if value is None or isinstance(value, str):
        return value
    if not isinstance(value, dict) or value.get("type") != "function":
        choice_type = value.get("type") if isinstance(value, Mapping) else type(value).__name__
        raise UnsupportedProtocolTranslationError(
            "unsupported_protocol_semantics",
            f"Cannot translate Responses tool_choice type {choice_type!r} to Chat Completions.",
        )
    _require_supported_fields(value, {"type", "name"}, "Responses function tool_choice")
    name = value.get("name")
    if not isinstance(name, str) or not name:
        raise UnsupportedProtocolTranslationError(
            "unsupported_protocol_semantics",
            "Cannot translate a Responses function tool_choice without a non-empty name.",
        )
    return {"type": "function", "function": {"name": name}}


def responses_request_to_chat_completion_body(
    body: bytes,
    *,
    drop_client_metadata: bool = False,
    drop_client_transport_fields: bool = False,
    drop_reasoning: bool = False,
    preserve_reasoning_history: bool = False,
) -> bytes:
    payload = decode_protocol_json(body)
    if not isinstance(payload, dict):
        return body
    # ``client_metadata`` is Codex transport bookkeeping.  It has no
    # Chat Completions representation, and the Gateway may explicitly drop
    # it when crossing into a third-party Chat endpoint.  Keep the strict
    # default for direct converter callers so accidental semantic loss still
    # fails closed unless the route selected this documented policy.
    if drop_client_transport_fields:
        for key in ("client_metadata", "include", "prompt_cache_key", "store", "text"):
            payload.pop(key, None)
    elif drop_client_metadata:
        payload.pop("client_metadata", None)
    if drop_reasoning:
        payload.pop("reasoning", None)
    _require_supported_fields(
        payload,
        {
            "model",
            "input",
            "instructions",
            "tools",
            "tool_choice",
            "stream",
            "temperature",
            "top_p",
            "presence_penalty",
            "frequency_penalty",
            "parallel_tool_calls",
            "max_output_tokens",
            "reasoning",
            # These Responses-only transport controls are accepted only when
            # they carry their explicit no-op defaults; semantic values are
            # rejected below instead of silently dropped.
            "client_metadata",
            "include",
            "prompt_cache_key",
            "store",
            "text",
        },
        "Responses request",
    )
    _require_omittable_responses_transport_fields(payload)
    chat_reasoning_effort: str | None = None
    reasoning_control = payload.get("reasoning")
    if reasoning_control is not None:
        if not preserve_reasoning_history:
            raise UnsupportedProtocolTranslationError(
                "unsupported_protocol_semantics",
                "Cannot translate Responses reasoning controls to Chat Completions without a proven equivalent.",
            )
        if not isinstance(reasoning_control, Mapping):
            raise UnsupportedProtocolTranslationError(
                "unsupported_protocol_semantics",
                "Cannot translate a non-object Responses reasoning control to Chat Completions.",
            )
        _require_supported_fields(
            reasoning_control,
            {"effort", "summary"},
            "Responses reasoning control",
        )
        effort = reasoning_control.get("effort")
        if not isinstance(effort, str) or effort not in {
            "none", "minimal", "low", "medium", "high", "xhigh", "max",
        }:
            raise UnsupportedProtocolTranslationError(
                "unsupported_protocol_semantics",
                "Cannot translate an unknown Responses reasoning effort to Chat Completions.",
            )
        # ``summary=auto`` is Codex's default selector and does not request a
        # provider-specific summary representation.  It is therefore safe to
        # consume at this protocol seam.  Any explicit alternative (including
        # null) could change the requested output semantics and must remain
        # fail-closed rather than being silently dropped.
        if "summary" in reasoning_control and reasoning_control.get("summary") != "auto":
            raise UnsupportedProtocolTranslationError(
                "unsupported_protocol_semantics",
                "Cannot translate non-default Responses reasoning summary to Chat Completions.",
            )
        chat_reasoning_effort = effort
    if "input" in payload and not isinstance(payload["input"], (str, list)):
        raise UnsupportedProtocolTranslationError(
            "unsupported_protocol_semantics",
            "Cannot translate a present non-string, non-list Responses input payload.",
        )
    if "instructions" in payload and payload["instructions"] is not None and not isinstance(
        payload["instructions"], str
    ):
        raise UnsupportedProtocolTranslationError(
            "unsupported_protocol_semantics",
            "Cannot translate a present non-string Responses instructions payload.",
        )

    messages: list[dict[str, Any]] = []
    loaded_tools: list[dict[str, Any]] = []
    instructions = payload.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        messages.append({"role": "system", "content": instructions})
    messages.extend(
        responses_input_to_chat_messages(
            payload.get("input"),
            loaded_tools=loaded_tools,
            preserve_reasoning_history=preserve_reasoning_history,
        )
    )
    if not messages:
        messages.append({"role": "user", "content": ""})

    chat_payload: dict[str, Any] = {
        "model": payload.get("model"),
        "messages": messages,
    }
    for key in ("stream", "temperature", "top_p", "presence_penalty", "frequency_penalty", "parallel_tool_calls"):
        if key in payload:
            chat_payload[key] = payload[key]
    if payload.get("stream") is True:
        stream_options = chat_payload.get("stream_options")
        if not isinstance(stream_options, dict):
            stream_options = {}
        stream_options["include_usage"] = True
        chat_payload["stream_options"] = stream_options
    if "max_output_tokens" in payload:
        chat_payload["max_tokens"] = payload["max_output_tokens"]
    if chat_reasoning_effort is not None:
        # This field is a provider-neutral Chat Completions control accepted
        # only on the explicit reasoning-history capability route.  It is not
        # inferred from a provider name or silently added to ordinary Chat
        # requests.
        chat_payload["reasoning_effort"] = chat_reasoning_effort

    tools = responses_tools_to_chat_tools(payload.get("tools"))
    tools.extend(loaded_tools)
    if tools:
        chat_payload["tools"] = tools
    tool_choice = responses_tool_choice_to_chat_tool_choice(payload.get("tool_choice"))
    if tool_choice is not None:
        chat_payload["tool_choice"] = tool_choice

    return json.dumps(chat_payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


def chat_content_to_responses_content(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, str):
        return [{"type": "input_text", "text": value}]
    if value is None:
        return []
    if not isinstance(value, list):
        raise UnsupportedProtocolTranslationError(
            "unsupported_protocol_semantics",
            "Cannot translate a non-string, non-list Chat Completions content value.",
        )
    parts: list[dict[str, Any]] = []
    for fragment in value:
        if not isinstance(fragment, dict):
            raise UnsupportedProtocolTranslationError(
                "unsupported_protocol_semantics",
                "Cannot translate a non-object Chat Completions content part.",
            )
        if fragment.get("type") == "text" and isinstance(fragment.get("text"), str):
            _require_supported_fields(fragment, {"type", "text", "annotations"}, "Chat Completions text content part")
            annotations = fragment.get("annotations")
            if annotations not in (None, []):
                raise UnsupportedProtocolTranslationError(
                    "unsupported_protocol_semantics",
                    "Cannot translate Chat Completions text annotations to Responses without losing them.",
                )
            parts.append({"type": "input_text", "text": fragment["text"]})
        elif fragment.get("type") == "image_url" and isinstance(fragment.get("image_url"), dict):
            _require_supported_fields(fragment, {"type", "image_url"}, "Chat Completions image content part")
            _require_supported_fields(fragment["image_url"], {"url", "detail"}, "Chat Completions image URL")
            url = fragment["image_url"].get("url")
            if isinstance(url, str):
                part: dict[str, str] = {"type": "input_image", "image_url": url}
                detail = fragment["image_url"].get("detail")
                if isinstance(detail, str):
                    part["detail"] = detail
                elif detail is not None:
                    raise UnsupportedProtocolTranslationError(
                        "unsupported_protocol_semantics",
                        "Cannot translate a non-string Chat Completions image detail value.",
                    )
                parts.append(part)
                continue
            raise UnsupportedProtocolTranslationError(
                "unsupported_protocol_semantics",
                "Cannot translate a Chat Completions image without a URL.",
            )
        else:
            raise UnsupportedProtocolTranslationError(
                "unsupported_protocol_semantics",
                f"Cannot translate Chat Completions content part type {fragment.get('type')!r} to Responses.",
            )
    return parts


def chat_messages_to_responses_input(
    messages: Any,
    *,
    chat_content_text: ChatContentText = _default_chat_content_text,
) -> tuple[str | None, list[dict[str, Any]]]:
    if messages is None:
        return None, []
    if not isinstance(messages, list):
        raise UnsupportedProtocolTranslationError(
            "unsupported_protocol_semantics",
            "Cannot translate a non-list Chat Completions messages payload.",
        )

    instructions_parts: list[str] = []
    input_items: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict):
            raise UnsupportedProtocolTranslationError(
                "unsupported_protocol_semantics",
                "Cannot translate a non-object Chat Completions message.",
            )
        _require_supported_chat_message_fields(message, "Chat Completions message")
        role = message.get("role")
        if role == "system":
            content = message.get("content")
            if content is not None and not isinstance(content, str):
                raise UnsupportedProtocolTranslationError(
                    "unsupported_protocol_semantics",
                    "Cannot translate non-text Chat Completions system content to Responses instructions.",
                )
            text = content if isinstance(content, str) else ""
            if text:
                instructions_parts.append(text)
            continue
        tool_calls = message.get("tool_calls")
        if role == "assistant" and tool_calls is not None and not isinstance(tool_calls, list):
            raise UnsupportedProtocolTranslationError(
                "unsupported_protocol_semantics",
                "Cannot translate a non-list assistant tool_calls payload.",
            )
        if isinstance(tool_calls, list) and role == "assistant":
            reasoning_output = _chat_reasoning_output(message)
            if reasoning_output is not None:
                input_items.append(reasoning_output)
            content = message.get("content")
            if content is not None and not isinstance(content, str):
                raise UnsupportedProtocolTranslationError(
                    "unsupported_protocol_semantics",
                    "Cannot translate non-text assistant content alongside tool calls.",
                )
            text = content if isinstance(content, str) else ""
            if text:
                input_items.append(
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": text, "annotations": []}],
                    }
                )
            for tool_call in tool_calls:
                if not isinstance(tool_call, dict):
                    raise UnsupportedProtocolTranslationError(
                        "unsupported_protocol_semantics",
                        "Cannot translate a non-object assistant tool call.",
                    )
                _require_supported_fields(
                    tool_call,
                    {"id", "type", "function"},
                    "Chat Completions assistant tool call",
                )
                tool_type = tool_call.get("type")
                if tool_type not in (None, "function"):
                    raise UnsupportedProtocolTranslationError(
                        "unsupported_protocol_semantics",
                        f"Cannot translate assistant tool type {tool_type!r} to Responses.",
                    )
                function = tool_call.get("function")
                if not isinstance(function, dict):
                    raise UnsupportedProtocolTranslationError(
                        "unsupported_protocol_semantics",
                        "Cannot translate an assistant tool call without a function payload.",
                    )
                _require_supported_fields(
                    function,
                    {"name", "arguments"},
                    "Chat Completions assistant function call",
                )
                name = function.get("name")
                if not isinstance(name, str) or not name:
                    raise UnsupportedProtocolTranslationError(
                        "unsupported_protocol_semantics",
                        "Cannot translate an assistant tool call without a non-empty function name.",
                    )
                call_id = tool_call.get("id")
                if not isinstance(call_id, str) or not call_id:
                    raise UnsupportedProtocolTranslationError(
                        "unpaired_tool_call",
                        "Cannot translate an assistant tool call without a non-empty id.",
                    )
                arguments = _function_arguments(function, "Chat Completions function-call")
                input_items.append(
                    {
                        "type": "function_call",
                        "call_id": call_id,
                        "name": name,
                        "arguments": arguments,
                    }
                )
            continue
        if role == "tool":
            call_id = message.get("tool_call_id")
            if not isinstance(call_id, str) or not call_id:
                raise UnsupportedProtocolTranslationError(
                    "unpaired_tool_call",
                    "Cannot translate a tool result without a non-empty tool_call_id.",
                )
            content = message.get("content")
            if content is not None and not isinstance(content, str):
                raise UnsupportedProtocolTranslationError(
                    "unsupported_protocol_semantics",
                    "Cannot translate non-text Chat Completions tool result content to Responses.",
                )
            output = content if isinstance(content, str) else ""
            input_items.append(
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": output or "",
                }
            )
            continue

        if role not in {"user", "assistant"}:
            raise UnsupportedProtocolTranslationError(
                "unsupported_protocol_semantics",
                f"Cannot translate Chat Completions message role {role!r} to Responses.",
            )
        response_role = role
        reasoning_output = _chat_reasoning_output(message) if role == "assistant" else None
        if reasoning_output is not None:
            input_items.append(reasoning_output)
        content_parts = chat_content_to_responses_content(message.get("content"))
        if not content_parts:
            content_parts = [{"type": "input_text", "text": ""}]
        adjusted: list[dict[str, Any]] = []
        for part in content_parts:
            if part.get("type") == "input_text" and response_role == "assistant":
                adjusted.append({"type": "output_text", "text": part.get("text", ""), "annotations": []})
            elif part.get("type") == "output_text" and response_role == "user":
                adjusted.append({"type": "input_text", "text": part.get("text", "")})
            else:
                adjusted.append(part)
        input_items.append(
            {
                "type": "message",
                "role": response_role,
                "content": adjusted or [{"type": "input_text", "text": ""}],
            }
        )

    instructions = "\n\n".join(instructions_parts) if instructions_parts else None
    return instructions, input_items


_CHAT_NATIVE_TOOL_FIELDS = {
    "web_search": {"type", "search_context_size", "user_location", "filters"},
    "web_search_preview": {"type", "search_context_size", "user_location"},
    "file_search": {"type", "vector_store_ids", "max_num_results", "ranking_options", "filters"},
    "code_interpreter": {"type", "container"},
    "custom": {"type", "name", "description", "format"},
    "tool_search": {"type", "execution", "description", "parameters"},
    "namespace": {"type", "name", "description", "tools"},
}


def _chat_native_tool_to_responses(item: Mapping[str, Any]) -> dict[str, Any]:
    tool_type = item.get("type")
    if tool_type not in CHAT_NATIVE_TOOL_TYPES:
        raise UnsupportedProtocolTranslationError(
            "unsupported_protocol_semantics",
            f"Cannot translate Chat Completions tool type {tool_type!r} to Responses.",
        )
    for key, child in item.items():
        if isinstance(key, str) and "encrypted" in key and child not in (None, "", [], False):
            raise UnsupportedProtocolTranslationError(
                "unsupported_protocol_semantics",
                "Cannot translate encrypted Chat Completions native-tool fields to Responses.",
            )
    allowed = _CHAT_NATIVE_TOOL_FIELDS[str(tool_type)]
    _require_supported_fields(item, allowed, f"Chat Completions {tool_type} tool")
    if tool_type == "custom":
        name = item.get("name")
        if not isinstance(name, str) or not name:
            raise UnsupportedProtocolTranslationError(
                "unsupported_protocol_semantics",
                "Cannot translate a Chat Completions custom tool without a non-empty name.",
            )
        if not isinstance(item.get("format"), Mapping):
            raise UnsupportedProtocolTranslationError(
                "unsupported_protocol_semantics",
                "Cannot translate a Chat Completions custom tool without a format object.",
            )
    if tool_type == "tool_search" and item.get("execution") != "client":
        raise UnsupportedProtocolTranslationError(
            "unsupported_protocol_semantics",
            "Cannot translate a non-client Chat Completions tool_search declaration to Responses.",
        )
    if tool_type == "namespace":
        name = item.get("name")
        if not isinstance(name, str) or not name:
            raise UnsupportedProtocolTranslationError(
                "unsupported_protocol_semantics",
                "Cannot translate a Chat Completions namespace tool without a non-empty name.",
            )
        if "tools" in item and not isinstance(item.get("tools"), list):
            raise UnsupportedProtocolTranslationError(
                "unsupported_protocol_semantics",
                "Cannot translate a Chat Completions namespace tool without a tools list.",
            )
    return {key: item[key] for key in allowed if key in item}


def chat_tools_to_responses_tools(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise UnsupportedProtocolTranslationError(
            "unsupported_protocol_semantics",
            "Cannot translate a non-list Chat Completions tools payload.",
        )
    tools: list[dict[str, Any]] = []
    for item in value:
        if isinstance(item, dict) and item.get("type") in CHAT_NATIVE_TOOL_TYPES:
            tools.append(_chat_native_tool_to_responses(item))
            continue
        if not isinstance(item, dict) or item.get("type") != "function":
            tool_type = item.get("type") if isinstance(item, Mapping) else type(item).__name__
            raise UnsupportedProtocolTranslationError(
                "unsupported_protocol_semantics",
                f"Cannot translate Chat Completions tool type {tool_type!r} to Responses.",
            )
        _require_supported_fields(item, {"type", "function"}, "Chat Completions function tool")
        function = item.get("function")
        if not isinstance(function, dict):
            raise UnsupportedProtocolTranslationError(
                "unsupported_protocol_semantics",
                "Cannot translate a Chat Completions function tool without a function payload.",
            )
        _validate_function_tool_fields(
            function,
            {"name", "description", "parameters", "strict"},
            "Chat Completions function tool",
        )
        name = function.get("name")
        if not isinstance(name, str) or not name:
            raise UnsupportedProtocolTranslationError(
                "unsupported_protocol_semantics",
                "Cannot translate a Chat Completions function tool without a non-empty name.",
            )
        tool: dict[str, Any] = {"type": "function", "name": name}
        description = function.get("description")
        if isinstance(description, str):
            tool["description"] = description
        parameters = function.get("parameters")
        if isinstance(parameters, dict):
            tool["parameters"] = parameters
        strict = function.get("strict")
        if isinstance(strict, bool):
            tool["strict"] = strict
        tools.append(tool)
    return tools


def chat_tool_choice_to_responses_tool_choice(value: Any) -> Any:
    if isinstance(value, str):
        return value
    if value is None:
        return value
    if isinstance(value, dict) and value.get("type") in CHAT_OFFICIAL_HOSTED_KINDS:
        _require_supported_fields(value, {"type"}, "Chat Completions hosted tool_choice")
        return {"type": value["type"]}
    if isinstance(value, dict) and value.get("type") == "custom":
        _require_supported_fields(value, {"type", "name"}, "Chat Completions custom tool_choice")
        name = value.get("name")
        if not isinstance(name, str) or not name:
            raise UnsupportedProtocolTranslationError(
                "unsupported_protocol_semantics",
                "Cannot translate a Chat Completions custom tool_choice without a non-empty name.",
            )
        return {"type": "custom", "name": name}
    if isinstance(value, dict) and value.get("type") == "tool_search":
        _require_supported_fields(value, {"type"}, "Chat Completions tool_search tool_choice")
        return {"type": "tool_search"}
    if not isinstance(value, dict) or value.get("type") != "function":
        choice_type = value.get("type") if isinstance(value, Mapping) else type(value).__name__
        raise UnsupportedProtocolTranslationError(
            "unsupported_protocol_semantics",
            f"Cannot translate Chat Completions tool_choice type {choice_type!r} to Responses.",
        )
    _require_supported_fields(value, {"type", "function"}, "Chat Completions function tool_choice")
    function = value.get("function")
    if isinstance(function, Mapping):
        _require_supported_fields(function, {"name"}, "Chat Completions function tool_choice")
    if not isinstance(function, dict) or not isinstance(function.get("name"), str) or not function["name"]:
        raise UnsupportedProtocolTranslationError(
            "unsupported_protocol_semantics",
            "Cannot translate a Chat Completions function tool_choice without a non-empty name.",
        )
    return {"type": "function", "name": function["name"]}


def chat_completions_request_to_responses_body(
    body: bytes,
    *,
    chat_content_text: ChatContentText = _default_chat_content_text,
) -> bytes:
    payload = decode_protocol_json(body)
    if not isinstance(payload, dict):
        return body
    _require_supported_fields(
        payload,
        {
            "model",
            "messages",
            "tools",
            "tool_choice",
            "stream",
            "temperature",
            "top_p",
            "presence_penalty",
            "frequency_penalty",
            "parallel_tool_calls",
            "max_tokens",
            "max_output_tokens",
            "reasoning",
            "reasoning_effort",
            "chat_template_kwargs",
            "stream_options",
            "n",
            "prompt_cache_key",
        },
        "Chat Completions request",
    )
    template_reasoning_effort = _chat_template_reasoning_effort(payload.get("chat_template_kwargs"))
    _require_representable_stream_options(payload.get("stream_options"))
    direct_reasoning_effort = _chat_reasoning_effort(payload)
    reasoning_control_present = (
        payload.get("reasoning") is not None or payload.get("reasoning_effort") is not None
    )
    if reasoning_control_present and direct_reasoning_effort is None:
        raise UnsupportedProtocolTranslationError(
            "unsupported_protocol_semantics",
            "Cannot translate Chat Completions reasoning controls to Responses without a proven equivalent.",
        )
    if (
        direct_reasoning_effort is not None
        and template_reasoning_effort is not None
        and direct_reasoning_effort != template_reasoning_effort
    ):
        raise UnsupportedProtocolTranslationError(
            "unsupported_protocol_semantics",
            "Cannot translate conflicting Chat Completions reasoning controls to Responses without losing intent.",
        )
    effective_reasoning_effort = (
        direct_reasoning_effort if direct_reasoning_effort is not None else template_reasoning_effort
    )
    if "n" in payload and payload.get("n") not in (None, 1):
        raise UnsupportedProtocolTranslationError(
            "unsupported_protocol_semantics",
            "Cannot translate multiple Chat Completions choices to a single Responses result.",
        )
    if "messages" in payload and not isinstance(payload["messages"], list):
        raise UnsupportedProtocolTranslationError(
            "unsupported_protocol_semantics",
            "Cannot translate a present non-list Chat Completions messages payload.",
        )

    instructions, input_items = chat_messages_to_responses_input(
        payload.get("messages"),
        chat_content_text=chat_content_text,
    )
    if not input_items:
        input_items = [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": ""}]}]

    responses_payload: dict[str, Any] = {
        "model": payload.get("model"),
        "input": input_items,
    }
    if isinstance(instructions, str) and instructions.strip():
        responses_payload["instructions"] = instructions

    for key in ("stream", "temperature", "top_p", "presence_penalty", "frequency_penalty", "parallel_tool_calls"):
        if key in payload:
            responses_payload[key] = payload[key]
    if "max_tokens" in payload:
        responses_payload["max_output_tokens"] = payload["max_tokens"]
    if "max_output_tokens" in payload:
        responses_payload["max_output_tokens"] = payload["max_output_tokens"]

    tools = chat_tools_to_responses_tools(payload.get("tools"))
    if tools:
        responses_payload["tools"] = tools
    tool_choice = chat_tool_choice_to_responses_tool_choice(payload.get("tool_choice"))
    if tool_choice is not None:
        responses_payload["tool_choice"] = tool_choice
    if effective_reasoning_effort is not None:
        responses_payload["reasoning"] = {"effort": effective_reasoning_effort}
    if "prompt_cache_key" in payload:
        cache_key = payload["prompt_cache_key"]
        if cache_key is not None and not isinstance(cache_key, str):
            raise UnsupportedProtocolTranslationError(
                "unsupported_protocol_semantics",
                "Cannot translate a non-string Chat Completions prompt_cache_key.",
            )
        responses_payload["prompt_cache_key"] = cache_key

    return json.dumps(responses_payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


def _chat_template_reasoning_effort(value: Any) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, Mapping)
        or set(value.keys()) != {"reasoning_effort"}
        or not isinstance(value.get("reasoning_effort"), str)
        or not value["reasoning_effort"]
    ):
        raise UnsupportedProtocolTranslationError(
            "unsupported_protocol_semantics",
            "Cannot translate Chat Completions chat_template_kwargs fields without losing them; only chat_template_kwargs.reasoning_effort is supported.",
        )
    return value["reasoning_effort"]


def _require_representable_stream_options(value: Any) -> None:
    if value is None:
        return
    # stream_options.include_usage is representable by construction: the
    # Responses stream always carries usage and the reverse adapter re-injects
    # include_usage for streaming Chat Completions callers.
    if (
        not isinstance(value, Mapping)
        or set(value.keys()) != {"include_usage"}
        or not isinstance(value.get("include_usage"), bool)
    ):
        raise UnsupportedProtocolTranslationError(
            "unsupported_protocol_semantics",
            "Cannot translate Chat Completions stream_options fields without losing them; only stream_options.include_usage is supported.",
        )


def _chat_reasoning_effort(payload: Mapping[str, Any]) -> str | None:
    reasoning = payload.get("reasoning")
    if isinstance(reasoning, str):
        return reasoning or None
    if isinstance(reasoning, Mapping):
        if (
            set(reasoning.keys()) == {"effort"}
            and isinstance(reasoning.get("effort"), str)
            and reasoning["effort"]
        ):
            return reasoning["effort"]
        return None
    effort = payload.get("reasoning_effort")
    if isinstance(effort, str) and effort:
        return effort
    return None


def _chat_completion_message_output(
    message: Mapping[str, Any],
    index: int,
    *,
    chat_content_text: ChatContentText,
) -> dict[str, Any] | None:
    _raise_for_unsupported_chat_message_semantics(message)
    content = message.get("content")
    if isinstance(content, list):
        content_parts = chat_content_to_responses_content(content)
        if any(part.get("type") != "input_text" for part in content_parts):
            raise UnsupportedProtocolTranslationError(
                "unsupported_protocol_semantics",
                "Cannot translate non-text Chat Completions response content to Responses without losing it.",
            )
    text = content if isinstance(content, str) else chat_content_text(content)
    if not text:
        return None
    return {
        "id": f"msg_{index}",
        "type": "message",
        "status": "completed",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }


def _chat_completion_tool_outputs(
    message: Mapping[str, Any],
    *,
    chat_content_text: ChatContentText,
    xmlish_tool_outputs: XmlishToolOutputs | None,
) -> list[dict[str, Any]]:
    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, list):
        if tool_calls is not None:
            raise UnsupportedProtocolTranslationError(
                "unsupported_protocol_semantics",
                "Cannot translate a non-list Chat Completions tool_calls payload.",
            )
        content = message.get("content")
        text = content if isinstance(content, str) else chat_content_text(content)
        return xmlish_tool_outputs(text) if text and xmlish_tool_outputs is not None else []

    output: list[dict[str, Any]] = []
    for tool_call in tool_calls:
        if not isinstance(tool_call, dict):
            raise UnsupportedProtocolTranslationError(
                "unsupported_protocol_semantics",
                "Cannot translate a non-object assistant tool call.",
            )
        _require_supported_fields(
            tool_call,
            {"id", "type", "function"},
            "Chat Completions assistant tool call",
        )
        tool_type = tool_call.get("type")
        if tool_type not in (None, "function"):
            raise UnsupportedProtocolTranslationError(
                "unsupported_protocol_semantics",
                f"Cannot translate assistant tool type {tool_type!r} to Responses.",
            )
        function = tool_call.get("function")
        if not isinstance(function, dict):
            raise UnsupportedProtocolTranslationError(
                "unsupported_protocol_semantics",
                "Cannot translate an assistant tool call without a function payload.",
            )
        _require_supported_fields(
            function,
            {"name", "arguments"},
            "Chat Completions assistant function call",
        )
        name = function.get("name")
        if not isinstance(name, str) or not name:
            raise UnsupportedProtocolTranslationError(
                "unsupported_protocol_semantics",
                "Cannot translate an assistant tool call without a non-empty function name.",
            )
        call_id = tool_call.get("id")
        if not isinstance(call_id, str) or not call_id:
            raise UnsupportedProtocolTranslationError(
                "unpaired_tool_call",
                "Cannot translate an assistant tool call without a non-empty id.",
            )
        arguments = _function_arguments(function, "Chat Completions function-call")
        output.append(
            {
                "id": f"fc_{call_id}",
                "type": "function_call",
                "status": "completed",
                "call_id": call_id,
                "name": name,
                "arguments": arguments,
            }
        )
    return output


def chat_completion_to_response_body(
    body: bytes,
    *,
    repair: bool = True,
    chat_content_text: ChatContentText = _default_chat_content_text,
    xmlish_tool_outputs: XmlishToolOutputs | None = None,
    repair_response: ResponseRepair | None = None,
) -> bytes:
    payload = decode_protocol_json(body)
    if not isinstance(payload, dict):
        return body

    upstream_error = payload.get("error")
    if upstream_error is not None:
        error = dict(upstream_error) if isinstance(upstream_error, Mapping) else {"message": str(upstream_error)}
        error.setdefault("type", "upstream_error")
        return json.dumps(
            {
                "id": payload.get("id") if isinstance(payload.get("id"), str) else f"resp_{uuid.uuid4().hex[:12]}",
                "object": "response",
                "status": "failed",
                "model": payload.get("model"),
                "output": [],
                "error": error,
            },
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode("utf-8")

    if "choices" in payload and not isinstance(payload["choices"], list):
        raise UnsupportedProtocolTranslationError(
            "unsupported_protocol_semantics",
            "Cannot translate a present non-list Chat Completions choices payload.",
        )

    output: list[dict[str, Any]] = []
    incomplete_details: dict[str, str] | None = None
    choices = payload.get("choices")
    if isinstance(choices, list):
        if len(choices) > 1:
            raise UnsupportedProtocolTranslationError(
                "unsupported_protocol_semantics",
                "Cannot translate multiple Chat Completions choices to a single Responses result.",
            )
        for index, choice in enumerate(choices):
            if not isinstance(choice, dict):
                raise UnsupportedProtocolTranslationError(
                    "unsupported_protocol_semantics",
                    "Cannot translate a non-object Chat Completions response choice.",
                )
            choice_index = choice.get("index", index)
            if choice_index not in (None, 0):
                raise UnsupportedProtocolTranslationError(
                    "unsupported_protocol_semantics",
                    f"Cannot translate Chat Completions choice index {choice_index!r} to a single Responses result.",
                )
            finish_reason = choice.get("finish_reason")
            if finish_reason == "length":
                incomplete_details = {"reason": "max_output_tokens"}
            elif finish_reason not in (None, "stop", "tool_calls"):
                raise UnsupportedProtocolTranslationError(
                    "unsupported_protocol_semantics",
                    f"Cannot translate Chat Completions finish_reason {finish_reason!r} to Responses.",
                )
            message = choice.get("message")
            if not isinstance(message, dict):
                raise UnsupportedProtocolTranslationError(
                    "unsupported_protocol_semantics",
                    "Cannot translate a Chat Completions response choice without an object message.",
                )
            _require_supported_chat_message_fields(message, "Chat Completions response message")
            reasoning_output = _chat_reasoning_output(message)
            if reasoning_output is not None:
                output.append(reasoning_output)
            tool_outputs = _chat_completion_tool_outputs(
                message,
                chat_content_text=chat_content_text,
                xmlish_tool_outputs=xmlish_tool_outputs,
            )
            if tool_outputs and not isinstance(message.get("tool_calls"), list):
                # Gateway compatibility: XML-ish tool markup represents the
                # tool call itself, not assistant text to relay separately.
                output.extend(tool_outputs)
                continue
            message_output = _chat_completion_message_output(
                message,
                index,
                chat_content_text=chat_content_text,
            )
            if message_output is not None:
                output.append(message_output)
            output.extend(tool_outputs)

    response_payload: dict[str, Any] = {
        "id": payload.get("id") if isinstance(payload.get("id"), str) else f"resp_{uuid.uuid4().hex[:12]}",
        "object": "response",
        "status": "incomplete" if incomplete_details is not None else "completed",
        "model": payload.get("model"),
        "output": output,
    }
    if incomplete_details is not None:
        response_payload["incomplete_details"] = incomplete_details
    usage = payload.get("usage")
    if isinstance(usage, Mapping):
        response_payload["usage"] = _chat_completion_usage_to_responses_usage(usage)

    if repair and repair_response is not None:
        response_payload = repair_response(response_payload)
    return json.dumps(response_payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


def chat_completion_error_body(payload: Mapping[str, Any]) -> bytes:
    error = payload.get("error")
    if isinstance(error, Mapping):
        normalized_error = dict(error)
        if not isinstance(normalized_error.get("message"), str):
            normalized_error["message"] = json.dumps(error, ensure_ascii=True, separators=(",", ":"))
        normalized_error.setdefault("type", "upstream_error")
        normalized_error.setdefault("code", payload.get("code"))
    else:
        error_type = payload.get("type") if isinstance(payload.get("type"), str) else "upstream_error"
        detail = payload.get("detail")
        message = error if isinstance(error, str) and error else detail or "Upstream request failed"
        if error_type == "upstream_stream_error" and isinstance(detail, str) and detail:
            message = detail
        normalized_error = {
            "message": message,
            "type": error_type,
            "code": payload.get("code") or (error if error_type == "upstream_stream_error" else None),
        }
    if isinstance(payload.get("status"), int):
        normalized_error.setdefault("status", payload.get("status"))
    if isinstance(payload.get("upstream"), str):
        normalized_error.setdefault("upstream", payload.get("upstream"))
    return json.dumps({"error": normalized_error}, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


def response_body_to_chat_completion_body(
    body: bytes,
    *,
    function_name_from_response_item: FunctionNameFromResponseItem = _default_function_name_from_response_item,
    error_body: Callable[[Mapping[str, Any]], bytes] = chat_completion_error_body,
    preserve_reasoning_history: bool = False,
) -> bytes:
    payload = decode_protocol_json(body)
    if not isinstance(payload, dict):
        return body
    output = payload.get("output")
    has_error_signal = (
        payload.get("error") is not None
        or isinstance(payload.get("detail"), str)
        or payload.get("status") in {"failed", "incomplete"}
    )
    if has_error_signal:
        return error_body(payload)
    if "output" in payload and not isinstance(output, list):
        raise UnsupportedProtocolTranslationError(
            "unsupported_protocol_semantics",
            "Cannot translate a present non-list Responses output payload.",
        )

    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    reasoning_parts: list[str] = []
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                raise UnsupportedProtocolTranslationError(
                    "unsupported_protocol_semantics",
                    "Cannot translate a non-object Responses output item.",
                )
            item = dict(_collapse_hosted_output_item_for_chat(item))
            if item.get("type") == "reasoning":
                if not preserve_reasoning_history:
                    raise UnsupportedProtocolTranslationError(
                        "unsupported_protocol_semantics",
                        "Responses reasoning output requires an explicit Chat capability.",
                    )
                _require_supported_fields(
                    item,
                    {"id", "type", "status", "summary", "content", "encrypted_content"},
                    "Responses output reasoning item",
                )
                portable = _portable_output_reasoning_text(item, drop_encrypted=True)
                if portable:
                    reasoning_parts.append(portable)
            elif item.get("type") == "message":
                _require_supported_fields(
                    item,
                    {"id", "type", "status", "role", "content", "phase"},
                    "Responses output message item",
                )
                content = item.get("content")
                role = item.get("role")
                if role not in (None, "assistant"):
                    raise UnsupportedProtocolTranslationError(
                        "unsupported_protocol_semantics",
                        f"Cannot translate Responses output message role {role!r} to Chat Completions.",
                    )
                if isinstance(content, list):
                    responses_content_to_chat_content(content)
                    for part in content:
                        if isinstance(part, dict) and part.get("type") in ("output_text", "text"):
                            text = part.get("text")
                            if isinstance(text, str):
                                text_parts.append(text)
                elif isinstance(content, str):
                    text_parts.append(content)
                elif content is not None:
                    raise UnsupportedProtocolTranslationError(
                        "unsupported_protocol_semantics",
                        "Cannot translate a non-text Responses output message content value.",
                    )
            elif item.get("type") == "function_call":
                _require_supported_fields(
                    item,
                    {"id", "type", "status", "call_id", "namespace", "name", "arguments"},
                    "Responses output function-call item",
                )
                call_id = item.get("call_id")
                if not isinstance(call_id, str) or not call_id:
                    raise UnsupportedProtocolTranslationError(
                        "unpaired_tool_call",
                        "Cannot translate a function call without a non-empty call_id.",
                    )
                name = function_name_from_response_item(item)
                arguments = _function_arguments(item, "Responses function-call")
                if not isinstance(name, str) or not name:
                    raise UnsupportedProtocolTranslationError(
                        "unsupported_protocol_semantics",
                        "Cannot translate a function call without a non-empty name.",
                    )
                tool_calls.append(
                    {
                        "id": call_id,
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": arguments,
                        },
                    }
                )
            else:
                raise UnsupportedProtocolTranslationError(
                    "unsupported_protocol_semantics",
                    f"Cannot translate Responses output item type {item.get('type')!r} to Chat Completions.",
                )

    if not text_parts and not tool_calls and not reasoning_parts:
        had_reasoning = any(
            isinstance(item, dict) and item.get("type") == "reasoning"
            for item in (output if isinstance(output, list) else [])
        )
        if had_reasoning:
            raise UnsupportedProtocolTranslationError(
                "unsupported_protocol_semantics",
                "Cannot translate Responses output to Chat Completions without portable text, tools, or reasoning.",
            )

    message: dict[str, Any] = {"role": "assistant", "content": "".join(text_parts) or None}
    if tool_calls:
        message["tool_calls"] = tool_calls
        if not message["content"]:
            message["content"] = None
    if reasoning_parts:
        message["reasoning_content"] = "\n".join(dict.fromkeys(reasoning_parts))

    choice: dict[str, Any] = {
        "index": 0,
        "message": message,
        "finish_reason": "tool_calls" if tool_calls else "stop",
    }
    chat_payload: dict[str, Any] = {
        "id": payload.get("id") if isinstance(payload.get("id"), str) else f"chatcmpl_{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": payload.get("model"),
        "choices": [choice],
    }
    usage = payload.get("usage")
    if isinstance(usage, dict):
        chat_payload["usage"] = _responses_usage_to_chat_usage(usage)
    return json.dumps(chat_payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


def chat_completion_body_to_stream_chunks(body: bytes) -> list[dict[str, Any]]:
    payload = decode_protocol_json(body)
    if not isinstance(payload, dict):
        return []
    if "choices" in payload and not isinstance(payload["choices"], list):
        raise UnsupportedProtocolTranslationError(
            "unsupported_protocol_semantics",
            "Cannot synthesize Chat Completions chunks from a present non-list choices payload.",
        )

    response_id = payload.get("id") if isinstance(payload.get("id"), str) else f"chatcmpl_{uuid.uuid4().hex[:12]}"
    model = payload.get("model")
    chunks: list[dict[str, Any]] = [
        {
            "id": response_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
        }
    ]
    choices = payload.get("choices")
    if not isinstance(choices, list):
        return chunks

    for fallback_index, choice in enumerate(choices):
        if not isinstance(choice, Mapping):
            raise UnsupportedProtocolTranslationError(
                "unsupported_protocol_semantics",
                "Cannot synthesize Chat Completions chunks for a non-object response choice.",
            )
        index = choice.get("index")
        index = index if isinstance(index, int) else fallback_index
        message = choice.get("message")
        if not isinstance(message, Mapping):
            raise UnsupportedProtocolTranslationError(
                "unsupported_protocol_semantics",
                "Cannot synthesize Chat Completions chunks for a response choice without an object message.",
            )
        _require_supported_chat_message_fields(message, "Chat Completions response message")
        reasoning_output = _chat_reasoning_output(message)
        if reasoning_output is not None:
            for summary in reasoning_output["summary"]:
                chunks.append(
                    {
                        "id": response_id,
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": model,
                        "choices": [
                            {
                                "index": index,
                                "delta": {"reasoning_content": summary["text"]},
                                "finish_reason": None,
                            }
                        ],
                    }
                )
        content = message.get("content")
        if isinstance(content, str) and content:
            chunks.append(
                {
                    "id": response_id,
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [{"index": index, "delta": {"content": content}, "finish_reason": None}],
                }
            )
        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list):
            for fallback_tool_index, tool_call in enumerate(tool_calls):
                if not isinstance(tool_call, Mapping):
                    raise UnsupportedProtocolTranslationError(
                        "unsupported_protocol_semantics",
                        "Cannot translate a non-object assistant tool call into Chat Completions chunks.",
                    )
                _require_supported_fields(
                    tool_call,
                    {"id", "type", "function", "index"},
                    "Chat Completions assistant tool call",
                )
                tool_type = tool_call.get("type")
                if tool_type not in (None, "function"):
                    raise UnsupportedProtocolTranslationError(
                        "unsupported_protocol_semantics",
                        f"Cannot translate assistant tool type {tool_type!r} into Chat Completions chunks.",
                    )
                function = tool_call.get("function")
                if not isinstance(function, Mapping):
                    raise UnsupportedProtocolTranslationError(
                        "unsupported_protocol_semantics",
                        "Cannot translate an assistant tool call without a function payload into Chat Completions chunks.",
                    )
                _require_supported_fields(
                    function,
                    {"name", "arguments"},
                    "Chat Completions assistant function call",
                )
                name = function.get("name")
                if not isinstance(name, str) or not name:
                    raise UnsupportedProtocolTranslationError(
                        "unsupported_protocol_semantics",
                        "Cannot translate an assistant tool call without a non-empty function name into Chat Completions chunks.",
                    )
                tool_index = tool_call.get("index")
                tool_index = tool_index if isinstance(tool_index, int) else fallback_tool_index
                call_id = tool_call.get("id")
                if not isinstance(call_id, str) or not call_id:
                    raise UnsupportedProtocolTranslationError(
                        "unpaired_tool_call",
                        "Cannot translate an assistant tool call without a non-empty id into Chat Completions chunks.",
                    )
                arguments = _function_arguments(function, "Chat Completions function-call")
                chunks.append(
                    {
                        "id": response_id,
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": model,
                        "choices": [
                            {
                                "index": index,
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": tool_index,
                                            "id": call_id,
                                            "type": "function",
                                            "function": {"name": name, "arguments": arguments},
                                        }
                                    ]
                                },
                                "finish_reason": None,
                            }
                        ],
                    }
                )
        finish_reason = choice.get("finish_reason")
        if not isinstance(finish_reason, str):
            finish_reason = "tool_calls" if isinstance(tool_calls, list) and tool_calls else "stop"
        chunks.append(
            {
                "id": response_id,
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [{"index": index, "delta": {}, "finish_reason": finish_reason}],
            }
        )
    return chunks


def _identity_function_name(name: str) -> str:
    return name


def _validate_chat_stream_choices(chunk: Mapping[str, Any]) -> None:
    choices = chunk.get("choices")
    if choices is None and "choices" not in chunk:
        return
    if not isinstance(choices, list):
        raise UnsupportedProtocolTranslationError(
            "unsupported_protocol_semantics",
            "Cannot translate a non-list Chat Completions stream choices payload.",
        )
    if not choices:
        return
    if len(choices) != 1:
        raise UnsupportedProtocolTranslationError(
            "unsupported_protocol_semantics",
            "Cannot translate multiple Chat Completions stream choices to one Responses stream.",
        )
    choice = choices[0]
    if not isinstance(choice, Mapping):
        raise UnsupportedProtocolTranslationError(
            "unsupported_protocol_semantics",
            "Cannot translate a non-object Chat Completions stream choice.",
        )
    choice_index = choice.get("index", 0)
    if choice_index != 0:
        raise UnsupportedProtocolTranslationError(
            "unsupported_protocol_semantics",
            f"Cannot translate Chat Completions stream choice index {choice_index!r} to one Responses stream.",
        )


def _is_chat_stream_usage_only_chunk(chunk: Mapping[str, Any]) -> bool:
    return chunk.get("choices") == [] and "usage" in chunk


def _validate_chat_stream_terminal_order(chunks: list[Mapping[str, Any] | str]) -> None:
    terminal_seen = False
    for chunk in chunks:
        if chunk == "[DONE]":
            terminal_seen = True
            continue
        if not isinstance(chunk, Mapping):
            continue
        _validate_chat_stream_choices(chunk)
        if terminal_seen:
            if _is_chat_stream_usage_only_chunk(chunk):
                continue
            raise UnsupportedProtocolTranslationError(
                "unsupported_protocol_semantics",
                "Cannot translate Chat Completions stream semantics after a terminal chunk.",
            )
        choices = chunk.get("choices")
        if not isinstance(choices, list) or not choices:
            continue
        if choices[0].get("finish_reason") is not None:
            terminal_seen = True


def _validate_chat_stream_source(source: Mapping[str, Any]) -> None:
    _require_supported_fields(
        source,
        {
            "role",
            "content",
            "tool_calls",
            "refusal",
            "audio",
            "annotations",
            "reasoning",
            "reasoning_content",
            "reasoning_details",
        },
        "Chat Completions stream delta",
    )
    _raise_for_unsupported_chat_message_semantics(source)


def _chat_stream_source(choice: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Select the semantic source from a Chat stream choice.

    OpenAI-compatible providers normally send either ``delta`` or
    ``message``.  A few send both, with an empty delta as a framing marker and
    the actual ``reasoning_content`` on the message object.  Treating the
    empty delta as authoritative silently drops the thinking trace and makes
    thinking-mode tool continuations invalid.  Prefer a non-empty delta while
    carrying over any missing reasoning fields from the companion message;
    when the delta is empty, use the message as the source.
    """

    delta = choice.get("delta")
    message = choice.get("message")
    if isinstance(delta, Mapping):
        if not delta and isinstance(message, Mapping):
            return message
        if isinstance(message, Mapping):
            merged = dict(delta)
            for field in ("reasoning", "reasoning_content", "reasoning_details"):
                value = merged.get(field)
                if value in (None, "", [], {}) and field in message:
                    merged[field] = message[field]
            return merged
        return delta
    if isinstance(message, Mapping):
        return message
    return None


def chat_stream_chunks_to_response_events(
    chunks: list[Mapping[str, Any] | str],
    *,
    normalize_function_name: NormalizeChatFunctionName = _identity_function_name,
    xmlish_tool_outputs: XmlishToolOutputs | None = None,
) -> list[dict[str, Any]]:
    """Translate Chat Completions chunks into Responses SSE events."""
    states: dict[int, dict[str, Any]] = {}
    events: list[dict[str, Any]] = []
    text_parts: list[str] = []
    finished = False
    incomplete_details: dict[str, str] | None = None
    response_id = f"resp_{uuid.uuid4().hex[:12]}"
    model: str | None = None
    usage: dict[str, Any] | None = None
    next_output_index = 0
    message_output_index: int | None = None
    reasoning_output_index: int | None = None
    reasoning_parts: list[str] = []

    _validate_chat_stream_terminal_order(chunks)

    for chunk in chunks:
        if not isinstance(chunk, Mapping):
            continue
        chunk_model = chunk.get("model")
        if isinstance(chunk_model, str) and chunk_model:
            model = chunk_model
            break

    created_response: dict[str, Any] = {
        "id": response_id,
        "object": "response",
        "status": "in_progress",
        "output": [],
    }
    if model:
        created_response["model"] = model
    events.append({"type": "response.created", "response": created_response})

    def allocate_output_index() -> int:
        nonlocal next_output_index
        output_index = next_output_index
        next_output_index += 1
        return output_index

    def state_for(index: int) -> dict[str, Any]:
        if index not in states:
            states[index] = {
                "output_index": allocate_output_index(),
                "item_id": "",
                "call_id": "",
                "name": "",
                "arguments": [],
                "added": False,
            }
        return states[index]

    def reserve_reasoning_prefix() -> None:
        """Give reasoning the prefix output index even if its delta arrived late.

        A Chat provider is allowed to stream thinking after a tool-call delta.
        Responses history nevertheless represents thinking as the prefix of
        that assistant turn.  Re-index already-created output items/events so
        the terminal body and SSE event stream agree on that order.
        """
        nonlocal next_output_index, reasoning_output_index, message_output_index
        if reasoning_output_index is not None:
            return
        if next_output_index == 0:
            next_output_index = 1
        else:
            for state in states.values():
                state["output_index"] += 1
            if message_output_index is not None:
                message_output_index += 1
            for event in events:
                output_index = event.get("output_index") if isinstance(event, Mapping) else None
                if isinstance(output_index, int):
                    event["output_index"] = output_index + 1
            next_output_index += 1
        reasoning_output_index = 0

    def maybe_emit_added(state: dict[str, Any]) -> None:
        if state["added"] or not state["call_id"] or not state["name"]:
            return
        state["item_id"] = f"fc_{state['call_id']}"
        events.append(
            {
                "type": "response.output_item.added",
                "output_index": state["output_index"],
                "item": {
                    "id": state["item_id"],
                    "type": "function_call",
                    "status": "in_progress",
                    "call_id": state["call_id"],
                    "name": state["name"],
                    "arguments": "",
                },
            }
        )
        state["added"] = True

    for chunk in chunks:
        if chunk == "[DONE]":
            finished = True
            continue
        if not isinstance(chunk, Mapping):
            continue
        choices = chunk.get("choices")
        if not isinstance(choices, list):
            continue
        chunk_usage = chunk.get("usage")
        if isinstance(chunk_usage, Mapping):
            usage = _chat_completion_usage_to_responses_usage(chunk_usage)
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            finish_reason = choice.get("finish_reason")
            if finish_reason is not None:
                finished = True
                if finish_reason == "length":
                    incomplete_details = {"reason": "max_output_tokens"}
                elif finish_reason not in {"stop", "tool_calls"}:
                    raise UnsupportedProtocolTranslationError(
                        "unsupported_protocol_semantics",
                        f"Cannot translate Chat Completions finish_reason {finish_reason!r} to Responses stream events.",
                    )
            source = _chat_stream_source(choice)
            if not isinstance(source, dict):
                continue
            _validate_chat_stream_source(source)
            reasoning_output = _chat_reasoning_output(source)
            if reasoning_output is not None:
                reasoning_text = reasoning_output["summary"][0]["text"]
                if reasoning_text not in reasoning_parts:
                    reasoning_parts.append(reasoning_text)
                reserve_reasoning_prefix()
            content = source.get("content")
            if isinstance(content, str):
                if content:
                    if message_output_index is None:
                        message_output_index = allocate_output_index()
                    text_parts.append(content)
            elif content is not None:
                raise UnsupportedProtocolTranslationError(
                    "unsupported_protocol_semantics",
                    "Cannot translate non-text Chat Completions stream content to Responses without losing it.",
                )
            tool_calls = source.get("tool_calls")
            if tool_calls is None:
                continue
            if not isinstance(tool_calls, list):
                raise UnsupportedProtocolTranslationError(
                    "unsupported_protocol_semantics",
                    "Cannot translate a non-list Chat Completions stream tool_calls payload.",
                )
            for fallback_index, tool_call in enumerate(tool_calls):
                if not isinstance(tool_call, dict):
                    raise UnsupportedProtocolTranslationError(
                        "unsupported_protocol_semantics",
                        "Cannot translate a non-object assistant tool-call stream delta.",
                    )
                _require_supported_fields(
                    tool_call,
                    {"index", "id", "type", "function"},
                    "Chat Completions assistant tool-call stream delta",
                )
                tool_type = tool_call.get("type")
                if tool_type not in (None, "function"):
                    raise UnsupportedProtocolTranslationError(
                        "unsupported_protocol_semantics",
                        f"Cannot translate assistant tool type {tool_type!r} to Responses stream events.",
                    )
                raw_index = tool_call.get("index", fallback_index)
                if not isinstance(raw_index, int):
                    raise UnsupportedProtocolTranslationError(
                        "unsupported_protocol_semantics",
                        "Cannot translate a non-integer assistant tool-call stream index.",
                    )
                index = raw_index
                state = state_for(index)
                call_id = tool_call.get("id")
                if call_id is not None:
                    if not isinstance(call_id, str):
                        raise UnsupportedProtocolTranslationError(
                            "unpaired_tool_call",
                            "Cannot translate a tool-call stream delta with an invalid id.",
                        )
                    if call_id:
                        if state["call_id"] and state["call_id"] != call_id:
                            raise UnsupportedProtocolTranslationError(
                                "unpaired_tool_call",
                                "Cannot translate conflicting tool-call ids for one stream index.",
                            )
                        state["call_id"] = call_id

                function = tool_call.get("function")
                if function is not None and not isinstance(function, dict):
                    raise UnsupportedProtocolTranslationError(
                        "unsupported_protocol_semantics",
                        "Cannot translate a non-object assistant function-call stream delta.",
                    )
                if isinstance(function, dict):
                    _require_supported_fields(
                        function,
                        {"name", "arguments"},
                        "Chat Completions assistant function-call stream delta",
                    )
                    name = function.get("name")
                    if name is not None:
                        if not isinstance(name, str) or not name:
                            raise UnsupportedProtocolTranslationError(
                                "unsupported_protocol_semantics",
                                "Cannot translate an invalid assistant function name in a stream delta.",
                            )
                        normalized_name = normalize_function_name(name)
                        if state["name"] and state["name"] != normalized_name:
                            raise UnsupportedProtocolTranslationError(
                                "unsupported_protocol_semantics",
                                "Cannot translate conflicting function names for one stream index.",
                            )
                        state["name"] = normalized_name
                    arguments = _function_arguments(function, "Chat Completions function-call stream")
                    if arguments:
                        state["arguments"].append(arguments)

                maybe_emit_added(state)
                if state["added"] and isinstance(function, dict):
                    arguments = _function_arguments(function, "Chat Completions function-call stream")
                    if arguments:
                        events.append(
                            {
                                "type": "response.function_call_arguments.delta",
                                "item_id": state["item_id"],
                                "output_index": state["output_index"],
                                "delta": arguments,
                            }
                        )

    if not finished:
        return events

    output_by_index: dict[int, dict[str, Any]] = {}
    text = "".join(text_parts)
    extracted_xmlish_tool_outputs = xmlish_tool_outputs(text) if text and xmlish_tool_outputs is not None else []
    for state in sorted(states.values(), key=lambda item: item["output_index"]):
        if not state["call_id"]:
            raise UnsupportedProtocolTranslationError(
                "unpaired_tool_call",
                "Cannot translate a terminal assistant tool call without a non-empty id.",
            )
        if not state["name"]:
            raise UnsupportedProtocolTranslationError(
                "unsupported_protocol_semantics",
                "Cannot translate a terminal assistant tool call without a non-empty function name.",
            )
        maybe_emit_added(state)
        arguments = "".join(state["arguments"])
        item = {
            "id": state["item_id"],
            "type": "function_call",
            "status": "completed",
            "call_id": state["call_id"],
            "name": state["name"],
            "arguments": arguments,
        }
        events.append(
            {
                "type": "response.function_call_arguments.done",
                "item_id": state["item_id"],
                "output_index": state["output_index"],
                "arguments": arguments,
            }
        )
        events.append({"type": "response.output_item.done", "output_index": state["output_index"], "item": item})
        output_by_index[state["output_index"]] = item

    if reasoning_parts:
        output_index = reasoning_output_index if reasoning_output_index is not None else allocate_output_index()
        reasoning_item = {
            "id": f"rs_{uuid.uuid4().hex[:12]}",
            "type": "reasoning",
            "status": "completed",
            "summary": [
                {"type": "summary_text", "text": text}
                for text in reasoning_parts
            ],
        }
        reasoning_events = [
            {
                "type": "response.output_item.added",
                "output_index": output_index,
                "item": {**reasoning_item, "status": "in_progress"},
            },
            {
                "type": "response.output_item.done",
                "output_index": output_index,
                "item": reasoning_item,
            },
        ]
        # Reasoning is the assistant turn's prefix.  Insert it immediately
        # after response.created so tool-call/output events retain the
        # Responses ordering required by the next request.
        events[1:1] = reasoning_events
        output_by_index[output_index] = reasoning_item

    if extracted_xmlish_tool_outputs and not output_by_index:
        for output_index, item in enumerate(extracted_xmlish_tool_outputs):
            in_progress_item = dict(item)
            in_progress_item["status"] = "in_progress"
            in_progress_item["arguments"] = ""
            events.append(
                {
                    "type": "response.output_item.added",
                    "output_index": output_index,
                    "item": in_progress_item,
                }
            )
            events.append(
                {
                    "type": "response.function_call_arguments.done",
                    "item_id": item["id"],
                    "output_index": output_index,
                    "arguments": item["arguments"],
                }
            )
            events.append({"type": "response.output_item.done", "output_index": output_index, "item": item})
            output_by_index[output_index] = item
    elif text:
        output_index = message_output_index if message_output_index is not None else allocate_output_index()
        item_id = f"msg_{uuid.uuid4().hex[:12]}"
        item = {
            "id": item_id,
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        }
        events.append(
            {
                "type": "response.output_item.added",
                "output_index": output_index,
                "item": {
                    "id": item_id,
                    "type": "message",
                    "status": "in_progress",
                    "role": "assistant",
                    "content": [],
                },
            }
        )
        for part in text_parts:
            events.append(
                {
                    "type": "response.output_text.delta",
                    "item_id": item_id,
                    "output_index": output_index,
                    "content_index": 0,
                    "delta": part,
                }
            )
        events.append(
            {
                "type": "response.output_text.done",
                "item_id": item_id,
                "output_index": output_index,
                "content_index": 0,
                "text": text,
            }
        )
        events.append({"type": "response.output_item.done", "output_index": output_index, "item": item})
        output_by_index[output_index] = item

    output = [item for _, item in sorted(output_by_index.items(), key=lambda pair: pair[0])]

    completed_response: dict[str, Any] = {
        "id": response_id,
        "object": "response",
        "status": "incomplete" if incomplete_details is not None else "completed",
        "output": output,
    }
    if incomplete_details is not None:
        completed_response["incomplete_details"] = incomplete_details
    if model:
        completed_response["model"] = model
    if usage is not None:
        completed_response["usage"] = usage
    events.append(
        {
            "type": "response.incomplete" if incomplete_details is not None else "response.completed",
            "response": completed_response,
        }
    )
    return events


def responses_events_have_completed(events: list[Mapping[str, Any]]) -> bool:
    return any(isinstance(event, Mapping) and event.get("type") == "response.completed" for event in events)


def _collapse_hosted_output_item_for_chat(item: Mapping[str, Any]) -> Mapping[str, Any]:
    try:
        collapsed = collapse_hosted_item_for_chat(item)
    except ToolCompatibilityError as exc:
        raise UnsupportedProtocolTranslationError(exc.code, str(exc)) from exc
    return collapsed if collapsed is not None else item


def _validated_responses_stream_output_item(
    item: Any,
    *,
    function_name_from_response_item: FunctionNameFromResponseItem = _default_function_name_from_response_item,
) -> tuple[str, str | None, str | None, str | None]:
    if not isinstance(item, Mapping):
        raise UnsupportedProtocolTranslationError(
            "unsupported_protocol_semantics",
            "Cannot translate a non-object Responses stream output item.",
        )
    item = _collapse_hosted_output_item_for_chat(item)
    item_type = item.get("type")
    if item_type == "message":
        _require_supported_fields(
            item,
            {"id", "type", "status", "role", "content", "phase"},
            "Responses stream message item",
        )
        if item.get("content") is not None:
            responses_content_to_chat_content(item.get("content"))
        return "message", None, None, None
    if item_type != "function_call":
        raise UnsupportedProtocolTranslationError(
            "unsupported_protocol_semantics",
            f"Cannot translate Responses stream output item type {item_type!r} to Chat Completions.",
        )
    _require_supported_fields(
        item,
        {"id", "type", "status", "call_id", "namespace", "name", "arguments"},
        "Responses stream function-call item",
    )
    call_id = item.get("call_id")
    if not isinstance(call_id, str) or not call_id:
        raise UnsupportedProtocolTranslationError(
            "unpaired_tool_call",
            "Cannot translate a function-call stream item without a non-empty call_id.",
        )
    name = function_name_from_response_item(item)
    if not isinstance(name, str) or not name:
        raise UnsupportedProtocolTranslationError(
            "unsupported_protocol_semantics",
            "Cannot translate a function-call stream item without a non-empty name.",
        )
    arguments = _function_arguments(item, "Responses function-call stream")
    return "function_call", call_id, name, arguments


def _function_argument_suffix(current: str, final: str, label: str) -> str:
    if final == current:
        return ""
    if final.startswith(current):
        return final[len(current) :]
    raise UnsupportedProtocolTranslationError(
        "unsupported_protocol_semantics",
        f"Cannot translate disagreeing {label} argument deltas and final value.",
    )


_POST_TERMINAL_SEMANTIC_EVENT_TYPES = frozenset(
    {
        "response.output_text.delta",
        "response.reasoning_summary_text.delta",
        "response.function_call_arguments.delta",
        "response.output_item.added",
        "response.failed",
        "response.incomplete",
        "error",
    }
)


def _validate_responses_stream_terminal_order(events: list[Mapping[str, Any] | str]) -> None:
    terminal_seen = False
    for event in events:
        if event == "[DONE]":
            terminal_seen = True
            continue
        if not isinstance(event, Mapping):
            continue
        event_type = event.get("type")
        if terminal_seen:
            if event_type in _POST_TERMINAL_SEMANTIC_EVENT_TYPES:
                raise UnsupportedProtocolTranslationError(
                    "unsupported_protocol_semantics",
                    "Cannot translate Responses stream semantics after a terminal event.",
                )
            continue
        if event_type in {"response.completed", "response.failed", "response.incomplete", "error"}:
            terminal_seen = True


def response_events_to_chat_stream_chunks(
    events: list[Mapping[str, Any] | str],
    *,
    require_completed: bool = False,
    function_name_from_response_item: FunctionNameFromResponseItem = _default_function_name_from_response_item,
    preserve_reasoning_history: bool = False,
) -> list[dict[str, Any]]:
    _validate_responses_stream_terminal_order(events)
    if require_completed and not responses_events_have_completed(events):
        raise UpstreamStreamIncompleteError("Responses stream ended before response.completed")

    chunks: list[dict[str, Any]] = []
    tool_states: dict[str, dict[str, Any]] = {}
    model: str | None = None
    response_id: str | None = None
    finish_reason: str | None = None
    reasoning_texts_emitted: set[str] = set()

    def tool_state(item_id: str) -> dict[str, Any]:
        if item_id not in tool_states:
            index = len(tool_states)
            tool_states[item_id] = {
                "index": index,
                "id": "",
                "name": "",
                "arguments": "",
                "emitted_header": False,
            }
        return tool_states[item_id]

    def append_final_arguments(state: dict[str, Any], final_arguments: str, label: str) -> None:
        if not state["emitted_header"]:
            raise UnsupportedProtocolTranslationError(
                "unpaired_tool_call",
                f"Cannot translate {label} arguments without a paired function-call stream item.",
            )
        suffix = _function_argument_suffix(state["arguments"], final_arguments, label)
        if not suffix:
            return
        chunks.append(
            {
                "id": response_id or f"chatcmpl_{uuid.uuid4().hex[:12]}",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": state["index"],
                                    "function": {"arguments": suffix},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ],
            }
        )
        state["arguments"] += suffix

    for event in events:
        if not isinstance(event, Mapping):
            continue
        event_type = event.get("type")
        if event_type in {"response.failed", "response.incomplete", "error"}:
            raise UnsupportedProtocolTranslationError(
                "upstream_response_failed",
                f"Cannot translate terminal Responses stream event {event_type!r} as a successful Chat Completions stream.",
            )
        if event_type == "response.created":
            response_obj = event.get("response")
            if isinstance(response_obj, Mapping):
                response_id = response_obj.get("id") or response_id
                model = response_obj.get("model") or model
            chunks.append(
                {
                    "id": response_id or f"chatcmpl_{uuid.uuid4().hex[:12]}",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": model,
                    "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
                }
            )
            continue
        if event_type == "response.output_text.delta":
            delta_text = event.get("delta")
            if isinstance(delta_text, str) and delta_text:
                chunks.append(
                    {
                        "id": response_id or f"chatcmpl_{uuid.uuid4().hex[:12]}",
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": model,
                        "choices": [{"index": 0, "delta": {"content": delta_text}, "finish_reason": None}],
                    }
                )
            continue
        if event_type in {"response.content_part.added", "response.content_part.done"}:
            part = event.get("part")
            if not isinstance(part, Mapping):
                raise UnsupportedProtocolTranslationError(
                    "unsupported_protocol_semantics",
                    "Cannot translate a Responses stream content-part event without an object part.",
                )
            responses_content_to_chat_content([part])
            continue
        if event_type == "response.output_item.added":
            item = event.get("item")
            if isinstance(item, Mapping) and item.get("type") == "reasoning":
                if not preserve_reasoning_history:
                    raise UnsupportedProtocolTranslationError(
                        "unsupported_protocol_semantics",
                        "Responses reasoning output requires an explicit Chat capability.",
                    )
                _require_supported_fields(
                    item,
                    {"id", "type", "status", "summary", "content", "encrypted_content"},
                    "Responses stream reasoning item",
                )
                continue
            item_type, call_id, name, arguments = _validated_responses_stream_output_item(
                item,
                function_name_from_response_item=function_name_from_response_item,
            )
            if item_type == "message":
                continue
            item_id = item.get("id") or item.get("call_id") or ""
            state = tool_state(str(item_id))
            if state["id"] and state["id"] != call_id:
                raise UnsupportedProtocolTranslationError(
                    "unpaired_tool_call",
                    "Cannot translate conflicting function-call ids for one Responses stream item.",
                )
            if state["name"] and state["name"] != name:
                raise UnsupportedProtocolTranslationError(
                    "unsupported_protocol_semantics",
                    "Cannot translate conflicting function names for one Responses stream item.",
                )
            state["id"] = call_id
            state["name"] = name
            if state["id"] and state["name"] and not state["emitted_header"]:
                chunks.append(
                    {
                        "id": response_id or f"chatcmpl_{uuid.uuid4().hex[:12]}",
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {
                                    "tool_calls": [
                                        {
                                            "index": state["index"],
                                            "id": state["id"],
                                            "type": "function",
                                            "function": {"name": state["name"], "arguments": ""},
                                        }
                                    ]
                                },
                                "finish_reason": None,
                            }
                        ],
                    }
                )
                state["emitted_header"] = True
            append_final_arguments(state, arguments or "", "function-call item")
            continue
        if event_type == "response.reasoning_summary_text.delta":
            if not preserve_reasoning_history:
                raise UnsupportedProtocolTranslationError(
                    "unsupported_protocol_semantics",
                    "Responses reasoning output requires an explicit Chat capability.",
                )
            delta = event.get("delta")
            if not isinstance(delta, str):
                raise UnsupportedProtocolTranslationError(
                    "unsupported_protocol_semantics",
                    "Cannot translate a non-string Responses reasoning summary delta.",
                )
            if delta:
                reasoning_texts_emitted.add(delta)
                chunks.append(
                    {
                        "id": response_id or f"chatcmpl_{uuid.uuid4().hex[:12]}",
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"reasoning_content": delta},
                                "finish_reason": None,
                            }
                        ],
                    }
                )
            continue
        if event_type == "response.reasoning_summary_text.done":
            if not preserve_reasoning_history:
                raise UnsupportedProtocolTranslationError(
                    "unsupported_protocol_semantics",
                    "Responses reasoning output requires an explicit Chat capability.",
                )
            text = event.get("text")
            if not isinstance(text, str):
                raise UnsupportedProtocolTranslationError(
                    "unsupported_protocol_semantics",
                    "Cannot translate a non-string Responses reasoning summary.",
                )
            if text and text not in reasoning_texts_emitted:
                chunks.append(
                    {
                        "id": response_id or f"chatcmpl_{uuid.uuid4().hex[:12]}",
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": model,
                        "choices": [
                            {
                                "index": 0,
                                "delta": {"reasoning_content": text},
                                "finish_reason": None,
                            }
                        ],
                    }
                )
            continue
        if event_type == "response.function_call_arguments.delta":
            item_id = event.get("item_id") or ""
            state = tool_state(str(item_id))
            delta_args = event.get("delta")
            if delta_args is not None and not isinstance(delta_args, str):
                raise UnsupportedProtocolTranslationError(
                    "unsupported_protocol_semantics",
                    "Cannot translate non-string function-call argument delta.",
                )
            if isinstance(delta_args, str) and delta_args:
                if not state["emitted_header"]:
                    if not state["id"]:
                        raise UnsupportedProtocolTranslationError(
                            "unpaired_tool_call",
                            "Cannot translate function-call arguments without a paired non-empty call_id.",
                        )
                    if not state["name"]:
                        raise UnsupportedProtocolTranslationError(
                            "unsupported_protocol_semantics",
                            "Cannot translate function-call arguments without a paired function name.",
                        )
                    chunks.append(
                        {
                            "id": response_id or f"chatcmpl_{uuid.uuid4().hex[:12]}",
                            "object": "chat.completion.chunk",
                            "created": int(time.time()),
                            "model": model,
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {
                                        "tool_calls": [
                                            {
                                                "index": state["index"],
                                                "id": state["id"],
                                                "type": "function",
                                                "function": {"name": state["name"], "arguments": delta_args},
                                            }
                                        ]
                                    },
                                    "finish_reason": None,
                                }
                            ],
                        }
                    )
                    state["emitted_header"] = True
                else:
                    chunks.append(
                        {
                            "id": response_id or f"chatcmpl_{uuid.uuid4().hex[:12]}",
                            "object": "chat.completion.chunk",
                            "created": int(time.time()),
                            "model": model,
                            "choices": [
                                {
                                    "index": 0,
                                    "delta": {
                                        "tool_calls": [
                                            {
                                                "index": state["index"],
                                                "function": {"arguments": delta_args},
                                            }
                                        ]
                                    },
                                    "finish_reason": None,
                                }
                            ],
                        }
                    )
                state["arguments"] += delta_args
            continue
        if event_type == "response.function_call_arguments.done":
            item_id = str(event.get("item_id") or "")
            state = tool_states.get(item_id)
            arguments = event.get("arguments")
            if not isinstance(arguments, str):
                raise UnsupportedProtocolTranslationError(
                    "unsupported_protocol_semantics",
                    "Cannot translate non-string final function-call arguments.",
                )
            if state is None:
                raise UnsupportedProtocolTranslationError(
                    "unpaired_tool_call",
                    "Cannot translate final function-call arguments without a paired stream item.",
                )
            append_final_arguments(state, arguments, "function-call done")
            continue
        if event_type == "response.output_item.done":
            item = event.get("item")
            item_type, call_id, name, arguments = _validated_responses_stream_output_item(
                item,
                function_name_from_response_item=function_name_from_response_item,
            )
            if item_type == "function_call":
                item_id = str(item.get("id") or call_id or "")
                state = tool_states.get(item_id)
                if state is None or not state["emitted_header"]:
                    raise UnsupportedProtocolTranslationError(
                        "unpaired_tool_call",
                        "Cannot translate a completed function call that was never paired with a stream item.",
                    )
                if state["id"] != call_id or state["name"] != name:
                    raise UnsupportedProtocolTranslationError(
                        "unpaired_tool_call",
                        "Cannot translate a completed function call with conflicting identity.",
                    )
                append_final_arguments(state, arguments or "", "completed function-call item")
            continue
        if event_type == "response.completed":
            response_obj = event.get("response")
            if isinstance(response_obj, Mapping):
                output = response_obj.get("output")
                if "output" in response_obj and not isinstance(output, list):
                    raise UnsupportedProtocolTranslationError(
                        "unsupported_protocol_semantics",
                        "Cannot translate a terminal Responses event with a non-list output payload.",
                    )
                if isinstance(output, list):
                    for item in output:
                        if isinstance(item, Mapping) and item.get("type") == "reasoning":
                            if not preserve_reasoning_history:
                                raise UnsupportedProtocolTranslationError(
                                    "unsupported_protocol_semantics",
                                    "Responses reasoning output requires an explicit Chat capability.",
                                )
                            _require_supported_fields(
                                item,
                                {"id", "type", "status", "summary", "content", "encrypted_content"},
                                "Responses terminal reasoning item",
                            )
                            for part in item.get("summary", []):
                                if isinstance(part, Mapping) and isinstance(part.get("text"), str):
                                    text = part["text"]
                                    if text and text not in reasoning_texts_emitted:
                                        reasoning_texts_emitted.add(text)
                                        chunks.append(
                                            {
                                                "id": response_id or f"chatcmpl_{uuid.uuid4().hex[:12]}",
                                                "object": "chat.completion.chunk",
                                                "created": int(time.time()),
                                                "model": model,
                                                "choices": [
                                                    {
                                                        "index": 0,
                                                        "delta": {"reasoning_content": text},
                                                        "finish_reason": None,
                                                    }
                                                ],
                                            }
                                        )
                            continue
                        item_type, call_id, name, arguments = _validated_responses_stream_output_item(
                            item,
                            function_name_from_response_item=function_name_from_response_item,
                        )
                        if item_type == "function_call":
                            item_id = str(item.get("id") or call_id or "")
                            state = tool_states.get(item_id)
                            if state is None or not state["emitted_header"]:
                                raise UnsupportedProtocolTranslationError(
                                    "unpaired_tool_call",
                                    "Cannot translate a completed function call that was never paired with a stream item.",
                                )
                            if state["id"] != call_id or state["name"] != name:
                                raise UnsupportedProtocolTranslationError(
                                    "unpaired_tool_call",
                                    "Cannot translate terminal function-call output with conflicting identity.",
                                )
                            append_final_arguments(state, arguments or "", "terminal function-call output")
                    finish_reason = "stop"
                else:
                    finish_reason = "stop"
            else:
                finish_reason = "stop"

    if any(state.get("emitted_header") for state in tool_states.values()):
        finish_reason = "tool_calls"
    chunks.append(
        {
            "id": response_id or f"chatcmpl_{uuid.uuid4().hex[:12]}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason or "stop"}],
        }
    )
    return chunks


class ResponsesToChatStreamConverter:
    """Incrementally translate Responses events into Chat Completions chunks."""

    def __init__(self, *, preserve_reasoning_history: bool = False) -> None:
        self.tool_states: dict[str, dict[str, Any]] = {}
        self.model: str | None = None
        self.response_id: str | None = None
        self.completed = False
        self.preserve_reasoning_history = preserve_reasoning_history
        self._reasoning_texts_emitted: set[str] = set()
        self._visible_output = False

    def _tool_state(self, item_id: str) -> dict[str, Any]:
        if item_id not in self.tool_states:
            index = len(self.tool_states)
            self.tool_states[item_id] = {
                "index": index,
                "id": "",
                "name": "",
                "arguments": "",
                "emitted_header": False,
            }
        return self.tool_states[item_id]

    def _chunk(self, delta: Mapping[str, Any], finish_reason: str | None = None) -> dict[str, Any]:
        if any(key in delta for key in ("content", "reasoning_content", "tool_calls")):
            self._visible_output = True
        return {
            "id": self.response_id or f"chatcmpl_{uuid.uuid4().hex[:12]}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": self.model,
            "choices": [{"index": 0, "delta": dict(delta), "finish_reason": finish_reason}],
        }

    def _final_argument_chunks(
        self,
        state: dict[str, Any],
        final_arguments: str,
        label: str,
    ) -> list[dict[str, Any]]:
        if not state["emitted_header"]:
            raise UnsupportedProtocolTranslationError(
                "unpaired_tool_call",
                f"Cannot translate {label} arguments without a paired function-call stream item.",
            )
        suffix = _function_argument_suffix(state["arguments"], final_arguments, label)
        if not suffix:
            return []
        state["arguments"] += suffix
        return [
            self._chunk(
                {
                    "tool_calls": [
                        {
                            "index": state["index"],
                            "function": {"arguments": suffix},
                        }
                    ]
                }
            )
        ]

    def chunks_for_event(self, event: Mapping[str, Any]) -> list[dict[str, Any]]:
        event_type = event.get("type")
        if self.completed:
            # OpenCode and similar providers append bookkeeping after
            # response.completed. New visible deltas still fail closed.
            if event_type in _POST_TERMINAL_SEMANTIC_EVENT_TYPES:
                raise UnsupportedProtocolTranslationError(
                    "unsupported_protocol_semantics",
                    "Cannot translate Responses stream semantics after a terminal event.",
                )
            return []
        if event_type in {"response.failed", "response.incomplete", "error"}:
            raise UnsupportedProtocolTranslationError(
                "upstream_response_failed",
                f"Cannot translate terminal Responses stream event {event_type!r} as a successful Chat Completions stream.",
            )
        if event_type == "response.created":
            response_obj = event.get("response")
            if isinstance(response_obj, Mapping):
                self.response_id = response_obj.get("id") or self.response_id
                self.model = response_obj.get("model") or self.model
            return [self._chunk({"role": "assistant"})]
        if event_type == "response.output_text.delta":
            delta_text = event.get("delta")
            return [self._chunk({"content": delta_text})] if isinstance(delta_text, str) and delta_text else []
        if event_type == "response.reasoning_summary_text.delta":
            if not self.preserve_reasoning_history:
                raise UnsupportedProtocolTranslationError(
                    "unsupported_protocol_semantics",
                    "Responses reasoning output requires an explicit Chat capability.",
                )
            delta = event.get("delta")
            if not isinstance(delta, str):
                raise UnsupportedProtocolTranslationError(
                    "unsupported_protocol_semantics",
                    "Cannot translate a non-string Responses reasoning summary delta.",
                )
            if not delta:
                return []
            self._reasoning_texts_emitted.add(delta)
            return [self._chunk({"reasoning_content": delta})]
        if event_type == "response.reasoning_summary_text.done":
            if not self.preserve_reasoning_history:
                raise UnsupportedProtocolTranslationError(
                    "unsupported_protocol_semantics",
                    "Responses reasoning output requires an explicit Chat capability.",
                )
            text = event.get("text")
            if not isinstance(text, str):
                raise UnsupportedProtocolTranslationError(
                    "unsupported_protocol_semantics",
                    "Cannot translate a non-string Responses reasoning summary.",
                )
            if not text or text in self._reasoning_texts_emitted:
                return []
            self._reasoning_texts_emitted.add(text)
            return [self._chunk({"reasoning_content": text})]
        if event_type in {"response.content_part.added", "response.content_part.done"}:
            part = event.get("part")
            if not isinstance(part, Mapping):
                raise UnsupportedProtocolTranslationError(
                    "unsupported_protocol_semantics",
                    "Cannot translate a Responses stream content-part event without an object part.",
                )
            responses_content_to_chat_content([part])
            return []
        if event_type == "response.output_item.added":
            item = event.get("item")
            if isinstance(item, Mapping) and item.get("type") == "reasoning":
                if not self.preserve_reasoning_history:
                    raise UnsupportedProtocolTranslationError(
                        "unsupported_protocol_semantics",
                        "Responses reasoning output requires an explicit Chat capability.",
                    )
                _require_supported_fields(
                    item,
                    {"id", "type", "status", "summary", "content", "encrypted_content"},
                    "Responses stream reasoning item",
                )
                return []
            item_type, call_id, name, arguments = _validated_responses_stream_output_item(item)
            if item_type == "message":
                return []
            item_id = item.get("id") or item.get("call_id") or ""
            state = self._tool_state(str(item_id))
            if state["id"] and state["id"] != call_id:
                raise UnsupportedProtocolTranslationError(
                    "unpaired_tool_call",
                    "Cannot translate conflicting function-call ids for one Responses stream item.",
                )
            if state["name"] and state["name"] != name:
                raise UnsupportedProtocolTranslationError(
                    "unsupported_protocol_semantics",
                    "Cannot translate conflicting function names for one Responses stream item.",
                )
            state["id"] = call_id
            state["name"] = name
            if not (state["id"] and state["name"] and not state["emitted_header"]):
                return self._final_argument_chunks(state, arguments or "", "function-call item")
            state["emitted_header"] = True
            chunks = [
                self._chunk(
                    {
                        "tool_calls": [
                            {
                                "index": state["index"],
                                "id": state["id"],
                                "type": "function",
                                "function": {"name": state["name"], "arguments": ""},
                            }
                        ]
                    }
                )
            ]
            chunks.extend(self._final_argument_chunks(state, arguments or "", "function-call item"))
            return chunks
        if event_type == "response.function_call_arguments.delta":
            item_id = str(event.get("item_id") or "")
            state = self._tool_state(item_id)
            delta_args = event.get("delta")
            if delta_args is not None and not isinstance(delta_args, str):
                raise UnsupportedProtocolTranslationError(
                    "unsupported_protocol_semantics",
                    "Cannot translate non-string function-call argument delta.",
                )
            if not (isinstance(delta_args, str) and delta_args):
                return []
            if not state["emitted_header"]:
                if not state["id"]:
                    raise UnsupportedProtocolTranslationError(
                        "unpaired_tool_call",
                        "Cannot translate function-call arguments without a paired non-empty call_id.",
                    )
                if not state["name"]:
                    raise UnsupportedProtocolTranslationError(
                        "unsupported_protocol_semantics",
                        "Cannot translate function-call arguments without a paired function name.",
                    )
                state["emitted_header"] = True
                state["arguments"] += delta_args
                return [
                    self._chunk(
                        {
                            "tool_calls": [
                                {
                                    "index": state["index"],
                                    "id": state["id"],
                                    "type": "function",
                                    "function": {"name": state["name"], "arguments": delta_args},
                                }
                            ]
                        }
                    )
                ]
            state["arguments"] += delta_args
            return [
                self._chunk(
                    {
                        "tool_calls": [
                            {
                                "index": state["index"],
                                "function": {"arguments": delta_args},
                            }
                        ]
                    }
                )
            ]
        if event_type == "response.function_call_arguments.done":
            item_id = str(event.get("item_id") or "")
            state = self.tool_states.get(item_id)
            arguments = event.get("arguments")
            if not isinstance(arguments, str):
                raise UnsupportedProtocolTranslationError(
                    "unsupported_protocol_semantics",
                    "Cannot translate non-string final function-call arguments.",
                )
            if state is None:
                raise UnsupportedProtocolTranslationError(
                    "unpaired_tool_call",
                    "Cannot translate final function-call arguments without a paired stream item.",
                )
            return self._final_argument_chunks(state, arguments, "function-call done")
        if event_type == "response.output_item.done":
            item = event.get("item")
            if isinstance(item, Mapping) and item.get("type") == "reasoning":
                if not self.preserve_reasoning_history:
                    raise UnsupportedProtocolTranslationError(
                        "unsupported_protocol_semantics",
                        "Responses reasoning output requires an explicit Chat capability.",
                    )
                _require_supported_fields(
                    item,
                                {"id", "type", "status", "summary", "content", "encrypted_content"},
                    "Responses completed reasoning item",
                )
                for part in item.get("summary", []):
                    if isinstance(part, Mapping) and isinstance(part.get("text"), str):
                        text = part["text"]
                        if text and text not in self._reasoning_texts_emitted:
                            self._reasoning_texts_emitted.add(text)
                            return [self._chunk({"reasoning_content": text})]
                return []
            item_type, call_id, name, arguments = _validated_responses_stream_output_item(item)
            if item_type == "function_call":
                item_id = str(item.get("id") or call_id or "")
                state = self.tool_states.get(item_id)
                if state is None or not state["emitted_header"]:
                    raise UnsupportedProtocolTranslationError(
                        "unpaired_tool_call",
                        "Cannot translate a completed function call that was never paired with a stream item.",
                    )
                if state["id"] != call_id or state["name"] != name:
                    raise UnsupportedProtocolTranslationError(
                        "unpaired_tool_call",
                        "Cannot translate a completed function call with conflicting identity.",
                    )
                return self._final_argument_chunks(state, arguments or "", "completed function-call item")
            return []
        if event_type == "response.completed":
            finish_reason = "stop"
            chunks: list[dict[str, Any]] = []
            response_obj = event.get("response")
            if isinstance(response_obj, Mapping):
                response_id = response_obj.get("id")
                if isinstance(response_id, str) and response_id:
                    self.response_id = response_id
                response_model = response_obj.get("model")
                if isinstance(response_model, str) and response_model:
                    self.model = response_model
                output = response_obj.get("output")
                if "output" in response_obj and not isinstance(output, list):
                    raise UnsupportedProtocolTranslationError(
                        "unsupported_protocol_semantics",
                        "Cannot translate a terminal Responses event with a non-list output payload.",
                    )
                if isinstance(output, list):
                    for item in output:
                        if isinstance(item, Mapping) and item.get("type") == "reasoning":
                            if not self.preserve_reasoning_history:
                                raise UnsupportedProtocolTranslationError(
                                    "unsupported_protocol_semantics",
                                    "Responses reasoning output requires an explicit Chat capability.",
                                )
                            _require_supported_fields(
                                item,
                                {"id", "type", "status", "summary", "content", "encrypted_content"},
                                "Responses terminal reasoning item",
                            )
                            for part in item.get("summary", []):
                                if isinstance(part, Mapping) and isinstance(part.get("text"), str):
                                    text = part["text"]
                                    if text and text not in self._reasoning_texts_emitted:
                                        self._reasoning_texts_emitted.add(text)
                                        chunks.append(self._chunk({"reasoning_content": text}))
                            continue
                        item_type, call_id, name, arguments = _validated_responses_stream_output_item(item)
                        if item_type == "function_call":
                            item_id = str(item.get("id") or call_id or "")
                            state = self.tool_states.get(item_id)
                            if state is None or not state["emitted_header"]:
                                raise UnsupportedProtocolTranslationError(
                                    "unpaired_tool_call",
                                    "Cannot translate a completed function call that was never paired with a stream item.",
                                )
                            if state["id"] != call_id or state["name"] != name:
                                raise UnsupportedProtocolTranslationError(
                                    "unpaired_tool_call",
                                    "Cannot translate terminal function-call output with conflicting identity.",
                                )
                            chunks.extend(
                                self._final_argument_chunks(
                                    state,
                                    arguments or "",
                                    "terminal function-call output",
                                )
                            )
            if any(state.get("emitted_header") for state in self.tool_states.values()):
                finish_reason = "tool_calls"
            had_reasoning = False
            if isinstance(response_obj, Mapping) and isinstance(response_obj.get("output"), list):
                had_reasoning = any(
                    isinstance(item, Mapping) and item.get("type") == "reasoning"
                    for item in response_obj["output"]
                )
            if had_reasoning and not self._visible_output:
                raise UnsupportedProtocolTranslationError(
                    "unsupported_protocol_semantics",
                    "Cannot translate Responses output to Chat Completions without portable text, tools, or reasoning.",
                )
            self.completed = True
            chunks.append(self._chunk({}, finish_reason=finish_reason))
            usage = response_obj.get("usage") if isinstance(response_obj, Mapping) else None
            if isinstance(usage, Mapping):
                chunks.append(
                    {
                        "id": self.response_id or f"chatcmpl_{uuid.uuid4().hex[:12]}",
                        "object": "chat.completion.chunk",
                        "created": int(time.time()),
                        "model": self.model,
                        "choices": [],
                        "usage": _responses_usage_to_chat_usage(usage),
                    }
                )
            return chunks
        return []


class ChatToResponsesStreamConverter:
    """Incrementally translate Chat Completions chunks into Responses events."""

    def __init__(self) -> None:
        self.response_id = f"resp_{uuid.uuid4().hex[:12]}"
        self.model: str | None = None
        self.item_id = f"msg_{uuid.uuid4().hex[:12]}"
        self.text_parts: list[str] = []
        self.reasoning_item_id = f"rs_{uuid.uuid4().hex[:12]}"
        self.reasoning_parts: list[str] = []
        self.reasoning_output_index: int | None = None
        self.reasoning_started = False
        self.message_output_index: int | None = None
        self.next_output_index = 0
        self.tool_states: dict[int, dict[str, Any]] = {}
        self.created = False
        self.message_started = False
        self.completed = False
        self.pending_incomplete: bool | None = None
        self.usage: dict[str, Any] | None = None

    def _allocate_output_index(self) -> int:
        output_index = self.next_output_index
        self.next_output_index += 1
        return output_index

    def _created_events(self) -> list[dict[str, Any]]:
        if self.created:
            return []
        self.created = True
        response = {
            "id": self.response_id,
            "object": "response",
            "status": "in_progress",
            "model": self.model,
            "output": [],
        }
        return [
            {"type": "response.created", "response": response},
            {"type": "response.in_progress", "response": response},
        ]

    def _message_start_events(self) -> list[dict[str, Any]]:
        events = self._created_events()
        if self.message_started:
            return events
        if self.message_output_index is None:
            self.message_output_index = self._allocate_output_index()
        self.message_started = True
        events.extend(
            [
                {
                    "type": "response.output_item.added",
                    "output_index": self.message_output_index,
                    "item": {
                        "id": self.item_id,
                        "type": "message",
                        "status": "in_progress",
                        "role": "assistant",
                        "content": [],
                    },
                },
                {
                    "type": "response.content_part.added",
                    "output_index": self.message_output_index,
                    "item_id": self.item_id,
                    "content_index": 0,
                    "part": {"type": "output_text", "text": "", "annotations": []},
                },
            ]
        )
        return events

    def _tool_state(self, index: int) -> dict[str, Any]:
        if index not in self.tool_states:
            self.tool_states[index] = {
                "output_index": self._allocate_output_index(),
                "item_id": "",
                "call_id": "",
                "name": "",
                "arguments": [],
                "added": False,
                "done": False,
            }
        return self.tool_states[index]

    def _tool_added_events(self, state: dict[str, Any]) -> list[dict[str, Any]]:
        if state["added"] or not state["call_id"] or not state["name"]:
            return []
        events = self._created_events()
        state["item_id"] = f"fc_{state['call_id']}"
        events.append(
            {
                "type": "response.output_item.added",
                "output_index": state["output_index"],
                "item": {
                    "id": state["item_id"],
                    "type": "function_call",
                    "status": "in_progress",
                    "call_id": state["call_id"],
                    "name": state["name"],
                    "arguments": "",
                },
            }
        )
        state["added"] = True
        return events

    def _reasoning_events(self, text: str) -> list[dict[str, Any]]:
        if not text or text in self.reasoning_parts:
            return []
        self.reasoning_parts.append(text)
        events = self._created_events()
        if self.reasoning_output_index is None:
            self.reasoning_output_index = self._allocate_output_index()
        if not self.reasoning_started:
            events.append(
                {
                    "type": "response.output_item.added",
                    "output_index": self.reasoning_output_index,
                    "item": {
                        "id": self.reasoning_item_id,
                        "type": "reasoning",
                        "status": "in_progress",
                        "summary": [],
                    },
                }
            )
            self.reasoning_started = True
        events.append(
            {
                "type": "response.reasoning_summary_text.delta",
                "item_id": self.reasoning_item_id,
                "output_index": self.reasoning_output_index,
                "summary_index": 0,
                "delta": text,
            }
        )
        return events

    def _complete_events(self, *, incomplete: bool = False) -> list[dict[str, Any]]:
        if self.completed:
            return []
        self.completed = True
        events = self._created_events()
        output_by_index: dict[int, dict[str, Any]] = {}
        if self.reasoning_started:
            if self.reasoning_output_index is None or not self.reasoning_parts:
                raise UnsupportedProtocolTranslationError(
                    "unsupported_protocol_semantics",
                    "Cannot complete a reasoning stream without summary text.",
                )
            reasoning_text = "\n".join(self.reasoning_parts)
            reasoning_item = {
                "id": self.reasoning_item_id,
                "type": "reasoning",
                "status": "completed",
                "summary": [
                    {"type": "summary_text", "text": text}
                    for text in self.reasoning_parts
                ],
            }
            events.extend(
                [
                    {
                        "type": "response.reasoning_summary_text.done",
                        "item_id": self.reasoning_item_id,
                        "output_index": self.reasoning_output_index,
                        "summary_index": 0,
                        "text": reasoning_text,
                    },
                    {
                        "type": "response.output_item.done",
                        "output_index": self.reasoning_output_index,
                        "item": reasoning_item,
                    },
                ]
            )
            output_by_index[self.reasoning_output_index] = reasoning_item
        for state in sorted(self.tool_states.values(), key=lambda item: item["output_index"]):
            if not state["call_id"]:
                raise UnsupportedProtocolTranslationError(
                    "unpaired_tool_call",
                    "Cannot translate a terminal assistant tool call without a non-empty id.",
                )
            if not state["name"]:
                raise UnsupportedProtocolTranslationError(
                    "unsupported_protocol_semantics",
                    "Cannot translate a terminal assistant tool call without a non-empty function name.",
                )
            events.extend(self._tool_added_events(state))
            if state["done"]:
                continue
            arguments = "".join(state["arguments"])
            item = {
                "id": state["item_id"],
                "type": "function_call",
                "status": "completed",
                "call_id": state["call_id"],
                "name": state["name"],
                "arguments": arguments,
            }
            events.extend(
                [
                    {
                        "type": "response.function_call_arguments.done",
                        "item_id": state["item_id"],
                        "output_index": state["output_index"],
                        "arguments": arguments,
                    },
                    {"type": "response.output_item.done", "output_index": state["output_index"], "item": item},
                ]
            )
            state["done"] = True
            output_by_index[state["output_index"]] = item
        if self.message_started:
            text = "".join(self.text_parts)
            output_index = self.message_output_index if self.message_output_index is not None else 0
            item = {
                "id": self.item_id,
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            }
            events.extend(
                [
                    {
                        "type": "response.output_text.done",
                        "item_id": self.item_id,
                        "output_index": output_index,
                        "content_index": 0,
                        "text": text,
                    },
                    {"type": "response.output_item.done", "output_index": output_index, "item": item},
                ]
            )
            output_by_index[output_index] = item
        output = [item for _, item in sorted(output_by_index.items(), key=lambda pair: pair[0])]
        response = {
            "id": self.response_id,
            "object": "response",
            "status": "incomplete" if incomplete else "completed",
            "model": self.model,
            "output": output,
        }
        if incomplete:
            response["incomplete_details"] = {"reason": "max_output_tokens"}
        if self.usage is not None:
            response["usage"] = dict(self.usage)
        events.append({"type": "response.incomplete" if incomplete else "response.completed", "response": response})
        return events

    def events_for_done(self) -> list[dict[str, Any]]:
        return self._complete_events(incomplete=self.pending_incomplete is True)

    def events_for_chunk(self, chunk: Mapping[str, Any]) -> list[dict[str, Any]]:
        _validate_chat_stream_choices(chunk)
        choices = chunk.get("choices")
        if self.completed:
            if _is_chat_stream_usage_only_chunk(chunk):
                return []
            raise UnsupportedProtocolTranslationError(
                "unsupported_protocol_semantics",
                "Cannot translate Chat Completions stream semantics after a terminal chunk.",
            )
        if _is_chat_stream_usage_only_chunk(chunk):
            usage = chunk.get("usage")
            if not isinstance(usage, Mapping):
                raise UnsupportedProtocolTranslationError(
                    "unsupported_protocol_semantics",
                    "Cannot translate a Chat Completions usage chunk without an object usage payload.",
                )
            self.usage = _chat_completion_usage_to_responses_usage(usage)
            return []
        if self.pending_incomplete is not None:
            raise UnsupportedProtocolTranslationError(
                "unsupported_protocol_semantics",
                "Cannot translate Chat Completions stream semantics after a terminal chunk.",
            )
        if isinstance(chunk.get("model"), str):
            self.model = chunk.get("model")
        events: list[dict[str, Any]] = []
        if not isinstance(choices, list):
            return events
        for choice in choices:
            if not isinstance(choice, Mapping):
                continue
            source = _chat_stream_source(choice)
            if isinstance(source, Mapping):
                _validate_chat_stream_source(source)
                reasoning_output = _chat_reasoning_output(source)
                if reasoning_output is not None:
                    events.extend(self._reasoning_events(reasoning_output["summary"][0]["text"]))
                content = source.get("content")
                if isinstance(content, str):
                    if content:
                        self.text_parts.append(content)
                        events.extend(self._message_start_events())
                        events.append(
                            {
                                "type": "response.output_text.delta",
                                "item_id": self.item_id,
                                "output_index": self.message_output_index if self.message_output_index is not None else 0,
                                "content_index": 0,
                                "delta": content,
                            }
                        )
                elif content is not None:
                    raise UnsupportedProtocolTranslationError(
                        "unsupported_protocol_semantics",
                        "Cannot translate non-text Chat Completions stream content to Responses without losing it.",
                    )
                tool_calls = source.get("tool_calls")
                if tool_calls is not None:
                    if not isinstance(tool_calls, list):
                        raise UnsupportedProtocolTranslationError(
                            "unsupported_protocol_semantics",
                            "Cannot translate a non-list Chat Completions stream tool_calls payload.",
                        )
                    for fallback_index, tool_call in enumerate(tool_calls):
                        if not isinstance(tool_call, Mapping):
                            raise UnsupportedProtocolTranslationError(
                                "unsupported_protocol_semantics",
                                "Cannot translate a non-object assistant tool-call stream delta.",
                            )
                        _require_supported_fields(
                            tool_call,
                            {"index", "id", "type", "function"},
                            "Chat Completions assistant tool-call stream delta",
                        )
                        tool_type = tool_call.get("type")
                        if tool_type not in (None, "function"):
                            raise UnsupportedProtocolTranslationError(
                                "unsupported_protocol_semantics",
                                f"Cannot translate assistant tool type {tool_type!r} to Responses stream events.",
                            )
                        raw_index = tool_call.get("index", fallback_index)
                        if not isinstance(raw_index, int):
                            raise UnsupportedProtocolTranslationError(
                                "unsupported_protocol_semantics",
                                "Cannot translate a non-integer assistant tool-call stream index.",
                            )
                        index = raw_index
                        state = self._tool_state(index)
                        call_id = tool_call.get("id")
                        if call_id is not None:
                            if not isinstance(call_id, str):
                                raise UnsupportedProtocolTranslationError(
                                    "unpaired_tool_call",
                                    "Cannot translate a tool-call stream delta with an invalid id.",
                                )
                            if call_id:
                                if state["call_id"] and state["call_id"] != call_id:
                                    raise UnsupportedProtocolTranslationError(
                                        "unpaired_tool_call",
                                        "Cannot translate conflicting tool-call ids for one stream index.",
                                    )
                                state["call_id"] = call_id
                        function = tool_call.get("function")
                        argument_delta: str | None = None
                        if function is not None and not isinstance(function, Mapping):
                            raise UnsupportedProtocolTranslationError(
                                "unsupported_protocol_semantics",
                                "Cannot translate a non-object assistant function-call stream delta.",
                            )
                        if isinstance(function, Mapping):
                            _require_supported_fields(
                                function,
                                {"name", "arguments"},
                                "Chat Completions assistant function-call stream delta",
                            )
                            name = function.get("name")
                            if name is not None:
                                if not isinstance(name, str) or not name:
                                    raise UnsupportedProtocolTranslationError(
                                        "unsupported_protocol_semantics",
                                        "Cannot translate an invalid assistant function name in a stream delta.",
                                    )
                                if state["name"] and state["name"] != name:
                                    raise UnsupportedProtocolTranslationError(
                                        "unsupported_protocol_semantics",
                                        "Cannot translate conflicting function names for one stream index.",
                                    )
                                state["name"] = name
                            arguments = _function_arguments(function, "Chat Completions function-call stream")
                            if arguments:
                                state["arguments"].append(arguments)
                                argument_delta = arguments
                        events.extend(self._tool_added_events(state))
                        if state["added"] and argument_delta:
                            events.append(
                                {
                                    "type": "response.function_call_arguments.delta",
                                    "item_id": state["item_id"],
                                    "output_index": state["output_index"],
                                    "delta": argument_delta,
                                }
                            )
            finish_reason = choice.get("finish_reason")
            if finish_reason == "length":
                self.pending_incomplete = True
            elif finish_reason is not None:
                if finish_reason not in {"stop", "tool_calls"}:
                    raise UnsupportedProtocolTranslationError(
                        "unsupported_protocol_semantics",
                        f"Cannot translate Chat Completions finish_reason {finish_reason!r} to Responses stream events.",
                    )
                self.pending_incomplete = False
        return events


class GatewayResponsesToChatStreamConverter(ResponsesToChatStreamConverter):
    """Gateway-facing converter that maps seam errors onto transport errors."""

    def chunks_for_event(self, event: Mapping[str, Any]) -> list[dict[str, Any]]:
        try:
            return super().chunks_for_event(event)
        except UnsupportedProtocolTranslationError as exc:
            raise UpstreamProtocolTranslationError(exc) from exc


class GatewayChatToResponsesStreamConverter(ChatToResponsesStreamConverter):
    """Gateway-facing converter that maps seam errors onto transport errors."""

    def events_for_chunk(self, chunk: Mapping[str, Any]) -> list[dict[str, Any]]:
        try:
            return super().events_for_chunk(chunk)
        except UnsupportedProtocolTranslationError as exc:
            raise UpstreamProtocolTranslationError(exc) from exc

    def events_for_done(self) -> list[dict[str, Any]]:
        try:
            return super().events_for_done()
        except UnsupportedProtocolTranslationError as exc:
            raise UpstreamProtocolTranslationError(exc) from exc


def events_to_responses_body(
    events: list[Mapping[str, Any]],
    *,
    require_completed: bool = False,
    usage_from_response: UsageFromResponse = _default_usage_from_response,
) -> bytes:
    """Reconstruct a non-streaming Responses body from Responses SSE events."""
    terminal_types = {
        event.get("type")
        for event in events
        if isinstance(event, Mapping)
    }
    if require_completed and not terminal_types.intersection(
        {"response.completed", "response.incomplete"}
    ):
        raise UpstreamStreamIncompleteError(
            "Responses stream ended before response.completed or response.incomplete"
        )

    output: list[dict[str, Any]] = []
    response_id = f"resp_{uuid.uuid4().hex[:12]}"
    model: str | None = None
    text_parts: list[str] = []
    reasoning_summary_parts: list[str] = []
    current_item: dict[str, Any] | None = None
    usage: Mapping[str, Any] | None = None
    response_payload: dict[str, Any] = {}
    terminal_status: str | None = None

    for event in events:
        if not isinstance(event, Mapping):
            continue
        event_type = event.get("type")
        if event_type == "response.created":
            response = event.get("response")
            if isinstance(response, Mapping):
                response_payload.update(dict(response))
                response_id = response.get("id") or response_id
                model = response.get("model") or model
        elif event_type == "response.output_item.added":
            item = event.get("item")
            if isinstance(item, dict):
                current_item = dict(item)
        elif event_type == "response.output_text.delta":
            delta = event.get("delta")
            if isinstance(delta, str):
                text_parts.append(delta)
        elif event_type == "response.reasoning_summary_text.delta":
            delta = event.get("delta")
            if isinstance(delta, str) and delta:
                reasoning_summary_parts.append(delta)
        elif event_type == "response.reasoning_summary_text.done":
            text = event.get("text")
            if isinstance(text, str) and text and not reasoning_summary_parts:
                reasoning_summary_parts.append(text)
        elif event_type == "response.output_item.done":
            item = event.get("item")
            if isinstance(item, dict):
                output.append(dict(item))
                current_item = None
        elif event_type == "response.function_call_arguments.done":
            arguments = event.get("arguments")
            if current_item and isinstance(arguments, str):
                current_item["arguments"] = arguments
        elif event_type == "response.custom_tool_call_input.done":
            tool_input = event.get("input")
            if current_item and isinstance(tool_input, str):
                current_item["input"] = tool_input
        elif event_type in {"response.completed", "response.incomplete"}:
            response = event.get("response")
            if isinstance(response, Mapping):
                response_payload.update(dict(response))
                response_id = response.get("id") or response_id
                model = response.get("model") or model
                usage = usage_from_response(response) or usage
                response_output = response.get("output")
                if isinstance(response_output, list) and not output:
                    output = [dict(item) for item in response_output if isinstance(item, dict)]
            terminal_status = (
                "incomplete"
                if event_type == "response.incomplete"
                else "completed"
            )

    joined_reasoning = "".join(reasoning_summary_parts)
    if joined_reasoning:
        for item in output:
            if not isinstance(item, dict) or item.get("type") != "reasoning":
                continue
            summary = item.get("summary")
            has_text = isinstance(summary, list) and any(
                isinstance(part, Mapping) and isinstance(part.get("text"), str) and part["text"]
                for part in summary
            )
            if not has_text:
                item["summary"] = [{"type": "summary_text", "text": joined_reasoning}]

    if text_parts and not any(item.get("type") == "message" for item in output):
        output.append(
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "".join(text_parts), "annotations": []}],
            }
        )

    payload: dict[str, Any] = dict(response_payload)
    payload["id"] = response_id
    payload.setdefault("object", "response")
    if terminal_status is not None:
        payload["status"] = terminal_status
    else:
        payload.setdefault("status", "completed")
    if model is not None or "model" not in payload:
        payload["model"] = model
    if output or not isinstance(payload.get("output"), list):
        payload["output"] = output
    if usage is not None:
        payload["usage"] = dict(usage)
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


def response_body_to_response_sse_events(
    body: bytes,
    *,
    collect_text_fragments: CollectTextFragments = _default_collect_text_fragments,
) -> list[dict[str, Any]]:
    payload = decode_protocol_json(body)
    if not isinstance(payload, dict):
        return []

    response = dict(payload)
    response_id = response.get("id") if isinstance(response.get("id"), str) else f"resp_{uuid.uuid4().hex[:12]}"
    response["id"] = response_id
    response.setdefault("object", "response")
    status = response.get("status")
    if status is None:
        status = "failed" if response.get("error") is not None else "completed"
    if status not in {"completed", "incomplete", "failed"}:
        raise UnsupportedProtocolTranslationError(
            "unsupported_protocol_semantics",
            f"Cannot synthesize a terminal Responses stream from body status {status!r}.",
        )
    response["status"] = status
    output = response.get("output")
    if "output" in response and not isinstance(output, list):
        raise UnsupportedProtocolTranslationError(
            "unsupported_protocol_semantics",
            "Cannot synthesize Responses stream events from a present non-list output payload.",
        )
    output_items = output if isinstance(output, list) else []
    model_value = response.get("model")

    created_response = dict(response)
    created_response["status"] = "in_progress"
    created_response["output"] = []
    events: list[dict[str, Any]] = [
        {"type": "response.created", "response": created_response},
        {"type": "response.in_progress", "response": created_response},
    ]

    for output_index, raw_item in enumerate(output_items):
        if not isinstance(raw_item, Mapping):
            raise UnsupportedProtocolTranslationError(
                "unsupported_protocol_semantics",
                "Cannot synthesize Responses stream events for a non-object output item.",
            )
        item = dict(raw_item)
        if item.get("type") == "reasoning":
            _require_supported_fields(
                item,
                {"id", "type", "status", "summary", "content", "encrypted_content"},
                "Responses stream reasoning item",
            )
            _responses_reasoning_text(item)
            item_id = item.get("id") if isinstance(item.get("id"), str) else f"item_{output_index}"
            item["id"] = item_id
            in_progress_item = dict(item)
            in_progress_item["status"] = "in_progress"
            events.append(
                {
                    "type": "response.output_item.added",
                    "output_index": output_index,
                    "item": in_progress_item,
                }
            )
            for summary_index, part in enumerate(item["summary"]):
                text = part["text"]
                events.extend(
                    [
                        {
                            "type": "response.reasoning_summary_part.added",
                            "output_index": output_index,
                            "item_id": item_id,
                            "summary_index": summary_index,
                            "part": {"type": "summary_text", "text": ""},
                        },
                        {
                            "type": "response.reasoning_summary_text.delta",
                            "output_index": output_index,
                            "item_id": item_id,
                            "summary_index": summary_index,
                            "delta": text,
                        },
                        {
                            "type": "response.reasoning_summary_text.done",
                            "output_index": output_index,
                            "item_id": item_id,
                            "summary_index": summary_index,
                            "text": text,
                        },
                    ]
                )
            events.append(
                {
                    "type": "response.output_item.done",
                    "output_index": output_index,
                    "item": item,
                }
            )
            continue
        if item.get("type") == "custom_tool_call":
            _require_supported_fields(
                item,
                {"id", "type", "status", "call_id", "name", "input"},
                "Responses stream custom-tool item",
            )
            call_id = item.get("call_id")
            name = item.get("name")
            tool_input = item.get("input")
            if not isinstance(call_id, str) or not call_id:
                raise UnsupportedProtocolTranslationError(
                    "unpaired_tool_call",
                    "Cannot synthesize a custom-tool stream item without a non-empty call_id.",
                )
            if not isinstance(name, str) or not name:
                raise UnsupportedProtocolTranslationError(
                    "unsupported_protocol_semantics",
                    "Cannot synthesize a custom-tool stream item without a non-empty name.",
                )
            if not isinstance(tool_input, str):
                raise UnsupportedProtocolTranslationError(
                    "unsupported_protocol_semantics",
                    "Cannot synthesize a custom-tool stream item without string input.",
                )
            item_id = item.get("id") if isinstance(item.get("id"), str) else f"item_{output_index}"
            item["id"] = item_id
            in_progress_item = dict(item)
            in_progress_item["status"] = "in_progress"
            in_progress_item["input"] = ""
            events.append(
                {
                    "type": "response.output_item.added",
                    "output_index": output_index,
                    "item": in_progress_item,
                }
            )
            if tool_input:
                events.append(
                    {
                        "type": "response.custom_tool_call_input.delta",
                        "output_index": output_index,
                        "item_id": item_id,
                        "delta": tool_input,
                    }
                )
            events.extend(
                [
                    {
                        "type": "response.custom_tool_call_input.done",
                        "output_index": output_index,
                        "item_id": item_id,
                        "input": tool_input,
                    },
                    {
                        "type": "response.output_item.done",
                        "output_index": output_index,
                        "item": item,
                    },
                ]
            )
            continue
        item_type, _, _, arguments = _validated_responses_stream_output_item(item)
        item_id = item.get("id") if isinstance(item.get("id"), str) else f"item_{output_index}"
        item["id"] = item_id
        if item_type == "message":
            in_progress_item = dict(item)
            in_progress_item["status"] = "in_progress"
            events.append(
                {
                    "type": "response.output_item.added",
                    "output_index": output_index,
                    "item": in_progress_item,
                }
            )
            text = "".join(collect_text_fragments(item.get("content")))
            if text:
                events.extend(
                    [
                        {
                            "type": "response.content_part.added",
                            "output_index": output_index,
                            "item_id": item_id,
                            "content_index": 0,
                            "part": {"type": "output_text", "text": "", "annotations": []},
                        },
                        {
                            "type": "response.output_text.delta",
                            "output_index": output_index,
                            "item_id": item_id,
                            "content_index": 0,
                            "delta": text,
                        },
                        {
                            "type": "response.output_text.done",
                            "output_index": output_index,
                            "item_id": item_id,
                            "content_index": 0,
                            "text": text,
                        },
                        {
                            "type": "response.content_part.done",
                            "output_index": output_index,
                            "item_id": item_id,
                            "content_index": 0,
                            "part": {"type": "output_text", "text": text, "annotations": []},
                        },
                    ]
                )
            events.append(
                {
                    "type": "response.output_item.done",
                    "output_index": output_index,
                    "item": item,
                }
            )
            continue
        if item_type == "function_call":
            in_progress_item = dict(item)
            in_progress_item["status"] = "in_progress"
            events.append(
                {
                    "type": "response.output_item.added",
                    "output_index": output_index,
                    "item": in_progress_item,
                }
            )
            if arguments:
                events.append(
                    {
                        "type": "response.function_call_arguments.delta",
                        "output_index": output_index,
                        "item_id": item_id,
                        "delta": arguments,
                    }
                )
            events.extend(
                [
                    {
                        "type": "response.function_call_arguments.done",
                        "output_index": output_index,
                        "item_id": item_id,
                        "arguments": arguments or "",
                    },
                    {
                        "type": "response.output_item.done",
                        "output_index": output_index,
                        "item": item,
                    },
                ]
            )

    if model_value is not None:
        response["model"] = model_value
    terminal_event_type = {
        "completed": "response.completed",
        "incomplete": "response.incomplete",
        "failed": "response.failed",
    }[status]
    events.append({"type": terminal_event_type, "response": response})
    return events


@dataclass(frozen=True)
class PreparedExchange:
    inbound_format: str
    outbound_format: str
    upstream_body: bytes
    stream: bool
    function_name_from_response_item: FunctionNameFromResponseItem | None = None
    dropped_cache_controls: tuple[str, ...] = ()
    preserve_reasoning_history: bool = False

    def decode_stream(self) -> ChatToResponsesStreamConverter | ResponsesToChatStreamConverter:
        if self.inbound_format == "responses" and self.outbound_format == "chat_completions":
            return ChatToResponsesStreamConverter()
        if self.inbound_format == "chat_completions" and self.outbound_format == "responses":
            return ResponsesToChatStreamConverter(
                preserve_reasoning_history=self.preserve_reasoning_history,
            )
        raise NonForwardable(
            "unsupported_protocol_semantics",
            "No stream decoder for %s -> %s." % (self.inbound_format, self.outbound_format),
        )

    stream_decoder = decode_stream

    def decode_response(
        self,
        body: bytes,
        *,
        function_name_from_response_item: FunctionNameFromResponseItem | None = None,
    ) -> bytes:
        if self.inbound_format == self.outbound_format:
            return body
        try:
            if self.inbound_format == "chat_completions" and self.outbound_format == "responses":
                return response_body_to_chat_completion_body(
                    body,
                    function_name_from_response_item=(
                        function_name_from_response_item
                        or self.function_name_from_response_item
                        or _default_function_name_from_response_item
                    ),
                    preserve_reasoning_history=self.preserve_reasoning_history,
                )
            if self.inbound_format == "responses" and self.outbound_format == "chat_completions":
                return chat_completion_to_response_body(body)
        except UnsupportedProtocolTranslationError as error:
            raise NonForwardable(error.code, str(error)) from error
        except (UnicodeError, json.JSONDecodeError) as error:
            raise NonForwardable(
                "unsupported_protocol_semantics",
                "Cannot decode the upstream response as JSON.",
            ) from error
        raise NonForwardable(
            "unsupported_protocol_semantics",
            "No response decoder for %s -> %s." % (self.inbound_format, self.outbound_format),
        )


class NonForwardable(UnsupportedProtocolTranslationError):
    """The request cannot cross the protocol seam without loss."""


def prepare_exchange(
    request_body: bytes,
    *,
    inbound_format: str,
    outbound_format: str,
    prompt_cache_key_policy: PromptCacheKeyPolicy = PromptCacheKeyPolicy.DROP_UNVERIFIED,
    preserve_reasoning_history: bool = False,
) -> PreparedExchange:
    inbound = str(inbound_format or "").strip().lower()
    outbound = str(outbound_format or "").strip().lower()
    try:
        cache_key_present = False
        cache_key = None
        dropped_cache_controls: tuple[str, ...] = ()
        conversion_body = request_body
        if inbound != outbound:
            source_payload = decode_protocol_json(request_body)
            if not isinstance(source_payload, dict):
                raise UnsupportedProtocolTranslationError(
                    "unsupported_protocol_semantics", "Cannot prepare a non-object conversion request.",
                )
            if "prompt_cache_key" in source_payload:
                cache_key_present = True
                cache_key = source_payload.pop("prompt_cache_key")
                if cache_key is not None and not isinstance(cache_key, str):
                    raise UnsupportedProtocolTranslationError(
                        "unsupported_protocol_semantics", "Cannot translate non-string prompt_cache_key.",
                    )
                if prompt_cache_key_policy is not PromptCacheKeyPolicy.PRESERVE:
                    dropped_cache_controls = ("prompt_cache_key",)
                conversion_body = json.dumps(source_payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")

        def converted(upstream: bytes) -> PreparedExchange:
            payload = decode_protocol_json(upstream)
            if cache_key_present and prompt_cache_key_policy is PromptCacheKeyPolicy.PRESERVE:
                payload["prompt_cache_key"] = cache_key
                upstream = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
            return PreparedExchange(
                inbound,
                outbound,
                upstream,
                bool(payload.get("stream")),
                dropped_cache_controls=dropped_cache_controls,
                preserve_reasoning_history=preserve_reasoning_history,
            )

        if inbound == "responses" and outbound == "chat_completions":
            request_payload = source_payload
            _consume_codex_chat_transport_fields(
                request_payload,
                preserve_reasoning_history=preserve_reasoning_history,
            )
            prepared_request_body = json.dumps(
                request_payload,
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("utf-8")
            upstream = responses_request_to_chat_completion_body(
                prepared_request_body,
                preserve_reasoning_history=preserve_reasoning_history,
            )
            return converted(upstream)
        if inbound == "chat_completions" and outbound == "responses":
            upstream = chat_completions_request_to_responses_body(conversion_body)
            return converted(upstream)
        if inbound == outbound:
            stream = bool(
                re.search(rb'"stream"\s*:\s*true\b', request_body, flags=re.IGNORECASE)
            )
            return PreparedExchange(
                inbound,
                outbound,
                request_body,
                stream,
                preserve_reasoning_history=preserve_reasoning_history,
            )
    except UnsupportedProtocolTranslationError as error:
        raise NonForwardable(error.code, str(error)) from error
    except (UnicodeError, json.JSONDecodeError) as error:
        raise NonForwardable(
            "unsupported_protocol_semantics",
            "Cannot parse the inbound request as JSON.",
        ) from error
    raise NonForwardable(
        "unsupported_protocol_semantics",
        "Cannot prepare %s -> %s without a lossless mapping." % (inbound, outbound),
    )


__all__ = [
    entrypoint.__name__
    for entrypoint in (
        ChatToResponsesStreamConverter,
        GatewayChatToResponsesStreamConverter,
        GatewayResponsesToChatStreamConverter,
        NonForwardable,
        PreparedExchange,
        ResponsesToChatStreamConverter,
        UnsupportedProtocolTranslationError,
        UpstreamStreamIncompleteError,
        chat_completion_body_to_stream_chunks,
        chat_completion_error_body,
        chat_completion_to_response_body,
        chat_completions_request_to_responses_body,
        chat_content_to_responses_content,
        chat_messages_to_responses_input,
        chat_stream_chunks_to_response_events,
        chat_tool_choice_to_responses_tool_choice,
        chat_tools_to_responses_tools,
        events_to_responses_body,
        prepare_exchange,
        response_body_to_chat_completion_body,
        response_body_to_response_sse_events,
        response_events_to_chat_stream_chunks,
        responses_content_to_chat_content,
        responses_events_have_completed,
        responses_input_to_chat_messages,
        responses_request_to_chat_completion_body,
        responses_tool_choice_to_chat_tool_choice,
        responses_tools_to_chat_tools,
    )
]
