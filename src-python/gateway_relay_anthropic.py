"""Inbound Anthropic Messages SSE relay."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

import anthropic_messages_ir
import gateway_events
import protocol_translation
from protocol_translation import UpstreamStreamIncompleteError
from sse_events import SseEvent, SseFrameTooLargeError

def _anthropic_sse_terminal_observer(event_name: str | None, _data: bytes, payload: Any) -> bool:
    return event_name in {"message_stop", "error"} or (
        isinstance(payload, Mapping) and payload.get("type") in {"message_stop", "error"}
    )


def _anthropic_sse_terminal_kind(frame: SseEvent) -> str | None:
    event_name = frame.event.decode("utf-8") if frame.event else ""
    payload: Any = None
    if frame.data:
        try:
            payload = json.loads(frame.data)
        except json.JSONDecodeError:
            payload = None
    payload_type = payload.get("type") if isinstance(payload, Mapping) else None
    if event_name == "error" or payload_type == "error":
        return "error"
    if event_name == "message_stop" or payload_type == "message_stop":
        return "success"
    return None


def json_message_from_anthropic_sse(frames: list[SseEvent]) -> bytes | None:
    """Assemble one Anthropic JSON message from buffered native SSE frames."""

    message: dict[str, Any] | None = None
    blocks: dict[int, dict[str, Any]] = {}
    usage: dict[str, Any] = {}
    saw_stop = False
    for frame in frames:
        event_name = frame.event.decode("utf-8") if frame.event else ""
        if not frame.data:
            continue
        try:
            payload = json.loads(frame.data)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, Mapping):
            continue
        if event_name == "message_start":
            raw_message = payload.get("message")
            if isinstance(raw_message, dict):
                message = dict(raw_message)
                gateway_events.merge_anthropic_usage_snapshot(
                    usage, raw_message.get("usage"), include_output=False
                )
            continue
        if event_name == "content_block_start":
            index = payload.get("index") if isinstance(payload.get("index"), int) else 0
            block = payload.get("content_block")
            blocks[index] = dict(block) if isinstance(block, dict) else {"type": "text", "text": ""}
            continue
        if event_name == "content_block_delta":
            index = payload.get("index") if isinstance(payload.get("index"), int) else 0
            delta = payload.get("delta") if isinstance(payload.get("delta"), Mapping) else {}
            block = blocks.setdefault(index, {"type": "text", "text": ""})
            if delta.get("type") == "text_delta" and isinstance(delta.get("text"), str):
                block["text"] = str(block.get("text") or "") + delta["text"]
            elif delta.get("type") == "thinking_delta" and isinstance(delta.get("thinking"), str):
                block["type"] = "thinking"
                block["thinking"] = str(block.get("thinking") or "") + delta["thinking"]
            continue
        if event_name == "message_delta":
            if message is None:
                continue
            delta = payload.get("delta")
            if isinstance(delta, Mapping):
                for key, value in delta.items():
                    if value is not None:
                        message[key] = value
            gateway_events.merge_anthropic_usage_snapshot(usage, payload.get("usage"))
            continue
        if event_name == "message_stop":
            saw_stop = True
    if not saw_stop or message is None:
        return None
    content = []
    for index in sorted(blocks):
        block = blocks[index]
        if block.get("type") == "thinking" and not block.get("thinking"):
            continue
        content.append(block)
    message["content"] = content
    if usage:
        message["usage"] = usage
    message.setdefault("type", "message")
    message.setdefault("role", "assistant")
    return json.dumps(message, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


def _commit_conversion_error(seam: Any, finish_closed: Callable[[OSError], int], handler: Any) -> int:
    adapted = anthropic_messages_ir.adapt_upstream_response(
        "chat_completions",
        b'{"error":{"type":"invalid_request_error"}}',
        status=400,
    )
    body = adapted.body if isinstance(adapted, anthropic_messages_ir.AdaptedResponse) else b""
    event = b"event: error\ndata: " + body + b"\n\n" if body else b""
    if event and not seam.commit_sse_bytes(event):
        return finish_closed(seam.last_write_error() or OSError("downstream closed"))
    handler.close_connection = True
    return 400


def relay_inbound_anthropic_sse(
    *,
    handler: Any,
    response: Any,
    read_lines: Callable[..., Any],
    iter_events: Callable[..., Any],
    seam: Any,
    send_headers: Callable[[], bool],
    finish_closed: Callable[[OSError], int],
    write_proxy_event: Callable[..., None],
    observe_line: Callable[[bytes], None],
    upstream_format: str,
    inbound_format: str,
    status: int,
    usage_capture: dict[str, Any] | None = None,
) -> int:
    usage: dict[str, Any] = {}

    def capture_usage(missing_reason: str) -> None:
        gateway_events.capture_usage(
            usage_capture,
            usage or None,
            missing_reason=missing_reason,
            upstream_format="anthropic_messages",
        )

    if not send_headers():
        capture_usage("downstream_cancelled_before_response")
        return finish_closed(
            seam.last_write_error() or OSError("downstream closed")
        )
    if upstream_format == "anthropic_messages":
        terminal_kind: str | None = None
        try:
            for frame in iter_events(
                response,
                read_lines=read_lines,
                event_resets_idle_timeout=lambda _event: True,
                on_chunk=observe_line,
            ):
                event_name = frame.event.decode("utf-8") if frame.event else ""
                if frame.data:
                    try:
                        payload = json.loads(frame.data)
                    except json.JSONDecodeError:
                        payload = None
                    if isinstance(payload, Mapping):
                        if event_name == "message_start":
                            message = payload.get("message")
                            if isinstance(message, Mapping):
                                gateway_events.merge_anthropic_usage_snapshot(
                                    usage, message.get("usage"), include_output=False
                                )
                        elif event_name == "message_delta":
                            gateway_events.merge_anthropic_usage_snapshot(
                                usage, payload.get("usage")
                            )
                if frame.raw and not seam.commit_sse_bytes(frame.raw):
                    if seam.terminal_committed:
                        break
                    capture_usage("downstream_cancelled")
                    return finish_closed(
                        seam.last_write_error() or OSError("downstream closed")
                    )
                terminal_kind = _anthropic_sse_terminal_kind(frame)
                if terminal_kind is not None:
                    break
        except (SseFrameTooLargeError, UpstreamStreamIncompleteError):
            capture_usage("stream_incomplete")
            seam.cancel()
            return 502
        if terminal_kind is None or not seam.terminal_committed:
            capture_usage("stream_incomplete")
            seam.cancel()
            return 502
        handler.close_connection = True
        if terminal_kind == "error":
            capture_usage("upstream_stream_error")
            return status if status >= 400 else 502
        capture_usage("upstream_missing_usage")
        return status
    emitter = anthropic_messages_ir.ChatToAnthropicEmitter()
    responses_converter = (
        protocol_translation.ResponsesToChatStreamConverter(preserve_reasoning_history=True)
        if upstream_format == "responses"
        else None
    )
    try:
        for frame in iter_events(
            response,
            read_lines=read_lines,
            event_resets_idle_timeout=lambda _event: True,
            on_chunk=observe_line,
        ):
            data = frame.data.strip() if frame.data else b""
            if data == b"[DONE]":
                payload: Any = "[DONE]"
            elif not data:
                continue
            else:
                try:
                    payload = protocol_translation.decode_protocol_json(data)
                except (UnicodeError, json.JSONDecodeError, protocol_translation.UnsupportedProtocolTranslationError):
                    continue
            frames: list[bytes] | anthropic_messages_ir.NotForwardable
            if responses_converter is not None:
                if not isinstance(payload, Mapping):
                    continue
                if payload.get("type") in {"error", "response.failed", "response.incomplete"}:
                    error_bytes = anthropic_messages_ir.adapt_upstream_response(
                        "responses",
                        json.dumps(payload).encode(),
                        status=status if status >= 400 else 502,
                        content_type="application/json",
                    )
                    body_bytes = error_bytes.body if isinstance(error_bytes, anthropic_messages_ir.AdaptedResponse) else b""
                    if body_bytes and not seam.commit_sse_bytes(body_bytes):
                        return finish_closed(
                            seam.last_write_error() or OSError("downstream closed")
                        )
                    handler.close_connection = True
                    return status if status >= 400 else 502
                chat_chunks = responses_converter.chunks_for_event(payload)
                if not isinstance(responses_converter.response_id, str) or not responses_converter.response_id:
                    continue
                for chunk in chat_chunks:
                    frames = emitter.feed(chunk)
                    if isinstance(frames, anthropic_messages_ir.NotForwardable):
                        return _commit_conversion_error(seam, finish_closed, handler)
                    for item in frames:
                        if not seam.commit_sse_bytes(item):
                            return finish_closed(
                                seam.last_write_error() or OSError("downstream closed")
                            )
                if payload.get("type") == "response.completed":
                    frames = emitter.finish(require_done=False)
                    if isinstance(frames, anthropic_messages_ir.NotForwardable):
                        return _commit_conversion_error(seam, finish_closed, handler)
                    for item in frames:
                        if not seam.commit_sse_bytes(item):
                            return finish_closed(
                                seam.last_write_error() or OSError("downstream closed")
                            )
                    for item in emitter.declared:
                        write_proxy_event(
                            "protocol_adaptation",
                            field=item.field,
                            policy=item.policy,
                            detail=item.detail,
                            inbound_format=inbound_format,
                            upstream_format=upstream_format,
                        )
                    handler.close_connection = True
                    return status
            else:
                frames = emitter.feed(payload)
                if isinstance(frames, anthropic_messages_ir.NotForwardable):
                    return _commit_conversion_error(seam, finish_closed, handler)
                for item in frames:
                    if not seam.commit_sse_bytes(item):
                        return finish_closed(
                            seam.last_write_error() or OSError("downstream closed")
                        )
                if payload == "[DONE]":
                    frames = emitter.finish(require_done=True)
                    if isinstance(frames, anthropic_messages_ir.NotForwardable):
                        return _commit_conversion_error(seam, finish_closed, handler)
                    for item in frames:
                        if not seam.commit_sse_bytes(item):
                            return finish_closed(
                                seam.last_write_error() or OSError("downstream closed")
                            )
                    for item in emitter.declared:
                        write_proxy_event(
                            "protocol_adaptation",
                            field=item.field,
                            policy=item.policy,
                            detail=item.detail,
                            inbound_format=inbound_format,
                            upstream_format=upstream_format,
                        )
                    handler.close_connection = True
                    return status
    except (SseFrameTooLargeError, UpstreamStreamIncompleteError):
        seam.cancel()
        return 502
    seam.cancel()
    return 502
