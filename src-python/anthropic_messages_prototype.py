"""Isolated prototype: pure Anthropic Messages representation (Tickets #74/#75).

Evidence-gate prototype for ADR-0001 / ADR-0014. It is deliberately NOT
imported by production Gateway code: no route, handler, or client configuration
uses it. It re-establishes the representation seam ADR-0001 requires so #75 can
build on a reviewed shape instead of reviving the retired spike.

Three outcomes, never a silent drop:

* the native path (:meth:`AnthropicRequest.to_native_body`) re-serializes every
  modelled and unmodelled field, so an Anthropic-format upstream loses nothing;
* a converted path returns :class:`Adapted`, the translated body plus one named
  :class:`Adaptation` per non-equivalent field;
* anything the prototype cannot carry returns :class:`NotForwardable` naming the
  fields before any upstream I/O.

Conversion reuses existing repository seams rather than adding a third
translator: Anthropic Messages becomes Chat Completions here, then the existing
``protocol_translation.chat_completions_request_to_responses_body`` seam
produces the Responses request.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from protocol_translation import (
    UnsupportedProtocolTranslationError,
    chat_completions_request_to_responses_body,
)


__all__ = [
    "Adaptation",
    "Adapted",
    "AnthropicRequest",
    "ContentBlock",
    "HeaderPlan",
    "Message",
    "NotForwardable",
    "classify_headers",
    "parse_request",
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
            parts.append({"type": "text", "text": str(block.data.get("text") or "")})
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
            text_parts.append(str(block.data.get("text") or ""))
        elif block.type == "thinking":
            kinds.append("content")
            _declare_block_extras(
                block, {"type", "thinking", "signature"}, label=block_label, declared=declared
            )
            thinking = block.data.get("thinking")
            signature = block.data.get("signature")
            if isinstance(thinking, str):
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
    if not isinstance(tool_id, str) or not tool_id:
        declared.refuse(f"messages[].content[{index}].id")
    if not isinstance(name, str) or not name:
        declared.refuse(f"messages[].content[{index}].name")
    return {
        "id": tool_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps(block.data.get("input") or {}, ensure_ascii=True, separators=(",", ":")),
        },
    }


def _tool_result_message(block: ContentBlock, declared: _Declared, *, label: str) -> dict[str, Any]:
    tool_use_id = block.data.get("tool_use_id")
    if not isinstance(tool_use_id, str) or not tool_use_id:
        declared.refuse(f"{label}.tool_use_id")
    if block.data.get("is_error") is True:
        # A failed tool result must never reach an upstream as an ordinary
        # successful result that carries no error signal.
        declared.refuse(f"{label}.is_error")
    _declare_block_extras(
        block, {"type", "tool_use_id", "content", "is_error"}, label=label, declared=declared
    )
    content = block.data.get("content")
    if isinstance(content, list):
        text = _chat_text(tuple(_block(entry) for entry in content), declared, label=label)
    elif isinstance(content, str):
        text = content
    else:
        text = ""
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
        if isinstance(value, str):
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
        schema = tool.get("input_schema")
        if not isinstance(schema, Mapping):
            declared.refuse(f"tools[{index}].input_schema")
            schema = {"type": "object", "properties": {}}
        function: dict[str, Any] = {"name": tool.get("name"), "parameters": dict(schema)}
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
        if choice_type == "tool" and isinstance(value.get("name"), str):
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
