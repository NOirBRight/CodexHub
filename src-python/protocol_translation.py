"""Pure-ish wire-format translations between Responses and Chat Completions.

The Gateway owns routing, transport, retries, and Codex-specific semantic
repair.  This module owns only the protocol shapes used at that boundary.
Optional callbacks keep the few Gateway-owned naming and repair policies out of
the translation implementation while preserving existing behavior.

Only the documented lossless subset crosses this seam: text without
annotations, URL-backed images (including detail), and paired function calls.
The longstanding developer-to-system/instructions text compatibility mapping
remains for third-party Chat endpoints. Other semantic items—including new
content fields—raise ``UnsupportedProtocolTranslationError`` instead of being
dropped or rewritten.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Mapping
import uuid


ChatContentText = Callable[[Any], str]
CollectTextFragments = Callable[[Any], list[str]]
FunctionNameFromResponseItem = Callable[[Mapping[str, Any]], str | None]
NormalizeChatFunctionName = Callable[[str], str]
XmlishToolOutputs = Callable[[str], list[dict[str, Any]]]
ResponseRepair = Callable[[dict[str, Any]], dict[str, Any]]
UsageFromResponse = Callable[[Mapping[str, Any]], Mapping[str, Any] | None]

class UpstreamStreamIncompleteError(RuntimeError):
    """Raised when an upstream stream ends without a terminal event."""


class UnsupportedProtocolTranslationError(ValueError):
    """Raised when a wire shape cannot cross the protocol seam losslessly."""

    def __init__(self, code: str, detail: str):
        self.code = code
        super().__init__(detail)


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


def _raise_for_unsupported_chat_message_semantics(message: Mapping[str, Any]) -> None:
    for field in ("refusal", "audio", "annotations", "reasoning", "reasoning_content"):
        value = message.get(field)
        if value not in (None, "", [], {}):
            raise UnsupportedProtocolTranslationError(
                "unsupported_protocol_semantics",
                f"Cannot translate Chat Completions message field {field!r} to Responses without losing it.",
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
        },
        "tool": {"role", "content", "tool_call_id"},
    }
    _require_supported_fields(message, allowed_by_role.get(role, {"role", "content"}), label)
    _raise_for_unsupported_chat_message_semantics(message)


def _require_supported_fields(value: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unsupported = sorted(str(key) for key in value.keys() if key not in allowed)
    if unsupported:
        raise UnsupportedProtocolTranslationError(
            "unsupported_protocol_semantics",
            f"Cannot translate {label} fields without losing them: {', '.join(unsupported)}.",
        )


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
    if not isinstance(value, list):
        return ""

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
            _require_supported_fields(part, {"type", "text", "annotations"}, "Responses text content part")
            annotations = part.get("annotations")
            if annotations not in (None, []):
                raise UnsupportedProtocolTranslationError(
                    "unsupported_protocol_semantics",
                    "Cannot translate Responses text annotations to Chat Completions without losing them.",
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


def responses_input_to_chat_messages(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, str):
        return [{"role": "user", "content": value}]
    if not isinstance(value, list):
        return []

    messages: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise UnsupportedProtocolTranslationError(
                "unsupported_protocol_semantics",
                "Cannot translate a non-object Responses input item.",
            )
        item_type = item.get("type")
        if item_type == "message" or (item_type is None and ("role" in item or "content" in item)):
            _require_supported_fields(item, {"type", "role", "content"}, "Responses message input item")
            role = item.get("role")
            if role == "developer":
                role = "system"
            elif role not in {"system", "user", "assistant"}:
                raise UnsupportedProtocolTranslationError(
                    "unsupported_protocol_semantics",
                    f"Cannot translate Responses message role {role!r} to Chat Completions.",
                )
            messages.append({"role": role, "content": responses_content_to_chat_content(item.get("content"))})
            continue
        if item_type == "function_call":
            _require_supported_fields(
                item,
                {"type", "call_id", "name", "arguments"},
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
            messages.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": arguments},
                        }
                    ],
                }
            )
            continue
        if item_type == "function_call_output":
            _require_supported_fields(
                item,
                {"type", "call_id", "output"},
                "Responses function-call output item",
            )
            call_id = item.get("call_id")
            if not isinstance(call_id, str) or not call_id:
                raise UnsupportedProtocolTranslationError(
                    "unpaired_tool_call",
                    "Cannot translate a function result without a non-empty call_id.",
                )
            output = item.get("output")
            if not isinstance(output, str):
                raise UnsupportedProtocolTranslationError(
                    "unsupported_protocol_semantics",
                    "Cannot translate a non-string Responses function-call output to Chat Completions.",
                )
            messages.append({"role": "tool", "tool_call_id": call_id, "content": output})
            continue
        raise UnsupportedProtocolTranslationError(
            "unsupported_protocol_semantics",
            f"Cannot translate Responses input item type {item_type!r} to Chat Completions.",
        )
    return messages


def responses_tools_to_chat_tools(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
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


def responses_request_to_chat_completion_body(body: bytes) -> bytes:
    payload = json.loads(body.decode("utf-8-sig"))
    if not isinstance(payload, dict):
        return body
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
        },
        "Responses request",
    )
    if payload.get("reasoning") is not None:
        raise UnsupportedProtocolTranslationError(
            "unsupported_protocol_semantics",
            "Cannot translate Responses reasoning controls to Chat Completions without a proven equivalent.",
        )

    messages: list[dict[str, Any]] = []
    instructions = payload.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        messages.append({"role": "system", "content": instructions})
    messages.extend(responses_input_to_chat_messages(payload.get("input")))
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

    tools = responses_tools_to_chat_tools(payload.get("tools"))
    if tools:
        chat_payload["tools"] = tools
    tool_choice = responses_tool_choice_to_chat_tool_choice(payload.get("tool_choice"))
    if tool_choice is not None:
        chat_payload["tool_choice"] = tool_choice

    return json.dumps(chat_payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


def chat_content_to_responses_content(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, str):
        return [{"type": "input_text", "text": value}]
    if not isinstance(value, list):
        return []
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
    if not isinstance(messages, list):
        return None, []

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


def chat_tools_to_responses_tools(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    tools: list[dict[str, Any]] = []
    for item in value:
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
    payload = json.loads(body.decode("utf-8-sig"))
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
            "n",
        },
        "Chat Completions request",
    )
    if payload.get("reasoning") is not None or payload.get("reasoning_effort") is not None:
        raise UnsupportedProtocolTranslationError(
            "unsupported_protocol_semantics",
            "Cannot translate Chat Completions reasoning controls to Responses without a proven equivalent.",
        )
    if "n" in payload and payload.get("n") not in (None, 1):
        raise UnsupportedProtocolTranslationError(
            "unsupported_protocol_semantics",
            "Cannot translate multiple Chat Completions choices to a single Responses result.",
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

    return json.dumps(responses_payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


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
    payload = json.loads(body.decode("utf-8-sig"))
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
                continue
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
                continue
            _require_supported_chat_message_fields(message, "Chat Completions response message")
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
    if "usage" in payload:
        response_payload["usage"] = payload["usage"]

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
) -> bytes:
    payload = json.loads(body.decode("utf-8-sig"))
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

    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                raise UnsupportedProtocolTranslationError(
                    "unsupported_protocol_semantics",
                    "Cannot translate a non-object Responses output item.",
                )
            if item.get("type") == "message":
                _require_supported_fields(
                    item,
                    {"id", "type", "status", "role", "content"},
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

    message: dict[str, Any] = {"role": "assistant", "content": "".join(text_parts) or None}
    if tool_calls:
        message["tool_calls"] = tool_calls
        if not message["content"]:
            message["content"] = None

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
        chat_payload["usage"] = usage
    return json.dumps(chat_payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


def chat_completion_body_to_stream_chunks(body: bytes) -> list[dict[str, Any]]:
    payload = json.loads(body.decode("utf-8-sig"))
    if not isinstance(payload, dict):
        return []

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
            continue
        index = choice.get("index")
        index = index if isinstance(index, int) else fallback_index
        message = choice.get("message")
        if not isinstance(message, Mapping):
            continue
        _require_supported_chat_message_fields(message, "Chat Completions response message")
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
        },
        "Chat Completions stream delta",
    )
    _raise_for_unsupported_chat_message_semantics(source)


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
    next_output_index = 0
    message_output_index: int | None = None

    for chunk in chunks:
        if isinstance(chunk, Mapping):
            _validate_chat_stream_choices(chunk)

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
            delta = choice.get("delta")
            message = choice.get("message")
            source = delta if isinstance(delta, dict) else message if isinstance(message, dict) else None
            if not isinstance(source, dict):
                continue
            _validate_chat_stream_source(source)
            content = source.get("content")
            if isinstance(content, str) and content:
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
    events.append(
        {
            "type": "response.incomplete" if incomplete_details is not None else "response.completed",
            "response": completed_response,
        }
    )
    return events


def responses_events_have_completed(events: list[Mapping[str, Any]]) -> bool:
    return any(isinstance(event, Mapping) and event.get("type") == "response.completed" for event in events)


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
    item_type = item.get("type")
    if item_type == "message":
        _require_supported_fields(
            item,
            {"id", "type", "status", "role", "content"},
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


def response_events_to_chat_stream_chunks(
    events: list[Mapping[str, Any]],
    *,
    require_completed: bool = False,
    function_name_from_response_item: FunctionNameFromResponseItem = _default_function_name_from_response_item,
) -> list[dict[str, Any]]:
    if require_completed and not responses_events_have_completed(events):
        raise UpstreamStreamIncompleteError("Responses stream ended before response.completed")

    chunks: list[dict[str, Any]] = []
    tool_states: dict[str, dict[str, Any]] = {}
    model: str | None = None
    response_id: str | None = None
    finish_reason: str | None = None

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
                if isinstance(output, list):
                    for item in output:
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
                    finish_reason = "tool_calls" if any(
                        isinstance(item, Mapping) and item.get("type") == "function_call"
                        for item in output
                    ) else "stop"
                else:
                    finish_reason = "stop"
            else:
                finish_reason = "stop"

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

    def __init__(self) -> None:
        self.tool_states: dict[str, dict[str, Any]] = {}
        self.model: str | None = None
        self.response_id: str | None = None
        self.completed = False

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
                output = response_obj.get("output")
                if isinstance(output, list):
                    for item in output:
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
                    if any(isinstance(item, Mapping) and item.get("type") == "function_call" for item in output):
                        finish_reason = "tool_calls"
            self.completed = True
            chunks.append(self._chunk({}, finish_reason=finish_reason))
            return chunks
        return []


class ChatToResponsesStreamConverter:
    """Incrementally translate Chat Completions chunks into Responses events."""

    def __init__(self) -> None:
        self.response_id = f"resp_{uuid.uuid4().hex[:12]}"
        self.model: str | None = None
        self.item_id = f"msg_{uuid.uuid4().hex[:12]}"
        self.text_parts: list[str] = []
        self.message_output_index: int | None = None
        self.next_output_index = 0
        self.tool_states: dict[int, dict[str, Any]] = {}
        self.created = False
        self.message_started = False
        self.completed = False

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

    def _complete_events(self, *, incomplete: bool = False) -> list[dict[str, Any]]:
        if self.completed:
            return []
        self.completed = True
        events = self._created_events()
        output_by_index: dict[int, dict[str, Any]] = {}
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
        events.append({"type": "response.incomplete" if incomplete else "response.completed", "response": response})
        return events

    def events_for_done(self) -> list[dict[str, Any]]:
        return self._complete_events()

    def events_for_chunk(self, chunk: Mapping[str, Any]) -> list[dict[str, Any]]:
        _validate_chat_stream_choices(chunk)
        if isinstance(chunk.get("model"), str):
            self.model = chunk.get("model")
        events: list[dict[str, Any]] = []
        choices = chunk.get("choices")
        if not isinstance(choices, list):
            return events
        for choice in choices:
            if not isinstance(choice, Mapping):
                continue
            delta = choice.get("delta")
            if isinstance(delta, Mapping):
                _validate_chat_stream_source(delta)
                content = delta.get("content")
                if isinstance(content, str) and content:
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
                tool_calls = delta.get("tool_calls")
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
                events.extend(self._complete_events(incomplete=True))
            elif finish_reason is not None:
                if finish_reason not in {"stop", "tool_calls"}:
                    raise UnsupportedProtocolTranslationError(
                        "unsupported_protocol_semantics",
                        f"Cannot translate Chat Completions finish_reason {finish_reason!r} to Responses stream events.",
                    )
                events.extend(self._complete_events())
        return events


def events_to_responses_body(
    events: list[Mapping[str, Any]],
    *,
    require_completed: bool = False,
    usage_from_response: UsageFromResponse = _default_usage_from_response,
) -> bytes:
    """Reconstruct a non-streaming Responses body from Responses SSE events."""
    if require_completed and not responses_events_have_completed(events):
        raise UpstreamStreamIncompleteError("Responses stream ended before response.completed")

    output: list[dict[str, Any]] = []
    response_id = f"resp_{uuid.uuid4().hex[:12]}"
    model: str | None = None
    text_parts: list[str] = []
    current_item: dict[str, Any] | None = None
    usage: Mapping[str, Any] | None = None
    response_payload: dict[str, Any] = {}

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
        elif event_type == "response.output_item.done":
            item = event.get("item")
            if isinstance(item, dict):
                output.append(dict(item))
                current_item = None
        elif event_type == "response.function_call_arguments.done":
            arguments = event.get("arguments")
            if current_item and isinstance(arguments, str):
                current_item["arguments"] = arguments
        elif event_type == "response.completed":
            response = event.get("response")
            if isinstance(response, Mapping):
                response_payload.update(dict(response))
                response_id = response.get("id") or response_id
                model = response.get("model") or model
                usage = usage_from_response(response) or usage
                response_output = response.get("output")
                if isinstance(response_output, list) and not output:
                    output = [dict(item) for item in response_output if isinstance(item, dict)]

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
    payload = json.loads(body.decode("utf-8-sig"))
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


__all__ = [
    entrypoint.__name__
    for entrypoint in (
        ChatToResponsesStreamConverter,
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
