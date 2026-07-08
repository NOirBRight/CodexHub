# Generic Subagent Supervision Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split CodexHub subagent supervision into a locked protocol lifecycle layer and a generic workflow scheduler layer so single-agent protocol stability is protected from workflow gate churn.

**Architecture:** Add `subagent_protocol.py` as the protocol event reducer and lifecycle authority, add `subagent_policy.py` for strict/guided/assisted enforcement decisions, then add `subagent_scheduler.py` above protocol state for generic workflow actions. Keep `codex_proxy.py` as the adapter/orchestrator and migrate existing `subagent_state.py` behavior behind compatibility interfaces before deleting or narrowing old logic.

**Tech Stack:** Python 3.13, `unittest`, CodexHub proxy modules in `src-python/`, E2E runner in `diagnostics/subagent-e2e/run_level12_e2e.py`, PowerShell commands on Windows.

## Global Constraints

- Protocol state must not encode implementer, reviewer, task-name, diagnostic sentinel, plan-read, or Level 1/Level 2 final-format semantics.
- Workflow failures must not modify protocol code unless a failing protocol unit or transcript-contract test is added first.
- `strict` mode allows protocol normalization only and no semantic action repair.
- `guided` mode injects guidance but does not repair model output into required tool calls.
- `assisted` mode may enforce or repair only when exactly one legal action exists and every argument is deterministic.
- Gateway must not synthesize final answers or choose between multiple valid workflow actions.
- Generated E2E artifacts under `diagnostics/subagent-e2e/level12-e2e-*` are diagnostics and are not committed by default.

---

## File Structure

- Create `src-python/subagent_protocol.py`
  - Owns protocol event types, agent registry, lifecycle reducer, protocol-safe action calculation, and protocol violation reporting.

- Create `tests/test_subagent_protocol.py`
  - Owns protocol unit and transcript-contract tests. These tests use synthetic Responses-style input items and do not call external models.

- Create `src-python/subagent_policy.py`
  - Owns assist-mode parsing, repair eligibility, and the rule that only one deterministic legal action can be enforced.

- Create `tests/test_subagent_policy.py`
  - Owns strict/guided/assisted policy tests independent of `codex_proxy.py`.

- Modify `src-python/subagent_state.py`
  - Keep existing public names initially, but delegate generic protocol lifecycle facts to `subagent_protocol.py`.
  - Keep workflow-specific compatibility behavior until scheduler migration tasks replace it.

- Modify `src-python/codex_proxy.py`
  - Replace direct single-action assumptions with protocol/policy outputs.
  - Keep request/response adaptation, tool visibility, event logging, and provider compatibility here.

- Create `src-python/subagent_scheduler.py`
  - Owns generic workflow nodes, dependency edges, ready action calculation, dynamic node registration, and scheduler violations.

- Create `tests/test_subagent_scheduler.py`
  - Owns scheduler unit tests using fake protocol state.

- Modify `diagnostics/subagent-e2e/run_level12_e2e.py`
  - Add protocol-lock summary support and failure classification fields without changing generated artifact commit policy.

---

### Task 1: Add Protocol Data Model and Lifecycle Reducer

**Files:**
- Create: `src-python/subagent_protocol.py`
- Create: `tests/test_subagent_protocol.py`

**Interfaces:**
- Produces: `ProtocolEvent`, `AgentRecord`, `ProtocolState`, `reduce_protocol_events(events: Iterable[ProtocolEvent]) -> ProtocolState`
- Produces: `ProtocolState.open_agent_ids`, `waitable_agent_ids`, `closeable_agent_ids`, `closed_agent_ids`, `lifecycle_complete`
- Consumes: no project-specific workflow code

- [ ] **Step 1: Write failing protocol reducer tests**

Add `tests/test_subagent_protocol.py`:

```python
import unittest

from subagent_protocol import ProtocolEvent, reduce_protocol_events


class SubagentProtocolTests(unittest.TestCase):
    def test_single_agent_lifecycle_reaches_closed(self):
        state = reduce_protocol_events(
            [
                ProtocolEvent.spawn(call_id="call_spawn", agent_id="agent-1", prompt="return ok", nickname="child"),
                ProtocolEvent.wait(call_id="call_wait", targets=("agent-1",), results={"agent-1": "ok"}),
                ProtocolEvent.close(call_id="call_close", target="agent-1"),
            ]
        )

        self.assertEqual(state.open_agent_ids, [])
        self.assertEqual(state.waitable_agent_ids, [])
        self.assertEqual(state.closeable_agent_ids, [])
        self.assertEqual(state.closed_agent_ids, ["agent-1"])
        self.assertTrue(state.lifecycle_complete)
        self.assertFalse(state.violations)

    def test_empty_wait_result_requires_input_before_close(self):
        state = reduce_protocol_events(
            [
                ProtocolEvent.spawn(call_id="call_spawn", agent_id="agent-1", prompt="return exact line", nickname=None),
                ProtocolEvent.wait(call_id="call_wait", targets=("agent-1",), results={"agent-1": ""}),
            ]
        )

        self.assertEqual(state.needs_input_agent_ids, ["agent-1"])
        self.assertEqual(state.waitable_agent_ids, [])
        self.assertEqual(state.closeable_agent_ids, [])
        self.assertFalse(state.lifecycle_complete)

    def test_send_input_reopens_empty_wait_agent_for_wait(self):
        state = reduce_protocol_events(
            [
                ProtocolEvent.spawn(call_id="call_spawn", agent_id="agent-1", prompt="return exact line", nickname=None),
                ProtocolEvent.wait(call_id="call_wait", targets=("agent-1",), results={"agent-1": ""}),
                ProtocolEvent.send_input(call_id="call_send", target="agent-1", message="Return the exact requested output."),
            ]
        )

        self.assertEqual(state.needs_input_agent_ids, [])
        self.assertEqual(state.waitable_agent_ids, ["agent-1"])
        self.assertEqual(state.closeable_agent_ids, [])

    def test_close_before_successful_wait_is_violation(self):
        state = reduce_protocol_events(
            [
                ProtocolEvent.spawn(call_id="call_spawn", agent_id="agent-1", prompt="return ok", nickname=None),
                ProtocolEvent.close(call_id="call_close", target="agent-1"),
            ]
        )

        self.assertEqual([violation.code for violation in state.violations], ["close_unwaited_agent"])
        self.assertEqual(state.open_agent_ids, ["agent-1"])
        self.assertEqual(state.closed_agent_ids, [])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest tests.test_subagent_protocol -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'subagent_protocol'`.

- [ ] **Step 3: Implement protocol dataclasses and reducer**

Create `src-python/subagent_protocol.py`:

```python
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
    nickname: str | None
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
```

- [ ] **Step 4: Run tests and verify they pass**

Run:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest tests.test_subagent_protocol -v
```

Expected: `4 tests OK`.

- [ ] **Step 5: Commit**

```powershell
git add src-python/subagent_protocol.py tests/test_subagent_protocol.py
git commit -m "feat: add subagent protocol lifecycle reducer"
```

---

### Task 2: Add Protocol Transcript Contract Parser

**Files:**
- Modify: `src-python/subagent_protocol.py`
- Modify: `tests/test_subagent_protocol.py`
- Read-only reference: `src-python/subagent_state.py`

**Interfaces:**
- Consumes: `ProtocolEvent`, `reduce_protocol_events`
- Produces: `protocol_events_from_input_items(input_items: object) -> list[ProtocolEvent]`
- Produces: `protocol_state_from_input_items(input_items: object) -> ProtocolState`

- [ ] **Step 1: Add failing transcript-contract tests**

Append to `tests/test_subagent_protocol.py`:

```python
import json


def message(content):
    return {"type": "message", "role": "user", "content": content}


def call(call_id, name, arguments):
    return {
        "type": "function_call",
        "call_id": call_id,
        "namespace": "multi_agent_v1",
        "name": name,
        "arguments": arguments,
    }


def output(call_id, value):
    return {"type": "function_call_output", "call_id": call_id, "output": json.dumps(value)}
```

Add methods to `SubagentProtocolTests`:

```python
    def test_protocol_state_from_responses_structured_items(self):
        from subagent_protocol import protocol_state_from_input_items

        state = protocol_state_from_input_items(
            [
                message("Run one child."),
                call("call_spawn", "spawn_agent", {"message": "return ok", "nickname": "child"}),
                output("call_spawn", {"agent_id": "agent-1", "nickname": "child"}),
                call("call_wait", "wait_agent", {"targets": ["agent-1"], "timeout_ms": 60000}),
                output("call_wait", {"timed_out": False, "status": {"agent-1": {"completed": "ok"}}}),
                call("call_close", "close_agent", {"target": "agent-1"}),
                output("call_close", {"previous_status": {"completed": "ok"}}),
            ]
        )

        self.assertTrue(state.lifecycle_complete)
        self.assertEqual(state.closed_agent_ids, ["agent-1"])

    def test_protocol_parser_accepts_actual_status_message_shape(self):
        from subagent_protocol import protocol_state_from_input_items

        state = protocol_state_from_input_items(
            [
                call("call_spawn", "spawn_agent", {"message": "return ok"}),
                output("call_spawn", {"agent_id": "agent-1"}),
                call("call_wait", "wait_agent", {"targets": ["agent-1"], "timeout_ms": 60000}),
                output("call_wait", {"timed_out": False, "status": {"agent-1": {"status": "completed", "message": "ok"}}}),
            ]
        )

        self.assertEqual(state.waitable_agent_ids, [])
        self.assertEqual(state.closeable_agent_ids, ["agent-1"])
        self.assertEqual(state.agents["agent-1"].result, "ok")
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest tests.test_subagent_protocol -v
```

Expected: FAIL with `ImportError` for `protocol_state_from_input_items`.

- [ ] **Step 3: Add parser functions**

In `src-python/subagent_protocol.py`, add these functions after `reduce_protocol_events`:

```python
import json
from collections.abc import Mapping as MappingABC
from typing import Any


MULTI_AGENT_TOOL_NAMES = {"spawn_agent", "wait_agent", "close_agent", "resume_agent", "send_input"}


def protocol_state_from_input_items(input_items: Any) -> ProtocolState:
    return reduce_protocol_events(protocol_events_from_input_items(input_items))


def protocol_events_from_input_items(input_items: Any) -> list[ProtocolEvent]:
    if not isinstance(input_items, list):
        return []
    calls: dict[str, MappingABC[str, Any]] = {}
    events: list[ProtocolEvent] = []
    for item in input_items:
        if not isinstance(item, MappingABC):
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
        call_item = calls[call_id]
        event = _event_from_call_output(call_item, item)
        if event is not None:
            events.append(event)
    return events


def _event_from_call_output(call_item: MappingABC[str, Any], output_item: MappingABC[str, Any]) -> ProtocolEvent | None:
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
```

- [ ] **Step 4: Run protocol tests**

Run:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest tests.test_subagent_protocol -v
```

Expected: all protocol tests pass.

- [ ] **Step 5: Commit**

```powershell
git add src-python/subagent_protocol.py tests/test_subagent_protocol.py
git commit -m "feat: parse transcript into subagent protocol state"
```

---

### Task 3: Add Policy Layer for Action Enforcement

**Files:**
- Create: `src-python/subagent_policy.py`
- Create: `tests/test_subagent_policy.py`

**Interfaces:**
- Consumes: legal action dictionaries with `kind`, `tool_name`, and `arguments`
- Produces: `subagent_assist_mode() -> str`
- Produces: `guidance_enabled(context: Mapping[str, object] | None) -> bool`
- Produces: `semantic_repair_enabled(context: Mapping[str, object] | None) -> bool`
- Produces: `deterministic_required_action(actions: Sequence[Mapping[str, object]]) -> Mapping[str, object] | None`

- [ ] **Step 1: Write failing policy tests**

Create `tests/test_subagent_policy.py`:

```python
import os
import unittest
from unittest.mock import patch

from subagent_policy import (
    deterministic_required_action,
    guidance_enabled,
    semantic_repair_enabled,
    subagent_assist_mode,
)


class SubagentPolicyTests(unittest.TestCase):
    def test_assist_mode_defaults_to_assisted(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(subagent_assist_mode(), "assisted")

    def test_guided_has_guidance_without_repair(self):
        with patch.dict(os.environ, {"CODEXHUB_SUBAGENT_ASSIST_MODE": "guided"}, clear=False):
            self.assertTrue(guidance_enabled({}))
            self.assertFalse(semantic_repair_enabled({}))

    def test_raw_probe_disables_guidance_and_repair(self):
        with patch.dict(os.environ, {"CODEXHUB_SUBAGENT_ASSIST_MODE": "assisted"}, clear=False):
            self.assertFalse(guidance_enabled({"raw_provider_probe": True}))
            self.assertFalse(semantic_repair_enabled({"raw_provider_probe": True}))

    def test_deterministic_required_action_returns_single_known_action(self):
        action = {"kind": "protocol", "tool_name": "wait_agent", "arguments": {"targets": ["agent-1"]}}
        self.assertEqual(deterministic_required_action([action]), action)

    def test_deterministic_required_action_refuses_multiple_valid_actions(self):
        actions = [
            {"kind": "workflow", "tool_name": "spawn_agent", "arguments": {"message": "task B"}},
            {"kind": "workflow", "tool_name": "spawn_agent", "arguments": {"message": "review A"}},
        ]
        self.assertIsNone(deterministic_required_action(actions))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest tests.test_subagent_policy -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'subagent_policy'`.

- [ ] **Step 3: Implement policy module**

Create `src-python/subagent_policy.py`:

```python
from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from typing import Any


ASSIST_MODES = {"strict", "guided", "assisted"}


def subagent_assist_mode() -> str:
    raw = os.environ.get("CODEXHUB_SUBAGENT_ASSIST_MODE", "assisted")
    value = raw.strip().lower() if isinstance(raw, str) else "assisted"
    return value if value in ASSIST_MODES else "assisted"


def guidance_enabled(context: Mapping[str, Any] | None) -> bool:
    if _raw_provider_probe(context):
        return False
    return subagent_assist_mode() in {"guided", "assisted"}


def semantic_repair_enabled(context: Mapping[str, Any] | None) -> bool:
    if _raw_provider_probe(context):
        return False
    return subagent_assist_mode() == "assisted"


def deterministic_required_action(actions: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    deterministic = [action for action in actions if _has_known_tool_and_arguments(action)]
    if len(deterministic) != 1:
        return None
    return deterministic[0]


def _has_known_tool_and_arguments(action: Mapping[str, Any]) -> bool:
    tool_name = action.get("tool_name")
    arguments = action.get("arguments")
    return isinstance(tool_name, str) and bool(tool_name) and isinstance(arguments, Mapping)


def _raw_provider_probe(context: Mapping[str, Any] | None) -> bool:
    return bool(context and context.get("raw_provider_probe"))
```

- [ ] **Step 4: Run policy tests**

Run:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest tests.test_subagent_policy -v
```

Expected: all policy tests pass.

- [ ] **Step 5: Commit**

```powershell
git add src-python/subagent_policy.py tests/test_subagent_policy.py
git commit -m "feat: add subagent supervision policy layer"
```

---

### Task 4: Delegate Generic Lifecycle Facts from Existing State to Protocol

**Files:**
- Modify: `src-python/subagent_state.py`
- Modify: `tests/test_subagent_state.py`
- Test: `tests/test_subagent_protocol.py`

**Interfaces:**
- Consumes: `protocol_state_from_input_items(input_items) -> ProtocolState`
- Preserves: `build_subagent_state(input_items) -> SubagentState`
- Preserves: `SubagentState.spawned_agent_ids`, `open_agent_ids`, `wait_agent_ids`, `close_agent_ids`, `closed_agent_ids`, `lifecycle_complete`

- [ ] **Step 1: Add regression test proving generic lifecycle mirrors protocol**

Append to `tests/test_subagent_state.py`:

```python
    def test_generic_lifecycle_facts_match_protocol_state(self):
        from subagent_protocol import protocol_state_from_input_items

        items = [
            message("Run exactly one subagent lifecycle: spawn_agent, wait_agent, close_agent."),
            *spawn("call_spawn", "agent-1", "return ok", "child"),
            *wait("call_wait", ["agent-1"], "ok"),
            *close("call_close", "agent-1"),
        ]

        old_state = build_subagent_state(items)
        protocol_state = protocol_state_from_input_items(items)

        self.assertEqual(old_state.open_agent_ids, protocol_state.open_agent_ids)
        self.assertEqual(old_state.wait_agent_ids, protocol_state.waitable_agent_ids)
        self.assertEqual(old_state.close_agent_ids, protocol_state.closeable_agent_ids)
        self.assertEqual(old_state.closed_agent_ids, protocol_state.closed_agent_ids)
        self.assertEqual(old_state.lifecycle_complete, protocol_state.lifecycle_complete)
```

- [ ] **Step 2: Run targeted test**

Run:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest tests.test_subagent_state.SubagentStateTests.test_generic_lifecycle_facts_match_protocol_state -v
```

Expected: PASS before refactor. This locks observable compatibility.

- [ ] **Step 3: Add protocol state field to `SubagentState`**

In `src-python/subagent_state.py`, import protocol:

```python
from subagent_protocol import ProtocolState, protocol_state_from_input_items
```

Add field:

```python
    protocol_state: ProtocolState | None = None
```

In `build_subagent_state`, compute protocol state before old event application:

```python
    protocol_state = protocol_state_from_input_items(input_items)
```

Pass it into `SubagentState(...)`:

```python
        protocol_state=protocol_state,
```

- [ ] **Step 4: Make generic property access prefer protocol state**

Update these `SubagentState` properties:

```python
    @property
    def closed_agent_ids(self) -> list[str]:
        if self.protocol_state is not None and not self.workflow_intent:
            return self.protocol_state.closed_agent_ids
        return [agent.agent_id for agent in self.agents.values() if agent.closed]

    @property
    def open_agent_ids(self) -> list[str]:
        if self.protocol_state is not None and not self.workflow_intent:
            return self.protocol_state.open_agent_ids
        return [agent.agent_id for agent in self.agents.values() if not agent.closed]

    @property
    def wait_agent_ids(self) -> list[str]:
        if self.protocol_state is not None and not self.workflow_intent:
            return self.protocol_state.waitable_agent_ids
        return [agent.agent_id for agent in self.agents.values() if not agent.closed and not agent.waited]

    @property
    def close_agent_ids(self) -> list[str]:
        if self.protocol_state is not None and not self.workflow_intent:
            return self.protocol_state.closeable_agent_ids if self.close_waited_agents else []
        if not self.close_waited_agents:
            return []
        return [agent.agent_id for agent in self.agents.values() if not agent.closed and agent.waited]
```

This keeps workflow compatibility on old logic while generic lifecycle starts reading protocol truth.

- [ ] **Step 5: Run focused regression**

Run:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest tests.test_subagent_protocol tests.test_subagent_state -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```powershell
git add src-python/subagent_state.py tests/test_subagent_state.py
git commit -m "refactor: delegate generic subagent lifecycle to protocol state"
```

---

### Task 5: Route Required Protocol Actions Through Policy

**Files:**
- Modify: `src-python/codex_proxy.py`
- Modify: `tests/test_routing.py`
- Test: `tests/test_subagent_policy.py`

**Interfaces:**
- Consumes: `subagent_policy.semantic_repair_enabled`
- Consumes: `subagent_policy.deterministic_required_action`
- Produces: `event_context["subagent_legal_actions"]` for logging and repair decisions

- [ ] **Step 1: Add routing test for multi-action non-coercion**

Add to `tests/test_routing.py` near required subagent repair tests:

```python
    def test_assisted_mode_does_not_repair_when_multiple_legal_actions_exist(self):
        body = json.dumps(
            {
                "id": "resp_text",
                "object": "response",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "I will choose the next branch."}],
                    }
                ],
            }
        ).encode("utf-8")
        context = {
            "tool_protocol": "responses_structured",
            "subagent_lifecycle_complete": False,
            "subagent_legal_actions": [
                {"tool_name": "spawn_agent", "arguments": {"message": "task B"}},
                {"tool_name": "spawn_agent", "arguments": {"message": "review task A"}},
            ],
        }

        with patch.dict(os.environ, {"CODEXHUB_SUBAGENT_ASSIST_MODE": "assisted"}, clear=False):
            transformed = compatible_response_body(body, "ollama_cloud", event_context=context)

        payload = json.loads(transformed)
        self.assertEqual(payload["output"][0]["type"], "message")
        self.assertNotIn("function_call", json.dumps(payload))
```

- [ ] **Step 2: Run targeted test and verify current failure**

Run:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest tests.test_routing.RoutingTests.test_assisted_mode_does_not_repair_when_multiple_legal_actions_exist -v
```

Expected: PASS if old required fields are absent, or FAIL if current repair path ignores `subagent_legal_actions`. Continue either way; this test defines the new contract.

- [ ] **Step 3: Use policy in `_required_subagent_call_spec`**

In `src-python/codex_proxy.py`, import:

```python
from subagent_policy import deterministic_required_action
```

At the start of `_required_subagent_call_spec`, after protocol checks and before legacy fields:

```python
    legal_actions = context.get("subagent_legal_actions")
    if isinstance(legal_actions, list):
        action = deterministic_required_action([item for item in legal_actions if isinstance(item, Mapping)])
        if action is None:
            return None
        tool_name = action.get("tool_name")
        arguments = action.get("arguments")
        if isinstance(tool_name, str) and isinstance(arguments, Mapping):
            agent_ids = action.get("agent_ids")
            return {
                "tool_name": tool_name,
                "agent_ids": _string_list(agent_ids) if isinstance(agent_ids, list) else [],
                "arguments": dict(arguments),
            }
```

Keep existing legacy `wait_agent_ids` and `close_agent_ids` branches after this block for compatibility.

- [ ] **Step 4: Run required repair regression**

Run:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest tests.test_subagent_policy tests.test_routing.RoutingTests.test_assisted_mode_does_not_repair_when_multiple_legal_actions_exist tests.test_routing.RoutingTests.test_strict_mode_does_not_repair_missing_required_close_body tests.test_routing.RoutingTests.test_assisted_mode_repairs_missing_required_close_body -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```powershell
git add src-python/codex_proxy.py tests/test_routing.py
git commit -m "refactor: route subagent required actions through policy"
```

---

### Task 6: Establish P0 Protocol Lock Gate

**Files:**
- Modify: `tests/test_subagent_protocol.py`
- Modify: `diagnostics/subagent-e2e/run_level12_e2e.py`
- Modify: `docs/superpowers/specs/2026-07-07-generic-subagent-supervision-design.md`

**Interfaces:**
- Produces: protocol-lock unit command
- Produces: runner summary fields `failure_classification` and `protocol_lock_relevant`

- [ ] **Step 1: Add protocol contract coverage for invalid transitions**

Add these tests to `tests/test_subagent_protocol.py`:

```python
    def test_wait_unknown_agent_is_protocol_defect_signal(self):
        state = reduce_protocol_events(
            [ProtocolEvent.wait(call_id="call_wait", targets=("missing",), results={"missing": "ok"})]
        )

        self.assertEqual([violation.code for violation in state.violations], ["wait_unknown_agent"])

    def test_close_unknown_agent_is_protocol_defect_signal(self):
        state = reduce_protocol_events([ProtocolEvent.close(call_id="call_close", target="missing")])

        self.assertEqual([violation.code for violation in state.violations], ["close_unknown_agent"])

    def test_wait_closed_agent_is_protocol_defect_signal(self):
        state = reduce_protocol_events(
            [
                ProtocolEvent.spawn(call_id="call_spawn", agent_id="agent-1", prompt="return ok"),
                ProtocolEvent.wait(call_id="call_wait", targets=("agent-1",), results={"agent-1": "ok"}),
                ProtocolEvent.close(call_id="call_close", target="agent-1"),
                ProtocolEvent.wait(call_id="call_wait_again", targets=("agent-1",), results={"agent-1": "ok"}),
            ]
        )

        self.assertEqual([violation.code for violation in state.violations], ["wait_closed_agent"])
```

- [ ] **Step 2: Run P0 command**

Run:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest tests.test_subagent_protocol tests.test_subagent_policy -v
```

Expected: all tests pass.

- [ ] **Step 3: Add runner classification helper**

In `diagnostics/subagent-e2e/run_level12_e2e.py`, add near analysis helpers:

```python
def classify_failure(summary: dict[str, Any]) -> str:
    if summary.get("pass"):
        return "none"
    checks = summary.get("checks") if isinstance(summary.get("checks"), dict) else {}
    if not checks.get("exit_code_zero", True) or summary.get("timed_out"):
        if summary.get("upstream_stream_error") or summary.get("upstream_stream_idle_timeout") or summary.get("cli_stream_reconnect"):
            return "provider_stream_flake"
        return "timeout"
    if summary.get("native_router_error"):
        return "adapter_defect"
    if not checks.get("completed_spawn_count", True):
        return "model_choice"
    if not checks.get("wait_covers_agents", True) or not checks.get("close_covers_agents", True):
        return "protocol_or_policy_defect"
    if not checks.get("sentinels_seen", True):
        return "scheduler_or_model_prompt_defect"
    if not checks.get("final_exact", True) or not checks.get("artifact_exact", True):
        return "workflow_output_defect"
    return "unclassified"
```

After each `analyze_level1` and `analyze_level2` summary is produced, set:

```python
        summary["failure_classification"] = classify_failure(summary)
        summary["protocol_lock_relevant"] = task.get("scenario") == "single" and summary["failure_classification"] in {
            "none",
            "protocol_or_policy_defect",
            "adapter_defect",
        }
```

- [ ] **Step 4: Run runner syntax check**

Run:

```powershell
$env:PYTHONPATH='src-python'; python -m py_compile diagnostics/subagent-e2e/run_level12_e2e.py
```

Expected: no output and exit code 0.

- [ ] **Step 5: Commit**

```powershell
git add tests/test_subagent_protocol.py diagnostics/subagent-e2e/run_level12_e2e.py docs/superpowers/specs/2026-07-07-generic-subagent-supervision-design.md
git commit -m "test: establish subagent protocol lock gate"
```

---

### Task 7: Run and Record P1 Single-Agent Stability Gate

**Files:**
- No source changes expected unless P1 finds a protocol or adapter defect.
- E2E artifacts: `diagnostics/subagent-e2e/level12-e2e-*`

**Interfaces:**
- Consumes: P0 protocol tests from Task 6
- Produces: P1 stability evidence in generated `summary.md` and `summary.json`

- [ ] **Step 1: Verify upstream availability**

Run:

```powershell
Invoke-WebRequest -Uri 'http://127.0.0.1:9099/v1/models' -UseBasicParsing -TimeoutSec 10
```

Expected: HTTP 200 response.

- [ ] **Step 2: Run P0 immediately before P1**

Run:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest tests.test_subagent_protocol tests.test_subagent_policy tests.test_subagent_state tests.test_routing -v
```

Expected: all tests pass.

- [ ] **Step 3: Run P1 single-agent real E2E**

Run:

```powershell
$env:PYTHONPATH='src-python'; python diagnostics\subagent-e2e\run_level12_e2e.py --level level1 --models glm52,k2_7,m3 --endpoints responses,chat --scenarios single --level1-timeout 420 --jobs 3 --repeat 20 --subagent-mode assisted --main-retry-attempts 1 --upstream-base-url http://127.0.0.1:9099/v1
```

Expected:

```text
120 total rows
at least 114 pass
native_router_error=0 for every protocol-lock-relevant row
no unclassified protocol-lock-relevant failures
```

- [ ] **Step 4: Classify failures before editing code**

If P1 has failures, inspect the latest generated `summary.json`.

Allowed protocol edits require one of:

```text
failure_classification=protocol_or_policy_defect
failure_classification=adapter_defect with a reproducible protocol transcript test
```

Do not edit `src-python/subagent_protocol.py` for:

```text
provider_stream_flake
model_choice
scheduler_or_model_prompt_defect
workflow_output_defect
```

- [ ] **Step 5: Commit only source/test fixes if P1 required them**

If source or tests changed:

```powershell
git add src-python/subagent_protocol.py src-python/subagent_policy.py src-python/subagent_state.py src-python/codex_proxy.py tests/test_subagent_protocol.py tests/test_subagent_policy.py tests/test_subagent_state.py tests/test_routing.py diagnostics/subagent-e2e/run_level12_e2e.py
git commit -m "fix: lock single-agent subagent protocol gate"
```

Do not add generated E2E run directories.

---

### Task 8: Add Generic Scheduler Action Model

**Files:**
- Create: `src-python/subagent_scheduler.py`
- Create: `tests/test_subagent_scheduler.py`

**Interfaces:**
- Consumes: `ProtocolState`
- Produces: `WorkflowNode`, `WorkflowState`, `WorkflowAction`, `compute_allowed_actions(workflow: WorkflowState, protocol: ProtocolState) -> list[WorkflowAction]`

- [ ] **Step 1: Write failing scheduler tests**

Create `tests/test_subagent_scheduler.py`:

```python
import unittest

from subagent_protocol import ProtocolEvent, reduce_protocol_events
from subagent_scheduler import WorkflowNode, WorkflowState, compute_allowed_actions


class SubagentSchedulerTests(unittest.TestCase):
    def test_parallel_ready_nodes_return_multiple_spawn_actions(self):
        workflow = WorkflowState(
            nodes={
                "task-a": WorkflowNode(node_id="task-a", prompt="do A"),
                "task-b": WorkflowNode(node_id="task-b", prompt="do B"),
            }
        )
        protocol = reduce_protocol_events([])

        actions = compute_allowed_actions(workflow, protocol)

        self.assertEqual([action.tool_name for action in actions], ["spawn_agent", "spawn_agent"])
        self.assertEqual([action.arguments["message"] for action in actions], ["do A", "do B"])

    def test_dependent_node_waits_for_dependency_completion(self):
        workflow = WorkflowState(
            nodes={
                "task-a": WorkflowNode(node_id="task-a", prompt="do A", assigned_agent_id="agent-a"),
                "task-b": WorkflowNode(node_id="task-b", prompt="do B", dependencies=("task-a",)),
            }
        )
        protocol = reduce_protocol_events([ProtocolEvent.spawn("call_spawn", "agent-a", "do A")])

        actions = compute_allowed_actions(workflow, protocol)

        self.assertEqual([action.tool_name for action in actions], ["wait_agent"])
        self.assertEqual(actions[0].arguments, {"targets": ["agent-a"], "timeout_ms": 60000})

    def test_completed_dependency_releases_next_spawn(self):
        workflow = WorkflowState(
            nodes={
                "task-a": WorkflowNode(node_id="task-a", prompt="do A", assigned_agent_id="agent-a"),
                "task-b": WorkflowNode(node_id="task-b", prompt="do B", dependencies=("task-a",)),
            }
        )
        protocol = reduce_protocol_events(
            [
                ProtocolEvent.spawn("call_spawn", "agent-a", "do A"),
                ProtocolEvent.wait("call_wait", ("agent-a",), {"agent-a": "done"}),
                ProtocolEvent.close("call_close", "agent-a"),
            ]
        )

        actions = compute_allowed_actions(workflow, protocol)

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].tool_name, "spawn_agent")
        self.assertEqual(actions[0].arguments["message"], "do B")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest tests.test_subagent_scheduler -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'subagent_scheduler'`.

- [ ] **Step 3: Implement scheduler model**

Create `src-python/subagent_scheduler.py`:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from subagent_protocol import ProtocolState


@dataclass(frozen=True)
class WorkflowNode:
    node_id: str
    prompt: str
    dependencies: tuple[str, ...] = ()
    assigned_agent_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass
class WorkflowState:
    nodes: dict[str, WorkflowNode] = field(default_factory=dict)


@dataclass(frozen=True)
class WorkflowAction:
    kind: str
    tool_name: str
    arguments: Mapping[str, Any]
    node_id: str | None = None
    agent_ids: tuple[str, ...] = ()


def compute_allowed_actions(workflow: WorkflowState, protocol: ProtocolState) -> list[WorkflowAction]:
    protocol_actions = _protocol_actions(protocol)
    if protocol_actions:
        return protocol_actions
    actions: list[WorkflowAction] = []
    for node_id in sorted(workflow.nodes):
        node = workflow.nodes[node_id]
        if node.assigned_agent_id:
            continue
        if all(_node_complete(workflow.nodes[dep], protocol) for dep in node.dependencies):
            actions.append(
                WorkflowAction(
                    kind="workflow",
                    tool_name="spawn_agent",
                    node_id=node.node_id,
                    arguments={"message": node.prompt, "fork_context": False},
                )
            )
    return actions


def _protocol_actions(protocol: ProtocolState) -> list[WorkflowAction]:
    if protocol.needs_input_agent_ids:
        agent_id = protocol.needs_input_agent_ids[0]
        agent = protocol.agents[agent_id]
        return [
            WorkflowAction(
                kind="protocol",
                tool_name="send_input",
                agent_ids=(agent_id,),
                arguments={
                    "target": agent_id,
                    "message": f"Return exactly the output requested in your original prompt, with no prose or markdown.\nOriginal prompt:\n{agent.prompt}",
                },
            )
        ]
    if protocol.waitable_agent_ids:
        return [
            WorkflowAction(
                kind="protocol",
                tool_name="wait_agent",
                agent_ids=tuple(protocol.waitable_agent_ids),
                arguments={"targets": protocol.waitable_agent_ids, "timeout_ms": 60000},
            )
        ]
    if protocol.closeable_agent_ids:
        agent_id = protocol.closeable_agent_ids[0]
        return [
            WorkflowAction(
                kind="protocol",
                tool_name="close_agent",
                agent_ids=(agent_id,),
                arguments={"target": agent_id},
            )
        ]
    return []


def _node_complete(node: WorkflowNode, protocol: ProtocolState) -> bool:
    return bool(node.assigned_agent_id and node.assigned_agent_id in protocol.closed_agent_ids)
```

- [ ] **Step 4: Run scheduler tests**

Run:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest tests.test_subagent_scheduler -v
```

Expected: all scheduler tests pass.

- [ ] **Step 5: Commit**

```powershell
git add src-python/subagent_scheduler.py tests/test_subagent_scheduler.py
git commit -m "feat: add generic subagent workflow scheduler"
```

---

### Task 9: Move Bounded Prompt Queues into Scheduler Fixtures

**Files:**
- Modify: `src-python/subagent_scheduler.py`
- Modify: `src-python/codex_proxy.py`
- Modify: `tests/test_subagent_scheduler.py`
- Modify: `tests/test_routing.py`

**Interfaces:**
- Consumes: `WorkflowState`, `WorkflowNode`, `compute_allowed_actions`
- Produces: `bounded_workflow_from_exact_prompts(prompts: list[str], assigned_agent_ids: list[str]) -> WorkflowState`

- [ ] **Step 1: Add scheduler test for ordered exact prompts**

Add to `tests/test_subagent_scheduler.py`:

```python
    def test_bounded_exact_prompt_queue_releases_second_prompt_after_first_spawn(self):
        from subagent_scheduler import bounded_workflow_from_exact_prompts

        workflow = bounded_workflow_from_exact_prompts(
            prompts=["Return A", "Return B"],
            assigned_agent_ids=["agent-a"],
        )
        protocol = reduce_protocol_events([ProtocolEvent.spawn("call_spawn_a", "agent-a", "Return A")])

        actions = compute_allowed_actions(workflow, protocol)

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].tool_name, "spawn_agent")
        self.assertEqual(actions[0].arguments["message"], "Return B")
```

- [ ] **Step 2: Run scheduler test and verify it fails**

Run:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest tests.test_subagent_scheduler.SubagentSchedulerTests.test_bounded_exact_prompt_queue_releases_second_prompt_after_first_spawn -v
```

Expected: FAIL with `ImportError` for `bounded_workflow_from_exact_prompts`.

- [ ] **Step 3: Implement bounded workflow fixture helper**

In `src-python/subagent_scheduler.py`, add:

```python
def bounded_workflow_from_exact_prompts(prompts: list[str], assigned_agent_ids: list[str] | None = None) -> WorkflowState:
    assigned = assigned_agent_ids or []
    nodes: dict[str, WorkflowNode] = {}
    previous_node_id: str | None = None
    for index, prompt in enumerate(prompts):
        node_id = f"bounded-{index + 1}"
        assigned_agent_id = assigned[index] if index < len(assigned) else None
        dependencies = (previous_node_id,) if previous_node_id and index >= len(assigned) else ()
        nodes[node_id] = WorkflowNode(
            node_id=node_id,
            prompt=prompt,
            dependencies=dependencies,
            assigned_agent_id=assigned_agent_id,
            metadata={"source": "bounded_exact_prompt"},
        )
        previous_node_id = node_id
    return WorkflowState(nodes=nodes)
```

This helper is for diagnostic fixtures and bounded product tests. It does not belong in protocol.

- [ ] **Step 4: Add routing regression for second exact prompt**

Add to `tests/test_routing.py`:

```python
    def test_bounded_two_prompt_scheduler_uses_second_prompt_after_first_spawn(self):
        prompt = (
            "Spawn child A with prompt exactly this complete string: `Return A`\n"
            "Spawn child B with prompt exactly this complete string: `Return B`\n"
        )
        body = json.dumps(
            {
                "model": "ollama-e2e-responses/minimax-m3",
                "input": [
                    {"type": "message", "role": "user", "content": prompt},
                    {
                        "type": "function_call",
                        "call_id": "call_spawn_a",
                        "namespace": "multi_agent_v1",
                        "name": "spawn_agent",
                        "arguments": json.dumps({"message": "Return A", "fork_context": False}),
                    },
                    {
                        "type": "function_call_output",
                        "call_id": "call_spawn_a",
                        "output": json.dumps({"agent_id": "agent-a"}),
                    },
                ],
                "tools": [{"type": "function", "name": "multi_agent_v1__spawn_agent", "parameters": {"type": "object"}}],
            }
        ).encode("utf-8")
        event_context = {"request_id": "req"}

        with patch.dict(os.environ, {"CODEXHUB_SUBAGENT_ASSIST_MODE": "assisted"}, clear=False):
            transformed = compatible_request_body(body, "ollama_cloud", event_context=event_context)

        self.assertIn("subagent_legal_actions", event_context)
        self.assertEqual(event_context["subagent_legal_actions"][0]["arguments"]["message"], "Return B")
        self.assertIn("Return B", transformed.decode("utf-8"))
```

- [ ] **Step 5: Wire bounded exact prompts into request context**

In `src-python/codex_proxy.py`, when exact child prompts are extracted and protocol state exists:

```python
from subagent_scheduler import bounded_workflow_from_exact_prompts, compute_allowed_actions
```

Replace direct `subagent_required_spawn_arguments` derivation for exact prompts with:

```python
            exact_prompts = _exact_child_prompts_from_request_text(_active_user_request_text(input_items))
            if exact_prompts and subagent_state is not None and subagent_state.protocol_state is not None:
                workflow = bounded_workflow_from_exact_prompts(
                    exact_prompts,
                    assigned_agent_ids=list(subagent_state.protocol_state.agents.keys()),
                )
                legal_actions = compute_allowed_actions(workflow, subagent_state.protocol_state)
                event_context["subagent_legal_actions"] = [
                    {
                        "kind": action.kind,
                        "tool_name": action.tool_name,
                        "arguments": dict(action.arguments),
                        "agent_ids": list(action.agent_ids),
                        "node_id": action.node_id,
                    }
                    for action in legal_actions
                ]
```

Do not delete legacy required-spawn fallback until the new test passes.

- [ ] **Step 6: Run focused tests**

Run:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest tests.test_subagent_scheduler tests.test_routing.RoutingTests.test_bounded_two_prompt_scheduler_uses_second_prompt_after_first_spawn -v
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```powershell
git add src-python/subagent_scheduler.py src-python/codex_proxy.py tests/test_subagent_scheduler.py tests/test_routing.py
git commit -m "fix: move bounded prompt ordering into scheduler"
```

---

### Task 10: Migrate Workflow Role Sequencing into Scheduler Adapter

**Files:**
- Modify: `src-python/subagent_scheduler.py`
- Modify: `src-python/subagent_state.py`
- Modify: `tests/test_subagent_scheduler.py`
- Modify: `tests/test_subagent_state.py`

**Interfaces:**
- Produces: `workflow_from_role_sequence(tasks: list[str], roles: list[str], assigned: Mapping[str, str]) -> WorkflowState`
- Preserves: existing public workflow behavior in `build_subagent_state`

- [ ] **Step 1: Add scheduler test for implementer then reviewers**

Add to `tests/test_subagent_scheduler.py`:

```python
    def test_role_sequence_releases_spec_reviewer_after_implementer_closed(self):
        from subagent_scheduler import workflow_from_role_sequence

        workflow = workflow_from_role_sequence(
            tasks=["task-1"],
            roles=["implementer", "spec_reviewer", "code_quality_reviewer"],
            assigned={"task-1:implementer": "impl-1"},
        )
        protocol = reduce_protocol_events(
            [
                ProtocolEvent.spawn("call_impl", "impl-1", "implement task-1"),
                ProtocolEvent.wait("call_wait", ("impl-1",), {"impl-1": "DONE"}),
                ProtocolEvent.close("call_close", "impl-1"),
            ]
        )

        actions = compute_allowed_actions(workflow, protocol)

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].tool_name, "spawn_agent")
        self.assertIn("spec_reviewer", actions[0].arguments["message"])
```

- [ ] **Step 2: Run test and verify it fails**

Run:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest tests.test_subagent_scheduler.SubagentSchedulerTests.test_role_sequence_releases_spec_reviewer_after_implementer_closed -v
```

Expected: FAIL with `ImportError` for `workflow_from_role_sequence`.

- [ ] **Step 3: Implement role-sequence adapter above generic scheduler**

In `src-python/subagent_scheduler.py`, add:

```python
def workflow_from_role_sequence(
    tasks: list[str],
    roles: list[str],
    assigned: Mapping[str, str] | None = None,
) -> WorkflowState:
    assigned = assigned or {}
    nodes: dict[str, WorkflowNode] = {}
    previous_node_id: str | None = None
    for task in tasks:
        for role in roles:
            node_id = f"{task}:{role}"
            nodes[node_id] = WorkflowNode(
                node_id=node_id,
                prompt=f"You are the {role} subagent for {task}. Return DONE when complete.",
                dependencies=(previous_node_id,) if previous_node_id else (),
                assigned_agent_id=assigned.get(node_id),
                metadata={"task": task, "role": role, "adapter": "role_sequence"},
            )
            previous_node_id = node_id
    return WorkflowState(nodes=nodes)
```

- [ ] **Step 4: Keep existing `SubagentState` workflow behavior compatible**

Do not delete `_compute_next_action` workflow branches in this task. Add a comment immediately above the workflow branch:

```python
    # Compatibility path for existing Level 2 workflow gates.
    # New workflow behavior should be modeled in subagent_scheduler.py first,
    # then wired here after scheduler tests cover it.
```

- [ ] **Step 5: Run workflow tests**

Run:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest tests.test_subagent_scheduler tests.test_subagent_state -v
```

Expected: all tests pass.

- [ ] **Step 6: Commit**

```powershell
git add src-python/subagent_scheduler.py src-python/subagent_state.py tests/test_subagent_scheduler.py
git commit -m "feat: add workflow role sequence scheduler adapter"
```

---

### Task 11: Re-run Level 1 Two-Agent Through Scheduler

**Files:**
- No source changes expected unless scheduler bounded tests fail in real E2E.
- E2E artifacts: `diagnostics/subagent-e2e/level12-e2e-*`

**Interfaces:**
- Consumes: bounded scheduler from Task 9
- Produces: Level 1 two-agent stability evidence without protocol changes

- [ ] **Step 1: Run focused two-agent M3 responses gate**

Run:

```powershell
$env:PYTHONPATH='src-python'; python diagnostics\subagent-e2e\run_level12_e2e.py --level level1 --models m3 --endpoints responses --scenarios two --level1-timeout 420 --jobs 2 --repeat 5 --subagent-mode assisted --main-retry-attempts 1 --upstream-base-url http://127.0.0.1:9099/v1
```

Expected:

```text
5 rows
at least 5 pass
second child prompt contains the B sentinel
native_router_error=0
```

- [ ] **Step 2: If it fails, classify before editing**

Allowed edits:

```text
scheduler_or_model_prompt_defect -> edit subagent_scheduler.py or codex_proxy.py scheduler wiring
adapter_defect -> edit codex_proxy.py with routing test
provider_stream_flake -> no code change
protocol_or_policy_defect -> first add failing tests/test_subagent_protocol.py or tests/test_subagent_policy.py
```

- [ ] **Step 3: Run full Level 1 assisted matrix**

Run:

```powershell
$env:PYTHONPATH='src-python'; python diagnostics\subagent-e2e\run_level12_e2e.py --level level1 --models glm52,k2_7,m3 --endpoints responses,chat --scenarios single,two --level1-timeout 420 --jobs 3 --repeat 3 --subagent-mode assisted --main-retry-attempts 1 --upstream-base-url http://127.0.0.1:9099/v1
```

Expected:

```text
36 rows
all pass, or only provider_stream_flake rows with lifecycle evidence already complete
native_router_error=0
```

- [ ] **Step 4: Commit only source/test fixes if needed**

If source or tests changed:

```powershell
git add src-python/subagent_scheduler.py src-python/codex_proxy.py tests/test_subagent_scheduler.py tests/test_routing.py diagnostics/subagent-e2e/run_level12_e2e.py
git commit -m "fix: stabilize two-agent scheduler gate"
```

Do not add generated E2E run directories.

---

### Task 12: Rebuild Level 2 Against Scheduler Without Protocol Edits

**Files:**
- Modify only scheduler, policy, proxy adapter, tests, or runner classification unless a P0/P1 protocol repro is added first.
- E2E artifacts: `diagnostics/subagent-e2e/level12-e2e-*`

**Interfaces:**
- Consumes: protocol lock from Tasks 6-7
- Consumes: scheduler adapters from Tasks 8-10
- Produces: full assisted Level 2 product gate evidence

- [ ] **Step 1: Run scheduler and routing regression before E2E**

Run:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest tests.test_subagent_protocol tests.test_subagent_policy tests.test_subagent_scheduler tests.test_subagent_state tests.test_routing -v
```

Expected: all tests pass.

- [ ] **Step 2: Run M3 Level 2 first**

Run:

```powershell
$env:PYTHONPATH='src-python'; python diagnostics\subagent-e2e\run_level12_e2e.py --level level2 --models m3 --endpoints responses,chat --level2-timeout 720 --jobs 2 --repeat 3 --subagent-mode assisted --main-retry-attempts 1 --upstream-base-url http://127.0.0.1:9099/v1
```

Expected:

```text
6 rows
all pass
completed_spawn >= 3
completed_wait >= 3
completed_close >= 3
native_router_error=0
```

- [ ] **Step 3: If M3 fails, enforce layer boundary**

Do not edit `src-python/subagent_protocol.py` unless this command also fails:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest tests.test_subagent_protocol -v
```

and a new failing protocol test reproduces the transition defect.

- [ ] **Step 4: Run full Level 2 assisted matrix**

Run:

```powershell
$env:PYTHONPATH='src-python'; python diagnostics\subagent-e2e\run_level12_e2e.py --level level2 --models glm52,k2_7,m3 --endpoints responses,chat --level2-timeout 720 --jobs 3 --repeat 3 --subagent-mode assisted --main-retry-attempts 1 --upstream-base-url http://127.0.0.1:9099/v1
```

Expected:

```text
18 rows
all pass
native_router_error=0
summary.md reports repair, retry, resample, and stream counts
```

- [ ] **Step 5: Record decision summary in generated run directory**

In the latest Level 2 run directory, create `decision-summary.md` with:

```markdown
# Assisted Subagent Gate Decision Summary

- Protocol P0: PASS
- Protocol P1: PASS
- Scheduler W0: PASS
- Assisted Level 1: PASS
- Assisted Level 2: PASS
- M3 must-pass status: PASS
- Chat adapter status: PASS
- Responses adapter status: PASS
- Main retry attempts: 1
- Strict diagnostic status: recorded separately; not used as product gate
- Conclusion: ready for assisted/product mode
```

This generated summary remains an artifact and is not committed by default.

- [ ] **Step 6: Commit source/test fixes only**

If source or tests changed:

```powershell
git add src-python/subagent_scheduler.py src-python/subagent_policy.py src-python/subagent_state.py src-python/codex_proxy.py tests/test_subagent_scheduler.py tests/test_subagent_policy.py tests/test_subagent_state.py tests/test_routing.py diagnostics/subagent-e2e/run_level12_e2e.py
git commit -m "fix: pass assisted scheduler level two matrix"
```

Do not add generated E2E run directories.

---

## Final Verification Commands

Run these before claiming completion:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest tests.test_subagent_protocol tests.test_subagent_policy tests.test_subagent_scheduler tests.test_subagent_state tests.test_routing tests.test_proxy_event_logging tests.test_level12_e2e_parser -v
```

```powershell
$env:PYTHONPATH='src-python'; python diagnostics\subagent-e2e\run_level12_e2e.py --level level1 --models glm52,k2_7,m3 --endpoints responses,chat --scenarios single,two --level1-timeout 420 --jobs 3 --repeat 3 --subagent-mode assisted --main-retry-attempts 1 --upstream-base-url http://127.0.0.1:9099/v1
```

```powershell
$env:PYTHONPATH='src-python'; python diagnostics\subagent-e2e\run_level12_e2e.py --level level2 --models glm52,k2_7,m3 --endpoints responses,chat --level2-timeout 720 --jobs 3 --repeat 3 --subagent-mode assisted --main-retry-attempts 1 --upstream-base-url http://127.0.0.1:9099/v1
```

Completion requires:

```text
P0 protocol tests PASS
P1 single-agent protocol gate PASS or >=95% with classified non-protocol failures
W0 scheduler tests PASS
Level 1 assisted matrix PASS
Level 2 assisted matrix PASS
native_router_error=0
workflow failures did not modify protocol code without a protocol repro
```

## Self-Review

Spec coverage:

- Protocol lifecycle isolation is covered by Tasks 1-7.
- Strict/guided/assisted policy separation is covered by Tasks 3 and 5.
- Generic workflow scheduling is covered by Tasks 8-10.
- Bounded two-agent prompt ordering is covered by Tasks 9 and 11.
- Level 2 product gate rebuild is covered by Task 12.
- Generated diagnostic artifacts remain uncommitted in Tasks 7, 11, and 12.

Placeholder scan:

- No unresolved placeholder markers are present.
- Every code-changing task includes concrete tests, commands, and expected outcomes.

Type consistency:

- Protocol types are `ProtocolEvent`, `AgentRecord`, `ProtocolState`, and `ProtocolViolation`.
- Policy function names are `subagent_assist_mode`, `guidance_enabled`, `semantic_repair_enabled`, and `deterministic_required_action`.
- Scheduler types are `WorkflowNode`, `WorkflowState`, and `WorkflowAction`.
- Existing public compatibility entrypoint remains `build_subagent_state(input_items)`.
