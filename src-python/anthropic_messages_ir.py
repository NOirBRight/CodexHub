"""Pure Anthropic Messages representation and conversion seam.

Production inbound ``/v1/messages`` uses this in-memory seam. The isolated
HTTP exchange and relay helpers live in ``claude_messages_evidence_exchange``.

Three outcomes, never a silent drop:

* the native path (:meth:`AnthropicRequest.to_native_body`) re-serializes every
  modelled and unmodelled field, so an Anthropic-format upstream loses nothing;
* a converted path returns :class:`Adapted`, the translated body plus one named
  :class:`Adaptation` per non-equivalent field;
* anything the adapter cannot carry returns :class:`NotForwardable` naming the
  fields before any upstream I/O.

Conversion reuses existing repository seams rather than adding a third
translator: Anthropic Messages becomes Chat Completions here, then the existing
``protocol_translation.chat_completions_request_to_responses_body`` seam
produces the Responses request.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping

from gateway_errors import UpstreamStreamIncompleteError
from protocol_translation import (
    ResponsesToChatStreamConverter,
    UnsupportedProtocolTranslationError,
    chat_completion_to_response_body,
    chat_completions_request_to_responses_body,
    chat_stream_chunks_to_response_events,
    decode_protocol_json,
    response_events_to_chat_stream_chunks,
)
from sse_events import (
    SseEventAssembler,
)


__all__ = [
    "Adaptation",
    "Adapted",
    "AdaptedResponse",
    "AnthropicRequest",
    "ContentBlock",
    "HeaderPlan",
    "Message",
    "NotForwardable",
    "adapt_upstream_response",
    "adapt_upstream_stream",
    "anthropic_error_body",
    "anthropic_error_sse",
    "ChatToAnthropicEmitter",
    "classify_headers",
    "content_type_is_sse",
    "parse_request",
    "prepare_upstream_request",
    "response_refusal",
]

ANTHROPIC_VERSION = "2023-06-01"

# Modelled request fields. Anything else is unmodelled and must be declared.
MODELLED_REQUEST_FIELDS = frozenset(
    {
        "model",
        "messages",
        "system",
        "max_tokens",
        "stream",
        "temperature",
        "top_p",
        "stop_sequences",
        "tools",
        "tool_choice",
        "thinking",
    }
)

# Real Claude Code traffic observed on v2.1.278 carries these; Chat Completions
# has no equivalent, so they are omitted under a named policy rather than dropped.
OPTION_FIELDS_WITHOUT_CHAT_EQUIVALENT = {
    "context_management": "context_management_omitted_for_chat",
    "metadata": "request_metadata_omitted_for_chat",
}

# Explicitly requested safety controls are not "best effort" material. Until an
# equivalent enforcement is proven upstream, they make the request
# non-forwardable instead of being adapted away (ADR-0013 critical content).
SAFETY_BEARING_FIELDS = frozenset({"safeguards"})

# The native path forwards the inbound bytes; the Gateway still owns these
# rewrites at the boundary (ADR-0013). Named here so nothing is implicit.
# Mirrors the Chat Completions request fields accepted by
# protocol_translation.chat_completions_request_to_responses_body, so a refusal
# can name the rejected field instead of only quoting the seam's message.
RESPONSES_SEAM_CHAT_FIELDS = frozenset(
    {
        "model", "messages", "tools", "tool_choice", "stream", "temperature", "top_p",
        "presence_penalty", "frequency_penalty", "parallel_tool_calls", "max_tokens",
        "max_output_tokens", "reasoning", "reasoning_effort", "chat_template_kwargs",
        "stream_options", "n", "prompt_cache_key",
    }
)

NATIVE_GATEWAY_REWRITES = (
    "model: resolved to the selected upstream provider/model identity",
    "credential headers: Gateway credential consumed, upstream credential injected",
    "transport: host and connection-management headers regenerated",
)

CREDENTIAL_HEADERS = frozenset({"authorization", "x-api-key", "proxy-authorization"})
SEMANTIC_HEADERS = frozenset({"anthropic-version", "anthropic-beta", "anthropic-workspace-id"})
TRANSPORT_HEADERS = frozenset(
    {"content-type", "accept", "accept-encoding", "content-length", "connection", "host", "user-agent"}
)
CLIENT_METADATA_PREFIXES = ("x-claude-code-", "x-stainless-", "x-app")

_TOOL_RESULT_BLOCK = "tool_result"
_TOOL_USE_BLOCK = "tool_use"
_TEXT_LIKE_BLOCKS = frozenset({"text", "thinking", "redacted_thinking"})


@dataclass(frozen=True, slots=True)
class Adaptation:
    """One field that a converted path did not carry byte-for-byte."""

    field: str
    policy: str
    detail: str

    def diagnostic(self) -> str:
        return f"{self.field}: {self.policy} ({self.detail})"


@dataclass(frozen=True, slots=True)
class Adapted:
    body: bytes = field(repr=False)
    adaptations: tuple[Adaptation, ...] = ()

    def diagnostics(self) -> tuple[str, ...]:
        return tuple(item.diagnostic() for item in self.adaptations)


@dataclass(frozen=True, slots=True)
class AdaptedResponse:
    """Anthropic response produced from one upstream response envelope.

    The adapter buffers a finite SSE fixture so tests can inspect the full
    result.  A production relay can use the same conversion helpers per frame;
    this seam deliberately keeps that transport policy outside the converter.
    """

    body: bytes = field(repr=False)
    status: int = 200
    content_type: str = "application/json"
    adaptations: tuple[Adaptation, ...] = ()

    @property
    def is_stream(self) -> bool:
        return self.content_type.lower().split(";", 1)[0].strip() == "text/event-stream"

    def diagnostics(self) -> tuple[str, ...]:
        return tuple(item.diagnostic() for item in self.adaptations)


@dataclass(frozen=True, slots=True)
class NotForwardable:
    reason: str
    fields: tuple[str, ...]

    def diagnostic(self) -> str:
        return f"{self.reason}: {', '.join(self.fields)}"


@dataclass(frozen=True, slots=True)
class ContentBlock:
    type: str
    data: Mapping[str, Any] = field(repr=False)  # prompt/tool content never renders

    def to_json(self) -> dict[str, Any]:
        return dict(self.data)


@dataclass(frozen=True, slots=True)
class Message:
    role: str
    content: tuple[ContentBlock, ...]
    extra: Mapping[str, Any] = field(default_factory=dict, repr=False)  # unmodelled message keys

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"role": self.role, "content": [block.to_json() for block in self.content]}
        for name, value in self.extra.items():
            payload[name] = value
        return payload


@dataclass(frozen=True, slots=True)
class HeaderPlan:
    """Case-insensitive classification of inbound request headers.

    Credential values stay out of ``repr`` so a logged plan cannot leak a key.
    """

    credential: tuple[tuple[str, str], ...] = field(default=(), repr=False)
    semantic: tuple[tuple[str, str], ...] = ()
    transport: tuple[tuple[str, str], ...] = ()
    client_metadata: tuple[tuple[str, str], ...] = ()
    unclassified: tuple[tuple[str, str], ...] = ()

    def names(self, group: str) -> tuple[str, ...]:
        return tuple(name for name, _ in getattr(self, group))


def classify_headers(headers: Mapping[str, str] | Iterable[tuple[str, str]]) -> HeaderPlan:
    items = headers.items() if isinstance(headers, Mapping) else headers
    groups: dict[str, list[tuple[str, str]]] = {
        "credential": [],
        "semantic": [],
        "transport": [],
        "client_metadata": [],
        "unclassified": [],
    }
    for name, value in items:
        lowered = name.lower()
        if lowered in CREDENTIAL_HEADERS:
            groups["credential"].append((name, value))
        elif lowered in SEMANTIC_HEADERS:
            groups["semantic"].append((name, value))
        elif lowered in TRANSPORT_HEADERS:
            groups["transport"].append((name, value))
        elif lowered.startswith(CLIENT_METADATA_PREFIXES):
            groups["client_metadata"].append((name, value))
        else:
            groups["unclassified"].append((name, value))
    return HeaderPlan(**{key: tuple(value) for key, value in groups.items()})


@dataclass(frozen=True, slots=True)
class AnthropicRequest:
    """Ordered Messages request that keeps every unmodelled field on the wire."""

    model: str | None
    messages: tuple[Message, ...] = field(repr=False)
    system: tuple[ContentBlock, ...] = field(default=(), repr=False)
    options: Mapping[str, Any] = field(default_factory=dict, repr=False)
    unmodelled: Mapping[str, Any] = field(default_factory=dict, repr=False)
    source_body: bytes = field(default=b"", repr=False)

    @property
    def unmodelled_fields(self) -> tuple[str, ...]:
        return tuple(sorted(self.unmodelled))

    def to_native_body(self) -> bytes:
        """Native pass-through returns the exact inbound bytes.

        The native Anthropic path never round-trips through this representation
        on the wire; the representation exists so a converted path can prove what
        it would otherwise lose.
        """

        return self.source_body

    def reserialize_body(self) -> bytes:
        """Rebuild the body from the representation, including unmodelled fields.

        Tests compare this against :meth:`to_native_body` to prove the
        representation kept every field. It is not a wire path: the native path
        forwards the original bytes.
        """

        payload: dict[str, Any] = {}
        if "model" in self.options:
            payload["model"] = self.model
        if self.messages:
            payload["messages"] = [message.to_json() for message in self.messages]
        if self.system:
            payload["system"] = [block.to_json() for block in self.system]
        for name, value in self.options.items():
            if name != "model":
                payload[name] = value
        for name, value in self.unmodelled.items():
            payload[name] = value
        return _dump(payload)

    def to_chat_request(self) -> Adapted | NotForwardable:
        return _to_chat_request(self)

    def to_responses_request(self) -> Adapted | NotForwardable:
        """Chat conversion, then the existing Chat->Responses repository seam."""

        converted = _to_chat_request(self)
        if isinstance(converted, NotForwardable):
            return converted
        try:
            body = chat_completions_request_to_responses_body(converted.body)
        except UnsupportedProtocolTranslationError as exc:
            refused = tuple(sorted(set(_load(converted.body)) - RESPONSES_SEAM_CHAT_FIELDS))
            return NotForwardable(reason=str(exc), fields=refused or ("messages",))
        return Adapted(body=body, adaptations=converted.adaptations)


class _Declared:
    """Accumulates adaptations so no conversion path can drop a field silently."""

    def __init__(self) -> None:
        self.adaptations: list[Adaptation] = []
        self.unrepresentable: list[str] = []

    def adapt(self, field_name: str, policy: str, detail: str) -> None:
        self.adaptations.append(Adaptation(field=field_name, policy=policy, detail=detail))

    def refuse(self, *fields: str) -> None:
        self.unrepresentable.extend(fields)

    def result(self, body: bytes) -> Adapted | NotForwardable:
        if self.unrepresentable:
            return NotForwardable(
                reason="unsupported_for_chat_conversion",
                fields=tuple(sorted(set(self.unrepresentable))),
            )
        return Adapted(body=body, adaptations=tuple(self.adaptations))


def parse_request(body: bytes | str) -> AnthropicRequest:
    payload = _load(body)
    raw_messages = payload.get("messages")
    if raw_messages is not None and not isinstance(raw_messages, list):
        raise ValueError("Anthropic Messages 'messages' must be an array.")
    messages = tuple(_message(item) for item in (raw_messages or []))
    system = _system_blocks(payload.get("system"))
    modelled = (
        MODELLED_REQUEST_FIELDS
        | set(OPTION_FIELDS_WITHOUT_CHAT_EQUIVALENT)
        | SAFETY_BEARING_FIELDS
        | {"output_config"}
    )
    options = {
        name: payload[name]
        for name in modelled
        if name in payload and name not in {"messages", "system"}
    }
    unmodelled = {
        name: value
        for name, value in payload.items()
        if name not in modelled
    }
    model = payload.get("model")
    if model is not None and not isinstance(model, str):
        raise ValueError("Anthropic Messages 'model' must be a string.")
    return AnthropicRequest(
        model=model,
        messages=messages,
        system=system,
        options=options,
        unmodelled=unmodelled,
        source_body=body.encode("utf-8") if isinstance(body, str) else body,
    )


def _message(item: Any) -> Message:
    if not isinstance(item, Mapping):
        raise ValueError("Anthropic Messages entries must be objects.")
    role = item.get("role")
    if not isinstance(role, str) or not role:
        raise ValueError("Anthropic Messages entries need a string role.")
    content = item.get("content")
    if isinstance(content, str):
        blocks = (ContentBlock(type="text", data={"type": "text", "text": content}),)
    elif isinstance(content, list):
        blocks = tuple(_block(entry) for entry in content)
    else:
        raise ValueError(f"Anthropic Messages content must be text or blocks, not {type(content).__name__}.")
    extra = {name: value for name, value in item.items() if name not in {"role", "content"}}
    return Message(role=role, content=blocks, extra=extra)


def _block(entry: Any) -> ContentBlock:
    if not isinstance(entry, Mapping):
        raise ValueError("Anthropic Messages content blocks must be objects.")
    block_type = entry.get("type")
    if not isinstance(block_type, str) or not block_type:
        raise ValueError("Anthropic Messages content blocks need a string type.")
    return ContentBlock(type=block_type, data=dict(entry))


def _system_blocks(value: Any) -> tuple[ContentBlock, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (ContentBlock(type="text", data={"type": "text", "text": value}),)
    if isinstance(value, list):
        return tuple(_block(entry) for entry in value)
    raise ValueError("Anthropic Messages 'system' must be text or blocks.")


def _to_chat_request(request: AnthropicRequest) -> Adapted | NotForwardable:
    declared = _Declared()
    chat: dict[str, Any] = {"model": request.model}
    messages: list[dict[str, Any]] = []

    if request.system:
        system_text = _system_text(request.system, declared)
        if system_text:
            messages.append({"role": "system", "content": system_text})

    pending_tool_results: list[dict[str, Any]] = []

    def flush_tool_results() -> None:
        # Anthropic carries several tool results in one user message; Chat
        # Completions has one role:"tool" message per result. Order is preserved.
        for result in pending_tool_results:
            messages.append({"role": "tool", **result})
        pending_tool_results.clear()

    for index, message in enumerate(request.messages):
        label = f"messages[{index}]"
        for name in sorted(message.extra):
            # ADR-0001: an unmodelled message-level key is refused by name.
            declared.refuse(f"{label}.{name}")
        if message.role == "user":
            results = [block for block in message.content if block.type == _TOOL_RESULT_BLOCK]
            if results:
                for position, block in enumerate(message.content):
                    if block.type != _TOOL_RESULT_BLOCK:
                        continue
                    pending_tool_results.append(
                        _tool_result_message(block, declared, label=f"{label}.content[{position}]")
                    )
                for position, block in enumerate(message.content):
                    if block.type != _TOOL_RESULT_BLOCK:
                        declared.refuse(f"user_content_after_tool_result:{block.type}")
                continue
            flush_tool_results()
            messages.append({"role": "user", "content": _chat_user_content(message.content, declared, label=label)})
            continue
        flush_tool_results()
        if message.role == "assistant":
            messages.append(_chat_assistant_message(message.content, declared, label=label))
            continue
        if message.role == "system":
            # Mid-conversation system entries are a Claude Code beta feature.
            declared.adapt(
                "messages[].role=system",
                "mid_conversation_system_flattened_to_chat_system",
                "Chat Completions has no mid-conversation system role.",
            )
            messages.append({"role": "system", "content": _chat_text(message.content, declared, label=label)})
            continue
        declared.refuse(f"message_role:{message.role}")
    flush_tool_results()

    for name, value in request.options.items():
        if name == "model":
            continue
        if name in SAFETY_BEARING_FIELDS:
            # Fail closed: never adapt an explicitly requested safety control away.
            declared.refuse(name)
            continue
        if name == "output_config":
            _map_output_config(value, chat, declared)
            continue
        if name == "thinking":
            _declare_thinking(value, declared)
            continue
        if name in OPTION_FIELDS_WITHOUT_CHAT_EQUIVALENT:
            declared.adapt(name, OPTION_FIELDS_WITHOUT_CHAT_EQUIVALENT[name], "no Chat Completions equivalent")
            continue
        if name in {"tools", "tool_choice"}:
            continue
        if name == "stop_sequences":
            chat["stop"] = value
            declared.adapt(
                "stop_sequences",
                "anthropic_stop_sequences_mapped_to_chat_stop",
                "Chat Completions names the same stop list 'stop'.",
            )
            continue
        chat[name] = value
    for name in request.unmodelled_fields:
        # ADR-0001: an unmodelled field makes the request non-forwardable by
        # name. Known options with no Chat equivalent are adapted above instead.
        declared.refuse(name)

    tools = request.options.get("tools")
    if tools:
        chat["tools"] = _chat_tools(tools, declared)
    if "tool_choice" in request.options:
        chat["tool_choice"] = _chat_tool_choice(request.options["tool_choice"], declared)

    if not messages:
        declared.refuse("messages")
    chat["messages"] = messages
    return declared.result(_dump(chat))


def _declare_block_extras(
    block: ContentBlock, allowed: set[str], *, label: str, declared: _Declared
) -> None:
    """Every key a converted path does not carry is declared by name."""

    for name in sorted(set(block.data) - allowed):
        declared.adapt(
            f"{label}.{name}",
            "content_block_field_dropped_for_chat",
            "Chat Completions has no equivalent for this content-block field.",
        )


def _map_output_config(value: Any, chat: dict[str, Any], declared: _Declared) -> None:
    """Map effort through the Chat seam instead of dropping the whole field.

    ``reasoning_effort`` reaches Responses through the existing
    ``protocol_translation`` seam, so a plain effort selector is preserved
    end to end. Anything else in ``output_config`` fails closed.
    """

    if isinstance(value, Mapping):
        extra = sorted(set(value) - {"effort"})
        if extra:
            declared.refuse(*(f"output_config.{name}" for name in extra))
            return
        effort = value.get("effort")
        if isinstance(effort, str) and effort:
            chat["reasoning_effort"] = effort
            declared.adapt(
                "output_config.effort",
                "output_config_effort_mapped_to_chat_reasoning_effort",
                "Chat reasoning_effort carries the same selector to the Responses seam.",
            )
            return
    declared.refuse("output_config")


def _declare_thinking(value: Any, declared: _Declared) -> None:
    """Name exactly what happens to an explicit thinking control; never silent."""

    kind = value.get("type") if isinstance(value, Mapping) else None
    if kind == "disabled":
        policy = "thinking_disabled_not_representable_in_chat"
    elif kind == "adaptive":
        policy = "adaptive_thinking_not_representable_in_chat"
    else:
        policy = "thinking_budget_not_representable_in_chat"
    declared.adapt("thinking", policy, "Chat Completions has no equivalent thinking control.")


def _system_text(blocks: tuple[ContentBlock, ...], declared: _Declared) -> str:
    parts: list[str] = []
    for index, block in enumerate(blocks):
        for name in sorted(set(block.data) - {"type", "text"}):
            declared.adapt(
                f"system[{index}].{name}",
                "system_block_metadata_dropped_for_chat",
                "Chat Completions system content is plain text.",
            )
        text = block.data.get("text")
        if isinstance(text, str):
            parts.append(text)
        else:
            declared.refuse(f"system[{index}]")
    if len(blocks) > 1:
        declared.adapt(
            "system",
            "system_block_array_joined_for_chat",
            f"{len(blocks)} ordered system blocks become one Chat system message.",
        )
    return "\n\n".join(parts)


def _chat_user_content(blocks: tuple[ContentBlock, ...], declared: _Declared, *, label: str) -> Any:
    parts: list[dict[str, Any]] = []
    for index, block in enumerate(blocks):
        block_label = f"{label}.content[{index}]"
        if block.type == "text":
            _declare_block_extras(block, {"type", "text"}, label=block_label, declared=declared)
            text = block.data.get("text")
            if not isinstance(text, str):
                declared.refuse(f"{block_label}.text")
                continue
            parts.append({"type": "text", "text": text})
            continue
        if block.type == "image":
            _declare_block_extras(block, {"type", "source"}, label=block_label, declared=declared)
            parts.append({"type": "image_url", "image_url": {"url": _image_url(block, index, declared)}})
            continue
        declared.refuse(f"user_content_block:{block.type}")
    if not parts:
        return ""
    if len(parts) == 1 and parts[0]["type"] == "text":
        return parts[0]["text"]
    return parts


def _image_url(block: ContentBlock, index: int, declared: _Declared) -> str:
    source = block.data.get("source")
    if isinstance(source, Mapping) and source.get("type") == "base64":
        media = source.get("media_type")
        data = source.get("data")
        if isinstance(media, str) and isinstance(data, str):
            declared.adapt(
                f"messages[].content[{index}].source",
                "base64_image_inlined_as_data_uri",
                "Chat Completions transports base64 images as a data URI.",
            )
            return f"data:{media};base64,{data}"
    if isinstance(source, Mapping) and source.get("type") == "url" and isinstance(source.get("url"), str):
        return str(source["url"])
    declared.refuse(f"messages[].content[{index}].source")
    return ""


def _chat_assistant_message(
    blocks: tuple[ContentBlock, ...], declared: _Declared, *, label: str
) -> dict[str, Any]:
    text_parts: list[str] = []
    thinking_parts: list[str] = []
    thinking_details: list[dict[str, Any]] = []
    tool_calls: list[dict[str, Any]] = []
    kinds: list[str] = []
    for index, block in enumerate(blocks):
        block_label = f"{label}.content[{index}]"
        if block.type == "text":
            kinds.append("content")
            _declare_block_extras(block, {"type", "text"}, label=block_label, declared=declared)
            text = block.data.get("text")
            if not isinstance(text, str):
                declared.refuse(f"{block_label}.text")
                continue
            text_parts.append(text)
        elif block.type == "thinking":
            kinds.append("content")
            _declare_block_extras(
                block, {"type", "thinking", "signature"}, label=block_label, declared=declared
            )
            thinking = block.data.get("thinking")
            signature = block.data.get("signature")
            if not isinstance(thinking, str):
                declared.refuse(f"{block_label}.thinking")
                continue
            thinking_parts.append(thinking)
            detail: dict[str, Any] = {"type": "anthropic_thinking", "thinking": thinking}
            if isinstance(signature, str) and signature:
                detail["signature"] = signature
            thinking_details.append(detail)
        elif block.type == "redacted_thinking":
            declared.refuse(f"assistant_content_block:{block.type}")
        elif block.type == _TOOL_USE_BLOCK:
            kinds.append("tool_call")
            _declare_block_extras(
                block, {"type", "id", "name", "input"}, label=block_label, declared=declared
            )
            tool_calls.append(_chat_tool_call(block, index, declared))
        else:
            declared.refuse(f"assistant_content_block:{block.type}")
    # Chat stores assistant text and tool calls in separate fields, so an
    # interleaved block order cannot survive; name the loss instead of claiming
    # ordered-block preservation on the converted path.
    first_tool_call = kinds.index("tool_call") if "tool_call" in kinds else len(kinds)
    if "content" in kinds[first_tool_call:]:
        declared.adapt(
            f"{label}.content",
            "assistant_block_interleaving_flattened_for_chat",
            f"observed order {kinds} becomes text-then-tool_calls in Chat Completions.",
        )
    message: dict[str, Any] = {"role": "assistant", "content": "".join(text_parts) or None}
    if thinking_details:
        message["reasoning_content"] = "\n".join(thinking_parts)
        message["reasoning_details"] = thinking_details
    if tool_calls:
        message["tool_calls"] = tool_calls
    return message


def _chat_tool_call(block: ContentBlock, index: int, declared: _Declared) -> dict[str, Any]:
    tool_id = block.data.get("id")
    name = block.data.get("name")
    tool_input = block.data.get("input")
    valid = True
    if not isinstance(tool_id, str) or not tool_id:
        declared.refuse(f"messages[].content[{index}].id")
        valid = False
    if not isinstance(name, str) or not name:
        declared.refuse(f"messages[].content[{index}].name")
        valid = False
    if not isinstance(tool_input, Mapping):
        declared.refuse(f"messages[].content[{index}].input")
        valid = False
    if not valid:
        return {}
    return {
        "id": tool_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(dict(tool_input), ensure_ascii=True, separators=(",", ":")),
        },
    }


def _tool_result_message(block: ContentBlock, declared: _Declared, *, label: str) -> dict[str, Any]:
    tool_use_id = block.data.get("tool_use_id")
    if not isinstance(tool_use_id, str) or not tool_use_id:
        declared.refuse(f"{label}.tool_use_id")
    _declare_block_extras(
        block, {"type", "tool_use_id", "content", "is_error"}, label=label, declared=declared
    )
    content = block.data.get("content")
    if isinstance(content, list):
        text = _chat_text(tuple(_block(entry) for entry in content), declared, label=label)
    elif isinstance(content, str):
        text = content
    else:
        declared.refuse(f"{label}.content")
        text = ""
    if block.data.get("is_error") is True:
        declared.adapt(
            f"{label}.is_error",
            "tool_error_prefixed_in_chat_content",
            "Chat Completions has no tool-result error flag; prefix the content.",
        )
        text = f"[tool_error] {text}"
    return {"tool_call_id": tool_use_id, "content": text}


def _chat_text(blocks: tuple[ContentBlock, ...], declared: _Declared, *, label: str) -> str:
    parts: list[str] = []
    for index, block in enumerate(blocks):
        if block.type not in _TEXT_LIKE_BLOCKS:
            declared.refuse(f"text_content_block:{block.type}")
            continue
        _declare_block_extras(
            block, {"type", "text", "thinking", "signature"},
            label=f"{label}.content[{index}]", declared=declared,
        )
        value = block.data.get("text") if block.type == "text" else block.data.get("thinking")
        if not isinstance(value, str):
            declared.refuse(f"{label}.content[{index}].text")
            continue
        parts.append(value)
    return "".join(parts)


def _chat_tools(tools: Any, declared: _Declared) -> list[dict[str, Any]]:
    if not isinstance(tools, list):
        declared.refuse("tools")
        return []
    converted: list[dict[str, Any]] = []
    for index, tool in enumerate(tools):
        if not isinstance(tool, Mapping):
            declared.refuse(f"tools[{index}]")
            continue
        name = tool.get("name")
        if not isinstance(name, str) or not name:
            declared.refuse(f"tools[{index}].name")
            continue
        schema = tool.get("input_schema")
        if not isinstance(schema, Mapping):
            declared.refuse(f"tools[{index}].input_schema")
            schema = {"type": "object", "properties": {}}
        function: dict[str, Any] = {"name": name, "parameters": dict(schema)}
        if isinstance(tool.get("description"), str):
            function["description"] = tool["description"]
        converted.append({"type": "function", "function": function})
        extra = sorted(set(tool) - {"name", "description", "input_schema"})
        if extra:
            declared.adapt(
                f"tools[{index}].{extra[0]}",
                "client_tool_metadata_dropped_for_chat",
                "Chat Completions tool entries carry no Anthropic tool metadata.",
            )
    return converted


def _chat_tool_choice(value: Any, declared: _Declared) -> Any:
    if isinstance(value, Mapping):
        choice_type = value.get("type")
        if choice_type == "auto":
            return "auto"
        if choice_type == "any":
            return "required"
        if choice_type == "tool" and isinstance(value.get("name"), str) and value["name"]:
            return {"type": "function", "function": {"name": value["name"]}}
        if choice_type == "none":
            return "none"
    declared.refuse("tool_choice")
    return None


def _dump(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


def _load(body: bytes | str) -> dict[str, Any]:
    try:
        payload = json.loads(body.decode("utf-8-sig") if isinstance(body, bytes) else body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Anthropic Messages body is not valid JSON.") from exc
    if not isinstance(payload, dict):
        raise ValueError("Anthropic Messages body must be a JSON object.")
    return payload


UPSTREAM_FORMATS = frozenset({"responses", "chat_completions", "anthropic_messages"})
MAX_BUFFERED_RESPONSE_BYTES = 16 * 1024 * 1024
_MISSING = object()


def prepare_upstream_request(body: bytes, upstream_format: str) -> Adapted | NotForwardable:
    """Prepare one Claude request for an explicitly selected upstream format."""

    try:
        request = parse_request(body)
    except ValueError:
        return NotForwardable("invalid_anthropic_request", ("request",))
    selected = str(upstream_format or "").strip().lower()
    if selected == "anthropic_messages":
        return Adapted(body=request.to_native_body())
    if selected == "responses":
        return request.to_responses_request()
    if selected == "chat_completions":
        return request.to_chat_request()
    return NotForwardable("unsupported_upstream_format", (selected or "missing",))


def response_refusal(reason: str, field_name: str = "response") -> NotForwardable:
    return NotForwardable(reason, (field_name,))


def content_type_is_sse(content_type: str) -> bool:
    return content_type.lower().split(";", 1)[0].strip() == "text/event-stream"


def _error_type(status: int, raw_type: Any = None) -> str:
    known = {
        400: "invalid_request_error",
        401: "authentication_error",
        403: "permission_error",
        404: "not_found_error",
        413: "request_too_large",
        429: "rate_limit_error",
        529: "overloaded_error",
    }
    safe_types = {
        "api_error",
        "authentication_error",
        "invalid_request_error",
        "not_found_error",
        "overloaded_error",
        "permission_error",
        "rate_limit_error",
        "request_too_large",
        "timeout_error",
    }
    if status in known:
        return known[status]
    return raw_type if isinstance(raw_type, str) and raw_type in safe_types else "api_error"


def _error_message(_payload: Mapping[str, Any] | None, status: int, default: str) -> str:
    # Upstream error text can contain credentials, URLs, or prompt content.
    return f"{default} (status {status})"


def anthropic_error_body(status: int, payload: Mapping[str, Any] | None, *, default: str) -> bytes:
    raw = payload.get("error") if isinstance(payload, Mapping) else None
    raw_type = raw.get("type") if isinstance(raw, Mapping) else None
    value = {
        "type": "error",
        "error": {
            "type": _error_type(status, raw_type),
            "message": _error_message(payload, status, default),
        },
    }
    return _dump(value)


def _sse_record(event_name: str, payload: Mapping[str, Any]) -> bytes:
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    return f"event: {event_name}\n" f"data: {encoded}\n\n".encode("utf-8")


def anthropic_error_sse(status: int, payload: Mapping[str, Any] | None, *, default: str) -> bytes:
    return _sse_record(
        "error",
        json.loads(anthropic_error_body(status, payload, default=default).decode("utf-8")),
    )


def _usage_for_anthropic(
    value: Any,
    declared: list[Adaptation],
    *,
    source: str,
) -> dict[str, int]:
    """Keep only truthful Anthropic counts and name every omitted detail."""

    if value is None:
        declared.append(
            Adaptation(
                "usage",
                "usage_unavailable",
                f"{source} supplied no supported token counts; none are synthesized.",
            )
        )
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{source} usage is not an object")

    def count(name: str) -> int | None:
        if name not in value:
            return None
        item = value[name]
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            raise ValueError(f"{source} usage field {name} is invalid")
        return item

    input_tokens = count("input_tokens")
    if input_tokens is None:
        input_tokens = count("prompt_tokens")
    output_tokens = count("output_tokens")
    if output_tokens is None:
        output_tokens = count("completion_tokens")
    total_tokens = count("total_tokens")
    if total_tokens is not None and input_tokens is not None and output_tokens is not None:
        if total_tokens != input_tokens + output_tokens:
            raise ValueError(f"{source} usage total_tokens disagrees with input/output counts")
        declared.append(
            Adaptation(
                "usage.total_tokens",
                "usage_detail_not_representable",
                "Anthropic Messages exposes input_tokens and output_tokens only.",
            )
        )
    elif total_tokens is not None:
        declared.append(
            Adaptation(
                "usage.total_tokens",
                "usage_detail_not_representable",
                "A total without both base counts cannot be truthfully split.",
            )
        )

    known = {
        "input_tokens",
        "output_tokens",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
    }
    for name in value:
        if name not in known:
            declared.append(
                Adaptation(
                    f"usage.{name}",
                    "usage_detail_not_representable",
                    f"{source} usage detail has no verified Anthropic Messages field.",
                )
            )
    result: dict[str, int] = {}
    if input_tokens is not None:
        result["input_tokens"] = input_tokens
    if output_tokens is not None:
        result["output_tokens"] = output_tokens
    if not result:
        declared.append(
            Adaptation(
                "usage",
                "usage_unavailable",
                f"{source} supplied no supported token counts; none are synthesized.",
            )
        )
    return result


def _responses_output_to_anthropic(
    payload: Mapping[str, Any],
    *,
    status: int,
) -> tuple[bytes, tuple[Adaptation, ...]] | AdaptedResponse | NotForwardable:
    declared: list[Adaptation] = []
    response_status = payload.get("status")
    if payload.get("error") is not None or response_status == "failed":
        effective_status = status if status >= 400 else 502
        return AdaptedResponse(
            body=anthropic_error_body(effective_status, payload, default="Upstream response failed"),
            status=effective_status,
            adaptations=(
                Adaptation(
                    "response.error",
                    "upstream_error_mapped_to_anthropic_error",
                    "An upstream failure is never presented as a successful message.",
                ),
            ),
        )
    if response_status not in ("completed", "incomplete"):
        return response_refusal("unsupported_upstream_response", "response.status")
    if "output" not in payload or not isinstance(payload["output"], list):
        return response_refusal("unsupported_upstream_response", "response.output")
    output = payload["output"]
    if response_status == "completed" and not output:
        return response_refusal("unsupported_upstream_response", "response.output")

    blocks: list[dict[str, Any]] = []
    for index, raw_item in enumerate(output):
        if not isinstance(raw_item, Mapping):
            return response_refusal("unsupported_upstream_response", f"response.output[{index}]")
        item_type = raw_item.get("type")
        if item_type == "message":
            if "content" not in raw_item:
                return response_refusal("unsupported_upstream_response", f"response.output[{index}].content")
            content = raw_item["content"]
            if not isinstance(content, list):
                return response_refusal("unsupported_upstream_response", f"response.output[{index}].content")
            for content_index, raw_part in enumerate(content):
                if not isinstance(raw_part, Mapping):
                    return response_refusal(
                        "unsupported_upstream_response",
                        f"response.output[{index}].content[{content_index}]",
                    )
                part_type = raw_part.get("type")
                if part_type not in {"output_text", "text"}:
                    return response_refusal(
                        "unsupported_upstream_response",
                        f"response.output[{index}].content[{content_index}].type",
                    )
                text = raw_part.get("text")
                if not isinstance(text, str):
                    return response_refusal(
                        "unsupported_upstream_response",
                        f"response.output[{index}].content[{content_index}].text",
                    )
                if raw_part.get("annotations") not in (None, [], {}):
                    declared.append(
                        Adaptation(
                            f"response.output[{index}].content[{content_index}].annotations",
                            "response_annotations_omitted_for_anthropic",
                            "Anthropic Messages text blocks have no verified citation field.",
                        )
                    )
                blocks.append({"type": "text", "text": text})
            continue
        if item_type == "function_call":
            call_id = raw_item.get("call_id")
            name = raw_item.get("name")
            arguments = raw_item.get("arguments")
            if not isinstance(call_id, str) or not call_id:
                return response_refusal("unpaired_tool_call", f"response.output[{index}].call_id")
            if not isinstance(name, str) or not name:
                return response_refusal("unsupported_upstream_response", f"response.output[{index}].name")
            if not isinstance(arguments, str):
                return response_refusal("unsupported_upstream_response", f"response.output[{index}].arguments")
            try:
                tool_input = json.loads(arguments)
            except json.JSONDecodeError:
                return response_refusal("unsupported_upstream_response", f"response.output[{index}].arguments")
            if not isinstance(tool_input, Mapping):
                return response_refusal("unsupported_upstream_response", f"response.output[{index}].arguments")
            blocks.append({"type": "tool_use", "id": call_id, "name": name, "input": dict(tool_input)})
            continue
        declared.append(
            Adaptation(
                f"response.output[{index}].type",
                "responses_output_item_omitted_for_anthropic",
                f"Anthropic Messages has no equivalent for Responses '{item_type}'.",
            )
        )
    if response_status == "completed" and not blocks:
        return response_refusal("unsupported_upstream_response", "response.output")

    usage = _usage_for_anthropic(payload.get("usage"), declared, source="Responses")
    response_id = payload.get("id")
    if not isinstance(response_id, str) or not response_id:
        return response_refusal("unsupported_upstream_response", "response.id")
    model = payload.get("model")
    if not isinstance(model, str) or not model:
        return response_refusal("unsupported_upstream_response", "response.model")
    stop_reason = "tool_use" if any(block["type"] == "tool_use" for block in blocks) else (
        "max_tokens" if response_status == "incomplete" else "end_turn"
    )
    message = {
        "id": response_id,
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": blocks,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": usage,
    }
    return _dump(message), tuple(declared)


def adapt_upstream_response(
    upstream_format: str,
    body: bytes,
    *,
    status: int = 200,
    content_type: str = "application/json",
    cancelled: bool = False,
) -> AdaptedResponse | NotForwardable:
    """Convert one upstream JSON/SSE response to Anthropic Messages.

    ``anthropic_messages`` is intentionally a byte-for-byte pass-through.  The
    other two formats use existing Responses/Chat converters before the small
    Anthropic response adapter here; unsupported semantics return a refusal.
    """

    selected = str(upstream_format or "").strip().lower()
    if selected not in UPSTREAM_FORMATS:
        return response_refusal("unsupported_upstream_format", selected or "missing")
    if cancelled:
        cancelled_status = 499
        if content_type_is_sse(content_type):
            return AdaptedResponse(
                body=anthropic_error_sse(cancelled_status, None, default="Upstream stream cancelled"),
                status=cancelled_status,
                content_type="text/event-stream",
                adaptations=(
                    Adaptation(
                        "stream.cancellation",
                        "cancelled_before_terminal",
                        "No synthetic message_stop or usage is emitted.",
                    ),
                ),
            )
        return AdaptedResponse(
            body=anthropic_error_body(cancelled_status, None, default="Upstream request cancelled"),
            status=cancelled_status,
            adaptations=(
                Adaptation(
                    "request.cancellation",
                    "cancelled_before_success",
                    "No synthetic message or usage is emitted.",
                ),
            ),
        )
    if selected == "anthropic_messages":
        return AdaptedResponse(body=body, status=status, content_type=content_type)
    if content_type_is_sse(content_type):
        return adapt_upstream_stream(selected, (body,), status=status, content_type=content_type)
    try:
        payload = decode_protocol_json(body)
    except (UnicodeError, json.JSONDecodeError, UnsupportedProtocolTranslationError):
        return response_refusal("invalid_upstream_response", "json")
    if not isinstance(payload, Mapping):
        return response_refusal("invalid_upstream_response", "json")
    if status >= 400:
        return AdaptedResponse(
            body=anthropic_error_body(status, payload, default="Upstream request failed"),
            status=status,
            adaptations=(
                Adaptation(
                    "upstream.error",
                    "upstream_error_mapped_to_anthropic_error",
                    "An upstream failure is never presented as a successful message.",
                ),
            ),
        )
    try:
        if selected == "chat_completions":
            if payload.get("error") is None:
                if not isinstance(payload.get("id"), str) or not payload.get("id"):
                    return response_refusal("unsupported_upstream_response", "chat.id")
                if not isinstance(payload.get("model"), str) or not payload.get("model"):
                    return response_refusal("unsupported_upstream_response", "chat.model")
                choices = payload.get("choices")
                if not isinstance(choices, list) or not choices:
                    return response_refusal("unsupported_upstream_response", "chat.choices")
                for index, choice in enumerate(choices):
                    if not isinstance(choice, Mapping):
                        return response_refusal("unsupported_upstream_response", f"chat.choices[{index}]")
                    message = choice.get("message")
                    if not isinstance(message, Mapping) or message.get("role") != "assistant":
                        return response_refusal("unsupported_upstream_response", f"chat.choices[{index}].message.role")
                    if choice.get("finish_reason") not in {"stop", "length", "tool_calls", "function_call"}:
                        return response_refusal("unsupported_upstream_response", f"chat.choices[{index}].finish_reason")
                    content = message.get("content")
                    if content is not None and not isinstance(content, (str, list)):
                        return response_refusal("unsupported_upstream_response", f"chat.choices[{index}].message.content")
                    tool_calls = message.get("tool_calls")
                    if tool_calls is not None and not isinstance(tool_calls, list):
                        return response_refusal("unsupported_upstream_response", f"chat.choices[{index}].message.tool_calls")
            converted = chat_completion_to_response_body(body, repair=False)
            result = _responses_output_to_anthropic(json.loads(converted), status=status)
            if isinstance(result, tuple):
                converted_body, adaptations = result
                return AdaptedResponse(
                    body=converted_body,
                    status=status if status >= 400 else 200,
                    adaptations=(
                        Adaptation(
                            "chat_response",
                            "chat_response_via_existing_responses_adapter",
                            "Chat response conversion reuses protocol_translation before Messages adaptation.",
                        ),
                        *adaptations,
                    ),
                )
            if isinstance(result, AdaptedResponse):
                return AdaptedResponse(
                    body=result.body,
                    status=result.status,
                    content_type=result.content_type,
                    adaptations=(
                        Adaptation(
                            "chat_response",
                            "chat_error_via_existing_responses_adapter",
                            "Chat error conversion preserves terminal failure semantics.",
                        ),
                        *result.adaptations,
                    ),
                )
            return result
        result = _responses_output_to_anthropic(payload, status=status)
    except (TypeError, ValueError, UnsupportedProtocolTranslationError, json.JSONDecodeError):
        return response_refusal("unsupported_upstream_response", "response")
    if isinstance(result, tuple):
        converted_body, adaptations = result
        return AdaptedResponse(
            body=converted_body,
            status=status if status >= 400 else 200,
            adaptations=adaptations,
        )
    return result


def _decode_sse_frames(chunks: Iterable[bytes]) -> tuple[list[Mapping[str, Any] | str], bytes] | NotForwardable:
    assembler = SseEventAssembler()
    frames = []
    raw_parts: list[bytes] = []
    try:
        for chunk in chunks:
            if not isinstance(chunk, bytes):
                return response_refusal("invalid_upstream_stream", "sse.bytes")
            raw_parts.append(chunk)
            frames.extend(assembler.feed(chunk))
        termination = assembler.finish()
        frames.extend(termination.events)
    except (ValueError, RuntimeError):
        return response_refusal("invalid_upstream_stream", "sse")
    if termination.disposition != "complete":
        return response_refusal("incomplete_upstream_stream", "sse")
    payloads: list[Mapping[str, Any] | str] = []
    for frame in frames:
        data = frame.data.decode("utf-8", errors="strict")
        if data == "[DONE]":
            payloads.append("[DONE]")
            continue
        if not data:
            continue
        try:
            payload = decode_protocol_json(data)
        except (UnicodeError, json.JSONDecodeError, UnsupportedProtocolTranslationError):
            return response_refusal("invalid_upstream_stream", "sse.data")
        if not isinstance(payload, Mapping):
            return response_refusal("invalid_upstream_stream", "sse.data")
        payloads.append(payload)
    return payloads, b"".join(raw_parts)


class ChatToAnthropicEmitter:
    """Emit Anthropic SSE frames as each Chat Completions chunk arrives."""

    def __init__(
        self,
        *,
        terminal_usage: Any = _MISSING,
        expected_id: str | None = None,
        expected_model: str | None = None,
        allow_empty_text: bool = False,
        usage_source: str = "Chat Completions",
    ) -> None:
        self.declared: list[Adaptation] = []
        self.expected_id = expected_id
        self.expected_model = expected_model
        self.allow_empty_text = allow_empty_text
        self.usage_source = usage_source
        self.response_id: str | None = None
        self.model: str | None = None
        self.message_started = False
        self.text_index: int | None = None
        self.next_index = 0
        self.tools: dict[int, dict[str, Any]] = {}
        self.terminal_reason: str | None = None
        self.empty_text_seen = False
        self.usage: dict[str, int] = {}
        self.usage_seen = False
        self.saw_done = False
        self._frames: list[bytes] = []
        if terminal_usage is not _MISSING:
            self.usage = _usage_for_anthropic(terminal_usage, self.declared, source="Responses")
            self.usage_seen = True

    def _start_message(self) -> None:
        if self.message_started or not self.response_id or not self.model:
            return
        self.message_started = True
        self._frames.append(
            _sse_record(
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": self.response_id,
                        "type": "message",
                        "role": "assistant",
                        "model": self.model,
                        "content": [],
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {},
                    },
                },
            )
        )

    def _take(self) -> list[bytes]:
        frames = self._frames
        self._frames = []
        return frames

    def feed(self, chunk: Mapping[str, Any] | str) -> list[bytes] | NotForwardable:
        if chunk == "[DONE]":
            self.saw_done = True
            return []
        if not isinstance(chunk, Mapping):
            return response_refusal("unsupported_upstream_stream", "chat.chunk")
        chunk_id = chunk.get("id")
        chunk_model = chunk.get("model")
        if self.response_id is None:
            if not isinstance(chunk_id, str) or not chunk_id:
                return response_refusal("unsupported_upstream_stream", "chat.id")
            if not isinstance(chunk_model, str) or not chunk_model:
                return response_refusal("unsupported_upstream_stream", "chat.model")
            if self.expected_id is not None and chunk_id != self.expected_id:
                return response_refusal("unsupported_upstream_stream", "chat.id")
            if self.expected_model is not None and chunk_model != self.expected_model:
                return response_refusal("unsupported_upstream_stream", "chat.model")
            self.response_id = chunk_id
            self.model = chunk_model
        if "id" in chunk and (not isinstance(chunk_id, str) or not chunk_id or chunk_id != self.response_id):
            return response_refusal("unsupported_upstream_stream", "chat.id")
        if "model" in chunk and (
            not isinstance(chunk_model, str) or not chunk_model or chunk_model != self.model
        ):
            return response_refusal("unsupported_upstream_stream", "chat.model")
        if isinstance(chunk_id, str) and chunk_id:
            self.response_id = chunk_id
        if isinstance(chunk_model, str) and chunk_model:
            self.model = chunk_model
        self._start_message()
        raw_usage = chunk.get("usage")
        if raw_usage is not None and not self.usage_seen:
            self.usage_seen = True
            try:
                self.usage = _usage_for_anthropic(raw_usage, self.declared, source=self.usage_source)
            except ValueError:
                return response_refusal("unsupported_upstream_usage", "chat.usage")
        choices = chunk.get("choices")
        if choices is None:
            return self._take()
        if not isinstance(choices, list):
            return response_refusal("unsupported_upstream_stream", "chat.choices")
        for choice in choices:
            if not isinstance(choice, Mapping):
                return response_refusal("unsupported_upstream_stream", "chat.choice")
            raw_delta = choice.get("delta", {})
            if not isinstance(raw_delta, Mapping):
                return response_refusal("unsupported_upstream_stream", "chat.delta")
            if any(
                name in raw_delta
                for name in (
                    "reasoning",
                    "reasoning_content",
                    "reasoning_details",
                    "refusal",
                    "audio",
                    "annotations",
                )
            ):
                return response_refusal("unsupported_upstream_stream", "chat.delta")
            content = raw_delta.get("content")
            if content is not None and not isinstance(content, str):
                return response_refusal("unsupported_upstream_stream", "chat.delta.content")
            if isinstance(content, str) and "content" in raw_delta and not content:
                self.empty_text_seen = True
            if isinstance(content, str) and content:
                if self.text_index is None:
                    self.text_index = self.next_index
                    self.next_index += 1
                    self._frames.append(
                        _sse_record(
                            "content_block_start",
                            {
                                "type": "content_block_start",
                                "index": self.text_index,
                                "content_block": {"type": "text", "text": ""},
                            },
                        )
                    )
                self._frames.append(
                    _sse_record(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": self.text_index,
                            "delta": {"type": "text_delta", "text": content},
                        },
                    )
                )
            raw_tools = raw_delta.get("tool_calls")
            if raw_tools is not None:
                if not isinstance(raw_tools, list):
                    return response_refusal("unsupported_upstream_stream", "chat.tool_calls")
                for fallback_index, raw_tool in enumerate(raw_tools):
                    if not isinstance(raw_tool, Mapping):
                        return response_refusal("unsupported_upstream_stream", "chat.tool_calls")
                    tool_index = raw_tool.get("index", fallback_index)
                    if not isinstance(tool_index, int):
                        return response_refusal("unsupported_upstream_stream", "chat.tool_calls.index")
                    function = raw_tool.get("function", {})
                    if not isinstance(function, Mapping):
                        return response_refusal("unsupported_upstream_stream", "chat.tool_calls.function")
                    state = self.tools.get(tool_index)
                    call_id = raw_tool.get("id")
                    name = function.get("name")
                    if state is None:
                        if not isinstance(call_id, str) or not call_id or not isinstance(name, str) or not name:
                            return response_refusal("unpaired_tool_call", "chat.tool_calls")
                        state = {"id": call_id, "name": name, "index": self.next_index, "arguments": []}
                        self.next_index += 1
                        self.tools[tool_index] = state
                        self._frames.append(
                            _sse_record(
                                "content_block_start",
                                {
                                    "type": "content_block_start",
                                    "index": state["index"],
                                    "content_block": {
                                        "type": "tool_use",
                                        "id": call_id,
                                        "name": name,
                                        "input": {},
                                    },
                                },
                            )
                        )
                    elif call_id not in (None, "") and call_id != state["id"]:
                        return response_refusal("unpaired_tool_call", "chat.tool_calls.id")
                    elif name not in (None, "") and name != state["name"]:
                        return response_refusal("unsupported_upstream_stream", "chat.tool_calls.name")
                    arguments = function.get("arguments")
                    if arguments is not None and not isinstance(arguments, str):
                        return response_refusal("unsupported_upstream_stream", "chat.tool_calls.arguments")
                    if isinstance(arguments, str) and arguments:
                        state["arguments"].append(arguments)
                        self._frames.append(
                            _sse_record(
                                "content_block_delta",
                                {
                                    "type": "content_block_delta",
                                    "index": state["index"],
                                    "delta": {"type": "input_json_delta", "partial_json": arguments},
                                },
                            )
                        )
            finish = choice.get("finish_reason")
            if finish is not None:
                if finish == "stop":
                    self.terminal_reason = "end_turn"
                elif finish == "tool_calls":
                    self.terminal_reason = "tool_use"
                elif finish == "length":
                    self.terminal_reason = "max_tokens"
                else:
                    return response_refusal("unsupported_upstream_stream", "chat.finish_reason")
        return self._take()

    def finish(self, *, require_done: bool) -> list[bytes] | NotForwardable:
        if self.terminal_reason is None or (require_done and not self.saw_done):
            return response_refusal("incomplete_upstream_stream", "chat.terminal")
        if not self.response_id:
            return response_refusal("unsupported_upstream_stream", "chat.id")
        if not self.model:
            return response_refusal("unsupported_upstream_stream", "chat.model")
        if self.terminal_reason == "end_turn" and self.text_index is None and not self.tools:
            if not (self.allow_empty_text or self.empty_text_seen):
                return response_refusal("unsupported_upstream_stream", "chat.output")
            self.text_index = self.next_index
            self._frames.append(
                _sse_record(
                    "content_block_start",
                    {"type": "content_block_start", "index": self.text_index, "content_block": {"type": "text", "text": ""}},
                )
            )
        if not self.usage_seen:
            self.declared.append(
                Adaptation(
                    "usage",
                    "usage_unavailable",
                    "The upstream stream supplied no truthful token counts; none are synthesized.",
                )
            )
        for state in sorted(self.tools.values(), key=lambda value: value["index"]):
            self._frames.append(_sse_record("content_block_stop", {"type": "content_block_stop", "index": state["index"]}))
        if self.text_index is not None:
            self._frames.append(_sse_record("content_block_stop", {"type": "content_block_stop", "index": self.text_index}))
        self._frames.append(
            _sse_record(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": self.terminal_reason, "stop_sequence": None},
                    "usage": self.usage,
                },
            )
        )
        self._frames.append(_sse_record("message_stop", {"type": "message_stop"}))
        return self._take()


def _chat_chunks_to_anthropic_sse(
    chunks: list[Mapping[str, Any] | str],
    *,
    status: int,
    require_done: bool = False,
    terminal_usage: Any = _MISSING,
    expected_id: str | None = None,
    expected_model: str | None = None,
    allow_empty_text: bool = False,
) -> AdaptedResponse | NotForwardable:
    source_chunks = [chunk for chunk in chunks if chunk != "[DONE]"]
    if not source_chunks or not isinstance(source_chunks[0], Mapping):
        return response_refusal("unsupported_upstream_stream", "chat.identity")
    try:
        chat_stream_chunks_to_response_events(chunks)
    except (ValueError, UnsupportedProtocolTranslationError):
        return response_refusal("unsupported_upstream_stream", "chat.stream")
    try:
        emitter = ChatToAnthropicEmitter(
            terminal_usage=terminal_usage,
            expected_id=expected_id,
            expected_model=expected_model,
            allow_empty_text=allow_empty_text,
            usage_source="Responses" if terminal_usage is not _MISSING else "Chat Completions",
        )
    except ValueError:
        return response_refusal("unsupported_upstream_usage", "responses.usage")
    events: list[bytes] = []
    for chunk in chunks:
        fed = emitter.feed(chunk)
        if isinstance(fed, NotForwardable):
            return fed
        events.extend(fed)
    finished = emitter.finish(require_done=require_done)
    if isinstance(finished, NotForwardable):
        return finished
    events.extend(finished)
    return AdaptedResponse(
        body=b"".join(events),
        status=status,
        content_type="text/event-stream",
        adaptations=tuple(emitter.declared),
    )
def adapt_upstream_stream(
    upstream_format: str,
    chunks: Iterable[bytes],
    *,
    status: int = 200,
    content_type: str = "text/event-stream",
    cancelled: Callable[[], bool] | None = None,
    max_buffered_bytes: int = MAX_BUFFERED_RESPONSE_BYTES,
) -> AdaptedResponse | NotForwardable:
    """Adapt a finite SSE fixture while checking cancellation between chunks."""

    selected = str(upstream_format or "").strip().lower()
    if selected not in UPSTREAM_FORMATS:
        return response_refusal("unsupported_upstream_format", selected or "missing")
    if max_buffered_bytes <= 0:
        return response_refusal("invalid_response_limit", "max_buffered_bytes")
    if cancelled is not None and cancelled():
        return adapt_upstream_response(selected, b"", status=status, content_type=content_type, cancelled=True)
    if selected == "anthropic_messages":
        raw_parts: list[bytes] = []
        total_bytes = 0
        for chunk in chunks:
            if not isinstance(chunk, bytes):
                return response_refusal("invalid_upstream_stream", "sse.bytes")
            total_bytes += len(chunk)
            if total_bytes > max_buffered_bytes:
                return response_refusal("upstream_response_too_large", "sse.bytes")
            raw_parts.append(chunk)
            if cancelled is not None and cancelled():
                return adapt_upstream_response(selected, b"", status=status, content_type=content_type, cancelled=True)
        return AdaptedResponse(body=b"".join(raw_parts), status=status, content_type=content_type)
    raw_parts: list[bytes] = []
    total_bytes = 0
    for chunk in chunks:
        if not isinstance(chunk, bytes):
            return response_refusal("invalid_upstream_stream", "sse.bytes")
        total_bytes += len(chunk)
        if total_bytes > max_buffered_bytes:
            return response_refusal("upstream_response_too_large", "sse.bytes")
        raw_parts.append(chunk)
        if cancelled is not None and cancelled():
            return adapt_upstream_response(selected, b"", status=status, content_type=content_type, cancelled=True)
    decoded = _decode_sse_frames(raw_parts)
    if isinstance(decoded, NotForwardable):
        return decoded
    payloads, _raw = decoded
    if any(isinstance(payload, Mapping) and payload.get("error") is not None for payload in payloads):
        error_payload = next(payload for payload in payloads if isinstance(payload, Mapping) and payload.get("error") is not None)
        effective_status = status if status >= 400 else 502
        return AdaptedResponse(
            body=anthropic_error_sse(effective_status, error_payload, default="Upstream stream failed"),
            status=effective_status,
            content_type="text/event-stream",
            adaptations=(
                Adaptation(
                    "stream.error",
                    "upstream_error_mapped_to_anthropic_error",
                    "A terminal stream error is not presented as message success.",
                ),
            ),
        )
    try:
        if selected == "responses":
            if any(isinstance(payload, Mapping) and payload.get("type") == "response.failed" for payload in payloads):
                failed = next(payload for payload in payloads if isinstance(payload, Mapping) and payload.get("type") == "response.failed")
                effective_status = status if status >= 400 else 502
                failed_response = failed.get("response")
                failed_payload = (
                    failed_response
                    if isinstance(failed_response, Mapping) and failed_response.get("error") is not None
                    else failed
                )
                return AdaptedResponse(
                    body=anthropic_error_sse(effective_status, failed_payload, default="Upstream response failed"),
                    status=effective_status,
                    content_type="text/event-stream",
                    adaptations=(
                        Adaptation(
                            "stream.error",
                            "upstream_error_mapped_to_anthropic_error",
                            "A terminal Responses failure is not presented as message success.",
                        ),
                    ),
                )
            if any(isinstance(payload, Mapping) and payload.get("type") == "response.incomplete" for payload in payloads):
                return response_refusal("incomplete_upstream_stream", "responses.terminal")
            created = next(
                (payload for payload in payloads if isinstance(payload, Mapping) and payload.get("type") == "response.created"),
                None,
            )
            if not isinstance(created, Mapping) or not isinstance(created.get("response"), Mapping):
                return response_refusal("unsupported_upstream_stream", "responses.identity")
            created_response = created["response"]
            response_id = created_response.get("id")
            model = created_response.get("model")
            if not isinstance(response_id, str) or not response_id:
                return response_refusal("unsupported_upstream_stream", "responses.id")
            if not isinstance(model, str) or not model:
                return response_refusal("unsupported_upstream_stream", "responses.model")
            for payload in payloads:
                if not isinstance(payload, Mapping) or not isinstance(payload.get("response"), Mapping):
                    continue
                response = payload["response"]
                if "id" in response and response["id"] != response_id:
                    return response_refusal("unsupported_upstream_stream", "responses.id")
                if "model" in response and response["model"] != model:
                    return response_refusal("unsupported_upstream_stream", "responses.model")
            terminal = next(
                (payload for payload in payloads if isinstance(payload, Mapping) and payload.get("type") == "response.completed"),
                None,
            )
            if not isinstance(terminal, Mapping) or not isinstance(terminal.get("response"), Mapping):
                return response_refusal("incomplete_upstream_stream", "responses.terminal")
            terminal_response = terminal["response"]
            if terminal_response.get("id") != response_id or terminal_response.get("model") != model:
                return response_refusal("unsupported_upstream_stream", "responses.identity")
            if terminal_response.get("status") != "completed":
                return response_refusal("unsupported_upstream_stream", "responses.status")
            if not isinstance(terminal_response.get("output"), list) or not terminal_response["output"]:
                return response_refusal("unsupported_upstream_stream", "responses.output")
            terminal_has_text = any(
                isinstance(item, Mapping)
                and item.get("type") == "message"
                and isinstance(item.get("content"), list)
                and any(
                    isinstance(part, Mapping)
                    and part.get("type") in {"output_text", "text"}
                    and isinstance(part.get("text"), str)
                    for part in item["content"]
                )
                for item in terminal_response["output"]
            )
            terminal_usage = terminal_response["usage"] if "usage" in terminal_response else _MISSING
            chunks_for_chat = response_events_to_chat_stream_chunks(
                [payload for payload in payloads if payload != "[DONE]"],
                require_completed=True,
            )
            return _chat_chunks_to_anthropic_sse(
                chunks_for_chat,
                status=status,
                terminal_usage=terminal_usage,
                expected_id=response_id,
                expected_model=model,
                allow_empty_text=terminal_has_text,
            )
        chunks_for_chat = [payload for payload in payloads]
        return _chat_chunks_to_anthropic_sse(chunks_for_chat, status=status, require_done=True)
    except UpstreamStreamIncompleteError:
        return response_refusal("incomplete_upstream_stream", "responses.terminal")
    except (TypeError, ValueError, UnsupportedProtocolTranslationError):
        return response_refusal("unsupported_upstream_stream", "stream")
