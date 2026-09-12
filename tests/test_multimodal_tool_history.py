from __future__ import annotations

import json

import pytest

import gateway_compat
import gateway_stream_semantics
import multimodal_tool_result
import protocol_translation
import route_primitives
from gateway_errors import UpstreamProtocolTranslationError
from io import BytesIO
from urllib.error import HTTPError

from gateway_transport import _upstream_failure_class
from route_primitives import RETRY_FAILURE_PERMANENT


MARKER = "data:image/png;base64," + ("ABCD" * 1000)
PLAIN_BASE64_IN_TEXT = "data:image/png;base64,USERCODESHOULDSTAY"


def _xai_upstream(**overrides):
    upstream = {
        "name": "xai",
        "upstream_model": "grok-4.6",
        "upstream_format": "responses",
        "tool_protocol": "responses_structured",
        "tool_surface_strategy": "eager",
        "input_modalities": ["text"],
    }
    upstream.update(overrides)
    return upstream


def _image_history(*, marker: str = MARKER, count: int = 1, extra_text: str | None = "ok", tools=None):
    items = []
    for index in range(count):
        call_id = f"c{index + 1}"
        output = []
        if extra_text:
            output.append({"type": "input_text", "text": extra_text})
        output.append({"type": "input_image", "image_url": marker})
        items.append(
            {
                "type": "function_call",
                "call_id": call_id,
                "name": "inspect_image",
                "arguments": "{}",
                "status": "completed",
            }
        )
        items.append(
            {
                "type": "function_call_output",
                "call_id": call_id,
                "output": output,
            }
        )
    payload = {"model": "xai/grok-4.6", "input": items, "stream": True}
    if tools is not None:
        payload["tools"] = tools
    return payload


def _transform(payload: dict, upstream: dict, event_context: dict | None = None):
    return json.loads(
        gateway_compat.compatible_request_body(
            json.dumps(payload).encode("utf-8"),
            upstream,
            event_context=event_context if event_context is not None else {},
            inject_codex_tools=False,
            behavior_profile=route_primitives.BEHAVIOR_CODEX_APP_EXTERNAL_ADAPTER,
        )
    )


def _collect_text(value, *, in_media: bool = False) -> str:
    if isinstance(value, str):
        return "" if in_media else value
    if isinstance(value, list):
        return "".join(_collect_text(item, in_media=in_media) for item in value)
    if isinstance(value, dict):
        chunks = []
        for key, item in value.items():
            child_media = in_media or key in {"image_url", "url", "file_id"}
            chunks.append(_collect_text(item, in_media=child_media))
        return "".join(chunks)
    return ""


def test_absent_tools_do_not_stringify_image_tool_results_on_generation():
    payload = _image_history()
    with pytest.raises(UpstreamProtocolTranslationError) as raised:
        _transform(payload, _xai_upstream())
    assert "media" in str(raised.value).lower() or "image" in str(raised.value).lower()


def test_compact_without_trusted_marker_cannot_placeholder_images():
    payload = _image_history()
    with pytest.raises(UpstreamProtocolTranslationError):
        _transform(
            payload,
            _xai_upstream(),
            event_context={"request_kind": "compact", "compact_placeholder_authorized": False},
        )


def test_trusted_compact_placeholders_omit_base64_and_do_not_scale_with_payload():
    small_marker = "data:image/png;base64,AA=="
    large_payload = _image_history(marker=MARKER, count=82, extra_text="note")
    small_payload = _image_history(marker=small_marker, count=82, extra_text="note")
    context = {"request_kind": "compact", "compact_placeholder_authorized": True}
    large = _transform(large_payload, _xai_upstream(), event_context=dict(context))
    small = _transform(small_payload, _xai_upstream(), event_context=dict(context))
    large_text = _collect_text(large)
    small_text = _collect_text(small)
    assert MARKER not in large_text
    assert MARKER not in json.dumps(large)
    assert small_marker not in json.dumps(small)
    assert multimodal_tool_result.VISUAL_CONTENT_OMITTED_NOTICE in large_text
    assert "omitted tool result image" in large_text
    assert abs(len(large_text) - len(small_text)) < 200


def test_message_image_capability_lifts_tool_images_next_to_results():
    payload = _image_history(extra_text="visible text")
    transformed = _transform(
        payload,
        _xai_upstream(input_modalities=["text", "image"]),
    )
    text = _collect_text(transformed)
    assert MARKER not in text
    assert "visible text" in text
    image_items = [
        item
        for item in transformed["input"]
        if isinstance(item, dict)
        and item.get("type") == "message"
        and item.get("role") == "user"
        and any(
            isinstance(part, dict) and part.get("type") == "input_image"
            for part in (item.get("content") or [])
            if isinstance(item.get("content"), list)
        )
    ]
    assert len(image_items) == 1
    assert image_items[0]["content"][1]["image_url"] == MARKER
    assert image_items[0]["role"] != "developer"
    assert image_items[0]["role"] != "system"
    assert "call_id=c1" in _collect_text(image_items[0])


def test_plain_user_base64_text_is_not_scrubbed():
    payload = {
        "model": "xai/grok-4.6",
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": f"keep {PLAIN_BASE64_IN_TEXT}"}
                ],
            },
            {
                "type": "function_call",
                "call_id": "c1",
                "name": "inspect_image",
                "arguments": "{}",
                "status": "completed",
            },
            {
                "type": "function_call_output",
                "call_id": "c1",
                "output": [{"type": "input_image", "image_url": MARKER}],
            },
        ],
    }
    transformed = _transform(
        payload,
        _xai_upstream(input_modalities=["text", "image"]),
    )
    assert PLAIN_BASE64_IN_TEXT in _collect_text(transformed)
    assert MARKER not in _collect_text(transformed)


def test_official_passthrough_keeps_function_output_images():
    payload = _image_history()
    transformed = json.loads(
        gateway_compat.compatible_request_body(
            json.dumps(payload).encode("utf-8"),
            {"name": "official", "upstream_model": "gpt-5.6-sol"},
            behavior_profile=route_primitives.BEHAVIOR_OFFICIAL_CODEX_APP_HTTP_PASSTHROUGH,
        )
    )
    outputs = [
        item
        for item in transformed["input"]
        if isinstance(item, dict) and item.get("type") == "function_call_output"
    ]
    assert outputs
    assert outputs[0]["output"][1]["image_url"] == MARKER


def test_compat_then_chat_does_not_carry_base64_in_tool_text():
    payload = _image_history(
        extra_text="caption",
        tools=[{"type": "function", "name": "inspect_image", "parameters": {"type": "object"}}],
    )
    adapted = _transform(
        payload,
        _xai_upstream(input_modalities=["text", "image"]),
    )
    exchange = protocol_translation.prepare_exchange(
        json.dumps(adapted).encode("utf-8"),
        inbound_format="responses",
        outbound_format="chat_completions",
    )
    chat_payload = json.loads(exchange.upstream_body)
    tool_messages = [message for message in chat_payload["messages"] if message.get("role") == "tool"]
    assert tool_messages
    for message in tool_messages:
        content = message.get("content")
        if isinstance(content, str):
            assert MARKER not in content
        else:
            assert MARKER not in _collect_text(content)
    user_images = [
        message
        for message in chat_payload["messages"]
        if message.get("role") == "user"
        and isinstance(message.get("content"), list)
        and any(part.get("type") == "image_url" for part in message["content"] if isinstance(part, dict))
    ]
    assert user_images
    assert user_images[0]["content"][1]["image_url"]["url"] == MARKER


def test_mixed_calls_urls_and_duplicates_preserve_order():
    url = "https://example.test/a.png"
    payload = {
        "model": "xai/grok-4.6",
        "input": [
            {"type": "function_call", "call_id": "c1", "name": "inspect_image", "arguments": "{}", "status": "completed"},
            {
                "type": "function_call_output",
                "call_id": "c1",
                "output": [
                    {"type": "input_text", "text": "first"},
                    {"type": "input_image", "image_url": MARKER},
                    {"type": "input_image", "image_url": MARKER},
                    {"type": "input_image", "image_url": url, "detail": "high"},
                ],
            },
            {"type": "function_call", "call_id": "c2", "name": "inspect_image", "arguments": "{}", "status": "completed"},
            {
                "type": "function_call_output",
                "call_id": "c2",
                "output": [{"type": "input_image", "image_url": url}],
            },
        ],
    }
    transformed = _transform(
        payload,
        _xai_upstream(input_modalities=["text", "image"]),
    )
    captions = [
        part["text"]
        for item in transformed["input"]
        if isinstance(item, dict) and item.get("role") == "user"
        for part in item.get("content", [])
        if isinstance(part, dict) and part.get("type") == "input_text"
    ]
    assert captions == [
        "Tool result image 1/3 from call_id=c1.",
        "Tool result image 2/3 from call_id=c1.",
        "Tool result image 3/3 from call_id=c1.",
        "Tool result image 1/1 from call_id=c2.",
    ]


def test_unknown_media_type_fails_closed():
    payload = {
        "model": "xai/grok-4.6",
        "input": [
            {"type": "function_call", "call_id": "c1", "name": "inspect_image", "arguments": "{}", "status": "completed"},
            {
                "type": "function_call_output",
                "call_id": "c1",
                "output": [{"type": "input_unknown_blob", "data": MARKER}],
            },
        ],
    }
    with pytest.raises(UpstreamProtocolTranslationError):
        _transform(payload, _xai_upstream(input_modalities=["text", "image"]))


def test_compact_response_notice_is_written_into_summary():
    context = {
        "request_kind": "compact",
        "compact_placeholder_authorized": True,
        "omitted_tool_result_images": 2,
    }
    stats = multimodal_tool_result.ToolResultMediaStats(omitted_image_count=2)
    context[multimodal_tool_result._EVENT_STATS_KEY] = stats
    body = json.dumps(
        {
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Earlier work was summarized."}],
                }
            ]
        }
    ).encode("utf-8")
    transformed = json.loads(
        gateway_compat.compatible_response_body(body, "xai", event_context=context)
    )
    text = transformed["output"][0]["content"][0]["text"]
    assert multimodal_tool_result.VISUAL_CONTENT_OMITTED_NOTICE in text
    assert "Earlier work was summarized." in text


def test_compact_sse_completed_event_receives_notice():
    context = {
        "request_kind": "compact",
        "compact_placeholder_authorized": True,
        "omitted_tool_result_images": 1,
    }
    stats = multimodal_tool_result.ToolResultMediaStats(omitted_image_count=1)
    context[multimodal_tool_result._EVENT_STATS_KEY] = stats
    event = {
        "type": "response.completed",
        "response": {
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Summary text."}],
                }
            ]
        },
    }
    line = b"data: " + json.dumps(event).encode("utf-8") + b"\n\n"
    rewritten = gateway_compat.compatible_sse_line(line, "xai", event_context=context)
    payload = json.loads(rewritten.split(b"data:", 1)[1].strip())
    text = payload["response"]["output"][0]["content"][0]["text"]
    assert multimodal_tool_result.VISUAL_CONTENT_OMITTED_NOTICE in text


def test_chat_converted_sse_completed_event_receives_notice():
    context = {
        "request_kind": "compact",
        "compact_placeholder_authorized": True,
        "omitted_tool_result_images": 1,
    }
    stats = multimodal_tool_result.ToolResultMediaStats(omitted_image_count=1)
    context[multimodal_tool_result._EVENT_STATS_KEY] = stats
    event = {
        "type": "response.completed",
        "response": {
            "output": [
                {"type": "reasoning", "summary": []},
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Summary text."}],
                },
            ]
        },
    }
    line = b"data: " + json.dumps(event).encode("utf-8") + b"\n\n"
    rewritten = gateway_compat.compatible_sse_line(
        line, "commandcode", event_context=context, runtime_tool_inverse_only=True
    )
    payload = json.loads(rewritten.split(b"data:", 1)[1].strip())
    text = payload["response"]["output"][1]["content"][0]["text"]
    assert multimodal_tool_result.VISUAL_CONTENT_OMITTED_NOTICE in text


def test_trusted_compact_header_does_not_use_natural_language():
    headers = {"x-codex-turn-metadata": json.dumps({"request_kind": "compaction"})}
    assert gateway_stream_semantics.trusted_compact_request(headers) is True
    heuristic_payload = {
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": (
                    "Create a detailed summary of the conversation so far. "
                    "Do not call any tools. The summary should include <summary>."
                ),
            }
        ]
    }
    assert gateway_stream_semantics.trusted_compact_request({}) is False
    assert (
        gateway_stream_semantics.request_kind_from_headers_and_payload(
            {}, heuristic_payload, "responses"
        )
        == "compact"
    )


def test_prompt_length_overflow_is_permanent_and_not_retryable():
    body = json.dumps(
        {
            "code": "invalid-argument",
            "error": "This model's maximum prompt length is 500000 but the request contains 14484524 tokens.",
        }
    ).encode("utf-8")
    exc = HTTPError("https://example.test/v1", 400, "error", {}, BytesIO(body))
    assert _upstream_failure_class(exc) == RETRY_FAILURE_PERMANENT


def test_direct_chat_translation_of_image_tool_output_still_fails_closed():
    body = {
        "model": "example-model",
        "input": [
            {
                "type": "function_call_output",
                "call_id": "call_123",
                "output": [{"type": "input_image", "image_url": "data:image/png;base64,AA=="}],
            }
        ],
    }
    with pytest.raises(protocol_translation.UnsupportedProtocolTranslationError) as raised:
        protocol_translation.responses_request_to_chat_completion_body(
            json.dumps(body).encode("utf-8")
        )
    assert raised.value.code == "unsupported_protocol_semantics"
