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
    if actions:
        return actions
    return _protocol_actions(protocol)


def bounded_workflow_from_exact_prompts(
    prompts: list[str], assigned_agent_ids: list[str] | None = None
) -> WorkflowState:
    assigned = assigned_agent_ids or []
    nodes: dict[str, WorkflowNode] = {}
    previous_node_id: str | None = None
    for index, prompt in enumerate(prompts):
        node_id = f"bounded-{index + 1}"
        assigned_agent_id = assigned[index] if index < len(assigned) else None
        nodes[node_id] = WorkflowNode(
            node_id=node_id,
            prompt=prompt,
            dependencies=(),
            assigned_agent_id=assigned_agent_id,
            metadata={"source": "bounded_exact_prompt"},
        )
        previous_node_id = node_id
    return WorkflowState(nodes=nodes)


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
                    "message": (
                        "Return exactly the output requested in your original prompt, "
                        "with no prose or markdown.\n"
                        f"Original prompt:\n{agent.prompt}"
                    ),
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
