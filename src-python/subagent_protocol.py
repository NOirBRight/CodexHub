from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping


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
