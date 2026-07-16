"""In-memory Anthropic Messages compatibility prototype.

This module deliberately has no HTTP handler, Gateway configuration, upstream I/O,
or retry dependencies.  It is a test-only design seam for Issue #74, where an
Anthropic Messages request can be translated and assessed before any production
route is considered.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import json
from typing import Any, Mapping


@dataclass(frozen=True)
class TranslationResult:
    """A translated payload plus every deliberate adaptation or rejection.

    ``body`` is ``None`` whenever a field cannot be represented safely.  That
    keeps a prospective HTTP caller from forwarding a lossy request by mistake.
    """

    body: bytes | None
    adapted: tuple[str, ...] = ()
    unsupported: tuple[str, ...] = ()

    @property
    def forwardable(self) -> bool:
        return self.body is not None and not self.unsupported


@dataclass(frozen=True)
class PrototypeExchange:
    """Evidence from exercising one in-memory upstream adapter."""

    upstream_kind: str
    translation: TranslationResult
    outbound_body: bytes | None
    downstream_sse: tuple[bytes, ...]


@dataclass(frozen=True)
class AnthropicMessage:
    """Canonical in-memory form for one Claude Messages history entry.

    This is intentionally narrower than an HTTP request: it preserves role and
    ordered content blocks so both Responses and Chat Completions adapters can
    share the same conversation/history interpretation.
    """

    role: str
    content: tuple[dict[str, Any], ...]


class CompatibilityStatus(str, Enum):
    """Conservative status labels used by the Spike report and tests."""

    PRESERVED = "preserved"
    ADAPTED = "adapted"
    UNSUPPORTED = "unsupported"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class HeaderPolicy:
    """Open-set header classification with safe trace values only."""

    forwarded: tuple[str, ...]
    consumed: tuple[str, ...]
    unsupported: tuple[str, ...]
    semantic_headers: dict[str, str]
    sanitized: dict[str, str]


_SUPPORTED_TOP_LEVEL_FIELDS = frozenset(
    {
        "model",
        "max_tokens",
        "system",
        "messages",
        "tools",
        "tool_choice",
        "stream",
        "temperature",
        "top_p",
    }
)


def compatibility_matrix() -> dict[str, CompatibilityStatus]:
    """Return the explicit non-production compatibility decision for each capability."""

    return {
        "text": CompatibilityStatus.ADAPTED,
        "tools": CompatibilityStatus.ADAPTED,
        "images": CompatibilityStatus.UNKNOWN,
        "thinking": CompatibilityStatus.UNSUPPORTED,
        "prompt_caching": CompatibilityStatus.UNKNOWN,
        "compact_resume": CompatibilityStatus.UNKNOWN,
        "subagents": CompatibilityStatus.UNKNOWN,
        "beta_fields": CompatibilityStatus.UNSUPPORTED,
        "errors": CompatibilityStatus.ADAPTED,
        "retries": CompatibilityStatus.UNKNOWN,
        "cancellation": CompatibilityStatus.UNKNOWN,
        "count_tokens": CompatibilityStatus.UNSUPPORTED,
    }


def classify_claude_headers(
    headers: Mapping[str, str],
    *,
    upstream_format: str,
) -> HeaderPolicy:
    """Classify Claude Code headers without retaining secret trace values.

    The policy is deliberately open-set: new ``x-claude-code-*`` names become
    consumed opaque metadata; new ``anthropic-*`` names are only forwardable to
    an Anthropic-format upstream and otherwise become explicit work items.
    """

    forwarded: list[str] = []
    consumed: list[str] = []
    unsupported: list[str] = []
    semantic_headers: dict[str, str] = {}
    sanitized: dict[str, str] = {}
    for raw_name, raw_value in headers.items():
        if not isinstance(raw_name, str):
            continue
        name = raw_name.lower()
        value = raw_value if isinstance(raw_value, str) else ""
        if name in {"authorization", "x-api-key", "proxy-authorization", "cookie"}:
            consumed.append(name)
            sanitized[name] = "<redacted>"
            continue
        if name.startswith("x-claude-code-"):
            consumed.append(name)
            semantic_headers[name] = value
            sanitized[name] = "<pseudonymized>"
            continue
        if name in {
            "accept",
            "content-type",
            "user-agent",
            "connection",
            "host",
            "accept-encoding",
            "content-length",
            "x-app",
        } or name.startswith("x-stainless-"):
            consumed.append(name)
            sanitized[name] = "<transport>"
            continue
        if name.startswith("anthropic-"):
            semantic_headers[name] = value
            if name == "anthropic-version":
                sanitized[name] = value
            else:
                sanitized[name] = "<redacted-open-set>"
            if upstream_format == "anthropic_messages":
                forwarded.append(name)
            elif name == "anthropic-version":
                consumed.append(name)
            else:
                unsupported.append(f"header.{name}")
            continue
        sanitized[name] = "<redacted>"
        unsupported.append(f"header.{name}")
    return HeaderPolicy(
        forwarded=tuple(dict.fromkeys(forwarded)),
        consumed=tuple(dict.fromkeys(consumed)),
        unsupported=tuple(dict.fromkeys(unsupported)),
        semantic_headers=semantic_headers,
        sanitized=sanitized,
    )


def _content_blocks(value: Any) -> tuple[dict[str, Any], ...] | None:
    if isinstance(value, str):
        return ({"type": "text", "text": value},)
    if not isinstance(value, list):
        return None
    blocks: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, Mapping):
            return None
        blocks.append(dict(item))
    return tuple(blocks)


def _parse_messages(value: Any) -> tuple[tuple[AnthropicMessage, ...], list[str]]:
    if not isinstance(value, list):
        return (), ["messages"]

    parsed: list[AnthropicMessage] = []
    unsupported: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            unsupported.append(f"messages[{index}]")
            continue
        role = item.get("role")
        blocks = _content_blocks(item.get("content"))
        if role not in {"user", "assistant"}:
            unsupported.append(f"messages[{index}].role")
            continue
        if blocks is None:
            unsupported.append(f"messages[{index}].content")
            continue
        parsed.append(AnthropicMessage(role=role, content=blocks))
    return tuple(parsed), unsupported


def _text_input(block: Mapping[str, Any], field: str) -> tuple[dict[str, Any] | None, str | None]:
    text = block.get("text")
    if not isinstance(text, str):
        return None, field
    if set(block).difference({"type", "text"}):
        return None, field
    return {"type": "input_text", "text": text}, None


def _tool_result_output(value: Any, field: str) -> tuple[str | None, str | None]:
    if isinstance(value, str):
        return value, None
    blocks = _content_blocks(value)
    if blocks is None:
        return None, field
    text_parts: list[str] = []
    for index, block in enumerate(blocks):
        text, unsupported = _text_input(block, f"{field}[{index}]")
        if unsupported is not None or text is None:
            return None, unsupported or field
        text_parts.append(text["text"])
    return "".join(text_parts), None


def _messages_to_responses_input(
    messages: tuple[AnthropicMessage, ...],
) -> tuple[list[dict[str, Any]], list[str]]:
    input_items: list[dict[str, Any]] = []
    unsupported: list[str] = []
    for message_index, message in enumerate(messages):
        text_and_image: list[dict[str, Any]] = []
        for block_index, block in enumerate(message.content):
            field = f"messages[{message_index}].content[{block_index}]"
            block_type = block.get("type")
            if block_type == "text":
                item, failure = _text_input(block, field)
                if failure is not None:
                    unsupported.append(failure)
                elif item is not None:
                    text_and_image.append(item)
                continue
            if block_type == "image":
                # A syntactically valid data URL is not evidence that the
                # selected upstream/provider accepts the same image contract.
                unsupported.append(field)
                continue
            if block_type == "tool_use" and message.role == "assistant":
                call_id = block.get("id")
                name = block.get("name")
                arguments = block.get("input")
                if (
                    not isinstance(call_id, str)
                    or not call_id
                    or not isinstance(name, str)
                    or not name
                    or not isinstance(arguments, Mapping)
                    or set(block).difference({"type", "id", "name", "input"})
                ):
                    unsupported.append(field)
                    continue
                if text_and_image:
                    input_items.append(
                        {
                            "type": "message",
                            "role": message.role,
                            "content": text_and_image,
                        }
                    )
                    text_and_image = []
                input_items.append(
                    {
                        "id": f"fc_{call_id}",
                        "type": "function_call",
                        "call_id": call_id,
                        "name": name,
                        "arguments": json.dumps(arguments, ensure_ascii=True, separators=(",", ":")),
                    }
                )
                continue
            if block_type == "tool_result" and message.role == "user":
                call_id = block.get("tool_use_id")
                output, failure = _tool_result_output(block.get("content"), f"{field}.content")
                if (
                    not isinstance(call_id, str)
                    or not call_id
                    or failure is not None
                    or output is None
                    or set(block).difference({"type", "tool_use_id", "content"})
                ):
                    unsupported.append(failure or field)
                    continue
                if text_and_image:
                    input_items.append(
                        {
                            "type": "message",
                            "role": message.role,
                            "content": text_and_image,
                        }
                    )
                    text_and_image = []
                input_items.append(
                    {
                        "type": "function_call_output",
                        "call_id": call_id,
                        "output": output,
                    }
                )
                continue
            unsupported.append(field)
        if text_and_image:
            input_items.append(
                {
                    "type": "message",
                    "role": message.role,
                    "content": text_and_image,
                }
            )
    return input_items, unsupported


def _tool_history_failures(messages: tuple[AnthropicMessage, ...]) -> list[str]:
    """Reject incomplete, reordered, or unmatched tool-call history."""

    pending_tool_ids: dict[str, str] = {}
    unsupported: list[str] = []
    for message_index, message in enumerate(messages):
        if pending_tool_ids and message.role != "user":
            unsupported.extend(f"{field}.unresolved" for field in pending_tool_ids.values())
            pending_tool_ids.clear()
        seen_non_tool_result = False
        for block_index, block in enumerate(message.content):
            field = f"messages[{message_index}].content[{block_index}]"
            block_type = block.get("type")
            if message.role == "assistant" and block_type == "tool_use":
                call_id = block.get("id")
                if isinstance(call_id, str) and call_id:
                    if call_id in pending_tool_ids:
                        unsupported.append(f"{field}.id")
                    else:
                        pending_tool_ids[call_id] = field
                continue
            if message.role == "user" and block_type == "tool_result":
                call_id = block.get("tool_use_id")
                if seen_non_tool_result:
                    unsupported.append(f"{field}.tool_result_order")
                if not isinstance(call_id, str) or call_id not in pending_tool_ids:
                    unsupported.append(f"{field}.tool_use_id")
                else:
                    pending_tool_ids.pop(call_id)
                continue
            if message.role == "user":
                seen_non_tool_result = True
        if message.role == "user" and pending_tool_ids:
            unsupported.extend(f"{field}.unresolved" for field in pending_tool_ids.values())
            pending_tool_ids.clear()
    if pending_tool_ids:
        unsupported.extend(f"{field}.unresolved" for field in pending_tool_ids.values())
    return unsupported


def _system_to_instructions(value: Any) -> tuple[str | None, list[str], list[str]]:
    if value is None:
        return None, [], []
    blocks = _content_blocks(value)
    if blocks is None:
        return None, [], ["system"]
    texts: list[str] = []
    for index, block in enumerate(blocks):
        item, failure = _text_input(block, f"system[{index}]")
        if failure is not None or item is None:
            return None, [], [failure or f"system[{index}]"]
        texts.append(item["text"])
    return "\n\n".join(texts), (["system.text_blocks_joined"] if len(texts) > 1 else []), []


def _tools_to_responses(value: Any) -> tuple[list[dict[str, Any]], list[str]]:
    if value is None:
        return [], []
    if not isinstance(value, list):
        return [], ["tools"]
    tools: list[dict[str, Any]] = []
    unsupported: list[str] = []
    for index, raw_tool in enumerate(value):
        field = f"tools[{index}]"
        if not isinstance(raw_tool, Mapping):
            unsupported.append(field)
            continue
        tool = dict(raw_tool)
        name = tool.get("name")
        schema = tool.get("input_schema")
        if (
            tool.get("type", "custom") != "custom"
            or not isinstance(name, str)
            or not name
            or not isinstance(schema, Mapping)
            or set(tool).difference({"type", "name", "description", "input_schema", "strict"})
        ):
            unsupported.append(field)
            continue
        translated: dict[str, Any] = {
            "type": "function",
            "name": name,
            "parameters": dict(schema),
        }
        if isinstance(tool.get("description"), str):
            translated["description"] = tool["description"]
        if isinstance(tool.get("strict"), bool):
            translated["strict"] = tool["strict"]
        tools.append(translated)
    return tools, unsupported


def _tool_choice_to_responses(value: Any) -> tuple[Any, list[str]]:
    if value is None:
        return None, []
    if not isinstance(value, Mapping):
        return None, ["tool_choice"]
    choice_type = value.get("type")
    if choice_type == "auto" and set(value) == {"type"}:
        return "auto", []
    if choice_type == "any" and set(value) == {"type"}:
        return "required", []
    if choice_type == "none" and set(value) == {"type"}:
        return "none", []
    if choice_type == "tool" and isinstance(value.get("name"), str) and set(value) == {"type", "name"}:
        return {"type": "function", "name": value["name"]}, []
    return None, ["tool_choice"]


def messages_to_responses(request: Mapping[str, Any]) -> TranslationResult:
    """Translate an Anthropic Messages request into a Responses request.

    The return value refuses to provide a serializable body when an unmodelled
    field is present.  Production code must make an explicit policy decision
    before accepting any new Messages capability.
    """

    unsupported = [
        f"request.{key}"
        for key in request
        if key not in _SUPPORTED_TOP_LEVEL_FIELDS
    ]
    adapted: list[str] = []
    model = request.get("model")
    if not isinstance(model, str) or not model:
        unsupported.append("model")
    max_tokens = request.get("max_tokens")
    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens <= 0:
        unsupported.append("max_tokens")

    messages, message_failures = _parse_messages(request.get("messages"))
    unsupported.extend(message_failures)
    unsupported.extend(_tool_history_failures(messages))
    input_items, input_failures = _messages_to_responses_input(messages)
    unsupported.extend(input_failures)

    instructions, system_adapted, system_failures = _system_to_instructions(request.get("system"))
    adapted.extend(system_adapted)
    unsupported.extend(system_failures)

    tools, tool_failures = _tools_to_responses(request.get("tools"))
    unsupported.extend(tool_failures)
    tool_choice, tool_choice_failures = _tool_choice_to_responses(request.get("tool_choice"))
    unsupported.extend(tool_choice_failures)

    if unsupported:
        return TranslationResult(
            body=None,
            adapted=tuple(dict.fromkeys(adapted)),
            unsupported=tuple(dict.fromkeys(unsupported)),
        )

    payload: dict[str, Any] = {
        "model": model,
        "max_output_tokens": max_tokens,
        "input": input_items,
    }
    if instructions is not None:
        payload["instructions"] = instructions
    for key in ("stream", "temperature", "top_p"):
        if key in request:
            payload[key] = request[key]
    if tools:
        payload["tools"] = tools
    if tool_choice is not None:
        payload["tool_choice"] = tool_choice
    return TranslationResult(
        body=json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8"),
        adapted=tuple(dict.fromkeys(adapted)),
    )


def _responses_request_to_chat_completions_body(body: bytes) -> bytes:
    payload = json.loads(body.decode("utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("Responses prototype request must be a JSON object")

    messages: list[dict[str, Any]] = []
    instructions = payload.get("instructions")
    if isinstance(instructions, str) and instructions:
        messages.append({"role": "system", "content": instructions})
    for item in payload.get("input", []):
        if not isinstance(item, Mapping):
            continue
        item_type = item.get("type")
        if item_type == "message":
            role = item.get("role")
            content = item.get("content")
            if not isinstance(role, str) or not isinstance(content, list):
                raise ValueError("Responses message input is incomplete")
            chat_content: list[dict[str, Any]] = []
            for part in content:
                if not isinstance(part, Mapping):
                    raise ValueError("Responses message content is incomplete")
                if part.get("type") == "input_text" and isinstance(part.get("text"), str):
                    chat_content.append({"type": "text", "text": part["text"]})
                else:
                    raise ValueError("Responses message content has no Chat Completions equivalent")
            if len(chat_content) == 1 and chat_content[0]["type"] == "text":
                messages.append({"role": role, "content": chat_content[0]["text"]})
            else:
                messages.append({"role": role, "content": chat_content})
        elif item_type == "function_call":
            call_id = item.get("call_id")
            name = item.get("name")
            arguments = item.get("arguments")
            if not isinstance(call_id, str) or not isinstance(name, str) or not isinstance(arguments, str):
                raise ValueError("Responses function call is incomplete")
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
        elif item_type == "function_call_output":
            call_id = item.get("call_id")
            output = item.get("output")
            if not isinstance(call_id, str) or not isinstance(output, str):
                raise ValueError("Responses function output is incomplete")
            messages.append({"role": "tool", "tool_call_id": call_id, "content": output})
        else:
            raise ValueError("Responses input item has no Chat Completions equivalent")

    chat_payload: dict[str, Any] = {"model": payload.get("model"), "messages": messages}
    if "max_output_tokens" in payload:
        chat_payload["max_tokens"] = payload["max_output_tokens"]
    for key in ("stream", "temperature", "top_p"):
        if key in payload:
            chat_payload[key] = payload[key]
    tools = payload.get("tools")
    if isinstance(tools, list) and tools:
        chat_tools: list[dict[str, Any]] = []
        for tool in tools:
            if not isinstance(tool, Mapping) or tool.get("type") != "function":
                raise ValueError("Responses tool has no Chat Completions equivalent")
            function: dict[str, Any] = {
                "name": tool.get("name"),
                "parameters": tool.get("parameters"),
            }
            if isinstance(tool.get("description"), str):
                function["description"] = tool["description"]
            if isinstance(tool.get("strict"), bool):
                function["strict"] = tool["strict"]
            chat_tools.append({"type": "function", "function": function})
        chat_payload["tools"] = chat_tools
    tool_choice = payload.get("tool_choice")
    if isinstance(tool_choice, Mapping) and tool_choice.get("type") == "function":
        name = tool_choice.get("name")
        if not isinstance(name, str):
            raise ValueError("Responses function tool choice is incomplete")
        chat_payload["tool_choice"] = {"type": "function", "function": {"name": name}}
    elif tool_choice is not None:
        chat_payload["tool_choice"] = tool_choice
    return json.dumps(chat_payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


def messages_to_chat_completions(request: Mapping[str, Any]) -> TranslationResult:
    """Translate the supported Messages subset into an OpenAI Chat request."""

    responses_result = messages_to_responses(request)
    if responses_result.body is None:
        return responses_result
    return TranslationResult(
        body=_responses_request_to_chat_completions_body(responses_result.body),
        adapted=responses_result.adapted + ("responses_to_chat.request_shape",),
        unsupported=responses_result.unsupported,
    )


def _sse_record(event_name: str, payload: Mapping[str, Any]) -> bytes:
    encoded = json.dumps(payload, ensure_ascii=True, separators=(",", ":"))
    return f"event: {event_name}\ndata: {encoded}\n\n".encode("utf-8")


def upstream_error_to_messages_sse(
    status: int,
    payload: Mapping[str, Any],
) -> tuple[bytes, ...]:
    """Translate a deterministic upstream error into the Messages SSE envelope.

    The upstream message is intentionally retained for Claude Code's documented
    capability fallback matching.  Trace capture must sanitize it separately.
    """

    error_type_by_status = {
        400: "invalid_request_error",
        401: "authentication_error",
        403: "permission_error",
        404: "not_found_error",
        413: "request_too_large",
        429: "rate_limit_error",
        529: "overloaded_error",
    }
    raw_error = payload.get("error")
    if isinstance(raw_error, Mapping):
        message = raw_error.get("message")
    else:
        message = None
    if not isinstance(message, str) or not message:
        message = f"Upstream request failed with status {status}"
    error_type = error_type_by_status.get(status, "api_error")
    return (
        _sse_record(
            "error",
            {
                "type": "error",
                "error": {"type": error_type, "message": message},
            },
        ),
    )


def _usage_count(value: Mapping[str, Any], field: str, source: str) -> int | None:
    if field not in value:
        return None
    count = value[field]
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        raise ValueError(f"{source} usage field {field} is not a non-negative integer")
    return count


def _validate_usage_total(
    value: Mapping[str, Any],
    input_tokens: int | None,
    output_tokens: int | None,
    source: str,
) -> None:
    total_tokens = _usage_count(value, "total_tokens", source)
    if total_tokens is None:
        return
    if input_tokens is None or output_tokens is None or total_tokens != input_tokens + output_tokens:
        raise ValueError(f"{source} total_tokens cannot be represented from input/output tokens")


def _anthropic_usage(value: Any) -> dict[str, int]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("Responses usage is not an object")
    unknown_fields = sorted(str(field) for field in set(value).difference({"input_tokens", "output_tokens", "total_tokens"}))
    if unknown_fields:
        raise ValueError(f"Unsupported Responses usage field: {unknown_fields[0]}")
    input_tokens = _usage_count(value, "input_tokens", "Responses")
    output_tokens = _usage_count(value, "output_tokens", "Responses")
    _validate_usage_total(value, input_tokens, output_tokens, "Responses")
    usage: dict[str, int] = {}
    if input_tokens is not None:
        usage["input_tokens"] = input_tokens
    if output_tokens is not None:
        usage["output_tokens"] = output_tokens
    return usage


def responses_events_to_messages_sse(events: list[Mapping[str, Any]]) -> tuple[bytes, ...]:
    """Adapt a finite OpenAI Responses event sequence into Anthropic SSE records.

    This in-memory prototype deliberately takes a finite list so its output can
    be inspected as a fixture.  A production adapter must preserve the same
    event ordering without buffering upstream output.
    """

    records: list[bytes] = []
    response_id = "resp_spike"
    model = "unknown"
    started = False
    next_block_index = 0
    active_blocks: dict[int, dict[str, Any]] = {}
    active_item_ids: dict[str, int] = {}
    emitted_tool_use = False
    terminal = False
    supported_event_types = {
        "response.created",
        "response.in_progress",
        "response.output_item.added",
        "response.content_part.added",
        "response.output_text.delta",
        "response.output_text.done",
        "response.function_call_arguments.delta",
        "response.function_call_arguments.done",
        "response.output_item.done",
        "response.completed",
        "response.failed",
    }

    def start_message(response: Mapping[str, Any]) -> None:
        nonlocal response_id, model, started
        if started:
            return
        candidate_id = response.get("id")
        candidate_model = response.get("model")
        if isinstance(candidate_id, str) and candidate_id:
            response_id = candidate_id
        if isinstance(candidate_model, str) and candidate_model:
            model = candidate_model
        records.append(
            _sse_record(
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": response_id,
                        "type": "message",
                        "role": "assistant",
                        "model": model,
                        "content": [],
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": _anthropic_usage(response.get("usage")),
                    },
                },
            )
        )
        started = True

    def emit_tool_argument_delta(active: Mapping[str, Any], value: str) -> None:
        records.append(
            _sse_record(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": active["index"],
                    "delta": {"type": "input_json_delta", "partial_json": value},
                },
            )
        )

    def emit_text_delta(active: Mapping[str, Any], value: str) -> None:
        records.append(
            _sse_record(
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": active["index"],
                    "delta": {"type": "text_delta", "text": value},
                },
            )
        )

    def validate_text_snapshot(active: dict[str, Any], snapshot: Any) -> None:
        if not isinstance(snapshot, str):
            raise ValueError("Responses output text snapshot is missing")
        streamed_text = "".join(active["text"])
        if streamed_text and streamed_text != snapshot:
            raise ValueError("Responses text deltas do not match final text")
        if not streamed_text:
            active["text"].append(snapshot)
            emit_text_delta(active, snapshot)

    def validate_tool_arguments(active: dict[str, Any], fallback: Any = None) -> None:
        arguments = "".join(active["arguments"])
        if isinstance(fallback, str):
            if arguments and arguments != fallback:
                raise ValueError("Responses tool argument deltas do not match final arguments")
            if not arguments:
                arguments = fallback
                active["arguments"].append(fallback)
                emit_tool_argument_delta(active, fallback)
        try:
            parsed = json.loads(arguments)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("Responses tool arguments must be a JSON object") from exc
        if not isinstance(parsed, Mapping):
            raise ValueError("Responses tool arguments must be a JSON object")

    for event in events:
        if not isinstance(event, Mapping):
            raise ValueError("Responses stream event is not an object")
        event_type = event.get("type")
        if not isinstance(event_type, str) or event_type not in supported_event_types:
            raise ValueError(f"Unsupported Responses event type: {event_type}")
        if terminal:
            raise ValueError("Responses stream has an event after its terminal event")
        response = event.get("response")
        if event_type == "response.created":
            start_message(response if isinstance(response, Mapping) else {})
            continue
        if event_type in {
            "response.in_progress",
            "response.content_part.added",
        }:
            # These confirmed upstream transitions are represented by the
            # Messages start/delta/stop events emitted around their item.
            continue
        if event_type == "response.output_item.added":
            start_message({})
            item = event.get("item")
            if not isinstance(item, Mapping):
                raise ValueError("Responses output item is not an object")
            output_index = event.get("output_index")
            if not isinstance(output_index, int):
                output_index = len(active_blocks)
            item_type = item.get("type")
            if item_type == "message":
                block = {"type": "text", "text": ""}
                block_kind = "text"
            elif item_type == "function_call":
                call_id = item.get("call_id")
                name = item.get("name")
                if not isinstance(call_id, str) or not call_id or not isinstance(name, str) or not name:
                    raise ValueError("Responses function call is missing call_id or name")
                block = {"type": "tool_use", "id": call_id, "name": name, "input": {}}
                block_kind = "tool_use"
                emitted_tool_use = True
            else:
                raise ValueError(f"Unsupported Responses output item type: {item_type}")
            block_index = next_block_index
            next_block_index += 1
            active: dict[str, Any] = {"index": block_index, "kind": block_kind}
            if block_kind == "text":
                active["text"] = []
            elif block_kind == "tool_use":
                active["arguments"] = []
            active_blocks[output_index] = active
            item_id = item.get("id")
            if isinstance(item_id, str) and item_id:
                active_item_ids[item_id] = output_index
            records.append(
                _sse_record(
                    "content_block_start",
                    {"type": "content_block_start", "index": block_index, "content_block": block},
                )
            )
            initial_arguments = item.get("arguments")
            if block_kind == "tool_use" and isinstance(initial_arguments, str) and initial_arguments:
                active["arguments"].append(initial_arguments)
                emit_tool_argument_delta(active, initial_arguments)
            continue
        if event_type in {"response.output_text.delta", "response.function_call_arguments.delta"}:
            output_index = event.get("output_index")
            if not isinstance(output_index, int):
                item_id = event.get("item_id")
                output_index = active_item_ids.get(item_id) if isinstance(item_id, str) else None
            active = active_blocks.get(output_index) if isinstance(output_index, int) else None
            delta = event.get("delta")
            if active is None or not isinstance(delta, str):
                raise ValueError("Responses delta has no active compatible content block")
            if event_type == "response.output_text.delta" and active["kind"] == "text":
                active["text"].append(delta)
                downstream_delta = {"type": "text_delta", "text": delta}
            elif event_type == "response.function_call_arguments.delta" and active["kind"] == "tool_use":
                active["arguments"].append(delta)
                downstream_delta = {"type": "input_json_delta", "partial_json": delta}
            else:
                raise ValueError("Responses delta does not match its active content block")
            records.append(
                _sse_record(
                    "content_block_delta",
                    {"type": "content_block_delta", "index": active["index"], "delta": downstream_delta},
                )
            )
            continue
        if event_type == "response.output_text.done":
            output_index = event.get("output_index")
            if not isinstance(output_index, int):
                item_id = event.get("item_id")
                output_index = active_item_ids.get(item_id) if isinstance(item_id, str) else None
            active = active_blocks.get(output_index) if isinstance(output_index, int) else None
            if active is None or active["kind"] != "text":
                raise ValueError("Responses output text has no active text block")
            validate_text_snapshot(active, event.get("text"))
            continue
        if event_type == "response.function_call_arguments.done":
            output_index = event.get("output_index")
            if not isinstance(output_index, int):
                item_id = event.get("item_id")
                output_index = active_item_ids.get(item_id) if isinstance(item_id, str) else None
            active = active_blocks.get(output_index) if isinstance(output_index, int) else None
            if active is None or active["kind"] != "tool_use":
                raise ValueError("Responses function call arguments have no active tool block")
            arguments = event.get("arguments")
            if not isinstance(arguments, str):
                raise ValueError("Responses function call arguments are missing")
            streamed_arguments = "".join(active["arguments"])
            if streamed_arguments and streamed_arguments != arguments:
                raise ValueError("Responses tool argument deltas do not match final arguments")
            if not streamed_arguments:
                active["arguments"].append(arguments)
                emit_tool_argument_delta(active, arguments)
            validate_tool_arguments(active)
            continue
        if event_type == "response.output_item.done":
            output_index = event.get("output_index")
            item = event.get("item")
            if not isinstance(output_index, int):
                item_id = item.get("id") if isinstance(item, Mapping) else None
                output_index = active_item_ids.get(item_id) if isinstance(item_id, str) else None
            active = active_blocks.get(output_index) if isinstance(output_index, int) else None
            if active is None:
                raise ValueError("Responses output item stop has no active content block")
            if active["kind"] == "tool_use":
                fallback = item.get("arguments") if isinstance(item, Mapping) else None
                validate_tool_arguments(active, fallback)
            active_blocks.pop(output_index)
            records.append(
                _sse_record(
                    "content_block_stop",
                    {"type": "content_block_stop", "index": active["index"]},
                )
            )
            continue
        if event_type == "response.completed":
            response_payload = response if isinstance(response, Mapping) else {}
            start_message(response_payload)
            for active in active_blocks.values():
                if active["kind"] == "tool_use":
                    validate_tool_arguments(active)
                records.append(
                    _sse_record(
                        "content_block_stop",
                        {"type": "content_block_stop", "index": active["index"]},
                    )
                )
            active_blocks.clear()
            usage = _anthropic_usage(response_payload.get("usage"))
            records.append(
                _sse_record(
                    "message_delta",
                    {
                        "type": "message_delta",
                        "delta": {
                            "stop_reason": "tool_use" if emitted_tool_use else "end_turn",
                            "stop_sequence": None,
                        },
                        "usage": usage,
                    },
                )
            )
            records.append(_sse_record("message_stop", {"type": "message_stop"}))
            terminal = True
            continue
        if event_type == "response.failed":
            response_payload = response if isinstance(response, Mapping) else {}
            status = event.get("status")
            if not isinstance(status, int):
                status = 500
            error_payload = {"error": response_payload.get("error")}
            records.extend(upstream_error_to_messages_sse(status, error_payload))
            terminal = True
            continue

    if not terminal:
        raise ValueError("Responses event sequence ended without response.completed")
    return tuple(records)


def _chat_usage_to_responses_usage(value: Any) -> dict[str, int]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("Chat Completions usage is not an object")
    unknown_fields = sorted(
        str(field)
        for field in set(value).difference(
            {"prompt_tokens", "completion_tokens", "input_tokens", "output_tokens", "total_tokens"}
        )
    )
    if unknown_fields:
        raise ValueError(f"Unsupported Chat Completions usage field: {unknown_fields[0]}")
    prompt_tokens = _usage_count(value, "prompt_tokens", "Chat Completions")
    input_alias = _usage_count(value, "input_tokens", "Chat Completions")
    completion_tokens = _usage_count(value, "completion_tokens", "Chat Completions")
    output_alias = _usage_count(value, "output_tokens", "Chat Completions")
    if prompt_tokens is not None and input_alias is not None and prompt_tokens != input_alias:
        raise ValueError("Chat Completions input token aliases disagree")
    if completion_tokens is not None and output_alias is not None and completion_tokens != output_alias:
        raise ValueError("Chat Completions output token aliases disagree")
    input_tokens = prompt_tokens if prompt_tokens is not None else input_alias
    output_tokens = completion_tokens if completion_tokens is not None else output_alias
    _validate_usage_total(value, input_tokens, output_tokens, "Chat Completions")
    usage: dict[str, int] = {}
    if input_tokens is not None:
        usage["input_tokens"] = input_tokens
    if output_tokens is not None:
        usage["output_tokens"] = output_tokens
    return usage


def _reject_unsupported_chat_fields(value: Mapping[str, Any], allowed: set[str], scope: str) -> None:
    unsupported_fields = sorted(str(field) for field in set(value).difference(allowed))
    if unsupported_fields:
        raise ValueError(f"Unsupported Chat Completions {scope} field: {unsupported_fields[0]}")


def _chat_chunks_to_responses_events(chunks: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if not chunks:
        raise ValueError("Chat Completions stream is empty")

    events: list[dict[str, Any]] = []
    response_id = "resp_chat_spike"
    model = "unknown"
    established_chunk_id: str | None = None
    established_chunk_model: str | None = None
    created = False
    next_output_index = 0
    text_output_index: int | None = None
    tool_states: dict[int, dict[str, Any]] = {}
    tool_id_indexes: dict[str, int] = {}
    terminal = False
    latest_usage: dict[str, int] | None = None

    for chunk in chunks:
        if not isinstance(chunk, Mapping):
            raise ValueError("Chat Completions stream chunk is not an object")
        _reject_unsupported_chat_fields(
            chunk,
            {"id", "model", "choices", "usage", "service_tier", "system_fingerprint"},
            "chunk",
        )
        for nullable_field in ("service_tier", "system_fingerprint"):
            if chunk.get(nullable_field) is not None:
                raise ValueError(f"Unsupported Chat Completions chunk field: {nullable_field}")
        chunk_id = chunk.get("id")
        chunk_model = chunk.get("model")
        if "id" in chunk and (not isinstance(chunk_id, str) or not chunk_id):
            raise ValueError("Chat Completions chunk id is not a non-empty string")
        if "model" in chunk and (not isinstance(chunk_model, str) or not chunk_model):
            raise ValueError("Chat Completions chunk model is not a non-empty string")
        if isinstance(chunk_id, str):
            if established_chunk_id is not None and chunk_id != established_chunk_id:
                raise ValueError("Chat Completions chunk id changed during stream")
            established_chunk_id = chunk_id
            response_id = chunk_id
        if isinstance(chunk_model, str):
            if established_chunk_model is not None and chunk_model != established_chunk_model:
                raise ValueError("Chat Completions chunk model changed during stream")
            established_chunk_model = chunk_model
            model = chunk_model
        if not created:
            events.append(
                {
                    "type": "response.created",
                    "response": {"id": response_id, "model": model, "status": "in_progress"},
                }
            )
            created = True
        if "usage" in chunk:
            latest_usage = _chat_usage_to_responses_usage(chunk.get("usage"))
        if "choices" not in chunk:
            continue
        choices = chunk["choices"]
        if not isinstance(choices, list):
            raise ValueError("Chat Completions choices is not a list")
        if len(choices) > 1:
            raise ValueError("Multiple Chat Completions choices are unsupported")
        for choice in choices:
            if not isinstance(choice, Mapping):
                raise ValueError("Chat Completions choice is not an object")
            _reject_unsupported_chat_fields(choice, {"index", "delta", "finish_reason", "logprobs"}, "choice")
            if choice.get("logprobs") is not None:
                raise ValueError("Unsupported Chat Completions choice field: logprobs")
            choice_index = choice.get("index")
            if "index" in choice and (
                isinstance(choice_index, bool) or not isinstance(choice_index, int) or choice_index < 0
            ):
                raise ValueError("Chat Completions choice index is not a non-negative integer")
            if isinstance(choice_index, int) and choice_index != 0:
                raise ValueError(f"Unsupported Chat Completions choice index: {choice_index}")
            if "delta" not in choice:
                delta = {}
            else:
                delta = choice["delta"]
                if not isinstance(delta, Mapping):
                    raise ValueError("Chat Completions delta is not an object")
            _reject_unsupported_chat_fields(
                delta,
                {"role", "content", "tool_calls", "function_call", "refusal"},
                "delta",
            )
            for nullable_field in ("function_call", "refusal"):
                if delta.get(nullable_field) is not None:
                    raise ValueError(f"Unsupported Chat Completions delta field: {nullable_field}")
            role = delta.get("role")
            if "role" in delta and role is not None and role != "assistant":
                raise ValueError("Chat Completions delta role is not assistant or null")
            content = delta.get("content")
            if "content" in delta and content is not None and not isinstance(content, str):
                raise ValueError("Chat Completions delta content is not a string or null")
            if isinstance(content, str) and content:
                if text_output_index is None:
                    text_output_index = next_output_index
                    next_output_index += 1
                    events.append(
                        {
                            "type": "response.output_item.added",
                            "output_index": text_output_index,
                            "item": {
                                "id": f"msg_{response_id}",
                                "type": "message",
                                "role": "assistant",
                                "content": [],
                            },
                        }
                    )
                events.append(
                    {
                        "type": "response.output_text.delta",
                        "output_index": text_output_index,
                        "delta": content,
                    }
                )
            raw_tool_calls = delta.get("tool_calls")
            if "tool_calls" in delta and raw_tool_calls is not None and not isinstance(raw_tool_calls, list):
                raise ValueError("Chat Completions delta tool_calls is not a list or null")
            if isinstance(raw_tool_calls, list):
                for fallback_index, raw_call in enumerate(raw_tool_calls):
                    if not isinstance(raw_call, Mapping):
                        raise ValueError("Chat Completions tool call is not an object")
                    _reject_unsupported_chat_fields(
                        raw_call,
                        {"index", "id", "type", "function"},
                        "tool call",
                    )
                    if "index" not in raw_call:
                        tool_index = fallback_index
                    else:
                        tool_index = raw_call["index"]
                        if isinstance(tool_index, bool) or not isinstance(tool_index, int) or tool_index < 0:
                            raise ValueError("Chat Completions tool call index is not a non-negative integer")
                    call_type = raw_call.get("type")
                    if "type" in raw_call and call_type is not None and call_type != "function":
                        raise ValueError("Chat Completions tool call type is not function or null")
                    function = raw_call.get("function")
                    if not isinstance(function, Mapping):
                        raise ValueError("Chat Completions tool call has no function")
                    _reject_unsupported_chat_fields(function, {"name", "arguments"}, "function")
                    arguments = function.get("arguments")
                    if "arguments" in function and arguments is not None and not isinstance(arguments, str):
                        raise ValueError("Chat Completions function arguments is not a string or null")
                    call_id = raw_call.get("id")
                    if "id" in raw_call and call_id is not None and (
                        not isinstance(call_id, str) or not call_id
                    ):
                        raise ValueError("Chat Completions tool call id is not a non-empty string or null")
                    name = function.get("name")
                    if "name" in function and name is not None and (not isinstance(name, str) or not name):
                        raise ValueError("Chat Completions function name is not a non-empty string or null")
                    state = tool_states.get(tool_index)
                    if state is None:
                        if not isinstance(call_id, str) or not call_id or not isinstance(name, str) or not name:
                            raise ValueError("First Chat Completions tool delta needs id and function name")
                        previous_index = tool_id_indexes.get(call_id)
                        if previous_index is not None and previous_index != tool_index:
                            raise ValueError(
                                f"Chat Completions tool call id reused for indexes {previous_index} and {tool_index}"
                            )
                        tool_id_indexes[call_id] = tool_index
                        output_index = next_output_index
                        next_output_index += 1
                        state = {"output_index": output_index, "call_id": call_id, "name": name}
                        tool_states[tool_index] = state
                        events.append(
                            {
                                "type": "response.output_item.added",
                                "output_index": output_index,
                                "item": {
                                    "id": f"fc_{call_id}",
                                    "type": "function_call",
                                    "call_id": call_id,
                                    "name": name,
                                    "arguments": "",
                                },
                            }
                        )
                    else:
                        if call_id is not None and call_id != state["call_id"]:
                            raise ValueError(f"Chat Completions tool call id changed for index {tool_index}")
                        if name is not None and name != state["name"]:
                            raise ValueError(f"Chat Completions function name changed for index {tool_index}")
                    if isinstance(arguments, str) and arguments:
                        events.append(
                            {
                                "type": "response.function_call_arguments.delta",
                                "output_index": state["output_index"],
                                "delta": arguments,
                            }
                        )
            finish_reason = choice.get("finish_reason")
            if "finish_reason" in choice and finish_reason is not None and not isinstance(finish_reason, str):
                raise ValueError("Chat Completions finish_reason is not a string or null")
            if isinstance(finish_reason, str) and finish_reason not in {"stop", "tool_calls"}:
                raise ValueError(f"Unsupported Chat Completions finish_reason: {finish_reason}")
            if finish_reason is not None:
                terminal = True

    if not terminal:
        raise ValueError("Chat Completions stream ended without finish_reason")
    if text_output_index is not None:
        events.append(
            {
                "type": "response.output_item.done",
                "output_index": text_output_index,
                "item": {"id": f"msg_{response_id}", "type": "message", "role": "assistant", "content": []},
            }
        )
    for state in tool_states.values():
        events.append(
            {
                "type": "response.output_item.done",
                "output_index": state["output_index"],
                "item": {
                    "id": f"fc_{state['call_id']}",
                    "type": "function_call",
                    "call_id": state["call_id"],
                    "name": state["name"],
                },
            }
        )
    completed_response: dict[str, Any] = {
        "id": response_id,
        "model": model,
        "status": "completed",
    }
    if latest_usage is not None:
        completed_response["usage"] = latest_usage
    events.append({"type": "response.completed", "response": completed_response})
    return events


def chat_chunks_to_messages_sse(chunks: list[Mapping[str, Any]]) -> tuple[bytes, ...]:
    """Adapt a Chat Completions tool/text stream through the isolated seam."""

    return responses_events_to_messages_sse(_chat_chunks_to_responses_events(chunks))


def exercise_responses_upstream(
    request: Mapping[str, Any],
    response_events: list[Mapping[str, Any]],
) -> PrototypeExchange:
    """Exercise a Responses-shaped upstream without touching the HTTP handler."""

    translation = messages_to_responses(request)
    if translation.body is None:
        return PrototypeExchange("responses", translation, None, ())
    return PrototypeExchange(
        "responses",
        translation,
        translation.body,
        responses_events_to_messages_sse(response_events),
    )


def exercise_chat_completions_upstream(
    request: Mapping[str, Any],
    chunks: list[Mapping[str, Any]],
) -> PrototypeExchange:
    """Exercise a Chat Completions-shaped upstream without touching the HTTP handler."""

    translation = messages_to_chat_completions(request)
    if translation.body is None:
        return PrototypeExchange("chat_completions", translation, None, ())
    return PrototypeExchange(
        "chat_completions",
        translation,
        translation.body,
        chat_chunks_to_messages_sse(chunks),
    )
