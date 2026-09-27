"""xAI's recorded search stream can reuse one reasoning id across output indices."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import gateway_compat
from gateway_errors import UpstreamProtocolTranslationError


FIXTURE = Path(__file__).parent / "fixtures/xai_reasoning_identity/rez-search.jsonl"
XAI = {
    "name": "xai",
    "provider_id": "xai",
    "upstream_format": "responses",
    "tool_protocol": "responses_structured",
    "tool_surface_strategy": "eager",
}


def recorded_events() -> list[dict]:
    return [json.loads(line) for line in FIXTURE.read_text().splitlines()]


def request(context: dict, *, input_items: list[dict] | None = None) -> dict:
    body = {
        "model": "grok-4.7",
        "input": input_items or [{"role": "user", "content": "Search the web."}],
        "tools": [{"type": "web_search"}],
        "stream": True,
        "store": False,
    }
    return json.loads(gateway_compat.compatible_request_body(
        json.dumps(body).encode(), XAI, event_context=context, inject_codex_tools=False,
    ))


def test_recorded_xai_search_stream_and_history_roundtrip():
    context: dict = {}
    request(context)
    emitted = []
    for event in recorded_events():
        line = gateway_compat.compatible_sse_line(
            b"data: " + json.dumps(event).encode() + b"\n", "xai", context,
        )
        if line:
            emitted.append(json.loads(line.split(b":", 1)[1]))

    assert emitted[-1]["type"] == "response.completed"
    output = emitted[-1]["response"]["output"]
    assert len(output) == 12
    assert len({item["id"] for item in output}) == 12
    assert [item["type"] for item in output].count("web_search_call") == 4
    assert sum(event["type"] == "response.output_text.annotation.added" for event in emitted) == 11
    assert emitted[-1]["response"]["usage"] == recorded_events()[-1]["response"]["usage"]
    by_index = {index: item["id"] for index, item in enumerate(output)}
    for event in emitted:
        index = event.get("output_index")
        if event["type"] in {"response.output_item.added", "response.output_item.done"}:
            assert event["item"]["id"] == by_index[index]
        if isinstance(event.get("item_id"), str) and index in {0, 5, 10}:
            assert event["item_id"] == by_index[index]
    assert output[0]["summary"] and not output[5]["summary"] and not output[10]["summary"]
    plan = gateway_compat.official_passthrough.request_tool_plan(context)
    assert plan.decode_history(output) == output
    assert plan.encode_history(output) == output

    body_context: dict = {}
    request(body_context)
    body_output = json.loads(gateway_compat.compatible_response_body(
        json.dumps(recorded_events()[-1]["response"]).encode(), "xai", body_context,
    ))["output"]
    assert [item["id"] for item in body_output] == [item["id"] for item in output]

    next_context: dict = {}
    replay = request(next_context, input_items=output + [{"role": "user", "content": "Continue."}])
    assert all(item.get("type") == "message" for item in replay["input"])
    assert not any("rs_codexhub_xai_" in json.dumps(item) for item in replay["input"])
    assert any(item.get("role") == "developer" for item in replay["input"])
    assert any(item.get("role") == "assistant" for item in replay["input"])


def test_recorded_xai_search_nonstream_uses_same_ids():
    context: dict = {}
    request(context)
    payload = recorded_events()[-1]["response"]
    decoded = json.loads(gateway_compat.compatible_response_body(
        json.dumps(payload).encode(), "xai", context,
    ))
    assert decoded["status"] == "completed"
    assert len({item["id"] for item in decoded["output"]}) == 12
    stream_ids = [item["id"] for item in decoded["output"] if item["type"] == "reasoning"]
    assert len(stream_ids) == 7


def test_xai_search_and_reasoning_can_complete_out_of_added_order():
    events = recorded_events()
    reasoning_added = next(e for e in events if e["type"] == "response.output_item.added" and e["output_index"] == 7)
    search_done = next(e for e in events if e["type"] == "response.output_item.done" and e["output_index"] == 6)
    events.remove(reasoning_added)
    events.insert(events.index(search_done), reasoning_added)
    context: dict = {}
    request(context)
    terminal = None
    for event in events:
        line = gateway_compat.compatible_sse_line(
            b"data: " + json.dumps(event).encode() + b"\n", "xai", context,
        )
        if event["type"] == "response.completed":
            terminal = json.loads(line.split(b":", 1)[1])
    assert terminal["type"] == "response.completed"


def test_other_provider_still_rejects_recorded_duplicate_ids():
    context: dict = {}
    upstream = {**XAI, "name": "other", "provider_id": "other"}
    body = {"model": "model", "input": [{"role": "user", "content": "Search."}], "tools": [{"type": "web_search"}]}
    gateway_compat.compatible_request_body(json.dumps(body).encode(), upstream, event_context=context, inject_codex_tools=False)
    with pytest.raises(UpstreamProtocolTranslationError):
        gateway_compat.compatible_response_body(json.dumps(recorded_events()[-1]["response"]).encode(), "other", context)


@pytest.mark.parametrize("collision", ["tool_item", "cross_type", "unknown_type"])
def test_xai_nonstream_still_rejects_other_identity_collisions(collision):
    context: dict = {}
    request(context)
    payload = recorded_events()[-1]["response"]
    output = [dict(item) for item in payload["output"]]
    if collision == "unknown_type":
        output[0].pop("type")
        output[0]["id"] = output[5]["id"]
    else:
        output[3]["id"] = output[1 if collision == "tool_item" else 0]["id"]
    with pytest.raises(UpstreamProtocolTranslationError) as exc:
        gateway_compat.compatible_response_body(
            json.dumps({**payload, "output": output}).encode(), "xai", context,
        )
    assert exc.value.classification == "duplicate_item_identity"


@pytest.mark.parametrize("mutation", ["duplicate_done", "missing_tool_done", "unindexed_ambiguous_reference"])
def test_xai_stream_rejects_invalid_lifecycles(mutation):
    events = recorded_events()
    if mutation == "duplicate_done":
        event = next(e for e in events if e["type"] == "response.output_item.done" and e["output_index"] == 5)
        events.insert(events.index(event) + 1, event)
    elif mutation == "missing_tool_done":
        events = [e for e in events if not (e["type"] == "response.output_item.done" and e["output_index"] == 6)]
    else:
        added = next(e for e in events if e["type"] == "response.output_item.added" and e["output_index"] == 5)
        events.insert(events.index(added) + 1, {
            "type": "response.reasoning_summary_part.added",
            "item_id": added["item"]["id"],
            "summary_index": 0,
            "part": {"type": "summary_text", "text": ""},
        })
    context: dict = {}
    request(context)
    with pytest.raises(UpstreamProtocolTranslationError):
        for event in events:
            gateway_compat.compatible_sse_line(
                b"data: " + json.dumps(event).encode() + b"\n", "xai", context,
            )
