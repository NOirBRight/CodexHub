"""Adapt structured tool-result media at the protocol conversion boundary.

Tool history may contain images or files inside function/tool outputs. Generic
JSON stringification of those fields turns binary payloads into prompt text and
can explode token counts during compaction. This module is the shared adapter
for third-party Responses, Chat, and remaining text-compatibility paths.

Official passthrough does not use this adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

import json

from protocol_translation import UnsupportedProtocolTranslationError

VISUAL_CONTENT_OMITTED_NOTICE = (
    "Visual content from tool results was omitted from this compact summary."
)

_TEXT_PART_TYPES = {"input_text", "output_text", "text"}
_IMAGE_PART_TYPES = {"input_image", "image_url", "image"}
_FILE_PART_TYPES = {"input_file", "file"}
_AUDIO_PART_TYPES = {"input_audio", "audio"}
_KNOWN_MEDIA_TYPES = _IMAGE_PART_TYPES | _FILE_PART_TYPES | _AUDIO_PART_TYPES
_EVENT_STATS_KEY = "_tool_result_media_stats"

_SOURCE_NOTE_PREFIX = "Tool result image"
_OMITTED_NOTE_PREFIX = "[omitted tool result"


@dataclass(frozen=True)
class ToolResultMediaPolicy:
    """Capability and request-kind facts for one adaptation pass."""

    request_kind: str = "main_generation"
    placeholder_authorized: bool = False
    message_images_supported: bool | None = None
    tool_result_images_supported: bool = False
    target_format: str = "responses"

    @property
    def can_carry_message_images(self) -> bool:
        return self.message_images_supported is True

    @property
    def can_carry_tool_result_images(self) -> bool:
        return self.tool_result_images_supported is True

    @property
    def is_compact(self) -> bool:
        return self.request_kind == "compact"


@dataclass
class ToolResultMediaStats:
    structured_image_count: int = 0
    structured_file_count: int = 0
    omitted_image_count: int = 0
    omitted_file_count: int = 0
    lifted_image_count: int = 0
    input_bytes: int = 0
    output_bytes: int = 0
    placeholder_count: int = 0

    def add(self, other: "ToolResultMediaStats") -> None:
        self.structured_image_count += other.structured_image_count
        self.structured_file_count += other.structured_file_count
        self.omitted_image_count += other.omitted_image_count
        self.omitted_file_count += other.omitted_file_count
        self.lifted_image_count += other.lifted_image_count
        self.input_bytes += other.input_bytes
        self.output_bytes += other.output_bytes
        self.placeholder_count += other.placeholder_count

    def as_event_fields(self) -> dict[str, int]:
        return {
            "structured_image_count": self.structured_image_count,
            "structured_file_count": self.structured_file_count,
            "omitted_image_count": self.omitted_image_count,
            "omitted_file_count": self.omitted_file_count,
            "lifted_image_count": self.lifted_image_count,
            "placeholder_count": self.placeholder_count,
            "input_bytes": self.input_bytes,
            "output_bytes": self.output_bytes,
        }


def _translation_error(detail: str) -> None:
    raise UnsupportedProtocolTranslationError("unsupported_protocol_semantics", detail)


def _measure_bytes(value: Any) -> int:
    if value is None:
        return 0
    if isinstance(value, str):
        return len(value.encode("utf-8"))
    try:
        return len(json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError):
        return len(str(value).encode("utf-8"))


def _part_type(part: Mapping[str, Any]) -> str | None:
    value = part.get("type")
    return value if isinstance(value, str) and value else None


def _is_image_part(part: Mapping[str, Any]) -> bool:
    part_type = _part_type(part)
    if part_type in _IMAGE_PART_TYPES:
        return True
    if part_type is None and isinstance(part.get("image_url"), (str, Mapping)):
        return True
    return False


def _is_file_part(part: Mapping[str, Any]) -> bool:
    part_type = _part_type(part)
    if part_type in _FILE_PART_TYPES:
        return True
    return False


def _is_audio_part(part: Mapping[str, Any]) -> bool:
    return _part_type(part) in _AUDIO_PART_TYPES


def _is_text_part(part: Mapping[str, Any]) -> bool:
    part_type = _part_type(part)
    return part_type in _TEXT_PART_TYPES and isinstance(part.get("text"), str)


def _is_media_part(part: Mapping[str, Any]) -> bool:
    return _is_image_part(part) or _is_file_part(part) or _is_audio_part(part)


def _is_unknown_typed_part(part: Mapping[str, Any]) -> bool:
    part_type = _part_type(part)
    return (
        part_type is not None
        and part_type not in _TEXT_PART_TYPES
        and part_type not in _KNOWN_MEDIA_TYPES
    )


def output_contains_structured_media(value: Any) -> bool:
    """True when a tool output field carries identified structured media."""

    if isinstance(value, Mapping):
        return _is_media_part(value) or _is_unknown_typed_part(value)
    if isinstance(value, list):
        return any(
            isinstance(item, Mapping)
            and (_is_media_part(item) or _is_unknown_typed_part(item))
            for item in value
        )
    return False


def _split_output_parts(output: Any) -> tuple[list[str], list[dict[str, Any]]]:
    if output is None:
        return [], []
    if isinstance(output, str):
        return ([output] if output else []), []
    if isinstance(output, Mapping):
        parts = [output]
    elif isinstance(output, list):
        parts = []
        for item in output:
            if not isinstance(item, Mapping):
                _translation_error(
                    "Cannot adapt a non-object tool-result output part."
                )
            parts.append(item)
    else:
        _translation_error(
            "Cannot adapt a non-string, non-list, non-object tool-result output."
        )

    texts: list[str] = []
    media: list[dict[str, Any]] = []
    for part in parts:
        if _is_text_part(part):
            extra = set(part) - {"type", "text"}
            if extra:
                _translation_error(
                    "Cannot adapt a tool-result text part with unsupported fields."
                )
            texts.append(part["text"])
            continue
        if _is_media_part(part):
            media.append(dict(part))
            continue
        part_type = _part_type(part)
        if part_type is None:
            _translation_error(
                "Cannot adapt an ambiguous tool-result output object without a type."
            )
        _translation_error(
            f"Cannot adapt tool-result output part type {part_type!r}."
        )
    return texts, media


def _media_kind(part: Mapping[str, Any]) -> str:
    if _is_image_part(part):
        return "image"
    if _is_file_part(part):
        return "file"
    if _is_audio_part(part):
        return "audio"
    return "unknown"


def _image_url_from_part(part: Mapping[str, Any]) -> str | None:
    image_url = part.get("image_url")
    if isinstance(image_url, str) and image_url:
        return image_url
    if isinstance(image_url, Mapping):
        url = image_url.get("url")
        if isinstance(url, str) and url:
            return url
    return None


def _file_id_from_part(part: Mapping[str, Any]) -> str | None:
    file_id = part.get("file_id")
    if isinstance(file_id, str) and file_id:
        return file_id
    return None


def _detail_from_part(part: Mapping[str, Any]) -> str | None:
    detail = part.get("detail")
    if isinstance(detail, str) and detail:
        return detail
    image_url = part.get("image_url")
    if isinstance(image_url, Mapping):
        nested = image_url.get("detail")
        if isinstance(nested, str) and nested:
            return nested
    return None


def _identified_payloads(part: Mapping[str, Any]) -> list[str]:
    payloads: list[str] = []
    url = _image_url_from_part(part)
    if url:
        payloads.append(url)
    file_id = _file_id_from_part(part)
    if file_id:
        payloads.append(file_id)
    return payloads


def _assert_text_has_no_media_payloads(text: str, payloads: list[str]) -> None:
    for payload in payloads:
        if payload and payload in text:
            _translation_error(
                "Adapted tool-result text still contains identified media data."
            )


def _placeholder_line(kind: str, index: int, total: int, call_id: str | None) -> str:
    source = f" from call_id={call_id}" if call_id else ""
    return f"{_OMITTED_NOTE_PREFIX} {kind} {index}/{total}{source}]"


def _source_caption(index: int, total: int, call_id: str | None) -> str:
    source = f" from call_id={call_id}" if call_id else ""
    return f"{_SOURCE_NOTE_PREFIX} {index}/{total}{source}."


def _responses_image_part(part: Mapping[str, Any]) -> dict[str, Any]:
    image: dict[str, Any] = {"type": "input_image"}
    url = _image_url_from_part(part)
    file_id = _file_id_from_part(part)
    detail = _detail_from_part(part)
    if url:
        image["image_url"] = url
    elif file_id:
        image["file_id"] = file_id
    else:
        _translation_error("Cannot lift an image part without image_url or file_id.")
    if detail:
        image["detail"] = detail
    return image


def _chat_image_part(part: Mapping[str, Any]) -> dict[str, Any]:
    url = _image_url_from_part(part)
    if not url:
        _translation_error(
            "Cannot lift a file-id image into Chat Completions message content."
        )
    image_url: dict[str, str] = {"url": url}
    detail = _detail_from_part(part)
    if detail:
        image_url["detail"] = detail
    return {"type": "image_url", "image_url": image_url}


def _user_image_item(
    *,
    target_format: str,
    caption: str,
    part: Mapping[str, Any],
) -> dict[str, Any]:
    if target_format == "chat_completions":
        return {
            "role": "user",
            "content": [
                {"type": "text", "text": caption},
                _chat_image_part(part),
            ],
        }
    return {
        "type": "message",
        "role": "user",
        "content": [
            {"type": "input_text", "text": caption},
            _responses_image_part(part),
        ],
    }


def _join_text(fragments: list[str]) -> str:
    return "\n".join(fragment for fragment in fragments if fragment)


def _compact_notice_item(target_format: str) -> dict[str, Any]:
    if target_format == "chat_completions":
        return {"role": "system", "content": VISUAL_CONTENT_OMITTED_NOTICE}
    return {
        "type": "message",
        "role": "developer",
        "content": VISUAL_CONTENT_OMITTED_NOTICE,
    }


def _replace_output_text(item: dict[str, Any], text: str, *, chat_tool: bool) -> dict[str, Any]:
    rewritten = dict(item)
    if chat_tool:
        rewritten["content"] = text
    else:
        rewritten["output"] = text
    return rewritten


def _call_id_from_item(item: Mapping[str, Any], *, chat_tool: bool) -> str | None:
    key = "tool_call_id" if chat_tool else "call_id"
    value = item.get(key)
    return value if isinstance(value, str) and value else None


def adapt_tool_result_item(
    item: Mapping[str, Any],
    policy: ToolResultMediaPolicy,
    *,
    chat_tool: bool = False,
) -> tuple[list[dict[str, Any]], ToolResultMediaStats]:
    """Adapt one function/tool result. Returns replacement items and stats."""

    stats = ToolResultMediaStats()
    raw_output = item.get("content") if chat_tool else item.get("output")
    stats.input_bytes = _measure_bytes(raw_output)
    if not output_contains_structured_media(raw_output):
        stats.output_bytes = stats.input_bytes
        return [dict(item)], stats
    texts, media = _split_output_parts(raw_output)
    if not media:
        stats.output_bytes = stats.input_bytes
        return [dict(item)], stats

    payloads: list[str] = []
    image_parts: list[dict[str, Any]] = []
    file_parts: list[dict[str, Any]] = []
    for part in media:
        kind = _media_kind(part)
        if kind == "image":
            image_parts.append(part)
            stats.structured_image_count += 1
        elif kind == "file":
            file_parts.append(part)
            stats.structured_file_count += 1
        else:
            _translation_error(
                f"Cannot adapt tool-result media type {kind!r}."
            )
        payloads.extend(_identified_payloads(part))

    other_media = file_parts
    carry_as_tool_result = policy.can_carry_tool_result_images and not other_media
    if carry_as_tool_result:
        stats.output_bytes = stats.input_bytes
        return [dict(item)], stats

    lift = policy.can_carry_message_images
    placeholder = policy.placeholder_authorized and policy.is_compact
    if not lift and not placeholder:
        _translation_error(
            "Structured tool-result media cannot be forwarded on this route."
        )

    call_id = _call_id_from_item(item, chat_tool=chat_tool)
    extra_text: list[str] = []
    follow_items: list[dict[str, Any]] = []
    total = len(image_parts)
    for index, part in enumerate(image_parts, start=1):
        can_lift_part = lift and (not chat_tool or bool(_image_url_from_part(part)))
        if can_lift_part:
            caption = _source_caption(index, total, call_id)
            _assert_text_has_no_media_payloads(caption, payloads)
            follow_items.append(
                _user_image_item(
                    target_format="chat_completions" if chat_tool else "responses",
                    caption=caption,
                    part=part,
                )
            )
            stats.lifted_image_count += 1
            continue
        if placeholder:
            extra_text.append(_placeholder_line("image", index, total, call_id))
            stats.omitted_image_count += 1
            stats.placeholder_count += 1
            continue
        _translation_error(
            "Structured tool-result images cannot be forwarded on this route."
        )

    if other_media:
        file_total = len(other_media)
        if placeholder:
            for index, _part in enumerate(other_media, start=1):
                extra_text.append(_placeholder_line("file", index, file_total, call_id))
                stats.omitted_file_count += 1
                stats.placeholder_count += 1
        else:
            _translation_error("Structured tool-result files cannot be forwarded on this route.")

    combined = _join_text([*texts, *extra_text])
    _assert_text_has_no_media_payloads(combined, payloads)
    rewritten = _replace_output_text(dict(item), combined, chat_tool=chat_tool)
    stats.output_bytes = _measure_bytes(rewritten.get("content" if chat_tool else "output"))
    return [rewritten, *follow_items], stats


def _is_responses_tool_result(item: Mapping[str, Any]) -> bool:
    return item.get("type") in {"function_call_output", "custom_tool_call_output"}


def _is_chat_tool_result(item: Mapping[str, Any]) -> bool:
    return item.get("role") == "tool" and "tool_call_id" in item


def _adapt_item_list(
    items: list[Any],
    policy: ToolResultMediaPolicy,
    *,
    chat_tool: bool,
) -> tuple[list[Any], ToolResultMediaStats, bool]:
    rewritten: list[Any] = []
    stats = ToolResultMediaStats()
    changed = False
    for item in items:
        if not isinstance(item, Mapping):
            rewritten.append(item)
            continue
        is_result = _is_chat_tool_result(item) if chat_tool else _is_responses_tool_result(item)
        if not is_result:
            rewritten.append(item)
            continue
        replacements, item_stats = adapt_tool_result_item(
            item, policy, chat_tool=chat_tool
        )
        stats.add(item_stats)
        if replacements != [item] or item_stats.structured_image_count or item_stats.structured_file_count:
            changed = True
        rewritten.extend(replacements)
    return rewritten, stats, changed


def _payload_already_has_notice(items: list[Any], *, chat: bool) -> bool:
    for item in items:
        if not isinstance(item, Mapping):
            continue
        if chat:
            content = item.get("content")
        else:
            content = item.get("content")
        if isinstance(content, str) and VISUAL_CONTENT_OMITTED_NOTICE in content:
            return True
        if isinstance(content, list):
            for part in content:
                if isinstance(part, Mapping) and isinstance(part.get("text"), str):
                    if VISUAL_CONTENT_OMITTED_NOTICE in part["text"]:
                        return True
    return False


def adapt_request_payload(
    payload: dict[str, Any],
    policy: ToolResultMediaPolicy,
    event_context: Mapping[str, Any] | None = None,
) -> tuple[bool, ToolResultMediaStats]:
    """Adapt tool-result media on a request payload. Returns whether it changed."""

    stats = ToolResultMediaStats()
    changed = False
    input_items = payload.get("input")
    if isinstance(input_items, list):
        rewritten, item_stats, item_changed = _adapt_item_list(
            input_items, policy, chat_tool=False
        )
        stats.add(item_stats)
        if item_changed:
            payload["input"] = rewritten
            changed = True
            input_items = rewritten
        if (
            stats.omitted_image_count or stats.omitted_file_count
        ) and not _payload_already_has_notice(input_items, chat=False):
            payload["input"] = [*input_items, _compact_notice_item("responses")]
            changed = True
    messages = payload.get("messages")
    if isinstance(messages, list):
        rewritten, item_stats, item_changed = _adapt_item_list(
            messages, policy, chat_tool=True
        )
        stats.add(item_stats)
        if item_changed:
            payload["messages"] = rewritten
            changed = True
            messages = rewritten
        if (
            stats.omitted_image_count or stats.omitted_file_count
        ) and not _payload_already_has_notice(messages, chat=True):
            payload["messages"] = [*messages, _compact_notice_item("chat_completions")]
            changed = True
    _store_stats(event_context, stats)
    return changed, stats


def _store_stats(event_context: Mapping[str, Any] | None, stats: ToolResultMediaStats) -> None:
    if not isinstance(event_context, dict):
        return
    existing = event_context.get(_EVENT_STATS_KEY)
    if isinstance(existing, ToolResultMediaStats):
        existing.add(stats)
        stored = existing
    else:
        stored = stats
        event_context[_EVENT_STATS_KEY] = stored
    event_context["omitted_tool_result_images"] = stored.omitted_image_count
    event_context["omitted_tool_result_files"] = stored.omitted_file_count
    event_context["lifted_tool_result_images"] = stored.lifted_image_count
    event_context["structured_tool_result_images"] = stored.structured_image_count


def stored_media_stats(event_context: Mapping[str, Any] | None) -> ToolResultMediaStats | None:
    if not isinstance(event_context, Mapping):
        return None
    stats = event_context.get(_EVENT_STATS_KEY)
    return stats if isinstance(stats, ToolResultMediaStats) else None


def _modalities_include_image(value: Any) -> bool | None:
    if value is None:
        return None
    if not isinstance(value, (list, tuple, set)):
        return None
    return any(str(item).lower() == "image" for item in value)


def policy_from_request(
    upstream: Mapping[str, Any] | None,
    payload: Mapping[str, Any] | None,
    event_context: Mapping[str, Any] | None = None,
) -> ToolResultMediaPolicy:
    """Build a policy from request-local facts. Unknown image support is not guessed."""

    context = event_context if isinstance(event_context, Mapping) else {}
    request_kind = context.get("request_kind")
    if not isinstance(request_kind, str) or not request_kind:
        request_kind = "main_generation"
    placeholder_authorized = bool(context.get("compact_placeholder_authorized"))
    upstream = upstream or {}
    payload = payload or {}
    target_format = str(upstream.get("upstream_format") or "responses")
    if target_format not in {"responses", "chat_completions"}:
        if isinstance(payload.get("messages"), list) and "input" not in payload:
            target_format = "chat_completions"
        else:
            target_format = "responses"

    modalities = upstream.get("input_modalities")
    message_images = _modalities_include_image(modalities)
    if message_images is None:
        import gateway_catalog_runtime as _catalog

        model_id = None
        for candidate in (
            payload.get("model") if isinstance(payload, Mapping) else None,
            upstream.get("upstream_model"),
        ):
            if isinstance(candidate, str) and candidate:
                model_id = candidate
                break
        catalog_modalities = _catalog.catalog_input_modalities(model_id, upstream)
        message_images = _modalities_include_image(catalog_modalities)

    tool_result_images = False
    explicit = upstream.get("supports_tool_result_images")
    if isinstance(explicit, bool):
        tool_result_images = explicit

    return ToolResultMediaPolicy(
        request_kind=request_kind,
        placeholder_authorized=placeholder_authorized,
        message_images_supported=message_images,
        tool_result_images_supported=tool_result_images,
        target_format=target_format,
    )


def _append_notice_to_text(text: str) -> str:
    if VISUAL_CONTENT_OMITTED_NOTICE in text:
        return text
    if not text:
        return VISUAL_CONTENT_OMITTED_NOTICE
    return text.rstrip() + "\n\n" + VISUAL_CONTENT_OMITTED_NOTICE


def _append_notice_to_content(content: Any) -> tuple[Any, bool]:
    if isinstance(content, str):
        next_text = _append_notice_to_text(content)
        return next_text, next_text != content
    if isinstance(content, list):
        rewritten: list[Any] = []
        changed = False
        appended = False
        for part in content:
            if (
                not appended
                and isinstance(part, Mapping)
                and isinstance(part.get("text"), str)
                and part.get("type") in {None, "text", "output_text", "input_text"}
            ):
                next_part = dict(part)
                next_text = _append_notice_to_text(next_part["text"])
                if next_text != next_part["text"]:
                    next_part["text"] = next_text
                    changed = True
                rewritten.append(next_part)
                appended = True
                continue
            rewritten.append(part)
        if not appended:
            rewritten.append({"type": "output_text", "text": VISUAL_CONTENT_OMITTED_NOTICE})
            changed = True
        return rewritten, changed
    return content, False


def apply_visual_omission_notice(payload: dict[str, Any]) -> bool:
    """Append the compact visual-omission notice to a complete model response."""

    changed = False
    output = payload.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "message" or "content" in item:
                next_content, item_changed = _append_notice_to_content(item.get("content"))
                if item_changed:
                    item["content"] = next_content
                    changed = True
                    return True
    nested = payload.get("response")
    if isinstance(nested, dict) and apply_visual_omission_notice(nested):
        return True
    choices = payload.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, Mapping):
                continue
            message = choice.get("message")
            if isinstance(message, dict) and "content" in message:
                next_content, item_changed = _append_notice_to_content(message.get("content"))
                if item_changed:
                    message["content"] = next_content
                    return True
    return changed


def should_annotate_compact_response(event_context: Mapping[str, Any] | None) -> bool:
    stats = stored_media_stats(event_context)
    if stats is None:
        omitted = 0
        if isinstance(event_context, Mapping):
            value = event_context.get("omitted_tool_result_images")
            if isinstance(value, int):
                omitted = value
        return omitted > 0
    return (stats.omitted_image_count + stats.omitted_file_count) > 0


def annotate_compact_response_payload(
    payload: dict[str, Any],
    event_context: Mapping[str, Any] | None,
) -> bool:
    if not should_annotate_compact_response(event_context):
        return False
    event_type = payload.get("type")
    if event_type in {None, "response.completed", "response.incomplete"}:
        return apply_visual_omission_notice(payload)
    if payload.get("object") == "chat.completion":
        return apply_visual_omission_notice(payload)
    choices = payload.get("choices")
    if isinstance(choices, list) and any(
        isinstance(choice, Mapping) and choice.get("finish_reason")
        for choice in choices
    ):
        return apply_visual_omission_notice(payload)
    return False
