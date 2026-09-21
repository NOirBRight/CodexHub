"""Inbound Anthropic Messages SSE relay."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from typing import Any

import anthropic_messages_prototype
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


def _commit_conversion_error(seam: Any, finish_closed: Callable[[OSError], int], handler: Any) -> int:
    adapted = anthropic_messages_prototype.adapt_upstream_response(
        "chat_completions",
        b'{"error":{"type":"invalid_request_error"}}',
        status=400,
    )
    body = adapted.body if isinstance(adapted, anthropic_messages_prototype.AdaptedResponse) else b""
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
) -> int:
    if not send_headers():
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
                if frame.raw and not seam.commit_sse_bytes(frame.raw):
                    if seam.terminal_committed:
                        break
                    return finish_closed(
                        seam.last_write_error() or OSError("downstream closed")
                    )
                terminal_kind = _anthropic_sse_terminal_kind(frame)
                if terminal_kind is not None:
                    break
        except (SseFrameTooLargeError, UpstreamStreamIncompleteError):
            seam.cancel()
            return 502
        if terminal_kind is None or not seam.terminal_committed:
            seam.cancel()
            return 502
        handler.close_connection = True
        if terminal_kind == "error":
            return status if status >= 400 else 502
        return status
    emitter = anthropic_messages_prototype.ChatToAnthropicEmitter()
    responses_converter = (
        protocol_translation.ResponsesToChatStreamConverter()
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
            frames: list[bytes] | anthropic_messages_prototype.NotForwardable
            if responses_converter is not None:
                if not isinstance(payload, Mapping):
                    continue
                if payload.get("type") in {"error", "response.failed", "response.incomplete"}:
                    error_bytes = anthropic_messages_prototype.adapt_upstream_response(
                        "responses",
                        json.dumps(payload).encode(),
                        status=status if status >= 400 else 502,
                        content_type="application/json",
                    )
                    body_bytes = error_bytes.body if isinstance(error_bytes, anthropic_messages_prototype.AdaptedResponse) else b""
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
                    if isinstance(frames, anthropic_messages_prototype.NotForwardable):
                        return _commit_conversion_error(seam, finish_closed, handler)
                    for item in frames:
                        if not seam.commit_sse_bytes(item):
                            return finish_closed(
                                seam.last_write_error() or OSError("downstream closed")
                            )
                if payload.get("type") == "response.completed":
                    frames = emitter.finish(require_done=False)
                    if isinstance(frames, anthropic_messages_prototype.NotForwardable):
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
                if isinstance(frames, anthropic_messages_prototype.NotForwardable):
                    return _commit_conversion_error(seam, finish_closed, handler)
                for item in frames:
                    if not seam.commit_sse_bytes(item):
                        return finish_closed(
                            seam.last_write_error() or OSError("downstream closed")
                        )
                if payload == "[DONE]":
                    frames = emitter.finish(require_done=True)
                    if isinstance(frames, anthropic_messages_prototype.NotForwardable):
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
