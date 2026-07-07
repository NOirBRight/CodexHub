from __future__ import annotations

import json
import re
from collections.abc import Mapping as MappingABC
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping


MULTI_AGENT_TOOL_NAMES = {"spawn_agent", "wait_agent", "close_agent", "resume_agent", "send_input"}


@dataclass(frozen=True)
class ProtocolViolation:
    code: str
    agent_id: str | None = None
    detail: str = ""


@dataclass(frozen=True)
class ProtocolEvent:
    kind: str
    call_id: str | None = None
    agent_id: str | None = None
    prompt: str = ""
    nickname: str | None = None
    targets: tuple[str, ...] = ()
    target: str | None = None
    results: Mapping[str, str] = field(default_factory=dict)
    message: str = ""

    @classmethod
    def spawn(cls, call_id: str, agent_id: str, prompt: str, nickname: str | None = None) -> "ProtocolEvent":
        return cls(kind="spawn", call_id=call_id, agent_id=agent_id, prompt=prompt, nickname=nickname)

    @classmethod
    def wait(cls, call_id: str, targets: tuple[str, ...], results: Mapping[str, str]) -> "ProtocolEvent":
        return cls(kind="wait", call_id=call_id, targets=targets, results=dict(results))

    @classmethod
    def close(cls, call_id: str, target: str) -> "ProtocolEvent":
        return cls(kind="close", call_id=call_id, target=target)

    @classmethod
    def send_input(cls, call_id: str, target: str, message: str) -> "ProtocolEvent":
        return cls(kind="send_input", call_id=call_id, target=target, message=message)

    @classmethod
    def resume(cls, call_id: str, target: str, message: str) -> "ProtocolEvent":
        return cls(kind="resume", call_id=call_id, target=target, message=message)


@dataclass
class AgentRecord:
    agent_id: str
    spawn_call_id: str | None
    prompt: str
    nickname: str | None = None
    waited: bool = False
    closed: bool = False
    result: str = ""
    needs_input: bool = False


@dataclass
class ProtocolState:
    agents: dict[str, AgentRecord] = field(default_factory=dict)
    violations: list[ProtocolViolation] = field(default_factory=list)

    @property
    def open_agent_ids(self) -> list[str]:
        return [agent_id for agent_id, agent in self.agents.items() if not agent.closed]

    @property
    def waitable_agent_ids(self) -> list[str]:
        return [
            agent_id
            for agent_id, agent in self.agents.items()
            if not agent.closed and not agent.waited and not agent.needs_input
        ]

    @property
    def needs_input_agent_ids(self) -> list[str]:
        return [agent_id for agent_id, agent in self.agents.items() if not agent.closed and agent.needs_input]

    @property
    def closeable_agent_ids(self) -> list[str]:
        return [agent_id for agent_id, agent in self.agents.items() if not agent.closed and agent.waited]

    @property
    def closed_agent_ids(self) -> list[str]:
        return [agent_id for agent_id, agent in self.agents.items() if agent.closed]

    @property
    def lifecycle_complete(self) -> bool:
        return bool(self.agents) and not self.open_agent_ids and not self.violations


def reduce_protocol_events(events: Iterable[ProtocolEvent]) -> ProtocolState:
    state = ProtocolState()
    for event in events:
        if event.kind == "spawn":
            if not event.agent_id:
                state.violations.append(ProtocolViolation("spawn_missing_agent_id", detail=str(event.call_id or "")))
                continue
            state.agents[event.agent_id] = AgentRecord(
                agent_id=event.agent_id,
                spawn_call_id=event.call_id,
                prompt=event.prompt,
                nickname=event.nickname,
            )
            continue

        if event.kind == "wait":
            for agent_id in event.targets:
                agent = state.agents.get(agent_id)
                if agent is None:
                    state.violations.append(ProtocolViolation("wait_unknown_agent", agent_id=agent_id))
                    continue
                if agent.closed:
                    state.violations.append(ProtocolViolation("wait_closed_agent", agent_id=agent_id))
                    continue
                result = str(event.results.get(agent_id, "") or "")
                if result.strip():
                    agent.waited = True
                    agent.result = result
                    agent.needs_input = False
                else:
                    agent.waited = False
                    agent.result = ""
                    agent.needs_input = True
            continue

        if event.kind == "close":
            agent_id = event.target or ""
            agent = state.agents.get(agent_id)
            if agent is None:
                state.violations.append(ProtocolViolation("close_unknown_agent", agent_id=agent_id or None))
                continue
            if not agent.waited:
                state.violations.append(ProtocolViolation("close_unwaited_agent", agent_id=agent_id))
                continue
            agent.closed = True
            continue

        if event.kind in {"send_input", "resume"}:
            agent_id = event.target or ""
            agent = state.agents.get(agent_id)
            if agent is None:
                state.violations.append(ProtocolViolation(f"{event.kind}_unknown_agent", agent_id=agent_id or None))
                continue
            agent.waited = False
            agent.result = ""
            agent.needs_input = False
            if event.kind == "resume":
                agent.closed = False
            continue

        state.violations.append(ProtocolViolation("unknown_event_kind", detail=event.kind))
    return state


def protocol_state_from_input_items(input_items: Any) -> ProtocolState:
    return reduce_protocol_events(protocol_events_from_input_items(input_items))


def protocol_events_from_input_items(input_items: Any) -> list[ProtocolEvent]:
    if not isinstance(input_items, list):
        return []
    calls: dict[str, MappingABC[str, Any]] = {}
    text_calls: dict[str, tuple[str, MappingABC[str, Any]]] = {}
    events: list[ProtocolEvent] = []
    for item in input_items:
        if not isinstance(item, MappingABC):
            continue
        if item.get("type") == "message":
            text = _joined_text(item.get("content"))
            previous = _previous_call_from_text(text)
            if previous is not None:
                call_id, tool_name, arguments = previous
                text_calls[call_id] = (tool_name, arguments)
                continue
            event = _event_from_result_text(text, text_calls)
            if event is not None:
                events.append(event)
            continue
        if item.get("type") == "function_call":
            call_id = _string(item.get("call_id"))
            name = _tool_name(item)
            if call_id and name in MULTI_AGENT_TOOL_NAMES:
                calls[call_id] = item
            continue
        if item.get("type") != "function_call_output":
            continue
        call_id = _string(item.get("call_id"))
        if not call_id or call_id not in calls:
            continue
        event = _event_from_call_output(calls[call_id], item)
        if event is not None:
            events.append(event)
    return events


def _previous_call_from_text(text: str) -> tuple[str, str, MappingABC[str, Any]] | None:
    first = _first_nonempty_line(text)
    prefix = "Previous real Codex native multi_agent_v1."
    suffix = " call transcript"
    if not first.startswith(prefix) or not first.endswith(suffix):
        return None
    tool_name = first[len(prefix) : -len(suffix)]
    if tool_name not in MULTI_AGENT_TOOL_NAMES:
        return None
    call_id = _line_value(text, "call_id:")
    if not call_id:
        return None
    return call_id, tool_name, _json_block_after(text, "arguments:")


def _event_from_result_text(
    text: str, previous_calls: MappingABC[str, tuple[str, MappingABC[str, Any]]]
) -> ProtocolEvent | None:
    first = _first_nonempty_line(text)
    prefix = "Codex native multi_agent_v1."
    suffix = " result"
    if not first.startswith(prefix) or not first.endswith(suffix):
        return None
    tool_name = first[len(prefix) : -len(suffix)]
    if tool_name not in MULTI_AGENT_TOOL_NAMES:
        return None
    call_id = _line_value(text, "call_id:") or ""
    previous_tool, arguments = previous_calls.get(call_id, (tool_name, {}))
    if previous_tool != tool_name:
        arguments = {}
    raw_output = _json_block_after(text, "raw_output:")
    if tool_name == "spawn_agent":
        agent_id = _line_value(text, "agent_id:") or _string(raw_output.get("agent_id"))
        if not agent_id:
            return None
        return ProtocolEvent.spawn(
            call_id=call_id,
            agent_id=agent_id,
            prompt=_string(arguments.get("message") or arguments.get("prompt") or arguments.get("input")),
            nickname=_string(arguments.get("nickname")) or None,
        )
    if tool_name == "wait_agent":
        targets = tuple(_target_list(arguments.get("targets") or arguments.get("target")))
        if not targets:
            targets = tuple(_split_agent_ids(_line_value(text, "completed_agent_ids:")))
        return ProtocolEvent.wait(call_id=call_id, targets=targets, results=_wait_results(raw_output))
    if tool_name == "close_agent":
        target = _string(arguments.get("target")) or (_line_value(text, "closed_agent_id:") or "")
        return ProtocolEvent.close(call_id=call_id, target=target)
    if tool_name == "send_input":
        return ProtocolEvent.send_input(
            call_id=call_id,
            target=_string(arguments.get("target")),
            message=_string(arguments.get("message")),
        )
    if tool_name == "resume_agent":
        return ProtocolEvent.resume(
            call_id=call_id,
            target=_string(arguments.get("target")),
            message=_string(arguments.get("message")),
        )
    return None


def _event_from_call_output(
    call_item: MappingABC[str, Any], output_item: MappingABC[str, Any]
) -> ProtocolEvent | None:
    call_id = _string(call_item.get("call_id"))
    name = _tool_name(call_item)
    arguments = _mapping_arguments(call_item.get("arguments"))
    output = _mapping_output(output_item.get("output"))
    if name == "spawn_agent":
        agent_id = _string(output.get("agent_id"))
        if not agent_id:
            return None
        return ProtocolEvent.spawn(
            call_id=call_id,
            agent_id=agent_id,
            prompt=_string(arguments.get("message") or arguments.get("prompt") or arguments.get("input")),
            nickname=_string(arguments.get("nickname")) or None,
        )
    if name == "wait_agent":
        targets = tuple(_target_list(arguments.get("targets") or arguments.get("target")))
        return ProtocolEvent.wait(call_id=call_id, targets=targets, results=_wait_results(output))
    if name == "close_agent":
        return ProtocolEvent.close(call_id=call_id, target=_string(arguments.get("target")))
    if name == "send_input":
        return ProtocolEvent.send_input(
            call_id=call_id,
            target=_string(arguments.get("target")),
            message=_string(arguments.get("message")),
        )
    if name == "resume_agent":
        return ProtocolEvent.resume(
            call_id=call_id,
            target=_string(arguments.get("target")),
            message=_string(arguments.get("message")),
        )
    return None


def _tool_name(item: MappingABC[str, Any]) -> str:
    name = _string(item.get("name"))
    if name in MULTI_AGENT_TOOL_NAMES:
        return name
    if name.startswith("multi_agent_v1__"):
        return name.removeprefix("multi_agent_v1__")
    if name.startswith("multi_agent_v1."):
        return name.removeprefix("multi_agent_v1.")
    return ""


def _mapping_arguments(value: Any) -> MappingABC[str, Any]:
    if isinstance(value, MappingABC):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, MappingABC) else {}
    return {}


def _mapping_output(value: Any) -> MappingABC[str, Any]:
    if isinstance(value, MappingABC):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, MappingABC) else {}
    return {}


def _target_list(value: Any) -> list[str]:
    if isinstance(value, str) and value:
        return [value]
    if isinstance(value, list):
        return [_string(item) for item in value if _string(item)]
    return []


def _wait_results(output: MappingABC[str, Any]) -> dict[str, str]:
    status = output.get("status")
    if not isinstance(status, MappingABC):
        return {}
    results: dict[str, str] = {}
    for agent_id, value in status.items():
        key = _string(agent_id)
        if not key:
            continue
        if isinstance(value, MappingABC):
            if "completed" in value:
                results[key] = _string(value.get("completed"))
            elif value.get("status") == "completed":
                results[key] = _string(value.get("message"))
            else:
                results[key] = ""
        else:
            results[key] = _string(value)
    return results


def _string(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _joined_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, MappingABC):
        return "\n".join(_joined_text(child) for child in value.values())
    if isinstance(value, list):
        return "\n".join(_joined_text(child) for child in value)
    return ""


def _first_nonempty_line(text: str) -> str:
    return next((line.strip() for line in text.splitlines() if line.strip()), "")


def _line_value(text: str, prefix: str) -> str | None:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(prefix):
            value = stripped[len(prefix) :].strip()
            return value or None
    return None


def _json_block_after(text: str, marker: str) -> MappingABC[str, Any]:
    index = text.find(marker)
    if index < 0:
        return {}
    raw = text[index + len(marker) :].strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, MappingABC) else {}


def _split_agent_ids(value: str | None) -> list[str]:
    if not value:
        return []
    return [part for part in re.split(r"[\s,]+", value.strip()) if part]
