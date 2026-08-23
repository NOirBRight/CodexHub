"""SSE assertion helpers for Gateway characterization tests."""

from __future__ import annotations

import json
from typing import Any

from sse_events import SseEvent, SseEventAssembler

RESPONSES_TERMINAL_EVENTS = frozenset(
    {
        b"response.completed",
        b"response.failed",
        b"response.incomplete",
    }
)


def parsed_sse_events(body: bytes) -> tuple[SseEvent, ...]:
    assembler = SseEventAssembler()
    events = list(assembler.feed(body))
    termination = assembler.finish()
    events.extend(termination.events)
    return tuple(events)


def event_payload(event: SseEvent) -> dict[str, Any] | None:
    if not event.data or event.data == b"[DONE]":
        return None
    try:
        payload = json.loads(event.data)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


def require_single_terminal(
    events: tuple[SseEvent, ...],
    *,
    terminal_names: frozenset[bytes] = RESPONSES_TERMINAL_EVENTS,
) -> SseEvent:
    terminals: list[SseEvent] = []
    for event in events:
        if event.event in terminal_names:
            terminals.append(event)
            continue
        payload = event_payload(event)
        if isinstance(payload, dict):
            event_type = payload.get("type")
            if isinstance(event_type, str) and event_type.encode("utf-8") in terminal_names:
                terminals.append(event)
    if len(terminals) != 1:
        names = [
            event.event.decode("utf-8", "replace") if event.event else "<unnamed>"
            for event in events
        ]
        raise AssertionError(
            f"expected exactly one terminal SSE event from {sorted(n.decode() for n in terminal_names)}; "
            f"found {len(terminals)} among {names}"
        )
    return terminals[0]
