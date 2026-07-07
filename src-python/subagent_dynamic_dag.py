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
        "You are a Level 3 Dynamic DAG worker.\n"
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
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, MappingABC):
                    value = part.get("text")
                    if isinstance(value, str):
                        parts.append(value)
        value = item.get("text")
        if isinstance(value, str):
            parts.append(value)
    return "\n".join(parts)
