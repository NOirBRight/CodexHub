"""Third-party vs official Responses stream start/visible/empty-completed classification."""

from __future__ import annotations

import json
from pathlib import Path

import gateway_compat
import gateway_stream_semantics
from gateway_exchange_adapters import live_handle_empty_completed
from gateway_stream_semantics import UpstreamEmptyCompletedResponseError


def _sse(payload: dict) -> bytes:
    return b"data: " + json.dumps(payload).encode() + b"\n\n"


def _text_delta(text: str = "hello") -> dict:
    return {"type": "response.output_text.delta", "delta": text}


def _summary_delta(text: str = "thinking") -> dict:
    return {"type": "response.reasoning_summary_text.delta", "delta": text}


def _raw_reasoning_delta(text: str = "secret chain") -> dict:
    return {"type": "response.reasoning_text.delta", "delta": text}


def _message_done(text: str = "done") -> dict:
    return {
        "type": "response.output_item.done",
        "item": {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text}],
        },
    }


def _reasoning_done() -> dict:
    return {
        "type": "response.output_item.done",
        "item": {
            "type": "reasoning",
            "summary": [{"type": "summary_text", "text": "thought"}],
        },
    }


def _completed(output: list) -> dict:
    return {"type": "response.completed", "response": {"id": "resp_x", "output": output}}


def test_xai_output_text_and_message_start_downstream() -> None:
    starts = gateway_stream_semantics.responses_event_starts_downstream_output
    assert starts(_text_delta())
    assert starts(_message_done())
    assert not starts({"type": "response.created", "response": {}})


def test_xai_reasoning_summary_starts_downstream_raw_does_not() -> None:
    starts = gateway_stream_semantics.responses_event_starts_downstream_output
    assert starts(_summary_delta())
    assert not starts(_reasoning_done())
    assert not starts(_raw_reasoning_delta())


def test_third_party_reasoning_is_not_visible_output() -> None:
    visible = gateway_stream_semantics.responses_event_has_visible_or_tool_output
    assert not visible(_summary_delta(), "xai")
    assert not visible(_raw_reasoning_delta(), "xai")
    assert not visible(_reasoning_done(), "xai")
    assert visible(_text_delta(), "xai")
    assert visible(_message_done(), "xai")


def test_official_reasoning_summary_and_text_are_visible() -> None:
    visible = gateway_stream_semantics.responses_event_has_visible_or_tool_output
    assert visible(_summary_delta(), "official")
    assert visible(_raw_reasoning_delta(), "official")
    assert visible(_text_delta(), "official")


def test_reasoning_only_completed_is_not_visible_on_xai() -> None:
    completed = _completed([{"type": "reasoning", "summary": [{"type": "summary_text", "text": "t"}]}])
    assert not gateway_stream_semantics.responses_completed_event_has_visible_or_tool_output(completed)
    assert not gateway_stream_semantics.responses_event_has_visible_or_tool_output(completed, "xai")


def test_message_completed_is_visible_on_xai() -> None:
    completed = _completed(
        [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "hi"}]}]
    )
    assert gateway_stream_semantics.responses_completed_event_has_visible_or_tool_output(completed)
    assert gateway_stream_semantics.responses_event_has_visible_or_tool_output(completed, "xai")


def test_tool_search_call_completed_is_visible() -> None:
    completed = _completed(
        [{"type": "tool_search_call", "call_id": "search_1", "execution": "client"}]
    )
    assert gateway_stream_semantics.responses_completed_event_has_visible_or_tool_output(completed)


def test_empty_completed_conjunction_matches_relay() -> None:
    disconnect = gateway_stream_semantics.third_party_empty_completed_is_disconnect
    assert disconnect(
        upstream_name="xai",
        downstream_output_started=False,
        visible_or_tool_output_seen=False,
    )
    assert not disconnect(
        upstream_name="xai",
        downstream_output_started=True,
        visible_or_tool_output_seen=False,
    )
    assert not disconnect(
        upstream_name="official",
        downstream_output_started=False,
        visible_or_tool_output_seen=False,
    )


def test_official_output_text_starts_and_commits() -> None:
    text = _text_delta()
    assert gateway_stream_semantics.responses_event_starts_downstream_output(text)
    assert gateway_stream_semantics.responses_event_commits_downstream_output(text, "official")
    assert gateway_stream_semantics.responses_event_has_visible_or_tool_output(text, "official")


def test_compatible_sse_line_drops_raw_reasoning_for_third_party() -> None:
    raw = _sse(_raw_reasoning_delta())
    assert gateway_compat.compatible_sse_line(raw, "xai") == b""
    summary = _sse(_summary_delta())
    assert gateway_compat.compatible_sse_line(summary, "xai") == summary
    text = _sse(_text_delta())
    assert gateway_compat.compatible_sse_line(text, "xai") == text


def test_compatible_sse_line_passthrough_for_official() -> None:
    raw = _sse(_raw_reasoning_delta())
    text = _sse(_text_delta())
    assert gateway_compat.compatible_sse_line(raw, "official") == raw
    assert gateway_compat.compatible_sse_line(text, "official") == text


class _EmptyCompletedHandler:
    def __init__(self) -> None:
        self.headers: list[tuple[int, str]] = []
        self.events: list[tuple[str, dict]] = []
        self._downstream_stream_commit = None

    def _send_sse_headers(self, status: int, upstream_name: str) -> bool:
        self.headers.append((status, upstream_name))
        return True

    def _write_sse_event(self, event: str, payload: dict) -> bool:
        self.events.append((event, payload))
        return True


def test_empty_completed_handler_writes_response_failed_terminal() -> None:
    handler = _EmptyCompletedHandler()
    wrote = live_handle_empty_completed(
        handler,
        UpstreamEmptyCompletedResponseError("Responses stream returned empty completed response"),
    )
    assert wrote is True
    assert handler.headers == [(502, "")]
    assert handler.events[0][0] == "response.failed"
    assert handler.events[0][1]["type"] == "response.failed"
    assert handler.events[0][1]["response"]["status"] == "failed"
    assert handler.events[0][1]["response"]["error"]["code"] == "upstream_empty_completed_response"


def test_ayaspace_reconnect_fixture_is_not_empty_disconnect_once_started() -> None:
    fixture = json.loads(
        (Path(__file__).resolve().parent / "fixtures" / "stream_reconnect_repro.json").read_text(
            encoding="utf-8"
        )
    )
    started = False
    visible = False
    for event in fixture["events"]:
        if gateway_stream_semantics.responses_event_starts_downstream_output(event):
            started = True
        if gateway_stream_semantics.responses_event_has_visible_or_tool_output(event, fixture["upstream"]):
            visible = True
        if event.get("type") == "response.completed":
            assert started is True
            assert visible is False
            assert not gateway_stream_semantics.third_party_empty_completed_is_disconnect(
                upstream_name=fixture["upstream"],
                downstream_output_started=started,
                visible_or_tool_output_seen=visible,
            )
