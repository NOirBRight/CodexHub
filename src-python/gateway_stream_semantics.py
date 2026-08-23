"""Streaming semantics for Gateway SSE and protocol-translation wrappers.

Frame assembly stays in ``gateway_sse``; this module owns observers, terminal
detection, lifecycle-final checks, and the thin ``protocol_translation``
adapters used on the request path.
"""

from __future__ import annotations

from functools import partial
import html
import json
import re
import time
import uuid
from collections.abc import Mapping
from typing import Any

from gateway_events import (
    RUNTIME_CODEX_DIR,
    _usage_from_payload,
    write_adapter_event as _write_adapter_event,
)
from gateway_errors import (
    LifecycleEmptyFinalResponseError,
    LifecycleFinalFormatResponseError,
    UpstreamProtocolTranslationError,
)
from gateway_settings import lifecycle_empty_final_resample_enabled
from gateway_sse import _parse_sse_json_payload
from gateway_transport import _get_header, bind_transport_failure_types
from protocol_translation import (
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
    responses_input_to_chat_messages,
    responses_request_to_chat_completion_body,
    responses_tool_choice_to_chat_tool_choice,
    responses_tools_to_chat_tools,
)
from route_primitives import (
    IMAGE_PROXY_PROGRESS_TEXT,
    RETRY_REQUEST_COMPACT,
    RETRY_REQUEST_MAIN_GENERATION,
)
from sse_events import SseEvent
from tool_surface_adapter import NODE_REPL_NAMESPACE, THIRD_PARTY_TOOL_NAME_ALIASES
import proxy_telemetry

MULTI_AGENT_TOOL_NAMES = {
    "spawn_agent",
    "wait_agent",
    "close_agent",
    "resume_agent",
    "send_input",
}
REASONING_TEXT_EVENT_PREFIXES = (
    "response.reasoning_text.",
    "response.reasoning_content.",
    "response.reasoning_raw_content.",
)
REASONING_SUMMARY_EVENT_PREFIX = "response.reasoning_summary_text."


def _collect_text_fragments(value: Any) -> list[str]:
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, list):
        fragments: list[str] = []
        for item in value:
            fragments.extend(_collect_text_fragments(item))
        return fragments
    if isinstance(value, dict):
        fragments: list[str] = []
        for key in ("text", "content", "summary", "message"):
            if key in value:
                fragments.extend(_collect_text_fragments(value[key]))
        return fragments
    return []


def _hide_reasoning_text(value: Any) -> bool:
    changed = False
    if isinstance(value, list):
        for item in value:
            if _hide_reasoning_text(item):
                changed = True
        return changed
    if not isinstance(value, dict):
        return False
    if value.get("type") == "reasoning":
        summary = value.get("summary")
        valid_summary = (
            [
                {"type": "summary_text", "text": item["text"]}
                for item in summary
                if isinstance(item, dict)
                and item.get("type") == "summary_text"
                and isinstance(item.get("text"), str)
            ]
            if isinstance(summary, list)
            else []
        )
        if summary != valid_summary:
            value["summary"] = valid_summary
            changed = True
        for key in ("content", "raw_content", "reasoning_content", "thinking", "encrypted_content"):
            if key in value:
                value.pop(key, None)
                changed = True
    for item in value.values():
        if _hide_reasoning_text(item):
            changed = True
    return changed



def _message_item_visible_text(*args: Any, **kwargs: Any) -> Any:
    from gateway_compat import lookup
    return lookup("_message_item_visible_text")(*args, **kwargs)


def _valid_tool_name(*args: Any, **kwargs: Any) -> Any:
    from gateway_compat import lookup
    return lookup("_valid_tool_name")(*args, **kwargs)


def _codex_apps_namespace_flat_alias(*args: Any, **kwargs: Any) -> Any:
    from gateway_compat import lookup
    return lookup("_codex_apps_namespace_flat_alias")(*args, **kwargs)


def _supports_explicit_namespace_alias(*args: Any, **kwargs: Any) -> Any:
    from gateway_compat import lookup
    return lookup("_supports_explicit_namespace_alias")(*args, **kwargs)


def normalize_third_party_tool_call(*args: Any, **kwargs: Any) -> Any:
    from gateway_compat import lookup
    return lookup("_normalize_third_party_tool_call")(*args, **kwargs)


def downgrade_invalid_third_party_tool_calls(*args: Any, **kwargs: Any) -> Any:
    from gateway_compat import lookup
    return lookup("_downgrade_invalid_third_party_tool_calls")(*args, **kwargs)


def _is_raw_reasoning_stream_event(payload: Mapping[str, Any]) -> bool:
    event_type = payload.get("type")
    return isinstance(event_type, str) and event_type.startswith(REASONING_TEXT_EVENT_PREFIXES)


def _is_reasoning_summary_stream_event(payload: Mapping[str, Any]) -> bool:
    event_type = payload.get("type")
    return isinstance(event_type, str) and event_type.startswith(REASONING_SUMMARY_EVENT_PREFIX)


def _is_reasoning_text_stream_event(payload: Mapping[str, Any]) -> bool:
    return _is_raw_reasoning_stream_event(payload) or _is_reasoning_summary_stream_event(payload)



class UpstreamSseSemanticError(ValueError):
    """A complete converted SSE frame is not valid source-protocol JSON."""

    def __init__(
        self,
        message: str,
        *,
        classification: str = "upstream_protocol_error",
    ) -> None:
        self.classification = classification
        super().__init__(message)


class _RuntimeToolInverseStreamError(ValueError):
    """A runtime-tool inverse failed after converted streaming began."""

    def __init__(self, translation_error: UpstreamProtocolTranslationError) -> None:
        self.translation_error = translation_error
        super().__init__(str(translation_error))


def _verified_converted_sse_semantic_error(
    source_format: str,
) -> UpstreamSseSemanticError:
    source_label = (
        "Responses" if source_format == "responses" else "Chat Completions"
    )
    return UpstreamSseSemanticError(
        f"Upstream returned a structurally invalid complete {source_label} SSE event."
    )


def _validate_verified_converted_sse_payload(
    payload: Mapping[str, Any],
    source_format: str,
) -> None:
    def invalid_shape() -> None:
        raise _verified_converted_sse_semantic_error(source_format)

    def validate_usage(
        usage: Any,
        *,
        token_fields: tuple[str, ...],
        detail_fields: tuple[str, ...],
    ) -> None:
        if not isinstance(usage, Mapping):
            invalid_shape()
        for field in token_fields:
            if field in usage and type(usage.get(field)) is not int:
                invalid_shape()
        for field in detail_fields:
            if field not in usage:
                continue
            details = usage.get(field)
            if not isinstance(details, Mapping):
                invalid_shape()
            if any(type(count) is not int for count in details.values()):
                invalid_shape()

    def validate_error_envelope(error: Any) -> None:
        if isinstance(error, str):
            if not error:
                invalid_shape()
            return
        if not isinstance(error, Mapping):
            invalid_shape()
        message = error.get("message")
        if not isinstance(message, str) or not message:
            invalid_shape()
        if "code" in error:
            code = error.get("code")
            if code is not None and type(code) not in {str, int}:
                invalid_shape()

    if source_format == "responses":
        def validate_content_part(part: Any) -> None:
            if not isinstance(part, Mapping):
                invalid_shape()
            part_type = part.get("type")
            if not isinstance(part_type, str):
                invalid_shape()
            if part_type in {"input_text", "output_text", "text"}:
                if not isinstance(part.get("text"), str):
                    invalid_shape()
                if (
                    "annotations" in part
                    and not isinstance(part.get("annotations"), list)
                ):
                    invalid_shape()
            elif part_type == "input_image":
                if not isinstance(part.get("image_url"), str):
                    invalid_shape()
                if (
                    "detail" in part
                    and not isinstance(part.get("detail"), str)
                ):
                    invalid_shape()

        def validate_output_item(item: Any) -> None:
            if not isinstance(item, Mapping):
                invalid_shape()
            if not isinstance(item.get("type"), str):
                invalid_shape()
            for field in ("id", "status"):
                if field in item and not isinstance(item.get(field), str):
                    invalid_shape()
            if item.get("type") == "message":
                if "role" in item and not isinstance(item.get("role"), str):
                    invalid_shape()
                if "content" in item:
                    content = item.get("content")
                    if not isinstance(content, list):
                        invalid_shape()
                    for part in content:
                        validate_content_part(part)
            if item.get("type") == "function_call":
                for field in ("call_id", "namespace", "name", "arguments"):
                    if field in item and not isinstance(item.get(field), str):
                        invalid_shape()

        event_type = payload.get("type")
        if not isinstance(event_type, str):
            invalid_shape()
        if event_type in {
            "response.created",
            "response.completed",
            "response.incomplete",
            "response.failed",
        }:
            response = payload.get("response")
            if not isinstance(response, Mapping):
                invalid_shape()
            for field in ("id", "model", "status"):
                if field in response and not isinstance(response.get(field), str):
                    invalid_shape()
            if "output" in response:
                output = response.get("output")
                if not isinstance(output, list):
                    invalid_shape()
                for item in output:
                    validate_output_item(item)
            if "usage" in response:
                validate_usage(
                    response.get("usage"),
                    token_fields=("input_tokens", "output_tokens", "total_tokens"),
                    detail_fields=(
                        "input_tokens_details",
                        "output_tokens_details",
                    ),
                )
            if event_type == "response.failed":
                if "error" not in response:
                    invalid_shape()
                validate_error_envelope(response.get("error"))
        elif event_type == "error":
            if "error" not in payload:
                invalid_shape()
            validate_error_envelope(payload.get("error"))
        elif event_type == "response.output_text.delta":
            if not isinstance(payload.get("delta"), str):
                invalid_shape()
        elif event_type in {
            "response.content_part.added",
            "response.content_part.done",
        }:
            validate_content_part(payload.get("part"))
        elif event_type in {
            "response.output_item.added",
            "response.output_item.done",
        }:
            validate_output_item(payload.get("item"))
        elif event_type == "response.function_call_arguments.delta":
            if (
                not isinstance(payload.get("item_id"), str)
                or not isinstance(payload.get("delta"), str)
            ):
                invalid_shape()
        elif event_type == "response.function_call_arguments.done":
            if (
                not isinstance(payload.get("item_id"), str)
                or not isinstance(payload.get("arguments"), str)
            ):
                invalid_shape()
        return
    if source_format != "chat_completions":
        return
    choices = payload.get("choices")
    if "error" in payload:
        validate_error_envelope(payload.get("error"))
        if choices is None:
            return
    if not isinstance(choices, list):
        invalid_shape()
    if "usage" in payload:
        validate_usage(
            payload.get("usage"),
            token_fields=("prompt_tokens", "completion_tokens", "total_tokens"),
            detail_fields=(
                "prompt_tokens_details",
                "completion_tokens_details",
            ),
        )
    for field in ("id", "object", "model"):
        if field in payload and not isinstance(payload.get(field), str):
            invalid_shape()
    for choice in choices:
        if not isinstance(choice, Mapping):
            invalid_shape()
        if "index" in choice and type(choice.get("index")) is not int:
            invalid_shape()
        if "delta" in choice and not isinstance(choice.get("delta"), Mapping):
            invalid_shape()
        delta = choice.get("delta")
        if isinstance(delta, Mapping):
            content = delta.get("content")
            if content is not None and not isinstance(content, str):
                invalid_shape()
            tool_calls = delta.get("tool_calls")
            if tool_calls is not None:
                if not isinstance(tool_calls, list):
                    invalid_shape()
                for tool_call in tool_calls:
                    if not isinstance(tool_call, Mapping):
                        invalid_shape()
                    if (
                        "index" in tool_call
                        and type(tool_call.get("index")) is not int
                    ):
                        invalid_shape()
                    for field in ("id", "type"):
                        if (
                            field in tool_call
                            and not isinstance(tool_call.get(field), str)
                        ):
                            invalid_shape()
                    function = tool_call.get("function")
                    if function is not None:
                        if not isinstance(function, Mapping):
                            invalid_shape()
                        for field in ("name", "arguments"):
                            if (
                                field in function
                                and not isinstance(function.get(field), str)
                            ):
                                invalid_shape()
        finish_reason = choice.get("finish_reason")
        if finish_reason is not None and not isinstance(finish_reason, str):
            invalid_shape()


def _converted_sse_payload(
    event: SseEvent,
    *,
    verified_source_format: str | None = None,
) -> Mapping[str, Any] | str | None:
    if not any(line.name == b"data" for line in event.lines) or not event.data:
        return None
    if event.data == b"[DONE]":
        return "[DONE]"
    try:
        payload = json.loads(event.data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpstreamSseSemanticError(
            "Upstream returned a malformed complete SSE event."
        ) from exc
    if not isinstance(payload, Mapping):
        raise UpstreamSseSemanticError(
            "Upstream returned a structurally invalid complete SSE event."
        )
    if verified_source_format is not None:
        _validate_verified_converted_sse_payload(payload, verified_source_format)
    return payload


def _responses_sse_event_resets_idle_timeout(event: SseEvent) -> bool:
    try:
        payload = _converted_sse_payload(event)
    except UpstreamSseSemanticError:
        return False
    if not isinstance(payload, Mapping):
        return False
    event_type = payload.get("type")
    return isinstance(event_type, str) and (event_type.startswith("response.") or event_type == "error")


def _chat_sse_event_resets_idle_timeout(event: SseEvent) -> bool:
    try:
        payload = _converted_sse_payload(event)
    except UpstreamSseSemanticError:
        return False
    return payload == "[DONE]" or isinstance(payload, Mapping)


RESPONSES_TERMINAL_EVENT_TYPES = {
    "response.completed",
    "response.failed",
    "response.incomplete",
    "error",
}


def _responses_terminal_observer(
    event_name: str | None,
    data: bytes,
    payload: Any,
) -> bool:
    """Responses protocol terminal predicate: [DONE] or known terminal types."""
    if data == b"[DONE]":
        return True
    event_type = event_name
    if isinstance(payload, Mapping):
        payload_type = payload.get("type")
        if isinstance(payload_type, str) and payload_type:
            event_type = payload_type
    return event_type in RESPONSES_TERMINAL_EVENT_TYPES


def _chat_terminal_observer(
    event_name: str | None,
    data: bytes,
    payload: Any,
) -> bool:
    """Chat Completions protocol terminal predicate: only the [DONE] sentinel."""
    del event_name, payload
    return data == b"[DONE]"


def _responses_events_have_terminal(events: list[Mapping[str, Any]]) -> bool:
    for event in events:
        if not isinstance(event, Mapping):
            continue
        event_type = event.get("type")
        if isinstance(event_type, str) and event_type in RESPONSES_TERMINAL_EVENT_TYPES:
            return True
    return False


def _responses_event_starts_downstream_output(event: Mapping[str, Any]) -> bool:
    event_type = event.get("type")
    if event_type in {"response.output_text.delta", "response.reasoning_summary_text.delta"}:
        delta = event.get("delta")
        return isinstance(delta, str) and bool(delta)
    if event_type == "response.output_text.done":
        text = event.get("text")
        return isinstance(text, str) and bool(text)
    if event_type == "response.reasoning_summary_text.done":
        text = event.get("text")
        return isinstance(text, str) and bool(text)
    if event_type == "response.function_call_arguments.delta":
        delta = event.get("delta")
        return isinstance(delta, str) and bool(delta)
    if event_type == "response.function_call_arguments.done":
        return True
    if event_type == "response.custom_tool_call_input.delta":
        delta = event.get("delta")
        return isinstance(delta, str) and bool(delta)
    if event_type == "response.custom_tool_call_input.done":
        return True
    if event_type in {"response.output_item.added", "response.output_item.done"}:
        item = event.get("item")
        return isinstance(item, Mapping) and item.get("type") in {"function_call", "custom_tool_call", "message"}
    return False


def _responses_event_commits_downstream_output(event: Mapping[str, Any], upstream_name: str) -> bool:
    event_type = event.get("type")
    if event_type in {"response.output_text.delta", "response.refusal.delta"}:
        delta = event.get("delta")
        return isinstance(delta, str) and bool(delta)
    if event_type == "response.output_text.done":
        text = event.get("text")
        return isinstance(text, str) and bool(text)
    if event_type == "response.refusal.done":
        refusal = event.get("refusal")
        return isinstance(refusal, str) and bool(refusal)
    if event_type == "response.reasoning_summary_text.delta":
        delta = event.get("delta")
        return isinstance(delta, str) and bool(delta)
    if event_type == "response.reasoning_summary_text.done":
        text = event.get("text")
        return isinstance(text, str) and bool(text)
    if event_type == "response.output_item.done":
        item = event.get("item")
        return isinstance(item, Mapping) and item.get("type") == "reasoning"
    return False


def _responses_output_item_has_visible_or_tool_output(item: Mapping[str, Any]) -> bool:
    item_type = item.get("type")
    if item_type in {"function_call", "custom_tool_call"}:
        return _responses_completed_tool_item(item) is not None
    # `tool_search_call` is a client-executed Responses output item.  The
    # Gateway must not execute or rewrite it, but it is still a completed
    # control/tool item and therefore makes a third-party response non-empty.
    # Without this classification the relay buffers the entire stream and
    # rejects the valid `response.completed` as an empty response, causing
    # Codex to reconnect before it can submit `tool_search_output`.
    if item_type == "tool_search_call":
        return (
            isinstance(item.get("call_id"), str)
            and bool(item.get("call_id"))
            and item.get("execution") == "client"
        )
    if item_type == "message":
        return bool(_message_item_visible_text(item))
    return False


def _responses_completed_event_has_visible_or_tool_output(event: Mapping[str, Any]) -> bool:
    if event.get("type") != "response.completed":
        return False
    response = event.get("response")
    if not isinstance(response, Mapping):
        return False
    output = response.get("output")
    if not isinstance(output, list):
        return False
    for item in output:
        if isinstance(item, Mapping) and _responses_output_item_has_visible_or_tool_output(item):
            return True
    return False


def _responses_event_has_visible_or_tool_output(event: Mapping[str, Any], upstream_name: str) -> bool:
    event_type = event.get("type")
    if upstream_name != "official":
        if _is_reasoning_text_stream_event(event):
            return False
        if event_type in {"response.output_item.added", "response.output_item.done"}:
            item = event.get("item")
            if isinstance(item, Mapping) and item.get("type") == "reasoning":
                return False
    if _responses_event_commits_downstream_output(event, upstream_name):
        return True
    if _is_reasoning_text_stream_event(event):
        delta = event.get("delta")
        return upstream_name == "official" and isinstance(delta, str) and bool(delta)
    if event_type in {
        "response.function_call_arguments.delta",
        "response.custom_tool_call_input.delta",
    }:
        delta = event.get("delta")
        return isinstance(delta, str) and bool(delta)
    if event_type in {
        "response.function_call_arguments.done",
        "response.custom_tool_call_input.done",
    }:
        return True
    if event_type in {"response.output_item.added", "response.output_item.done"}:
        item = event.get("item")
        return isinstance(item, Mapping) and _responses_output_item_has_visible_or_tool_output(item)
    if event_type == "response.completed":
        return _responses_completed_event_has_visible_or_tool_output(event)
    return False


def _responses_event_is_tool_call_construction(event: Mapping[str, Any]) -> bool:
    event_type = event.get("type")
    if event_type in {
        "response.function_call_arguments.delta",
        "response.function_call_arguments.done",
        "response.custom_tool_call_input.delta",
        "response.custom_tool_call_input.done",
    }:
        return True
    if event_type in {"response.output_item.added", "response.output_item.done"}:
        item = event.get("item")
        return isinstance(item, Mapping) and item.get("type") in {"function_call", "custom_tool_call"}
    return False


def _responses_completed_tool_item(item: Mapping[str, Any]) -> dict[str, Any] | None:
    item_type = item.get("type")
    call_id = item.get("call_id")
    name = item.get("name")
    if not isinstance(call_id, str) or not call_id or not isinstance(name, str) or not name:
        return None
    if item_type == "function_call":
        arguments = item.get("arguments")
        if not isinstance(arguments, str):
            return None
        return dict(item)
    if item_type == "custom_tool_call":
        tool_input = item.get("input")
        if not isinstance(tool_input, str):
            return None
        return dict(item)
    return None


def _synthetic_response_completed_from_tool_items(
    *,
    created_response: Mapping[str, Any] | None,
    model: str,
    output_items: list[Mapping[str, Any]],
) -> dict[str, Any] | None:
    completed_items = [
        completed
        for item in output_items
        if isinstance(item, Mapping)
        for completed in [_responses_completed_tool_item(item)]
        if completed is not None
    ]
    if not completed_items:
        return None
    response = dict(created_response or {})
    response_id = response.get("id")
    if not isinstance(response_id, str) or not response_id:
        response_id = f"resp_{uuid.uuid4().hex[:12]}"
    response["id"] = response_id
    response.setdefault("object", "response")
    response["status"] = "completed"
    response["model"] = response.get("model") if isinstance(response.get("model"), str) else model
    response["output"] = completed_items
    return {"type": "response.completed", "response": response}


def _responses_sse_line_resets_idle_timeout(line: bytes) -> bool:
    event = _parse_sse_json_payload(line)
    if not isinstance(event, Mapping):
        return False
    event_type = event.get("type")
    return isinstance(event_type, str) and (event_type.startswith("response.") or event_type == "error")


def _stream_error_event_detail(payload: Mapping[str, Any]) -> str:
    error = payload.get("error")
    if isinstance(error, Mapping):
        message = error.get("message")
        code = error.get("code")
        if isinstance(message, str) and message:
            return f"{code}: {message}" if code is not None else message
        return json.dumps(error, ensure_ascii=True, separators=(",", ":"))[:300]
    if isinstance(error, str) and error:
        return error[:300]
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"))[:300]


def _responses_stream_error_detail(event: Mapping[str, Any]) -> str:
    response = event.get("response")
    if isinstance(response, Mapping):
        error = response.get("error")
        if isinstance(error, Mapping):
            message = error.get("message")
            code = error.get("code")
            if isinstance(message, str) and message:
                return f"{code}: {message}" if code is not None else message
            return json.dumps(error, ensure_ascii=True, separators=(",", ":"))[:300]
        if isinstance(error, str) and error:
            return error[:300]
    return _stream_error_event_detail(event)


def _responses_stream_error_type(event: Mapping[str, Any]) -> str | None:
    event_type = event.get("type")
    return event_type if event_type in {"error", "response.failed", "response.incomplete"} else None


def _chat_stream_error_detail(payload: Mapping[str, Any]) -> str | None:
    if "error" not in payload:
        return None
    return _stream_error_event_detail(payload)


def _chat_stream_chunk_has_finish(chunk: Mapping[str, Any]) -> bool:
    choices = chunk.get("choices")
    if not isinstance(choices, list):
        return False
    for choice in choices:
        if isinstance(choice, Mapping) and choice.get("finish_reason") is not None:
            return True
    return False


def _chat_stream_chunk_starts_downstream_output(chunk: Mapping[str, Any]) -> bool:
    choices = chunk.get("choices")
    if not isinstance(choices, list):
        return False
    for choice in choices:
        if not isinstance(choice, Mapping):
            continue
        delta = choice.get("delta")
        if not isinstance(delta, Mapping):
            continue
        content = delta.get("content")
        if isinstance(content, str) and content:
            return True
        if isinstance(delta.get("tool_calls"), list) and delta.get("tool_calls"):
            return True
        message = choice.get("message")
        if isinstance(message, Mapping):
            content = message.get("content")
            if isinstance(content, str) and content:
                return True
            if isinstance(message.get("tool_calls"), list) and message.get("tool_calls"):
                return True
    return False


def _chat_stream_chunks_have_terminal(chunks: list[Mapping[str, Any] | str]) -> bool:
    for chunk in chunks:
        if chunk == "[DONE]":
            return True
        if isinstance(chunk, Mapping) and _chat_stream_chunk_has_finish(chunk):
            return True
    return False


def _sse_json_line(payload: Mapping[str, Any], line_ending: bytes) -> bytes:
    return b"data: " + json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode("utf-8") + line_ending


def _chat_stream_status_chunk(
    status: Mapping[str, Any],
    model: str | None,
) -> dict[str, Any]:
    return {
        "id": f"chatcmpl_{uuid.uuid4().hex[:12]}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {"role": "assistant", "content": IMAGE_PROXY_PROGRESS_TEXT},
                "finish_reason": None,
            }
        ],
        "codexhub_status": dict(status),
    }


def _responses_stream_status_event(status: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "type": "response.output_text.delta",
        "output_index": 0,
        "content_index": 0,
        "delta": IMAGE_PROXY_PROGRESS_TEXT,
        "codexhub_status": dict(status),
    }


def _downstream_stream_status_payload(
    inbound_format: str,
    status: Mapping[str, Any],
    model: str | None,
) -> dict[str, Any]:
    if inbound_format == "chat_completions":
        return _chat_stream_status_chunk(status, model)
    return _responses_stream_status_event(status)


def _chat_content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    fragments = _collect_text_fragments(value)
    return "\n".join(fragments)


def _tail_text_for_compact_detection(payload: Mapping[str, Any], inbound_format: str) -> str:
    if inbound_format == "chat_completions":
        messages = payload.get("messages")
        if isinstance(messages, list):
            fragments: list[str] = []
            for message in messages[-5:]:
                if isinstance(message, Mapping):
                    fragments.append(_chat_content_text(message.get("content")))
            return "\n".join(fragment for fragment in fragments if fragment)

    input_items = payload.get("input")
    if isinstance(input_items, list):
        return "\n".join(_collect_text_fragments(input_items[-5:]))
    return "\n".join(_collect_text_fragments(input_items))


def _is_compact_summary_payload(payload: Mapping[str, Any], inbound_format: str) -> bool:
    text = _tail_text_for_compact_detection(payload, inbound_format).lower()
    if not text:
        return False

    summary_prompt = (
        "detailed summary of the conversation so far" in text
        or "create a detailed summary of the conversation" in text
        or "compact summary" in text
    )
    text_only_instruction = "do not call any tools" in text or "respond with text only" in text
    summary_shape = "<summary>" in text or "summary should include" in text
    return summary_prompt and text_only_instruction and summary_shape


def _request_kind_from_headers_and_payload(
    headers: Mapping[str, str] | Any,
    payload: Mapping[str, Any] | None,
    inbound_format: str,
) -> str:
    turn_metadata = _get_header(headers, "x-codex-turn-metadata")
    if isinstance(turn_metadata, str):
        try:
            parsed_turn_metadata = json.loads(turn_metadata)
        except json.JSONDecodeError:
            parsed_turn_metadata = None
        if (
            isinstance(parsed_turn_metadata, Mapping)
            and parsed_turn_metadata.get("request_kind") == "compaction"
        ):
            return RETRY_REQUEST_COMPACT
    for header_name in ("x-request-kind", "x-query-source"):
        header_value = _get_header(headers, header_name)
        if isinstance(header_value, str) and header_value.strip().lower() == RETRY_REQUEST_COMPACT:
            return RETRY_REQUEST_COMPACT
    if isinstance(payload, Mapping) and _is_compact_summary_payload(payload, inbound_format):
        return RETRY_REQUEST_COMPACT
    return RETRY_REQUEST_MAIN_GENERATION


def _strip_tools_for_text_only_proxy_payload(
    payload: dict[str, Any],
    *,
    event_context: Mapping[str, Any] | None = None,
    upstream_name: str | None = None,
    event_name: str = "text_only_proxy_tools_stripped",
) -> bool:
    removed_tools = payload.pop("tools", None)
    removed_tool_choice = payload.pop("tool_choice", None)
    if removed_tools is None and removed_tool_choice is None:
        return False

    removed_tool_count = len(removed_tools) if isinstance(removed_tools, list) else 0
    _write_adapter_event(
        event_context,
        event_name,
        upstream=upstream_name,
        model=payload.get("model") if isinstance(payload.get("model"), str) else None,
        removed_tool_count=removed_tool_count,
        removed_tool_choice=removed_tool_choice if isinstance(removed_tool_choice, str) else None,
    )
    return True


def _strip_tools_for_compact_payload(
    payload: dict[str, Any],
    *,
    event_context: Mapping[str, Any] | None = None,
    upstream_name: str | None = None,
) -> bool:
    return _strip_tools_for_text_only_proxy_payload(
        payload,
        event_context=event_context,
        upstream_name=upstream_name,
        event_name="compact_text_only_tools_stripped",
    )


def _chat_completion_body_is_empty(body: bytes) -> bool:
    try:
        payload = json.loads(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict) or "error" in payload:
        return False
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return True
    for choice in choices:
        if not isinstance(choice, Mapping):
            continue
        message = choice.get("message")
        if not isinstance(message, Mapping):
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return False
        if not isinstance(content, str) and _chat_content_text(content).strip():
            return False
        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list) and tool_calls:
            return False
    return True


def _responses_body_is_empty(body: bytes) -> bool:
    try:
        payload = json.loads(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return False
    if not isinstance(payload, dict) or "error" in payload:
        return False
    output = payload.get("output")
    if not isinstance(output, list) or not output:
        return True
    for item in output:
        if not isinstance(item, Mapping):
            continue
        if item.get("type") == "function_call":
            return False
        if item.get("type") != "message":
            continue
        if _chat_content_text(item.get("content")).strip():
            return False
    return True


def _compact_response_body_is_empty(body: bytes, inbound_format: str) -> bool:
    if inbound_format == "chat_completions":
        return _chat_completion_body_is_empty(body)
    return _responses_body_is_empty(body)


def _downstream_json_error_body(
    *,
    message: str,
    error_type: str,
    code: str,
    upstream_name: str,
) -> bytes:
    return json.dumps(
        {
            "error": {
                "message": message,
                "type": error_type,
                "code": code,
                "upstream": upstream_name,
            }
        },
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _incomplete_stream_json_error_body(upstream_name: str) -> bytes:
    return _downstream_json_error_body(
        message="Upstream stream ended before a terminal event.",
        error_type="upstream_stream_incomplete",
        code="upstream_stream_incomplete",
        upstream_name=upstream_name,
    )


_responses_content_to_chat_content = responses_content_to_chat_content
_responses_input_to_chat_messages = responses_input_to_chat_messages
_responses_tools_to_chat_tools = responses_tools_to_chat_tools
_responses_tool_choice_to_chat_tool_choice = responses_tool_choice_to_chat_tool_choice


def _responses_request_to_chat_completion_body(
    body: bytes,
    *,
    drop_client_metadata: bool = False,
    drop_client_transport_fields: bool = False,
    drop_reasoning: bool = False,
) -> bytes:
    return responses_request_to_chat_completion_body(
        body,
        drop_client_metadata=drop_client_metadata,
        drop_client_transport_fields=drop_client_transport_fields,
        drop_reasoning=drop_reasoning,
    )


XMLISH_TOOL_INVOKE_RE = re.compile(
    r"<invoke\s+name\s*=\s*['\"]([^'\"]+)['\"]\s*>(.*?)</invoke>",
    re.IGNORECASE | re.DOTALL,
)
XMLISH_TOOL_ARG_RE = re.compile(
    r"<([A-Za-z_][A-Za-z0-9_.-]*)\s*>(.*?)</\1>",
    re.IGNORECASE | re.DOTALL,
)
MODEL_STREAM_TAG_RE = re.compile(r"\]<\][A-Za-z0-9_.:-]+\[>")


def _strip_model_stream_tags(text: str) -> str:
    return MODEL_STREAM_TAG_RE.sub("", text)


def _xmlish_tool_call_outputs_from_text(text: str) -> list[dict[str, Any]]:
    cleaned = _strip_model_stream_tags(text)
    if "<invoke" not in cleaned.lower():
        return []
    output: list[dict[str, Any]] = []
    for match in XMLISH_TOOL_INVOKE_RE.finditer(cleaned):
        name = html.unescape(match.group(1)).strip()
        if not _valid_tool_name(name):
            continue
        arguments: dict[str, Any] = {}
        for arg_match in XMLISH_TOOL_ARG_RE.finditer(match.group(2)):
            key = arg_match.group(1).strip()
            if key.lower() in {"tool_call", "invoke"}:
                continue
            value = html.unescape(_strip_model_stream_tags(arg_match.group(2))).strip()
            arguments[key] = value
        call_id = f"call_{uuid.uuid4().hex[:12]}"
        output.append(
            {
                "id": f"fc_{call_id}",
                "type": "function_call",
                "status": "completed",
                "call_id": call_id,
                "name": name,
                "arguments": json.dumps(arguments, ensure_ascii=True, separators=(",", ":")),
            }
        )
    return output


def _repair_chat_completion_response_payload(payload: dict[str, Any]) -> dict[str, Any]:
    _hide_reasoning_text(payload)
    payload, _ = normalize_third_party_tool_call(payload)
    payload, _ = downgrade_invalid_third_party_tool_calls(payload)
    return payload


def _chat_completion_to_response_body(body: bytes, *, repair: bool = True) -> bytes:
    try:
        return chat_completion_to_response_body(
            body,
            repair=repair,
            chat_content_text=_chat_content_text,
            xmlish_tool_outputs=_xmlish_tool_call_outputs_from_text,
            repair_response=_repair_chat_completion_response_payload,
        )
    except UnsupportedProtocolTranslationError as exc:
        raise UpstreamProtocolTranslationError(exc) from exc


def _normalize_chat_function_call_name(name: str) -> str:
    if name == f"{NODE_REPL_NAMESPACE}.js":
        return f"{NODE_REPL_NAMESPACE}__js"
    if name == f"{NODE_REPL_NAMESPACE}__js":
        return name
    tool_name = THIRD_PARTY_TOOL_NAME_ALIASES.get(name)
    if tool_name in MULTI_AGENT_TOOL_NAMES:
        return f"multi_agent_v1__{tool_name}"
    return name


def _chat_stream_chunks_to_response_events(chunks: list[Mapping[str, Any] | str]) -> list[dict[str, Any]]:
    try:
        return chat_stream_chunks_to_response_events(
            chunks,
            normalize_function_name=_normalize_chat_function_call_name,
            xmlish_tool_outputs=_xmlish_tool_call_outputs_from_text,
        )
    except UnsupportedProtocolTranslationError as exc:
        raise UpstreamProtocolTranslationError(exc) from exc


def _response_events_shape_summary(events: list[Mapping[str, Any]]) -> dict[str, Any]:
    type_counts: dict[str, int] = {}
    tool_items: list[dict[str, Any]] = []
    output_items: list[dict[str, Any]] = []
    terminal_count = 0
    for event in events:
        if not isinstance(event, Mapping):
            continue
        event_type = event.get("type")
        if isinstance(event_type, str):
            type_counts[event_type] = type_counts.get(event_type, 0) + 1
            if event_type == "response.completed":
                terminal_count += 1
        item = event.get("item")
        if isinstance(item, Mapping):
            item_summary = {
                "event_type": event_type,
                "type": item.get("type"),
                "name": item.get("name"),
                "namespace": item.get("namespace"),
                "call_id": item.get("call_id"),
                "has_arguments": bool(item.get("arguments")),
            }
            output_items.append(item_summary)
            if item.get("type") == "function_call":
                tool_items.append(item_summary)
        response = event.get("response")
        if isinstance(response, Mapping):
            output = response.get("output")
            if isinstance(output, list):
                for output_item in output:
                    if not isinstance(output_item, Mapping):
                        continue
                    item_summary = {
                        "event_type": event_type,
                        "type": output_item.get("type"),
                        "name": output_item.get("name"),
                        "namespace": output_item.get("namespace"),
                        "call_id": output_item.get("call_id"),
                        "has_arguments": bool(output_item.get("arguments")),
                    }
                    output_items.append(item_summary)
                    if output_item.get("type") == "function_call":
                        tool_items.append(item_summary)
    return {
        "event_count": len(events),
        "event_type_counts": type_counts,
        "terminal_count": terminal_count,
        "output_items": output_items[:12],
        "output_item_count": len(output_items),
        "tool_items": tool_items[:12],
        "tool_item_count": len(tool_items),
    }


def _chat_stream_shape_summary(chunks: list[Mapping[str, Any] | str]) -> dict[str, Any]:
    text_parts: list[str] = []
    reasoning_chars = 0
    source_key_counts: dict[str, int] = {}
    finish_reason_counts: dict[str, int] = {}
    tool_call_names: list[str] = []
    summary: dict[str, Any] = {
        "chunk_count": len(chunks),
        "done_count": 0,
        "choice_count": 0,
        "delta_source_count": 0,
        "message_source_count": 0,
        "content_source_count": 0,
        "tool_call_count": 0,
        "tool_call_id_count": 0,
        "tool_call_name_count": 0,
        "tool_call_argument_chars": 0,
        "reasoning_source_count": 0,
        "reasoning_chars": 0,
        "text_chars": 0,
        "xmlish_tool_count": 0,
    }

    for chunk in chunks:
        if chunk == "[DONE]":
            summary["done_count"] += 1
            continue
        if not isinstance(chunk, Mapping):
            continue
        choices = chunk.get("choices")
        if not isinstance(choices, list):
            continue
        summary["choice_count"] += len(choices)
        for choice in choices:
            if not isinstance(choice, Mapping):
                continue
            finish_reason = choice.get("finish_reason")
            if finish_reason is not None:
                key = str(finish_reason)[:80]
                finish_reason_counts[key] = finish_reason_counts.get(key, 0) + 1
            for source_name in ("delta", "message"):
                source = choice.get(source_name)
                if not isinstance(source, Mapping):
                    continue
                summary[f"{source_name}_source_count"] += 1
                for key in source.keys():
                    key_text = str(key)[:80]
                    source_key_counts[key_text] = source_key_counts.get(key_text, 0) + 1
                content = source.get("content")
                text = content if isinstance(content, str) else _chat_content_text(content)
                if text:
                    summary["content_source_count"] += 1
                    text_parts.append(text)
                for key, value in source.items():
                    if "reason" not in str(key).lower():
                        continue
                    summary["reasoning_source_count"] += 1
                    if isinstance(value, str):
                        reasoning_chars += len(value)
                    elif value is not None:
                        reasoning_chars += len(str(value))
                tool_calls = source.get("tool_calls")
                if not isinstance(tool_calls, list):
                    continue
                summary["tool_call_count"] += len(tool_calls)
                for tool_call in tool_calls:
                    if not isinstance(tool_call, Mapping):
                        continue
                    if isinstance(tool_call.get("id"), str) and tool_call.get("id"):
                        summary["tool_call_id_count"] += 1
                    function = tool_call.get("function")
                    if not isinstance(function, Mapping):
                        continue
                    if isinstance(function.get("name"), str) and function.get("name"):
                        summary["tool_call_name_count"] += 1
                        if len(tool_call_names) < 12:
                            tool_call_names.append(function["name"])
                    arguments = function.get("arguments")
                    if isinstance(arguments, str):
                        summary["tool_call_argument_chars"] += len(arguments)

    text = "".join(text_parts)
    summary["text_chars"] = len(text)
    summary["reasoning_chars"] = reasoning_chars
    summary["xmlish_tool_count"] = len(_xmlish_tool_call_outputs_from_text(text)) if text else 0
    summary["finish_reasons"] = finish_reason_counts
    summary["source_keys"] = source_key_counts
    summary["tool_call_names"] = tool_call_names
    if text:
        summary["text_hmac"] = proxy_telemetry.telemetry_hmac(
            RUNTIME_CODEX_DIR,
            b"chat-stream-text",
            text.encode("utf-8", errors="ignore"),
        )
    return summary


CHAT_RAW_REASONING_FIELDS = frozenset({"reasoning", "reasoning_content"})


def _suppress_chat_reasoning_extensions(
    chunks: list[Mapping[str, Any] | str],
    *,
    event_context: Mapping[str, Any] | None,
    upstream_name: str | None,
) -> tuple[list[Mapping[str, Any] | str], bool]:
    """Drop third-party Chat reasoning extensions before Responses conversion."""
    rewritten_chunks: list[Mapping[str, Any] | str] = []
    field_count = 0
    chunk_count = 0

    for chunk in chunks:
        if not isinstance(chunk, Mapping):
            rewritten_chunks.append(chunk)
            continue
        choices = chunk.get("choices")
        if not isinstance(choices, list):
            rewritten_chunks.append(chunk)
            continue

        rewritten_choices: list[Any] = []
        chunk_changed = False
        for choice in choices:
            if not isinstance(choice, Mapping):
                rewritten_choices.append(choice)
                continue
            rewritten_choice: Mapping[str, Any] = choice
            for source_name in ("delta", "message"):
                source = choice.get(source_name)
                if not isinstance(source, Mapping):
                    continue
                fields = CHAT_RAW_REASONING_FIELDS.intersection(source)
                if not fields:
                    continue
                if rewritten_choice is choice:
                    rewritten_choice = dict(choice)
                rewritten_source = dict(source)
                for field in fields:
                    rewritten_source.pop(field, None)
                rewritten_choice[source_name] = rewritten_source
                field_count += len(fields)
                chunk_changed = True
            rewritten_choices.append(rewritten_choice)

        if chunk_changed:
            rewritten_chunk = dict(chunk)
            rewritten_chunk["choices"] = rewritten_choices
            rewritten_chunks.append(rewritten_chunk)
            chunk_count += 1
        else:
            rewritten_chunks.append(chunk)

    if not field_count:
        return chunks, False
    _write_adapter_event(
        event_context,
        "chat_reasoning_extensions_suppressed",
        upstream=upstream_name,
        field_count=field_count,
        chunk_count=chunk_count,
    )
    return rewritten_chunks, True


def _chat_stream_is_empty_lifecycle_final(
    summary: Mapping[str, Any],
    event_context: Mapping[str, Any] | None,
    request_kind: str,
) -> bool:
    if not lifecycle_empty_final_resample_enabled(event_context, request_kind):
        return False
    return int(summary.get("text_chars") or 0) == 0 and int(summary.get("tool_call_count") or 0) == 0


FINAL_REPORT_LINE_PREFIXES = (
    ("RESULT:", "SENTINEL:", "SUBAGENT_CHAIN:"),
    ("SPAWNED:", "AGENT_ID:", "SENTINEL_SEEN:", "CLOSED:"),
    (
        "SPAWN_COUNT:",
        "AGENT_IDS:",
        "SENTINEL_A_SEEN:",
        "SENTINEL_B_SEEN:",
        "CLOSED_COUNT:",
        "EXTRA_SPAWN:",
    ),
)


def _final_report_nonempty_lines(text: str) -> list[str]:
    return [line.strip() for line in text.strip().splitlines() if line.strip()]


def _lines_match_final_report_prefixes(lines: list[str], start: int, prefixes: tuple[str, ...]) -> bool:
    if start + len(prefixes) > len(lines):
        return False
    for offset, prefix in enumerate(prefixes):
        if not lines[start + offset].upper().startswith(prefix):
            return False
    return True


def _lifecycle_final_format_violation(text: str) -> bool:
    lines = _final_report_nonempty_lines(text)
    if not lines:
        return False
    for prefixes in FINAL_REPORT_LINE_PREFIXES:
        for start in range(len(lines)):
            if not _lines_match_final_report_prefixes(lines, start, prefixes):
                continue
            return start != 0 or len(lines) != len(prefixes)
    return False


def _text_contains_lifecycle_final_report(text: str) -> bool:
    lines = _final_report_nonempty_lines(text)
    if not lines:
        return False
    for prefixes in FINAL_REPORT_LINE_PREFIXES:
        for start in range(len(lines)):
            if _lines_match_final_report_prefixes(lines, start, prefixes):
                return True
    return False


def _chat_stream_visible_text(chunks: list[Mapping[str, Any] | str]) -> str:
    text_parts: list[str] = []
    for chunk in chunks:
        if not isinstance(chunk, Mapping):
            continue
        choices = chunk.get("choices")
        if not isinstance(choices, list):
            continue
        for choice in choices:
            if not isinstance(choice, Mapping):
                continue
            for source_name in ("delta", "message"):
                source = choice.get(source_name)
                if not isinstance(source, Mapping):
                    continue
                content = source.get("content")
                text = content if isinstance(content, str) else _chat_content_text(content)
                if text:
                    text_parts.append(text)
    return "".join(text_parts).strip()


def _response_payload_visible_text(payload: Any) -> str:
    text_parts: list[str] = []
    if not isinstance(payload, Mapping):
        return ""
    output = payload.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, Mapping) or item.get("type") != "message":
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if isinstance(part, Mapping) and part.get("type") in {"output_text", "text"}:
                    text = part.get("text")
                    if isinstance(text, str) and text:
                        text_parts.append(text)
    choices = payload.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, Mapping):
                continue
            message = choice.get("message")
            if not isinstance(message, Mapping):
                continue
            content = message.get("content")
            if isinstance(content, str) and content:
                text_parts.append(content)
            elif isinstance(content, list):
                text_parts.append(_chat_content_text(content))
    return "\n".join(part.strip() for part in text_parts if part.strip()).strip()


def _response_payload_tool_call_count(payload: Any) -> int:
    if not isinstance(payload, Mapping):
        return 0
    count = 0
    output = payload.get("output")
    if isinstance(output, list):
        count += sum(1 for item in output if isinstance(item, Mapping) and item.get("type") == "function_call")
    choices = payload.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, Mapping):
                continue
            message = choice.get("message")
            if not isinstance(message, Mapping):
                continue
            tool_calls = message.get("tool_calls")
            if isinstance(tool_calls, list):
                count += len(tool_calls)
    return count


def _response_body_lifecycle_final_issue(
    body: bytes,
    event_context: Mapping[str, Any] | None,
    request_kind: str,
) -> str | None:
    if not lifecycle_empty_final_resample_enabled(event_context, request_kind):
        return None
    try:
        payload = json.loads(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if _response_payload_tool_call_count(payload) > 0:
        return None
    text = _response_payload_visible_text(payload)
    if not text:
        return "empty"
    if _lifecycle_final_format_violation(text):
        return "format"
    return None


def _responses_events_lifecycle_final_issue(
    events: list[Mapping[str, Any]],
    event_context: Mapping[str, Any] | None,
    request_kind: str,
) -> str | None:
    if not lifecycle_empty_final_resample_enabled(event_context, request_kind):
        return None
    return _response_body_lifecycle_final_issue(_events_to_responses_body(events), event_context, request_kind)


def _chat_stream_lifecycle_final_issue(
    chunks: list[Mapping[str, Any] | str],
    summary: Mapping[str, Any],
    event_context: Mapping[str, Any] | None,
    request_kind: str,
) -> str | None:
    if not lifecycle_empty_final_resample_enabled(event_context, request_kind):
        return None
    if int(summary.get("tool_call_count") or 0) > 0:
        return None
    if int(summary.get("text_chars") or 0) == 0:
        return "empty"
    if _lifecycle_final_format_violation(_chat_stream_visible_text(chunks)):
        return "format"
    return None


def _raise_lifecycle_final_issue(upstream_name: str, issue: str) -> None:
    if issue == "empty":
        raise LifecycleEmptyFinalResponseError(upstream_name)
    if issue == "format":
        raise LifecycleFinalFormatResponseError(upstream_name)


def _lifecycle_final_issue_event_name(issue: str) -> str:
    if issue == "empty":
        return "lifecycle_empty_final_resample"
    return "lifecycle_final_format_resample"


def _lifecycle_final_issue_missing_reason(issue: str) -> str:
    if issue == "empty":
        return "lifecycle_empty_final_response"
    return "lifecycle_final_format_response"


_chat_content_to_responses_content = chat_content_to_responses_content
_chat_messages_to_responses_input = partial(
    chat_messages_to_responses_input,
    chat_content_text=_chat_content_text,
)


def _normalize_responses_string_input(payload: dict[str, Any]) -> bool:
    value = payload.get("input")
    if not isinstance(value, str):
        return False
    payload["input"] = [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": value}],
        }
    ]
    return True


def _normalize_responses_message_input_items(payload: dict[str, Any]) -> bool:
    input_items = payload.get("input")
    if not isinstance(input_items, list):
        return False

    changed = False
    normalized_items: list[Any] = []
    for item in input_items:
        if (
            isinstance(item, dict)
            and item.get("type") is None
            and isinstance(item.get("role"), str)
            and "content" in item
        ):
            rewritten = dict(item)
            rewritten["type"] = "message"
            normalized_items.append(rewritten)
            changed = True
        else:
            normalized_items.append(item)

    if changed:
        payload["input"] = normalized_items
    return changed


_chat_tools_to_responses_tools = chat_tools_to_responses_tools
_chat_tool_choice_to_responses_tool_choice = chat_tool_choice_to_responses_tool_choice


def _chat_completions_request_to_responses_body(body: bytes) -> bytes:
    return chat_completions_request_to_responses_body(
        body,
        chat_content_text=_chat_content_text,
    )


def _chat_function_name_from_response_item(item: Mapping[str, Any]) -> str | None:
    name = item.get("name")
    if not isinstance(name, str) or not name:
        return None
    namespace = item.get("namespace")
    if namespace == "multi_agent_v1":
        return f"multi_agent_v1__{name}"
    if namespace == NODE_REPL_NAMESPACE:
        return f"{NODE_REPL_NAMESPACE}__{name}"
    flat_codex_apps_alias = _codex_apps_namespace_flat_alias(namespace, name)
    if flat_codex_apps_alias is not None:
        return flat_codex_apps_alias
    if isinstance(namespace, str) and _supports_explicit_namespace_alias(namespace) and _valid_tool_name(name):
        alias = f"{namespace}__{name}"
        if _valid_tool_name(alias):
            return alias
    return name


def _response_body_to_chat_completion_body(body: bytes) -> bytes:
    try:
        return response_body_to_chat_completion_body(
            body,
            function_name_from_response_item=_chat_function_name_from_response_item,
            error_body=_chat_completion_error_body,
        )
    except UnsupportedProtocolTranslationError as exc:
        raise UpstreamProtocolTranslationError(exc) from exc


def _chat_completion_body_to_stream_chunks(body: bytes) -> list[dict[str, Any]]:
    try:
        return chat_completion_body_to_stream_chunks(body)
    except UnsupportedProtocolTranslationError as exc:
        raise UpstreamProtocolTranslationError(exc) from exc


def _chat_completion_error_body(payload: Mapping[str, Any]) -> bytes:
    return chat_completion_error_body(payload)


class UpstreamStreamInterruptedError(RuntimeError):
    """Raised when an upstream stream is interrupted before downstream output starts."""

    def __init__(self, cause: BaseException):
        self.cause = cause
        super().__init__(str(cause))

class UpstreamEmptyCompletedResponseError(UpstreamStreamIncompleteError):
    """Raised when a third-party Responses stream completes with no visible output."""


class UpstreamStreamErrorEvent(RuntimeError):
    """Raised when an upstream Responses SSE stream emits an error event."""

    def __init__(self, payload: Mapping[str, Any]):
        self.payload = dict(payload)
        super().__init__(_stream_error_event_detail(payload))


class DownstreamClosedError(RuntimeError):
    """Raised when a downstream closure aborts Gateway output for a request."""


class DownstreamClosedBeforeRetryError(DownstreamClosedError):
    """Raised when a downstream closure aborts an upstream retry attempt."""


class DownstreamClosedDuringImageProxyError(DownstreamClosedError):
    """Raised when a downstream closure aborts image-proxy preprocessing."""


class DownstreamKeepaliveFailedError(DownstreamClosedError):
    """Raised when a downstream keepalive write fails, aborting upstream iteration."""


bind_transport_failure_types(
    stream_interrupted_error=UpstreamStreamInterruptedError,
    stream_error_event=UpstreamStreamErrorEvent,
)


RESPONSES_TERMINAL_EVENT_TYPES = {
    "response.completed",
    "response.failed",
    "response.incomplete",
    "error",
}


def _responses_events_have_terminal(events: list[Mapping[str, Any]]) -> bool:
    for event in events:
        if not isinstance(event, Mapping):
            continue
        event_type = event.get("type")
        if isinstance(event_type, str) and event_type in RESPONSES_TERMINAL_EVENT_TYPES:
            return True
    return False


def _chat_stream_chunk_has_finish(chunk: Mapping[str, Any]) -> bool:
    choices = chunk.get("choices")
    if not isinstance(choices, list):
        return False
    for choice in choices:
        if isinstance(choice, Mapping) and choice.get("finish_reason") is not None:
            return True
    return False


def _chat_stream_chunks_have_terminal(chunks: list[Mapping[str, Any] | str]) -> bool:
    for chunk in chunks:
        if chunk == "[DONE]":
            return True
        if isinstance(chunk, Mapping) and _chat_stream_chunk_has_finish(chunk):
            return True
    return False


def _response_events_to_chat_stream_chunks(
    events: list[Mapping[str, Any]],
    *,
    require_completed: bool = False,
) -> list[dict[str, Any]]:
    try:
        return response_events_to_chat_stream_chunks(
            events,
            require_completed=require_completed,
            function_name_from_response_item=_chat_function_name_from_response_item,
        )
    except UnsupportedProtocolTranslationError as exc:
        raise UpstreamProtocolTranslationError(exc) from exc


def _is_reasoning_sse_payload(payload: Mapping[str, Any] | None) -> bool:
    if payload is None:
        return False
    event_type = payload.get("type")
    if isinstance(event_type, str) and "reasoning" in event_type:
        return True
    item = payload.get("item")
    return isinstance(item, dict) and item.get("type") == "reasoning"


def _events_to_responses_body(
    events: list[Mapping[str, Any]],
    *,
    require_completed: bool = False,
) -> bytes:
    return events_to_responses_body(
        events,
        require_completed=require_completed,
        usage_from_response=_usage_from_payload,
    )


def _response_body_to_response_sse_events(body: bytes) -> list[dict[str, Any]]:
    return response_body_to_response_sse_events(
        body,
        collect_text_fragments=_collect_text_fragments,
    )


def _count_sse_reasoning_event(
    stats: dict[str, Any],
    original_payload: Mapping[str, Any] | None,
    rewritten_payload: Mapping[str, Any] | None,
) -> None:
    if not _is_reasoning_sse_payload(original_payload) and not _is_reasoning_sse_payload(rewritten_payload):
        return

    stats["seen"] = True
    original_type = original_payload.get("type") if original_payload is not None else None
    rewritten_type = rewritten_payload.get("type") if rewritten_payload is not None else None
    if isinstance(original_type, str):
        counts = stats["original_event_counts"]
        counts[original_type] = counts.get(original_type, 0) + 1
    if isinstance(rewritten_type, str):
        counts = stats["rewritten_event_counts"]
        counts[rewritten_type] = counts.get(rewritten_type, 0) + 1

    delta_payload = rewritten_payload if rewritten_payload is not None else original_payload
    delta = delta_payload.get("delta") if delta_payload is not None else None
    if isinstance(delta, str):
        stats["delta_events"] += 1
        stats["delta_chars"] += len(delta)




chat_stream_chunks_have_terminal = _chat_stream_chunks_have_terminal
responses_events_have_terminal = _responses_events_have_terminal
request_kind_from_headers_and_payload = _request_kind_from_headers_and_payload
is_compact_summary_payload = _is_compact_summary_payload
strip_tools_for_compact_payload = _strip_tools_for_compact_payload
