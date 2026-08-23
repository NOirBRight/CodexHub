"""Direct seam tests for the Gateway apply_patch adapter."""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

import apply_patch_adapter
import gateway_errors
from apply_patch_adapter import (
    APPLY_PATCH_ADAPTER_ERROR_CODE,
    APPLY_PATCH_ADAPTER_EVENT,
    APPLY_PATCH_HISTORY_ADAPTER_EVENT,
    ApplyPatchAdapter,
    ApplyPatchFacts,
)


FORBIDDEN_SOURCE_MARKERS = (
    "import codex_proxy",
    "from codex_proxy",
    "gateway_sse",
    "transport",
    "urllib3",
    "urlopen",
    "BaseHTTPRequestHandler",
)

PATCH = "*** Begin Patch\n*** Update File: target.txt\n@@\n-before\n+after\n*** End Patch"
SECRET_PATCH = "*** Begin Patch\n*** Update File: secret.txt\n@@\n-SECRET_BODY\n+leaked\n*** End Patch"


def _adapter(events: list | None = None) -> tuple[ApplyPatchAdapter, list]:
    captured = events if events is not None else []

    def write_event(event_context, event, **fields):
        captured.append((event, fields, event_context))

    adapter = ApplyPatchAdapter(facts=ApplyPatchFacts(), write_event=write_event)
    return adapter, captured


def _function_call(*, item_id="item_patch", call_id="call_patch", patch=PATCH, status="completed"):
    return {
        "id": item_id,
        "type": "function_call",
        "status": status,
        "call_id": call_id,
        "name": "apply_patch",
        "arguments": json.dumps({"patch": patch}, ensure_ascii=True, separators=(",", ":")),
    }


def _custom_call(*, call_id="call_patch", patch=PATCH, status="completed", item_id=None):
    item = {
        "type": "custom_tool_call",
        "status": status,
        "call_id": call_id,
        "name": "apply_patch",
        "input": patch,
    }
    if item_id is not None:
        item["id"] = item_id
    return item


def test_apply_patch_adapter_typed_context_is_the_seam():
    assert {"facts", "write_event"} <= set(ApplyPatchAdapter.__annotations__)
    source = Path(apply_patch_adapter.__file__).read_text(encoding="utf-8")
    for marker in FORBIDDEN_SOURCE_MARKERS:
        assert marker not in source
    assert inspect.isclass(ApplyPatchFacts)
    assert inspect.isclass(ApplyPatchAdapter)
    assert "getattr(" not in source


def test_adapt_custom_tool_history_reconstructs_exact_pair():
    adapter, events = _adapter()
    apply_call = _custom_call()
    apply_output = {
        "type": "custom_tool_call_output",
        "call_id": "call_patch",
        "output": "Success. Updated target.txt",
    }
    unrelated = {
        "type": "custom_tool_call",
        "status": "completed",
        "call_id": "call_other",
        "name": "shell",
        "input": "echo hi",
    }
    rewritten, adapted_call_ids, changed = adapter.adapt_custom_tool_history(
        [unrelated, apply_call, apply_output],
        event_context={"request_id": "req-history"},
    )
    assert changed is True
    assert adapted_call_ids == {"call_patch"}
    assert rewritten[0] is unrelated
    assert rewritten[1]["type"] == "function_call"
    assert rewritten[1]["name"] == "apply_patch"
    assert rewritten[1]["arguments"] == json.dumps({"patch": PATCH}, ensure_ascii=True, separators=(",", ":"))
    assert rewritten[2] == {"type": "function_call_output", "call_id": "call_patch", "output": "Success. Updated target.txt"}
    outcomes = {(event, fields["outcome"], fields["count"]) for event, fields, _ in events}
    assert outcomes == {
        (APPLY_PATCH_HISTORY_ADAPTER_EVENT, "adapted", 1),
        (APPLY_PATCH_HISTORY_ADAPTER_EVENT, "untouched", 1),
    }
    for _, fields, _ in events:
        for forbidden in ("call_id", "name", "input", "output", "patch", "arguments"):
            assert forbidden not in fields


def test_apply_patch_lifecycle_status_is_phase_specific():
    adapter, _ = _adapter()
    with pytest.raises(Exception):
        adapter.adapt_response_body({"output": [_function_call(status="in_progress")]})
    with pytest.raises(Exception):
        adapter.adapt_stream_events([
            {"type": "response.output_item.added", "output_index": 0, "item": _function_call(status="completed")},
        ])


def test_adapt_response_body_rewrites_function_call_to_custom_tool():
    adapter, events = _adapter()
    payload, changed = adapter.adapt_response_body(
        {"output": [_function_call()]},
        {"request_id": "req-body"},
    )
    assert changed is True
    item = payload["output"][0]
    assert item["type"] == "custom_tool_call"
    assert item["input"] == PATCH
    assert "arguments" not in item
    assert events == [
        (
            APPLY_PATCH_ADAPTER_EVENT,
            {"surface": "body", "outcome": "adapted", "count": 1},
            {"request_id": "req-body"},
        )
    ]


def test_adapt_stream_events_rewrites_lifecycle_to_custom_tool():
    adapter, events = _adapter()
    added = _function_call(status="in_progress")
    added["arguments"] = ""
    done_item = _function_call()
    stream = [
        {"type": "response.output_item.added", "output_index": 0, "item": added},
        {
            "type": "response.function_call_arguments.done",
            "output_index": 0,
            "item_id": "item_patch",
            "arguments": done_item["arguments"],
        },
        {"type": "response.output_item.done", "output_index": 0, "item": done_item},
        {"type": "response.completed", "response": {"output": [done_item]}}
    ]
    rewritten, changed = adapter.adapt_stream_events(
        stream,
        event_context={"request_id": "req-stream"},
    )
    assert changed is True
    assert rewritten[0]["item"]["type"] == "custom_tool_call"
    assert rewritten[0]["item"]["input"] == ""
    assert rewritten[1]["type"] == "response.custom_tool_call_input.done"
    assert rewritten[1]["input"] == PATCH
    assert "arguments" not in rewritten[1]
    assert rewritten[2]["item"]["type"] == "custom_tool_call"
    assert rewritten[2]["item"]["input"] == PATCH
    assert rewritten[3]["response"]["output"][0]["type"] == "custom_tool_call"
    assert events[-1][0] == APPLY_PATCH_ADAPTER_EVENT
    assert events[-1][1]["outcome"] == "adapted"


def test_invalid_shapes_fail_closed_without_payload_leak():
    adapter, events = _adapter()
    extra_field = _function_call()
    extra_field["extra"] = SECRET_PATCH
    with pytest.raises(gateway_errors.UpstreamProtocolTranslationError) as raised:
        adapter.adapt_response_body({"output": [extra_field]}, {"request_id": "req-invalid"})
    assert raised.value.cause.code == APPLY_PATCH_ADAPTER_ERROR_CODE
    assert events == [
        (
            APPLY_PATCH_ADAPTER_EVENT,
            {"surface": "body", "outcome": "rejected", "count": 1, "reason": "function_call_fields_not_exact"},
            {"request_id": "req-invalid"},
        )
    ]
    assert "SECRET_BODY" not in repr(events)

    history_adapter, history_events = _adapter()
    with pytest.raises(gateway_errors.UpstreamProtocolTranslationError) as history_raised:
        history_adapter.adapt_custom_tool_history(
            [_custom_call(patch=SECRET_PATCH)],
            event_context={"request_id": "req-unpaired"},
        )
    assert history_raised.value.cause.code == APPLY_PATCH_ADAPTER_ERROR_CODE
    assert history_events == [
        (APPLY_PATCH_HISTORY_ADAPTER_EVENT, {"outcome": "rejected", "count": 1}, {"request_id": "req-unpaired"})
    ]
    assert "SECRET_BODY" not in repr(history_events)

    empty_adapter, empty_events = _adapter()
    empty_call = _function_call(patch="   ")
    with pytest.raises(gateway_errors.UpstreamProtocolTranslationError) as empty_raised:
        empty_adapter.adapt_response_body({"output": [empty_call]}, {"request_id": "req-empty"})
    assert empty_raised.value.cause.code == APPLY_PATCH_ADAPTER_ERROR_CODE
    assert empty_events[0][1]["reason"] == "patch_empty"


def test_disabled_context_is_passthrough():
    adapter, events = _adapter()
    items = [_custom_call()]
    rewritten, adapted_call_ids, changed = adapter.adapt_custom_tool_history(
        items,
        event_context={"_apply_patch_adapter_enabled": False},
    )
    assert rewritten is items
    assert adapted_call_ids == set()
    assert changed is False
    payload = {"output": [_function_call()]}
    body, body_changed = adapter.adapt_response_body(payload, {"_apply_patch_adapter_enabled": False})
    assert body is payload
    assert body_changed is False
    stream = [{"type": "response.output_item.added", "output_index": 0, "item": _function_call()}]
    rewritten_events, stream_changed = adapter.adapt_stream_events(
        stream,
        event_context={"_apply_patch_adapter_enabled": False},
    )
    assert rewritten_events is stream
    assert stream_changed is False
    assert events == []


def test_facade_wrappers_use_live_write_event(monkeypatch):
    import codex_proxy
    import gateway_stream_semantics

    seen = []

    def write_event(event_context, event, **fields):
        seen.append((event, fields, event_context))

    monkeypatch.setattr(codex_proxy, "_write_adapter_event", write_event)
    payload, changed = codex_proxy.adapt_third_party_apply_patch_response_body(
        {"output": [_function_call()]},
        {"request_id": "live-write"},
    )
    assert changed is True
    assert payload["output"][0]["type"] == "custom_tool_call"
    assert seen == [
        (
            APPLY_PATCH_ADAPTER_EVENT,
            {"surface": "body", "outcome": "adapted", "count": 1},
            {"request_id": "live-write"},
        )
    ]
    live_adapter = codex_proxy.apply_patch_adapter()
    assert live_adapter.write_event is write_event

    monkeypatch.setattr(
        gateway_stream_semantics,
        "RESPONSES_TERMINAL_EVENT_TYPES",
        {"response.custom_terminal"},
    )
    terminal_adapter = codex_proxy.apply_patch_adapter()
    assert terminal_adapter.facts.terminal_event_types == frozenset({"response.custom_terminal"})
    events, changed = terminal_adapter.adapt_stream_events([{"type": "response.custom_terminal"}])
    assert events == [{"type": "response.custom_terminal"}]
    assert changed is False
