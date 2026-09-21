"""Anthropic Messages wire shapes used by Gateway routing.

OpenCode Go Union Alpha speaks ``/messages`` with ``x-api-key``. This module
owns the third-party Chat Completions subset: portable text, URL/data images,
function tools, paired tool results, and thinking round-trip. Responses callers
convert through Chat first. Codex adapter surfaces are out of scope.
"""

from __future__ import annotations

import json
from typing import Any, Mapping
from urllib.parse import urlsplit

from protocol_json import AmbiguousJSONError, strict_json_loads
from protocol_translation import UnsupportedProtocolTranslationError
from sse_events import SseEvent


ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_MAX_TOKENS = 4096
THINKING_DETAIL_TYPE = "anthropic_thinking"
EFFORT_BUDGET_TOKENS = {
    "low": 1024,
    "medium": 4096,
    "high": 8192,
    "xhigh": 16384,
    "max": 32768,
}
CHAT_REQUEST_FIELDS = {
    "model",
    "messages",
    "tools",
    "tool_choice",
    "stream",
    "stream_options",
    "temperature",
    "top_p",
    "presence_penalty",
    "frequency_penalty",
    "parallel_tool_calls",
    "max_tokens",
    "max_output_tokens",
    "reasoning",
    "reasoning_effort",
    "thinking",
}
CHAT_MESSAGE_FIELDS = {
    "system": {"role", "content", "name"},
    "user": {"role", "content", "name"},
    "assistant": {
        "role",
        "content",
        "name",
        "tool_calls",
        "reasoning",
        "reasoning_content",
        "reasoning_details",
    },
    "tool": {"role", "content", "name", "tool_call_id"},
}
CHAT_IMAGE_URL_FIELDS = {"url", "detail"}
CHAT_CONTENT_PART_FIELDS = {"type", "text", "image_url"}


def estimate_input_tokens(payload: Mapping[str, Any]) -> int:
    """Best-effort stand-in for POST /v1/messages/count_tokens.

    # ponytail: CJK~1 token, else ~4 chars; swap for a real tokenizer if
    # Claude Code preflight drift starts rejecting requests.
    """
    texts: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, str):
            texts.append(value)
        elif isinstance(value, Mapping):
            for item in value.values():
                walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    walk(payload)
    joined = "\n".join(texts)
    tokens = 0
    latin = 0
    for char in joined:
        if ord(char) > 0x2E80:
            if latin:
                tokens += max(1, (latin + 3) // 4)
                latin = 0
            tokens += 1
        else:
            latin += 1
    if latin:
        tokens += max(1, (latin + 3) // 4)
    return max(1, tokens)


def sse_event_resets_idle_timeout(event: SseEvent) -> bool:
    name = event.event.decode("utf-8") if event.event else ""
    return name in {
        "message_start",
        "content_block_start",
        "content_block_delta",
        "message_delta",
    }


def chat_chunks_for_sse_event(
    event_name: str | None,
    payload: Mapping[str, Any] | str | None,
    converter: "AnthropicToChatStreamConverter | None",
) -> list[Mapping[str, Any] | str]:
    """Return Chat SSE payloads for one Anthropic SSE frame.

    ``converter`` is the caller's AnthropicToChatStreamConverter, or None for
    an ordinary Chat Completions stream.  Payload is the parsed SSE JSON (or
    ``[DONE]``); ``None``/missing ``data`` yields no payloads.  Raising matches
    the Chat path without borrowing its converter.
    """

    if converter is not None:
        return converter.chat_payloads_for_sse(event_name, payload)
    if payload is None or payload == "[DONE]":
        return [payload] if payload == "[DONE]" else []
    if not isinstance(payload, Mapping):
        raise UnsupportedProtocolTranslationError(
            "unsupported_protocol_semantics",
            "Cannot translate a non-object Chat Completions SSE payload.",
        )
    return [payload]


def bind_request_headers(headers: Mapping[str, str], endpoint_url: str) -> dict[str, str]:
    """Use Anthropic's x-api-key on ``/messages`` without dropping Bearer."""

    path = urlsplit(endpoint_url).path.rstrip("/").lower()
    if not path.endswith("/messages"):
        return dict(headers)
    outgoing = {key: value for key, value in headers.items() if key.lower() != "x-api-key"}
    authorization = next(
        (value for key, value in outgoing.items() if key.lower() == "authorization"),
        "",
    )
    token = authorization.split(None, 1)[1].strip() if authorization.lower().startswith("bearer ") else ""
    if token:
        outgoing["x-api-key"] = token
    if not any(key.lower() == "anthropic-version" for key in outgoing):
        outgoing["anthropic-version"] = ANTHROPIC_VERSION
    return outgoing


def chat_request_to_anthropic_body(body: bytes) -> bytes:
    payload = _object_payload(body, "Cannot prepare a non-object Anthropic conversion request.")
    _reject_unknown_fields(payload, CHAT_REQUEST_FIELDS, "Chat Completions request")
    messages: list[dict[str, Any]] = []
    system_parts: list[str] = []
    pending_tool_results: list[dict[str, Any]] = []

    def flush_tool_results() -> None:
        nonlocal pending_tool_results
        if pending_tool_results:
            messages.append({"role": "user", "content": pending_tool_results})
            pending_tool_results = []

    for message in payload.get("messages") or []:
        if not isinstance(message, Mapping):
            raise UnsupportedProtocolTranslationError(
                "unsupported_protocol_semantics",
                "Cannot translate a non-object Chat Completions message to Anthropic Messages.",
            )
        role = message.get("role")
        _reject_unknown_fields(
            message,
            CHAT_MESSAGE_FIELDS.get(role, {"role", "content"}),
            "Chat Completions message",
        )
        if role == "system":
            text = _chat_text(message.get("content"))
            if text:
                system_parts.append(text)
            continue
        if role == "tool":
            tool_use_id = message.get("tool_call_id")
            if not isinstance(tool_use_id, str) or not tool_use_id:
                raise UnsupportedProtocolTranslationError(
                    "unsupported_protocol_semantics",
                    "Cannot translate a Chat Completions tool result without tool_call_id.",
                )
            pending_tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": _chat_text(message.get("content")),
                }
            )
            continue
        flush_tool_results()
        if role == "assistant":
            messages.append({"role": "assistant", "content": _assistant_content(message)})
            continue
        if role == "user":
            messages.append({"role": "user", "content": _user_content(message.get("content"))})
            continue
        raise UnsupportedProtocolTranslationError(
            "unsupported_protocol_semantics",
            f"Cannot translate Chat Completions role {role!r} to Anthropic Messages.",
        )
    flush_tool_results()
    if not messages:
        raise UnsupportedProtocolTranslationError(
            "unsupported_protocol_semantics",
            "Cannot translate an empty Chat Completions conversation to Anthropic Messages.",
        )
    anthropic: dict[str, Any] = {
        "model": payload.get("model"),
        "messages": messages,
        "max_tokens": _max_tokens(payload),
    }
    if system_parts:
        anthropic["system"] = "\n\n".join(system_parts)
    if payload.get("stream") is True:
        anthropic["stream"] = True
    for key in ("temperature", "top_p"):
        if key in payload:
            anthropic[key] = payload[key]
    thinking = _anthropic_thinking(payload)
    if thinking is not None:
        anthropic["thinking"] = thinking
    tools = _anthropic_tools(payload.get("tools"))
    if tools:
        anthropic["tools"] = tools
    tool_choice = _anthropic_tool_choice(payload.get("tool_choice"), has_tools=bool(tools))
    if payload.get("tool_choice") == "none":
        tool_choice = None
    if tool_choice is not None:
        anthropic["tool_choice"] = tool_choice
    return json.dumps(anthropic, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


def anthropic_message_to_chat_completion_body(body: bytes) -> bytes:
    payload = _object_payload(body, "Cannot translate a non-object Anthropic message.")
    if payload.get("type") == "error":
        raise UnsupportedProtocolTranslationError(
            "unsupported_protocol_semantics",
            "Cannot translate an Anthropic error object to Chat Completions.",
        )
    message = payload.get("message") if isinstance(payload.get("message"), Mapping) else payload
    content = message.get("content") if isinstance(message, Mapping) else None
    text_parts: list[str] = []
    thinking_parts: list[str] = []
    thinking_details: list[dict[str, Any]] = []
    tool_calls: list[dict[str, Any]] = []
    if isinstance(content, list):
        for index, block in enumerate(content):
            if not isinstance(block, Mapping):
                continue
            block_type = block.get("type")
            if block_type == "text" and isinstance(block.get("text"), str):
                text_parts.append(block["text"])
            elif block_type == "thinking" and isinstance(block.get("thinking"), str):
                thinking_parts.append(block["thinking"])
                detail = {
                    "type": THINKING_DETAIL_TYPE,
                    "thinking": block["thinking"],
                }
                signature = block.get("signature")
                if isinstance(signature, str) and signature:
                    detail["signature"] = signature
                thinking_details.append(detail)
            elif block_type == "tool_use":
                tool_calls.append(
                    {
                        "id": str(block.get("id") or f"tool_{index}"),
                        "type": "function",
                        "function": {
                            "name": str(block.get("name") or ""),
                            "arguments": json.dumps(block.get("input") or {}, ensure_ascii=True, separators=(",", ":")),
                        },
                    }
                )
            elif block_type not in {None, "text", "thinking", "tool_use", "redacted_thinking"}:
                raise UnsupportedProtocolTranslationError(
                    "unsupported_protocol_semantics",
                    "Cannot translate unsupported Anthropic content block to Chat Completions.",
                )
    stop_reason = message.get("stop_reason") if isinstance(message, Mapping) else None
    finish_reason = "tool_calls" if tool_calls and stop_reason == "tool_use" else "stop"
    chat_message: dict[str, Any] = {
        "role": "assistant",
        "content": "".join(text_parts) or None,
    }
    if thinking_parts:
        chat_message["reasoning_content"] = "\n".join(thinking_parts)
        chat_message["reasoning_details"] = thinking_details
    if tool_calls:
        chat_message["tool_calls"] = tool_calls
    usage = message.get("usage") if isinstance(message, Mapping) else None
    chat: dict[str, Any] = {
        "id": str(message.get("id") or "chatcmpl_anthropic"),
        "object": "chat.completion",
        "model": message.get("model") if isinstance(message, Mapping) else None,
        "choices": [{"index": 0, "finish_reason": finish_reason, "message": chat_message}],
    }
    if isinstance(usage, Mapping):
        chat["usage"] = {
            "prompt_tokens": usage.get("input_tokens", 0),
            "completion_tokens": usage.get("output_tokens", 0),
            "total_tokens": int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0),
        }
    return json.dumps(chat, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


class AnthropicToChatStreamConverter:
    """Turn Anthropic SSE payloads into Chat Completions stream chunks."""

    def __init__(self) -> None:
        self._model = "anthropic"
        self._message_id = "chatcmpl_anthropic"
        self._tool_index = -1
        self._tool_id = ""
        self._tool_name = ""
        self._finished = False

    def chat_payloads_for_sse(
        self,
        event_name: str | None,
        payload: Mapping[str, Any] | str | None,
    ) -> list[dict[str, Any] | str]:
        if payload is None:
            return []
        if payload == "[DONE]":
            return self.finish()
        if not isinstance(payload, Mapping):
            raise UnsupportedProtocolTranslationError(
                "unsupported_protocol_semantics",
                "Cannot translate a non-object Anthropic SSE payload.",
            )
        name = event_name or str(payload.get("type") or "")
        if name in {"ping", "content_block_stop"}:
            return []
        if name == "message_start":
            message = payload.get("message")
            if isinstance(message, Mapping):
                self._model = str(message.get("model") or self._model)
                self._message_id = str(message.get("id") or self._message_id)
            return []
        if name == "content_block_start":
            block = payload.get("content_block")
            if isinstance(block, Mapping) and block.get("type") == "tool_use":
                self._tool_index += 1
                self._tool_id = str(block.get("id") or f"tool_{self._tool_index}")
                self._tool_name = str(block.get("name") or "")
                return [self._chunk({"tool_calls": [self._tool_delta(arguments="")]} )]
            if isinstance(block, Mapping) and block.get("type") in {"thinking", "text"}:
                return []
            if isinstance(block, Mapping) and block.get("type") not in {None, "thinking", "text", "tool_use"}:
                raise UnsupportedProtocolTranslationError(
                    "unsupported_protocol_semantics",
                    "Cannot translate unsupported Anthropic stream block to Chat Completions.",
                )
            return []
        if name == "content_block_delta":
            delta = payload.get("delta")
            if not isinstance(delta, Mapping):
                return []
            if delta.get("type") == "text_delta" and isinstance(delta.get("text"), str):
                return [self._chunk({"content": delta["text"]})]
            if delta.get("type") == "thinking_delta" and isinstance(delta.get("thinking"), str):
                return [self._chunk({"reasoning_content": delta["thinking"]})]
            if delta.get("type") == "input_json_delta" and isinstance(delta.get("partial_json"), str):
                return [self._chunk({"tool_calls": [self._tool_delta(arguments=delta["partial_json"])]})]
            return []
        if name == "message_delta":
            delta = payload.get("delta")
            stop = delta.get("stop_reason") if isinstance(delta, Mapping) else None
            finish = "tool_calls" if stop == "tool_use" else ("stop" if stop else None)
            if finish:
                self._finished = True
                return [self._chunk({}, finish_reason=finish)]
            return []
        if name == "message_stop":
            return self.finish()
        return []

    def finish(self) -> list[dict[str, Any] | str]:
        if self._finished:
            return ["[DONE]"]
        self._finished = True
        return [self._chunk({}, finish_reason="stop"), "[DONE]"]

    def _tool_delta(self, *, arguments: str) -> dict[str, Any]:
        return {
            "index": max(self._tool_index, 0),
            "id": self._tool_id,
            "type": "function",
            "function": {"name": self._tool_name, "arguments": arguments},
        }

    def _chunk(
        self,
        delta: Mapping[str, Any],
        *,
        finish_reason: str | None = None,
    ) -> dict[str, Any]:
        return {
            "id": self._message_id,
            "object": "chat.completion.chunk",
            "model": self._model,
            "choices": [{"index": 0, "delta": dict(delta), "finish_reason": finish_reason}],
        }


def _object_payload(body: bytes, detail: str) -> dict[str, Any]:
    try:
        payload = strict_json_loads(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError, AmbiguousJSONError) as exc:
        raise UnsupportedProtocolTranslationError(
            "unsupported_protocol_semantics",
            "Cannot parse the inbound request as JSON.",
        ) from exc
    if not isinstance(payload, dict):
        raise UnsupportedProtocolTranslationError("unsupported_protocol_semantics", detail)
    return payload


def _reject_unknown_fields(value: Mapping[str, Any], allowed: set[str], label: str) -> None:
    if any(key not in allowed for key in value):
        cache_fields = [
            key
            for key in ("prompt_cache_options", "prompt_cache_breakpoint")
            if key in value and key not in allowed
        ]
        detail = f"Cannot translate unsupported {label} fields without losing them."
        if cache_fields:
            detail += " Unsupported cache controls: " + ", ".join(cache_fields) + "."
        raise UnsupportedProtocolTranslationError("unsupported_protocol_semantics", detail)


def _max_tokens(payload: Mapping[str, Any]) -> int:
    for key in ("max_tokens", "max_output_tokens"):
        value = payload.get(key)
        if isinstance(value, int) and value > 0:
            return value
    catalog = _catalog_max_output_tokens(payload.get("model"))
    if catalog is not None:
        return catalog
    return DEFAULT_MAX_TOKENS


def _catalog_max_output_tokens(model_id: Any) -> int | None:
    if not isinstance(model_id, str) or not model_id.strip():
        return None
    import maintained_catalog

    leaf = model_id.rsplit("/", 1)[-1]
    for provider_id in maintained_catalog.MAINTAINED_PROVIDER_IDS:
        model = maintained_catalog.resolve_model(provider_id, leaf)
        if model is not None and model.max_output_tokens > 0:
            return model.max_output_tokens
    return None


def _anthropic_thinking(payload: Mapping[str, Any]) -> dict[str, Any] | None:
    inbound = payload.get("thinking")
    if isinstance(inbound, Mapping) and str(inbound.get("type") or "").strip().lower() == "disabled":
        return None
    effort: str | None = None
    if isinstance(payload.get("reasoning_effort"), str):
        effort = payload["reasoning_effort"].strip().lower()
    reasoning = payload.get("reasoning")
    if effort is None and isinstance(reasoning, Mapping) and isinstance(reasoning.get("effort"), str):
        effort = reasoning["effort"].strip().lower()
    if effort in {"none", "minimal"}:
        return None
    if effort in EFFORT_BUDGET_TOKENS:
        return {"type": "enabled", "budget_tokens": EFFORT_BUDGET_TOKENS[effort]}
    if isinstance(inbound, Mapping) and str(inbound.get("type") or "").strip().lower() in {
        "enabled",
        "adaptive",
    }:
        return {"type": "enabled", "budget_tokens": EFFORT_BUDGET_TOKENS["medium"]}
    return None


def _chat_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        texts: list[str] = []
        for part in content:
            if isinstance(part, str) and part:
                texts.append(part)
            elif isinstance(part, Mapping):
                text = part.get("text")
                if isinstance(text, str) and text:
                    texts.append(text)
        return "".join(texts)
    return ""


def _image_source(url: str) -> dict[str, str]:
    if url.startswith("data:"):
        header, separator, data = url.partition(",")
        if separator != "," or not data:
            raise UnsupportedProtocolTranslationError(
                "unsupported_protocol_semantics",
                "Cannot translate a malformed data URI image to Anthropic Messages.",
            )
        media = header[5:] if header.startswith("data:") else header
        media_type, _, encoding = media.partition(";")
        if encoding.lower() != "base64" or not media_type.startswith("image/"):
            raise UnsupportedProtocolTranslationError(
                "unsupported_protocol_semantics",
                "Cannot translate a non-base64 data URI image to Anthropic Messages.",
            )
        return {"type": "base64", "media_type": media_type, "data": data}
    if url.startswith("https://") or url.startswith("http://"):
        return {"type": "url", "url": url}
    raise UnsupportedProtocolTranslationError(
        "unsupported_protocol_semantics",
        "Cannot translate an unsupported image URL to Anthropic Messages.",
    )


def _user_content(content: Any) -> str | list[dict[str, Any]]:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return _chat_text(content)
    blocks: list[dict[str, Any]] = []
    for part in content:
        if isinstance(part, str) and part:
            blocks.append({"type": "text", "text": part})
            continue
        if not isinstance(part, Mapping):
            raise UnsupportedProtocolTranslationError(
                "unsupported_protocol_semantics",
                "Cannot translate a non-object Chat Completions content part to Anthropic Messages.",
            )
        _reject_unknown_fields(part, CHAT_CONTENT_PART_FIELDS, "Chat Completions content part")
        part_type = part.get("type")
        if part_type in {None, "text"}:
            text = _chat_text([part] if part.get("text") is not None else part)
            if text:
                blocks.append({"type": "text", "text": text})
            continue
        if part_type == "image_url":
            image = part.get("image_url")
            if not isinstance(image, Mapping) or not isinstance(image.get("url"), str):
                raise UnsupportedProtocolTranslationError(
                    "unsupported_protocol_semantics",
                    "Cannot translate a Chat Completions image without a URL.",
                )
            _reject_unknown_fields(image, CHAT_IMAGE_URL_FIELDS, "Chat Completions image URL")
            blocks.append({"type": "image", "source": _image_source(image["url"])})
            continue
        if part_type == "input_image":
            raise UnsupportedProtocolTranslationError(
                "unsupported_protocol_semantics",
                "Cannot translate Responses image parts on the Chat Completions Anthropic seam.",
            )
        raise UnsupportedProtocolTranslationError(
            "unsupported_protocol_semantics",
            "Cannot translate unsupported Chat Completions content part to Anthropic Messages.",
        )
    if not blocks:
        return ""
    if len(blocks) == 1 and blocks[0].get("type") == "text":
        return blocks[0]["text"]
    return blocks


def _thinking_blocks(message: Mapping[str, Any]) -> list[dict[str, Any]]:
    details = message.get("reasoning_details")
    if isinstance(details, list) and details:
        blocks: list[dict[str, Any]] = []
        for detail in details:
            if not isinstance(detail, Mapping) or detail.get("type") != THINKING_DETAIL_TYPE:
                raise UnsupportedProtocolTranslationError(
                    "unsupported_protocol_semantics",
                    "Cannot translate unsupported Chat Completions reasoning_details to Anthropic Messages.",
                )
            thinking = detail.get("thinking")
            signature = detail.get("signature")
            if not isinstance(thinking, str):
                raise UnsupportedProtocolTranslationError(
                    "unsupported_protocol_semantics",
                    "Cannot translate Anthropic thinking history without thinking text.",
                )
            if not isinstance(signature, str) or not signature:
                raise UnsupportedProtocolTranslationError(
                    "unsupported_protocol_semantics",
                    "Cannot translate Anthropic thinking history without a signature.",
                )
            blocks.append({"type": "thinking", "thinking": thinking, "signature": signature})
        return blocks
    reasoning = message.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning:
        return [{"type": "thinking", "thinking": reasoning}]
    return []


def _assistant_content(message: Mapping[str, Any]) -> str | list[dict[str, Any]]:
    blocks = _thinking_blocks(message)
    tool_calls = message.get("tool_calls")
    text = _chat_text(message.get("content"))
    if not tool_calls and not blocks:
        return text
    if text:
        blocks.append({"type": "text", "text": text})
    if not tool_calls:
        return blocks
    if not isinstance(tool_calls, list):
        raise UnsupportedProtocolTranslationError(
            "unsupported_protocol_semantics",
            "Cannot translate non-list Chat Completions tool_calls to Anthropic Messages.",
        )
    for call in tool_calls:
        if not isinstance(call, Mapping):
            raise UnsupportedProtocolTranslationError(
                "unsupported_protocol_semantics",
                "Cannot translate a non-object tool call to Anthropic Messages.",
            )
        function = call.get("function") if isinstance(call.get("function"), Mapping) else call
        name = function.get("name") if isinstance(function, Mapping) else None
        arguments = function.get("arguments") if isinstance(function, Mapping) else "{}"
        if not isinstance(name, str) or not name:
            raise UnsupportedProtocolTranslationError(
                "unsupported_protocol_semantics",
                "Cannot translate a nameless tool call to Anthropic Messages.",
            )
        parsed: Any = {}
        if isinstance(arguments, str) and arguments:
            try:
                parsed = strict_json_loads(arguments)
            except (json.JSONDecodeError, AmbiguousJSONError) as exc:
                raise UnsupportedProtocolTranslationError(
                    "unsupported_protocol_semantics",
                    "Cannot translate malformed tool arguments to Anthropic Messages.",
                ) from exc
        elif isinstance(arguments, Mapping):
            parsed = arguments
        blocks.append(
            {
                "type": "tool_use",
                "id": str(call.get("id") or name),
                "name": name,
                "input": parsed if isinstance(parsed, Mapping) else {},
            }
        )
    return blocks


def _anthropic_tool_choice(value: Any, *, has_tools: bool) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, str):
        if value == "none":
            return None
        if value in {"auto", "any"}:
            if not has_tools:
                raise UnsupportedProtocolTranslationError(
                    "unsupported_protocol_semantics",
                    "Cannot translate a Chat Completions tool_choice without tools.",
                )
            return {"type": value}
        raise UnsupportedProtocolTranslationError(
            "unsupported_protocol_semantics",
            f"Cannot translate Chat Completions tool_choice {value!r} to Anthropic Messages.",
        )
    if not isinstance(value, Mapping):
        raise UnsupportedProtocolTranslationError(
            "unsupported_protocol_semantics",
            "Cannot translate a non-object Chat Completions tool_choice to Anthropic Messages.",
        )
    if not has_tools:
        raise UnsupportedProtocolTranslationError(
            "unsupported_protocol_semantics",
            "Cannot translate a Chat Completions tool_choice without tools.",
        )
    function = value.get("function") if isinstance(value.get("function"), Mapping) else None
    name = (function.get("name") if function else value.get("name"))
    if (
        value.get("type") == "function"
        and isinstance(name, str)
        and name
    ):
        return {"type": "tool", "name": name}
    raise UnsupportedProtocolTranslationError(
        "unsupported_protocol_semantics",
        "Cannot translate unsupported Chat Completions tool_choice to Anthropic Messages.",
    )


def _anthropic_tools(tools: Any) -> list[dict[str, Any]]:
    if not isinstance(tools, list) or not tools:
        return []
    converted: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, Mapping):
            raise UnsupportedProtocolTranslationError(
                "unsupported_protocol_semantics",
                "Cannot translate a non-object Chat Completions tool to Anthropic Messages.",
            )
        function = tool.get("function") if isinstance(tool.get("function"), Mapping) else tool
        name = function.get("name") if isinstance(function, Mapping) else None
        if not isinstance(name, str) or not name:
            raise UnsupportedProtocolTranslationError(
                "unsupported_protocol_semantics",
                "Cannot translate a nameless Chat Completions tool to Anthropic Messages.",
            )
        parameters = function.get("parameters") if isinstance(function, Mapping) else None
        schema = parameters if isinstance(parameters, Mapping) else {"type": "object", "properties": {}}
        item = {
            "name": name,
            "description": str(function.get("description") or "") if isinstance(function, Mapping) else "",
            "input_schema": schema,
        }
        converted.append(item)
    return converted
