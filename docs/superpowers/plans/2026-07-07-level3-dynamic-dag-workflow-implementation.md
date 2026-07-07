# Level3 Dynamic DAG Workflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Level 3 Dynamic DAG workflow path that proves runtime workflow branching and parallel subagent scheduling without changing the locked protocol lifecycle layer.

**Architecture:** Keep `subagent_protocol.py` as lifecycle fact source. Extend `subagent_scheduler.py` with generic dynamic DAG primitives, add a small `subagent_dynamic_dag.py` adapter for the Level 3 diagnostic workflow, and wire legal actions into `codex_proxy.py` so Gateway guidance and assisted repair remain deterministic. Extend the E2E runner with a `level3` gate after scheduler and Gateway unit tests are green.

**Tech Stack:** Python 3.13, `unittest`, CodexHub Gateway, Codex native `multi_agent_v1` tools, existing `diagnostics/subagent-e2e/run_level12_e2e.py` harness.

## Global Constraints

- Support runtime workflow graph changes above the protocol layer.
- Allow multiple ready spawn actions when the DAG permits parallel work.
- Keep assisted repair deterministic: repair only when exactly one legal action exists.
- Preserve Level 1 protocol behavior and Level 2 ordered workflow behavior.
- Provide an E2E gate that distinguishes scheduler defects from protocol defects.
- Keep the framework generic, not tied to Superpowers-specific roles.
- Do not rewrite the protocol lifecycle state machine.
- Do not move business workflow semantics into `subagent_protocol.py`.
- Do not let the Gateway synthesize final answers or reviewer decisions.
- Do not make the Gateway choose between multiple valid DAG branches.
- Do not build a full planner language in the first Level 3 slice.
- Do not require every workflow to be dynamic; fixed ordered workflows remain supported.

---

## File Structure

- Modify `src-python/subagent_scheduler.py`: generic DAG node metadata, `append_node`, public node completion helper, workflow completion helper, spawn action nickname support.
- Create `src-python/subagent_dynamic_dag.py`: Level 3 diagnostic adapter that materializes nodes from protocol state at runtime.
- Modify `src-python/subagent_state.py`: detect Level 3 Dynamic DAG requests and expose dynamic workflow state without changing Level 2 ordered workflow logic.
- Modify `src-python/codex_proxy.py`: consume dynamic legal actions for tool visibility, deterministic repair, and duplicate spawn suppression.
- Modify `diagnostics/subagent-e2e/run_level12_e2e.py`: add `--level level3`, Level 3 prompt, parser, analyzer, and summary fields.
- Modify `tests/test_subagent_scheduler.py`: scheduler unit coverage for dynamic DAG primitives.
- Create `tests/test_subagent_dynamic_dag.py`: adapter unit tests for runtime node materialization.
- Modify `tests/test_subagent_state.py`: request detection and state summarization tests.
- Modify `tests/test_routing.py`: Gateway legal-action and response repair tests.
- Modify `tests/test_level12_e2e_parser.py`: Level 3 parser tests.

---

### Task 1: Dynamic Scheduler Primitives

**Files:**
- Modify: `src-python/subagent_scheduler.py`
- Modify: `tests/test_subagent_scheduler.py`

**Interfaces:**
- Consumes: `ProtocolState.agents`, `ProtocolState.open_agent_ids`, `ProtocolState.closed_agent_ids`, `ProtocolState.needs_input_agent_ids`, `ProtocolState.violations`
- Produces:
  - `append_node(workflow: WorkflowState, node: WorkflowNode, *, allow_external_dependencies: bool = False) -> WorkflowState`
  - `node_complete(node: WorkflowNode, protocol: ProtocolState) -> bool`
  - `workflow_complete(workflow: WorkflowState, protocol: ProtocolState) -> bool`
  - `WorkflowNode.terminal: bool`
  - Node status remains derived from `ProtocolState`; do not add a persisted `WorkflowNode.status` field.

- [ ] **Step 1: Write failing scheduler tests**

Add these tests to `tests/test_subagent_scheduler.py`:

```python
from subagent_scheduler import (
    WorkflowNode,
    WorkflowState,
    append_node,
    compute_allowed_actions,
    node_complete,
    workflow_complete,
)


def test_append_node_rejects_duplicate_node_id(self):
    workflow = WorkflowState(nodes={"task-a": WorkflowNode(node_id="task-a", prompt="do A")})

    with self.assertRaisesRegex(ValueError, "duplicate workflow node: task-a"):
        append_node(workflow, WorkflowNode(node_id="task-a", prompt="do A again"))


def test_append_node_rejects_missing_dependency(self):
    workflow = WorkflowState()

    with self.assertRaisesRegex(ValueError, "missing workflow dependency: task-a"):
        append_node(
            workflow,
            WorkflowNode(node_id="review-a", prompt="review A", dependencies=("task-a",)),
        )


def test_closed_dependency_releases_multiple_ready_nodes(self):
    workflow = WorkflowState(
        nodes={
            "task-a": WorkflowNode(
                node_id="task-a",
                prompt="do A",
                assigned_agent_id="agent-a",
            )
        }
    )
    workflow = append_node(workflow, WorkflowNode(node_id="review-a", prompt="review A", dependencies=("task-a",)))
    workflow = append_node(workflow, WorkflowNode(node_id="task-b", prompt="do B", dependencies=("task-a",)))
    protocol = reduce_protocol_events(
        [
            ProtocolEvent.spawn("call_spawn", "agent-a", "do A", "task-a"),
            ProtocolEvent.wait("call_wait", ("agent-a",), {"agent-a": "A_DONE"}),
            ProtocolEvent.close("call_close", "agent-a"),
        ]
    )

    actions = compute_allowed_actions(workflow, protocol)

    self.assertEqual([action.node_id for action in actions], ["review-a", "task-b"])
    self.assertEqual([action.tool_name for action in actions], ["spawn_agent", "spawn_agent"])
    self.assertEqual(actions[0].arguments["nickname"], "review-a")
    self.assertEqual(actions[1].arguments["nickname"], "task-b")


def test_assigned_node_is_complete_only_after_close(self):
    node = WorkflowNode(node_id="task-a", prompt="do A", assigned_agent_id="agent-a")
    spawned = reduce_protocol_events([ProtocolEvent.spawn("call_spawn", "agent-a", "do A", "task-a")])
    waited = reduce_protocol_events(
        [
            ProtocolEvent.spawn("call_spawn", "agent-a", "do A", "task-a"),
            ProtocolEvent.wait("call_wait", ("agent-a",), {"agent-a": "A_DONE"}),
        ]
    )
    closed = reduce_protocol_events(
        [
            ProtocolEvent.spawn("call_spawn", "agent-a", "do A", "task-a"),
            ProtocolEvent.wait("call_wait", ("agent-a",), {"agent-a": "A_DONE"}),
            ProtocolEvent.close("call_close", "agent-a"),
        ]
    )

    self.assertFalse(node_complete(node, spawned))
    self.assertFalse(node_complete(node, waited))
    self.assertTrue(node_complete(node, closed))


def test_workflow_complete_requires_terminal_nodes_closed(self):
    workflow = WorkflowState(
        nodes={
            "final": WorkflowNode(
                node_id="final",
                prompt="summarize",
                assigned_agent_id="agent-final",
                terminal=True,
            )
        }
    )
    waited = reduce_protocol_events(
        [
            ProtocolEvent.spawn("call_spawn", "agent-final", "summarize", "final"),
            ProtocolEvent.wait("call_wait", ("agent-final",), {"agent-final": "FINAL_READY"}),
        ]
    )
    closed = reduce_protocol_events(
        [
            ProtocolEvent.spawn("call_spawn", "agent-final", "summarize", "final"),
            ProtocolEvent.wait("call_wait", ("agent-final",), {"agent-final": "FINAL_READY"}),
            ProtocolEvent.close("call_close", "agent-final"),
        ]
    )

    self.assertFalse(workflow_complete(workflow, waited))
    self.assertTrue(workflow_complete(workflow, closed))
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest tests.test_subagent_scheduler -v
```

Expected: FAIL or ERROR because `append_node`, `node_complete`, `workflow_complete`, and `WorkflowNode.terminal` do not exist.

- [ ] **Step 3: Implement minimal scheduler primitives**

Modify `src-python/subagent_scheduler.py`:

```python
@dataclass(frozen=True)
class WorkflowNode:
    node_id: str
    prompt: str
    dependencies: tuple[str, ...] = ()
    assigned_agent_id: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)
    terminal: bool = False
```

Add these functions below `workflow_from_role_sequence`:

```python
def append_node(
    workflow: WorkflowState,
    node: WorkflowNode,
    *,
    allow_external_dependencies: bool = False,
) -> WorkflowState:
    if node.node_id in workflow.nodes:
        raise ValueError(f"duplicate workflow node: {node.node_id}")
    if not allow_external_dependencies:
        for dependency in node.dependencies:
            if dependency not in workflow.nodes:
                raise ValueError(f"missing workflow dependency: {dependency}")
    nodes = dict(workflow.nodes)
    nodes[node.node_id] = node
    return WorkflowState(nodes=nodes)


def node_complete(node: WorkflowNode, protocol: ProtocolState) -> bool:
    return bool(node.assigned_agent_id and node.assigned_agent_id in protocol.closed_agent_ids)


def workflow_complete(workflow: WorkflowState, protocol: ProtocolState) -> bool:
    if protocol.violations or protocol.open_agent_ids or protocol.needs_input_agent_ids:
        return False
    terminal_nodes = [node for node in workflow.nodes.values() if node.terminal]
    if terminal_nodes:
        return all(node_complete(node, protocol) for node in terminal_nodes)
    return bool(workflow.nodes) and all(node_complete(node, protocol) for node in workflow.nodes.values())
```

Update spawn action arguments in `compute_allowed_actions`:

```python
arguments={"message": node.prompt, "nickname": node.node_id, "fork_context": False},
```

Update the private helper:

```python
def _node_complete(node: WorkflowNode, protocol: ProtocolState) -> bool:
    return node_complete(node, protocol)
```

- [ ] **Step 4: Run scheduler tests**

Run:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest tests.test_subagent_scheduler -v
```

Expected: all scheduler tests pass.

- [ ] **Step 5: Commit scheduler primitive slice**

```powershell
git add src-python/subagent_scheduler.py tests/test_subagent_scheduler.py
git commit -m "feat: add dynamic dag scheduler primitives"
```

---

### Task 2: Level3 Dynamic DAG Adapter

**Files:**
- Create: `src-python/subagent_dynamic_dag.py`
- Create: `tests/test_subagent_dynamic_dag.py`

**Interfaces:**
- Consumes:
  - `subagent_scheduler.WorkflowNode`
  - `subagent_scheduler.WorkflowState`
  - `subagent_scheduler.append_node`
  - `subagent_scheduler.node_complete`
  - `subagent_protocol.ProtocolState`
- Produces:
  - `LEVEL3_DYNAMIC_DAG_MARKER = "LEVEL3_DYNAMIC_DAG"`
  - `is_dynamic_dag_request(input_items: Any) -> bool`
  - `build_dynamic_dag_workflow(input_items: Any, protocol: ProtocolState) -> WorkflowState`
  - `dynamic_dag_guidance_message(workflow: WorkflowState, protocol: ProtocolState) -> dict[str, Any]`

- [ ] **Step 1: Write failing adapter tests**

Create `tests/test_subagent_dynamic_dag.py`:

```python
import unittest

from subagent_dynamic_dag import (
    LEVEL3_DYNAMIC_DAG_MARKER,
    build_dynamic_dag_workflow,
    dynamic_dag_guidance_message,
    is_dynamic_dag_request,
)
from subagent_protocol import ProtocolEvent, reduce_protocol_events


def message(text):
    return {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}


class DynamicDagAdapterTests(unittest.TestCase):
    def test_detects_dynamic_dag_request_marker(self):
        self.assertTrue(is_dynamic_dag_request([message(f"Run {LEVEL3_DYNAMIC_DAG_MARKER}.")]))
        self.assertFalse(is_dynamic_dag_request([message("Run an ordered Level 2 workflow.")]))

    def test_initial_workflow_contains_only_task_a_implementer(self):
        protocol = reduce_protocol_events([])

        workflow = build_dynamic_dag_workflow([message(f"Run {LEVEL3_DYNAMIC_DAG_MARKER}.")], protocol)

        self.assertEqual(list(workflow.nodes), ["task-a-implementer"])
        self.assertEqual(workflow.nodes["task-a-implementer"].dependencies, ())

    def test_task_a_close_appends_review_and_task_b(self):
        protocol = reduce_protocol_events(
            [
                ProtocolEvent.spawn("call_a", "agent-a", "Node: task-a-implementer", "task-a-implementer"),
                ProtocolEvent.wait("wait_a", ("agent-a",), {"agent-a": "A_DONE"}),
                ProtocolEvent.close("close_a", "agent-a"),
            ]
        )

        workflow = build_dynamic_dag_workflow([message(f"Run {LEVEL3_DYNAMIC_DAG_MARKER}.")], protocol)

        self.assertEqual(
            list(workflow.nodes),
            ["task-a-implementer", "task-a-reviewer", "task-b-implementer"],
        )
        self.assertEqual(workflow.nodes["task-a-reviewer"].dependencies, ("task-a-implementer",))
        self.assertEqual(workflow.nodes["task-b-implementer"].dependencies, ("task-a-implementer",))
        self.assertEqual(workflow.nodes["task-a-implementer"].assigned_agent_id, "agent-a")

    def test_branch_closes_append_terminal_final_summarizer(self):
        protocol = reduce_protocol_events(
            [
                ProtocolEvent.spawn("call_a", "agent-a", "Node: task-a-implementer", "task-a-implementer"),
                ProtocolEvent.wait("wait_a", ("agent-a",), {"agent-a": "A_DONE"}),
                ProtocolEvent.close("close_a", "agent-a"),
                ProtocolEvent.spawn("call_review", "agent-review", "Node: task-a-reviewer", "task-a-reviewer"),
                ProtocolEvent.wait("wait_review", ("agent-review",), {"agent-review": "A_REVIEW_PASS"}),
                ProtocolEvent.close("close_review", "agent-review"),
                ProtocolEvent.spawn("call_b", "agent-b", "Node: task-b-implementer", "task-b-implementer"),
                ProtocolEvent.wait("wait_b", ("agent-b",), {"agent-b": "B_DONE"}),
                ProtocolEvent.close("close_b", "agent-b"),
            ]
        )

        workflow = build_dynamic_dag_workflow([message(f"Run {LEVEL3_DYNAMIC_DAG_MARKER}.")], protocol)

        self.assertIn("final-summarizer", workflow.nodes)
        self.assertEqual(
            workflow.nodes["final-summarizer"].dependencies,
            ("task-a-reviewer", "task-b-implementer"),
        )
        self.assertTrue(workflow.nodes["final-summarizer"].terminal)

    def test_guidance_lists_ready_dynamic_nodes(self):
        protocol = reduce_protocol_events(
            [
                ProtocolEvent.spawn("call_a", "agent-a", "Node: task-a-implementer", "task-a-implementer"),
                ProtocolEvent.wait("wait_a", ("agent-a",), {"agent-a": "A_DONE"}),
                ProtocolEvent.close("close_a", "agent-a"),
            ]
        )
        workflow = build_dynamic_dag_workflow([message(f"Run {LEVEL3_DYNAMIC_DAG_MARKER}.")], protocol)

        guidance = dynamic_dag_guidance_message(workflow, protocol)
        text = guidance["content"][0]["text"]

        self.assertIn("workflow_type: dynamic_dag", text)
        self.assertIn("ready_nodes: task-a-reviewer, task-b-implementer", text)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest tests.test_subagent_dynamic_dag -v
```

Expected: ERROR because `subagent_dynamic_dag` does not exist.

- [ ] **Step 3: Implement the adapter**

Create `src-python/subagent_dynamic_dag.py`:

```python
from __future__ import annotations

from collections.abc import Mapping as MappingABC
from typing import Any

from subagent_protocol import ProtocolState
from subagent_scheduler import WorkflowNode, WorkflowState, append_node, node_complete


LEVEL3_DYNAMIC_DAG_MARKER = "LEVEL3_DYNAMIC_DAG"


_NODE_OUTPUTS = {
    "task-a-implementer": "A_DONE",
    "task-a-reviewer": "A_REVIEW_PASS",
    "task-b-implementer": "B_DONE",
    "final-summarizer": "FINAL_READY",
}


def is_dynamic_dag_request(input_items: Any) -> bool:
    return LEVEL3_DYNAMIC_DAG_MARKER in _input_text(input_items)


def build_dynamic_dag_workflow(input_items: Any, protocol: ProtocolState) -> WorkflowState:
    if not is_dynamic_dag_request(input_items):
        return WorkflowState()
    workflow = WorkflowState()
    workflow = append_node(workflow, _node("task-a-implementer", protocol))
    if node_complete(workflow.nodes["task-a-implementer"], protocol):
        workflow = append_node(
            workflow,
            _node("task-a-reviewer", protocol, dependencies=("task-a-implementer",)),
        )
        workflow = append_node(
            workflow,
            _node("task-b-implementer", protocol, dependencies=("task-a-implementer",)),
        )
    if (
        "task-a-reviewer" in workflow.nodes
        and "task-b-implementer" in workflow.nodes
        and node_complete(workflow.nodes["task-a-reviewer"], protocol)
        and node_complete(workflow.nodes["task-b-implementer"], protocol)
    ):
        workflow = append_node(
            workflow,
            _node(
                "final-summarizer",
                protocol,
                dependencies=("task-a-reviewer", "task-b-implementer"),
                terminal=True,
            ),
        )
    return workflow


def dynamic_dag_guidance_message(workflow: WorkflowState, protocol: ProtocolState) -> dict[str, Any]:
    ready_nodes = [
        node.node_id
        for node in workflow.nodes.values()
        if not node.assigned_agent_id
        and all(node_complete(workflow.nodes[dependency], protocol) for dependency in node.dependencies)
    ]
    lines = [
        "Dynamic DAG workflow state",
        "workflow_type: dynamic_dag",
        f"ready_nodes: {', '.join(ready_nodes) if ready_nodes else '<none>'}",
        f"closed_agents: {', '.join(protocol.closed_agent_ids) if protocol.closed_agent_ids else '<none>'}",
        "spawn_rule: when spawning a ready node, set nickname exactly to the node_id.",
    ]
    return {
        "type": "message",
        "role": "developer",
        "content": [{"type": "input_text", "text": "\n".join(lines)}],
    }


def _node(
    node_id: str,
    protocol: ProtocolState,
    *,
    dependencies: tuple[str, ...] = (),
    terminal: bool = False,
) -> WorkflowNode:
    return WorkflowNode(
        node_id=node_id,
        prompt=_prompt(node_id),
        dependencies=dependencies,
        assigned_agent_id=_assigned_agent_id(protocol, node_id),
        metadata={"workflow": "dynamic_dag", "expected_output": _NODE_OUTPUTS[node_id]},
        terminal=terminal,
    )


def _prompt(node_id: str) -> str:
    expected = _NODE_OUTPUTS[node_id]
    return (
        f"You are a Level 3 Dynamic DAG worker.\n"
        f"Node: {node_id}\n"
        f"Return exactly one line:\n{expected}\n"
        "Do not call multi_agent tools. Do not create or modify files."
    )


def _assigned_agent_id(protocol: ProtocolState, node_id: str) -> str | None:
    for agent_id, agent in protocol.agents.items():
        if agent.nickname == node_id:
            return agent_id
        if f"Node: {node_id}" in agent.prompt:
            return agent_id
    return None


def _input_text(input_items: Any) -> str:
    if not isinstance(input_items, list):
        return ""
    parts: list[str] = []
    for item in input_items:
        if not isinstance(item, MappingABC):
            continue
        content = item.get("content")
        if isinstance(content, list):
            for part in content:
                if isinstance(part, MappingABC):
                    value = part.get("text")
                    if isinstance(value, str):
                        parts.append(value)
        value = item.get("text")
        if isinstance(value, str):
            parts.append(value)
    return "\n".join(parts)
```

- [ ] **Step 4: Run adapter and scheduler tests**

Run:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest tests.test_subagent_scheduler tests.test_subagent_dynamic_dag -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit adapter slice**

```powershell
git add src-python/subagent_dynamic_dag.py tests/test_subagent_dynamic_dag.py
git commit -m "feat: add level3 dynamic dag adapter"
```

---

### Task 3: Dynamic DAG State and Gateway Legal Actions

**Files:**
- Modify: `src-python/subagent_state.py`
- Modify: `src-python/codex_proxy.py`
- Modify: `tests/test_subagent_state.py`
- Modify: `tests/test_routing.py`

**Interfaces:**
- Consumes:
  - `subagent_dynamic_dag.is_dynamic_dag_request`
  - `subagent_dynamic_dag.build_dynamic_dag_workflow`
  - `subagent_dynamic_dag.dynamic_dag_guidance_message`
  - `subagent_scheduler.compute_allowed_actions`
  - `subagent_scheduler.workflow_complete`
- Produces event context fields:
  - `subagent_dynamic_dag_active: bool`
  - `subagent_dynamic_dag_ready_nodes: list[str]`
  - `subagent_legal_actions: list[dict[str, Any]]`

- [ ] **Step 1: Write failing state tests**

Add to `tests/test_subagent_state.py`:

```python
def test_dynamic_dag_request_does_not_use_ordered_level2_role_sequence(self):
    state = build_subagent_state(
        [
            message(
                "Run LEVEL3_DYNAMIC_DAG. Start with task-a-implementer. "
                "After it closes, run task-a-reviewer and task-b-implementer."
            )
        ]
    )

    self.assertTrue(state.workflow_intent)
    self.assertEqual(state.next_action, "spawn")
    self.assertIsNone(state.next_expected_role)
    self.assertIsNone(state.next_expected_task)
```

- [ ] **Step 2: Write failing Gateway tests**

Add to `tests/test_routing.py`:

```python
def test_responses_structured_dynamic_dag_exposes_multiple_legal_spawns_without_required_repair(self):
    body = json.dumps(
        {
            "model": "ollama-e2e-responses/minimax-m3",
            "input": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "Run LEVEL3_DYNAMIC_DAG. Start with task-a-implementer. "
                                "After it closes, run task-a-reviewer and task-b-implementer."
                            ),
                        }
                    ],
                },
                {
                    "type": "function_call",
                    "call_id": "call_a",
                    "name": "multi_agent_v1__spawn_agent",
                    "arguments": json.dumps(
                        {"message": "Node: task-a-implementer", "nickname": "task-a-implementer"}
                    ),
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_a",
                    "output": json.dumps({"agent_id": "agent-a", "nickname": "task-a-implementer"}),
                },
                {
                    "type": "function_call",
                    "call_id": "wait_a",
                    "name": "multi_agent_v1__wait_agent",
                    "arguments": json.dumps({"targets": ["agent-a"]}),
                },
                {
                    "type": "function_call_output",
                    "call_id": "wait_a",
                    "output": json.dumps({"agents": {"agent-a": {"status": "completed", "message": "A_DONE"}}}),
                },
                {
                    "type": "function_call",
                    "call_id": "close_a",
                    "name": "multi_agent_v1__close_agent",
                    "arguments": json.dumps({"target": "agent-a"}),
                },
                {
                    "type": "function_call_output",
                    "call_id": "close_a",
                    "output": json.dumps({"agent_id": "agent-a", "status": "closed"}),
                },
            ],
        }
    ).encode("utf-8")
    event_context = {"request_id": "req"}

    transformed = compatible_request_body(body, {"name": "ollama_cloud"}, event_context=event_context)

    self.assertTrue(event_context["subagent_dynamic_dag_active"])
    self.assertTrue(event_context["subagent_spawn_allowed"])
    self.assertEqual(
        [action["node_id"] for action in event_context["subagent_legal_actions"]],
        ["task-a-reviewer", "task-b-implementer"],
    )
    self.assertNotIn("subagent_required_spawn_arguments", event_context)
    self.assertIn("Dynamic DAG workflow state", json.dumps(json.loads(transformed)))


def test_dynamic_dag_duplicate_spawn_for_assigned_node_is_suppressed(self):
    event_context = {
        "request_id": "req",
        "tool_protocol": "responses_structured",
        "subagent_spawn_allowed": True,
        "subagent_dynamic_dag_active": True,
        "subagent_legal_actions": [
            {
                "kind": "workflow",
                "tool_name": "spawn_agent",
                "node_id": "task-a-reviewer",
                "arguments": {
                    "message": "Node: task-a-reviewer",
                    "nickname": "task-a-reviewer",
                    "fork_context": False,
                },
            }
        ],
        "subagent_assigned_dynamic_nodes": ["task-a-reviewer"],
    }
    events = [
        {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": {
                "id": "fc_dup",
                "type": "function_call",
                "status": "completed",
                "call_id": "call_dup",
                "name": "multi_agent_v1__spawn_agent",
                "arguments": json.dumps({"message": "Node: task-a-reviewer", "nickname": "task-a-reviewer"}),
            },
        }
    ]

    guarded, changed = codex_proxy._guard_duplicate_multi_agent_spawn_calls(events, event_context)

    self.assertTrue(changed)
    self.assertEqual(guarded[0]["item"]["type"], "message")
    self.assertIn("already assigned", guarded[0]["item"]["content"])
```

- [ ] **Step 3: Run tests to verify they fail**

Run:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest tests.test_subagent_state tests.test_routing -v
```

Expected: FAIL because Dynamic DAG detection and event context fields do not exist.

- [ ] **Step 4: Add Dynamic DAG fields to `SubagentState`**

Modify `src-python/subagent_state.py`:

```python
from subagent_dynamic_dag import is_dynamic_dag_request
```

Extend `SubagentState`:

```python
    dynamic_dag_intent: bool = False
```

In `build_subagent_state` after `workflow_intent` is computed:

```python
    dynamic_dag_intent = is_dynamic_dag_request(input_items)
    workflow_intent = workflow_intent or dynamic_dag_intent
```

When constructing `SubagentState`, pass:

```python
        dynamic_dag_intent=dynamic_dag_intent,
```

In `_compute_next_action`, add this before ordered workflow logic:

```python
    if state.dynamic_dag_intent:
        state.next_action = "spawn" if not state.lifecycle_complete else "final"
        state.next_expected_role = None
        state.next_expected_task = None
        return
```

- [ ] **Step 5: Wire legal actions in `codex_proxy.py`**

Add imports:

```python
from subagent_dynamic_dag import (
    build_dynamic_dag_workflow,
    dynamic_dag_guidance_message,
    is_dynamic_dag_request,
)
from subagent_scheduler import workflow_complete
```

In the `compatible_request_body` multi-agent preparation block, after `protocol_state = getattr(subagent_state, "protocol_state", None)`, add:

```python
            if (
                protocol_state is not None
                and bool(getattr(subagent_state, "dynamic_dag_intent", False))
                and is_dynamic_dag_request(input_items)
            ):
                workflow = build_dynamic_dag_workflow(input_items, protocol_state)
                legal_actions = compute_allowed_actions(workflow, protocol_state)
                event_context["subagent_dynamic_dag_active"] = True
                event_context["subagent_dynamic_dag_ready_nodes"] = [
                    action.node_id for action in legal_actions if action.tool_name == "spawn_agent" and action.node_id
                ]
                event_context["subagent_assigned_dynamic_nodes"] = [
                    node.node_id for node in workflow.nodes.values() if node.assigned_agent_id
                ]
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
                include_spawn_agent = any(action.tool_name == "spawn_agent" for action in legal_actions)
                include_wait_agent = any(action.tool_name == "wait_agent" for action in legal_actions)
                include_close_agent = any(action.tool_name == "close_agent" for action in legal_actions)
                include_send_input = any(action.tool_name == "send_input" for action in legal_actions)
                include_resume_agent = include_send_input
                lifecycle_complete = workflow_complete(workflow, protocol_state)
                if guidance_enabled and isinstance(input_items, list):
                    input_items.append(dynamic_dag_guidance_message(workflow, protocol_state))
                if len(legal_actions) != 1:
                    event_context.pop("subagent_required_spawn_arguments", None)
```

Update `_required_subagent_call_spec` so multiple legal actions do not repair:

```python
    legal_actions = context.get("subagent_legal_actions")
    if isinstance(legal_actions, list):
        if len(legal_actions) != 1:
            return None
```

Update `_guard_duplicate_multi_agent_spawn_calls` so dynamic DAG response streams can be guarded even when no ordered-workflow `SubagentState` object exists:

```python
    tool_protocol = str((event_context or {}).get("tool_protocol") or "")
    if tool_protocol not in {"text_compat", "chat_tools", "responses_structured"}:
        return value, False

    spawn_allowed = bool((event_context or {}).get("subagent_spawn_allowed"))
    subagent_state = (event_context or {}).get("_subagent_state")
    dynamic_dag_active = bool((event_context or {}).get("subagent_dynamic_dag_active"))
    if spawn_allowed and subagent_state is None and not dynamic_dag_active:
        return value, False
```

Pass `event_context` into `_guard_duplicate_multi_agent_spawn_calls_inner`:

```python
    return _guard_duplicate_multi_agent_spawn_calls_inner(
        value,
        event_context=event_context,
        spawn_allowed=spawn_allowed,
        subagent_state=subagent_state,
        lifecycle_complete=lifecycle_complete,
        wait_agent_ids=wait_agent_ids,
        open_agent_ids=open_agent_ids,
        accepted_workflow_spawn=accepted_workflow_spawn,
    )
```

Update `_guard_duplicate_multi_agent_spawn_calls_inner` signature and recursive calls:

```python
def _guard_duplicate_multi_agent_spawn_calls_inner(
    value: Any,
    *,
    event_context: Mapping[str, Any] | None,
    spawn_allowed: bool,
    subagent_state: Any | None,
    lifecycle_complete: bool,
    wait_agent_ids: list[str],
    open_agent_ids: list[str],
    accepted_workflow_spawn: list[bool],
) -> tuple[Any, bool]:
```

Add this block at the start of the `_is_multi_agent_spawn_function_call(value)` branch, before ordered-workflow duplicate checks:

```python
        if bool((event_context or {}).get("subagent_dynamic_dag_active")):
            arguments = _json_object_from_arguments(value.get("arguments")) or {}
            nickname = str(arguments.get("nickname") or "")
            assigned_nodes = {
                node_id
                for node_id in (event_context or {}).get("subagent_assigned_dynamic_nodes", [])
                if isinstance(node_id, str)
            }
            if nickname in assigned_nodes:
                return {
                    "type": "message",
                    "role": "assistant",
                    "content": (
                        "dynamic_dag_spawn_suppressed: node already assigned; "
                        "wait or close existing work before repeating it."
                    ),
                }, True
```

- [ ] **Step 6: Run focused state and routing tests**

Run:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest tests.test_subagent_state tests.test_routing -v
```

Expected: all tests pass.

- [ ] **Step 7: Commit Gateway integration slice**

```powershell
git add src-python/subagent_state.py src-python/codex_proxy.py tests/test_subagent_state.py tests/test_routing.py
git commit -m "feat: wire dynamic dag legal actions"
```

---

### Task 4: Level3 E2E Runner and Parser

**Files:**
- Modify: `diagnostics/subagent-e2e/run_level12_e2e.py`
- Modify: `tests/test_level12_e2e_parser.py`

**Interfaces:**
- Consumes existing runner helpers:
  - `run_codex_case`
  - `run_e2e_tasks`
  - `completed_tool_calls`
  - `proxy_event_counts_for_case`
  - `router_errors`
  - `classify_failure`
- Produces:
  - `level3_dynamic_dag_prompt(case_name: str) -> str`
  - `analyze_level3_dynamic_dag(case: dict[str, Any]) -> dict[str, Any]`
  - CLI `--level level3`
  - CLI `--workflow dynamic-dag`
  - CLI `--level3-timeout`

- [ ] **Step 1: Write failing parser tests**

Add to `tests/test_level12_e2e_parser.py`:

```python
    def test_level3_analyzer_accepts_parallel_branch_order(self):
        runner = load_runner_module()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stdout = root / "case.stdout.jsonl"
            stderr = root / "case.stderr.txt"
            stderr.write_text("", encoding="utf-8")
            events = [
                completed_call("spawn_agent", "Node: task-a-implementer"),
                completed_call("wait", None, messages={"agent-a": "A_DONE"}),
                completed_call("close_agent", None),
                completed_call("spawn_agent", "Node: task-b-implementer"),
                completed_call("spawn_agent", "Node: task-a-reviewer"),
                completed_call("wait", None, messages={"agent-b": "B_DONE", "agent-review": "A_REVIEW_PASS"}),
                completed_call("close_agent", None),
                completed_call("close_agent", None),
                completed_call("spawn_agent", "Node: final-summarizer"),
                completed_call("wait", None, messages={"agent-final": "FINAL_READY"}),
                completed_call("close_agent", None),
                {
                    "type": "item.completed",
                    "item": {
                        "type": "agent_message",
                        "text": (
                            "RESULT: PASS\n"
                            "DYNAMIC_DAG_CHAIN: task-a-implementer,task-a-reviewer,task-b-implementer,final-summarizer\n"
                            "DYNAMIC_DAG_STATUS: a-done,a-review-pass,b-done"
                        ),
                    },
                },
            ]
            stdout.write_text(
                "".join(json.dumps(event, ensure_ascii=True) + "\n" for event in events),
                encoding="utf-8",
                newline="\n",
            )
            case = {
                "case": "level3-m3-responses-r01",
                "model": "ollama-e2e-responses/minimax-m3",
                "endpoint": "responses",
                "stdout": str(stdout),
                "stderr": str(stderr),
                "exit_code": 0,
                "timed_out": False,
            }

            summary = runner.analyze_level3_dynamic_dag(case)

            self.assertTrue(summary["checks"]["branch_nodes_seen"])
            self.assertTrue(summary["checks"]["final_exact"])
            self.assertTrue(summary["pass"])
```

Update test helper signature:

```python
def completed_call(tool, prompt, messages=None):
    item = {
        "type": "collab_tool_call",
        "tool": tool,
        "status": "completed",
        "prompt": prompt,
        "receiver_thread_ids": ["agent-1"] if tool == "spawn_agent" else [],
    }
    if messages is not None:
        item["agents_states"] = {
            agent_id: {"status": "completed", "message": message}
            for agent_id, message in messages.items()
        }
    return {"type": "item.completed", "item": item}
```

- [ ] **Step 2: Run parser tests to verify they fail**

Run:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest tests.test_level12_e2e_parser -v
```

Expected: FAIL because `analyze_level3_dynamic_dag` does not exist.

- [ ] **Step 3: Add Level3 prompt**

Add to `diagnostics/subagent-e2e/run_level12_e2e.py` near the Level 2 prompt:

```python
def level3_dynamic_dag_prompt(case_name: str) -> str:
    return f"""Run LEVEL3_DYNAMIC_DAG using real Codex native subagents.

Case: {case_name}

Workflow:
1. Spawn node task-a-implementer first. Set nickname exactly to task-a-implementer.
2. Wait for task-a-implementer and close it after it returns A_DONE.
3. After task-a-implementer is closed, spawn both ready nodes:
   - task-a-reviewer, nickname task-a-reviewer, returns A_REVIEW_PASS
   - task-b-implementer, nickname task-b-implementer, returns B_DONE
   These two nodes may be spawned in either order or in the same turn.
4. Wait for both branch nodes and close both after their expected outputs are returned.
5. Spawn final-summarizer, nickname final-summarizer, after both branch nodes are closed.
6. Wait for final-summarizer and close it after it returns FINAL_READY.

Worker prompt rule:
Every worker prompt must contain a line `Node: <node_id>`.
Workers must not call multi_agent tools and must not create or modify files.

Coordinator constraints:
Do not use local_tool_gateway or mcp__codex_apps__local_tool_gateway tools.
Do not directly perform worker outputs yourself.
Do not write final response until final-summarizer is closed.

Final coordinator response must be exactly:
RESULT: PASS
DYNAMIC_DAG_CHAIN: task-a-implementer,task-a-reviewer,task-b-implementer,final-summarizer
DYNAMIC_DAG_STATUS: a-done,a-review-pass,b-done
"""
```

- [ ] **Step 4: Add analyzer**

Add:

```python
def level3_node_from_prompt(prompt: str | None) -> str | None:
    if not isinstance(prompt, str):
        return None
    match = re.search(r"Node:\s*(task-a-implementer|task-a-reviewer|task-b-implementer|final-summarizer)", prompt)
    return match.group(1) if match else None


def analyze_level3_dynamic_dag(case: dict[str, Any]) -> dict[str, Any]:
    stdout_path = Path(case["stdout"])
    stderr_path = Path(case["stderr"])
    parsed = parse_codex_jsonl(stdout_path)
    spawns = completed_tool_calls(parsed, {"spawn_agent"})
    waits = completed_tool_calls(parsed, {"wait"})
    closes = completed_tool_calls(parsed, {"close_agent"})
    nodes = [node for node in (level3_node_from_prompt(spawn.get("prompt")) for spawn in spawns) if node]
    final_lines = [line.strip() for line in parsed["final_text"].splitlines() if line.strip()]
    router = router_errors(parsed, stderr_path)
    proxy_counts = proxy_event_counts_for_case(case, parsed)
    expected_final = [
        "RESULT: PASS",
        "DYNAMIC_DAG_CHAIN: task-a-implementer,task-a-reviewer,task-b-implementer,final-summarizer",
        "DYNAMIC_DAG_STATUS: a-done,a-review-pass,b-done",
    ]
    branch_positions = [nodes.index(node) for node in ("task-a-reviewer", "task-b-implementer") if node in nodes]
    checks = {
        "exit_code_zero": case.get("exit_code") == 0,
        "not_timed_out": not case.get("timed_out"),
        "initial_node_first": nodes[:1] == ["task-a-implementer"],
        "branch_nodes_seen": set(["task-a-reviewer", "task-b-implementer"]).issubset(nodes),
        "final_summarizer_last": nodes[-1:] == ["final-summarizer"],
        "no_duplicate_nodes": len(nodes) == len(set(nodes)),
        "branch_after_initial": bool(branch_positions) and all(position > 0 for position in branch_positions),
        "has_waits": len(waits) >= 3,
        "has_closes": len(closes) >= 4,
        "final_exact": final_lines == expected_final,
        "no_router_errors": not router and proxy_counts["native_router_error"] == 0,
    }
    summary = {
        **case,
        **proxy_counts,
        "scenario": "level3_dynamic_dag",
        "pass": all(checks.values()),
        "checks": checks,
        "nodes": nodes,
        "router_errors": router,
        "tool_counts": {
            "completed_spawn": len(spawns),
            "completed_wait": len(waits),
            "completed_close": len(closes),
        },
        "final_text": parsed["final_text"],
    }
    summary["failure_classification"] = classify_failure(summary)
    if not summary["pass"] and summary["failure_classification"] == "unclassified":
        summary["failure_classification"] = "dynamic_scheduler_defect"
    return summary
```

- [ ] **Step 5: Extend CLI dispatch**

Change parser choices:

```python
parser.add_argument("--level", choices=["level1", "level2", "level3", "all"], default="all")
parser.add_argument("--workflow", choices=["dynamic-dag"], default="dynamic-dag")
parser.add_argument("--level3-timeout", type=int, default=720)
```

Add task construction after Level 2:

```python
        level2_ok = all(item.get("pass") for item in summaries if item.get("scenario") == "level2")
        if args.level == "level3" or (args.level == "all" and level1_ok and level2_ok):
            level3_tasks: list[dict[str, Any]] = []
            for short_model, model, endpoint, _provider, model_id in cases:
                case_name = f"level3-{short_model}-{endpoint}"
                level3_tasks.append(
                    {
                        "case_name": case_name,
                        "prompt": level3_dynamic_dag_prompt(case_name),
                        "model_id": model_id,
                        "endpoint": endpoint,
                        "timeout": args.level3_timeout,
                        "scenario": "level3_dynamic_dag",
                        "preserve_cli_tools": not args.minimal_cli_tools,
                        "subagent_mode": args.subagent_mode,
                        "main_retry_attempts": args.main_retry_attempts,
                    }
                )
            level3_tasks = repeated_tasks(level3_tasks, args.repeat)
            summaries.extend(run_e2e_tasks(run_dir, port, level3_tasks, args.jobs, args.ephemeral_cli))
```

Update the analyzer dispatch in `run_codex_case` so `scenario == "level3_dynamic_dag"` calls `analyze_level3_dynamic_dag(case)`.

- [ ] **Step 6: Run parser tests**

Run:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest tests.test_level12_e2e_parser -v
```

Expected: all parser tests pass.

- [ ] **Step 7: Commit E2E runner slice**

```powershell
git add diagnostics/subagent-e2e/run_level12_e2e.py tests/test_level12_e2e_parser.py
git commit -m "feat: add level3 dynamic dag e2e gate"
```

---

### Task 5: Verification and E2E Gates

**Files:**
- No source edits unless a verified defect is found.
- Diagnostic output under `diagnostics/subagent-e2e/` remains uncommitted.

**Interfaces:**
- Consumes all previous tasks.
- Produces Level 3 verification evidence.

- [ ] **Step 1: Run focused unit suite**

Run:

```powershell
$env:PYTHONPATH='src-python'; python -m unittest tests.test_subagent_protocol tests.test_subagent_policy tests.test_subagent_scheduler tests.test_subagent_dynamic_dag tests.test_subagent_state tests.test_routing tests.test_proxy_event_logging tests.test_level12_e2e_parser -v
```

Expected: all tests pass.

- [ ] **Step 2: Run Level 1 focused regression**

Run:

```powershell
$env:PYTHONPATH='src-python'; python diagnostics\subagent-e2e\run_level12_e2e.py --level level1 --models glm52,k2_7,m3 --endpoints responses,chat --scenarios single --level1-timeout 420 --jobs 3 --repeat 3 --subagent-mode assisted --main-retry-attempts 1 --upstream-base-url http://127.0.0.1:9099/v1
```

Expected: all selected Level 1 single-agent cases pass. Any failure must be classified before Level 3 code is changed.

- [ ] **Step 3: Run Level 2 focused regression**

Run:

```powershell
$env:PYTHONPATH='src-python'; python diagnostics\subagent-e2e\run_level12_e2e.py --level level2 --models glm52,k2_7,m3 --endpoints responses,chat --level2-timeout 720 --jobs 3 --repeat 1 --subagent-mode assisted --main-retry-attempts 1 --upstream-base-url http://127.0.0.1:9099/v1
```

Expected: all selected Level 2 cases pass.

- [ ] **Step 4: Run Level 3 focused single-model gate**

Run:

```powershell
$env:PYTHONPATH='src-python'; python diagnostics\subagent-e2e\run_level12_e2e.py --level level3 --workflow dynamic-dag --models m3 --endpoints responses --level3-timeout 720 --jobs 3 --repeat 3 --subagent-mode assisted --main-retry-attempts 1 --upstream-base-url http://127.0.0.1:9099/v1
```

Expected: `3/3 PASS`. If not, inspect parsed summaries before editing source:

```powershell
Get-ChildItem diagnostics\subagent-e2e\level12-e2e-* | Sort-Object LastWriteTime -Descending | Select-Object -First 1
```

- [ ] **Step 5: Run Level 3 all-model focused endpoint gate**

Run:

```powershell
$env:PYTHONPATH='src-python'; python diagnostics\subagent-e2e\run_level12_e2e.py --level level3 --workflow dynamic-dag --models glm52,k2_7,m3 --endpoints responses --level3-timeout 720 --jobs 3 --repeat 3 --subagent-mode assisted --main-retry-attempts 1 --upstream-base-url http://127.0.0.1:9099/v1
```

Expected: `9/9 PASS` or only provider stream flakes where lifecycle, nodes, and final checks are correct.

- [ ] **Step 6: Run full Level 3 matrix**

Run:

```powershell
$env:PYTHONPATH='src-python'; python diagnostics\subagent-e2e\run_level12_e2e.py --level level3 --workflow dynamic-dag --models glm52,k2_7,m3 --endpoints responses,chat --level3-timeout 720 --jobs 3 --repeat 3 --subagent-mode assisted --main-retry-attempts 1 --upstream-base-url http://127.0.0.1:9099/v1
```

Expected: `18/18 PASS` or classified provider stream flakes with protocol and scheduler checks correct.

- [ ] **Step 7: Commit final verification notes only if source changed after Task 4**

If source changes were needed during verification:

```powershell
git add src-python diagnostics/subagent-e2e/run_level12_e2e.py tests
git commit -m "fix: stabilize level3 dynamic dag workflow"
```

Do not stage generated `diagnostics/subagent-e2e/level12-e2e-*` run directories.
